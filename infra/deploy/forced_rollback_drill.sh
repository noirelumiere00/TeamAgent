#!/usr/bin/env bash
# Guarded two-leg forced-rollback drill controller.
set -euo pipefail
umask 077

SCHEMA_VERSION="1"
ACCOUNT_ID="718959508629"
REGION="ap-northeast-1"
ENVIRONMENT="dev"
MAX_START_DELAY_SECONDS=1800
MAX_OLD_DWELL_SECONDS=1200
EVIDENCE_BUCKET="teamagent-dev-openclaw-rollout-evidence"
EVIDENCE_PREFIX="forced-rollback-drills"
EVIDENCE_ENCRYPTION_KEY_ALIAS="alias/teamagent-dev-openclaw-rollout-evidence"
DRILL_SIGNING_KEY_ALIAS="alias/teamagent-dev-forced-rollback-drill-signing"
DRILL_SIGNING_ALGORITHM="RSASSA_PSS_SHA_256"
EVIDENCE_MIN_RETENTION_DAYS=3650
TRUSTED_AUTOMATION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
AUTHORIZE_IMAGE_RELEASE="$SCRIPT_DIR/authorize_image_release.sh"
TERRAFORM_RUNTIME_GUARD="$SCRIPT_DIR/terraform_runtime_guard.sh"
CONSUMER_REGISTRY="$REPO_ROOT/infra/codebuild/image_deployment_consumers.json"
AGGREGATE_VALIDATOR="$REPO_ROOT/infra/codebuild/forced_rollback_drill_evidence.py"
AGGREGATE_BUILDER="$REPO_ROOT/infra/codebuild/forced_rollback_drill_aggregate_builder.py"
ARTIFACT_STORE="$REPO_ROOT/infra/codebuild/forced_rollback_drill_artifact_store.py"

DRILL_DIR=""
STATE_FILE=""
LOCK_DIR=""
LOCK_HELD="false"

die() {
  echo "FATAL: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
usage:
  forced_rollback_drill.sh prepare \
    --contract DRILL.json --initial-apply-receipt FILE \
    --var-file FILE.tfvars.json --out-dir DIR
  forced_rollback_drill.sh preflight --drill-dir DIR --targets old,new
  forced_rollback_drill.sh plan-leg --drill-dir DIR \
    --leg rollback-to-previous|restore-active
  forced_rollback_drill.sh apply-leg --drill-dir DIR \
    --leg rollback-to-previous|restore-active
  forced_rollback_drill.sh finalize --drill-dir DIR
EOF
}

value() {
  [ "$#" -ge 2 ] && [ -n "${2-}" ] || die "$1 requires a value"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

new_uuid_v4() {
  need_cmd python3
  python3 -c 'import uuid; print(uuid.uuid4())'
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    die "sha256sum or shasum is required"
  fi
}

assert_sha256() {
  [[ "$1" =~ ^[0-9a-f]{64}$ ]] || die "$2 is not a lowercase SHA-256"
}

assert_regular_file() {
  [ ! -L "$1" ] || die "symlink input is forbidden: $1"
  [ -f "$1" ] || die "regular input file does not exist: $1"
}

validate_json_file() {
  assert_regular_file "$1"
  python3 - "$1" <<'PY' >/dev/null
import json
import sys


def object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


with open(sys.argv[1], encoding="utf-8") as handle:
    json.load(
        handle,
        object_pairs_hook=object_without_duplicates,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON number: {value}")
        ),
    )
PY
}

canonical_copy() {
  validate_json_file "$1"
  jq -S -c . "$1" > "$2"
  chmod 600 "$2"
}

release_lock() {
  if [ "$LOCK_HELD" = "true" ]; then
    rmdir "$LOCK_DIR" >/dev/null 2>&1 || true
    LOCK_HELD="false"
  fi
}

acquire_lock() {
  LOCK_DIR="$DRILL_DIR/.controller.lock"
  mkdir "$LOCK_DIR" 2>/dev/null ||
    die "drill is locked or an interrupted command requires reconciliation"
  chmod 700 "$LOCK_DIR"
  LOCK_HELD="true"
  trap 'release_lock' EXIT
}

canonical_drill_dir() {
  local requested="$1" canonical
  [ ! -L "$requested" ] || die "drill directory symlinks are forbidden"
  [ -d "$requested" ] || die "drill directory does not exist"
  canonical="$(cd -- "$requested" && pwd -P)"
  [ -n "$canonical" ] || die "could not resolve drill directory"
  printf '%s\n' "$canonical"
}

target_file() {
  printf '%s/targets/%s.json\n' "$DRILL_DIR" "$1"
}

manifest_file() {
  printf '%s/inputs/%s.consumer-manifest.json\n' "$DRILL_DIR" "$1"
}

preflight_file() {
  printf '%s/preflight/%s.json\n' "$DRILL_DIR" "$1"
}

leg_dir() {
  case "$1" in
    rollback-to-previous) printf '%s/legs/rollback-to-previous\n' "$DRILL_DIR" ;;
    restore-active) printf '%s/legs/restore-active\n' "$DRILL_DIR" ;;
    *) die "unsupported leg: $1" ;;
  esac
}

leg_state_key() {
  case "$1" in
    rollback-to-previous) printf 'rollback_to_previous\n' ;;
    restore-active) printf 'restore_active\n' ;;
    *) die "unsupported leg: $1" ;;
  esac
}

leg_target() {
  case "$1" in
    rollback-to-previous) printf 'old\n' ;;
    restore-active) printf 'new\n' ;;
    *) die "unsupported leg: $1" ;;
  esac
}

leg_channel() {
  case "$1" in
    rollback-to-previous) printf 'rollback\n' ;;
    restore-active) printf 'active\n' ;;
    *) die "unsupported leg: $1" ;;
  esac
}

leg_approval_id() {
  case "$1" in
    rollback-to-previous) printf 'OK-1\n' ;;
    restore-active) printf 'OK-2\n' ;;
    *) die "unsupported leg: $1" ;;
  esac
}

leg_approval_action() {
  case "$1" in
    rollback-to-previous) printf 'rollback\n' ;;
    restore-active) printf 'restore\n' ;;
    *) die "unsupported leg: $1" ;;
  esac
}

expected_state_for_plan() {
  case "$1" in
    rollback-to-previous) printf 'PREFLIGHTED\n' ;;
    restore-active) printf 'LEG1_APPLIED\n' ;;
    *) die "unsupported leg: $1" ;;
  esac
}

expected_state_for_apply() {
  case "$1" in
    rollback-to-previous) printf 'LEG1_PLANNED\n' ;;
    restore-active) printf 'LEG2_PLANNED\n' ;;
    *) die "unsupported leg: $1" ;;
  esac
}

validate_target_file() {
  local path="$1"
  validate_json_file "$path"
  jq -e --slurpfile registry "$CONSUMER_REGISTRY" '
    def exact_keys($expected):
      (keys | sort) == ($expected | sort);
    def digest_from_image:
      capture("@(?<digest>sha256:[0-9a-f]{64})$").digest;
    def repository_from_image:
      capture(
        "^[0-9]+[.]dkr[.]ecr[.][a-z0-9-]+[.]amazonaws[.]com/"
        + "(?<repository>[a-z0-9._/-]+)@sha256:[0-9a-f]{64}$"
      ).repository;
    def expected_image($target; $id):
      if $id == "tiktok_acquire" then $target.images.tiktok
      elif $id == "x_buzz_worker" then $target.images.x_buzz
      else $target.images.mcp
      end;
    . as $target |
    exact_keys([
      "approval",
      "candidate",
      "consumer_manifest",
      "images",
      "preflight_migration_id",
      "resources",
      "runtime_migration_id",
      "subjects"
    ]) and
    (.images | exact_keys(["mcp", "openclaw", "tiktok", "x_buzz"])) and
    ([.images[]] | all(
      type == "string" and
      test(
        "^718959508629[.]dkr[.]ecr[.]ap-northeast-1[.]amazonaws[.]com/"
        + "[a-z0-9._/-]+@sha256:[0-9a-f]{64}$"
      )
    )) and
    .images.x_buzz == .images.mcp and
    (.subjects | type == "array" and length > 0) and
    ([.subjects[] | keys | sort] |
      all(. == (["digest", "name", "pipeline", "release_repository"] | sort))) and
    ([.subjects[].pipeline] | all(. == "mcp")) and
    ([.subjects[].name] | all(type == "string" and length > 0)) and
    ([.subjects[].release_repository] |
      all(type == "string" and length > 0)) and
    ([.subjects[].digest] |
      all(type == "string" and test("^sha256:[0-9a-f]{64}$"))) and
    (.subjects == (.subjects | sort_by(.pipeline, .name))) and
    ([.subjects[] | [.pipeline, .name]] | unique | length) ==
      (.subjects | length) and
    (.resources | type == "array" and length > 0) and
    (.resources == (.resources | sort_by(.consumer_id))) and
    ([.resources[].consumer_id] | unique | length) == (.resources | length) and
    ([.resources[].consumer_id] -
      ($registry[0].consumers | map(.consumer_id)) | length) == 0 and
    ([.resources[].consumer_id] | sort) ==
      ([
        .subjects[] as $subject |
        $registry[0].consumers[] |
        select(
          .receipt.pipeline == $subject.pipeline and
          .receipt.subject == $subject.name
        ) |
        .consumer_id
      ] | sort) and
    all(.resources[];
      exact_keys([
        "activation",
        "consumer_id",
        "image",
        "pipeline",
        "subject",
        "terraform_address",
        "task_definition_arn"
      ]) and
      (.activation | exact_keys(["identity", "state", "type"])) and
      (.task_definition_arn | type == "string" and test(
        "^arn:aws:ecs:ap-northeast-1:718959508629:task-definition/"
        + "[A-Za-z0-9_-]+:[1-9][0-9]*$"
      )) and
      .image == expected_image($target; .consumer_id) and
      (. as $resource |
        ($registry[0].consumers[] |
          select(.consumer_id == $resource.consumer_id)) as $owner |
        $owner.receipt.pipeline == "mcp" and
        $resource.pipeline == $owner.receipt.pipeline and
        $resource.subject == $owner.receipt.subject and
        $resource.terraform_address ==
          $owner.terraform_task_definition_address and
        ($resource.task_definition_arn |
          startswith(
            "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/"
            + $owner.ecs_family + ":"
          )) and
        $resource.activation.type == $owner.activator.type and
        $resource.activation.identity == $owner.activator.identity and
        (
          if $owner.activator.type == "ecs_service" then
            ($resource.activation.state |
              type == "number" and . >= 0 and floor == .)
          elif ($owner.activator.type == "eventbridge_rule_ecs_target" or
            $owner.activator.type ==
              "eventbridge_rule_lambda_taskdef_arn_environment") then
            ($resource.activation.state |
              . == "ENABLED" or . == "DISABLED")
          elif $owner.activator.type ==
            "lambda_taskdef_arn_environment" then
            ($resource.activation.state | type == "boolean")
          else
            false
          end
        ) and
        ($target.subjects[] |
          select(
            .pipeline == $resource.pipeline and
            .name == $resource.subject
          )) as $subject |
        $subject.release_repository == $owner.release_repository and
        $subject.digest == ($resource.image | digest_from_image) and
        $subject.release_repository ==
          ($resource.image | repository_from_image))
    ) and
    ([.subjects[] | [.pipeline, .name]] | sort) ==
      ([.resources[] | [.pipeline, .subject]] | unique | sort) and
    ([.subjects[].digest] | sort | unique) ==
      ([.resources[].image | digest_from_image] | sort | unique) and
    (.preflight_migration_id |
      type == "string" and test("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")) and
    (.runtime_migration_id |
      type == "string" and test("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")) and
    (.candidate | exact_keys([
      "receipt_key",
      "receipt_signature_version_id",
      "receipt_version_id"
    ])) and
    (.candidate.receipt_key | type == "string" and length > 0) and
    (.candidate.receipt_version_id | type == "string" and length > 0) and
    (.candidate.receipt_signature_version_id |
      type == "string" and length > 0) and
    (.approval | exact_keys([
      "payload_bucket",
      "payload_key",
      "payload_sha256",
      "payload_version_id",
      "signature_bucket",
      "signature_key",
      "signature_sha256",
      "signature_version_id"
    ])) and
    (.approval.payload_sha256 | test("^[0-9a-f]{64}$")) and
    (.approval.signature_sha256 | test("^[0-9a-f]{64}$")) and
    ([.approval[]] | all(type == "string" and length > 0)) and
    (.consumer_manifest |
      exact_keys(["path", "sha256"]) and
      (.path | type == "string" and length > 0) and
      (.sha256 | test("^[0-9a-f]{64}$")))
  ' "$path" >/dev/null || die "target contract is invalid: $path"
}

validate_target_pair() {
  local old_path="$1" new_path="$2"
  jq -e --slurpfile new "$new_path" '
    . as $old |
    $new[0] as $new |
    def subject_identity:
      {pipeline, name, release_repository};
    def resource_identity:
      {
        consumer_id,
        terraform_address,
        pipeline,
        subject,
        activation_type:.activation.type,
        activation_identity:.activation.identity
      };
    def task_family:
      sub(":[1-9][0-9]*$"; "");
    ([ $old.subjects[] | subject_identity ]) ==
      ([ $new.subjects[] | subject_identity ]) and
    ([ $old.resources[] | resource_identity ]) ==
      ([ $new.resources[] | resource_identity ]) and
    all($old.subjects[];
      . as $old_subject |
      ($new.subjects[] |
        select(
          .pipeline == $old_subject.pipeline and
          .name == $old_subject.name
        )) as $new_subject |
      $old_subject.digest != $new_subject.digest
    ) and
    all($old.resources[];
      . as $old_resource |
      ($new.resources[] |
        select(.consumer_id == $old_resource.consumer_id)) as $new_resource |
      ($old_resource.task_definition_arn | task_family) ==
        ($new_resource.task_definition_arn | task_family) and
      ($old_resource.task_definition_arn |
        capture(":(?<revision>[1-9][0-9]*)$").revision |
        tonumber) <
      ($new_resource.task_definition_arn |
        capture(":(?<revision>[1-9][0-9]*)$").revision |
        tonumber)
    ) and
    $old.images.openclaw == $new.images.openclaw and
    (
      any($old.subjects[]; .name == "core") or
      (
        $old.images.mcp == $new.images.mcp and
        $old.images.x_buzz == $new.images.x_buzz
      )
    ) and
    (
      any($old.subjects[]; .name == "media") or
      $old.images.tiktok == $new.images.tiktok
    ) and
    $old.preflight_migration_id != $new.preflight_migration_id and
    $old.runtime_migration_id != $new.runtime_migration_id and
    [
      $old.candidate.receipt_key,
      $old.candidate.receipt_version_id,
      $old.candidate.receipt_signature_version_id
    ] != [
      $new.candidate.receipt_key,
      $new.candidate.receipt_version_id,
      $new.candidate.receipt_signature_version_id
    ]
  ' "$old_path" >/dev/null ||
    die "old/new targets are not one exact changed-consumer scope"
}

validate_contract_file() {
  local contract="$1"
  validate_json_file "$contract"
  validate_json_file "$CONSUMER_REGISTRY"
  jq -e \
    --arg account "$ACCOUNT_ID" \
    --arg region "$REGION" \
    --arg environment "$ENVIRONMENT" \
    --argjson schema "$SCHEMA_VERSION" \
    --argjson start_limit "$MAX_START_DELAY_SECONDS" \
    --argjson dwell_limit "$MAX_OLD_DWELL_SECONDS" '
    def exact_keys($expected):
      (keys | sort) == ($expected | sort);
    exact_keys([
      "actors",
      "blocked_reason",
      "control",
      "drill_id",
      "environment",
      "evidence",
      "guard_receipts",
      "kind",
      "limits",
      "pipeline",
      "ready",
      "schema_version",
      "targets"
    ]) and
    .schema_version == $schema and
    .kind == "teamagent.forced-rollback-drill-contract" and
    .ready == true and
    .blocked_reason == "" and
    (.drill_id | type == "string" and test(
      "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )) and
    .environment == {
      account_id:$account,
      name:$environment,
      region:$region
    } and
    .limits == {
      max_old_dwell_seconds:$dwell_limit,
      max_start_delay_seconds:$start_limit
    } and
    .pipeline == "mcp" and
    (.control | exact_keys([
      "git_commit",
      "initial_release_apply_locator"
    ])) and
    (.control.git_commit | test("^[0-9a-f]{40}$")) and
    (.control.initial_release_apply_locator | type == "object") and
    (.actors | exact_keys([
      "automation_principals",
      "initiating_principal"
    ])) and
    (.actors.initiating_principal |
      exact_keys(["arn", "source_identity", "user_id"])) and
    ([.actors.initiating_principal[]] | all(type == "string")) and
    (.actors.automation_principals |
      type == "array" and all(.[]; type == "string" and length > 0)) and
    (.targets | exact_keys(["new", "old"])) and
    (.guard_receipts | exact_keys([
      "alarm_delivery",
      "alarm_migration",
      "log_readiness",
      "media_cutover",
      "versioning"
    ])) and
    all(
      .guard_receipts.alarm_delivery,
      .guard_receipts.alarm_migration,
      .guard_receipts.log_readiness,
      .guard_receipts.versioning;
      exact_keys(["path", "sha256"]) and
      (.path | type == "string" and length > 0) and
      (.sha256 | test("^[0-9a-f]{64}$"))
    ) and
    .guard_receipts.media_cutover == null and
    (.evidence | exact_keys(["artifact_manifest", "integrity"])) and
    (.evidence.artifact_manifest | type == "array") and
    (.evidence.integrity |
      exact_keys(["immutable_object", "kms_key_arn", "signature"])) and
    (.evidence.integrity.kms_key_arn | type == "string") and
    (.evidence.integrity.signature | type == "object") and
    (.evidence.integrity.immutable_object | type == "object")
  ' "$contract" >/dev/null || die "drill contract is invalid or not ready"
}

validate_bound_file() {
  local path="$1" expected="$2" label="$3"
  assert_regular_file "$path"
  assert_sha256 "$expected" "$label SHA-256"
  [ "$(sha256_file "$path")" = "$expected" ] ||
    die "$label SHA-256 does not match the drill contract"
}

validate_initial_release_locator_binding() {
  local receipt="$1" contract="$2" expected_sha expected_size actual_size
  if jq -e '
    .control.initial_release_apply_locator |
    (has("sha256") or has("size"))
  ' "$contract" >/dev/null; then
    jq -e '
      .control.initial_release_apply_locator |
      has("sha256") and has("size") and
      (.sha256 | test("^[0-9a-f]{64}$")) and
      (.size | type == "number" and . >= 1 and floor == .)
    ' "$contract" >/dev/null ||
      die "initial release apply locator has an incomplete byte binding"
    expected_sha="$(
      jq -er '.control.initial_release_apply_locator.sha256' "$contract"
    )"
    expected_size="$(
      jq -er '.control.initial_release_apply_locator.size' "$contract"
    )"
    actual_size="$(wc -c < "$receipt" | tr -d '[:space:]')"
    [ "$(sha256_file "$receipt")" = "$expected_sha" ] &&
      [ "$actual_size" = "$expected_size" ] ||
      die "initial release apply locator does not bind the exact receipt bytes"
  fi
}

validate_state_file() {
  local state="$1"
  if ! validate_json_file "$state"; then
    die "state.json is corrupt or has an unexpected state"
  fi
  jq -e \
    --argjson schema "$SCHEMA_VERSION" '
    def exact_keys($expected):
      (keys | sort) == ($expected | sort);
    def sha:
      type == "string" and test("^[0-9a-f]{64}$");
    def receipt_or_null:
      . == null or (
        type == "object" and
        exact_keys(["migration_id", "path", "sha256"]) and
        (.path | type == "string" and length > 0) and
        (.sha256 | sha) and
        (.migration_id | type == "string" and length > 0)
      );
    def authorization_or_null:
      . == null or (
        type == "object" and
        exact_keys(["path", "sha256"]) and
        (.path | type == "string" and length > 0) and
        (.sha256 | sha)
      );
    def plan_or_null:
      . == null or (
        type == "object" and
        exact_keys([
          "baseline_task_revisions_sha256",
          "created_at_epoch",
          "intent_id",
          "path",
          "receipt_path",
          "receipt_sha256",
          "sha256",
          "state_serial",
          "terraform_lineage"
        ]) and
        (.path | type == "string" and length > 0) and
        (.receipt_path | type == "string" and length > 0) and
        (.sha256 | sha) and
        (.receipt_sha256 | sha) and
        (.baseline_task_revisions_sha256 | sha) and
        (.intent_id | test(
          "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )) and
        (.terraform_lineage | type == "string" and length > 0) and
        (.state_serial | type == "number" and . >= 0 and floor == .) and
        (.created_at_epoch | type == "number" and . >= 0 and floor == .)
      );
    def approval_or_null:
      . == null or (
        type == "object" and
        exact_keys([
          "action",
          "approval_id",
          "approval_text_sha256",
          "consumed_at_epoch",
          "drill_id",
          "plan_sha256"
        ]) and
        (.action | . == "rollback" or . == "restore") and
        (.approval_id | . == "OK-1" or . == "OK-2") and
        (.drill_id | test(
          "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )) and
        (.plan_sha256 | sha) and
        (.approval_text_sha256 | sha) and
        (.consumed_at_epoch | type == "number" and . >= 0 and floor == .)
      );
    def apply_or_null:
      . == null or (
        . as $apply |
        type == "object" and
        exact_keys([
          "applied_at_epoch",
          "apply_attempt_id",
          "automation_identity_path",
          "automation_identity_sha256",
          "completed_at_epoch",
          "path",
          "plan_sha256",
          "post_serial",
          "post_target_sha256",
          "sha256",
          "started_at_epoch",
          "terraform_lineage"
        ]) and
        (.path | type == "string" and length > 0) and
        (.sha256 | sha) and
        (.automation_identity_path | type == "string" and length > 0) and
        (.automation_identity_sha256 | sha) and
        (.plan_sha256 | sha) and
        (.post_target_sha256 | sha) and
        (.apply_attempt_id | test(
          "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )) and
        (.terraform_lineage | type == "string" and length > 0) and
        (.post_serial | type == "number" and . >= 0 and floor == .) and
        (.started_at_epoch | type == "number" and . >= 0 and floor == .) and
        (.applied_at_epoch |
          type == "number" and . >= $apply.started_at_epoch and floor == .) and
        (.completed_at_epoch |
          type == "number" and
          . >= $apply.applied_at_epoch and
          floor == .)
      );
    def leg($name; $ordinal; $channel; $target; $approval):
      exact_keys([
        "apply",
        "approval",
        "approval_id",
        "authorization",
        "channel",
        "name",
        "ordinal",
        "plan",
        "status",
        "target"
      ]) and
      .name == $name and
      .ordinal == $ordinal and
      .channel == $channel and
      .target == $target and
      .approval_id == $approval and
      (.status |
        IN("PENDING", "AUTHORIZING", "PLANNED", "APPLYING", "APPLIED", "FAILED")) and
      (.authorization | authorization_or_null) and
      (.plan | plan_or_null) and
      (.approval | approval_or_null) and
      (.apply | apply_or_null) and
      (
        .approval == null or
        (
          .approval.approval_id == $approval and
          .approval.action ==
            (if $channel == "rollback" then "rollback" else "restore" end) and
          .plan != null and
          .approval.plan_sha256 == .plan.sha256
        )
      ) and
      (
        .apply == null or
        (
          .plan != null and
          .apply.plan_sha256 == .plan.sha256
        )
      );
    def pending_leg:
      .status == "PENDING" and
      .authorization == null and
      .plan == null and
      .approval == null and
      .apply == null;
    def planned_leg:
      .status == "PLANNED" and
      .authorization != null and
      .plan != null and
      .approval == null and
      .apply == null;
    def applied_leg:
      .status == "APPLIED" and
      .authorization != null and
      .plan != null and
      .approval != null and
      .apply != null;
    exact_keys([
      "aggregate_sha256",
      "contract_sha256",
      "created_at_epoch",
      "drill_id",
      "failures",
      "final_status",
      "git_commit",
      "initial_apply_receipt_sha256",
      "initial_release_deadline_epoch",
      "initial_release_verified_at_epoch",
      "initial_state",
      "kind",
      "legs",
      "old_dwell",
      "preflight",
      "schema_version",
      "state",
      "target_sha256",
      "updated_at_epoch",
      "var_file_sha256"
    ]) and
    .schema_version == $schema and
    .kind == "teamagent.forced-rollback-drill-state" and
    (.drill_id | type == "string" and test(
      "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )) and
    (.git_commit | test("^[0-9a-f]{40}$")) and
    (.state | IN(
      "PREPARED",
      "PREFLIGHTED",
      "LEG1_PLANNED",
      "LEG1_APPLIED",
      "LEG2_PLANNED",
      "LEG2_APPLIED",
      "FINALIZED",
      "RECOVERY_REQUIRED"
    )) and
    (.contract_sha256 | sha) and
    (.initial_apply_receipt_sha256 | sha) and
    (.var_file_sha256 | sha) and
    (.created_at_epoch | type == "number" and . >= 0 and floor == .) and
    (. as $state |
      (.updated_at_epoch |
        type == "number" and . >= $state.created_at_epoch and floor == .)) and
    (.initial_release_verified_at_epoch |
      type == "number" and . >= 0 and floor == .) and
    .initial_release_deadline_epoch ==
      (.initial_release_verified_at_epoch + 1800) and
    (.initial_state | exact_keys([
      "address_set_sha256",
      "lineage",
      "serial"
    ])) and
    (.initial_state.lineage | type == "string" and length > 0) and
    (.initial_state.serial | type == "number" and . >= 0 and floor == .) and
    (.initial_state.address_set_sha256 | sha) and
    (.target_sha256 |
      exact_keys(["new", "old"]) and
      (.new | sha) and
      (.old | sha) and
      .new != .old) and
    (.preflight |
      exact_keys(["new", "old"]) and
      (.new | receipt_or_null) and
      (.old | receipt_or_null)) and
    (.legs | exact_keys(["restore_active", "rollback_to_previous"])) and
    (.legs.rollback_to_previous |
      leg("rollback-to-previous"; 1; "rollback"; "old"; "OK-1")) and
    (.legs.restore_active |
      leg("restore-active"; 2; "active"; "new"; "OK-2")) and
    (.old_dwell | exact_keys([
      "deadline_epoch",
      "exceeded_at_epoch",
      "started_at_epoch"
    ])) and
    (
      (
        .old_dwell.started_at_epoch == null and
        .old_dwell.deadline_epoch == null
      ) or
      (
        (.old_dwell.started_at_epoch |
          type == "number" and . >= 0 and floor == .) and
        .old_dwell.deadline_epoch ==
          (.old_dwell.started_at_epoch + 1200)
      )
    ) and
    (
      .old_dwell.exceeded_at_epoch == null or
      (
        (.old_dwell.exceeded_at_epoch |
          type == "number" and floor == .) and
        .old_dwell.deadline_epoch != null and
        .old_dwell.exceeded_at_epoch > .old_dwell.deadline_epoch
      )
    ) and
    (.failures | type == "array") and
    all(.failures[];
      exact_keys(["at_epoch", "leg", "phase", "reason"]) and
      (.at_epoch | type == "number" and floor == .) and
      (.leg | type == "string" and length > 0) and
      (.phase | type == "string" and length > 0) and
      (.reason | type == "string" and length > 0)
    ) and
    (.final_status == null or
      (.final_status | IN("PASSED", "FAILED", "RECONCILE_REQUIRED"))) and
    (.aggregate_sha256 == null or (.aggregate_sha256 | sha)) and
    (
      if .state == "PREPARED" then
        .preflight.old == null and .preflight.new == null and
        (.legs.rollback_to_previous | pending_leg) and
        (.legs.restore_active | pending_leg) and
        (.failures | length) == 0
      elif .state == "PREFLIGHTED" then
        .preflight.old != null and .preflight.new != null and
        (.legs.rollback_to_previous | pending_leg) and
        (.legs.restore_active | pending_leg) and
        (.failures | length) == 0
      elif .state == "LEG1_PLANNED" then
        (.legs.rollback_to_previous | planned_leg) and
        (.legs.restore_active | pending_leg) and
        (.failures | length) == 0
      elif .state == "LEG1_APPLIED" then
        (.legs.rollback_to_previous | applied_leg) and
        (.legs.restore_active | pending_leg) and
        .old_dwell.started_at_epoch != null and
        (.failures | length) == 0
      elif .state == "LEG2_PLANNED" then
        (.legs.rollback_to_previous | applied_leg) and
        (.legs.restore_active | planned_leg) and
        (.failures | length) == 0
      elif .state == "LEG2_APPLIED" then
        (.legs.rollback_to_previous | applied_leg) and
        (.legs.restore_active | applied_leg)
      elif .state == "FINALIZED" then
        .final_status != null and .aggregate_sha256 != null
      else
        true
      end
    )
  ' "$state" >/dev/null || die "state.json is corrupt or has an unexpected state"
}

load_drill() {
  local target state_key leg_name leg_path authorization_path plan_path
  local plan_receipt_path apply_path
  DRILL_DIR="$(canonical_drill_dir "$1")"
  STATE_FILE="$DRILL_DIR/state.json"
  acquire_lock
  [ ! -L "$STATE_FILE" ] || die "state.json symlinks are forbidden"
  [ -f "$STATE_FILE" ] || die "state.json is missing"
  validate_state_file "$STATE_FILE"
  validate_bound_file \
    "$DRILL_DIR/contract.json" \
    "$(jq -er '.contract_sha256' "$STATE_FILE")" \
    "drill contract"
  validate_bound_file \
    "$DRILL_DIR/inputs/initial-release.apply.json" \
    "$(jq -er '.initial_apply_receipt_sha256' "$STATE_FILE")" \
    "initial apply receipt"
  validate_initial_release_locator_binding \
    "$DRILL_DIR/inputs/initial-release.apply.json" \
    "$DRILL_DIR/contract.json"
  validate_bound_file \
    "$DRILL_DIR/inputs/terraform.tfvars.json" \
    "$(jq -er '.var_file_sha256' "$STATE_FILE")" \
    "base var-file"
  [ "$(jq -er '.drill_id' "$DRILL_DIR/contract.json")" = \
    "$(jq -er '.drill_id' "$STATE_FILE")" ] ||
    die "drill ID differs between contract and state"
  [ "$(jq -er '.control.git_commit' "$DRILL_DIR/contract.json")" = \
    "$(jq -er '.git_commit' "$STATE_FILE")" ] ||
    die "git commit differs between contract and state"
  validate_target_file "$(target_file old)"
  validate_target_file "$(target_file new)"
  jq -e --slurpfile old "$(target_file old)" \
    --slurpfile new "$(target_file new)" '
    .targets.old == $old[0] and
    .targets.new == $new[0]
  ' "$DRILL_DIR/contract.json" >/dev/null ||
    die "copied targets differ from the SHA-bound drill contract"
  [ "$(sha256_file "$(target_file old)")" = \
    "$(jq -er '.target_sha256.old' "$STATE_FILE")" ] ||
    die "old target changed after prepare"
  [ "$(sha256_file "$(target_file new)")" = \
    "$(jq -er '.target_sha256.new' "$STATE_FILE")" ] ||
    die "new target changed after prepare"
  for target in old new; do
    validate_bound_file \
      "$(manifest_file "$target")" \
      "$(jq -er '.consumer_manifest.sha256' "$(target_file "$target")")" \
      "$target consumer manifest"
    if jq -e ".preflight.$target != null" "$STATE_FILE" >/dev/null; then
      [ "$(jq -er ".preflight.$target.path" "$STATE_FILE")" = \
        "$(preflight_file "$target")" ] ||
        die "$target preflight path differs from state.json"
      [ "$(jq -er ".preflight.$target.migration_id" "$STATE_FILE")" = \
        "$(jq -er '.preflight_migration_id' "$(target_file "$target")")" ] ||
        die "$target preflight migration differs from the target"
      validate_bound_file \
        "$(preflight_file "$target")" \
        "$(jq -er ".preflight.$target.sha256" "$STATE_FILE")" \
        "$target preflight receipt"
    fi
  done
  for state_key in rollback_to_previous restore_active; do
    if [ "$state_key" = "rollback_to_previous" ]; then
      leg_name="rollback-to-previous"
    else
      leg_name="restore-active"
    fi
    leg_path="$(leg_dir "$leg_name")"
    if jq -e ".legs.$state_key.authorization != null" \
      "$STATE_FILE" >/dev/null; then
      authorization_path="$leg_path/authorization.json"
      [ "$(jq -er ".legs.$state_key.authorization.path" "$STATE_FILE")" = \
        "$authorization_path" ] ||
        die "$leg_name authorization path differs from state.json"
      validate_bound_file \
        "$authorization_path" \
        "$(jq -er ".legs.$state_key.authorization.sha256" "$STATE_FILE")" \
        "$leg_name authorization"
      validate_bound_file \
        "$leg_path/release-authorization.tfvars.json" \
        "$(jq -er '.gate_var_sha256' "$authorization_path")" \
        "$leg_name authorization gate var-file"
    fi
    if jq -e ".legs.$state_key.plan != null" "$STATE_FILE" >/dev/null; then
      plan_path="$leg_path/plan.tfplan"
      plan_receipt_path="$leg_path/plan.runtime-guard.json"
      [ "$(jq -er ".legs.$state_key.plan.path" "$STATE_FILE")" = \
        "$plan_path" ] ||
        die "$leg_name plan path differs from state.json"
      [ "$(jq -er ".legs.$state_key.plan.receipt_path" "$STATE_FILE")" = \
        "$plan_receipt_path" ] ||
        die "$leg_name plan receipt path differs from state.json"
      validate_bound_file \
        "$plan_path" \
        "$(jq -er ".legs.$state_key.plan.sha256" "$STATE_FILE")" \
        "$leg_name plan"
      validate_bound_file \
        "$plan_receipt_path" \
        "$(jq -er ".legs.$state_key.plan.receipt_sha256" "$STATE_FILE")" \
        "$leg_name plan receipt"
      validate_bound_file \
        "$leg_path/terraform.tfvars.json" \
        "$(jq -er '.var_file_sha256' "$plan_receipt_path")" \
        "$leg_name merged var-file"
    fi
    if jq -e ".legs.$state_key.apply != null" "$STATE_FILE" >/dev/null; then
      apply_path="$leg_path/apply.runtime-guard.json"
      [ "$(jq -er ".legs.$state_key.apply.path" "$STATE_FILE")" = \
        "$apply_path" ] ||
        die "$leg_name apply receipt path differs from state.json"
      validate_bound_file \
        "$apply_path" \
        "$(jq -er ".legs.$state_key.apply.sha256" "$STATE_FILE")" \
        "$leg_name apply receipt"
    fi
  done
  verify_copied_guard_receipts
}

replace_state() {
  local temporary
  temporary="$(mktemp "$DRILL_DIR/.state.XXXXXXXX")"
  if ! jq -S -c "$@" "$STATE_FILE" > "$temporary"; then
    rm -f "$temporary"
    die "could not update drill state"
  fi
  chmod 600 "$temporary"
  validate_state_file "$temporary"
  mv -f "$temporary" "$STATE_FILE"
}

require_state() {
  local expected="$1" actual
  actual="$(jq -er '.state' "$STATE_FILE")"
  [ "$actual" = "$expected" ] ||
    die "invalid state transition: expected $expected, found $actual"
}

record_failure() {
  local leg="$1" phase="$2" reason="$3" now
  now="$(date +%s)"
  replace_state \
    --arg leg "$leg" \
    --arg phase "$phase" \
    --arg reason "$reason" \
    --argjson now "$now" '
    .state = "RECOVERY_REQUIRED" |
    .updated_at_epoch = $now |
    .failures += [{
      at_epoch:$now,
      leg:$leg,
      phase:$phase,
      reason:$reason
    }] |
    if $leg == "rollback-to-previous" then
      .legs.rollback_to_previous.status = "FAILED"
    elif $leg == "restore-active" then
      .legs.restore_active.status = "FAILED"
    else
      .
    end
  '
}

check_old_dwell() {
  local now deadline
  deadline="$(jq -er '.old_dwell.deadline_epoch' "$STATE_FILE")"
  [ "$deadline" != "null" ] || die "old dwell timer was not started"
  now="$(date +%s)"
  if [ "$now" -gt "$deadline" ]; then
    replace_state \
      --argjson now "$now" '
      .state = "RECOVERY_REQUIRED" |
      .updated_at_epoch = $now |
      .old_dwell.exceeded_at_epoch = $now |
      .failures += [{
        at_epoch:$now,
        leg:"restore-active",
        phase:"old-dwell",
        reason:"maximum old dwell exceeded"
      }] |
      .legs.restore_active.status = "FAILED"
    '
    return 1
  fi
}

verify_copied_guard_receipts() {
  local name
  for name in alarm-delivery alarm-migration log-readiness versioning; do
    validate_bound_file \
      "$DRILL_DIR/inputs/$name.json" \
      "$(jq -er ".guard_receipts[\"${name//-/_}\"].sha256" \
        "$DRILL_DIR/contract.json")" \
      "$name receipt"
  done
}

prepare_command() {
  local contract="" initial_apply="" var_file="" out_dir=""
  local now verified deadline contract_sha initial_sha var_sha
  local old_target new_target old_sha new_sha
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --contract)
        value "$@"
        [ -z "$contract" ] || die "--contract may be specified only once"
        contract="$2"
        shift 2
        ;;
      --initial-apply-receipt)
        value "$@"
        [ -z "$initial_apply" ] ||
          die "--initial-apply-receipt may be specified only once"
        initial_apply="$2"
        shift 2
        ;;
      --var-file)
        value "$@"
        [ -z "$var_file" ] || die "--var-file may be specified only once"
        var_file="$2"
        shift 2
        ;;
      --out-dir)
        value "$@"
        [ -z "$out_dir" ] || die "--out-dir may be specified only once"
        out_dir="$2"
        shift 2
        ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
  done
  [ -n "$contract" ] && [ -n "$initial_apply" ] &&
    [ -n "$var_file" ] && [ -n "$out_dir" ] ||
    die "prepare requires --contract, --initial-apply-receipt, --var-file, and --out-dir"
  [ ! -e "$out_dir" ] && [ ! -L "$out_dir" ] ||
    die "output drill directory already exists; an interrupted drill cannot be overwritten"
  [ -d "$(dirname -- "$out_dir")" ] ||
    die "output drill parent directory does not exist"
  case "$var_file" in
    *.json) ;;
    *) die "--var-file must be a JSON var-file so fresh authorization can be merged exactly" ;;
  esac

  validate_contract_file "$contract"
  validate_json_file "$initial_apply"
  validate_json_file "$var_file"
  validate_initial_release_locator_binding "$initial_apply" "$contract"
  jq -e 'type == "object"' "$var_file" >/dev/null ||
    die "base var-file must contain one JSON object"

  old_target="$(mktemp "${TMPDIR:-/tmp}/forced-drill-old.XXXXXXXX")"
  new_target="$(mktemp "${TMPDIR:-/tmp}/forced-drill-new.XXXXXXXX")"
  trap 'rm -f "$old_target" "$new_target"' EXIT
  jq -S -c '.targets.old' "$contract" > "$old_target"
  jq -S -c '.targets.new' "$contract" > "$new_target"
  validate_target_file "$old_target"
  validate_target_file "$new_target"
  validate_target_pair "$old_target" "$new_target"
  old_sha="$(sha256_file "$old_target")"
  new_sha="$(sha256_file "$new_target")"
  [ "$old_sha" != "$new_sha" ] || die "old and new targets must be distinct"

  jq -e \
    --slurpfile previous "$old_target" \
    --slurpfile expected "$new_target" '
    .kind == "terraform-runtime-apply-receipt" and
    .status == "applied" and
    (.applied_at_epoch | type) == "number" and
    .applied_at_epoch >= 0 and
    (.post_state_contract.state.lineage |
      type == "string" and length > 0) and
    (.post_state_contract.state.serial |
      type == "number" and . >= 0 and floor == .) and
    (.post_state_contract.state.address_set_sha256 |
      test("^[0-9a-f]{64}$")) and
    .post_live_contract.images == $expected[0].images and
    .post_live_contract.resources == $expected[0].resources and
    .post_state_contract.task_revisions == (
      $expected[0].resources |
      map({
        key:.consumer_id,
        value:(.task_definition_arn |
          capture(":(?<revision>[1-9][0-9]*)$").revision |
          tonumber)
      }) |
      from_entries
    ) and
    .pre_live_contract.images == $previous[0].images and
    .pre_live_contract.resources == $previous[0].resources and
    .pre_state_contract.state.lineage ==
      .post_state_contract.state.lineage and
    .pre_state_contract.state.address_set_sha256 ==
      .post_state_contract.state.address_set_sha256 and
    (.pre_state_contract.state.serial |
      type == "number" and . >= 0 and floor == .) and
    .pre_state_contract.state.serial <
      .post_state_contract.state.serial and
    .pre_state_contract.task_revisions == (
      $previous[0].resources |
      map({
        key:.consumer_id,
        value:(.task_definition_arn |
          capture(":(?<revision>[1-9][0-9]*)$").revision |
          tonumber)
      }) |
      from_entries
    )
  ' "$initial_apply" >/dev/null ||
    die "initial apply receipt does not bind exact previous-old and initial-new targets"

  verified="$(
    jq -er '
      .initial_release_verified_at_epoch //
      .verified_at_epoch //
      .applied_at_epoch
    ' "$initial_apply"
  )"
  [[ "$verified" =~ ^[0-9]+$ ]] ||
    die "initial release verified epoch is invalid"
  deadline=$((verified + MAX_START_DELAY_SECONDS))
  now="$(date +%s)"
  [ "$verified" -le "$now" ] ||
    die "initial release verified epoch cannot be in the future"
  [ "$now" -le "$deadline" ] ||
    die "initial release verification is older than the 30 minute drill start limit"

  for target in old new; do
    target_path="$old_target"
    [ "$target" = "old" ] || target_path="$new_target"
    manifest_path="$(jq -er '.consumer_manifest.path' "$target_path")"
    manifest_sha="$(jq -er '.consumer_manifest.sha256' "$target_path")"
    validate_bound_file "$manifest_path" "$manifest_sha" "$target consumer manifest"
    validate_json_file "$manifest_path"
  done
  for receipt_key in alarm_delivery alarm_migration log_readiness versioning; do
    receipt_path="$(jq -er ".guard_receipts.$receipt_key.path" "$contract")"
    receipt_sha="$(jq -er ".guard_receipts.$receipt_key.sha256" "$contract")"
    validate_bound_file "$receipt_path" "$receipt_sha" "$receipt_key receipt"
    validate_json_file "$receipt_path"
  done
  mkdir "$out_dir" ||
    die "could not atomically reserve output drill directory"
  chmod 700 "$out_dir"
  mkdir "$out_dir/inputs" "$out_dir/targets" "$out_dir/preflight" \
    "$out_dir/legs" "$out_dir/legs/rollback-to-previous" \
    "$out_dir/legs/restore-active"
  chmod 700 "$out_dir/inputs" "$out_dir/targets" "$out_dir/preflight" \
    "$out_dir/legs" "$out_dir/legs/rollback-to-previous" \
    "$out_dir/legs/restore-active"

  cp "$contract" "$out_dir/contract.json"
  cp "$initial_apply" "$out_dir/inputs/initial-release.apply.json"
  canonical_copy "$var_file" "$out_dir/inputs/terraform.tfvars.json"
  cp "$old_target" "$out_dir/targets/old.json"
  cp "$new_target" "$out_dir/targets/new.json"
  chmod 600 "$out_dir/contract.json" \
    "$out_dir/inputs/initial-release.apply.json" \
    "$out_dir/targets/old.json" "$out_dir/targets/new.json"

  for target in old new; do
    target_path="$old_target"
    [ "$target" = "old" ] || target_path="$new_target"
    manifest_path="$(jq -er '.consumer_manifest.path' "$target_path")"
    cp "$manifest_path" "$out_dir/inputs/$target.consumer-manifest.json"
    chmod 600 "$out_dir/inputs/$target.consumer-manifest.json"
  done
  for receipt_key in alarm_delivery alarm_migration log_readiness versioning; do
    receipt_path="$(jq -er ".guard_receipts.$receipt_key.path" "$contract")"
    cp "$receipt_path" "$out_dir/inputs/${receipt_key//_/-}.json"
    chmod 600 "$out_dir/inputs/${receipt_key//_/-}.json"
  done
  contract_sha="$(sha256_file "$out_dir/contract.json")"
  initial_sha="$(sha256_file "$out_dir/inputs/initial-release.apply.json")"
  var_sha="$(sha256_file "$out_dir/inputs/terraform.tfvars.json")"
  old_sha="$(sha256_file "$out_dir/targets/old.json")"
  new_sha="$(sha256_file "$out_dir/targets/new.json")"
  jq -n -S -c \
    --argjson schema_version "$SCHEMA_VERSION" \
    --arg drill_id "$(jq -er '.drill_id' "$contract")" \
    --arg git_commit "$(jq -er '.control.git_commit' "$contract")" \
    --arg contract_sha256 "$contract_sha" \
    --arg initial_apply_receipt_sha256 "$initial_sha" \
    --arg var_file_sha256 "$var_sha" \
    --arg old_sha256 "$old_sha" \
    --arg new_sha256 "$new_sha" \
    --arg lineage "$(jq -er '.post_state_contract.state.lineage' "$initial_apply")" \
    --arg address_set_sha256 "$(
      jq -er '.post_state_contract.state.address_set_sha256' "$initial_apply"
    )" \
    --argjson serial "$(jq -er '.post_state_contract.state.serial' "$initial_apply")" \
    --argjson verified "$verified" \
    --argjson deadline "$deadline" \
    --argjson now "$now" '{
      schema_version:$schema_version,
      kind:"teamagent.forced-rollback-drill-state",
      drill_id:$drill_id,
      git_commit:$git_commit,
      state:"PREPARED",
      contract_sha256:$contract_sha256,
      initial_apply_receipt_sha256:$initial_apply_receipt_sha256,
      var_file_sha256:$var_file_sha256,
      initial_release_verified_at_epoch:$verified,
      initial_release_deadline_epoch:$deadline,
      created_at_epoch:$now,
      updated_at_epoch:$now,
      initial_state:{
        lineage:$lineage,
        serial:$serial,
        address_set_sha256:$address_set_sha256
      },
      target_sha256:{
        old:$old_sha256,
        new:$new_sha256
      },
      preflight:{
        old:null,
        new:null
      },
      legs:{
        rollback_to_previous:{
          ordinal:1,
          name:"rollback-to-previous",
          channel:"rollback",
          target:"old",
          approval_id:"OK-1",
          status:"PENDING",
          authorization:null,
          plan:null,
          approval:null,
          apply:null
        },
        restore_active:{
          ordinal:2,
          name:"restore-active",
          channel:"active",
          target:"new",
          approval_id:"OK-2",
          status:"PENDING",
          authorization:null,
          plan:null,
          approval:null,
          apply:null
        }
      },
      old_dwell:{
        started_at_epoch:null,
        deadline_epoch:null,
        exceeded_at_epoch:null
      },
      failures:[],
      final_status:null,
      aggregate_sha256:null
    }' > "$out_dir/state.json"
  chmod 600 "$out_dir/state.json"
  validate_state_file "$out_dir/state.json"
  trap - EXIT
  rm -f "$old_target" "$new_target"
  echo "Prepared forced-rollback drill without changing ECS services:"
  echo "  drill_id=$(jq -er '.drill_id' "$contract")"
  echo "  contract_sha256=$contract_sha"
  echo "  drill_dir=$out_dir"
}

preflight_command() {
  local requested_dir="" targets="" now target output migration target_path
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --drill-dir)
        value "$@"
        [ -z "$requested_dir" ] || die "--drill-dir may be specified only once"
        requested_dir="$2"
        shift 2
        ;;
      --targets)
        value "$@"
        [ -z "$targets" ] || die "--targets may be specified only once"
        targets="$2"
        shift 2
        ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
  done
  [ -n "$requested_dir" ] && [ "$targets" = "old,new" ] ||
    die "preflight requires --drill-dir and exact --targets old,new"
  load_drill "$requested_dir"
  require_state PREPARED
  [ -f "$TERRAFORM_RUNTIME_GUARD" ] ||
    die "terraform runtime guard is missing"
  now="$(date +%s)"
  replace_state --argjson now "$now" '
    .state = "RECOVERY_REQUIRED" |
    .updated_at_epoch = $now
  '

  for target in old new; do
    output="$(preflight_file "$target")"
    [ ! -e "$output" ] && [ ! -L "$output" ] ||
      {
        record_failure drill preflight "preflight output already exists"
        die "preflight output already exists"
      }
    target_path="$(target_file "$target")"
    migration="$(jq -er '.preflight_migration_id' "$target_path")"
    if ! bash "$TERRAFORM_RUNTIME_GUARD" preflight \
      --migration "$migration" \
      --out "$output"; then
      record_failure drill preflight "$target preflight failed"
      die "$target signature/run-task preflight failed"
    fi
    chmod 600 "$output"
    jq -e \
      --arg migration "$migration" \
      --slurpfile expected "$target_path" '
      .kind == "runtime-preflight-receipt" and
      .migration_id == $migration and
      .images == $expected[0].images and
      (.expires_at_epoch | type) == "number" and
      .expires_at_epoch > .created_at_epoch and
      (.supply_chain | type == "object") and
      (.supply_chain.main.rekor_transparency_log_verified == true) and
      (.supply_chain.main.signature_count |
        type == "number" and . >= 1 and floor == .) and
      (.profiles | type == "object" and length > 0) and
      all(.profiles[];
        .exit_code == 0 and
        .stopped_reason_code == "EssentialContainerExited" and
        (.image | test("@sha256:[0-9a-f]{64}$")) and
        .image_digest == (.image | split("@")[1])
      )
    ' "$output" >/dev/null ||
      {
        record_failure drill preflight "$target preflight receipt was invalid"
        die "$target preflight receipt does not bind the target"
      }
  done
  now="$(date +%s)"
  replace_state \
    --arg old_path "$(preflight_file old)" \
    --arg old_sha "$(sha256_file "$(preflight_file old)")" \
    --arg old_migration "$(jq -er '.preflight_migration_id' "$(target_file old)")" \
    --arg new_path "$(preflight_file new)" \
    --arg new_sha "$(sha256_file "$(preflight_file new)")" \
    --arg new_migration "$(jq -er '.preflight_migration_id' "$(target_file new)")" \
    --argjson now "$now" '
    .state = "PREFLIGHTED" |
    .updated_at_epoch = $now |
    .preflight.old = {
      path:$old_path,
      sha256:$old_sha,
      migration_id:$old_migration
    } |
    .preflight.new = {
      path:$new_path,
      sha256:$new_sha,
      migration_id:$new_migration
    }
  '
  echo "Preflight passed for exact targets old,new"
}

authorization_value() {
  local name="$1" output="$2" value
  value="$(
    sed -n "s/^[[:space:]]*${name}=//p" "$output"
  )"
  [ -n "$value" ] && [ "$(printf '%s\n' "$value" | wc -l | tr -d ' ')" = "1" ] ||
    die "release authorization output is missing $name"
  printf '%s\n' "$value"
}

merge_authorized_var_file() {
  local gate_vars="$1" output="$2"
  validate_json_file "$gate_vars"
  jq -e '
    (keys | sort) == ([
      "image_deployment_consumer_manifest",
      "image_release_consumer_receipt_bindings",
      "image_release_receipt_catalog"
    ] | sort)
  ' "$gate_vars" >/dev/null ||
    die "release authorization gate var-file has unexpected keys"
  jq -S -c -s '.[0] * .[1]' \
    "$DRILL_DIR/inputs/terraform.tfvars.json" "$gate_vars" > "$output"
  chmod 600 "$output"
  jq -e 'type == "object"' "$output" >/dev/null ||
    die "could not merge the authorized JSON var-file"
}

plan_leg_command() {
  local requested_dir="" leg="" expected state_key target channel leg_path
  local target_path stdout_file gate_vars merged_var plan receipt migration
  local now expected_serial expected_lineage live_target plan_sha receipt_sha
  local intent authorization authorization_sha previous_intent minimum_created
  local receipt_now approval_record authorization_id
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --drill-dir)
        value "$@"
        [ -z "$requested_dir" ] || die "--drill-dir may be specified only once"
        requested_dir="$2"
        shift 2
        ;;
      --leg)
        value "$@"
        [ -z "$leg" ] || die "--leg may be specified only once"
        leg="$2"
        shift 2
        ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
  done
  case "$leg" in rollback-to-previous|restore-active) ;; *)
    die "plan-leg requires --leg rollback-to-previous or restore-active"
  esac
  [ -n "$requested_dir" ] || die "plan-leg requires --drill-dir"
  load_drill "$requested_dir"
  expected="$(expected_state_for_plan "$leg")"
  require_state "$expected"
  if [ "$leg" = "restore-active" ]; then
    check_old_dwell ||
      die "old dwell exceeded 20 minutes; recovery is required"
  fi
  [ -f "$AUTHORIZE_IMAGE_RELEASE" ] ||
    die "release authorization launcher is missing"
  [ -f "$TERRAFORM_RUNTIME_GUARD" ] ||
    die "terraform runtime guard is missing"
  verify_copied_guard_receipts

  state_key="$(leg_state_key "$leg")"
  target="$(leg_target "$leg")"
  channel="$(leg_channel "$leg")"
  leg_path="$(leg_dir "$leg")"
  target_path="$(target_file "$target")"
  stdout_file="$leg_path/authorization.stdout"
  gate_vars="$leg_path/release-authorization.tfvars.json"
  merged_var="$leg_path/terraform.tfvars.json"
  authorization="$leg_path/authorization.json"
  approval_record="$leg_path/verified-release-approval.json"
  plan="$leg_path/plan.tfplan"
  receipt="$leg_path/plan.runtime-guard.json"
  for output in \
    "$stdout_file" "$gate_vars" "$merged_var" "$authorization" \
    "$approval_record" "$plan" "$receipt"; do
    [ ! -e "$output" ] && [ ! -L "$output" ] ||
      die "leg artifact already exists; plans and receipts are one-use"
  done

  now="$(date +%s)"
  replace_state \
    --arg key "$state_key" \
    --argjson now "$now" '
    .state = "RECOVERY_REQUIRED" |
    .updated_at_epoch = $now |
    .legs[$key].status = "AUTHORIZING"
  '
  if ! bash "$AUTHORIZE_IMAGE_RELEASE" \
    --pipeline "$(jq -er '.pipeline' "$DRILL_DIR/contract.json")" \
    --channel "$channel" \
    --receipt-key "$(jq -er '.candidate.receipt_key' "$target_path")" \
    --receipt-version-id "$(jq -er '.candidate.receipt_version_id' "$target_path")" \
    --receipt-signature-version-id "$(
      jq -er '.candidate.receipt_signature_version_id' "$target_path"
    )" \
    --consumer-manifest "$(manifest_file "$target")" \
    --terraform-gate-vars-out "$gate_vars" \
    --approval-payload-bucket "$(jq -er '.approval.payload_bucket' "$target_path")" \
    --approval-payload-key "$(jq -er '.approval.payload_key' "$target_path")" \
    --approval-payload-version-id "$(
      jq -er '.approval.payload_version_id' "$target_path"
    )" \
    --approval-payload-sha256 "$(jq -er '.approval.payload_sha256' "$target_path")" \
    --approval-signature-bucket "$(
      jq -er '.approval.signature_bucket' "$target_path"
    )" \
    --approval-signature-key "$(jq -er '.approval.signature_key' "$target_path")" \
    --approval-signature-version-id "$(
      jq -er '.approval.signature_version_id' "$target_path"
    )" \
    --approval-signature-sha256 "$(
      jq -er '.approval.signature_sha256' "$target_path"
    )" \
    --verified-approval-out "$approval_record" > "$stdout_file"; then
    record_failure "$leg" authorization "fresh release authorization failed"
    die "fresh $channel release authorization failed"
  fi
  chmod 600 "$stdout_file" "$gate_vars" "$approval_record"
  merge_authorized_var_file "$gate_vars" "$merged_var"
  authorization_sha="$(sha256_file "$gate_vars")"
  authorization_id="$(new_uuid_v4)"
  jq -n -S -c \
    --arg authorization_id "$authorization_id" \
    --arg drill_id "$(jq -er '.drill_id' "$STATE_FILE")" \
    --arg pipeline "$(jq -er '.pipeline' "$DRILL_DIR/contract.json")" \
    --arg channel "$channel" \
    --arg receipt_key "$(authorization_value receipt_key "$stdout_file")" \
    --arg receipt_version_id "$(
      authorization_value receipt_version_id "$stdout_file"
    )" \
    --arg receipt_signature_key "$(
      authorization_value receipt_signature_key "$stdout_file"
    )" \
    --arg receipt_signature_version_id "$(
      authorization_value receipt_signature_version_id "$stdout_file"
    )" \
    --arg gate_var_sha256 "$authorization_sha" \
    --argjson issued_at_epoch "$(date +%s)" \
    --slurpfile release_approval "$approval_record" '{
      authorization_id:$authorization_id,
      drill_id:$drill_id,
      pipeline:$pipeline,
      channel:$channel,
      receipt_key:$receipt_key,
      receipt_version_id:$receipt_version_id,
      receipt_signature_key:$receipt_signature_key,
      receipt_signature_version_id:$receipt_signature_version_id,
      gate_var_sha256:$gate_var_sha256,
      issued_at_epoch:$issued_at_epoch,
      release_approval:$release_approval[0]
    }' > "$authorization"
  chmod 600 "$authorization"
  if [ "$leg" = "restore-active" ]; then
    jq -e --slurpfile previous \
      "$DRILL_DIR/legs/rollback-to-previous/authorization.json" '
      .authorization_id != $previous[0].authorization_id and
      .release_approval.approval_id !=
        $previous[0].release_approval.approval_id and
      [
        .receipt_key,
        .receipt_version_id,
        .receipt_signature_version_id
      ] != [
        $previous[0].receipt_key,
        $previous[0].receipt_version_id,
        $previous[0].receipt_signature_version_id
      ]
    ' "$authorization" >/dev/null ||
      {
        record_failure "$leg" authorization "release receipt was reused"
        die "restore leg did not receive a fresh release receipt"
      }
  fi

  migration="$(jq -er '.runtime_migration_id' "$target_path")"
  if ! bash "$TERRAFORM_RUNTIME_GUARD" plan \
    --var-file "$merged_var" \
    --out "$plan" \
    --receipt "$receipt" \
    --runtime-migration "$migration" \
    --preflight-receipt "$(preflight_file "$target")" \
    --alarm-delivery-receipt "$DRILL_DIR/inputs/alarm-delivery.json" \
    --versioning-receipt "$DRILL_DIR/inputs/versioning.json" \
    --log-readiness-receipt "$DRILL_DIR/inputs/log-readiness.json" \
    --alarm-migration-receipt "$DRILL_DIR/inputs/alarm-migration.json"; then
    record_failure "$leg" plan "guarded plan generation failed"
    die "guarded drill plan generation failed"
  fi
  chmod 600 "$plan" "$receipt"
  plan_sha="$(sha256_file "$plan")"
  receipt_sha="$(sha256_file "$receipt")"
  if [ "$leg" = "restore-active" ]; then
    [ "$plan_sha" != "$(
      jq -er '.legs.rollback_to_previous.plan.sha256' "$STATE_FILE"
    )" ] ||
      {
        record_failure "$leg" plan "saved plan SHA was reused"
        die "restore leg reused the rollback saved plan"
      }
    [ "$receipt_sha" != "$(
      jq -er '.legs.rollback_to_previous.plan.receipt_sha256' "$STATE_FILE"
    )" ] ||
      {
        record_failure "$leg" plan "plan receipt was reused"
        die "restore leg reused the rollback plan receipt"
      }
  fi
  if [ "$leg" = "rollback-to-previous" ]; then
    expected_serial="$(jq -er '.initial_state.serial' "$STATE_FILE")"
    expected_lineage="$(jq -er '.initial_state.lineage' "$STATE_FILE")"
    live_target="$(target_file new)"
    minimum_created="$(
      jq -er '.initial_release_verified_at_epoch' "$STATE_FILE"
    )"
  else
    expected_serial="$(
      jq -er '.legs.rollback_to_previous.apply.post_serial' "$STATE_FILE"
    )"
    expected_lineage="$(
      jq -er '.legs.rollback_to_previous.apply.terraform_lineage' "$STATE_FILE"
    )"
    live_target="$(target_file old)"
    minimum_created="$(
      jq -er '.legs.rollback_to_previous.apply.applied_at_epoch' "$STATE_FILE"
    )"
  fi
  receipt_now="$(date +%s)"
  jq -e \
    --arg plan_sha "$plan_sha" \
    --arg var_sha "$(sha256_file "$merged_var")" \
    --arg migration "$migration" \
    --arg lineage "$expected_lineage" \
    --arg address_set_sha256 "$(
      jq -er '.initial_state.address_set_sha256' "$STATE_FILE"
    )" \
    --argjson serial "$expected_serial" \
    --argjson minimum_created "$minimum_created" \
    --argjson now "$receipt_now" \
    --slurpfile desired "$target_path" \
    --slurpfile live "$live_target" '
    .kind == "terraform-runtime-plan-receipt" and
    .plan_sha256 == $plan_sha and
    .var_file_sha256 == $var_sha and
    .migration_id == $migration and
    (.created_at_epoch |
      type == "number" and floor == . and
      . >= $minimum_created and . <= $now) and
    (.image_deployment_intent_id | test(
      "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )) and
    .images.live == $live[0].images and
    .images.desired == $desired[0].images and
    .state_contract.state.lineage == $lineage and
    .state_contract.state.serial == $serial and
    .state_contract.state.address_set_sha256 == $address_set_sha256 and
    .state_contract.task_revisions == (
      $live[0].resources |
      map({
        key:.consumer_id,
        value:(.task_definition_arn |
          capture(":(?<revision>[1-9][0-9]*)$").revision |
          tonumber)
      }) |
      from_entries
    )
  ' "$receipt" >/dev/null ||
    {
      record_failure "$leg" plan "plan receipt baseline or target mismatch"
      die "plan receipt does not bind the exact baseline and target"
    }
  intent="$(jq -er '.image_deployment_intent_id' "$receipt")"
  if [ "$leg" = "restore-active" ]; then
    previous_intent="$(
      jq -er '.legs.rollback_to_previous.plan.intent_id' "$STATE_FILE"
    )"
    [ "$intent" != "$previous_intent" ] ||
      {
        record_failure "$leg" plan "deployment intent was reused"
        die "restore plan reused the rollback deployment intent"
      }
    check_old_dwell ||
      die "old dwell exceeded 20 minutes while planning; recovery is required"
  fi
  now="$(date +%s)"
  if [ "$leg" = "rollback-to-previous" ]; then
    next_state="LEG1_PLANNED"
  else
    next_state="LEG2_PLANNED"
  fi
  replace_state \
    --arg key "$state_key" \
    --arg next_state "$next_state" \
    --arg authorization_path "$authorization" \
    --arg authorization_sha "$(sha256_file "$authorization")" \
    --arg plan_path "$plan" \
    --arg receipt_path "$receipt" \
    --arg plan_sha "$plan_sha" \
    --arg receipt_sha "$receipt_sha" \
    --arg intent "$intent" \
    --arg lineage "$expected_lineage" \
    --arg baseline_task_sha "$(jq -er ".target_sha256.$(
      if [ "$leg" = "rollback-to-previous" ]; then printf new; else printf old; fi
    )" "$STATE_FILE")" \
    --argjson serial "$expected_serial" \
    --argjson created "$(jq -er '.created_at_epoch' "$receipt")" \
    --argjson now "$now" '
    .state = $next_state |
    .updated_at_epoch = $now |
    .legs[$key].status = "PLANNED" |
    .legs[$key].authorization = {
      path:$authorization_path,
      sha256:$authorization_sha
    } |
    .legs[$key].plan = {
      path:$plan_path,
      receipt_path:$receipt_path,
      sha256:$plan_sha,
      receipt_sha256:$receipt_sha,
      intent_id:$intent,
      terraform_lineage:$lineage,
      state_serial:$serial,
      baseline_task_revisions_sha256:$baseline_task_sha,
      created_at_epoch:$created
    }
  '
  echo "plan_sha256=$plan_sha"
}

validate_apply_receipt() {
  local leg="$1" receipt="$2" plan_sha="$3" target_path resources now state_key
  target_path="$(target_file "$(leg_target "$leg")")"
  state_key="$(leg_state_key "$leg")"
  now="$(date +%s)"
  resources="$(mktemp "$DRILL_DIR/.post-resources.XXXXXXXX")"
  if ! jq -S -c '
    .post_live_contract.resources //
    .ecs_service_saga_verification_receipt.resources //
    .ecs_service_saga_verification_receipt.planned.resources //
    empty
  ' "$receipt" > "$resources" || [ ! -s "$resources" ]; then
    rm -f "$resources"
    return 1
  fi
  if ! jq -e --slurpfile expected "$target_path" \
    --slurpfile resources "$resources" \
    --arg plan_sha "$plan_sha" \
    --arg intent "$(jq -er ".legs.$state_key.plan.intent_id" "$STATE_FILE")" \
    --arg address_set_sha256 "$(
      jq -er '.initial_state.address_set_sha256' "$STATE_FILE"
    )" \
    --argjson now "$now" '
    .kind == "terraform-runtime-apply-receipt" and
    .status == "applied" and
    .plan_sha256 == $plan_sha and
    .image_deployment_intent_id == $intent and
    (.apply_attempt_id | test(
      "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )) and
    (.applied_at_epoch |
      type == "number" and . >= 0 and floor == . and . <= $now) and
    .post_live_contract.images == $expected[0].images and
    $resources[0] == $expected[0].resources and
    .post_state_contract.task_revisions == (
      $expected[0].resources |
      map({
        key:.consumer_id,
        value:(.task_definition_arn |
          capture(":(?<revision>[1-9][0-9]*)$").revision |
          tonumber)
      }) |
      from_entries
    ) and
    (.post_state_contract.state.lineage |
      type == "string" and length > 0) and
    (.post_state_contract.state.serial |
      type == "number" and . >= 0 and floor == .) and
    .post_state_contract.state.address_set_sha256 == $address_set_sha256 and
    .post_apply_service_probe.kind ==
      "teamagent-post-apply-service-probe-receipt" and
    .post_apply_service_probe.apply_attempt_id == .apply_attempt_id and
    (.post_apply_service_probe.verified_at_utc |
      test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")) and
    .post_apply_service_probe.task.exit_code == 0 and
    ([.post_apply_service_probe.result.checks[]] | all(. == true)) and
    .openclaw_rollout_result.passed == true and
    .openclaw_rollout_result.applyAttemptId == .apply_attempt_id and
    (.openclaw_rollout_result.dmQa as $dm_qa |
      ($dm_qa | keys | sort) == ([
        "applyAttemptId",
        "kind",
        "locator",
        "mcpTaskDefinitionArn",
        "openclawTaskDefinitionArn",
        "result",
        "schema_version",
        "verified_at_utc"
      ] | sort) and
      $dm_qa.kind == "teamagent-forced-rollback-dm-qa-result" and
      $dm_qa.schema_version == 1 and
      $dm_qa.result == "PASSED" and
      $dm_qa.applyAttemptId == .apply_attempt_id and
      ($dm_qa.verified_at_utc |
        test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")) and
      ($dm_qa.openclawTaskDefinitionArn |
        test("^arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-openclaw:[1-9][0-9]*$")) and
      $dm_qa.openclawTaskDefinitionArn ==
        .openclaw_rollout_result.newTaskDefinitionArn and
      any($expected[0].resources[];
        .consumer_id == "mcp" and
        .task_definition_arn == $dm_qa.mcpTaskDefinitionArn
      ) and
      ($dm_qa.locator | keys | sort) == ([
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
      $dm_qa.locator.object_lock_mode == "COMPLIANCE" and
      $dm_qa.locator.key == (
        "forced-rollback-drills/" + .apply_attempt_id +
        "/dm-qa/result.json"
      ) and
      ($dm_qa.locator.sha256 | test("^[0-9a-f]{64}$")) and
      $dm_qa.locator.signature.key == ($dm_qa.locator.key + ".sig") and
      $dm_qa.locator.signature.verified == true and
      $dm_qa.locator.exact_version_redownload.bytes_match == true
    ) and
    .ecs_service_saga_verification_receipt.stage == "VERIFIED_APPLIED" and
    .ecs_service_saga_verification_receipt.apply_attempt_id ==
      .apply_attempt_id and
    .ecs_service_saga_verification_receipt.plan_sha256 == .plan_sha256 and
    .deployment_finalization_receipt.state == "APPLIED" and
    .deployment_finalization_receipt.apply_attempt_id == .apply_attempt_id and
    .deployment_finalization_receipt.plan_sha256 == .plan_sha256 and
    (.post_apply_service_probe.result.checks |
      type == "object" and
      (keys | sort) == ([
        "connect_build_inputs_sha256",
        "connect_contract_ok",
        "connect_http_200",
        "connect_manifest_sha256",
        "connect_sha256",
        "connect_version_id",
        "mcp_http_200"
      ] | sort) and
      length == 7 and
      all(.[]; . == true))
  ' "$receipt" >/dev/null; then
    rm -f "$resources"
    return 1
  fi
  rm -f "$resources"
}

apply_leg_command() {
  local requested_dir="" leg="" expected state_key approval_id approval_action
  local drill_id leg_path plan receipt apply_receipt plan_sha expected_text supplied
  local automation_identity
  local now started applied_epoch post_serial post_lineage plan_serial
  local attempt target_sha next_state dm_qa_deadline guard_status
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --drill-dir)
        value "$@"
        [ -z "$requested_dir" ] || die "--drill-dir may be specified only once"
        requested_dir="$2"
        shift 2
        ;;
      --leg)
        value "$@"
        [ -z "$leg" ] || die "--leg may be specified only once"
        leg="$2"
        shift 2
        ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
  done
  case "$leg" in rollback-to-previous|restore-active) ;; *)
    die "apply-leg requires --leg rollback-to-previous or restore-active"
  esac
  [ -n "$requested_dir" ] || die "apply-leg requires --drill-dir"
  load_drill "$requested_dir"
  expected="$(expected_state_for_apply "$leg")"
  require_state "$expected"
  if [ "$leg" = "restore-active" ]; then
    check_old_dwell ||
      die "old dwell exceeded 20 minutes; recovery is required"
  fi

  state_key="$(leg_state_key "$leg")"
  approval_id="$(leg_approval_id "$leg")"
  approval_action="$(leg_approval_action "$leg")"
  drill_id="$(jq -er '.drill_id' "$STATE_FILE")"
  leg_path="$(leg_dir "$leg")"
  plan="$(jq -er ".legs.$state_key.plan.path" "$STATE_FILE")"
  receipt="$(jq -er ".legs.$state_key.plan.receipt_path" "$STATE_FILE")"
  plan_sha="$(jq -er ".legs.$state_key.plan.sha256" "$STATE_FILE")"
  validate_bound_file "$plan" "$plan_sha" "$leg plan"
  validate_bound_file \
    "$receipt" \
    "$(jq -er ".legs.$state_key.plan.receipt_sha256" "$STATE_FILE")" \
    "$leg plan receipt"
  now="$(date +%s)"
  if [ "$leg" = "rollback-to-previous" ] &&
    [ "$now" -gt "$(jq -er '.initial_release_deadline_epoch' "$STATE_FILE")" ]; then
    record_failure "$leg" approval "30 minute rollback start deadline exceeded"
    die "rollback apply did not start within 30 minutes of initial verification"
  fi
  expected_text="APPROVE $drill_id $approval_action $plan_sha"
  echo "$approval_id requires this exact one-use approval:" >&2
  echo "$expected_text" >&2
  IFS= read -r supplied || die "$approval_id approval was not provided"
  [ "$supplied" = "$expected_text" ] ||
    die "$approval_id approval does not bind this drill ID, action, and plan SHA"

  now="$(date +%s)"
  if [ "$leg" = "rollback-to-previous" ] &&
    [ "$now" -gt "$(jq -er '.initial_release_deadline_epoch' "$STATE_FILE")" ]; then
    record_failure "$leg" approval "30 minute rollback start deadline exceeded"
    die "rollback apply did not start within 30 minutes of initial verification"
  fi
  apply_receipt="$leg_path/apply.runtime-guard.json"
  automation_identity="$leg_path/apply.automation-identity.json"
  [ ! -e "$apply_receipt" ] && [ ! -L "$apply_receipt" ] &&
    [ ! -e "$automation_identity" ] && [ ! -L "$automation_identity" ] ||
    die "apply receipt already exists; this plan cannot be executed again"
  started="$now"
  if [ "$leg" = "rollback-to-previous" ]; then
    dm_qa_deadline="$((started + MAX_OLD_DWELL_SECONDS))"
  else
    dm_qa_deadline="$(jq -er '.old_dwell.deadline_epoch' "$STATE_FILE")"
  fi
  replace_state \
    --arg key "$state_key" \
    --arg approval_id "$approval_id" \
    --arg drill_id "$drill_id" \
    --arg action "$approval_action" \
    --arg plan_sha "$plan_sha" \
    --arg text_sha "$(printf '%s\n' "$supplied" | {
      if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | awk '{print $1}'
      else
        shasum -a 256 | awk '{print $1}'
      fi
    })" \
    --argjson dm_qa_deadline "$dm_qa_deadline" \
    --argjson now "$now" '
    .state = "RECOVERY_REQUIRED" |
    .updated_at_epoch = $now |
    .legs[$key].status = "APPLYING" |
    .legs[$key].approval = {
      approval_id:$approval_id,
      drill_id:$drill_id,
      action:$action,
      plan_sha256:$plan_sha,
      approval_text_sha256:$text_sha,
      consumed_at_epoch:$now
    } |
    if $key == "rollback_to_previous" then
      .old_dwell.started_at_epoch = $now |
      .old_dwell.deadline_epoch = $dm_qa_deadline
    else
      .
    end
  '

  if ! bash "$TERRAFORM_RUNTIME_GUARD" verify \
    --plan "$plan" \
    --receipt "$receipt"; then
    record_failure "$leg" verify "saved-plan verification failed after approval consumption"
    die "saved-plan verification failed; approval and plan are consumed"
  fi
  now="$(date +%s)"
  if [ "$leg" = "rollback-to-previous" ] &&
    [ "$now" -gt "$(jq -er '.initial_release_deadline_epoch' "$STATE_FILE")" ]; then
    record_failure "$leg" verify "30 minute rollback start deadline exceeded"
    die "rollback apply did not start within 30 minutes of initial verification"
  fi
  if [ "$leg" = "restore-active" ]; then
    check_old_dwell ||
      die "old dwell exceeded 20 minutes before restore apply"
  fi
  if [ "$now" -ge "$dm_qa_deadline" ]; then
    record_failure "$leg" dm-qa-timeout \
      "no bounded DM QA window remained before guarded apply"
    die "DM QA deadline elapsed before apply; recovery is required"
  fi
  if bash "$TERRAFORM_RUNTIME_GUARD" apply \
    --plan "$plan" \
    --receipt "$receipt" \
    --forced-rollback-dm-qa-deadline-epoch "$dm_qa_deadline" \
    --automation-identity-out "$automation_identity" \
    --out "$apply_receipt"; then
    :
  else
    guard_status=$?
    if [ "$guard_status" -eq 124 ]; then
      record_failure "$leg" dm-qa-timeout \
        "forced rollback DM QA exhausted its bounded old-dwell window"
      die "DM QA timed out; recovery is required and the plan must not be retried"
    fi
    if [ "$guard_status" -eq 24 ]; then
      record_failure "$leg" dm-qa "forced rollback DM QA failed"
      die "DM QA failed; recovery is required and the plan must not be retried"
    fi
    record_failure "$leg" apply "guarded apply or pre-finalization QA failed"
    die "guarded apply failed; the plan must not be retried"
  fi
  chmod 600 "$apply_receipt" "$automation_identity"
  jq -e \
    --arg account "$ACCOUNT_ID" \
    --arg arn "$TRUSTED_AUTOMATION_ARN" '
    (keys | sort) == ["Account","Arn","UserId"] and
    .Account == $account and
    .Arn == $arn and
    (.UserId | type == "string" and length > 0)
  ' "$automation_identity" >/dev/null ||
    die "guard automation identity receipt is invalid"
  if ! validate_apply_receipt "$leg" "$apply_receipt" "$plan_sha"; then
    record_failure "$leg" apply "final apply receipt lacks exact steady run-task DM or saga evidence"
    die "apply receipt does not prove all pre-finalization leg gates"
  fi
  post_serial="$(jq -er '.post_state_contract.state.serial' "$apply_receipt")"
  post_lineage="$(jq -er '.post_state_contract.state.lineage' "$apply_receipt")"
  plan_serial="$(jq -er ".legs.$state_key.plan.state_serial" "$STATE_FILE")"
  [ "$post_lineage" = "$(jq -er ".legs.$state_key.plan.terraform_lineage" "$STATE_FILE")" ] &&
    [ "$post_serial" -eq "$((plan_serial + 1))" ] ||
    {
      record_failure "$leg" apply "post-apply Terraform state contract mismatch"
      die "post-apply Terraform state does not advance the bound plan baseline"
    }
  attempt="$(jq -er '.apply_attempt_id' "$apply_receipt")"
  applied_epoch="$(jq -er '.applied_at_epoch' "$apply_receipt")"
  if [ "$leg" = "restore-active" ]; then
    [ "$attempt" != "$(
      jq -er '.legs.rollback_to_previous.apply.apply_attempt_id' "$STATE_FILE"
    )" ] ||
      {
        record_failure "$leg" apply "apply attempt ID was reused"
        die "restore leg reused the rollback apply attempt ID"
      }
  fi
  now="$(date +%s)"
  target_sha="$(jq -er ".target_sha256.$(leg_target "$leg")" "$STATE_FILE")"
  if [ "$leg" = "rollback-to-previous" ]; then
    next_state="LEG1_APPLIED"
  else
    next_state="LEG2_APPLIED"
  fi
  replace_state \
    --arg key "$state_key" \
    --arg next_state "$next_state" \
    --arg receipt_path "$apply_receipt" \
    --arg receipt_sha "$(sha256_file "$apply_receipt")" \
    --arg automation_identity_path "$automation_identity" \
    --arg automation_identity_sha "$(sha256_file "$automation_identity")" \
    --arg plan_sha "$plan_sha" \
    --arg attempt "$attempt" \
    --arg lineage "$post_lineage" \
    --arg target_sha "$target_sha" \
    --argjson post_serial "$post_serial" \
    --argjson started "$started" \
    --argjson applied "$applied_epoch" \
    --argjson dwell_deadline "$dm_qa_deadline" \
    --argjson now "$now" '
    .state = $next_state |
    .updated_at_epoch = $now |
    .legs[$key].status = "APPLIED" |
    .legs[$key].apply = {
      path:$receipt_path,
      sha256:$receipt_sha,
      automation_identity_path:$automation_identity_path,
      automation_identity_sha256:$automation_identity_sha,
      plan_sha256:$plan_sha,
      apply_attempt_id:$attempt,
      terraform_lineage:$lineage,
      post_serial:$post_serial,
      post_target_sha256:$target_sha,
      started_at_epoch:$started,
      applied_at_epoch:$applied,
      completed_at_epoch:$now
    } |
    if $key == "rollback_to_previous" then
      .old_dwell.started_at_epoch = $started |
      .old_dwell.deadline_epoch = $dwell_deadline
    else
      .
    end
  '
  check_old_dwell ||
    die "old dwell exceeded 20 minutes during $leg; incident evidence was recorded"
  echo "Applied $leg with $approval_id and exact plan_sha256=$plan_sha"
}

validate_terminal_live_snapshot() {
  local raw="$1" full="$2" previous="$3" initial="$4"
  local output="$5" observed_at="$6"
  python3 - \
    "$raw" "$full" "$previous" "$initial" "$output" "$observed_at" <<'PY'
import json
import os
import sys

(
    raw_path,
    full_path,
    previous_path,
    initial_path,
    output_path,
    observed_at,
) = sys.argv[1:]
with open(previous_path, encoding="utf-8") as handle:
    previous = json.load(handle)
with open(initial_path, encoding="utf-8") as handle:
    initial = json.load(handle)
with open(full_path, encoding="utf-8") as handle:
    full = json.load(handle)

image_fields = {
    "openclaw_image": "openclaw",
    "mcp_image": "mcp",
    "x_buzz_image": "x_buzz",
    "media_worker_image": "tiktok",
}
activation_fields = {
    "connect_web": "enable_connect_web",
    "ingest": "ingest_rule_enabled",
    "morning_digest": "morning_digest_rule_enabled",
    "canary": "canary_rule_enabled",
    "x_buzz_worker": "enable_x_research",
    "tiktok_acquire": "enable_tiktok_acquire",
}
wanted = set(image_fields) | set(activation_fields.values())
values = {}
with open(raw_path, encoding="utf-8") as handle:
    for raw_line in handle:
        line = raw_line.strip()
        if not line or line.startswith("#") or " = " not in line:
            continue
        name, encoded = line.split(" = ", 1)
        if name not in wanted:
            continue
        if name in values:
            raise SystemExit(f"duplicate terminal snapshot field: {name}")
        values[name] = json.loads(encoded)

def expected_raw(target):
    expected = {
        field: target["images"][image_key]
        for field, image_key in image_fields.items()
    }
    for resource in target["resources"]:
        field = activation_fields.get(resource["consumer_id"])
        if field is None:
            continue
        state = resource["activation"]["state"]
        if resource["activation"]["type"] == "ecs_service":
            state = state > 0
        elif resource["activation"]["type"] in {
            "eventbridge_rule_ecs_target",
            "eventbridge_rule_lambda_taskdef_arn_environment",
        }:
            state = state == "ENABLED"
        expected[field] = state
    return expected


previous_raw = expected_raw(previous)
initial_raw = expected_raw(initial)
if set(values) != wanted:
    raise SystemExit("terminal HCL snapshot is missing exact observed fields")

task_keys = {
    "mcp": "mcp",
    "connect_web": "connect_web",
    "openclaw": "openclaw",
    "canary": "canary",
    "ingest": "ingest",
    "morning_digest": "morning",
    "x_buzz_worker": "x_buzz",
    "tiktok_acquire": "tiktok",
}

def live_activation(consumer_id):
    if consumer_id == "mcp":
        return full["services"]["mcp"]["critical"]["desired_count"]
    if consumer_id == "connect_web":
        return full["services"]["connect_web"]["critical"]["desired_count"]
    if consumer_id == "openclaw":
        return full["services"]["openclaw"]["critical"]["desired_count"]
    if consumer_id == "canary":
        return full["rules"]["canary"]["critical"]["state"]
    if consumer_id == "ingest":
        return full["rules"]["ingest"]["critical"]["state"]
    if consumer_id == "morning_digest":
        return full["rules"]["morning"]["critical"]["state"]
    if consumer_id == "x_buzz_worker":
        return full["event_mappings"]["x_buzz"]["critical"]["enabled"]
    if consumer_id == "tiktok_acquire":
        return full["event_mappings"]["tiktok"]["critical"]["enabled"]
    raise SystemExit(f"terminal consumer is outside fixed scope: {consumer_id}")

observed_resources = []
for expected in initial["resources"]:
    consumer_id = expected["consumer_id"]
    task = full["taskdefs"][task_keys[consumer_id]]
    observed = {
        "activation": {
            "identity": expected["activation"]["identity"],
            "state": live_activation(consumer_id),
            "type": expected["activation"]["type"],
        },
        "consumer_id": consumer_id,
        "image": task["image"],
        "pipeline": expected["pipeline"],
        "subject": expected["subject"],
        "task_definition_arn": task["arn"],
        "terraform_address": expected["terraform_address"],
    }
    observed_resources.append(observed)

observed_raw = {
    "openclaw_image": full["taskdefs"]["openclaw"]["image"],
    "mcp_image": full["taskdefs"]["mcp"]["image"],
    "x_buzz_image": full["taskdefs"]["x_buzz"]["image"],
    "media_worker_image": full["taskdefs"]["tiktok"]["image"],
}
for expected in initial["resources"]:
    field = activation_fields.get(expected["consumer_id"])
    if field is None:
        continue
    state = live_activation(expected["consumer_id"])
    if expected["activation"]["type"] == "ecs_service":
        state = state > 0
    elif expected["activation"]["type"] in {
        "eventbridge_rule_ecs_target",
        "eventbridge_rule_lambda_taskdef_arn_environment",
    }:
        state = state == "ENABLED"
    observed_raw[field] = state
if values != observed_raw:
    raise SystemExit("terminal HCL and full JSON observations disagree")

if observed_resources == initial["resources"] and values == initial_raw:
    classification = "INITIAL_NEW"
elif observed_resources == previous["resources"] and values == previous_raw:
    classification = "PREVIOUS_OLD"
else:
    classification = "UNKNOWN"

evidence = {
    "kind": "teamagent-forced-rollback-terminal-live-snapshot",
    "classification": classification,
    "observed_at_epoch": int(observed_at),
    "images": {
        image_key: values[field]
        for field, image_key in image_fields.items()
    },
    "activations": {
        field: values[field]
        for field in sorted(activation_fields.values())
    },
    "resources": observed_resources,
}
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(output_path, flags, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
    json.dump(
        evidence,
        handle,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    handle.write("\n")
if classification != "INITIAL_NEW":
    raise SystemExit(2)
PY
}

build_trusted_scope() {
  local output="$1"
  python3 - \
    "$CONSUMER_REGISTRY" \
    "$DRILL_DIR/inputs/initial-release.apply.json" \
    "$output" <<'PY'
import json
import os
import re
import sys

registry_path, receipt_path, output_path = sys.argv[1:]
with open(registry_path, encoding="utf-8") as handle:
    registry = json.load(handle)
with open(receipt_path, encoding="utf-8") as handle:
    receipt = json.load(handle)


def fail(message):
    raise SystemExit(f"trusted scope: {message}")


def resource_index(value, label):
    if not isinstance(value, list) or not value:
        fail(f"{label} must contain scoped resources")
    result = {}
    for resource in value:
        if not isinstance(resource, dict):
            fail(f"{label} contains a non-object resource")
        consumer_id = resource.get("consumer_id")
        if not isinstance(consumer_id, str) or not consumer_id:
            fail(f"{label} contains an invalid consumer_id")
        if consumer_id in result:
            fail(f"{label} contains duplicate consumer_id {consumer_id}")
        result[consumer_id] = resource
    return result


def registry_index(value):
    consumers = value.get("consumers") if isinstance(value, dict) else None
    if not isinstance(consumers, list) or not consumers:
        fail("consumer registry is empty")
    result = {}
    for consumer in consumers:
        if not isinstance(consumer, dict):
            fail("consumer registry contains a non-object consumer")
        consumer_id = consumer.get("consumer_id")
        if not isinstance(consumer_id, str) or not consumer_id:
            fail("consumer registry contains an invalid consumer_id")
        if consumer_id in result:
            fail(f"consumer registry contains duplicate {consumer_id}")
        result[consumer_id] = consumer
    return result


image_pattern = re.compile(
    r"^718959508629[.]dkr[.]ecr[.]ap-northeast-1[.]amazonaws[.]com/"
    r"(?P<repository>[a-z0-9._/-]+)@"
    r"(?P<digest>sha256:[0-9a-f]{64})$"
)
task_pattern = re.compile(
    r"^arn:aws:ecs:ap-northeast-1:718959508629:task-definition/"
    r"(?P<family>[A-Za-z0-9_-]+):(?P<revision>[1-9][0-9]*)$"
)


def digest_for(resource, owner, label):
    image = resource.get("image")
    match = image_pattern.fullmatch(image) if isinstance(image, str) else None
    if match is None:
        fail(f"{label} has a non-canonical image")
    if match.group("repository") != owner.get("release_repository"):
        fail(f"{label} image repository differs from the registry")
    return match.group("digest")


def task_for(resource, owner, label):
    arn = resource.get("task_definition_arn")
    match = task_pattern.fullmatch(arn) if isinstance(arn, str) else None
    if match is None:
        fail(f"{label} has a non-canonical task definition ARN")
    if match.group("family") != owner.get("ecs_family"):
        fail(f"{label} task definition family differs from the registry")
    return arn, int(match.group("revision"))


try:
    previous_resources = receipt["pre_live_contract"]["resources"]
    initial_resources = receipt["post_live_contract"]["resources"]
    previous_revisions = receipt["pre_state_contract"]["task_revisions"]
    initial_revisions = receipt["post_state_contract"]["task_revisions"]
except (KeyError, TypeError):
    fail("initial release receipt lacks exact pre/post resource contracts")

previous_by_id = resource_index(previous_resources, "pre_live_contract.resources")
initial_by_id = resource_index(initial_resources, "post_live_contract.resources")
if set(previous_by_id) != set(initial_by_id):
    fail("pre/post live contracts have different consumer scopes")
consumer_ids = sorted(initial_by_id)
owners = registry_index(registry)
if any(consumer_id not in owners for consumer_id in consumer_ids):
    fail("initial release receipt contains an unregistered consumer")
if (
    not isinstance(previous_revisions, dict)
    or not isinstance(initial_revisions, dict)
    or set(previous_revisions) != set(consumer_ids)
    or set(initial_revisions) != set(consumer_ids)
):
    fail("pre/post task revisions do not exactly cover the scoped consumers")

subjects_by_identity = {}
resources = []
for consumer_id in consumer_ids:
    owner = owners[consumer_id]
    owner_receipt = owner.get("receipt")
    if not isinstance(owner_receipt, dict):
        fail(f"registry consumer {consumer_id} lacks receipt ownership")
    pipeline = owner_receipt.get("pipeline")
    subject = owner_receipt.get("subject")
    terraform_address = owner.get("terraform_task_definition_address")
    if not all(
        isinstance(value, str) and value
        for value in (pipeline, subject, terraform_address)
    ):
        fail(f"registry consumer {consumer_id} has incomplete ownership")

    previous = previous_by_id[consumer_id]
    initial = initial_by_id[consumer_id]
    for label, resource in (("previous", previous), ("initial", initial)):
        if resource.get("pipeline") != pipeline:
            fail(f"{label} resource {consumer_id} pipeline differs from registry")
        if resource.get("subject") != subject:
            fail(f"{label} resource {consumer_id} subject differs from registry")
        if resource.get("terraform_address") != terraform_address:
            fail(
                f"{label} resource {consumer_id} Terraform address differs from registry"
            )

    previous_digest = digest_for(previous, owner, f"previous resource {consumer_id}")
    initial_digest = digest_for(initial, owner, f"initial resource {consumer_id}")
    previous_arn, previous_revision = task_for(
        previous, owner, f"previous resource {consumer_id}"
    )
    initial_arn, initial_revision = task_for(
        initial, owner, f"initial resource {consumer_id}"
    )
    if (
        type(previous_revisions[consumer_id]) is not int
        or type(initial_revisions[consumer_id]) is not int
        or previous_revisions[consumer_id] != previous_revision
        or initial_revisions[consumer_id] != initial_revision
    ):
        fail(f"resource {consumer_id} ARN/revision binding is inconsistent")

    subject_identity = (pipeline, subject)
    subject_value = {
        "pipeline": pipeline,
        "name": subject,
        "release_repository": owner["release_repository"],
        "previous_digest": previous_digest,
        "initial_new_digest": initial_digest,
    }
    existing_subject = subjects_by_identity.setdefault(
        subject_identity,
        subject_value,
    )
    if existing_subject != subject_value:
        fail(f"subject {pipeline}/{subject} has inconsistent resource digests")
    resources.append(
        {
            "consumer_id": consumer_id,
            "terraform_address": terraform_address,
            "pipeline": pipeline,
            "subject": subject,
            "previous_task_definition_arn": previous_arn,
            "previous_task_revision": previous_revision,
            "initial_new_task_definition_arn": initial_arn,
            "initial_new_task_revision": initial_revision,
        }
    )

subjects = [
    subjects_by_identity[identity]
    for identity in sorted(subjects_by_identity)
]
scope = {
    "pipelines": sorted({subject["pipeline"] for subject in subjects}),
    "subjects": subjects,
    "resources": resources,
}
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(output_path, flags, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
    json.dump(
        scope,
        handle,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    handle.write("\n")
PY
}

prepare_aggregate_artifacts() {
  local output="$1" artifact_directory="$2" trusted_scope="$3"
  python3 "$AGGREGATE_BUILDER" prepare \
    --state "$STATE_FILE" \
    --contract "$DRILL_DIR/contract.json" \
    --initial-receipt "$DRILL_DIR/inputs/initial-release.apply.json" \
    --trusted-scope "$trusted_scope" \
    --terminal-snapshot "$DRILL_DIR/final-live.snapshot.json" \
    --artifact-directory "$artifact_directory" \
    --out "$output"
}

persist_aggregate_artifacts() {
  local manifest="$1" output="$2" now aws_bin
  need_cmd aws
  aws_bin="$(command -v aws)"
  now="$(date +%s)"
  PYTHONPATH="$REPO_ROOT/infra/codebuild${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "$ARTIFACT_STORE" \
      --manifest "$manifest" \
      --out "$output" \
      --aws-bin "$aws_bin" \
      --account-id "$ACCOUNT_ID" \
      --region "$REGION" \
      --bucket "$EVIDENCE_BUCKET" \
      --prefix "$EVIDENCE_PREFIX" \
      --encryption-key-alias "$EVIDENCE_ENCRYPTION_KEY_ALIAS" \
      --signing-key-alias "$DRILL_SIGNING_KEY_ALIAS" \
      --signing-algorithm "$DRILL_SIGNING_ALGORITHM" \
      --minimum-retention-days "$EVIDENCE_MIN_RETENTION_DAYS" \
      --now-epoch "$now"
}

build_aggregate() {
  local status="$1" output="$2" trusted_scope="$3" locators="$4"
  python3 "$AGGREGATE_BUILDER" build \
    --status "$status" \
    --state "$STATE_FILE" \
    --contract "$DRILL_DIR/contract.json" \
    --initial-receipt "$DRILL_DIR/inputs/initial-release.apply.json" \
    --trusted-scope "$trusted_scope" \
    --terminal-snapshot "$DRILL_DIR/final-live.snapshot.json" \
    --locators "$locators" \
    --out "$output"
}

persist_aggregate() {
  local source="$1" output="$2" drill_id="$3" now aws_bin
  need_cmd aws
  aws_bin="$(command -v aws)"
  now="$(date +%s)"
  PYTHONPATH="$REPO_ROOT/infra/codebuild${PYTHONPATH:+:$PYTHONPATH}" \
    python3 - \
      "$source" \
      "$output" \
      "$aws_bin" \
      "$ACCOUNT_ID" \
      "$REGION" \
      "$EVIDENCE_BUCKET" \
      "$EVIDENCE_PREFIX" \
      "$EVIDENCE_ENCRYPTION_KEY_ALIAS" \
      "$DRILL_SIGNING_KEY_ALIAS" \
      "$DRILL_SIGNING_ALGORITHM" \
      "$EVIDENCE_MIN_RETENTION_DAYS" \
      "$drill_id" \
      "$now" <<'PY'
import base64
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from forced_rollback_drill_evidence import canonical_drill_body_bytes
from teamagent_release_approval import canonical_json_bytes

(
    source_argument,
    output_argument,
    aws_bin,
    account_id,
    region,
    evidence_bucket,
    evidence_prefix,
    encryption_key_alias,
    signing_key_alias,
    signing_algorithm,
    minimum_retention_days_argument,
    drill_id,
    now_argument,
) = sys.argv[1:]
source_path = Path(source_argument)
output_path = Path(output_argument)
minimum_retention_days = int(minimum_retention_days_argument)
now = int(now_argument)
version_id_pattern = re.compile(r"[A-Za-z0-9._~+/=-]{1,1024}")
kms_arn_pattern = re.compile(
    rf"arn:aws:kms:{re.escape(region)}:{re.escape(account_id)}:"
    r"key/[0-9a-f-]{36}"
)


def fail(message: str) -> None:
    raise RuntimeError(message)


def aws_json(
    service: str,
    operation: str,
    arguments: list[str],
) -> dict[str, object]:
    endpoint_service = "s3" if service == "s3api" else service
    command = [
        aws_bin,
        service,
        operation,
        "--region",
        region,
        "--endpoint-url",
        f"https://{endpoint_service}.{region}.amazonaws.com",
        *arguments,
        "--no-cli-pager",
        "--no-paginate",
        "--output",
        "json",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip()[:1000]
        fail(f"AWS {service} {operation} failed: {detail}")
    try:
        value = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"AWS {service} {operation} returned invalid JSON"
        ) from exc
    if not isinstance(value, dict):
        fail(f"AWS {service} {operation} returned a non-object")
    return value


def resolve_key(
    alias: str,
    *,
    key_usage: str,
    key_spec: str | None = None,
) -> str:
    response = aws_json("kms", "describe-key", ["--key-id", alias])
    metadata = response.get("KeyMetadata")
    if not isinstance(metadata, dict):
        fail(f"KMS describe-key returned no metadata for {alias}")
    arn = metadata.get("Arn")
    if (
        not isinstance(arn, str)
        or not kms_arn_pattern.fullmatch(arn)
        or metadata.get("Enabled") is not True
        or metadata.get("KeyState") != "Enabled"
        or metadata.get("KeyUsage") != key_usage
        or (key_spec is not None and metadata.get("KeySpec") != key_spec)
    ):
        fail(f"KMS alias {alias} did not resolve to the required enabled key")
    return arn


def exact_version_id(response: dict[str, object], *, label: str) -> str:
    version_id = response.get("VersionId")
    if (
        not isinstance(version_id, str)
        or not version_id_pattern.fullmatch(version_id)
        or version_id in {"None", "null"}
    ):
        fail(f"S3 did not return an exact {label} VersionId")
    return version_id


def normalize_timestamp(value: object, *, label: str) -> tuple[str, dt.datetime]:
    if not isinstance(value, str):
        fail(f"{label} is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        fail(f"{label} is not timezone-aware")
    normalized = parsed.astimezone(dt.timezone.utc).replace(microsecond=0)
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ"), normalized


def put_object(
    *,
    key: str,
    body: Path,
    content_type: str,
    encryption_key_arn: str,
    retain_until: str,
) -> str:
    response = aws_json(
        "s3api",
        "put-object",
        [
            "--bucket",
            evidence_bucket,
            "--key",
            key,
            "--body",
            str(body),
            "--content-type",
            content_type,
            "--server-side-encryption",
            "aws:kms",
            "--ssekms-key-id",
            encryption_key_arn,
            "--object-lock-mode",
            "COMPLIANCE",
            "--object-lock-retain-until-date",
            retain_until,
            "--expected-bucket-owner",
            account_id,
            "--if-none-match",
            "*",
        ],
    )
    return exact_version_id(response, label=key)


def exact_version_download(
    *,
    key: str,
    version_id: str,
    expected_bytes: bytes,
    content_type: str,
    encryption_key_arn: str,
    minimum_retain_until: dt.datetime,
    destination: Path,
) -> tuple[dict[str, object], str, str]:
    metadata = aws_json(
        "s3api",
        "get-object",
        [
            "--bucket",
            evidence_bucket,
            "--key",
            key,
            "--version-id",
            version_id,
            "--expected-bucket-owner",
            account_id,
            str(destination),
        ],
    )
    downloaded = destination.read_bytes()
    downloaded_sha256 = hashlib.sha256(downloaded).hexdigest()
    retain_until, parsed_retain_until = normalize_timestamp(
        metadata.get("ObjectLockRetainUntilDate"),
        label=f"{key} ObjectLockRetainUntilDate",
    )
    if (
        downloaded != expected_bytes
        or metadata.get("VersionId") != version_id
        or metadata.get("ContentLength") != len(expected_bytes)
        or metadata.get("ContentType") != content_type
        or metadata.get("ServerSideEncryption") != "aws:kms"
        or metadata.get("SSEKMSKeyId") != encryption_key_arn
        or metadata.get("ObjectLockMode") != "COMPLIANCE"
        or parsed_retain_until < minimum_retain_until
    ):
        fail(f"immutable aggregate exact-version download did not match: {key}")
    return metadata, downloaded_sha256, retain_until


with source_path.open(encoding="utf-8") as handle:
    aggregate = json.load(handle)
if not isinstance(aggregate, dict) or aggregate.get("drill_id") != drill_id:
    fail("aggregate skeleton does not bind the requested drill")

encryption_key_arn = resolve_key(
    encryption_key_alias,
    key_usage="ENCRYPT_DECRYPT",
)
signing_key_arn = resolve_key(
    signing_key_alias,
    key_usage="SIGN_VERIFY",
    key_spec="RSA_3072",
)
if encryption_key_arn == signing_key_arn:
    fail("aggregate encryption and signing keys must be distinct")

aggregate["integrity"] = {
    "canonical_sha256": "",
    "kms_key_arn": signing_key_arn,
    "signing_algorithm": signing_algorithm,
    "signature": {},
    "immutable_object": {},
}
payload_bytes = canonical_drill_body_bytes(aggregate)
payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
payload_key = f"{evidence_prefix}/{drill_id}/aggregate.json"
signature_key = f"{payload_key}.sig"
# Terraform/IAM require at least 3650 whole remaining days.  The extra day is
# the same request-time cushion used by the DM QA evidence writer.
requested_retain_until = (
    dt.datetime.fromtimestamp(now, tz=dt.timezone.utc)
    + dt.timedelta(days=minimum_retention_days + 1)
).replace(microsecond=0)
requested_retain_until_text = requested_retain_until.strftime(
    "%Y-%m-%dT%H:%M:%SZ"
)

with tempfile.TemporaryDirectory(
    prefix=".aggregate-persistence.",
    dir=output_path.parent,
) as temporary_directory:
    directory = Path(temporary_directory)
    payload_path = directory / "aggregate.json"
    digest_path = directory / "aggregate.sha256"
    raw_signature_path = directory / "aggregate.sig"
    envelope_path = directory / "aggregate.sig.json"
    downloaded_payload_path = directory / "downloaded-aggregate.json"
    downloaded_signature_path = directory / "downloaded-aggregate.sig.json"
    payload_path.write_bytes(payload_bytes)
    digest_path.write_bytes(bytes.fromhex(payload_sha256))

    signed = aws_json(
        "kms",
        "sign",
        [
            "--key-id",
            signing_key_arn,
            "--message",
            f"fileb://{digest_path}",
            "--message-type",
            "DIGEST",
            "--signing-algorithm",
            signing_algorithm,
        ],
    )
    signature_base64 = signed.get("Signature")
    if (
        signed.get("KeyId") != signing_key_arn
        or signed.get("SigningAlgorithm") != signing_algorithm
        or not isinstance(signature_base64, str)
    ):
        fail("KMS returned an invalid aggregate signature")
    try:
        signature_bytes = base64.b64decode(signature_base64, validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("KMS returned malformed aggregate signature bytes") from exc
    if len(signature_bytes) < 256:
        fail("KMS returned a short aggregate signature")
    raw_signature_path.write_bytes(signature_bytes)

    signature_envelope = {
        "schema_version": 1,
        "drill_id": drill_id,
        "payload_key": payload_key,
        "payload_sha256": payload_sha256,
        "signing_kms_key_arn": signing_key_arn,
        "signing_algorithm": signing_algorithm,
        "signature_base64": signature_base64,
    }
    envelope_bytes = canonical_json_bytes(signature_envelope)
    envelope_path.write_bytes(envelope_bytes)

    payload_version_id = put_object(
        key=payload_key,
        body=payload_path,
        content_type="application/json",
        encryption_key_arn=encryption_key_arn,
        retain_until=requested_retain_until_text,
    )
    signature_version_id = put_object(
        key=signature_key,
        body=envelope_path,
        content_type="application/json",
        encryption_key_arn=encryption_key_arn,
        retain_until=requested_retain_until_text,
    )
    (
        _,
        downloaded_payload_sha256,
        returned_retain_until,
    ) = exact_version_download(
        key=payload_key,
        version_id=payload_version_id,
        expected_bytes=payload_bytes,
        content_type="application/json",
        encryption_key_arn=encryption_key_arn,
        minimum_retain_until=requested_retain_until,
        destination=downloaded_payload_path,
    )
    _, downloaded_signature_sha256, _ = exact_version_download(
        key=signature_key,
        version_id=signature_version_id,
        expected_bytes=envelope_bytes,
        content_type="application/json",
        encryption_key_arn=encryption_key_arn,
        minimum_retain_until=requested_retain_until,
        destination=downloaded_signature_path,
    )

    verified = aws_json(
        "kms",
        "verify",
        [
            "--key-id",
            signing_key_arn,
            "--message",
            f"fileb://{digest_path}",
            "--message-type",
            "DIGEST",
            "--signature",
            f"fileb://{raw_signature_path}",
            "--signing-algorithm",
            signing_algorithm,
        ],
    )
    if (
        verified.get("KeyId") != signing_key_arn
        or verified.get("SigningAlgorithm") != signing_algorithm
        or verified.get("SignatureValid") is not True
    ):
        fail("KMS aggregate signature verification failed")

aggregate["integrity"] = {
    "canonical_sha256": payload_sha256,
    "kms_key_arn": signing_key_arn,
    "signing_algorithm": signing_algorithm,
    "signature": {
        "key": signature_key,
        "version_id": signature_version_id,
        "sha256": downloaded_signature_sha256,
        "verified": True,
    },
    "immutable_object": {
        "bucket": evidence_bucket,
        "key": payload_key,
        "version_id": payload_version_id,
        "sha256": payload_sha256,
        "size": len(payload_bytes),
        "content_type": "application/json",
        "object_lock_mode": "COMPLIANCE",
        "retain_until": returned_retain_until,
        "encryption_kms_key_arn": encryption_key_arn,
        "exact_version_redownload": {
            "requested_version_id": payload_version_id,
            "returned_version_id": payload_version_id,
            "sha256": downloaded_payload_sha256,
            "size": len(payload_bytes),
            "bytes_match": True,
        },
    },
}
payload = canonical_json_bytes(aggregate)
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(output_path, flags, 0o600)
with os.fdopen(descriptor, "wb") as handle:
    handle.write(payload)
PY
}

validate_aggregate() {
  local source="$1" output="$2" trusted_scope="$3" mode="${4:-complete}"
  PYTHONPATH="$REPO_ROOT/infra/codebuild${PYTHONPATH:+:$PYTHONPATH}" \
    python3 - \
      "$AGGREGATE_VALIDATOR" \
      "$STATE_FILE" \
      "$DRILL_DIR/contract.json" \
      "$DRILL_DIR/inputs/initial-release.apply.json" \
      "$trusted_scope" \
      "$source" \
      "$output" \
      "$mode" <<'PY'
import copy
import datetime
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from teamagent_release_approval import canonical_json_bytes

(
    helper_argument,
    state_argument,
    contract_argument,
    initial_receipt_argument,
    trusted_scope_argument,
    source_argument,
    output_argument,
    mode,
) = sys.argv[1:]
helper_path = Path(helper_argument)
state_path = Path(state_argument)
contract_path = Path(contract_argument)
initial_receipt_path = Path(initial_receipt_argument)
trusted_scope_path = Path(trusted_scope_argument)
source_path = Path(source_argument)
output_path = Path(output_argument)
spec = importlib.util.spec_from_file_location(
    "forced_rollback_drill_evidence",
    helper_path,
)
if spec is None or spec.loader is None:
    raise SystemExit("FATAL: aggregate validator could not be imported")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with source_path.open(encoding="utf-8") as handle:
    aggregate = json.load(handle)
with state_path.open(encoding="utf-8") as handle:
    state = json.load(handle)
with contract_path.open(encoding="utf-8") as handle:
    contract = json.load(handle)
with initial_receipt_path.open(encoding="utf-8") as handle:
    initial_receipt = json.load(handle)
with trusted_scope_path.open(encoding="utf-8") as handle:
    trusted_scope = json.load(handle)

if state["git_commit"] != contract["control"]["git_commit"]:
    raise SystemExit("FATAL: trusted git commit binding changed")
if hashlib.sha256(contract_path.read_bytes()).hexdigest() != state["contract_sha256"]:
    raise SystemExit("FATAL: trusted drill contract binding changed")
verified_epoch = initial_receipt.get(
    "initial_release_verified_at_epoch",
    initial_receipt.get(
        "verified_at_epoch",
        initial_receipt.get("applied_at_epoch"),
    ),
)
if (
    type(verified_epoch) is not int
    or verified_epoch != state["initial_release_verified_at_epoch"]
):
    raise SystemExit("FATAL: trusted initial release timestamp binding changed")
initial_verified_at = datetime.datetime.fromtimestamp(
    verified_epoch,
    tz=datetime.timezone.utc,
).isoformat(timespec="seconds").replace("+00:00", "Z")
expected = {
    "git_commit": state["git_commit"],
    "drill_contract_sha256": state["contract_sha256"],
    # Runtime apply receipts do not carry their own immutable locator.  The
    # SHA-bound drill contract supplies the exact locator; prepare/load bind
    # its sha256/size to the copied receipt whenever those fields are present.
    "initial_release_apply": contract["control"]["initial_release_apply_locator"],
    "initial_release_verified_at_utc": initial_verified_at,
    "scope": trusted_scope,
}
body = copy.deepcopy(aggregate)
body["integrity"].pop("canonical_sha256", None)
body["integrity"].pop("signature", None)
body["integrity"].pop("immutable_object", None)
canonical_sha256 = hashlib.sha256(
    canonical_json_bytes(body)
).hexdigest()
if mode == "body-only":
    aggregate["integrity"]["canonical_sha256"] = canonical_sha256
    try:
        module.validate_drill_evidence(aggregate, expected)
    except ValueError as exc:
        if not str(exc).startswith("drill.integrity."):
            raise
    else:
        raise SystemExit(
            "FATAL: aggregate body prevalidation accepted placeholder integrity"
        )
    raise SystemExit(0)
if mode != "complete":
    raise SystemExit("FATAL: unsupported aggregate validation mode")
if aggregate["integrity"].get("canonical_sha256") != canonical_sha256:
    raise SystemExit("FATAL: persisted aggregate canonical SHA-256 changed")
validated = module.validate_drill_evidence(aggregate, expected)
payload = canonical_json_bytes(validated)
flags = 0
if hasattr(__import__("os"), "O_NOFOLLOW"):
    flags |= __import__("os").O_NOFOLLOW
descriptor = __import__("os").open(
    output_path,
    __import__("os").O_WRONLY | __import__("os").O_CREAT | __import__("os").O_EXCL | flags,
    0o600,
)
with __import__("os").fdopen(descriptor, "wb") as handle:
    handle.write(payload)
PY
}

finalize_command() {
  local requested_dir="" state status final_receipt plan_sha aggregate_stage
  local aggregate_located aggregate_out validated_out trusted_scope final_sha
  local artifact_manifest artifact_directory artifact_locators
  local now terminal_raw terminal_full terminal_json drill_id
  local terminal_ok="true"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --drill-dir)
        value "$@"
        [ -z "$requested_dir" ] || die "--drill-dir may be specified only once"
        requested_dir="$2"
        shift 2
        ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
  done
  [ -n "$requested_dir" ] || die "finalize requires --drill-dir"
  load_drill "$requested_dir"
  state="$(jq -er '.state' "$STATE_FILE")"
  case "$state" in LEG2_APPLIED|RECOVERY_REQUIRED) ;; *)
    die "invalid state transition: finalize requires LEG2_APPLIED or RECOVERY_REQUIRED"
  esac
  [ -f "$AGGREGATE_VALIDATOR" ] ||
    die "forced rollback aggregate validator is missing"
  [ -f "$AGGREGATE_BUILDER" ] ||
    die "forced rollback aggregate builder is missing"
  [ -f "$ARTIFACT_STORE" ] ||
    die "forced rollback artifact store is missing"

  status="PASSED"
  if [ "$state" != "LEG2_APPLIED" ] ||
    [ "$(jq -er '.failures | length' "$STATE_FILE")" -ne 0 ]; then
    status="FAILED"
  fi
  if [ "$state" = "LEG2_APPLIED" ]; then
    final_receipt="$(jq -er '.legs.restore_active.apply.path' "$STATE_FILE")"
    plan_sha="$(jq -er '.legs.restore_active.plan.sha256' "$STATE_FILE")"
    validate_bound_file \
      "$final_receipt" \
      "$(jq -er '.legs.restore_active.apply.sha256' "$STATE_FILE")" \
      "final restore apply receipt"
    if ! validate_apply_receipt restore-active "$final_receipt" "$plan_sha"; then
      record_failure restore-active finalize "final new live/state evidence mismatch"
      status="RECONCILE_REQUIRED"
    fi
    [ "$(jq -er '.legs.restore_active.apply.post_target_sha256' "$STATE_FILE")" = \
      "$(jq -er '.target_sha256.new' "$STATE_FILE")" ] ||
      {
        record_failure restore-active finalize "final target differs from initial new"
        status="RECONCILE_REQUIRED"
      }
    terminal_raw="$DRILL_DIR/final-live.snapshot.hcl"
    terminal_full="$DRILL_DIR/final-live.snapshot.full.json"
    terminal_json="$DRILL_DIR/final-live.snapshot.json"
    [ ! -e "$terminal_raw" ] && [ ! -L "$terminal_raw" ] &&
      [ ! -e "$terminal_full" ] && [ ! -L "$terminal_full" ] &&
      [ ! -e "$terminal_json" ] && [ ! -L "$terminal_json" ] ||
      die "final live snapshot evidence already exists"
    if ! bash "$TERRAFORM_RUNTIME_GUARD" snapshot \
      --evidence-json-out "$terminal_full" > "$terminal_raw"; then
      record_failure restore-active finalize "fresh final live snapshot failed"
      terminal_ok="false"
    else
      chmod 600 "$terminal_raw" "$terminal_full"
      now="$(date +%s)"
      if ! validate_terminal_live_snapshot \
        "$terminal_raw" "$terminal_full" "$(target_file old)" \
        "$(target_file new)" "$terminal_json" "$now"; then
        record_failure restore-active finalize \
          "fresh final live snapshot differs from initial new"
        terminal_ok="false"
      fi
    fi
    if [ "$terminal_ok" != "true" ]; then
      status="RECONCILE_REQUIRED"
    fi
  else
    status="RECONCILE_REQUIRED"
  fi
  if [ "$(jq -er '.failures | length' "$STATE_FILE")" -ne 0 ] &&
    [ "$status" = "PASSED" ]; then
    status="FAILED"
  fi

  aggregate_stage="$DRILL_DIR/.aggregate.unvalidated.json"
  aggregate_located="$DRILL_DIR/.aggregate.located.json"
  aggregate_out="$DRILL_DIR/aggregate.json"
  validated_out="$DRILL_DIR/.aggregate.validated.json"
  trusted_scope="$DRILL_DIR/.trusted-scope.json"
  artifact_manifest="$DRILL_DIR/.aggregate-artifact-manifest.json"
  artifact_directory="$DRILL_DIR/aggregate-artifacts"
  artifact_locators="$DRILL_DIR/.aggregate-artifact-locators.json"
  drill_id="$(jq -er '.drill_id' "$STATE_FILE")"
  [ ! -e "$aggregate_out" ] && [ ! -L "$aggregate_out" ] ||
    die "aggregate evidence already exists"
  rm -f \
    "$aggregate_stage" "$aggregate_located" "$validated_out" "$trusted_scope" \
    "$artifact_manifest" "$artifact_locators"
  [ ! -e "$artifact_directory" ] && [ ! -L "$artifact_directory" ] ||
    die "aggregate artifact staging directory already exists"
  if ! build_trusted_scope "$trusted_scope"; then
    rm -f "$trusted_scope"
    die "could not derive trusted scope from registry and initial receipt"
  fi
  if ! prepare_aggregate_artifacts \
    "$artifact_manifest" "$artifact_directory" "$trusted_scope"; then
    die "could not prepare source-bound aggregate artifacts"
  fi
  if ! persist_aggregate_artifacts "$artifact_manifest" "$artifact_locators"; then
    die "could not persist and verify aggregate source artifacts"
  fi
  if ! build_aggregate \
    "$status" "$aggregate_stage" "$trusted_scope" "$artifact_locators"; then
    die "could not build the exact aggregate from persisted source artifacts"
  fi
  if ! validate_aggregate \
    "$aggregate_stage" "$validated_out" "$trusted_scope" body-only; then
    rm -f "$aggregate_stage" "$validated_out" "$trusted_scope"
    die "aggregate body failed pre-persistence validation"
  fi
  if ! persist_aggregate "$aggregate_stage" "$aggregate_located" "$drill_id"; then
    rm -f "$aggregate_stage" "$aggregate_located" "$trusted_scope"
    die "could not persist and verify immutable aggregate evidence"
  fi
  if ! validate_aggregate "$aggregate_located" "$validated_out" "$trusted_scope"; then
    rm -f \
      "$aggregate_stage" "$aggregate_located" "$validated_out" "$trusted_scope"
    die "validate_drill_evidence rejected the aggregate evidence"
  fi
  [ "$(jq -er '.status' "$validated_out")" = "$status" ] ||
    {
      rm -f \
        "$aggregate_stage" "$aggregate_located" "$validated_out" "$trusted_scope"
      die "aggregate validator changed the drill status"
    }
  if [ "$(jq -er '.failures | length' "$STATE_FILE")" -ne 0 ]; then
    [ "$(jq -er '.status' "$validated_out")" != "PASSED" ] ||
      {
        rm -f \
          "$aggregate_stage" "$aggregate_located" "$validated_out" "$trusted_scope"
        die "a drill with a failed leg cannot be finalized as PASSED"
      }
  fi
  mv "$validated_out" "$aggregate_out"
  rm -f "$aggregate_stage" "$aggregate_located" "$trusted_scope"
  chmod 600 "$aggregate_out"
  final_sha="$(sha256_file "$aggregate_out")"
  now="$(date +%s)"
  replace_state \
    --arg status "$status" \
    --arg sha "$final_sha" \
    --argjson now "$now" '
    .state = "FINALIZED" |
    .updated_at_epoch = $now |
    .final_status = $status |
    .aggregate_sha256 = $sha
  '
  echo "$status aggregate_sha256=$final_sha"
  [ "$status" = "PASSED" ] || exit 1
}

for tool in jq python3 sed awk mktemp; do
  need_cmd "$tool"
done

COMMAND="${1:-}"
case "$COMMAND" in
  "") usage >&2; die "a subcommand is required" ;;
  -h|--help) usage; exit 0 ;;
  prepare|preflight|plan-leg|apply-leg|finalize) shift ;;
  *) usage >&2; die "unknown subcommand: $COMMAND" ;;
esac

case "$COMMAND" in
  prepare) prepare_command "$@" ;;
  preflight) preflight_command "$@" ;;
  plan-leg) plan_leg_command "$@" ;;
  apply-leg) apply_leg_command "$@" ;;
  finalize) finalize_command "$@" ;;
esac
