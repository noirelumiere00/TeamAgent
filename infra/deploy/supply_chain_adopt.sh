#!/usr/bin/env bash
# PR2-A0 Supply-Chain Adopt の専用エントリポイント（既存 guard から完全に独立）。
#
# 【なぜ terraform_runtime_guard.sh を拡張せず別経路にしたか】
# adopt は「AWS 実体を一切変更せず、Terraform state だけを実態へ追いつかせる」操作で、
# 既存の sync / runtime migration / activation とは不変条件がまったく異なる（adopt の方が狭い）。
# guard を拡張すると本番 runtime 適用の主経路の allowlist を触ることになり、リスクを持ち込む。
# よって adopt は独立した fail-closed 経路として実装し、**guard には一切変更を加えない**。
#
# 【この経路が絶対にやらないこと】
#   - prevent_destroy / Object Lock / bucket policy の Delete Deny の解除・緩和
#   - S3 オブジェクトの作成・上書き・削除（adopt は import と forget のみ）
#   - raw terraform import / state rm / -target / 無制限 apply
#   - mapping（supply_chain_adoptions.json）に列挙されていない対象への操作
#
# 使い方:
#   supply_chain_adopt.sh plan   --var-file FILE --out DIR
#   supply_chain_adopt.sh verify --out DIR
#   supply_chain_adopt.sh apply  --out DIR --approve "<承認トークン>"
#
# apply は verify 済みの成果物一式が揃っていて、かつ明示の承認トークンが与えられた時だけ実行する。

set -euo pipefail
umask 077

ADOPT_VERSION="1"
EXPECTED_ACCOUNT_ID="718959508629"
REGION="ap-northeast-1"
APPROVE_TOKEN="I-HAVE-REVIEWED-THE-ADOPT-PLAN"

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
TF_DIR="$REPO_ROOT/infra/terraform"
MAPPING="$REPO_ROOT/infra/deploy/supply_chain_adoptions.json"
VALIDATOR="$REPO_ROOT/infra/deploy/supply_chain_adopt_validate.py"
INTEGRITY="$REPO_ROOT/infra/deploy/supply_chain_adopt_integrity.py"

die() {
  echo "★ $*" >&2
  exit 1
}

require_files() {
  for path in "$MAPPING" "$VALIDATOR" "$INTEGRITY"; do
    [ -f "$path" ] || die "必須ファイルがありません: $path"
  done
}

require_account() {
  local actual
  actual="$(aws sts get-caller-identity --query Account --output text)" ||
    die "AWS 認証を確認できません"
  [ "$actual" = "$EXPECTED_ACCOUNT_ID" ] ||
    die "想定外の AWS アカウントです: $actual（期待 $EXPECTED_ACCOUNT_ID）"
}

# adopt 対象の所有状態を確認する（ownership discovery）。
# 旧アドレスは state に存在し、新アドレスは存在しないこと。どちらか違えば中断する。
ownership_discovery() {
  local state_list="$1"
  local old new
  while IFS= read -r old; do
    grep -Fxq "$old" "$state_list" ||
      die "ownership discovery 失敗: 旧アドレスが state にありません: $old"
  done < <(python3 -c "
import json,sys
for a in json.load(open('$MAPPING'))['adoptions']:
    print(a['old_address'])
")
  while IFS= read -r new; do
    if grep -Fxq "$new" "$state_list"; then
      die "ownership discovery 失敗: 新アドレスが既に state にあります（adopt 済み?）: $new"
    fi
  done < <(python3 -c "
import json,sys
for a in json.load(open('$MAPPING'))['adoptions']:
    print(a['new_address'])
")
  echo "  ownership discovery OK（旧アドレスは state 内・新アドレスは未登録）"
}

cmd_plan() {
  local var_file="" out_dir=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --var-file) var_file="${2:?--var-file に値が必要}"; shift 2 ;;
      --out) out_dir="${2:?--out に値が必要}"; shift 2 ;;
      *) die "未知の引数: $1" ;;
    esac
  done
  [ -n "$var_file" ] || die "plan には --var-file が必須です"
  [ -n "$out_dir" ] || die "plan には --out が必須です"
  require_files
  require_account
  mkdir -p "$out_dir"
  chmod 700 "$out_dir"

  echo "▶ state backup"
  terraform -chdir="$TF_DIR" state pull > "$out_dir/state-backup.json"
  chmod 600 "$out_dir/state-backup.json"
  [ -s "$out_dir/state-backup.json" ] || die "state backup が空です"
  echo "  保存: $out_dir/state-backup.json ($(wc -c < "$out_dir/state-backup.json") bytes)"

  echo "▶ ownership discovery"
  terraform -chdir="$TF_DIR" state list > "$out_dir/state-list.txt"
  ownership_discovery "$out_dir/state-list.txt"

  echo "▶ S3 integrity snapshot（adopt 前）"
  python3 "$INTEGRITY" snapshot --mapping "$MAPPING" --out "$out_dir/integrity-before.json" \
    --region "$REGION" || die "adopt 前の integrity snapshot に失敗しました"

  echo "▶ terraform plan"
  terraform -chdir="$TF_DIR" plan -input=false -lock-timeout=5m \
    "-var-file=$var_file" -out="$out_dir/adopt.tfplan" ||
    die "terraform plan に失敗しました"
  terraform -chdir="$TF_DIR" show -json "$out_dir/adopt.tfplan" > "$out_dir/adopt-plan.json"
  chmod 600 "$out_dir/adopt.tfplan" "$out_dir/adopt-plan.json"

  echo "▶ adopt plan validation（fail-closed）"
  python3 "$VALIDATOR" --plan "$out_dir/adopt-plan.json" --mapping "$MAPPING" ||
    die "adopt plan が不変条件を満たしません（plan は破棄してください）"

  echo "✔ plan 完了。次: $0 verify --out $out_dir"
}

cmd_verify() {
  local out_dir=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --out) out_dir="${2:?--out に値が必要}"; shift 2 ;;
      *) die "未知の引数: $1" ;;
    esac
  done
  [ -n "$out_dir" ] || die "verify には --out が必須です"
  require_files
  for path in adopt.tfplan adopt-plan.json integrity-before.json state-backup.json; do
    [ -f "$out_dir/$path" ] || die "plan 成果物がありません: $out_dir/$path"
  done

  echo "▶ adopt plan を再検証"
  python3 "$VALIDATOR" --plan "$out_dir/adopt-plan.json" --mapping "$MAPPING" ||
    die "再検証に失敗しました"

  echo "▶ S3 実体が plan 時点から不変であることを確認"
  python3 "$INTEGRITY" snapshot --mapping "$MAPPING" --out "$out_dir/integrity-verify.json" \
    --region "$REGION" || die "verify 時の integrity snapshot に失敗しました"
  python3 "$INTEGRITY" compare --before "$out_dir/integrity-before.json" \
    --after "$out_dir/integrity-verify.json" ||
    die "plan 作成後に S3 実体が変化しました（adopt を中止してください）"

  echo "✔ verify 完了。apply するには承認トークンが必要です:"
  echo "    $0 apply --out $out_dir --approve \"$APPROVE_TOKEN\""
}

cmd_apply() {
  local out_dir="" approve=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --out) out_dir="${2:?--out に値が必要}"; shift 2 ;;
      --approve) approve="${2:?--approve に値が必要}"; shift 2 ;;
      *) die "未知の引数: $1" ;;
    esac
  done
  [ -n "$out_dir" ] || die "apply には --out が必須です"
  [ "$approve" = "$APPROVE_TOKEN" ] ||
    die "apply には明示の承認が必要です（--approve \"$APPROVE_TOKEN\"）"
  require_files
  require_account

  echo "▶ apply 直前の再検証"
  python3 "$VALIDATOR" --plan "$out_dir/adopt-plan.json" --mapping "$MAPPING" ||
    die "apply 直前の検証に失敗しました"
  python3 "$INTEGRITY" snapshot --mapping "$MAPPING" --out "$out_dir/integrity-preapply.json" \
    --region "$REGION" || die "apply 直前の integrity snapshot に失敗しました"
  python3 "$INTEGRITY" compare --before "$out_dir/integrity-before.json" \
    --after "$out_dir/integrity-preapply.json" || die "apply 直前に S3 実体が変化しています"

  echo "▶ terraform apply（保存済み adopt plan のみ）"
  terraform -chdir="$TF_DIR" apply -input=false -lock-timeout=5m "$out_dir/adopt.tfplan" ||
    die "apply に失敗しました。state backup: $out_dir/state-backup.json"

  echo "▶ S3 実体が adopt で変化していないことを確認"
  python3 "$INTEGRITY" snapshot --mapping "$MAPPING" --out "$out_dir/integrity-after.json" \
    --region "$REGION" || die "adopt 後の integrity snapshot に失敗しました"
  python3 "$INTEGRITY" compare --before "$out_dir/integrity-before.json" \
    --after "$out_dir/integrity-after.json" ||
    die "adopt により AWS 実体が変化しました（activation failure）"

  echo "✔ adopt 完了。post-activation で通常の guarded plan が clean になることを確認してください。"
}

main() {
  [ $# -gt 0 ] || die "使い方: $0 {plan|verify|apply} ..."
  local command="$1"; shift
  case "$command" in
    plan) cmd_plan "$@" ;;
    verify) cmd_verify "$@" ;;
    apply) cmd_apply "$@" ;;
    version) echo "supply_chain_adopt v$ADOPT_VERSION" ;;
    *) die "未知のサブコマンド: $command" ;;
  esac
}

main "$@"
