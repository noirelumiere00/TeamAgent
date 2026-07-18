#!/usr/bin/env bash
# Terraform が CLI 直デプロイ後の live ECS/EventBridge を巻き戻さないための
# TeamAgent dev専用 guarded Terraform workflow。
#
# - snapshot: live の non-secret desired-state 値を HCL snippet として表示（read-only）
# - preflight: candidate imageを実Fargateで検証し、短命なreceiptを発行する
# - plan:     strict live同期または一度限りmigrationの検証済みplanだけを保存する
# - verify:   plan/receipt/live/state が plan 作成時から不変か再確認（read-only）
# - apply:    trusted automation roleと共有lockの下でverify済みplanだけを適用
#
# これは協調運用のguardであり、AWS administrator/rootに対する認可境界ではない。
# 管理者が直接API/CLIを使って迂回できることは受容済みリスクとしてREADMEに明記する。
set -euo pipefail
umask 077

GUARD_VERSION="13"
EXPECTED_ACCOUNT_ID="718959508629"
REGION="ap-northeast-1"
PROJECT="teamagent"
ENVIRONMENT="dev"
EXPECTED_BACKEND_BUCKET="teamagent-tfstate-718959508629"
EXPECTED_BACKEND_KEY="teamagent/terraform.tfstate"
EXPECTED_BACKEND_DYNAMODB_TABLE="teamagent-tflock"
EXPECTED_WORKSPACE="default"
EXPECTED_ALARM_EMAIL_SHA256="88c6452f9db04017250aa5728b4815bccb55b5ecc0b35b50a5234170dc08d1e6"
TRUSTED_AUTOMATION_ROLE_NAME="teamagent-dev-terraform-runtime-automation"
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
TMP_ROOT=""

usage() {
  cat <<'EOF'
usage:
  terraform_runtime_guard.sh snapshot
  terraform_runtime_guard.sh attest-log-versioning --out RECEIPT
  terraform_runtime_guard.sh preflight --migration ID --out RECEIPT
  terraform_runtime_guard.sh plan --var-file FILE --out PLAN \
    (--runtime-sync | --runtime-migration ID --preflight-receipt FILE \
    --alarm-delivery-receipt FILE --versioning-receipt FILE \
    --log-readiness-receipt FILE) [--receipt FILE]
  terraform_runtime_guard.sh verify --plan PLAN [--receipt FILE]
  terraform_runtime_guard.sh apply --plan PLAN [--receipt FILE] --out APPLY_RECEIPT

plan:
  --runtime-sync           主要5 runtimeとTikTok/x-buzz worker/dispatcherを完全照合
  --runtime-migration ID   git管理のexact one-time allowlistだけを使用
  --preflight-receipt FILE migration候補を実Fargateで検証した短命receipt
  --alarm-delivery-receipt FILE 実SNS配送を確認した短命・非機微receipt
  --versioning-receipt FILE S3 versioning第1段階を束縛する短命receipt
  --log-readiness-receipt FILE versioning 15分待機・配信・retention export証跡
  --receipt FILE           receipt 出力先（default: PLAN.runtime-guard.json）

重要:
  - TeamAgent dev / account 718959508629 / ap-northeast-1 / 固定S3 backend専用。
  - snapshot/verify/attest-log-versioningはAWS read-only。preflightは一時task/EFSを使う。
  - applyはexact trusted automation role、共有DynamoDB lock、直前verify、保存planだけを必須にする。
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
    esac
  done < <(compgen -e | LC_ALL=C sort)
  if [ "${#rejected[@]}" -ne 0 ]; then
    # Never print values: TF_VAR_* can contain secrets.
    die "Terraform CLIへ影響する環境変数を消去して拒否しました: ${rejected[*]}"
  fi
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
  stat -f '%u' "$1" 2>/dev/null || stat -c '%u' "$1"
}

stat_mode() {
  stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1"
}

stat_identity() {
  stat -f '%d:%i' "$1" 2>/dev/null || stat -c '%d:%i' "$1"
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
  assert_git_tracked_clean "$SCRIPT_PATH"
  assert_git_tracked_clean "$GUARD_JQ"
  assert_git_tracked_clean "$MIGRATION_FILE"

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
  local output="$1" path relative
  : > "$output"
  for path in "$GUARD_JQ" "$MIGRATION_FILE"; do
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

aws_cli() {
  AWS_PAGER="" aws --region "$REGION" "$@"
}

aws_cost_cli() {
  # AWS Budgets and Cost Explorer use the us-east-1 global billing endpoint.
  AWS_PAGER="" aws --region us-east-1 "$@"
}

assert_trusted_automation_identity() {
  local identity="$TMP_ROOT/trusted-automation-identity.json"
  local expected_iam_arn expected_sts_prefix
  expected_iam_arn="arn:aws:iam::${EXPECTED_ACCOUNT_ID}:role/${TRUSTED_AUTOMATION_ROLE_NAME}"
  expected_sts_prefix="arn:aws:sts::${EXPECTED_ACCOUNT_ID}:assumed-role/${TRUSTED_AUTOMATION_ROLE_NAME}/"
  aws_cli sts get-caller-identity --output json > "$identity"
  jq -e \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg iam "$expected_iam_arn" \
    --arg sts_prefix "$expected_sts_prefix" '
    .Account == $account and
    ((.Arn == $iam) or (.Arn | startswith($sts_prefix)))
  ' "$identity" >/dev/null ||
    die "write-capable guard操作はexact trusted automation roleだけが実行できます"
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
  local raw="$TMP_ROOT/state-pull-$RANDOM.json"
  local listed="$TMP_ROOT/state-list-$RANDOM.txt"
  local canonical="$TMP_ROOT/state-list-canonical-$RANDOM.txt"
  local derived="$TMP_ROOT/state-list-derived-$RANDOM.txt"
  local specs="$TMP_ROOT/state-import-specs-$RANDOM.json"
  local backend="$TMP_ROOT/backend-identity-$RANDOM.json"
  local backend_after="$TMP_ROOT/backend-identity-after-$RANDOM.json"
  local workspace address_count address_sha

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
      address:"aws_cloudwatch_log_group.codebuild_image_builder",
      type:"aws_cloudwatch_log_group",
      name:"codebuild_image_builder",
      id:"/aws/codebuild/teamagent-dev-image-builder"
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
  ' > "$output" || die "state lineage/serial/address/import ownership契約を確定できません"
}

verify_alarm_delivery_test_receipt() {
  local receipt="$1" snapshot="$2"
  jq -e \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" \
    --arg topic \
      "arn:aws:sns:${REGION}:${EXPECTED_ACCOUNT_ID}:${PROJECT}-${ENVIRONMENT}-openclaw-alarms" \
    --arg subscription_sha \
      "$(jq -er '.alarm_delivery.confirmed_subscription_metadata_sha256' "$snapshot")" \
    --argjson now "$(date +%s)" '
    (keys | sort) == ([
      "account_id",
      "chatbot_configuration_arn",
      "chatbot_state",
      "delivery_channel",
      "delivery_evidence_sha256",
      "email_endpoint_sha256",
      "expires_at_epoch",
      "kind",
      "observer_identity_sha256",
      "region",
      "result",
      "schema_version",
      "subscription_metadata_sha256",
      "test_message_id_sha256",
      "tested_at_epoch",
      "topic_arn"
    ] | sort) and
    .kind == "teamagent-alarm-delivery-test-receipt" and
    .schema_version == 1 and
    .account_id == $account and .region == $region and
    .topic_arn == $topic and
    .subscription_metadata_sha256 == $subscription_sha and
    (.delivery_channel == "email" or .delivery_channel == "chat") and
    .result == "delivered" and
    (.observer_identity_sha256 | test("^[0-9a-f]{64}$")) and
    (.test_message_id_sha256 | test("^[0-9a-f]{64}$")) and
    (.delivery_evidence_sha256 | test("^[0-9a-f]{64}$")) and
    (.tested_at_epoch | type) == "number" and
    (.tested_at_epoch | floor) == .tested_at_epoch and
    .tested_at_epoch <= $now and
    .expires_at_epoch > $now and
    (.expires_at_epoch - .tested_at_epoch) > 0 and
    (.expires_at_epoch - .tested_at_epoch) <= 86400 and
    (
      if .delivery_channel == "email" then
        .email_endpoint_sha256 ==
          "88c6452f9db04017250aa5728b4815bccb55b5ecc0b35b50a5234170dc08d1e6" and
        .chatbot_configuration_arn == "" and
        .chatbot_state == ""
      else
        .email_endpoint_sha256 == "" and
        (.chatbot_configuration_arn | test(
          "^arn:aws:chatbot::718959508629:chat-configuration/(slack-channel|microsoft-teams-channel)/[A-Za-z0-9._-]+$"
        )) and
        .chatbot_state == "ENABLED"
      end
    )
  ' "$receipt" >/dev/null ||
    die "実配送を人が確認したfresh SNS delivery receiptが不正または期限切れです"
  jq -e \
    --arg expected_email "$EXPECTED_ALARM_EMAIL_SHA256" \
    --slurpfile receipt "$receipt" '
    $receipt[0] as $r |
    if $r.delivery_channel == "email" then
      .alarm_delivery.confirmed_email_endpoint_sha256 ==
        [$expected_email] and
      .alarm_delivery.attached_chatbot_configuration_arns == [] and
      .alarm_delivery_observation.attached_chatbot_configurations == []
    else
      .alarm_delivery.confirmed_email_endpoint_sha256 == [] and
      .alarm_delivery.attached_chatbot_configuration_arns ==
        [$r.chatbot_configuration_arn] and
      .alarm_delivery_observation.attached_chatbot_configurations == [{
        arn:$r.chatbot_configuration_arn,
        state:"ENABLED"
      }]
    end
  ' "$snapshot" >/dev/null ||
    die "SNS receipt channelとexclusive live delivery destination/stateが一致しません"
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

verify_versioning_attestation_receipt() {
  local receipt="$1" snapshot="$2" state_contract="$3"
  local config_manifest="$TMP_ROOT/versioning-config-manifest-$RANDOM.txt"
  local current_producer="$TMP_ROOT/versioning-current-producer-$RANDOM.json"
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
    --argjson now "$(date +%s)" '
    (keys | sort) == ([
      "account_id",
      "buckets",
      "config_manifest_sha256",
      "created_at_epoch",
      "deployment_lock_id",
      "expires_at_epoch",
      "git_commit",
      "guard_jq_sha256",
      "guard_script_sha256",
      "guard_version",
      "kind",
      "live_after_sha256",
      "migration_manifest_sha256",
      "producer_contract_sha256",
      "producer_evidence_sha256",
      "region",
      "schema_version",
      "stage_id",
      "state_contract",
      "versioning_observed_at_epoch"
    ] | sort) and
    .kind == "teamagent-log-versioning-attestation-receipt" and
    .schema_version == 1 and
    .stage_id == "2026-07-log-versioning-attest-v2" and
    .guard_version == $guard_version and
    .account_id == $account and .region == $region and
    .git_commit == $git_commit and
    .guard_script_sha256 == $guard_script_sha256 and
    .guard_jq_sha256 == $guard_jq_sha256 and
    .migration_manifest_sha256 == $migration_manifest_sha256 and
    .config_manifest_sha256 == $config_manifest_sha256 and
    .deployment_lock_id == $deployment_lock_id and
    (.live_after_sha256 | test("^[0-9a-f]{64}$")) and
    (.producer_contract_sha256 | test("^[0-9a-f]{64}$")) and
    (.producer_evidence_sha256 | test("^[0-9a-f]{64}$")) and
    (.created_at_epoch | type) == "number" and
    (.created_at_epoch | floor) == .created_at_epoch and
    (.versioning_observed_at_epoch | type) == "number" and
    (.versioning_observed_at_epoch | floor) ==
      .versioning_observed_at_epoch and
    .created_at_epoch <= .versioning_observed_at_epoch and
    .versioning_observed_at_epoch <= $now and
    .expires_at_epoch > $now and
    (.expires_at_epoch - .created_at_epoch) > 0 and
    (.expires_at_epoch - .created_at_epoch) <= 86400 and
    (.buckets | keys | sort) == ["bedrock","cloudtrail"] and
    (.buckets.cloudtrail | keys | sort) == ["after","before","name"] and
    (.buckets.bedrock | keys | sort) == ["after","before","name"] and
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
    die "S3 versioning attestation receiptがsource/state/bucket契約と不一致です"

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
    die "versioning attestation検証時のbucket/lifecycle状態が不正です"

  jq -e --slurpfile receipt "$receipt" '
    .backend == $receipt[0].state_contract.backend and
    .state.lineage == $receipt[0].state_contract.state.lineage and
    .state.serial >= $receipt[0].state_contract.state.serial
  ' "$state_contract" >/dev/null ||
    die "versioning attestation後にbackend/workspace/lineageが変化またはstate serialが後退しました"

  capture_log_delivery_contract "$current_producer"
  [ "$(log_delivery_contract_sha256 "$current_producer")" = \
    "$(jq -er '.producer_contract_sha256' "$receipt")" ] ||
    die "versioning attestation後にCloudTrail/Bedrock producer設定が変化しました"
}

verify_log_readiness_receipt() {
  local receipt="$1" versioning_receipt="$2" snapshot="$3"
  local versioning_sha observed_at evidence_path evidence_path_requested evidence_identity
  local retention_path retention_path_requested retention_identity
  versioning_sha="$(sha256_file "$versioning_receipt")"
  observed_at="$(jq -er '.versioning_observed_at_epoch' "$versioning_receipt")"
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
      "evidence_artifact_path",
      "evidence_artifact_sha256",
      "expires_at_epoch",
      "kind",
      "region",
      "schema_version",
      "versioning_receipt_sha256"
    ] | sort) and
    .kind == "teamagent-log-rollout-readiness-receipt" and
    .schema_version == 2 and
    .account_id == $account and .region == $region and
    .versioning_receipt_sha256 == $versioning_sha and
    (.evidence_artifact_path |
      type == "string" and startswith("/")) and
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
    --argjson versioning_observed_at "$observed_at" \
    --argjson receipt_created_at "$(jq -er '.created_at_epoch' "$receipt")" \
    --argjson now "$(date +%s)" \
    --slurpfile retention "$retention_path" '
    def delivery($prefix):
      (keys | sort) ==
        ["etag","key","last_modified_epoch","size_bytes","version_id"] and
      (.key | type == "string" and startswith($prefix)) and
      (.version_id |
        type == "string" and test("^[A-Za-z0-9._-]{1,1024}$")) and
      (.etag | type == "string" and test("^[0-9a-f]{32}(-[0-9]+)?$")) and
      (.size_bytes | type) == "number" and
      (.size_bytes | floor) == .size_bytes and .size_bytes > 0 and
      (.last_modified_epoch | type) == "number" and
      (.last_modified_epoch | floor) == .last_modified_epoch and
      .last_modified_epoch >= $versioning_observed_at and
      .last_modified_epoch <= $now;
    (keys | sort) == ([
      "account_id",
      "bedrock",
      "cloudtrail",
      "kind",
      "observed_at_epoch",
      "region",
      "retention_export_manifest_path",
      "retention_export_manifest_sha256",
      "schema_version",
      "versioning_observed_at_epoch"
    ] | sort) and
    .kind == "teamagent-log-readiness-evidence" and
    .schema_version == 1 and
    .account_id == $account and .region == $region and
    .versioning_observed_at_epoch == $versioning_observed_at and
    (.observed_at_epoch | type) == "number" and
    (.observed_at_epoch | floor) == .observed_at_epoch and
    (.observed_at_epoch - .versioning_observed_at_epoch) >= 900 and
    .observed_at_epoch <= $now and
    .observed_at_epoch <= $receipt_created_at and
    ($receipt_created_at - .observed_at_epoch) <= 900 and
    .retention_export_manifest_path == $retention_path and
    .retention_export_manifest_sha256 == $retention_sha and
    (.cloudtrail | keys | sort) ==
      ["bucket","latest_digest","latest_log"] and
    .cloudtrail.bucket == $cloudtrail and
    (.cloudtrail.latest_log |
      delivery("AWSLogs/" + $account + "/CloudTrail/")) and
    (.cloudtrail.latest_digest |
      delivery("AWSLogs/" + $account + "/CloudTrail-Digest/")) and
    (.bedrock | keys | sort) == ["bucket","latest_delivery"] and
    .bedrock.bucket == $bedrock and
    (.bedrock.latest_delivery |
      delivery(
        "bedrock/AWSLogs/" + $account +
        "/BedrockModelInvocationLogs/"
      )) and
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
    $retention[0].schema_version == 1 and
    $retention[0].account_id == $account and
    $retention[0].region == $region and
    ($retention[0].created_at_epoch | type) == "number" and
    ($retention[0].created_at_epoch | floor) ==
      $retention[0].created_at_epoch and
    $retention[0].created_at_epoch >= $versioning_observed_at and
    $retention[0].created_at_epoch <= .observed_at_epoch and
    ($retention[0].log_groups | type) == "array" and
    ($retention[0].log_groups | map(.log_group) | sort) == ([
      "/aws/codebuild/teamagent-dev-aiia-image-builder",
      "/aws/codebuild/teamagent-dev-image-builder",
      "/aws/lambda/teamagent-dev-reminders-notify",
      "/aws/lambda/teamagent-dev-tiktok-acquire-dispatch",
      "/aws/lambda/teamagent-dev-x-buzz-dispatch"
    ] | sort) and
    ($retention[0].log_groups | length) == 5 and
    all($retention[0].log_groups[];
      (keys | sort) ==
        ["content_sha256","event_count","exported_through_epoch","log_group"] and
      (.content_sha256 | test("^[0-9a-f]{64}$")) and
      (.event_count | type) == "number" and
      (.event_count | floor) == .event_count and .event_count > 0 and
      (.exported_through_epoch | type) == "number" and
      (.exported_through_epoch | floor) == .exported_through_epoch and
      .exported_through_epoch >= $versioning_observed_at and
      .exported_through_epoch <= $retention[0].created_at_epoch
    )
  ' "$evidence_path" >/dev/null ||
    die "log readiness evidence/retention exportの内容または時刻が不正です"

  [ "$(sha256_file "$evidence_path")" = \
    "$(jq -er '.evidence_artifact_sha256' "$receipt")" ] &&
    [ "$(stat_identity "$evidence_path")" = "$evidence_identity" ] ||
    die "検証中にlog readiness evidence artifactが差替えられました"
  [ "$(sha256_file "$retention_path")" = \
    "$(jq -er '.retention_export_manifest_sha256' "$evidence_path")" ] &&
    [ "$(stat_identity "$retention_path")" = "$retention_identity" ] ||
    die "検証中にretention export manifestが差替えられました"
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
      "mfa_delete",
      "producer_action",
      "required_status_after",
      "required_status_before"
    ] | sort) and
    .log_versioning_stage.id ==
      "2026-07-log-versioning-attest-v2" and
    .log_versioning_stage.enabled == true and
    (.log_versioning_stage.expires_at | fromdateiso8601) > $now and
    .log_versioning_stage.buckets == [
      "teamagent-dev-cloudtrail-718959508629",
      "teamagent-dev-bedrock-logs-718959508629"
    ] and
    .log_versioning_stage.allowed_write == "none" and
    .log_versioning_stage.required_status_before == ["Enabled"] and
    .log_versioning_stage.required_status_after == "Enabled" and
    .log_versioning_stage.mfa_delete == "Disabled" and
    .log_versioning_stage.producer_action == "active-observation-only" and
    .log_versioning_stage.cutover_mode ==
      "pre-versioned-destination-before-producer-cutover"
  ' "$MIGRATION_FILE" >/dev/null ||
    die "log versioning stageはreview済みmanifestでenabledかつ期限内の場合だけ実行できます"
}

migration_to_file() {
  local migration_id="$1" output="$2"
  jq -e -S -c --arg id "$migration_id" '
    .schema_version == 1 and
    .external_state_handoffs["2026-07-alarm-topic-consolidation-v1"] == {
      canonical_topic_arn:
        "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-openclaw-alarms",
      canonical_owner: "aws_sns_topic.alarms",
      legacy_topic_arn:
        "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-alarms",
      legacy_owner: "external-teamagent-state",
      import_legacy_into_this_state: false,
      ordered_phases: [
        "configure canonical email or chat destination",
        "confirm delivery and verify canonical metadata",
        "retarget every legacy alarm and budget publisher",
        "retire legacy topic from its owning state",
        "run strict sync, then activation migration"
      ],
      activation_requires: {
        minimum_confirmed_destinations: 1,
        legacy_topic_exists: false,
        legacy_action_reference_count: 0
      }
    } and
    (.migrations[$id] | type == "object") and
    .migrations[$id].enabled == true and
    (.migrations[$id].expires_at | fromdateiso8601 > now) and
    (
      if .migrations[$id].kind == "runtime" then
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
          test("^718959508629[.]dkr[.]ecr[.]ap-northeast-1[.]amazonaws[.]com/teamagent-dev-tiktok-acquire@sha256:[0-9a-f]{64}$")) and
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
          ingest: "DISABLED", morning: "ENABLED", canary: "DISABLED"
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
        (
          .migrations[$id].from.alarm_delivery.confirmed_email_endpoint_sha256 |
          type == "array" and
          all(test("^[0-9a-f]{64}$")) and
          length == (unique | length)
        ) and
        (
          .migrations[$id].from.alarm_delivery.attached_chatbot_configuration_arns |
          type == "array" and
          all(test(
            "^arn:aws:chatbot::718959508629:chat-configuration/(slack-channel|microsoft-teams-channel)/[A-Za-z0-9._-]+$"
          )) and
          length == (unique | length)
        ) and
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
          ingest: "DISABLED", morning: "ENABLED", canary: "DISABLED"
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
          minimum_confirmed_destinations: 1,
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
    die "migrationが未登録・disabled・期限切れ、またはdestination digestがexactではありません: $migration_id"
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
      $live.targets.ingest.task_definition == $live.taskdefs.ingest.arn and
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
      and (
        ($live.alarm_delivery.confirmed_email_endpoint_sha256 | length) +
        ($live.alarm_delivery.attached_chatbot_configuration_arns | length)
      ) >= $m.from.alarm_delivery.minimum_confirmed_destinations
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
    --bucket "$bucket" --output json > "$raw" 2> "$error_file"; then
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
    --output json > "$dir/cloudtrail-versioning.json"
  aws_cli s3api get-bucket-versioning --bucket "$bedrock_logs_bucket" \
    --output json > "$dir/bedrock-versioning.json"
  capture_cloudtrail_lifecycle_contract \
    "$cloudtrail_bucket" "$dir/cloudtrail-lifecycle-contract.json"
  for versioning_file in \
    "$dir/cloudtrail-versioning.json" \
    "$dir/bedrock-versioning.json"; do
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

  : > "$dir/confirmed-email-hashes.txt"
  : > "$dir/confirmed-subscription-attributes.jsonl"
  local subscription_count subscription_index subscription_arn
  local subscription_protocol subscription_endpoint normalized_endpoint
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
    case "$subscription_arn" in
      PendingConfirmation|Deleted)
        subscription_index=$((subscription_index + 1))
        continue
        ;;
    esac
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
      die "SNS subscription attributesがconfirmed/no-filter exact契約を満たしません"
    case "$subscription_protocol" in
      email|email-json)
        normalized_endpoint="$(printf '%s' "$subscription_endpoint" |
          tr '[:upper:]' '[:lower:]')"
        [ -n "$normalized_endpoint" ] ||
          die "SNS email endpoint metadataが空です"
        printf '%s' "$normalized_endpoint" | sha256_text \
          >> "$dir/confirmed-email-hashes.txt"
        printf '\n' >> "$dir/confirmed-email-hashes.txt"
        ;;
    esac
    jq -n -S -c \
      --arg subscription_arn "$subscription_arn" \
      --arg protocol "$subscription_protocol" \
      --arg endpoint_sha256 "$(printf '%s' "$subscription_endpoint" | sha256_text)" \
      '{
        subscription_arn:$subscription_arn,
        protocol:$protocol,
        endpoint_sha256:$endpoint_sha256,
        confirmed:true,
        filter_policy_present:false
      }' >> "$dir/confirmed-subscription-attributes.jsonl"
    subscription_index=$((subscription_index + 1))
  done
  jq -s -S -c 'sort_by(.subscription_arn)' \
    "$dir/confirmed-subscription-attributes.jsonl" \
    > "$dir/confirmed-subscription-attributes.json"
  jq -R -s -c '
    split("\n") | map(select(length > 0)) | sort |
    if length == (unique | length) then . else error("duplicate email hash") end
  ' "$dir/confirmed-email-hashes.txt" \
    > "$dir/confirmed-email-hashes.json" ||
    die "確認済みSNS email metadataを安全にhash化できません"

  aws_cli chatbot describe-slack-channel-configurations --output json \
    > "$dir/chatbot-slack.json"
  aws_cli chatbot list-microsoft-teams-channel-configurations --output json \
    > "$dir/chatbot-teams.json"
  jq -e '
    (.SlackChannelConfigurations // [] | type) == "array" and
    all(.SlackChannelConfigurations[];
      (.ChatConfigurationArn | type) == "string" and
      (.SnsTopicArns // [] | type) == "array")
  ' "$dir/chatbot-slack.json" >/dev/null ||
    die "Slack chat integration metadataが不正です"
  jq -e '
    (.TeamChannelConfigurations // [] | type) == "array" and
    all(.TeamChannelConfigurations[];
      (.ChatConfigurationArn | type) == "string" and
      (.SnsTopicArns // [] | type) == "array")
  ' "$dir/chatbot-teams.json" >/dev/null ||
    die "Teams chat integration metadataが不正です"

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
  local rule
  for rule in "$ingest_rule" "$morning_rule" "$canary_rule"; do
    local key
    case "$rule" in
      "$ingest_rule") key="ingest" ;;
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
      ingest) ingest_arn="$target_arn"; ingest_state="$rule_state" ;;
      morning) morning_arn="$target_arn"; morning_state="$rule_state" ;;
      canary) canary_arn="$target_arn"; canary_state="$rule_state" ;;
    esac
  done

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
      (.EventSourceMappings | length) == 1 and
      .EventSourceMappings[0].State == "Enabled" and
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
    --arg confirmed_subscription_metadata_sha256 \
      "$(sha256_file "$dir/confirmed-subscription-attributes.json")" \
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
          confirmed_subscription_metadata_sha256:
            $confirmed_subscription_metadata_sha256,
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
    . + {
      hmac: {
        mail: uniform(
          [.taskdefs.mcp, .taskdefs.connect_web, .taskdefs.morning];
          "mail"
        ),
        report: uniform(
          [.taskdefs.mcp, .taskdefs.connect_web];
          "report"
        )
      }
    }
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
  local tiktok_repo="${account_id}.dkr.ecr.${REGION}.amazonaws.com/${PROJECT}-${ENVIRONMENT}-tiktok-acquire"
  [[ "$tiktok_image" == "$tiktok_repo@sha256:"* ]] ||
    die "TikTok live imageは同一account/regionの専用ECR digestである必要があります"
  [[ "${tiktok_image#*@}" =~ ^sha256:[0-9a-f]{64}$ ]] ||
    die "TikTok live imageが完全digest pinではありません"
  local openclaw_image="${account_id}.dkr.ecr.${REGION}.amazonaws.com/teamagent-openclaw"
  [[ "$(jq -er '.taskdefs.openclaw.image' "$output")" == "$openclaw_image@sha256:"* ]] ||
    die "OpenClaw live imageは同一account/regionの専用ECR digestである必要があります"
  jq -e '
    .taskdefs.connect_web.env.CONNECT_APP_HTML_S3_URI ==
      "s3://teamagent-dev-raw-files/codebuild/connect-web-app.html" and
    .dispatchers.tiktok.task_definition == .taskdefs.tiktok.arn and
    .dispatchers.x_buzz.task_definition == .taskdefs.x_buzz.arn and
    .event_mappings.tiktok.critical.enabled == true and
    .event_mappings.x_buzz.critical.enabled == true and
    .event_mappings.tiktok.critical.function_arn == .dispatchers.tiktok.critical.function_arn and
    .event_mappings.x_buzz.critical.function_arn == .dispatchers.x_buzz.critical.function_arn
  ' "$output" >/dev/null || die "worker dispatcher/taskdef/event mappingのlive契約が不整合です"
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
  local preflight_sha256="$9"
  local hmac_transition_epoch="${10}"
  local desired_ingest_rule="${11:-}"
  local desired_morning_rule="${12:-}"
  local desired_canary_rule="${13:-}"
  if [ -z "$desired_ingest_rule" ]; then
    desired_ingest_rule="$(jq -r '.rules.ingest.critical.state == "ENABLED"' "$snapshot")"
    desired_morning_rule="$(jq -r '.rules.morning.critical.state == "ENABLED"' "$snapshot")"
    desired_canary_rule="$(jq -r '.rules.canary.critical.state == "ENABLED"' "$snapshot")"
  fi
  if [ "$mode" = "sync" ]; then
    jq -e '
      [
        .taskdefs.mcp.image,
        .taskdefs.connect_web.image,
        .taskdefs.ingest.image,
        .taskdefs.morning.image,
        .taskdefs.canary.image
      ] | unique | length == 1
    ' "$snapshot" >/dev/null ||
      die "strict syncは主要5 runtimeのdigest完全一致が必要です。divergent liveはexact one-time migrationでのみ収束できます"
  fi
  jq -S -c \
    --arg mode "$mode" \
    --arg migration_id "$migration_id" \
    --arg desired_openclaw_image "$desired_openclaw_image" \
    --arg desired_mcp_image "$desired_mcp_image" \
    --arg desired_x_image "$desired_x_image" \
    --arg desired_tiktok_image "$desired_tiktok_image" \
    --arg preflight_sha256 "$preflight_sha256" \
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
      live_openclaw_image: $s.taskdefs.openclaw.image,
      desired_openclaw_image: $desired_openclaw_image,
      live_mcp_image: $s.taskdefs.mcp.image,
      desired_mcp_image: $desired_mcp_image,
      live_x_image: $s.taskdefs.x_buzz.image,
      desired_x_image: $desired_x_image,
      live_tiktok_image: $s.taskdefs.tiktok.image,
      desired_tiktok_image: $desired_tiktok_image,
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
      (.alarm_delivery.confirmed_subscription_metadata_sha256 |
        test("^[0-9a-f]{64}$")) and
      (
        (
          (.alarm_delivery.confirmed_email_endpoint_sha256 | length) == 1 and
          (.alarm_delivery.attached_chatbot_configuration_arns | length) == 0
        ) or
        (
          (.alarm_delivery.confirmed_email_endpoint_sha256 | length) == 0 and
          (.alarm_delivery.attached_chatbot_configuration_arns | length) > 0
        )
      ) and
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
    [
      "# terraform_runtime_guard.sh snapshot (non-secret / live-derived)",
      "# 既存の gitignored terraform.tfvars へ必要行だけ反映し、この出力自体は commit しない。",
      line("openclaw_image"; .live_openclaw_image),
      line("mcp_image"; .live_mcp_image),
      line("x_buzz_image"; .live_x_image),
      line("tiktok_acquire_image"; .live_tiktok_image),
      line("enable_connect_web"; .enable_connect_web),
      line("enable_ingest_schedule"; .enable_ingest_schedule),
      line("enable_morning_digest"; .enable_morning_digest),
      line("enable_canary_health"; .enable_canary_health),
      line("enable_x_research"; .enable_x_research),
      line("enable_tiktok_acquire"; .enable_tiktok_acquire),
      line("enable_scrape_tools"; .enable_scrape_tools),
      line("enable_reminders"; .enable_reminders),
      line("enable_report_shorturl"; .enable_report_shorturl),
      line("enable_research_persist"; .enable_research_persist),
      line("enable_kaiwai_classify"; .enable_kaiwai_classify),
      line("use_calendar_event_tool"; .use_calendar_event_tool),
      line("use_schedule_propose_tool"; .use_schedule_propose_tool),
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

validate_common_plan_schema() {
  local plan_json="$1" core="$2"
  jq -e --slurpfile expected_core "$core" '
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
    .variables.tiktok_acquire_image.value == $expected_core[0].desired_tiktok_image and
    .variables.ingest_rule_enabled.value == $expected_core[0].ingest_rule_enabled and
    .variables.morning_digest_rule_enabled.value ==
      $expected_core[0].morning_digest_rule_enabled and
    .variables.canary_rule_enabled.value == $expected_core[0].canary_rule_enabled and
    .variables.require_alarm_delivery.value == true and
    .variables.bedrock_logs_retention_days.value == 60 and
    .variables.runtime_guard_live.value == $expected_core[0]
  ' "$plan_json" >/dev/null ||
    die "plan JSON schema/check/image/rule/runtime guard bindingが不正です"
}

validate_manifest_change_allowlist() {
  local plan_json="$1" migration="$2"
  local unexpected destructive
  unexpected="$(jq -r --slurpfile migration "$migration" '
    ($migration[0].allowed_changes // []) as $allowed |
    .resource_changes[]? |
    select(.mode == "managed" and .change.actions != ["no-op"]) |
    .address as $address |
    select(($allowed | index($address)) == null) |
    "\(.change.actions | join("/")) \(.address)"
  ' "$plan_json")"
  [ -z "$unexpected" ] ||
    die "migration exact allowlist外の変更を検出しました:\n$unexpected"

  destructive="$(jq -r '
    .resource_changes[]? |
    select(.mode == "managed") |
    (.change.actions // []) as $actions |
    select($actions == ["delete"] or $actions == ["delete", "create"]) |
    "\($actions | join("/")) \(.address)"
  ' "$plan_json")"
  [ -z "$destructive" ] ||
    die "delete-first/pure destroyはmigrationでも禁止です:\n$destructive"

  unexpected="$(jq -r --slurpfile migration "$migration" '
    ($migration[0].allowed_changes // []) as $allowed |
    .resource_drift[]? |
    .address as $address |
    select(($allowed | index($address)) == null) |
    "\(.change.actions | join("/")) \(.address)"
  ' "$plan_json")"
  [ -z "$unexpected" ] ||
    die "migration allowlist外resourceのdriftを検出しました:\n$unexpected"
}

validate_runtime_task_contracts() {
  local plan_json="$1" snapshot="$2" core="$3"

  jq -e \
    --arg mcp_health \
      "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4).read()\"" \
    --arg connect_health \
      "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/healthz', timeout=4).read()\"" \
    --arg openclaw_health \
      "fetch('http://127.0.0.1:18789/readyz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" \
    --slurpfile core "$core" '
    def envmap:
      (.environment // []) | map({key: .name, value: .value}) | from_entries;
    def plain_tmp_volume:
      .name == "tmp" and
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
        select(.sourceVolume == "tmp" and .containerPath == "/tmp" and
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
        name: "openclaw", image: $core[0].desired_openclaw_image,
        user: "65532:65532", profile: "openclaw"
      },
      {
        address: "aws_ecs_task_definition.mcp",
        name: "teamagent-mcp", image: $core[0].desired_mcp_image,
        user: "10001:10001", profile: "python"
      },
      {
        address: "aws_ecs_task_definition.connect_web[0]",
        name: "connect-web", image: $core[0].desired_mcp_image,
        user: "10001:10001", profile: "python"
      },
      {
        address: "aws_ecs_task_definition.ingest[0]",
        name: "ingest", image: $core[0].desired_mcp_image,
        user: "10001:10001", profile: "python"
      },
      {
        address: "aws_ecs_task_definition.morning_digest[0]",
        name: "morning-digest", image: $core[0].desired_mcp_image,
        user: "10001:10001", profile: "python"
      },
      {
        address: "aws_ecs_task_definition.canary[0]",
        name: "canary", image: $core[0].desired_mcp_image,
        user: "10001:10001", profile: "python"
      },
      {
        address: "aws_ecs_task_definition.tiktok_acquire[0]",
        name: "acquire", image: $core[0].desired_tiktok_image,
        user: "10001:10001", profile: "tiktok"
      },
      {
        address: "aws_ecs_task_definition.x_buzz_worker[0]",
        name: "worker", image: $core[0].desired_x_image,
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
          ([$task.volume[] | select(plain_tmp_volume)] | length) == 1 and
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
        elif $spec.profile == "tiktok" then
          ($task.volume | length) == 1 and
          ($task.volume[0] | plain_tmp_volume) and
          exact_command($container; ["npx", "tsx", "src/job.ts"]) and
          no_health($container) and
          $env.HOME == "/tmp/home" and
          $env.XDG_CACHE_HOME == "/tmp/.cache" and
          $env.npm_config_cache == "/tmp/.npm" and
          $env.PUPPETEER_CACHE_DIR == "/tmp/.cache/puppeteer" and
          $env.PLAYWRIGHT_BROWSERS_PATH == "/opt/pw"
        elif $spec.name == "teamagent-mcp" then
          ($task.volume | length) == 1 and
          ($task.volume[0] | plain_tmp_volume) and
          exact_command(
            $container;
            if $core[0].enable_scrape_tools
            then ["sh", "scripts/run_mcp_vertex_entrypoint.sh"]
            else []
            end
          ) and
          exact_health(
            $container;
            ["CMD-SHELL", $mcp_health];
            40
          ) and
          $env.HOME == "/tmp/home" and
          $env.XDG_CACHE_HOME == "/tmp/.cache" and
          $env.PYTHONPYCACHEPREFIX == "/tmp/.pycache"
        elif $spec.name == "connect-web" then
          ($task.volume | length) == 1 and
          ($task.volume[0] | plain_tmp_volume) and
          exact_command(
            $container;
            ["python", "-m", "teamagent.connect_web"]
          ) and
          exact_health(
            $container;
            ["CMD-SHELL", $connect_health];
            30
          ) and
          $env.HOME == "/tmp/home" and
          $env.XDG_CACHE_HOME == "/tmp/.cache" and
          $env.PYTHONPYCACHEPREFIX == "/tmp/.pycache"
        else
          ($task.volume | length) == 1 and
          ($task.volume[0] | plain_tmp_volume) and
          exact_command(
            $container;
            if $spec.name == "ingest" then
              ["python", "scripts/run_ingest_fargate.py"]
            elif $spec.name == "morning-digest" then
              ["python", "scripts/run_morning_digest_fargate.py"]
            elif $spec.name == "canary" then
              ["python", "scripts/run_canary_health.py"]
            elif $spec.name == "worker" then
              ["python", "-m", "teamagent.workers.x_buzz_job"]
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
    'aws_ecs_task_definition.tiktok_acquire[0]|tiktok|acquire|["HOME","TMPDIR","XDG_CACHE_HOME","npm_config_cache","PUPPETEER_CACHE_DIR","PLAYWRIGHT_BROWSERS_PATH","CHROMIUM_PATH"]|[]' \
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
    def purpose($container; $prefix; $expected):
      (envmap($container)) as $env |
      (secmap($container)) as $secrets |
      $secrets[$prefix + "_SECRET"] == $expected.primary_secret_arn and
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
    (container("aws_ecs_task_definition.mcp"; "teamagent-mcp")) as $mcp |
    (container("aws_ecs_task_definition.connect_web[0]"; "connect-web")) as $connect |
    (container("aws_ecs_task_definition.morning_digest[0]"; "morning-digest")) as $morning |
    purpose($mcp; "MAIL_ACTION_HMAC"; $proposed[0].mail) and
    purpose($connect; "MAIL_ACTION_HMAC"; $proposed[0].mail) and
    purpose($morning; "MAIL_ACTION_HMAC"; $proposed[0].mail) and
    purpose($mcp; "REPORT_LINK_HMAC"; $proposed[0].report) and
    purpose($connect; "REPORT_LINK_HMAC"; $proposed[0].report)
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
      ($change.change.before | guard_service_from_tf) ==
        $live[0].services[$component].critical and
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
          $change.change.after.deployment_circuit_breaker[0] ==
            {enable: true, rollback: true} and
          (($change.change.after | del(
             .task_definition, .deployment_circuit_breaker,
             .availability_zone_rebalancing
           )) ==
           ($change.change.before | del(
             .task_definition, .deployment_circuit_breaker,
             .availability_zone_rebalancing
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
      .resource_changes[] | select(.address == $address) as $change |
      ($change.change.before.ecs_target[0].task_definition_arn ==
        $live[0].targets[$component].task_definition) and
      (($change.change.before | guard_target_from_tf) ==
        $live[0].targets[$component].critical) and
      (($change.change.after | del(.ecs_target[0].task_definition_arn)) ==
       ($change.change.before | del(.ecs_target[0].task_definition_arn)))
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
      (($change.change.before | strip_provider_computed) ==
       ($change.change.after | strip_provider_computed)) and
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
        .environment = $after_environment
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
    def retention_adoption($address; $name):
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
        if $log.before.retention_in_days == 0 then
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
        "/aws/codebuild/teamagent-dev-aiia-image-builder"
      ],
      [
        "aws_cloudwatch_log_group.codebuild_image_builder",
        "/aws/codebuild/teamagent-dev-image-builder"
      ],
      [
        "aws_cloudwatch_log_group.reminder_notify",
        "/aws/lambda/teamagent-dev-reminders-notify"
      ],
      [
        "aws_cloudwatch_log_group.tiktok_dispatch",
        "/aws/lambda/teamagent-dev-tiktok-acquire-dispatch"
      ],
      [
        "aws_cloudwatch_log_group.x_dispatch",
        "/aws/lambda/teamagent-dev-x-buzz-dispatch"
      ]
    ] | all(. as $spec | $plan | retention_adoption($spec[0]; $spec[1]))
  ' "$plan_json" >/dev/null ||
    die "auto-created CodeBuild/Lambda log groupはexact import・30日・KMS不変のin-place更新だけを許可します"
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
      .variables.alarm_email_endpoints.value[0] |
      gsub("^\\s+|\\s+$"; "") |
      ascii_downcase
    ' "$plan_json")"
    configured_email_hash="$(printf '%s' "$configured_email" | sha256_text)"
  fi
  jq -e --arg configured_email_hash "$configured_email_hash" '
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
    (
      (($emails | length) == 1 and ($chat | length) == 0) or
      (($emails | length) == 0 and ($chat | length) > 0)
    ) and
    $topic.change.after.name == "teamagent-dev-openclaw-alarms" and
    ($topic.change.actions | index("delete") | not) and
    ([.resource_changes[] |
      select(.type == "aws_sns_topic_subscription")] | length) == 0 and
    (
      if ($emails | length) == 1 then
        $configured_email_hash != "" and
        $live_delivery.confirmed_email_endpoint_sha256 ==
          [$configured_email_hash] and
        $live_delivery.attached_chatbot_configuration_arns == []
      else
        $live_delivery.confirmed_email_endpoint_sha256 == [] and
        ($live_delivery.attached_chatbot_configuration_arns | sort) ==
          ($chat | sort)
      end
    ) and
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
    ($bedrock_document.Statement | length) == 2 and
    ($bedrock_document.Statement | map(.Sid) | sort) ==
      (["AllowBedrockPut", "DenyInsecureTransport"] | sort) and
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
       select(.id == "bedrock-current-59-noncurrent-1-total-60-days")] |
      if length != 1 then false else .[0] |
        .status == "Enabled" and
        (.filter | length) == 1 and .filter[0].prefix == "bedrock/" and
        (.expiration | length) == 1 and .expiration[0].days == 59 and
        (.expiration[0].expired_object_delete_marker // false) == false and
        (.noncurrent_version_expiration | length) == 1 and
        .noncurrent_version_expiration[0].noncurrent_days == 1 and
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
    .variables.bedrock_logs_retention_days.value == 60
  ' "$plan_json" >/dev/null ||
    die "CloudTrail/Bedrock log bucketがversioning/TLS/合計60日/KMSとproducer no-op契約を満たしません"
}

validate_retired_builder_and_admin_noninterference_plan() {
  local plan_json="$1"
  jq -e '
    def resource($address):
      [.resource_changes[] | select(.address == $address)] |
      if length == 1 then .[0] else error("required retired builder resource missing") end;
    def converges($change):
      ($change.actions == ["create"] or
       $change.actions == ["update"] or
       $change.actions == ["no-op"]) and
      (($change.actions | index("delete")) == null);
    def array:
      if type == "array" then . else [.] end;
    resource("aws_codebuild_project.image") as $project |
    resource("aws_iam_role_policy.codebuild") as $legacy_policy |
    ($legacy_policy.change.after.policy | fromjson) as $legacy_document |
    converges($project.change) and
    $project.change.after.name == "teamagent-dev-image-builder" and
    $project.change.after.description ==
      "RETIRED - mutable source.zip release publishing is denied" and
    ($project.change.after.artifacts | length) == 1 and
    $project.change.after.artifacts[0].type == "NO_ARTIFACTS" and
    ($project.change.after.environment | length) == 1 and
    $project.change.after.environment[0].privileged_mode == false and
    ($project.change.after.environment[0].environment_variable // []) == [] and
    ($project.change.after.source | length) == 1 and
    $project.change.after.source[0].type == "NO_SOURCE" and
    ($project.change.after.source[0].location // "") == "" and
    ($project.change.after.source[0].buildspec |
      contains("RETIRED: mutable source.zip image publishing is disabled") and
      contains("exit 64")) and
    $project.change.after.logs_config[0].cloudwatch_logs[0].status == "DISABLED" and
    $project.change.after.logs_config[0].s3_logs[0].status == "DISABLED" and
    converges($legacy_policy.change) and
    $legacy_policy.change.after.name == "teamagent-dev-codebuild-image" and
    ($legacy_document.Statement | length) == 1 and
    $legacy_document.Statement[0].Sid == "DenyLegacyBuildAwsAccess" and
    $legacy_document.Statement[0].Effect == "Deny" and
    ($legacy_document.Statement[0].Resource | array) == ["*"] and
    ($legacy_document.Statement[0].Action | array | sort) == ([
      "codebuild:*", "ecr:*", "ecs:*", "iam:PassRole", "lambda:*",
      "s3:*", "secretsmanager:*", "ssm:*", "ssmmessages:*",
      "sts:AssumeRole"
    ] | sort) and
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
    die "retired CodeBuild契約またはadministrator IAM非干渉契約を満たしません"
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
        "tiktok/proxy|tiktok/apify-token|hmac/mail-action|" +
        "hmac/report-link)-[A-Za-z0-9]{6}$"
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
          (resources | length > 0 and all(exact_secret_arn))) and
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
  validate_manifest_change_allowlist "$plan_json" "$migration"
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
    $morning.change.before.state == "ENABLED" and
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
  validate_manifest_change_allowlist "$plan_json" "$migration"
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
  validate_retired_builder_and_admin_noninterference_plan "$plan_json"
  validate_exact_runtime_iam_plan "$plan_json"

  jq -e '
    def changed($address):
      [.resource_changes[] | select(.address == $address)][0].change.after;
    changed("aws_sqs_queue.tiktok_jobs[0]") as $tiktok |
    changed("aws_sqs_queue.x_jobs[0]") as $x |
    $tiktok.message_retention_seconds == 1209600 and
    ($tiktok.redrive_policy | fromjson | .maxReceiveCount) == 24 and
    $x.message_retention_seconds == 1209600 and
    ($x.redrive_policy | fromjson | .maxReceiveCount) == 24 and
    changed("aws_lambda_event_source_mapping.tiktok_dispatch[0]").function_response_types ==
      ["ReportBatchItemFailures"] and
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

  validate_common_plan_schema "$plan_json" "$core"
  if [ "$(jq -er '.mode' "$core")" = "migration" ]; then
    [ -n "$migration" ] && [ -n "$proposed_hmac" ] ||
      die "migration plan validator内部bindingが不足しています"
    case "$(jq -er '.kind' "$migration")" in
      runtime)
        validate_runtime_migration_plan \
          "$plan_json" "$snapshot" "$core" "$migration" "$proposed_hmac" \
          "$state_contract"
        ;;
      activation)
        validate_activation_plan "$plan_json" "$snapshot" "$migration"
        ;;
      *) die "未知のmigration kindです" ;;
    esac
    return 0
  fi

  jq -e --arg desired_image "$desired_image" --slurpfile expected_core "$core" '
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
          .change.actions == ["create", "delete"])
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
    .variables.tiktok_acquire_image.value == $expected_core[0].desired_tiktok_image and
    .variables.require_alarm_delivery.value == true and
    .variables.bedrock_logs_retention_days.value == 60 and
    .variables.runtime_guard_live.value == $expected_core[0]
  ' "$plan_json" >/dev/null ||
    die "plan JSONのschema/action/check/runtime_guard束縛が不正です"

  local allowed_replacements
  allowed_replacements='["aws_ecs_task_definition.openclaw[0]","aws_ecs_task_definition.mcp","aws_ecs_task_definition.connect_web[0]","aws_ecs_task_definition.ingest[0]","aws_ecs_task_definition.morning_digest[0]","aws_ecs_task_definition.canary[0]","aws_ecs_task_definition.tiktok_acquire[0]","aws_ecs_task_definition.x_buzz_worker[0]"]'
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

  local spec address component expected_name image_kind expected_image
  for spec in \
    'aws_ecs_task_definition.openclaw[0]|openclaw|openclaw|openclaw' \
    'aws_ecs_task_definition.mcp|mcp|teamagent-mcp|mcp' \
    'aws_ecs_task_definition.connect_web[0]|connect_web|connect-web|mcp' \
    'aws_ecs_task_definition.ingest[0]|ingest|ingest|mcp' \
    'aws_ecs_task_definition.morning_digest[0]|morning|morning-digest|mcp' \
    'aws_ecs_task_definition.canary[0]|canary|canary|mcp' \
    'aws_ecs_task_definition.tiktok_acquire[0]|tiktok|acquire|tiktok' \
    'aws_ecs_task_definition.x_buzz_worker[0]|x_buzz|worker|x'; do
    IFS='|' read -r address component expected_name image_kind <<< "$spec"
    case "$image_kind" in
      openclaw) expected_image="$(jq -er '.desired_openclaw_image' "$core")" ;;
      mcp) expected_image="$desired_image" ;;
      x) expected_image="$(jq -er '.desired_x_image' "$core")" ;;
      tiktok) expected_image="$(jq -er '.desired_tiktok_image' "$core")" ;;
      *) die "内部error: unknown task image kind" ;;
    esac
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
        die "$address は期待container $expected_name・候補image・unknown allowlistを満たしません"

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
        (($change.change.before | guard_service_from_tf) == $live[0].services[$component].critical) and
        (($change.change.before | del(.task_definition)) ==
          ($change.change.after | del(.task_definition)))
      ' "$plan_json" >/dev/null ||
        die "$address はliveからtask_definition参照以外も変更します"
    else
      die "runtime planに必須addressがありません: $address"
    fi
  done

  for spec in \
    'aws_cloudwatch_event_target.ingest_run_task[0]|ingest|aws_cloudwatch_event_target.ingest_run_task|aws_ecs_task_definition.ingest[0]' \
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
          .expressions.ecs_target[0].task_definition_arn.references[]?] |
          index($task_address + ".arn")) as $reference |
        (($change.change.actions == ["no-op"] and
          ([$change.change.after_unknown // {} | paths(. == true)] | length == 0)) or
         ($change.change.actions == ["update"] and
          [$change.change.after_unknown // {} | paths(. == true)] ==
            [["ecs_target", 0, "task_definition_arn"]])) and
        $reference != null and
        ($change.change.before.ecs_target[0].task_definition_arn ==
          $live[0].targets[$component].task_definition) and
        (($change.change.before | guard_target_from_tf) == $live[0].targets[$component].critical) and
        (($change.change.before | del(.ecs_target[0].task_definition_arn)) ==
          ($change.change.after | del(.ecs_target[0].task_definition_arn)))
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
        for path in /tmp/home /tmp/.cache /tmp/.npm /tmp/.cache/puppeteer
        do
          mkdir -p "$path"
          printf writable > "$path/.teamagent-write-probe"
        done
        printf ok > /tmp/teamagent-preflight
        command -v npx
        command -v yt-dlp
        command -v chromium
        npx --version >/dev/null
        npx --no-install tsx --version >/dev/null
        yt-dlp --version >/dev/null
        test -x "$CHROMIUM_PATH"
        test -d "$PLAYWRIGHT_BROWSERS_PATH"
        find "$PLAYWRIGHT_BROWSERS_PATH" -type f -perm -100 -print -quit \
          | grep -q .
        chromium --headless --no-sandbox --disable-gpu \
          --dump-dom "data:text/html,<title>teamagent-preflight</title>" \
          | grep -q teamagent-preflight
        if touch /teamagent-preflight-root-write 2>/dev/null; then exit 42; fi
      '
      volume_json='[{"name":"tmp"}]'
      environment_json='[
        {"name":"HOME","value":"/tmp/home"},
        {"name":"TMPDIR","value":"/tmp"},
        {"name":"XDG_CACHE_HOME","value":"/tmp/.cache"},
        {"name":"npm_config_cache","value":"/tmp/.npm"},
        {"name":"PUPPETEER_CACHE_DIR","value":"/tmp/.cache/puppeteer"},
        {"name":"PLAYWRIGHT_BROWSERS_PATH","value":"/opt/pw"},
        {"name":"CHROMIUM_PATH","value":"/usr/bin/chromium"}
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

write_preflight_receipt() {
  local migration_id="$1" migration="$2" snapshot="$3" profiles="$4" output="$5"
  local config_manifest="$TMP_ROOT/config-manifest.txt"
  write_config_manifest "$config_manifest"
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
  write_config_manifest "$config_manifest"
  jq -e \
    --arg version "$GUARD_VERSION" \
    --arg migration_id "$migration_id" \
    --arg migration_kind "$(jq -er '.kind' "$migration")" \
    --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" \
    --arg git_commit "$(git_commit)" \
    --arg script_sha "$(sha256_file "$SCRIPT_PATH")" \
    --arg jq_sha "$(sha256_file "$GUARD_JQ")" \
    --arg manifest_sha "$(sha256_file "$MIGRATION_FILE")" \
    --arg config_sha "$(sha256_file "$config_manifest")" \
    --arg live_sha "$(sha256_file "$snapshot")" \
    --argjson now "$(date +%s)" \
    --slurpfile migration "$migration" '
    .kind == "runtime-preflight-receipt" and
    .guard_version == $version and
    .migration_id == $migration_id and
    .migration_kind == $migration_kind and
    .account_id == $account and .region == $region and
    .git_commit == $git_commit and
    .guard_script_sha256 == $script_sha and
    .guard_jq_sha256 == $jq_sha and
    .migration_manifest_sha256 == $manifest_sha and
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
  ' "$receipt" >/dev/null || die "preflight receiptが期限・live・commit・hash・profile契約と不一致です"
}

verify_receipt() {
  local plan="$1"
  local receipt="$2"
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
      "images",
      "kind",
      "live_fingerprint_sha256",
      "log_readiness_receipt_path",
      "log_readiness_receipt_sha256",
      "migration_id",
      "migration_kind",
      "migration_manifest_sha256",
      "mode",
      "plan_path",
      "plan_sha256",
      "preflight_receipt_path",
      "preflight_receipt_sha256",
      "project",
      "receipt_path",
      "region",
      "rule_states",
      "runtime_guard_sha256",
      "state_contract",
      "var_file",
      "var_file_sha256",
      "versioning_receipt_path",
      "versioning_receipt_sha256"
    ] | sort) and
    (.state_contract | keys | sort) == ["backend","imports","state"] and
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
    .config_manifest_sha256 == $config_sha and
    .created_at_epoch <= $now and .expires_at_epoch > $now and
    (.expires_at_epoch - .created_at_epoch) <= 3600 and
    (.images.live | type == "object") and
    (.images.desired | type == "object") and
    (.rule_states.live | type == "object") and
    (.rule_states.desired | type == "object") and
    (.images.live | keys | sort) == ["mcp","openclaw","tiktok","x_buzz"] and
    (.images.desired | keys | sort) == ["mcp","openclaw","tiktok","x_buzz"] and
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
    (.state_contract.imports | keys | sort) == ([
      "aws_cloudwatch_log_group.codebuild_aiia_image_builder",
      "aws_cloudwatch_log_group.codebuild_image_builder",
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
        .log_readiness_receipt_sha256 == ""
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
        (.log_readiness_receipt_sha256 | test("^[0-9a-f]{64}$"))
      end
    )
  ' "$stage/receipt.json" >/dev/null || die "receipt schema/bindingが不正です"

  local bound_plan bound_receipt var_file preflight_receipt alarm_delivery_receipt
  local versioning_receipt log_readiness_receipt
  local alarm_delivery_receipt_identity=""
  bound_plan="$(jq -er '.plan_path' "$stage/receipt.json")"
  bound_receipt="$(jq -er '.receipt_path' "$stage/receipt.json")"
  var_file="$(jq -er '.var_file' "$stage/receipt.json")"
  [ "$bound_plan" = "$plan" ] || die "receiptが別plan pathに束縛されています"
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

  local plan_sha_before var_sha_before plan_identity var_identity
  plan_identity="$(stat_identity "$plan")"
  var_identity="$(stat_identity "$var_file")"
  plan_sha_before="$(sha256_file "$plan")"
  var_sha_before="$(sha256_file "$var_file")"
  [ "$plan_sha_before" = "$(jq -er '.plan_sha256' "$stage/receipt.json")" ] || die "plan SHA256がreceiptと不一致です"
  [ "$var_sha_before" = "$(jq -er '.var_file_sha256' "$stage/receipt.json")" ] || die "var-file SHA256がreceiptと不一致です"
  cp "$plan" "$stage/plan.tfplan"
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
    . == $receipt[0].state_contract
  ' "$stage/state-before.json" >/dev/null ||
    die "backend/workspace/state lineage/serial/address ownershipがreceiptから変化しました"
  snapshot_live "$stage/live-before.json"
  [ "$(sha256_file "$stage/live-before.json")" = "$expected_live_sha" ] ||
    die "plan作成後にlive runtimeが変化しました"
  jq -e --slurpfile receipt "$stage/receipt.json" '
    {
      openclaw: .taskdefs.openclaw.image,
      mcp: .taskdefs.mcp.image,
      x_buzz: .taskdefs.x_buzz.image,
      tiktok: .taskdefs.tiktok.image
    } == $receipt[0].images.live and
    {
      ingest: .rules.ingest.critical.state,
      morning: .rules.morning.critical.state,
      canary: .rules.canary.critical.state
    } == $receipt[0].rule_states.live
  ' "$stage/live-before.json" >/dev/null ||
    die "receiptのlive image/EventBridge state束縛が現在liveと一致しません"
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

  local migration_file=""
  if [ "$mode" = "migration" ]; then
    migration_file="$stage/migration.json"
    migration_to_file "$migration_id" "$migration_file"
    validate_migration_source "$stage/live-before.json" "$migration_file"
    if [ "$mode" = "migration" ]; then
      verify_preflight_receipt \
        "$preflight_receipt" "$migration_id" "$migration_file" \
        "$stage/live-before.json"
    fi
  fi

  core_from_snapshot \
    "$stage/live-before.json" "$stage/core.json" "$mode" "$migration_id" \
    "$(jq -er '.images.desired.openclaw' "$stage/receipt.json")" \
    "$(jq -er '.images.desired.mcp' "$stage/receipt.json")" \
    "$(jq -er '.images.desired.x_buzz' "$stage/receipt.json")" \
    "$(jq -er '.images.desired.tiktok' "$stage/receipt.json")" \
    "$(jq -r '.preflight_receipt_sha256' "$stage/receipt.json")" \
    "$transition_epoch" \
    "$(jq -er '.rule_states.desired.ingest' "$stage/receipt.json")" \
    "$(jq -er '.rule_states.desired.morning' "$stage/receipt.json")" \
    "$(jq -er '.rule_states.desired.canary' "$stage/receipt.json")"
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
  [ "$(sha256_file "$stage/plan.tfplan")" = "$plan_sha_before" ] || die "plan検証後のprivate copy改ざんを検出しました"

  snapshot_live "$stage/live-after.json"
  capture_state_contract "$stage/state-after.json"
  [ "$(sha256_file "$stage/live-after.json")" = "$expected_live_sha" ] ||
    die "verify中にlive runtimeが変化しました"
  [ "$(sha256_file "$stage/state-after.json")" = \
    "$(sha256_file "$stage/state-before.json")" ] ||
    die "verify中にbackend/workspace/state lineage/serial/address ownershipが変化しました"
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
}

COMMAND="${1:-}"
case "$COMMAND" in
  -h|--help|help|"") usage; exit 0 ;;
esac
shift
assert_clean_terraform_environment
assert_guard_sources

case "$COMMAND" in
  snapshot)
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    need_cmd aws
    need_cmd jq
    ensure_tmp
    snapshot_live "$TMP_ROOT/live.json"
    core_from_snapshot \
      "$TMP_ROOT/live.json" "$TMP_ROOT/core.json" "sync" "" \
      "$(jq -er '.taskdefs.openclaw.image' "$TMP_ROOT/live.json")" \
      "$(jq -er '.taskdefs.mcp.image' "$TMP_ROOT/live.json")" \
      "$(jq -er '.taskdefs.x_buzz.image' "$TMP_ROOT/live.json")" \
      "$(jq -er '.taskdefs.tiktok.image' "$TMP_ROOT/live.json")" \
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
    VERSIONING_OUT="$(secure_new_file "$VERSIONING_OUT")"
    ensure_tmp
    assert_trusted_automation_identity
    acquire_deployment_lock

    VERSIONING_PUBLISHED="false"
    VERSIONING_STAGE="$(
      mktemp -d \
        "$(dirname "$VERSIONING_OUT")/.teamagent-log-versioning.XXXXXX"
    )"
    chmod 700 "$VERSIONING_STAGE"
    cleanup_versioning_command() {
      local status=$?
      set +e
      release_deployment_lock
      if [ "$VERSIONING_PUBLISHED" != "true" ]; then
        rm -f "$VERSIONING_OUT"
      fi
      rm -rf "$VERSIONING_STAGE" "$TMP_ROOT"
      exit "$status"
    }
    trap 'cleanup_versioning_command' EXIT

    capture_state_contract "$TMP_ROOT/versioning-state-before.json"
    snapshot_live "$TMP_ROOT/versioning-live-before.json"
    capture_log_delivery_contract "$TMP_ROOT/versioning-producer-before.json"
    jq -e '
      .log_buckets.cloudtrail.versioning_status == "Enabled" and
      .log_buckets.cloudtrail.mfa_delete == "Disabled" and
      .log_buckets.cloudtrail.lifecycle.deletion_rule_count == 0 and
      .log_buckets.bedrock == {
        versioning_status:"Enabled",
        mfa_delete:"Disabled"
      }
    ' "$TMP_ROOT/versioning-live-before.json" >/dev/null ||
      die "pre-versioned destinationだけをattestできます。Unversioned/Suspendedは拒否します"

    # The lock is already held. Re-read state, live runtime, producer config,
    # and bucket/lifecycle status. This command performs no versioning write.
    capture_state_contract "$TMP_ROOT/versioning-state-prewrite.json"
    snapshot_live "$TMP_ROOT/versioning-live-prewrite.json"
    capture_log_delivery_contract "$TMP_ROOT/versioning-producer-prewrite.json"
    cmp -s \
      "$TMP_ROOT/versioning-state-before.json" \
      "$TMP_ROOT/versioning-state-prewrite.json" ||
      die "versioning attestation中にTerraform state ownershipが変化しました"
    cmp -s \
      "$TMP_ROOT/versioning-live-before.json" \
      "$TMP_ROOT/versioning-live-prewrite.json" ||
      die "versioning attestation中にlive runtime/bucket状態が変化しました"
    [ "$(
      log_delivery_contract_sha256 \
        "$TMP_ROOT/versioning-producer-before.json"
    )" = "$(
      log_delivery_contract_sha256 \
        "$TMP_ROOT/versioning-producer-prewrite.json"
    )" ] ||
      die "versioning attestation中にCloudTrail/Bedrock producer設定が変化しました"

    snapshot_live "$TMP_ROOT/versioning-live-after.json"
    capture_state_contract "$TMP_ROOT/versioning-state-after.json"
    capture_log_delivery_contract "$TMP_ROOT/versioning-producer-after.json"
    jq -e '
      .log_buckets.cloudtrail.versioning_status == "Enabled" and
      .log_buckets.cloudtrail.mfa_delete == "Disabled" and
      .log_buckets.cloudtrail.lifecycle.deletion_rule_count == 0 and
      .log_buckets.bedrock == {
        versioning_status:"Enabled",
        mfa_delete:"Disabled"
      }
    ' "$TMP_ROOT/versioning-live-after.json" >/dev/null ||
      die "attestation後のversioning/lifecycle状態が不正です"
    cmp -s \
      "$TMP_ROOT/versioning-live-prewrite.json" \
      "$TMP_ROOT/versioning-live-after.json" ||
      die "versioning attestation中にlive状態が変化しました"
    cmp -s \
      "$TMP_ROOT/versioning-state-prewrite.json" \
      "$TMP_ROOT/versioning-state-after.json" ||
      die "versioning attestation中にTerraform state ownershipが変化しました"
    [ "$(
      log_delivery_contract_sha256 \
        "$TMP_ROOT/versioning-producer-prewrite.json"
    )" = "$(
      log_delivery_contract_sha256 \
        "$TMP_ROOT/versioning-producer-after.json"
    )" ] ||
      die "versioning attestation中にCloudTrail/Bedrock producer設定が変化しました"

    VERSIONING_CONFIG_MANIFEST="$TMP_ROOT/versioning-config-manifest.txt"
    write_config_manifest "$VERSIONING_CONFIG_MANIFEST"
    VERSIONING_NOW="$(date +%s)"
    VERSIONING_EXPIRES=$((VERSIONING_NOW + 86400))
    VERSIONING_STAGE_RECEIPT="$VERSIONING_STAGE/receipt.json"
    jq -n -S \
      --arg kind "teamagent-log-versioning-attestation-receipt" \
      --arg stage_id "2026-07-log-versioning-attest-v2" \
      --arg guard_version "$GUARD_VERSION" \
      --arg account_id "$EXPECTED_ACCOUNT_ID" \
      --arg region "$REGION" \
      --arg git_commit "$(git_commit)" \
      --arg guard_script_sha256 "$(sha256_file "$SCRIPT_PATH")" \
      --arg guard_jq_sha256 "$(sha256_file "$GUARD_JQ")" \
      --arg migration_manifest_sha256 "$(sha256_file "$MIGRATION_FILE")" \
      --arg config_manifest_sha256 \
        "$(sha256_file "$VERSIONING_CONFIG_MANIFEST")" \
      --arg deployment_lock_id "$DEPLOYMENT_LOCK_ID" \
      --arg cloudtrail_name \
        "${PROJECT}-${ENVIRONMENT}-cloudtrail-${EXPECTED_ACCOUNT_ID}" \
      --arg bedrock_name \
        "${PROJECT}-${ENVIRONMENT}-bedrock-logs-${EXPECTED_ACCOUNT_ID}" \
      --arg live_after_sha256 \
        "$(sha256_file "$TMP_ROOT/versioning-live-after.json")" \
      --arg producer_contract_sha256 "$(
        log_delivery_contract_sha256 \
          "$TMP_ROOT/versioning-producer-after.json"
      )" \
      --arg producer_evidence_sha256 \
        "$(sha256_file "$TMP_ROOT/versioning-producer-after.json")" \
      --argjson created_at_epoch "$VERSIONING_NOW" \
      --argjson versioning_observed_at_epoch "$VERSIONING_NOW" \
      --argjson expires_at_epoch "$VERSIONING_EXPIRES" \
      --slurpfile before "$TMP_ROOT/versioning-live-prewrite.json" \
      --slurpfile after "$TMP_ROOT/versioning-live-after.json" \
      --slurpfile state_contract "$TMP_ROOT/versioning-state-after.json" '{
        kind:$kind,
        schema_version:1,
        stage_id:$stage_id,
        guard_version:$guard_version,
        account_id:$account_id,
        region:$region,
        git_commit:$git_commit,
        guard_script_sha256:$guard_script_sha256,
        guard_jq_sha256:$guard_jq_sha256,
        migration_manifest_sha256:$migration_manifest_sha256,
        config_manifest_sha256:$config_manifest_sha256,
        deployment_lock_id:$deployment_lock_id,
        created_at_epoch:$created_at_epoch,
        versioning_observed_at_epoch:$versioning_observed_at_epoch,
        expires_at_epoch:$expires_at_epoch,
        buckets:{
          cloudtrail:{
            name:$cloudtrail_name,
            before:$before[0].log_buckets.cloudtrail,
            after:$after[0].log_buckets.cloudtrail
          },
          bedrock:{
            name:$bedrock_name,
            before:$before[0].log_buckets.bedrock,
            after:$after[0].log_buckets.bedrock
          }
        },
        producer_contract_sha256:$producer_contract_sha256,
        producer_evidence_sha256:$producer_evidence_sha256,
        live_after_sha256:$live_after_sha256,
        state_contract:$state_contract[0]
      }' > "$VERSIONING_STAGE_RECEIPT"
    chmod 600 "$VERSIONING_STAGE_RECEIPT"
    jq -e . "$VERSIONING_STAGE_RECEIPT" >/dev/null ||
      die "versioning receipt生成に失敗しました"
    VERSIONING_STAGE_IDENTITY="$(stat_identity "$VERSIONING_STAGE_RECEIPT")"
    ln "$VERSIONING_STAGE_RECEIPT" "$VERSIONING_OUT" ||
      die "versioning receipt出力pathを原子的に確保できません"
    [ "$(stat_identity "$VERSIONING_OUT")" = \
      "$VERSIONING_STAGE_IDENTITY" ] ||
      die "versioning receiptの原子的引渡しに失敗しました"
    chmod 600 "$VERSIONING_OUT"
    VERSIONING_PUBLISHED="true"
    release_deployment_lock
    echo "✅ log bucket versioning enabled and receipt published: $VERSIONING_OUT"
    echo "   900秒以上待機し、配信/export readiness証跡を作成してください"
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
    assert_trusted_automation_identity
    snapshot_live "$TMP_ROOT/live-before.json"
    migration_to_file "$MIGRATION_ID" "$TMP_ROOT/migration.json"
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

  plan)
    VAR_FILE=""
    PLAN=""
    RECEIPT=""
    RUNTIME_SYNC="false"
    MIGRATION_ID=""
    PREFLIGHT_RECEIPT=""
    ALARM_DELIVERY_RECEIPT=""
    VERSIONING_RECEIPT=""
    LOG_READINESS_RECEIPT=""
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
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$VAR_FILE" ] || die "plan には --var-file が必須です"
    [ -n "$PLAN" ] || die "plan には --out が必須です"
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
    if [ "$RUNTIME_SYNC" = "true" ] &&
       { [ -n "$PREFLIGHT_RECEIPT" ] ||
         [ -n "$ALARM_DELIVERY_RECEIPT" ] ||
         [ -n "$VERSIONING_RECEIPT" ] ||
         [ -n "$LOG_READINESS_RECEIPT" ]; }; then
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
    ALARM_DELIVERY_RECEIPT_SHA256=""
    ALARM_DELIVERY_RECEIPT_IDENTITY=""
    if [ -n "$ALARM_DELIVERY_RECEIPT" ]; then
      ALARM_DELIVERY_RECEIPT_SHA256="$(sha256_file "$ALARM_DELIVERY_RECEIPT")"
      ALARM_DELIVERY_RECEIPT_IDENTITY="$(stat_identity "$ALARM_DELIVERY_RECEIPT")"
    fi
    VERSIONING_RECEIPT_SHA256=""
    VERSIONING_RECEIPT_IDENTITY=""
    if [ -n "$VERSIONING_RECEIPT" ]; then
      VERSIONING_RECEIPT_SHA256="$(sha256_file "$VERSIONING_RECEIPT")"
      VERSIONING_RECEIPT_IDENTITY="$(stat_identity "$VERSIONING_RECEIPT")"
    fi
    LOG_READINESS_RECEIPT_SHA256=""
    LOG_READINESS_RECEIPT_IDENTITY=""
    if [ -n "$LOG_READINESS_RECEIPT" ]; then
      LOG_READINESS_RECEIPT_SHA256="$(sha256_file "$LOG_READINESS_RECEIPT")"
      LOG_READINESS_RECEIPT_IDENTITY="$(stat_identity "$LOG_READINESS_RECEIPT")"
    fi
    PLAN="$(secure_new_file "$PLAN")"
    RECEIPT="${RECEIPT:-${PLAN}.runtime-guard.json}"
    RECEIPT="$(secure_new_file "$RECEIPT")"
    [ "$(dirname "$PLAN")" = "$(dirname "$RECEIPT")" ] ||
      die "planとreceiptは同じprivate directoryへ出力してください"
    ensure_tmp
    STAGE="$(mktemp -d "$(dirname "$PLAN")/.teamagent-runtime-plan.XXXXXX")"
    chmod 700 "$STAGE"
    STAGE_PLAN="$STAGE/plan.tfplan"
    STAGE_RECEIPT="$STAGE/receipt.json"
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

    capture_state_contract "$TMP_ROOT/state-before.json"
    snapshot_live "$TMP_ROOT/live-before.json"
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
    LIVE_OPENCLAW_IMAGE="$(jq -er '.taskdefs.openclaw.image' "$TMP_ROOT/live-before.json")"
    LIVE_MCP_IMAGE="$(jq -er '.taskdefs.mcp.image' "$TMP_ROOT/live-before.json")"
    LIVE_X_IMAGE="$(jq -er '.taskdefs.x_buzz.image' "$TMP_ROOT/live-before.json")"
    LIVE_TIKTOK_IMAGE="$(jq -er '.taskdefs.tiktok.image' "$TMP_ROOT/live-before.json")"
    LIVE_RULE_STATES="$(jq -c '{
      ingest:.rules.ingest.critical.state,
      morning:.rules.morning.critical.state,
      canary:.rules.canary.critical.state
    }' "$TMP_ROOT/live-before.json")"

    MODE="sync"
    MIGRATION_KIND=""
    MIGRATION_JSON=""
    PREFLIGHT_SHA256=""
    DESIRED_OPENCLAW_IMAGE="$LIVE_OPENCLAW_IMAGE"
    DESIRED_MCP_IMAGE="$LIVE_MCP_IMAGE"
    DESIRED_X_IMAGE="$LIVE_X_IMAGE"
    DESIRED_TIKTOK_IMAGE="$LIVE_TIKTOK_IMAGE"
    DESIRED_INGEST_RULE="$(jq -r '.ingest == "ENABLED"' <<< "$LIVE_RULE_STATES")"
    DESIRED_MORNING_RULE="$(jq -r '.morning == "ENABLED"' <<< "$LIVE_RULE_STATES")"
    DESIRED_CANARY_RULE="$(jq -r '.canary == "ENABLED"' <<< "$LIVE_RULE_STATES")"
    TRANSITION_EPOCH="$(date +%s)"

    if [ -n "$MIGRATION_ID" ]; then
      MODE="migration"
      MIGRATION_JSON="$TMP_ROOT/migration.json"
      migration_to_file "$MIGRATION_ID" "$MIGRATION_JSON"
      validate_migration_source "$TMP_ROOT/live-before.json" "$MIGRATION_JSON"
      verify_preflight_receipt \
        "$PREFLIGHT_RECEIPT" "$MIGRATION_ID" "$MIGRATION_JSON" \
        "$TMP_ROOT/live-before.json"
      PREFLIGHT_SHA256="$(sha256_file "$PREFLIGHT_RECEIPT")"
      MIGRATION_KIND="$(jq -er '.kind' "$MIGRATION_JSON")"
      DESIRED_INGEST_RULE="$(jq -r '.to.rule_states.ingest == "ENABLED"' "$MIGRATION_JSON")"
      DESIRED_MORNING_RULE="$(jq -r '.to.rule_states.morning == "ENABLED"' "$MIGRATION_JSON")"
      DESIRED_CANARY_RULE="$(jq -r '.to.rule_states.canary == "ENABLED"' "$MIGRATION_JSON")"
      if [ "$MIGRATION_KIND" = "runtime" ]; then
        DESIRED_OPENCLAW_IMAGE="$(jq -er '.to.openclaw_image' "$MIGRATION_JSON")"
        DESIRED_MCP_IMAGE="$(jq -er '.to.mcp_image' "$MIGRATION_JSON")"
        DESIRED_X_IMAGE="$(jq -er '.to.x_buzz_image' "$MIGRATION_JSON")"
        DESIRED_TIKTOK_IMAGE="$(jq -er '.to.tiktok_image' "$MIGRATION_JSON")"
      elif [ "$MIGRATION_KIND" != "activation" ]; then
        die "未知のmigration kindです"
      fi
    fi

    core_from_snapshot \
      "$TMP_ROOT/live-before.json" "$TMP_ROOT/core.json" "$MODE" "$MIGRATION_ID" \
      "$DESIRED_OPENCLAW_IMAGE" "$DESIRED_MCP_IMAGE" "$DESIRED_X_IMAGE" \
      "$DESIRED_TIKTOK_IMAGE" "$PREFLIGHT_SHA256" "$TRANSITION_EPOCH" \
      "$DESIRED_INGEST_RULE" "$DESIRED_MORNING_RULE" "$DESIRED_CANARY_RULE"
    CORE_JSON="$(jq -c . "$TMP_ROOT/core.json")"
    TF_ARGS=(
      plan
      -input=false
      -refresh=true
      -lock-timeout=5m
      "-var-file=$STAGE_VAR"
      "-out=$STAGE_PLAN"
      "-var=runtime_guard_live=$CORE_JSON"
      "-var=openclaw_image=$DESIRED_OPENCLAW_IMAGE"
      "-var=mcp_image=$DESIRED_MCP_IMAGE"
      "-var=x_buzz_image=$DESIRED_X_IMAGE"
      "-var=tiktok_acquire_image=$DESIRED_TIKTOK_IMAGE"
      "-var=ingest_rule_enabled=$DESIRED_INGEST_RULE"
      "-var=morning_digest_rule_enabled=$DESIRED_MORNING_RULE"
      "-var=canary_rule_enabled=$DESIRED_CANARY_RULE"
      "-var=require_alarm_delivery=true"
    )
    terraform -chdir="$TF_DIR" "${TF_ARGS[@]}"
    chmod 600 "$STAGE_PLAN"
    PLAN_SHA="$(sha256_file "$STAGE_PLAN")"
    terraform -chdir="$TF_DIR" show -json "$STAGE_PLAN" > "$TMP_ROOT/plan.json"
    [ "$(sha256_file "$STAGE_PLAN")" = "$PLAN_SHA" ] || die "terraform show中のplan差替えを検出しました"
    hmac_from_plan "$TMP_ROOT/plan.json" "$TMP_ROOT/proposed-hmac.json"
    validate_hmac_transition_metadata \
      "$TMP_ROOT/live-before.json" "$TMP_ROOT/proposed-hmac.json" "$MODE" \
      "$TRANSITION_EPOCH" "$TMP_ROOT/hmac-transition.json"
    validate_hmac_secret_metadata "$TMP_ROOT/proposed-hmac.json"
    validate_plan \
      "$TMP_ROOT/plan.json" "$TMP_ROOT/live-before.json" "$TMP_ROOT/core.json" \
      "$DESIRED_MCP_IMAGE" "$MIGRATION_JSON" "$TMP_ROOT/proposed-hmac.json" \
      "$TMP_ROOT/state-before.json"
    [ "$(sha256_file "$STAGE_PLAN")" = "$PLAN_SHA" ] || die "plan検証中の差替えを検出しました"

    # plan 中に別デプロイが走った場合も fail-closed（TOCTOU 防止）。
    snapshot_live "$TMP_ROOT/live-after.json"
    capture_state_contract "$TMP_ROOT/state-after.json"
    BEFORE_SHA="$(sha256_file "$TMP_ROOT/live-before.json")"
    AFTER_SHA="$(sha256_file "$TMP_ROOT/live-after.json")"
    [ "$BEFORE_SHA" = "$AFTER_SHA" ] || die "plan 作成中に live runtime が変化しました。plan を再作成してください"
    [ "$(sha256_file "$TMP_ROOT/state-before.json")" = \
      "$(sha256_file "$TMP_ROOT/state-after.json")" ] ||
      die "plan作成中にbackend/workspace/state lineage/serial/address ownershipが変化しました"
    [ "$(sha256_file "$VAR_FILE")" = "$VAR_SHA" ] || die "plan作成中にvar-fileが変化しました"
    [ "$(stat_identity "$VAR_FILE")" = "$VAR_IDENTITY" ] || die "plan作成中にvar-file pathが差替えられました"
    [ "$(sha256_file "$STAGE_VAR")" = "$VAR_SHA" ] || die "private var-file copyが変化しました"
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
      --arg preflight_receipt_path "$PREFLIGHT_RECEIPT" \
      --arg preflight_receipt_sha256 "$PREFLIGHT_SHA256" \
      --arg alarm_delivery_receipt_path "$ALARM_DELIVERY_RECEIPT" \
      --arg alarm_delivery_receipt_sha256 "$ALARM_DELIVERY_RECEIPT_SHA256" \
      --arg versioning_receipt_path "$VERSIONING_RECEIPT" \
      --arg versioning_receipt_sha256 "$VERSIONING_RECEIPT_SHA256" \
      --arg log_readiness_receipt_path "$LOG_READINESS_RECEIPT" \
      --arg log_readiness_receipt_sha256 "$LOG_READINESS_RECEIPT_SHA256" \
      --argjson created_at_epoch "$NOW" \
      --argjson expires_at_epoch "$EXPIRES" \
      --arg git_commit "$(git_commit)" \
      --arg guard_script_sha256 "$(sha256_file "$SCRIPT_PATH")" \
      --arg guard_jq_sha256 "$(sha256_file "$GUARD_JQ")" \
      --arg migration_manifest_sha256 "$(sha256_file "$MIGRATION_FILE")" \
      --arg config_manifest_sha256 "$(sha256_file "$CONFIG_MANIFEST")" \
      --arg live_openclaw_image "$LIVE_OPENCLAW_IMAGE" \
      --arg live_mcp_image "$LIVE_MCP_IMAGE" \
      --arg live_x_image "$LIVE_X_IMAGE" \
      --arg live_tiktok_image "$LIVE_TIKTOK_IMAGE" \
      --arg desired_openclaw_image "$DESIRED_OPENCLAW_IMAGE" \
      --arg desired_mcp_image "$DESIRED_MCP_IMAGE" \
      --arg desired_x_image "$DESIRED_X_IMAGE" \
      --arg desired_tiktok_image "$DESIRED_TIKTOK_IMAGE" \
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
      --arg runtime_guard_sha256 "$CORE_SHA" \
      --slurpfile state_contract "$TMP_ROOT/state-after.json" \
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
        preflight_receipt_path:$preflight_receipt_path,
        preflight_receipt_sha256:$preflight_receipt_sha256,
        alarm_delivery_receipt_path:$alarm_delivery_receipt_path,
        alarm_delivery_receipt_sha256:$alarm_delivery_receipt_sha256,
        versioning_receipt_path:$versioning_receipt_path,
        versioning_receipt_sha256:$versioning_receipt_sha256,
        log_readiness_receipt_path:$log_readiness_receipt_path,
        log_readiness_receipt_sha256:$log_readiness_receipt_sha256,
        created_at_epoch:$created_at_epoch,
        expires_at_epoch:$expires_at_epoch,
        git_commit:$git_commit,
        guard_script_sha256:$guard_script_sha256,
        guard_jq_sha256:$guard_jq_sha256,
        migration_manifest_sha256:$migration_manifest_sha256,
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
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --plan) PLAN="${2:?--plan に値が必要}"; shift 2 ;;
        --receipt) RECEIPT="${2:?--receipt に値が必要}"; shift 2 ;;
        --out) APPLY_RECEIPT="${2:?--out に値が必要}"; shift 2 ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$PLAN" ] || die "applyには --plan が必須です"
    [ -n "$APPLY_RECEIPT" ] || die "applyには --out APPLY_RECEIPT が必須です"
    need_cmd aws
    need_cmd jq
    need_cmd terraform
    PLAN="$(secure_existing_file "$PLAN" 600)"
    secure_private_dir "$(dirname "$PLAN")" >/dev/null
    RECEIPT="${RECEIPT:-${PLAN}.runtime-guard.json}"
    RECEIPT="$(secure_existing_file "$RECEIPT" 600)"
    [ "$(dirname "$PLAN")" = "$(dirname "$RECEIPT")" ] ||
      die "planとreceiptは同じprivate directoryにある必要があります"
    APPLY_RECEIPT="$(secure_new_file "$APPLY_RECEIPT")"
    [ "$(dirname "$PLAN")" = "$(dirname "$APPLY_RECEIPT")" ] ||
      die "apply receiptもplanと同じprivate directoryに置いてください"
    ensure_tmp
    assert_trusted_automation_identity
    acquire_deployment_lock
    APPLY_RECEIPT_PUBLISHED="false"
    cleanup_apply_command() {
      local status=$?
      set +e
      release_deployment_lock
      if [ "$APPLY_RECEIPT_PUBLISHED" != "true" ]; then
        rm -f "$APPLY_RECEIPT"
      fi
      rm -rf "$TMP_ROOT"
      exit "$status"
    }
    trap 'cleanup_apply_command' EXIT

    # The deployment lock remains held across the final live/state/alarm
    # recheck and the saved-plan apply. verify_receipt stages immutable private
    # copies, so the exact bytes it verifies are the bytes Terraform consumes.
    verify_receipt "$PLAN" "$RECEIPT"
    terraform -chdir="$TF_DIR" apply \
      -input=false -lock-timeout=5m "$TMP_ROOT/verify/plan.tfplan"

    capture_state_contract "$TMP_ROOT/applied-state.json"
    snapshot_live "$TMP_ROOT/applied-live.json"
    APPLY_STAGE="$TMP_ROOT/apply-receipt.json"
    jq -n -S \
      --arg kind "terraform-runtime-apply-receipt" \
      --arg guard_version "$GUARD_VERSION" \
      --arg account_id "$EXPECTED_ACCOUNT_ID" \
      --arg region "$REGION" \
      --arg git_commit "$(git_commit)" \
      --arg deployment_lock_id "$DEPLOYMENT_LOCK_ID" \
      --arg source_receipt_sha256 "$(sha256_file "$RECEIPT")" \
      --arg plan_sha256 "$(sha256_file "$PLAN")" \
      --arg live_fingerprint_sha256 "$(sha256_file "$TMP_ROOT/applied-live.json")" \
      --argjson applied_at_epoch "$(date +%s)" \
      --slurpfile state_contract "$TMP_ROOT/applied-state.json" '{
        kind:$kind,
        guard_version:$guard_version,
        account_id:$account_id,
        region:$region,
        git_commit:$git_commit,
        deployment_lock_id:$deployment_lock_id,
        source_receipt_sha256:$source_receipt_sha256,
        plan_sha256:$plan_sha256,
        live_fingerprint_sha256:$live_fingerprint_sha256,
        applied_at_epoch:$applied_at_epoch,
        state_contract:$state_contract[0]
      }' > "$APPLY_STAGE"
    chmod 600 "$APPLY_STAGE"
    APPLY_STAGE_IDENTITY="$(stat_identity "$APPLY_STAGE")"
    ln "$APPLY_STAGE" "$APPLY_RECEIPT" ||
      die "apply receipt出力pathを原子的に確保できません"
    [ "$(stat_identity "$APPLY_RECEIPT")" = "$APPLY_STAGE_IDENTITY" ] ||
      die "apply receiptの原子的引渡しに失敗しました"
    chmod 600 "$APPLY_RECEIPT"
    APPLY_RECEIPT_PUBLISHED="true"
    release_deployment_lock
    echo "✅ guarded apply completed: $APPLY_RECEIPT"
    ;;

  *)
    die "不明な command: $COMMAND"
    ;;
esac
