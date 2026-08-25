#!/usr/bin/env bash
# Terraform が CLI 直デプロイ後の live ECS/EventBridge を巻き戻さないための
# TeamAgent dev専用 guarded Terraform workflow。
#
# - snapshot: live の non-secret desired-state 値を HCL snippet として表示（read-only）
# - preflight: candidate imageを実Fargateで検証し、短命なreceiptを発行する
# - review-plan: candidate migrationの全変更contractを副作用なしで抽出する
# - plan:     strict live同期またはreview済みmigrationの検証済みplanだけを保存する
# - verify:   plan/receipt/live/state が plan 作成時から不変か再確認（read-only）
# - apply:    trusted automation roleと共有lockの下でverify済みplanだけを適用
#
# これは協調運用のguardであり、AWS administrator/rootに対する認可境界ではない。
# 管理者が直接API/CLIを使って迂回できることは受容済みリスクとしてREADMEに明記する。
set -euo pipefail
umask 077

GUARD_VERSION="25"
EXPECTED_ACCOUNT_ID="718959508629"
REGION="ap-northeast-1"
PROJECT="teamagent"
ENVIRONMENT="dev"
EXPECTED_BACKEND_BUCKET="teamagent-tfstate-718959508629"
EXPECTED_BACKEND_KEY="teamagent/terraform.tfstate"
EXPECTED_BACKEND_DYNAMODB_TABLE="teamagent-tflock"
EXPECTED_WORKSPACE="default"
EXPECTED_ALARM_EMAIL="s-komata@vectorinc.co.jp"
EXPECTED_ALARM_EMAIL_SHA256="88c6452f9db04017250aa5728b4815bccb55b5ecc0b35b50a5234170dc08d1e6"
EXPECTED_ALARM_DESTINATION_STATE_SHA256="c942dbb7b97da1f4d9debb1ba241ee89bf8c1d951d8d75bdea3056850838ddc9"
LOG_VERSIONING_SETTLE_SECONDS=900
FORCED_ROLLBACK_DM_QA_MAX_SECONDS=300
FORCED_ROLLBACK_DM_QA_RECOVERY_RESERVE_SECONDS=30
TRUSTED_AUTOMATION_ROLE_NAME="teamagent-dev-terraform-runtime-automation"
TRUSTED_AUTOMATION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker"
TRUSTED_MEDIA_ATTESTOR_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-media-cutover-attestor/teamagent-media-cutover-attestor"
command -v realpath >/dev/null 2>&1 || {
  echo "★ realpath が必要です" >&2
  exit 1
}
SCRIPT_PATH="$(realpath "${BASH_SOURCE[0]}")"
[ -f "$SCRIPT_PATH" ] || {
  echo "★ guard scriptのcanonical pathを確認できません" >&2
  exit 1
}
REPO_ROOT="$(realpath "$(dirname "$SCRIPT_PATH")/../..")"
TF_DIR="$REPO_ROOT/infra/terraform"
GUARD_JQ_DIR="$REPO_ROOT/infra/deploy"
GUARD_JQ="$GUARD_JQ_DIR/terraform_runtime_guard.jq"
MIGRATION_FILE="$GUARD_JQ_DIR/terraform_runtime_migrations.json"
EVIDENCE_HELPER="$GUARD_JQ_DIR/runtime_evidence_guard.py"
MEDIA_APPLY_AUTHORIZER="$GUARD_JQ_DIR/media_cutover_apply_authorizer.py"
DEPLOYMENT_APPLY_FINALIZER="$GUARD_JQ_DIR/deployment_apply_finalizer.py"
PLAN_CONTRACT_HELPER="$GUARD_JQ_DIR/terraform_plan_contract.py"
FORCED_ROLLBACK_DM_QA_PROBE="$GUARD_JQ_DIR/forced_rollback_dm_qa_probe.py"
IMAGE_GATE_RUNNER="$GUARD_JQ_DIR/run_image_deployment_gate.sh"
RELEASE_EVIDENCE_HELPER="$REPO_ROOT/infra/codebuild/release_evidence.py"
IMAGE_DEPLOYMENT_CONSUMER_REGISTRY="$REPO_ROOT/infra/codebuild/image_deployment_consumers.json"
IMAGE_CONTEXT_HELPER="$TF_DIR/image_release_context.py"
APPLY_SUPERVISOR="$TF_DIR/terraform_apply_supervisor.py"
PLAN_STAGER="$TF_DIR/stage_saved_plan.py"
EVENTBRIDGE_APPLY_SAGA="$TF_DIR/eventbridge_apply_saga.py"
ECS_SERVICE_APPLY_SAGA="$TF_DIR/ecs_service_apply_saga.py"
HMAC_PLAN_HELPER="$REPO_ROOT/scripts/terraform_hmac_payload.py"
OPENCLAW_ROLLOUT_GATE="$REPO_ROOT/infra/openclaw/run-live-rollout-gates.mjs"
TMP_ROOT=""
AWS_BIN=""
AWS_BIN_SHA256=""
AWS_BIN_IDENTITY=""

usage() {
  cat <<'EOF'
usage:
  terraform_runtime_guard.sh snapshot [--evidence-json-out FILE]
  terraform_runtime_guard.sh attest-log-versioning --out RECEIPT
  terraform_runtime_guard.sh attest-log-readiness \
    --versioning-receipt FILE --spec FILE --artifact-dir DIR --out RECEIPT
  terraform_runtime_guard.sh issue-alarm-challenge --out CHALLENGE
  terraform_runtime_guard.sh sign-alarm-ack \
    --challenge FILE --out RECIPIENT_ACK
  terraform_runtime_guard.sh attest-alarm-delivery \
    --challenge FILE --recipient-ack FILE --out RECEIPT
  terraform_runtime_guard.sh advance-alarm-migration \
    --phase PHASE [--publisher-id ID] [--delivery-receipt FILE] --out RECEIPT
  terraform_runtime_guard.sh prepare-media-cutover \
    --migration ID --out CHALLENGE
  terraform_runtime_guard.sh attest-media-cutover \
    --migration ID --challenge FILE --out RECEIPT
  terraform_runtime_guard.sh preflight --migration ID --out RECEIPT
  terraform_runtime_guard.sh review-plan --var-file FILE --out REVIEWED_PLAN \
    --runtime-migration ID --preflight-receipt FILE \
    --alarm-delivery-receipt FILE --versioning-receipt FILE \
    --log-readiness-receipt FILE --alarm-migration-receipt FILE \
    [--prior-apply-receipt FILE]
  terraform_runtime_guard.sh plan --var-file FILE --out PLAN \
    (--runtime-sync | --runtime-migration ID --preflight-receipt FILE \
    --alarm-delivery-receipt FILE --versioning-receipt FILE \
    --log-readiness-receipt FILE --alarm-migration-receipt FILE \
    [--prior-apply-receipt FILE]) [--media-cutover-receipt FILE] \
    [--receipt FILE]
  terraform_runtime_guard.sh verify --plan PLAN [--receipt FILE]
  terraform_runtime_guard.sh adopt-plan --var-file FILE --out DIR
  terraform_runtime_guard.sh adopt-apply --out DIR --approve TOKEN
  terraform_runtime_guard.sh state-rebind-precheck --out DIR
  terraform_runtime_guard.sh state-rebind-apply --out DIR --var-file FILE --approve TOKEN
      state binding だけを live の exact revision へ付け替える（1 address ずつ atomic）。
      adopt の --out は repository 外を指定すること（repo 配下は fail-closed で拒否）。
      --approve は "I-HAVE-REVIEWED-THE-ADOPT-PLAN:<plan_sha256 先頭16桁>"。
  terraform_runtime_guard.sh authorize-media-apply --plan PLAN \
    [--receipt FILE] --apply-attempt-id UUID --out AUTHORIZATION
  terraform_runtime_guard.sh apply --plan PLAN [--receipt FILE] \
    [--media-authorization FILE --apply-attempt-id UUID] \
    [--forced-rollback-dm-qa-deadline-epoch EPOCH] \
    [--automation-identity-out FILE] \
    --out APPLY_RECEIPT

plan:
  --runtime-sync           主要5 runtimeとTikTok/x-buzz worker/dispatcherを完全照合
  --runtime-migration ID   git管理のexact one-time allowlistだけを使用
  --preflight-receipt FILE migration候補を実Fargateで検証した短命receipt
  --alarm-delivery-receipt FILE 実SNS配送を確認した短命・非機微receipt
  --versioning-receipt FILE producer-off/independent timestamp/900秒settle/cutoverを束縛する短命receipt
  --log-readiness-receipt FILE versioning 15分待機・配信・retention export証跡
  --alarm-migration-receipt FILE publisher別checkpointからlegacy retireまでのdurable chain
  --prior-apply-receipt FILE activationが要求する直前runtime migration成功apply receipt
  --media-cutover-receipt FILE legacy→generic media切替の独立KMS署名済みreceipt
  --receipt FILE           receipt 出力先（default: PLAN.runtime-guard.json）

review-plan:
  - migrationはenabled=false/reviewed_plan=nullのcandidateであること。
  - reviewed_inputsの固定intent、同じpreflight/外部receipt、同じlive/stateから
    exact reviewed_planを抽出する。DynamoDB intent作成やapply可能plan公開は行わない。
  - 抽出結果だけをmigration.reviewed_planへcommitしenabled=trueにした後、
    planを再実行するとexact contract一致が必須になる。

重要:
  - TeamAgent dev / account 718959508629 / ap-northeast-1 / 固定S3 backend専用。
  - snapshot/verifyはAWS read-only。attest-log-versioningはdisabled review
    manifestと共有lockの下だけで全writer切断/versioning/cutoverを書き込む。
  - preflightは一時task/EFSを使う。
  - applyはexact trusted automation role、共有DynamoDB lock、直前verify、保存planだけを必須にする。
  - legacy→generic media applyはMFA attestorが署名証跡・intent・lockを
    1回のDynamoDB transactionで消費したauthorizationを必須にする。
  - runtime preflightはCosign+exact KMS keyで新core digestの署名とRekor証跡も検証する。
  - planはrefreshとTerraform state lockのみ行う。
  - 出力directoryは0700、var-fileは0600相当、出力plan/receiptは未存在が必須。
  - tfvars や receipt に secret 値は書かない。
EOF
}

die() {
  echo "★ $*" >&2
  exit 1
}

ensure_forced_rollback_dm_qa_recovery_reserve() {
  local phase="$1" now latest_safe_epoch
  [ -n "${FORCED_ROLLBACK_DM_QA_DEADLINE_EPOCH:-}" ] || return 0
  now="$(date +%s)"
  latest_safe_epoch=$((
    FORCED_ROLLBACK_DM_QA_DEADLINE_EPOCH -
      FORCED_ROLLBACK_DM_QA_RECOVERY_RESERVE_SECONDS
  ))
  if [ "$now" -ge "$latest_safe_epoch" ]; then
    echo "FATAL: forced rollback DM QA deadline reserve exhausted during $phase" >&2
    return 124
  fi
  return 0
}

assert_clean_terraform_environment() {
  local name rejected=()
  while IFS= read -r name; do
    case "$name" in
      TF_CLI_ARGS|TF_CLI_ARGS_*|TF_WORKSPACE|TF_DATA_DIR|TF_VAR_*|\
      TF_CLI_CONFIG_FILE|TF_REATTACH_PROVIDERS|TF_PLUGIN_CACHE_DIR|\
      TF_REGISTRY_CLIENT_TIMEOUT|TF_REGISTRY_DISCOVERY_RETRY|\
      TF_IN_AUTOMATION|TF_LOG|TF_LOG_*)
        rejected+=("$name")
        unset "$name"
        ;;
      AWS_ENDPOINT_URL|AWS_ENDPOINT_URL_*|AWS_PROFILE|AWS_DEFAULT_PROFILE|\
      AWS_CONFIG_FILE|AWS_SHARED_CREDENTIALS_FILE|AWS_CA_BUNDLE)
        rejected+=("$name")
        unset "$name"
        ;;
    esac
  done < <(compgen -e | LC_ALL=C sort)
  if [ "${#rejected[@]}" -ne 0 ]; then
    # Never print values: TF_VAR_* can contain secrets.
    die "Terraform CLIへ影響する環境変数を消去して拒否しました: ${rejected[*]}"
  fi
}

# Corporate networks terminate TLS with a private CA, so every AWS call needs a
# CA bundle. The scrubber above rightly refuses an ambient AWS_CA_BUNDLE: taking
# one from the caller would let them redirect Terraform's trust anchor. Derive it
# from SSL_CERT_FILE instead, and only after that check has run, so the value is
# this script's own. runtime_evidence_guard.py derives it the same way for its
# own child environment.
derive_aws_ca_bundle_from_ssl_cert_file() {
  if [ -n "${AWS_CA_BUNDLE:-}" ]; then
    return 0
  fi
  if [ -n "${SSL_CERT_FILE:-}" ] && [ -f "${SSL_CERT_FILE}" ]; then
    export AWS_CA_BUNDLE="${SSL_CERT_FILE}"
  fi
  return 0
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 が必要です"
}

ensure_tmp() {
  if [ -z "$TMP_ROOT" ]; then
    TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-tf-guard.XXXXXX")"
    chmod 700 "$TMP_ROOT"
    trap 'rm -rf "$TMP_ROOT"' EXIT
  fi
}

stat_uid() {
  stat -c '%u' "$1" 2>/dev/null || stat -f '%u' "$1"
}

stat_mode() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

stat_identity() {
  stat -c '%d:%i' "$1" 2>/dev/null || stat -f '%d:%i' "$1"
}

stat_inode() {
  stat -c '%i' "$1" 2>/dev/null || stat -f '%i' "$1"
}

stat_size() {
  stat -c '%s' "$1" 2>/dev/null || stat -f '%z' "$1"
}

assert_owned() {
  [ "$(stat_uid "$1")" = "$(id -u)" ] || die "所有者が実行ユーザーではありません: $1"
}

assert_private_mode() {
  local path="$1" mode decimal
  mode="$(stat_mode "$path")"
  [[ "$mode" =~ ^[0-7]{3,4}$ ]] || die "permissionを判定できません: $path"
  decimal=$((8#$mode))
  (( (decimal & 8#077) == 0 )) || die "group/other権限を拒否します（0600/0700相当が必要）: $path"
}

assert_not_shared_writable() {
  local path="$1" mode decimal
  mode="$(stat_mode "$path")"
  [[ "$mode" =~ ^[0-7]{3,4}$ ]] || die "permissionを判定できません: $path"
  decimal=$((8#$mode))
  (( (decimal & 8#022) == 0 )) || die "group/other writable directoryを拒否します: $path"
}

assert_regular_nonwritable() {
  local path="$1" canonical mode decimal
  [ ! -L "$path" ] || die "symlink sourceを拒否します: $path"
  [ -f "$path" ] || die "regular source fileがありません: $path"
  canonical="$(realpath "$path")"
  [ "$canonical" = "$path" ] || die "non-canonical source pathを拒否します: $path"
  assert_owned "$canonical"
  mode="$(stat_mode "$canonical")"
  [[ "$mode" =~ ^[0-7]{3,4}$ ]] || die "permissionを判定できません: $canonical"
  decimal=$((8#$mode))
  (( (decimal & 8#022) == 0 )) ||
    die "helper/configのgroup/other writableを拒否します: $canonical"
}

assert_git_tracked_clean() {
  local path="$1" relative committed_blob actual_blob
  relative="${path#"$REPO_ROOT"/}"
  [ "$relative" != "$path" ] ||
    die "guard sourceがrepository外です: $path"
  git -C "$REPO_ROOT" ls-files --error-unmatch -- "$relative" >/dev/null 2>&1 ||
    die "guard sourceがGit管理されていません: $relative"
  committed_blob="$(git -C "$REPO_ROOT" rev-parse "HEAD:$relative" 2>/dev/null)" ||
    die "guard sourceのGit blobを確認できません: $relative"
  actual_blob="$(git -C "$REPO_ROOT" hash-object -- "$path")" ||
    die "guard sourceの実体hashを確認できません: $relative"
  [ "$actual_blob" = "$committed_blob" ] ||
    die "guard sourceの内容がreceiptのGit commitと一致しません: $relative"
  git -C "$REPO_ROOT" diff --quiet HEAD -- "$relative" ||
    die "guard sourceがreceiptのGit commitと一致しません: $relative"
}

assert_guard_sources() {
  need_cmd git
  assert_regular_nonwritable "$SCRIPT_PATH"
  assert_regular_nonwritable "$GUARD_JQ"
  assert_regular_nonwritable "$MIGRATION_FILE"
  assert_regular_nonwritable "$EVIDENCE_HELPER"
  assert_regular_nonwritable "$MEDIA_APPLY_AUTHORIZER"
  assert_regular_nonwritable "$DEPLOYMENT_APPLY_FINALIZER"
  assert_regular_nonwritable "$PLAN_CONTRACT_HELPER"
  assert_regular_nonwritable "$FORCED_ROLLBACK_DM_QA_PROBE"
  assert_regular_nonwritable "$IMAGE_GATE_RUNNER"
  assert_regular_nonwritable "$RELEASE_EVIDENCE_HELPER"
  assert_regular_nonwritable "$IMAGE_DEPLOYMENT_CONSUMER_REGISTRY"
  assert_regular_nonwritable "$IMAGE_CONTEXT_HELPER"
  assert_regular_nonwritable "$APPLY_SUPERVISOR"
  assert_regular_nonwritable "$PLAN_STAGER"
  assert_regular_nonwritable "$EVENTBRIDGE_APPLY_SAGA"
  assert_regular_nonwritable "$ECS_SERVICE_APPLY_SAGA"
  assert_regular_nonwritable "$HMAC_PLAN_HELPER"
  assert_regular_nonwritable "$OPENCLAW_ROLLOUT_GATE"
  assert_git_tracked_clean "$SCRIPT_PATH"
  assert_git_tracked_clean "$GUARD_JQ"
  assert_git_tracked_clean "$MIGRATION_FILE"
  assert_git_tracked_clean "$EVIDENCE_HELPER"
  assert_git_tracked_clean "$MEDIA_APPLY_AUTHORIZER"
  assert_git_tracked_clean "$DEPLOYMENT_APPLY_FINALIZER"
  assert_git_tracked_clean "$PLAN_CONTRACT_HELPER"
  assert_git_tracked_clean "$FORCED_ROLLBACK_DM_QA_PROBE"
  assert_git_tracked_clean "$IMAGE_GATE_RUNNER"
  assert_git_tracked_clean "$RELEASE_EVIDENCE_HELPER"
  assert_git_tracked_clean "$IMAGE_DEPLOYMENT_CONSUMER_REGISTRY"
  assert_git_tracked_clean "$IMAGE_CONTEXT_HELPER"
  assert_git_tracked_clean "$APPLY_SUPERVISOR"
  assert_git_tracked_clean "$PLAN_STAGER"
  assert_git_tracked_clean "$EVENTBRIDGE_APPLY_SAGA"
  assert_git_tracked_clean "$ECS_SERVICE_APPLY_SAGA"
  assert_git_tracked_clean "$HMAC_PLAN_HELPER"
  assert_git_tracked_clean "$OPENCLAW_ROLLOUT_GATE"

  local path
  while IFS= read -r path; do
    assert_regular_nonwritable "$path"
    assert_git_tracked_clean "$path"
  done < <(
    find "$TF_DIR" \
      -path "$TF_DIR/.terraform" -prune -o \
      -path "$TF_DIR/build" -prune -o \
      \( -name '*.tf' -o -name '*.tf.json' -o -name '*.sh' -o -name '.terraform.lock.hcl' -o -name 'handler.py' \) \
      -print | LC_ALL=C sort
  )

  local archive_dir relative
  for archive_dir in \
    "$TF_DIR/lambda/reminder_notify" \
    "$TF_DIR/lambda/tiktok_dispatch"; do
    while IFS= read -r path; do
      [ ! -L "$path" ] || die "Lambda archive symlinkを拒否します: $path"
      relative="${path#"$archive_dir"/}"
      case "$relative" in
        handler.py|__pycache__|__pycache__/*.pyc) ;;
        *) die "Lambda archive allowlist外pathを拒否します: $path" ;;
      esac
    done < <(find "$archive_dir" -mindepth 1 -print | LC_ALL=C sort)
  done
}

write_config_manifest() {
  local output="$1" migration_mode="${2:-include}" path relative
  : > "$output"
  for path in \
    "$GUARD_JQ" \
    "$MIGRATION_FILE" \
    "$EVIDENCE_HELPER" \
    "$MEDIA_APPLY_AUTHORIZER" \
    "$DEPLOYMENT_APPLY_FINALIZER" \
    "$PLAN_CONTRACT_HELPER" \
    "$FORCED_ROLLBACK_DM_QA_PROBE" \
    "$IMAGE_GATE_RUNNER" \
    "$RELEASE_EVIDENCE_HELPER" \
    "$IMAGE_DEPLOYMENT_CONSUMER_REGISTRY" \
    "$IMAGE_CONTEXT_HELPER" \
    "$APPLY_SUPERVISOR" \
    "$PLAN_STAGER" \
    "$EVENTBRIDGE_APPLY_SAGA" \
    "$ECS_SERVICE_APPLY_SAGA" \
    "$HMAC_PLAN_HELPER" \
    "$OPENCLAW_ROLLOUT_GATE"; do
    if [ "$migration_mode" = "exclude" ] &&
       [ "$path" = "$MIGRATION_FILE" ]; then
      continue
    fi
    relative="${path#"$REPO_ROOT"/}"
    printf '%s  %s\n' "$(sha256_file "$path")" "$relative" >> "$output"
  done
  while IFS= read -r path; do
    relative="${path#"$REPO_ROOT"/}"
    printf '%s  %s\n' "$(sha256_file "$path")" "$relative" >> "$output"
  done < <(
    find "$TF_DIR" \
      -path "$TF_DIR/.terraform" -prune -o \
      -path "$TF_DIR/build" -prune -o \
      \( -name '*.tf' -o -name '*.tf.json' -o -name '*.sh' -o -name '.terraform.lock.hcl' -o -name 'handler.py' \) \
      -print | LC_ALL=C sort
  )
  LC_ALL=C sort -o "$output" "$output"
}

git_commit() {
  git -C "$REPO_ROOT" rev-parse --verify HEAD^{commit} 2>/dev/null ||
    die "Git commitを確認できません"
}

assert_guard_paths_clean() {
  git -C "$REPO_ROOT" diff --quiet -- infra/deploy infra/terraform ||
    die "guard/Terraformの未commit変更を拒否します"
  git -C "$REPO_ROOT" diff --cached --quiet -- infra/deploy infra/terraform ||
    die "guard/Terraformの未commit index変更を拒否します"
  local untracked
  untracked="$(
    git -C "$REPO_ROOT" ls-files --others --exclude-standard -- \
      infra/deploy infra/terraform
  )"
  [ -z "$untracked" ] ||
    die "guard/Terraformの未追跡fileを拒否します:\n$untracked"
}

normalized_migration_manifest_sha256() {
  local migration_id="$1"
  jq -e -S -c --arg id "$migration_id" '
    if (.migrations[$id] | type) == "object" then
      del(
        .migrations[$id].enabled,
        .migrations[$id].reviewed_plan
      )
    else error("migration missing")
    end
  ' "$MIGRATION_FILE" | sha256_text
}

assert_review_commit_transition() {
  local source_commit="$1" migration_id="$2"
  local current_commit relative changed path
  [[ "$source_commit" =~ ^[0-9a-f]{40}$ ]] ||
    die "review source commitが不正です"
  current_commit="$(git_commit)"
  git -C "$REPO_ROOT" merge-base --is-ancestor \
    "$source_commit" "$current_commit" ||
    die "review source commitは現在HEADの祖先ではありません"
  relative="${MIGRATION_FILE#"$REPO_ROOT"/}"
  changed="$(
    git -C "$REPO_ROOT" diff --name-only --no-renames \
      "$source_commit" "$current_commit" --
  )"
  while IFS= read -r path; do
    [ -z "$path" ] || [ "$path" = "$relative" ] ||
      die "review後に許可外fileが変更されています: $path"
  done <<< "$changed"
  [ "$(normalized_migration_manifest_sha256 "$migration_id")" != "" ] ||
    die "normalized migration contractを計算できません"
}

canonical_existing_dir() {
  local path="$1" absolute
  [ ! -L "$path" ] || die "symlink directoryを拒否します: $path"
  [ -d "$path" ] || die "directoryがありません: $path"
  absolute="$(cd -P "$path" && pwd)"
  assert_owned "$absolute"
  assert_not_shared_writable "$absolute"
  printf '%s\n' "$absolute"
}

secure_private_dir() {
  local absolute
  absolute="$(canonical_existing_dir "$1")"
  assert_private_mode "$absolute"
  printf '%s\n' "$absolute"
}

secure_existing_file() {
  local path="$1" exact_mode="${2:-}" dir absolute
  [ ! -L "$path" ] || die "symlink fileを拒否します: $path"
  [ -f "$path" ] || die "regular fileがありません: $path"
  dir="$(canonical_existing_dir "$(dirname "$path")")"
  absolute="$dir/$(basename "$path")"
  [ ! -L "$absolute" ] && [ -f "$absolute" ] || die "path差替えを検出しました: $path"
  assert_owned "$absolute"
  assert_private_mode "$absolute"
  if [ -n "$exact_mode" ]; then
    [ "$(stat_mode "$absolute")" = "$exact_mode" ] || die "$exact_mode permissionが必要です: $absolute"
  fi
  printf '%s\n' "$absolute"
}

secure_new_file() {
  local path="$1" dir absolute
  dir="$(secure_private_dir "$(dirname "$path")")"
  absolute="$dir/$(basename "$path")"
  [ ! -e "$absolute" ] && [ ! -L "$absolute" ] || die "既存pathへの上書きを拒否します: $absolute"
  printf '%s\n' "$absolute"
}

sha256_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | awk '{print $1}'
  else
    die "sha256sum または shasum が必要です"
  fi
}

sha256_text() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print $1}'
  else
    die "sha256sum または shasum が必要です"
  fi
}

aws_endpoint() {
  case "$1" in
    apigatewayv2) printf 'https://apigateway.%s.amazonaws.com\n' "$REGION" ;;
    ecr) printf 'https://api.ecr.%s.amazonaws.com\n' "$REGION" ;;
    efs) printf 'https://elasticfilesystem.%s.amazonaws.com\n' "$REGION" ;;
    iam) printf 'https://iam.amazonaws.com\n' ;;
    s3api) printf 'https://s3.%s.amazonaws.com\n' "$REGION" ;;
    cloudwatch) printf 'https://monitoring.%s.amazonaws.com\n' "$REGION" ;;
    budgets) printf 'https://budgets.amazonaws.com\n' ;;
    ce) printf 'https://ce.us-east-1.amazonaws.com\n' ;;
    # AWS Chatbot is not deployed in ap-northeast-1 at all (the regional DNS name
    # does not exist), and its configurations are account-global, so they are read
    # through a deployed region. us-east-2 is pinned; the caller must sign for the
    # same region (aws_chatbot_cli), mirroring how ce is handled by aws_cost_cli.
    chatbot) printf 'https://chatbot.us-east-2.amazonaws.com\n' ;;
    *)
      case "$1" in
        sts|cloudtrail|bedrock|dynamodb|ec2|ecs|events|scheduler|lambda|\
        logs|sns|sqs|kms|autoscaling|codestar-notifications|rds|\
        secretsmanager)
          printf 'https://%s.%s.amazonaws.com\n' "$1" "$REGION"
          ;;
        *) die "AWS service endpointがallowlist外です: $1" ;;
      esac
      ;;
  esac
}

initialize_aws_trust() {
  [ -z "$AWS_BIN" ] || return 0
  need_cmd aws
  local discovered
  discovered="$(command -v aws)"
  AWS_BIN="$(realpath "$discovered")"
  [ -f "$AWS_BIN" ] && [ ! -L "$AWS_BIN" ] ||
    die "AWS executableのcanonical regular pathを確認できません"
  assert_regular_nonwritable "$AWS_BIN"
  [ "$(stat -c '%h' "$AWS_BIN" 2>/dev/null || stat -f '%l' "$AWS_BIN")" = "1" ] ||
    die "AWS executableはsingle-linkである必要があります"
  AWS_BIN_SHA256="$(sha256_file "$AWS_BIN")"
  AWS_BIN_IDENTITY="$(stat_identity "$AWS_BIN"):$(stat_size "$AWS_BIN")"
  "$AWS_BIN" --version 2>&1 | grep -Eq '^aws-cli/2[.]' ||
    die "review済みAWS CLI v2だけを使用できます"
  export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true
  export AWS_CONFIG_FILE=/dev/null
  export AWS_SHARED_CREDENTIALS_FILE=/dev/null
  export AWS_DEFAULT_REGION="$REGION"
  export AWS_REGION="$REGION"
  export AWS_PAGER=""
}

assert_aws_trust_unchanged() {
  initialize_aws_trust
  [ "$(stat_identity "$AWS_BIN"):$(stat_size "$AWS_BIN")" = \
    "$AWS_BIN_IDENTITY" ] ||
    die "AWS executable identityがworkflow中に変化しました"
  [ "$(sha256_file "$AWS_BIN")" = "$AWS_BIN_SHA256" ] ||
    die "AWS executable bytesがworkflow中に変化しました"
}

aws_cli() {
  initialize_aws_trust
  assert_aws_trust_unchanged
  local service="$1"
  shift
  "$AWS_BIN" --region "$REGION" \
    --endpoint-url "$(aws_endpoint "$service")" \
    --no-cli-pager "$service" "$@"
}

aws_cost_cli() {
  initialize_aws_trust
  assert_aws_trust_unchanged
  local service="$1"
  shift
  "$AWS_BIN" --region us-east-1 \
    --endpoint-url "$(aws_endpoint "$service")" \
    --no-cli-pager "$service" "$@"
}

aws_chatbot_cli() {
  initialize_aws_trust
  assert_aws_trust_unchanged
  local service="$1"
  shift
  "$AWS_BIN" --region us-east-2 \
    --endpoint-url "$(aws_endpoint "$service")" \
    --no-cli-pager "$service" "$@"
}

assert_trusted_automation_identity() {
  local output="${1:-}" identity="$TMP_ROOT/trusted-automation-identity.json"
  aws_cli sts get-caller-identity --output json > "$identity"
  jq -e \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg arn "$TRUSTED_AUTOMATION_ARN" '
    .Account == $account and
    .Arn == $arn and
    (.UserId | type) == "string" and
    (.UserId | length) > 0
  ' "$identity" >/dev/null ||
    die "write-capable guard操作はexact trusted automation sessionだけが実行できます"
  if [ -n "$output" ]; then
    output="$(secure_new_file "$output")"
    cp "$identity" "$output"
    chmod 600 "$output"
  fi
}

assert_trusted_media_attestor_identity() {
  local identity="$TMP_ROOT/trusted-media-attestor-identity.json"
  aws_cli sts get-caller-identity --output json > "$identity"
  jq -e \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg arn "$TRUSTED_MEDIA_ATTESTOR_ARN" '
    .Account == $account and
    .Arn == $arn and
    (.UserId | type) == "string" and
    (.UserId | length) > 0
  ' "$identity" >/dev/null ||
    die "media署名・一回限りapply承認はexact MFA attestor sessionだけが実行できます"
}

capture_backend_identity() {
  local output="$1"
  local metadata="${2:-$TF_DIR/.terraform/terraform.tfstate}"
  local canonical identity_sha

  canonical="$(realpath "$metadata" 2>/dev/null)" ||
    die "Terraform backend metadataをcanonicalizeできません"
  [ "$canonical" = "$metadata" ] ||
    die "Terraform backend metadataはcanonical pathで指定してください"
  assert_owned "$(dirname "$canonical")"
  assert_not_shared_writable "$(dirname "$canonical")"
  assert_regular_nonwritable "$canonical"
  jq -e -S \
    --arg bucket "$EXPECTED_BACKEND_BUCKET" \
    --arg key "$EXPECTED_BACKEND_KEY" \
    --arg region "$REGION" \
    --arg dynamodb_table "$EXPECTED_BACKEND_DYNAMODB_TABLE" '
    .backend as $backend |
    $backend.config as $config |
    if (
      ($backend | type) == "object" and
      $backend.type == "s3" and
      ($config | type) == "object" and
      $config.bucket == $bucket and
      $config.key == $key and
      $config.region == $region and
      $config.dynamodb_table == $dynamodb_table and
      $config.encrypt == true and
      (
        $config |
        to_entries |
        all(
          (.key == "bucket" or
           .key == "key" or
           .key == "region" or
           .key == "dynamodb_table" or
           .key == "encrypt") or
          .value == null
        )
      )
    ) then {
        type:"s3",
        bucket:$bucket,
        key:$key,
        region:$region,
        dynamodb_table:$dynamodb_table,
        encrypt:true
      }
    else error("unreviewed backend metadata")
    end
  ' "$canonical" > "$output" ||
    die "初期化済みTerraform backend metadataがreview済みS3設定と一致しません"
  identity_sha="$(sha256_file "$output")"
  jq -e -S --arg identity_sha "$identity_sha" \
    '. + {identity_sha256:$identity_sha}' "$output" > "${output}.bound" ||
    die "Terraform backend identityをhash束縛できません"
  mv "${output}.bound" "$output"
}

capture_state_contract() {
  local output="$1"
  local live_contract="${2:-}"
  local raw_output="${3:-}"
  local raw="$TMP_ROOT/state-pull-$RANDOM.json"
  local listed="$TMP_ROOT/state-list-$RANDOM.txt"
  local canonical="$TMP_ROOT/state-list-canonical-$RANDOM.txt"
  local derived="$TMP_ROOT/state-list-derived-$RANDOM.txt"
  local specs="$TMP_ROOT/state-import-specs-$RANDOM.json"
  local backend="$TMP_ROOT/backend-identity-$RANDOM.json"
  local backend_after="$TMP_ROOT/backend-identity-after-$RANDOM.json"
  local base_output="$output"
  local scoped_output=""
  local workspace address_count address_sha
  if [ -n "$live_contract" ]; then
    base_output="$TMP_ROOT/state-contract-base-$RANDOM.json"
    scoped_output="$TMP_ROOT/state-contract-scoped-$RANDOM.json"
  fi

  capture_backend_identity "$backend"
  workspace="$(terraform -chdir="$TF_DIR" workspace show)"
  [ "$workspace" = "$EXPECTED_WORKSPACE" ] ||
    die "Terraform workspaceはdefault以外を拒否します"
  terraform -chdir="$TF_DIR" state pull > "$raw"
  terraform -chdir="$TF_DIR" state list > "$listed"
  jq -e '
    .version == 4 and
    (.terraform_version | type) == "string" and
    (.serial | type) == "number" and .serial >= 0 and (.serial | floor) == .serial and
    (.lineage | type) == "string" and
    (.lineage | test("^[0-9a-fA-F-]{36}$")) and
    (.resources | type) == "array"
  ' "$raw" >/dev/null || die "Terraform state metadataが不正です"
  awk 'NF == 0 { exit 1 }' "$listed" ||
    die "terraform state listに空行があります"
  awk 'NF { print }' "$listed" | LC_ALL=C sort -u > "$canonical"
  [ "$(awk 'END { print NR + 0 }' "$listed")" = \
    "$(awk 'END { print NR + 0 }' "$canonical")" ] ||
    die "terraform state listに重複addressがあります"
  jq -r '
    .resources[] |
    (.module // "") as $module_path |
    . as $resource |
    (
      if $resource.mode == "managed" then ""
      elif $resource.mode == "data" then "data."
      else error("unsupported state resource mode")
      end
    ) as $mode_prefix |
    ($resource.instances // [])[] |
    (
      (if $module_path == "" then "" else ($module_path + ".") end) +
      $mode_prefix + $resource.type + "." + $resource.name +
      (
        if has("index_key") then
          if (.index_key | type) == "number" then
            "[" + (.index_key | tostring) + "]"
          elif (.index_key | type) == "string" then
            "[" + (.index_key | tojson) + "]"
          else error("unsupported state index key")
          end
        else ""
        end
      )
    )
  ' "$raw" | LC_ALL=C sort > "$derived" ||
    die "state pullからaddress ownershipを再構成できません"
  [ -z "$(uniq -d "$derived")" ] ||
    die "state pullに重複resource addressがあります"
  cmp -s "$canonical" "$derived" ||
    die "terraform state pull/listのaddress ownershipが一致しません"
  address_count="$(awk 'END { print NR + 0 }' "$canonical")"
  address_sha="$(sha256_file "$canonical")"
  capture_backend_identity "$backend_after"
  cmp -s "$backend" "$backend_after" ||
    die "state観測中にTerraform backend metadataが変化しました"

  jq -n '[
    {
      address:"aws_cloudwatch_log_group.codebuild_aiia_image_builder",
      type:"aws_cloudwatch_log_group",
      name:"codebuild_aiia_image_builder",
      id:"/aws/codebuild/teamagent-dev-aiia-image-builder"
    },
    {
      address:"aws_cloudwatch_log_group.codebuild_image",
      type:"aws_cloudwatch_log_group",
      name:"codebuild_image",
      id:"/aws/codebuild/teamagent-dev-image-builder"
    },
    {
      address:"aws_cloudwatch_log_group.ecs_containerinsights_teamagent",
      type:"aws_cloudwatch_log_group",
      name:"ecs_containerinsights_teamagent",
      id:"/aws/ecs/containerinsights/teamagent-dev/performance"
    },
    {
      address:"aws_cloudwatch_log_group.ecs_containerinsights_tiktok",
      type:"aws_cloudwatch_log_group",
      name:"ecs_containerinsights_tiktok",
      id:"/aws/ecs/containerinsights/teamagent-dev-tiktok/performance"
    },
    {
      address:"aws_cloudwatch_log_group.reminder_notify",
      type:"aws_cloudwatch_log_group",
      name:"reminder_notify",
      id:"/aws/lambda/teamagent-dev-reminders-notify"
    },
    {
      address:"aws_cloudwatch_log_group.tiktok_dispatch",
      type:"aws_cloudwatch_log_group",
      name:"tiktok_dispatch",
      id:"/aws/lambda/teamagent-dev-tiktok-acquire-dispatch"
    },
    {
      address:"aws_cloudwatch_log_group.x_dispatch",
      type:"aws_cloudwatch_log_group",
      name:"x_dispatch",
      id:"/aws/lambda/teamagent-dev-x-buzz-dispatch"
    }
  ]' > "$specs"

  jq -n -S \
    --arg workspace "$workspace" \
    --arg address_sha "$address_sha" \
    --argjson address_count "$address_count" \
    --slurpfile backend "$backend" \
    --slurpfile state "$raw" \
    --slurpfile specs "$specs" '
    def root_address($resource):
      if (($resource.module // "") != "") then null
      else ($resource.type + "." + $resource.name)
      end;
    def instance_id($instance):
      ($instance.attributes.id // $instance.attributes.name // "");
    def ownership($spec):
      ([
        $state[0].resources[] |
        select(
          .mode == "managed" and
          (.module // "") == "" and
          .type == $spec.type and .name == $spec.name
        )
      ]) as $owned |
      ([
        $state[0].resources[] as $resource |
        ($resource.instances // [])[] |
        select(instance_id(.) == $spec.id) |
        {
          address: root_address($resource),
          indexed: has("index_key")
        }
      ]) as $claims |
      if ($owned | length) == 0 then
        if ($claims | length) == 0 then {
          expected_id:$spec.id,
          present:false
        } else error("remote log group is owned by another state address")
        end
      elif ($owned | length) == 1 and
           (($owned[0].instances // []) | length) == 1 and
           (($owned[0].instances[0] | has("index_key")) | not) and
           instance_id($owned[0].instances[0]) == $spec.id and
           ($claims | length) == 1 and
           $claims[0].address == $spec.address and
           $claims[0].indexed == false
      then {
        expected_id:$spec.id,
        present:true
      }
      else error("log group state ownership is ambiguous")
      end;
    {
      backend:($backend[0] + {workspace:$workspace}),
      state:{
        lineage:$state[0].lineage,
        serial:$state[0].serial,
        address_count:$address_count,
        address_set_sha256:$address_sha
      },
      imports:(
        reduce $specs[0][] as $spec
          ({}; . + {($spec.address):ownership($spec)})
      )
    }
  ' > "$base_output" ||
    die "state lineage/serial/address/import ownership契約を確定できません"
  if [ -n "$live_contract" ]; then
    jq -n -S \
      --arg account "$EXPECTED_ACCOUNT_ID" \
      --arg region "$REGION" \
      --slurpfile base "$base_output" \
      --slurpfile state "$raw" \
      --slurpfile registry "$IMAGE_DEPLOYMENT_CONSUMER_REGISTRY" \
      --slurpfile live "$live_contract" '
      def instance_address($resource; $instance):
        (
          (if (($resource.module // "") == "") then
             ""
           else
             ($resource.module + ".")
           end) +
          (if $resource.mode == "managed" then
             ""
           elif $resource.mode == "data" then
             "data."
           else
             error("unsupported state resource mode")
           end) +
          $resource.type + "." + $resource.name +
          (
            if ($instance | has("index_key")) then
              if ($instance.index_key | type) == "number" then
                "[" + ($instance.index_key | tostring) + "]"
              elif ($instance.index_key | type) == "string" then
                "[" + ($instance.index_key | tojson) + "]"
              else
                error("unsupported state index key")
              end
            else
              ""
            end
          )
        );
      def registry_owner($scope):
        ([
          $registry[0].consumers[] |
          select(.consumer_id == $scope.consumer_id)
        ]) as $owners |
        if (
          ($owners | length) == 1 and
          ($owners[0].terraform_task_definition_address |
            type == "string" and length > 0) and
          ($owners[0].ecs_family |
            type == "string" and test("^[A-Za-z0-9_-]+$")) and
          ($owners[0].receipt | type) == "object" and
          ($owners[0].activator | type) == "object"
        ) then
          $owners[0]
        else
          error("consumer registry owner is not exact")
        end;
      def state_task_revision($scope):
        registry_owner($scope) as $owner |
        ([
          $state[0].resources[] as $resource |
          ($resource.instances // [])[] as $instance |
          select(
            instance_address($resource; $instance) ==
              $owner.terraform_task_definition_address
          ) |
          {
            attributes:$instance.attributes,
            mode:$resource.mode,
            type:$resource.type
          }
        ]) as $matches |
        if ($matches | length) != 1 then
          error("scoped task definition address ownership is not exact")
        else
          $matches[0] as $match |
          if (
            $match.mode == "managed" and
            $match.type == "aws_ecs_task_definition" and
            ($match.attributes | type) == "object" and
            ($match.attributes.family |
              type == "string" and test("^[A-Za-z0-9_-]+$")) and
            $match.attributes.family == $owner.ecs_family and
            ($match.attributes.revision |
              type == "number" and . >= 1 and floor == .) and
            ($match.attributes.arn | type) == "string" and
            $match.attributes.arn ==
              (
                "arn:aws:ecs:" + $region + ":" + $account +
                ":task-definition/" + $match.attributes.family + ":" +
                ($match.attributes.revision | tostring)
              ) and
            $match.attributes.arn == $scope.task_definition_arn and
            $scope.terraform_address ==
              $owner.terraform_task_definition_address and
            $scope.pipeline == $owner.receipt.pipeline and
            $scope.subject == $owner.receipt.subject and
            $scope.activation.type == $owner.activator.type and
            $scope.activation.identity == $owner.activator.identity
          ) then {
            key:$scope.consumer_id,
            value:$match.attributes.revision
          }
          else
            error("state task definition binding differs from live")
          end
        end;
      ($live[0].post_live_contract // $live[0]) as $scope |
      if (
        $registry[0].schema_version == 1 and
        ($registry[0].consumers | type) == "array" and
        ($scope | type) == "object" and
        ($scope.resources | type) == "array" and
        ($scope.resources | map(.consumer_id) | unique | length) ==
          ($scope.resources | length) and
        ($scope.resources | map(.terraform_address) | unique | length) ==
          ($scope.resources | length) and
        all($scope.resources[];
          (.consumer_id |
            type == "string" and
            test("^[a-z][a-z0-9_]*$")) and
          (.terraform_address |
            type == "string" and length > 0) and
          (.task_definition_arn |
            type == "string" and
            test(
              "^arn:aws:ecs:" + $region + ":" + $account +
              ":task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*$"
            ))
        )
      ) then
        $base[0] + {
          task_revisions:(
            $scope.resources |
            map(state_task_revision(.)) |
            from_entries
          )
        }
      else
        error("invalid scoped live contract")
      end
    ' > "$scoped_output" ||
      die "scope内consumerのTerraform state task definition bindingがlive契約と一致しません"
    mv "$scoped_output" "$output"
  fi
  if [ -n "$raw_output" ]; then
    cp "$raw" "$raw_output" ||
      die "検証済みTerraform state snapshotを保存できません"
    chmod 600 "$raw_output"
  fi
}

verify_alarm_delivery_test_receipt_legacy_retired() {
  die "retired: only fresh SNS challenge plus managed-KMS recipient acknowledgement is accepted"
}

capture_log_delivery_contract() {
  local output="$1"
  local dir="$TMP_ROOT/log-delivery-$RANDOM-$RANDOM"
  local trail_name="${PROJECT}-${ENVIRONMENT}-trail"
  local cloudtrail_bucket="${PROJECT}-${ENVIRONMENT}-cloudtrail-${EXPECTED_ACCOUNT_ID}"
  local bedrock_bucket="${PROJECT}-${ENVIRONMENT}-bedrock-logs-${EXPECTED_ACCOUNT_ID}"
  mkdir -m 700 "$dir"

  aws_cli cloudtrail get-trail --name "$trail_name" \
    --output json > "$dir/cloudtrail.json"
  aws_cli cloudtrail get-trail-status --name "$trail_name" \
    --output json > "$dir/cloudtrail-status.json"
  aws_cli bedrock get-model-invocation-logging-configuration \
    --output json > "$dir/bedrock.json"

  jq -e -S \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" \
    --arg trail_name "$trail_name" \
    --arg cloudtrail_bucket "$cloudtrail_bucket" \
    --arg bedrock_bucket "$bedrock_bucket" \
    --slurpfile trail "$dir/cloudtrail.json" \
    --slurpfile status "$dir/cloudtrail-status.json" \
    --slurpfile bedrock "$dir/bedrock.json" '
    ($trail[0].Trail // null) as $t |
    $status[0] as $s |
    ($bedrock[0].loggingConfig // null) as $b |
    if (
      ($t | type) == "object" and
      $t.Name == $trail_name and
      $t.S3BucketName == $cloudtrail_bucket and
      $t.IsMultiRegionTrail == true and
      $t.IncludeGlobalServiceEvents == true and
      $t.LogFileValidationEnabled == true and
      ($t.KmsKeyId | type) == "string" and
      ($t.KmsKeyId | test(
        "^arn:aws:kms:" + $region + ":" + $account +
        ":key/[0-9a-fA-F-]{36}$"
      )) and
      $s.IsLogging == true and
      (($s.LatestDeliveryError // "") == "") and
      (($s.LatestDigestDeliveryError // "") == "") and
      ($s.LatestDeliveryTime | type) == "string" and
      ($s.LatestDeliveryTime | length) > 0 and
      ($s.LatestDigestDeliveryTime | type) == "string" and
      ($s.LatestDigestDeliveryTime | length) > 0 and
      ($b | type) == "object" and
      ($b.cloudWatchConfig // null) == null and
      $b.textDataDeliveryEnabled == true and
      $b.embeddingDataDeliveryEnabled == true and
      $b.imageDataDeliveryEnabled == false and
      $b.videoDataDeliveryEnabled == false and
      $b.s3Config == {
        bucketName: $bedrock_bucket,
        keyPrefix: "bedrock/"
      }
    ) then {
      cloudtrail: {
        configuration: {
          name: $t.Name,
          s3_bucket_name: $t.S3BucketName,
          kms_key_id: $t.KmsKeyId,
          is_multi_region_trail: $t.IsMultiRegionTrail,
          include_global_service_events: $t.IncludeGlobalServiceEvents,
          log_file_validation_enabled: $t.LogFileValidationEnabled
        },
        health: {
          is_logging: $s.IsLogging,
          latest_delivery_time: $s.LatestDeliveryTime,
          latest_digest_delivery_time: $s.LatestDigestDeliveryTime
        }
      },
      bedrock: {
        configuration: {
          text_data_delivery_enabled: $b.textDataDeliveryEnabled,
          embedding_data_delivery_enabled:
            $b.embeddingDataDeliveryEnabled,
          image_data_delivery_enabled: $b.imageDataDeliveryEnabled,
          video_data_delivery_enabled: $b.videoDataDeliveryEnabled,
          s3_bucket_name: $b.s3Config.bucketName,
          key_prefix: $b.s3Config.keyPrefix
        }
      }
    } else error(
      "CloudTrail/Bedrock producer configuration or delivery health mismatch"
    )
    end
  ' > "$output" ||
    die "CloudTrail/Bedrock producerの配信設定・health契約が不正です"
}

log_delivery_contract_sha256() {
  local metadata="$1"
  jq -S -c '{
    cloudtrail:.cloudtrail.configuration,
    bedrock:.bedrock.configuration
  }' "$metadata" | sha256_text
}

capture_log_producer_off_contract() {
  local output="$1"
  local dir="$TMP_ROOT/log-producer-off-$RANDOM-$RANDOM"
  local trail_name="${PROJECT}-${ENVIRONMENT}-trail"
  local cloudtrail_bucket="${PROJECT}-${ENVIRONMENT}-cloudtrail-${EXPECTED_ACCOUNT_ID}"
  mkdir -m 700 "$dir"

  aws_cli cloudtrail get-trail --name "$trail_name" \
    --output json > "$dir/cloudtrail.json"
  aws_cli cloudtrail get-trail-status --name "$trail_name" \
    --output json > "$dir/cloudtrail-status.json"
  aws_cli bedrock get-model-invocation-logging-configuration \
    --output json > "$dir/bedrock.json"

  jq -n -e -S \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" \
    --arg trail_name "$trail_name" \
    --arg cloudtrail_bucket "$cloudtrail_bucket" \
    --slurpfile trail "$dir/cloudtrail.json" \
    --slurpfile status "$dir/cloudtrail-status.json" \
    --slurpfile bedrock "$dir/bedrock.json" '
    ($trail[0].Trail // null) as $t |
    $status[0] as $s |
    ($bedrock[0].loggingConfig // null) as $b |
    if (
      ($t | type) == "object" and
      $t.Name == $trail_name and
      $t.S3BucketName == $cloudtrail_bucket and
      $t.IsMultiRegionTrail == true and
      $t.IncludeGlobalServiceEvents == true and
      $t.LogFileValidationEnabled == true and
      ($t.KmsKeyId | type) == "string" and
      ($t.KmsKeyId | test(
        "^arn:aws:kms:" + $region + ":" + $account +
        ":key/[0-9a-fA-F-]{36}$"
      )) and
      $s.IsLogging == false and
      $b == null
    ) then {
      cloudtrail: {
        connection_state: "disconnected",
        configuration: {
          name: $t.Name,
          s3_bucket_name: $t.S3BucketName,
          kms_key_id: $t.KmsKeyId,
          is_multi_region_trail: $t.IsMultiRegionTrail,
          include_global_service_events: $t.IncludeGlobalServiceEvents,
          log_file_validation_enabled: $t.LogFileValidationEnabled
        },
        is_logging: $s.IsLogging
      },
      bedrock: {
        connection_state: "disconnected",
        configuration_present: false
      }
    } else error(
      "CloudTrail must be stopped on the exact destination and Bedrock logging absent"
    )
    end
  ' > "$output" ||
    die "pre-cutoverではCloudTrail/Bedrock writerのdisconnected状態が必須です"
}

write_log_cutover_contract() {
  local producer_off="$1" output="$2"
  jq -e -S \
    --arg bedrock_bucket \
      "${PROJECT}-${ENVIRONMENT}-bedrock-logs-${EXPECTED_ACCOUNT_ID}" '
    if (
      .cloudtrail.connection_state == "disconnected" and
      .cloudtrail.is_logging == false and
      .bedrock == {
        connection_state:"disconnected",
        configuration_present:false
      }
    ) then {
        cloudtrail: {
          configuration:.cloudtrail.configuration
        },
        bedrock: {
          configuration: {
            text_data_delivery_enabled:true,
            embedding_data_delivery_enabled:true,
            image_data_delivery_enabled:false,
            video_data_delivery_enabled:false,
            s3_bucket_name:$bedrock_bucket,
            key_prefix:"bedrock/"
          }
        }
      }
    else error("producer-off contract mismatch")
    end
  ' "$producer_off" > "$output" ||
    die "producer-off状態からexact cutover契約を導出できません"
}

capture_bucket_versioning_enablement() {
  die "retired: only the guard-owned PutBucketVersioning response Date is authoritative"
}

capture_versioning_enablement_contract() {
  local output="$1"
  local cloudtrail="$TMP_ROOT/cloudtrail-versioning-enablement-$RANDOM.json"
  local bedrock="$TMP_ROOT/bedrock-versioning-enablement-$RANDOM.json"
  capture_bucket_versioning_enablement \
    "${PROJECT}-${ENVIRONMENT}-cloudtrail-${EXPECTED_ACCOUNT_ID}" \
    "$cloudtrail"
  capture_bucket_versioning_enablement \
    "${PROJECT}-${ENVIRONMENT}-bedrock-logs-${EXPECTED_ACCOUNT_ID}" \
    "$bedrock"
  jq -n -e -S \
    --slurpfile cloudtrail "$cloudtrail" \
    --slurpfile bedrock "$bedrock" '{
      cloudtrail:$cloudtrail[0],
      bedrock:$bedrock[0]
    }' > "$output" ||
    die "bucket versioning enablement契約を作成できません"
}

verify_versioning_settle_window() {
  local enablement="$1" observed_at_epoch="$2"
  local latest_enabled_at not_before_epoch
  latest_enabled_at="$(jq -er '
    [
      .cloudtrail.enabled_at_epoch,
      .bedrock.enabled_at_epoch
    ] |
    if (
      length == 2 and
      all(.[];
        (type == "number") and
        (floor == .) and
        . >= 0
      )
    ) then max
    else error("invalid enablement timestamps")
    end
  ' "$enablement")" ||
    die "independent versioning-enabled timestampが不正です"
  case "$observed_at_epoch" in
    ''|*[!0-9]*) die "pre-cutover observation timestampが不正です" ;;
  esac
  not_before_epoch=$((latest_enabled_at + LOG_VERSIONING_SETTLE_SECONDS))
  [ "$observed_at_epoch" -ge "$not_before_epoch" ] ||
    die "independent versioning-enabled timestampから900秒のsettle windowが完了していません"
  printf '%s\n' "$not_before_epoch"
}

write_log_bucket_identity() {
  local snapshot="$1" bucket_key="$2" output="$3"
  local bucket_name
  bucket_name="${PROJECT}-${ENVIRONMENT}-${bucket_key}-${EXPECTED_ACCOUNT_ID}"
  if [ "$bucket_key" = "bedrock-logs" ]; then
    jq -e -S \
      --arg account "$EXPECTED_ACCOUNT_ID" \
      --arg region "$REGION" \
      --arg name "$bucket_name" '
      .log_buckets.bedrock as $bucket |
      {
        account_id:$account,
        region:$region,
        bucket_name:$name,
        bucket_arn:("arn:aws:s3:::" + $name),
        versioning_status:$bucket.versioning_status,
        mfa_delete:$bucket.mfa_delete
      }
    ' "$snapshot" > "$output"
  else
    jq -e -S \
      --arg account "$EXPECTED_ACCOUNT_ID" \
      --arg region "$REGION" \
      --arg name "$bucket_name" '
      .log_buckets.cloudtrail as $bucket |
      {
        account_id:$account,
        region:$region,
        bucket_name:$name,
        bucket_arn:("arn:aws:s3:::" + $name),
        versioning_status:$bucket.versioning_status,
        mfa_delete:$bucket.mfa_delete,
        lifecycle:$bucket.lifecycle
      }
    ' "$snapshot" > "$output"
  fi
}

verify_versioning_attestation_receipt_v2_retired() {
  die "retired v2 receipt: only the guard-owned first-time schema v4 workflow is accepted"
  local receipt="$1" snapshot="$2" state_contract="$3"
  local config_manifest="$TMP_ROOT/versioning-config-manifest-$RANDOM.txt"
  local current_enablement="$TMP_ROOT/versioning-current-enablement-$RANDOM.json"
  local current_producer="$TMP_ROOT/versioning-current-producer-$RANDOM.json"
  local cloudtrail_identity="$TMP_ROOT/versioning-cloudtrail-identity-$RANDOM.json"
  local bedrock_identity="$TMP_ROOT/versioning-bedrock-identity-$RANDOM.json"
  validate_log_versioning_stage_manifest
  write_config_manifest "$config_manifest"

  jq -e \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" \
    --arg git_commit "$(git_commit)" \
    --arg guard_version "$GUARD_VERSION" \
    --arg guard_script_sha256 "$(sha256_file "$SCRIPT_PATH")" \
    --arg guard_jq_sha256 "$(sha256_file "$GUARD_JQ")" \
    --arg migration_manifest_sha256 "$(sha256_file "$MIGRATION_FILE")" \
    --arg config_manifest_sha256 "$(sha256_file "$config_manifest")" \
    --arg deployment_lock_id "$DEPLOYMENT_LOCK_ID" \
    --arg cloudtrail \
      "${PROJECT}-${ENVIRONMENT}-cloudtrail-${EXPECTED_ACCOUNT_ID}" \
    --arg bedrock \
      "${PROJECT}-${ENVIRONMENT}-bedrock-logs-${EXPECTED_ACCOUNT_ID}" \
    --argjson settle_seconds "$LOG_VERSIONING_SETTLE_SECONDS" \
    --argjson now "$(date +%s)" '
    (keys | sort) == ([
      "account_id",
      "buckets",
      "config_manifest_sha256",
      "created_at_epoch",
      "cutover",
      "deployment_lock_id",
      "expires_at_epoch",
      "git_commit",
      "guard_jq_sha256",
      "guard_script_sha256",
      "guard_version",
      "kind",
      "live_after_sha256",
      "migration_manifest_sha256",
      "pre_cutover_observed_at_epoch",
      "producer_off",
      "region",
      "schema_version",
      "settle_window_seconds",
      "stage_id",
      "state_contract",
      "versioning_enablement"
    ] | sort) and
    .kind == "retired-teamagent-log-versioning-precutover-receipt" and
    .schema_version == 2 and
    .stage_id == "2026-07-log-versioning-cutover-v4" and
    .guard_version == $guard_version and
    .account_id == $account and .region == $region and
    .git_commit == $git_commit and
    .guard_script_sha256 == $guard_script_sha256 and
    .guard_jq_sha256 == $guard_jq_sha256 and
    .migration_manifest_sha256 == $migration_manifest_sha256 and
    .config_manifest_sha256 == $config_manifest_sha256 and
    .deployment_lock_id == $deployment_lock_id and
    (.live_after_sha256 | test("^[0-9a-f]{64}$")) and
    .settle_window_seconds == $settle_seconds and
    (.created_at_epoch | type) == "number" and
    (.created_at_epoch | floor) == .created_at_epoch and
    (.pre_cutover_observed_at_epoch | type) == "number" and
    (.pre_cutover_observed_at_epoch | floor) ==
      .pre_cutover_observed_at_epoch and
    .pre_cutover_observed_at_epoch <= .created_at_epoch and
    .created_at_epoch <= $now and
    .expires_at_epoch > $now and
    (.expires_at_epoch - .created_at_epoch) > 0 and
    (.expires_at_epoch - .created_at_epoch) <= 86400 and
    (.buckets | keys | sort) == ["bedrock","cloudtrail"] and
    (.buckets.cloudtrail | keys | sort) ==
      ["after","before","identity_sha256","name"] and
    (.buckets.bedrock | keys | sort) ==
      ["after","before","identity_sha256","name"] and
    (.buckets.cloudtrail.before | keys | sort) ==
      ["lifecycle","mfa_delete","versioning_status"] and
    (.buckets.cloudtrail.before.lifecycle | keys | sort) ==
      ([
        "canonical_sha256",
        "configuration_present",
        "deletion_rule_count",
        "rule_count"
      ] | sort) and
    .buckets.cloudtrail.before.versioning_status == "Enabled" and
    .buckets.cloudtrail.before.mfa_delete == "Disabled" and
    .buckets.cloudtrail.before.lifecycle.deletion_rule_count == 0 and
    (.buckets.cloudtrail.before.lifecycle.canonical_sha256 |
      test("^[0-9a-f]{64}$")) and
    .buckets.cloudtrail.after == .buckets.cloudtrail.before and
    .buckets.bedrock.before == {
      versioning_status:"Enabled",
      mfa_delete:"Disabled"
    } and
    .buckets.bedrock.after == .buckets.bedrock.before and
    .buckets.cloudtrail.name == $cloudtrail and
    .buckets.bedrock.name == $bedrock and
    (.buckets.cloudtrail.identity_sha256 |
      test("^[0-9a-f]{64}$")) and
    (.buckets.bedrock.identity_sha256 |
      test("^[0-9a-f]{64}$")) and
    (.versioning_enablement | keys | sort) == ["bedrock","cloudtrail"] and
    all(.versioning_enablement[];
      (keys | sort) == ([
        "aws_region",
        "bucket_arn",
        "bucket_name",
        "enabled_at_epoch",
        "enablement_event_id_sha256",
        "enablement_event_sha256",
        "event_name",
        "event_source",
        "event_time",
        "recipient_account_id",
        "status",
        "timestamp_source"
      ] | sort) and
      .aws_region == $region and
      .recipient_account_id == $account and
      .event_name == "PutBucketVersioning" and
      .event_source == "s3.amazonaws.com" and
      .status == "Enabled" and
      .timestamp_source == "aws-http-response-date" and
      (.enabled_at_epoch | type) == "number" and
      (.enabled_at_epoch | floor) == .enabled_at_epoch and
      (.enablement_event_id_sha256 | test("^[0-9a-f]{64}$")) and
      (.enablement_event_sha256 | test("^[0-9a-f]{64}$"))
    ) and
    .versioning_enablement.cloudtrail.bucket_name == $cloudtrail and
    .versioning_enablement.cloudtrail.bucket_arn ==
      ("arn:aws:s3:::" + $cloudtrail) and
    .versioning_enablement.bedrock.bucket_name == $bedrock and
    .versioning_enablement.bedrock.bucket_arn ==
      ("arn:aws:s3:::" + $bedrock) and
    (.producer_off | keys | sort) == ([
      "after_observed_at_epoch",
      "before_observed_at_epoch",
      "contract",
      "contract_sha256"
    ] | sort) and
    (.producer_off.contract_sha256 | test("^[0-9a-f]{64}$")) and
    .producer_off.contract.cloudtrail.connection_state == "disconnected" and
    .producer_off.contract.cloudtrail.is_logging == false and
    .producer_off.contract.cloudtrail.configuration.name ==
      "teamagent-dev-trail" and
    .producer_off.contract.cloudtrail.configuration.s3_bucket_name ==
      $cloudtrail and
    .producer_off.contract.cloudtrail.configuration.is_multi_region_trail ==
      true and
    .producer_off.contract.cloudtrail.configuration.include_global_service_events ==
      true and
    .producer_off.contract.cloudtrail.configuration.log_file_validation_enabled ==
      true and
    (.producer_off.contract.cloudtrail.configuration.kms_key_id |
      test("^arn:aws:kms:" + $region + ":" + $account +
        ":key/[0-9a-fA-F-]{36}$")) and
    .producer_off.contract.bedrock == {
      connection_state:"disconnected",
      configuration_present:false
    } and
    ([.producer_off.before_observed_at_epoch,
      .producer_off.after_observed_at_epoch] |
      all((type == "number") and (floor == .) and . >= 0)) and
    .producer_off.before_observed_at_epoch <=
      .producer_off.after_observed_at_epoch and
    .producer_off.after_observed_at_epoch ==
      .pre_cutover_observed_at_epoch and
    .versioning_enablement.cloudtrail.enabled_at_epoch <=
      .producer_off.before_observed_at_epoch and
    .versioning_enablement.bedrock.enabled_at_epoch <=
      .producer_off.before_observed_at_epoch and
    (.cutover | keys | sort) == ([
      "bedrock_action",
      "cloudtrail_action",
      "contract_sha256",
      "id",
      "not_before_epoch"
    ] | sort) and
    .cutover.id == "2026-07-cloudtrail-bedrock-writer-cutover-v1" and
    .cutover.cloudtrail_action == "start-logging" and
    .cutover.bedrock_action ==
      "put-model-invocation-logging-configuration" and
    (.cutover.contract_sha256 | test("^[0-9a-f]{64}$")) and
    .cutover.not_before_epoch ==
      ([
        .versioning_enablement.cloudtrail.enabled_at_epoch,
        .versioning_enablement.bedrock.enabled_at_epoch
      ] | max) + $settle_seconds and
    .producer_off.before_observed_at_epoch >=
      .cutover.not_before_epoch and
    (.state_contract | keys | sort) == ["backend","imports","state"] and
    .state_contract.backend.type == "s3" and
    .state_contract.backend.bucket ==
      "teamagent-tfstate-718959508629" and
    .state_contract.backend.key == "teamagent/terraform.tfstate" and
    .state_contract.backend.region == "ap-northeast-1" and
    .state_contract.backend.dynamodb_table == "teamagent-tflock" and
    .state_contract.backend.encrypt == true and
    .state_contract.backend.workspace == "default" and
    (.state_contract.backend.identity_sha256 |
      test("^[0-9a-f]{64}$")) and
    (.state_contract.state.lineage |
      test("^[0-9a-fA-F-]{36}$")) and
    (.state_contract.state.serial | type) == "number" and
    .state_contract.state.serial >= 0 and
    (.state_contract.state.address_count | type) == "number" and
    .state_contract.state.address_count >= 0 and
    (.state_contract.state.address_set_sha256 |
      test("^[0-9a-f]{64}$"))
  ' "$receipt" >/dev/null ||
    die "S3 versioning pre-cutover receiptがproducer-off/timestamp/settle/cutover契約と不一致です"

  jq -e '
    .log_buckets.cloudtrail.versioning_status == "Enabled" and
    .log_buckets.cloudtrail.mfa_delete == "Disabled" and
    .log_buckets.cloudtrail.lifecycle.deletion_rule_count == 0 and
    (.log_buckets.cloudtrail.lifecycle.canonical_sha256 |
      test("^[0-9a-f]{64}$")) and
    .log_buckets.bedrock == {
      versioning_status:"Enabled",
      mfa_delete:"Disabled"
    }
  ' "$snapshot" >/dev/null ||
    die "versioning pre-cutover receipt検証時のbucket/lifecycle状態が不正です"

  jq -e --slurpfile receipt "$receipt" '
    .backend == $receipt[0].state_contract.backend and
    .state.lineage == $receipt[0].state_contract.state.lineage and
    .state.serial >= $receipt[0].state_contract.state.serial
  ' "$state_contract" >/dev/null ||
    die "versioning pre-cutover receipt後にbackend/workspace/lineageが変化またはstate serialが後退しました"

  [ "$(jq -S -c '.producer_off.contract' "$receipt" | sha256_text)" = \
    "$(jq -er '.producer_off.contract_sha256' "$receipt")" ] ||
    die "versioning receiptのproducer-off contract hashが不一致です"
  write_log_bucket_identity "$snapshot" "cloudtrail" "$cloudtrail_identity"
  write_log_bucket_identity "$snapshot" "bedrock-logs" "$bedrock_identity"
  [ "$(sha256_file "$cloudtrail_identity")" = \
    "$(jq -er '.buckets.cloudtrail.identity_sha256' "$receipt")" ] ||
    die "CloudTrail bucket/versioning identityがpre-cutover receiptから変化しました"
  [ "$(sha256_file "$bedrock_identity")" = \
    "$(jq -er '.buckets.bedrock.identity_sha256' "$receipt")" ] ||
    die "Bedrock bucket/versioning identityがpre-cutover receiptから変化しました"
  capture_versioning_enablement_contract "$current_enablement"
  jq -e --slurpfile receipt "$receipt" '
    . == $receipt[0].versioning_enablement
  ' "$current_enablement" >/dev/null ||
    die "independent versioning enablement event/timestampがreceiptと一致しません"
  capture_log_delivery_contract "$current_producer"
  [ "$(log_delivery_contract_sha256 "$current_producer")" = \
    "$(jq -er '.cutover.contract_sha256' "$receipt")" ] ||
    die "CloudTrail/Bedrock producerがpre-cutover receiptのexact cutoverと一致しません"
}

verify_bound_export_file() {
  local binding="$1" label="$2"
  local verified="$TMP_ROOT/exact-export-verified-$RANDOM-$RANDOM.json"
  if jq -e '
    .kind == "teamagent-exact-s3-export" and
    .schema_version == 1
  ' "$binding" >/dev/null; then
    run_evidence_helper verify-s3-export \
      --binding "$binding" --fresh-dir "$TMP_ROOT" --output "$verified"
    jq -e '.verified == true' "$verified" >/dev/null ||
      die "$label exact S3 exportのplan/apply再取得検証に失敗しました"
    return 0
  fi
  die "$label legacy arbitrary-byte export bindingは証跡として禁止されています"
}

verify_readiness_export_bindings() {
  local evidence="$1" retention="$2"
  local refs="$TMP_ROOT/readiness-export-bindings-$RANDOM.json"
  local binding="$TMP_ROOT/readiness-export-binding-$RANDOM.json"
  local count index label
  jq -n -e -S \
    --slurpfile evidence "$evidence" \
    --slurpfile retention "$retention" '{
    bindings:([
      {
        label:"cloudtrail latest log",
        value:$evidence[0].cloudtrail.latest_log
      },
      {
        label:"cloudtrail latest digest",
        value:$evidence[0].cloudtrail.latest_digest
      },
      {
        label:"bedrock latest delivery",
        value:$evidence[0].bedrock.latest_delivery
      }
    ] + [
      $retention[0].log_groups[] |
      {
        label:("retention " + .log_group),
        value:.export
      }
    ])
  } |
  if (
    (.bindings | length) == 10 and
    ([.bindings[].value.file.path] | length) ==
      ([.bindings[].value.file.path] | unique | length) and
    ([.bindings[].value.file.identity.inode] | length) ==
      ([.bindings[].value.file.identity.inode] | unique | length)
  ) then .
  else error("non-unique export binding")
  end' > "$refs" ||
    die "delivery/retention export file bindingが一意ではありません"
  count="$(jq -er '.bindings | length' "$refs")"
  index=0
  while [ "$index" -lt "$count" ]; do
    jq -e --argjson index "$index" \
      '.bindings[$index].value' "$refs" > "$binding" ||
      die "export file bindingを読取れません"
    label="$(jq -er --argjson index "$index" \
      '.bindings[$index].label' "$refs")"
    verify_bound_export_file "$binding" "$label"
    index=$((index + 1))
  done
}

verify_log_readiness_receipt() {
  local receipt="$1" versioning_receipt="$2" snapshot="$3"
  local versioning_sha observed_at evidence_path evidence_path_requested
  local evidence_identity retention_path retention_path_requested
  local retention_identity
  local bedrock_retention_live="$TMP_ROOT/bedrock-retention-live-$RANDOM.json"
  versioning_sha="$(sha256_file "$versioning_receipt")"
  observed_at="$(jq -er '
    [
      .workflow.cutover.cloudtrail.response_date_epoch,
      .workflow.cutover.bedrock.response_date_epoch
    ] | max
  ' "$versioning_receipt")"
  jq -e '
    .log_buckets.cloudtrail.versioning_status == "Enabled" and
    .log_buckets.cloudtrail.mfa_delete == "Disabled" and
    .log_buckets.cloudtrail.lifecycle.deletion_rule_count == 0 and
    (.log_buckets.cloudtrail.lifecycle.canonical_sha256 |
      test("^[0-9a-f]{64}$")) and
    .log_buckets.bedrock == {
      versioning_status:"Enabled",
      mfa_delete:"Disabled"
    }
  ' "$snapshot" >/dev/null ||
    die "CloudTrail/Bedrockのlive versioning/lifecycleがreadiness契約と不一致です"
  jq -e \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" \
    --arg versioning_sha "$versioning_sha" \
    --argjson now "$(date +%s)" '
    (keys | sort) == ([
      "account_id",
      "created_at_epoch",
      "evidence_artifact_inode",
      "evidence_artifact_path",
      "evidence_artifact_sha256",
      "evidence_artifact_size_bytes",
      "expires_at_epoch",
      "kind",
      "region",
      "schema_version",
      "versioning_receipt_sha256"
    ] | sort) and
    .kind == "teamagent-log-rollout-readiness-receipt" and
    .schema_version == 3 and
    .account_id == $account and .region == $region and
    .versioning_receipt_sha256 == $versioning_sha and
    (.evidence_artifact_path |
      type == "string" and startswith("/")) and
    (.evidence_artifact_inode |
      type == "string" and test("^[1-9][0-9]*$")) and
    (.evidence_artifact_size_bytes |
      type == "number" and floor == . and . > 0) and
    (.evidence_artifact_sha256 | test("^[0-9a-f]{64}$")) and
    ([.created_at_epoch, .expires_at_epoch] |
      all((type == "number") and (floor == .) and . >= 0)) and
    .created_at_epoch <= $now and
    .expires_at_epoch > $now and
    (.expires_at_epoch - .created_at_epoch) <= 86400
  ' "$receipt" >/dev/null ||
    die "log readiness receiptのschema/versioning bindingが不正です"

  evidence_path_requested="$(jq -er '.evidence_artifact_path' "$receipt")"
  evidence_path="$(secure_existing_file "$evidence_path_requested" 600)"
  [ "$evidence_path" = "$evidence_path_requested" ] ||
    die "log readiness evidenceはcanonical pathで指定してください"
  evidence_identity="$(stat_identity "$evidence_path")"
  [ "$(stat_inode "$evidence_path")" = \
    "$(jq -er '.evidence_artifact_inode' "$receipt")" ] ||
    die "log readiness evidence artifact inodeがreceiptと一致しません"
  [ "$(stat_size "$evidence_path")" = \
    "$(jq -er '.evidence_artifact_size_bytes' "$receipt")" ] ||
    die "log readiness evidence artifact sizeがreceiptと一致しません"
  [ "$(sha256_file "$evidence_path")" = \
    "$(jq -er '.evidence_artifact_sha256' "$receipt")" ] ||
    die "log readiness evidence artifact SHAがreceiptと一致しません"
  retention_path_requested="$(
    jq -er '.retention_export_manifest_path' "$evidence_path"
  )"
  retention_path="$(secure_existing_file "$retention_path_requested" 600)"
  [ "$retention_path" = "$retention_path_requested" ] ||
    die "retention export manifestはcanonical pathで指定してください"
  retention_identity="$(stat_identity "$retention_path")"
  [ "$(stat_inode "$retention_path")" = \
    "$(jq -er '.retention_export_manifest_inode' "$evidence_path")" ] ||
    die "retention export manifest inodeがevidence artifactと一致しません"
  [ "$(stat_size "$retention_path")" = \
    "$(jq -er '.retention_export_manifest_size_bytes' "$evidence_path")" ] ||
    die "retention export manifest sizeがevidence artifactと一致しません"
  [ "$(sha256_file "$retention_path")" = \
    "$(jq -er '.retention_export_manifest_sha256' "$evidence_path")" ] ||
    die "retention export manifest SHAがevidence artifactと一致しません"

  jq -e \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" \
    --arg cloudtrail \
      "${PROJECT}-${ENVIRONMENT}-cloudtrail-${EXPECTED_ACCOUNT_ID}" \
    --arg bedrock \
      "${PROJECT}-${ENVIRONMENT}-bedrock-logs-${EXPECTED_ACCOUNT_ID}" \
    --arg retention_path "$retention_path" \
    --arg retention_sha "$(sha256_file "$retention_path")" \
    --argjson pre_cutover_observed_at "$observed_at" \
    --argjson receipt_created_at "$(jq -er '.created_at_epoch' "$receipt")" \
    --argjson now "$(date +%s)" \
    --slurpfile retention "$retention_path" '
    def delivery($prefix; $observation):
      (keys | sort) == ([
        "account_id",
        "file",
        "fresh_nonce",
        "fresh_nonce_sha256",
        "kind",
        "observed_at_epoch",
        "region",
        "s3",
        "schema_version"
      ] | sort) and
      .kind == "teamagent-exact-s3-export" and
      .schema_version == 1 and
      .account_id == $account and .region == $region and
      (.s3.key | type == "string" and startswith($prefix)) and
      (.s3.version_id |
        type == "string" and test("^[A-Za-z0-9._-]{1,1024}$")) and
      (.s3.etag | type == "string" and
        test("^\"?[0-9a-fA-F]{32}(-[0-9]+)?\"?$")) and
      (.s3.content_length | type) == "number" and
      .s3.content_length > 0 and
      (.s3.checksums | type) == "object" and
      (.s3.head_request_id_sha256 | test("^[0-9a-f]{64}$")) and
      (.s3.get_request_id_sha256 | test("^[0-9a-f]{64}$")) and
      (.file.content_sha256 | test("^[0-9a-f]{64}$")) and
      (.file.path |
        type == "string" and startswith("/") and
        (test("[\u0000-\u001f\u007f]") | not)) and
      (.file.acquisition_identity_before | keys | sort) ==
        ([
          "birthtime_ns",
          "ctime_ns",
          "device",
          "inode",
          "mode",
          "mtime_ns",
          "nlink",
          "path",
          "size",
          "uid"
        ] | sort) and
      .file.acquisition_identity_before.path == .file.path and
      .file.acquisition_identity_before.device ==
        .file.identity.device and
      .file.acquisition_identity_before.inode ==
        .file.identity.inode and
      .file.acquisition_identity_before.nlink == 1 and
      .file.acquisition_identity_before.size == 0 and
      .file.acquisition_identity_before.mode == 384 and
      (.file.acquisition_identity_before.mtime_ns | type) == "number" and
      (.file.acquisition_identity_before.ctime_ns | type) == "number" and
      ((.file.acquisition_identity_before.birthtime_ns == null) or
       ((.file.acquisition_identity_before.birthtime_ns | type) ==
        "number")) and
      (.file.identity.device | type) == "number" and
      (.file.identity.inode | type) == "number" and
      .file.identity.path == .file.path and
      .file.identity.nlink == 1 and
      .file.identity.mode == 384 and
      (.file.identity.size | type) == "number" and
      .file.identity.size == .s3.content_length and
      (.file.identity.mtime_ns | type) == "number" and
      (.file.identity.ctime_ns | type) == "number" and
      ((.file.identity.birthtime_ns == null) or
       ((.file.identity.birthtime_ns | type) == "number")) and
      (.fresh_nonce | test("^[0-9a-f]{64}$")) and
      (.fresh_nonce_sha256 | test("^[0-9a-f]{64}$")) and
      (.observed_at_epoch | type) == "number" and
      .observed_at_epoch <= $observation and
      .s3.last_modified_epoch >= $pre_cutover_observed_at and
      .s3.last_modified_epoch <= .observed_at_epoch and
      .s3.head_aws_date_epoch <= .observed_at_epoch and
      .s3.get_aws_date_epoch <= .observed_at_epoch and
      .observed_at_epoch <= $now;
    .observed_at_epoch as $evidence_observed_at |
    (keys | sort) == ([
      "account_id",
      "bedrock",
      "cloudtrail",
      "kind",
      "observed_at_epoch",
      "pre_cutover_observed_at_epoch",
      "region",
      "retention_export_manifest_inode",
      "retention_export_manifest_path",
      "retention_export_manifest_sha256",
      "retention_export_manifest_size_bytes",
      "schema_version"
    ] | sort) and
    .kind == "teamagent-log-readiness-evidence" and
    .schema_version == 2 and
    .account_id == $account and .region == $region and
    .pre_cutover_observed_at_epoch == $pre_cutover_observed_at and
    (.observed_at_epoch | type) == "number" and
    (.observed_at_epoch | floor) == .observed_at_epoch and
    .observed_at_epoch >= .pre_cutover_observed_at_epoch and
    .observed_at_epoch <= $now and
    .observed_at_epoch == $receipt_created_at and
    .retention_export_manifest_path == $retention_path and
    .retention_export_manifest_sha256 == $retention_sha and
    (.retention_export_manifest_inode |
      type == "string" and test("^[1-9][0-9]*$")) and
    (.retention_export_manifest_size_bytes |
      type == "number" and floor == . and . > 0) and
    (.cloudtrail | keys | sort) ==
      ["bucket","latest_digest","latest_log"] and
    .cloudtrail.bucket == $cloudtrail and
    (.cloudtrail.latest_log |
      delivery(
        "AWSLogs/" + $account + "/CloudTrail/";
        $evidence_observed_at
      )) and
    (.cloudtrail.latest_digest |
      delivery(
        "AWSLogs/" + $account + "/CloudTrail-Digest/";
        $evidence_observed_at
      )) and
    (.bedrock | keys | sort) ==
      ["bucket","latest_delivery","retention_live"] and
    .bedrock.bucket == $bedrock and
    (.bedrock.latest_delivery |
      delivery(
        "bedrock/AWSLogs/" + $account +
        "/BedrockModelInvocationLogs/";
        $evidence_observed_at
      )) and
    .bedrock.retention_live.kind ==
      "teamagent-bedrock-retention-live-evidence" and
    .bedrock.retention_live.schema_version == 1 and
    (.bedrock.retention_live.contract_sha256 |
      test("^[0-9a-f]{64}$")) and
    .bedrock.retention_live.contract.bucket == $bedrock and
    .bedrock.retention_live.contract.current_expiration_days == 60 and
    .bedrock.retention_live.contract.noncurrent_expiration_days == 60 and
    .bedrock.retention_live.contract.manual_delete_denied == true and
    .bedrock.retention_live.contract.writer_service ==
      "bedrock.amazonaws.com" and
    (.bedrock.retention_live.contract.lifecycle_sha256 |
      test("^[0-9a-f]{64}$")) and
    (.bedrock.retention_live.contract.policy_sha256 |
      test("^[0-9a-f]{64}$")) and
    .bedrock.retention_live.contract.observed_at_epoch <=
      $evidence_observed_at and
    ($retention[0] | keys | sort) == ([
      "account_id",
      "created_at_epoch",
      "kind",
      "log_groups",
      "region",
      "schema_version"
    ] | sort) and
    $retention[0].kind ==
      "teamagent-log-retention-export-manifest" and
    $retention[0].schema_version == 2 and
    $retention[0].account_id == $account and
    $retention[0].region == $region and
    ($retention[0].created_at_epoch | type) == "number" and
    ($retention[0].created_at_epoch | floor) ==
      $retention[0].created_at_epoch and
    $retention[0].created_at_epoch >= $pre_cutover_observed_at and
    $retention[0].created_at_epoch == .observed_at_epoch and
    ($retention[0].log_groups | type) == "array" and
    ($retention[0].log_groups | map(.log_group) | sort) == ([
      "/aws/codebuild/teamagent-dev-aiia-image-builder",
      "/aws/codebuild/teamagent-dev-image-builder",
      "/aws/ecs/containerinsights/teamagent-dev/performance",
      "/aws/ecs/containerinsights/teamagent-dev-tiktok/performance",
      "/aws/lambda/teamagent-dev-reminders-notify",
      "/aws/lambda/teamagent-dev-tiktok-acquire-dispatch",
      "/aws/lambda/teamagent-dev-x-buzz-dispatch"
    ] | sort) and
    ($retention[0].log_groups | length) == 7 and
    all($retention[0].log_groups[];
      (keys | sort) ==
        ([
          "event_count",
          "export",
          "exported_through_epoch",
          "log_group"
        ] | sort) and
      (.export | delivery("cloudwatch-logs-export/"; $evidence_observed_at)) and
      (.event_count | type) == "number" and
      (.event_count | floor) == .event_count and .event_count > 0 and
      (.exported_through_epoch | type) == "number" and
      (.exported_through_epoch | floor) == .exported_through_epoch and
      .exported_through_epoch >= $pre_cutover_observed_at and
      .exported_through_epoch <= $retention[0].created_at_epoch and
      .exported_through_epoch <= $evidence_observed_at
    )
  ' "$evidence_path" >/dev/null ||
    die "log readiness evidence/retention exportの内容または時刻が不正です"
  run_evidence_helper verify-bedrock-retention \
    --output "$bedrock_retention_live"
  jq -e --slurpfile evidence "$evidence_path" '
    (.contract.observed_at_epoch >=
      $evidence[0].bedrock.retention_live.contract.observed_at_epoch) and
    (del(.contract.observed_at_epoch, .contract_sha256) ==
      ($evidence[0].bedrock.retention_live |
        del(.contract.observed_at_epoch, .contract_sha256)))
  ' "$bedrock_retention_live" >/dev/null ||
    die "Bedrock current/noncurrent minimum-60-day live contractが変化しました"

  verify_readiness_export_bindings "$evidence_path" "$retention_path"
  [ "$(sha256_file "$evidence_path")" = \
    "$(jq -er '.evidence_artifact_sha256' "$receipt")" ] &&
    [ "$(stat_identity "$evidence_path")" = "$evidence_identity" ] &&
    [ "$(stat_inode "$evidence_path")" = \
      "$(jq -er '.evidence_artifact_inode' "$receipt")" ] &&
    [ "$(stat_size "$evidence_path")" = \
      "$(jq -er '.evidence_artifact_size_bytes' "$receipt")" ] ||
    die "検証中にlog readiness evidence artifactが差替えられました"
  [ "$(sha256_file "$retention_path")" = \
    "$(jq -er '.retention_export_manifest_sha256' "$evidence_path")" ] &&
    [ "$(stat_identity "$retention_path")" = "$retention_identity" ] &&
    [ "$(stat_inode "$retention_path")" = \
      "$(jq -er '.retention_export_manifest_inode' "$evidence_path")" ] &&
    [ "$(stat_size "$retention_path")" = \
      "$(jq -er '.retention_export_manifest_size_bytes' "$evidence_path")" ] ||
    die "検証中にretention export manifestが差替えられました"
  verify_readiness_export_bindings "$evidence_path" "$retention_path"
}

run_evidence_helper() {
  initialize_aws_trust
  need_cmd python3
  assert_aws_trust_unchanged
  python3 "$EVIDENCE_HELPER" --aws-bin "$AWS_BIN" "$@"
  assert_aws_trust_unchanged
}

new_uuid_v4() {
  need_cmd python3
  python3 -c 'import uuid; print(uuid.uuid4())'
}

capture_image_release_context() {
  local plan="$1" output="$2"
  need_cmd python3
  python3 "$IMAGE_CONTEXT_HELPER" capture \
    --terraform-dir "$TF_DIR" \
    --plan "$plan" \
    --output "$output"
  chmod 600 "$output"
  jq -e . "$output" >/dev/null ||
    die "production provenance Terraform context生成に失敗しました"
}

build_sync_image_deployment_consumer_manifest() {
  local state="$1" snapshot="$2" core="$3" output="$4"
  need_cmd python3
  jq -L "$GUARD_JQ_DIR" -e \
    --slurpfile registry "$IMAGE_DEPLOYMENT_CONSUMER_REGISTRY" \
    --slurpfile live "$snapshot" '
    include "terraform_runtime_guard";
    def instance_address($resource; $instance):
      (
        (if (($resource.module // "") == "") then
           ""
         else
           ($resource.module + ".")
         end) +
        (if ($resource.mode // "managed") == "managed" then
           ""
         elif $resource.mode == "data" then
           "data."
         else
           error("unsupported state resource mode")
         end) +
        $resource.type + "." + $resource.name +
        (
          if ($instance | has("index_key")) then
            if ($instance.index_key | type) == "number" then
              "[" + ($instance.index_key | tostring) + "]"
            elif ($instance.index_key | type) == "string" then
              "[" + ($instance.index_key | tojson) + "]"
            else
              error("unsupported state index key")
            end
          else
            ""
          end
        )
      );
    def live_task($id):
      if $id == "mcp" then $live[0].taskdefs.mcp.critical
      elif $id == "connect_web" then
        $live[0].taskdefs.connect_web.critical
      elif $id == "openclaw" then $live[0].taskdefs.openclaw.critical
      elif $id == "canary" then $live[0].taskdefs.canary.critical
      elif $id == "ingest" then $live[0].taskdefs.ingest.critical
      elif $id == "morning_digest" then
        $live[0].taskdefs.morning.critical
      elif $id == "x_buzz_worker" then
        $live[0].taskdefs.x_buzz.critical
      elif $id == "tiktok_acquire" then
        $live[0].taskdefs.tiktok.critical
      else error("consumer outside code-owned live task snapshot")
      end;
    ([
      .resources[] as $resource |
      select(($resource.mode // "managed") == "managed") |
      ($resource.instances // [])[] as $instance |
      {
        address:instance_address($resource; $instance),
        type:$resource.type,
        attributes:$instance.attributes
      }
    ]) as $state_instances |
    $registry[0].schema_version == 1 and
    ($registry[0].consumers | length) == 8 and
    all($registry[0].consumers[];
      . as $consumer |
      ([
        $state_instances[] |
        select(
          .address ==
            $consumer.terraform_task_definition_address
        )
      ]) as $matches |
      ($matches | length) == 1 and
      $matches[0].type == "aws_ecs_task_definition" and
      ($matches[0].attributes | guard_task_from_tf) ==
        live_task($consumer.consumer_id)
    )
  ' "$state" >/dev/null ||
    die "sync用consumerのTerraform state full task bodyがlive AWS snapshotと一致しません"
  python3 "$IMAGE_CONTEXT_HELPER" build-sync-consumer-manifest \
    --state "$state" \
    --output "$output" ||
    die "sync用consumer manifestをlive Terraform stateから生成できません"
  chmod 600 "$output"
  jq -e --slurpfile live "$snapshot" --slurpfile core "$core" '
    def live_binding($id):
      if $id == "mcp" then {
        image:$live[0].taskdefs.mcp.image,
        task_definition_arn:$live[0].taskdefs.mcp.arn,
        activation:{
          desired_count:$live[0].services.mcp.critical.desired_count,
          task_definition_arn:$live[0].services.mcp.task_definition
        }
      }
      elif $id == "connect_web" then {
        image:$live[0].taskdefs.connect_web.image,
        task_definition_arn:$live[0].taskdefs.connect_web.arn,
        activation:{
          desired_count:$live[0].services.connect_web.critical.desired_count,
          task_definition_arn:$live[0].services.connect_web.task_definition
        }
      }
      elif $id == "openclaw" then {
        image:$live[0].taskdefs.openclaw.image,
        task_definition_arn:$live[0].taskdefs.openclaw.arn,
        activation:{
          desired_count:$live[0].services.openclaw.critical.desired_count,
          task_definition_arn:$live[0].services.openclaw.task_definition
        }
      }
      elif $id == "canary" then {
        image:$live[0].taskdefs.canary.image,
        task_definition_arn:$live[0].taskdefs.canary.arn,
        activation:{
          state:$live[0].rules.canary.critical.state,
          task_definition_arn:$live[0].targets.canary.task_definition
        }
      }
      # ACTIVATION-SHIM(ingest): 一時対応。Activation 完了後に canonical registry と
      # release_evidence を原子的に正名化して撤去する。docs/activation/ACTIVATION_STATE.md 参照。
      elif $id == "ingest" then {
        image:$live[0].taskdefs.ingest.image,
        task_definition_arn:$live[0].taskdefs.ingest.arn,
        activation:{
          state:$live[0].rules.ingest.critical.state,
          task_definition_arn:
            $live[0].rule_dispatchers.ingest.task_definition
        }
      }
      elif $id == "morning_digest" then {
        image:$live[0].taskdefs.morning.image,
        task_definition_arn:$live[0].taskdefs.morning.arn,
        activation:{
          state:$live[0].rules.morning.critical.state,
          task_definition_arn:$live[0].targets.morning.task_definition
        }
      }
      elif $id == "x_buzz_worker" then {
        image:$live[0].taskdefs.x_buzz.image,
        task_definition_arn:$live[0].taskdefs.x_buzz.arn,
        activation:{
          event_source_mapping_enabled:
            $live[0].event_mappings.x_buzz.critical.enabled,
          task_definition_arn:$live[0].dispatchers.x_buzz.task_definition
        }
      }
      elif $id == "tiktok_acquire" then {
        image:$live[0].taskdefs.tiktok.image,
        task_definition_arn:$live[0].taskdefs.tiktok.arn,
        activation:{
          event_source_mapping_enabled:
            $live[0].event_mappings.tiktok.critical.enabled,
          task_definition_arn:$live[0].dispatchers.tiktok.task_definition
        }
      }
      else error("consumer outside code-owned live snapshot")
      end;
    .schema_version == 1 and
    .mode == "no-image-transition" and
    (.registry_sha256 | test("^[0-9a-f]{64}$")) and
    (.consumers | type) == "array" and
    (.consumers | length) == 8 and
    ([.consumers[].consumer_id] | length) ==
      ([.consumers[].consumer_id] | unique | length) and
    all(.consumers[]; .live == .before and .before == .after) and
    all(.consumers[];
      . as $consumer |
      live_binding($consumer.consumer_id) as $expected |
      $consumer.live.image == $expected.image and
      $consumer.live.task_definition_arn ==
        $expected.task_definition_arn and
      $consumer.live.activation == $expected.activation
    ) and
    ([
      .consumers[] |
      {key:.consumer_id,value:.after.image}
    ] | from_entries) == $core[0].desired_consumer_images
  ' "$output" >/dev/null ||
    die "sync用consumer manifestがlive AWS/state snapshot由来のexact-8 no-image-transition契約と不一致です"
}

build_scoped_release_live_contract() {
  local context="$1" snapshot="$2" output="$3"
  jq -n -S \
    --slurpfile context "$context" \
    --slurpfile live "$snapshot" '
    def execution_state($consumer; $phase):
      if $consumer.activator.type == "ecs_service" then
        $consumer[$phase].activation.desired_count
      elif $consumer.activator.type == "eventbridge_rule_ecs_target" then
        $consumer[$phase].activation.state
      elif $consumer.activator.type ==
        "lambda_taskdef_arn_environment" then
        $consumer[$phase].activation.event_source_mapping_enabled
      else error("consumer outside code-owned activator types")
      end;
    def changes_release_contract($consumer):
      if (($consumer.before.absent // false) or
        ($consumer.after.absent // false)) then
        ($consumer.before.absent // false) !=
          ($consumer.after.absent // false)
      else
        $consumer.before.image != $consumer.after.image or
        $consumer.before.task_definition !=
          $consumer.after.task_definition or
        execution_state($consumer; "before") !=
          execution_state($consumer; "after")
      end;
    def binding($id):
      if $id == "mcp" then {
        task:$live[0].taskdefs.mcp,
        state:$live[0].services.mcp.critical.desired_count
      }
      elif $id == "connect_web" then {
        task:$live[0].taskdefs.connect_web,
        state:$live[0].services.connect_web.critical.desired_count
      }
      elif $id == "openclaw" then {
        task:$live[0].taskdefs.openclaw,
        state:$live[0].services.openclaw.critical.desired_count
      }
      elif $id == "canary" then {
        task:$live[0].taskdefs.canary,
        state:$live[0].rules.canary.critical.state
      }
      elif $id == "ingest" then {
        task:$live[0].taskdefs.ingest,
        state:$live[0].rules.ingest.critical.state
      }
      elif $id == "morning_digest" then {
        task:$live[0].taskdefs.morning,
        state:$live[0].rules.morning.critical.state
      }
      elif $id == "x_buzz_worker" then {
        task:$live[0].taskdefs.x_buzz,
        state:$live[0].event_mappings.x_buzz.critical.enabled
      }
      elif $id == "tiktok_acquire" then {
        task:$live[0].taskdefs.tiktok,
        state:$live[0].event_mappings.tiktok.critical.enabled
      }
      else error("consumer outside code-owned release context")
      end;
    {
      images:{
        mcp:$live[0].taskdefs.mcp.image,
        connect_web:$live[0].taskdefs.connect_web.image,
        openclaw:$live[0].taskdefs.openclaw.image,
        canary:$live[0].taskdefs.canary.image,
        ingest:$live[0].taskdefs.ingest.image,
        morning_digest:$live[0].taskdefs.morning.image,
        x_buzz_worker:$live[0].taskdefs.x_buzz.image,
        tiktok_acquire:$live[0].taskdefs.tiktok.image
      },
      resources:([
        $context[0].consumer_manifest.consumers[] |
        select(
          (.before.absent // false) == false and
          (.after.absent // false) == false and
          changes_release_contract(.)
        ) |
        . as $owner |
        binding($owner.consumer_id) as $binding |
        {
          activation:{
            identity:$owner.activator.identity,
            state:$binding.state,
            type:$owner.activator.type
          },
          consumer_id:$owner.consumer_id,
          image:$binding.task.image,
          pipeline:$owner.receipt.pipeline,
          subject:$owner.receipt.subject,
          terraform_address:$owner.terraform_task_definition_address,
          task_definition_arn:$binding.task.arn
        }
      ] | sort_by(.consumer_id)),
      rule_states:{
        ingest:$live[0].rules.ingest.critical.state,
        morning:$live[0].rules.morning.critical.state,
        canary:$live[0].rules.canary.critical.state
      }
    }
  ' > "$output" ||
    die "scope内consumerのlive release契約を生成できません"
  jq -e --slurpfile context "$context" '
    def execution_state($consumer; $phase):
      if $consumer.activator.type == "ecs_service" then
        $consumer[$phase].activation.desired_count
      elif $consumer.activator.type == "eventbridge_rule_ecs_target" then
        $consumer[$phase].activation.state
      elif $consumer.activator.type ==
        "lambda_taskdef_arn_environment" then
        $consumer[$phase].activation.event_source_mapping_enabled
      else error("consumer outside code-owned activator types")
      end;
    def changes_release_contract($consumer):
      if (($consumer.before.absent // false) or
        ($consumer.after.absent // false)) then
        ($consumer.before.absent // false) !=
          ($consumer.after.absent // false)
      else
        $consumer.before.image != $consumer.after.image or
        $consumer.before.task_definition !=
          $consumer.after.task_definition or
        execution_state($consumer; "before") !=
          execution_state($consumer; "after")
      end;
    (.images | keys | sort) == ([
      "canary",
      "connect_web",
      "ingest",
      "mcp",
      "morning_digest",
      "openclaw",
      "tiktok_acquire",
      "x_buzz_worker"
    ] | sort) and
    (.rule_states | keys | sort) == ["canary","ingest","morning"] and
    (.resources | type) == "array" and
    .resources == (.resources | sort_by(.consumer_id)) and
    ([.resources[].consumer_id] | unique | length) ==
      (.resources | length) and
    (.resources | map(.consumer_id)) ==
      ([
        $context[0].consumer_manifest.consumers[] |
        select(
          (.before.absent // false) == false and
          (.after.absent // false) == false and
          changes_release_contract(.)
        ) |
        .consumer_id
      ] | sort) and
    all(.resources[];
      (keys | sort) == ([
        "activation",
        "consumer_id",
        "image",
        "pipeline",
        "subject",
        "task_definition_arn",
        "terraform_address"
      ] | sort) and
      (.activation | keys | sort) == ["identity","state","type"] and
      (.task_definition_arn |
        test(
          "^arn:aws:ecs:ap-northeast-1:718959508629:"
          + "task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*$"
        )) and
      (.image |
        test(
          "^718959508629[.]dkr[.]ecr[.]ap-northeast-1[.]amazonaws[.]com/"
          + "[a-z0-9._/-]+@sha256:[0-9a-f]{64}$"
        )) and
      (
        if .activation.type == "ecs_service" then
          (.activation.state |
            type == "number" and . >= 0 and floor == .)
        elif .activation.type == "eventbridge_rule_ecs_target" then
          (.activation.state == "ENABLED" or
            .activation.state == "DISABLED")
        elif .activation.type == "lambda_taskdef_arn_environment" then
          (.activation.state | type) == "boolean"
        else false
        end
      )
    )
  ' "$output" >/dev/null ||
    die "scope内consumerのlive release契約が不正です"
}

build_scoped_release_state_contract() {
  local state_contract="$1" live_contract="$2" output="$3"
  local fresh="$TMP_ROOT/state-contract-fresh-scoped-$RANDOM.json"
  capture_state_contract "$fresh" "$live_contract"
  jq -e --slurpfile base "$state_contract" '
    (del(.task_revisions)) ==
      ($base[0] | del(.task_revisions)) and
    (.task_revisions | type) == "object" and
    all(.task_revisions[];
      type == "number" and . >= 1 and floor == .)
  ' "$fresh" >/dev/null ||
    die "scope内consumerのTerraform state metadataが観測間で変化しました"
  mv "$fresh" "$output"
}

validate_image_release_context_consumer_images() {
  local context="$1"
  local expected_consumer_images="$2"
  jq -e --argjson expected "$expected_consumer_images" '
    (.consumer_manifest.consumers | type) == "array" and
    (.consumer_manifest.consumers | length) == 8 and
    ([.consumer_manifest.consumers[].consumer_id] | length) ==
      ([.consumer_manifest.consumers[].consumer_id] | unique | length) and
    ([
      .consumer_manifest.consumers[] |
      {key:.consumer_id,value:.after.image}
    ] | from_entries) == $expected
  ' "$context" >/dev/null ||
    die "gate検証済みconsumer manifestの8件のafter.imageがguardのconsumer別期待値と一致しません"
}

prepare_image_deployment_intent() {
  local plan="$1" context="$2" output="$3"
  bash "$IMAGE_GATE_RUNNER" prepare-deployment-intent \
    --plan "$plan" \
    --control-commit "$(git_commit)" \
    --terraform-context "$context" > "$output"
  chmod 600 "$output"
  jq -e \
    --arg plan_sha "$(sha256_file "$plan")" '
    (.intent_id |
      test("^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")) and
    .plan_sha256 == $plan_sha and
    (.authorization_expires_at | type) == "number"
  ' "$output" >/dev/null ||
    die "one-use production deployment intentのbindingが不正です"
}

capture_complete_runtime_inventory() {
  local output="$1"
  local raw="$TMP_ROOT/runtime-inventory-raw-$RANDOM.json"
  local stable="$TMP_ROOT/runtime-inventory-stable-$RANDOM.json"
  run_evidence_helper inventory --output "$raw"
  jq -e -S \
    --arg email_sha "$EXPECTED_ALARM_EMAIL_SHA256" \
    --arg destination_sha "$EXPECTED_ALARM_DESTINATION_STATE_SHA256" '
    if (
      .kind == "teamagent-runtime-inventory" and
      .schema_version == 1 and
      .raw_endpoint_utf8_sha256 == $email_sha and
      .destination_state_sha256 == $destination_sha and
      (.subscription_metadata_sha256 | test("^[0-9a-f]{64}$")) and
      .alarm_subscription_count == 1 and
      .chatbot_configuration_count >= 0 and
      (.publisher_coverage | type) == "array" and
      (.source_pages_sha256 | test("^[0-9a-f]{64}$")) and
      (.raw_reference_set_sha256 | test("^[0-9a-f]{64}$")) and
      (.publisher_reference_set_sha256 | test("^[0-9a-f]{64}$")) and
      (.publishers_sha256 | test("^[0-9a-f]{64}$")) and
      (.inventory_sha256 | test("^[0-9a-f]{64}$"))
    ) then
      {
        destination_state_sha256: .destination_state_sha256,
        subscription_metadata_sha256: .subscription_metadata_sha256,
        raw_endpoint_utf8_sha256: .raw_endpoint_utf8_sha256,
        raw_reference_set_sha256: .raw_reference_set_sha256,
        publisher_reference_set_sha256: .publisher_reference_set_sha256,
        publishers_sha256: .publishers_sha256,
        publisher_coverage: .publisher_coverage,
        topic_inventory: .topic_inventory,
        alarm_subscription_count: .alarm_subscription_count
      }
    else
      error("runtime inventory contract mismatch")
    end
  ' "$raw" > "$stable" ||
    die "all-page runtime/SNS publisher inventoryがexact contractを満たしません"
  local stable_sha
  stable_sha="$(sha256_file "$stable")"
  jq -e -S --arg stable_sha "$stable_sha" \
    '. + {inventory_sha256: $stable_sha}' "$stable" > "$output" ||
    die "stable runtime/SNS publisher inventory hash生成に失敗しました"
  rm -f "$stable"
}

verify_alarm_delivery_test_receipt() {
  local receipt="$1" snapshot="$2"
  local challenge="$TMP_ROOT/alarm-challenge-verify-$RANDOM.json"
  local ack="$TMP_ROOT/alarm-recipient-ack-verify-$RANDOM.json"
  local verified="$TMP_ROOT/alarm-delivery-live-verify-$RANDOM.json"
  jq -e \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" \
    --arg topic \
      "arn:aws:sns:${REGION}:${EXPECTED_ACCOUNT_ID}:${PROJECT}-${ENVIRONMENT}-openclaw-alarms" \
    --arg raw_email "$EXPECTED_ALARM_EMAIL" \
    --arg email_sha "$EXPECTED_ALARM_EMAIL_SHA256" \
    --arg destination_sha "$EXPECTED_ALARM_DESTINATION_STATE_SHA256" \
    --argjson now "$(date +%s)" '
    .kind == "teamagent-alarm-delivery-test-receipt" and
    .schema_version == 4 and
    .account_id == $account and .region == $region and
    .topic_arn == $topic and
    .raw_email == $raw_email and
    (.raw_email | explode) == ($raw_email | explode) and
    .raw_email_utf8_sha256 == $email_sha and
    .destination_state_sha256 == $destination_sha and
    (.subscription_metadata_sha256 | test("^[0-9a-f]{64}$")) and
    (.message_id_sha256 | test("^[0-9a-f]{64}$")) and
    (.challenge_nonce_sha256 | test("^[0-9a-f]{64}$")) and
    (.inventory_sha256 | test("^[0-9a-f]{64}$")) and
    (.raw_reference_set_sha256 | test("^[0-9a-f]{64}$")) and
    (.recipient_ack_claims_sha256 | test("^[0-9a-f]{64}$")) and
    (.recipient_ack_signature_sha256 | test("^[0-9a-f]{64}$")) and
    (.receipt_claims_sha256 | test("^[0-9a-f]{64}$")) and
    ([.published_at_epoch,.received_at_epoch,.verified_at_epoch,
      .observed_at_epoch,.ledger_ack_aws_date_epoch,
      .final_observed_at_epoch] |
      all((type == "number") and (floor == .) and . >= 0 and
          . <= $now)) and
    .published_at_epoch <= .received_at_epoch and
    .received_at_epoch <= .verified_at_epoch and
    .verified_at_epoch <= .observed_at_epoch and
    .observed_at_epoch <= .ledger_ack_aws_date_epoch and
    .ledger_ack_aws_date_epoch <= .final_observed_at_epoch and
    .final_observed_at_epoch <= $now and
    .expires_at_epoch > $now and
    (.expires_at_epoch - .published_at_epoch) <= 3600 and
    (.challenge | type) == "object" and
    (.recipient_ack | type) == "object"
  ' "$receipt" >/dev/null ||
    die "fresh SNS challenge/managed KMS recipient ack receiptが不正です"
  jq -e -S '.challenge' "$receipt" > "$challenge"
  jq -e -S '.recipient_ack' "$receipt" > "$ack"
  run_evidence_helper verify-sns-delivery \
    --challenge "$challenge" --ack "$ack" --receipt "$receipt" \
    --output "$verified"
  jq -e '.verified == true' "$verified" >/dev/null ||
    die "SNS challenge receiptの署名/inventory/one-use ledger再検証に失敗しました"
  jq -e \
    --arg email_sha "$EXPECTED_ALARM_EMAIL_SHA256" \
    --arg destination_sha "$EXPECTED_ALARM_DESTINATION_STATE_SHA256" \
    --slurpfile receipt "$receipt" '
    .alarm_delivery.confirmed_email_endpoint_sha256 == [$email_sha] and
    .alarm_delivery.subscription_inventory_count == 1 and
    .alarm_delivery.pending_subscription_count == 0 and
    .alarm_delivery.subscription_protocols == ["email"] and
    .alarm_delivery.destination_state_sha256 == $destination_sha and
    .alarm_delivery.destination_state_sha256 ==
      $receipt[0].destination_state_sha256 and
    .alarm_delivery.attached_chatbot_configuration_arns == [] and
    .alarm_delivery_observation.attached_chatbot_configurations == []
  ' "$snapshot" >/dev/null ||
    die "SNS receiptとraw approved emailだけのexclusive destinationが不一致です"
}

verify_alarm_migration_final_receipt() {
  local receipt="$1"
  local verified="$TMP_ROOT/alarm-migration-final-$RANDOM.json"
  run_evidence_helper verify-alarm-migration-final \
    --migration-id "2026-07-alarm-topic-consolidation-v1" \
    --receipt "$receipt" --output "$verified"
  jq -e \
    --arg migration_id "2026-07-alarm-topic-consolidation-v1" '
    .kind == "teamagent-alarm-migration-final-verification" and
    .schema_version == 1 and
    .migration_id == $migration_id and
    .phase == "legacy_retired" and
    (.checkpoint_sha256 | test("^[0-9a-f]{64}$")) and
    (.history_sha256 | test("^[0-9a-f]{64}$")) and
    (.inventory_sha256 | test("^[0-9a-f]{64}$")) and
    (.verified_at_epoch | type) == "number" and
    (.verified_at_epoch | floor) == .verified_at_epoch
  ' "$verified" >/dev/null ||
    die "alarm publisher migrationのdurable final checkpointを検証できません"
}

# Schema v4 can only be produced by the guard-owned first-time workflow:
# Unversioned -> fresh
# disconnect -> PutBucketVersioning -> 900-second no-write window -> two
# observations -> final recheck -> producer cutover under one lock.
verify_versioning_attestation_receipt() {
  local receipt="$1" snapshot="$2" state_contract="$3"
  local workflow="$TMP_ROOT/versioning-workflow-verify-$RANDOM.json"
  local verified="$TMP_ROOT/versioning-workflow-live-$RANDOM.json"
  jq -e \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" \
    --arg stage "2026-07-log-versioning-cutover-v4" \
    --arg guard_version "$GUARD_VERSION" \
    --arg git_commit "$(git_commit)" \
    --arg guard_script_sha256 "$(sha256_file "$SCRIPT_PATH")" \
    --arg guard_jq_sha256 "$(sha256_file "$GUARD_JQ")" \
    --arg evidence_helper_sha256 "$(sha256_file "$EVIDENCE_HELPER")" \
    --arg migration_manifest_sha256 "$(sha256_file "$MIGRATION_FILE")" \
    --argjson now "$(date +%s)" '
    (keys | sort) == ([
      "account_id",
      "created_at_epoch",
      "evidence_helper_sha256",
      "expires_at_epoch",
      "git_commit",
      "guard_jq_sha256",
      "guard_script_sha256",
      "guard_version",
      "kind",
      "migration_manifest_sha256",
      "region",
      "schema_version",
      "stage_id",
      "state_contract_after",
      "state_contract_before",
      "state_ownership_after_sha256",
      "state_ownership_before_sha256",
      "workflow",
      "workflow_sha256"
    ] | sort) and
    .kind == "teamagent-log-versioning-cutover-receipt" and
    .schema_version == 4 and
    .stage_id == $stage and
    .guard_version == $guard_version and
    .account_id == $account and .region == $region and
    .git_commit == $git_commit and
    .guard_script_sha256 == $guard_script_sha256 and
    .guard_jq_sha256 == $guard_jq_sha256 and
    .evidence_helper_sha256 == $evidence_helper_sha256 and
    .migration_manifest_sha256 == $migration_manifest_sha256 and
    .workflow_sha256 == .workflow.workflow_sha256 and
    (.workflow_sha256 | test("^[0-9a-f]{64}$")) and
    (.state_ownership_before_sha256 | test("^[0-9a-f]{64}$")) and
    (.state_ownership_after_sha256 | test("^[0-9a-f]{64}$")) and
    .state_contract_before == .state_contract_after and
    .state_ownership_before_sha256 == .state_ownership_after_sha256 and
    (.created_at_epoch | type) == "number" and
    (.created_at_epoch | floor) == .created_at_epoch and
    .created_at_epoch <= $now and
    .expires_at_epoch > $now and
    (.expires_at_epoch - .created_at_epoch) <= 86400
  ' "$receipt" >/dev/null ||
    die "first-time versioning/cutover receipt schemaまたはsource bindingが不正です"
  jq -e -S '.workflow' "$receipt" > "$workflow" ||
    die "versioning workflowをreceiptから抽出できません"
  jq -e --slurpfile receipt "$receipt" '
    .backend == $receipt[0].state_contract_after.backend and
    .state.lineage == $receipt[0].state_contract_after.state.lineage and
    .state.serial >= $receipt[0].state_contract_after.state.serial
  ' "$state_contract" >/dev/null ||
    die "versioning cutover後にbackend/workspace/lineageが変化またはstate serialが後退しました"
  jq -e '
    .log_buckets.cloudtrail.versioning_status == "Enabled" and
    .log_buckets.cloudtrail.mfa_delete == "Disabled" and
    .log_buckets.cloudtrail.lifecycle.deletion_rule_count == 0 and
    .log_buckets.bedrock == {
      versioning_status:"Enabled",
      mfa_delete:"Disabled"
    }
  ' "$snapshot" >/dev/null ||
    die "versioning/cutover receipt検証時のbucket/lifecycle状態が不正です"
  run_evidence_helper verify-versioning-cutover \
    --workflow "$workflow" --output "$verified"
  jq -e '.verified == true' "$verified" >/dev/null ||
    die "versioning/cutover live再検証に失敗しました"
}

run_first_time_versioning_cutover() {
  local requested_out="$1"
  local output stage workflow bedrock_config state_before state_after receipt
  local runtime_lock_receipt runtime_lock_release workflow_id
  output="$(secure_new_file "$requested_out")"
  ensure_tmp
  assert_trusted_automation_identity
  local published="false"
  local runtime_lock_acquired="false"
  stage="$(mktemp -d \
    "$(dirname "$output")/.teamagent-versioning-cutover.XXXXXX")"
  chmod 700 "$stage"
  runtime_lock_receipt="$stage/runtime-shared-lock.json"
  runtime_lock_release="$stage/runtime-shared-lock-release.json"
  workflow_id="$(new_uuid_v4)"
  cleanup_first_time_versioning() {
    local status=$?
    set +e
    release_deployment_lock
    if [ "$runtime_lock_acquired" = "true" ] ||
      [ -f "$runtime_lock_receipt" ]; then
      rm -f "$runtime_lock_release"
      run_evidence_helper release-runtime-lock \
        --lock "$runtime_lock_receipt" --output "$runtime_lock_release"
      runtime_lock_acquired="false"
    fi
    if [ "$published" != "true" ]; then
      rm -f "$output"
    fi
    rm -rf "$stage" "$TMP_ROOT"
    exit "$status"
  }
  trap 'cleanup_first_time_versioning' EXIT
  run_evidence_helper acquire-runtime-lock \
    --workflow-id "$workflow_id" --output "$runtime_lock_receipt"
  runtime_lock_acquired="true"
  acquire_deployment_lock

  workflow="$stage/workflow.json"
  bedrock_config="$stage/bedrock-config.json"
  state_before="$stage/state-before.json"
  state_after="$stage/state-after.json"
  receipt="$stage/receipt.json"
  jq -n -S \
    --arg bucket "${PROJECT}-${ENVIRONMENT}-bedrock-logs-${EXPECTED_ACCOUNT_ID}" '{
      textDataDeliveryEnabled:true,
      embeddingDataDeliveryEnabled:true,
      imageDataDeliveryEnabled:false,
      videoDataDeliveryEnabled:false,
      s3Config:{bucketName:$bucket,keyPrefix:"bedrock/"}
    }' > "$bedrock_config"
  chmod 600 "$bedrock_config"
  capture_state_contract "$state_before"
  run_evidence_helper first-time-versioning-cutover \
    --lock-id "lock#teamagent/terraform.tfstate" \
    --lock-receipt "$runtime_lock_receipt" \
    --bedrock-config "$bedrock_config" \
    --output "$workflow"
  capture_state_contract "$state_after"
  cmp -s "$state_before" "$state_after" ||
    die "versioning/cutover workflow中にTerraform state ownershipが変化しました"
  local created expires
  created="$(jq -er '
    [.cutover.cloudtrail.response_date_epoch,
     .cutover.bedrock.response_date_epoch] | max
  ' "$workflow")"
  expires=$((created + 86400))
  jq -n -S \
    --arg kind "teamagent-log-versioning-cutover-receipt" \
    --arg stage_id "2026-07-log-versioning-cutover-v4" \
    --arg guard_version "$GUARD_VERSION" \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" \
    --arg git_commit "$(git_commit)" \
    --arg guard_script_sha256 "$(sha256_file "$SCRIPT_PATH")" \
    --arg guard_jq_sha256 "$(sha256_file "$GUARD_JQ")" \
    --arg evidence_helper_sha256 "$(sha256_file "$EVIDENCE_HELPER")" \
    --arg migration_manifest_sha256 "$(sha256_file "$MIGRATION_FILE")" \
    --arg workflow_sha256 "$(jq -er '.workflow_sha256' "$workflow")" \
    --arg state_before_sha256 "$(sha256_file "$state_before")" \
    --arg state_after_sha256 "$(sha256_file "$state_after")" \
    --argjson created_at_epoch "$created" \
    --argjson expires_at_epoch "$expires" \
    --slurpfile workflow "$workflow" \
    --slurpfile state_before "$state_before" \
    --slurpfile state_after "$state_after" '{
      kind:$kind,
      schema_version:4,
      stage_id:$stage_id,
      guard_version:$guard_version,
      account_id:$account,
      region:$region,
      git_commit:$git_commit,
      guard_script_sha256:$guard_script_sha256,
      guard_jq_sha256:$guard_jq_sha256,
      evidence_helper_sha256:$evidence_helper_sha256,
      migration_manifest_sha256:$migration_manifest_sha256,
      created_at_epoch:$created_at_epoch,
      expires_at_epoch:$expires_at_epoch,
      workflow_sha256:$workflow_sha256,
      workflow:$workflow[0],
      state_ownership_before_sha256:$state_before_sha256,
      state_ownership_after_sha256:$state_after_sha256,
      state_contract_before:$state_before[0],
      state_contract_after:$state_after[0]
    }' > "$receipt"
  chmod 600 "$receipt"
  local receipt_identity
  receipt_identity="$(stat_identity "$receipt")"
  ln "$receipt" "$output" ||
    die "versioning/cutover receipt出力pathを原子的に確保できません"
  [ "$(stat_identity "$output")" = "$receipt_identity" ] ||
    die "versioning/cutover receiptの原子的引渡しに失敗しました"
  chmod 600 "$output"
  published="true"
  release_deployment_lock
  run_evidence_helper release-runtime-lock \
    --lock "$runtime_lock_receipt" --output "$runtime_lock_release"
  runtime_lock_acquired="false"
  trap - EXIT
  rm -rf "$stage" "$TMP_ROOT"
  TMP_ROOT=""
  echo "✅ first-time versioning/no-write/cutover receipt: $output"
}

DEPLOYMENT_LOCK_ID="${PROJECT}/${ENVIRONMENT}/terraform-runtime-deployment"
DEPLOYMENT_LOCK_OWNER=""

acquire_deployment_lock() {
  local now expires item values
  now="$(date +%s)"
  expires=$((now + 7200))
  DEPLOYMENT_LOCK_OWNER="$(
    printf '%s' "$(git_commit):$$:${now}:${RANDOM}:${RANDOM}" | sha256_text
  )"
  item="$TMP_ROOT/deployment-lock-item.json"
  values="$TMP_ROOT/deployment-lock-values.json"
  jq -n \
    --arg id "$DEPLOYMENT_LOCK_ID" \
    --arg owner "$DEPLOYMENT_LOCK_OWNER" \
    --argjson expires "$expires" '{
    LockID:{S:$id},
    OwnerToken:{S:$owner},
    ExpiresAt:{N:($expires | tostring)}
  }' > "$item"
  jq -n --argjson now "$now" '{
    ":now":{N:($now | tostring)}
  }' > "$values"
  aws_cli dynamodb put-item \
    --table-name teamagent-tflock \
    --item "file://$item" \
    --condition-expression \
      "attribute_not_exists(LockID) OR ExpiresAt < :now" \
    --expression-attribute-values "file://$values" \
    --output json >/dev/null ||
    die "共有deployment lockを取得できません"
}

release_deployment_lock() {
  [ -n "$DEPLOYMENT_LOCK_OWNER" ] || return 0
  local key values
  key="$TMP_ROOT/deployment-lock-key.json"
  values="$TMP_ROOT/deployment-lock-release-values.json"
  jq -n --arg id "$DEPLOYMENT_LOCK_ID" '{LockID:{S:$id}}' > "$key"
  jq -n --arg owner "$DEPLOYMENT_LOCK_OWNER" '{
    ":owner":{S:$owner}
  }' > "$values"
  aws_cli dynamodb delete-item \
    --table-name teamagent-tflock \
    --key "file://$key" \
    --condition-expression "OwnerToken = :owner" \
    --expression-attribute-values "file://$values" \
    --output json >/dev/null
  DEPLOYMENT_LOCK_OWNER=""
}

validate_log_versioning_stage_manifest() {
  jq -e --argjson now "$(date +%s)" '
    .schema_version == 1 and
    (.log_versioning_stage | keys | sort) == ([
      "allowed_write",
      "buckets",
      "cutover_mode",
      "enabled",
      "expires_at",
      "id",
      "minimum_settle_seconds",
      "mfa_delete",
      "producer_action",
      "producer_state_required",
      "required_status_after",
      "required_status_before",
      "timestamp_source"
    ] | sort) and
    .log_versioning_stage.id ==
      "2026-07-log-versioning-cutover-v4" and
    .log_versioning_stage.enabled == true and
    (.log_versioning_stage.expires_at | fromdateiso8601) > $now and
    .log_versioning_stage.buckets == [
      "teamagent-dev-cloudtrail-718959508629",
      "teamagent-dev-bedrock-logs-718959508629"
    ] and
    .log_versioning_stage.allowed_write ==
      "guard-disconnect-versioning-and-cutover-only" and
    .log_versioning_stage.required_status_before == ["Unversioned"] and
    .log_versioning_stage.required_status_after == "Enabled" and
    .log_versioning_stage.mfa_delete == "Disabled" and
    .log_versioning_stage.producer_action ==
      "guard-disconnect-enable-settle-double-observe-cutover" and
    .log_versioning_stage.producer_state_required == "disconnected" and
    .log_versioning_stage.timestamp_source ==
      "aws-http-response-date" and
    .log_versioning_stage.minimum_settle_seconds == 900 and
    .log_versioning_stage.cutover_mode ==
      "same-workflow-shared-lock-first-time-only"
  ' "$MIGRATION_FILE" >/dev/null ||
    die "log versioning stageはreview済みmanifestでenabledかつ期限内の場合だけ実行できます"
}

migration_to_file() {
  local migration_id="$1" output="$2" review_phase="${3:-final}"
  case "$review_phase" in
    candidate|final|preflight) ;;
    *) die "未知のmigration review phaseです: $review_phase" ;;
  esac
  jq -e -S -c --arg id "$migration_id" --arg review_phase "$review_phase" '
    .schema_version == 1 and
    .external_state_handoffs["2026-07-alarm-topic-consolidation-v1"] == {
      canonical_topic_arn:
        "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-openclaw-alarms",
      canonical_owner: "aws_sns_topic.alarms",
      legacy_topic_arn:
        "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-alarms",
      legacy_owner: "external-teamagent-state",
      import_legacy_into_this_state: false,
      ledger_table: "teamagent-dev-image-deployment-intents",
      ledger_record_prefix:
        "alarm-migration#2026-07-alarm-topic-consolidation-v1#",
      durable_checkpoint_required: true,
      idempotent_resume_required: true,
      ordered_phases: [
        "dual_publish",
        "publisher_checkpoint",
        "canonical_delivery_confirmed",
        "legacy_reference_zero",
        "legacy_retired"
      ],
      phase_contracts: {
        dual_publish:
          "every inventoried publisher targets canonical and legacy",
        publisher_checkpoint:
          "one durable postcondition checkpoint per exact publisher",
        canonical_delivery_confirmed:
          "fresh SNS challenge and managed-KMS recipient acknowledgement after every publisher is canonical-only",
        legacy_reference_zero:
          "all-page publisher reference count for legacy is zero while canonical remains complete",
        legacy_retired:
          "legacy topic is absent and canonical publisher set remains complete"
      },
      rollback_contracts: {
        dual_publish:
          "retain legacy until canonical delivery is rechecked",
        publisher_checkpoint:
          "restore the exact publisher from its previous durable checkpoint",
        canonical_delivery_confirmed:
          "retain both target sets and issue a fresh challenge",
        legacy_reference_zero:
          "restore every publisher from durable per-publisher checkpoints",
        legacy_retired:
          "never recreate automatically; require a new reviewed migration"
      },
      activation_requires: {
        confirmed_email_endpoint_sha256:
          "88c6452f9db04017250aa5728b4815bccb55b5ecc0b35b50a5234170dc08d1e6",
        subscription_inventory_count: 1,
        pending_subscription_count: 0,
        subscription_protocol: "email",
        destination_state_sha256:
          "c942dbb7b97da1f4d9debb1ba241ee89bf8c1d951d8d75bdea3056850838ddc9",
        chatbot_configuration_count: 0,
        legacy_topic_exists: false,
        legacy_action_reference_count: 0,
        final_phase: "legacy_retired",
        final_checkpoint_sha256_required: true,
        history_sha256_required: true
      }
    } and
    (.migrations[$id] | type == "object") and
    (.migrations[$id].expires_at | fromdateiso8601 > now) and
    (
      if $review_phase == "candidate" then
        .migrations[$id].enabled == false and
        .migrations[$id].reviewed_plan == null
      elif $review_phase == "final" then
        .migrations[$id].enabled == true and
        (.migrations[$id].reviewed_plan | type == "object")
      else
        (
          (
            .migrations[$id].enabled == false and
            .migrations[$id].reviewed_plan == null
          ) or
          (
            .migrations[$id].enabled == true and
            (.migrations[$id].reviewed_plan | type == "object")
          )
        )
      end
    ) and
    (.migrations[$id].reviewed_inputs | keys) ==
      ["image_deployment_intent_id"] and
    (.migrations[$id].reviewed_inputs.image_deployment_intent_id |
      test(
        "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
      )) and
    (
      if .migrations[$id].kind == "runtime" then
        .migrations[$id].requires_migration == null and
        .migrations[$id].requires_versioning_stage ==
          .log_versioning_stage.id and
        (.migrations[$id].from.images | keys | sort) == [
          "canary", "connect_web", "ingest", "mcp", "morning",
          "openclaw", "tiktok", "x_buzz"
        ] and
        ([
          .migrations[$id].from.images.mcp,
          .migrations[$id].from.images.connect_web,
          .migrations[$id].from.images.ingest,
          .migrations[$id].from.images.morning,
          .migrations[$id].from.images.canary,
          .migrations[$id].from.images.x_buzz
        ] | all(test(
          "^718959508629[.]dkr[.]ecr[.]ap-northeast-1[.]amazonaws[.]com/teamagent-mcp@sha256:[0-9a-f]{64}$"
        ))) and
        (.migrations[$id].from.images.openclaw |
          test("^718959508629[.]dkr[.]ecr[.]ap-northeast-1[.]amazonaws[.]com/teamagent-openclaw@sha256:[0-9a-f]{64}$")) and
        (.migrations[$id].from.images.tiktok |
          test("^718959508629[.]dkr[.]ecr[.]ap-northeast-1[.]amazonaws[.]com/teamagent-dev-tiktok-acquire@sha256:[0-9a-f]{64}$")) and
        (.migrations[$id].to.openclaw_image |
          test("^718959508629[.]dkr[.]ecr[.]ap-northeast-1[.]amazonaws[.]com/teamagent-openclaw@sha256:[0-9a-f]{64}$")) and
        (.migrations[$id].to.mcp_image |
          test("^718959508629[.]dkr[.]ecr[.]ap-northeast-1[.]amazonaws[.]com/teamagent-mcp@sha256:[0-9a-f]{64}$")) and
        (.migrations[$id].to.x_buzz_image |
          test("^718959508629[.]dkr[.]ecr[.]ap-northeast-1[.]amazonaws[.]com/teamagent-mcp@sha256:[0-9a-f]{64}$")) and
        (.migrations[$id].to.tiktok_image |
          test("^718959508629[.]dkr[.]ecr[.]ap-northeast-1[.]amazonaws[.]com/teamagent-media-worker@sha256:[0-9a-f]{64}$")) and
        (.migrations[$id].from.dispatcher_code_sha256 | keys | sort) ==
          ["tiktok", "x_buzz"] and
        (.migrations[$id].from.dispatcher_code_sha256 |
          to_entries | all(.value | test("^[A-Za-z0-9+/]{43}=$"))) and
        (.migrations[$id].to.dispatcher_code_sha256 | keys | sort) ==
          ["tiktok", "x_buzz"] and
        (.migrations[$id].to.dispatcher_code_sha256 |
          to_entries | all(.value | test("^[A-Za-z0-9+/]{43}=$"))) and
        (.migrations[$id].to.main_source_commit |
          test("^[0-9a-f]{40}$")) and
        .migrations[$id].to.main_signature == {
          minimum_source_commit:
            "0ff2ca8c7ca9b556cf590f531896055f962780fd",
          required_hmac_contract_commit:
            "2de3b15632bb2d671a4836d5cf3f252dd9b25727",
          kms_key_arn: .migrations[$id].to.main_signature.kms_key_arn,
          annotation_name: "org.opencontainers.image.revision",
          rekor_transparency_log_required: true
        } and
        (.migrations[$id].to.main_signature.kms_key_arn |
          test("^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")) and
        .migrations[$id].from.connect_app_html.bucket ==
          "teamagent-dev-raw-files" and
        .migrations[$id].from.connect_app_html.key ==
          "codebuild/connect-web-app.html" and
        (.migrations[$id].from.connect_app_html | keys | sort) == [
          "bucket", "build_inputs_sha256", "key", "sha256",
          "vault_manifest_sha256", "version_id"
        ] and
        (.migrations[$id].from.connect_app_html.version_id |
          test("^[A-Za-z0-9._-]{1,1024}$")) and
        (.migrations[$id].from.connect_app_html.sha256 |
          test("^[0-9a-f]{64}$")) and
        (.migrations[$id].from.connect_app_html.vault_manifest_sha256 |
          test("^[0-9a-f]{64}$")) and
        (.migrations[$id].from.connect_app_html.build_inputs_sha256 |
          test("^[0-9a-f]{64}$")) and
        .migrations[$id].to.connect_app_html ==
          .migrations[$id].from.connect_app_html and
        (.migrations[$id].required_preflight_profiles | sort) ==
          ["main", "openclaw", "tiktok", "x_buzz"] and
        (.migrations[$id].to.required_contract_labels |
          keys | sort) == ["main", "openclaw", "tiktok"] and
        .migrations[$id].to.required_contract_labels.main == {
          "io.teamagent.runtime.uid": "10001",
          "io.teamagent.runtime.gid": "10001",
          "io.teamagent.runtime.volume": "/tmp",
          "io.teamagent.runtime.contract": "fargate-readonly-v1"
        } and
        .migrations[$id].to.required_contract_labels.tiktok == {
          "io.teamagent.runtime.uid": "10001",
          "io.teamagent.runtime.gid": "10001",
          "io.teamagent.runtime.volume": "/tmp",
          "io.teamagent.runtime.contract": "fargate-readonly-v1"
        } and
        .migrations[$id].to.required_contract_labels.openclaw == {
          "io.teamagent.runtime.architecture": "linux/arm64",
          "io.teamagent.runtime.readonly-rootfs-required": "true"
        } and
        .migrations[$id].to.rule_states == {
          ingest: "DISABLED", morning: "DISABLED", canary: "DISABLED"
        } and
        .migrations[$id].from.active_task_counts == {
          ingest_active: 0
        } and
        .migrations[$id].from.api_gateway == {
          api_id: "esk97z9grh",
          name: "teamagent-connectweb-api",
          protocol_type: "HTTP",
          disable_execute_api_endpoint: false,
          default_stage: {
            stage_name: "$default",
            auto_deploy: true,
            access_log_enabled: false,
            detailed_metrics_enabled: false
          },
          custom_domain_mappings: [{
            domain_name: "connect.newstv.co.jp",
            api_id: "esk97z9grh",
            stage: "$default",
            api_mapping_key: ""
          }]
        } and
        .migrations[$id].from.service_contracts == {
          mcp: {
            availability_zone_rebalancing: "DISABLED",
            deployment_circuit_breaker: {enable: false, rollback: false}
          },
          connect_web: {
            availability_zone_rebalancing: "DISABLED",
            deployment_circuit_breaker: {enable: false, rollback: false}
          },
          openclaw: {
            deployment_circuit_breaker: {enable: false, rollback: false}
          }
        } and
        .migrations[$id].from.monitoring == {
          container_insights: "disabled"
        } and
        .migrations[$id].from.alarm_delivery.canonical_topic_arn ==
          "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-openclaw-alarms" and
        .migrations[$id].from.alarm_delivery.canonical_topic_exists == true and
        .migrations[$id].from.alarm_delivery.confirmed_email_endpoint_sha256 == [
          "88c6452f9db04017250aa5728b4815bccb55b5ecc0b35b50a5234170dc08d1e6"
        ] and
        .migrations[$id].from.alarm_delivery.subscription_inventory_count == 1 and
        .migrations[$id].from.alarm_delivery.pending_subscription_count == 0 and
        .migrations[$id].from.alarm_delivery.subscription_protocols == ["email"] and
        (.migrations[$id].from.alarm_delivery.subscription_inventory_sha256 |
          test("^[0-9a-f]{64}$")) and
        (.migrations[$id].from.alarm_delivery.confirmed_subscription_metadata_sha256 |
          test("^[0-9a-f]{64}$")) and
        .migrations[$id].from.alarm_delivery.destination_state_sha256 ==
          "c942dbb7b97da1f4d9debb1ba241ee89bf8c1d951d8d75bdea3056850838ddc9" and
        .migrations[$id].from.alarm_delivery.attached_chatbot_configuration_arns == [] and
        .migrations[$id].from.alarm_delivery.legacy_topic_arn ==
          "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-alarms" and
        .migrations[$id].from.alarm_delivery.legacy_topic_exists == true and
        (
          .migrations[$id].from.alarm_delivery.legacy_action_reference_count |
          type == "number" and . >= 0 and floor == .
        ) and
        .migrations[$id].to.api_gateway == {
          disable_execute_api_endpoint: true,
          default_stage: {
            access_log_enabled: true,
            access_log_destination_arn:
              "arn:aws:logs:ap-northeast-1:718959508629:log-group:/aws/apigateway/teamagent-dev-connect-web",
            detailed_metrics_enabled: false
          },
          custom_domain_mappings: [{
            domain_name: "connect.newstv.co.jp",
            api_id: "esk97z9grh",
            stage: "$default",
            api_mapping_key: ""
          }]
        } and
        .migrations[$id].to.service_contracts == {
          mcp: {
            availability_zone_rebalancing: "ENABLED",
            deployment_circuit_breaker: {enable: true, rollback: true}
          },
          connect_web: {
            availability_zone_rebalancing: "ENABLED",
            deployment_circuit_breaker: {enable: true, rollback: true}
          },
          openclaw: {
            availability_zone_rebalancing: "ENABLED",
            deployment_maximum_percent: 100,
            deployment_minimum_healthy_percent: 0,
            deployment_circuit_breaker: {enable: true, rollback: true}
          }
        } and
        .migrations[$id].to.monitoring == {
          container_insights: "enabled"
        } and
        .migrations[$id].to.alarm_delivery == {
          canonical_topic_arn:
            "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-openclaw-alarms",
          require_alarm_delivery: true,
          minimum_configured_destinations: 1,
          legacy_topic_arn:
            "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-alarms",
          legacy_owner_action:
            "retire-without-import-after-reference-count-zero"
        }
      elif .migrations[$id].kind == "activation" then
        .migrations[$id].requires_migration ==
          "2026-07-wolfi-runtime-v1" and
        (.migrations[$id].from.task_definition_arns.ingest |
          test("^arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-ingest:[0-9]+$")) and
        (.migrations[$id].from.task_definition_arns.canary |
          test("^arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-canary:[0-9]+$")) and
        ([.migrations[$id].from.images.ingest, .migrations[$id].from.images.canary] |
          all(test("^718959508629[.]dkr[.]ecr[.]ap-northeast-1[.]amazonaws[.]com/teamagent-mcp@sha256:[0-9a-f]{64}$"))) and
        .migrations[$id].from.connect_app_html.bucket ==
          "teamagent-dev-raw-files" and
        .migrations[$id].from.connect_app_html.key ==
          "codebuild/connect-web-app.html" and
        (.migrations[$id].from.connect_app_html | keys | sort) == [
          "bucket", "build_inputs_sha256", "key", "sha256",
          "vault_manifest_sha256", "version_id"
        ] and
        (.migrations[$id].from.connect_app_html.version_id |
          test("^[A-Za-z0-9._-]{1,1024}$")) and
        (.migrations[$id].from.connect_app_html.sha256 |
          test("^[0-9a-f]{64}$")) and
        (.migrations[$id].from.connect_app_html.vault_manifest_sha256 |
          test("^[0-9a-f]{64}$")) and
        (.migrations[$id].from.connect_app_html.build_inputs_sha256 |
          test("^[0-9a-f]{64}$")) and
        (.migrations[$id].required_preflight_profiles | sort) ==
          ["activation-canary", "activation-ingest-acl-quarantine"] and
        .migrations[$id].from.rule_states == {
          ingest: "DISABLED", morning: "DISABLED", canary: "DISABLED"
        } and
        .migrations[$id].to.rule_states == {
          ingest: "ENABLED", morning: "ENABLED", canary: "ENABLED"
        } and
        .migrations[$id].from.api_gateway.disable_execute_api_endpoint == true and
        .migrations[$id].from.api_gateway.default_stage.access_log_enabled == true and
        .migrations[$id].from.api_gateway.default_stage.detailed_metrics_enabled == false and
        .migrations[$id].from.api_gateway.custom_domain_mappings == [{
          domain_name: "connect.newstv.co.jp",
          api_id: "esk97z9grh",
          stage: "$default",
          api_mapping_key: ""
        }] and
        .migrations[$id].from.service_contracts == {
          mcp: {
            availability_zone_rebalancing: "ENABLED",
            deployment_circuit_breaker: {enable: true, rollback: true}
          },
          connect_web: {
            availability_zone_rebalancing: "ENABLED",
            deployment_circuit_breaker: {enable: true, rollback: true}
          },
          openclaw: {
            availability_zone_rebalancing: "ENABLED",
            deployment_maximum_percent: 100,
            deployment_minimum_healthy_percent: 0,
            deployment_circuit_breaker: {enable: true, rollback: true}
          }
        } and
        .migrations[$id].from.monitoring == {
          container_insights: "enabled"
        } and
        .migrations[$id].from.alarm_delivery == {
          canonical_topic_arn:
            "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-openclaw-alarms",
          confirmed_email_endpoint_sha256: [
            "88c6452f9db04017250aa5728b4815bccb55b5ecc0b35b50a5234170dc08d1e6"
          ],
          subscription_inventory_count: 1,
          pending_subscription_count: 0,
          subscription_protocols: ["email"],
          destination_state_sha256:
            "c942dbb7b97da1f4d9debb1ba241ee89bf8c1d951d8d75bdea3056850838ddc9",
          attached_chatbot_configuration_arns: [],
          legacy_topic_arn:
            "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-alarms",
          legacy_topic_exists: false,
          legacy_action_reference_count: 0
        }
      else false
      end
    ) and
    .migrations[$id]
  ' "$MIGRATION_FILE" > "$output" ||
    die "migrationが未登録・review phase不一致・期限切れ、またはdestination digestがexactではありません: $migration_id"
}

media_migration_binding_to_file() {
  local migration_id="$1" output="$2" migration="$TMP_ROOT/media-migration.json"
  migration_to_file "$migration_id" "$migration" final
  jq -e '
    .kind == "runtime" and
    (.reviewed_plan | type) == "object" and
    (.reviewed_inputs.image_deployment_intent_id |
      test(
        "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
      )) and
    (.to.tiktok_image |
      test(
        "^718959508629[.]dkr[.]ecr[.]ap-northeast-1[.]amazonaws[.]com/teamagent-media-worker@sha256:[0-9a-f]{64}$"
      ))
  ' "$migration" >/dev/null ||
    die "media cutoverはreview済みruntime migrationのexact generic imageだけが対象です"
  jq -n -S \
    --arg migration_id "$migration_id" \
    --arg desired_image "$(jq -er '.to.tiktok_image' "$migration")" \
    --arg image_deployment_intent_id "$(
      jq -er '.reviewed_inputs.image_deployment_intent_id' "$migration"
    )" \
    --arg migration_contract_sha256 "$(
      normalized_migration_manifest_sha256 "$migration_id"
    )" \
    --arg reviewed_plan_sha256 "$(
      jq -cS '.reviewed_plan' "$migration" | sha256_text
    )" '
    {
      migration_id:$migration_id,
      desired_image:$desired_image,
      image_deployment_intent_id:$image_deployment_intent_id,
      migration_contract_sha256:$migration_contract_sha256,
      reviewed_plan_sha256:$reviewed_plan_sha256
    }
  ' > "$output"
  jq -e '
    (.migration_contract_sha256 | test("^[0-9a-f]{64}$")) and
    (.reviewed_plan_sha256 | test("^[0-9a-f]{64}$"))
  ' "$output" >/dev/null ||
    die "media migration bindingを生成できません"
}

validate_migration_source() {
  local snapshot="$1" migration="$2"
  jq -e --slurpfile migration "$migration" '
    def api_source($api): {
      api_id: $api.api_id,
      name: $api.name,
      protocol_type: $api.protocol_type,
      disable_execute_api_endpoint: $api.disable_execute_api_endpoint,
      default_stage: {
        stage_name: $api.default_stage.stage_name,
        auto_deploy: $api.default_stage.auto_deploy,
        access_log_enabled: $api.default_stage.access_log_enabled,
        detailed_metrics_enabled:
          $api.default_stage.detailed_metrics_enabled
      },
      custom_domain_mappings: $api.custom_domain_mappings
    };
    def api_hardened($api): {
      disable_execute_api_endpoint: $api.disable_execute_api_endpoint,
      default_stage: {
        access_log_enabled: $api.default_stage.access_log_enabled,
        access_log_destination_arn:
          $api.default_stage.access_log_destination_arn,
        detailed_metrics_enabled:
          $api.default_stage.detailed_metrics_enabled
      },
      custom_domain_mappings: $api.custom_domain_mappings
    };
    def deployment_contract($service; $openclaw):
      {
        availability_zone_rebalancing:
          $service.critical.availability_zone_rebalancing,
        deployment_circuit_breaker:
          $service.critical.deployment_circuit_breaker
      } +
      if $openclaw then {
        deployment_maximum_percent:
          $service.critical.deployment_maximum_percent,
        deployment_minimum_healthy_percent:
          $service.critical.deployment_minimum_healthy_percent
      } else {} end;
    def delivery_source($delivery): {
      canonical_topic_arn: $delivery.canonical_topic_arn,
      canonical_topic_exists: $delivery.canonical_topic_exists,
      confirmed_email_endpoint_sha256:
        ($delivery.confirmed_email_endpoint_sha256 | sort),
      subscription_inventory_count:
        $delivery.subscription_inventory_count,
      pending_subscription_count:
        $delivery.pending_subscription_count,
      subscription_protocols:
        ($delivery.subscription_protocols | sort),
      subscription_inventory_sha256:
        $delivery.subscription_inventory_sha256,
      confirmed_subscription_metadata_sha256:
        $delivery.confirmed_subscription_metadata_sha256,
      destination_state_sha256:
        $delivery.destination_state_sha256,
      attached_chatbot_configuration_arns:
        ($delivery.attached_chatbot_configuration_arns | sort),
      legacy_topic_arn: $delivery.legacy_topic_arn,
      legacy_topic_exists: $delivery.legacy_topic_exists,
      legacy_action_reference_count:
        $delivery.legacy_action_reference_count
    };
    . as $live | $migration[0] as $m |
    if $m.kind == "runtime" then
      ($live.taskdefs | with_entries(.value = .value.arn)) ==
        $m.from.task_definition_arns and
      {
        openclaw: $live.taskdefs.openclaw.image,
        mcp: $live.taskdefs.mcp.image,
        connect_web: $live.taskdefs.connect_web.image,
        ingest: $live.taskdefs.ingest.image,
        morning: $live.taskdefs.morning.image,
        canary: $live.taskdefs.canary.image,
        x_buzz: $live.taskdefs.x_buzz.image,
        tiktok: $live.taskdefs.tiktok.image
      } == $m.from.images and
      {
        tiktok: $live.dispatchers.tiktok.code_sha256,
        x_buzz: $live.dispatchers.x_buzz.code_sha256
      } == $m.from.dispatcher_code_sha256 and
      {
        tiktok: $live.event_mappings.tiktok.critical.function_response_types,
        x_buzz: $live.event_mappings.x_buzz.critical.function_response_types
      } == $m.from.event_mapping_function_response_types and
      {
        ingest_active: ($live.active_tasks.ingest | length)
      } == $m.from.active_task_counts and
      $live.connect_app_html == $m.from.connect_app_html and
      $m.to.connect_app_html == $m.from.connect_app_html and
      {
        ingest: $live.rules.ingest.critical.state,
        morning: $live.rules.morning.critical.state,
        canary: $live.rules.canary.critical.state
      } == $m.to.rule_states and
      api_source($live.api_gateway) == $m.from.api_gateway and
      {
        mcp: (
          deployment_contract($live.services.mcp; false) |
          {
            availability_zone_rebalancing,
            deployment_circuit_breaker
          }
        ),
        connect_web: (
          deployment_contract($live.services.connect_web; false) |
          {
            availability_zone_rebalancing,
            deployment_circuit_breaker
          }
        ),
        openclaw: {
          deployment_circuit_breaker:
            $live.services.openclaw.critical.deployment_circuit_breaker
        }
      } == $m.from.service_contracts
      and $live.monitoring == $m.from.monitoring
      and delivery_source($live.alarm_delivery) ==
        $m.from.alarm_delivery
    elif $m.kind == "activation" then
      {
        ingest: $live.taskdefs.ingest.arn,
        canary: $live.taskdefs.canary.arn
      } == $m.from.task_definition_arns and
      {
        ingest: $live.taskdefs.ingest.image,
        canary: $live.taskdefs.canary.image
      } == $m.from.images and
      $live.connect_app_html == $m.from.connect_app_html and
      {
        ingest: $live.rules.ingest.critical.state,
        morning: $live.rules.morning.critical.state,
        canary: $live.rules.canary.critical.state
      } == $m.from.rule_states and
      # ACTIVATION-SHIM(ingest): 一時対応。Activation 完了後に canonical registry と
      # release_evidence を原子的に正名化して撤去する。docs/activation/ACTIVATION_STATE.md 参照。
      $live.rule_dispatchers.ingest.task_definition ==
        $live.taskdefs.ingest.arn and
      $live.targets.canary.task_definition == $live.taskdefs.canary.arn and
      api_hardened($live.api_gateway) == $m.from.api_gateway and
      {
        mcp: deployment_contract($live.services.mcp; false),
        connect_web:
          deployment_contract($live.services.connect_web; false),
        openclaw:
          deployment_contract($live.services.openclaw; true)
      } == $m.from.service_contracts
      and $live.monitoring == $m.from.monitoring
      and $live.alarm_delivery.canonical_topic_arn ==
        $m.from.alarm_delivery.canonical_topic_arn
      and $live.alarm_delivery.canonical_topic_exists == true
      and $live.alarm_delivery.confirmed_email_endpoint_sha256 ==
        $m.from.alarm_delivery.confirmed_email_endpoint_sha256
      and $live.alarm_delivery.subscription_inventory_count ==
        $m.from.alarm_delivery.subscription_inventory_count
      and $live.alarm_delivery.pending_subscription_count ==
        $m.from.alarm_delivery.pending_subscription_count
      and $live.alarm_delivery.subscription_protocols ==
        $m.from.alarm_delivery.subscription_protocols
      and $live.alarm_delivery.destination_state_sha256 ==
        $m.from.alarm_delivery.destination_state_sha256
      and $live.alarm_delivery.attached_chatbot_configuration_arns ==
        $m.from.alarm_delivery.attached_chatbot_configuration_arns
      and $live.alarm_delivery.legacy_topic_arn ==
        $m.from.alarm_delivery.legacy_topic_arn
      and $live.alarm_delivery.legacy_topic_exists ==
        $m.from.alarm_delivery.legacy_topic_exists
      and $live.alarm_delivery.legacy_action_reference_count ==
        $m.from.alarm_delivery.legacy_action_reference_count
    else false
    end
  ' "$snapshot" >/dev/null ||
    die "liveはmigrationのexact one-time source allowlistと一致しません（適用済み・drift・別revision）"
}

split_ecr_image() {
  local image="$1"
  ECR_REPOSITORY="${image#*.amazonaws.com/}"
  ECR_REPOSITORY="${ECR_REPOSITORY%@sha256:*}"
  ECR_DIGEST="${image##*@}"
  [[ "$ECR_REPOSITORY" =~ ^[A-Za-z0-9._/-]+$ ]] ||
    die "ECR repositoryが不正です"
  [[ "$ECR_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] ||
    die "imageはimmutable digest pinが必須です"
}

# 新coreはdigest pinだけでなく、固定KMS keyによるCosign署名、Rekor inclusion、
# exact source revisionの3点を同時に満たす必要がある。署名payload自体はreceiptへ
# 複製せず、その検証件数とhashだけを残す。
validate_signed_main_image() {
  local image="$1" source_commit="$2" minimum_source_commit="$3"
  local required_hmac_commit="$4" kms_key_arn="$5"
  local annotation_name="$6" output="$7"
  [[ "$source_commit" =~ ^[0-9a-f]{40}$ ]] ||
    die "main source commitは完全40桁SHAが必要です"
  [ "$minimum_source_commit" = "0ff2ca8c7ca9b556cf590f531896055f962780fd" ] ||
    die "main signed imageのminimum source commitが固定値と一致しません"
  [ "$required_hmac_commit" = "2de3b15632bb2d671a4836d5cf3f252dd9b25727" ] ||
    die "main signed imageのHMAC contract commitが固定値と一致しません"
  [[ "$kms_key_arn" =~ ^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] ||
    die "main image署名KMS key ARNはexact account/region/key IDが必要です"
  [ "$annotation_name" = "org.opencontainers.image.revision" ] ||
    die "main image署名annotation名が固定値と一致しません"
  git -C "$REPO_ROOT" cat-file -e "${source_commit}^{commit}" 2>/dev/null ||
    die "main image source commitがローカルGit objectにありません"
  git -C "$REPO_ROOT" merge-base --is-ancestor \
    "$minimum_source_commit" "$source_commit" ||
    die "main image source commitが監査済みorigin/dev 0ff2ca8c以降ではありません"
  git -C "$REPO_ROOT" merge-base --is-ancestor \
    "$required_hmac_commit" "$source_commit" ||
    die "main image source commitにHMAC separation 2de3b156が含まれません"
  git -C "$REPO_ROOT" merge-base --is-ancestor \
    "$source_commit" "$(git_commit)" ||
    die "main image source commitがrollout commitの祖先ではありません"

  split_ecr_image "$image"
  local claims="$TMP_ROOT/main-cosign-claims.json"
  unset COSIGN_REPOSITORY COSIGN_INSECURE_IGNORE_TLOG COSIGN_EXPERIMENTAL
  cosign verify \
    --key "awskms:///$kms_key_arn" \
    --insecure-ignore-tlog=false \
    -a "${annotation_name}=${source_commit}" \
    "$image" > "$claims" ||
    die "main imageのKMS/Cosign/Rekor署名検証に失敗しました"
  jq -e --arg digest "$ECR_DIGEST" \
    --arg annotation "$annotation_name" \
    --arg source_commit "$source_commit" '
    type == "array" and length > 0 and
    all(.[];
      (
        .critical.image["docker-manifest-digest"] //
        .Critical.Image["Docker-manifest-digest"] //
        ""
      ) == $digest and
      ((.optional // .Optional // {})[$annotation] // "") == $source_commit
    )
  ' "$claims" >/dev/null ||
    die "Cosign署名claimがexact image digest/source revisionと一致しません"

  jq -n -S \
    --arg image "$image" \
    --arg image_digest "$ECR_DIGEST" \
    --arg source_commit "$source_commit" \
    --arg minimum_source_commit "$minimum_source_commit" \
    --arg required_hmac_contract_commit "$required_hmac_commit" \
    --arg kms_key_arn "$kms_key_arn" \
    --arg annotation_name "$annotation_name" \
    --arg claims_sha256 "$(sha256_file "$claims")" \
    --argjson signature_count "$(jq -er 'length' "$claims")" '{
      image:$image,
      image_digest:$image_digest,
      source_commit:$source_commit,
      minimum_source_commit:$minimum_source_commit,
      required_hmac_contract_commit:$required_hmac_contract_commit,
      kms_key_arn:$kms_key_arn,
      annotation_name:$annotation_name,
      rekor_transparency_log_verified:true,
      signature_count:$signature_count,
      verified_claims_sha256:$claims_sha256
    }' > "$output"
}

# OCI/Docker configをECRから直接取得し、task起動前にimage metadataをfail-closed検証する。
validate_image_contract() {
  local image="$1" required_labels="$2" expected_user="$3"
  local require_tmp_volume="$4" profile="$5" output="$6"
  split_ecr_image "$image"
  ensure_tmp
  local response="$TMP_ROOT/ecr-manifest-$RANDOM.json"
  local manifest_file="$TMP_ROOT/image-manifest-$RANDOM.json"
  local config_digest config_url
  aws_cli ecr batch-get-image \
    --repository-name "$ECR_REPOSITORY" \
    --image-ids "imageDigest=$ECR_DIGEST" \
    --accepted-media-types \
      application/vnd.oci.image.manifest.v1+json \
      application/vnd.docker.distribution.manifest.v2+json \
    --output json > "$response"
  jq -e --arg digest "$ECR_DIGEST" '
    (.failures | length) == 0 and
    (.images | length) == 1 and
    .images[0].imageId.imageDigest == $digest and
    (.images[0].imageManifestMediaType ==
      "application/vnd.oci.image.manifest.v1+json" or
     .images[0].imageManifestMediaType ==
      "application/vnd.docker.distribution.manifest.v2+json")
  ' "$response" >/dev/null || die "single-platform OCI/Docker image manifestを取得できません: $image"
  # ECR returns the manifest as an encoded JSON string. Preserve its exact
  # bytes (no jq re-serialization/newline) and bind both manifest and config
  # downloads back to their immutable SHA-256 descriptors before trusting any
  # metadata from them.
  jq -jr '.images[0].imageManifest' "$response" > "$manifest_file"
  [ "sha256:$(sha256_file "$manifest_file")" = "$ECR_DIGEST" ] ||
    die "ECR image manifest bytesがrequested immutable digestと一致しません: $image"
  config_digest="$(jq -er '.config.digest | select(test("^sha256:[0-9a-f]{64}$"))' "$manifest_file")"
  config_url="$(aws_cli ecr get-download-url-for-layer \
    --repository-name "$ECR_REPOSITORY" \
    --layer-digest "$config_digest" \
    --query downloadUrl --output text)"
  [[ "$config_url" == https://* ]] || die "image config download URLが不正です"
  curl --fail --silent --show-error "$config_url" > "$output"
  [ "sha256:$(sha256_file "$output")" = "$config_digest" ] ||
    die "OCI image config bytesがmanifest descriptor digestと一致しません: $image"
  jq -e --argjson labels "$required_labels" \
    --arg expected_user "$expected_user" \
    --argjson require_tmp_volume "$require_tmp_volume" \
    --arg profile "$profile" '
    .architecture == "arm64" and
    .os == "linux" and
    (.config.User == $expected_user) and
    (($require_tmp_volume | not) or
      ((.config.Volumes["/tmp"] // null) | type == "object")) and
    (
      if $profile == "openclaw" then
        .config.Entrypoint ==
          ["/nodejs/bin/node", "/opt/teamagent/entrypoint.mjs"] and
        .config.Cmd ==
          ["/app/openclaw.mjs", "gateway", "--bind", "loopback", "--port", "18789"]
      else true
      end
    ) and
    ($labels | to_entries | all(. as $item |
      .config.Labels[$item.key] == $item.value))
  ' "$output" >/dev/null ||
    die "image contract不一致（arm64/linux, exact USER/VOLUME/labels）: $image"
}

capture_cloudtrail_lifecycle_contract() {
  local bucket="$1" output="$2"
  local raw="$TMP_ROOT/cloudtrail-lifecycle-raw-$RANDOM.json"
  local canonical="$TMP_ROOT/cloudtrail-lifecycle-canonical-$RANDOM.json"
  local error_file="$TMP_ROOT/cloudtrail-lifecycle-error-$RANDOM.txt"
  local configuration_present="true"

  if ! aws_cli s3api get-bucket-lifecycle-configuration \
    --bucket "$bucket" \
    --expected-bucket-owner "$EXPECTED_ACCOUNT_ID" \
    --output json > "$raw" 2> "$error_file"; then
    grep -q "NoSuchLifecycleConfiguration" "$error_file" ||
      die "CloudTrail bucket lifecycleを観測できません"
    printf '%s\n' '{"Rules":[]}' > "$raw"
    configuration_present="false"
  fi
  jq -e '
    (keys | sort) == ["Rules"] and
    (.Rules | type) == "array" and
    all(.Rules[];
      (has("Expiration") | not) and
      (has("NoncurrentVersionExpiration") | not)
    )
  ' "$raw" >/dev/null ||
    die "CloudTrail監査bucketにexpiration/noncurrent deletion lifecycleがあります"
  jq -S -c '{Rules:(.Rules | sort_by(.ID // ""))}' "$raw" > "$canonical" ||
    die "CloudTrail lifecycleを正規化できません"
  jq -n -S \
    --argjson configuration_present "$configuration_present" \
    --arg canonical_sha256 "$(sha256_file "$canonical")" \
    --argjson rule_count "$(jq '.Rules | length' "$canonical")" '{
      configuration_present:$configuration_present,
      rule_count:$rule_count,
      deletion_rule_count:0,
      canonical_sha256:$canonical_sha256
    }' > "$output"
}

# $1 に canonical JSON を書く。secret は値ではなく ECS valueFrom ARN だけを保持する。
snapshot_live() {
  local output="$1"
  ensure_tmp
  local dir="$TMP_ROOT/snapshot-$RANDOM-$RANDOM"
  mkdir -p "$dir"

  local cluster="${PROJECT}-${ENVIRONMENT}"
  local mcp_service="${PROJECT}-${ENVIRONMENT}-mcp"
  local connect_service="${PROJECT}-${ENVIRONMENT}-connect-web"
  local openclaw_service="${PROJECT}-${ENVIRONMENT}-openclaw"
  local ingest_rule="${PROJECT}-${ENVIRONMENT}-ingest-weekly"
  # ACTIVATION-SHIM(ingest): 一時対応。Activation 完了後に canonical registry と
  # release_evidence を原子的に正名化して撤去する。docs/activation/ACTIVATION_STATE.md 参照。
  local ingest_dispatch_function="${PROJECT}-${ENVIRONMENT}-ingest-dispatch"
  local morning_rule="${PROJECT}-${ENVIRONMENT}-morning-digest-weekday"
  local canary_rule="${PROJECT}-${ENVIRONMENT}-canary-hourly"
  local tiktok_function="${PROJECT}-${ENVIRONMENT}-tiktok-acquire-dispatch"
  local x_function="${PROJECT}-${ENVIRONMENT}-x-buzz-dispatch"
  local connect_http_api_id="esk97z9grh"
  local connect_http_domain="connect.newstv.co.jp"
  local connect_app_bucket="${PROJECT}-${ENVIRONMENT}-raw-files"
  local connect_app_key="codebuild/connect-web-app.html"
  local cloudtrail_bucket="${PROJECT}-${ENVIRONMENT}-cloudtrail-${EXPECTED_ACCOUNT_ID}"
  local bedrock_logs_bucket="${PROJECT}-${ENVIRONMENT}-bedrock-logs-${EXPECTED_ACCOUNT_ID}"
  local canonical_alarm_topic_arn="arn:aws:sns:${REGION}:${EXPECTED_ACCOUNT_ID}:${PROJECT}-${ENVIRONMENT}-openclaw-alarms"
  local legacy_alarm_topic_arn="arn:aws:sns:${REGION}:${EXPECTED_ACCOUNT_ID}:${PROJECT}-${ENVIRONMENT}-alarms"

  aws_cli sts get-caller-identity --output json > "$dir/identity.json"
  local account_id
  account_id="$(jq -er '.Account | select(type == "string")' "$dir/identity.json")" ||
    die "AWS accountを確認できません"
  [ "$account_id" = "$EXPECTED_ACCOUNT_ID" ] ||
    die "想定外のAWS accountです: $account_id"

  aws_cli s3api get-bucket-versioning --bucket "$cloudtrail_bucket" \
    --expected-bucket-owner "$EXPECTED_ACCOUNT_ID" \
    --output json > "$dir/cloudtrail-versioning.json"
  aws_cli s3api get-bucket-versioning --bucket "$bedrock_logs_bucket" \
    --expected-bucket-owner "$EXPECTED_ACCOUNT_ID" \
    --output json > "$dir/bedrock-versioning.json"
  capture_cloudtrail_lifecycle_contract \
    "$cloudtrail_bucket" "$dir/cloudtrail-lifecycle-contract.json"
  for versioning_file in \
    "$dir/cloudtrail-versioning.json" \
    "$dir/bedrock-versioning.json"; do
    # A bucket that never had a versioning configuration makes the CLI print
    # nothing at all -- not even {} -- which jq then rejects as malformed. set -e
    # would have aborted on a failed call before this point, so an empty file here
    # can only mean "call succeeded, no configuration", which is exactly the
    # Unversioned / MFA-Delete-Disabled case the checks below already accept.
    [ -s "$versioning_file" ] || printf '{}\n' > "$versioning_file"
    jq -e '
      ((.Status // "Unversioned") |
        . == "Unversioned" or . == "Enabled" or . == "Suspended") and
      ((.MFADelete // "Disabled") != "Enabled")
    ' "$versioning_file" >/dev/null ||
      die "S3 versioning/MFA Delete metadataが不正です"
  done

  aws_cli s3api head-object \
    --bucket "$connect_app_bucket" --key "$connect_app_key" \
    --output json > "$dir/connect-app-head.json"
  local connect_app_version connect_app_sha256
  connect_app_version="$(jq -er '
    .VersionId |
    select(type == "string" and test("^[A-Za-z0-9._-]{1,1024}$"))
  ' "$dir/connect-app-head.json")" ||
    die "connect /app S3 objectにlatest VersionIdがありません"
  jq -e '
    (.ContentLength | type) == "number" and .ContentLength > 0 and
    (.DeleteMarker // false) == false
  ' "$dir/connect-app-head.json" >/dev/null ||
    die "connect /app S3 object metadataが不正です"
  aws_cli s3api get-object \
    --bucket "$connect_app_bucket" --key "$connect_app_key" \
    --version-id "$connect_app_version" \
    "$dir/connect-app.html" > "$dir/connect-app-get.json"
  [ -s "$dir/connect-app.html" ] ||
    die "connect /app exact S3 versionが空です"
  jq -e --arg version "$connect_app_version" '
    .VersionId == $version
  ' "$dir/connect-app-get.json" >/dev/null ||
    die "connect /app取得versionがhead-objectから変化しました"
  connect_app_sha256="$(sha256_file "$dir/connect-app.html")"
  [[ "$connect_app_sha256" =~ ^[0-9a-f]{64}$ ]] ||
    die "connect /app SHA-256を計算できません"
  jq -Rse '
    [
      split("\n")[] |
      select(startswith("const DATA=") and endswith(";")) |
      ltrimstr("const DATA=") |
      rtrimstr(";") |
      fromjson
    ] |
    select(length == 1) |
    .[0] |
    {
      vault_manifest_sha256: .manifest_sha256,
      build_inputs_sha256: .build_inputs_sha256
    } |
    select(
      (.vault_manifest_sha256 | type) == "string" and
      (.vault_manifest_sha256 | test("^[0-9a-f]{64}$")) and
      (.build_inputs_sha256 | type) == "string" and
      (.build_inputs_sha256 | test("^[0-9a-f]{64}$"))
    )
  ' "$dir/connect-app.html" > "$dir/connect-app-provenance.json" ||
    die "connect /appのVault manifest/build inputs provenanceが不正です"
  jq -n -S -c \
    --arg bucket "$connect_app_bucket" \
    --arg key "$connect_app_key" \
    --arg version_id "$connect_app_version" \
    --arg sha256 "$connect_app_sha256" \
    --slurpfile provenance "$dir/connect-app-provenance.json" \
    '{
      bucket:$bucket,
      key:$key,
      version_id:$version_id,
      sha256:$sha256
    } + $provenance[0]' \
    > "$dir/connect-app-contract.json"

  aws_cli ecs describe-services --cluster "$cluster" \
    --services "$mcp_service" "$connect_service" "$openclaw_service" --include TAGS \
    --output json > "$dir/services.json"
  aws_cli ecs describe-clusters --clusters "$cluster" --include SETTINGS TAGS \
    --output json > "$dir/cluster.json"

  local service_failures
  service_failures="$(jq -r '.failures | length' "$dir/services.json")"
  [ "$service_failures" = "0" ] || die "ECS service の取得に失敗しました"
  jq -e --arg cluster "$cluster" '
    (.failures | length) == 0 and
    (.clusters | length) == 1 and
    .clusters[0].clusterName == $cluster and
    ([
      .clusters[0].settings[] |
      select(.name == "containerInsights" and
             (.value == "enabled" or .value == "disabled"))
    ] | length == 1)
  ' "$dir/cluster.json" >/dev/null ||
    die "ECS cluster Container Insights契約を取得できません"

  aws_cli apigatewayv2 get-api --api-id "$connect_http_api_id" \
    --output json > "$dir/connect-http-api.json"
  aws_cli apigatewayv2 get-stage --api-id "$connect_http_api_id" \
    --stage-name '$default' --output json > "$dir/connect-http-stage.json"
  aws_cli apigatewayv2 get-api-mappings --domain-name "$connect_http_domain" \
    --output json > "$dir/connect-http-mappings.json"
  jq -e --arg api_id "$connect_http_api_id" --arg domain "$connect_http_domain" '
    .ApiId == $api_id and
    .Name == "teamagent-connectweb-api" and
    .ProtocolType == "HTTP" and
    (.DisableExecuteApiEndpoint | type) == "boolean" and
    .ApiEndpoint ==
      ("https://" + $api_id + ".execute-api.ap-northeast-1.amazonaws.com")
  ' "$dir/connect-http-api.json" >/dev/null ||
    die "connect HTTP APIのidentity/protocol/endpoint契約が不正です"
  jq -e '
    .StageName == "$default" and
    .AutoDeploy == true and
    ((.DefaultRouteSettings.DetailedMetricsEnabled // false) == false) and
    (
      if (.AccessLogSettings // null) == null then true
      else
        .AccessLogSettings.DestinationArn ==
          "arn:aws:logs:ap-northeast-1:718959508629:log-group:/aws/apigateway/teamagent-dev-connect-web" and
        (.AccessLogSettings.Format | fromjson) == {
          requestId: "$context.requestId",
          routeKey: "$context.routeKey",
          status: "$context.status",
          responseLength: "$context.responseLength",
          integrationStatus: "$context.integration.status",
          integrationLatency: "$context.integrationLatency",
          responseType: "$context.error.responseType"
        }
      end
    )
  ' "$dir/connect-http-stage.json" >/dev/null ||
    die "connect HTTP API $default stage契約が不正です"
  jq -e --arg api_id "$connect_http_api_id" '
    (.NextToken // "") == "" and
    ([
      .Items[] | select(
        .ApiId == $api_id and .Stage == "$default" and
        (.ApiMappingKey // "") == ""
      )
    ] | length == 1)
  ' "$dir/connect-http-mappings.json" >/dev/null ||
    die "connect custom-domain root mappingが一意ではありません"

  aws_cli sns list-topics --output json > "$dir/sns-topics.json"
  jq -e --arg canonical "$canonical_alarm_topic_arn" \
    --arg legacy "$legacy_alarm_topic_arn" '
    (.NextToken // "") == "" and
    ([.Topics[] | select(.TopicArn == $canonical)] | length) == 1 and
    ([.Topics[] | select(.TopicArn == $legacy)] | length) <= 1
  ' "$dir/sns-topics.json" >/dev/null ||
    die "canonical/legacy alarm SNS topic inventoryが一意ではありません"
  aws_cli sns list-subscriptions-by-topic \
    --topic-arn "$canonical_alarm_topic_arn" --output json \
    > "$dir/canonical-alarm-subscriptions.json"
  jq -e --arg canonical "$canonical_alarm_topic_arn" '
    (.NextToken // "") == "" and
    (.Subscriptions | type) == "array" and
    all(.Subscriptions[];
      .TopicArn == $canonical and
      (.Protocol | type) == "string" and
      (.Endpoint | type) == "string" and
      (.SubscriptionArn | type) == "string")
  ' "$dir/canonical-alarm-subscriptions.json" >/dev/null ||
    die "canonical alarm SNS subscription metadataが不正です"

  : > "$dir/subscription-inventory.jsonl"
  : > "$dir/confirmed-subscription-attributes.jsonl"
  local subscription_count subscription_index subscription_arn
  local subscription_protocol subscription_endpoint raw_endpoint
  local subscription_state endpoint_sha subscription_arn_sha
  subscription_count="$(jq -er '.Subscriptions | length' \
    "$dir/canonical-alarm-subscriptions.json")"
  subscription_index=0
  while [ "$subscription_index" -lt "$subscription_count" ]; do
    subscription_arn="$(jq -er --argjson index "$subscription_index" \
      '.Subscriptions[$index].SubscriptionArn' \
      "$dir/canonical-alarm-subscriptions.json")"
    subscription_protocol="$(jq -er --argjson index "$subscription_index" \
      '.Subscriptions[$index].Protocol' \
      "$dir/canonical-alarm-subscriptions.json")"
    subscription_endpoint="$(jq -er --argjson index "$subscription_index" \
      '.Subscriptions[$index].Endpoint' \
      "$dir/canonical-alarm-subscriptions.json")"
    # SNS endpoint bytes are an authorization input. No trim, case folding, or
    # Unicode normalization is permitted.
    raw_endpoint="$subscription_endpoint"
    [ "$subscription_protocol" != "email" ] ||
      [ "$raw_endpoint" = "$EXPECTED_ALARM_EMAIL" ] ||
      die "alarm delivery SNS email endpointはapproved raw byte列とexact一致が必要です"
    endpoint_sha="$(printf '%s' "$raw_endpoint" | sha256_text)"
    subscription_arn_sha="$(
      printf '%s' "$subscription_arn" | sha256_text
    )"
    case "$subscription_arn" in
      PendingConfirmation) subscription_state="pending" ;;
      Deleted) subscription_state="deleted" ;;
      *) subscription_state="confirmed" ;;
    esac
    jq -n -S -c \
      --arg topic_arn "$canonical_alarm_topic_arn" \
      --arg protocol "$subscription_protocol" \
      --arg endpoint_sha256 "$endpoint_sha" \
      --arg subscription_arn_sha256 "$subscription_arn_sha" \
      --arg state "$subscription_state" '{
        topic_arn:$topic_arn,
        protocol:$protocol,
        endpoint_sha256:$endpoint_sha256,
        subscription_arn_sha256:$subscription_arn_sha256,
        state:$state
      }' >> "$dir/subscription-inventory.jsonl"
    if [ "$subscription_state" != "confirmed" ]; then
      subscription_index=$((subscription_index + 1))
      continue
    fi
    aws_cli sns get-subscription-attributes \
      --subscription-arn "$subscription_arn" --output json \
      > "$dir/subscription-attributes-${subscription_index}.json"
    jq -e \
      --arg arn "$subscription_arn" \
      --arg topic "$canonical_alarm_topic_arn" \
      --arg protocol "$subscription_protocol" \
      --arg endpoint "$subscription_endpoint" '
      .Attributes as $attributes |
      ($attributes | type) == "object" and
      $attributes.SubscriptionArn == $arn and
      $attributes.TopicArn == $topic and
      $attributes.Protocol == $protocol and
      $attributes.Endpoint == $endpoint and
      ($attributes.PendingConfirmation // "false") == "false" and
      (
        if ($protocol == "email" or $protocol == "email-json") then
          $attributes.ConfirmationWasAuthenticated == "true"
        else true
        end
      ) and
      (($attributes.RawMessageDelivery // "false") == "false") and
      ($attributes | has("FilterPolicy") | not) and
      ($attributes | has("FilterPolicyScope") | not)
    ' "$dir/subscription-attributes-${subscription_index}.json" >/dev/null ||
      die "alarm delivery SNS subscription attributesがconfirmed/no-filter exact契約を満たしません"
    jq -n -S -c \
      --arg subscription_arn_sha256 "$subscription_arn_sha" \
      --arg protocol "$subscription_protocol" \
      --arg endpoint_sha256 "$endpoint_sha" \
      '{
        subscription_arn_sha256:$subscription_arn_sha256,
        protocol:$protocol,
        endpoint_sha256:$endpoint_sha256,
        confirmed:true,
        filter_policy_present:false,
        raw_message_delivery:false
      }' >> "$dir/confirmed-subscription-attributes.jsonl"
    subscription_index=$((subscription_index + 1))
  done
  jq -s -S -c 'sort_by(
    .state, .protocol, .endpoint_sha256, .subscription_arn_sha256
  )' "$dir/subscription-inventory.jsonl" \
    > "$dir/subscription-inventory.json"
  jq -e \
    --arg topic "$canonical_alarm_topic_arn" \
    --arg email_sha "$EXPECTED_ALARM_EMAIL_SHA256" '
    length == 1 and
    .[0].topic_arn == $topic and
    .[0].protocol == "email" and
    .[0].endpoint_sha256 == $email_sha and
    .[0].state == "confirmed" and
    (. [0].subscription_arn_sha256 | test("^[0-9a-f]{64}$"))
  ' "$dir/subscription-inventory.json" >/dev/null ||
    die "alarm delivery canonical SNS topicはapproved email 1件だけのconfirmed subscriptionである必要があります"
  jq -s -S -c 'sort_by(.subscription_arn_sha256)' \
    "$dir/confirmed-subscription-attributes.jsonl" \
    > "$dir/confirmed-subscription-attributes.json"
  jq -e --arg email_sha "$EXPECTED_ALARM_EMAIL_SHA256" '
    length == 1 and
    .[0].protocol == "email" and
    .[0].endpoint_sha256 == $email_sha and
    .[0].confirmed == true and
    .[0].filter_policy_present == false and
    .[0].raw_message_delivery == false
  ' "$dir/confirmed-subscription-attributes.json" >/dev/null ||
    die "approved SNS email subscription attributesがexact契約を満たしません"
  jq -n -c --arg email_sha "$EXPECTED_ALARM_EMAIL_SHA256" \
    '[$email_sha]' > "$dir/confirmed-email-hashes.json"

  aws_chatbot_cli chatbot describe-slack-channel-configurations --output json \
    > "$dir/chatbot-slack.json"
  aws_chatbot_cli chatbot list-microsoft-teams-channel-configurations --output json \
    > "$dir/chatbot-teams.json"
  jq -e '
    (.NextToken // "") == "" and
    (.SlackChannelConfigurations // [] | type) == "array" and
    all(.SlackChannelConfigurations[];
      (.ChatConfigurationArn | type) == "string" and
      (.SnsTopicArns // [] | type) == "array")
  ' "$dir/chatbot-slack.json" >/dev/null ||
    die "Slack chat integration metadataが不正です"
  jq -e '
    (.NextToken // "") == "" and
    (.TeamChannelConfigurations // [] | type) == "array" and
    all(.TeamChannelConfigurations[];
      (.ChatConfigurationArn | type) == "string" and
      (.SnsTopicArns // [] | type) == "array")
  ' "$dir/chatbot-teams.json" >/dev/null ||
    die "Teams chat integration metadataが不正です"
  jq -e --arg topic "$canonical_alarm_topic_arn" '
    all(
      [
        (.SlackChannelConfigurations // [])[],
        (.TeamChannelConfigurations // [])[]
      ][];
      ((.SnsTopicArns // []) | index($topic)) == null
    )
  ' "$dir/chatbot-slack.json" "$dir/chatbot-teams.json" >/dev/null ||
    die "alarm delivery canonical SNS topicへのChatbot接続は禁止されています"
  jq -n -S -c \
    --arg topic "$canonical_alarm_topic_arn" \
    --arg endpoint "$EXPECTED_ALARM_EMAIL" '{
      chatbot_configuration_arns:[],
      subscription:{
        endpoint:$endpoint,
        filter_policy_present:false,
        protocol:"email",
        raw_message_delivery:false,
        state:"confirmed"
      },
      topic_arn:$topic
    }' > "$dir/alarm-destination-state.json"
  [ "$(sha256_file "$dir/alarm-destination-state.json")" = \
    "$EXPECTED_ALARM_DESTINATION_STATE_SHA256" ] ||
    die "approved alarm destination stateのpinned hashが不一致です"

  aws_cli cloudwatch describe-alarms --output json > "$dir/cloudwatch-alarms.json"
  jq -e '
    (.MetricAlarms // [] | type) == "array" and
    (.CompositeAlarms // [] | type) == "array"
  ' "$dir/cloudwatch-alarms.json" >/dev/null ||
    die "CloudWatch alarm action inventoryが不正です"

  # Topic retirement must include every publisher, not just CloudWatch.
  # Inventory all account Budgets and Cost Anomaly subscribers from their
  # global billing endpoint. Only metadata (type/address) is retained.
  aws_cost_cli budgets describe-budgets \
    --account-id "$EXPECTED_ACCOUNT_ID" --output json \
    > "$dir/budgets.json"
  jq -e '
    (.Budgets | type) == "array" and
    ((.NextToken // "") == "") and
    all(.Budgets[];
      (.BudgetName | type) == "string" and
      (.BudgetName | length) > 0)
  ' "$dir/budgets.json" >/dev/null ||
    die "AWS Budgets inventoryが不正またはpagination未完了です"

  : > "$dir/budget-subscribers.jsonl"
  local budget_index notification_index budget_count notification_count
  local budget_name notification_json
  budget_count="$(jq -er '.Budgets | length' "$dir/budgets.json")"
  budget_index=0
  while [ "$budget_index" -lt "$budget_count" ]; do
    budget_name="$(jq -er --argjson index "$budget_index" '
      .Budgets[$index].BudgetName
    ' "$dir/budgets.json")"
    aws_cost_cli budgets describe-notifications-for-budget \
      --account-id "$EXPECTED_ACCOUNT_ID" \
      --budget-name "$budget_name" --output json \
      > "$dir/budget-notifications-${budget_index}.json"
    jq -e '
      (.Notifications | type) == "array" and
      ((.NextToken // "") == "") and
      all(.Notifications[];
        (.NotificationType | type) == "string" and
        (.ComparisonOperator | type) == "string" and
        (.Threshold | type) == "number")
    ' "$dir/budget-notifications-${budget_index}.json" >/dev/null ||
      die "AWS Budget notification inventoryが不正またはpagination未完了です"
    notification_count="$(jq -er \
      '.Notifications | length' \
      "$dir/budget-notifications-${budget_index}.json")"
    notification_index=0
    while [ "$notification_index" -lt "$notification_count" ]; do
      notification_json="$(jq -c \
        --argjson index "$notification_index" '
        .Notifications[$index] |
        {
          NotificationType,
          ComparisonOperator,
          Threshold
        } +
        (if .ThresholdType == null then {}
         else {ThresholdType}
         end)
      ' "$dir/budget-notifications-${budget_index}.json")"
      aws_cost_cli budgets describe-subscribers-for-notification \
        --account-id "$EXPECTED_ACCOUNT_ID" \
        --budget-name "$budget_name" \
        --notification "$notification_json" --output json \
        > "$dir/budget-subscribers-${budget_index}-${notification_index}.json"
      jq -e '
        (.Subscribers | type) == "array" and
        ((.NextToken // "") == "") and
        all(.Subscribers[];
          (.SubscriptionType | type) == "string" and
          (.Address | type) == "string" and
          (.Address | length) > 0)
      ' "$dir/budget-subscribers-${budget_index}-${notification_index}.json" \
        >/dev/null ||
        die "AWS Budget subscriber inventoryが不正またはpagination未完了です"
      jq -c --arg budget "$budget_name" \
        '.Subscribers[] | {
          budget_name: $budget,
          subscription_type: .SubscriptionType,
          address: .Address
        }' \
        "$dir/budget-subscribers-${budget_index}-${notification_index}.json" \
        >> "$dir/budget-subscribers.jsonl"
      notification_index=$((notification_index + 1))
    done
    budget_index=$((budget_index + 1))
  done
  jq -s -S -c '.' "$dir/budget-subscribers.jsonl" \
    > "$dir/budget-subscribers.json"

  aws_cost_cli ce get-anomaly-subscriptions --output json \
    > "$dir/cost-anomaly-subscriptions.json"
  jq -e '
    (.AnomalySubscriptions | type) == "array" and
    ((.NextPageToken // "") == "") and
    all(.AnomalySubscriptions[];
      (.SubscriptionArn | type) == "string" and
      (.Subscribers | type) == "array" and
      all(.Subscribers[];
        (.Type | type) == "string" and
        (.Address | type) == "string" and
        (.Address | length) > 0))
  ' "$dir/cost-anomaly-subscriptions.json" >/dev/null ||
    die "Cost Anomaly subscriber inventoryが不正またはpagination未完了です"

  local mcp_arn connect_arn openclaw_arn
  mcp_arn="$(jq -er --arg name "$mcp_service" '
    .services[] | select(.serviceName == $name) |
    ([.deployments[] | select(.status == "PRIMARY")]) as $primary |
    if .status == "ACTIVE" and .desiredCount > 0 and
       .runningCount == .desiredCount and .pendingCount == 0 and
       ($primary | length) == 1 and
       $primary[0].rolloutState == "COMPLETED" and
       $primary[0].taskDefinition == .taskDefinition
    then .taskDefinition else error("mcp service is not a single completed PRIMARY") end
  ' "$dir/services.json")" || die "$mcp_service が安定稼働中ではありません"
  connect_arn="$(jq -er --arg name "$connect_service" '
    .services[] | select(.serviceName == $name) |
    ([.deployments[] | select(.status == "PRIMARY")]) as $primary |
    if .status == "ACTIVE" and .desiredCount > 0 and
       .runningCount == .desiredCount and .pendingCount == 0 and
       ($primary | length) == 1 and
       $primary[0].rolloutState == "COMPLETED" and
       $primary[0].taskDefinition == .taskDefinition
    then .taskDefinition else error("connect service is not a single completed PRIMARY") end
  ' "$dir/services.json")" || die "$connect_service が安定稼働中ではありません"
  openclaw_arn="$(jq -er --arg name "$openclaw_service" '
    .services[] | select(.serviceName == $name) |
    ([.deployments[] | select(.status == "PRIMARY")]) as $primary |
    if .status == "ACTIVE" and .desiredCount == 1 and
       .runningCount == 1 and .pendingCount == 0 and
       ($primary | length) == 1 and
       $primary[0].rolloutState == "COMPLETED" and
       $primary[0].taskDefinition == .taskDefinition
    then .taskDefinition else error("openclaw service is not a single completed PRIMARY") end
  ' "$dir/services.json")" || die "$openclaw_service がsingle-writer安定稼働中ではありません"

  local ingest_arn morning_arn canary_arn
  local ingest_state morning_state canary_state

  # ACTIVATION-SHIM(ingest): 一時対応。Activation 完了後に canonical registry と
  # release_evidence を原子的に正名化して撤去する。docs/activation/ACTIVATION_STATE.md 参照。
  local expected_ingest_dispatch_arn
  expected_ingest_dispatch_arn="arn:aws:lambda:${REGION}:${EXPECTED_ACCOUNT_ID}:function:${ingest_dispatch_function}"
  aws_cli events describe-rule --name "$ingest_rule" \
    --output json > "$dir/ingest-rule.json"
  aws_cli events list-targets-by-rule --rule "$ingest_rule" \
    --output json > "$dir/ingest-targets.json"
  jq -e --arg function_arn "$expected_ingest_dispatch_arn" '
    (.Targets | length) == 1 and
    .Targets[0].Arn == $function_arn and
    .Targets[0].EcsParameters == null
  ' "$dir/ingest-targets.json" >/dev/null ||
    die "$ingest_rule の dispatch Lambda target が一意ではありません"
  ingest_state="$(jq -er '
    .State | select(. == "ENABLED" or . == "DISABLED")
  ' "$dir/ingest-rule.json")" || die "$ingest_rule の state が不正です"

  local rule
  for rule in "$morning_rule" "$canary_rule"; do
    local key
    case "$rule" in
      "$morning_rule") key="morning" ;;
      "$canary_rule") key="canary" ;;
    esac
    aws_cli events describe-rule --name "$rule" --output json > "$dir/${key}-rule.json"
    aws_cli events list-targets-by-rule --rule "$rule" --output json > "$dir/${key}-targets.json"
    local target_arn rule_state
    target_arn="$(jq -er '
      if (.Targets | length) == 1 and .Targets[0].EcsParameters.TaskDefinitionArn != null
      then .Targets[0].EcsParameters.TaskDefinitionArn
      else error("expected exactly one ECS target") end
    ' "$dir/${key}-targets.json")" || die "$rule の ECS target が一意ではありません"
    rule_state="$(jq -er '.State | select(. == "ENABLED" or . == "DISABLED")' "$dir/${key}-rule.json")" \
      || die "$rule の state が不正です"
    case "$key" in
      morning) morning_arn="$target_arn"; morning_state="$rule_state" ;;
      canary) canary_arn="$target_arn"; canary_state="$rule_state" ;;
    esac
  done

  # ACTIVATION-SHIM(ingest): 一時対応。Activation 完了後に canonical registry と
  # release_evidence を原子的に正名化して撤去する。docs/activation/ACTIVATION_STATE.md 参照。
  aws_cli lambda get-function-configuration \
    --function-name "$ingest_dispatch_function" \
    --output json > "$dir/ingest-lambda.json"
  jq -e --arg name "$ingest_dispatch_function" '
    .FunctionName == $name and .State == "Active" and
    .LastUpdateStatus == "Successful" and
    (.Environment.Variables | type == "object") and
    (.Environment.Variables.TASKDEF_ARN | type == "string")
  ' "$dir/ingest-lambda.json" >/dev/null ||
    die "$ingest_dispatch_function が安定稼働中ではありません"
  local ingest_function_arn
  ingest_function_arn="$(jq -er '
    .FunctionArn | select(type == "string")
  ' "$dir/ingest-lambda.json")"
  aws_cli lambda list-tags --resource "$ingest_function_arn" \
    --output json > "$dir/ingest-lambda-tags.json"
  ingest_arn="$(jq -er '
    .Environment.Variables.TASKDEF_ARN
  ' "$dir/ingest-lambda.json")"
  [[ "$ingest_arn" =~ ^arn:aws:ecs:${REGION}:${EXPECTED_ACCOUNT_ID}:task-definition/${PROJECT}-${ENVIRONMENT}-ingest:[0-9]+$ ]] ||
    die "$ingest_dispatch_function のTASKDEF_ARNが期待familyのrevision pinではありません"

  aws_cli ecs list-tasks \
    --cluster "$cluster" \
    --family "${PROJECT}-${ENVIRONMENT}-ingest" \
    --desired-status RUNNING \
    --output json > "$dir/ingest-desired-running-tasks.json"
  jq -e '
    (.nextToken // "") == "" and
    (.taskArns | type) == "array" and
    (.taskArns | length) == (.taskArns | unique | length) and
    all(.taskArns[];
      test("^arn:aws:ecs:ap-northeast-1:718959508629:task/"))
  ' "$dir/ingest-desired-running-tasks.json" >/dev/null ||
    die "ingest desired-RUNNING task inventoryを一意に取得できません"
  if jq -e '.taskArns | length > 0' \
    "$dir/ingest-desired-running-tasks.json" >/dev/null; then
    local -a ingest_task_arns=()
    while IFS= read -r task_arn; do
      ingest_task_arns+=("$task_arn")
    done < <(jq -r '.taskArns[]' "$dir/ingest-desired-running-tasks.json")
    aws_cli ecs describe-tasks \
      --cluster "$cluster" \
      --tasks "${ingest_task_arns[@]}" \
      --output json > "$dir/ingest-active-task-details.json"
  else
    jq -n '{failures:[],tasks:[]}' > "$dir/ingest-active-task-details.json"
  fi
  jq -e '
    (.failures | length) == 0 and
    (.tasks | type) == "array" and
    ([.tasks[].taskArn] | length) == ([.tasks[].taskArn] | unique | length) and
    all(.tasks[];
      (.taskArn |
       test("^arn:aws:ecs:ap-northeast-1:718959508629:task/")) and
      (.desiredStatus == "RUNNING" or .desiredStatus == "STOPPED") and
      (.lastStatus | type) == "string")
  ' "$dir/ingest-active-task-details.json" >/dev/null ||
    die "ingest active task detailsを一意に取得できません"

  local dispatch_key dispatch_function expected_family
  for dispatch_key in tiktok x; do
    case "$dispatch_key" in
      tiktok)
        dispatch_function="$tiktok_function"
        expected_family="${PROJECT}-${ENVIRONMENT}-tiktok-acquire"
        ;;
      x)
        dispatch_function="$x_function"
        expected_family="${PROJECT}-${ENVIRONMENT}-x-buzz-worker"
        ;;
    esac

    aws_cli lambda get-function-configuration --function-name "$dispatch_function" \
      --output json > "$dir/${dispatch_key}-lambda.json"
    jq -e --arg name "$dispatch_function" '
      .FunctionName == $name and .State == "Active" and
      .LastUpdateStatus == "Successful" and
      (.Environment.Variables | type == "object") and
      (.Environment.Variables.TASKDEF_ARN | type == "string")
    ' "$dir/${dispatch_key}-lambda.json" >/dev/null ||
      die "$dispatch_function が安定稼働中ではありません"

    local function_arn
    function_arn="$(jq -er '.FunctionArn' "$dir/${dispatch_key}-lambda.json")"
    aws_cli lambda list-tags --resource "$function_arn" \
      --output json > "$dir/${dispatch_key}-lambda-tags.json"
    aws_cli lambda get-function-concurrency --function-name "$dispatch_function" \
      --output json > "$dir/${dispatch_key}-lambda-concurrency.json"
    if [ ! -s "$dir/${dispatch_key}-lambda-concurrency.json" ]; then
      jq -n '{}' > "$dir/${dispatch_key}-lambda-concurrency.json"
    fi

    aws_cli lambda list-event-source-mappings --function-name "$dispatch_function" \
      --output json > "$dir/${dispatch_key}-mappings.json"
    jq -e --arg function_arn "$function_arn" '
      ((.NextMarker // "") == "") and
      (.EventSourceMappings | length) == 1 and
      (.EventSourceMappings[0].State == "Enabled" or
       .EventSourceMappings[0].State == "Disabled") and
      .EventSourceMappings[0].FunctionArn == $function_arn and
      (.EventSourceMappings[0].EventSourceArn | startswith("arn:aws:sqs:"))
    ' "$dir/${dispatch_key}-mappings.json" >/dev/null ||
      die "$dispatch_function のevent source mappingが一意・Enabledではありません"
    local mapping_arn task_arn
    mapping_arn="$(jq -er '.EventSourceMappings[0].EventSourceMappingArn' "$dir/${dispatch_key}-mappings.json")"
    aws_cli lambda list-tags --resource "$mapping_arn" \
      --output json > "$dir/${dispatch_key}-mapping-tags.json"
    task_arn="$(jq -er '.Environment.Variables.TASKDEF_ARN' "$dir/${dispatch_key}-lambda.json")"
    [[ "$task_arn" =~ ^arn:aws:ecs:${REGION}:${EXPECTED_ACCOUNT_ID}:task-definition/${expected_family}:[0-9]+$ ]] ||
      die "$dispatch_function のTASKDEF_ARNが期待familyのrevision pinではありません"
    printf '%s\n' "$task_arn" > "$dir/${dispatch_key}-task-arn.txt"
  done

  aws_cli ecs describe-task-definition --task-definition "$mcp_arn" --include TAGS --output json > "$dir/mcp.json"
  aws_cli ecs describe-task-definition --task-definition "$connect_arn" --include TAGS --output json > "$dir/connect.json"
  aws_cli ecs describe-task-definition --task-definition "$openclaw_arn" --include TAGS --output json > "$dir/openclaw.json"
  aws_cli ecs describe-task-definition --task-definition "$ingest_arn" --include TAGS --output json > "$dir/ingest.json"
  aws_cli ecs describe-task-definition --task-definition "$morning_arn" --include TAGS --output json > "$dir/morning.json"
  aws_cli ecs describe-task-definition --task-definition "$canary_arn" --include TAGS --output json > "$dir/canary.json"
  aws_cli ecs describe-task-definition \
    --task-definition "$(<"$dir/tiktok-task-arn.txt")" --include TAGS --output json > "$dir/tiktok.json"
  aws_cli ecs describe-task-definition \
    --task-definition "$(<"$dir/x-task-arn.txt")" --include TAGS --output json > "$dir/x.json"

  jq -L "$GUARD_JQ_DIR" -n -S -c \
    --arg region "$REGION" \
    --arg project "$PROJECT" \
    --arg environment "$ENVIRONMENT" \
    --arg cluster "$cluster" \
    --slurpfile identity "$dir/identity.json" \
    --slurpfile ecs_cluster "$dir/cluster.json" \
    --slurpfile services "$dir/services.json" \
    --slurpfile mcp "$dir/mcp.json" \
    --slurpfile connect "$dir/connect.json" \
    --slurpfile openclaw "$dir/openclaw.json" \
    --slurpfile ingest "$dir/ingest.json" \
    --slurpfile morning "$dir/morning.json" \
    --slurpfile canary "$dir/canary.json" \
    --slurpfile tiktok "$dir/tiktok.json" \
    --slurpfile x "$dir/x.json" \
    --slurpfile ingest_lambda "$dir/ingest-lambda.json" \
    --slurpfile ingest_lambda_tags "$dir/ingest-lambda-tags.json" \
    --slurpfile tiktok_lambda "$dir/tiktok-lambda.json" \
    --slurpfile tiktok_lambda_concurrency "$dir/tiktok-lambda-concurrency.json" \
    --slurpfile tiktok_lambda_tags "$dir/tiktok-lambda-tags.json" \
    --slurpfile tiktok_mappings "$dir/tiktok-mappings.json" \
    --slurpfile tiktok_mapping_tags "$dir/tiktok-mapping-tags.json" \
    --slurpfile x_lambda "$dir/x-lambda.json" \
    --slurpfile x_lambda_concurrency "$dir/x-lambda-concurrency.json" \
    --slurpfile x_lambda_tags "$dir/x-lambda-tags.json" \
    --slurpfile x_mappings "$dir/x-mappings.json" \
    --slurpfile x_mapping_tags "$dir/x-mapping-tags.json" \
    --slurpfile ingest_rule "$dir/ingest-rule.json" \
    --slurpfile morning_rule "$dir/morning-rule.json" \
    --slurpfile canary_rule "$dir/canary-rule.json" \
    --slurpfile ingest_target "$dir/ingest-targets.json" \
    --slurpfile morning_target "$dir/morning-targets.json" \
    --slurpfile canary_target "$dir/canary-targets.json" \
    --slurpfile ingest_active_tasks "$dir/ingest-active-task-details.json" \
    --slurpfile connect_http_api "$dir/connect-http-api.json" \
    --slurpfile connect_http_stage "$dir/connect-http-stage.json" \
    --slurpfile connect_http_mappings "$dir/connect-http-mappings.json" \
    --slurpfile connect_app_html "$dir/connect-app-contract.json" \
    --slurpfile cloudtrail_versioning "$dir/cloudtrail-versioning.json" \
    --slurpfile cloudtrail_lifecycle "$dir/cloudtrail-lifecycle-contract.json" \
    --slurpfile bedrock_versioning "$dir/bedrock-versioning.json" \
    --slurpfile sns_topics "$dir/sns-topics.json" \
    --slurpfile confirmed_email_hashes "$dir/confirmed-email-hashes.json" \
    --slurpfile subscription_inventory "$dir/subscription-inventory.json" \
    --arg subscription_inventory_sha256 \
      "$(sha256_file "$dir/subscription-inventory.json")" \
    --arg confirmed_subscription_metadata_sha256 \
      "$(sha256_file "$dir/confirmed-subscription-attributes.json")" \
    --arg destination_state_sha256 \
      "$(sha256_file "$dir/alarm-destination-state.json")" \
    --slurpfile chatbot_slack "$dir/chatbot-slack.json" \
    --slurpfile chatbot_teams "$dir/chatbot-teams.json" \
    --slurpfile cloudwatch_alarms "$dir/cloudwatch-alarms.json" \
    --slurpfile budget_subscribers "$dir/budget-subscribers.json" \
    --slurpfile cost_anomaly_subscriptions "$dir/cost-anomaly-subscriptions.json" \
    --arg canonical_alarm_topic_arn "$canonical_alarm_topic_arn" \
    --arg legacy_alarm_topic_arn "$legacy_alarm_topic_arn" \
    --arg connect_http_domain "$connect_http_domain" '
      include "terraform_runtime_guard";
      def task($doc; $expected_name):
        ($doc[0].taskDefinition + {tags: ($doc[0].tags // [])}) as $td |
        ([$td.containerDefinitions[] | select(.name == $expected_name)]) as $containers |
        if ($containers | length) != 1 then error("expected container name is not unique")
        else $containers[0] as $container |
        {
          arn: $td.taskDefinitionArn,
          expected_container_name: $expected_name,
          image: $container.image,
          env_count: (($container.environment // []) | length),
          env: (($container.environment // []) | map({key: .name, value: .value}) | from_entries),
          secret_count: (($container.secrets // []) | length),
          secrets: (($container.secrets // []) | map({key: .name, value: .valueFrom}) | from_entries),
          critical: ($td | guard_task_from_aws)
        }
        end;
      def service($name):
        $services[0].services[] | select(.serviceName == $name) as $service | {
          task_definition: $service.taskDefinition,
          critical: ($service | guard_service_from_aws)
        };
      def rule($doc): {critical: ($doc[0] | guard_rule_from_aws)};
      def target($doc):
        $doc[0].Targets[0] as $target | {
          task_definition: $target.EcsParameters.TaskDefinitionArn,
          critical: ($target | guard_target_from_aws)
        };
      def dispatcher($doc; $concurrency; $tags):
        $doc[0] as $lambda | {
          task_definition: $lambda.Environment.Variables.TASKDEF_ARN,
          static_environment: ($lambda.Environment.Variables | del(.TASKDEF_ARN)),
          code_sha256: $lambda.CodeSha256,
          critical: ($lambda | guard_lambda_from_aws($concurrency[0]; $tags[0]))
        };
      # ACTIVATION-SHIM(ingest): 一時対応。Activation 完了後に canonical registry と
      # release_evidence を原子的に正名化して撤去する。docs/activation/ACTIVATION_STATE.md 参照。
      def rule_dispatcher($doc; $tags):
        $doc[0] as $lambda | {
          task_definition: $lambda.Environment.Variables.TASKDEF_ARN,
          static_environment: ($lambda.Environment.Variables | del(.TASKDEF_ARN)),
          code_sha256: $lambda.CodeSha256,
          critical: {
            function_name: $lambda.FunctionName,
            function_arn: $lambda.FunctionArn,
            state: $lambda.State,
            last_update_status: $lambda.LastUpdateStatus,
            environment: $lambda.Environment.Variables,
            tags: ($tags[0].Tags // {})
          }
        };
      def event_mapping($doc; $tags):
        $doc[0].EventSourceMappings[0] as $mapping | {
          critical: ($mapping | guard_mapping_from_aws($tags[0]))
        };
      {
        account_id: $identity[0].Account,
        region: $region,
        project: $project,
        environment: $environment,
        cluster: $cluster,
        monitoring: {
          container_insights: (
            $ecs_cluster[0].clusters[0].settings[] |
            select(.name == "containerInsights") |
            .value
          )
        },
        log_buckets: {
          cloudtrail: {
            versioning_status:
              ($cloudtrail_versioning[0].Status // "Unversioned"),
            mfa_delete:
              ($cloudtrail_versioning[0].MFADelete // "Disabled"),
            lifecycle:$cloudtrail_lifecycle[0]
          },
          bedrock: {
            versioning_status:
              ($bedrock_versioning[0].Status // "Unversioned"),
            mfa_delete:
              ($bedrock_versioning[0].MFADelete // "Disabled")
          }
        },
        alarm_delivery: {
          canonical_topic_arn: $canonical_alarm_topic_arn,
          canonical_topic_exists: (
            [$sns_topics[0].Topics[] |
              select(.TopicArn == $canonical_alarm_topic_arn)] |
            length == 1
          ),
          confirmed_email_endpoint_sha256:
            $confirmed_email_hashes[0],
          subscription_inventory_count:
            ($subscription_inventory[0] | length),
          pending_subscription_count: ([
            $subscription_inventory[0][] |
            select(.state == "pending")
          ] | length),
          subscription_protocols:
            ($subscription_inventory[0] | map(.protocol) | sort | unique),
          subscription_inventory_sha256:
            $subscription_inventory_sha256,
          confirmed_subscription_metadata_sha256:
            $confirmed_subscription_metadata_sha256,
          destination_state_sha256:$destination_state_sha256,
          attached_chatbot_configuration_arns: ([
            ($chatbot_slack[0].SlackChannelConfigurations // [])[],
            ($chatbot_teams[0].TeamChannelConfigurations // [])[]
          ] | map(
            select(
              (.SnsTopicArns // []) |
              index($canonical_alarm_topic_arn)
            ) |
            .ChatConfigurationArn
          ) | sort | unique),
          legacy_topic_arn: $legacy_alarm_topic_arn,
          legacy_topic_exists: (
            [$sns_topics[0].Topics[] |
              select(.TopicArn == $legacy_alarm_topic_arn)] |
            length == 1
          ),
          legacy_action_reference_count: (
            ([
              ($cloudwatch_alarms[0].MetricAlarms // [])[],
              ($cloudwatch_alarms[0].CompositeAlarms // [])[]
            ] | map(
              (
                (.AlarmActions // []) +
                (.OKActions // []) +
                (.InsufficientDataActions // [])
              ) | index($legacy_alarm_topic_arn) != null
            ) | map(select(.)) | length) +
            ([
              $budget_subscribers[0][] |
              select(
                .subscription_type == "SNS" and
                .address == $legacy_alarm_topic_arn
              )
            ] | length) +
            ([
              $cost_anomaly_subscriptions[0].AnomalySubscriptions[] |
              .Subscribers[] |
              select(
                .Type == "SNS" and
                .Address == $legacy_alarm_topic_arn
              )
            ] | length)
          )
        },
        alarm_delivery_observation: {
          attached_chatbot_configurations: ([
            ($chatbot_slack[0].SlackChannelConfigurations // [])[],
            ($chatbot_teams[0].TeamChannelConfigurations // [])[]
          ] | map(
            select(
              (.SnsTopicArns // []) |
              index($canonical_alarm_topic_arn)
            ) |
            {
              arn:.ChatConfigurationArn,
              state:(.State // "UNKNOWN")
            }
          ) | sort_by(.arn))
        },
        api_gateway: {
          api_id: $connect_http_api[0].ApiId,
          name: $connect_http_api[0].Name,
          protocol_type: $connect_http_api[0].ProtocolType,
          disable_execute_api_endpoint:
            $connect_http_api[0].DisableExecuteApiEndpoint,
          default_stage: {
            stage_name: $connect_http_stage[0].StageName,
            auto_deploy: $connect_http_stage[0].AutoDeploy,
            access_log_enabled:
              (($connect_http_stage[0].AccessLogSettings // null) != null),
            access_log_destination_arn:
              ($connect_http_stage[0].AccessLogSettings.DestinationArn // ""),
            access_log_format:
              ($connect_http_stage[0].AccessLogSettings.Format // ""),
            detailed_metrics_enabled:
              ($connect_http_stage[0].DefaultRouteSettings.DetailedMetricsEnabled // false)
          },
          custom_domain_mappings: [
            $connect_http_mappings[0].Items[] |
            select(.ApiId == $connect_http_api[0].ApiId) |
            {
              domain_name: $connect_http_domain,
              api_id: .ApiId,
              stage: .Stage,
              api_mapping_key: (.ApiMappingKey // "")
            }
          ] | sort_by(
            .domain_name, .api_mapping_key, .api_id, .stage
          )
        },
        connect_app_html: $connect_app_html[0],
        taskdefs: {
          openclaw: task($openclaw; "openclaw"),
          mcp: task($mcp; "teamagent-mcp"),
          connect_web: task($connect; "connect-web"),
          ingest: task($ingest; "ingest"),
          morning: task($morning; "morning-digest"),
          canary: task($canary; "canary"),
          tiktok: task($tiktok; "acquire"),
          x_buzz: task($x; "worker")
        },
        services: {
          openclaw: service($project + "-" + $environment + "-openclaw"),
          mcp: service($project + "-" + $environment + "-mcp"),
          connect_web: service($project + "-" + $environment + "-connect-web")
        },
        rules: {
          ingest: rule($ingest_rule),
          morning: rule($morning_rule),
          canary: rule($canary_rule)
        },
        targets: {
          ingest: target($ingest_target),
          morning: target($morning_target),
          canary: target($canary_target)
        },
        active_tasks: {
          ingest: ([
            $ingest_active_tasks[0].tasks[] |
            select(.desiredStatus == "RUNNING" and .lastStatus != "STOPPED") |
            .taskArn
          ] | sort)
        },
        # ACTIVATION-SHIM(ingest): 一時対応。Activation 完了後に canonical registry と
        # release_evidence を原子的に正名化して撤去する。docs/activation/ACTIVATION_STATE.md 参照。
        rule_dispatchers: {
          ingest: rule_dispatcher($ingest_lambda; $ingest_lambda_tags)
        },
        dispatchers: {
          tiktok: dispatcher($tiktok_lambda; $tiktok_lambda_concurrency; $tiktok_lambda_tags),
          x_buzz: dispatcher($x_lambda; $x_lambda_concurrency; $x_lambda_tags)
        },
        event_mappings: {
          tiktok: event_mapping($tiktok_mappings; $tiktok_mapping_tags),
          x_buzz: event_mapping($x_mappings; $x_mapping_tags)
        }
      }
    ' > "$output"

  # Purpose-separated HMAC state is derived only from deployed ECS metadata.
  # Secret values are never fetched. Before the separation migration, report
  # signing used MAIL_ACTION_HMAC_SECRET; preserve that as the effective
  # deployed report primary so the exact legacy version can become previous.
  jq -S -c '
    def primary_base_arn($purpose):
      . as $value |
      (
        if $purpose == "mail" then
          "(database-url|hmac/mail-action)"
        else
          "(database-url|hmac/mail-action|hmac/report-link)"
        end
      ) as $path |
      (
        "^arn:aws:secretsmanager:ap-northeast-1:718959508629:"
        + "secret:teamagent/dev/" + $path + "-[A-Za-z0-9]{6}$"
      ) as $base_pattern |
      (
        "^arn:aws:secretsmanager:ap-northeast-1:718959508629:"
        + "secret:teamagent/dev/" + $path
        + "-[A-Za-z0-9]{6}:::[A-Za-z0-9_-]{32,64}$"
      ) as $pinned_pattern |
      if (($value | type) != "string") then
        error("deployed HMAC primary selector is not a string")
      elif ($value | test($base_pattern)) then
        $value
      elif ($value | test($pinned_pattern)) then
        $value | split(":::")[0]
      else
        error("deployed HMAC primary selector is outside the exact contract")
      end;
    def parsed_t0($value):
      if $value == null then null
      elif (($value | type) == "string" and
            ($value | test("^(0|[1-9][0-9]{0,9})$")))
      then ($value | tonumber)
      else error("invalid deployed HMAC T0 metadata")
      end;
    def metadata($task; $purpose):
      if $purpose == "mail" then {
        primary_secret_arn: ($task.secrets.MAIL_ACTION_HMAC_SECRET // ""),
        previous_secret_arn: ($task.secrets.MAIL_ACTION_HMAC_PREVIOUS_SECRET // ""),
        rotation_started_at:
          parsed_t0($task.env.MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT)
      }
      else {
        primary_secret_arn:
          ($task.secrets.REPORT_LINK_HMAC_SECRET //
           $task.secrets.MAIL_ACTION_HMAC_SECRET // ""),
        previous_secret_arn: ($task.secrets.REPORT_LINK_HMAC_PREVIOUS_SECRET // ""),
        rotation_started_at:
          parsed_t0($task.env.REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT)
      }
      end |
      . + {previous_present: (.previous_secret_arn != "")};
    def uniform($items; $purpose):
      ($items | map(metadata(.; $purpose))) as $metadata |
      if (($metadata | unique | length) != 1) then
        error("deployed HMAC consumers are inconsistent")
      else $metadata[0]
      end |
      if (.previous_present != (.rotation_started_at != null)) then
        error("deployed HMAC previous/T0 pair mismatch")
      else .
      end;
    def prefix_absent($task; $prefix):
      all(($task.env | keys)[]; startswith($prefix) | not) and
      all(($task.secrets | keys)[]; startswith($prefix) | not);
    (uniform([.taskdefs.mcp, .taskdefs.morning]; "mail")) as $mail |
    (uniform([.taskdefs.mcp, .taskdefs.connect_web]; "report")) as $report |
    if (
      (
        prefix_absent(.taskdefs.connect_web; "MAIL_ACTION_") or
        metadata(.taskdefs.connect_web; "mail") == $mail
      ) and
      prefix_absent(.taskdefs.morning; "REPORT_LINK_")
    ) then
      . + {
        hmac: {
          mail: (
            $mail |
            .primary_secret_arn |= primary_base_arn("mail")
          ),
          report: (
            $report |
            .primary_secret_arn |= primary_base_arn("report")
          )
        }
      }
    else
      error("deployed HMAC consumer ownership is inconsistent")
    end
  ' "$output" > "$dir/snapshot-with-hmac.json" ||
    die "実デプロイHMAC metadataがpurpose consumer間で不整合です"
  mv "$dir/snapshot-with-hmac.json" "$output"

  local mcp_image
  mcp_image="$(jq -er '.taskdefs.mcp.image' "$output")"
  local expected_repo digest
  expected_repo="${account_id}.dkr.ecr.${REGION}.amazonaws.com/teamagent-mcp"
  [[ "$mcp_image" == "$expected_repo@sha256:"* ]] ||
    die "live mcp imageは同一account/regionのteamagent-mcp digestである必要があります: $mcp_image"
  digest="${mcp_image#*@}"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] ||
    die "live mcp image が完全digest pinではありません: $mcp_image"
  jq -e '[.taskdefs[] | .expected_container_name as $name |
    [.critical.containers[] | select(.name == $name)] | length] | all(. == 1)' "$output" >/dev/null \
    || die "runtime task definitionの期待container名が一意ではありません"
  jq -e '[.taskdefs[] | (.env_count == (.env | length)) and (.secret_count == (.secrets | length))] | all' "$output" >/dev/null \
    || die "live task definition に重複env/secret名があります"
  jq -e --arg prefix "$expected_repo@sha256:" '
    [
      .taskdefs.mcp.image,
      .taskdefs.connect_web.image,
      .taskdefs.ingest.image,
      .taskdefs.morning.image,
      .taskdefs.canary.image
    ] | all(.[];
      startswith($prefix) and
      (split("@") | length) == 2 and
      (split("@")[1] | test("^sha256:[0-9a-f]{64}$"))
    )
  ' "$output" >/dev/null ||
    die "主要5 task definitionは同一account/regionのexact teamagent-mcp digest参照が必要です"

  local x_image tiktok_image
  x_image="$(jq -er '.taskdefs.x_buzz.image' "$output")"
  tiktok_image="$(jq -er '.taskdefs.tiktok.image' "$output")"
  [[ "$x_image" == "$expected_repo@sha256:"* ]] ||
    die "x-buzz live imageは同一account/regionのteamagent-mcp digestである必要があります"
  [[ "${x_image#*@}" =~ ^sha256:[0-9a-f]{64}$ ]] ||
    die "x-buzz live imageが完全digest pinではありません"
  local tiktok_legacy_repo="${account_id}.dkr.ecr.${REGION}.amazonaws.com/${PROJECT}-${ENVIRONMENT}-tiktok-acquire"
  local media_worker_repo="${account_id}.dkr.ecr.${REGION}.amazonaws.com/teamagent-media-worker"
  [[ "$tiktok_image" == "$tiktok_legacy_repo@sha256:"* ||
     "$tiktok_image" == "$media_worker_repo@sha256:"* ]] ||
    die "media live imageは承認済みlegacyまたはteamagent-media-worker digestである必要があります"
  [[ "${tiktok_image#*@}" =~ ^sha256:[0-9a-f]{64}$ ]] ||
    die "TikTok live imageが完全digest pinではありません"
  local openclaw_image="${account_id}.dkr.ecr.${REGION}.amazonaws.com/teamagent-openclaw"
  [[ "$(jq -er '.taskdefs.openclaw.image' "$output")" == "$openclaw_image@sha256:"* ]] ||
    die "OpenClaw live imageは同一account/regionの専用ECR digestである必要があります"
  # ACTIVATION-SHIM(ingest): 一時対応。Activation 完了後に canonical registry と
  # release_evidence を原子的に正名化して撤去する。docs/activation/ACTIVATION_STATE.md 参照。
  jq -L "$GUARD_JQ_DIR" -e '
    include "terraform_runtime_guard";
    .taskdefs.connect_web.env.CONNECT_APP_HTML_S3_URI ==
      "s3://teamagent-dev-raw-files/codebuild/connect-web-app.html" and
    .rule_dispatchers.ingest.task_definition == .taskdefs.ingest.arn and
    .targets.ingest.critical.arn ==
      .rule_dispatchers.ingest.critical.function_arn and
    .rule_dispatchers.ingest.critical.state == "Active" and
    .targets.ingest.critical.ecs_target ==
      ({} | guard_norm_aws_ecs_target) and
    .dispatchers.tiktok.task_definition == .taskdefs.tiktok.arn and
    .dispatchers.x_buzz.task_definition == .taskdefs.x_buzz.arn and
    (.event_mappings.tiktok.critical.enabled | type) == "boolean" and
    (.event_mappings.x_buzz.critical.enabled | type) == "boolean" and
    .event_mappings.tiktok.critical.function_arn == .dispatchers.tiktok.critical.function_arn and
    .event_mappings.x_buzz.critical.function_arn == .dispatchers.x_buzz.critical.function_arn
  ' "$output" >/dev/null || die "worker dispatcher/taskdef/event mappingのlive契約が不整合です"
}

# A legacy-envelope task and a generic-envelope task intentionally retain the
# same physical queue/table/family.  The in-place image cutover therefore
# requires the durable 900-second AWS-time attestation.  The evidence helper
# re-reads the exact live producer, queue, mapping, task and legacy image state
# and rejects any drift from the READY ledger.
validate_media_envelope_cutover_gate() {
  local snapshot="$1" desired_image="$2" receipt="${3:-}"
  local image_deployment_intent_id="${4:-}"
  local migration_contract_sha256="${5:-}"
  local reviewed_plan_sha256="${6:-}"
  local expected_status="${7:-READY}"
  local apply_attempt_id="${8:-}"
  local plan_sha256="${9:-}"
  local live_image legacy_prefix generic_prefix verification
  live_image="$(jq -er '.taskdefs.tiktok.image' "$snapshot")"
  legacy_prefix="${EXPECTED_ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${PROJECT}-${ENVIRONMENT}-tiktok-acquire@sha256:"
  generic_prefix="${EXPECTED_ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/teamagent-media-worker@sha256:"
  if [[ "$live_image" != "$legacy_prefix"* ||
        "$desired_image" != "$generic_prefix"* ]]; then
    [ -z "$receipt" ] ||
      die "legacy→generic以外のplanへmedia cutover receiptを混在できません"
    return 0
  fi

  [ -n "$receipt" ] &&
    [[ "$image_deployment_intent_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] &&
    [[ "$migration_contract_sha256" =~ ^[0-9a-f]{64}$ ]] &&
    [[ "$reviewed_plan_sha256" =~ ^[0-9a-f]{64}$ ]] ||
    die "legacy→generic media切替にはexact signed receipt/intent/review bindingが必須です"
  case "$expected_status" in
    READY)
      [ -z "$apply_attempt_id" ] && [ -z "$plan_sha256" ] ||
        die "READY media証跡へapply bindingを指定できません"
      ;;
    CONSUMED)
      [[ "$apply_attempt_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] &&
        [[ "$plan_sha256" =~ ^[0-9a-f]{64}$ ]] ||
        die "CONSUMED media証跡にはexact apply attempt/plan bindingが必須です"
      ;;
    *) die "media evidence statusはREADYまたはCONSUMEDだけが許可されます" ;;
  esac

  verification="$TMP_ROOT/media-cutover-verification-${RANDOM}.json"
  local verify_args=(
    verify-media-cutover
    --receipt "$receipt"
    --desired-image "$desired_image"
    --image-deployment-intent-id "$image_deployment_intent_id"
    --migration-contract-sha256 "$migration_contract_sha256"
    --reviewed-plan-sha256 "$reviewed_plan_sha256"
    --expected-status "$expected_status"
    --output "$verification"
  )
  if [ "$expected_status" = "CONSUMED" ]; then
    verify_args+=(
      --apply-attempt-id "$apply_attempt_id"
      --plan-sha256 "$plan_sha256"
    )
  fi
  run_evidence_helper "${verify_args[@]}"
  jq -e \
    --arg desired "$desired_image" \
    --arg legacy "$live_image" \
    --arg intent "$image_deployment_intent_id" \
    --arg migration_sha "$migration_contract_sha256" \
    --arg reviewed_sha "$reviewed_plan_sha256" \
    --arg status "$expected_status" '
    .kind == "teamagent-media-envelope-cutover-verification" and
    .schema_version == 2 and
    .account_id == "718959508629" and
    .region == "ap-northeast-1" and
    .desired_image == $desired and
    .record_id == ("media-cutover#" + $intent) and
    .status == $status and
    .image_deployment_intent_id == $intent and
    .migration_contract_sha256 == $migration_sha and
    .reviewed_plan_sha256 == $reviewed_sha and
    (.claims_sha256 | test("^[0-9a-f]{64}$")) and
    (.signature_sha256 | test("^[0-9a-f]{64}$")) and
    (.kms_key_arn |
      test(
        "^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-fA-F-]{36}$"
      )) and
    (.ledger_item_sha256 | test("^[0-9a-f]{64}$")) and
    (.verification_sha256 | test("^[0-9a-f]{64}$")) and
    .current_observation.state.legacy_runtime.image == $legacy and
    .current_observation.state.event_source_mapping.state == "Disabled" and
    .current_observation.state.tasks == {pending:[],running:[]} and
    (.current_observation.state_sha256 | test("^[0-9a-f]{64}$"))
  ' "$verification" >/dev/null ||
    die "legacy→generic media切替のdurable 900秒証跡が不正です"
}

select_terraform_media_image_inputs() {
  local mode="$1" live_image="$2" desired_image="$3"
  local legacy_prefix
  legacy_prefix="${EXPECTED_ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/teamagent-dev-tiktok-acquire@sha256:"

  TF_MEDIA_WORKER_IMAGE="$desired_image"
  TF_TIKTOK_ACQUIRE_IMAGE=""
  # validate_media_envelope_cutover_gate uses this same canonical live image to
  # identify the legacy side of the cutover.  While sync proves live=desired,
  # keep both caller-controlled image variables empty and let Terraform recover
  # only this exact digest from the ephemeral runtime_guard_live object.
  if [ "$mode" = "sync" ] &&
     [ "$live_image" = "$desired_image" ] &&
     [[ "$live_image" == "$legacy_prefix"* ]] &&
     [[ "${live_image#"$legacy_prefix"}" =~ ^[0-9a-f]{64}$ ]]; then
    TF_MEDIA_WORKER_IMAGE=""
  fi
}

derive_live_hmac_terraform_inputs() {
  local snapshot="$1" output="$2"
  jq -e -S -c '
    def prefix_absent($task; $prefix):
      all(($task.env | keys)[]; startswith($prefix) | not) and
      all(($task.secrets | keys)[]; startswith($prefix) | not);
    def exact_generation($value; $purpose):
      if (($value | type) != "string") then
        error("HMAC secret valueFrom is not a string")
      else
        (
          $value |
          capture(
            "^arn:aws:secretsmanager:ap-northeast-1:718959508629:"
            + "secret:teamagent/dev/hmac/" + $purpose
            + "-[A-Za-z0-9]{6}:::(?<version>[A-Za-z0-9_-]{32,64})$"
          )
        ) as $pin |
        {
          generation:(
            ($value | split(":::")[0]) + "@" + $pin.version
          ),
          version:$pin.version
        }
      end;
    def exact_previous_generation($value; $purpose):
      if $value == "" then {generation:"",version:""}
      else
        (
          $value |
          capture(
            "^(?<arn>arn:aws:secretsmanager:ap-northeast-1:718959508629:"
            + "secret:teamagent/dev/(database-url|hmac/" + $purpose + ")"
            + "-[A-Za-z0-9]{6}):::(?<version>[A-Za-z0-9_-]{32,64})$"
          )
        ) as $pin |
        {
          generation:($pin.arn + "@" + $pin.version),
          version:$pin.version
        }
      end;
    def purpose_metadata($tasks; $prefix; $purpose):
      ([
        $tasks[] |
        (exact_generation(
          .secrets[$prefix + "_SECRET"]; $purpose
        )) as $primary |
        (.secrets[$prefix + "_PREVIOUS_SECRET"] // "") as $previous_value |
        (exact_previous_generation($previous_value; $purpose)) as $previous |
        (.env[$prefix + "_PRIMARY_GENERATION"] // "") as $primary_env |
        (.env[$prefix + "_PREVIOUS_GENERATION"] // "") as $previous_env |
        (.env[$prefix + "_PREVIOUS_ROTATION_STARTED_AT"] // "") as $t0 |
        (.env.TEAMAGENT_HMAC_ROTATION_EPOCH // "") as $rotation_epoch |
        {
          primary_generation:$primary.generation,
          primary_version_id:$primary.version,
          previous_generation:$previous.generation,
          previous_version_id:$previous.version,
          rotation_started_at:$t0,
          rotation_epoch:$rotation_epoch,
          valid:(
            $primary_env == $primary.generation and
            ($rotation_epoch |
              test("^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")) and
            (
              if $previous_value == "" then
                $previous_env == "" and $t0 == ""
              else
                $previous_env == $previous.generation and
                ($t0 | test("^(0|[1-9][0-9]{0,9})$"))
              end
            )
          )
        }
      ]) as $rows |
      if (
        ($rows | length) > 0 and
        ($rows | unique | length) == 1 and
        $rows[0].valid
      ) then $rows[0] | del(.valid)
      else error("purpose HMAC metadata is not exact and uniform")
      end;
    (purpose_metadata(
      [.taskdefs.mcp, .taskdefs.morning];
      "MAIL_ACTION_HMAC";
      "mail-action"
    )) as $mail |
    (purpose_metadata(
      [.taskdefs.mcp, .taskdefs.connect_web];
      "REPORT_LINK_HMAC";
      "report-link"
    )) as $report |
    if (
      (
        prefix_absent(.taskdefs.connect_web; "MAIL_ACTION_") or
        purpose_metadata(
          [.taskdefs.connect_web];
          "MAIL_ACTION_HMAC";
          "mail-action"
        ) == $mail
      ) and
      prefix_absent(.taskdefs.morning; "REPORT_LINK_")
    ) then . else
      error("live HMAC consumer ownership is not exact")
    end |
    if $mail.rotation_epoch != $report.rotation_epoch then
      error("purpose HMAC rotation epochs differ")
    else {
      mail_action_hmac_deployed_primary_generation:
        $mail.primary_generation,
      mail_action_hmac_deployed_previous_generation:
        $mail.previous_generation,
      mail_action_hmac_deployed_rotation_started_at:
        $mail.rotation_started_at,
      report_link_hmac_deployed_primary_generation:
        $report.primary_generation,
      report_link_hmac_deployed_previous_generation:
        $report.previous_generation,
      report_link_hmac_deployed_rotation_started_at:
        $report.rotation_started_at
    }
    end
  ' "$snapshot" > "$output" ||
    die "live HMAC metadataはexact VersionId pin・generation env・rotation epochの完全一致が必要です"
  chmod 600 "$output"
}

validate_ecs_service_saga_receipt() {
  local receipt="$1" expected_stage="$2"
  jq -e \
    --arg stage "$expected_stage" \
    --arg attempt "$APPLY_ATTEMPT_ID" \
    --arg plan "$STAGED_PLAN_SHA256" '
    (keys | sort) == ([
      "apply_attempt_id",
      "baseline_sha256",
      "kind",
      "ledger_item_sha256",
      "plan_sha256",
      "planned_sha256",
      "receipt_sha256",
      "record_id",
      "schema_version",
      "stage"
    ] | sort) and
    .kind == "teamagent-ecs-service-apply-saga-receipt" and
    .schema_version == 1 and
    .record_id == ("ecs-service-apply#" + $attempt) and
    .stage == $stage and
    .plan_sha256 == $plan and
    .apply_attempt_id == $attempt and
    (.baseline_sha256 | test("^[0-9a-f]{64}$")) and
    (.planned_sha256 | test("^[0-9a-f]{64}$")) and
    (.ledger_item_sha256 | test("^[0-9a-f]{64}$")) and
    (.receipt_sha256 | test("^[0-9a-f]{64}$"))
  ' "$receipt" >/dev/null ||
    die "durable ECS service saga receiptがplan/attempt/stageと不一致です"
  [ "$(jq -j -cS 'del(.receipt_sha256)' "$receipt" | sha256_text)" = "$(
    jq -er '.receipt_sha256' "$receipt"
  )" ] ||
    die "durable ECS service saga receipt hashが不正です"
}

validate_eventbridge_saga_receipt() {
  local receipt="$1" expected_stage="$2"
  jq -e \
    --arg stage "$expected_stage" \
    --arg attempt "$APPLY_ATTEMPT_ID" \
    --arg plan "$STAGED_PLAN_SHA256" '
    (keys | sort) == ([
      "apply_attempt_id",
      "baseline_sha256",
      "kind",
      "ledger_item_sha256",
      "plan_sha256",
      "planned_sha256",
      "receipt_sha256",
      "record_id",
      "rotation_epoch",
      "schema_version",
      "stage",
      "verified_at"
    ] | sort) and
    .kind == "teamagent-eventbridge-apply-saga-receipt" and
    .schema_version == 2 and
    .record_id ==
      ("ecs-service-apply#eventbridge#active#" + .rotation_epoch) and
    (.rotation_epoch | type == "string" and length > 0) and
    .stage == $stage and
    .plan_sha256 == $plan and
    .apply_attempt_id == $attempt and
    (.baseline_sha256 | test("^[0-9a-f]{64}$")) and
    (.planned_sha256 | test("^[0-9a-f]{64}$")) and
    (.ledger_item_sha256 | test("^[0-9a-f]{64}$")) and
    (.receipt_sha256 | test("^[0-9a-f]{64}$")) and
    (.verified_at | type == "number" and floor == . and . > 0)
  ' "$receipt" >/dev/null ||
    die "durable EventBridge saga receiptがplan/attempt/stageと不一致です"
  [ "$(jq -j -cS 'del(.receipt_sha256)' "$receipt" | sha256_text)" = "$(
    jq -er '.receipt_sha256' "$receipt"
  )" ] ||
    die "durable EventBridge saga receipt hashが不正です"
}

consumer_image_map() {
  local desired_openclaw_image="${1:-}"
  local desired_mcp_image="${2:-}"
  local desired_x_image="${3:-}"
  local desired_tiktok_image="${4:-}"
  local desired_connect_web_image="${5:-}"
  local desired_ingest_image="${6:-}"
  local desired_morning_digest_image="${7:-}"
  local desired_canary_image="${8:-}"
  jq -n -c \
    --arg mcp "$desired_mcp_image" \
    --arg connect_web "$desired_connect_web_image" \
    --arg openclaw "$desired_openclaw_image" \
    --arg canary "$desired_canary_image" \
    --arg ingest "$desired_ingest_image" \
    --arg morning_digest "$desired_morning_digest_image" \
    --arg x_buzz_worker "$desired_x_image" \
    --arg tiktok_acquire "$desired_tiktok_image" \
    '{
      mcp:$mcp,
      connect_web:$connect_web,
      openclaw:$openclaw,
      canary:$canary,
      ingest:$ingest,
      morning_digest:$morning_digest,
      x_buzz_worker:$x_buzz_worker,
      tiktok_acquire:$tiktok_acquire
    }'
}

validate_sync_consumer_images() {
  local snapshot="$1"
  local expected_consumer_images="$2"
  local registry="$IMAGE_DEPLOYMENT_CONSUMER_REGISTRY"
  local repository_prefix legacy_tiktok_repository
  repository_prefix="${EXPECTED_ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/"
  legacy_tiktok_repository="${repository_prefix}teamagent-dev-tiktok-acquire"

  # The exception below remains anchored to the same canonical live task image
  # used by validate_media_envelope_cutover_gate: snapshot.image must still equal
  # the sync expected image, and only the exact legacy TikTok digest may differ
  # from the code-owned generic release repository.
  jq -e \
    --argjson expected "$expected_consumer_images" \
    --arg repository_prefix "$repository_prefix" \
    --arg legacy_tiktok_repository "$legacy_tiktok_repository" \
    --slurpfile registry "$registry" '
    def guard_consumers:
      [
        {
          consumer_id:"mcp",
          snapshot_key:"mcp",
          terraform_task_definition_address:"aws_ecs_task_definition.mcp",
          ecs_family:"teamagent-dev-mcp",
          container_name:"teamagent-mcp",
          activator:{type:"ecs_service",identity:"teamagent-dev-mcp"}
        },
        {
          consumer_id:"connect_web",
          snapshot_key:"connect_web",
          terraform_task_definition_address:
            "aws_ecs_task_definition.connect_web[0]",
          ecs_family:"teamagent-dev-connect-web",
          container_name:"connect-web",
          activator:{
            type:"ecs_service",
            identity:"teamagent-dev-connect-web"
          }
        },
        {
          consumer_id:"openclaw",
          snapshot_key:"openclaw",
          terraform_task_definition_address:
            "aws_ecs_task_definition.openclaw[0]",
          ecs_family:"teamagent-dev-openclaw",
          container_name:"openclaw",
          activator:{type:"ecs_service",identity:"teamagent-dev-openclaw"}
        },
        {
          consumer_id:"canary",
          snapshot_key:"canary",
          terraform_task_definition_address:
            "aws_ecs_task_definition.canary[0]",
          ecs_family:"teamagent-dev-canary",
          container_name:"canary",
          activator:{
            type:"eventbridge_rule_ecs_target",
            identity:"teamagent-dev-canary-hourly"
          }
        },
        {
          consumer_id:"ingest",
          snapshot_key:"ingest",
          terraform_task_definition_address:
            "aws_ecs_task_definition.ingest[0]",
          ecs_family:"teamagent-dev-ingest",
          container_name:"ingest",
          activator:{
            type:"eventbridge_rule_ecs_target",
            identity:"teamagent-dev-ingest-weekly"
          }
        },
        {
          consumer_id:"morning_digest",
          snapshot_key:"morning",
          terraform_task_definition_address:
            "aws_ecs_task_definition.morning_digest[0]",
          ecs_family:"teamagent-dev-morning-digest",
          container_name:"morning-digest",
          activator:{
            type:"eventbridge_rule_ecs_target",
            identity:"teamagent-dev-morning-digest-weekday"
          }
        },
        {
          consumer_id:"x_buzz_worker",
          snapshot_key:"x_buzz",
          terraform_task_definition_address:
            "aws_ecs_task_definition.x_buzz_worker[0]",
          ecs_family:"teamagent-dev-x-buzz-worker",
          container_name:"worker",
          activator:{
            type:"lambda_taskdef_arn_environment",
            identity:"teamagent-dev-x-buzz-dispatch"
          }
        },
        {
          consumer_id:"tiktok_acquire",
          snapshot_key:"tiktok",
          terraform_task_definition_address:
            "aws_ecs_task_definition.tiktok_acquire[0]",
          ecs_family:"teamagent-dev-tiktok-acquire",
          container_name:"acquire",
          activator:{
            type:"lambda_taskdef_arn_environment",
            identity:"teamagent-dev-tiktok-acquire-dispatch"
          }
        }
      ];
    . as $snapshot |
    $registry as $registries |
    guard_consumers as $guard |
    ($registries | length) == 1 and
    ($registries[0] | keys | sort) == ["consumers","schema_version"] and
    $registries[0].schema_version == 1 and
    ($registries[0].consumers | type) == "array" and
    ($registries[0].consumers | length) == 8 and
    ($guard | length) == 8 and
    ($expected | type) == "object" and
    ($guard | map(.consumer_id) | sort) ==
      ($registries[0].consumers | map(.consumer_id) | sort) and
    ($expected | keys | sort) == ($guard | map(.consumer_id) | sort) and
    all($guard[];
      . as $spec |
      ([
        $registries[0].consumers[] |
        select(.consumer_id == $spec.consumer_id)
      ]) as $matches |
      ($matches | length) == 1 and
      ($matches[0] | keys | sort) == ([
        "activator",
        "consumer_id",
        "container_name",
        "ecs_family",
        "provisional",
        "provisional_reason",
        "receipt",
        "release_repository",
        "terraform_task_definition_address"
      ] | sort) and
      $matches[0].terraform_task_definition_address ==
        $spec.terraform_task_definition_address and
      $matches[0].ecs_family == $spec.ecs_family and
      $matches[0].container_name == $spec.container_name and
      $matches[0].activator == $spec.activator and
      ($matches[0].release_repository |
        type == "string" and test("^[a-z0-9][a-z0-9._/-]*$")) and
      ($matches[0].receipt | type) == "object" and
      ($matches[0].receipt | keys | sort) == ["pipeline","subject"] and
      ($matches[0].receipt.pipeline |
        type == "string" and length > 0) and
      ($matches[0].receipt.subject |
        type == "string" and length > 0) and
      $matches[0].provisional == false and
      $matches[0].provisional_reason == null and
      ($snapshot.taskdefs[$spec.snapshot_key] | type) == "object" and
      ($snapshot.taskdefs[$spec.snapshot_key].image | type) == "string" and
      $snapshot.taskdefs[$spec.snapshot_key].image ==
        $expected[$spec.consumer_id] and
      ($expected[$spec.consumer_id] | type) == "string" and
      ($expected[$spec.consumer_id] | split("@") | length) == 2 and
      (
        ($expected[$spec.consumer_id] | split("@")[0]) ==
          ($repository_prefix + $matches[0].release_repository) or
        (
          $spec.consumer_id == "tiktok_acquire" and
          $matches[0].release_repository == "teamagent-media-worker" and
          ($expected[$spec.consumer_id] | split("@")[0]) ==
            $legacy_tiktok_repository
        )
      ) and
      ($expected[$spec.consumer_id] | split("@")[1] |
        test("^sha256:[0-9a-f]{64}$"))
    )
  ' "$snapshot" >/dev/null ||
    die "strict syncはregistryと完全一致する8 consumerについて、検証済みafter.imageとの個別一致・許容repository・完全digest pinが必要です（tiktok_acquireのexact live legacy digestだけはmedia cutover前に限り許容）"
}

# Terraform precondition へ渡す、live 由来の non-secret object。
core_from_snapshot() {
  local snapshot="$1"
  local output="$2"
  local mode="$3"
  local migration_id="$4"
  local desired_openclaw_image="$5"
  local desired_mcp_image="$6"
  local desired_x_image="$7"
  local desired_tiktok_image="$8"
  local desired_connect_web_image="${9:-}"
  local desired_ingest_image="${10:-}"
  local desired_morning_digest_image="${11:-}"
  local desired_canary_image="${12:-}"
  local preflight_sha256="${13:-}"
  local hmac_transition_epoch="${14:-0}"
  local desired_ingest_rule="${15:-}"
  local desired_morning_rule="${16:-}"
  local desired_canary_rule="${17:-}"
  local versioning_pre_cutover_receipt_sha256="${18:-}"
  local log_cutover_contract_sha256="${19:-}"
  local required_migration_id="${20:-}"
  local required_migration_apply_receipt_sha256="${21:-}"
  local desired_consumer_images
  desired_consumer_images="$(
    consumer_image_map \
      "$desired_openclaw_image" "$desired_mcp_image" \
      "$desired_x_image" "$desired_tiktok_image" \
      "$desired_connect_web_image" "$desired_ingest_image" \
      "$desired_morning_digest_image" "$desired_canary_image"
  )" || die "consumer別期待image mapを構築できません"
  if [ -z "$desired_ingest_rule" ]; then
    desired_ingest_rule="$(jq -r '.rules.ingest.critical.state == "ENABLED"' "$snapshot")"
    desired_morning_rule="$(jq -r '.rules.morning.critical.state == "ENABLED"' "$snapshot")"
    desired_canary_rule="$(jq -r '.rules.canary.critical.state == "ENABLED"' "$snapshot")"
  fi
  if [ "$mode" = "sync" ]; then
    validate_sync_consumer_images "$snapshot" "$desired_consumer_images"
  fi
  jq -S -c \
    --arg mode "$mode" \
    --arg migration_id "$migration_id" \
    --arg desired_openclaw_image "$desired_openclaw_image" \
    --arg desired_mcp_image "$desired_mcp_image" \
    --arg desired_x_image "$desired_x_image" \
    --arg desired_tiktok_image "$desired_tiktok_image" \
    --argjson desired_consumer_images "$desired_consumer_images" \
    --arg preflight_sha256 "$preflight_sha256" \
    --arg versioning_pre_cutover_receipt_sha256 \
      "$versioning_pre_cutover_receipt_sha256" \
    --arg log_cutover_contract_sha256 "$log_cutover_contract_sha256" \
    --arg required_migration_id "$required_migration_id" \
    --arg required_migration_apply_receipt_sha256 \
      "$required_migration_apply_receipt_sha256" \
    --argjson hmac_transition_epoch "$hmac_transition_epoch" \
    --argjson desired_ingest_rule "$desired_ingest_rule" \
    --argjson desired_morning_rule "$desired_morning_rule" \
    --argjson desired_canary_rule "$desired_canary_rule" '
    def boolenv($value):
      (($value // "0") | tostring | ascii_downcase) as $v |
      if ($v == "1" or $v == "true" or $v == "yes" or $v == "on") then true
      elif ($v == "0" or $v == "false" or $v == "no" or $v == "off" or $v == "") then false
      else error("invalid boolean env") end;
    . as $s |
    ($s.taskdefs.mcp.env) as $m |
    ($s.taskdefs.ingest.env) as $i |
    ($s.taskdefs.morning.env) as $d |
    {
      project_name: $s.project,
      environment: $s.environment,
      aws_region: $s.region,
      account_id: $s.account_id,
      mode: $mode,
      migration_id: $migration_id,
      preflight_receipt_sha256: $preflight_sha256,
      versioning_pre_cutover_receipt_sha256:
        $versioning_pre_cutover_receipt_sha256,
      log_cutover_contract_sha256:$log_cutover_contract_sha256,
      required_migration_id:$required_migration_id,
      required_migration_apply_receipt_sha256:
        $required_migration_apply_receipt_sha256,
      live_openclaw_image: $s.taskdefs.openclaw.image,
      desired_openclaw_image: $desired_openclaw_image,
      live_mcp_image: $s.taskdefs.mcp.image,
      desired_mcp_image: $desired_mcp_image,
      live_x_image: $s.taskdefs.x_buzz.image,
      desired_x_image: $desired_x_image,
      live_tiktok_image: $s.taskdefs.tiktok.image,
      desired_tiktok_image: $desired_tiktok_image,
      live_consumer_images: {
        mcp:$s.taskdefs.mcp.image,
        connect_web:$s.taskdefs.connect_web.image,
        openclaw:$s.taskdefs.openclaw.image,
        canary:$s.taskdefs.canary.image,
        ingest:$s.taskdefs.ingest.image,
        morning_digest:$s.taskdefs.morning.image,
        x_buzz_worker:$s.taskdefs.x_buzz.image,
        tiktok_acquire:$s.taskdefs.tiktok.image
      },
      desired_consumer_images: $desired_consumer_images,
      enable_connect_web: true,
      enable_ingest_schedule: true,
      enable_morning_digest: true,
      enable_canary_health: true,
      enable_x_research: true,
      enable_tiktok_acquire: true,
      enable_scrape_tools: boolenv($m.USE_VIDEO_TOOLS),
      enable_reminders: (($d.REMINDER_SCHEDULER_GROUP // "") != ""),
      enable_report_shorturl: boolenv($m.USE_REPORT_SHORTURL),
      enable_research_persist: boolenv($m.USE_RESEARCH_PERSIST),
      enable_kaiwai_classify: boolenv($m.USE_KAIWAI_CLASSIFY),
      use_calendar_event_tool: boolenv($m.USE_CALENDAR_EVENT_TOOL),
      use_schedule_propose_tool: boolenv($m.USE_SCHEDULE_PROPOSE_TOOL),
      use_calendar_freebusy_tool: boolenv($m.USE_CALENDAR_FREEBUSY_TOOL),
      use_slack_summary_tool: boolenv($m.USE_SLACK_SUMMARY_TOOL),
      use_video_capture_tool: boolenv($m.USE_VIDEO_CAPTURE_TOOL),
      use_web_research_tool: boolenv($m.USE_WEB_RESEARCH_TOOL),
      enable_progress_notify: boolenv($m.ENABLE_PROGRESS_NOTIFY),
      use_entity_tags: boolenv($i.USE_ENTITY_TAGS),
      use_ailavault_deeplinks: boolenv($m.USE_AILAVAULT_DEEPLINKS),
      use_payload_offload: boolenv($m.USE_PAYLOAD_OFFLOAD),
      video_quota_enabled: boolenv($m.VIDEO_QUOTA_ENABLED),
      use_analysis_cache: boolenv($m.ANALYSIS_CACHE_ENABLED),
      morning_digest_slack_unread: boolenv($d.MORNING_DIGEST_SLACK_UNREAD),
      morning_digest_schedule_button: boolenv($d.MORNING_DIGEST_SCHEDULE_BUTTON),
      morning_digest_calendar_button: boolenv($d.MORNING_DIGEST_CALENDAR_BUTTON),
      morning_digest_compact: boolenv($d.MORNING_DIGEST_COMPACT),
      morning_digest_reminders: boolenv($d.MORNING_DIGEST_REMINDERS),
      shared_company_domains: ($m.TEAMAGENT_SHARED_COMPANY_DOMAINS // ""),
      slack_team_id: ($m.SLACK_TEAM_ID // ""),
      ingest_rule_enabled: $desired_ingest_rule,
      morning_digest_rule_enabled: $desired_morning_rule,
      canary_rule_enabled: $desired_canary_rule,
      tiktok_dispatch_static_environment: $s.dispatchers.tiktok.static_environment,
      x_dispatch_static_environment: $s.dispatchers.x_buzz.static_environment,
      tiktok_dispatch_code_sha256: $s.dispatchers.tiktok.code_sha256,
      x_dispatch_code_sha256: $s.dispatchers.x_buzz.code_sha256,
      monitoring: $s.monitoring,
      alarm_delivery: $s.alarm_delivery,
      api_gateway: $s.api_gateway,
      connect_app_html: $s.connect_app_html,
      hmac_transition_epoch: $hmac_transition_epoch,
      deployed_hmac: $s.hmac
    }
  ' "$snapshot" > "$output" || die "live runtimeのboolean/env契約が不正です"

  if [ "$mode" = "sync" ]; then
    jq -e '
      .alarm_delivery.canonical_topic_arn ==
        "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-openclaw-alarms" and
      .alarm_delivery.canonical_topic_exists == true and
      .alarm_delivery.confirmed_email_endpoint_sha256 == [
        "88c6452f9db04017250aa5728b4815bccb55b5ecc0b35b50a5234170dc08d1e6"
      ] and
      .alarm_delivery.subscription_inventory_count == 1 and
      .alarm_delivery.pending_subscription_count == 0 and
      .alarm_delivery.subscription_protocols == ["email"] and
      (.alarm_delivery.subscription_inventory_sha256 |
        test("^[0-9a-f]{64}$")) and
      (.alarm_delivery.confirmed_subscription_metadata_sha256 |
        test("^[0-9a-f]{64}$")) and
      .alarm_delivery.destination_state_sha256 ==
        "c942dbb7b97da1f4d9debb1ba241ee89bf8c1d951d8d75bdea3056850838ddc9" and
      .alarm_delivery.attached_chatbot_configuration_arns == [] and
      .alarm_delivery.legacy_topic_arn ==
        "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-alarms" and
      .alarm_delivery.legacy_topic_exists == false and
      .alarm_delivery.legacy_action_reference_count == 0
    ' "$output" >/dev/null ||
      die "strict syncは確認済みalarm deliveryとlegacy topic完全退役が必須です"
  fi

  # scrape gate は USE_VIDEO_TOOLS と USE_TIKTOK_TOOLS の対。片側だけなら source へ安全に写せない。
  local video_flag tiktok_flag
  video_flag="$(jq -r '.taskdefs.mcp.env.USE_VIDEO_TOOLS // "0" | ascii_downcase' "$snapshot")"
  tiktok_flag="$(jq -r '.taskdefs.mcp.env.USE_TIKTOK_TOOLS // "0" | ascii_downcase' "$snapshot")"
  [ "$video_flag" = "$tiktok_flag" ] || die "live USE_VIDEO_TOOLS と USE_TIKTOK_TOOLS が不一致です"
}

print_hcl_snapshot() {
  local core="$1"
  jq -r '
    def line($name; $value): $name + " = " + ($value | tojson);
    def pre_media_cutover_sync:
      .mode == "sync" and
      .live_tiktok_image == .desired_tiktok_image and
      (.live_tiktok_image |
        test(
          "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-dev-tiktok-acquire@sha256:[0-9a-f]{64}$"
        ));
    [
      "# terraform_runtime_guard.sh snapshot (non-secret / live-derived)",
      "# 既存の gitignored terraform.tfvars へ必要行だけ反映し、この出力自体は commit しない。",
      line("openclaw_image"; .live_openclaw_image),
      line("mcp_image"; .live_mcp_image),
      line("x_buzz_image"; .live_x_image),
      line(
        "media_worker_image";
        if pre_media_cutover_sync then "" else .live_tiktok_image end
      ),
      line("tiktok_acquire_image"; ""),
      line("enable_connect_web"; .enable_connect_web),
      line("enable_ingest_schedule"; .enable_ingest_schedule),
      line("enable_morning_digest"; .enable_morning_digest),
      line("enable_canary_health"; .enable_canary_health),
      line("enable_x_research"; .enable_x_research),
      line("enable_media_worker"; .enable_tiktok_acquire),
      line("enable_tiktok_acquire"; .enable_tiktok_acquire),
      line("enable_scrape_tools"; .enable_scrape_tools),
      line("enable_reminders"; .enable_reminders),
      line("enable_report_shorturl"; .enable_report_shorturl),
      line("enable_research_persist"; .enable_research_persist),
      line("enable_kaiwai_classify"; .enable_kaiwai_classify),
      line("use_calendar_event_tool"; .use_calendar_event_tool),
      line("use_schedule_propose_tool"; .use_schedule_propose_tool),
      line("use_calendar_freebusy_tool"; .use_calendar_freebusy_tool),
      line("use_slack_summary_tool"; .use_slack_summary_tool),
      line("use_video_capture_tool"; .use_video_capture_tool),
      line("use_web_research_tool"; .use_web_research_tool),
      line("enable_progress_notify"; .enable_progress_notify),
      line("use_entity_tags"; .use_entity_tags),
      line("use_ailavault_deeplinks"; .use_ailavault_deeplinks),
      line("use_payload_offload"; .use_payload_offload),
      line("video_quota_enabled"; .video_quota_enabled),
      line("use_analysis_cache"; .use_analysis_cache),
      line("morning_digest_slack_unread"; .morning_digest_slack_unread),
      line("morning_digest_schedule_button"; .morning_digest_schedule_button),
      line("morning_digest_calendar_button"; .morning_digest_calendar_button),
      line("morning_digest_compact"; .morning_digest_compact),
      line("morning_digest_reminders"; .morning_digest_reminders),
      line("shared_company_domains"; .shared_company_domains),
      line("slack_team_id"; .slack_team_id),
      line("ingest_rule_enabled"; .ingest_rule_enabled),
      line("morning_digest_rule_enabled"; .morning_digest_rule_enabled),
      line("canary_rule_enabled"; .canary_rule_enabled)
    ] | .[]
  ' "$core"
}

hmac_from_plan() {
  local plan_json="$1" output="$2"
  jq -e -S -c '
    {
      mail: {
        primary_secret_arn: .variables.mail_action_hmac_secret_arn.value,
        previous_secret_arn: .variables.mail_action_hmac_previous_secret_arn.value,
        previous_present:
          (.variables.mail_action_hmac_previous_secret_arn.value != ""),
        rotation_started_at:
          (.variables.mail_action_hmac_previous_rotation_started_at.value // null)
      },
      report: {
        primary_secret_arn: .variables.report_link_hmac_secret_arn.value,
        previous_secret_arn: .variables.report_link_hmac_previous_secret_arn.value,
        previous_present:
          (.variables.report_link_hmac_previous_secret_arn.value != ""),
        rotation_started_at:
          (.variables.report_link_hmac_previous_rotation_started_at.value // null)
      }
    } |
    . as $h |
    if (
      ($h.mail.primary_secret_arn | type) == "string" and
      ($h.report.primary_secret_arn | type) == "string" and
      ($h.mail.previous_secret_arn | type) == "string" and
      ($h.report.previous_secret_arn | type) == "string"
    ) then . else error("invalid HMAC plan metadata") end
  ' "$plan_json" > "$output" || die "planからHMAC metadataを一意に取得できません"
}

# Application transition validatorと同じpresence/T0/deadline契約を、実デプロイ
# task definitionから得たmetadataに対して実行する。secret materialは入力しない。
validate_hmac_transition_metadata() {
  local snapshot="$1" proposed="$2" mode="$3" trusted_now="$4" output="$5"
  jq -n -S -c \
    --arg mode "$mode" \
    --argjson trusted_now "$trusted_now" \
    --slurpfile live "$snapshot" \
    --slurpfile proposed "$proposed" '
    def base_arn:
      if contains(":::") then split(":::")[0] else . end;
    def result($purpose; $ok; $code; $deadline):
      {
        purpose: $purpose,
        ok: $ok,
        code: $code,
        previous_deadline: $deadline
      };
    def validate($purpose; $max_ttl):
      $live[0].hmac[$purpose] as $deployed |
      $proposed[0][$purpose] as $next |
      ($deployed.rotation_started_at //
        $next.rotation_started_at // null) as $t0 |
      (if $t0 == null then null else ($t0 + 900 + $max_ttl) end) as $deadline |
      if (
        ($deployed.previous_present | type) != "boolean" or
        ($next.previous_present | type) != "boolean" or
        ($deployed.primary_secret_arn | type) != "string" or
        ($next.primary_secret_arn | type) != "string"
      ) then result($purpose; false; "invalid_presence"; $deadline)
      elif $deployed.previous_present !=
           ($deployed.rotation_started_at != null)
      then result($purpose; false; "deployed_pair_mismatch"; $deadline)
      elif $next.previous_present != ($next.rotation_started_at != null)
      then result($purpose; false; "proposed_pair_mismatch"; $deadline)
      elif (
        $next.rotation_started_at != null and
        (($next.rotation_started_at | type) != "number" or
         ($next.rotation_started_at | floor) != $next.rotation_started_at or
         $next.rotation_started_at < 0 or
         $next.rotation_started_at > 9999999999)
      ) then result($purpose; false; "invalid_t0"; $deadline)
      elif $mode == "sync" and $next != $deployed
      then result($purpose; false; "sync_metadata_changed"; $deadline)
      elif (
        $deployed.previous_present and $next.previous_present and
        $next.rotation_started_at != $deployed.rotation_started_at
      ) then result($purpose; false; "t0_changed"; $deadline)
      elif (
        $deployed.previous_present and $next.previous_present and
        $next.previous_secret_arn != $deployed.previous_secret_arn
      ) then result($purpose; false; "previous_generation_changed"; $deadline)
      elif (
        $next.previous_present and
        $next.rotation_started_at > ($trusted_now + 300)
      ) then result($purpose; false; "future_t0"; $deadline)
      elif (
        $deployed.previous_present and ($next.previous_present | not) and
        $trusted_now < ($deployed.rotation_started_at + 900 + $max_ttl)
      ) then result($purpose; false; "removal_before_deadline";
        ($deployed.rotation_started_at + 900 + $max_ttl))
      elif (
        $next.previous_present and
        $trusted_now >= ($next.rotation_started_at + 900 + $max_ttl)
      ) then result($purpose; false; "expired_previous_not_removed";
        ($next.rotation_started_at + 900 + $max_ttl))
      elif (
        $deployed.previous_present and
        $next.primary_secret_arn != $deployed.primary_secret_arn
      ) then result($purpose; false; "prior_generation_not_removed"; $deadline)
      elif (
        $next.primary_secret_arn != $deployed.primary_secret_arn and
        (
          ($next.previous_present | not) or
          (($next.previous_secret_arn | base_arn) !=
            $deployed.primary_secret_arn) or
          $trusted_now > ($next.rotation_started_at + 900)
        )
      ) then result($purpose; false; "issuer_cutover_window"; $deadline)
      else result($purpose; true; "ok"; $deadline)
      end;
    [validate("mail"; 86400), validate("report"; 604800)] as $results |
    {
      ok: ($results | all(.ok)),
      code: (if ($results | all(.ok)) then "ok"
             else ([$results[] | select(.ok == false) | .code][0]) end),
      trusted_now: $trusted_now,
      results: $results
    }
  ' > "$output"
  jq -e '.ok == true and .code == "ok"' "$output" >/dev/null ||
    die "HMAC rotation transitionが拒否されました: $(jq -r '.code' "$output")"
}

validate_secret_metadata() {
  local value_from="$1" expect_current="$2" purpose="$3"
  local base_arn version_id describe versions purpose_path
  base_arn="${value_from%%:::*}"
  version_id=""
  if [[ "$value_from" == *":::"* ]]; then
    version_id="${value_from##*:::}"
  fi
  case "$purpose" in
    mail) purpose_path="mail-action" ;;
    report) purpose_path="report-link" ;;
    *) die "内部error: unknown HMAC purpose" ;;
  esac
  if [ "$expect_current" = "true" ]; then
    [[ "$base_arn" =~ ^arn:aws:secretsmanager:${REGION}:${EXPECTED_ACCOUNT_ID}:secret:teamagent/dev/hmac/${purpose_path}-[A-Za-z0-9]{6}$ ]] ||
      die "$purpose HMAC primary secret metadataのaccount/region/service/purpose pathが不正です"
    [ -z "$version_id" ] || die "primary HMAC secretはversion selectorを含められません"
  else
    [[ "$base_arn" =~ ^arn:aws:secretsmanager:${REGION}:${EXPECTED_ACCOUNT_ID}:secret:teamagent/dev/(database-url|hmac/${purpose_path})-[A-Za-z0-9]{6}$ ]] ||
      die "$purpose HMAC previous secret metadataのaccount/region/service/generation pathが不正です"
    [[ "$version_id" =~ ^[A-Za-z0-9-]{32,64}$ ]] ||
      die "previous HMAC secretはexact version ID pinが必須です"
  fi

  ensure_tmp
  describe="$TMP_ROOT/secret-describe-$RANDOM.json"
  versions="$TMP_ROOT/secret-versions-$RANDOM.json"
  aws_cli secretsmanager describe-secret --secret-id "$base_arn" --output json > "$describe"
  jq -e --arg arn "$base_arn" '
    .ARN == $arn and (.DeletedDate == null) and
    (.Name | startswith("teamagent/dev/"))
  ' "$describe" >/dev/null || die "HMAC secretのdeployed metadataが不正です"
  aws_cli secretsmanager list-secret-version-ids \
    --secret-id "$base_arn" --include-deprecated --output json > "$versions"
  if [ "$expect_current" = "true" ]; then
    jq -e '
      [.Versions[] |
        select((.VersionStages // []) | index("AWSCURRENT"))] | length == 1
    ' "$versions" >/dev/null || die "HMAC primaryに一意なAWSCURRENTがありません"
  else
    jq -e --arg version "$version_id" '
      [.Versions[] | select(.VersionId == $version)] | length == 1
    ' "$versions" >/dev/null || die "HMAC previousのexact version metadataが存在しません"
  fi
}

validate_hmac_secret_metadata() {
  local proposed="$1"
  local purpose primary previous
  for purpose in mail report; do
    primary="$(jq -er --arg purpose "$purpose" '.[$purpose].primary_secret_arn' "$proposed")"
    validate_secret_metadata "$primary" true "$purpose"
    previous="$(jq -er --arg purpose "$purpose" '.[$purpose].previous_secret_arn' "$proposed")"
    if [ -n "$previous" ]; then
      validate_secret_metadata "$previous" false "$purpose"
    fi
  done
}

plan_has_address() {
  local plan_json="$1"
  local address="$2"
  jq -e --arg address "$address" '.resource_changes[]? | select(.address == $address)' "$plan_json" >/dev/null
}

validate_hmac_runtime_mutation_gates() {
  local plan_json="$1"
  python3 "$HMAC_PLAN_HELPER" verify-runtime-mutations \
    --plan-json "$plan_json" >/dev/null ||
    die "HMAC task/service/EventBridge promotion gateのaction/input/workload束縛が不正です"
}

validate_common_plan_schema() {
  local plan_json="$1" core="$2"
  jq -e --slurpfile expected_core "$core" '
    def pre_media_cutover_sync:
      $expected_core[0].mode == "sync" and
      $expected_core[0].live_tiktok_image ==
        $expected_core[0].desired_tiktok_image and
      ($expected_core[0].live_tiktok_image |
        test(
          "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-dev-tiktok-acquire@sha256:[0-9a-f]{64}$"
        ));
    type == "object" and
    .format_version == "1.2" and
    (.terraform_version | type == "string") and
    (.applyable | type == "boolean") and .errored == false and
    .complete == true and
    (.timestamp | type == "string") and
    (.planned_values | type == "object") and
    (.prior_state | type == "object") and
    (.configuration | type == "object") and
    (.variables | type == "object") and
    (.resource_changes | type == "array") and
    (.resource_drift | type == "array") and
    ((.deferred_changes // []) | length == 0) and
    ((.action_invocations // []) | length == 0) and
    (.checks | type == "array") and
    ([.resource_changes[].address] | length == (unique | length)) and
    (.checks | all(
      .status == "pass" and ((.instances // []) | all(.status == "pass"))
    )) and
    .variables.openclaw_image.value == $expected_core[0].desired_openclaw_image and
    .variables.mcp_image.value == $expected_core[0].desired_mcp_image and
    .variables.x_buzz_image.value == $expected_core[0].desired_x_image and
    .variables.media_worker_image.value ==
      (if pre_media_cutover_sync
       then ""
       else $expected_core[0].desired_tiktok_image
       end) and
    .variables.tiktok_acquire_image.value == "" and
    .variables.enable_media_worker.value == true and
    .variables.enable_tiktok_acquire.value == true and
    .variables.ingest_rule_enabled.value == $expected_core[0].ingest_rule_enabled and
    .variables.morning_digest_rule_enabled.value ==
      $expected_core[0].morning_digest_rule_enabled and
    .variables.canary_rule_enabled.value == $expected_core[0].canary_rule_enabled and
    .variables.require_alarm_delivery.value == true and
    .variables.bedrock_logs_retention_days.value == 60 and
    (.variables.image_deployment_consumer_manifest.value.consumers |
      type) == "array" and
    (.variables.image_deployment_consumer_manifest.value.consumers |
      length) == 8 and
    ([
      .variables.image_deployment_consumer_manifest.value.consumers[] |
      .consumer_id
    ] | length) == ([
      .variables.image_deployment_consumer_manifest.value.consumers[] |
      .consumer_id
    ] | unique | length) and
    ([
      .variables.image_deployment_consumer_manifest.value.consumers[] |
      {key:.consumer_id,value:.after.image}
    ] | from_entries) == $expected_core[0].desired_consumer_images and
    (.variables.image_deployment_intent_id.value |
      test("^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")) and
    .variables.runtime_guard_live.value == $expected_core[0]
  ' "$plan_json" >/dev/null ||
    die "plan JSON schema/check/image/rule/runtime guard bindingが不正です"
  jq -e '
    ([.resource_changes[] |
      select(.address == "terraform_data.runtime_guard")] | length) == 1 and
    ([.resource_changes[] |
      select(.address == "terraform_data.production_image_release_gate")] |
      length) == 1 and
    ([.resource_changes[] |
      select(.address == "terraform_data.production_image_release_gate")][0]
      .change.actions) as $actions |
    ($actions | index("create")) != null and
    ($actions | all(. == "create" or . == "delete"))
  ' "$plan_json" >/dev/null ||
    die "runtime guardとone-use production provenance gateの複合planが不正です"
}

validate_manifest_change_allowlist() {
  local plan_json="$1" migration="$2"
  local contract_mode="${3:-verify}" contract_output="${4:-}"
  local reviewed destructive
  case "$contract_mode" in
    extract)
      [ -n "$contract_output" ] ||
        die "review plan contractの出力先がありません"
      python3 "$PLAN_CONTRACT_HELPER" extract \
        --plan "$plan_json" > "$contract_output" ||
        die "review plan contractを抽出できません"
      chmod 600 "$contract_output"
      ;;
    verify)
      reviewed="$TMP_ROOT/exact-reviewed-plan-${RANDOM}.json"
      jq -e -S '.reviewed_plan | select(type == "object")' \
        "$migration" > "$reviewed" ||
        die "migrationには全変更・driftのexact reviewed_planが必須です"
      chmod 600 "$reviewed"
      python3 "$PLAN_CONTRACT_HELPER" verify \
        --plan "$plan_json" --reviewed "$reviewed" ||
        die "migration planがexact reviewed_planと一致しません"
      ;;
    *) die "未知のplan contract modeです: $contract_mode" ;;
  esac

  destructive="$(jq -r --slurpfile migration "$migration" '
    .resource_changes[]? |
    select(.mode == "managed") |
    .address as $address |
    (.change.actions // []) as $actions |
    select($actions == ["delete"] or $actions == ["delete", "create"]) |
    select(
      ($migration[0].kind != "runtime") or
      ($address != "aws_iam_role_policy.tiktok_exec_secrets[0]") or
      ($actions != ["delete"])
    ) |
    "\($actions | join("/")) \(.address)"
  ' "$plan_json")"
  [ -z "$destructive" ] ||
    die "delete-first/pure destroyはmigrationでも禁止です:\n$destructive"
}

validate_runtime_task_contracts() {
  local plan_json="$1" snapshot="$2" core="$3"

  jq -e \
    --arg mcp_health \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4).close()" \
    --arg connect_health \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/healthz', timeout=4).close()" \
    --arg openclaw_health \
      "fetch('http://127.0.0.1:18789/readyz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" \
    --slurpfile core "$core" '
    def envmap:
      (.environment // []) | map({key: .name, value: .value}) | from_entries;
    def plain_tmp_volume:
      (.name == "tmp" or .name == "runtime-tmp") and
      ((.docker_volume_configuration // []) | length) == 0 and
      ((.efs_volume_configuration // []) | length) == 0 and
      ((.fsx_windows_file_server_volume_configuration // []) | length) == 0;
    def secure($container; $user):
      $container.user == $user and
      $container.readonlyRootFilesystem == true and
      $container.privileged != true and
      $container.linuxParameters.initProcessEnabled == true and
      (($container.linuxParameters.capabilities.drop // []) | sort) == ["ALL"] and
      (($container.linuxParameters.capabilities.add // []) | length) == 0;
    def tmp_mount($container):
      [($container.mountPoints // [])[] |
        select(
          (.sourceVolume == "tmp" or .sourceVolume == "runtime-tmp") and
          .containerPath == "/tmp" and
               .readOnly == false)] | length == 1;
    def exact_command($container; $command):
      (($container.entryPoint // []) | length) == 0 and
      ($container.command // []) == $command;
    def exact_health($container; $command; $start):
      $container.healthCheck == {
        command: $command,
        interval: 30,
        timeout: 5,
        retries: 5,
        startPeriod: $start
      };
    def no_health($container):
      (($container.healthCheck // {}) | length) == 0;
    def task($address; $name):
      [.resource_changes[] |
        select(.address == $address) |
        .change.after.container_definitions | fromjson |
        .[] | select(.name == $name)] |
      if length == 1 then .[0] else error("task container is not unique") end;
    def resource($address):
      [.resource_changes[] | select(.address == $address)] |
      if length == 1 then .[0].change.after else error("task resource missing") end;
    [
      {
        address: "aws_ecs_task_definition.openclaw[0]",
        name: "openclaw",
        image: $core[0].desired_consumer_images.openclaw,
        user: "65532:65532", profile: "openclaw"
      },
      {
        address: "aws_ecs_task_definition.mcp",
        name: "teamagent-mcp",
        image: $core[0].desired_consumer_images.mcp,
        user: "10001:10001", profile: "python"
      },
      {
        address: "aws_ecs_task_definition.connect_web[0]",
        name: "connect-web",
        image: $core[0].desired_consumer_images.connect_web,
        user: "10001:10001", profile: "python"
      },
      {
        address: "aws_ecs_task_definition.ingest[0]",
        name: "ingest",
        image: $core[0].desired_consumer_images.ingest,
        user: "10001:10001", profile: "python"
      },
      {
        address: "aws_ecs_task_definition.morning_digest[0]",
        name: "morning-digest",
        image: $core[0].desired_consumer_images.morning_digest,
        user: "10001:10001", profile: "python"
      },
      {
        address: "aws_ecs_task_definition.canary[0]",
        name: "canary",
        image: $core[0].desired_consumer_images.canary,
        user: "10001:10001", profile: "python"
      },
      {
        address: "aws_ecs_task_definition.tiktok_acquire[0]",
        name: "acquire",
        image: $core[0].desired_consumer_images.tiktok_acquire,
        user: "10001:10001", profile: "media"
      },
      {
        address: "aws_ecs_task_definition.x_buzz_worker[0]",
        name: "worker",
        image: $core[0].desired_consumer_images.x_buzz_worker,
        user: "10001:10001", profile: "python"
      }
    ] | all(. as $spec |
      (resource($spec.address)) as $task |
      (task($spec.address; $spec.name)) as $container |
      ($container | envmap) as $env |
      $task.skip_destroy == true and
      ($task.requires_compatibilities | sort) == ["FARGATE"] and
      $task.network_mode == "awsvpc" and
      $task.runtime_platform[0].cpu_architecture == "ARM64" and
      $task.runtime_platform[0].operating_system_family == "LINUX" and
      $container.image == $spec.image and
      secure($container; $spec.user) and
      tmp_mount($container) and
      $env.TMPDIR == "/tmp" and
      (
        if $spec.profile == "openclaw" then
          ($task.volume | length) == 2 and
          ([$task.volume[] | select(plain_tmp_volume and .name == "tmp")] | length) == 1 and
          ([$task.volume[] | select(
            .name == "state" and
            (.efs_volume_configuration | length) == 1 and
            .efs_volume_configuration[0].root_directory == "/" and
            .efs_volume_configuration[0].transit_encryption == "ENABLED" and
            (.efs_volume_configuration[0].authorization_config | length) == 1 and
            .efs_volume_configuration[0].authorization_config[0].iam == "ENABLED"
          )] | length) == 1 and
          [($container.mountPoints // [])[] | select(
            .sourceVolume == "state" and
            .containerPath == "/tmp/teamagent-openclaw/state" and
            .readOnly == false
          )] | length == 1 and
          exact_command($container; []) and
          $container.stopTimeout == 120 and
          exact_health(
            $container;
            ["CMD", "/nodejs/bin/node", "-e", $openclaw_health];
            40
          ) and
          ($env | has("OPENCLAW_CONFIG_PATH") | not)
        elif $spec.profile == "media" then
          ($task.volume | length) == 1 and
          ($task.volume[0] | (plain_tmp_volume and .name == "runtime-tmp")) and
          exact_command($container; []) and
          no_health($container) and
          $container.stopTimeout == 30 and
          $env.HOME == "/tmp/home" and
          $env.XDG_CACHE_HOME == "/tmp/.cache" and
          $env.PYTHONPYCACHEPREFIX == "/tmp/.pycache" and
          $env.MEDIA_JOB_BUCKET == "teamagent-dev-media-jobs-718959508629" and
          $env.MEDIA_JOBS_TABLE == "teamagent-dev-tiktok-acquire-jobs" and
          $env.MEDIA_ARTIFACT_TTL_SECONDS == "2592000" and
          ($env.MEDIA_BLOCKED_VPC_CIDRS | type) == "string" and
          ($env.MEDIA_BLOCKED_VPC_CIDRS | length) > 0
        elif $spec.name == "teamagent-mcp" then
          ($task.volume | length) == 1 and
          ($task.volume[0] | (plain_tmp_volume and .name == "runtime-tmp")) and
          exact_command(
            $container;
            ["/app/.venv/bin/python", "/app/scripts/run_mcp_vertex_entrypoint.py"]
          ) and
          exact_health(
            $container;
            ["CMD", "/app/.venv/bin/python", "-c", $mcp_health];
            40
          ) and
          $env.HOME == "/tmp/home" and
          $env.XDG_CACHE_HOME == "/tmp/.cache" and
          $env.PYTHONPYCACHEPREFIX == "/tmp/.pycache"
        elif $spec.name == "connect-web" then
          ($task.volume | length) == 1 and
          ($task.volume[0] | (plain_tmp_volume and .name == "runtime-tmp")) and
          exact_command(
            $container;
            ["/app/.venv/bin/python", "-m", "teamagent.connect_web"]
          ) and
          exact_health(
            $container;
            ["CMD", "/app/.venv/bin/python", "-c", $connect_health];
            30
          ) and
          $env.HOME == "/tmp/home" and
          $env.XDG_CACHE_HOME == "/tmp/.cache" and
          $env.PYTHONPYCACHEPREFIX == "/tmp/.pycache"
        else
          ($task.volume | length) == 1 and
          ($task.volume[0] | (plain_tmp_volume and .name == "runtime-tmp")) and
          exact_command(
            $container;
            if $spec.name == "ingest" then
              ["/app/.venv/bin/python", "/app/scripts/run_ingest_fargate.py"]
            elif $spec.name == "morning-digest" then
              ["/app/.venv/bin/python", "/app/scripts/run_morning_digest_fargate.py"]
            elif $spec.name == "canary" then
              ["/app/.venv/bin/python", "/app/scripts/run_canary_health.py"]
            elif $spec.name == "worker" then
              ["/app/.venv/bin/python", "-m", "teamagent.workers.x_buzz_job"]
            else error("unknown runtime command contract")
            end
          ) and
          no_health($container) and
          $env.HOME == "/tmp/home" and
          $env.XDG_CACHE_HOME == "/tmp/.cache" and
          $env.PYTHONPYCACHEPREFIX == "/tmp/.pycache"
        end
      )
    )
  ' "$plan_json" >/dev/null ||
    die "planned exact task definitionsがUID/GID/read-only/cap-drop/tmp/EFS/cache/image契約を満たしません"

  local spec address component expected_name allowed_env allowed_secrets
  for spec in \
    'aws_ecs_task_definition.openclaw[0]|openclaw|openclaw|["OPENCLAW_CONFIG_PATH","TMPDIR"]|[]' \
    'aws_ecs_task_definition.mcp|mcp|teamagent-mcp|["HOME","TMPDIR","XDG_CACHE_HOME","PYTHONPYCACHEPREFIX","UV_CACHE_DIR","MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT","REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT"]|["MAIL_ACTION_HMAC_SECRET","MAIL_ACTION_HMAC_PREVIOUS_SECRET","REPORT_LINK_HMAC_SECRET","REPORT_LINK_HMAC_PREVIOUS_SECRET"]' \
    'aws_ecs_task_definition.connect_web[0]|connect_web|connect-web|["HOME","TMPDIR","XDG_CACHE_HOME","PYTHONPYCACHEPREFIX","MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT","REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT"]|["MAIL_ACTION_HMAC_SECRET","MAIL_ACTION_HMAC_PREVIOUS_SECRET","REPORT_LINK_HMAC_SECRET","REPORT_LINK_HMAC_PREVIOUS_SECRET"]' \
    'aws_ecs_task_definition.ingest[0]|ingest|ingest|["HOME","TMPDIR","XDG_CACHE_HOME","PYTHONPYCACHEPREFIX"]|[]' \
    'aws_ecs_task_definition.morning_digest[0]|morning|morning-digest|["HOME","TMPDIR","XDG_CACHE_HOME","PYTHONPYCACHEPREFIX","MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT"]|["MAIL_ACTION_HMAC_SECRET","MAIL_ACTION_HMAC_PREVIOUS_SECRET"]' \
    'aws_ecs_task_definition.canary[0]|canary|canary|["HOME","TMPDIR","XDG_CACHE_HOME","PYTHONPYCACHEPREFIX"]|[]' \
    'aws_ecs_task_definition.tiktok_acquire[0]|tiktok|acquire|["AWS_REGION","HOME","TMPDIR","XDG_CACHE_HOME","PYTHONPYCACHEPREFIX","MEDIA_JOB_BUCKET","MEDIA_JOBS_TABLE","MEDIA_ARTIFACT_TTL_SECONDS","MEDIA_BLOCKED_VPC_CIDRS"]|[]' \
    'aws_ecs_task_definition.x_buzz_worker[0]|x_buzz|worker|["HOME","TMPDIR","XDG_CACHE_HOME","PYTHONPYCACHEPREFIX"]|[]'; do
    IFS='|' read -r address component expected_name allowed_env allowed_secrets <<< "$spec"
    jq -L "$GUARD_JQ_DIR" -e \
      --arg address "$address" \
      --arg component "$component" \
      --arg expected_name "$expected_name" \
      --argjson allowed_env "$allowed_env" \
      --argjson allowed_secrets "$allowed_secrets" \
      --slurpfile live "$snapshot" '
      include "terraform_runtime_guard";
      def envmap: (. // []) | map({key: .name, value: .value}) | from_entries;
      def secmap: (. // []) | map({key: .name, value: .valueFrom}) | from_entries;
      def changed_keys($before; $after):
        (($before | keys) + ($after | keys) | unique) |
        map(select($before[.] != $after[.]));
      .resource_changes[] | select(.address == $address) as $change |
      ($change.change.before | guard_task_from_tf) ==
        $live[0].taskdefs[$component].critical and
      ($change.change.after | guard_task_from_tf |
        del(.volumes) |
        .containers |= map(del(
          .user, .readonly_root_filesystem, .linux_parameters, .mount_points,
          .stop_timeout, .health_check, .command
        ))) ==
      ($live[0].taskdefs[$component].critical |
        del(.volumes) |
        .containers |= map(del(
          .user, .readonly_root_filesystem, .linux_parameters, .mount_points,
          .stop_timeout, .health_check, .command
        ))) and
      ($change.change.after.container_definitions | fromjson |
        [.[] | select(.name == $expected_name)][0]) as $after_container |
      ($live[0].taskdefs[$component].env) as $before_env |
      ($live[0].taskdefs[$component].secrets) as $before_secrets |
      ($after_container.environment | envmap) as $after_env |
      ($after_container.secrets | secmap) as $after_secrets |
      (changed_keys($before_env; $after_env) - $allowed_env | length) == 0 and
      (changed_keys($before_secrets; $after_secrets) - $allowed_secrets | length) == 0
    ' "$plan_json" >/dev/null ||
      die "$address はlive exact sourceから許可されたruntime/HMAC field以外も変更します"
  done
}

validate_planned_hmac_consumers() {
  local plan_json="$1" proposed="$2"
  jq -e --slurpfile proposed "$proposed" '
    def container($address; $name):
      [.resource_changes[] | select(.address == $address) |
       .change.after.container_definitions | fromjson |
       .[] | select(.name == $name)] |
      if length == 1 then .[0] else error("consumer missing") end;
    def envmap($container):
      ($container.environment // []) |
      map({key: .name, value: .value}) | from_entries;
    def secmap($container):
      ($container.secrets // []) |
      map({key: .name, value: .valueFrom}) | from_entries;
    def primary_base_arn($value):
      if ($value | contains(":::")) then
        $value | split(":::")[0]
      else $value
      end;
    def purpose($container; $prefix; $expected):
      (envmap($container)) as $env |
      (secmap($container)) as $secrets |
      primary_base_arn($secrets[$prefix + "_SECRET"]) ==
        $expected.primary_secret_arn and
      (
        if $expected.previous_present then
          $secrets[$prefix + "_PREVIOUS_SECRET"] ==
            $expected.previous_secret_arn and
          $env[$prefix + "_PREVIOUS_ROTATION_STARTED_AT"] ==
            ($expected.rotation_started_at | tostring)
        else
          ($secrets | has($prefix + "_PREVIOUS_SECRET") | not) and
          ($env | has($prefix + "_PREVIOUS_ROTATION_STARTED_AT") | not)
        end
      );
    def purpose_absent($container; $prefix):
      (envmap($container) | keys |
        all(.[]; startswith($prefix + "_") | not)) and
      (secmap($container) | keys |
        all(.[]; startswith($prefix + "_") | not));
    (container("aws_ecs_task_definition.mcp"; "teamagent-mcp")) as $mcp |
    (container("aws_ecs_task_definition.connect_web[0]"; "connect-web")) as $connect |
    (container("aws_ecs_task_definition.morning_digest[0]"; "morning-digest")) as $morning |
    purpose($mcp; "MAIL_ACTION_HMAC"; $proposed[0].mail) and
    purpose($morning; "MAIL_ACTION_HMAC"; $proposed[0].mail) and
    purpose_absent($connect; "MAIL_ACTION") and
    purpose($mcp; "REPORT_LINK_HMAC"; $proposed[0].report) and
    purpose($connect; "REPORT_LINK_HMAC"; $proposed[0].report) and
    purpose_absent($morning; "REPORT_LINK")
  ' "$plan_json" >/dev/null ||
    die "planned task definitionsのpurpose別HMAC env/secrets/T0が同一revision契約を満たしません"
}

validate_runtime_links() {
  local plan_json="$1" snapshot="$2"
  local spec address component
  for spec in \
    'aws_ecs_service.mcp[0]|mcp' \
    'aws_ecs_service.connect_web[0]|connect_web' \
    'aws_ecs_service.openclaw[0]|openclaw'; do
    IFS='|' read -r address component <<< "$spec"
    jq -L "$GUARD_JQ_DIR" -e \
      --arg address "$address" --arg component "$component" \
      --slurpfile live "$snapshot" '
      include "terraform_runtime_guard";
      .resource_changes[] | select(.address == $address) as $change |
      ($change.change.before | guard_service_from_tf |
        del(.wait_for_steady_state)) ==
        ($live[0].services[$component].critical |
          del(.wait_for_steady_state)) and
      (
        if $component == "openclaw" then
          $change.change.after.deployment_maximum_percent == 100 and
          $change.change.after.deployment_minimum_healthy_percent == 0 and
          $change.change.after.availability_zone_rebalancing == "ENABLED" and
          $change.change.after.deployment_circuit_breaker[0] ==
            {enable: true, rollback: true} and
          (($change.change.after | del(
             .task_definition, .deployment_maximum_percent,
             .deployment_minimum_healthy_percent,
             .deployment_circuit_breaker,
             .availability_zone_rebalancing
           )) ==
           ($change.change.before | del(
             .task_definition, .deployment_maximum_percent,
             .deployment_minimum_healthy_percent,
             .deployment_circuit_breaker,
             .availability_zone_rebalancing
           )))
        else
          $change.change.after.availability_zone_rebalancing == "ENABLED" and
          $change.change.after.wait_for_steady_state == true and
          $change.change.after.deployment_circuit_breaker[0] ==
            {enable: true, rollback: true} and
          (($change.change.after | del(
             .task_definition, .deployment_circuit_breaker,
             .availability_zone_rebalancing,
             .wait_for_steady_state
           )) ==
           ($change.change.before | del(
             .task_definition, .deployment_circuit_breaker,
             .availability_zone_rebalancing,
             .wait_for_steady_state
           )))
        end
      )
    ' "$plan_json" >/dev/null || die "$address のtask revision以外の非許可service変更を検出しました"
  done

  for spec in \
    'aws_cloudwatch_event_target.ingest_run_task[0]|ingest' \
    'aws_cloudwatch_event_target.morning_digest_run_task[0]|morning' \
    'aws_cloudwatch_event_target.canary_run_task[0]|canary'; do
    IFS='|' read -r address component <<< "$spec"
    jq -L "$GUARD_JQ_DIR" -e \
      --arg address "$address" --arg component "$component" \
      --slurpfile live "$snapshot" '
      include "terraform_runtime_guard";
      . as $plan |
      $plan.resource_changes[] | select(.address == $address) as $change |
      # ACTIVATION-SHIM(ingest): 一時対応。Activation 完了後に canonical registry と
      # release_evidence を原子的に正名化して撤去する。docs/activation/ACTIVATION_STATE.md 参照。
      if $component == "ingest" then
        $change.change.actions == ["no-op"] and
        ([$change.change.after_unknown // {} | paths(. == true)] |
          length == 0) and
        ([$plan.configuration.root_module.resources[] |
          select(
            .address ==
              "aws_cloudwatch_event_target.ingest_run_task"
          ) |
          .expressions.arn.references[]?] |
          index("aws_lambda_function.ingest_dispatch[0].arn")) != null and
        (($change.change.before | guard_target_from_tf) ==
          $live[0].targets.ingest.critical) and
        $change.change.before == $change.change.after
      else
        ($change.change.before.ecs_target[0].task_definition_arn ==
          $live[0].targets[$component].task_definition) and
        (($change.change.before | guard_target_from_tf) ==
          $live[0].targets[$component].critical) and
        (($change.change.after | del(.ecs_target[0].task_definition_arn)) ==
         ($change.change.before | del(.ecs_target[0].task_definition_arn)))
      end
    ' "$plan_json" >/dev/null || die "$address はtask revision以外を変更します"
  done
}

validate_dispatcher_migration_plan() {
  local plan_json="$1" snapshot="$2" core="$3" migration="$4"
  local spec address component config_address task_address archive_address
  local static_environment_key expected_code
  for spec in \
    'aws_lambda_function.tiktok_dispatch[0]|tiktok|aws_lambda_function.tiktok_dispatch|aws_ecs_task_definition.tiktok_acquire[0]|data.archive_file.tiktok_dispatch[0]|tiktok_dispatch_static_environment' \
    'aws_lambda_function.x_dispatch[0]|x_buzz|aws_lambda_function.x_dispatch|aws_ecs_task_definition.x_buzz_worker[0]|data.archive_file.x_dispatch[0]|x_dispatch_static_environment'; do
    IFS='|' read -r address component config_address task_address \
      archive_address static_environment_key <<< "$spec"
    expected_code="$(jq -er --arg component "$component" \
      '.to.dispatcher_code_sha256[$component]' "$migration")"
    jq -L "$GUARD_JQ_DIR" -e \
      --arg address "$address" \
      --arg component "$component" \
      --arg config_address "$config_address" \
      --arg task_address "$task_address" \
      --arg archive_address "$archive_address" \
      --arg static_environment_key "$static_environment_key" \
      --arg expected_code "$expected_code" \
      --slurpfile live "$snapshot" \
      --slurpfile core "$core" '
      include "terraform_runtime_guard";
      def lambda_environment:
        if ((.environment // []) | length) == 0 then {}
        else (.environment[0].variables // {})
        end;
      def strip_provider_computed:
        del(.arn, .id, .invoke_arn, .qualified_arn, .qualified_invoke_arn,
            .last_modified, .source_code_size, .version, .signing_job_arn,
            .signing_profile_version_arn, .environment, .source_code_hash);
      . as $plan |
      ([$plan.resource_changes[] | select(.address == $address)] |
        if length == 1 then .[0] else error("dispatcher change missing") end
      ) as $change |
      ([$plan.configuration.root_module.resources[] |
        select(.address == $config_address)] |
        if length == 1 then .[0] else error("dispatcher config missing") end
      ) as $config |
      ([$config | .. | objects | .references? // empty | .[]] |
        index($task_address + ".arn")) as $task_reference |
      ([$config.expressions.source_code_hash.references[]?] | sort) as
        $source_hash_references |
      ([$config.expressions.filename.references[]?] | sort) as
        $filename_references |
      ([$change.change.after_unknown // {} | paths(. == true)]) as $unknown |
      ($change.change.before | lambda_environment) as $before_environment |
      ($change.change.after | lambda_environment) as $after_environment |
      ($change.change.after | guard_lambda_from_tf) as $after_lambda |
      ($live[0].dispatchers[$component].critical) as $live_lambda |
      ($live[0].dispatchers[$component].critical.environment) as $live_environment |
      ($core[0][$static_environment_key]) as $static_environment |
      ($live[0].taskdefs[$component].arn | sub(":[0-9]+$"; "")) as $task_prefix |
      (
        if $component == "tiktok" then
          "teamagent-dev-tiktok-acquire-dispatch"
        else "teamagent-dev-x-buzz-dispatch"
        end
      ) as $expected_function |
      $change.change.actions == ["update"] and
      (($change.change.before | guard_lambda_from_tf) == $live_lambda) and
      ($before_environment == $live_environment) and
      ($task_reference != null) and
      $source_hash_references ==
        [($archive_address + ".output_base64sha256")] and
      $filename_references == [($archive_address + ".output_path")] and
      (
        if $component == "tiktok" then
          (($change.change.before | strip_provider_computed |
            del(.reserved_concurrent_executions)) ==
           ($change.change.after | strip_provider_computed |
            del(.reserved_concurrent_executions))) and
          $change.change.after.reserved_concurrent_executions == 2
        else
          (($change.change.before | strip_provider_computed) ==
           ($change.change.after | strip_provider_computed))
        end
      ) and
      $change.change.after.source_code_hash == $expected_code and
      ($expected_code | test("^[A-Za-z0-9+/]{43}=$")) and
      $after_lambda.function_name == $expected_function and
      $after_lambda.function_arn ==
        ("arn:aws:lambda:ap-northeast-1:718959508629:function:" +
         $expected_function) and
      $after_lambda.role ==
        ("arn:aws:iam::718959508629:role/" + $expected_function) and
      $after_lambda.runtime == "python3.12" and
      $after_lambda.handler == "handler.handler" and
      $after_lambda.architectures == ["arm64"] and
      $after_lambda.timeout == 30 and
      $after_lambda.memory_size == 128 and
      $after_lambda.package_type == "Zip" and
      $after_lambda.kms_key_arn == "" and
      $after_lambda.vpc_config == null and
      $after_lambda.layers == [] and
      $after_lambda.file_system_configs == [] and
      $after_lambda.dead_letter_target_arn == "" and
      $after_lambda.publish == false and
      $after_lambda == (
        $live_lambda |
        .code_sha256 = $expected_code |
        .environment = $after_environment |
        if $component == "tiktok" then
          .reserved_concurrent_executions = 2
        else .
        end
      ) and
      ($unknown | all(. as $path |
        ($path == ["environment", 0, "variables", "TASKDEF_ARN"]) or
        ((["arn", "id", "invoke_arn", "qualified_arn", "qualified_invoke_arn",
           "last_modified", "source_code_size", "version", "signing_job_arn",
           "signing_profile_version_arn"] | index($path[0])) != null))) and
      (($after_environment | del(.TASKDEF_ARN)) == $static_environment) and
      (
        if ($unknown |
          any(. == ["environment", 0, "variables", "TASKDEF_ARN"]))
        then true
        else
          (($after_environment.TASKDEF_ARN | type) == "string") and
          ($after_environment.TASKDEF_ARN |
            startswith($task_prefix + ":")) and
          ($after_environment.TASKDEF_ARN |
            split(":")[-1] | test("^[0-9]+$"))
        end
      )
    ' "$plan_json" >/dev/null ||
      die "$address はmigration destination code hash/taskdef参照以外のdispatcher設定を変更します"
  done
}

validate_runtime_rule_staging() {
  local plan_json="$1" snapshot="$2"
  local spec address component
  for spec in \
    'aws_cloudwatch_event_rule.ingest_weekly[0]|ingest' \
    'aws_cloudwatch_event_rule.morning_digest_weekday[0]|morning' \
    'aws_cloudwatch_event_rule.canary_hourly[0]|canary'; do
    IFS='|' read -r address component <<< "$spec"
    jq -L "$GUARD_JQ_DIR" -e \
      --arg address "$address" --arg component "$component" \
      --slurpfile live "$snapshot" '
      include "terraform_runtime_guard";
      .resource_changes[] | select(.address == $address) as $change |
      $change.change.actions == ["no-op"] and
      $change.change.before == $change.change.after and
      (($change.change.before | guard_rule_from_tf) ==
        $live[0].rules[$component].critical)
    ' "$plan_json" >/dev/null ||
      die "runtime phase中のEventBridge先行enable/設定変更を拒否しました: $address"
  done
}

validate_canary_vpce_plan() {
  local plan_json="$1" snapshot="$2"
  jq -e --slurpfile live "$snapshot" '
    def resource($address):
      [.resource_changes[] | select(.address == $address)] |
      if length == 1 then .[0] else error("required resource missing") end;
    def https_rule($value):
      [($value.ingress // [])[] |
       select(.from_port == 443 and .to_port == 443 and .protocol == "tcp")] |
      if length == 1 then .[0] else error("unique HTTPS ingress missing") end;
    def without_https_sources:
      .ingress |= map(
        if .from_port == 443 and .to_port == 443 and .protocol == "tcp"
        then del(.security_groups)
        else .
        end
      );
    resource("aws_security_group.vpce[0]") as $vpce |
    ($live[0].targets.canary.critical.ecs_target
      .network_configuration.security_groups | sort) as $canary_groups |
    ($canary_groups | length) == 1 and
    ($canary_groups[0]) as $canary_group |
    (https_rule($vpce.change.before)) as $before_https |
    (https_rule($vpce.change.after)) as $after_https |
    (
      [
        .configuration.root_module.resources[] |
        select(.address == "aws_security_group.vpce") |
        .expressions.ingress[0].security_groups.references[]?
      ] | index("aws_security_group.canary[0].id")
    ) != null and
    (($after_https.security_groups // []) | index($canary_group)) != null and
    (
      if $vpce.change.actions == ["update"] then
        (($before_https.security_groups // []) | index($canary_group)) == null and
        (($after_https.security_groups // []) | sort) ==
          ((($before_https.security_groups // []) + [$canary_group]) |
           unique | sort) and
        ($vpce.change.before | without_https_sources) ==
          ($vpce.change.after | without_https_sources)
      elif $vpce.change.actions == ["no-op"] then
        $vpce.change.before == $vpce.change.after
      else false
      end
    )
  ' "$plan_json" >/dev/null ||
    die "VPCE security groupはlive canary SGの443追加以外を変更できません"
}

validate_external_hardening_plan() {
  local plan_json="$1"
  jq -e '
    def change($address):
      [.resource_changes[] | select(.address == $address)] |
      if length == 1 then .[0].change else error("resource change missing") end;
    def import_is($change; $id):
      (($change.importing // null) == {id: $id});
    def tags_are_managed($after):
      ({
        Environment: "dev",
        ManagedBy: "Terraform",
        Project: "TeamAgent",
        Version: "v3.0"
      }) as $managed |
      (($after.tags == null) or
       (($after.tags // {}) == {}) or
       (($after.tags // {}) == $managed)) and
      ($after.tags_all // {}) == $managed;
    (change("aws_apigatewayv2_api.connect_web")) as $api |
    (change("aws_apigatewayv2_stage.connect_web_default")) as $stage |
    (change("aws_cloudwatch_log_group.connect_http_api_access")) as $api_logs |
    $api.actions == ["update"] and
    import_is($api; "esk97z9grh") and
    $api.before.name == "teamagent-connectweb-api" and
    $api.before.protocol_type == "HTTP" and
    $api.before.disable_execute_api_endpoint == false and
    $api.after.disable_execute_api_endpoint == true and
    tags_are_managed($api.after) and
    (($api.before | del(
      .disable_execute_api_endpoint, .tags, .tags_all
    )) == ($api.after | del(
      .disable_execute_api_endpoint, .tags, .tags_all
    ))) and
    $stage.actions == ["update"] and
    import_is($stage; "esk97z9grh/$default") and
    $stage.before.api_id == "esk97z9grh" and
    $stage.before.name == "$default" and
    $stage.before.auto_deploy == true and
    (($stage.before.access_log_settings // []) | length) == 0 and
    (($stage.before.default_route_settings[0].detailed_metrics_enabled // false) == false) and
    (($stage.after.access_log_settings // []) | length) == 1 and
    $stage.after.access_log_settings[0].destination_arn ==
      "arn:aws:logs:ap-northeast-1:718959508629:log-group:/aws/apigateway/teamagent-dev-connect-web" and
    ($stage.after.access_log_settings[0].format | fromjson) == {
      requestId: "$context.requestId",
      routeKey: "$context.routeKey",
      status: "$context.status",
      responseLength: "$context.responseLength",
      integrationStatus: "$context.integration.status",
      integrationLatency: "$context.integrationLatency",
      responseType: "$context.error.responseType"
    } and
    (($stage.after.default_route_settings // []) | length) == 1 and
    $stage.after.default_route_settings[0].detailed_metrics_enabled == false and
    tags_are_managed($stage.after) and
    (($stage.before | del(
      .access_log_settings, .default_route_settings, .tags, .tags_all
    )) == ($stage.after | del(
      .access_log_settings, .default_route_settings, .tags, .tags_all
    ))) and
    $api_logs.actions == ["create"] and
    $api_logs.before == null and
    $api_logs.after.name ==
      "/aws/apigateway/teamagent-dev-connect-web" and
    $api_logs.after.retention_in_days == 30 and
    ($api_logs.after.kms_key_id // null) == null and
    tags_are_managed($api_logs.after)
  ' "$plan_json" >/dev/null ||
    die "API Gateway origin/access log hardening planがexact契約を満たしません"
}

validate_auto_created_log_retention_plan() {
  local plan_json="$1" state_contract="$2"
  jq -e --slurpfile ownership "$state_contract" '
    def change($address):
      [.resource_changes[] | select(.address == $address)] |
      if length == 1 then .[0].change else error("required log group change missing") end;
    def tags_are_managed($after):
      ({
        Environment: "dev",
        ManagedBy: "Terraform",
        Project: "TeamAgent",
        Version: "v3.0"
      }) as $managed |
      (($after.tags == null) or
       (($after.tags // {}) == {}) or
       (($after.tags // {}) == $managed)) and
      ($after.tags_all // {}) == $managed;
    def ownership($address):
      $ownership[0].imports[$address] //
        error("state ownership metadata missing");
    def retention_adoption($address; $name; $initial_retention):
      change($address) as $log |
      ownership($address) as $state |
      $state.expected_id == $name and
      (
        if $state.present then
          ($log.importing // null) == null
        else
          ($log.importing // null) == {id: $name}
        end
      ) and
      (
        if $log.before.retention_in_days == $initial_retention then
          $log.actions == ["update"]
        elif $log.before.retention_in_days == 30 then
          $log.actions == ["no-op"]
        else false
        end
      ) and
      $log.before.name == $name and
      $log.after.name == $name and
      $log.after.retention_in_days == 30 and
      (($log.before.kms_key_id // null) == ($log.after.kms_key_id // null)) and
      tags_are_managed($log.after) and
      (($log.before | del(.retention_in_days, .tags, .tags_all)) ==
       ($log.after | del(.retention_in_days, .tags, .tags_all)));
    . as $plan |
    [
      [
        "aws_cloudwatch_log_group.codebuild_aiia_image_builder",
        "/aws/codebuild/teamagent-dev-aiia-image-builder",
        0
      ],
      [
        "aws_cloudwatch_log_group.codebuild_image",
        "/aws/codebuild/teamagent-dev-image-builder",
        0
      ],
      [
        "aws_cloudwatch_log_group.ecs_containerinsights_teamagent",
        "/aws/ecs/containerinsights/teamagent-dev/performance",
        1
      ],
      [
        "aws_cloudwatch_log_group.ecs_containerinsights_tiktok",
        "/aws/ecs/containerinsights/teamagent-dev-tiktok/performance",
        1
      ],
      [
        "aws_cloudwatch_log_group.reminder_notify",
        "/aws/lambda/teamagent-dev-reminders-notify",
        0
      ],
      [
        "aws_cloudwatch_log_group.tiktok_dispatch",
        "/aws/lambda/teamagent-dev-tiktok-acquire-dispatch",
        0
      ],
      [
        "aws_cloudwatch_log_group.x_dispatch",
        "/aws/lambda/teamagent-dev-x-buzz-dispatch",
        0
      ]
    ] |
    all(. as $spec |
      $plan | retention_adoption($spec[0]; $spec[1]; $spec[2]))
  ' "$plan_json" >/dev/null ||
    die "auto-created operational log groupはexact import・30日・KMS不変のin-place更新だけを許可します"
}

validate_runtime_monitoring_plan() {
  local plan_json="$1"
  jq -e '
    def change($address):
      [.resource_changes[] | select(.address == $address)] |
      if length == 1 then .[0].change else error("monitor missing") end;
    def created($address):
      change($address) as $change |
      if $change.actions == ["create"] and $change.before == null
      then $change.after else error("monitor is not a pure create") end;
    def sns:
      "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-openclaw-alarms";
    def notified($alarm):
      $alarm.alarm_actions == [sns] and $alarm.ok_actions == [sns];
    (change("aws_ecs_cluster.main")) as $cluster |
    $cluster.actions == ["update"] and
    $cluster.before.name == "teamagent-dev" and
    $cluster.before.setting == [{
      name: "containerInsights", value: "disabled"
    }] and
    $cluster.after.setting == [{
      name: "containerInsights", value: "enabled"
    }] and
    (($cluster.before | del(.setting)) ==
      ($cluster.after | del(.setting))) and
    ([
      ["mcp", "teamagent-dev-mcp"],
      ["connect_web", "teamagent-dev-connect-web"],
      ["openclaw", "teamagent-dev-openclaw"]
    ] | all(. as $spec |
      created(
        "aws_cloudwatch_metric_alarm.ecs_running_tasks[\"" +
        $spec[0] + "\"]"
      ) as $alarm |
      $alarm.namespace == "ECS/ContainerInsights" and
      $alarm.metric_name == "RunningTaskCount" and
      $alarm.statistic == "Minimum" and
      $alarm.period == 60 and
      $alarm.evaluation_periods == 2 and
      $alarm.datapoints_to_alarm == 2 and
      $alarm.threshold == 1 and
      $alarm.comparison_operator == "LessThanThreshold" and
      $alarm.treat_missing_data == "breaching" and
      $alarm.dimensions == {
        ClusterName: "teamagent-dev", ServiceName: $spec[1]
      } and
      notified($alarm)
    )) and
    created("aws_cloudwatch_metric_alarm.connect_api_5xx") as $api |
    $api.namespace == "AWS/ApiGateway" and
    $api.metric_name == "5xx" and
    $api.statistic == "Sum" and
    $api.period == 300 and
    $api.threshold == 1 and
    $api.treat_missing_data == "notBreaching" and
    $api.dimensions == {ApiId: "esk97z9grh", Stage: "$default"} and
    notified($api) and
    ([
      ["reminders", "teamagent-dev-reminders-notify"],
      ["tiktok", "teamagent-dev-tiktok-acquire-dispatch"],
      ["x_buzz", "teamagent-dev-x-buzz-dispatch"]
    ] | all(. as $spec |
      ["lambda_errors", "lambda_throttles"] | all(. as $kind |
        created(
          "aws_cloudwatch_metric_alarm." + $kind +
          "[\"" + $spec[0] + "\"]"
        ) as $alarm |
        $alarm.namespace == "AWS/Lambda" and
        $alarm.metric_name ==
          (if $kind == "lambda_errors" then "Errors" else "Throttles" end) and
        $alarm.statistic == "Sum" and
        $alarm.period == 300 and
        $alarm.evaluation_periods == 1 and
        $alarm.threshold == 1 and
        $alarm.comparison_operator == "GreaterThanOrEqualToThreshold" and
        $alarm.treat_missing_data == "notBreaching" and
        $alarm.dimensions == {FunctionName: $spec[1]} and
        notified($alarm)
      )
    )) and
    created(
      "aws_cloudwatch_metric_alarm.tiktok_jobs_dlq_depth[0]"
    ) as $tiktok_dlq |
    $tiktok_dlq.namespace == "AWS/SQS" and
    $tiktok_dlq.metric_name == "ApproximateNumberOfMessagesVisible" and
    $tiktok_dlq.threshold == 1 and
    $tiktok_dlq.treat_missing_data == "notBreaching" and
    $tiktok_dlq.dimensions.QueueName ==
      "teamagent-dev-tiktok-acquire-dlq" and
    notified($tiktok_dlq) and
    (change("aws_cloudwatch_metric_alarm.x_jobs_dlq_depth[0]")) as $x_dlq |
    $x_dlq.actions == ["update"] and
    $x_dlq.after.namespace == "AWS/SQS" and
    $x_dlq.after.metric_name == "ApproximateNumberOfMessagesVisible" and
    $x_dlq.after.threshold == 1 and
    $x_dlq.after.treat_missing_data == "notBreaching" and
    $x_dlq.after.dimensions.QueueName == "teamagent-dev-x-buzz-dlq" and
    notified($x_dlq.after) and
    created(
      "aws_cloudwatch_metric_alarm.rds_database_connections_high"
    ) as $connections |
    $connections.namespace == "AWS/RDS" and
    $connections.metric_name == "DatabaseConnections" and
    $connections.statistic == "Maximum" and
    $connections.period == 300 and
    $connections.evaluation_periods == 3 and
    $connections.datapoints_to_alarm == 2 and
    $connections.threshold == 80 and
    $connections.comparison_operator == "GreaterThanOrEqualToThreshold" and
    $connections.dimensions.DBInstanceIdentifier == "teamagent-dev" and
    notified($connections) and
    created(
      "aws_cloudwatch_metric_alarm.rds_freeable_memory_low"
    ) as $memory |
    $memory.namespace == "AWS/RDS" and
    $memory.metric_name == "FreeableMemory" and
    $memory.statistic == "Minimum" and
    $memory.period == 300 and
    $memory.evaluation_periods == 3 and
    $memory.datapoints_to_alarm == 2 and
    $memory.threshold == 536870912 and
    $memory.comparison_operator == "LessThanThreshold" and
    $memory.dimensions.DBInstanceIdentifier == "teamagent-dev" and
    notified($memory) and
    created(
      "aws_cloudwatch_log_metric_filter.canary_heartbeat"
    ) as $heartbeat |
    $heartbeat.log_group_name == "/teamagent/dev/canary-health" and
    $heartbeat.pattern == "{ $.event = \"canary_health_result\" }" and
    ($heartbeat.metric_transformation | length) == 1 and
    $heartbeat.metric_transformation[0].name == "CanaryHeartbeat" and
    $heartbeat.metric_transformation[0].namespace == "teamagent/dev" and
    $heartbeat.metric_transformation[0].value == "1" and
    $heartbeat.metric_transformation[0].unit == "Count" and
    ($heartbeat.metric_transformation[0].default_value // "") == ""
  ' "$plan_json" >/dev/null ||
    die "production-path ECS/API/Lambda/DLQ/canary/RDS monitoring planがexact契約を満たしません"
}

validate_alarm_delivery_plan() {
  local plan_json="$1"
  local configured_email="" configured_email_hash=""
  if [ "$(jq -er '.variables.alarm_email_endpoints.value | length' "$plan_json")" = "1" ]; then
    configured_email="$(jq -er '
      .variables.alarm_email_endpoints.value[0]
    ' "$plan_json")"
    configured_email_hash="$(printf '%s' "$configured_email" | sha256_text)"
  fi
  jq -e \
    --arg configured_email "$configured_email" \
    --arg configured_email_hash "$configured_email_hash" \
    --arg expected_email "$EXPECTED_ALARM_EMAIL" \
    --arg expected_email_hash "$EXPECTED_ALARM_EMAIL_SHA256" \
    --arg destination_state_sha "$EXPECTED_ALARM_DESTINATION_STATE_SHA256" '
    def change($address):
      [.resource_changes[] | select(.address == $address)][0];
    def canonical:
      "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-openclaw-alarms";
    def legacy:
      "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-alarms";
    (.variables.alarm_email_endpoints.value // []) as $emails |
    (.variables.alarm_chatbot_configuration_arns.value // []) as $chat |
    .variables.runtime_guard_live.value.alarm_delivery as $live_delivery |
    change("aws_sns_topic.alarms") as $topic |
    .variables.require_alarm_delivery.value == true and
    ($emails | length) == 1 and
    ($chat | length) == 0 and
    $configured_email == $expected_email and
    $configured_email_hash == $expected_email_hash and
    $topic.change.after.name == "teamagent-dev-openclaw-alarms" and
    ($topic.change.actions | index("delete") | not) and
    ([.resource_changes[] |
      select(.type == "aws_sns_topic_subscription")] | length) == 0 and
    $live_delivery.confirmed_email_endpoint_sha256 ==
      [$expected_email_hash] and
    $live_delivery.subscription_inventory_count == 1 and
    $live_delivery.pending_subscription_count == 0 and
    $live_delivery.subscription_protocols == ["email"] and
    ($live_delivery.subscription_inventory_sha256 |
      test("^[0-9a-f]{64}$")) and
    ($live_delivery.confirmed_subscription_metadata_sha256 |
      test("^[0-9a-f]{64}$")) and
    $live_delivery.destination_state_sha256 ==
      $destination_state_sha and
    $live_delivery.attached_chatbot_configuration_arns == [] and
    ([.resource_changes[] |
      select(
        .type == "aws_cloudwatch_metric_alarm" or
        .type == "aws_cloudwatch_composite_alarm"
      ) |
      .change.after |
      (
        ((.alarm_actions // []) | length) > 0 and
        ((.alarm_actions // []) | all(. == canonical)) and
        ((.ok_actions // []) | all(. == canonical)) and
        ((.insufficient_data_actions // []) | all(. == canonical))
      )
    ] | all) and
    ([.resource_changes[] |
      select(
        .type == "aws_budgets_budget" or
        .type == "aws_ce_anomaly_subscription" or
        .type == "aws_sns_topic_policy"
      ) |
      [.change.after | .. | strings | select(. == legacy)] |
      length == 0
    ] | all)
  ' "$plan_json" >/dev/null ||
    die "alarm delivery planがcanonical topic/configured destination/legacy排除契約を満たしません"
}

validate_log_bucket_hardening_plan() {
  local plan_json="$1"
  jq -e \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" \
    --arg project "$PROJECT" \
    --arg environment "$ENVIRONMENT" '
    def resource($address):
      [.resource_changes[] | select(.address == $address)] |
      if length == 1 then .[0] else error("required log bucket resource missing") end;
    def converges($resource):
      ($resource.change.actions == ["create"] or
       $resource.change.actions == ["update"] or
       $resource.change.actions == ["no-op"]) and
      (($resource.change.actions | index("delete")) == null);
    def exact_no_op($resource):
      $resource.change.actions == ["no-op"] and
      $resource.change.before == $resource.change.after and
      ([($resource.change.after_unknown // {}) | .. |
        booleans | select(. == true)] | length) == 0;
    def array:
      if type == "array" then . else [.] end;
    def statement($document; $sid):
      [$document.Statement[] | select(.Sid == $sid)] |
      if length == 1 then .[0] else error("required policy statement missing") end;
    def tls_deny($document; $bucket_arn):
      statement($document; "DenyInsecureTransport") as $deny |
      $deny.Effect == "Deny" and
      $deny.Principal == "*" and
      ($deny.Action | array) == ["s3:*"] and
      ($deny.Resource | array | sort) ==
        ([$bucket_arn, ($bucket_arn + "/*")] | sort) and
      $deny.Condition == {
        Bool: {"aws:SecureTransport": "false"}
      };
    ("arn:aws:s3:::" + $project + "-" + $environment +
      "-cloudtrail-" + $account) as $cloudtrail_bucket |
    ("arn:aws:s3:::" + $project + "-" + $environment +
      "-bedrock-logs-" + $account) as $bedrock_bucket |
    ("arn:aws:cloudtrail:" + $region + ":" + $account + ":trail/" +
      $project + "-" + $environment + "-trail") as $trail_arn |
    resource("aws_s3_bucket_versioning.cloudtrail[0]") as $cloudtrail_versioning |
    resource("aws_s3_bucket_versioning.bedrock_logs[0]") as $bedrock_versioning |
    resource("aws_s3_bucket_policy.cloudtrail[0]") as $cloudtrail_policy |
    resource("aws_s3_bucket_policy.bedrock_logs[0]") as $bedrock_policy |
    resource("aws_s3_bucket_lifecycle_configuration.bedrock_logs[0]") as $lifecycle |
    resource("aws_kms_key.logs") as $kms |
    resource("aws_cloudtrail.main[0]") as $cloudtrail_producer |
    resource(
      "aws_bedrock_model_invocation_logging_configuration.main[0]"
    ) as $bedrock_producer |
    ($cloudtrail_policy.change.after.policy | fromjson) as $cloudtrail_document |
    ($bedrock_policy.change.after.policy | fromjson) as $bedrock_document |
    ($kms.change.after.policy | fromjson) as $kms_document |
    converges($cloudtrail_versioning) and
    $cloudtrail_versioning.change.after.bucket ==
      ($project + "-" + $environment + "-cloudtrail-" + $account) and
    ($cloudtrail_versioning.change.after.versioning_configuration | length) == 1 and
    $cloudtrail_versioning.change.after.versioning_configuration[0].status == "Enabled" and
    ($cloudtrail_versioning.change.after.versioning_configuration[0].mfa_delete //
      "Disabled") != "Enabled" and
    converges($bedrock_versioning) and
    $bedrock_versioning.change.after.bucket ==
      ($project + "-" + $environment + "-bedrock-logs-" + $account) and
    ($bedrock_versioning.change.after.versioning_configuration | length) == 1 and
    $bedrock_versioning.change.after.versioning_configuration[0].status == "Enabled" and
    ($bedrock_versioning.change.after.versioning_configuration[0].mfa_delete //
      "Disabled") != "Enabled" and
    converges($cloudtrail_policy) and
    ($cloudtrail_document.Statement | length) == 3 and
    ($cloudtrail_document.Statement | map(.Sid) | sort) ==
      (["AWSCloudTrailAclCheck", "AWSCloudTrailWrite", "DenyInsecureTransport"] | sort) and
    (
      statement($cloudtrail_document; "AWSCloudTrailAclCheck") as $acl |
      $acl.Effect == "Allow" and
      $acl.Principal == {Service: "cloudtrail.amazonaws.com"} and
      ($acl.Action | array) == ["s3:GetBucketAcl"] and
      ($acl.Resource | array) == [$cloudtrail_bucket] and
      $acl.Condition == {
        StringEquals: {"aws:SourceArn": $trail_arn}
      }
    ) and
    (
      statement($cloudtrail_document; "AWSCloudTrailWrite") as $write |
      $write.Effect == "Allow" and
      $write.Principal == {Service: "cloudtrail.amazonaws.com"} and
      ($write.Action | array) == ["s3:PutObject"] and
      ($write.Resource | array) ==
        [($cloudtrail_bucket + "/AWSLogs/" + $account + "/*")] and
      $write.Condition == {
        StringEquals: {
          "s3:x-amz-acl": "bucket-owner-full-control",
          "aws:SourceArn": $trail_arn
        }
      }
    ) and
    tls_deny($cloudtrail_document; $cloudtrail_bucket) and
    converges($bedrock_policy) and
    ($bedrock_document.Statement | length) == 4 and
    ($bedrock_document.Statement | map(.Sid) | sort) ==
      ([
        "AllowBedrockPut",
        "DenyInsecureTransport",
        "DenyManualBedrockPayloadDeletion",
        "DenyNonBedrockPayloadWriters"
      ] | sort) and
    (
      statement($bedrock_document; "AllowBedrockPut") as $write |
      $write.Effect == "Allow" and
      $write.Principal == {Service: "bedrock.amazonaws.com"} and
      ($write.Action | array) == ["s3:PutObject"] and
      ($write.Resource | array) ==
        [($bedrock_bucket + "/bedrock/AWSLogs/" + $account +
          "/BedrockModelInvocationLogs/*")] and
      $write.Condition == {
        StringEquals: {
          "aws:SourceAccount": $account
        },
        ArnLike: {
          "aws:SourceArn":
            ("arn:aws:bedrock:" + $region + ":" + $account + ":*")
        }
      }
    ) and
    (
      statement($bedrock_document; "DenyManualBedrockPayloadDeletion") as $deny |
      $deny.Effect == "Deny" and $deny.Principal == "*" and
      ($deny.Action | array | sort) ==
        (["s3:DeleteObject","s3:DeleteObjectVersion"] | sort) and
      ($deny.Resource | array) == [($bedrock_bucket + "/bedrock/*")]
    ) and
    (
      statement($bedrock_document; "DenyNonBedrockPayloadWriters") as $deny |
      $deny.Effect == "Deny" and $deny.Principal == "*" and
      ($deny.Action | array) == ["s3:PutObject"] and
      ($deny.Resource | array) == [($bedrock_bucket + "/bedrock/*")] and
      $deny.Condition == {
        StringNotEquals: {
          "aws:PrincipalServiceName": "bedrock.amazonaws.com"
        }
      }
    ) and
    tls_deny($bedrock_document; $bedrock_bucket) and
    converges($kms) and
    (
      statement($kms_document; "AllowBedrockLogs") as $bedrock_kms |
      $bedrock_kms.Effect == "Allow" and
      $bedrock_kms.Principal == {Service:"bedrock.amazonaws.com"} and
      ($bedrock_kms.Action | array) == ["kms:GenerateDataKey"] and
      ($bedrock_kms.Resource | array) == ["*"] and
      $bedrock_kms.Condition == {
        StringEquals: {
          "aws:SourceAccount": $account
        },
        ArnLike: {
          "aws:SourceArn":
            ("arn:aws:bedrock:" + $region + ":" + $account + ":*")
        }
      }
    ) and
    converges($lifecycle) and
    $lifecycle.change.after.bucket ==
      ($project + "-" + $environment + "-bedrock-logs-" + $account) and
    ($lifecycle.change.after.rule | length) == 2 and
    (
      [$lifecycle.change.after.rule[] |
       select(.id == "bedrock-current-and-noncurrent-minimum-60-days")] |
      if length != 1 then false else .[0] |
        .status == "Enabled" and
        (.filter | length) == 1 and .filter[0].prefix == "bedrock/" and
        (.expiration | length) == 1 and .expiration[0].days == 60 and
        (.expiration[0].expired_object_delete_marker // false) == false and
        (.noncurrent_version_expiration | length) == 1 and
        .noncurrent_version_expiration[0].noncurrent_days == 60 and
        (.noncurrent_version_expiration[0].newer_noncurrent_versions // null) == null
      end
    ) and
    (
      [$lifecycle.change.after.rule[] |
       select(.id == "bedrock-expired-delete-markers")] |
      if length != 1 then false else .[0] |
        .status == "Enabled" and
        (.filter | length) == 1 and .filter[0].prefix == "bedrock/" and
        (.expiration | length) == 1 and
        .expiration[0].expired_object_delete_marker == true and
        (.expiration[0].days // null) == null and
        ((.noncurrent_version_expiration // []) | length) == 0
      end
    ) and
    ([.resource_changes[] |
      select(.address | startswith(
        "aws_s3_bucket_lifecycle_configuration.cloudtrail"
      ))] | length) == 0 and
    exact_no_op($cloudtrail_producer) and
    $cloudtrail_producer.change.after.name ==
      ($project + "-" + $environment + "-trail") and
    $cloudtrail_producer.change.after.s3_bucket_name ==
      ($project + "-" + $environment + "-cloudtrail-" + $account) and
    $cloudtrail_producer.change.after.is_multi_region_trail == true and
    $cloudtrail_producer.change.after.include_global_service_events == true and
    $cloudtrail_producer.change.after.enable_log_file_validation == true and
    ($cloudtrail_producer.change.after.kms_key_id |
      test("^arn:aws:kms:" + $region + ":" + $account +
        ":key/[0-9a-fA-F-]{36}$")) and
    exact_no_op($bedrock_producer) and
    ($bedrock_producer.change.after.logging_config | length) == 1 and
    $bedrock_producer.change.after.logging_config[0] == {
      cloudwatch_config: [],
      embedding_data_delivery_enabled: true,
      image_data_delivery_enabled: false,
      s3_config: [{
        bucket_name:
          ($project + "-" + $environment + "-bedrock-logs-" + $account),
        key_prefix: "bedrock/"
      }],
      text_data_delivery_enabled: true,
      video_data_delivery_enabled: false
    } and
    (.variables.runtime_guard_live.value.versioning_pre_cutover_receipt_sha256 |
      test("^[0-9a-f]{64}$")) and
    (.variables.runtime_guard_live.value.log_cutover_contract_sha256 |
      test("^[0-9a-f]{64}$")) and
    .variables.bedrock_logs_retention_days.value == 60
  ' "$plan_json" >/dev/null ||
    die "CloudTrail/Bedrock log bucketがversioning/TLS/current・noncurrent各60日/KMSとproducer契約を満たしません"
}

validate_quarantine_builder_and_admin_noninterference_plan() {
  local plan_json="$1"
  jq -e '
    def resource($address):
      [.resource_changes[] | select(.address == $address)] |
      if length == 1 then .[0] else error("required quarantine builder resource missing") end;
    def converges($change):
      ($change.actions == ["create"] or
       $change.actions == ["update"] or
       $change.actions == ["no-op"]) and
      (($change.actions | index("delete")) == null);
    def array:
      if type == "array" then . else [.] end;
    resource("aws_codebuild_project.image") as $project |
    resource("aws_iam_role_policy.codebuild") as $builder_policy |
    ($builder_policy.change.after.policy | fromjson) as $builder_document |
    def statement($sid):
      [$builder_document.Statement[] | select(.Sid == $sid)] |
      if length == 1 then .[0] else error("required builder policy statement missing") end;
    def exact_actions($sid; $actions):
      (statement($sid).Action | array | sort) == ($actions | sort);
    def exact_resources($sid; $resources):
      (statement($sid).Resource | array | sort) == ($resources | sort);
    converges($project.change) and
    $project.change.after.name == "teamagent-dev-image-builder" and
    $project.change.after.description ==
      "Build and vulnerability-gate TeamAgent MCP candidate images inside AWS" and
    ($project.change.after.artifacts | length) == 1 and
    $project.change.after.artifacts[0].type == "NO_ARTIFACTS" and
    ($project.change.after.environment | length) == 1 and
    $project.change.after.environment[0].type == "ARM_CONTAINER" and
    $project.change.after.environment[0].image ==
      "aws/codebuild/amazonlinux-aarch64-standard:3.0" and
    $project.change.after.environment[0].privileged_mode == true and
    ($project.change.after.environment[0].environment_variable // []) == [] and
    ($project.change.after.source | length) == 1 and
    $project.change.after.source[0].type == "S3" and
    $project.change.after.source[0].location ==
      "teamagent-dev-raw-files/codebuild/source.zip" and
    ($project.change.after.source[0].buildspec |
      contains("release_evidence.py verify-source-declaration") and
      contains("source_provenance.py verify-source") and
      contains("teamagent-mcp-quarantine") and
      contains("teamagent-media-worker-quarantine") and
      contains("docker buildx build")) and
    converges($builder_policy.change) and
    $builder_policy.change.after.name == "teamagent-dev-codebuild-image" and
    statement("EcrMcpQuarantineWrite").Effect == "Allow" and
    exact_resources("EcrMcpQuarantineWrite"; [
      "arn:aws:ecr:ap-northeast-1:718959508629:repository/teamagent-mcp-quarantine",
      "arn:aws:ecr:ap-northeast-1:718959508629:repository/teamagent-media-worker-quarantine"
    ]) and
    exact_actions("EcrMcpQuarantineWrite"; [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:DescribeImages",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:DescribeImageScanFindings"
    ]) and
    statement("DenyMcpCandidateAndReleaseWrite").Effect == "Deny" and
    exact_resources("DenyMcpCandidateAndReleaseWrite"; [
      "arn:aws:ecr:ap-northeast-1:718959508629:repository/teamagent-mcp-verified-candidates",
      "arn:aws:ecr:ap-northeast-1:718959508629:repository/teamagent-mcp",
      "arn:aws:ecr:ap-northeast-1:718959508629:repository/teamagent-media-worker-verified-candidates",
      "arn:aws:ecr:ap-northeast-1:718959508629:repository/teamagent-media-worker"
    ]) and
    exact_actions("DenyMcpCandidateAndReleaseWrite"; [
      "ecr:BatchDeleteImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart"
    ]) and
    statement("DenyDynamicEnvironmentAndDebugChannels").Effect == "Deny" and
    exact_resources("DenyDynamicEnvironmentAndDebugChannels"; ["*"]) and
    exact_actions("DenyDynamicEnvironmentAndDebugChannels"; [
      "secretsmanager:GetSecretValue",
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssmmessages:*"
    ]) and
    statement("DenySourceEvidenceWritesAndSigning").Effect == "Deny" and
    exact_resources("DenySourceEvidenceWritesAndSigning"; ["*"]) and
    exact_actions("DenySourceEvidenceWritesAndSigning"; [
      "kms:Sign",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention"
    ]) and
    ([ $builder_document.Statement[] |
      select(.Effect == "Allow") |
      (.Action | array)[] |
      select(
        startswith("ecs:") or
        . == "iam:PassRole" or
        . == "kms:Sign" or
        startswith("lambda:")
      )
    ] | length) == 0 and
    ([.configuration.root_module.resources[]? |
      select(
        .type == "aws_iam_user_policy" or
        .type == "aws_iam_user_policy_attachment"
      )] | length) == 0 and
    ([.resource_changes[]? |
      select(
        .type == "aws_iam_user_policy" or
        .type == "aws_iam_user_policy_attachment" or
        (
          .type == "aws_iam_policy" and
          .change.after.name == "teamagent-dev-deny-direct-runtime-mutation"
        )
      )] | length) == 0
  ' "$plan_json" >/dev/null ||
    die "quarantine-only CodeBuild契約またはadministrator IAM非干渉契約を満たしません"
}

validate_exact_runtime_iam_plan() {
  local plan_json="$1"
  jq -e \
    --arg region "$REGION" \
    --arg account "$EXPECTED_ACCOUNT_ID" '
    def array:
      if type == "array" then . else [.] end;
    def resource($address):
      [.resource_changes[] | select(.address == $address)] |
      if length == 1 then .[0] else error("required IAM resource missing") end;
    def converges($change):
      ($change.actions == ["create"] or
       $change.actions == ["update"] or
       $change.actions == ["no-op"]) and
      (($change.actions | index("delete")) == null) and
      ($change.after.policy | type) == "string";
    def actions:
      (.Action // []) | array;
    def resources:
      (.Resource // []) | array;
    def allows_action_prefix($prefix):
      .Effect == "Allow" and
      ([actions[] | select(startswith($prefix))] | length) > 0;
    def exact_secret_arn:
      test(
        "^arn:aws:secretsmanager:" + $region + ":" + $account +
        ":secret:teamagent/dev/(db_password|database-url|mcp/bearer|" +
        "openclaw/slack-bot-token|openclaw/slack-app-token|" +
        "openclaw/gateway-token|oauth_state_secret|connect_google_secret|" +
        "connect_slack_client_id|connect_slack_secret|" +
        "slack_oauth_state_secret|google_oauth|vertex_sa|" +
        "hmac/mail-action|" +
        "hmac/report-link)-[A-Za-z0-9]{6}$"
      );
    def exact_openclaw_rollout_secret_arn:
      . == (
        "arn:aws:secretsmanager:" + $region + ":" + $account +
        ":secret:teamagent/dev/openclaw/rollout-canary-*"
      );
    def exact_kms_key_arn:
      test(
        "^arn:aws:kms:" + $region + ":" + $account +
        ":key/(mrk-)?[0-9a-f-]{32,64}$"
      );
    def approved_bedrock_arns:
      [
        "arn:aws:bedrock:ap-northeast-1:" + $account +
          ":inference-profile/jp.anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:ap-northeast-1:" + $account +
          ":inference-profile/jp.anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:ap-northeast-1::foundation-model/" +
          "anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:ap-northeast-3::foundation-model/" +
          "anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:ap-northeast-1::foundation-model/" +
          "anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:ap-northeast-3::foundation-model/" +
          "anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:ap-northeast-1::foundation-model/" +
          "cohere.rerank-v3-5:0"
      ];
    def exact_pass_service:
      (.Condition.StringEquals["iam:PassedToService"] // []) |
      array |
      length > 0 and
      all(. == "ecs-tasks.amazonaws.com" or . == "scheduler.amazonaws.com");
    . as $plan |
    (
      [
        "aws_iam_role_policy.worker_app",
        "aws_iam_role_policy.lambda_app",
        "aws_iam_role_policy.mcp_task",
        "aws_iam_role_policy.connect_web_task[0]",
        "aws_iam_role_policy.ingest_task[0]",
        "aws_iam_role_policy.morning_digest_task[0]"
      ] |
      all(. as $address | $plan | resource($address) | converges(.change))
    ) and
    (
      [$plan.resource_changes[] |
       select(
         .type == "aws_iam_role_policy" or
         .type == "aws_iam_policy"
       ) |
       select(.change.after != null)] |
      all((.change.after.policy | type) == "string")
    ) and
    (
      [$plan.resource_changes[] |
       select(
         (.type == "aws_iam_role_policy" or
          .type == "aws_iam_policy") and
         .change.after != null
       ) |
       (.change.after.policy | fromjson) |
      .Statement[]] |
      all(
        ((allows_action_prefix("secretsmanager:") | not) or
          (
            resources | length > 0 and
            all(
              . as $arn |
              ($arn | exact_secret_arn) or
              ($arn | exact_openclaw_rollout_secret_arn)
            )
          )) and
        ((allows_action_prefix("bedrock:") | not) or
          (approved_bedrock_arns as $approved |
           resources | length > 0 and all(. as $arn |
             ($approved | index($arn)) != null))) and
        ((allows_action_prefix("kms:") | not) or
          (resources | length > 0 and all(exact_kms_key_arn))) and
        ((allows_action_prefix("iam:PassRole") | not) or
          exact_pass_service)
      )
    )
  ' "$plan_json" >/dev/null ||
    die "runtime IAM planがexact secret/KMS/Bedrock/PassedToService契約を満たしません"
}

validate_activation_plan() {
  local plan_json="$1" snapshot="$2" migration="$3"
  local contract_mode="${4:-verify}" contract_output="${5:-}"
  validate_manifest_change_allowlist \
    "$plan_json" "$migration" "$contract_mode" "$contract_output"
  jq -L "$GUARD_JQ_DIR" -e --slurpfile live "$snapshot" '
    include "terraform_runtime_guard";
    def change($address):
      [.resource_changes[] | select(.address == $address)] |
      if length == 1 then .[0] else error("rule missing") end;
    (change("aws_cloudwatch_event_rule.morning_digest_weekday[0]")) as $morning |
    (change("aws_cloudwatch_metric_alarm.canary_heartbeat_missing[0]")) as $heartbeat |
    [
      ["aws_cloudwatch_event_rule.ingest_weekly[0]", "ingest"],
      ["aws_cloudwatch_event_rule.canary_hourly[0]", "canary"]
    ] | all(. as $spec |
      change($spec[0]) as $change |
      $change.change.actions == ["update"] and
      ($change.change.before | guard_rule_from_tf) ==
        $live[0].rules[$spec[1]].critical and
      $change.change.before.state == "DISABLED" and
      $change.change.after.state == "ENABLED" and
      (($change.change.before | del(.state)) ==
        ($change.change.after | del(.state)))
    ) and
    $morning.change.actions == ["no-op"] and
    $morning.change.before.state == "DISABLED" and
    $morning.change.before == $morning.change.after and
    $heartbeat.change.actions == ["create"] and
    $heartbeat.change.before == null and
    $heartbeat.change.after.namespace == "teamagent/dev" and
    $heartbeat.change.after.metric_name == "CanaryHeartbeat" and
    $heartbeat.change.after.statistic == "Sum" and
    $heartbeat.change.after.period == 3600 and
    $heartbeat.change.after.evaluation_periods == 2 and
    $heartbeat.change.after.datapoints_to_alarm == 2 and
    $heartbeat.change.after.threshold == 1 and
    $heartbeat.change.after.comparison_operator == "LessThanThreshold" and
    $heartbeat.change.after.treat_missing_data == "breaching" and
    $heartbeat.change.after.alarm_actions == [
      "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-openclaw-alarms"
    ]
  ' "$plan_json" >/dev/null ||
    die "activation phaseはingest/canaryのDISABLED→ENABLED以外を変更できません"
}

validate_runtime_migration_plan() {
  local plan_json="$1" snapshot="$2" core="$3" migration="$4" proposed_hmac="$5"
  local state_contract="$6"
  local contract_mode="${7:-verify}" contract_output="${8:-}"
  validate_manifest_change_allowlist \
    "$plan_json" "$migration" "$contract_mode" "$contract_output"
  validate_runtime_task_contracts "$plan_json" "$snapshot" "$core"
  validate_planned_hmac_consumers "$plan_json" "$proposed_hmac"
  validate_runtime_links "$plan_json" "$snapshot"
  validate_dispatcher_migration_plan \
    "$plan_json" "$snapshot" "$core" "$migration"
  validate_runtime_rule_staging "$plan_json" "$snapshot"
  validate_canary_vpce_plan "$plan_json" "$snapshot"
  validate_external_hardening_plan "$plan_json"
  validate_auto_created_log_retention_plan "$plan_json" "$state_contract"
  validate_alarm_delivery_plan "$plan_json"
  validate_log_bucket_hardening_plan "$plan_json"
  validate_runtime_monitoring_plan "$plan_json"
  validate_quarantine_builder_and_admin_noninterference_plan "$plan_json"
  validate_exact_runtime_iam_plan "$plan_json"

  jq -e '
    def changed($address):
      [.resource_changes[] | select(.address == $address)][0].change.after;
    changed("aws_sqs_queue.tiktok_jobs[0]") as $tiktok |
    changed("aws_sqs_queue.x_jobs[0]") as $x |
    changed("aws_lambda_event_source_mapping.tiktok_dispatch[0]") as $tiktok_mapping |
    $tiktok.visibility_timeout_seconds == 180 and
    $tiktok.message_retention_seconds == 1209600 and
    ($tiktok.redrive_policy | fromjson | .maxReceiveCount) == 5 and
    $x.message_retention_seconds == 1209600 and
    ($x.redrive_policy | fromjson | .maxReceiveCount) == 24 and
    $tiktok_mapping.batch_size == 1 and
    $tiktok_mapping.function_response_types == ["ReportBatchItemFailures"] and
    $tiktok_mapping.scaling_config == [{maximum_concurrency: 2}] and
    changed("aws_lambda_event_source_mapping.x_dispatch[0]").function_response_types ==
      ["ReportBatchItemFailures"]
  ' "$plan_json" >/dev/null ||
    die "dispatcher queue retention/redrive/partial-batch contractが不正です"
}

validate_plan() {
  local plan_json="$1"
  local snapshot="$2"
  local core="$3"
  local desired_image="$4"
  local migration="${5:-}"
  local proposed_hmac="${6:-}"
  local state_contract="${7:-}"
  local contract_mode="${8:-verify}"
  local contract_output="${9:-}"

  validate_common_plan_schema "$plan_json" "$core"
  validate_hmac_runtime_mutation_gates "$plan_json"
  if [ "$(jq -er '.mode' "$core")" = "migration" ]; then
    [ -n "$migration" ] && [ -n "$proposed_hmac" ] ||
      die "migration plan validator内部bindingが不足しています"
    case "$(jq -er '.kind' "$migration")" in
      runtime)
        validate_runtime_migration_plan \
          "$plan_json" "$snapshot" "$core" "$migration" "$proposed_hmac" \
          "$state_contract" "$contract_mode" "$contract_output"
        ;;
      activation)
        validate_activation_plan \
          "$plan_json" "$snapshot" "$migration" \
          "$contract_mode" "$contract_output"
        ;;
      *) die "未知のmigration kindです" ;;
    esac
    return 0
  fi

  jq -e --arg desired_image "$desired_image" --slurpfile expected_core "$core" '
    def pre_media_cutover_sync:
      $expected_core[0].mode == "sync" and
      $expected_core[0].live_tiktok_image ==
        $expected_core[0].desired_tiktok_image and
      ($expected_core[0].live_tiktok_image |
        test(
          "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-dev-tiktok-acquire@sha256:[0-9a-f]{64}$"
        ));
    type == "object" and
    .format_version == "1.2" and
    (.terraform_version | type == "string") and
    (.applyable | type == "boolean") and .errored == false and
    .complete == true and
    (.timestamp | type == "string") and
    (.planned_values | type == "object") and
    (.prior_state | type == "object") and
    (.configuration | type == "object") and
    (.variables | type == "object") and
    (.resource_changes | type == "array") and
    (.resource_drift | type == "array") and
    ((.deferred_changes // []) | type == "array") and
    ((.deferred_changes // []) | length == 0) and
    ((.action_invocations // []) | type == "array") and
    ((.action_invocations // []) | length == 0) and
    (.checks | type == "array") and
    ([.resource_changes[].address] | length == (unique | length)) and
    (.resource_changes | all(
      (.address | type == "string") and
      (.mode == "managed" or .mode == "data") and
      (.type | type == "string") and
      (.change | type == "object") and
      (if .mode == "data" then
         (.change.actions == ["no-op"] or .change.actions == ["read"])
       else
         (.change.actions == ["no-op"] or
          .change.actions == ["update"] or
          .change.actions == ["create", "delete"] or
          (
            .change.actions == ["create"] and
            (.address |
              test(
                "^terraform_data\\.hmac_live_task_gate\\[\\\"(mcp|connect_web|morning_digest)\\\"\\]$|^terraform_data\\.hmac_(mcp|connect_web|morning_digest)_(pre|post)_update\\[0\\]$"
              ))
          ))
       end)
    )) and
    (.resource_drift | all(
      (.address | type == "string") and
      (.change | type == "object") and
      ((.change.actions == ["no-op"]) or (.change.actions == ["update"]))
    )) and
    (.checks | all(
      .status == "pass" and ((.instances // []) | all(.status == "pass"))
    )) and
    .variables.mcp_image.value == $desired_image and
    .variables.media_worker_image.value ==
      (if pre_media_cutover_sync
       then ""
       else $expected_core[0].desired_tiktok_image
       end) and
    .variables.tiktok_acquire_image.value == "" and
    .variables.enable_media_worker.value == true and
    .variables.enable_tiktok_acquire.value == true and
    .variables.require_alarm_delivery.value == true and
    .variables.bedrock_logs_retention_days.value == 60 and
    (.variables.image_deployment_intent_id.value |
      test("^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")) and
    .variables.runtime_guard_live.value == $expected_core[0]
  ' "$plan_json" >/dev/null ||
    die "plan JSONのschema/action/check/runtime_guard束縛が不正です"

  local allowed_replacements
  allowed_replacements='["terraform_data.production_image_release_gate","terraform_data.hmac_live_task_gate[\"mcp\"]","terraform_data.hmac_live_task_gate[\"connect_web\"]","terraform_data.hmac_live_task_gate[\"morning_digest\"]","terraform_data.hmac_mcp_pre_update[0]","terraform_data.hmac_mcp_post_update[0]","terraform_data.hmac_connect_web_pre_update[0]","terraform_data.hmac_connect_web_post_update[0]","terraform_data.hmac_morning_digest_pre_update[0]","terraform_data.hmac_morning_digest_post_update[0]","aws_ecs_task_definition.openclaw[0]","aws_ecs_task_definition.mcp","aws_ecs_task_definition.connect_web[0]","aws_ecs_task_definition.ingest[0]","aws_ecs_task_definition.morning_digest[0]","aws_ecs_task_definition.canary[0]","aws_ecs_task_definition.tiktok_acquire[0]","aws_ecs_task_definition.x_buzz_worker[0]"]'
  local destructive
  destructive="$(jq -r --argjson allowed "$allowed_replacements" '
    .resource_changes[]? |
    .address as $address |
    (.change.actions // []) as $actions |
    select($actions | index("delete")) |
    select(($actions != ["create", "delete"]) or (($allowed | index($address)) == null)) |
    "\($actions | join("/")) \($address)"
  ' "$plan_json")"
  [ -z "$destructive" ] || die "非許可の destroy/replace を検出しました（plan は破棄）:\n$destructive"

  local allowed_runtime_changes unexpected_changes
    allowed_runtime_changes='[
      "terraform_data.production_image_release_gate",
      "terraform_data.hmac_live_task_gate[\"mcp\"]",
      "terraform_data.hmac_live_task_gate[\"connect_web\"]",
      "terraform_data.hmac_live_task_gate[\"morning_digest\"]",
      "terraform_data.hmac_mcp_pre_update[0]",
      "terraform_data.hmac_mcp_post_update[0]",
      "terraform_data.hmac_connect_web_pre_update[0]",
      "terraform_data.hmac_connect_web_post_update[0]",
      "terraform_data.hmac_morning_digest_pre_update[0]",
      "terraform_data.hmac_morning_digest_post_update[0]",
      "aws_ecs_task_definition.openclaw[0]",
      "aws_ecs_task_definition.mcp",
      "aws_ecs_task_definition.connect_web[0]",
      "aws_ecs_task_definition.ingest[0]",
      "aws_ecs_task_definition.morning_digest[0]",
      "aws_ecs_task_definition.canary[0]",
      "aws_ecs_service.mcp[0]",
      "aws_ecs_service.connect_web[0]",
      "aws_ecs_service.openclaw[0]",
      "aws_cloudwatch_event_target.ingest_run_task[0]",
      "aws_cloudwatch_event_target.morning_digest_run_task[0]",
      "aws_cloudwatch_event_target.canary_run_task[0]",
      "aws_cloudwatch_event_rule.ingest_weekly[0]",
      "aws_cloudwatch_event_rule.morning_digest_weekday[0]",
      "aws_cloudwatch_event_rule.canary_hourly[0]",
      "aws_ecs_task_definition.tiktok_acquire[0]",
      "aws_ecs_task_definition.x_buzz_worker[0]",
      "aws_lambda_function.tiktok_dispatch[0]",
      "aws_lambda_function.x_dispatch[0]",
      "aws_lambda_event_source_mapping.tiktok_dispatch[0]",
      "aws_lambda_event_source_mapping.x_dispatch[0]"
    ]'
    unexpected_changes="$(jq -r --argjson allowed "$allowed_runtime_changes" '
      .resource_changes[]? |
      select(.mode == "managed") |
      select(.change.actions != ["no-op"]) |
      .address as $address |
      select(($allowed | index($address)) == null) |
      "\(.change.actions | join("/")) \(.address)"
    ' "$plan_json")"
  [ -z "$unexpected_changes" ] ||
    die "runtime planに許可外の変更を検出しました（planは破棄）:\n$unexpected_changes"

  local unexpected_drift
  unexpected_drift="$(jq -r --argjson allowed "$allowed_runtime_changes" '
    .resource_drift[]? |
    .address as $address |
    select(($allowed | index($address)) == null) |
    "\(.change.actions | join("/")) \(.address)"
  ' "$plan_json")"
  [ -z "$unexpected_drift" ] ||
    die "runtime planに許可外resourceのdriftを検出しました（planは破棄）:\n$unexpected_drift"

  jq -e --argjson allowed "$allowed_runtime_changes" '
    .resource_changes | all(
      .address as $address |
      if .mode == "data" then
        (.change.actions == ["no-op"] or .change.actions == ["read"])
      elif .change.actions == ["no-op"] then
        (.change.before == .change.after) and
        ([.change.after_unknown // {} | paths(. == true)] | length == 0)
      else
        ($allowed | index($address)) != null
      end
    )
  ' "$plan_json" >/dev/null ||
    die "no-op resourceの値/unknownまたはresource actionが不正です"

  local spec address component expected_name consumer_id expected_image
  for spec in \
    'aws_ecs_task_definition.openclaw[0]|openclaw|openclaw|openclaw' \
    'aws_ecs_task_definition.mcp|mcp|teamagent-mcp|mcp' \
    'aws_ecs_task_definition.connect_web[0]|connect_web|connect-web|connect_web' \
    'aws_ecs_task_definition.ingest[0]|ingest|ingest|ingest' \
    'aws_ecs_task_definition.morning_digest[0]|morning|morning-digest|morning_digest' \
    'aws_ecs_task_definition.canary[0]|canary|canary|canary' \
    'aws_ecs_task_definition.tiktok_acquire[0]|tiktok|acquire|tiktok_acquire' \
    'aws_ecs_task_definition.x_buzz_worker[0]|x_buzz|worker|x_buzz_worker'; do
    IFS='|' read -r address component expected_name consumer_id <<< "$spec"
    expected_image="$(
      jq -er --arg consumer_id "$consumer_id" \
        '.desired_consumer_images[$consumer_id]' "$core"
    )" || die "内部error: consumer別期待imageがありません: $consumer_id"
    if plan_has_address "$plan_json" "$address"; then
      jq -e --arg address "$address" --arg expected_image "$expected_image" \
        --arg expected_name "$expected_name" '
        .resource_changes[] | select(.address == $address) as $change |
        ($change.change.after.container_definitions | fromjson) as $containers |
        ([$containers[] | select(.name == $expected_name)]) as $expected |
        (($change.change.actions == ["no-op"]) or
         ($change.change.actions == ["create", "delete"])) and
        $change.change.after.skip_destroy == true and
        ($containers | length) == 1 and ($expected | length) == 1 and
        $expected[0].image == $expected_image and
        (([$change.change.after_unknown // {} | paths(. == true)] -
          [["arn"], ["arn_without_revision"], ["enable_fault_injection"],
           ["id"], ["revision"], ["volume", 0, "configure_at_launch"]]) | length == 0)
      ' "$plan_json" >/dev/null ||
        die "$address は期待container ${expected_name}・候補image・unknown allowlistを満たしません"

      local parity_diff
      parity_diff="$(jq -r --arg address "$address" --arg component "$component" \
        --arg expected_name "$expected_name" --slurpfile live "$snapshot" '
        def envmap: map({key: .name, value: .value}) | from_entries;
        def secmap: map({key: .name, value: .valueFrom}) | from_entries;
        ($live[0].taskdefs[$component].env) as $live_env |
        ($live[0].taskdefs[$component].secrets) as $live_secrets |
        (.resource_changes[] | select(.address == $address) |
          .change.after.container_definitions | fromjson |
          [.[] | select(.name == $expected_name)][0]) as $planned |
        ($planned.environment // [] | envmap) as $planned_env |
        ($planned.secrets // [] | secmap) as $planned_secrets |
        if (($planned.environment // [] | length) == ($planned_env | length)) and
           (($planned.secrets // [] | length) == ($planned_secrets | length)) and
           ($planned_env == $live_env) and ($planned_secrets == $live_secrets)
        then empty
        else
          (["env", (($live_env | keys_unsorted) + ($planned_env | keys_unsorted) | unique)] |
            .[1][] as $key | select($live_env[$key] != $planned_env[$key]) | .[0] + ":" + $key),
          (["secret", (($live_secrets | keys_unsorted) + ($planned_secrets | keys_unsorted) | unique)] |
            .[1][] as $key | select($live_secrets[$key] != $planned_secrets[$key]) | .[0] + ":" + $key),
          (if (($planned.environment // [] | length) != ($planned_env | length)) then "env:<duplicate-name>" else empty end),
          (if (($planned.secrets // [] | length) != ($planned_secrets | length)) then "secret:<duplicate-name>" else empty end)
        end
      ' "$plan_json")"
      [ -z "$parity_diff" ] ||
        die "$address のenv/secretsがliveと完全一致しません（追加・変更・削除を禁止）:\n$parity_diff"

      jq -L "$GUARD_JQ_DIR" -e \
        --arg address "$address" --arg component "$component" --slurpfile live "$snapshot" '
        include "terraform_runtime_guard";
        (.resource_changes[] | select(.address == $address) | .change.after |
          guard_task_from_tf) == $live[0].taskdefs[$component].critical
      ' "$plan_json" >/dev/null ||
        die "$address のrole/cpu/memory/runtime/container/port/health/log/volume等がliveから変化します"
    else
      die "runtime planに必須addressがありません: $address"
    fi
  done

  for spec in \
    'aws_ecs_service.mcp[0]|mcp|aws_ecs_service.mcp|aws_ecs_task_definition.mcp' \
    'aws_ecs_service.connect_web[0]|connect_web|aws_ecs_service.connect_web|aws_ecs_task_definition.connect_web[0]' \
    'aws_ecs_service.openclaw[0]|openclaw|aws_ecs_service.openclaw|aws_ecs_task_definition.openclaw[0]'; do
    local config_address task_address
    IFS='|' read -r address component config_address task_address <<< "$spec"
    if plan_has_address "$plan_json" "$address"; then
      jq -L "$GUARD_JQ_DIR" -e \
        --arg address "$address" --arg component "$component" \
        --arg config_address "$config_address" --arg task_address "$task_address" \
        --slurpfile live "$snapshot" '
        include "terraform_runtime_guard";
        . as $plan |
        $plan.resource_changes[] | select(.address == $address) as $change |
        ([$plan.configuration.root_module.resources[] |
          select(.address == $config_address) |
          .expressions.task_definition.references[]?] | index($task_address + ".arn")) as $reference |
        (($change.change.actions == ["no-op"] and
          ([$change.change.after_unknown // {} | paths(. == true)] | length == 0)) or
         ($change.change.actions == ["update"] and
          [$change.change.after_unknown // {} | paths(. == true)] == [["task_definition"]])) and
        $reference != null and
        ($change.change.before.task_definition == $live[0].services[$component].task_definition) and
        (
          $component == "openclaw" or
          $change.change.after.wait_for_steady_state == true
        ) and
        (($change.change.before | guard_service_from_tf |
          del(.wait_for_steady_state)) ==
          ($live[0].services[$component].critical |
            del(.wait_for_steady_state))) and
        (($change.change.before | del(.task_definition)) ==
          ($change.change.after | del(.task_definition)))
      ' "$plan_json" >/dev/null ||
        die "$address はliveからtask_definition参照以外も変更します"
    else
      die "runtime planに必須addressがありません: $address"
    fi
  done

  # ACTIVATION-SHIM(ingest): 一時対応。Activation 完了後に canonical registry と
  # release_evidence を原子的に正名化して撤去する。docs/activation/ACTIVATION_STATE.md 参照。
  for spec in \
    'aws_cloudwatch_event_target.ingest_run_task[0]|ingest|aws_cloudwatch_event_target.ingest_run_task|aws_lambda_function.ingest_dispatch[0]' \
    'aws_cloudwatch_event_target.morning_digest_run_task[0]|morning|aws_cloudwatch_event_target.morning_digest_run_task|aws_ecs_task_definition.morning_digest[0]' \
    'aws_cloudwatch_event_target.canary_run_task[0]|canary|aws_cloudwatch_event_target.canary_run_task|aws_ecs_task_definition.canary[0]'; do
    IFS='|' read -r address component config_address task_address <<< "$spec"
    if plan_has_address "$plan_json" "$address"; then
      jq -L "$GUARD_JQ_DIR" -e \
        --arg address "$address" --arg component "$component" \
        --arg config_address "$config_address" --arg task_address "$task_address" \
        --slurpfile live "$snapshot" '
        include "terraform_runtime_guard";
        . as $plan |
        $plan.resource_changes[] | select(.address == $address) as $change |
        ([$plan.configuration.root_module.resources[] |
          select(.address == $config_address) |
          if $component == "ingest" then
            .expressions.arn.references[]?
          else
            .expressions.ecs_target[0].task_definition_arn.references[]?
          end] |
          index($task_address + ".arn")) as $reference |
        if $component == "ingest" then
          $change.change.actions == ["no-op"] and
          ([$change.change.after_unknown // {} | paths(. == true)] |
            length == 0) and
          $reference != null and
          (($change.change.before | guard_target_from_tf) ==
            $live[0].targets.ingest.critical) and
          $change.change.before == $change.change.after
        else
          (($change.change.actions == ["no-op"] and
            ([$change.change.after_unknown // {} | paths(. == true)] |
              length == 0)) or
           ($change.change.actions == ["update"] and
            [$change.change.after_unknown // {} | paths(. == true)] ==
              [["ecs_target", 0, "task_definition_arn"]])) and
          $reference != null and
          ($change.change.before.ecs_target[0].task_definition_arn ==
            $live[0].targets[$component].task_definition) and
          (($change.change.before | guard_target_from_tf) ==
            $live[0].targets[$component].critical) and
          (($change.change.before | del(.ecs_target[0].task_definition_arn)) ==
            ($change.change.after | del(.ecs_target[0].task_definition_arn)))
        end
      ' "$plan_json" >/dev/null ||
        die "$address はliveからtask_definition参照以外も変更します"
    else
      die "runtime planに必須addressがありません: $address"
    fi
  done

  for spec in \
    'aws_cloudwatch_event_rule.ingest_weekly[0]:ingest' \
    'aws_cloudwatch_event_rule.morning_digest_weekday[0]:morning' \
    'aws_cloudwatch_event_rule.canary_hourly[0]:canary'; do
    address="${spec%%:*}"
    component="${spec#*:}"
    if plan_has_address "$plan_json" "$address"; then
      jq -L "$GUARD_JQ_DIR" -e \
        --arg address "$address" --arg component "$component" --slurpfile live "$snapshot" '
        include "terraform_runtime_guard";
        .resource_changes[] | select(.address == $address) as $change |
        ($change.change.actions == ["no-op"]) and
        (($change.change.before | guard_rule_from_tf) ==
          $live[0].rules[$component].critical) and
        ($change.change.before == $change.change.after) and
        ([$change.change.after_unknown // {} | paths(. == true)] | length == 0)
      ' "$plan_json" >/dev/null ||
        die "$address のstate/schedule/description等を変更するruntime planは禁止です"
    else
      die "runtime planに必須addressがありません: $address"
    fi
  done

  # Dispatcherはtask definition revisionだけを追従してよい。static env・ZIP
  # code hash・role/runtime等はliveと完全一致させ、参照元も所定taskdefに固定する。
  for spec in \
    'aws_lambda_function.tiktok_dispatch[0]|tiktok|aws_lambda_function.tiktok_dispatch|aws_ecs_task_definition.tiktok_acquire[0]|data.archive_file.tiktok_dispatch[0]|tiktok_dispatch_static_environment' \
    'aws_lambda_function.x_dispatch[0]|x_buzz|aws_lambda_function.x_dispatch|aws_ecs_task_definition.x_buzz_worker[0]|data.archive_file.x_dispatch[0]|x_dispatch_static_environment'; do
    local config_address task_address archive_address static_environment_key
    IFS='|' read -r address component config_address task_address archive_address static_environment_key <<< "$spec"
    if plan_has_address "$plan_json" "$address"; then
      jq -L "$GUARD_JQ_DIR" -e \
        --arg address "$address" --arg component "$component" \
        --arg config_address "$config_address" --arg task_address "$task_address" \
        --arg archive_address "$archive_address" \
        --arg static_environment_key "$static_environment_key" \
        --slurpfile live "$snapshot" --slurpfile core "$core" '
        include "terraform_runtime_guard";
        def lambda_environment:
          if ((.environment // []) | length) == 0 then {}
          else (.environment[0].variables // {})
          end;
        def strip_provider_computed:
          del(.arn, .id, .invoke_arn, .qualified_arn, .qualified_invoke_arn,
              .last_modified, .source_code_size, .version, .signing_job_arn,
              .signing_profile_version_arn, .environment, .source_code_hash);
        . as $plan |
        $plan.resource_changes[] | select(.address == $address) as $change |
        ([$plan.configuration.root_module.resources[] |
          select(.address == $config_address) |
          .. | objects | .references? // empty | .[]] |
          index($task_address + ".arn")) as $reference |
        ([$plan.configuration.root_module.resources[] |
          select(.address == $config_address) |
          .expressions.source_code_hash.references[]?] |
          sort) as $source_hash_references |
        ([$plan.configuration.root_module.resources[] |
          select(.address == $config_address) |
          .expressions.filename.references[]?] |
          sort) as $filename_references |
        ([$change.change.after_unknown // {} | paths(. == true)]) as $unknown |
        ($change.change.before | lambda_environment) as $before_environment |
        ($change.change.after | lambda_environment) as $after_environment |
        ($live[0].dispatchers[$component].critical.environment) as $live_environment |
        ($core[0][$static_environment_key]) as $static_environment |
        ($live[0].taskdefs[$component].arn | sub(":[0-9]+$"; "")) as $task_prefix |
        (
          if $component == "tiktok" then
            "teamagent-dev-tiktok-acquire-dispatch"
          else "teamagent-dev-x-buzz-dispatch"
          end
        ) as $expected_function |
        ($change.change.after | guard_lambda_from_tf) as $after_lambda |
        (($change.change.before | guard_lambda_from_tf) ==
          $live[0].dispatchers[$component].critical) and
        ($before_environment == $live_environment) and
        ($reference != null) and
        $source_hash_references == [
          ($archive_address + ".output_base64sha256")
        ] and
        $filename_references == [
          ($archive_address + ".output_path")
        ] and
        $after_lambda.function_name == $expected_function and
        $after_lambda.function_arn ==
          ("arn:aws:lambda:ap-northeast-1:718959508629:function:" +
           $expected_function) and
        $after_lambda.role ==
          ("arn:aws:iam::718959508629:role/" + $expected_function) and
        $after_lambda.runtime == "python3.12" and
        $after_lambda.handler == "handler.handler" and
        $after_lambda.architectures == ["arm64"] and
        $after_lambda.timeout == 30 and
        $after_lambda.memory_size == 128 and
        $after_lambda.package_type == "Zip" and
        $after_lambda.kms_key_arn == "" and
        $after_lambda.vpc_config == null and
        $after_lambda.layers == [] and
        $after_lambda.file_system_configs == [] and
        $after_lambda.dead_letter_target_arn == "" and
        $after_lambda.publish == false and
        ($after_lambda.code_sha256 | test("^[A-Za-z0-9+/]{43}=$")) and
        if $change.change.actions == ["no-op"] then
          ($change.change.before == $change.change.after) and ($unknown | length == 0)
        elif $change.change.actions == ["update"] then
          (($change.change.before | strip_provider_computed) ==
            ($change.change.after | strip_provider_computed)) and
          ($change.change.after.source_code_hash |
            test("^[A-Za-z0-9+/]{43}=$")) and
          (
            ($change.change.after | guard_lambda_from_tf) as $after |
            $after == (
              $live[0].dispatchers[$component].critical |
              .code_sha256 = $after.code_sha256 |
              .environment = $after.environment
            )
          ) and
          ($unknown | all(. as $path |
            ($path == ["environment", 0, "variables", "TASKDEF_ARN"]) or
            ((["arn", "id", "invoke_arn", "qualified_arn", "qualified_invoke_arn",
               "last_modified", "source_code_size", "version", "signing_job_arn",
               "signing_profile_version_arn"] | index($path[0])) != null))) and
          (($after_environment | del(.TASKDEF_ARN)) == $static_environment) and
          (if ($unknown | any(. == ["environment", 0, "variables", "TASKDEF_ARN"]))
           then true
           else (($after_environment.TASKDEF_ARN | type) == "string") and
                ($after_environment.TASKDEF_ARN | startswith($task_prefix + ":")) and
                ($after_environment.TASKDEF_ARN | split(":")[-1] | test("^[0-9]+$"))
           end)
        else false
        end
      ' "$plan_json" >/dev/null ||
        die "$address は所定taskdef参照以外のdispatcher設定を変更します"
    else
      die "runtime planに必須addressがありません: $address"
    fi
  done

  # SQS event source mappingはruntime rolloutで変更不要。queue/function/有効状態、
  # batch/retry/filter/concurrency/tagsを含む全設定をliveから不変にする。
  for spec in \
    'aws_lambda_event_source_mapping.tiktok_dispatch[0]:tiktok' \
    'aws_lambda_event_source_mapping.x_dispatch[0]:x_buzz'; do
    address="${spec%%:*}"
    component="${spec#*:}"
    if plan_has_address "$plan_json" "$address"; then
      jq -L "$GUARD_JQ_DIR" -e \
        --arg address "$address" --arg component "$component" --slurpfile live "$snapshot" '
        include "terraform_runtime_guard";
        .resource_changes[] | select(.address == $address) as $change |
        ($change.change.actions == ["no-op"]) and
        ($change.change.before == $change.change.after) and
        ([$change.change.after_unknown // {} | paths(. == true)] | length == 0) and
        (($change.change.before | guard_mapping_from_tf) ==
          $live[0].event_mappings[$component].critical)
      ' "$plan_json" >/dev/null ||
        die "$address のqueue/function/enabled/batch/retry/filter等を変更するruntime planは禁止です"
    else
      die "runtime planに必須addressがありません: $address"
    fi
  done

  # Strict sync is not a grandfathering path for legacy wildcard policies.
  # Every no-op IAM policy is re-parsed, so a broad secret/model/key grant
  # blocks all future runtime plans until the reviewed migration removes it.
  validate_exact_runtime_iam_plan "$plan_json"
}

wait_task_and_record() {
  local cluster="$1" task_arn="$2" expected_image="$3" output="$4"
  local describe="$TMP_ROOT/task-${RANDOM}.json"
  local attempt=0
  while [ "$attempt" -lt 120 ]; do
    aws_cli ecs describe-tasks --cluster "$cluster" --tasks "$task_arn" \
      --output json > "$describe"
    if [ "$(jq -r '.tasks[0].lastStatus // ""' "$describe")" = "STOPPED" ]; then
      break
    fi
    attempt=$((attempt + 1))
    sleep 5
  done
  [ "$attempt" -lt 120 ] || die "preflight Fargate taskが10分以内に停止しませんでした"
  jq -e --arg image "$expected_image" '
    (.failures | length) == 0 and
    (.tasks | length) == 1 and
    .tasks[0].stopCode == "EssentialContainerExited" and
    (.tasks[0].containers | length) == 1 and
    .tasks[0].containers[0].exitCode == 0 and
    .tasks[0].containers[0].image == $image and
    .tasks[0].containers[0].imageDigest == ($image | split("@")[1])
  ' "$describe" >/dev/null ||
    die "preflight taskのexit/image digest契約が不一致です"
  jq -S -c '{
    task_arn: .tasks[0].taskArn,
    task_definition_arn: .tasks[0].taskDefinitionArn,
    image: .tasks[0].containers[0].image,
    image_digest: .tasks[0].containers[0].imageDigest,
    exit_code: .tasks[0].containers[0].exitCode,
    stopped_reason_code: .tasks[0].stopCode,
    log_stream_name: (.tasks[0].containers[0].logStreamName // "")
  }' "$describe" > "$output"
}

run_registered_preflight_task() {
  local profile="$1" image="$2" snapshot="$3" output="$4"
  local cluster="${PROJECT}-${ENVIRONMENT}"
  local family="${PROJECT}-${ENVIRONMENT}-runtime-preflight-${profile}"
  local task_role execution_role subnets security_groups expected_user script
  local volume_json environment_json entry_point_json command_json

  case "$profile" in
    main)
      task_role="$(jq -er '.taskdefs.mcp.critical.task_role_arn' "$snapshot")"
      execution_role="$(jq -er '.taskdefs.mcp.critical.execution_role_arn' "$snapshot")"
      subnets="$(jq -c '.services.mcp.critical.network_configuration.subnets' "$snapshot")"
      security_groups="$(jq -c '.services.mcp.critical.network_configuration.security_groups' "$snapshot")"
      expected_user="10001:10001"
      script='
        set -eu
        test "$(id -u)" = 10001
        test "$(id -g)" = 10001
        test "$(awk "/^CapEff:/{print \\$2}" /proc/self/status)" = 0000000000000000
        test "$(stat -c %a /tmp)" = 1777
        for path in \
          /tmp/home /tmp/.cache /tmp/.pycache /tmp/.uv-cache \
          /tmp/.npm /tmp/.cache/puppeteer
        do
          mkdir -p "$path"
          printf writable > "$path/.teamagent-write-probe"
        done
        printf ok > /tmp/teamagent-preflight
        python -c "import sys; assert sys.version_info[:2] == (3, 14)"
        command -v npx
        command -v yt-dlp
        command -v chromium
        npx --version >/dev/null
        yt-dlp --version >/dev/null
        chromium --headless --no-sandbox --disable-gpu \
          --dump-dom "data:text/html,<title>teamagent-main-preflight</title>" \
          | grep -q teamagent-main-preflight
        if touch /teamagent-preflight-root-write 2>/dev/null; then exit 41; fi
      '
      volume_json='[{"name":"tmp"}]'
      environment_json='[
        {"name":"HOME","value":"/tmp/home"},
        {"name":"TMPDIR","value":"/tmp"},
        {"name":"XDG_CACHE_HOME","value":"/tmp/.cache"},
        {"name":"PYTHONPYCACHEPREFIX","value":"/tmp/.pycache"},
        {"name":"UV_CACHE_DIR","value":"/tmp/.uv-cache"},
        {"name":"npm_config_cache","value":"/tmp/.npm"},
        {"name":"PUPPETEER_CACHE_DIR","value":"/tmp/.cache/puppeteer"}
      ]'
      ;;
    tiktok)
      task_role="$(jq -er '.taskdefs.tiktok.critical.task_role_arn' "$snapshot")"
      execution_role="$(jq -er '.taskdefs.tiktok.critical.execution_role_arn' "$snapshot")"
      subnets="$(jq -c '.dispatchers.tiktok.static_environment.SUBNETS | split(",")' "$snapshot")"
      security_groups="$(jq -c '[.dispatchers.tiktok.static_environment.SG_ID]' "$snapshot")"
      expected_user="10001:10001"
      script='
        set -eu
        test "$(id -u)" = 10001
        test "$(id -g)" = 10001
        test "$(awk "/^CapEff:/{print \\$2}" /proc/self/status)" = 0000000000000000
        test "$(stat -c %a /tmp)" = 1777
        for path in /tmp/home /tmp/.cache /tmp/.pycache
        do
          mkdir -p "$path"
          printf writable > "$path/.teamagent-write-probe"
        done
        printf ok > /tmp/teamagent-preflight
        /app/.venv/bin/python -c "import sys; assert sys.version_info[:2] == (3, 14)"
        /app/.venv/bin/python -c "import playwright, teamagent.media.tool_worker, yt_dlp"
        command -v node
        command -v yt-dlp
        command -v chromium-browser
        command -v ffmpeg
        node --version >/dev/null
        yt-dlp --version >/dev/null
        test -x "$CHROMIUM_PATH"
        test -f "$TIKTOK_SCRAPER_PATH"
        node -e "require(\"/app/tools/tiktok_scraper/node_modules/playwright-core\")"
        chromium-browser --headless --disable-gpu \
          --dump-dom "data:text/html,<title>teamagent-preflight</title>" \
          | grep -q teamagent-preflight
        if touch /teamagent-preflight-root-write 2>/dev/null; then exit 42; fi
      '
      volume_json='[{"name":"tmp"}]'
      environment_json='[
        {"name":"HOME","value":"/tmp/home"},
        {"name":"TMPDIR","value":"/tmp"},
        {"name":"XDG_CACHE_HOME","value":"/tmp/.cache"},
        {"name":"PYTHONPYCACHEPREFIX","value":"/tmp/.pycache"}
      ]'
      ;;
    x_buzz)
      task_role="$(jq -er '.taskdefs.x_buzz.critical.task_role_arn' "$snapshot")"
      execution_role="$(jq -er '.taskdefs.x_buzz.critical.execution_role_arn' "$snapshot")"
      subnets="$(jq -c '.dispatchers.x_buzz.static_environment.SUBNETS | split(",")' "$snapshot")"
      security_groups="$(jq -c '[.dispatchers.x_buzz.static_environment.SG_ID]' "$snapshot")"
      expected_user="10001:10001"
      script='
        set -eu
        test "$(id -u)" = 10001
        test "$(id -g)" = 10001
        test "$(awk "/^CapEff:/{print \\$2}" /proc/self/status)" = 0000000000000000
        test "$(stat -c %a /tmp)" = 1777
        for path in /tmp/home /tmp/.cache /tmp/.pycache
        do
          mkdir -p "$path"
          printf writable > "$path/.teamagent-write-probe"
        done
        printf ok > /tmp/teamagent-preflight
        python -c "import sys; assert sys.version_info[:2] == (3, 14)"
        if touch /teamagent-preflight-root-write 2>/dev/null; then exit 44; fi
      '
      volume_json='[{"name":"tmp"}]'
      environment_json='[
        {"name":"HOME","value":"/tmp/home"},
        {"name":"TMPDIR","value":"/tmp"},
        {"name":"XDG_CACHE_HOME","value":"/tmp/.cache"},
        {"name":"PYTHONPYCACHEPREFIX","value":"/tmp/.pycache"}
      ]'
      ;;
    openclaw)
      task_role="$PREFLIGHT_EFS_ROLE_ARN"
      execution_role="$(jq -er '.taskdefs.openclaw.critical.execution_role_arn' "$snapshot")"
      subnets="$(jq -c '.services.openclaw.critical.network_configuration.subnets' "$snapshot")"
      security_groups="$(jq -c '.services.openclaw.critical.network_configuration.security_groups' "$snapshot")"
      expected_user="65532:65532"
      script='
        const fs = require("fs");
        if (process.getuid() !== 65532 || process.getgid() !== 65532) process.exit(40);
        const status = fs.readFileSync("/proc/self/status", "utf8");
        if (!/^CapEff:\s+0+$/m.test(status)) process.exit(41);
        fs.writeFileSync("/tmp/teamagent-preflight", "tmp");
        fs.writeFileSync("/tmp/teamagent-openclaw/state/preflight", "state");
        const state = fs.statSync("/tmp/teamagent-openclaw/state");
        if (state.uid !== 65532 || state.gid !== 65532) process.exit(42);
        if ((state.mode & 0o777) !== 0o700) process.exit(44);
        let rootBlocked = false;
        try {
          fs.writeFileSync("/teamagent-preflight-root-write", "bad");
        } catch (error) {
          if (["EROFS", "EACCES", "EPERM"].includes(error.code)) rootBlocked = true;
          else throw error;
        }
        if (!rootBlocked) process.exit(43);
      '
      volume_json="$(jq -n -c \
        --arg fs "$PREFLIGHT_EFS_ID" --arg ap "$PREFLIGHT_EFS_AP_ID" '[
        {name:"tmp"},
        {
          name:"state",
          efsVolumeConfiguration:{
            fileSystemId:$fs,
            rootDirectory:"/",
            transitEncryption:"ENABLED",
            authorizationConfig:{accessPointId:$ap,iam:"ENABLED"}
          }
        }
      ]')"
      environment_json='[]'
      ;;
    *) die "未知のpreflight profileです: $profile" ;;
  esac

  if [ "$profile" = "openclaw" ]; then
    entry_point_json='["/nodejs/bin/node"]'
    command_json="$(jq -n -c --arg script "$script" '["-e", $script]')"
  else
    entry_point_json='["/bin/sh","-c"]'
    command_json="$(jq -n -c --arg script "$script" '[$script]')"
  fi

  local definition="$TMP_ROOT/preflight-td-${profile}.json"
  local registered="$TMP_ROOT/preflight-register-${profile}.json"
  local container_mounts
  if [ "$profile" = "openclaw" ]; then
    container_mounts='[
      {"sourceVolume":"tmp","containerPath":"/tmp","readOnly":false},
      {"sourceVolume":"state","containerPath":"/tmp/teamagent-openclaw/state","readOnly":false}
    ]'
  else
    container_mounts='[
      {"sourceVolume":"tmp","containerPath":"/tmp","readOnly":false}
    ]'
  fi
  jq -n \
    --arg family "$family" \
    --arg task_role "$task_role" \
    --arg execution_role "$execution_role" \
    --arg image "$image" \
    --arg user "$expected_user" \
    --argjson volumes "$volume_json" \
    --argjson mounts "$container_mounts" \
    --argjson environment "$environment_json" \
    --argjson entry_point "$entry_point_json" \
    --argjson command "$command_json" '
    {
      family:$family,
      taskRoleArn:$task_role,
      executionRoleArn:$execution_role,
      networkMode:"awsvpc",
      requiresCompatibilities:["FARGATE"],
      cpu:"512",
      memory:"1024",
      runtimePlatform:{cpuArchitecture:"ARM64",operatingSystemFamily:"LINUX"},
      volumes:$volumes,
      containerDefinitions:[{
        name:"preflight",
        image:$image,
        essential:true,
        user:$user,
        readonlyRootFilesystem:true,
        environment:$environment,
        entryPoint:$entry_point,
        command:$command,
        linuxParameters:{
          initProcessEnabled:true,
          capabilities:{drop:["ALL"],add:[]}
        },
        mountPoints:$mounts
      }],
      tags:[
        {key:"Purpose",value:"TeamAgentRuntimePreflight"},
        {key:"ManagedBy",value:"terraform-runtime-guard"}
      ]
    }
  ' > "$definition"
  aws_cli ecs register-task-definition --cli-input-json "file://$definition" \
    --output json > "$registered"
  local task_definition task_response task_arn
  task_definition="$(jq -er '.taskDefinition.taskDefinitionArn' "$registered")"
  PREFLIGHT_REGISTERED_TASK_DEFINITIONS="$PREFLIGHT_REGISTERED_TASK_DEFINITIONS $task_definition"
  task_response="$TMP_ROOT/preflight-run-${profile}.json"
  aws_cli ecs run-task \
    --cluster "$cluster" \
    --task-definition "$task_definition" \
    --launch-type FARGATE \
    --count 1 \
    --network-configuration "$(jq -n -c \
      --argjson subnets "$subnets" --argjson groups "$security_groups" '{
      awsvpcConfiguration:{
        subnets:$subnets,
        securityGroups:$groups,
        assignPublicIp:"ENABLED"
      }
    }')" \
    --output json > "$task_response"
  jq -e '(.failures | length) == 0 and (.tasks | length) == 1' \
    "$task_response" >/dev/null || die "preflight RunTaskが拒否されました: $profile"
  task_arn="$(jq -er '.tasks[0].taskArn' "$task_response")"
  PREFLIGHT_TASKS="$PREFLIGHT_TASKS ${cluster}|${task_arn}"
  wait_task_and_record "$cluster" "$task_arn" "$image" "$output"
  aws_cli ecs deregister-task-definition --task-definition "$task_definition" \
    --output json >/dev/null
}

PREFLIGHT_EFS_ID=""
PREFLIGHT_EFS_AP_ID=""
PREFLIGHT_EFS_SG_ID=""
PREFLIGHT_EFS_ROLE_NAME=""
PREFLIGHT_EFS_ROLE_ARN=""
PREFLIGHT_EFS_MOUNT_TARGETS=""
PREFLIGHT_REGISTERED_TASK_DEFINITIONS=""
PREFLIGHT_TASKS=""

cleanup_preflight_tasks() {
  set +e
  local item cluster task task_definition
  for item in $PREFLIGHT_TASKS; do
    cluster="${item%%|*}"
    task="${item#*|}"
    aws_cli ecs stop-task --cluster "$cluster" --task "$task" \
      --reason "TeamAgent runtime preflight cleanup" >/dev/null 2>&1
  done
  for task_definition in $PREFLIGHT_REGISTERED_TASK_DEFINITIONS; do
    aws_cli ecs deregister-task-definition \
      --task-definition "$task_definition" >/dev/null 2>&1
  done
  set -e
}

cleanup_preflight_efs() {
  set +e
  local id
  if [ -n "$PREFLIGHT_EFS_AP_ID" ]; then
    aws_cli efs delete-access-point --access-point-id "$PREFLIGHT_EFS_AP_ID" >/dev/null 2>&1
  fi
  for id in $PREFLIGHT_EFS_MOUNT_TARGETS; do
    aws_cli efs delete-mount-target --mount-target-id "$id" >/dev/null 2>&1
  done
  if [ -n "$PREFLIGHT_EFS_ID" ]; then
    local attempt=0
    while [ "$attempt" -lt 60 ]; do
      if [ "$(aws_cli efs describe-mount-targets --file-system-id "$PREFLIGHT_EFS_ID" \
          --query 'MountTargets | length(@)' --output text 2>/dev/null)" = "0" ]; then
        break
      fi
      attempt=$((attempt + 1))
      sleep 5
    done
    aws_cli efs delete-file-system --file-system-id "$PREFLIGHT_EFS_ID" >/dev/null 2>&1
  fi
  if [ -n "$PREFLIGHT_EFS_ROLE_NAME" ]; then
    aws_cli iam delete-role-policy --role-name "$PREFLIGHT_EFS_ROLE_NAME" \
      --policy-name EfsPreflight >/dev/null 2>&1
    aws_cli iam delete-role --role-name "$PREFLIGHT_EFS_ROLE_NAME" >/dev/null 2>&1
  fi
  if [ -n "$PREFLIGHT_EFS_SG_ID" ]; then
    aws_cli ec2 delete-security-group --group-id "$PREFLIGHT_EFS_SG_ID" >/dev/null 2>&1
  fi
  set -e
}

create_preflight_efs() {
  local snapshot="$1"
  local token="teamagent-runtime-preflight-$(date +%s)-$$"
  local vpc openclaw_sg fs_json ap_json role_json subnet mount_json
  vpc="$(aws_cli ec2 describe-subnets \
    --subnet-ids "$(jq -r '.services.openclaw.critical.network_configuration.subnets[0]' "$snapshot")" \
    --query 'Subnets[0].VpcId' --output text)"
  openclaw_sg="$(jq -er '.services.openclaw.critical.network_configuration.security_groups[0]' "$snapshot")"
  PREFLIGHT_EFS_SG_ID="$(aws_cli ec2 create-security-group \
    --group-name "$token" \
    --description "Temporary TeamAgent OpenClaw EFS preflight" \
    --vpc-id "$vpc" --query GroupId --output text)"
  aws_cli ec2 authorize-security-group-ingress \
    --group-id "$PREFLIGHT_EFS_SG_ID" \
    --ip-permissions "IpProtocol=tcp,FromPort=2049,ToPort=2049,UserIdGroupPairs=[{GroupId=$openclaw_sg}]" \
    >/dev/null

  fs_json="$TMP_ROOT/preflight-efs.json"
  aws_cli efs create-file-system --creation-token "$token" --encrypted \
    --performance-mode generalPurpose --throughput-mode bursting \
    --tags "Key=Purpose,Value=TeamAgentRuntimePreflight" \
    --output json > "$fs_json"
  PREFLIGHT_EFS_ID="$(jq -er '.FileSystemId' "$fs_json")"
  aws_cli efs wait file-system-available --file-system-id "$PREFLIGHT_EFS_ID"

  for subnet in $(jq -r '.services.openclaw.critical.network_configuration.subnets[]' "$snapshot"); do
    mount_json="$TMP_ROOT/preflight-mount-${subnet}.json"
    aws_cli efs create-mount-target --file-system-id "$PREFLIGHT_EFS_ID" \
      --subnet-id "$subnet" --security-groups "$PREFLIGHT_EFS_SG_ID" \
      --output json > "$mount_json"
    PREFLIGHT_EFS_MOUNT_TARGETS="$PREFLIGHT_EFS_MOUNT_TARGETS $(jq -er '.MountTargetId' "$mount_json")"
  done
  local attempt=0
  while [ "$attempt" -lt 60 ]; do
    if aws_cli efs describe-mount-targets --file-system-id "$PREFLIGHT_EFS_ID" \
      --output json | jq -e '
        (.MountTargets | length) > 0 and
        (.MountTargets | all(.LifeCycleState == "available"))
      ' >/dev/null; then
      break
    fi
    attempt=$((attempt + 1))
    sleep 5
  done
  [ "$attempt" -lt 60 ] || die "temporary EFS mount targetsがavailableになりません"

  ap_json="$TMP_ROOT/preflight-ap.json"
  aws_cli efs create-access-point \
    --file-system-id "$PREFLIGHT_EFS_ID" \
    --posix-user Uid=65532,Gid=65532 \
    --root-directory 'Path=/state,CreationInfo={OwnerUid=65532,OwnerGid=65532,Permissions=0700}' \
    --tags "Key=Purpose,Value=TeamAgentRuntimePreflight" \
    --output json > "$ap_json"
  PREFLIGHT_EFS_AP_ID="$(jq -er '.AccessPointId' "$ap_json")"

  # Keep the disposable preflight role outside the protected
  # "${PROJECT}-${ENVIRONMENT}-*" production-role namespace. After the
  # direct-mutation deny is attached, production role policy/delete calls are
  # intentionally blocked for the human operator, while this exact
  # guard-created role still has to be configured and removed in the same
  # preflight run.
  PREFLIGHT_EFS_ROLE_NAME="${PROJECT}-runtime-preflight-${ENVIRONMENT}-$$"
  local assume="$TMP_ROOT/preflight-assume.json"
  jq -n '{
    Version:"2012-10-17",
    Statement:[{
      Effect:"Allow",
      Principal:{Service:"ecs-tasks.amazonaws.com"},
      Action:"sts:AssumeRole"
    }]
  }' > "$assume"
  role_json="$TMP_ROOT/preflight-role.json"
  aws_cli iam create-role --role-name "$PREFLIGHT_EFS_ROLE_NAME" \
    --assume-role-policy-document "file://$assume" \
    --tags \
      Key=Purpose,Value=TeamAgentRuntimePreflight \
      Key=ManagedBy,Value=terraform-runtime-guard \
    --output json > "$role_json"
  PREFLIGHT_EFS_ROLE_ARN="$(jq -er '.Role.Arn' "$role_json")"
  local policy="$TMP_ROOT/preflight-efs-policy.json"
  jq -n \
    --arg fs "arn:aws:elasticfilesystem:${REGION}:${EXPECTED_ACCOUNT_ID}:file-system/${PREFLIGHT_EFS_ID}" \
    --arg ap "arn:aws:elasticfilesystem:${REGION}:${EXPECTED_ACCOUNT_ID}:access-point/${PREFLIGHT_EFS_AP_ID}" '{
    Version:"2012-10-17",
    Statement:[{
      Effect:"Allow",
      Action:["elasticfilesystem:ClientMount","elasticfilesystem:ClientWrite"],
      Resource:$fs,
      Condition:{
        StringEquals:{"elasticfilesystem:AccessPointArn":$ap},
        Bool:{"elasticfilesystem:AccessedViaMountTarget":"true"}
      }
    }]
  }' > "$policy"
  aws_cli iam put-role-policy --role-name "$PREFLIGHT_EFS_ROLE_NAME" \
    --policy-name EfsPreflight --policy-document "file://$policy"
}

run_activation_task() {
  local profile="$1" snapshot="$2" output="$3"
  local component container env_override task_definition image network
  case "$profile" in
    activation-ingest-acl-quarantine)
      component="ingest"
      container="ingest"
      env_override='[{"name":"INGEST_DRY_RUN","value":"1"}]'
      ;;
    activation-canary)
      component="canary"
      container="canary"
      env_override='[]'
      ;;
    *) die "未知のactivation preflight profileです" ;;
  esac
  task_definition="$(jq -er --arg c "$component" '.taskdefs[$c].arn' "$snapshot")"
  image="$(jq -er --arg c "$component" '.taskdefs[$c].image' "$snapshot")"
  network="$(jq -c --arg c "$component" '{
    awsvpcConfiguration:{
      subnets:.targets[$c].critical.ecs_target.network_configuration.subnets,
      securityGroups:.targets[$c].critical.ecs_target.network_configuration.security_groups,
      assignPublicIp:
        (if .targets[$c].critical.ecs_target.network_configuration.assign_public_ip
         then "ENABLED" else "DISABLED" end)
    }
  }' "$snapshot")"
  local response="$TMP_ROOT/activation-${component}.json"
  aws_cli ecs run-task --cluster "${PROJECT}-${ENVIRONMENT}" \
    --task-definition "$task_definition" --launch-type FARGATE --count 1 \
    --network-configuration "$network" \
    --overrides "$(jq -n -c \
      --arg name "$container" --argjson env "$env_override" '{
      containerOverrides:[{name:$name,environment:$env}]
    }')" --output json > "$response"
  jq -e '(.failures | length) == 0 and (.tasks | length) == 1' "$response" >/dev/null ||
    die "activation preflight RunTaskが拒否されました: $profile"
  local task_arn
  task_arn="$(jq -er '.tasks[0].taskArn' "$response")"
  PREFLIGHT_TASKS="$PREFLIGHT_TASKS ${PROJECT}-${ENVIRONMENT}|${task_arn}"
  wait_task_and_record "${PROJECT}-${ENVIRONMENT}" "$task_arn" "$image" "$output"

  if [ "$component" = "ingest" ]; then
    local stream logs
    stream="$(jq -er '.log_stream_name | select(length > 0)' "$output")" ||
      die "ACL quarantine taskのCloudWatch log streamが取得できません"
    logs="$TMP_ROOT/activation-ingest-logs.json"
    aws_cli logs get-log-events --log-group-name "/${PROJECT}/${ENVIRONMENT}/ingest" \
      --log-stream-name "$stream" --start-from-head --output json > "$logs"
    jq -e '
      ([.events[].message |
        select(contains("ingest_slack_channel_skipped_no_acl"))] | length) == 0 and
      ([.events[].message |
        select(contains("ingest_slack_channel_acl_resolved"))] | length) > 0
    ' "$logs" >/dev/null ||
      die "ingest ACL quarantineがACL解決ゼロ/skipを検出しました"
    jq '. + {acl_quarantine:{
      skipped_no_acl:0,
      resolved_events:1,
      log_content_persisted:false
    }}' "$output" > "$output.next"
    mv "$output.next" "$output"
  fi
}

POST_APPLY_TASKS=""

cleanup_post_apply_tasks() {
  set +e
  local item cluster task
  for item in $POST_APPLY_TASKS; do
    cluster="${item%%|*}"
    task="${item#*|}"
    aws_cli ecs stop-task --cluster "$cluster" --task "$task" \
      --reason "TeamAgent post-apply probe cleanup" >/dev/null 2>&1
  done
  set -e
}

run_post_apply_service_probe() {
  local snapshot="$1" core="$2" apply_attempt_id="$3" output="$4"
  local task_definition image network response task_arn task_result
  local probe_code overrides log_stream logs result attempt
  task_definition="$(jq -er '.taskdefs.canary.arn' "$snapshot")"
  image="$(jq -er '.taskdefs.canary.image' "$snapshot")"
  network="$(jq -c '{
    awsvpcConfiguration:{
      subnets:.targets.canary.critical.ecs_target.network_configuration.subnets,
      securityGroups:
        .targets.canary.critical.ecs_target.network_configuration.security_groups,
      assignPublicIp:
        (if .targets.canary.critical.ecs_target.network_configuration.assign_public_ip
         then "ENABLED" else "DISABLED" end)
    }
  }' "$snapshot")"
  probe_code='import json
import os
import urllib.request

def fetch(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, response.read()

mcp_status, _ = fetch("http://teamagent-mcp.teamagent.internal:8787/healthz")
connect_status, connect_raw = fetch(
    "http://connect-web.teamagent.internal:8788/healthz"
)
connect = json.loads(connect_raw)
checks = {
    "mcp_http_200": mcp_status == 200,
    "connect_http_200": connect_status == 200,
    "connect_contract_ok": connect.get("ok") is True,
    "connect_version_id": (
        connect.get("app_html_s3_version_id")
        == os.environ["EXPECTED_APP_VERSION_ID"]
    ),
    "connect_sha256": (
        connect.get("app_html_sha256") == os.environ["EXPECTED_APP_SHA256"]
    ),
    "connect_manifest_sha256": (
        connect.get("app_html_manifest_sha256")
        == os.environ["EXPECTED_APP_MANIFEST_SHA256"]
    ),
    "connect_build_inputs_sha256": (
        connect.get("app_html_build_inputs_sha256")
        == os.environ["EXPECTED_APP_BUILD_INPUTS_SHA256"]
    ),
}
print(json.dumps({
    "kind": "teamagent-post-apply-service-probe",
    "schema_version": 1,
    "apply_attempt_id": os.environ["APPLY_ATTEMPT_ID"],
    "checks": checks,
}, sort_keys=True, separators=(",", ":")), flush=True)
raise SystemExit(0 if all(checks.values()) else 23)
'
  overrides="$(jq -n -c \
    --arg code "$probe_code" \
    --arg attempt "$apply_attempt_id" \
    --arg version_id "$(jq -er '.connect_app_html.version_id' "$core")" \
    --arg sha256 "$(jq -er '.connect_app_html.sha256' "$core")" \
    --arg manifest "$(
      jq -er '.connect_app_html.vault_manifest_sha256' "$core"
    )" \
    --arg build_inputs "$(
      jq -er '.connect_app_html.build_inputs_sha256' "$core"
    )" '{
    containerOverrides:[{
      name:"canary",
      command:["/app/.venv/bin/python","-c",$code],
      environment:[
        {name:"APPLY_ATTEMPT_ID",value:$attempt},
        {name:"EXPECTED_APP_VERSION_ID",value:$version_id},
        {name:"EXPECTED_APP_SHA256",value:$sha256},
        {name:"EXPECTED_APP_MANIFEST_SHA256",value:$manifest},
        {name:"EXPECTED_APP_BUILD_INPUTS_SHA256",value:$build_inputs}
      ]
    }]
  }')"
  response="$TMP_ROOT/post-apply-service-probe-run.json"
  aws_cli ecs run-task --cluster "${PROJECT}-${ENVIRONMENT}" \
    --task-definition "$task_definition" --launch-type FARGATE --count 1 \
    --network-configuration "$network" --overrides "$overrides" \
    --output json > "$response"
  jq -e '(.failures | length) == 0 and (.tasks | length) == 1' \
    "$response" >/dev/null ||
    die "post-apply service probe RunTaskが拒否されました"
  task_arn="$(jq -er '.tasks[0].taskArn' "$response")"
  POST_APPLY_TASKS="$POST_APPLY_TASKS ${PROJECT}-${ENVIRONMENT}|${task_arn}"
  task_result="$TMP_ROOT/post-apply-service-probe-task.json"
  wait_task_and_record \
    "${PROJECT}-${ENVIRONMENT}" "$task_arn" "$image" "$task_result"
  log_stream="$(jq -er '.log_stream_name | select(length > 0)' "$task_result")" ||
    die "post-apply service probeのlog streamを取得できません"
  logs="$TMP_ROOT/post-apply-service-probe-logs.json"
  result="$TMP_ROOT/post-apply-service-probe-result.json"
  attempt=0
  while [ "$attempt" -lt 60 ]; do
    aws_cli logs get-log-events \
      --log-group-name "/${PROJECT}/${ENVIRONMENT}/canary-health" \
      --log-stream-name "$log_stream" --start-from-head \
      --output json > "$logs"
    if jq -e --arg attempt "$apply_attempt_id" '
      [
        .events[].message |
        (fromjson? // empty) |
        select(
          .kind == "teamagent-post-apply-service-probe" and
          .schema_version == 1 and
          .apply_attempt_id == $attempt and
          (.checks | type) == "object" and
          ([.checks[]] | length) == 7 and
          ([.checks[]] | all(. == true))
        )
      ] |
      if length == 1 then .[0] else empty end
    ' "$logs" > "$result"; then
      break
    fi
    attempt=$((attempt + 1))
    sleep 5
  done
  [ "$attempt" -lt 60 ] ||
    die "post-apply MCP/connect-web exact probe証跡を5分以内に確認できません"
  jq -n -S \
    --arg kind "teamagent-post-apply-service-probe-receipt" \
    --argjson schema_version 1 \
    --arg apply_attempt_id "$apply_attempt_id" \
    --arg task_definition "$task_definition" \
    --arg image "$image" \
    --arg log_stream_name "$log_stream" \
    --arg verified_at_utc "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --slurpfile task "$task_result" \
    --slurpfile result "$result" '{
      kind:$kind,
      schema_version:$schema_version,
      apply_attempt_id:$apply_attempt_id,
      task_definition:$task_definition,
      image:$image,
      log_stream_name:$log_stream_name,
      verified_at_utc:$verified_at_utc,
      task:$task[0],
      result:$result[0]
    }' > "$output"
  chmod 600 "$output"
}

run_forced_rollback_dm_qa() {
  local snapshot="$1" apply_attempt_id="$2" deadline_epoch="$3" output="$4"
  local evidence_bucket="$5" evidence_prefix="$6"
  local encryption_kms_key_arn="$7" signing_kms_key_arn="$8"
  local now available timeout_seconds status
  now="$(date +%s)"
  [[ "$deadline_epoch" =~ ^[0-9]+$ ]] ||
    die "forced rollback DM QA deadline must be an epoch second"
  available=$((deadline_epoch - now - FORCED_ROLLBACK_DM_QA_RECOVERY_RESERVE_SECONDS))
  if [ "$available" -le 0 ]; then
    echo "FATAL: forced rollback DM QA has no time before the old-dwell recovery reserve" >&2
    return 124
  fi
  timeout_seconds="$available"
  if [ "$timeout_seconds" -gt "$FORCED_ROLLBACK_DM_QA_MAX_SECONDS" ]; then
    timeout_seconds="$FORCED_ROLLBACK_DM_QA_MAX_SECONDS"
  fi

  local probe_output
  if ! probe_output="$(mktemp "${output}.probe.XXXXXX")"; then
    echo "FATAL: forced rollback DM QA result用一時ファイルを作成できません" >&2
    return 24
  fi
  if ! chmod 600 "$probe_output"; then
    echo "FATAL: forced rollback DM QA result用一時ファイルを保護できません" >&2
    rm -f "$probe_output"
    return 24
  fi

  if python3 "$FORCED_ROLLBACK_DM_QA_PROBE" \
    "$AWS_BIN" \
    "$snapshot" \
    "$apply_attempt_id" \
    "$timeout_seconds" \
    "$evidence_bucket" \
    "$evidence_prefix" \
    "$encryption_kms_key_arn" \
    "$signing_kms_key_arn" \
    > "$probe_output"; then
    status=0
  else
    status=$?
  fi
  case "$status" in
    0) ;;
    24|124)
      rm -f "$probe_output"
      return "$status"
      ;;
    *)
      echo "FATAL: forced rollback DM QA probe returned an unexpected status" >&2
      rm -f "$probe_output"
      return 24
      ;;
  esac
  if ! jq -s -e \
    --arg attempt "$apply_attempt_id" \
    --arg openclaw "$(jq -er '.taskdefs.openclaw.arn' "$snapshot")" \
    --arg mcp "$(jq -er '.taskdefs.mcp.arn' "$snapshot")" \
    --arg bucket "$evidence_bucket" \
    --arg prefix "$evidence_prefix" \
    --arg encryption_kms "$encryption_kms_key_arn" \
    --arg signing_kms "$signing_kms_key_arn" '
    length == 1 and
    (.[0] |
      (keys | sort) == ([
        "applyAttemptId",
        "kind",
        "locator",
        "mcpTaskDefinitionArn",
        "openclawTaskDefinitionArn",
        "result",
        "schema_version",
        "verified_at_utc"
      ] | sort) and
      .kind == "teamagent-forced-rollback-dm-qa-result" and
      .schema_version == 1 and
      .result == "PASSED" and
      .applyAttemptId == $attempt and
      .openclawTaskDefinitionArn == $openclaw and
      .mcpTaskDefinitionArn == $mcp and
      (.verified_at_utc |
        test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")) and
      (.locator | keys | sort) == ([
        "bucket",
        "content_type",
        "encryption_kms_key_arn",
        "exact_version_redownload",
        "key",
        "object_lock_mode",
        "retain_until",
        "sha256",
        "signature",
        "signer",
        "size",
        "version_id"
      ] | sort) and
      $bucket == "teamagent-dev-openclaw-rollout-evidence" and
      $prefix == "forced-rollback-drills/" and
      .locator.bucket == $bucket and
      .locator.key ==
        ($prefix + $attempt + "/dm-qa/result.json") and
      .locator.content_type == "application/json" and
      .locator.object_lock_mode == "COMPLIANCE" and
      .locator.encryption_kms_key_arn == $encryption_kms and
      (.locator.retain_until |
        test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")) and
      (.locator.sha256 | test("^[0-9a-f]{64}$")) and
      (.locator.size |
        type == "number" and . > 0 and floor == .) and
      (.locator.version_id |
        test("^[A-Za-z0-9._~+/=-]{1,1024}$") and
        . != "null" and . != "None") and
      (.locator.signature | keys | sort) == ([
        "key",
        "sha256",
        "verified",
        "version_id"
      ] | sort) and
      .locator.signature.key == (.locator.key + ".sig") and
      (.locator.signature.sha256 | test("^[0-9a-f]{64}$")) and
      (.locator.signature.version_id |
        test("^[A-Za-z0-9._~+/=-]{1,1024}$") and
        . != "null" and . != "None") and
      .locator.signature.verified == true and
      (.locator.signer | keys | sort) == ([
        "algorithm",
        "kms_key_arn"
      ] | sort) and
      .locator.signer.kms_key_arn == $signing_kms and
      .locator.signer.algorithm == "RSASSA_PSS_SHA_256" and
      (.locator.exact_version_redownload | keys | sort) == ([
        "bytes_match",
        "requested_version_id",
        "returned_version_id",
        "sha256",
        "size"
      ] | sort) and
      .locator.exact_version_redownload.requested_version_id ==
        .locator.version_id and
      .locator.exact_version_redownload.returned_version_id ==
        .locator.version_id and
      .locator.exact_version_redownload.sha256 == .locator.sha256 and
      .locator.exact_version_redownload.size == .locator.size and
      .locator.exact_version_redownload.bytes_match == true
    )
  ' "$probe_output" >/dev/null 2>&1; then
    echo "FATAL: forced rollback DM QA evidence binding is invalid" >&2
    rm -f "$probe_output"
    return 24
  fi
  if ! python3 -c \
    'import os, sys; os.link(sys.argv[1], sys.argv[2], follow_symlinks=False)' \
    "$probe_output" "$output" 2>/dev/null; then
    echo "FATAL: forced rollback DM QA result pathを排他的に確定できません" >&2
    rm -f "$probe_output"
    return 24
  fi
  if ! rm -f "$probe_output"; then
    echo "FATAL: forced rollback DM QA result一時linkを除去できません" >&2
    rm -f "$output" || true
    return 24
  fi
  if ! chmod 600 "$output"; then
    echo "FATAL: forced rollback DM QA resultを保護できません" >&2
    rm -f "$output"
    return 24
  fi
}

write_preflight_receipt() {
  local migration_id="$1" migration="$2" snapshot="$3" profiles="$4" output="$5"
  local config_manifest="$TMP_ROOT/config-manifest.txt"
  write_config_manifest "$config_manifest" exclude
  local now expires
  now="$(date +%s)"
  expires=$((now + 7200))
  jq -n -S \
    --arg guard_version "$GUARD_VERSION" \
    --arg migration_id "$migration_id" \
    --arg migration_kind "$(jq -er '.kind' "$migration")" \
    --arg account_id "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" \
    --argjson created_at_epoch "$now" \
    --argjson expires_at_epoch "$expires" \
    --arg git_commit "$(git_commit)" \
    --arg guard_script_sha256 "$(sha256_file "$SCRIPT_PATH")" \
    --arg guard_jq_sha256 "$(sha256_file "$GUARD_JQ")" \
    --arg migration_manifest_sha256 "$(sha256_file "$MIGRATION_FILE")" \
    --arg migration_contract_sha256 "$(
      normalized_migration_manifest_sha256 "$migration_id"
    )" \
    --arg config_manifest_sha256 "$(sha256_file "$config_manifest")" \
    --arg live_fingerprint_sha256 "$(sha256_file "$snapshot")" \
    --slurpfile migration "$migration" \
    --slurpfile profiles "$profiles" \
    --slurpfile supply_chain "$TMP_ROOT/supply-chain.json" '{
      kind:"runtime-preflight-receipt",
      guard_version:$guard_version,
      migration_id:$migration_id,
      migration_kind:$migration_kind,
      account_id:$account_id,
      region:$region,
      created_at_epoch:$created_at_epoch,
      expires_at_epoch:$expires_at_epoch,
      git_commit:$git_commit,
      guard_script_sha256:$guard_script_sha256,
      guard_jq_sha256:$guard_jq_sha256,
      migration_manifest_sha256:$migration_manifest_sha256,
      migration_contract_sha256:$migration_contract_sha256,
      config_manifest_sha256:$config_manifest_sha256,
      live_fingerprint_sha256:$live_fingerprint_sha256,
      images:(
        if $migration[0].kind == "runtime" then {
          openclaw:$migration[0].to.openclaw_image,
          mcp:$migration[0].to.mcp_image,
          x_buzz:$migration[0].to.x_buzz_image,
          tiktok:$migration[0].to.tiktok_image
        } else {
          ingest:$migration[0].from.images.ingest,
          canary:$migration[0].from.images.canary
        } end
      ),
      supply_chain:$supply_chain[0],
      profiles:$profiles[0]
    }' > "$output"
}

verify_preflight_receipt() {
  local receipt="$1" migration_id="$2" migration="$3" snapshot="$4"
  local config_manifest="$TMP_ROOT/config-manifest-verify.txt"
  local receipt_commit
  write_config_manifest "$config_manifest" exclude
  receipt_commit="$(
    jq -er '.git_commit | select(test("^[0-9a-f]{40}$"))' "$receipt"
  )" || die "preflight receiptのsource commitが不正です"
  assert_review_commit_transition "$receipt_commit" "$migration_id"
  jq -e \
    --arg version "$GUARD_VERSION" \
    --arg migration_id "$migration_id" \
    --arg migration_kind "$(jq -er '.kind' "$migration")" \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" \
    --arg script_sha "$(sha256_file "$SCRIPT_PATH")" \
    --arg jq_sha "$(sha256_file "$GUARD_JQ")" \
    --arg migration_contract_sha "$(
      normalized_migration_manifest_sha256 "$migration_id"
    )" \
    --arg config_sha "$(sha256_file "$config_manifest")" \
    --arg live_sha "$(sha256_file "$snapshot")" \
    --argjson now "$(date +%s)" \
    --slurpfile migration "$migration" '
    .kind == "runtime-preflight-receipt" and
    .guard_version == $version and
    .migration_id == $migration_id and
    .migration_kind == $migration_kind and
    .account_id == $account and .region == $region and
    (.git_commit | test("^[0-9a-f]{40}$")) and
    .guard_script_sha256 == $script_sha and
    .guard_jq_sha256 == $jq_sha and
    (.migration_manifest_sha256 | test("^[0-9a-f]{64}$")) and
    .migration_contract_sha256 == $migration_contract_sha and
    .config_manifest_sha256 == $config_sha and
    .live_fingerprint_sha256 == $live_sha and
    .created_at_epoch <= $now and .expires_at_epoch > $now and
    (.expires_at_epoch - .created_at_epoch) <= 7200 and
    (
      if $migration[0].kind == "runtime" then
        (.supply_chain | keys) == ["main"] and
        .supply_chain.main.image == $migration[0].to.mcp_image and
        .supply_chain.main.image_digest ==
          ($migration[0].to.mcp_image | split("@")[1]) and
        .supply_chain.main.source_commit ==
          $migration[0].to.main_source_commit and
        .supply_chain.main.minimum_source_commit ==
          $migration[0].to.main_signature.minimum_source_commit and
        .supply_chain.main.required_hmac_contract_commit ==
          $migration[0].to.main_signature.required_hmac_contract_commit and
        .supply_chain.main.kms_key_arn ==
          $migration[0].to.main_signature.kms_key_arn and
        .supply_chain.main.annotation_name ==
          $migration[0].to.main_signature.annotation_name and
        .supply_chain.main.rekor_transparency_log_verified == true and
        (.supply_chain.main.signature_count | type == "number" and
          . >= 1 and floor == .) and
        (.supply_chain.main.verified_claims_sha256 |
          test("^[0-9a-f]{64}$"))
      else .supply_chain == {}
      end
    ) and
    (.profiles | keys | sort) ==
      ($migration[0].required_preflight_profiles | sort) and
    (.profiles | to_entries | all(
      .value.exit_code == 0 and
      .value.stopped_reason_code == "EssentialContainerExited" and
      (.value.image | test("@sha256:[0-9a-f]{64}$")) and
      .value.image_digest == (.value.image | split("@")[1])
    ))
  ' "$receipt" >/dev/null ||
    die "preflight receiptが期限・live・review transition・hash・profile契約と不一致です"
}

verify_required_migration_apply_receipt() {
  local receipt="$1" required_migration_id="$2" snapshot="$3"
  local state_contract="$4"
  local receipt_commit required_migration_contract_sha256
  local retention_live="$TMP_ROOT/prior-bedrock-retention-$RANDOM.json"
  local runtime_inventory="$TMP_ROOT/prior-runtime-inventory-$RANDOM.json"
  local embedded_retention="$TMP_ROOT/prior-bedrock-embedded-$RANDOM.json"
  local embedded_lock="$TMP_ROOT/prior-shared-lock-embedded-$RANDOM.json"
  local embedded_outcome="$TMP_ROOT/prior-outcome-embedded-$RANDOM.json"
  local embedded_rollout="$TMP_ROOT/prior-openclaw-rollout-embedded-$RANDOM.json"
  local embedded_service_probe="$TMP_ROOT/prior-service-probe-embedded-$RANDOM.json"
  local embedded_media_authorization="$TMP_ROOT/prior-media-authorization-embedded-$RANDOM.json"
  local embedded_ecs_saga="$TMP_ROOT/prior-ecs-saga-embedded-$RANDOM.json"
  local embedded_ecs_saga_verification="$TMP_ROOT/prior-ecs-saga-verification-embedded-$RANDOM.json"
  local embedded_eventbridge_verification="$TMP_ROOT/prior-eventbridge-verification-embedded-$RANDOM.json"
  local embedded_finalization="$TMP_ROOT/prior-deployment-finalization-embedded-$RANDOM.json"
  local embedded_persisted="$TMP_ROOT/prior-openclaw-persisted-$RANDOM.json"
  local current_state_contract="$TMP_ROOT/prior-current-state-$RANDOM.json"
  receipt_commit="$(
    jq -er '.git_commit | select(test("^[0-9a-f]{40}$"))' "$receipt"
  )" || die "prior apply receiptのsource commitが不正です"
  assert_review_commit_transition "$receipt_commit" "$required_migration_id"
  required_migration_contract_sha256="$(
    normalized_migration_manifest_sha256 "$required_migration_id"
  )"
  run_evidence_helper verify-bedrock-retention --output "$retention_live"
  capture_complete_runtime_inventory "$runtime_inventory"
  build_scoped_release_state_contract \
    "$state_contract" "$receipt" "$current_state_contract"
  jq -S -c '.bedrock_retention_live' "$receipt" > "$embedded_retention"
  jq -S -c '.shared_deployment_lock_receipt' "$receipt" > "$embedded_lock"
  jq -j -S -c '.provenance_outcome_receipt' "$receipt" > "$embedded_outcome"
  jq -S -c '.openclaw_rollout_result' "$receipt" > "$embedded_rollout"
  jq -S -c '.post_apply_service_probe' "$receipt" > "$embedded_service_probe"
  jq -S -c '.media_apply_authorization' "$receipt" \
    > "$embedded_media_authorization"
  jq -j -S -c '.ecs_service_saga_receipt' "$receipt" > "$embedded_ecs_saga"
  jq -j -S -c '.ecs_service_saga_verification_receipt' "$receipt" \
    > "$embedded_ecs_saga_verification"
  jq -j -S -c '.eventbridge_apply_saga_verification_receipt' "$receipt" \
    > "$embedded_eventbridge_verification"
  jq -j -S -c '.deployment_finalization_receipt' "$receipt" \
    > "$embedded_finalization"
  [ "$(sha256_file "$embedded_retention")" = \
    "$(jq -er '.bedrock_retention_live_sha256' "$receipt")" ] &&
    [ "$(sha256_file "$embedded_lock")" = \
      "$(jq -er '.shared_deployment_lock_receipt_sha256' "$receipt")" ] &&
    [ "$(sha256_file "$embedded_outcome")" = \
      "$(jq -er '.provenance_outcome_receipt_sha256' "$receipt")" ] &&
    [ "$(sha256_file "$embedded_rollout")" = \
      "$(jq -er '.openclaw_rollout_result_sha256' "$receipt")" ] &&
    [ "$(sha256_file "$embedded_service_probe")" = \
      "$(jq -er '.post_apply_service_probe_sha256' "$receipt")" ] &&
    [ "$(sha256_file "$embedded_media_authorization")" = \
      "$(jq -er '.media_apply_authorization_sha256' "$receipt")" ] &&
    [ "$(sha256_file "$embedded_ecs_saga")" = \
      "$(jq -er '.ecs_service_saga_receipt_sha256' "$receipt")" ] &&
    [ "$(sha256_file "$embedded_ecs_saga_verification")" = \
      "$(jq -er '.ecs_service_saga_verification_receipt_sha256' "$receipt")" ] &&
    [ "$(sha256_file "$embedded_eventbridge_verification")" = \
      "$(jq -er '.eventbridge_apply_saga_verification_receipt_sha256' "$receipt")" ] &&
    [ "$(sha256_file "$embedded_finalization")" = \
      "$(jq -er '.deployment_finalization_receipt_sha256' "$receipt")" ] ||
    die "prior apply receiptのembedded evidence hash bindingが不正です"
  if [ "$(jq -er '.openclaw_rollout_result.required' "$receipt")" = "true" ]; then
    jq -S -c '.openclaw_rollout_result.persistedResult' "$receipt" \
      > "$embedded_persisted"
    [ "$(sha256_file "$embedded_persisted")" = "$(
      jq -er '.openclaw_rollout_result.immutableEvidence.resultSha256' \
        "$receipt"
    )" ] ||
      die "prior OpenClaw immutable result hash bindingが不正です"
  fi
  jq -e \
    --arg version "$GUARD_VERSION" \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" \
    --arg required_migration_id "$required_migration_id" \
    --arg required_migration_contract_sha256 \
      "$required_migration_contract_sha256" \
    --arg receipt_sha "$(sha256_file "$receipt")" \
    --arg state_sha "$(sha256_file "$current_state_contract")" \
    --arg live_sha "$(sha256_file "$snapshot")" \
    --arg inventory_sha "$(jq -er '.inventory_sha256' "$runtime_inventory")" \
    --argjson now "$(date +%s)" \
    --slurpfile state "$current_state_contract" \
    --slurpfile live "$snapshot" \
    --slurpfile retention "$retention_live" '
    def task_revisions($resources):
      $resources |
      map({
        key:.consumer_id,
        value:(.task_definition_arn |
          capture(":(?<revision>[1-9][0-9]*)$").revision |
          tonumber)
      }) |
      from_entries;
    def resource_identity:
      {
        activation_identity:.activation.identity,
        activation_type:.activation.type,
        consumer_id,
        pipeline,
        subject,
        terraform_address
      };
    def current_binding($id):
      if $id == "mcp" then {
        task:$live[0].taskdefs.mcp,
        state:$live[0].services.mcp.critical.desired_count
      }
      elif $id == "connect_web" then {
        task:$live[0].taskdefs.connect_web,
        state:$live[0].services.connect_web.critical.desired_count
      }
      elif $id == "openclaw" then {
        task:$live[0].taskdefs.openclaw,
        state:$live[0].services.openclaw.critical.desired_count
      }
      elif $id == "canary" then {
        task:$live[0].taskdefs.canary,
        state:$live[0].rules.canary.critical.state
      }
      elif $id == "ingest" then {
        task:$live[0].taskdefs.ingest,
        state:$live[0].rules.ingest.critical.state
      }
      elif $id == "morning_digest" then {
        task:$live[0].taskdefs.morning,
        state:$live[0].rules.morning.critical.state
      }
      elif $id == "x_buzz_worker" then {
        task:$live[0].taskdefs.x_buzz,
        state:$live[0].event_mappings.x_buzz.critical.enabled
      }
      elif $id == "tiktok_acquire" then {
        task:$live[0].taskdefs.tiktok,
        state:$live[0].event_mappings.tiktok.critical.enabled
      }
      else error("consumer outside code-owned release scope")
      end;
    (keys | sort) == ([
      "account_id",
      "alarm_delivery_receipt_sha256",
      "alarm_migration_receipt_sha256",
      "applied_at_epoch",
      "apply_attempt_id",
      "bedrock_retention_live",
      "bedrock_retention_live_sha256",
      "deployment_finalization_receipt",
      "deployment_finalization_receipt_sha256",
      "ecs_service_saga_receipt",
      "ecs_service_saga_receipt_sha256",
      "ecs_service_saga_verification_receipt",
      "ecs_service_saga_verification_receipt_sha256",
      "eventbridge_apply_saga_verification_receipt",
      "eventbridge_apply_saga_verification_receipt_sha256",
      "git_commit",
      "guard_version",
      "image_deployment_intent_id",
      "kind",
      "log_readiness_receipt_sha256",
      "media_apply_authorization",
      "media_apply_authorization_sha256",
      "media_cutover_receipt_sha256",
      "migration_contract_sha256",
      "migration_id",
      "migration_kind",
      "openclaw_rollout_result",
      "openclaw_rollout_result_sha256",
      "plan_sha256",
      "pre_live_contract",
      "pre_state_contract",
      "post_live_contract",
      "post_live_fingerprint_sha256",
      "post_runtime_inventory_sha256",
      "post_apply_service_probe",
      "post_apply_service_probe_sha256",
      "post_state_contract",
      "post_state_contract_sha256",
      "post_state_ownership_sha256",
      "provenance_outcome",
      "provenance_outcome_receipt",
      "provenance_outcome_receipt_sha256",
      "region",
      "required_migration_id",
      "reviewed_plan_sha256",
      "schema_version",
      "shared_deployment_lock_receipt",
      "shared_deployment_lock_receipt_sha256",
      "shared_deployment_lock_record_id",
      "source_receipt_sha256",
      "status",
      "versioning_receipt_sha256"
    ] | sort) and
    .kind == "terraform-runtime-apply-receipt" and
    .schema_version == 7 and
    .guard_version == $version and
    .account_id == $account and .region == $region and
    (.git_commit | test("^[0-9a-f]{40}$")) and
    .status == "applied" and
    .migration_kind == "runtime" and
    .migration_id == $required_migration_id and
    .migration_contract_sha256 ==
      $required_migration_contract_sha256 and
    (.reviewed_plan_sha256 | test("^[0-9a-f]{64}$")) and
    (.media_cutover_receipt_sha256 | test("^[0-9a-f]{64}$")) and
    (.media_apply_authorization_sha256 | test("^[0-9a-f]{64}$")) and
    .media_apply_authorization.kind ==
      "teamagent-media-apply-authorization" and
    .media_apply_authorization.state == "AUTHORIZED" and
    .media_apply_authorization.image_deployment_intent_id ==
      .image_deployment_intent_id and
    .media_apply_authorization.apply_attempt_id == .apply_attempt_id and
    .media_apply_authorization.plan_sha256 == .plan_sha256 and
    .media_apply_authorization.migration_contract_sha256 ==
      .migration_contract_sha256 and
    .media_apply_authorization.reviewed_plan_sha256 ==
      .reviewed_plan_sha256 and
    .required_migration_id == "" and
    .provenance_outcome == "applied" and
    (.image_deployment_intent_id |
      test("^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")) and
    (.apply_attempt_id |
      test("^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")) and
    (.source_receipt_sha256 | test("^[0-9a-f]{64}$")) and
    (.plan_sha256 | test("^[0-9a-f]{64}$")) and
    (.versioning_receipt_sha256 | test("^[0-9a-f]{64}$")) and
    (.log_readiness_receipt_sha256 | test("^[0-9a-f]{64}$")) and
    (.alarm_delivery_receipt_sha256 | test("^[0-9a-f]{64}$")) and
    (.alarm_migration_receipt_sha256 | test("^[0-9a-f]{64}$")) and
    (.ecs_service_saga_receipt_sha256 | test("^[0-9a-f]{64}$")) and
    .ecs_service_saga_receipt.kind ==
      "teamagent-ecs-service-apply-saga-receipt" and
    .ecs_service_saga_receipt.stage == "APPLIED" and
    .ecs_service_saga_receipt.apply_attempt_id == .apply_attempt_id and
    .ecs_service_saga_receipt.plan_sha256 == .plan_sha256 and
    (.ecs_service_saga_verification_receipt_sha256 |
      test("^[0-9a-f]{64}$")) and
    .ecs_service_saga_verification_receipt.kind ==
      "teamagent-ecs-service-apply-saga-receipt" and
    .ecs_service_saga_verification_receipt.stage ==
      "VERIFIED_APPLIED" and
    .ecs_service_saga_verification_receipt.apply_attempt_id ==
      .apply_attempt_id and
    .ecs_service_saga_verification_receipt.plan_sha256 ==
      .plan_sha256 and
    (.eventbridge_apply_saga_verification_receipt_sha256 |
      test("^[0-9a-f]{64}$")) and
    .eventbridge_apply_saga_verification_receipt.kind ==
      "teamagent-eventbridge-apply-saga-receipt" and
    .eventbridge_apply_saga_verification_receipt.stage ==
      "verified_applied" and
    .eventbridge_apply_saga_verification_receipt.apply_attempt_id ==
      .apply_attempt_id and
    .eventbridge_apply_saga_verification_receipt.plan_sha256 ==
      .plan_sha256 and
    (.deployment_finalization_receipt_sha256 |
      test("^[0-9a-f]{64}$")) and
    .deployment_finalization_receipt.kind ==
      "teamagent-deployment-apply-finalization-receipt" and
    .deployment_finalization_receipt.state == "APPLIED" and
    .deployment_finalization_receipt.record_id ==
      ("apply-finalization#" + .image_deployment_intent_id) and
    .deployment_finalization_receipt.intent_id ==
      .image_deployment_intent_id and
    .deployment_finalization_receipt.apply_attempt_id ==
      .apply_attempt_id and
    .deployment_finalization_receipt.plan_sha256 == .plan_sha256 and
    .deployment_finalization_receipt.ecs_attempt_record_id ==
      .ecs_service_saga_receipt.record_id and
    .deployment_finalization_receipt.ecs_active_record_id ==
      "ecs-service-apply#active#teamagent-dev-mcp-connect-web" and
    .deployment_finalization_receipt.eventbridge_record_id ==
      .eventbridge_apply_saga_verification_receipt.record_id and
    .deployment_finalization_receipt.ecs_verification_receipt_sha256 ==
      .ecs_service_saga_verification_receipt.receipt_sha256 and
    .deployment_finalization_receipt.eventbridge_verification_receipt_sha256 ==
      .eventbridge_apply_saga_verification_receipt.receipt_sha256 and
    (.deployment_finalization_receipt.apply_receipt_draft_sha256 |
      test("^[0-9a-f]{64}$")) and
    (.deployment_finalization_receipt.ecs_attempt_terminal_ledger_item_sha256 |
      test("^[0-9a-f]{64}$")) and
    (.deployment_finalization_receipt.ecs_active_terminal_ledger_item_sha256 |
      test("^[0-9a-f]{64}$")) and
    (.deployment_finalization_receipt.eventbridge_terminal_ledger_item_sha256 |
      test("^[0-9a-f]{64}$")) and
    (.deployment_finalization_receipt.receipt_sha256 |
      test("^[0-9a-f]{64}$")) and
    .deployment_finalization_receipt.applied_at_epoch ==
      .eventbridge_apply_saga_verification_receipt.verified_at and
    .applied_at_epoch ==
      .deployment_finalization_receipt.applied_at_epoch and
    (.openclaw_rollout_result_sha256 | test("^[0-9a-f]{64}$")) and
    .openclaw_rollout_result.schemaVersion == 2 and
    .openclaw_rollout_result.passed == true and
    .openclaw_rollout_result.applyAttemptId == .apply_attempt_id and
    (.post_apply_service_probe_sha256 | test("^[0-9a-f]{64}$")) and
    .post_apply_service_probe.kind ==
      "teamagent-post-apply-service-probe-receipt" and
    .post_apply_service_probe.schema_version == 1 and
    .post_apply_service_probe.apply_attempt_id == .apply_attempt_id and
    (.post_apply_service_probe.verified_at_utc |
      test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")) and
    .post_apply_service_probe.image == $live[0].taskdefs.canary.image and
    .post_apply_service_probe.task.exit_code == 0 and
    .post_apply_service_probe.result.kind ==
      "teamagent-post-apply-service-probe" and
    .post_apply_service_probe.result.apply_attempt_id == .apply_attempt_id and
    ([.post_apply_service_probe.result.checks[]] | length) == 7 and
    ([.post_apply_service_probe.result.checks[]] | all(. == true)) and
    .openclaw_rollout_result.newTaskDefinitionArn ==
      $live[0].taskdefs.openclaw.arn and
    (
      (
        .openclaw_rollout_result.required == false and
        .openclaw_rollout_result.reason == "task-definition-unchanged" and
        .openclaw_rollout_result.previousTaskDefinitionArn ==
          .openclaw_rollout_result.newTaskDefinitionArn
      ) or
      (
        .openclaw_rollout_result.required == true and
        .openclaw_rollout_result.previousTaskDefinitionArn !=
          .openclaw_rollout_result.newTaskDefinitionArn and
        .openclaw_rollout_result.persistedResult.automationRoleArn ==
          "arn:aws:sts::718959508629:assumed-role/teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker" and
        .openclaw_rollout_result.persistedResult.rollbackAuthorization.state ==
          "AUTHORIZED" and
        .openclaw_rollout_result.persistedResult.rollbackAuthorization.oneUse ==
          true and
        .openclaw_rollout_result.persistedResult.runningTasksBeforeSlack.complete ==
          true and
        .openclaw_rollout_result.persistedResult.runningTasksBeforeSlack.exactCandidateRevision ==
          true and
        (.openclaw_rollout_result.persistedResult.runningTasksBeforeSlack |
          (.tasks | length) == (.taskArns | length) and
          ([.tasks[].taskArn] | sort) == (.taskArns | sort) and
          all(.tasks[];
            .taskDefinitionArn ==
              $live[0].taskdefs.openclaw.arn)
        ) and
        .openclaw_rollout_result.persistedResult.runningTasksAfterSlack.complete ==
          true and
        .openclaw_rollout_result.persistedResult.runningTasksAfterSlack.exactCandidateRevision ==
          true and
        (.openclaw_rollout_result.persistedResult.runningTasksAfterSlack |
          (.tasks | length) == (.taskArns | length) and
          ([.tasks[].taskArn] | sort) == (.taskArns | sort) and
          all(.tasks[];
            .taskDefinitionArn ==
              $live[0].taskdefs.openclaw.arn)
        ) and
        (
          (
            .openclaw_rollout_result.persistedResult.slack.skipped == true and
            .openclaw_rollout_result.persistedResult.slack.connected == false and
            .openclaw_rollout_result.persistedResult.slack.mentionReplyExact == false and
            .openclaw_rollout_result.persistedResult.slack.skipReasonCodes == [
              "slack_self_authored_message_filtered",
              "aila_prompt_injection_defense_rejected_canary"
            ] and
            (.openclaw_rollout_result.persistedResult.slack |
              has("candidateLogCorrelation")) and
            .openclaw_rollout_result.persistedResult.slack.candidateLogCorrelation ==
              null and
            (.openclaw_rollout_result.persistedResult.slack |
              has("postedTs") | not) and
            (.openclaw_rollout_result.persistedResult.slack |
              has("replyTs") | not) and
            (.openclaw_rollout_result.persistedResult.slack |
              has("tokenSha256") | not) and
            (.openclaw_rollout_result.persistedResult.slack |
              has("correlationSha256") | not) and
            (.openclaw_rollout_result.persistedResult.slack |
              has("responseTokenAbsentFromPrompt") | not)
          ) or
          (
            (
              .openclaw_rollout_result.persistedResult.slack.skipped == false or
              (.openclaw_rollout_result.persistedResult.slack |
                has("skipped") | not)
            ) and
            .openclaw_rollout_result.persistedResult.slack.connected == true and
            .openclaw_rollout_result.persistedResult.slack.mentionReplyExact == true and
            .openclaw_rollout_result.persistedResult.slack.responseTokenAbsentFromPrompt ==
              true and
            (.openclaw_rollout_result.persistedResult.slack |
              has("skipReasonCodes") | not) and
            .openclaw_rollout_result.persistedResult.slack.candidateLogCorrelation.matched ==
              true and
            (.openclaw_rollout_result.persistedResult.slack.candidateLogCorrelation
              as $correlation |
              any(
                .openclaw_rollout_result.persistedResult.runningTasksBeforeSlack.tasks[];
                .taskArn == $correlation.taskArn and
                .logStreamName == $correlation.logStreamName
              )
            )
          )
        ) and
        .openclaw_rollout_result.immutableEvidence.verified == true and
        .openclaw_rollout_result.immutableEvidence.bucket ==
          "teamagent-dev-openclaw-rollout-evidence" and
        .openclaw_rollout_result.immutableEvidence.resultKey ==
          ("rollout-results/" + .apply_attempt_id + "/passed/result.json") and
        .openclaw_rollout_result.immutableEvidence.signatureKey ==
          ("rollout-results/" + .apply_attempt_id +
            "/passed/result.sig.json") and
        (.openclaw_rollout_result.immutableEvidence.resultVersionId |
          test("^[A-Za-z0-9._~+/=-]{1,1024}$") and
          . != "null" and . != "None") and
        (.openclaw_rollout_result.immutableEvidence.signatureVersionId |
          test("^[A-Za-z0-9._~+/=-]{1,1024}$") and
          . != "null" and . != "None") and
        (
          .openclaw_rollout_result.immutableEvidence.resultObjectLockMode ==
            "COMPLIANCE" or
          .openclaw_rollout_result.immutableEvidence.resultObjectLockMode ==
            "GOVERNANCE"
        ) and
        (
          .openclaw_rollout_result.immutableEvidence.signatureObjectLockMode ==
            "COMPLIANCE" or
          .openclaw_rollout_result.immutableEvidence.signatureObjectLockMode ==
            "GOVERNANCE"
        ) and
        .openclaw_rollout_result.immutableEvidence.signatureValid == true and
        .openclaw_rollout_result.immutableEvidence.encryptionKmsAlias ==
          "alias/teamagent-dev-openclaw-rollout-evidence" and
        .openclaw_rollout_result.immutableEvidence.signingKmsAlias ==
          "alias/teamagent-dev-openclaw-rollout-signing" and
        (.openclaw_rollout_result.immutableEvidence.encryptionKmsKeyArn |
          test("^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-f-]{36}$")) and
        (.openclaw_rollout_result.immutableEvidence.signingKmsKeyArn |
          test("^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-f-]{36}$")) and
        (.openclaw_rollout_result.immutableEvidence.resultSha256 |
          test("^[0-9a-f]{64}$")) and
        (.openclaw_rollout_result.immutableEvidence.signatureSha256 |
          test("^[0-9a-f]{64}$")) and
        .openclaw_rollout_result.immutableEvidence.exactVersionDownloadsVerified ==
          true
      )
    ) and
    (.bedrock_retention_live_sha256 | test("^[0-9a-f]{64}$")) and
    .bedrock_retention_live.kind ==
      "teamagent-bedrock-retention-live-evidence" and
    .bedrock_retention_live.schema_version == 1 and
    (.bedrock_retention_live.contract.observed_at_epoch <=
      $retention[0].contract.observed_at_epoch) and
    ((.bedrock_retention_live |
      del(.contract.observed_at_epoch, .contract_sha256)) ==
      ($retention[0] |
        del(.contract.observed_at_epoch, .contract_sha256))) and
    .post_state_contract_sha256 == $state_sha and
    .post_state_ownership_sha256 ==
      $state[0].state.address_set_sha256 and
    .post_state_contract == $state[0] and
    .post_live_fingerprint_sha256 == $live_sha and
    ([.pre_live_contract, .post_live_contract] |
      all(.[];
        (keys | sort) == ["images","resources","rule_states"] and
        (.images | keys | sort) == ([
          "canary",
          "connect_web",
          "ingest",
          "mcp",
          "morning_digest",
          "openclaw",
          "tiktok_acquire",
          "x_buzz_worker"
        ] | sort) and
        (.rule_states | keys | sort) ==
          ["canary","ingest","morning"] and
        (.resources | type) == "array" and
        .resources == (.resources | sort_by(.consumer_id)) and
        ([.resources[].consumer_id] | unique | length) ==
          (.resources | length)
      )) and
    ([.pre_live_contract.resources[] | resource_identity]) ==
      ([.post_live_contract.resources[] | resource_identity]) and
    .pre_state_contract.task_revisions ==
      task_revisions(.pre_live_contract.resources) and
    .post_state_contract.task_revisions ==
      task_revisions(.post_live_contract.resources) and
    ([.pre_state_contract, .post_state_contract] |
      all(.[];
        (keys | sort) ==
          ["backend","imports","state","task_revisions"]
      )) and
    .pre_state_contract.backend == .post_state_contract.backend and
    .pre_state_contract.imports == .post_state_contract.imports and
    .pre_state_contract.state.lineage ==
      .post_state_contract.state.lineage and
    .pre_state_contract.state.address_count ==
      .post_state_contract.state.address_count and
    .pre_state_contract.state.address_set_sha256 ==
      .post_state_contract.state.address_set_sha256 and
    .pre_state_contract.state.serial <
      .post_state_contract.state.serial and
    .post_live_contract.images == {
      mcp: $live[0].taskdefs.mcp.image,
      connect_web: $live[0].taskdefs.connect_web.image,
      openclaw: $live[0].taskdefs.openclaw.image,
      canary: $live[0].taskdefs.canary.image,
      ingest: $live[0].taskdefs.ingest.image,
      morning_digest: $live[0].taskdefs.morning.image,
      x_buzz_worker: $live[0].taskdefs.x_buzz.image,
      tiktok_acquire: $live[0].taskdefs.tiktok.image
    } and
    (.post_live_contract | del(.resources)) == {
      images: {
        mcp: $live[0].taskdefs.mcp.image,
        connect_web: $live[0].taskdefs.connect_web.image,
        openclaw: $live[0].taskdefs.openclaw.image,
        canary: $live[0].taskdefs.canary.image,
        ingest: $live[0].taskdefs.ingest.image,
        morning_digest: $live[0].taskdefs.morning.image,
        x_buzz_worker: $live[0].taskdefs.x_buzz.image,
        tiktok_acquire: $live[0].taskdefs.tiktok.image
      },
      rule_states: {
        ingest: $live[0].rules.ingest.critical.state,
        morning: $live[0].rules.morning.critical.state,
        canary: $live[0].rules.canary.critical.state
      }
    } and
    .post_live_contract.rule_states == {
      ingest: $live[0].rules.ingest.critical.state,
      morning: $live[0].rules.morning.critical.state,
      canary: $live[0].rules.canary.critical.state
    } and
    all(.post_live_contract.resources[];
      current_binding(.consumer_id) as $current |
      .task_definition_arn == $current.task.arn and
      .image == $current.task.image and
      .activation.state == $current.state
    ) and
    .post_runtime_inventory_sha256 == $inventory_sha and
    (.shared_deployment_lock_receipt_sha256 |
      test("^[0-9a-f]{64}$")) and
    .shared_deployment_lock_receipt.record_id ==
      "lock#teamagent/terraform.tfstate" and
    .shared_deployment_lock_receipt.record_type ==
      "teamagent.image-release-apply-lock" and
    .shared_deployment_lock_receipt.state == "LOCKED" and
    .shared_deployment_lock_receipt.intent_id ==
      .image_deployment_intent_id and
    .shared_deployment_lock_receipt.apply_attempt_id ==
      .apply_attempt_id and
    .shared_deployment_lock_receipt.plan_sha256 == .plan_sha256 and
    (.provenance_outcome_receipt_sha256 |
      test("^[0-9a-f]{64}$")) and
    .provenance_outcome_receipt == {
      intent_id:.image_deployment_intent_id,
      plan_sha256:.plan_sha256,
      state:"APPLIED"
    } and
    (.applied_at_epoch | type) == "number" and
    (.applied_at_epoch | floor) == .applied_at_epoch and
    .applied_at_epoch <= $now and
    (.shared_deployment_lock_record_id ==
      "lock#teamagent/terraform.tfstate") and
    ($receipt_sha | test("^[0-9a-f]{64}$"))
  ' "$receipt" >/dev/null ||
    die "activation requires the exact successful runtime apply receipt/post-state/provenance chain"
}

verify_receipt() {
  local plan="$1"
  local receipt="$2"
  local receipt_plan_path="${3:-$plan}"
  local expected_media_status="${4:-READY}"
  local media_apply_attempt_id="${5:-}"
  ensure_tmp

  local stage="$TMP_ROOT/verify"
  mkdir -m 700 "$stage"
  local receipt_sha_before receipt_identity
  receipt_identity="$(stat_identity "$receipt")"
  receipt_sha_before="$(sha256_file "$receipt")"
  cp "$receipt" "$stage/receipt.json"
  chmod 600 "$stage/receipt.json"
  [ "$receipt_sha_before" = "$(sha256_file "$receipt")" ] ||
    die "receipt読取中の差替えを検出しました"
  [ "$receipt_identity" = "$(stat_identity "$receipt")" ] ||
    die "receipt読取中のpath差替えを検出しました"

  local config_manifest="$stage/config-manifest.txt"
  write_config_manifest "$config_manifest"
  jq -e --arg version "$GUARD_VERSION" --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" --arg project "$PROJECT" --arg environment "$ENVIRONMENT" \
    --arg git_commit "$(git_commit)" \
    --arg script_sha "$(sha256_file "$SCRIPT_PATH")" \
    --arg jq_sha "$(sha256_file "$GUARD_JQ")" \
    --arg manifest_sha "$(sha256_file "$MIGRATION_FILE")" \
    --arg config_sha "$(sha256_file "$config_manifest")" \
    --argjson now "$(date +%s)" '
    (keys | sort) == ([
      "account_id",
      "alarm_delivery_receipt_path",
      "alarm_delivery_receipt_sha256",
      "alarm_migration_receipt_path",
      "alarm_migration_receipt_sha256",
      "config_manifest_sha256",
      "created_at_epoch",
      "environment",
      "expires_at_epoch",
      "git_commit",
      "guard_jq_sha256",
      "guard_script_sha256",
      "guard_version",
      "hmac_transition_epoch",
      "hmac_transition_sha256",
      "image_deployment_intent_expires_at",
      "image_deployment_intent_id",
      "image_deployment_intent_receipt_sha256",
      "image_release_context_sha256",
      "images",
      "kind",
      "live_fingerprint_sha256",
      "log_readiness_receipt_path",
      "log_readiness_receipt_sha256",
      "media_cutover_receipt_path",
      "media_cutover_receipt_sha256",
      "migration_id",
      "migration_kind",
      "migration_contract_sha256",
      "migration_manifest_sha256",
      "mode",
      "plan_path",
      "plan_sha256",
      "preflight_receipt_path",
      "preflight_receipt_sha256",
      "prior_apply_receipt_path",
      "prior_apply_receipt_sha256",
      "project",
      "reviewed_plan_sha256",
      "receipt_path",
      "region",
      "rule_states",
      "runtime_inventory_sha256",
      "runtime_guard_sha256",
      "state_contract",
      "var_file",
      "var_file_sha256",
      "versioning_receipt_path",
      "versioning_receipt_sha256"
    ] | sort) and
    (.state_contract | keys | sort) ==
      ["backend","imports","state","task_revisions"] and
    (.state_contract.backend | keys | sort) ==
      ([
        "bucket",
        "dynamodb_table",
        "encrypt",
        "identity_sha256",
        "key",
        "region",
        "type",
        "workspace"
      ] | sort) and
    (.state_contract.state | keys | sort) ==
      ["address_count","address_set_sha256","lineage","serial"] and
    .kind == "terraform-runtime-plan-receipt" and
    .guard_version == $version and .account_id == $account and
    .region == $region and .project == $project and .environment == $environment and
    (.mode == "sync" or .mode == "migration") and
    .git_commit == $git_commit and
    .guard_script_sha256 == $script_sha and
    .guard_jq_sha256 == $jq_sha and
    .migration_manifest_sha256 == $manifest_sha and
    (
      if .mode == "migration" then
        (.migration_contract_sha256 | test("^[0-9a-f]{64}$")) and
        (.reviewed_plan_sha256 | test("^[0-9a-f]{64}$"))
      else
        .migration_contract_sha256 == "" and
        .reviewed_plan_sha256 == ""
      end
    ) and
    .config_manifest_sha256 == $config_sha and
    .created_at_epoch <= $now and .expires_at_epoch > $now and
    (.expires_at_epoch - .created_at_epoch) <= 3600 and
    (.image_deployment_intent_id |
      test("^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")) and
    (.image_deployment_intent_receipt_sha256 |
      test("^[0-9a-f]{64}$")) and
    (.image_release_context_sha256 | test("^[0-9a-f]{64}$")) and
    (.image_deployment_intent_expires_at | type) == "number" and
    .image_deployment_intent_expires_at > $now and
    (.runtime_inventory_sha256 | test("^[0-9a-f]{64}$")) and
    (.images | keys | sort) == ["consumers","desired","live"] and
    (.images.live | type == "object") and
    (.images.desired | type == "object") and
    (.images.consumers | keys | sort) == ["desired","live"] and
    (.images.consumers.live | type) == "object" and
    (.images.consumers.desired | type) == "object" and
    (.rule_states.live | type == "object") and
    (.rule_states.desired | type == "object") and
    (.images.live | keys | sort) == ["mcp","openclaw","tiktok","x_buzz"] and
    (.images.desired | keys | sort) == ["mcp","openclaw","tiktok","x_buzz"] and
    (.images.consumers.live | keys | sort) == ([
      "canary",
      "connect_web",
      "ingest",
      "mcp",
      "morning_digest",
      "openclaw",
      "tiktok_acquire",
      "x_buzz_worker"
    ] | sort) and
    (.images.consumers.desired | keys | sort) ==
      (.images.consumers.live | keys | sort) and
    (.images.consumers.live | to_entries | all(
      (.value | type) == "string" and
      (.value | split("@") | length) == 2 and
      (.value | split("@")[1] | test("^sha256:[0-9a-f]{64}$"))
    )) and
    (.images.consumers.desired | to_entries | all(
      (.value | type) == "string" and
      (.value | split("@") | length) == 2 and
      (.value | split("@")[1] | test("^sha256:[0-9a-f]{64}$"))
    )) and
    (.rule_states.live | keys | sort) == ["canary","ingest","morning"] and
    (.rule_states.desired | keys | sort) == ["canary","ingest","morning"] and
    (.rule_states.live |
      [values[]] | all(. == "ENABLED" or . == "DISABLED")) and
    (.rule_states.desired |
      [values[]] | all(type == "boolean")) and
    (.plan_sha256 | test("^[0-9a-f]{64}$")) and
    (.var_file_sha256 | test("^[0-9a-f]{64}$")) and
    (.live_fingerprint_sha256 | test("^[0-9a-f]{64}$")) and
    (.runtime_guard_sha256 | test("^[0-9a-f]{64}$")) and
    (.hmac_transition_sha256 | test("^[0-9a-f]{64}$")) and
    .state_contract.backend.type == "s3" and
    .state_contract.backend.bucket ==
      "teamagent-tfstate-718959508629" and
    .state_contract.backend.key == "teamagent/terraform.tfstate" and
    .state_contract.backend.region == "ap-northeast-1" and
    .state_contract.backend.dynamodb_table == "teamagent-tflock" and
    .state_contract.backend.encrypt == true and
    .state_contract.backend.workspace == "default" and
    (.state_contract.backend.identity_sha256 |
      test("^[0-9a-f]{64}$")) and
    (.state_contract.state.lineage |
      test("^[0-9a-fA-F-]{36}$")) and
    (.state_contract.state.serial | type) == "number" and
    .state_contract.state.serial >= 0 and
    (.state_contract.state.address_count | type) == "number" and
    .state_contract.state.address_count >= 0 and
    (.state_contract.state.address_set_sha256 |
      test("^[0-9a-f]{64}$")) and
    (.state_contract.task_revisions | type) == "object" and
    (.state_contract.task_revisions | to_entries | all(
      (.key | type) == "string" and
      (.key | length) > 0 and
      (.value | type) == "number" and
      .value >= 1 and
      (.value | floor) == .value
    )) and
    (.state_contract.imports | keys | sort) == ([
      "aws_cloudwatch_log_group.codebuild_aiia_image_builder",
      "aws_cloudwatch_log_group.codebuild_image",
      "aws_cloudwatch_log_group.ecs_containerinsights_teamagent",
      "aws_cloudwatch_log_group.ecs_containerinsights_tiktok",
      "aws_cloudwatch_log_group.reminder_notify",
      "aws_cloudwatch_log_group.tiktok_dispatch",
      "aws_cloudwatch_log_group.x_dispatch"
    ] | sort) and
    (.state_contract.imports | to_entries | all(
      (.value.expected_id | type) == "string" and
      (.value.expected_id | startswith("/aws/")) and
      (.value.present | type) == "boolean"
    )) and
    (
      if .mode == "sync" then
        .migration_id == "" and .migration_kind == "" and
        .preflight_receipt_path == "" and
        .preflight_receipt_sha256 == "" and
        .alarm_delivery_receipt_path == "" and
        .alarm_delivery_receipt_sha256 == "" and
        .versioning_receipt_path == "" and
        .versioning_receipt_sha256 == "" and
        .log_readiness_receipt_path == "" and
        .log_readiness_receipt_sha256 == "" and
        .alarm_migration_receipt_path == "" and
        .alarm_migration_receipt_sha256 == "" and
        .prior_apply_receipt_path == "" and
        .prior_apply_receipt_sha256 == "" and
        .media_cutover_receipt_path == "" and
        .media_cutover_receipt_sha256 == ""
      else
        (.migration_id | length) > 0 and
        (.migration_kind == "runtime" or .migration_kind == "activation") and
        (.preflight_receipt_sha256 | test("^[0-9a-f]{64}$")) and
        (.alarm_delivery_receipt_path | type) == "string" and
        (.alarm_delivery_receipt_path | length) > 0 and
        (.alarm_delivery_receipt_sha256 | test("^[0-9a-f]{64}$")) and
        (.versioning_receipt_path | type) == "string" and
        (.versioning_receipt_path | length) > 0 and
        (.versioning_receipt_sha256 | test("^[0-9a-f]{64}$")) and
        (.log_readiness_receipt_path | type) == "string" and
        (.log_readiness_receipt_path | length) > 0 and
        (.log_readiness_receipt_sha256 | test("^[0-9a-f]{64}$")) and
        (.alarm_migration_receipt_path | type) == "string" and
        (.alarm_migration_receipt_path | length) > 0 and
        (.alarm_migration_receipt_sha256 | test("^[0-9a-f]{64}$")) and
        (
          (
            .media_cutover_receipt_path == "" and
            .media_cutover_receipt_sha256 == ""
          ) or
          (
            (.media_cutover_receipt_path | type) == "string" and
            (.media_cutover_receipt_path | length) > 0 and
            (.media_cutover_receipt_sha256 |
              test("^[0-9a-f]{64}$"))
          )
        ) and
        (
          if .migration_kind == "activation" then
            (.prior_apply_receipt_path | type) == "string" and
            (.prior_apply_receipt_path | length) > 0 and
            (.prior_apply_receipt_sha256 | test("^[0-9a-f]{64}$"))
          else
            .prior_apply_receipt_path == "" and
            .prior_apply_receipt_sha256 == ""
          end
        )
      end
    )
  ' "$stage/receipt.json" >/dev/null || die "receipt schema/bindingが不正です"

  local bound_plan bound_receipt var_file preflight_receipt alarm_delivery_receipt
  local versioning_receipt log_readiness_receipt alarm_migration_receipt
  local prior_apply_receipt media_cutover_receipt
  local versioning_cutover_contract_sha256=""
  local alarm_delivery_receipt_identity=""
  bound_plan="$(jq -er '.plan_path' "$stage/receipt.json")"
  bound_receipt="$(jq -er '.receipt_path' "$stage/receipt.json")"
  var_file="$(jq -er '.var_file' "$stage/receipt.json")"
  [ "$bound_plan" = "$receipt_plan_path" ] ||
    die "receiptが別plan pathに束縛されています"
  [ "$bound_receipt" = "$receipt" ] || die "receipt path束縛が一致しません"
  var_file="$(secure_existing_file "$var_file")"
  [ "$var_file" = "$(jq -er '.var_file' "$stage/receipt.json")" ] || die "var-file path束縛が一致しません"
  preflight_receipt="$(jq -r '.preflight_receipt_path' "$stage/receipt.json")"
  if [ -n "$preflight_receipt" ]; then
    preflight_receipt="$(secure_existing_file "$preflight_receipt" 600)"
    [ "$(sha256_file "$preflight_receipt")" = \
      "$(jq -er '.preflight_receipt_sha256' "$stage/receipt.json")" ] ||
      die "preflight receipt SHA256が不一致です"
  fi
  alarm_delivery_receipt="$(jq -r '.alarm_delivery_receipt_path' \
    "$stage/receipt.json")"
  if [ -n "$alarm_delivery_receipt" ]; then
    alarm_delivery_receipt="$(secure_existing_file \
      "$alarm_delivery_receipt" 600)"
    [ "$(sha256_file "$alarm_delivery_receipt")" = \
      "$(jq -er '.alarm_delivery_receipt_sha256' "$stage/receipt.json")" ] ||
      die "alarm delivery receipt SHA256が不一致です"
    alarm_delivery_receipt_identity="$(stat_identity "$alarm_delivery_receipt")"
  fi
  versioning_receipt="$(jq -r '.versioning_receipt_path' \
    "$stage/receipt.json")"
  local versioning_receipt_identity=""
  if [ -n "$versioning_receipt" ]; then
    versioning_receipt="$(secure_existing_file \
      "$versioning_receipt" 600)"
    [ "$(sha256_file "$versioning_receipt")" = \
      "$(jq -er '.versioning_receipt_sha256' "$stage/receipt.json")" ] ||
      die "versioning receipt SHA256が不一致です"
    versioning_receipt_identity="$(stat_identity "$versioning_receipt")"
    versioning_cutover_contract_sha256="$(
      jq -er '.workflow_sha256' "$versioning_receipt"
    )"
  fi
  log_readiness_receipt="$(jq -r '.log_readiness_receipt_path' \
    "$stage/receipt.json")"
  local log_readiness_receipt_identity=""
  if [ -n "$log_readiness_receipt" ]; then
    log_readiness_receipt="$(secure_existing_file \
      "$log_readiness_receipt" 600)"
    [ "$(sha256_file "$log_readiness_receipt")" = \
      "$(jq -er '.log_readiness_receipt_sha256' "$stage/receipt.json")" ] ||
      die "log readiness receipt SHA256が不一致です"
    log_readiness_receipt_identity="$(stat_identity "$log_readiness_receipt")"
  fi
  alarm_migration_receipt="$(jq -r '.alarm_migration_receipt_path' \
    "$stage/receipt.json")"
  local alarm_migration_receipt_identity=""
  if [ -n "$alarm_migration_receipt" ]; then
    alarm_migration_receipt="$(secure_existing_file \
      "$alarm_migration_receipt" 600)"
    [ "$(sha256_file "$alarm_migration_receipt")" = \
      "$(jq -er '.alarm_migration_receipt_sha256' "$stage/receipt.json")" ] ||
      die "alarm migration receipt SHA256が不一致です"
    alarm_migration_receipt_identity="$(stat_identity \
      "$alarm_migration_receipt")"
  fi
  prior_apply_receipt="$(jq -r '.prior_apply_receipt_path' \
    "$stage/receipt.json")"
  local prior_apply_receipt_identity=""
  if [ -n "$prior_apply_receipt" ]; then
    prior_apply_receipt="$(secure_existing_file \
      "$prior_apply_receipt" 600)"
    [ "$(sha256_file "$prior_apply_receipt")" = \
      "$(jq -er '.prior_apply_receipt_sha256' "$stage/receipt.json")" ] ||
      die "prior apply receipt SHA256が不一致です"
    prior_apply_receipt_identity="$(stat_identity "$prior_apply_receipt")"
  fi
  media_cutover_receipt="$(jq -r '.media_cutover_receipt_path' \
    "$stage/receipt.json")"
  local media_cutover_receipt_identity=""
  if [ -n "$media_cutover_receipt" ]; then
    media_cutover_receipt="$(secure_existing_file \
      "$media_cutover_receipt" 600)"
    [ "$(sha256_file "$media_cutover_receipt")" = \
      "$(jq -er '.media_cutover_receipt_sha256' "$stage/receipt.json")" ] ||
      die "media cutover receipt SHA256が不一致です"
    media_cutover_receipt_identity="$(
      stat_identity "$media_cutover_receipt"
    )"
  fi

  local plan_sha_before var_sha_before plan_identity var_identity
  plan_identity="$(stat_identity "$plan")"
  var_identity="$(stat_identity "$var_file")"
  plan_sha_before="$(sha256_file "$plan")"
  var_sha_before="$(sha256_file "$var_file")"
  [ "$plan_sha_before" = "$(jq -er '.plan_sha256' "$stage/receipt.json")" ] || die "plan SHA256がreceiptと不一致です"
  [ "$var_sha_before" = "$(jq -er '.var_file_sha256' "$stage/receipt.json")" ] || die "var-file SHA256がreceiptと不一致です"
  if [ "$plan" = "$receipt_plan_path" ]; then
    cp "$plan" "$stage/plan.tfplan"
  else
    # apply pathはO_NOFOLLOWで作ったprivate staged inodeをhard-linkし、
    # show/context/heartbeat/supervisor/provisionerが同じplanを使う。
    ln "$plan" "$stage/plan.tfplan" ||
      die "private staged plan inodeをverify pathへ固定できません"
  fi
  cp "$var_file" "$stage/terraform.tfvars"
  chmod 600 "$stage/plan.tfplan" "$stage/terraform.tfvars"
  [ "$(sha256_file "$stage/plan.tfplan")" = "$plan_sha_before" ] || die "private plan copyが不一致です"
  [ "$(sha256_file "$stage/terraform.tfvars")" = "$var_sha_before" ] || die "private var-file copyが不一致です"
  [ "$plan_identity" = "$(stat_identity "$plan")" ] || die "plan読取中のpath差替えを検出しました"
  [ "$var_identity" = "$(stat_identity "$var_file")" ] || die "var-file読取中のpath差替えを検出しました"

  local mode migration_id expected_live_sha transition_epoch
  mode="$(jq -er '.mode' "$stage/receipt.json")"
  migration_id="$(jq -r '.migration_id' "$stage/receipt.json")"
  expected_live_sha="$(jq -er '.live_fingerprint_sha256' "$stage/receipt.json")"
  transition_epoch="$(jq -er '.hmac_transition_epoch' "$stage/receipt.json")"

  capture_state_contract "$stage/state-before.json"
  jq -e --slurpfile receipt "$stage/receipt.json" '
    . == ($receipt[0].state_contract | del(.task_revisions))
  ' "$stage/state-before.json" >/dev/null ||
    die "backend/workspace/state lineage/serial/address ownershipがreceiptから変化しました"
  snapshot_live "$stage/live-before.json"
  capture_complete_runtime_inventory "$stage/inventory-before.json"
  [ "$(sha256_file "$stage/live-before.json")" = "$expected_live_sha" ] ||
    die "plan作成後にlive runtimeが変化しました"
  [ "$(jq -er '.inventory_sha256' "$stage/inventory-before.json")" = \
    "$(jq -er '.runtime_inventory_sha256' "$stage/receipt.json")" ] ||
    die "plan作成後にall-page runtime/SNS publisher inventoryが変化しました"
  jq -e --slurpfile receipt "$stage/receipt.json" '
    {
      openclaw: .taskdefs.openclaw.image,
      mcp: .taskdefs.mcp.image,
      x_buzz: .taskdefs.x_buzz.image,
      tiktok: .taskdefs.tiktok.image
    } == $receipt[0].images.live and
    {
      mcp:.taskdefs.mcp.image,
      connect_web:.taskdefs.connect_web.image,
      openclaw:.taskdefs.openclaw.image,
      canary:.taskdefs.canary.image,
      ingest:.taskdefs.ingest.image,
      morning_digest:.taskdefs.morning.image,
      x_buzz_worker:.taskdefs.x_buzz.image,
      tiktok_acquire:.taskdefs.tiktok.image
    } == $receipt[0].images.consumers.live and
    {
      ingest: .rules.ingest.critical.state,
      morning: .rules.morning.critical.state,
      canary: .rules.canary.critical.state
    } == $receipt[0].rule_states.live
  ' "$stage/live-before.json" >/dev/null ||
    die "receiptのlive image/EventBridge state束縛が現在liveと一致しません"
  local media_plan_sha256=""
  if [ "$expected_media_status" = "CONSUMED" ]; then
    media_plan_sha256="$plan_sha_before"
  fi
  validate_media_envelope_cutover_gate \
    "$stage/live-before.json" \
    "$(jq -er '.images.desired.tiktok' "$stage/receipt.json")" \
    "$media_cutover_receipt" \
    "$(jq -er '.image_deployment_intent_id' "$stage/receipt.json")" \
    "$(jq -r '.migration_contract_sha256' "$stage/receipt.json")" \
    "$(jq -r '.reviewed_plan_sha256' "$stage/receipt.json")" \
    "$expected_media_status" "$media_apply_attempt_id" \
    "$media_plan_sha256"
  if [ -n "$alarm_delivery_receipt" ]; then
    verify_alarm_delivery_test_receipt \
      "$alarm_delivery_receipt" "$stage/live-before.json"
  fi
  if [ -n "$versioning_receipt" ]; then
    verify_versioning_attestation_receipt \
      "$versioning_receipt" "$stage/live-before.json" \
      "$stage/state-before.json"
  fi
  if [ -n "$log_readiness_receipt" ]; then
    verify_log_readiness_receipt \
      "$log_readiness_receipt" "$versioning_receipt" \
      "$stage/live-before.json"
  fi
  if [ -n "$alarm_migration_receipt" ]; then
    verify_alarm_migration_final_receipt "$alarm_migration_receipt"
  fi

  local migration_file=""
  if [ "$mode" = "migration" ]; then
    migration_file="$stage/migration.json"
    migration_to_file "$migration_id" "$migration_file"
    [ "$(normalized_migration_manifest_sha256 "$migration_id")" = "$(
      jq -er '.migration_contract_sha256' "$stage/receipt.json"
    )" ] ||
      die "migration normalized contractがplan receiptと一致しません"
    [ "$(jq -cS '.reviewed_plan' "$migration_file" | sha256_text)" = "$(
      jq -er '.reviewed_plan_sha256' "$stage/receipt.json"
    )" ] ||
      die "reviewed plan hashがplan receiptと一致しません"
    validate_migration_source "$stage/live-before.json" "$migration_file"
    if [ "$mode" = "migration" ]; then
      verify_preflight_receipt \
        "$preflight_receipt" "$migration_id" "$migration_file" \
        "$stage/live-before.json"
    fi
    if [ "$(jq -er '.kind' "$migration_file")" = "activation" ]; then
      verify_required_migration_apply_receipt \
        "$prior_apply_receipt" \
        "$(jq -er '.requires_migration' "$migration_file")" \
        "$stage/live-before.json" "$stage/state-before.json"
    fi
  fi

  core_from_snapshot \
    "$stage/live-before.json" "$stage/core.json" "$mode" "$migration_id" \
    "$(jq -er '.images.desired.openclaw' "$stage/receipt.json")" \
    "$(jq -er '.images.desired.mcp' "$stage/receipt.json")" \
    "$(jq -er '.images.desired.x_buzz' "$stage/receipt.json")" \
    "$(jq -er '.images.desired.tiktok' "$stage/receipt.json")" \
    "$(jq -er '.images.consumers.desired.connect_web' \
      "$stage/receipt.json")" \
    "$(jq -er '.images.consumers.desired.ingest' "$stage/receipt.json")" \
    "$(jq -er '.images.consumers.desired.morning_digest' \
      "$stage/receipt.json")" \
    "$(jq -er '.images.consumers.desired.canary' "$stage/receipt.json")" \
    "$(jq -r '.preflight_receipt_sha256' "$stage/receipt.json")" \
    "$transition_epoch" \
    "$(jq -er '.rule_states.desired.ingest' "$stage/receipt.json")" \
    "$(jq -er '.rule_states.desired.morning' "$stage/receipt.json")" \
    "$(jq -er '.rule_states.desired.canary' "$stage/receipt.json")" \
    "$(jq -r '.versioning_receipt_sha256' "$stage/receipt.json")" \
    "$versioning_cutover_contract_sha256" \
    "$(
      if [ -n "$prior_apply_receipt" ]; then
        jq -er '.migration_id' "$prior_apply_receipt"
      fi
    )" \
    "$(jq -r '.prior_apply_receipt_sha256' "$stage/receipt.json")"
  [ "$(sha256_file "$stage/core.json")" = "$(jq -er '.runtime_guard_sha256' "$stage/receipt.json")" ] ||
    die "runtime_guard_live束縛がreceiptと一致しません"

  terraform -chdir="$TF_DIR" show -json "$stage/plan.tfplan" > "$stage/plan.json"
  [ "$(sha256_file "$stage/plan.tfplan")" = "$plan_sha_before" ] || die "plan検証中のprivate copy改ざんを検出しました"
  hmac_from_plan "$stage/plan.json" "$stage/proposed-hmac.json"
  validate_hmac_transition_metadata \
    "$stage/live-before.json" "$stage/proposed-hmac.json" "$mode" \
    "$transition_epoch" "$stage/hmac-transition-bound.json"
  [ "$(sha256_file "$stage/hmac-transition-bound.json")" = \
    "$(jq -er '.hmac_transition_sha256' "$stage/receipt.json")" ] ||
    die "receiptのHMAC transition束縛がplanと一致しません"
  # Receipt発行後にissuer切替窓やprevious削除期限を跨いだplanは、
  # 発行時に有効でもapply直前には拒否する。
  validate_hmac_transition_metadata \
    "$stage/live-before.json" "$stage/proposed-hmac.json" "$mode" \
    "$(date +%s)" "$stage/hmac-transition-now.json"
  validate_hmac_secret_metadata "$stage/proposed-hmac.json"
  validate_plan \
    "$stage/plan.json" "$stage/live-before.json" "$stage/core.json" \
    "$(jq -er '.images.desired.mcp' "$stage/receipt.json")" \
    "$migration_file" "$stage/proposed-hmac.json" "$stage/state-before.json"
  [ "$(jq -er '.variables.image_deployment_intent_id.value' \
      "$stage/plan.json")" = \
    "$(jq -er '.image_deployment_intent_id' "$stage/receipt.json")" ] ||
    die "saved plan/receiptのone-use production intentが不一致です"
  capture_image_release_context \
    "$stage/plan.tfplan" "$stage/image-release-context.json"
  [ "$(sha256_file "$stage/image-release-context.json")" = \
    "$(jq -er '.image_release_context_sha256' "$stage/receipt.json")" ] ||
    die "saved planのbackend/workspace/state ownership contextが変化しました"
  build_scoped_release_live_contract \
    "$stage/image-release-context.json" \
    "$stage/live-before.json" \
    "$stage/plan-live-contract.json"
  validate_image_release_context_consumer_images \
    "$stage/image-release-context.json" \
    "$(jq -c '.images.consumers.desired' "$stage/receipt.json")"
  build_scoped_release_state_contract \
    "$stage/state-before.json" \
    "$stage/plan-live-contract.json" \
    "$stage/plan-state-contract.json"
  jq -e --slurpfile receipt "$stage/receipt.json" '
    . == $receipt[0].state_contract
  ' "$stage/plan-state-contract.json" >/dev/null ||
    die "saved planのscope内task definition revisionがreceiptから変化しました"
  [ "$(sha256_file "$stage/plan.tfplan")" = "$plan_sha_before" ] || die "plan検証後のprivate copy改ざんを検出しました"

  snapshot_live "$stage/live-after.json"
  validate_media_envelope_cutover_gate \
    "$stage/live-after.json" \
    "$(jq -er '.images.desired.tiktok' "$stage/receipt.json")" \
    "$media_cutover_receipt" \
    "$(jq -er '.image_deployment_intent_id' "$stage/receipt.json")" \
    "$(jq -r '.migration_contract_sha256' "$stage/receipt.json")" \
    "$(jq -r '.reviewed_plan_sha256' "$stage/receipt.json")" \
    "$expected_media_status" "$media_apply_attempt_id" \
    "$media_plan_sha256"
  capture_state_contract "$stage/state-after.json"
  capture_complete_runtime_inventory "$stage/inventory-after.json"
  [ "$(sha256_file "$stage/live-after.json")" = "$expected_live_sha" ] ||
    die "verify中にlive runtimeが変化しました"
  [ "$(sha256_file "$stage/state-after.json")" = \
    "$(sha256_file "$stage/state-before.json")" ] ||
    die "verify中にbackend/workspace/state lineage/serial/address ownershipが変化しました"
  build_scoped_release_state_contract \
    "$stage/state-after.json" \
    "$stage/plan-live-contract.json" \
    "$stage/final-state-contract.json"
  cmp -s \
    "$stage/plan-state-contract.json" \
    "$stage/final-state-contract.json" ||
    die "verify中にscope内Terraform state task definition revisionが変化しました"
  cmp -s "$stage/inventory-before.json" "$stage/inventory-after.json" ||
    die "verify中にall-page runtime/SNS publisher inventoryが変化しました"
  [ "$(sha256_file "$plan")" = "$plan_sha_before" ] || die "verify中にplan pathが変化しました"
  [ "$(sha256_file "$receipt")" = "$receipt_sha_before" ] || die "verify中にreceipt pathが変化しました"
  [ "$(sha256_file "$var_file")" = "$var_sha_before" ] || die "verify中にvar-fileが変化しました"
  [ "$plan_identity" = "$(stat_identity "$plan")" ] || die "verify中にplan pathが差替えられました"
  [ "$receipt_identity" = "$(stat_identity "$receipt")" ] || die "verify中にreceipt pathが差替えられました"
  [ "$var_identity" = "$(stat_identity "$var_file")" ] || die "verify中にvar-file pathが差替えられました"
  if [ -n "$alarm_delivery_receipt" ]; then
    [ "$(sha256_file "$alarm_delivery_receipt")" = \
      "$(jq -er '.alarm_delivery_receipt_sha256' "$stage/receipt.json")" ] ||
      die "verify中にalarm delivery receiptが変化しました"
    [ "$(stat_identity "$alarm_delivery_receipt")" = \
      "$alarm_delivery_receipt_identity" ] ||
      die "verify中にalarm delivery receipt pathが差替えられました"
  fi
  if [ -n "$versioning_receipt" ]; then
    [ "$(sha256_file "$versioning_receipt")" = \
      "$(jq -er '.versioning_receipt_sha256' "$stage/receipt.json")" ] ||
      die "verify中にversioning receiptが変化しました"
    [ "$(stat_identity "$versioning_receipt")" = \
      "$versioning_receipt_identity" ] ||
      die "verify中にversioning receipt pathが差替えられました"
  fi
  if [ -n "$log_readiness_receipt" ]; then
    [ "$(sha256_file "$log_readiness_receipt")" = \
      "$(jq -er '.log_readiness_receipt_sha256' "$stage/receipt.json")" ] ||
      die "verify中にlog readiness receiptが変化しました"
    [ "$(stat_identity "$log_readiness_receipt")" = \
      "$log_readiness_receipt_identity" ] ||
      die "verify中にlog readiness receipt pathが差替えられました"
  fi
  if [ -n "$alarm_migration_receipt" ]; then
    [ "$(sha256_file "$alarm_migration_receipt")" = \
      "$(jq -er '.alarm_migration_receipt_sha256' \
        "$stage/receipt.json")" ] ||
      die "verify中にalarm migration receiptが変化しました"
    [ "$(stat_identity "$alarm_migration_receipt")" = \
      "$alarm_migration_receipt_identity" ] ||
      die "verify中にalarm migration receipt pathが差替えられました"
  fi
  if [ -n "$prior_apply_receipt" ]; then
    [ "$(sha256_file "$prior_apply_receipt")" = \
      "$(jq -er '.prior_apply_receipt_sha256' "$stage/receipt.json")" ] ||
      die "verify中にprior apply receiptが変化しました"
    [ "$(stat_identity "$prior_apply_receipt")" = \
      "$prior_apply_receipt_identity" ] ||
      die "verify中にprior apply receipt pathが差替えられました"
    verify_required_migration_apply_receipt \
      "$prior_apply_receipt" \
      "$(jq -er '.requires_migration' "$migration_file")" \
      "$stage/live-after.json" "$stage/state-after.json"
  fi
  if [ -n "$media_cutover_receipt" ]; then
    [ "$(sha256_file "$media_cutover_receipt")" = \
      "$(jq -er '.media_cutover_receipt_sha256' "$stage/receipt.json")" ] ||
      die "verify中にmedia cutover receiptが変化しました"
    [ "$(stat_identity "$media_cutover_receipt")" = \
      "$media_cutover_receipt_identity" ] ||
      die "verify中にmedia cutover receipt pathが差替えられました"
  fi
  # Re-run the content validators against the final live/state observations.
  # apply invokes verify_receipt while holding the deployment lock, so these
  # are the final evidence/backend/lifecycle/SNS checks before Terraform uses
  # the private saved-plan copy.
  if [ -n "$alarm_delivery_receipt" ]; then
    verify_alarm_delivery_test_receipt \
      "$alarm_delivery_receipt" "$stage/live-after.json"
  fi
  if [ -n "$versioning_receipt" ]; then
    verify_versioning_attestation_receipt \
      "$versioning_receipt" "$stage/live-after.json" \
      "$stage/state-after.json"
  fi
  if [ -n "$log_readiness_receipt" ]; then
    verify_log_readiness_receipt \
      "$log_readiness_receipt" "$versioning_receipt" \
      "$stage/live-after.json"
  fi
  if [ -n "$alarm_migration_receipt" ]; then
    verify_alarm_migration_final_receipt "$alarm_migration_receipt"
  fi
}


# ── Freeze v2: desired-state binding（Terraform 変数の機械注入）──────────────
# activation_freeze_policy.tf の var.activation_freeze_enabled は既定 false。
# Freeze が ACTIVE な間にこれを注入し忘れると、次の full plan で freeze の 11
# リソース（policy + attachment 10）が **destroy 候補**になり、freeze 自体が
# 巻き戻る。2026-08-24 ユーザー裁定に従い、宣言の state を単一の真実源として
# 変数を機械束縛する:
#
#   activation_freeze.json.state == "active"  →  activation_freeze_enabled=true
#
# 宣言が読めない / state が未知 / true を注入できない場合はいずれも FATAL
# （fail-open で freeze を溶かさない）。normal plan と adopt-plan は共有の
# build_live_injection_args を通るので、ここ 1 箇所で両経路を守る。IAM targeted
# plan は guard を通らないため runbook が同じ変数の明示を要求する。
FREEZE_DECLARATION="$REPO_ROOT/infra/deploy/activation_freeze.json"
FREEZE_CHECKER="$REPO_ROOT/infra/deploy/activation_freeze_check.py"
FREEZE_DESIRED_ENABLED=""

freeze_desired_state_binding() {
  [ -f "$FREEZE_DECLARATION" ] ||
    die "freeze 宣言がありません: $FREEZE_DECLARATION"
  [ -f "$FREEZE_CHECKER" ] ||
    die "freeze checker がありません: $FREEZE_CHECKER"
  local state
  state="$(python3 "$FREEZE_CHECKER" --freeze "$FREEZE_DECLARATION" desired-var)" ||
    die "freeze desired-state の判定に失敗しました（fail-closed）"
  case "$state" in
    true|false) FREEZE_DESIRED_ENABLED="$state" ;;
    *) die "freeze desired-state の判定結果が不正です: $state" ;;
  esac
}

# ── PR2-A0.3.2: live snapshot 注入の共有経路 ─────────────────────────────────
# 通常 guarded plan と adopt-plan が「live snapshot → CORE_JSON → live-derived vars →
# terraform への注入」を同一コードで行うための唯一の実装。第二実装は禁止で、
# `-var=runtime_guard_live=` の構築はこのファイル内で build_live_injection_args だけが
# 持つ（契約テストが出現回数 1 を固定している）。共有コードを消すと両経路が同時に壊れる。
#
# sync_live_world_from_snapshot: live snapshot から LIVE_* / DESIRED_*（sync では
# desired == live）/ rule 状態 / epoch / intent id を導出して globals へ置く。
# migration 経路はこの直後に DESIRED_* を上書きする（通常経路のみ）。
sync_live_world_from_snapshot() {
  local live="$1"
  LIVE_OPENCLAW_IMAGE="$(jq -er '.taskdefs.openclaw.image' "$live")"
  LIVE_MCP_IMAGE="$(jq -er '.taskdefs.mcp.image' "$live")"
  LIVE_X_IMAGE="$(jq -er '.taskdefs.x_buzz.image' "$live")"
  LIVE_TIKTOK_IMAGE="$(jq -er '.taskdefs.tiktok.image' "$live")"
  LIVE_RULE_STATES="$(jq -c '{
    ingest:.rules.ingest.critical.state,
    morning:.rules.morning.critical.state,
    canary:.rules.canary.critical.state
  }' "$live")"

  MODE="sync"
  MIGRATION_KIND=""
  MIGRATION_JSON=""
  MIGRATION_CONTRACT_SHA256=""
  REVIEWED_PLAN_SHA256=""
  PREFLIGHT_SHA256=""
  REQUIRED_MIGRATION_ID=""
  REQUIRED_MIGRATION_APPLY_RECEIPT_SHA256=""
  DESIRED_OPENCLAW_IMAGE="$LIVE_OPENCLAW_IMAGE"
  DESIRED_MCP_IMAGE="$LIVE_MCP_IMAGE"
  DESIRED_X_IMAGE="$LIVE_X_IMAGE"
  DESIRED_TIKTOK_IMAGE="$LIVE_TIKTOK_IMAGE"
  DESIRED_CONNECT_WEB_IMAGE="$(
    jq -er '.taskdefs.connect_web.image' "$live"
  )"
  DESIRED_INGEST_IMAGE="$(
    jq -er '.taskdefs.ingest.image' "$live"
  )"
  DESIRED_MORNING_DIGEST_IMAGE="$(
    jq -er '.taskdefs.morning.image' "$live"
  )"
  DESIRED_CANARY_IMAGE="$(
    jq -er '.taskdefs.canary.image' "$live"
  )"
  DESIRED_INGEST_RULE="$(jq -r '.ingest == "ENABLED"' <<< "$LIVE_RULE_STATES")"
  DESIRED_MORNING_RULE="$(jq -r '.morning == "ENABLED"' <<< "$LIVE_RULE_STATES")"
  DESIRED_CANARY_RULE="$(jq -r '.canary == "ENABLED"' <<< "$LIVE_RULE_STATES")"
  TRANSITION_EPOCH="$(date +%s)"
  IMAGE_DEPLOYMENT_INTENT_ID="$(new_uuid_v4)"
}

# build_live_injection_args: CORE_JSON と live-derived overlay を構築し、terraform へ
# 渡す注入引数列を LIVE_INJECTION_TF_ARGS へ置く。呼び出し前提は
# sync_live_world_from_snapshot（+ migration 経路なら DESIRED_* 上書き）済みであること。
# sync では overlay（consumer manifest + HMAC deployed 世代）も生成し、改竄検出用に
# SYNC_DERIVED_VAR_SHA256 / SYNC_DERIVED_VAR_IDENTITY を採取する（plan 後に呼び出し側が
# 再照合する）。
build_live_injection_args() {
  local live="$1" core_out="$2" state_full="$3" derived_dir="$4"
  ensure_tmp
  core_from_snapshot \
    "$live" "$core_out" "$MODE" "$MIGRATION_ID" \
    "$DESIRED_OPENCLAW_IMAGE" "$DESIRED_MCP_IMAGE" "$DESIRED_X_IMAGE" \
    "$DESIRED_TIKTOK_IMAGE" \
    "$DESIRED_CONNECT_WEB_IMAGE" "$DESIRED_INGEST_IMAGE" \
    "$DESIRED_MORNING_DIGEST_IMAGE" "$DESIRED_CANARY_IMAGE" \
    "$PREFLIGHT_SHA256" "$TRANSITION_EPOCH" \
    "$DESIRED_INGEST_RULE" "$DESIRED_MORNING_RULE" "$DESIRED_CANARY_RULE" \
    "$VERSIONING_RECEIPT_SHA256" "$LOG_CUTOVER_CONTRACT_SHA256" \
    "$REQUIRED_MIGRATION_ID" \
    "$REQUIRED_MIGRATION_APPLY_RECEIPT_SHA256"
  CORE_JSON="$(jq -c . "$core_out")"
  SYNC_DERIVED_VAR_FILE=""
  SYNC_DERIVED_VAR_SHA256=""
  SYNC_DERIVED_VAR_IDENTITY=""
  if [ "$MODE" = "sync" ]; then
    derive_live_hmac_terraform_inputs \
      "$live" "$TMP_ROOT/hmac-terraform-inputs.json"
    MAIL_HMAC_DEPLOYED_PRIMARY="$(
      jq -er '.mail_action_hmac_deployed_primary_generation' \
        "$TMP_ROOT/hmac-terraform-inputs.json"
    )"
    MAIL_HMAC_DEPLOYED_PREVIOUS="$(
      jq -r '.mail_action_hmac_deployed_previous_generation' \
        "$TMP_ROOT/hmac-terraform-inputs.json"
    )"
    MAIL_HMAC_DEPLOYED_T0="$(
      jq -r '.mail_action_hmac_deployed_rotation_started_at' \
        "$TMP_ROOT/hmac-terraform-inputs.json"
    )"
    REPORT_HMAC_DEPLOYED_PRIMARY="$(
      jq -er '.report_link_hmac_deployed_primary_generation' \
        "$TMP_ROOT/hmac-terraform-inputs.json"
    )"
    REPORT_HMAC_DEPLOYED_PREVIOUS="$(
      jq -r '.report_link_hmac_deployed_previous_generation' \
        "$TMP_ROOT/hmac-terraform-inputs.json"
    )"
    REPORT_HMAC_DEPLOYED_T0="$(
      jq -r '.report_link_hmac_deployed_rotation_started_at' \
        "$TMP_ROOT/hmac-terraform-inputs.json"
    )"
    build_sync_image_deployment_consumer_manifest \
      "$state_full" "$live" \
      "$core_out" \
      "$TMP_ROOT/sync-consumer-manifest.json"
    SYNC_DERIVED_VAR_FILE="$derived_dir/sync-derived.tfvars.json"
    jq -n -S \
      --slurpfile manifest "$TMP_ROOT/sync-consumer-manifest.json" \
      --arg mail_primary "$MAIL_HMAC_DEPLOYED_PRIMARY" \
      --arg mail_previous "$MAIL_HMAC_DEPLOYED_PREVIOUS" \
      --arg mail_t0 "$MAIL_HMAC_DEPLOYED_T0" \
      --arg report_primary "$REPORT_HMAC_DEPLOYED_PRIMARY" \
      --arg report_previous "$REPORT_HMAC_DEPLOYED_PREVIOUS" \
      --arg report_t0 "$REPORT_HMAC_DEPLOYED_T0" '
      {
        image_deployment_consumer_manifest:$manifest[0],
        image_release_receipt_catalog:{},
        image_release_consumer_receipt_bindings:{},
        mail_action_hmac_deployed_primary_generation:$mail_primary,
        mail_action_hmac_deployed_previous_generation:$mail_previous,
        mail_action_hmac_deployed_rotation_started_at:$mail_t0,
        report_link_hmac_deployed_primary_generation:$report_primary,
        report_link_hmac_deployed_previous_generation:$report_previous,
        report_link_hmac_deployed_rotation_started_at:$report_t0
      }
    ' > "$SYNC_DERIVED_VAR_FILE" ||
      die "sync用live-derived Terraform variable overlayを生成できません"
    chmod 600 "$SYNC_DERIVED_VAR_FILE"
    SYNC_DERIVED_VAR_SHA256="$(
      sha256_file "$SYNC_DERIVED_VAR_FILE"
    )"
    SYNC_DERIVED_VAR_IDENTITY="$(
      stat_identity "$SYNC_DERIVED_VAR_FILE"
    )"
  fi
  select_terraform_media_image_inputs \
    "$MODE" "$LIVE_TIKTOK_IMAGE" "$DESIRED_TIKTOK_IMAGE"
  freeze_desired_state_binding
  LIVE_INJECTION_TF_ARGS=(
    "-var=activation_freeze_enabled=$FREEZE_DESIRED_ENABLED"
    "-var=runtime_guard_live=$CORE_JSON"
    "-var=openclaw_image=$DESIRED_OPENCLAW_IMAGE"
    "-var=mcp_image=$DESIRED_MCP_IMAGE"
    "-var=x_buzz_image=$DESIRED_X_IMAGE"
    "-var=media_worker_image=$TF_MEDIA_WORKER_IMAGE"
    "-var=tiktok_acquire_image=$TF_TIKTOK_ACQUIRE_IMAGE"
    "-var=enable_media_worker=true"
    "-var=enable_tiktok_acquire=true"
    "-var=ingest_rule_enabled=$DESIRED_INGEST_RULE"
    "-var=morning_digest_rule_enabled=$DESIRED_MORNING_RULE"
    "-var=canary_rule_enabled=$DESIRED_CANARY_RULE"
    "-var=require_alarm_delivery=true"
    "-var=image_deployment_intent_id=$IMAGE_DEPLOYMENT_INTENT_ID"
    "-var=hmac_preflight_epoch_s=$TRANSITION_EPOCH"
  )
  if [ "$MODE" = "sync" ]; then
    LIVE_INJECTION_TF_ARGS+=(
      "-var-file=$SYNC_DERIVED_VAR_FILE"
    )
  fi
}


# ── PR2-A0: Supply-Chain Adopt（既存 sync / runtime migration / activation とは
# 完全に独立した経路）。adopt は「AWS 実体を一切変更せず Terraform state だけを実態へ
# 追いつかせる」操作で、許可範囲は sync より狭い。既存 3 経路の validator・allowlist には
# 一切関与しない。判定ロジックは infra/deploy/supply_chain_adopt_validate.py（fail-closed）と
# supply_chain_adopt_integrity.py（S3 実体の不変性検査）が持つ。
ADOPT_MAPPING="$REPO_ROOT/infra/deploy/supply_chain_adoptions.json"
ADOPT_VALIDATOR="$REPO_ROOT/infra/deploy/supply_chain_adopt_validate.py"
ADOPT_INTEGRITY="$REPO_ROOT/infra/deploy/supply_chain_adopt_integrity.py"
ADOPT_BINDING="$REPO_ROOT/infra/deploy/supply_chain_adopt_binding.py"
ADOPT_APPROVE_TOKEN="I-HAVE-REVIEWED-THE-ADOPT-PLAN"

adopt_require_helpers() {
  for adopt_path in "$ADOPT_MAPPING" "$ADOPT_VALIDATOR" "$ADOPT_INTEGRITY" "$ADOPT_BINDING"; do
    [ -f "$adopt_path" ] || die "adopt の必須ファイルがありません: $adopt_path"
  done
}

# 旧アドレスが state に存在し、新アドレスが未登録であることを確認する。
adopt_ownership_discovery() {
  local state_list="$1" address
  while IFS= read -r address; do
    grep -Fxq "$address" "$state_list" ||
      die "adopt ownership discovery 失敗: 旧アドレスが state にありません: $address"
  done < <(python3 -c 'import json,sys
for a in json.load(open(sys.argv[1]))["adoptions"]: print(a["old_address"])' "$ADOPT_MAPPING")
  while IFS= read -r address; do
    if grep -Fxq "$address" "$state_list"; then
      die "adopt ownership discovery 失敗: 新アドレスが既に state にあります: $address"
    fi
  done < <(python3 -c 'import json,sys
for a in json.load(open(sys.argv[1]))["adoptions"]: print(a["new_address"])' "$ADOPT_MAPPING")
}

# adopt の成果物は Terraform state 全文の backup・保存 plan・binding manifest を含む。
# これを repository 配下へ出力すると (1) untracked artifact が working tree を dirty にし、
# apply 時の binding 照合（git_tree_clean）が必ず失敗する (2) 機微な state を repository へ
# 持ち込む。運用の注意ではなく入口で機械的に拒否する。relative / ../ / symlink は
# realpath で正規化してから判定するので、repo 内へ戻る経路はすべて塞がる。
adopt_canonical_path() {
  python3 -c 'import os, sys
print(os.path.realpath(os.path.join(sys.argv[1], sys.argv[2])))' "$1" "$2"
}

adopt_assert_out_dir_outside_repo() {
  local out_dir="$1" canonical root resolved toplevel git_common
  [ -n "$out_dir" ] || die "adopt の --out が空です"
  canonical="$(adopt_canonical_path "$PWD" "$out_dir")" ||
    die "adopt の --out を正規化できませんでした: $out_dir"

  local -a forbidden_roots=()
  forbidden_roots+=("$REPO_ROOT")
  toplevel="$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -n "$toplevel" ]; then
    forbidden_roots+=("$toplevel")
  fi
  git_common="$(git -C "$REPO_ROOT" rev-parse --git-common-dir 2>/dev/null || true)"
  if [ -n "$git_common" ]; then
    forbidden_roots+=("$(adopt_canonical_path "$REPO_ROOT" "$git_common")")
  fi

  for root in "${forbidden_roots[@]}"; do
    [ -n "$root" ] || continue
    resolved="$(adopt_canonical_path "$PWD" "$root")" || continue
    if [ "$canonical" = "$resolved" ] || [ "${canonical#"$resolved"/}" != "$canonical" ]; then
      die "adopt の --out に repository 配下は指定できません: $out_dir
   解決後のパス: $canonical
   禁止領域    : $resolved
   理由: 生成物が working tree を dirty にし apply 時の binding 照合が必ず失敗します。
         また state-backup.json は Terraform state 全文（機微）を含みます。
   repository 外の安全なディレクトリを --out に指定してください。"
    fi
  done
}

# adopt は Terraform state を書き換える操作なので「誰が実行したか」を plan と apply で束縛する。
# account ID の一致だけでは principal の差し替えを検出できない。
#
# 認可は adopt 専用ロジックを作らず、既存の trusted identity verifier
# （assert_trusted_automation_identity）をそのまま入口に置く。canonical principal は
# その verifier が既に持っている TRUSTED_AUTOMATION_ARN（role session ARN）で、role の
# trust policy が sts:RoleSessionName を固定値で要求しているため（runtime_evidence.tf の
# runtime_automation_assume）、credential を取り直しても session ARN は変わらない。
# したがって plan と apply が別 session になっても canonical principal は一致する。
# 読み取った生の caller identity は out_dir へ audit evidence として残す。
adopt_trusted_principal_arn() {
  local identity_out="$1" arn
  # 後段の identity 検査は $TMP_ROOT へ evidence を書く。adopt 経路は他モードの
  # 前処理を通らないため、ここで必ず tmp を確保する（未確保だと / 直下への
  # 書き込みになり preflight で実測どおり即死する）。
  ensure_tmp
  arn="$(aws_cli sts get-caller-identity --query Arn --output text)" ||
    die "adopt: caller identity を取得できませんでした"
  [ -n "$arn" ] && [ "$arn" != "None" ] ||
    die "adopt: caller identity の ARN が空です"
  case "$arn" in
    arn:aws*:iam::*:root)
      die "root principal では adopt を実行できません: $arn
   root は全リソースへの実質無制限権限を持ち、一時 credential でもありません。
   infra/deploy/bootstrap_runtime_session.sh 経由の trusted automation session で実行してください。"
      ;;
  esac
  assert_trusted_automation_identity "$identity_out"
  jq -er '.Arn' "$identity_out" ||
    die "adopt: trusted identity から canonical principal を取り出せませんでした"
}

adopt_plan() {
  local var_file="$1" out_dir="$2" principal_arn
  adopt_require_helpers
  adopt_assert_out_dir_outside_repo "$out_dir"
  (umask 077 && mkdir -p "$out_dir") ||
    die "adopt の out ディレクトリを作成できませんでした: $out_dir"
  chmod 700 "$out_dir"
  principal_arn="$(adopt_trusted_principal_arn "$out_dir/identity-plan.json")"

  # plan した「世界」を manifest へ固定する。apply 時に全項目を exact match で再照合し、
  # 1 項目でも違えば FATAL にする（別 commit / 別 state / 改竄 plan での apply を封じる）。
  git -C "$REPO_ROOT" diff --quiet HEAD -- ||
    die "adopt-plan は clean tree でのみ実行できます（working tree に未コミット変更があります）"
  [ -z "$(git -C "$REPO_ROOT" ls-files --others --exclude-standard)" ] ||
    die "adopt-plan は clean tree でのみ実行できます（untracked file があります）"

  terraform -chdir="$TF_DIR" state pull > "$out_dir/state-backup.json"
  chmod 600 "$out_dir/state-backup.json"
  [ -s "$out_dir/state-backup.json" ] || die "adopt の state backup が空です"

  terraform -chdir="$TF_DIR" state list > "$out_dir/state-list.txt"
  chmod 600 "$out_dir/state-list.txt"
  adopt_ownership_discovery "$out_dir/state-list.txt"

  python3 "$ADOPT_INTEGRITY" snapshot --mapping "$ADOPT_MAPPING" \
    --out "$out_dir/integrity-before.json" ||
    die "adopt 前の S3 integrity snapshot に失敗しました"
  chmod 600 "$out_dir/integrity-before.json"

  # PR2-A0.3.2: 通常 guarded plan と同一の live snapshot / CORE_JSON / live-derived vars
  # 注入を共有実装から reuse する。注入なしの plan は runtime_guard_verified の前提
  # 17 項を評価できず、preflight を「純粋 forget+import」にできない。adopt 専用の
  # 第二実装は禁止（-var=runtime_guard_live= の構築は build_live_injection_args のみ）。
  # 通常経路の受領 receipt 検査・migration 分岐・media cutover gate は deployment
  # 承認の機構であり、AWS 実体変更ゼロが validator で強制される adopt では対象外。
  snapshot_live "$out_dir/live-before.json"
  chmod 600 "$out_dir/live-before.json"
  sync_live_world_from_snapshot "$out_dir/live-before.json"
  build_live_injection_args \
    "$out_dir/live-before.json" "$out_dir/adopt-core.json" \
    "$out_dir/state-backup.json" "$out_dir"
  chmod 600 "$out_dir/adopt-core.json"

  terraform -chdir="$TF_DIR" plan -input=false -lock-timeout=5m \
    "-var-file=$var_file" "${LIVE_INJECTION_TF_ARGS[@]}" \
    -out="$out_dir/adopt.tfplan" ||
    die "adopt の terraform plan に失敗しました"
  [ "$(sha256_file "$SYNC_DERIVED_VAR_FILE")" = "$SYNC_DERIVED_VAR_SHA256" ] &&
    [ "$(stat_identity "$SYNC_DERIVED_VAR_FILE")" = "$SYNC_DERIVED_VAR_IDENTITY" ] ||
    die "adopt plan 中に live-derived variable overlay が差替えられました"
  terraform -chdir="$TF_DIR" show -json "$out_dir/adopt.tfplan" > "$out_dir/adopt-plan.json"
  chmod 600 "$out_dir/adopt.tfplan" "$out_dir/adopt-plan.json"

  python3 "$ADOPT_VALIDATOR" --plan "$out_dir/adopt-plan.json" --mapping "$ADOPT_MAPPING" ||
    die "adopt plan が不変条件を満たしません（plan は破棄してください）"

  # 層2: validator は plan 内部の before/after 整合しか見ない。plan の before は Terraform が
  # 読んだ値なので、独立に採取した integrity snapshot と突き合わせて初めて
  # 「plan が live 実体そのものを宣言している」と言える。
  python3 "$ADOPT_INTEGRITY" crosscheck --snapshot "$out_dir/integrity-before.json" \
    --plan "$out_dir/adopt-plan.json" --mapping "$ADOPT_MAPPING" ||
    die "adopt plan が live の AWS 実体と一致しません（plan は破棄してください）"

  # Freeze v2 の enforcement 保全検査（validator / crosscheck の後）。
  python3 "$FREEZE_CHECKER" --freeze "$FREEZE_DECLARATION" \
    assert-plan-preserves-freeze --plan "$out_dir/adopt-plan.json" ||
    die "adopt plan が Freeze v2 の enforcement を壊します（plan は破棄してください）"

  # 通常経路と同じ TOCTOU 防止: plan が bind した live snapshot が plan 完了時点でも
  # そのままであることを要求する（plan 中の別デプロイは fail-closed）。
  snapshot_live "$out_dir/live-after.json"
  chmod 600 "$out_dir/live-after.json"
  [ "$(sha256_file "$out_dir/live-before.json")" = \
    "$(sha256_file "$out_dir/live-after.json")" ] ||
    die "adopt plan 作成中に live runtime が変化しました（plan は破棄してください）"

  python3 "$ADOPT_BINDING" record \
    --repo-root "$REPO_ROOT" --tf-dir "$TF_DIR" --out-dir "$out_dir" \
    --mapping "$ADOPT_MAPPING" --state "$out_dir/state-backup.json" \
    --account "$EXPECTED_ACCOUNT_ID" --principal-arn "$principal_arn" \
    --workspace "$EXPECTED_WORKSPACE" ||
    die "adopt plan binding manifest の作成に失敗しました"
  chmod 600 "$out_dir/adopt-binding.json"

  echo "✅ adopt plan 検証済み: $out_dir/adopt.tfplan"
  echo "   承認は plan SHA256 に束縛されます: $(python3 -c 'import json,sys
print(json.load(open(sys.argv[1]))["plan_sha256"])' "$out_dir/adopt-binding.json")"
}

adopt_apply() {
  local out_dir="$1" approve="$2" principal_arn
  adopt_require_helpers
  adopt_assert_out_dir_outside_repo "$out_dir"
  principal_arn="$(adopt_trusted_principal_arn "$out_dir/identity-apply.json")"
  for adopt_artifact in adopt.tfplan adopt-plan.json integrity-before.json \
    state-backup.json adopt-binding.json; do
    [ -f "$out_dir/$adopt_artifact" ] || die "adopt 成果物がありません: $out_dir/$adopt_artifact"
  done

  # plan した世界と apply する世界が完全一致することを要求する。
  # commit / tree / plan hash / mapping hash / state lineage+serial / account /
  # workspace / terraform version のいずれか 1 つでも違えば FATAL。
  python3 "$ADOPT_BINDING" verify \
    --repo-root "$REPO_ROOT" --tf-dir "$TF_DIR" --out-dir "$out_dir" \
    --mapping "$ADOPT_MAPPING" --account "$EXPECTED_ACCOUNT_ID" \
    --principal-arn "$principal_arn" \
    --workspace "$EXPECTED_WORKSPACE" --approve "$approve" ||
    die "adopt plan binding の再照合に失敗しました（apply は行いません）"

  python3 "$ADOPT_VALIDATOR" --plan "$out_dir/adopt-plan.json" --mapping "$ADOPT_MAPPING" ||
    die "adopt-apply 直前の検証に失敗しました"
  python3 "$ADOPT_INTEGRITY" snapshot --mapping "$ADOPT_MAPPING" \
    --out "$out_dir/integrity-preapply.json" ||
    die "adopt-apply 直前の S3 integrity snapshot に失敗しました"
  chmod 600 "$out_dir/integrity-preapply.json"
  python3 "$ADOPT_INTEGRITY" compare --before "$out_dir/integrity-before.json" \
    --after "$out_dir/integrity-preapply.json" ||
    die "adopt-apply 直前に S3 実体が変化しています"

  terraform -chdir="$TF_DIR" apply -input=false -lock-timeout=5m "$out_dir/adopt.tfplan" ||
    die "adopt の apply に失敗しました。state backup: $out_dir/state-backup.json"

  python3 "$ADOPT_INTEGRITY" snapshot --mapping "$ADOPT_MAPPING" \
    --out "$out_dir/integrity-after.json" ||
    die "adopt 後の S3 integrity snapshot に失敗しました"
  chmod 600 "$out_dir/integrity-after.json"
  python3 "$ADOPT_INTEGRITY" compare --before "$out_dir/integrity-before.json" \
    --after "$out_dir/integrity-after.json" ||
    die "adopt により AWS 実体が変化しました（activation failure）"

  echo "✅ adopt completed（AWS 実体の変更ゼロを前後比較で確認）"
}

# ── PR2-A0.4: Terraform state の同一アドレス rebind ──────────────────────────
# 本番は正しく（approved receipt == live）、state の binding だけが旧 revision を指す
# とき、live を再デプロイして state へ合わせるのは順序が逆。state の binding だけを
# live の exact revision へ付け替える。同一アドレスの rebind は removed/import ブロック
# では表現できないため、`state rm → 即 import` を guard 監督下の唯一の経路として
# 儀式化する（素の state 操作は引き続き禁止）。
# 正式契約: AWS managed application resources mutation = 0 / Terraform remote state mutation only
REBIND_MAPPING="$REPO_ROOT/infra/deploy/state_rebind_targets.json"
REBIND_HELPER="$REPO_ROOT/infra/deploy/state_rebind.py"

rebind_require_helpers() {
  for rebind_path in "$REBIND_MAPPING" "$REBIND_HELPER"; do
    [ -f "$rebind_path" ] || die "rebind の必須ファイルがありません: $rebind_path"
  done
}

# mapping の consumer 宣言に基づき、live の参照先が target_arn と一致することを確認する。
# これは検証であって再解決ではない（mapping の動的追随は禁止 — 不一致なら die）。
rebind_assert_consumer_points_at_target() {
  local kind="$1" name="$2" cluster="$3" target_arn="$4" live_arn=""
  case "$kind" in
    ecs-service)
      live_arn="$(aws_cli ecs describe-services --cluster "$cluster" --services "$name"         --query 'services[0].taskDefinition' --output text)" ||
        die "rebind: consumer service $name を describe できません"
      ;;
    events-rule)
      live_arn="$(aws_cli events list-targets-by-rule --rule "$name"         --query 'Targets[0].EcsParameters.TaskDefinitionArn' --output text)" ||
        die "rebind: rule $name の target を取得できません"
      ;;
    lambda-env)
      live_arn="$(aws_cli lambda get-function-configuration --function-name "$name"         --query 'Environment.Variables.TASKDEF_ARN' --output text)" ||
        die "rebind: lambda $name の TASKDEF_ARN を取得できません"
      ;;
    *) die "rebind: 未知の consumer kind: $kind" ;;
  esac
  [ "$live_arn" = "$target_arn" ] ||
    die "rebind: consumer $name の live 参照が mapping と一致しません
   live    : $live_arn
   mapping : $target_arn
   live が動いた場合は STALE MAPPING — freeze を確認し mapping を作り直すこと（動的追随は禁止）"
}

rebind_precheck() {
  local out_dir="$1" principal_arn
  rebind_require_helpers
  adopt_assert_out_dir_outside_repo "$out_dir"
  (umask 077 && mkdir -p "$out_dir") ||
    die "rebind の out ディレクトリを作成できませんでした: $out_dir"
  chmod 700 "$out_dir"
  principal_arn="$(adopt_trusted_principal_arn "$out_dir/identity-precheck.json")"

  git -C "$REPO_ROOT" diff --quiet HEAD -- ||
    die "rebind は clean tree でのみ実行できます（未コミット変更があります）"
  [ -z "$(git -C "$REPO_ROOT" ls-files --others --exclude-standard)" ] ||
    die "rebind は clean tree でのみ実行できます（untracked file があります）"

  python3 "$REBIND_HELPER" validate --mapping "$REBIND_MAPPING" --require-targets ||
    die "rebind mapping が不変条件を満たしません"

  terraform -chdir="$TF_DIR" state pull > "$out_dir/state-backup.json"
  chmod 600 "$out_dir/state-backup.json"
  [ -s "$out_dir/state-backup.json" ] || die "rebind の state backup が空です"
  terraform -chdir="$TF_DIR" state list > "$out_dir/state-list.txt"
  chmod 600 "$out_dir/state-list.txt"

  local address family target_arn kind name cluster current_arn
  while IFS=$'\t' read -r address family target_arn kind name cluster; do
    grep -Fxq "$address" "$out_dir/state-list.txt" ||
      die "rebind: $address が state にありません"
    base_address="${address%%\[*}"
    current_arn="$(jq -er --arg addr "$base_address" '
      .resources[] | select(.mode == "managed")
      | select((.type + "." + .name) == $addr)
      | .instances[0].attributes.arn
    ' "$out_dir/state-backup.json")" ||
      die "rebind: $address の現 state ARN を取得できません"
    [ "$current_arn" != "$target_arn" ] ||
      die "rebind: $address は既に $target_arn へ束縛済みです（mapping から除外すること）"
    aws_cli ecs describe-task-definition --task-definition "$target_arn"       --query 'taskDefinition.status' --output text | grep -qx "ACTIVE" ||
      die "rebind: $target_arn が ACTIVE ではありません"
    rebind_assert_consumer_points_at_target "$kind" "$name" "$cluster" "$target_arn"
    printf '%s\t%s\t%s\n' "$address" "$current_arn" "$target_arn" >> "$out_dir/rebind-plan.tsv"
  done < <(python3 -c 'import json,sys
for t in json.load(open(sys.argv[1]))["targets"]:
    c = t["consumer"]
    print(t["address"], t["family"], t["target_arn"], c["kind"], c["name"], c.get("cluster",""), sep="\t")' "$REBIND_MAPPING")
  chmod 600 "$out_dir/rebind-plan.tsv"

  python3 "$REBIND_HELPER" record \
    --repo-root "$REPO_ROOT" --out-dir "$out_dir" --mapping "$REBIND_MAPPING" \
    --state "$out_dir/state-backup.json" \
    --account "$EXPECTED_ACCOUNT_ID" --principal-arn "$principal_arn" ||
    die "rebind binding の作成に失敗しました"
  echo "✅ rebind precheck 完了: $out_dir/rebind-plan.tsv（人間レビュー後、表示された承認トークンで apply）"
}

rebind_apply() {
  local out_dir="$1" var_file="$2" approve="$3" principal_arn
  rebind_require_helpers
  adopt_assert_out_dir_outside_repo "$out_dir"
  ensure_tmp
  var_file="$(secure_existing_file "$var_file")"
  for rebind_artifact in state-backup.json rebind-plan.tsv rebind-binding.json; do
    [ -f "$out_dir/$rebind_artifact" ] || die "rebind 成果物がありません: $out_dir/$rebind_artifact"
  done
  principal_arn="$(adopt_trusted_principal_arn "$out_dir/identity-apply.json")"

  # precheck した世界と apply する世界の完全一致（mapping/state/commit/principal）+ 承認束縛。
  terraform -chdir="$TF_DIR" state pull > "$out_dir/state-now.json"
  chmod 600 "$out_dir/state-now.json"
  python3 "$REBIND_HELPER" verify \
    --repo-root "$REPO_ROOT" --out-dir "$out_dir" --mapping "$REBIND_MAPPING" \
    --state "$out_dir/state-now.json" \
    --account "$EXPECTED_ACCOUNT_ID" --principal-arn "$principal_arn" \
    --approve "$approve" ||
    die "rebind binding の再照合に失敗しました（apply は行いません）"

  # 共有 deployment lock で他の guard 操作を完全停止（terraform の backend lock は
  # state rm / import が各操作で個別に取得する）。die した場合 lock は TTL まで残り、
  # 復旧判断まで他の apply を塞ぐ — これは意図された fail-closed。
  acquire_deployment_lock

  # 1 address ずつ atomic-like に進める: precheck → rm → 即 import → verify → 次へ。
  # 「全部 rm してから import」は禁止（途中失敗で複数 address が未束縛になる状態を作らない）。
  local address family target_arn kind name cluster
  while IFS=$'\t' read -r address family target_arn kind name cluster; do
    # 実行直前の再検証: live の参照が precheck 時から動いていたら STALE として停止。
    rebind_assert_consumer_points_at_target "$kind" "$name" "$cluster" "$target_arn"
    aws_cli ecs describe-task-definition --task-definition "$target_arn"       --output json > "$out_dir/describe-$family.json"
    chmod 600 "$out_dir/describe-$family.json"

    terraform -chdir="$TF_DIR" state rm -lock-timeout=5m "$address" ||
      die "rebind: state rm に失敗しました: $address（以降の対象へ進みません。復旧は human 裁定）"
    terraform -chdir="$TF_DIR" import -input=false -lock-timeout=5m \
      "-var-file=$var_file" "$address" "$target_arn" ||
      die "rebind: import に失敗しました: $address（state backup から復旧を検討。以降の対象へ進みません）"

    terraform -chdir="$TF_DIR" state pull > "$out_dir/state-after-$family.json"
    chmod 600 "$out_dir/state-after-$family.json"
    python3 "$REBIND_HELPER" compare \
      --state-json "$out_dir/state-after-$family.json" \
      --address "$address" \
      --describe-json "$out_dir/describe-$family.json" ||
      die "rebind: $address の state と live が一致しません（以降の対象へ進みません）"
    echo "✅ rebound: $address → $target_arn"
  done < <(python3 -c 'import json,sys
for t in json.load(open(sys.argv[1]))["targets"]:
    c = t["consumer"]
    print(t["address"], t["family"], t["target_arn"], c["kind"], c["name"], c.get("cluster",""), sep="\t")' "$REBIND_MAPPING")

  release_deployment_lock
  echo "✅ state rebind 完了（AWS managed application resources mutation = 0 / Terraform remote state mutation only）"
}

COMMAND="${1:-}"
case "$COMMAND" in
  -h|--help|help|"") usage; exit 0 ;;
esac
shift
assert_clean_terraform_environment
derive_aws_ca_bundle_from_ssl_cert_file
assert_guard_sources

case "$COMMAND" in
  snapshot)
    SNAPSHOT_EVIDENCE_JSON_OUT=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --evidence-json-out)
          [ -z "$SNAPSHOT_EVIDENCE_JSON_OUT" ] ||
            die "--evidence-json-out は1回だけ指定できます"
          SNAPSHOT_EVIDENCE_JSON_OUT="$(
            printf '%s' "${2:?--evidence-json-out に値が必要}"
          )"
          shift 2
          ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    need_cmd aws
    need_cmd jq
    ensure_tmp
    snapshot_live "$TMP_ROOT/live.json"
    if [ -n "$SNAPSHOT_EVIDENCE_JSON_OUT" ]; then
      SNAPSHOT_EVIDENCE_JSON_OUT="$(
        secure_new_file "$SNAPSHOT_EVIDENCE_JSON_OUT"
      )"
      cp "$TMP_ROOT/live.json" "$SNAPSHOT_EVIDENCE_JSON_OUT"
      chmod 600 "$SNAPSHOT_EVIDENCE_JSON_OUT"
    fi
    core_from_snapshot \
      "$TMP_ROOT/live.json" "$TMP_ROOT/core.json" "sync" "" \
      "$(jq -er '.taskdefs.openclaw.image' "$TMP_ROOT/live.json")" \
      "$(jq -er '.taskdefs.mcp.image' "$TMP_ROOT/live.json")" \
      "$(jq -er '.taskdefs.x_buzz.image' "$TMP_ROOT/live.json")" \
      "$(jq -er '.taskdefs.tiktok.image' "$TMP_ROOT/live.json")" \
      "$(jq -er '.taskdefs.connect_web.image' "$TMP_ROOT/live.json")" \
      "$(jq -er '.taskdefs.ingest.image' "$TMP_ROOT/live.json")" \
      "$(jq -er '.taskdefs.morning.image' "$TMP_ROOT/live.json")" \
      "$(jq -er '.taskdefs.canary.image' "$TMP_ROOT/live.json")" \
      "" 0
    print_hcl_snapshot "$TMP_ROOT/core.json"
    ;;

  attest-log-versioning)
    VERSIONING_OUT=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --out) VERSIONING_OUT="${2:?--out に値が必要}"; shift 2 ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$VERSIONING_OUT" ] ||
      die "attest-log-versioningには --out RECEIPT が必須です"
    need_cmd aws
    need_cmd git
    need_cmd jq
    need_cmd terraform
    validate_log_versioning_stage_manifest
    run_first_time_versioning_cutover "$VERSIONING_OUT"
    exit 0
    ;;

  issue-alarm-challenge)
    CHALLENGE_OUT=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --out) CHALLENGE_OUT="${2:?--out に値が必要}"; shift 2 ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$CHALLENGE_OUT" ] ||
      die "issue-alarm-challengeには --out が必須です"
    CHALLENGE_OUT="$(secure_new_file "$CHALLENGE_OUT")"
    ensure_tmp
    assert_trusted_automation_identity
    acquire_deployment_lock
    CHALLENGE_STAGE="$TMP_ROOT/alarm-challenge.json"
    CHALLENGE_PUBLISHED="false"
    cleanup_alarm_challenge() {
      local status=$?
      set +e
      release_deployment_lock
      if [ "$CHALLENGE_PUBLISHED" != "true" ]; then
        rm -f "$CHALLENGE_OUT"
      fi
      rm -rf "$TMP_ROOT"
      exit "$status"
    }
    trap 'cleanup_alarm_challenge' EXIT
    run_evidence_helper issue-sns-challenge --output "$CHALLENGE_STAGE"
    CHALLENGE_STAGE_IDENTITY="$(stat_identity "$CHALLENGE_STAGE")"
    ln "$CHALLENGE_STAGE" "$CHALLENGE_OUT" ||
      die "SNS challenge出力pathを原子的に確保できません"
    [ "$(stat_identity "$CHALLENGE_OUT")" = "$CHALLENGE_STAGE_IDENTITY" ] ||
      die "SNS challengeの原子的引渡しに失敗しました"
    chmod 600 "$CHALLENGE_OUT"
    CHALLENGE_PUBLISHED="true"
    release_deployment_lock
    trap - EXIT
    rm -rf "$TMP_ROOT"
    TMP_ROOT=""
    echo "✅ SNS challenge published; recipient KMS ack is now required: $CHALLENGE_OUT"
    ;;

  sign-alarm-ack)
    CHALLENGE=""
    ACK_OUT=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --challenge) CHALLENGE="${2:?--challenge に値が必要}"; shift 2 ;;
        --out) ACK_OUT="${2:?--out に値が必要}"; shift 2 ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$CHALLENGE" ] && [ -n "$ACK_OUT" ] ||
      die "sign-alarm-ackには --challenge と --out が必須です"
    CHALLENGE="$(secure_existing_file "$CHALLENGE" 600)"
    ACK_OUT="$(secure_new_file "$ACK_OUT")"
    ensure_tmp
    ACK_STAGE="$TMP_ROOT/recipient-ack.json"
    run_evidence_helper sign-sns-ack \
      --challenge "$CHALLENGE" --output "$ACK_STAGE"
    ACK_STAGE_IDENTITY="$(stat_identity "$ACK_STAGE")"
    ln "$ACK_STAGE" "$ACK_OUT" ||
      die "recipient ack出力pathを原子的に確保できません"
    [ "$(stat_identity "$ACK_OUT")" = "$ACK_STAGE_IDENTITY" ] ||
      die "recipient ackの原子的引渡しに失敗しました"
    chmod 600 "$ACK_OUT"
    echo "✅ recipient acknowledgement signed by managed KMS: $ACK_OUT"
    ;;

  attest-alarm-delivery)
    CHALLENGE=""
    RECIPIENT_ACK=""
    ALARM_OUT=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --challenge) CHALLENGE="${2:?--challenge に値が必要}"; shift 2 ;;
        --recipient-ack) RECIPIENT_ACK="${2:?--recipient-ack に値が必要}"; shift 2 ;;
        --out) ALARM_OUT="${2:?--out に値が必要}"; shift 2 ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$CHALLENGE" ] && [ -n "$RECIPIENT_ACK" ] &&
      [ -n "$ALARM_OUT" ] ||
      die "attest-alarm-deliveryにはchallenge/recipient-ack/outが必須です"
    CHALLENGE="$(secure_existing_file "$CHALLENGE" 600)"
    RECIPIENT_ACK="$(secure_existing_file "$RECIPIENT_ACK" 600)"
    ALARM_OUT="$(secure_new_file "$ALARM_OUT")"
    ensure_tmp
    assert_trusted_automation_identity
    acquire_deployment_lock
    ALARM_STAGE="$TMP_ROOT/alarm-delivery-receipt.json"
    ALARM_PUBLISHED="false"
    cleanup_alarm_attestation() {
      local status=$?
      set +e
      release_deployment_lock
      if [ "$ALARM_PUBLISHED" != "true" ]; then
        rm -f "$ALARM_OUT"
      fi
      rm -rf "$TMP_ROOT"
      exit "$status"
    }
    trap 'cleanup_alarm_attestation' EXIT
    run_evidence_helper attest-sns-delivery \
      --challenge "$CHALLENGE" --ack "$RECIPIENT_ACK" \
      --output "$ALARM_STAGE"
    ALARM_STAGE_IDENTITY="$(stat_identity "$ALARM_STAGE")"
    ln "$ALARM_STAGE" "$ALARM_OUT" ||
      die "alarm delivery receipt出力pathを原子的に確保できません"
    [ "$(stat_identity "$ALARM_OUT")" = "$ALARM_STAGE_IDENTITY" ] ||
      die "alarm delivery receiptの原子的引渡しに失敗しました"
    chmod 600 "$ALARM_OUT"
    ALARM_PUBLISHED="true"
    release_deployment_lock
    trap - EXIT
    rm -rf "$TMP_ROOT"
    TMP_ROOT=""
    echo "✅ exact email SNS delivery + managed KMS ack verified: $ALARM_OUT"
    ;;

  advance-alarm-migration)
    ALARM_PHASE=""
    ALARM_PUBLISHER_ID=""
    ALARM_PHASE_DELIVERY_RECEIPT=""
    ALARM_PHASE_OUT=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --phase) ALARM_PHASE="${2:?--phase に値が必要}"; shift 2 ;;
        --publisher-id)
          ALARM_PUBLISHER_ID="${2:?--publisher-id に値が必要}"
          shift 2
          ;;
        --delivery-receipt)
          ALARM_PHASE_DELIVERY_RECEIPT="${2:?--delivery-receipt に値が必要}"
          shift 2
          ;;
        --out) ALARM_PHASE_OUT="${2:?--out に値が必要}"; shift 2 ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$ALARM_PHASE" ] && [ -n "$ALARM_PHASE_OUT" ] ||
      die "advance-alarm-migrationには --phase と --out が必須です"
    case "$ALARM_PHASE" in
      dual_publish|legacy_reference_zero|legacy_retired)
        [ -z "$ALARM_PUBLISHER_ID" ] &&
          [ -z "$ALARM_PHASE_DELIVERY_RECEIPT" ] ||
          die "$ALARM_PHASE はpublisher/delivery引数を受け付けません"
        ;;
      publisher_checkpoint)
        [ -n "$ALARM_PUBLISHER_ID" ] &&
          [ -z "$ALARM_PHASE_DELIVERY_RECEIPT" ] ||
          die "publisher_checkpointにはpublisher-idだけが必須です"
        ;;
      canonical_delivery_confirmed)
        [ -z "$ALARM_PUBLISHER_ID" ] &&
          [ -n "$ALARM_PHASE_DELIVERY_RECEIPT" ] ||
          die "canonical_delivery_confirmedにはdelivery receiptだけが必須です"
        ;;
      *) die "未知のalarm migration phaseです: $ALARM_PHASE" ;;
    esac
    if [ -n "$ALARM_PHASE_DELIVERY_RECEIPT" ]; then
      ALARM_PHASE_DELIVERY_RECEIPT="$(
        secure_existing_file "$ALARM_PHASE_DELIVERY_RECEIPT" 600
      )"
    fi
    ALARM_PHASE_OUT="$(secure_new_file "$ALARM_PHASE_OUT")"
    ensure_tmp
    assert_trusted_automation_identity
    ALARM_PHASE_STAGE="$TMP_ROOT/alarm-migration-phase.json"
    ALARM_PHASE_LOCK="$TMP_ROOT/alarm-migration-lock.json"
    ALARM_PHASE_LOCK_RELEASE="$TMP_ROOT/alarm-migration-lock-release.json"
    ALARM_PHASE_PUBLISHED="false"
    ALARM_PHASE_LOCK_ACQUIRED="false"
    ALARM_PHASE_WORKFLOW_ID="$(new_uuid_v4)"
    cleanup_alarm_migration_phase() {
      local status=$?
      set +e
      release_deployment_lock
      if [ "$ALARM_PHASE_LOCK_ACQUIRED" = "true" ] ||
        [ -f "$ALARM_PHASE_LOCK" ]; then
        rm -f "$ALARM_PHASE_LOCK_RELEASE"
        run_evidence_helper release-runtime-lock \
          --lock "$ALARM_PHASE_LOCK" \
          --output "$ALARM_PHASE_LOCK_RELEASE"
        ALARM_PHASE_LOCK_ACQUIRED="false"
      fi
      if [ "$ALARM_PHASE_PUBLISHED" != "true" ]; then
        rm -f "$ALARM_PHASE_OUT"
      fi
      rm -rf "$TMP_ROOT"
      exit "$status"
    }
    trap 'cleanup_alarm_migration_phase' EXIT
    run_evidence_helper acquire-runtime-lock \
      --workflow-id "$ALARM_PHASE_WORKFLOW_ID" \
      --output "$ALARM_PHASE_LOCK"
    ALARM_PHASE_LOCK_ACQUIRED="true"
    acquire_deployment_lock
    ALARM_PHASE_ARGS=(
      advance-alarm-migration
      --migration-id "2026-07-alarm-topic-consolidation-v1"
      --phase "$ALARM_PHASE"
      --publisher-id "$ALARM_PUBLISHER_ID"
      --lock-receipt "$ALARM_PHASE_LOCK"
      --output "$ALARM_PHASE_STAGE"
    )
    if [ -n "$ALARM_PHASE_DELIVERY_RECEIPT" ]; then
      ALARM_PHASE_ARGS+=(
        --delivery-receipt "$ALARM_PHASE_DELIVERY_RECEIPT"
      )
    fi
    run_evidence_helper "${ALARM_PHASE_ARGS[@]}"
    jq -e \
      --arg phase "$ALARM_PHASE" \
      --arg workflow "$ALARM_PHASE_WORKFLOW_ID" '
      .kind == "teamagent-alarm-migration-phase-receipt" and
      .schema_version == 1 and
      .migration_id == "2026-07-alarm-topic-consolidation-v1" and
      .phase == $phase and
      .shared_lock_record_id == "lock#teamagent/terraform.tfstate" and
      .shared_lock_workflow_id == $workflow and
      (.shared_lock_receipt_sha256 | test("^[0-9a-f]{64}$")) and
      (.checkpoint_sha256 | test("^[0-9a-f]{64}$")) and
      (.history_sha256 | test("^[0-9a-f]{64}$"))
    ' "$ALARM_PHASE_STAGE" >/dev/null ||
      die "alarm migration phase receiptがshared lock/ledgerと不一致です"
    ALARM_PHASE_STAGE_IDENTITY="$(stat_identity "$ALARM_PHASE_STAGE")"
    ln "$ALARM_PHASE_STAGE" "$ALARM_PHASE_OUT" ||
      die "alarm migration phase receipt pathを原子的に確保できません"
    [ "$(stat_identity "$ALARM_PHASE_OUT")" = \
      "$ALARM_PHASE_STAGE_IDENTITY" ] ||
      die "alarm migration phase receiptの原子的引渡しに失敗しました"
    chmod 600 "$ALARM_PHASE_OUT"
    ALARM_PHASE_PUBLISHED="true"
    release_deployment_lock
    run_evidence_helper release-runtime-lock \
      --lock "$ALARM_PHASE_LOCK" --output "$ALARM_PHASE_LOCK_RELEASE"
    ALARM_PHASE_LOCK_ACQUIRED="false"
    trap - EXIT
    rm -rf "$TMP_ROOT"
    TMP_ROOT=""
    echo "✅ alarm migration checkpoint: $ALARM_PHASE / $ALARM_PHASE_OUT"
    ;;

  prepare-media-cutover)
    MEDIA_MIGRATION_ID=""
    MEDIA_CHALLENGE_OUT=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --migration)
          MEDIA_MIGRATION_ID="${2:?--migration に値が必要}"
          shift 2
          ;;
        --out)
          MEDIA_CHALLENGE_OUT="${2:?--out に値が必要}"
          shift 2
          ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$MEDIA_MIGRATION_ID" ] && [ -n "$MEDIA_CHALLENGE_OUT" ] ||
      die "prepare-media-cutoverには --migration と --out が必須です"
    MEDIA_CHALLENGE_OUT="$(secure_new_file "$MEDIA_CHALLENGE_OUT")"
    need_cmd aws
    need_cmd jq
    ensure_tmp
    assert_trusted_automation_identity
    MEDIA_BINDING="$TMP_ROOT/media-migration-binding.json"
    media_migration_binding_to_file "$MEDIA_MIGRATION_ID" "$MEDIA_BINDING"
    MEDIA_CHALLENGE_STAGE="$TMP_ROOT/media-cutover-challenge.json"
    MEDIA_CUTOVER_LOCK="$TMP_ROOT/media-cutover-runtime-lock.json"
    MEDIA_CUTOVER_LOCK_RELEASE="$TMP_ROOT/media-cutover-runtime-lock-release.json"
    MEDIA_CHALLENGE_PUBLISHED="false"
    MEDIA_CUTOVER_LOCK_ACQUIRED="false"
    MEDIA_CUTOVER_WORKFLOW_ID="$(new_uuid_v4)"
    cleanup_media_cutover_prepare() {
      local status=$?
      set +e
      release_deployment_lock
      if [ "$MEDIA_CUTOVER_LOCK_ACQUIRED" = "true" ] ||
        [ -f "$MEDIA_CUTOVER_LOCK" ]; then
        rm -f "$MEDIA_CUTOVER_LOCK_RELEASE"
        run_evidence_helper release-runtime-lock \
          --lock "$MEDIA_CUTOVER_LOCK" \
          --output "$MEDIA_CUTOVER_LOCK_RELEASE"
        MEDIA_CUTOVER_LOCK_ACQUIRED="false"
      fi
      if [ "$MEDIA_CHALLENGE_PUBLISHED" != "true" ]; then
        rm -f "$MEDIA_CHALLENGE_OUT"
      fi
      rm -rf "$TMP_ROOT"
      exit "$status"
    }
    trap 'cleanup_media_cutover_prepare' EXIT
    run_evidence_helper acquire-runtime-lock \
      --workflow-id "$MEDIA_CUTOVER_WORKFLOW_ID" \
      --output "$MEDIA_CUTOVER_LOCK"
    MEDIA_CUTOVER_LOCK_ACQUIRED="true"
    acquire_deployment_lock
    run_evidence_helper prepare-media-cutover \
      --desired-image "$(jq -er '.desired_image' "$MEDIA_BINDING")" \
      --image-deployment-intent-id "$(
        jq -er '.image_deployment_intent_id' "$MEDIA_BINDING"
      )" \
      --migration-contract-sha256 "$(
        jq -er '.migration_contract_sha256' "$MEDIA_BINDING"
      )" \
      --reviewed-plan-sha256 "$(
        jq -er '.reviewed_plan_sha256' "$MEDIA_BINDING"
      )" \
      --lock-receipt "$MEDIA_CUTOVER_LOCK" \
      --output "$MEDIA_CHALLENGE_STAGE"
    jq -e \
      --arg desired "$(jq -er '.desired_image' "$MEDIA_BINDING")" \
      --arg intent "$(
        jq -er '.image_deployment_intent_id' "$MEDIA_BINDING"
      )" \
      --arg migration_sha "$(
        jq -er '.migration_contract_sha256' "$MEDIA_BINDING"
      )" \
      --arg reviewed_sha "$(
        jq -er '.reviewed_plan_sha256' "$MEDIA_BINDING"
      )" \
      --arg workflow "$MEDIA_CUTOVER_WORKFLOW_ID" '
      (keys | sort) == ([
        "aws_executable",
        "challenge_sha256",
        "claims",
        "claims_sha256",
        "expires_at_epoch",
        "kind",
        "prepared_at_epoch",
        "schema_version"
      ] | sort) and
      .kind == "teamagent-media-envelope-cutover-challenge" and
      .schema_version == 2 and
      .claims.kind == "teamagent-media-envelope-cutover" and
      .claims.schema_version == 2 and
      .claims.account_id == "718959508629" and
      .claims.region == "ap-northeast-1" and
      .claims.record_id == ("media-cutover#" + $intent) and
      .claims.image_deployment_intent_id == $intent and
      .claims.migration_contract_sha256 == $migration_sha and
      .claims.reviewed_plan_sha256 == $reviewed_sha and
      .claims.desired_image == $desired and
      (.claims.attestation_nonce | test("^[0-9a-f]{64}$")) and
      .claims.shared_lock.workflow_id == $workflow and
      .claims.settle_seconds == 900 and
      (
        .claims.second_observation.earliest_observed_at_epoch -
        .claims.first_observation.observed_at_epoch
      ) >= 900 and
      .claims.first_observation.state_sha256 ==
        .claims.second_observation.state_sha256 and
      .claims.first_observation.state.event_source_mapping.state ==
        "Disabled" and
      .claims.second_observation.state.event_source_mapping.state ==
        "Disabled" and
      .claims.second_observation.state.tasks == {
        pending:[],
        running:[]
      } and
      (.claims_sha256 | test("^[0-9a-f]{64}$")) and
      (.challenge_sha256 | test("^[0-9a-f]{64}$")) and
      .expires_at_epoch == (.prepared_at_epoch + 3600)
    ' "$MEDIA_CHALLENGE_STAGE" >/dev/null ||
      die "media cutover challengeがrelease binding/900秒/shared lock契約と不一致です"
    MEDIA_CHALLENGE_STAGE_IDENTITY="$(
      stat_identity "$MEDIA_CHALLENGE_STAGE"
    )"
    ln "$MEDIA_CHALLENGE_STAGE" "$MEDIA_CHALLENGE_OUT" ||
      die "media cutover challenge pathを原子的に確保できません"
    [ "$(stat_identity "$MEDIA_CHALLENGE_OUT")" = \
      "$MEDIA_CHALLENGE_STAGE_IDENTITY" ] ||
      die "media cutover challengeの原子的引渡しに失敗しました"
    chmod 600 "$MEDIA_CHALLENGE_OUT"
    MEDIA_CHALLENGE_PUBLISHED="true"
    release_deployment_lock
    run_evidence_helper release-runtime-lock \
      --lock "$MEDIA_CUTOVER_LOCK" \
      --output "$MEDIA_CUTOVER_LOCK_RELEASE"
    MEDIA_CUTOVER_LOCK_ACQUIRED="false"
    trap - EXIT
    rm -rf "$TMP_ROOT"
    TMP_ROOT=""
    echo "✅ media cutover challenge prepared: $MEDIA_CHALLENGE_OUT"
    ;;

  attest-media-cutover)
    MEDIA_MIGRATION_ID=""
    MEDIA_CHALLENGE=""
    MEDIA_CUTOVER_OUT=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --migration)
          MEDIA_MIGRATION_ID="${2:?--migration に値が必要}"
          shift 2
          ;;
        --challenge)
          MEDIA_CHALLENGE="${2:?--challenge に値が必要}"
          shift 2
          ;;
        --out)
          MEDIA_CUTOVER_OUT="${2:?--out に値が必要}"
          shift 2
          ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$MEDIA_MIGRATION_ID" ] && [ -n "$MEDIA_CHALLENGE" ] &&
      [ -n "$MEDIA_CUTOVER_OUT" ] ||
      die "attest-media-cutoverには --migration、--challenge、--out が必須です"
    MEDIA_CHALLENGE="$(secure_existing_file "$MEDIA_CHALLENGE" 600)"
    MEDIA_CUTOVER_OUT="$(secure_new_file "$MEDIA_CUTOVER_OUT")"
    need_cmd aws
    need_cmd jq
    ensure_tmp
    assert_trusted_media_attestor_identity
    MEDIA_BINDING="$TMP_ROOT/media-migration-binding.json"
    media_migration_binding_to_file "$MEDIA_MIGRATION_ID" "$MEDIA_BINDING"
    MEDIA_CHALLENGE_SHA256="$(sha256_file "$MEDIA_CHALLENGE")"
    MEDIA_CHALLENGE_IDENTITY="$(stat_identity "$MEDIA_CHALLENGE")"
    MEDIA_CUTOVER_STAGE="$TMP_ROOT/media-cutover-receipt.json"
    run_evidence_helper attest-media-cutover \
      --challenge "$MEDIA_CHALLENGE" \
      --desired-image "$(jq -er '.desired_image' "$MEDIA_BINDING")" \
      --image-deployment-intent-id "$(
        jq -er '.image_deployment_intent_id' "$MEDIA_BINDING"
      )" \
      --migration-contract-sha256 "$(
        jq -er '.migration_contract_sha256' "$MEDIA_BINDING"
      )" \
      --reviewed-plan-sha256 "$(
        jq -er '.reviewed_plan_sha256' "$MEDIA_BINDING"
      )" \
      --output "$MEDIA_CUTOVER_STAGE"
    [ "$(sha256_file "$MEDIA_CHALLENGE")" = "$MEDIA_CHALLENGE_SHA256" ] &&
      [ "$(stat_identity "$MEDIA_CHALLENGE")" = \
        "$MEDIA_CHALLENGE_IDENTITY" ] ||
      die "independent attestation中にchallengeが差替えられました"
    jq -e \
      --arg desired "$(jq -er '.desired_image' "$MEDIA_BINDING")" \
      --arg intent "$(
        jq -er '.image_deployment_intent_id' "$MEDIA_BINDING"
      )" \
      --arg migration_sha "$(
        jq -er '.migration_contract_sha256' "$MEDIA_BINDING"
      )" \
      --arg reviewed_sha "$(
        jq -er '.reviewed_plan_sha256' "$MEDIA_BINDING"
      )" \
      --arg attestor "$TRUSTED_MEDIA_ATTESTOR_ARN" '
      (keys | sort) == ([
        "challenge_sha256",
        "claims",
        "claims_sha256",
        "kind",
        "kms_key_arn",
        "kms_key_metadata_sha256",
        "ledger",
        "receipt_sha256",
        "schema_version",
        "sign_request_id_sha256",
        "signature_base64",
        "signature_sha256",
        "signed_at_epoch"
      ] | sort) and
      .kind == "teamagent-media-envelope-cutover-receipt" and
      .schema_version == 2 and
      .claims.kind == "teamagent-media-envelope-cutover" and
      .claims.schema_version == 2 and
      .claims.record_id == ("media-cutover#" + $intent) and
      .claims.image_deployment_intent_id == $intent and
      .claims.migration_contract_sha256 == $migration_sha and
      .claims.reviewed_plan_sha256 == $reviewed_sha and
      .claims.desired_image == $desired and
      .claims.attestor_principal_arn == $attestor and
      (.claims.attestation_nonce | test("^[0-9a-f]{64}$")) and
      (.claims_sha256 | test("^[0-9a-f]{64}$")) and
      (.receipt_sha256 | test("^[0-9a-f]{64}$")) and
      (.signature_base64 | test("^[A-Za-z0-9+/]+={0,2}$")) and
      (.signature_sha256 | test("^[0-9a-f]{64}$")) and
      (.kms_key_arn |
        test(
          "^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-fA-F-]{36}$"
        )) and
      .ledger.table == "teamagent-dev-image-deployment-intents" and
      (.ledger.item_sha256 | test("^[0-9a-f]{64}$")) and
      (.ledger.put_request_id_sha256 | test("^[0-9a-f]{64}$")) and
      (.ledger.confirmation_request_id_sha256 | test("^[0-9a-f]{64}$"))
    ' "$MEDIA_CUTOVER_STAGE" >/dev/null ||
      die "independent signed media receiptがintent/plan/KMS/ledger契約と不一致です"
    MEDIA_CUTOVER_STAGE_IDENTITY="$(stat_identity "$MEDIA_CUTOVER_STAGE")"
    ln "$MEDIA_CUTOVER_STAGE" "$MEDIA_CUTOVER_OUT" ||
      die "media cutover receipt pathを原子的に確保できません"
    [ "$(stat_identity "$MEDIA_CUTOVER_OUT")" = \
      "$MEDIA_CUTOVER_STAGE_IDENTITY" ] ||
      die "media cutover receiptの原子的引渡しに失敗しました"
    chmod 600 "$MEDIA_CUTOVER_OUT"
    trap - EXIT
    rm -rf "$TMP_ROOT"
    TMP_ROOT=""
    echo "✅ media cutover independently signed: $MEDIA_CUTOVER_OUT"
    ;;

  attest-log-readiness)
    VERSIONING_RECEIPT=""
    READINESS_SPEC=""
    ARTIFACT_DIR=""
    READINESS_OUT=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --versioning-receipt) VERSIONING_RECEIPT="${2:?値が必要}"; shift 2 ;;
        --spec) READINESS_SPEC="${2:?値が必要}"; shift 2 ;;
        --artifact-dir) ARTIFACT_DIR="${2:?値が必要}"; shift 2 ;;
        --out) READINESS_OUT="${2:?値が必要}"; shift 2 ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$VERSIONING_RECEIPT" ] && [ -n "$READINESS_SPEC" ] &&
      [ -n "$ARTIFACT_DIR" ] && [ -n "$READINESS_OUT" ] ||
      die "attest-log-readinessにはversioning/spec/artifact-dir/outが必須です"
    VERSIONING_RECEIPT="$(secure_existing_file "$VERSIONING_RECEIPT" 600)"
    READINESS_SPEC="$(secure_existing_file "$READINESS_SPEC" 600)"
    ARTIFACT_DIR="$(secure_private_dir "$ARTIFACT_DIR")"
    READINESS_OUT="$(secure_new_file "$READINESS_OUT")"
    [ "$(dirname "$READINESS_OUT")" = "$ARTIFACT_DIR" ] ||
      die "readiness receiptはartifact-dir直下に置いてください"
    ensure_tmp
    assert_trusted_automation_identity
    acquire_deployment_lock
    EXPORT_DIR="$ARTIFACT_DIR/exact-exports"
    mkdir -m 700 "$EXPORT_DIR" ||
      die "fresh exact export directoryを作成できません"
    RETENTION_OUT="$ARTIFACT_DIR/retention-export-manifest.json"
    EVIDENCE_OUT="$ARTIFACT_DIR/log-readiness-evidence.json"
    for path in "$RETENTION_OUT" "$EVIDENCE_OUT"; do
      [ ! -e "$path" ] && [ ! -L "$path" ] ||
        die "readiness artifact出力先は未存在が必須です: $path"
    done
    READINESS_STAGE="$TMP_ROOT/readiness-receipt.json"
    READINESS_PUBLISHED="false"
    cleanup_log_readiness() {
      local status=$?
      set +e
      release_deployment_lock
      if [ "$READINESS_PUBLISHED" != "true" ]; then
        rm -f "$READINESS_OUT" "$RETENTION_OUT" "$EVIDENCE_OUT"
        rm -rf "$EXPORT_DIR"
      fi
      rm -rf "$TMP_ROOT"
      exit "$status"
    }
    trap 'cleanup_log_readiness' EXIT
    run_evidence_helper build-log-readiness \
      --spec "$READINESS_SPEC" \
      --versioning-receipt "$VERSIONING_RECEIPT" \
      --export-dir "$EXPORT_DIR" \
      --retention-output "$RETENTION_OUT" \
      --evidence-output "$EVIDENCE_OUT" \
      --receipt-output "$READINESS_STAGE"
    READINESS_STAGE_IDENTITY="$(stat_identity "$READINESS_STAGE")"
    ln "$READINESS_STAGE" "$READINESS_OUT" ||
      die "log readiness receipt出力pathを原子的に確保できません"
    [ "$(stat_identity "$READINESS_OUT")" = "$READINESS_STAGE_IDENTITY" ] ||
      die "log readiness receiptの原子的引渡しに失敗しました"
    chmod 600 "$READINESS_OUT"
    READINESS_PUBLISHED="true"
    release_deployment_lock
    trap - EXIT
    rm -rf "$TMP_ROOT"
    TMP_ROOT=""
    echo "✅ exact-version delivery/retention evidence fetched: $READINESS_OUT"
    ;;

  preflight)
    MIGRATION_ID=""
    PREFLIGHT_OUT=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --migration) MIGRATION_ID="${2:?--migration に値が必要}"; shift 2 ;;
        --out) PREFLIGHT_OUT="${2:?--out に値が必要}"; shift 2 ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$MIGRATION_ID" ] || die "preflightには --migration が必須です"
    [ -n "$PREFLIGHT_OUT" ] || die "preflightには --out が必須です"
    need_cmd aws
    need_cmd curl
    need_cmd jq
    PREFLIGHT_OUT="$(secure_new_file "$PREFLIGHT_OUT")"
    ensure_tmp
    assert_guard_paths_clean
    assert_trusted_automation_identity
    snapshot_live "$TMP_ROOT/live-before.json"
    migration_to_file "$MIGRATION_ID" "$TMP_ROOT/migration.json" preflight
    validate_migration_source "$TMP_ROOT/live-before.json" "$TMP_ROOT/migration.json"

    PREFLIGHT_PUBLISHED="false"
    cleanup_preflight_command() {
      cleanup_preflight_tasks
      cleanup_preflight_efs
      if [ "$PREFLIGHT_PUBLISHED" != "true" ]; then
        rm -f "$PREFLIGHT_OUT"
      fi
      rm -rf "$TMP_ROOT"
    }
    trap 'cleanup_preflight_command' EXIT
    jq -n '{}' > "$TMP_ROOT/profiles.json"
    jq -n '{}' > "$TMP_ROOT/supply-chain.json"

    case "$(jq -er '.kind' "$TMP_ROOT/migration.json")" in
      runtime)
        need_cmd cosign
        need_cmd git
        MAIN_SOURCE_COMMIT="$(jq -er '.to.main_source_commit' "$TMP_ROOT/migration.json")"
        MAIN_SIGNING_KMS_KEY_ARN="$(jq -er '.to.main_signature.kms_key_arn' "$TMP_ROOT/migration.json")"
        MAIN_SIGNATURE_ANNOTATION="$(jq -er '.to.main_signature.annotation_name' "$TMP_ROOT/migration.json")"
        MAIN_MINIMUM_SOURCE_COMMIT="$(jq -er '.to.main_signature.minimum_source_commit' "$TMP_ROOT/migration.json")"
        MAIN_REQUIRED_HMAC_COMMIT="$(jq -er '.to.main_signature.required_hmac_contract_commit' "$TMP_ROOT/migration.json")"
        MAIN_LABELS="$(jq -c --arg commit "$MAIN_SOURCE_COMMIT" '
          .to.required_contract_labels.main +
          {"org.opencontainers.image.revision":$commit}
        ' "$TMP_ROOT/migration.json")"
        X_LABELS="$(jq -c '.to.required_contract_labels.main' "$TMP_ROOT/migration.json")"
        TIKTOK_LABELS="$(jq -c '.to.required_contract_labels.tiktok' "$TMP_ROOT/migration.json")"
        OPENCLAW_LABELS="$(jq -c '.to.required_contract_labels.openclaw' "$TMP_ROOT/migration.json")"
        validate_signed_main_image \
          "$(jq -er '.to.mcp_image' "$TMP_ROOT/migration.json")" \
          "$MAIN_SOURCE_COMMIT" "$MAIN_MINIMUM_SOURCE_COMMIT" \
          "$MAIN_REQUIRED_HMAC_COMMIT" "$MAIN_SIGNING_KMS_KEY_ARN" \
          "$MAIN_SIGNATURE_ANNOTATION" \
          "$TMP_ROOT/main-signature-verification.json"
        jq --slurpfile result "$TMP_ROOT/main-signature-verification.json" \
          '. + {main:$result[0]}' \
          "$TMP_ROOT/supply-chain.json" > "$TMP_ROOT/supply-chain.next"
        mv "$TMP_ROOT/supply-chain.next" "$TMP_ROOT/supply-chain.json"
        validate_image_contract \
          "$(jq -er '.to.mcp_image' "$TMP_ROOT/migration.json")" \
          "$MAIN_LABELS" "10001:10001" true main \
          "$TMP_ROOT/main-image-config.json"
        validate_image_contract \
          "$(jq -er '.to.x_buzz_image' "$TMP_ROOT/migration.json")" \
          "$X_LABELS" "10001:10001" true x_buzz \
          "$TMP_ROOT/x-image-config.json"
        validate_image_contract \
          "$(jq -er '.to.tiktok_image' "$TMP_ROOT/migration.json")" \
          "$TIKTOK_LABELS" "10001:10001" true tiktok \
          "$TMP_ROOT/tiktok-image-config.json"
        validate_image_contract \
          "$(jq -er '.to.openclaw_image' "$TMP_ROOT/migration.json")" \
          "$OPENCLAW_LABELS" "65532:65532" true openclaw \
          "$TMP_ROOT/openclaw-image-config.json"

        for PROFILE in main tiktok x_buzz; do
          case "$PROFILE" in
            main) PROFILE_IMAGE="$(jq -er '.to.mcp_image' "$TMP_ROOT/migration.json")" ;;
            tiktok) PROFILE_IMAGE="$(jq -er '.to.tiktok_image' "$TMP_ROOT/migration.json")" ;;
            x_buzz) PROFILE_IMAGE="$(jq -er '.to.x_buzz_image' "$TMP_ROOT/migration.json")" ;;
          esac
          run_registered_preflight_task \
            "$PROFILE" "$PROFILE_IMAGE" "$TMP_ROOT/live-before.json" \
            "$TMP_ROOT/profile-${PROFILE}.json"
          jq --arg profile "$PROFILE" \
            --slurpfile result "$TMP_ROOT/profile-${PROFILE}.json" \
            '. + {($profile):$result[0]}' \
            "$TMP_ROOT/profiles.json" > "$TMP_ROOT/profiles.next"
          mv "$TMP_ROOT/profiles.next" "$TMP_ROOT/profiles.json"
        done

        create_preflight_efs "$TMP_ROOT/live-before.json"
        run_registered_preflight_task \
          openclaw "$(jq -er '.to.openclaw_image' "$TMP_ROOT/migration.json")" \
          "$TMP_ROOT/live-before.json" "$TMP_ROOT/profile-openclaw.json"
        jq --slurpfile result "$TMP_ROOT/profile-openclaw.json" \
          '. + {openclaw:$result[0]}' \
          "$TMP_ROOT/profiles.json" > "$TMP_ROOT/profiles.next"
        mv "$TMP_ROOT/profiles.next" "$TMP_ROOT/profiles.json"
        cleanup_preflight_efs
        PREFLIGHT_EFS_ID=""
        PREFLIGHT_EFS_AP_ID=""
        PREFLIGHT_EFS_SG_ID=""
        PREFLIGHT_EFS_ROLE_NAME=""
        PREFLIGHT_EFS_ROLE_ARN=""
        PREFLIGHT_EFS_MOUNT_TARGETS=""
        ;;
      activation)
        for PROFILE in activation-ingest-acl-quarantine activation-canary; do
          run_activation_task \
            "$PROFILE" "$TMP_ROOT/live-before.json" "$TMP_ROOT/profile-${PROFILE}.json"
          jq --arg profile "$PROFILE" \
            --slurpfile result "$TMP_ROOT/profile-${PROFILE}.json" \
            '. + {($profile):$result[0]}' \
            "$TMP_ROOT/profiles.json" > "$TMP_ROOT/profiles.next"
          mv "$TMP_ROOT/profiles.next" "$TMP_ROOT/profiles.json"
        done
        ;;
      *) die "未知のmigration kindです" ;;
    esac

    snapshot_live "$TMP_ROOT/live-after.json"
    [ "$(sha256_file "$TMP_ROOT/live-before.json")" = \
      "$(sha256_file "$TMP_ROOT/live-after.json")" ] ||
      die "preflight中にlive runtimeが変化しました"
    write_preflight_receipt \
      "$MIGRATION_ID" "$TMP_ROOT/migration.json" "$TMP_ROOT/live-after.json" \
      "$TMP_ROOT/profiles.json" "$TMP_ROOT/preflight-receipt.json"
    chmod 600 "$TMP_ROOT/preflight-receipt.json"
    ln "$TMP_ROOT/preflight-receipt.json" "$PREFLIGHT_OUT" ||
      die "preflight receipt出力pathを原子的に確保できません"
    chmod 600 "$PREFLIGHT_OUT"
    PREFLIGHT_PUBLISHED="true"
    echo "✅ runtime preflight passed: $PREFLIGHT_OUT"
    echo "   migration: $MIGRATION_ID"
    ;;

  review-plan|plan)
    REVIEW_ONLY="false"
    if [ "$COMMAND" = "review-plan" ]; then
      REVIEW_ONLY="true"
    fi
    VAR_FILE=""
    PLAN=""
    RECEIPT=""
    RUNTIME_SYNC="false"
    MIGRATION_ID=""
    PREFLIGHT_RECEIPT=""
    ALARM_DELIVERY_RECEIPT=""
    VERSIONING_RECEIPT=""
    LOG_READINESS_RECEIPT=""
    ALARM_MIGRATION_RECEIPT=""
    PRIOR_APPLY_RECEIPT=""
    MEDIA_CUTOVER_RECEIPT=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --var-file) VAR_FILE="${2:?--var-file に値が必要}"; shift 2 ;;
        --out) PLAN="${2:?--out に値が必要}"; shift 2 ;;
        --receipt) RECEIPT="${2:?--receipt に値が必要}"; shift 2 ;;
        --runtime-sync) RUNTIME_SYNC="true"; shift ;;
        --runtime-migration) MIGRATION_ID="${2:?--runtime-migration に値が必要}"; shift 2 ;;
        --preflight-receipt) PREFLIGHT_RECEIPT="${2:?--preflight-receipt に値が必要}"; shift 2 ;;
        --alarm-delivery-receipt) ALARM_DELIVERY_RECEIPT="${2:?--alarm-delivery-receipt に値が必要}"; shift 2 ;;
        --versioning-receipt) VERSIONING_RECEIPT="${2:?--versioning-receipt に値が必要}"; shift 2 ;;
        --log-readiness-receipt) LOG_READINESS_RECEIPT="${2:?--log-readiness-receipt に値が必要}"; shift 2 ;;
        --alarm-migration-receipt) ALARM_MIGRATION_RECEIPT="${2:?--alarm-migration-receipt に値が必要}"; shift 2 ;;
        --prior-apply-receipt) PRIOR_APPLY_RECEIPT="${2:?--prior-apply-receipt に値が必要}"; shift 2 ;;
        --media-cutover-receipt) MEDIA_CUTOVER_RECEIPT="${2:?--media-cutover-receipt に値が必要}"; shift 2 ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$VAR_FILE" ] || die "plan には --var-file が必須です"
    [ -n "$PLAN" ] || die "plan には --out が必須です"
    if [ "$REVIEW_ONLY" = "true" ] && [ -n "$RECEIPT" ]; then
      die "review-planは--receiptを受け付けません"
    fi
    if [ "$REVIEW_ONLY" = "true" ] &&
       [ -n "$MEDIA_CUTOVER_RECEIPT" ]; then
      die "review-planはmedia cutover receiptを受け付けません"
    fi
    if [ "$REVIEW_ONLY" = "true" ] && [ "$RUNTIME_SYNC" = "true" ]; then
      die "review-planは--runtime-migration専用です"
    fi
    if [ "$RUNTIME_SYNC" = "true" ] && [ -n "$MIGRATION_ID" ]; then
      die "--runtime-sync と --runtime-migration は併用できません"
    fi
    if [ -n "$MIGRATION_ID" ] && [ -z "$PREFLIGHT_RECEIPT" ]; then
      die "--runtime-migration には --preflight-receipt が必須です"
    fi
    if [ -n "$MIGRATION_ID" ] && [ -z "$ALARM_DELIVERY_RECEIPT" ]; then
      die "--runtime-migration には --alarm-delivery-receipt が必須です"
    fi
    if [ -n "$MIGRATION_ID" ] && [ -z "$VERSIONING_RECEIPT" ]; then
      die "--runtime-migration には --versioning-receipt が必須です"
    fi
    if [ -n "$MIGRATION_ID" ] && [ -z "$LOG_READINESS_RECEIPT" ]; then
      die "--runtime-migration には --log-readiness-receipt が必須です"
    fi
    if [ -n "$MIGRATION_ID" ] && [ -z "$ALARM_MIGRATION_RECEIPT" ]; then
      die "--runtime-migration には --alarm-migration-receipt が必須です"
    fi
    if [ "$RUNTIME_SYNC" = "true" ] &&
       { [ -n "$PREFLIGHT_RECEIPT" ] ||
         [ -n "$ALARM_DELIVERY_RECEIPT" ] ||
         [ -n "$VERSIONING_RECEIPT" ] ||
         [ -n "$LOG_READINESS_RECEIPT" ] ||
         [ -n "$ALARM_MIGRATION_RECEIPT" ] ||
         [ -n "$PRIOR_APPLY_RECEIPT" ] ||
         [ -n "$MEDIA_CUTOVER_RECEIPT" ]; }; then
      die "--runtime-syncに外部receiptは指定できません"
    fi
    [ "$RUNTIME_SYNC" = "true" ] || [ -n "$MIGRATION_ID" ] ||
      die "--runtime-sync または --runtime-migration が必須です"

    need_cmd aws
    need_cmd jq
    need_cmd terraform
    VAR_FILE="$(secure_existing_file "$VAR_FILE")"
    if [ -n "$PREFLIGHT_RECEIPT" ]; then
      PREFLIGHT_RECEIPT="$(secure_existing_file "$PREFLIGHT_RECEIPT" 600)"
    fi
    if [ -n "$ALARM_DELIVERY_RECEIPT" ]; then
      ALARM_DELIVERY_RECEIPT="$(
        secure_existing_file "$ALARM_DELIVERY_RECEIPT" 600
      )"
    fi
    if [ -n "$VERSIONING_RECEIPT" ]; then
      VERSIONING_RECEIPT="$(
        secure_existing_file "$VERSIONING_RECEIPT" 600
      )"
    fi
    if [ -n "$LOG_READINESS_RECEIPT" ]; then
      LOG_READINESS_RECEIPT="$(
        secure_existing_file "$LOG_READINESS_RECEIPT" 600
      )"
    fi
    if [ -n "$ALARM_MIGRATION_RECEIPT" ]; then
      ALARM_MIGRATION_RECEIPT="$(
        secure_existing_file "$ALARM_MIGRATION_RECEIPT" 600
      )"
    fi
    if [ -n "$PRIOR_APPLY_RECEIPT" ]; then
      PRIOR_APPLY_RECEIPT="$(
        secure_existing_file "$PRIOR_APPLY_RECEIPT" 600
      )"
    fi
    if [ -n "$MEDIA_CUTOVER_RECEIPT" ]; then
      MEDIA_CUTOVER_RECEIPT="$(
        secure_existing_file "$MEDIA_CUTOVER_RECEIPT" 600
      )"
    fi
    ALARM_DELIVERY_RECEIPT_SHA256=""
    ALARM_DELIVERY_RECEIPT_IDENTITY=""
    if [ -n "$ALARM_DELIVERY_RECEIPT" ]; then
      ALARM_DELIVERY_RECEIPT_SHA256="$(sha256_file "$ALARM_DELIVERY_RECEIPT")"
      ALARM_DELIVERY_RECEIPT_IDENTITY="$(stat_identity "$ALARM_DELIVERY_RECEIPT")"
    fi
    VERSIONING_RECEIPT_SHA256=""
    VERSIONING_RECEIPT_IDENTITY=""
    LOG_CUTOVER_CONTRACT_SHA256=""
    if [ -n "$VERSIONING_RECEIPT" ]; then
      VERSIONING_RECEIPT_SHA256="$(sha256_file "$VERSIONING_RECEIPT")"
      VERSIONING_RECEIPT_IDENTITY="$(stat_identity "$VERSIONING_RECEIPT")"
      LOG_CUTOVER_CONTRACT_SHA256="$(
        jq -er '.workflow_sha256' "$VERSIONING_RECEIPT"
      )"
    fi
    LOG_READINESS_RECEIPT_SHA256=""
    LOG_READINESS_RECEIPT_IDENTITY=""
    if [ -n "$LOG_READINESS_RECEIPT" ]; then
      LOG_READINESS_RECEIPT_SHA256="$(sha256_file "$LOG_READINESS_RECEIPT")"
      LOG_READINESS_RECEIPT_IDENTITY="$(stat_identity "$LOG_READINESS_RECEIPT")"
    fi
    ALARM_MIGRATION_RECEIPT_SHA256=""
    ALARM_MIGRATION_RECEIPT_IDENTITY=""
    if [ -n "$ALARM_MIGRATION_RECEIPT" ]; then
      ALARM_MIGRATION_RECEIPT_SHA256="$(
        sha256_file "$ALARM_MIGRATION_RECEIPT"
      )"
      ALARM_MIGRATION_RECEIPT_IDENTITY="$(
        stat_identity "$ALARM_MIGRATION_RECEIPT"
      )"
    fi
    PRIOR_APPLY_RECEIPT_SHA256=""
    PRIOR_APPLY_RECEIPT_IDENTITY=""
    if [ -n "$PRIOR_APPLY_RECEIPT" ]; then
      PRIOR_APPLY_RECEIPT_SHA256="$(sha256_file "$PRIOR_APPLY_RECEIPT")"
      PRIOR_APPLY_RECEIPT_IDENTITY="$(stat_identity "$PRIOR_APPLY_RECEIPT")"
    fi
    MEDIA_CUTOVER_RECEIPT_SHA256=""
    MEDIA_CUTOVER_RECEIPT_IDENTITY=""
    if [ -n "$MEDIA_CUTOVER_RECEIPT" ]; then
      MEDIA_CUTOVER_RECEIPT_SHA256="$(
        sha256_file "$MEDIA_CUTOVER_RECEIPT"
      )"
      MEDIA_CUTOVER_RECEIPT_IDENTITY="$(
        stat_identity "$MEDIA_CUTOVER_RECEIPT"
      )"
    fi
    PLAN="$(secure_new_file "$PLAN")"
    if [ "$REVIEW_ONLY" != "true" ]; then
      RECEIPT="${RECEIPT:-${PLAN}.runtime-guard.json}"
      RECEIPT="$(secure_new_file "$RECEIPT")"
      [ "$(dirname "$PLAN")" = "$(dirname "$RECEIPT")" ] ||
        die "planとreceiptは同じprivate directoryへ出力してください"
    fi
    ensure_tmp
    assert_guard_paths_clean
    STAGE="$(mktemp -d "$(dirname "$PLAN")/.teamagent-runtime-plan.XXXXXX")"
    chmod 700 "$STAGE"
    STAGE_PLAN="$STAGE/plan.tfplan"
    STAGE_RECEIPT="$STAGE/receipt.json"
    STAGE_REVIEWED_PLAN="$STAGE/reviewed-plan.json"
    case "$VAR_FILE" in
      *.json) STAGE_VAR="$STAGE/terraform.tfvars.json" ;;
      *) STAGE_VAR="$STAGE/terraform.tfvars" ;;
    esac
    cp "$VAR_FILE" "$STAGE_VAR"
    chmod 600 "$STAGE_VAR"
    VAR_IDENTITY="$(stat_identity "$VAR_FILE")"
    VAR_SHA="$(sha256_file "$VAR_FILE")"
    [ "$(sha256_file "$STAGE_VAR")" = "$VAR_SHA" ] || die "private var-file copyが不一致です"

    PUBLISHED="false"
    PLAN_CREATED="false"
    RECEIPT_CREATED="false"
    cleanup_plan_stage() {
      rm -rf "$STAGE"
      if [ "$PUBLISHED" != "true" ]; then
        [ "$PLAN_CREATED" != "true" ] || rm -f "$PLAN"
        [ "$RECEIPT_CREATED" != "true" ] || rm -f "$RECEIPT"
      fi
      rm -rf "$TMP_ROOT"
    }
    trap 'cleanup_plan_stage' EXIT

    capture_state_contract \
      "$TMP_ROOT/state-before.json" "" "$TMP_ROOT/state-before-full.json"
    snapshot_live "$TMP_ROOT/live-before.json"
    capture_complete_runtime_inventory "$TMP_ROOT/inventory-before.json"
    if [ -n "$ALARM_DELIVERY_RECEIPT" ]; then
      verify_alarm_delivery_test_receipt \
        "$ALARM_DELIVERY_RECEIPT" "$TMP_ROOT/live-before.json"
    fi
    if [ -n "$VERSIONING_RECEIPT" ]; then
      verify_versioning_attestation_receipt \
        "$VERSIONING_RECEIPT" "$TMP_ROOT/live-before.json" \
        "$TMP_ROOT/state-before.json"
    fi
    if [ -n "$LOG_READINESS_RECEIPT" ]; then
      verify_log_readiness_receipt \
        "$LOG_READINESS_RECEIPT" "$VERSIONING_RECEIPT" \
        "$TMP_ROOT/live-before.json"
    fi
    if [ -n "$ALARM_MIGRATION_RECEIPT" ]; then
      verify_alarm_migration_final_receipt "$ALARM_MIGRATION_RECEIPT"
    fi
    # PR2-A0.3.2: live からの世界導出は adopt-plan と共有の単一実装
    #（sync_live_world_from_snapshot）。migration 経路は直後に DESIRED_* を上書きする。
    sync_live_world_from_snapshot "$TMP_ROOT/live-before.json"

    if [ -n "$MIGRATION_ID" ]; then
      MODE="migration"
      MIGRATION_JSON="$TMP_ROOT/migration.json"
      if [ "$REVIEW_ONLY" = "true" ]; then
        migration_to_file "$MIGRATION_ID" "$MIGRATION_JSON" candidate
      else
        migration_to_file "$MIGRATION_ID" "$MIGRATION_JSON" final
      fi
      MIGRATION_CONTRACT_SHA256="$(
        normalized_migration_manifest_sha256 "$MIGRATION_ID"
      )"
      if [ "$REVIEW_ONLY" != "true" ]; then
        REVIEWED_PLAN_SHA256="$(
          jq -cS '.reviewed_plan' "$MIGRATION_JSON" | sha256_text
        )"
        [[ "$REVIEWED_PLAN_SHA256" =~ ^[0-9a-f]{64}$ ]] ||
          die "reviewed plan SHA256を計算できません"
      fi
      validate_migration_source "$TMP_ROOT/live-before.json" "$MIGRATION_JSON"
      verify_preflight_receipt \
        "$PREFLIGHT_RECEIPT" "$MIGRATION_ID" "$MIGRATION_JSON" \
        "$TMP_ROOT/live-before.json"
      PREFLIGHT_SHA256="$(sha256_file "$PREFLIGHT_RECEIPT")"
      TRANSITION_EPOCH="$(jq -er '.created_at_epoch' "$PREFLIGHT_RECEIPT")"
      IMAGE_DEPLOYMENT_INTENT_ID="$(
        jq -er '.reviewed_inputs.image_deployment_intent_id' "$MIGRATION_JSON"
      )"
      MIGRATION_KIND="$(jq -er '.kind' "$MIGRATION_JSON")"
      DESIRED_INGEST_RULE="$(jq -r '.to.rule_states.ingest == "ENABLED"' "$MIGRATION_JSON")"
      DESIRED_MORNING_RULE="$(jq -r '.to.rule_states.morning == "ENABLED"' "$MIGRATION_JSON")"
      DESIRED_CANARY_RULE="$(jq -r '.to.rule_states.canary == "ENABLED"' "$MIGRATION_JSON")"
      if [ "$MIGRATION_KIND" = "runtime" ]; then
        [ -z "$PRIOR_APPLY_RECEIPT" ] ||
          die "runtime migrationはprior apply receiptを受け付けません"
        jq -e '.requires_migration == null' "$MIGRATION_JSON" >/dev/null ||
          die "runtime migrationのrequires_migration契約が不正です"
        DESIRED_OPENCLAW_IMAGE="$(jq -er '.to.openclaw_image' "$MIGRATION_JSON")"
        DESIRED_MCP_IMAGE="$(jq -er '.to.mcp_image' "$MIGRATION_JSON")"
        DESIRED_X_IMAGE="$(jq -er '.to.x_buzz_image' "$MIGRATION_JSON")"
        DESIRED_TIKTOK_IMAGE="$(jq -er '.to.tiktok_image' "$MIGRATION_JSON")"
        DESIRED_CONNECT_WEB_IMAGE="$DESIRED_MCP_IMAGE"
        DESIRED_INGEST_IMAGE="$DESIRED_MCP_IMAGE"
        DESIRED_MORNING_DIGEST_IMAGE="$DESIRED_MCP_IMAGE"
        DESIRED_CANARY_IMAGE="$DESIRED_MCP_IMAGE"
      elif [ "$MIGRATION_KIND" = "activation" ]; then
        [ -n "$PRIOR_APPLY_RECEIPT" ] ||
          die "activationには --prior-apply-receipt が必須です"
        REQUIRED_MIGRATION_ID="$(jq -er '.requires_migration' "$MIGRATION_JSON")"
        verify_required_migration_apply_receipt \
          "$PRIOR_APPLY_RECEIPT" "$REQUIRED_MIGRATION_ID" \
          "$TMP_ROOT/live-before.json" "$TMP_ROOT/state-before.json"
        REQUIRED_MIGRATION_APPLY_RECEIPT_SHA256="$PRIOR_APPLY_RECEIPT_SHA256"
      else
        die "未知のmigration kindです"
      fi
    fi

    if [ "$REVIEW_ONLY" != "true" ]; then
      validate_media_envelope_cutover_gate \
        "$TMP_ROOT/live-before.json" "$DESIRED_TIKTOK_IMAGE" \
        "$MEDIA_CUTOVER_RECEIPT" "$IMAGE_DEPLOYMENT_INTENT_ID" \
        "$MIGRATION_CONTRACT_SHA256" "$REVIEWED_PLAN_SHA256"
    fi

    # PR2-A0.3.2: CORE_JSON / live-derived vars の構築と注入引数列は adopt-plan と
    # 共有の単一実装（build_live_injection_args）。plan 固有の flag だけをここで足す。
    build_live_injection_args \
      "$TMP_ROOT/live-before.json" "$TMP_ROOT/core.json" \
      "$TMP_ROOT/state-before-full.json" "$STAGE"
    TF_ARGS=(
      plan
      -input=false
      -refresh=true
      -lock-timeout=5m
      "-var-file=$STAGE_VAR"
      "-out=$STAGE_PLAN"
      "${LIVE_INJECTION_TF_ARGS[@]}"
    )
    terraform -chdir="$TF_DIR" "${TF_ARGS[@]}"
    if [ "$MODE" = "sync" ]; then
      [ "$(sha256_file "$SYNC_DERIVED_VAR_FILE")" = \
        "$SYNC_DERIVED_VAR_SHA256" ] &&
        [ "$(stat_identity "$SYNC_DERIVED_VAR_FILE")" = \
          "$SYNC_DERIVED_VAR_IDENTITY" ] ||
        die "terraform plan中にsync用live-derived variable overlayが差替えられました"
    fi
    chmod 600 "$STAGE_PLAN"
    PLAN_SHA="$(sha256_file "$STAGE_PLAN")"
    terraform -chdir="$TF_DIR" show -json "$STAGE_PLAN" > "$TMP_ROOT/plan.json"
    [ "$(sha256_file "$STAGE_PLAN")" = "$PLAN_SHA" ] || die "terraform show中のplan差替えを検出しました"
    hmac_from_plan "$TMP_ROOT/plan.json" "$TMP_ROOT/proposed-hmac.json"
    validate_hmac_transition_metadata \
      "$TMP_ROOT/live-before.json" "$TMP_ROOT/proposed-hmac.json" "$MODE" \
      "$TRANSITION_EPOCH" "$TMP_ROOT/hmac-transition.json"
    validate_hmac_secret_metadata "$TMP_ROOT/proposed-hmac.json"
    PLAN_CONTRACT_MODE="verify"
    PLAN_CONTRACT_OUTPUT=""
    if [ "$REVIEW_ONLY" = "true" ]; then
      PLAN_CONTRACT_MODE="extract"
      PLAN_CONTRACT_OUTPUT="$STAGE_REVIEWED_PLAN"
    fi
    validate_plan \
      "$TMP_ROOT/plan.json" "$TMP_ROOT/live-before.json" "$TMP_ROOT/core.json" \
      "$DESIRED_MCP_IMAGE" "$MIGRATION_JSON" "$TMP_ROOT/proposed-hmac.json" \
      "$TMP_ROOT/state-before.json" "$PLAN_CONTRACT_MODE" \
      "$PLAN_CONTRACT_OUTPUT"
    [ "$(sha256_file "$STAGE_PLAN")" = "$PLAN_SHA" ] || die "plan検証中の差替えを検出しました"

    # Freeze v2 の enforcement 保全検査。plan 自体の整合性検査（validate_plan 等）を
    # 通した後に置く。先に置くと malformed plan の診断を奪ってしまう（2026-08-24 実測）。
    python3 "$FREEZE_CHECKER" --freeze "$FREEZE_DECLARATION" \
      assert-plan-preserves-freeze --plan "$TMP_ROOT/plan.json" ||
      die "plan が Freeze v2 の enforcement を壊します"

    # plan 中に別デプロイが走った場合も fail-closed（TOCTOU 防止）。
    snapshot_live "$TMP_ROOT/live-after.json"
    if [ "$REVIEW_ONLY" != "true" ]; then
      validate_media_envelope_cutover_gate \
        "$TMP_ROOT/live-after.json" "$DESIRED_TIKTOK_IMAGE" \
        "$MEDIA_CUTOVER_RECEIPT" "$IMAGE_DEPLOYMENT_INTENT_ID" \
        "$MIGRATION_CONTRACT_SHA256" "$REVIEWED_PLAN_SHA256"
    fi
    capture_state_contract "$TMP_ROOT/state-after.json"
    capture_complete_runtime_inventory "$TMP_ROOT/inventory-after.json"
    BEFORE_SHA="$(sha256_file "$TMP_ROOT/live-before.json")"
    AFTER_SHA="$(sha256_file "$TMP_ROOT/live-after.json")"
    [ "$BEFORE_SHA" = "$AFTER_SHA" ] || die "plan 作成中に live runtime が変化しました。plan を再作成してください"
    [ "$(sha256_file "$TMP_ROOT/state-before.json")" = \
      "$(sha256_file "$TMP_ROOT/state-after.json")" ] ||
      die "plan作成中にbackend/workspace/state lineage/serial/address ownershipが変化しました"
    cmp -s "$TMP_ROOT/inventory-before.json" "$TMP_ROOT/inventory-after.json" ||
      die "plan作成中にall-page runtime/SNS publisher inventoryが変化しました"
    [ "$(sha256_file "$VAR_FILE")" = "$VAR_SHA" ] || die "plan作成中にvar-fileが変化しました"
    [ "$(stat_identity "$VAR_FILE")" = "$VAR_IDENTITY" ] || die "plan作成中にvar-file pathが差替えられました"
    [ "$(sha256_file "$STAGE_VAR")" = "$VAR_SHA" ] || die "private var-file copyが変化しました"
    if [ "$MODE" = "sync" ]; then
      [ "$(sha256_file "$SYNC_DERIVED_VAR_FILE")" = \
        "$SYNC_DERIVED_VAR_SHA256" ] &&
        [ "$(stat_identity "$SYNC_DERIVED_VAR_FILE")" = \
          "$SYNC_DERIVED_VAR_IDENTITY" ] ||
        die "plan検証中にsync用live-derived variable overlayが差替えられました"
    fi
    if [ -n "$ALARM_DELIVERY_RECEIPT" ]; then
      [ "$(sha256_file "$ALARM_DELIVERY_RECEIPT")" = \
        "$ALARM_DELIVERY_RECEIPT_SHA256" ] ||
        die "plan作成中にalarm delivery receiptが変化しました"
      [ "$(stat_identity "$ALARM_DELIVERY_RECEIPT")" = \
        "$ALARM_DELIVERY_RECEIPT_IDENTITY" ] ||
        die "plan作成中にalarm delivery receipt pathが差替えられました"
    fi
    if [ -n "$VERSIONING_RECEIPT" ]; then
      [ "$(sha256_file "$VERSIONING_RECEIPT")" = \
        "$VERSIONING_RECEIPT_SHA256" ] ||
        die "plan作成中にversioning receiptが変化しました"
      [ "$(stat_identity "$VERSIONING_RECEIPT")" = \
        "$VERSIONING_RECEIPT_IDENTITY" ] ||
        die "plan作成中にversioning receipt pathが差替えられました"
    fi
    if [ -n "$LOG_READINESS_RECEIPT" ]; then
      [ "$(sha256_file "$LOG_READINESS_RECEIPT")" = \
        "$LOG_READINESS_RECEIPT_SHA256" ] ||
        die "plan作成中にlog readiness receiptが変化しました"
      [ "$(stat_identity "$LOG_READINESS_RECEIPT")" = \
        "$LOG_READINESS_RECEIPT_IDENTITY" ] ||
        die "plan作成中にlog readiness receipt pathが差替えられました"
    fi
    if [ -n "$ALARM_MIGRATION_RECEIPT" ]; then
      [ "$(sha256_file "$ALARM_MIGRATION_RECEIPT")" = \
        "$ALARM_MIGRATION_RECEIPT_SHA256" ] ||
        die "plan作成中にalarm migration receiptが変化しました"
      [ "$(stat_identity "$ALARM_MIGRATION_RECEIPT")" = \
        "$ALARM_MIGRATION_RECEIPT_IDENTITY" ] ||
        die "plan作成中にalarm migration receipt pathが差替えられました"
      verify_alarm_migration_final_receipt "$ALARM_MIGRATION_RECEIPT"
    fi
    if [ -n "$PRIOR_APPLY_RECEIPT" ]; then
      [ "$(sha256_file "$PRIOR_APPLY_RECEIPT")" = \
        "$PRIOR_APPLY_RECEIPT_SHA256" ] ||
        die "plan作成中にprior apply receiptが変化しました"
      [ "$(stat_identity "$PRIOR_APPLY_RECEIPT")" = \
        "$PRIOR_APPLY_RECEIPT_IDENTITY" ] ||
        die "plan作成中にprior apply receipt pathが差替えられました"
      verify_required_migration_apply_receipt \
        "$PRIOR_APPLY_RECEIPT" "$REQUIRED_MIGRATION_ID" \
        "$TMP_ROOT/live-after.json" "$TMP_ROOT/state-after.json"
    fi
    if [ -n "$MEDIA_CUTOVER_RECEIPT" ]; then
      [ "$(sha256_file "$MEDIA_CUTOVER_RECEIPT")" = \
        "$MEDIA_CUTOVER_RECEIPT_SHA256" ] ||
        die "plan作成中にmedia cutover receiptが変化しました"
      [ "$(stat_identity "$MEDIA_CUTOVER_RECEIPT")" = \
        "$MEDIA_CUTOVER_RECEIPT_IDENTITY" ] ||
        die "plan作成中にmedia cutover receipt pathが差替えられました"
    fi
    if [ -n "$ALARM_DELIVERY_RECEIPT" ]; then
      verify_alarm_delivery_test_receipt \
        "$ALARM_DELIVERY_RECEIPT" "$TMP_ROOT/live-after.json"
    fi
    if [ -n "$VERSIONING_RECEIPT" ]; then
      verify_versioning_attestation_receipt \
        "$VERSIONING_RECEIPT" "$TMP_ROOT/live-after.json" \
        "$TMP_ROOT/state-after.json"
    fi
    if [ -n "$LOG_READINESS_RECEIPT" ]; then
      verify_log_readiness_receipt \
        "$LOG_READINESS_RECEIPT" "$VERSIONING_RECEIPT" \
        "$TMP_ROOT/live-after.json"
    fi
    if [ "$REVIEW_ONLY" = "true" ]; then
      [ -s "$STAGE_REVIEWED_PLAN" ] ||
        die "reviewed plan contractが生成されませんでした"
      REVIEW_IDENTITY="$(stat_identity "$STAGE_REVIEWED_PLAN")"
      ln "$STAGE_REVIEWED_PLAN" "$PLAN" ||
        die "reviewed plan出力pathを原子的に確保できません"
      PLAN_CREATED="true"
      [ "$(stat_identity "$PLAN")" = "$REVIEW_IDENTITY" ] ||
        die "reviewed plan出力pathの同一性を確認できません"
      chmod 600 "$PLAN"
      PUBLISHED="true"
      trap - EXIT
      rm -rf "$STAGE" "$TMP_ROOT"
      TMP_ROOT=""
      echo "✅ exact reviewed plan candidate: $PLAN"
      echo "   migration: $MIGRATION_ID"
      echo "   intent: $IMAGE_DEPLOYMENT_INTENT_ID"
      echo "   このJSONだけをreviewed_planへcommitし、enabled=trueでfinal planを再生成してください"
      exit 0
    fi

    capture_image_release_context \
      "$STAGE_PLAN" "$TMP_ROOT/image-release-context.json"
    build_scoped_release_live_contract \
      "$TMP_ROOT/image-release-context.json" \
      "$TMP_ROOT/live-after.json" \
      "$TMP_ROOT/plan-live-contract.json"
    validate_image_release_context_consumer_images \
      "$TMP_ROOT/image-release-context.json" \
      "$(jq -c '.desired_consumer_images' "$TMP_ROOT/core.json")"
    build_scoped_release_state_contract \
      "$TMP_ROOT/state-after.json" \
      "$TMP_ROOT/plan-live-contract.json" \
      "$TMP_ROOT/plan-state-contract.json"
    prepare_image_deployment_intent \
      "$STAGE_PLAN" "$TMP_ROOT/image-release-context.json" \
      "$TMP_ROOT/image-deployment-intent.json"
    [ "$(jq -er '.intent_id' "$TMP_ROOT/image-deployment-intent.json")" = \
      "$IMAGE_DEPLOYMENT_INTENT_ID" ] ||
      die "prepared production intentがsaved planのephemeral intentと不一致です"

    ACCOUNT_ID="$(jq -er '.account_id' "$TMP_ROOT/live-after.json")"
    CORE_SHA="$(sha256_file "$TMP_ROOT/core.json")"
    CONFIG_MANIFEST="$TMP_ROOT/config-manifest-plan.txt"
    write_config_manifest "$CONFIG_MANIFEST"
    NOW="$(date +%s)"
    EXPIRES=$((NOW + 3600))
    DESIRED_RULE_STATES="$(jq -n -c \
      --argjson ingest "$DESIRED_INGEST_RULE" \
      --argjson morning "$DESIRED_MORNING_RULE" \
      --argjson canary "$DESIRED_CANARY_RULE" \
      '{ingest:$ingest,morning:$morning,canary:$canary}')"
    jq -n -S \
      --arg kind "terraform-runtime-plan-receipt" \
      --arg guard_version "$GUARD_VERSION" \
      --arg account_id "$ACCOUNT_ID" \
      --arg region "$REGION" \
      --arg project "$PROJECT" \
      --arg environment "$ENVIRONMENT" \
      --arg mode "$MODE" \
      --arg migration_id "$MIGRATION_ID" \
      --arg migration_kind "$MIGRATION_KIND" \
      --arg image_deployment_intent_id "$IMAGE_DEPLOYMENT_INTENT_ID" \
      --arg image_release_context_sha256 \
        "$(sha256_file "$TMP_ROOT/image-release-context.json")" \
      --arg image_deployment_intent_receipt_sha256 \
        "$(sha256_file "$TMP_ROOT/image-deployment-intent.json")" \
      --argjson image_deployment_intent_expires_at "$(
        jq -er '.authorization_expires_at' \
          "$TMP_ROOT/image-deployment-intent.json"
      )" \
      --arg preflight_receipt_path "$PREFLIGHT_RECEIPT" \
      --arg preflight_receipt_sha256 "$PREFLIGHT_SHA256" \
      --arg alarm_delivery_receipt_path "$ALARM_DELIVERY_RECEIPT" \
      --arg alarm_delivery_receipt_sha256 "$ALARM_DELIVERY_RECEIPT_SHA256" \
      --arg versioning_receipt_path "$VERSIONING_RECEIPT" \
      --arg versioning_receipt_sha256 "$VERSIONING_RECEIPT_SHA256" \
      --arg log_readiness_receipt_path "$LOG_READINESS_RECEIPT" \
      --arg log_readiness_receipt_sha256 "$LOG_READINESS_RECEIPT_SHA256" \
      --arg alarm_migration_receipt_path "$ALARM_MIGRATION_RECEIPT" \
      --arg alarm_migration_receipt_sha256 \
        "$ALARM_MIGRATION_RECEIPT_SHA256" \
      --arg prior_apply_receipt_path "$PRIOR_APPLY_RECEIPT" \
      --arg prior_apply_receipt_sha256 "$PRIOR_APPLY_RECEIPT_SHA256" \
      --arg media_cutover_receipt_path "$MEDIA_CUTOVER_RECEIPT" \
      --arg media_cutover_receipt_sha256 \
        "$MEDIA_CUTOVER_RECEIPT_SHA256" \
      --argjson created_at_epoch "$NOW" \
      --argjson expires_at_epoch "$EXPIRES" \
      --arg git_commit "$(git_commit)" \
      --arg guard_script_sha256 "$(sha256_file "$SCRIPT_PATH")" \
      --arg guard_jq_sha256 "$(sha256_file "$GUARD_JQ")" \
      --arg migration_manifest_sha256 "$(sha256_file "$MIGRATION_FILE")" \
      --arg migration_contract_sha256 "$MIGRATION_CONTRACT_SHA256" \
      --arg reviewed_plan_sha256 "$REVIEWED_PLAN_SHA256" \
      --arg config_manifest_sha256 "$(sha256_file "$CONFIG_MANIFEST")" \
      --arg live_openclaw_image "$LIVE_OPENCLAW_IMAGE" \
      --arg live_mcp_image "$LIVE_MCP_IMAGE" \
      --arg live_x_image "$LIVE_X_IMAGE" \
      --arg live_tiktok_image "$LIVE_TIKTOK_IMAGE" \
      --arg desired_openclaw_image "$DESIRED_OPENCLAW_IMAGE" \
      --arg desired_mcp_image "$DESIRED_MCP_IMAGE" \
      --arg desired_x_image "$DESIRED_X_IMAGE" \
      --arg desired_tiktok_image "$DESIRED_TIKTOK_IMAGE" \
      --argjson live_consumer_images "$(
        jq -c '.live_consumer_images' "$TMP_ROOT/core.json"
      )" \
      --argjson desired_consumer_images "$(
        jq -c '.desired_consumer_images' "$TMP_ROOT/core.json"
      )" \
      --argjson live_rule_states "$LIVE_RULE_STATES" \
      --argjson desired_rule_states "$DESIRED_RULE_STATES" \
      --argjson hmac_transition_epoch "$TRANSITION_EPOCH" \
      --arg hmac_transition_sha256 "$(sha256_file "$TMP_ROOT/hmac-transition.json")" \
      --arg plan_path "$PLAN" \
      --arg receipt_path "$RECEIPT" \
      --arg var_file "$VAR_FILE" \
      --arg var_file_sha256 "$VAR_SHA" \
      --arg plan_sha256 "$PLAN_SHA" \
      --arg live_fingerprint_sha256 "$AFTER_SHA" \
      --arg runtime_inventory_sha256 "$(
        jq -er '.inventory_sha256' "$TMP_ROOT/inventory-after.json"
      )" \
      --arg runtime_guard_sha256 "$CORE_SHA" \
      --slurpfile state_contract "$TMP_ROOT/plan-state-contract.json" \
      '{
        kind:$kind,
        guard_version:$guard_version,
        account_id:$account_id,
        region:$region,
        project:$project,
        environment:$environment,
        mode:$mode,
        migration_id:$migration_id,
        migration_kind:$migration_kind,
        image_deployment_intent_id:$image_deployment_intent_id,
        image_release_context_sha256:$image_release_context_sha256,
        image_deployment_intent_receipt_sha256:
          $image_deployment_intent_receipt_sha256,
        image_deployment_intent_expires_at:
          $image_deployment_intent_expires_at,
        preflight_receipt_path:$preflight_receipt_path,
        preflight_receipt_sha256:$preflight_receipt_sha256,
        alarm_delivery_receipt_path:$alarm_delivery_receipt_path,
        alarm_delivery_receipt_sha256:$alarm_delivery_receipt_sha256,
        versioning_receipt_path:$versioning_receipt_path,
        versioning_receipt_sha256:$versioning_receipt_sha256,
        log_readiness_receipt_path:$log_readiness_receipt_path,
        log_readiness_receipt_sha256:$log_readiness_receipt_sha256,
        alarm_migration_receipt_path:$alarm_migration_receipt_path,
        alarm_migration_receipt_sha256:$alarm_migration_receipt_sha256,
        prior_apply_receipt_path:$prior_apply_receipt_path,
        prior_apply_receipt_sha256:$prior_apply_receipt_sha256,
        media_cutover_receipt_path:$media_cutover_receipt_path,
        media_cutover_receipt_sha256:$media_cutover_receipt_sha256,
        created_at_epoch:$created_at_epoch,
        expires_at_epoch:$expires_at_epoch,
        git_commit:$git_commit,
        guard_script_sha256:$guard_script_sha256,
        guard_jq_sha256:$guard_jq_sha256,
        migration_manifest_sha256:$migration_manifest_sha256,
        migration_contract_sha256:$migration_contract_sha256,
        reviewed_plan_sha256:$reviewed_plan_sha256,
        config_manifest_sha256:$config_manifest_sha256,
        images:{
          live:{
            openclaw:$live_openclaw_image,
            mcp:$live_mcp_image,
            x_buzz:$live_x_image,
            tiktok:$live_tiktok_image
          },
          desired:{
            openclaw:$desired_openclaw_image,
            mcp:$desired_mcp_image,
            x_buzz:$desired_x_image,
            tiktok:$desired_tiktok_image
          },
          consumers:{
            live:$live_consumer_images,
            desired:$desired_consumer_images
          }
        },
        rule_states:{
          live:$live_rule_states,
          desired:$desired_rule_states
        },
        hmac_transition_epoch:$hmac_transition_epoch,
        hmac_transition_sha256:$hmac_transition_sha256,
        plan_path:$plan_path,
        receipt_path:$receipt_path,
        var_file:$var_file,
        var_file_sha256:$var_file_sha256,
        plan_sha256:$plan_sha256,
        live_fingerprint_sha256:$live_fingerprint_sha256,
        runtime_inventory_sha256:$runtime_inventory_sha256,
        runtime_guard_sha256:$runtime_guard_sha256,
        state_contract:$state_contract[0]
      }' > "$STAGE_RECEIPT"
    chmod 600 "$STAGE_RECEIPT"
    jq -e . "$STAGE_RECEIPT" >/dev/null || die "receipt生成に失敗しました"
    [ "$(sha256_file "$STAGE_PLAN")" = "$PLAN_SHA" ] || die "publish直前にplanが変化しました"
    # stageは出力先と同一private directoryに置く。hard linkの作成は既存pathを
    # 上書きせず原子的に失敗するため、check-then-mvのsymlink/TOCTOU窓を作らない。
    STAGE_PLAN_IDENTITY="$(stat_identity "$STAGE_PLAN")"
    STAGE_RECEIPT_IDENTITY="$(stat_identity "$STAGE_RECEIPT")"
    ln "$STAGE_PLAN" "$PLAN" || die "publish先plan pathを原子的に確保できません"
    PLAN_CREATED="true"
    [ "$(stat_identity "$PLAN")" = "$STAGE_PLAN_IDENTITY" ] ||
      die "publish先plan pathの同一性を確認できません"
    ln "$STAGE_RECEIPT" "$RECEIPT" || die "publish先receipt pathを原子的に確保できません"
    RECEIPT_CREATED="true"
    [ "$(stat_identity "$RECEIPT")" = "$STAGE_RECEIPT_IDENTITY" ] ||
      die "publish先receipt pathの同一性を確認できません"
    rm -f "$STAGE_PLAN" "$STAGE_RECEIPT"
    chmod 600 "$PLAN" "$RECEIPT"
    [ "$(sha256_file "$PLAN")" = "$PLAN_SHA" ] || die "atomic publish後のplan SHAが不一致です"
    PUBLISHED="true"
    echo "✅ guarded plan: $PLAN"
    echo "   receipt: $RECEIPT"
    echo "   mode/migration: $MODE / ${MIGRATION_ID:-strict-equality}"
    echo "   desired images: openclaw=$DESIRED_OPENCLAW_IMAGE mcp=$DESIRED_MCP_IMAGE"
    echo "                   x-buzz=$DESIRED_X_IMAGE tiktok=$DESIRED_TIKTOK_IMAGE"
    echo "   desired rules: ingest=$DESIRED_INGEST_RULE morning=$DESIRED_MORNING_RULE canary=$DESIRED_CANARY_RULE"
    echo "   plan sha256: $PLAN_SHA"
    ;;

  authorize-media-apply)
    PLAN=""
    RECEIPT=""
    APPLY_ATTEMPT_ID=""
    MEDIA_AUTHORIZATION=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --plan) PLAN="${2:?--plan に値が必要}"; shift 2 ;;
        --receipt) RECEIPT="${2:?--receipt に値が必要}"; shift 2 ;;
        --apply-attempt-id)
          APPLY_ATTEMPT_ID="${2:?--apply-attempt-id に値が必要}"
          shift 2
          ;;
        --out)
          MEDIA_AUTHORIZATION="${2:?--out に値が必要}"
          shift 2
          ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$PLAN" ] && [ -n "$APPLY_ATTEMPT_ID" ] &&
      [ -n "$MEDIA_AUTHORIZATION" ] ||
      die "authorize-media-applyには --plan、--apply-attempt-id、--out が必須です"
    [[ "$APPLY_ATTEMPT_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] ||
      die "apply attempt IDはUUIDv4が必要です"
    PLAN="$(secure_existing_file "$PLAN" 600)"
    RECEIPT="${RECEIPT:-${PLAN}.runtime-guard.json}"
    RECEIPT="$(secure_existing_file "$RECEIPT" 600)"
    MEDIA_AUTHORIZATION="$(secure_new_file "$MEDIA_AUTHORIZATION")"
    need_cmd aws
    need_cmd jq
    need_cmd python3
    ensure_tmp
    assert_trusted_media_attestor_identity
    PLAN_SHA256="$(sha256_file "$PLAN")"
    PLAN_IDENTITY="$(stat_identity "$PLAN")"
    RECEIPT_SHA256="$(sha256_file "$RECEIPT")"
    RECEIPT_IDENTITY="$(stat_identity "$RECEIPT")"
    jq -e \
      --arg version "$GUARD_VERSION" \
      --arg commit "$(git_commit)" \
      --arg plan "$PLAN" \
      --arg receipt "$RECEIPT" \
      --arg plan_sha "$PLAN_SHA256" \
      --argjson now "$(date +%s)" '
      .kind == "terraform-runtime-plan-receipt" and
      .guard_version == $version and
      .git_commit == $commit and
      .mode == "migration" and
      .migration_kind == "runtime" and
      .plan_path == $plan and
      .receipt_path == $receipt and
      .plan_sha256 == $plan_sha and
      (.created_at_epoch | type) == "number" and
      .created_at_epoch <= $now and
      (.expires_at_epoch | type) == "number" and
      .expires_at_epoch > $now and
      .image_deployment_intent_expires_at > $now and
      (.media_cutover_receipt_path | type) == "string" and
      (.media_cutover_receipt_path | length) > 0 and
      (.media_cutover_receipt_sha256 | test("^[0-9a-f]{64}$")) and
      (.migration_contract_sha256 | test("^[0-9a-f]{64}$")) and
      (.reviewed_plan_sha256 | test("^[0-9a-f]{64}$"))
    ' "$RECEIPT" >/dev/null ||
      die "media apply authorizationのplan receipt契約が不正です"
    MEDIA_CUTOVER_RECEIPT="$(
      jq -er '.media_cutover_receipt_path' "$RECEIPT"
    )"
    MEDIA_CUTOVER_RECEIPT="$(
      secure_existing_file "$MEDIA_CUTOVER_RECEIPT" 600
    )"
    [ "$(sha256_file "$MEDIA_CUTOVER_RECEIPT")" = "$(
      jq -er '.media_cutover_receipt_sha256' "$RECEIPT"
    )" ] ||
      die "media apply authorizationのsigned receipt SHA256が不一致です"
    MEDIA_CUTOVER_RECEIPT_SHA256="$(sha256_file "$MEDIA_CUTOVER_RECEIPT")"
    MEDIA_CUTOVER_RECEIPT_IDENTITY="$(
      stat_identity "$MEDIA_CUTOVER_RECEIPT"
    )"
    MEDIA_BINDING="$TMP_ROOT/media-authorization-binding.json"
    media_migration_binding_to_file "$(
      jq -er '.migration_id' "$RECEIPT"
    )" "$MEDIA_BINDING"
    jq -e \
      --slurpfile binding "$MEDIA_BINDING" '
      .image_deployment_intent_id ==
        $binding[0].image_deployment_intent_id and
      .migration_contract_sha256 ==
        $binding[0].migration_contract_sha256 and
      .reviewed_plan_sha256 ==
        $binding[0].reviewed_plan_sha256 and
      .images.desired.tiktok == $binding[0].desired_image
    ' "$RECEIPT" >/dev/null ||
      die "plan receiptとreview済みmedia migration bindingが不一致です"
    initialize_aws_trust
    assert_aws_trust_unchanged
    python3 "$MEDIA_APPLY_AUTHORIZER" \
      --aws-bin "$AWS_BIN" \
      --plan "$PLAN" \
      --media-receipt "$MEDIA_CUTOVER_RECEIPT" \
      --desired-image "$(jq -er '.desired_image' "$MEDIA_BINDING")" \
      --image-deployment-intent-id "$(
        jq -er '.image_deployment_intent_id' "$MEDIA_BINDING"
      )" \
      --migration-contract-sha256 "$(
        jq -er '.migration_contract_sha256' "$MEDIA_BINDING"
      )" \
      --reviewed-plan-sha256 "$(
        jq -er '.reviewed_plan_sha256' "$MEDIA_BINDING"
      )" \
      --apply-attempt-id "$APPLY_ATTEMPT_ID" \
      --control-commit "$(git_commit)" \
      --output "$MEDIA_AUTHORIZATION"
    assert_aws_trust_unchanged
    chmod 600 "$MEDIA_AUTHORIZATION"
    jq -e \
      --arg attempt "$APPLY_ATTEMPT_ID" \
      --arg plan "$PLAN_SHA256" \
      --arg commit "$(git_commit)" \
      --arg intent "$(
        jq -er '.image_deployment_intent_id' "$MEDIA_BINDING"
      )" \
      --arg desired "$(jq -er '.desired_image' "$MEDIA_BINDING")" \
      --arg migration_sha "$(
        jq -er '.migration_contract_sha256' "$MEDIA_BINDING"
      )" \
      --arg reviewed_sha "$(
        jq -er '.reviewed_plan_sha256' "$MEDIA_BINDING"
      )" \
      --arg media_claims "$(
        jq -er '.claims_sha256' "$MEDIA_CUTOVER_RECEIPT"
      )" \
      --arg signature_sha "$(
        jq -er '.signature_sha256' "$MEDIA_CUTOVER_RECEIPT"
      )" \
      --arg kms_key "$(
        jq -er '.kms_key_arn' "$MEDIA_CUTOVER_RECEIPT"
      )" '
      (keys | sort) == ([
        "apply_attempt_id",
        "authorization_sha256",
        "authorized_at_epoch",
        "claims_sha256",
        "control_commit",
        "image_deployment_intent_id",
        "kind",
        "kms_key_arn",
        "lock_lease_expires_at",
        "migration_contract_sha256",
        "plan_sha256",
        "record_id",
        "reviewed_plan_sha256",
        "schema_version",
        "signature_sha256",
        "state"
      ] | sort) and
      .kind == "teamagent-media-apply-authorization" and
      .schema_version == 1 and
      .state == "AUTHORIZED" and
      .record_id == ("media-cutover#" + $intent) and
      .image_deployment_intent_id == $intent and
      .apply_attempt_id == $attempt and
      .plan_sha256 == $plan and
      .claims_sha256 == $media_claims and
      .signature_sha256 == $signature_sha and
      .kms_key_arn == $kms_key and
      .migration_contract_sha256 == $migration_sha and
      .reviewed_plan_sha256 == $reviewed_sha and
      .control_commit == $commit and
      (.authorized_at_epoch | type) == "number" and
      .lock_lease_expires_at > .authorized_at_epoch and
      (.authorization_sha256 | test("^[0-9a-f]{64}$"))
    ' "$MEDIA_AUTHORIZATION" >/dev/null ||
      die "media apply authorization receiptがatomic bindingと不一致です"
    [ "$(jq -cS 'del(.authorization_sha256)' \
      "$MEDIA_AUTHORIZATION" | sha256_text)" = "$(
        jq -er '.authorization_sha256' "$MEDIA_AUTHORIZATION"
      )" ] ||
      die "media apply authorization receipt hashが不正です"
    [ "$(sha256_file "$PLAN")" = "$PLAN_SHA256" ] &&
      [ "$(stat_identity "$PLAN")" = "$PLAN_IDENTITY" ] &&
      [ "$(sha256_file "$RECEIPT")" = "$RECEIPT_SHA256" ] &&
      [ "$(stat_identity "$RECEIPT")" = "$RECEIPT_IDENTITY" ] &&
      [ "$(sha256_file "$MEDIA_CUTOVER_RECEIPT")" = \
        "$MEDIA_CUTOVER_RECEIPT_SHA256" ] &&
      [ "$(stat_identity "$MEDIA_CUTOVER_RECEIPT")" = \
        "$MEDIA_CUTOVER_RECEIPT_IDENTITY" ] ||
      die "media apply authorization中に入力が差替えられました"
    echo "✅ atomic one-use media apply authorized: $MEDIA_AUTHORIZATION"
    ;;

  verify)
    PLAN=""
    RECEIPT=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --plan) PLAN="${2:?--plan に値が必要}"; shift 2 ;;
        --receipt) RECEIPT="${2:?--receipt に値が必要}"; shift 2 ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$PLAN" ] || die "$COMMAND には --plan が必須です"
    need_cmd aws
    need_cmd jq
    need_cmd terraform
    PLAN="$(secure_existing_file "$PLAN" 600)"
    secure_private_dir "$(dirname "$PLAN")" >/dev/null
    RECEIPT="${RECEIPT:-${PLAN}.runtime-guard.json}"
    RECEIPT="$(secure_existing_file "$RECEIPT" 600)"
    [ "$(dirname "$PLAN")" = "$(dirname "$RECEIPT")" ] ||
      die "planとreceiptは同じprivate directoryにある必要があります"
    ensure_tmp
    verify_receipt "$PLAN" "$RECEIPT"
    PLAN_SHA="$(sha256_file "$PLAN")"
    echo "✅ read-only検証完了（適用は行っていません）: $PLAN_SHA"
    ;;

  apply)
    PLAN=""
    RECEIPT=""
    APPLY_RECEIPT=""
    MEDIA_AUTHORIZATION=""
    REQUESTED_APPLY_ATTEMPT_ID=""
    FORCED_ROLLBACK_DM_QA_DEADLINE_EPOCH=""
    AUTOMATION_IDENTITY_OUT=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --plan) PLAN="${2:?--plan に値が必要}"; shift 2 ;;
        --receipt) RECEIPT="${2:?--receipt に値が必要}"; shift 2 ;;
        --media-authorization)
          MEDIA_AUTHORIZATION="${2:?--media-authorization に値が必要}"
          shift 2
          ;;
        --apply-attempt-id)
          REQUESTED_APPLY_ATTEMPT_ID="${2:?--apply-attempt-id に値が必要}"
          shift 2
          ;;
        --forced-rollback-dm-qa-deadline-epoch)
          [ -z "$FORCED_ROLLBACK_DM_QA_DEADLINE_EPOCH" ] ||
            die "--forced-rollback-dm-qa-deadline-epoch は1回だけ指定できます"
          FORCED_ROLLBACK_DM_QA_DEADLINE_EPOCH="$(
            printf '%s' "${2:?--forced-rollback-dm-qa-deadline-epoch に値が必要}"
          )"
          shift 2
          ;;
        --automation-identity-out)
          [ -z "$AUTOMATION_IDENTITY_OUT" ] ||
            die "--automation-identity-out は1回だけ指定できます"
          AUTOMATION_IDENTITY_OUT="$(
            printf '%s' "${2:?--automation-identity-out に値が必要}"
          )"
          shift 2
          ;;
        --out) APPLY_RECEIPT="${2:?--out に値が必要}"; shift 2 ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$PLAN" ] || die "applyには --plan が必須です"
    [ -n "$APPLY_RECEIPT" ] || die "applyには --out APPLY_RECEIPT が必須です"
    if [ -n "$FORCED_ROLLBACK_DM_QA_DEADLINE_EPOCH" ]; then
      [[ "$FORCED_ROLLBACK_DM_QA_DEADLINE_EPOCH" =~ ^[0-9]+$ ]] ||
        die "forced rollback DM QA deadlineはepoch秒で指定してください"
    fi
    need_cmd aws
    need_cmd jq
    need_cmd node
    need_cmd python3
    need_cmd terraform
    PLAN="$(secure_existing_file "$PLAN" 600)"
    secure_private_dir "$(dirname "$PLAN")" >/dev/null
    RECEIPT="${RECEIPT:-${PLAN}.runtime-guard.json}"
    RECEIPT="$(secure_existing_file "$RECEIPT" 600)"
    [ "$(dirname "$PLAN")" = "$(dirname "$RECEIPT")" ] ||
      die "planとreceiptは同じprivate directoryにある必要があります"
    # `jq -e` exits 1 when the expression itself evaluates to false, so the
    # legitimate "this apply carries no media cutover" receipt -- which the
    # schema explicitly allows as an empty path -- was dying here instead of
    # being read as false. Emit the verdict as a string and check it: a parse
    # failure leaves it empty, so the branch still fails closed.
    MEDIA_APPLY_REQUIRED="$(
      jq -r '
        if (.media_cutover_receipt_path // "") != "" then "true" else "false" end
      ' "$RECEIPT"
    )"
    [ "$MEDIA_APPLY_REQUIRED" = "true" ] || [ "$MEDIA_APPLY_REQUIRED" = "false" ] ||
      die "plan receiptのmedia apply契約を判定できません"
    MEDIA_AUTHORIZATION_SHA256=""
    MEDIA_AUTHORIZATION_IDENTITY=""
    if [ "$MEDIA_APPLY_REQUIRED" = "true" ]; then
      [ -n "$MEDIA_AUTHORIZATION" ] &&
        [ -n "$REQUESTED_APPLY_ATTEMPT_ID" ] ||
        die "legacy→generic applyにはmedia authorizationと同じapply attempt IDが必須です"
      [[ "$REQUESTED_APPLY_ATTEMPT_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] ||
        die "media apply attempt IDはUUIDv4が必要です"
      MEDIA_AUTHORIZATION="$(
        secure_existing_file "$MEDIA_AUTHORIZATION" 600
      )"
      MEDIA_AUTHORIZATION_SHA256="$(sha256_file "$MEDIA_AUTHORIZATION")"
      MEDIA_AUTHORIZATION_IDENTITY="$(stat_identity "$MEDIA_AUTHORIZATION")"
      jq -e \
        --arg attempt "$REQUESTED_APPLY_ATTEMPT_ID" \
        --arg plan "$(sha256_file "$PLAN")" \
        --arg intent "$(jq -er \
          '.image_deployment_intent_id' "$RECEIPT")" \
        --arg migration_sha "$(jq -er \
          '.migration_contract_sha256' "$RECEIPT")" \
        --arg reviewed_sha "$(jq -er \
          '.reviewed_plan_sha256' "$RECEIPT")" \
        --arg commit "$(git_commit)" '
        .kind == "teamagent-media-apply-authorization" and
        .schema_version == 1 and
        .state == "AUTHORIZED" and
        .record_id == ("media-cutover#" + $intent) and
        .image_deployment_intent_id == $intent and
        .apply_attempt_id == $attempt and
        .plan_sha256 == $plan and
        .migration_contract_sha256 == $migration_sha and
        .reviewed_plan_sha256 == $reviewed_sha and
        .control_commit == $commit and
        (.claims_sha256 | test("^[0-9a-f]{64}$")) and
        (.signature_sha256 | test("^[0-9a-f]{64}$")) and
        (.kms_key_arn |
          test(
            "^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-fA-F-]{36}$"
          )) and
        (.authorization_sha256 | test("^[0-9a-f]{64}$"))
      ' "$MEDIA_AUTHORIZATION" >/dev/null ||
        die "media authorizationがplan/intent/attemptと不一致です"
      [ "$(jq -cS 'del(.authorization_sha256)' \
        "$MEDIA_AUTHORIZATION" | sha256_text)" = "$(
          jq -er '.authorization_sha256' "$MEDIA_AUTHORIZATION"
        )" ] ||
        die "media authorization hashが不正です"
    else
      [ -z "$MEDIA_AUTHORIZATION" ] &&
        [ -z "$REQUESTED_APPLY_ATTEMPT_ID" ] ||
        die "media切替を含まないapplyへmedia authorizationを指定できません"
    fi
    APPLY_RECEIPT="$(secure_new_file "$APPLY_RECEIPT")"
    [ "$(dirname "$PLAN")" = "$(dirname "$APPLY_RECEIPT")" ] ||
      die "apply receiptもplanと同じprivate directoryに置いてください"
    ensure_tmp
    assert_trusted_automation_identity "$AUTOMATION_IDENTITY_OUT"
    TERRAFORM_BIN="$(realpath "$(command -v terraform)")"
    assert_regular_nonwritable "$TERRAFORM_BIN"
    [[ "$("$TERRAFORM_BIN" version | head -n 1)" =~ ^Terraform[[:space:]]v1[.] ]] ||
      die "review済みTerraform v1だけを使用できます"
    ORIGINAL_PLAN="$PLAN"
    STAGED_PLAN="$TMP_ROOT/staged-plan.tfplan"
    STAGE_RESULT="$(
      python3 "$PLAN_STAGER" \
        --source "$ORIGINAL_PLAN" \
        --destination "$STAGED_PLAN"
    )" || die "saved planをprivate inodeへ固定できません"
    STAGED_PLAN_SHA256="$(
      jq -er 'select(.ok == true) | .sha256 | select(test("^[a-f0-9]{64}$"))' \
        <<<"$STAGE_RESULT"
    )" || die "staged saved planのdigestが不正です"
    [ "$STAGED_PLAN_SHA256" = "$(sha256_file "$STAGED_PLAN")" ] ||
      die "staged saved planのdigestが一致しません"
    STAGED_PLAN_IDENTITY="$(stat_identity "$STAGED_PLAN")"
    PLAN="$STAGED_PLAN"
    RECOVERY_INTENT_ID="$(
      jq -er '.image_deployment_intent_id' "$RECEIPT"
    )" || die "plan receiptのdeployment intent IDが不正です"
    RECOVERY_RESULT="$TMP_ROOT/deployment-finalization-recovery.json"
    RECOVERY_ERROR="$TMP_ROOT/deployment-finalization-recovery.err"
    if python3 "$DEPLOYMENT_APPLY_FINALIZER" recover \
      --aws-bin "$AWS_BIN" \
      --intent-id "$RECOVERY_INTENT_ID" \
      --plan-sha256 "$STAGED_PLAN_SHA256" \
      --out "$APPLY_RECEIPT" > "$RECOVERY_RESULT" 2> "$RECOVERY_ERROR"; then
      jq -e \
        --arg intent "$RECOVERY_INTENT_ID" \
        --arg plan "$STAGED_PLAN_SHA256" '
        .ok == true and
        .state == "RECOVERED" and
        .intent_id == $intent and
        .plan_sha256 == $plan and
        (.apply_attempt_id |
          test("^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")) and
        (.apply_receipt_sha256 | test("^[0-9a-f]{64}$")) and
        (.deployment_finalization_receipt_sha256 |
          test("^[0-9a-f]{64}$"))
      ' "$RECOVERY_RESULT" >/dev/null ||
        die "recovered apply receiptのfinalization bindingが不正です"
      [ "$(sha256_file "$APPLY_RECEIPT")" = "$(
        jq -er '.apply_receipt_sha256' "$RECOVERY_RESULT"
      )" ] ||
        die "recovered apply receiptのdigestが不一致です"
      echo "✅ previously committed guarded apply receipt recovered: $APPLY_RECEIPT"
      exit 0
    else
      RECOVERY_STATUS=$?
      [ "$RECOVERY_STATUS" -eq 3 ] ||
        die "durable apply finalizationが破損または照合不能です"
      [ ! -e "$APPLY_RECEIPT" ] && [ ! -L "$APPLY_RECEIPT" ] ||
        die "未確定recoveryがapply receipt pathを変更しました"
    fi
    if [ "$MEDIA_APPLY_REQUIRED" = "true" ]; then
      APPLY_ATTEMPT_ID="$REQUESTED_APPLY_ATTEMPT_ID"
      MEDIA_EXPECTED_STATUS="CONSUMED"
    else
      APPLY_ATTEMPT_ID="$(new_uuid_v4)"
      MEDIA_EXPECTED_STATUS="READY"
    fi
    GATE_PLAN="$PLAN"
    GATE_LOCK_RECEIPT="$TMP_ROOT/provenance-shared-lock.json"
    GATE_PREFLIGHT_RECEIPT="$TMP_ROOT/provenance-preflight.json"
    GATE_LOCK_ACQUIRED="false"
    if [ "$MEDIA_APPLY_REQUIRED" = "true" ]; then
      GATE_LOCK_ACQUIRED="true"
    fi
    GATE_OUTCOME_RECORDED="false"
    GATE_HEARTBEAT_PID=""
    APPLY_RECEIPT_PUBLISHED="false"
    COMPOSITE_FINALIZED="false"
    EVENTBRIDGE_SAGA_STARTED="false"
    EVENTBRIDGE_SAGA_FINISHED="false"
    ECS_SERVICE_SAGA_STARTED="false"
    ECS_SERVICE_SAGA_FINISHED="false"
    ECS_SERVICE_SAGA_BEGIN_RECEIPT="$TMP_ROOT/ecs-service-saga-begin.json"
    ECS_SERVICE_SAGA_VERIFY_RECEIPT="$TMP_ROOT/ecs-service-saga-verify.json"
    ECS_SERVICE_SAGA_RESTORE_RECEIPT="$TMP_ROOT/ecs-service-saga-restore.json"
    EVENTBRIDGE_SAGA_VERIFY_RECEIPT="$TMP_ROOT/eventbridge-saga-verify.json"
    APPLY_DRAFT="$TMP_ROOT/apply-receipt-draft.json"
    FINALIZATION_RESULT="$TMP_ROOT/deployment-finalization-result.json"
    OPENCLAW_POST_APPLY_STARTED="false"
    OPENCLAW_ROLLOUT_REQUIRED="false"
    OPENCLAW_PREVIOUS_TASK_DEFINITION=""
    OPENCLAW_NEW_TASK_DEFINITION="AUTO"
    MCP_NEW_TASK_DEFINITION=""
    OPENCLAW_ROLLOUT_RESULT="$TMP_ROOT/openclaw-rollout-result.json"
    OPENCLAW_ROLLBACK_RESULT="$TMP_ROOT/openclaw-rollback-result.json"
    POST_APPLY_SERVICE_PROBE_RESULT="$TMP_ROOT/post-apply-service-probe.json"
    OPENCLAW_EVIDENCE_KMS_KEY_ARN=""
    OPENCLAW_SIGNING_KMS_KEY_ARN=""
    start_gate_heartbeat() {
      local parent_pid="$$"
      bash "$IMAGE_GATE_RUNNER" heartbeat-deployment-lock \
        --plan "$GATE_PLAN" \
        --apply-attempt-id "$APPLY_ATTEMPT_ID" >/dev/null
      (
        trap - EXIT
        while sleep 30; do
          bash "$IMAGE_GATE_RUNNER" heartbeat-deployment-lock \
            --plan "$GATE_PLAN" \
            --apply-attempt-id "$APPLY_ATTEMPT_ID" >/dev/null ||
            {
              kill -TERM "$parent_pid"
              exit 75
            }
        done
      ) &
      GATE_HEARTBEAT_PID="$!"
    }
    stop_gate_heartbeat() {
      [ -n "$GATE_HEARTBEAT_PID" ] || return 0
      kill "$GATE_HEARTBEAT_PID" >/dev/null 2>&1 || true
      wait "$GATE_HEARTBEAT_PID" >/dev/null 2>&1 || true
      GATE_HEARTBEAT_PID=""
    }
    recover_committed_finalization() {
      local recovery_out="$APPLY_RECEIPT"
      local recovery_result="$TMP_ROOT/cleanup-finalization-recovery.json"
      local recovery_error="$TMP_ROOT/cleanup-finalization-recovery.err"
      local existing_output="false"
      if [ -e "$APPLY_RECEIPT" ] || [ -L "$APPLY_RECEIPT" ]; then
        [ ! -L "$APPLY_RECEIPT" ] && [ -f "$APPLY_RECEIPT" ] ||
          return 2
        recovery_out="$TMP_ROOT/cleanup-recovered-apply-receipt.json"
        existing_output="true"
      fi
      python3 "$DEPLOYMENT_APPLY_FINALIZER" recover \
        --aws-bin "$AWS_BIN" \
        --intent-id "$RECOVERY_INTENT_ID" \
        --plan-sha256 "$STAGED_PLAN_SHA256" \
        --apply-attempt-id "$APPLY_ATTEMPT_ID" \
        --out "$recovery_out" > "$recovery_result" 2> "$recovery_error"
      local recovery_status=$?
      [ "$recovery_status" -eq 0 ] || return "$recovery_status"
      jq -e \
        --arg intent "$RECOVERY_INTENT_ID" \
        --arg plan "$STAGED_PLAN_SHA256" \
        --arg attempt "$APPLY_ATTEMPT_ID" '
        .ok == true and
        .state == "RECOVERED" and
        .intent_id == $intent and
        .plan_sha256 == $plan and
        .apply_attempt_id == $attempt and
        (.apply_receipt_sha256 | test("^[0-9a-f]{64}$")) and
        (.deployment_finalization_receipt_sha256 |
          test("^[0-9a-f]{64}$"))
      ' "$recovery_result" >/dev/null || return 2
      [ "$(sha256_file "$recovery_out")" = "$(
        jq -er '.apply_receipt_sha256' "$recovery_result"
      )" ] || return 2
      if [ "$existing_output" = "true" ]; then
        [ "$(sha256_file "$APPLY_RECEIPT")" = "$(
          sha256_file "$recovery_out"
        )" ] || return 2
        rm -f "$recovery_out"
      fi
      APPLY_RECEIPT_PUBLISHED="true"
      COMPOSITE_FINALIZED="true"
      EVENTBRIDGE_SAGA_FINISHED="true"
      ECS_SERVICE_SAGA_FINISHED="true"
      GATE_OUTCOME_RECORDED="true"
      GATE_LOCK_ACQUIRED="false"
      return 0
    }
    restore_lambda_dispatcher_baselines() {
      local dispatcher function family function_arn expected observed revision_id
      local restore_failed="false"
      for dispatcher in x_buzz tiktok; do
        case "$dispatcher" in
          x_buzz)
            function="${PROJECT}-${ENVIRONMENT}-x-buzz-dispatch"
            family="${PROJECT}-${ENVIRONMENT}-x-buzz-worker"
            ;;
          tiktok)
            function="${PROJECT}-${ENVIRONMENT}-tiktok-acquire-dispatch"
            family="${PROJECT}-${ENVIRONMENT}-tiktok-acquire"
            ;;
        esac
        function_arn="arn:aws:lambda:${REGION}:${EXPECTED_ACCOUNT_ID}:function:${function}"
        expected="$TMP_ROOT/cleanup-${dispatcher}-lambda-environment.json"
        observed="$TMP_ROOT/cleanup-${dispatcher}-lambda-observed.json"
        if ! jq -n -S \
          --arg dispatcher "$dispatcher" \
          --slurpfile live "$TMP_ROOT/verify/live-after.json" '{
            Variables:$live[0].dispatchers[$dispatcher].critical.environment
          }' > "$expected"; then
          restore_failed="true"
          continue
        fi
        if ! jq -e \
          --arg dispatcher "$dispatcher" \
          --arg function "$function" \
          --arg function_arn "$function_arn" \
          --arg family "$family" \
          --slurpfile expected "$expected" '
          .dispatchers[$dispatcher].critical.function_name == $function and
          .dispatchers[$dispatcher].critical.function_arn == $function_arn and
          .dispatchers[$dispatcher].critical.environment ==
            $expected[0].Variables and
          .dispatchers[$dispatcher].task_definition ==
            $expected[0].Variables.TASKDEF_ARN and
          ($expected[0] | keys) == ["Variables"] and
          ($expected[0].Variables | type) == "object" and
          ($expected[0].Variables | to_entries |
            all(.[];
              (.key | type) == "string" and
              (.value | type) == "string"
            )) and
          ($expected[0].Variables.TASKDEF_ARN |
            test(
              "^arn:aws:ecs:ap-northeast-1:718959508629:"
              + "task-definition/" + $family + ":[1-9][0-9]*$"
            ))
        ' "$TMP_ROOT/verify/live-after.json" >/dev/null; then
          restore_failed="true"
          continue
        fi
        if ! aws_cli lambda get-function-configuration \
          --function-name "$function" --output json > "$observed"; then
          restore_failed="true"
          continue
        fi
        if [ "$(jq -r '.LastUpdateStatus // ""' "$observed")" = \
          "InProgress" ]; then
          aws_cli lambda wait function-updated-v2 \
            --function-name "$function" >/dev/null 2>&1 || true
          if ! aws_cli lambda get-function-configuration \
            --function-name "$function" --output json > "$observed"; then
            restore_failed="true"
            continue
          fi
        fi
        if jq -e \
          --arg function "$function" \
          --arg function_arn "$function_arn" \
          --slurpfile expected "$expected" '
          .FunctionName == $function and
          .FunctionArn == $function_arn and
          .State == "Active" and
          .LastUpdateStatus == "Successful" and
          .Environment == $expected[0]
        ' "$observed" >/dev/null; then
          continue
        fi
        revision_id="$(
          jq -er '
            .RevisionId |
            select(
              type == "string" and
              test("^[A-Za-z0-9._~+/=-]{1,1024}$") and
              . != "null" and . != "None"
            )
          ' "$observed"
        )" || {
          restore_failed="true"
          continue
        }
        if ! aws_cli lambda update-function-configuration \
          --function-name "$function" \
          --revision-id "$revision_id" \
          --environment "file://$expected" \
          --output json > "$observed"; then
          restore_failed="true"
          continue
        fi
        if ! aws_cli lambda wait function-updated-v2 \
          --function-name "$function" >/dev/null; then
          restore_failed="true"
          continue
        fi
        if ! aws_cli lambda get-function-configuration \
          --function-name "$function" --output json > "$observed"; then
          restore_failed="true"
          continue
        fi
        if ! jq -e \
          --arg function "$function" \
          --arg function_arn "$function_arn" \
          --slurpfile expected "$expected" '
          .FunctionName == $function and
          .FunctionArn == $function_arn and
          .State == "Active" and
          .LastUpdateStatus == "Successful" and
          .Environment == $expected[0]
        ' "$observed" >/dev/null; then
          restore_failed="true"
        fi
      done
      [ "$restore_failed" = "false" ]
    }
    cleanup_apply_command() {
      local status=$?
      local saga_restore_failed="false"
      local ecs_restore_failed="false"
      local lambda_restore_failed="false"
      local openclaw_restore_failed="false"
      local composite_recovery_failed="false"
      set +e
      if [ "$COMPOSITE_FINALIZED" != "true" ] &&
        { [ "$EVENTBRIDGE_SAGA_STARTED" = "true" ] ||
          [ "$ECS_SERVICE_SAGA_STARTED" = "true" ]; }; then
        recover_committed_finalization
        case "$?" in
          0)
            status=0
            echo "✅ committed guarded apply receipt recovered: $APPLY_RECEIPT"
            ;;
          3) ;;
          *) composite_recovery_failed="true" ;;
        esac
      fi
      if [ "$composite_recovery_failed" = "true" ]; then
        stop_gate_heartbeat
        release_deployment_lock
        rm -rf "$TMP_ROOT"
        echo "FATAL: committed deployment finalization requires reconciliation" >&2
        exit 73
      fi
      if [ "$OPENCLAW_POST_APPLY_STARTED" = "true" ] &&
        [ "$OPENCLAW_ROLLOUT_REQUIRED" = "true" ] &&
        [ "$GATE_OUTCOME_RECORDED" != "true" ] &&
        [ "$GATE_LOCK_ACQUIRED" = "true" ] &&
        [ -n "$OPENCLAW_PREVIOUS_TASK_DEFINITION" ]; then
        if [ -z "$GATE_HEARTBEAT_PID" ]; then
          start_gate_heartbeat >/dev/null 2>&1
        fi
        if ! node "$OPENCLAW_ROLLOUT_GATE" --restore-and-verify \
          --new-task-definition "$OPENCLAW_NEW_TASK_DEFINITION" \
          --previous-task-definition "$OPENCLAW_PREVIOUS_TASK_DEFINITION" \
          --receipt-consumption "$GATE_PREFLIGHT_RECEIPT" \
          --apply-attempt-id "$APPLY_ATTEMPT_ID" \
          --plan-sha256 "$STAGED_PLAN_SHA256" \
          --output "$OPENCLAW_ROLLBACK_RESULT"; then
          openclaw_restore_failed="true"
        fi
      fi
      cleanup_post_apply_tasks
      if [ "$ECS_SERVICE_SAGA_STARTED" = "true" ] &&
        [ "$ECS_SERVICE_SAGA_FINISHED" != "true" ]; then
        if ! restore_lambda_dispatcher_baselines; then
          lambda_restore_failed="true"
        fi
      fi
      if [ "$EVENTBRIDGE_SAGA_STARTED" = "true" ] &&
        [ "$EVENTBRIDGE_SAGA_FINISHED" != "true" ]; then
        if python3 "$EVENTBRIDGE_APPLY_SAGA" finish \
          --terraform-bin "$TERRAFORM_BIN" \
          --plan "$GATE_PLAN" \
          --plan-sha256 "$STAGED_PLAN_SHA256" \
          --apply-attempt-id "$APPLY_ATTEMPT_ID" \
          --outcome failed >/dev/null; then
          EVENTBRIDGE_SAGA_FINISHED="true"
        else
          saga_restore_failed="true"
        fi
      fi
      if [ "$ECS_SERVICE_SAGA_STARTED" = "true" ] &&
        [ "$ECS_SERVICE_SAGA_FINISHED" != "true" ]; then
        if python3 "$ECS_SERVICE_APPLY_SAGA" finish \
          --aws-bin "$AWS_BIN" \
          --terraform-bin "$TERRAFORM_BIN" \
          --plan "$GATE_PLAN" \
          --plan-sha256 "$STAGED_PLAN_SHA256" \
          --apply-attempt-id "$APPLY_ATTEMPT_ID" \
          --outcome failed > "$ECS_SERVICE_SAGA_RESTORE_RECEIPT"; then
          ECS_SERVICE_SAGA_FINISHED="true"
        else
          ecs_restore_failed="true"
        fi
      fi
      stop_gate_heartbeat
      if [ "$GATE_LOCK_ACQUIRED" = "true" ]; then
        if [ "$GATE_OUTCOME_RECORDED" != "true" ]; then
          bash "$IMAGE_GATE_RUNNER" mark-deployment-intent-outcome \
            --plan "$GATE_PLAN" \
            --apply-attempt-id "$APPLY_ATTEMPT_ID" \
            --outcome reconcile-required >/dev/null
        fi
        bash "$IMAGE_GATE_RUNNER" release-deployment-lock \
          --plan "$GATE_PLAN" \
          --apply-attempt-id "$APPLY_ATTEMPT_ID" >/dev/null
        GATE_LOCK_ACQUIRED="false"
      fi
      release_deployment_lock
      if [ "$APPLY_RECEIPT_PUBLISHED" != "true" ]; then
        rm -f "$APPLY_RECEIPT"
      fi
      rm -rf "$TMP_ROOT"
      if [ "$saga_restore_failed" = "true" ]; then
        echo "FATAL: EventBridge baseline restoration requires reconciliation" >&2
        exit 70
      fi
      if [ "$openclaw_restore_failed" = "true" ]; then
        echo "FATAL: durable previous OpenClaw revision restoration requires reconciliation" >&2
        exit 71
      fi
      if [ "$lambda_restore_failed" = "true" ]; then
        echo "FATAL: Lambda dispatcher baseline restoration requires reconciliation" >&2
        exit 74
      fi
      if [ "$ecs_restore_failed" = "true" ]; then
        echo "FATAL: durable ECS consumer baseline restoration requires reconciliation" >&2
        exit 72
      fi
      exit "$status"
    }
    trap 'cleanup_apply_command' EXIT

    # Ordinary applies atomically consume the intent and acquire the shared
    # lock here. A media cutover has already consumed READY evidence and moved
    # the same intent/lock to this exact attempt in the independent attestor's
    # single DynamoDB transaction, so only an exact heartbeat is accepted.
    if [ "$MEDIA_APPLY_REQUIRED" = "true" ]; then
      bash "$IMAGE_GATE_RUNNER" heartbeat-deployment-lock \
        --plan "$PLAN" \
        --apply-attempt-id "$APPLY_ATTEMPT_ID" > "$GATE_LOCK_RECEIPT"
    else
      bash "$IMAGE_GATE_RUNNER" acquire-deployment-lock \
        --plan "$PLAN" \
        --apply-attempt-id "$APPLY_ATTEMPT_ID" \
        --control-commit "$(git_commit)" > "$GATE_LOCK_RECEIPT"
    fi
    jq -S -c . "$GATE_LOCK_RECEIPT" > "$GATE_LOCK_RECEIPT.canonical"
    mv "$GATE_LOCK_RECEIPT.canonical" "$GATE_LOCK_RECEIPT"
    chmod 600 "$GATE_LOCK_RECEIPT"
    jq -e \
      --arg attempt "$APPLY_ATTEMPT_ID" '
      .record_id == "lock#teamagent/terraform.tfstate" and
      .record_type == "teamagent.image-release-apply-lock" and
      .state == "LOCKED" and
      .apply_attempt_id == $attempt and
      (.plan_sha256 | test("^[0-9a-f]{64}$")) and
      (.terraform_context_sha256 | test("^[0-9a-f]{64}$"))
    ' "$GATE_LOCK_RECEIPT" >/dev/null ||
      die "provenance shared lock receiptが不正です"
    GATE_LOCK_ACQUIRED="true"
    start_gate_heartbeat
    acquire_deployment_lock

    # The two locks remain held across final live/state/evidence rechecks and
    # the exact private saved-plan apply.
    verify_receipt \
      "$GATE_PLAN" "$RECEIPT" "$ORIGINAL_PLAN" \
      "$MEDIA_EXPECTED_STATUS" "$(
        if [ "$MEDIA_EXPECTED_STATUS" = "CONSUMED" ]; then
          printf '%s' "$APPLY_ATTEMPT_ID"
        fi
      )"
    GATE_PLAN="$TMP_ROOT/verify/plan.tfplan"
    [ "$(sha256_file "$GATE_PLAN")" = "$STAGED_PLAN_SHA256" ] ||
      die "verify pathのsaved plan digestがstaged planと一致しません"
    [ "$(stat_identity "$GATE_PLAN")" = "$STAGED_PLAN_IDENTITY" ] ||
      die "verify pathがstaged planと同一inodeではありません"
    rm -f "$STAGED_PLAN"
    [ ! -e "$STAGED_PLAN" ] && [ ! -L "$STAGED_PLAN" ] ||
      die "saved planの余分なhard linkを除去できません"
    if [ "$MEDIA_APPLY_REQUIRED" = "true" ]; then
      [ "$(sha256_file "$MEDIA_AUTHORIZATION")" = \
        "$MEDIA_AUTHORIZATION_SHA256" ] &&
        [ "$(stat_identity "$MEDIA_AUTHORIZATION")" = \
          "$MEDIA_AUTHORIZATION_IDENTITY" ] ||
        die "final verify中にmedia authorizationが差替えられました"
      jq -e \
        --slurpfile media "$(
          jq -er '.media_cutover_receipt_path' \
            "$TMP_ROOT/verify/receipt.json"
        )" \
        --arg plan "$STAGED_PLAN_SHA256" '
        .plan_sha256 == $plan and
        .claims_sha256 == $media[0].claims_sha256 and
        .signature_sha256 == $media[0].signature_sha256 and
        .kms_key_arn == $media[0].kms_key_arn
      ' "$MEDIA_AUTHORIZATION" >/dev/null ||
        die "media authorizationとverified signed receiptが不一致です"
    fi
    bash "$IMAGE_GATE_RUNNER" validate-deployment-preflight \
      --plan "$GATE_PLAN" \
      --terraform-context "$TMP_ROOT/verify/image-release-context.json" \
      --apply-attempt-id "$APPLY_ATTEMPT_ID" \
      --control-commit "$(git_commit)" > "$GATE_PREFLIGHT_RECEIPT"
    jq -S -c . "$GATE_PREFLIGHT_RECEIPT" \
      > "$GATE_PREFLIGHT_RECEIPT.canonical"
    mv "$GATE_PREFLIGHT_RECEIPT.canonical" "$GATE_PREFLIGHT_RECEIPT"
    chmod 600 "$GATE_PREFLIGHT_RECEIPT"
    jq -e \
      --arg attempt "$APPLY_ATTEMPT_ID" \
      --arg intent "$(
        jq -er '.image_deployment_intent_id' "$TMP_ROOT/verify/receipt.json"
      )" \
      --arg plan "$STAGED_PLAN_SHA256" \
      --arg context_sha "$(
        sha256_file "$TMP_ROOT/verify/image-release-context.json"
      )" '
      .record_id == ("intent#" + $intent) and
      .record_type == "teamagent.image-deployment-intent" and
      .schema_version == 1 and
      .intent_id == $intent and
      .state == "CONSUMED" and
      .apply_attempt_id == $attempt and
      .plan_sha256 == $plan and
      .terraform_context_sha256 == $context_sha
    ' "$GATE_PREFLIGHT_RECEIPT" >/dev/null ||
      die "provenance apply preflightがsaved plan contextと不一致です"
    OPENCLAW_PREVIOUS_TASK_DEFINITION="$(
      jq -er '.taskdefs.openclaw.arn' "$TMP_ROOT/verify/live-after.json"
    )"
    [[ "$OPENCLAW_PREVIOUS_TASK_DEFINITION" =~ ^arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-openclaw:[1-9][0-9]*$ ]] ||
      die "apply前のdurable OpenClaw task revisionが不正です"
    OPENCLAW_ROLLOUT_REQUIRED="$(
      jq -er '
        any(
          .resource_changes[]?;
          .address == "aws_ecs_task_definition.openclaw[0]" and
          .change.actions != ["no-op"] and
          .change.actions != ["read"]
        )
      ' "$TMP_ROOT/verify/plan.json"
    )" ||
      die "saved planからOpenClaw rollout要否を一意に確定できません"

    python3 "$ECS_SERVICE_APPLY_SAGA" begin \
      --aws-bin "$AWS_BIN" \
      --terraform-bin "$TERRAFORM_BIN" \
      --plan "$GATE_PLAN" \
      --plan-sha256 "$STAGED_PLAN_SHA256" \
      --apply-attempt-id "$APPLY_ATTEMPT_ID" \
      > "$ECS_SERVICE_SAGA_BEGIN_RECEIPT" ||
      die "MCP/connect-web ECS rollback baselineをdurableに固定できません"
    chmod 600 "$ECS_SERVICE_SAGA_BEGIN_RECEIPT"
    validate_ecs_service_saga_receipt \
      "$ECS_SERVICE_SAGA_BEGIN_RECEIPT" "APPLYING"
    ECS_SERVICE_SAGA_STARTED="true"

    python3 "$EVENTBRIDGE_APPLY_SAGA" begin \
      --terraform-bin "$TERRAFORM_BIN" \
      --plan "$GATE_PLAN" \
      --plan-sha256 "$STAGED_PLAN_SHA256" \
      --apply-attempt-id "$APPLY_ATTEMPT_ID" >/dev/null ||
      die "EventBridge apply baselineをdurableに固定できません"
    EVENTBRIDGE_SAGA_STARTED="true"

    OPENCLAW_POST_APPLY_STARTED="true"
    stop_gate_heartbeat
    export TEAMAGENT_SAVED_PLAN_PATH="$GATE_PLAN"
    export TEAMAGENT_SAVED_PLAN_SHA256="$STAGED_PLAN_SHA256"
    export TEAMAGENT_SAVED_PLAN_IDENTITY="$STAGED_PLAN_IDENTITY"
    export TEAMAGENT_APPLY_ATTEMPT_ID="$APPLY_ATTEMPT_ID"
    if ! (
      cd "$TF_DIR"
      python3 "$APPLY_SUPERVISOR" \
        --terraform-bin "$TERRAFORM_BIN" \
        --gate-runner "$IMAGE_GATE_RUNNER" \
        --plan "$GATE_PLAN" \
        --plan-sha256 "$STAGED_PLAN_SHA256" \
        --plan-identity "$STAGED_PLAN_IDENTITY" \
        --apply-attempt-id "$APPLY_ATTEMPT_ID"
    ); then
      die "supervised saved-plan apply failed; provenance reconciliation is required"
    fi
    unset TEAMAGENT_SAVED_PLAN_PATH TEAMAGENT_SAVED_PLAN_SHA256
    unset TEAMAGENT_SAVED_PLAN_IDENTITY TEAMAGENT_APPLY_ATTEMPT_ID
    start_gate_heartbeat

    capture_state_contract "$TMP_ROOT/applied-state.json"
    snapshot_live "$TMP_ROOT/applied-live.json"
    capture_complete_runtime_inventory "$TMP_ROOT/applied-inventory.json"
    cp "$TMP_ROOT/verify/plan-live-contract.json" \
      "$TMP_ROOT/pre-live-contract.json"
    cp "$TMP_ROOT/verify/plan-state-contract.json" \
      "$TMP_ROOT/pre-state-contract.json"
    chmod 600 \
      "$TMP_ROOT/pre-live-contract.json" \
      "$TMP_ROOT/pre-state-contract.json"
    build_scoped_release_live_contract \
      "$TMP_ROOT/verify/image-release-context.json" \
      "$TMP_ROOT/applied-live.json" \
      "$TMP_ROOT/post-live-contract.json"
    build_scoped_release_state_contract \
      "$TMP_ROOT/applied-state.json" \
      "$TMP_ROOT/post-live-contract.json" \
      "$TMP_ROOT/post-state-contract.json"
    jq -e --slurpfile receipt "$TMP_ROOT/verify/receipt.json" '
      {
        openclaw:.taskdefs.openclaw.image,
        mcp:.taskdefs.mcp.image,
        x_buzz:.taskdefs.x_buzz.image,
        tiktok:.taskdefs.tiktok.image
      } == $receipt[0].images.desired and
      {
        mcp:.taskdefs.mcp.image,
        connect_web:.taskdefs.connect_web.image,
        openclaw:.taskdefs.openclaw.image,
        canary:.taskdefs.canary.image,
        ingest:.taskdefs.ingest.image,
        morning_digest:.taskdefs.morning.image,
        x_buzz_worker:.taskdefs.x_buzz.image,
        tiktok_acquire:.taskdefs.tiktok.image
      } == $receipt[0].images.consumers.desired and
      {
        ingest:(.rules.ingest.critical.state == "ENABLED"),
        morning:(.rules.morning.critical.state == "ENABLED"),
        canary:(.rules.canary.critical.state == "ENABLED")
      } == $receipt[0].rule_states.desired
    ' "$TMP_ROOT/applied-live.json" >/dev/null ||
      die "apply後live runtimeがsaved planのexact desired stateと不一致です"
    jq -e --slurpfile receipt "$TMP_ROOT/verify/receipt.json" '
      .backend == $receipt[0].state_contract.backend and
      .state.lineage == $receipt[0].state_contract.state.lineage and
      .state.serial > $receipt[0].state_contract.state.serial
    ' "$TMP_ROOT/applied-state.json" >/dev/null ||
      die "apply後backend/workspace/lineage/serial契約が不正です"

    APPLIED_ALARM_RECEIPT="$(
      jq -r '.alarm_delivery_receipt_path' "$TMP_ROOT/verify/receipt.json"
    )"
    APPLIED_VERSIONING_RECEIPT="$(
      jq -r '.versioning_receipt_path' "$TMP_ROOT/verify/receipt.json"
    )"
    APPLIED_READINESS_RECEIPT="$(
      jq -r '.log_readiness_receipt_path' "$TMP_ROOT/verify/receipt.json"
    )"
    APPLIED_ALARM_MIGRATION_RECEIPT="$(
      jq -r '.alarm_migration_receipt_path' "$TMP_ROOT/verify/receipt.json"
    )"
    if [ -n "$APPLIED_ALARM_RECEIPT" ]; then
      verify_alarm_delivery_test_receipt \
        "$APPLIED_ALARM_RECEIPT" "$TMP_ROOT/applied-live.json"
    fi
    if [ -n "$APPLIED_VERSIONING_RECEIPT" ]; then
      verify_versioning_attestation_receipt \
        "$APPLIED_VERSIONING_RECEIPT" "$TMP_ROOT/applied-live.json" \
        "$TMP_ROOT/applied-state.json"
    fi
    if [ -n "$APPLIED_READINESS_RECEIPT" ]; then
      verify_log_readiness_receipt \
        "$APPLIED_READINESS_RECEIPT" "$APPLIED_VERSIONING_RECEIPT" \
        "$TMP_ROOT/applied-live.json"
    fi
    if [ -n "$APPLIED_ALARM_MIGRATION_RECEIPT" ]; then
      verify_alarm_migration_final_receipt \
        "$APPLIED_ALARM_MIGRATION_RECEIPT"
    fi
    APPLIED_BEDROCK_RETENTION_SHA256=""
    if [ "$(jq -er '.mode' "$TMP_ROOT/verify/receipt.json")" = "migration" ]; then
      run_evidence_helper verify-bedrock-retention \
        --output "$TMP_ROOT/applied-bedrock-retention.json"
      APPLIED_BEDROCK_RETENTION_SHA256="$(
        sha256_file "$TMP_ROOT/applied-bedrock-retention.json"
      )"
    else
      jq -n 'null' > "$TMP_ROOT/applied-bedrock-retention.json"
    fi

    run_post_apply_service_probe \
      "$TMP_ROOT/applied-live.json" "$TMP_ROOT/verify/core.json" \
      "$APPLY_ATTEMPT_ID" "$POST_APPLY_SERVICE_PROBE_RESULT"

    OPENCLAW_NEW_TASK_DEFINITION="$(
      jq -er '.taskdefs.openclaw.arn' "$TMP_ROOT/applied-live.json"
    )"
    [[ "$OPENCLAW_NEW_TASK_DEFINITION" =~ ^arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-openclaw:[1-9][0-9]*$ ]] ||
      die "apply後のOpenClaw task revisionが不正です"
    MCP_NEW_TASK_DEFINITION="$(
      jq -er '.taskdefs.mcp.arn' "$TMP_ROOT/applied-live.json"
    )"
    [[ "$MCP_NEW_TASK_DEFINITION" =~ ^arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-mcp:[1-9][0-9]*$ ]] ||
      die "apply後のMCP task revisionが不正です"
    if [ -n "$FORCED_ROLLBACK_DM_QA_DEADLINE_EPOCH" ] ||
      [ "$OPENCLAW_ROLLOUT_REQUIRED" = "true" ]; then
      OPENCLAW_EVIDENCE_KMS_KEY_ARN="$(
        terraform -chdir="$TF_DIR" output -raw \
          openclaw_rollout_evidence_key_arn
      )" ||
        die "OpenClaw rollout evidence KMS keyをstateから固定できません"
      [[ "$OPENCLAW_EVIDENCE_KMS_KEY_ARN" =~ ^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-f-]{36}$ ]] ||
        die "OpenClaw rollout evidence KMS key state bindingが不正です"
    fi
    if [ -n "$FORCED_ROLLBACK_DM_QA_DEADLINE_EPOCH" ] ||
      [ "$OPENCLAW_ROLLOUT_REQUIRED" = "true" ]; then
      OPENCLAW_SIGNING_KMS_KEY_ARN="$(
        terraform -chdir="$TF_DIR" output -raw \
          openclaw_rollout_signing_key_arn
      )" ||
        die "OpenClaw rollout signing KMS keyをstateから固定できません"
      [[ "$OPENCLAW_SIGNING_KMS_KEY_ARN" =~ ^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-f-]{36}$ ]] &&
        [ "$OPENCLAW_EVIDENCE_KMS_KEY_ARN" !=
          "$OPENCLAW_SIGNING_KMS_KEY_ARN" ] ||
        die "OpenClaw rollout KMS key state bindingが不正です"
    fi
    FORCED_ROLLBACK_DM_QA_RESULT=""
    if [ -n "$FORCED_ROLLBACK_DM_QA_DEADLINE_EPOCH" ]; then
      FORCED_ROLLBACK_DM_QA_EVIDENCE_BUCKET="$(
        terraform -chdir="$TF_DIR" output -raw \
          forced_rollback_drill_evidence_bucket
      )" ||
        die "forced rollback evidence bucketをstateから固定できません"
      FORCED_ROLLBACK_DM_QA_EVIDENCE_PREFIX="$(
        terraform -chdir="$TF_DIR" output -raw \
          forced_rollback_drill_evidence_prefix
      )" ||
        die "forced rollback evidence prefixをstateから固定できません"
      [ "$FORCED_ROLLBACK_DM_QA_EVIDENCE_BUCKET" = \
        "teamagent-dev-openclaw-rollout-evidence" ] &&
        [ "$FORCED_ROLLBACK_DM_QA_EVIDENCE_PREFIX" = \
          "forced-rollback-drills/" ] ||
        die "forced rollback DM QA evidence state bindingが不正です"
    fi
    if [ "$OPENCLAW_ROLLOUT_REQUIRED" = "false" ]; then
      [ "$OPENCLAW_PREVIOUS_TASK_DEFINITION" = \
        "$OPENCLAW_NEW_TASK_DEFINITION" ] ||
        die "saved plan外のOpenClaw task revision変更を検出しました"
      jq -n -S -c \
        --argjson schemaVersion 2 \
        --argjson required false \
        --argjson passed true \
        --arg applyAttemptId "$APPLY_ATTEMPT_ID" \
        --arg previousTaskDefinitionArn \
          "$OPENCLAW_PREVIOUS_TASK_DEFINITION" \
        --arg newTaskDefinitionArn "$OPENCLAW_NEW_TASK_DEFINITION" \
        --arg reason "task-definition-unchanged" '{
          schemaVersion:$schemaVersion,
          required:$required,
          passed:$passed,
          applyAttemptId:$applyAttemptId,
          previousTaskDefinitionArn:$previousTaskDefinitionArn,
          newTaskDefinitionArn:$newTaskDefinitionArn,
          reason:$reason
        }' > "$OPENCLAW_ROLLOUT_RESULT"
    else
      [ "$OPENCLAW_PREVIOUS_TASK_DEFINITION" != \
        "$OPENCLAW_NEW_TASK_DEFINITION" ] ||
        die "planned OpenClaw candidateがdistinct live revisionになっていません"
      if ! node "$OPENCLAW_ROLLOUT_GATE" \
        --new-task-definition "$OPENCLAW_NEW_TASK_DEFINITION" \
        --previous-task-definition "$OPENCLAW_PREVIOUS_TASK_DEFINITION" \
        --receipt-consumption "$GATE_PREFLIGHT_RECEIPT" \
        --apply-attempt-id "$APPLY_ATTEMPT_ID" \
        --plan-sha256 "$STAGED_PLAN_SHA256" \
        --evidence-encryption-kms-key-arn \
          "$OPENCLAW_EVIDENCE_KMS_KEY_ARN" \
        --evidence-signing-kms-key-arn \
          "$OPENCLAW_SIGNING_KMS_KEY_ARN" \
        --mcp-task-definition "$MCP_NEW_TASK_DEFINITION" \
        --output "$OPENCLAW_ROLLOUT_RESULT"; then
        die "OpenClaw post-apply gate failed; cleanup must verify the previous revision"
      fi
      jq -e \
        --arg attempt "$APPLY_ATTEMPT_ID" \
        --arg previous "$OPENCLAW_PREVIOUS_TASK_DEFINITION" \
        --arg candidate "$OPENCLAW_NEW_TASK_DEFINITION" \
        --arg automation "$TRUSTED_AUTOMATION_ARN" \
        --arg result_key \
          "rollout-results/$APPLY_ATTEMPT_ID/passed/result.json" \
        --arg signature_key \
          "rollout-results/$APPLY_ATTEMPT_ID/passed/result.sig.json" \
        --arg encryption_kms "$OPENCLAW_EVIDENCE_KMS_KEY_ARN" \
        --arg signing_kms "$OPENCLAW_SIGNING_KMS_KEY_ARN" '
        .schemaVersion == 2 and
        .required == true and .passed == true and
        .applyAttemptId == $attempt and
        .previousTaskDefinitionArn == $previous and
        .newTaskDefinitionArn == $candidate and
        $previous != $candidate and
        .persistedResult.passed == true and
        .persistedResult.automationRoleArn == $automation and
        .persistedResult.applyAttemptId == $attempt and
        .persistedResult.previousTaskDefinitionArn == $previous and
        .persistedResult.newTaskDefinitionArn == $candidate and
        .persistedResult.distinctTaskRevisions == true and
        .persistedResult.runningTasksBeforeSlack.complete == true and
        .persistedResult.runningTasksBeforeSlack.exactCandidateRevision == true and
        (.persistedResult.runningTasksBeforeSlack |
          (.tasks | length) == (.taskArns | length) and
          ([.tasks[].taskArn] | sort) == (.taskArns | sort) and
          all(.tasks[];
            .taskDefinitionArn == $candidate and
            (.taskArn |
              test("^arn:aws:ecs:ap-northeast-1:718959508629:task/teamagent-dev/[0-9a-f]{32}$")) and
            ((.taskArn | split("/") | last) as $task_id |
              .logStreamName == ("openclaw/openclaw/" + $task_id))
          )
        ) and
        (
          (
            .persistedResult.slack.skipped == true and
            .persistedResult.slack.connected == false and
            .persistedResult.slack.mentionReplyExact == false and
            .persistedResult.slack.skipReasonCodes == [
              "slack_self_authored_message_filtered",
              "aila_prompt_injection_defense_rejected_canary"
            ] and
            (.persistedResult.slack | has("candidateLogCorrelation")) and
            .persistedResult.slack.candidateLogCorrelation == null and
            (.persistedResult.slack | has("postedTs") | not) and
            (.persistedResult.slack | has("replyTs") | not) and
            (.persistedResult.slack | has("tokenSha256") | not) and
            (.persistedResult.slack | has("correlationSha256") | not) and
            (.persistedResult.slack |
              has("responseTokenAbsentFromPrompt") | not)
          ) or
          (
            (
              .persistedResult.slack.skipped == false or
              (.persistedResult.slack | has("skipped") | not)
            ) and
            .persistedResult.slack.connected == true and
            .persistedResult.slack.mentionReplyExact == true and
            .persistedResult.slack.responseTokenAbsentFromPrompt == true and
            (.persistedResult.slack | has("skipReasonCodes") | not) and
            .persistedResult.slack.candidateLogCorrelation.matched == true and
            (.persistedResult.slack.candidateLogCorrelation as $correlation |
              any(.persistedResult.runningTasksBeforeSlack.tasks[];
                .taskArn == $correlation.taskArn and
                .logStreamName == $correlation.logStreamName
              )
            )
          )
        ) and
        .persistedResult.runningTasksAfterSlack.complete == true and
        .persistedResult.runningTasksAfterSlack.exactCandidateRevision == true and
        (.persistedResult.runningTasksAfterSlack |
          (.tasks | length) == (.taskArns | length) and
          ([.tasks[].taskArn] | sort) == (.taskArns | sort) and
          all(.tasks[];
            .taskDefinitionArn == $candidate and
            (.taskArn |
              test("^arn:aws:ecs:ap-northeast-1:718959508629:task/teamagent-dev/[0-9a-f]{32}$")) and
            ((.taskArn | split("/") | last) as $task_id |
              .logStreamName == ("openclaw/openclaw/" + $task_id))
          )
        ) and
        .persistedResult.rollbackAuthorization.state == "AUTHORIZED" and
        .persistedResult.rollbackAuthorization.oneUse == true and
        .immutableEvidence.verified == true and
        .immutableEvidence.bucket ==
          "teamagent-dev-openclaw-rollout-evidence" and
        .immutableEvidence.resultKey == $result_key and
        .immutableEvidence.signatureKey == $signature_key and
        (.immutableEvidence.resultVersionId |
          test("^[A-Za-z0-9._~+/=-]{1,1024}$") and
          . != "null" and . != "None") and
        (.immutableEvidence.signatureVersionId |
          test("^[A-Za-z0-9._~+/=-]{1,1024}$") and
          . != "null" and . != "None") and
        (
          .immutableEvidence.resultObjectLockMode == "COMPLIANCE" or
          .immutableEvidence.resultObjectLockMode == "GOVERNANCE"
        ) and
        (
          .immutableEvidence.signatureObjectLockMode == "COMPLIANCE" or
          .immutableEvidence.signatureObjectLockMode == "GOVERNANCE"
        ) and
        .immutableEvidence.encryptionKmsAlias ==
          "alias/teamagent-dev-openclaw-rollout-evidence" and
        .immutableEvidence.encryptionKmsKeyArn == $encryption_kms and
        .immutableEvidence.signingKmsAlias ==
          "alias/teamagent-dev-openclaw-rollout-signing" and
        .immutableEvidence.signingKmsKeyArn == $signing_kms and
        .immutableEvidence.signingAlgorithm == "RSASSA_PSS_SHA_256" and
        .immutableEvidence.signatureValid == true and
        .immutableEvidence.exactVersionDownloadsVerified == true and
        (.immutableEvidence.resultSha256 | test("^[0-9a-f]{64}$")) and
        (.immutableEvidence.signatureSha256 | test("^[0-9a-f]{64}$"))
      ' "$OPENCLAW_ROLLOUT_RESULT" >/dev/null ||
        die "OpenClaw signed immutable rollout result bindingが不正です"
      jq -S -c '.persistedResult' "$OPENCLAW_ROLLOUT_RESULT" \
        > "$TMP_ROOT/openclaw-persisted-result.json"
      [ "$(sha256_file "$TMP_ROOT/openclaw-persisted-result.json")" = "$(
        jq -er '.immutableEvidence.resultSha256' "$OPENCLAW_ROLLOUT_RESULT"
      )" ] ||
        die "OpenClaw immutable result hashがpersisted bytesと不一致です"
    fi
    if [ -n "$FORCED_ROLLBACK_DM_QA_DEADLINE_EPOCH" ]; then
      FORCED_ROLLBACK_DM_QA_RESULT="$TMP_ROOT/forced-rollback-dm-qa-result.json"
      if run_forced_rollback_dm_qa \
        "$TMP_ROOT/applied-live.json" \
        "$APPLY_ATTEMPT_ID" \
        "$FORCED_ROLLBACK_DM_QA_DEADLINE_EPOCH" \
        "$FORCED_ROLLBACK_DM_QA_RESULT" \
        "$FORCED_ROLLBACK_DM_QA_EVIDENCE_BUCKET" \
        "$FORCED_ROLLBACK_DM_QA_EVIDENCE_PREFIX" \
        "$OPENCLAW_EVIDENCE_KMS_KEY_ARN" \
        "$OPENCLAW_SIGNING_KMS_KEY_ARN"; then
        :
      else
        DM_QA_STATUS=$?
        if [ "$DM_QA_STATUS" -eq 124 ]; then
          echo "FATAL: forced rollback DM QA timed out before old-dwell recovery reserve" >&2
          exit 124
        fi
        echo "FATAL: forced rollback DM QA failed; apply saga must not be finalized" >&2
        exit 24
      fi
      jq -S -c \
        --slurpfile dm_qa "$FORCED_ROLLBACK_DM_QA_RESULT" \
        '. + {dmQa:$dm_qa[0]}' \
        "$OPENCLAW_ROLLOUT_RESULT" \
        > "$OPENCLAW_ROLLOUT_RESULT.with-dm-qa"
      mv "$OPENCLAW_ROLLOUT_RESULT.with-dm-qa" \
        "$OPENCLAW_ROLLOUT_RESULT"
      jq -e \
        --arg attempt "$APPLY_ATTEMPT_ID" \
        --arg openclaw "$OPENCLAW_NEW_TASK_DEFINITION" \
        --arg mcp "$MCP_NEW_TASK_DEFINITION" '
        .dmQa.kind == "teamagent-forced-rollback-dm-qa-result" and
        .dmQa.schema_version == 1 and
        .dmQa.result == "PASSED" and
        .dmQa.applyAttemptId == $attempt and
        .dmQa.openclawTaskDefinitionArn == $openclaw and
        .dmQa.mcpTaskDefinitionArn == $mcp and
        .dmQa.locator.object_lock_mode == "COMPLIANCE" and
        .dmQa.locator.signature.verified == true and
        .dmQa.locator.exact_version_redownload.bytes_match == true
      ' "$OPENCLAW_ROLLOUT_RESULT" >/dev/null ||
        die "forced rollback DM QA result was not bound to the apply result"
      if ! ensure_forced_rollback_dm_qa_recovery_reserve "DM QA"; then
        exit 124
      fi
    fi
    jq -S -c . "$OPENCLAW_ROLLOUT_RESULT" \
      > "$OPENCLAW_ROLLOUT_RESULT.canonical"
    mv "$OPENCLAW_ROLLOUT_RESULT.canonical" "$OPENCLAW_ROLLOUT_RESULT"
    chmod 600 "$OPENCLAW_ROLLOUT_RESULT"

    python3 "$EVENTBRIDGE_APPLY_SAGA" verify \
      --terraform-bin "$TERRAFORM_BIN" \
      --plan "$GATE_PLAN" \
      --plan-sha256 "$STAGED_PLAN_SHA256" \
      --apply-attempt-id "$APPLY_ATTEMPT_ID" \
      > "$EVENTBRIDGE_SAGA_VERIFY_RECEIPT" ||
      die "EventBridge 3 rule apply結果を非破壊検証できません"
    chmod 600 "$EVENTBRIDGE_SAGA_VERIFY_RECEIPT"
    validate_eventbridge_saga_receipt \
      "$EVENTBRIDGE_SAGA_VERIFY_RECEIPT" "verified_applied"
    if ! ensure_forced_rollback_dm_qa_recovery_reserve \
      "EventBridge verification"; then
      exit 124
    fi

    python3 "$ECS_SERVICE_APPLY_SAGA" verify \
      --aws-bin "$AWS_BIN" \
      --terraform-bin "$TERRAFORM_BIN" \
      --plan "$GATE_PLAN" \
      --plan-sha256 "$STAGED_PLAN_SHA256" \
      --apply-attempt-id "$APPLY_ATTEMPT_ID" \
      > "$ECS_SERVICE_SAGA_VERIFY_RECEIPT" ||
      die "MCP/connect-web ECS steady stateを非破壊検証できません"
    chmod 600 "$ECS_SERVICE_SAGA_VERIFY_RECEIPT"
    validate_ecs_service_saga_receipt \
      "$ECS_SERVICE_SAGA_VERIFY_RECEIPT" "VERIFIED_APPLIED"
    [ "$(jq -er '.baseline_sha256' \
      "$ECS_SERVICE_SAGA_BEGIN_RECEIPT")" = "$(
        jq -er '.baseline_sha256' "$ECS_SERVICE_SAGA_VERIFY_RECEIPT"
      )" ] &&
      [ "$(jq -er '.planned_sha256' \
        "$ECS_SERVICE_SAGA_BEGIN_RECEIPT")" = "$(
          jq -er '.planned_sha256' "$ECS_SERVICE_SAGA_VERIFY_RECEIPT"
        )" ] ||
      die "ECS sagaのbegin/verify bindingが不一致です"

    MEDIA_AUTHORIZATION_FOR_RECEIPT="$TMP_ROOT/media-authorization.json"
    if [ "$MEDIA_APPLY_REQUIRED" = "true" ]; then
      [ "$(sha256_file "$MEDIA_AUTHORIZATION")" = \
        "$MEDIA_AUTHORIZATION_SHA256" ] &&
        [ "$(stat_identity "$MEDIA_AUTHORIZATION")" = \
          "$MEDIA_AUTHORIZATION_IDENTITY" ] ||
        die "apply完了前にmedia authorizationが差替えられました"
      cp "$MEDIA_AUTHORIZATION" "$MEDIA_AUTHORIZATION_FOR_RECEIPT"
      [ "$(sha256_file "$MEDIA_AUTHORIZATION_FOR_RECEIPT")" = \
        "$MEDIA_AUTHORIZATION_SHA256" ] ||
        die "apply receipt用media authorization copyが不一致です"
    else
      jq -n 'null' > "$MEDIA_AUTHORIZATION_FOR_RECEIPT"
    fi
    chmod 600 "$MEDIA_AUTHORIZATION_FOR_RECEIPT"
    jq -n -S \
      --arg kind "terraform-runtime-apply-receipt-draft" \
      --argjson schema_version 7 \
      --arg guard_version "$GUARD_VERSION" \
      --arg account_id "$EXPECTED_ACCOUNT_ID" \
      --arg region "$REGION" \
      --arg git_commit "$(git_commit)" \
      --arg status "verified_pending_finalization" \
      --arg migration_kind "$(
        jq -r '.migration_kind' "$TMP_ROOT/verify/receipt.json"
      )" \
      --arg migration_id "$(
        jq -r '.migration_id' "$TMP_ROOT/verify/receipt.json"
      )" \
      --arg required_migration_id "$(
        if [ -n "$(jq -r '.prior_apply_receipt_path' \
          "$TMP_ROOT/verify/receipt.json")" ]; then
          jq -r '.migration_id' "$(
            jq -r '.prior_apply_receipt_path' \
              "$TMP_ROOT/verify/receipt.json"
          )"
        fi
      )" \
      --arg provenance_outcome "pending" \
      --arg image_deployment_intent_id "$(
        jq -er '.image_deployment_intent_id' \
          "$TMP_ROOT/verify/receipt.json"
      )" \
      --arg apply_attempt_id "$APPLY_ATTEMPT_ID" \
      --arg source_receipt_sha256 "$(sha256_file "$RECEIPT")" \
      --arg migration_contract_sha256 "$(
        jq -r '.migration_contract_sha256' "$TMP_ROOT/verify/receipt.json"
      )" \
      --arg reviewed_plan_sha256 "$(
        jq -r '.reviewed_plan_sha256' "$TMP_ROOT/verify/receipt.json"
      )" \
      --arg media_cutover_receipt_sha256 "$(
        jq -r '.media_cutover_receipt_sha256' \
          "$TMP_ROOT/verify/receipt.json"
      )" \
      --arg media_apply_authorization_sha256 "$MEDIA_AUTHORIZATION_SHA256" \
      --arg plan_sha256 "$(sha256_file "$PLAN")" \
      --arg openclaw_rollout_result_sha256 "$(
        sha256_file "$OPENCLAW_ROLLOUT_RESULT"
      )" \
      --arg post_apply_service_probe_sha256 "$(
        sha256_file "$POST_APPLY_SERVICE_PROBE_RESULT"
      )" \
      --arg versioning_receipt_sha256 "$(
        jq -r '.versioning_receipt_sha256' "$TMP_ROOT/verify/receipt.json"
      )" \
      --arg log_readiness_receipt_sha256 "$(
        jq -r '.log_readiness_receipt_sha256' \
          "$TMP_ROOT/verify/receipt.json"
      )" \
      --arg alarm_delivery_receipt_sha256 "$(
        jq -r '.alarm_delivery_receipt_sha256' \
          "$TMP_ROOT/verify/receipt.json"
      )" \
      --arg alarm_migration_receipt_sha256 "$(
        jq -r '.alarm_migration_receipt_sha256' \
          "$TMP_ROOT/verify/receipt.json"
      )" \
      --arg bedrock_retention_live_sha256 \
        "$APPLIED_BEDROCK_RETENTION_SHA256" \
      --arg post_state_contract_sha256 "$(
        sha256_file "$TMP_ROOT/post-state-contract.json"
      )" \
      --arg post_state_ownership_sha256 "$(
        jq -er '.state.address_set_sha256' "$TMP_ROOT/applied-state.json"
      )" \
      --arg post_live_fingerprint_sha256 "$(
        sha256_file "$TMP_ROOT/applied-live.json"
      )" \
      --arg post_runtime_inventory_sha256 "$(
        jq -er '.inventory_sha256' "$TMP_ROOT/applied-inventory.json"
      )" \
      --arg shared_deployment_lock_record_id \
        "lock#teamagent/terraform.tfstate" \
      --arg shared_deployment_lock_receipt_sha256 "$(
        sha256_file "$GATE_LOCK_RECEIPT"
      )" \
      --slurpfile pre_state_contract "$TMP_ROOT/pre-state-contract.json" \
      --slurpfile post_state_contract "$TMP_ROOT/post-state-contract.json" \
      --slurpfile pre_live_contract "$TMP_ROOT/pre-live-contract.json" \
      --slurpfile post_live_contract "$TMP_ROOT/post-live-contract.json" \
      --slurpfile live "$TMP_ROOT/applied-live.json" \
      --slurpfile bedrock_retention \
        "$TMP_ROOT/applied-bedrock-retention.json" \
      --slurpfile shared_lock "$GATE_LOCK_RECEIPT" \
      --slurpfile media_authorization \
        "$MEDIA_AUTHORIZATION_FOR_RECEIPT" \
      --slurpfile post_apply_service_probe \
        "$POST_APPLY_SERVICE_PROBE_RESULT" \
      --slurpfile openclaw_rollout "$OPENCLAW_ROLLOUT_RESULT" '{
        kind:$kind,
        schema_version:$schema_version,
        guard_version:$guard_version,
        account_id:$account_id,
        region:$region,
        git_commit:$git_commit,
        status:$status,
        migration_kind:$migration_kind,
        migration_id:$migration_id,
        required_migration_id:$required_migration_id,
        provenance_outcome:$provenance_outcome,
        image_deployment_intent_id:$image_deployment_intent_id,
        apply_attempt_id:$apply_attempt_id,
        source_receipt_sha256:$source_receipt_sha256,
        migration_contract_sha256:$migration_contract_sha256,
        reviewed_plan_sha256:$reviewed_plan_sha256,
        media_cutover_receipt_sha256:$media_cutover_receipt_sha256,
        media_apply_authorization_sha256:
          $media_apply_authorization_sha256,
        media_apply_authorization:$media_authorization[0],
        plan_sha256:$plan_sha256,
        openclaw_rollout_result_sha256:
          $openclaw_rollout_result_sha256,
        openclaw_rollout_result:$openclaw_rollout[0],
        post_apply_service_probe_sha256:
          $post_apply_service_probe_sha256,
        post_apply_service_probe:$post_apply_service_probe[0],
        versioning_receipt_sha256:$versioning_receipt_sha256,
        log_readiness_receipt_sha256:$log_readiness_receipt_sha256,
        alarm_delivery_receipt_sha256:$alarm_delivery_receipt_sha256,
        alarm_migration_receipt_sha256:$alarm_migration_receipt_sha256,
        bedrock_retention_live_sha256:$bedrock_retention_live_sha256,
        bedrock_retention_live:$bedrock_retention[0],
        post_state_contract_sha256:$post_state_contract_sha256,
        post_state_ownership_sha256:$post_state_ownership_sha256,
        pre_state_contract:$pre_state_contract[0],
        post_state_contract:$post_state_contract[0],
        post_live_fingerprint_sha256:$post_live_fingerprint_sha256,
        pre_live_contract:$pre_live_contract[0],
        post_live_contract:{
          images:{
            mcp:$live[0].taskdefs.mcp.image,
            connect_web:$live[0].taskdefs.connect_web.image,
            openclaw:$live[0].taskdefs.openclaw.image,
            canary:$live[0].taskdefs.canary.image,
            ingest:$live[0].taskdefs.ingest.image,
            morning_digest:$live[0].taskdefs.morning.image,
            x_buzz_worker:$live[0].taskdefs.x_buzz.image,
            tiktok_acquire:$live[0].taskdefs.tiktok.image
          },
          resources:$post_live_contract[0].resources,
          rule_states:{
            ingest:$live[0].rules.ingest.critical.state,
            morning:$live[0].rules.morning.critical.state,
            canary:$live[0].rules.canary.critical.state
          }
        },
        post_runtime_inventory_sha256:$post_runtime_inventory_sha256,
        shared_deployment_lock_record_id:
          $shared_deployment_lock_record_id,
        shared_deployment_lock_receipt_sha256:
          $shared_deployment_lock_receipt_sha256,
        shared_deployment_lock_receipt:$shared_lock[0]
      }' > "$APPLY_DRAFT"
    chmod 600 "$APPLY_DRAFT"
    jq -e \
      --argjson forced_dm_qa_required "$(
        if [ -n "$FORCED_ROLLBACK_DM_QA_DEADLINE_EPOCH" ]; then
          printf true
        else
          printf false
        fi
      )" '
      def task_revisions($resources):
        $resources |
        map({
          key:.consumer_id,
          value:(.task_definition_arn |
            capture(":(?<revision>[1-9][0-9]*)$").revision |
            tonumber)
        }) |
        from_entries;
      def resource_identity:
        {
          activation_identity:.activation.identity,
          activation_type:.activation.type,
          consumer_id,
          pipeline,
          subject,
          terraform_address
        };
      .kind == "terraform-runtime-apply-receipt-draft" and
      .schema_version == 7 and
      .status == "verified_pending_finalization" and
      .provenance_outcome == "pending" and
      .openclaw_rollout_result.passed == true and
      .post_apply_service_probe.kind ==
        "teamagent-post-apply-service-probe-receipt" and
      .post_apply_service_probe.apply_attempt_id == .apply_attempt_id and
      (.post_apply_service_probe_sha256 | test("^[0-9a-f]{64}$")) and
      .openclaw_rollout_result.applyAttemptId == .apply_attempt_id and
      (.openclaw_rollout_result_sha256 | test("^[0-9a-f]{64}$")) and
      (
        if $forced_dm_qa_required then
          .openclaw_rollout_result.dmQa.result == "PASSED" and
          .openclaw_rollout_result.dmQa.applyAttemptId ==
            .apply_attempt_id and
          .openclaw_rollout_result.dmQa.locator.object_lock_mode ==
            "COMPLIANCE" and
          .openclaw_rollout_result.dmQa.locator.key ==
            (
              "forced-rollback-drills/" + .apply_attempt_id +
              "/dm-qa/result.json"
            )
        else
          true
        end
      ) and
      (
        if .media_cutover_receipt_sha256 == "" then
          .media_apply_authorization_sha256 == "" and
          .media_apply_authorization == null
        else
          (.media_cutover_receipt_sha256 |
            test("^[0-9a-f]{64}$")) and
          (.media_apply_authorization_sha256 |
            test("^[0-9a-f]{64}$")) and
          .media_apply_authorization.kind ==
            "teamagent-media-apply-authorization" and
          .media_apply_authorization.state == "AUTHORIZED" and
          .media_apply_authorization.apply_attempt_id ==
            .apply_attempt_id and
          .media_apply_authorization.plan_sha256 == .plan_sha256
        end
      ) and
      .shared_deployment_lock_record_id ==
        "lock#teamagent/terraform.tfstate" and
      ([.pre_live_contract, .post_live_contract] |
        all(.[];
          (keys | sort) == ["images","resources","rule_states"] and
          (.resources | type) == "array" and
          .resources == (.resources | sort_by(.consumer_id)) and
          ([.resources[].consumer_id] | unique | length) ==
            (.resources | length)
        )) and
      ([.pre_live_contract.resources[],
        .post_live_contract.resources[]] |
        all(.[];
          (.task_definition_arn |
            test(
              "^arn:aws:ecs:ap-northeast-1:718959508629:"
              + "task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*$"
            ))
        )) and
      ([.pre_live_contract.resources[] | resource_identity]) ==
        ([.post_live_contract.resources[] | resource_identity]) and
      .pre_state_contract.task_revisions ==
        task_revisions(.pre_live_contract.resources) and
      .post_state_contract.task_revisions ==
        task_revisions(.post_live_contract.resources) and
      ([.pre_state_contract, .post_state_contract] |
        all(.[];
          (keys | sort) ==
            ["backend","imports","state","task_revisions"]
        )) and
      .pre_state_contract.backend == .post_state_contract.backend and
      .pre_state_contract.imports == .post_state_contract.imports and
      .pre_state_contract.state.lineage ==
        .post_state_contract.state.lineage and
      .pre_state_contract.state.address_count ==
        .post_state_contract.state.address_count and
      .pre_state_contract.state.address_set_sha256 ==
        .post_state_contract.state.address_set_sha256 and
      .pre_state_contract.state.serial <
        .post_state_contract.state.serial
    ' "$APPLY_DRAFT" >/dev/null ||
      die "apply receipt draft schema/provenance bindingの生成に失敗しました"
    if ! ensure_forced_rollback_dm_qa_recovery_reserve \
      "pre-finalization verification"; then
      exit 124
    fi
    stop_gate_heartbeat
    python3 "$DEPLOYMENT_APPLY_FINALIZER" commit \
      --aws-bin "$AWS_BIN" \
      --intent-id "$RECOVERY_INTENT_ID" \
      --plan-sha256 "$STAGED_PLAN_SHA256" \
      --apply-attempt-id "$APPLY_ATTEMPT_ID" \
      --draft "$APPLY_DRAFT" \
      --eventbridge-verification "$EVENTBRIDGE_SAGA_VERIFY_RECEIPT" \
      --ecs-verification "$ECS_SERVICE_SAGA_VERIFY_RECEIPT" \
      --out "$APPLY_RECEIPT" > "$FINALIZATION_RESULT" ||
      die "deployment ledgersとapply receiptを原子的に確定できません"
    jq -e \
      --arg intent "$RECOVERY_INTENT_ID" \
      --arg plan "$STAGED_PLAN_SHA256" \
      --arg attempt "$APPLY_ATTEMPT_ID" '
      .ok == true and
      (.state == "COMMITTED" or
       .state == "RECOVERED" or
       .state == "RECOVERED_AFTER_AMBIGUOUS_COMMIT") and
      .intent_id == $intent and
      .plan_sha256 == $plan and
      .apply_attempt_id == $attempt and
      (.apply_receipt_sha256 | test("^[0-9a-f]{64}$")) and
      (.deployment_finalization_receipt_sha256 |
        test("^[0-9a-f]{64}$"))
    ' "$FINALIZATION_RESULT" >/dev/null ||
      die "deployment finalization resultがplan/intent/attemptと不一致です"
    [ "$(sha256_file "$APPLY_RECEIPT")" = "$(
      jq -er '.apply_receipt_sha256' "$FINALIZATION_RESULT"
    )" ] ||
      die "finalized apply receipt digestが不一致です"
    jq -e \
      --arg intent "$RECOVERY_INTENT_ID" \
      --arg plan "$STAGED_PLAN_SHA256" \
      --arg attempt "$APPLY_ATTEMPT_ID" \
      --arg post_state_sha "$(
        sha256_file "$TMP_ROOT/post-state-contract.json"
      )" \
      --slurpfile pre_live "$TMP_ROOT/pre-live-contract.json" \
      --slurpfile post_live "$TMP_ROOT/post-live-contract.json" \
      --slurpfile pre_state "$TMP_ROOT/pre-state-contract.json" \
      --slurpfile post_state "$TMP_ROOT/post-state-contract.json" '
      .kind == "terraform-runtime-apply-receipt" and
      .schema_version == 7 and
      .status == "applied" and
      .provenance_outcome == "applied" and
      .image_deployment_intent_id == $intent and
      .plan_sha256 == $plan and
      .apply_attempt_id == $attempt and
      .provenance_outcome_receipt == {
        intent_id:$intent,
        plan_sha256:$plan,
        state:"APPLIED"
      } and
      (.provenance_outcome_receipt_sha256 | test("^[0-9a-f]{64}$")) and
      (.ecs_service_saga_receipt_sha256 | test("^[0-9a-f]{64}$")) and
      .ecs_service_saga_receipt.stage == "APPLIED" and
      .ecs_service_saga_receipt.apply_attempt_id == $attempt and
      (.ecs_service_saga_verification_receipt_sha256 |
        test("^[0-9a-f]{64}$")) and
      .ecs_service_saga_verification_receipt.stage ==
        "VERIFIED_APPLIED" and
      (.eventbridge_apply_saga_verification_receipt_sha256 |
        test("^[0-9a-f]{64}$")) and
      .eventbridge_apply_saga_verification_receipt.stage ==
        "verified_applied" and
      .deployment_finalization_receipt.state == "APPLIED" and
      .deployment_finalization_receipt.intent_id == $intent and
      .deployment_finalization_receipt.plan_sha256 == $plan and
      .deployment_finalization_receipt.apply_attempt_id == $attempt and
      (.deployment_finalization_receipt_sha256 |
        test("^[0-9a-f]{64}$")) and
      .pre_live_contract == $pre_live[0] and
      .post_live_contract == $post_live[0] and
      .pre_state_contract == $pre_state[0] and
      .post_state_contract == $post_state[0] and
      .post_state_contract_sha256 == $post_state_sha
    ' "$APPLY_RECEIPT" >/dev/null ||
      die "finalized apply receipt schema/provenance bindingが不正です"
    APPLY_RECEIPT_PUBLISHED="true"
    COMPOSITE_FINALIZED="true"
    EVENTBRIDGE_SAGA_FINISHED="true"
    ECS_SERVICE_SAGA_FINISHED="true"
    GATE_OUTCOME_RECORDED="true"
    GATE_LOCK_ACQUIRED="false"
    release_deployment_lock
    echo "✅ guarded apply completed: $APPLY_RECEIPT"
    ;;

  adopt-plan)
    ADOPT_VAR_FILE=""
    ADOPT_OUT=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --var-file) ADOPT_VAR_FILE="${2:?--var-file に値が必要}"; shift 2 ;;
        --out) ADOPT_OUT="${2:?--out に値が必要}"; shift 2 ;;
        *) die "未知の引数: $1" ;;
      esac
    done
    [ -n "$ADOPT_VAR_FILE" ] || die "adopt-plan には --var-file が必須です"
    [ -n "$ADOPT_OUT" ] || die "adopt-plan には --out が必須です"
    adopt_plan "$ADOPT_VAR_FILE" "$ADOPT_OUT"
    ;;

  adopt-apply)
    ADOPT_OUT=""
    ADOPT_APPROVE=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --out) ADOPT_OUT="${2:?--out に値が必要}"; shift 2 ;;
        --approve) ADOPT_APPROVE="${2:?--approve に値が必要}"; shift 2 ;;
        *) die "未知の引数: $1" ;;
      esac
    done
    [ -n "$ADOPT_OUT" ] || die "adopt-apply には --out が必須です"
    adopt_apply "$ADOPT_OUT" "$ADOPT_APPROVE"
    ;;

  state-rebind-precheck)
    REBIND_OUT=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --out) REBIND_OUT="${2:?--out に値が必要}"; shift 2 ;;
        *) die "未知の引数: $1" ;;
      esac
    done
    [ -n "$REBIND_OUT" ] || die "state-rebind-precheck には --out が必須です"
    rebind_precheck "$REBIND_OUT"
    ;;

  state-rebind-apply)
    REBIND_OUT=""
    REBIND_VAR_FILE=""
    REBIND_APPROVE=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --out) REBIND_OUT="${2:?--out に値が必要}"; shift 2 ;;
        --var-file) REBIND_VAR_FILE="${2:?--var-file に値が必要}"; shift 2 ;;
        --approve) REBIND_APPROVE="${2:?--approve に値が必要}"; shift 2 ;;
        *) die "未知の引数: $1" ;;
      esac
    done
    [ -n "$REBIND_OUT" ] || die "state-rebind-apply には --out が必須です"
    [ -n "$REBIND_VAR_FILE" ] || die "state-rebind-apply には --var-file が必須です"
    rebind_apply "$REBIND_OUT" "$REBIND_VAR_FILE" "$REBIND_APPROVE"
    ;;

  *)
    die "不明な command: $COMMAND"
    ;;
esac
