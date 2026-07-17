#!/usr/bin/env bash
# Terraform が CLI 直デプロイ後の live ECS/EventBridge を巻き戻さないための
# TeamAgent dev専用 read-only plan validator。
#
# - snapshot: live の non-secret desired-state 値を HCL snippet として表示（read-only）
# - plan:     live 同期または明示digest rolloutの検証済みruntime planだけを保存する
# - verify:   plan/receipt/live が plan 作成時から不変か再確認（read-only）
#
# 共有deployment lockを全経路で取得できないためapply機能は意図的に持たない。
# 本validatorは事故防止であり、IAM境界・デプロイ承認・verify後の不変性を保証しない。
set -euo pipefail
umask 077

GUARD_VERSION="4"
EXPECTED_ACCOUNT_ID="718959508629"
REGION="ap-northeast-1"
PROJECT="teamagent"
ENVIRONMENT="dev"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
TF_DIR="$REPO_ROOT/infra/terraform"
GUARD_JQ_DIR="$REPO_ROOT/infra/deploy"
TMP_ROOT=""

usage() {
  cat <<'EOF'
usage:
  terraform_runtime_guard.sh snapshot
  terraform_runtime_guard.sh plan --var-file FILE --out PLAN \
    (--runtime-sync | --runtime-rollout-image ECR_URI@sha256:DIGEST) [--receipt FILE]
  terraform_runtime_guard.sh verify --plan PLAN [--receipt FILE]

plan:
  --runtime-sync           主要5 runtimeとTikTok/x-buzz worker/dispatcherを完全照合
  --runtime-rollout-image  同じaccount/regionのteamagent-mcp候補digestへ主要5+x-buzz taskdefを検証
  --receipt FILE           receipt 出力先（default: PLAN.runtime-guard.json）

重要:
  - TeamAgent dev / account 718959508629 / ap-northeast-1 / 固定S3 backend専用。
  - snapshot/verifyはAWS read-only。planはrefreshとTerraform state lockのみ行う。
  - apply機能はない。共有deployment lockがないためverify後の安全な適用は保証しない。
  - 出力directoryは0700、var-fileは0600相当、出力plan/receiptは未存在が必須。
  - tfvars や receipt に secret 値は書かない。
EOF
}

die() {
  echo "★ $*" >&2
  exit 1
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

aws_cli() {
  AWS_PAGER="" aws --region "$REGION" "$@"
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
  local ingest_rule="${PROJECT}-${ENVIRONMENT}-ingest-weekly"
  local morning_rule="${PROJECT}-${ENVIRONMENT}-morning-digest-weekday"
  local canary_rule="${PROJECT}-${ENVIRONMENT}-canary-hourly"
  local tiktok_function="${PROJECT}-${ENVIRONMENT}-tiktok-acquire-dispatch"
  local x_function="${PROJECT}-${ENVIRONMENT}-x-buzz-dispatch"

  aws_cli sts get-caller-identity --output json > "$dir/identity.json"
  local account_id
  account_id="$(jq -er '.Account | select(type == "string")' "$dir/identity.json")" ||
    die "AWS accountを確認できません"
  [ "$account_id" = "$EXPECTED_ACCOUNT_ID" ] ||
    die "想定外のAWS accountです: $account_id"

  aws_cli ecs describe-services --cluster "$cluster" \
    --services "$mcp_service" "$connect_service" --include TAGS \
    --output json > "$dir/services.json"

  local service_failures
  service_failures="$(jq -r '.failures | length' "$dir/services.json")"
  [ "$service_failures" = "0" ] || die "ECS service の取得に失敗しました"

  local mcp_arn connect_arn
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
    --slurpfile services "$dir/services.json" \
    --slurpfile mcp "$dir/mcp.json" \
    --slurpfile connect "$dir/connect.json" \
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
    --slurpfile canary_target "$dir/canary-targets.json" '
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
        taskdefs: {
          mcp: task($mcp; "teamagent-mcp"),
          connect_web: task($connect; "connect-web"),
          ingest: task($ingest; "ingest"),
          morning: task($morning; "morning-digest"),
          canary: task($canary; "canary"),
          tiktok: task($tiktok; "acquire"),
          x_buzz: task($x; "worker")
        },
        services: {
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

  local common_image
  common_image="$(jq -er '.taskdefs.mcp.image' "$output")"
  local expected_repo digest
  expected_repo="${account_id}.dkr.ecr.${REGION}.amazonaws.com/teamagent-mcp"
  [[ "$common_image" == "$expected_repo@sha256:"* ]] ||
    die "live image は同一account/regionのteamagent-mcp digestである必要があります: $common_image"
  digest="${common_image#*@}"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "live mcp image が完全digest pinではありません: $common_image"
  jq -e '[.taskdefs[] | .expected_container_name as $name |
    [.critical.containers[] | select(.name == $name)] | length] | all(. == 1)' "$output" >/dev/null \
    || die "runtime task definitionの期待container名が一意ではありません"
  jq -e '[.taskdefs[] | (.env_count == (.env | length)) and (.secret_count == (.secrets | length))] | all' "$output" >/dev/null \
    || die "live task definition に重複env/secret名があります"
  jq -e --arg image "$common_image" '
    [.taskdefs.mcp.image, .taskdefs.connect_web.image, .taskdefs.ingest.image,
     .taskdefs.morning.image, .taskdefs.canary.image] | all(. == $image)
  ' "$output" >/dev/null || die "主要5 task definition の live image が一致していません"

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
  jq -e '
    .dispatchers.tiktok.task_definition == .taskdefs.tiktok.arn and
    .dispatchers.x_buzz.task_definition == .taskdefs.x_buzz.arn and
    .event_mappings.tiktok.critical.enabled == true and
    .event_mappings.x_buzz.critical.enabled == true and
    .event_mappings.tiktok.critical.function_arn == .dispatchers.tiktok.critical.function_arn and
    .event_mappings.x_buzz.critical.function_arn == .dispatchers.x_buzz.critical.function_arn
  ' "$output" >/dev/null || die "worker dispatcher/taskdef/event mappingのlive契約が不整合です"
}

# rollout候補はliveと同じprivate ECR repositoryの別digestだけを許可し、実在をread-only確認する。
validate_rollout_image() {
  local snapshot="$1"
  local candidate="$2"
  local account_id live_image expected_repo digest result
  account_id="$(jq -er '.account_id' "$snapshot")"
  live_image="$(jq -er '.taskdefs.mcp.image' "$snapshot")"
  expected_repo="${account_id}.dkr.ecr.${REGION}.amazonaws.com/teamagent-mcp"
  [[ "$candidate" == "$expected_repo@sha256:"* ]] ||
    die "rollout imageは $expected_repo の完全digest pinだけを許可します"
  digest="${candidate#*@}"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "rollout imageのdigestが不正です: $candidate"
  [ "$candidate" != "$live_image" ] || die "rollout候補がliveと同じです。state同期には --runtime-sync を使用してください"

  ensure_tmp
  result="$TMP_ROOT/ecr-describe-${RANDOM}.json"
  aws_cli ecr describe-images \
    --repository-name teamagent-mcp \
    --image-ids "imageDigest=$digest" \
    --output json > "$result" || die "rollout候補digestがECRに存在しません: $digest"
  jq -e --arg digest "$digest" '
    (.imageDetails | length) == 1 and .imageDetails[0].imageDigest == $digest
  ' "$result" >/dev/null || die "rollout候補digestをECRで一意に確認できません: $digest"
}

# Terraform precondition へ渡す、live 由来の non-secret object。
core_from_snapshot() {
  local snapshot="$1"
  local output="$2"
  local mode="$3"
  local desired_image="$4"
  jq -S -c --arg mode "$mode" --arg desired_image "$desired_image" '
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
      live_mcp_image: $s.taskdefs.mcp.image,
      desired_mcp_image: $desired_image,
      live_x_image: $s.taskdefs.x_buzz.image,
      desired_x_image: (if $mode == "sync" then $s.taskdefs.x_buzz.image else $desired_image end),
      live_tiktok_image: $s.taskdefs.tiktok.image,
      desired_tiktok_image: $s.taskdefs.tiktok.image,
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
      ingest_rule_enabled: ($s.rules.ingest.critical.state == "ENABLED"),
      morning_digest_rule_enabled: ($s.rules.morning.critical.state == "ENABLED"),
      canary_rule_enabled: ($s.rules.canary.critical.state == "ENABLED"),
      tiktok_dispatch_static_environment: $s.dispatchers.tiktok.static_environment,
      x_dispatch_static_environment: $s.dispatchers.x_buzz.static_environment,
      tiktok_dispatch_code_sha256: $s.dispatchers.tiktok.code_sha256,
      x_dispatch_code_sha256: $s.dispatchers.x_buzz.code_sha256
    }
  ' "$snapshot" > "$output" || die "live runtimeのboolean/env契約が不正です"

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
      line("mcp_image"; .live_mcp_image),
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

runtime_targets() {
  cat <<'EOF'
data.aws_iam_policy_document.x_buzz_exec_secrets
data.aws_iam_policy_document.x_buzz_task_app
data.aws_iam_policy_document.x_dispatch_policy
data.aws_iam_policy_document.x_mcp_policy
data.aws_iam_policy_document.tiktok_task_app
data.aws_iam_policy_document.tiktok_dispatch_policy
data.aws_iam_policy_document.tiktok_mcp_policy
aws_ecs_task_definition.mcp
aws_ecs_service.mcp
aws_ecs_task_definition.connect_web[0]
aws_ecs_service.connect_web[0]
aws_ecs_task_definition.ingest[0]
aws_cloudwatch_event_rule.ingest_weekly[0]
aws_cloudwatch_event_target.ingest_run_task[0]
aws_ecs_task_definition.morning_digest[0]
aws_cloudwatch_event_rule.morning_digest_weekday[0]
aws_cloudwatch_event_target.morning_digest_run_task[0]
aws_ecs_task_definition.canary[0]
aws_cloudwatch_event_rule.canary_hourly[0]
aws_cloudwatch_event_target.canary_run_task[0]
aws_ecs_task_definition.tiktok_acquire[0]
aws_lambda_function.tiktok_dispatch[0]
aws_lambda_event_source_mapping.tiktok_dispatch[0]
aws_ecs_task_definition.x_buzz_worker[0]
aws_lambda_function.x_dispatch[0]
aws_lambda_event_source_mapping.x_dispatch[0]
EOF
}

plan_has_address() {
  local plan_json="$1"
  local address="$2"
  jq -e --arg address "$address" '.resource_changes[]? | select(.address == $address)' "$plan_json" >/dev/null
}

validate_plan() {
  local plan_json="$1"
  local snapshot="$2"
  local core="$3"
  local desired_image="$4"

  jq -e --arg desired_image "$desired_image" --slurpfile expected_core "$core" '
    type == "object" and
    .format_version == "1.2" and
    (.terraform_version | type == "string") and
    .applyable == true and .errored == false and
    (.complete | type == "boolean") and
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
    .variables.runtime_guard_live.value == $expected_core[0]
  ' "$plan_json" >/dev/null ||
    die "plan JSONのschema/action/check/runtime_guard束縛が不正です"

  local allowed_replacements
  allowed_replacements='["aws_ecs_task_definition.mcp","aws_ecs_task_definition.connect_web[0]","aws_ecs_task_definition.ingest[0]","aws_ecs_task_definition.morning_digest[0]","aws_ecs_task_definition.canary[0]","aws_ecs_task_definition.tiktok_acquire[0]","aws_ecs_task_definition.x_buzz_worker[0]"]'
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
      "aws_ecs_task_definition.mcp",
      "aws_ecs_task_definition.connect_web[0]",
      "aws_ecs_task_definition.ingest[0]",
      "aws_ecs_task_definition.morning_digest[0]",
      "aws_ecs_task_definition.canary[0]",
      "aws_ecs_service.mcp[0]",
      "aws_ecs_service.connect_web[0]",
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
    'aws_ecs_task_definition.mcp|mcp|teamagent-mcp|mcp' \
    'aws_ecs_task_definition.connect_web[0]|connect_web|connect-web|mcp' \
    'aws_ecs_task_definition.ingest[0]|ingest|ingest|mcp' \
    'aws_ecs_task_definition.morning_digest[0]|morning|morning-digest|mcp' \
    'aws_ecs_task_definition.canary[0]|canary|canary|mcp' \
    'aws_ecs_task_definition.tiktok_acquire[0]|tiktok|acquire|tiktok' \
    'aws_ecs_task_definition.x_buzz_worker[0]|x_buzz|worker|x'; do
    IFS='|' read -r address component expected_name image_kind <<< "$spec"
    case "$image_kind" in
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
    'aws_ecs_service.connect_web[0]|connect_web|aws_ecs_service.connect_web|aws_ecs_task_definition.connect_web[0]'; do
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
    'aws_lambda_function.tiktok_dispatch[0]|tiktok|aws_lambda_function.tiktok_dispatch|aws_ecs_task_definition.tiktok_acquire[0]|tiktok_dispatch_static_environment' \
    'aws_lambda_function.x_dispatch[0]|x_buzz|aws_lambda_function.x_dispatch|aws_ecs_task_definition.x_buzz_worker[0]|x_dispatch_static_environment'; do
    local config_address task_address static_environment_key
    IFS='|' read -r address component config_address task_address static_environment_key <<< "$spec"
    if plan_has_address "$plan_json" "$address"; then
      jq -L "$GUARD_JQ_DIR" -e \
        --arg address "$address" --arg component "$component" \
        --arg config_address "$config_address" --arg task_address "$task_address" \
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
              .signing_profile_version_arn, .environment);
        . as $plan |
        $plan.resource_changes[] | select(.address == $address) as $change |
        ([$plan.configuration.root_module.resources[] |
          select(.address == $config_address) |
          .. | objects | .references? // empty | .[]] |
          index($task_address + ".arn")) as $reference |
        ([$change.change.after_unknown // {} | paths(. == true)]) as $unknown |
        ($change.change.before | lambda_environment) as $before_environment |
        ($change.change.after | lambda_environment) as $after_environment |
        ($live[0].dispatchers[$component].critical.environment) as $live_environment |
        ($core[0][$static_environment_key]) as $static_environment |
        ($live[0].taskdefs[$component].arn | sub(":[0-9]+$"; "")) as $task_prefix |
        (($change.change.before | guard_lambda_from_tf) ==
          $live[0].dispatchers[$component].critical) and
        ($before_environment == $live_environment) and
        ($reference != null) and
        if $change.change.actions == ["no-op"] then
          ($change.change.before == $change.change.after) and ($unknown | length == 0)
        elif $change.change.actions == ["update"] then
          (($change.change.before | strip_provider_computed) ==
            ($change.change.after | strip_provider_computed)) and
          ($unknown | all(. as $path |
            ($path == ["environment", 0, "variables"]) or
            ($path == ["environment", 0, "variables", "TASKDEF_ARN"]) or
            ((["arn", "id", "invoke_arn", "qualified_arn", "qualified_invoke_arn",
               "last_modified", "source_code_size", "version", "signing_job_arn",
               "signing_profile_version_arn"] | index($path[0])) != null))) and
          if ($unknown | any(. == ["environment", 0, "variables"])) then true
          else
            (($after_environment | del(.TASKDEF_ARN)) == $static_environment) and
            (if ($unknown | any(. == ["environment", 0, "variables", "TASKDEF_ARN"]))
             then true
             else (($after_environment.TASKDEF_ARN | type) == "string") and
                  ($after_environment.TASKDEF_ARN | startswith($task_prefix + ":")) and
                  ($after_environment.TASKDEF_ARN | split(":")[-1] | test("^[0-9]+$"))
             end)
          end
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
}

verify_receipt() {
  local plan="$1"
  local receipt="$2"
  ensure_tmp

  local stage="$TMP_ROOT/verify"
  mkdir -m 700 "$stage"
  local receipt_sha_before receipt_sha_after receipt_identity
  receipt_identity="$(stat_identity "$receipt")"
  receipt_sha_before="$(sha256_file "$receipt")"
  cp "$receipt" "$stage/receipt.json"
  chmod 600 "$stage/receipt.json"
  receipt_sha_after="$(sha256_file "$receipt")"
  [ "$receipt_sha_before" = "$receipt_sha_after" ] || die "receipt読取中の差替えを検出しました"
  [ "$receipt_identity" = "$(stat_identity "$receipt")" ] ||
    die "receipt読取中のpath差替えを検出しました"

  jq -e --arg version "$GUARD_VERSION" --arg account "$EXPECTED_ACCOUNT_ID" \
    --arg region "$REGION" --arg project "$PROJECT" --arg environment "$ENVIRONMENT" '
    (keys | sort) == (["account_id","desired_image","environment","guard_version",
      "live_fingerprint_sha256","live_image","mode","plan_path","plan_sha256",
      "project","receipt_path","region","runtime_guard_sha256","var_file",
      "var_file_sha256"] | sort) and
    .guard_version == $version and .account_id == $account and
    .region == $region and .project == $project and .environment == $environment and
    (.mode == "sync" or .mode == "rollout") and
    (.plan_sha256 | test("^[0-9a-f]{64}$")) and
    (.var_file_sha256 | test("^[0-9a-f]{64}$")) and
    (.live_fingerprint_sha256 | test("^[0-9a-f]{64}$")) and
    (.runtime_guard_sha256 | test("^[0-9a-f]{64}$"))
  ' "$stage/receipt.json" >/dev/null || die "receipt schema/bindingが不正です"

  local bound_plan bound_receipt var_file
  bound_plan="$(jq -er '.plan_path' "$stage/receipt.json")"
  bound_receipt="$(jq -er '.receipt_path' "$stage/receipt.json")"
  var_file="$(jq -er '.var_file' "$stage/receipt.json")"
  [ "$bound_plan" = "$plan" ] || die "receiptが別plan pathに束縛されています"
  [ "$bound_receipt" = "$receipt" ] || die "receipt path束縛が一致しません"
  var_file="$(secure_existing_file "$var_file")"
  [ "$var_file" = "$(jq -er '.var_file' "$stage/receipt.json")" ] || die "var-file path束縛が一致しません"

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

  local mode live_image desired_image expected_live_sha
  mode="$(jq -er '.mode' "$stage/receipt.json")"
  live_image="$(jq -er '.live_image' "$stage/receipt.json")"
  desired_image="$(jq -er '.desired_image' "$stage/receipt.json")"
  expected_live_sha="$(jq -er '.live_fingerprint_sha256' "$stage/receipt.json")"
  if [ "$mode" = "sync" ]; then
    [ "$desired_image" = "$live_image" ] || die "sync receiptのdesired/live imageが不一致です"
  else
    [ "$desired_image" != "$live_image" ] || die "rollout receiptのdesired/live imageが同一です"
  fi

  snapshot_live "$stage/live-before.json"
  [ "$(sha256_file "$stage/live-before.json")" = "$expected_live_sha" ] ||
    die "plan作成後にlive runtimeが変化しました"
  [ "$(jq -er '.taskdefs.mcp.image' "$stage/live-before.json")" = "$live_image" ] ||
    die "receiptのlive imageが現在の実機と一致しません"
  if [ "$mode" = "rollout" ]; then
    validate_rollout_image "$stage/live-before.json" "$desired_image"
  fi
  core_from_snapshot "$stage/live-before.json" "$stage/core.json" "$mode" "$desired_image"
  [ "$(sha256_file "$stage/core.json")" = "$(jq -er '.runtime_guard_sha256' "$stage/receipt.json")" ] ||
    die "runtime_guard_live束縛がreceiptと一致しません"

  terraform -chdir="$TF_DIR" show -json "$stage/plan.tfplan" > "$stage/plan.json"
  [ "$(sha256_file "$stage/plan.tfplan")" = "$plan_sha_before" ] || die "plan検証中のprivate copy改ざんを検出しました"
  validate_plan "$stage/plan.json" "$stage/live-before.json" "$stage/core.json" "$desired_image"
  [ "$(sha256_file "$stage/plan.tfplan")" = "$plan_sha_before" ] || die "plan検証後のprivate copy改ざんを検出しました"

  snapshot_live "$stage/live-after.json"
  [ "$(sha256_file "$stage/live-after.json")" = "$expected_live_sha" ] ||
    die "verify中にlive runtimeが変化しました"
  [ "$(sha256_file "$plan")" = "$plan_sha_before" ] || die "verify中にplan pathが変化しました"
  [ "$(sha256_file "$receipt")" = "$receipt_sha_before" ] || die "verify中にreceipt pathが変化しました"
  [ "$(sha256_file "$var_file")" = "$var_sha_before" ] || die "verify中にvar-fileが変化しました"
  [ "$plan_identity" = "$(stat_identity "$plan")" ] || die "verify中にplan pathが差替えられました"
  [ "$receipt_identity" = "$(stat_identity "$receipt")" ] || die "verify中にreceipt pathが差替えられました"
  [ "$var_identity" = "$(stat_identity "$var_file")" ] || die "verify中にvar-file pathが差替えられました"
}

COMMAND="${1:-}"
case "$COMMAND" in
  -h|--help|help|"") usage; exit 0 ;;
esac
shift

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
    LIVE_IMAGE="$(jq -er '.taskdefs.mcp.image' "$TMP_ROOT/live.json")"
    core_from_snapshot "$TMP_ROOT/live.json" "$TMP_ROOT/core.json" "sync" "$LIVE_IMAGE"
    print_hcl_snapshot "$TMP_ROOT/core.json"
    ;;

  plan)
    VAR_FILE=""
    PLAN=""
    RECEIPT=""
    RUNTIME_SYNC="false"
    ROLLOUT_IMAGE=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -h|--help) usage; exit 0 ;;
        --var-file) VAR_FILE="${2:?--var-file に値が必要}"; shift 2 ;;
        --out) PLAN="${2:?--out に値が必要}"; shift 2 ;;
        --receipt) RECEIPT="${2:?--receipt に値が必要}"; shift 2 ;;
        --runtime-sync) RUNTIME_SYNC="true"; shift ;;
        --runtime-rollout-image) ROLLOUT_IMAGE="${2:?--runtime-rollout-image に値が必要}"; shift 2 ;;
        *) die "不明な引数: $1" ;;
      esac
    done
    [ -n "$VAR_FILE" ] || die "plan には --var-file が必須です"
    [ -n "$PLAN" ] || die "plan には --out が必須です"
    if [ "$RUNTIME_SYNC" = "true" ] && [ -n "$ROLLOUT_IMAGE" ]; then
      die "--runtime-sync と --runtime-rollout-image は併用できません"
    fi
    if [ -n "$ROLLOUT_IMAGE" ] && [[ ! "$ROLLOUT_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]]; then
      die "--runtime-rollout-image は完全digest pinで指定してください"
    fi
    [ "$RUNTIME_SYNC" = "true" ] || [ -n "$ROLLOUT_IMAGE" ] ||
      die "--runtime-sync または --runtime-rollout-image が必須です"

    need_cmd aws
    need_cmd jq
    need_cmd terraform
    VAR_FILE="$(secure_existing_file "$VAR_FILE")"
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

    snapshot_live "$TMP_ROOT/live-before.json"
    LIVE_IMAGE="$(jq -er '.taskdefs.mcp.image' "$TMP_ROOT/live-before.json")"
    MODE="sync"
    DESIRED_IMAGE="$LIVE_IMAGE"
    if [ -n "$ROLLOUT_IMAGE" ]; then
      MODE="rollout"
      DESIRED_IMAGE="$ROLLOUT_IMAGE"
      validate_rollout_image "$TMP_ROOT/live-before.json" "$DESIRED_IMAGE"
    fi
    core_from_snapshot "$TMP_ROOT/live-before.json" "$TMP_ROOT/core.json" "$MODE" "$DESIRED_IMAGE"
    CORE_JSON="$(jq -c . "$TMP_ROOT/core.json")"
    TF_ARGS=(plan -input=false -refresh=true -lock-timeout=5m "-var-file=$STAGE_VAR" "-out=$STAGE_PLAN" "-var=runtime_guard_live=$CORE_JSON")
    if [ "$MODE" = "rollout" ]; then
      TF_ARGS+=("-var=mcp_image=$DESIRED_IMAGE")
    fi
    while IFS= read -r address; do
      [[ "$address" =~ ^[A-Za-z0-9_.-]+(\[[0-9]+\])?$ ]] || die "不正な target address: $address"
      TF_ARGS+=("-target=$address")
    done < <(runtime_targets)

    terraform -chdir="$TF_DIR" "${TF_ARGS[@]}"
    chmod 600 "$STAGE_PLAN"
    PLAN_SHA="$(sha256_file "$STAGE_PLAN")"
    terraform -chdir="$TF_DIR" show -json "$STAGE_PLAN" > "$TMP_ROOT/plan.json"
    [ "$(sha256_file "$STAGE_PLAN")" = "$PLAN_SHA" ] || die "terraform show中のplan差替えを検出しました"
    validate_plan "$TMP_ROOT/plan.json" "$TMP_ROOT/live-before.json" "$TMP_ROOT/core.json" "$DESIRED_IMAGE"
    [ "$(sha256_file "$STAGE_PLAN")" = "$PLAN_SHA" ] || die "plan検証中の差替えを検出しました"

    # plan 中に別デプロイが走った場合も fail-closed（TOCTOU 防止）。
    snapshot_live "$TMP_ROOT/live-after.json"
    BEFORE_SHA="$(sha256_file "$TMP_ROOT/live-before.json")"
    AFTER_SHA="$(sha256_file "$TMP_ROOT/live-after.json")"
    [ "$BEFORE_SHA" = "$AFTER_SHA" ] || die "plan 作成中に live runtime が変化しました。plan を再作成してください"
    [ "$(sha256_file "$VAR_FILE")" = "$VAR_SHA" ] || die "plan作成中にvar-fileが変化しました"
    [ "$(stat_identity "$VAR_FILE")" = "$VAR_IDENTITY" ] || die "plan作成中にvar-file pathが差替えられました"
    [ "$(sha256_file "$STAGE_VAR")" = "$VAR_SHA" ] || die "private var-file copyが変化しました"

    ACCOUNT_ID="$(jq -er '.account_id' "$TMP_ROOT/live-after.json")"
    CORE_SHA="$(sha256_file "$TMP_ROOT/core.json")"
    jq -n \
      --arg guard_version "$GUARD_VERSION" \
      --arg account_id "$ACCOUNT_ID" \
      --arg region "$REGION" \
      --arg project "$PROJECT" \
      --arg environment "$ENVIRONMENT" \
      --arg mode "$MODE" \
      --arg live_image "$LIVE_IMAGE" \
      --arg desired_image "$DESIRED_IMAGE" \
      --arg plan_path "$PLAN" \
      --arg receipt_path "$RECEIPT" \
      --arg var_file "$VAR_FILE" \
      --arg var_file_sha256 "$VAR_SHA" \
      --arg plan_sha256 "$PLAN_SHA" \
      --arg live_fingerprint_sha256 "$AFTER_SHA" \
      --arg runtime_guard_sha256 "$CORE_SHA" \
      '{guard_version:$guard_version,account_id:$account_id,region:$region,project:$project,
        environment:$environment,mode:$mode,live_image:$live_image,desired_image:$desired_image,
        plan_path:$plan_path,receipt_path:$receipt_path,var_file:$var_file,
        var_file_sha256:$var_file_sha256,plan_sha256:$plan_sha256,
        live_fingerprint_sha256:$live_fingerprint_sha256,
        runtime_guard_sha256:$runtime_guard_sha256}' > "$STAGE_RECEIPT"
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
    echo "   mode/image: $MODE / $DESIRED_IMAGE"
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

  *)
    die "不明な command: $COMMAND"
    ;;
esac
