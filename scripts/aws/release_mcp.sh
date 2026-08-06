#!/usr/bin/env bash
# mcp 署名リリース 5 段を「MFA 1 回」で完走させるランチャー。
#
# 使い方:
#   bash scripts/aws/release_mcp.sh --dry-run                  # 投げる env の検証のみ（MFA 不要）
#   bash scripts/aws/release_mcp.sh "APPROVED: <承認理由> ..."  # 本番実行（MFA 1 回）
#
# 設計（2026-08-06 の 3 連続失敗の再発防止）:
#   - 各段の StartBuild は IAM Condition で「渡してよい環境変数の集合」が完全固定
#     （ForAllValues:StringEquals + Null:false。過不足どちらでも AccessDenied）。
#     本スクリプトは投入前に許可集合と突き合わせ、ずれていれば投げずに落とす。
#   - 許可集合の写し元:
#       段1: role teamagent-dev-approval-caller の inline policy（3 本）
#       段2-5: managed policy teamagent-dev-codebuild-launcher-start の各 Sid
#     ポリシー変更時は `aws iam get-policy-version` で読み直してここを更新すること。
#   - MFA は統制の本体なので回避しない。TOTP は同一コードの 30 秒窓内なら
#     複数回 assume できるため、2 ロール分をまとめて取得して 1 回入力にする。
#   - 段間の受け渡し値は前段ログではなく S3 の決定的キー構造から導出する
#     （ログは buildspec 本文をエコーするだけで実値を出さない。実測 2026-08-06）:
#       source-declarations/mcp/<commit>/<src_sha>/<SOURCE_ARCHIVE_VERSION_ID>.json
#       release-receipts/mcp/<commit>/<receipt_sha>.json
#   - 固定値（契約 SHA256 / APP_HTML 系）は infra/codebuild/ の契約ファイルから
#     実測ハッシュで再計算する。ハードコードとの不一致は投げる前に検出できる。
#   - 本スクリプトは「焼く」まで。本番タスク定義の差し替えは行わない。

set -euo pipefail

REGION=ap-northeast-1
ACCOUNT=718959508629
PROFILE="${RELEASE_MCP_PROFILE:-aiiadev}"
MFA_ARN="arn:aws:iam::${ACCOUNT}:mfa/N2"
EVIDENCE_BUCKET=teamagent-dev-image-release-evidence
POLL_SECONDS=20

ROLE_APPROVAL="arn:aws:iam::${ACCOUNT}:role/teamagent-dev-approval-caller"
SESS_APPROVAL="teamagent-approval-caller"
ROLE_LAUNCHER="arn:aws:iam::${ACCOUNT}:role/teamagent-dev-codebuild-launcher"
SESS_LAUNCHER="teamagent-build-launcher"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RELEASE_CONTRACT="$REPO_ROOT/infra/codebuild/teamagent_core_media_release_contract.json"
MANIFEST_CONTRACT="$REPO_ROOT/infra/codebuild/teamagent_runtime_contract.json"

die() { echo "FATAL: $*" >&2; exit 1; }
info() { echo "[release_mcp] $*"; }

command -v aws >/dev/null || die "aws CLI が見つかりません"
command -v python3 >/dev/null || die "python3 が見つかりません"
[ -f "$RELEASE_CONTRACT" ] || die "契約ファイルがありません: $RELEASE_CONTRACT"
[ -f "$MANIFEST_CONTRACT" ] || die "契約ファイルがありません: $MANIFEST_CONTRACT"

# ---------- 許可集合（IAM ポリシーの写し・2026-08-06 実測） ----------
ALLOWED_STAGE1="APPROVAL_DECISION EXPECTED_COMMIT FORCED_ROLLBACK_EVIDENCE_JSON"
ALLOWED_STAGE2="APPROVAL_PAYLOAD_BUCKET APPROVAL_PAYLOAD_KEY APPROVAL_PAYLOAD_SHA256 APPROVAL_PAYLOAD_VERSION_ID APPROVAL_SIGNATURE_BUCKET APPROVAL_SIGNATURE_KEY APPROVAL_SIGNATURE_SHA256 APPROVAL_SIGNATURE_VERSION_ID APPROVAL_SIGNING_KEY_ARN EXPECTED_BASE_OID EXPECTED_COMMIT RELEASE_CONTRACT_SHA256 SOURCE_MANIFEST_CONTRACT_SHA256"
ALLOWED_STAGE3="APPROVAL_PAYLOAD_BUCKET APPROVAL_PAYLOAD_KEY APPROVAL_PAYLOAD_SHA256 APPROVAL_PAYLOAD_VERSION_ID APPROVAL_SIGNATURE_BUCKET APPROVAL_SIGNATURE_KEY APPROVAL_SIGNATURE_SHA256 APPROVAL_SIGNATURE_VERSION_ID APPROVAL_SIGNING_KEY_ARN APP_HTML_SHA256 APP_HTML_VERSION_ID APP_PROVENANCE_SHA256 BAKED_APP_HTML_SHA256 BAKED_APP_HTML_VERSION_ID BUILD_INPUTS_SHA256 GIT_BRANCH GIT_COMMIT RELEASE_CONTRACT_SHA256 SOURCE_ARCHIVE_VERSION_ID SOURCE_DECLARATION_KEY SOURCE_DECLARATION_SHA256 SOURCE_DECLARATION_SIGNATURE_KEY SOURCE_DECLARATION_SIGNATURE_VERSION_ID SOURCE_DECLARATION_VERSION_ID SOURCE_MANIFEST_CONTRACT_SHA256 VAULT_MANIFEST_SHA256"
ALLOWED_STAGE4="APPROVAL_PAYLOAD_BUCKET APPROVAL_PAYLOAD_KEY APPROVAL_PAYLOAD_SHA256 APPROVAL_PAYLOAD_VERSION_ID APPROVAL_SIGNATURE_BUCKET APPROVAL_SIGNATURE_KEY APPROVAL_SIGNATURE_SHA256 APPROVAL_SIGNATURE_VERSION_ID APPROVAL_SIGNING_KEY_ARN BUILD_ID CONTRACT_SHA256 PIPELINE PROMOTION_CHANNEL SOURCE_COMMIT SOURCE_EVIDENCE_BUCKET SOURCE_EVIDENCE_KEY SOURCE_EVIDENCE_SHA256 SOURCE_EVIDENCE_SIGNATURE_KEY SOURCE_EVIDENCE_SIGNATURE_VERSION_ID SOURCE_EVIDENCE_VERSION_ID SUBJECTS_JSON"
ALLOWED_STAGE5="APPROVAL_PAYLOAD_BUCKET APPROVAL_PAYLOAD_KEY APPROVAL_PAYLOAD_SHA256 APPROVAL_PAYLOAD_VERSION_ID APPROVAL_SIGNATURE_BUCKET APPROVAL_SIGNATURE_KEY APPROVAL_SIGNATURE_SHA256 APPROVAL_SIGNATURE_VERSION_ID APPROVAL_SIGNING_KEY_ARN CONTRACT_SHA256 PIPELINE PROMOTION_CHANNEL RECEIPT_KEY RECEIPT_SIGNATURE_KEY RECEIPT_SIGNATURE_VERSION_ID RECEIPT_VERSION_ID SOURCE_COMMIT"

# ---------- 契約から固定値を導出（ハードコードしない） ----------
RELEASE_CONTRACT_SHA256="$(shasum -a 256 "$RELEASE_CONTRACT" | cut -d' ' -f1)"
SOURCE_MANIFEST_CONTRACT_SHA256="$(shasum -a 256 "$MANIFEST_CONTRACT" | cut -d' ' -f1)"

contract() { python3 -c "import json;d=json.load(open('$RELEASE_CONTRACT'));print(d$1)"; }
APP_HTML_VERSION_ID="$(contract "['app_html']['production']['app_html_s3_version_id']")"
APP_HTML_SHA256="$(contract "['app_html']['production']['app_html_sha256']")"
VAULT_MANIFEST_SHA256="$(contract "['app_html']['production']['vault_manifest_sha256']")"
BUILD_INPUTS_SHA256="$(contract "['app_html']['production']['build_inputs_sha256']")"
BAKED_APP_HTML_VERSION_ID="$(contract "['app_html']['baked_fallback']['s3_version_id']")"
BAKED_APP_HTML_SHA256="$(contract "['app_html']['baked_fallback']['sha256']")"

# APP_PROVENANCE_SHA256 と KMS 鍵はポリシー Condition の固定値（契約 JSON に無い）。
# ポリシー実物 teamagent-dev-codebuild-launcher-start から写した（2026-08-06）。
APP_PROVENANCE_SHA256="f0d40e7986fcd54d68f9e1ceed9a9987af23a72f5cc4a608fee5819b078a5008"
APPROVAL_SIGNING_KEY_ARN="arn:aws:kms:ap-northeast-1:${ACCOUNT}:key/8ef3c43c-3fff-4f2e-9d92-e493a3a923b1"

GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"
# EXPECTED_BASE_OID は「親コミット」ではなく origin/main の先端。
# publisher は EXPECTED_BASE_REF="refs/heads/main" を fresh に照合し、
# さらに merge-base(main, release) == main（main が祖先）を要求する。
# HEAD^1 を渡すと 'fresh protected remote base differs from expected base' で落ちる
# （2026-08-06 実測）。
git -C "$REPO_ROOT" fetch -q origin main
GIT_BASE_OID="$(git -C "$REPO_ROOT" rev-parse origin/main)"
GIT_BRANCH=dev

# ---------- ヘルパ ----------
# env JSON（[{name,value,type}...]）の name 集合が許可集合と完全一致するか検査。
validate_env() { # $1=stage名 $2=許可集合(空白区切り) $3=env JSON ファイル
  python3 - "$1" "$3" <<PYEOF
import json, sys
allowed = set("""$2""".split())
got = {e["name"] for e in json.load(open(sys.argv[2]))}
extra, missing = sorted(got - allowed), sorted(allowed - got)
if extra or missing:
    print(f"FATAL: {sys.argv[1]} の環境変数が許可集合とずれています", file=sys.stderr)
    if extra:   print(f"  余分: {extra}", file=sys.stderr)
    if missing: print(f"  不足: {missing}", file=sys.stderr)
    sys.exit(1)
print(f"  {sys.argv[1]}: {len(got)} 本 = 許可集合と完全一致")
PYEOF
}

mkenv() { # 標準入力の "NAME=VALUE" 行を env JSON にして $1 へ
  python3 -c "
import json, sys
env = []
for line in sys.stdin:
    line = line.rstrip('\n')
    if not line: continue
    k, _, v = line.partition('=')
    env.append({'name': k, 'value': v, 'type': 'PLAINTEXT'})
json.dump(env, open(sys.argv[1], 'w'), ensure_ascii=False)
" "$1"
}

s3_version_id() { # $1=key → 最新 VersionId
  aws s3api list-object-versions --bucket "$EVIDENCE_BUCKET" --prefix "$1" \
    --query "Versions[?Key=='$1' && IsLatest].VersionId | [0]" --output text
}

s3_sha256() { # $1=key $2=version-id → sha256
  local tmp; tmp="$(mktemp)"
  aws s3api get-object --bucket "$EVIDENCE_BUCKET" --key "$1" --version-id "$2" "$tmp" >/dev/null
  shasum -a 256 "$tmp" | cut -d' ' -f1
  rm -f "$tmp"
}

start_and_wait() { # $1=project $2=env json $3=source-version("-"で無指定) $4=起動用creds("AK\tSK\tTK")
  local project="$1" envfile="$2" srcver="$3" creds="$4"
  local args=(--project-name "$project" --environment-variables-override "file://$envfile"
              --region "$REGION" --query 'build.id' --output text)
  [ "$srcver" != "-" ] && args+=(--source-version "$srcver")
  # StartBuild だけロール認証で撃つ。待機・ログ・S3 は MFA セッション（AIIAdev）で行う
  # （ロール側に読み取り権限があるとは限らないため。AIIAdev の読み取りは本日実績あり）。
  local bid; bid="$(AWS_ACCESS_KEY_ID="$(echo "$creds" | cut -f1)" \
    AWS_SECRET_ACCESS_KEY="$(echo "$creds" | cut -f2)" \
    AWS_SESSION_TOKEN="$(echo "$creds" | cut -f3)" \
    env -u AWS_PROFILE aws codebuild start-build "${args[@]}")"
  info "$project 起動: $bid"
  while :; do
    sleep "$POLL_SECONDS"
    local st; st="$(aws codebuild batch-get-builds --ids "$bid" --region "$REGION" \
      --query 'builds[0].buildStatus' --output text)"
    case "$st" in
      SUCCEEDED) info "$project SUCCEEDED"; LAST_BUILD_ID="$bid"; return 0;;
      IN_PROGRESS) ;;
      *)
        echo "FATAL: $project が $st で終了しました。失敗ログ:" >&2
        local lg ls
        lg="$(aws codebuild batch-get-builds --ids "$bid" --region "$REGION" --query 'builds[0].logs.groupName' --output text)"
        ls="$(aws codebuild batch-get-builds --ids "$bid" --region "$REGION" --query 'builds[0].logs.streamName' --output text)"
        aws logs get-log-events --log-group-name "$lg" --log-stream-name "$ls" --region "$REGION" \
          --limit 40 --query 'events[].message' --output text 2>/dev/null | tr '\t' '\n' | \
          grep -iE "FATAL|error|fail" | tail -12 >&2 || true
        exit 1;;
    esac
  done
}

# ---------- 引数 ----------
DRY_RUN=0
DECISION=""
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=1
elif [ -n "${1:-}" ]; then
  DECISION="$1"
  case "$DECISION" in APPROVED:*) ;; *) die "承認文は 'APPROVED: ' で始めてください";; esac
else
  die "使い方: $0 --dry-run | $0 \"APPROVED: <承認理由>\""
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

info "コミット: $GIT_COMMIT (base: $GIT_BASE_OID)"
info "契約: RELEASE=$RELEASE_CONTRACT_SHA256"
info "契約: MANIFEST=$SOURCE_MANIFEST_CONTRACT_SHA256"

# ---------- dry-run: 5 段全部の env 名集合を許可集合と照合 ----------
if [ "$DRY_RUN" = 1 ]; then
  info "=== dry-run: 各段の環境変数を許可集合と照合します（MFA 不要・何も起動しません） ==="
  D=DUMMY
  mkenv "$WORK/s1.json" <<EOF
APPROVAL_DECISION=APPROVED: dry-run
EXPECTED_COMMIT=$GIT_COMMIT
FORCED_ROLLBACK_EVIDENCE_JSON=$D
EOF
  validate_env "段1 approval-publisher" "$ALLOWED_STAGE1" "$WORK/s1.json"

  mkenv "$WORK/s2.json" <<EOF
EXPECTED_COMMIT=$GIT_COMMIT
EXPECTED_BASE_OID=$GIT_BASE_OID
SOURCE_MANIFEST_CONTRACT_SHA256=$SOURCE_MANIFEST_CONTRACT_SHA256
RELEASE_CONTRACT_SHA256=$RELEASE_CONTRACT_SHA256
APPROVAL_PAYLOAD_BUCKET=$D
APPROVAL_PAYLOAD_KEY=$D
APPROVAL_PAYLOAD_VERSION_ID=$D
APPROVAL_PAYLOAD_SHA256=$D
APPROVAL_SIGNATURE_BUCKET=$D
APPROVAL_SIGNATURE_KEY=$D
APPROVAL_SIGNATURE_VERSION_ID=$D
APPROVAL_SIGNATURE_SHA256=$D
APPROVAL_SIGNING_KEY_ARN=$APPROVAL_SIGNING_KEY_ARN
EOF
  validate_env "段2 mcp-source-publisher" "$ALLOWED_STAGE2" "$WORK/s2.json"

  mkenv "$WORK/s3.json" <<EOF
GIT_COMMIT=$GIT_COMMIT
GIT_BRANCH=$GIT_BRANCH
SOURCE_ARCHIVE_VERSION_ID=$D
SOURCE_DECLARATION_KEY=$D
SOURCE_DECLARATION_VERSION_ID=$D
SOURCE_DECLARATION_SHA256=$D
SOURCE_DECLARATION_SIGNATURE_KEY=$D
SOURCE_DECLARATION_SIGNATURE_VERSION_ID=$D
APP_HTML_VERSION_ID=$APP_HTML_VERSION_ID
APP_HTML_SHA256=$APP_HTML_SHA256
VAULT_MANIFEST_SHA256=$VAULT_MANIFEST_SHA256
BUILD_INPUTS_SHA256=$BUILD_INPUTS_SHA256
BAKED_APP_HTML_VERSION_ID=$BAKED_APP_HTML_VERSION_ID
BAKED_APP_HTML_SHA256=$BAKED_APP_HTML_SHA256
APP_PROVENANCE_SHA256=$APP_PROVENANCE_SHA256
SOURCE_MANIFEST_CONTRACT_SHA256=$SOURCE_MANIFEST_CONTRACT_SHA256
RELEASE_CONTRACT_SHA256=$RELEASE_CONTRACT_SHA256
APPROVAL_PAYLOAD_BUCKET=$D
APPROVAL_PAYLOAD_KEY=$D
APPROVAL_PAYLOAD_VERSION_ID=$D
APPROVAL_PAYLOAD_SHA256=$D
APPROVAL_SIGNATURE_BUCKET=$D
APPROVAL_SIGNATURE_KEY=$D
APPROVAL_SIGNATURE_VERSION_ID=$D
APPROVAL_SIGNATURE_SHA256=$D
APPROVAL_SIGNING_KEY_ARN=$APPROVAL_SIGNING_KEY_ARN
EOF
  validate_env "段3 image-builder" "$ALLOWED_STAGE3" "$WORK/s3.json"

  mkenv "$WORK/s4.json" <<EOF
PIPELINE=mcp
PROMOTION_CHANNEL=verified-candidate
SOURCE_COMMIT=$GIT_COMMIT
CONTRACT_SHA256=$RELEASE_CONTRACT_SHA256
SOURCE_EVIDENCE_BUCKET=$EVIDENCE_BUCKET
SOURCE_EVIDENCE_KEY=$D
SOURCE_EVIDENCE_VERSION_ID=$D
SOURCE_EVIDENCE_SHA256=$D
SOURCE_EVIDENCE_SIGNATURE_KEY=$D
SOURCE_EVIDENCE_SIGNATURE_VERSION_ID=$D
BUILD_ID=$D
SUBJECTS_JSON=$D
APPROVAL_PAYLOAD_BUCKET=$D
APPROVAL_PAYLOAD_KEY=$D
APPROVAL_PAYLOAD_VERSION_ID=$D
APPROVAL_PAYLOAD_SHA256=$D
APPROVAL_SIGNATURE_BUCKET=$D
APPROVAL_SIGNATURE_KEY=$D
APPROVAL_SIGNATURE_VERSION_ID=$D
APPROVAL_SIGNATURE_SHA256=$D
APPROVAL_SIGNING_KEY_ARN=$APPROVAL_SIGNING_KEY_ARN
EOF
  validate_env "段4 image-attestor" "$ALLOWED_STAGE4" "$WORK/s4.json"

  mkenv "$WORK/s5.json" <<EOF
PIPELINE=mcp
PROMOTION_CHANNEL=verified-candidate
SOURCE_COMMIT=$GIT_COMMIT
CONTRACT_SHA256=$RELEASE_CONTRACT_SHA256
RECEIPT_KEY=$D
RECEIPT_VERSION_ID=$D
RECEIPT_SIGNATURE_KEY=$D
RECEIPT_SIGNATURE_VERSION_ID=$D
APPROVAL_PAYLOAD_BUCKET=$D
APPROVAL_PAYLOAD_KEY=$D
APPROVAL_PAYLOAD_VERSION_ID=$D
APPROVAL_PAYLOAD_SHA256=$D
APPROVAL_SIGNATURE_BUCKET=$D
APPROVAL_SIGNATURE_KEY=$D
APPROVAL_SIGNATURE_VERSION_ID=$D
APPROVAL_SIGNATURE_SHA256=$D
APPROVAL_SIGNING_KEY_ARN=$APPROVAL_SIGNING_KEY_ARN
EOF
  validate_env "段5 image-promoter" "$ALLOWED_STAGE5" "$WORK/s5.json"
  info "=== dry-run OK: 5 段すべて許可集合と完全一致 ==="
  exit 0
fi

# ---------- 本番: MFA 1 回で 2 ロール分の一時認証を取得 ----------
# AWS の TOTP コードは 1 回しか使えない（同一コードの再利用は
# "MultiFactorAuthentication failed" で拒否される。2026-08-06 実測）。
# そのため「MFA 付きセッション」を GetSessionToken で 1 回だけ確立し、
# そのセッション（aws:MultiFactorAuthPresent=true を帯びる）から
# 両ロールを MFA なしで assume する。これが AWS の定石。
read -r -s -p "MFAコード（1回だけ入力）: " MFA_CODE; echo
[ -n "$MFA_CODE" ] || die "MFA コードが空です"

info "MFA 付きセッションを確立中..."
BASE="$(aws sts get-session-token \
  --serial-number "$MFA_ARN" --token-code "$MFA_CODE" \
  --duration-seconds 3600 \
  --profile "$PROFILE" --region "$REGION" \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)" \
  || die "MFA セッションの確立に失敗（コードの打ち間違い・時計ずれの可能性）"
unset MFA_CODE
BASE_AK="$(echo "$BASE" | cut -f1)"
BASE_SK="$(echo "$BASE" | cut -f2)"
BASE_TK="$(echo "$BASE" | cut -f3)"

assume() { # $1=role-arn $2=session-name → "AK SK TOKEN"（MFA セッション経由・コード不要）
  # AWS_PROFILE を空文字で潰すと「空名プロファイル指定」と解釈され失敗する。env -u で外す。
  AWS_ACCESS_KEY_ID="$BASE_AK" AWS_SECRET_ACCESS_KEY="$BASE_SK" AWS_SESSION_TOKEN="$BASE_TK" \
  env -u AWS_PROFILE aws sts assume-role --role-arn "$1" --role-session-name "$2" \
    --region "$REGION" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text
}

info "承認ロールを引き受け中..."
CRED_A="$(assume "$ROLE_APPROVAL" "$SESS_APPROVAL")" || die "承認ロールの assume に失敗"
info "起動ロールを引き受け中..."
CRED_L="$(assume "$ROLE_LAUNCHER" "$SESS_LAUNCHER")" || die "起動ロールの assume に失敗"

# 以降の既定認証は MFA セッション（AIIAdev）。読み取り系はすべてこちらで行う。
AWS_ACCESS_KEY_ID="$BASE_AK"; AWS_SECRET_ACCESS_KEY="$BASE_SK"; AWS_SESSION_TOKEN="$BASE_TK"
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
unset AWS_PROFILE 2>/dev/null || true

# ---------- 段1: 承認発行 ----------
FORCED_ROLLBACK_EVIDENCE_JSON='{"gate_version":1,"state":"PROVISIONAL_INITIAL_RELEASE","provisional":true}'
# 注: 本番の実値は過去の承認ビルドから写す運用。異なる場合は下の値を差し替える。
PREV_FRJ="$(aws codebuild batch-get-builds --profile "$PROFILE" --region "$REGION" \
  --ids "$(aws codebuild list-builds-for-project --project-name teamagent-dev-approval-publisher \
      --profile "$PROFILE" --region "$REGION" --max-items 1 --query 'ids' --output json | \
      python3 -c 'import json,sys;print(json.load(sys.stdin)[0])')" \
  --query "builds[0].environment.environmentVariables[?name=='FORCED_ROLLBACK_EVIDENCE_JSON'].value | [0]" \
  --output text 2>/dev/null || true)"
[ -n "$PREV_FRJ" ] && [ "$PREV_FRJ" != "None" ] && FORCED_ROLLBACK_EVIDENCE_JSON="$PREV_FRJ"

mkenv "$WORK/s1.json" <<EOF
APPROVAL_DECISION=$DECISION
EXPECTED_COMMIT=$GIT_COMMIT
FORCED_ROLLBACK_EVIDENCE_JSON=$FORCED_ROLLBACK_EVIDENCE_JSON
EOF
validate_env "段1 approval-publisher" "$ALLOWED_STAGE1" "$WORK/s1.json"

start_and_wait teamagent-dev-approval-publisher "$WORK/s1.json" refs/heads/dev "$CRED_A"
APPROVAL_BUILD_ID="$LAST_BUILD_ID"

# 承認レコード（{"mcp":{"payload":{...},"signature":{...}}}）をログから取得（実測済み形式）
LG="$(aws codebuild batch-get-builds --ids "$APPROVAL_BUILD_ID" --region "$REGION" --query 'builds[0].logs.groupName' --output text)"
LS="$(aws codebuild batch-get-builds --ids "$APPROVAL_BUILD_ID" --region "$REGION" --query 'builds[0].logs.streamName' --output text)"
aws logs get-log-events --log-group-name "$LG" --log-stream-name "$LS" --region "$REGION" \
  --limit 300 --query 'events[].message' --output text | grep -o '{"mcp":{.*' | tail -1 | \
  python3 -c "import sys,json; d=json.loads(sys.stdin.read().split('\t')[0]); json.dump(d['mcp'], open('$WORK/approval.json','w'))"
[ -s "$WORK/approval.json" ] || die "承認レコードをログから取得できませんでした"

ap() { python3 -c "import json;print(json.load(open('$WORK/approval.json'))$1)"; }
AP_B="$(ap "['payload']['bucket']")";   AP_K="$(ap "['payload']['key']")"
AP_V="$(ap "['payload']['version_id']")"; AP_S="$(ap "['payload']['sha256']")"
SG_B="$(ap "['signature']['bucket']")"; SG_K="$(ap "['signature']['key']")"
SG_V="$(ap "['signature']['version_id']")"; SG_S="$(ap "['signature']['sha256']")"
info "承認レコード: $AP_K (v=$AP_V)"

APPROVAL_BLOCK="APPROVAL_PAYLOAD_BUCKET=$AP_B
APPROVAL_PAYLOAD_KEY=$AP_K
APPROVAL_PAYLOAD_VERSION_ID=$AP_V
APPROVAL_PAYLOAD_SHA256=$AP_S
APPROVAL_SIGNATURE_BUCKET=$SG_B
APPROVAL_SIGNATURE_KEY=$SG_K
APPROVAL_SIGNATURE_VERSION_ID=$SG_V
APPROVAL_SIGNATURE_SHA256=$SG_S
APPROVAL_SIGNING_KEY_ARN=$APPROVAL_SIGNING_KEY_ARN"

# ---------- 段2: ソース公開 ----------
mkenv "$WORK/s2.json" <<EOF
EXPECTED_COMMIT=$GIT_COMMIT
EXPECTED_BASE_OID=$GIT_BASE_OID
SOURCE_MANIFEST_CONTRACT_SHA256=$SOURCE_MANIFEST_CONTRACT_SHA256
RELEASE_CONTRACT_SHA256=$RELEASE_CONTRACT_SHA256
$APPROVAL_BLOCK
EOF
validate_env "段2 mcp-source-publisher" "$ALLOWED_STAGE2" "$WORK/s2.json"
start_and_wait teamagent-dev-mcp-source-publisher "$WORK/s2.json" "$GIT_COMMIT" "$CRED_L"

# 段2 の出力を S3 のキー構造から導出:
#   source-declarations/mcp/<commit>/<src_sha>/<SOURCE_ARCHIVE_VERSION_ID>.json (+.sig)
DECL_KEY="$(aws s3api list-objects-v2 --bucket "$EVIDENCE_BUCKET" \
  --prefix "source-declarations/mcp/$GIT_COMMIT/" \
  --query "sort_by(Contents,&LastModified)[?ends_with(Key,'.json')] | [-1].Key" --output text)"
[ -n "$DECL_KEY" ] && [ "$DECL_KEY" != "None" ] || die "source declaration が S3 に見つかりません"
SIG_KEY="$DECL_KEY.sig"
SOURCE_ARCHIVE_VERSION_ID="$(basename "$DECL_KEY" .json)"
DECL_VID="$(s3_version_id "$DECL_KEY")"
SIG_VID="$(s3_version_id "$SIG_KEY")"
DECL_SHA="$(s3_sha256 "$DECL_KEY" "$DECL_VID")"
info "宣言: $DECL_KEY (archive_vid=$SOURCE_ARCHIVE_VERSION_ID)"

# ---------- 段3: イメージビルド ----------
mkenv "$WORK/s3.json" <<EOF
GIT_COMMIT=$GIT_COMMIT
GIT_BRANCH=$GIT_BRANCH
SOURCE_ARCHIVE_VERSION_ID=$SOURCE_ARCHIVE_VERSION_ID
SOURCE_DECLARATION_KEY=$DECL_KEY
SOURCE_DECLARATION_VERSION_ID=$DECL_VID
SOURCE_DECLARATION_SHA256=$DECL_SHA
SOURCE_DECLARATION_SIGNATURE_KEY=$SIG_KEY
SOURCE_DECLARATION_SIGNATURE_VERSION_ID=$SIG_VID
APP_HTML_VERSION_ID=$APP_HTML_VERSION_ID
APP_HTML_SHA256=$APP_HTML_SHA256
VAULT_MANIFEST_SHA256=$VAULT_MANIFEST_SHA256
BUILD_INPUTS_SHA256=$BUILD_INPUTS_SHA256
BAKED_APP_HTML_VERSION_ID=$BAKED_APP_HTML_VERSION_ID
BAKED_APP_HTML_SHA256=$BAKED_APP_HTML_SHA256
APP_PROVENANCE_SHA256=$APP_PROVENANCE_SHA256
SOURCE_MANIFEST_CONTRACT_SHA256=$SOURCE_MANIFEST_CONTRACT_SHA256
RELEASE_CONTRACT_SHA256=$RELEASE_CONTRACT_SHA256
$APPROVAL_BLOCK
EOF
validate_env "段3 image-builder" "$ALLOWED_STAGE3" "$WORK/s3.json"
start_and_wait teamagent-dev-image-builder "$WORK/s3.json" "$SOURCE_ARCHIVE_VERSION_ID" "$CRED_L"
BUILDER_BUILD_ID="$LAST_BUILD_ID"

# SUBJECTS_JSON: quarantine の candidate タグから digest を引いて構成（形式は昨日の実測どおり）
subjects() {
  python3 - "$GIT_COMMIT" <<'PYEOF'
import json, subprocess, sys
commit = sys.argv[1]
subs = []
for name, q, c, r in (
    ("core",  "teamagent-mcp-quarantine",          "teamagent-mcp-verified-candidates",          "teamagent-mcp"),
    ("media", "teamagent-media-worker-quarantine", "teamagent-media-worker-verified-candidates", "teamagent-media-worker"),
):
    tag = f"candidate-{commit}-{name}"
    out = subprocess.run(
        ["aws", "ecr", "describe-images", "--repository-name", q,
         "--image-ids", f"imageTag={tag}",
         "--query", "imageDetails[0].imageDigest", "--output", "text",
         "--region", "ap-northeast-1"],
        capture_output=True, text=True)
    digest = out.stdout.strip()
    if out.returncode != 0 or not digest or digest == "None":
        print(f"FATAL: {q} に {tag} が見つかりません", file=sys.stderr); sys.exit(1)
    subs.append({"name": name, "quarantine_repository": q,
                 "candidate_repository": c, "release_repository": r, "digest": digest})
print(json.dumps(subs, separators=(",", ":")))
PYEOF
}
SUBJECTS_JSON="$(subjects)" || exit 1
info "subjects: $SUBJECTS_JSON"

# ---------- 段4: 出自証明 ----------
mkenv "$WORK/s4.json" <<EOF
PIPELINE=mcp
PROMOTION_CHANNEL=verified-candidate
SOURCE_COMMIT=$GIT_COMMIT
CONTRACT_SHA256=$RELEASE_CONTRACT_SHA256
SOURCE_EVIDENCE_BUCKET=$EVIDENCE_BUCKET
SOURCE_EVIDENCE_KEY=$DECL_KEY
SOURCE_EVIDENCE_VERSION_ID=$DECL_VID
SOURCE_EVIDENCE_SHA256=$DECL_SHA
SOURCE_EVIDENCE_SIGNATURE_KEY=$SIG_KEY
SOURCE_EVIDENCE_SIGNATURE_VERSION_ID=$SIG_VID
BUILD_ID=$BUILDER_BUILD_ID
SUBJECTS_JSON=$SUBJECTS_JSON
$APPROVAL_BLOCK
EOF
validate_env "段4 image-attestor" "$ALLOWED_STAGE4" "$WORK/s4.json"
start_and_wait teamagent-dev-image-attestor "$WORK/s4.json" "-" "$CRED_L"

# 段4 の出力（レシート）を S3 から: release-receipts/mcp/<commit>/<sha>.json (+.sig)
RECEIPT_KEY="$(aws s3api list-objects-v2 --bucket "$EVIDENCE_BUCKET" \
  --prefix "release-receipts/mcp/$GIT_COMMIT/" \
  --query "sort_by(Contents,&LastModified)[?ends_with(Key,'.json')] | [-1].Key" --output text)"
[ -n "$RECEIPT_KEY" ] && [ "$RECEIPT_KEY" != "None" ] || die "release receipt が S3 に見つかりません"
RECEIPT_SIG_KEY="$RECEIPT_KEY.sig"
RECEIPT_VID="$(s3_version_id "$RECEIPT_KEY")"
RECEIPT_SIG_VID="$(s3_version_id "$RECEIPT_SIG_KEY")"
info "レシート: $RECEIPT_KEY"

# ---------- 段5: 昇格 ----------
mkenv "$WORK/s5.json" <<EOF
PIPELINE=mcp
PROMOTION_CHANNEL=verified-candidate
SOURCE_COMMIT=$GIT_COMMIT
CONTRACT_SHA256=$RELEASE_CONTRACT_SHA256
RECEIPT_KEY=$RECEIPT_KEY
RECEIPT_VERSION_ID=$RECEIPT_VID
RECEIPT_SIGNATURE_KEY=$RECEIPT_SIG_KEY
RECEIPT_SIGNATURE_VERSION_ID=$RECEIPT_SIG_VID
$APPROVAL_BLOCK
EOF
validate_env "段5 image-promoter" "$ALLOWED_STAGE5" "$WORK/s5.json"
start_and_wait teamagent-dev-image-promoter "$WORK/s5.json" "-" "$CRED_L"

# ---------- 完了: 昇格済み digest を表示（本番へは適用しない） ----------
info "=== 5 段完走 ==="
echo "$SUBJECTS_JSON" | python3 -c "
import json, sys
for s in json.load(sys.stdin):
    print(f\"  {s['release_repository']}: {s['digest']}\")"
info "本番への適用（タスク定義の差し替え）は別途行ってください。"
