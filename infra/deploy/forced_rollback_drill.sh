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

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
AUTHORIZE_IMAGE_RELEASE="$SCRIPT_DIR/authorize_image_release.sh"
TERRAFORM_RUNTIME_GUARD="$SCRIPT_DIR/terraform_runtime_guard.sh"
CONSUMER_REGISTRY="$REPO_ROOT/infra/codebuild/image_deployment_consumers.json"
AGGREGATE_VALIDATOR="$REPO_ROOT/infra/codebuild/forced_rollback_drill_evidence.py"

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
          elif $owner.activator.type == "eventbridge_rule_ecs_target" then
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
        (.plan_sha256 | sha) and
        (.post_target_sha256 | sha) and
        (.apply_attempt_id | test(
          "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )) and
        (.terraform_lineage | type == "string" and length > 0) and
        (.post_serial | type == "number" and . >= 0 and floor == .) and
        (.started_at_epoch | type == "number" and . >= 0 and floor == .) and
        (.applied_at_epoch |
          type == "number" and . >= $apply.started_at_epoch and floor == .)
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
  validate_bound_file \
    "$DRILL_DIR/inputs/terraform.tfvars.json" \
    "$(jq -er '.var_file_sha256' "$STATE_FILE")" \
    "base var-file"
  [ "$(jq -er '.drill_id' "$DRILL_DIR/contract.json")" = \
    "$(jq -er '.drill_id' "$STATE_FILE")" ] ||
    die "drill ID differs between contract and state"
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
  local receipt_now
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
  plan="$leg_path/plan.tfplan"
  receipt="$leg_path/plan.runtime-guard.json"
  for output in \
    "$stdout_file" "$gate_vars" "$merged_var" "$authorization" "$plan" "$receipt"; do
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
    )" > "$stdout_file"; then
    record_failure "$leg" authorization "fresh release authorization failed"
    die "fresh $channel release authorization failed"
  fi
  chmod 600 "$stdout_file" "$gate_vars"
  merge_authorized_var_file "$gate_vars" "$merged_var"
  authorization_sha="$(sha256_file "$gate_vars")"
  jq -n -S -c \
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
    --argjson issued_at_epoch "$(date +%s)" '{
      pipeline:$pipeline,
      channel:$channel,
      receipt_key:$receipt_key,
      receipt_version_id:$receipt_version_id,
      receipt_signature_key:$receipt_signature_key,
      receipt_signature_version_id:$receipt_signature_version_id,
      gate_var_sha256:$gate_var_sha256,
      issued_at_epoch:$issued_at_epoch
    }' > "$authorization"
  chmod 600 "$authorization"
  if [ "$leg" = "restore-active" ]; then
    jq -e --slurpfile previous \
      "$DRILL_DIR/legs/rollback-to-previous/authorization.json" '
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
    .post_apply_service_probe.task.exit_code == 0 and
    ([.post_apply_service_probe.result.checks[]] | all(. == true)) and
    .openclaw_rollout_result.passed == true and
    .openclaw_rollout_result.applyAttemptId == .apply_attempt_id and
    .ecs_service_saga_verification_receipt.stage == "VERIFIED_APPLIED" and
    .ecs_service_saga_verification_receipt.apply_attempt_id ==
      .apply_attempt_id and
    .ecs_service_saga_verification_receipt.plan_sha256 == .plan_sha256 and
    .deployment_finalization_receipt.state == "APPLIED" and
    .deployment_finalization_receipt.apply_attempt_id == .apply_attempt_id and
    .deployment_finalization_receipt.plan_sha256 == .plan_sha256 and
    (.post_apply_service_probe.result.checks |
      type == "array" and length > 0 and all(.[]; . == true))
  ' "$receipt" >/dev/null; then
    rm -f "$resources"
    return 1
  fi
  rm -f "$resources"
}

apply_leg_command() {
  local requested_dir="" leg="" expected state_key approval_id approval_action
  local drill_id leg_path plan receipt apply_receipt plan_sha expected_text supplied
  local now started applied_epoch post_serial post_lineage plan_serial
  local attempt target_sha next_state
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
  [ ! -e "$apply_receipt" ] && [ ! -L "$apply_receipt" ] ||
    die "apply receipt already exists; this plan cannot be executed again"
  started="$now"
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
    }
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
  if ! bash "$TERRAFORM_RUNTIME_GUARD" apply \
    --plan "$plan" \
    --receipt "$receipt" \
    --out "$apply_receipt"; then
    record_failure "$leg" apply "guarded apply or pre-finalization QA failed"
    die "guarded apply failed; the plan must not be retried"
  fi
  chmod 600 "$apply_receipt"
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
    --arg plan_sha "$plan_sha" \
    --arg attempt "$attempt" \
    --arg lineage "$post_lineage" \
    --arg target_sha "$target_sha" \
    --argjson post_serial "$post_serial" \
    --argjson started "$started" \
    --argjson applied "$applied_epoch" \
    --argjson dwell_deadline "$((started + MAX_OLD_DWELL_SECONDS))" \
    --argjson now "$now" '
    .state = $next_state |
    .updated_at_epoch = $now |
    .legs[$key].status = "APPLIED" |
    .legs[$key].apply = {
      path:$receipt_path,
      sha256:$receipt_sha,
      plan_sha256:$plan_sha,
      apply_attempt_id:$attempt,
      terraform_lineage:$lineage,
      post_serial:$post_serial,
      post_target_sha256:$target_sha,
      started_at_epoch:$started,
      applied_at_epoch:$applied
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

epoch_to_utc() {
  python3 - "$1" <<'PY'
import datetime
import sys

value = datetime.datetime.fromtimestamp(int(sys.argv[1]), tz=datetime.timezone.utc)
print(value.isoformat(timespec="seconds").replace("+00:00", "Z"))
PY
}

validate_terminal_live_snapshot() {
  local raw="$1" target="$2" output="$3" observed_at="$4"
  python3 - "$raw" "$target" "$output" "$observed_at" <<'PY'
import json
import os
import sys

raw_path, target_path, output_path, observed_at = sys.argv[1:]
with open(target_path, encoding="utf-8") as handle:
    target = json.load(handle)

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

expected_images = {
    field: target["images"][image_key]
    for field, image_key in image_fields.items()
}
if any(values.get(field) != value for field, value in expected_images.items()):
    raise SystemExit("terminal live images differ from initial new")

expected_activations = {}
for resource in target["resources"]:
    field = activation_fields.get(resource["consumer_id"])
    if field is None:
        continue
    state = resource["activation"]["state"]
    if resource["activation"]["type"] == "ecs_service":
        state = state > 0
    elif resource["activation"]["type"] == "eventbridge_rule_ecs_target":
        state = state == "ENABLED"
    expected_activations[field] = state
if any(
    values.get(field) != expected
    for field, expected in expected_activations.items()
):
    raise SystemExit("terminal live activations differ from initial new")

evidence = {
    "kind": "teamagent-forced-rollback-terminal-live-snapshot",
    "observed_at_epoch": int(observed_at),
    "images": {
        image_key: values[field]
        for field, image_key in image_fields.items()
    },
    "activations": {
        field: values[field]
        for field in sorted(expected_activations)
    },
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
PY
}

build_aggregate() {
  local status="$1" output="$2" state="$STATE_FILE"
  local leg1_plan leg2_plan leg1_apply leg2_apply
  local leg1_started leg1_completed leg2_started leg2_completed
  leg1_plan="$DRILL_DIR/legs/rollback-to-previous/plan.runtime-guard.json"
  leg2_plan="$DRILL_DIR/legs/restore-active/plan.runtime-guard.json"
  leg1_apply="$DRILL_DIR/legs/rollback-to-previous/apply.runtime-guard.json"
  leg2_apply="$DRILL_DIR/legs/restore-active/apply.runtime-guard.json"
  [ -f "$leg1_plan" ] || leg1_plan="/dev/null"
  [ -f "$leg2_plan" ] || leg2_plan="/dev/null"
  [ -f "$leg1_apply" ] || leg1_apply="/dev/null"
  [ -f "$leg2_apply" ] || leg2_apply="/dev/null"
  leg1_started="$(jq -r '.legs.rollback_to_previous.apply.started_at_epoch // .updated_at_epoch' "$state")"
  leg1_completed="$(jq -r '.legs.rollback_to_previous.apply.applied_at_epoch // .updated_at_epoch' "$state")"
  leg2_started="$(jq -r '.legs.restore_active.apply.started_at_epoch // .updated_at_epoch' "$state")"
  leg2_completed="$(jq -r '.legs.restore_active.apply.applied_at_epoch // .updated_at_epoch' "$state")"
  jq -n -S \
    --argjson schema_version "$SCHEMA_VERSION" \
    --arg drill_id "$(jq -er '.drill_id' "$state")" \
    --arg status "$status" \
    --arg git_commit "$(jq -er '.control.git_commit' "$DRILL_DIR/contract.json")" \
    --arg contract_sha "$(jq -er '.contract_sha256' "$state")" \
    --arg initial_verified "$(
      epoch_to_utc "$(jq -er '.initial_release_verified_at_epoch' "$state")"
    )" \
    --arg started "$(epoch_to_utc "$leg1_started")" \
    --arg completed "$(epoch_to_utc "$leg2_completed")" \
    --arg leg1_started "$(epoch_to_utc "$leg1_started")" \
    --arg leg1_completed "$(epoch_to_utc "$leg1_completed")" \
    --arg leg2_started "$(epoch_to_utc "$leg2_started")" \
    --arg leg2_completed "$(epoch_to_utc "$leg2_completed")" \
    --arg lineage "$(jq -er '.initial_state.lineage' "$state")" \
    --argjson initial_serial "$(jq -er '.initial_state.serial' "$state")" \
    --arg terminal_verified "$(epoch_to_utc "$(date +%s)")" \
    --arg terminal_classification "$(
      if [ -f "$DRILL_DIR/final-live.snapshot.json" ] &&
        jq -e '.legs.restore_active.apply != null' "$state" >/dev/null &&
        [ "$(jq -er '.legs.restore_active.apply.post_target_sha256 // ""' "$state")" = \
          "$(jq -er '.target_sha256.new' "$state")" ]; then
        printf INITIAL_NEW
      else
        printf UNKNOWN
      fi
    )" \
    --argjson terminal_steady "$(
      if [ "$status" = "RECONCILE_REQUIRED" ]; then printf false; else printf true; fi
    )" \
    --argjson leg1_result "$(
      if [ "$(jq -er '.legs.rollback_to_previous.status' "$state")" = "APPLIED" ]; then
        printf true
      else
        printf false
      fi
    )" \
    --argjson leg2_result "$(
      if [ "$(jq -er '.legs.restore_active.status' "$state")" = "APPLIED" ]; then
        printf true
      else
        printf false
      fi
    )" \
    --slurpfile contract "$DRILL_DIR/contract.json" \
    --slurpfile state "$state" \
    --slurpfile old "$(target_file old)" \
    --slurpfile new "$(target_file new)" \
    --slurpfile initial "$DRILL_DIR/inputs/initial-release.apply.json" \
    --slurpfile leg1_plan "$leg1_plan" \
    --slurpfile leg2_plan "$leg2_plan" \
    --slurpfile leg1_apply "$leg1_apply" \
    --slurpfile leg2_apply "$leg2_apply" '{
      schema_version:$schema_version,
      kind:"teamagent.forced-rollback-drill",
      drill_id:$drill_id,
      status:$status,
      environment:{
        account_id:"718959508629",
        region:"ap-northeast-1",
        name:"dev"
      },
      control:{
        git_commit:$git_commit,
        drill_contract_sha256:$contract_sha,
        initial_release_apply:$contract[0].control.initial_release_apply_locator,
        initial_release_verified_at_utc:$initial_verified,
        started_at_utc:$started,
        completed_at_utc:$completed,
        max_start_delay_seconds:1800,
        max_old_dwell_seconds:1200
      },
      actors:{
        initiating_principal:$contract[0].actors.initiating_principal,
        automation_principals:$contract[0].actors.automation_principals,
        approvals:[
          $state[0].legs.rollback_to_previous.approval,
          $state[0].legs.restore_active.approval
        ] | map(select(. != null))
      },
      scope:{
        pipelines:[$contract[0].pipeline],
        subjects:$new[0].subjects,
        resources:$new[0].resources
      },
      baseline:{
        terraform_lineage:$lineage,
        terraform_serial:$initial_serial,
        live_snapshot:$new[0],
        initial_new:$new[0],
        initial_new_verified:true
      },
      legs:[
        {
          ordinal:1,
          name:"rollback_to_previous",
          channel:"rollback",
          from:$new[0],
          to:$old[0],
          release_authorizations:[
            $state[0].legs.rollback_to_previous.authorization
          ],
          plan:(
            $state[0].legs.rollback_to_previous.plan +
            {receipt:$leg1_plan[0]}
          ),
          approval:$state[0].legs.rollback_to_previous.approval,
          apply:(
            ($state[0].legs.rollback_to_previous.apply // {}) +
            {passed:$leg1_result,receipt:($leg1_apply[0] // {})}
          ),
          ecs:{
            steady:$leg1_result,
            evidence:($leg1_apply[0].ecs_service_saga_verification_receipt // {})
          },
          run_task_health:{
            passed:$leg1_result,
            evidence:($leg1_apply[0].post_apply_service_probe // {})
          },
          dm_qa:{
            passed:$leg1_result,
            evidence:($leg1_apply[0].openclaw_rollout_result // {})
          },
          started_at_utc:$leg1_started,
          completed_at_utc:$leg1_completed,
          result:(if $leg1_result then "PASSED" else "FAILED" end),
          recovery:{
            attempted:($state[0].failures | length > 0),
            result:(
              if $terminal_classification == "INITIAL_NEW" then
                "INITIAL_NEW_VERIFIED"
              else
                "RECONCILIATION_REQUIRED"
              end
            ),
            last_exact_target:$terminal_classification
          }
        },
        {
          ordinal:2,
          name:"restore_active",
          channel:"active",
          from:$old[0],
          to:$new[0],
          release_authorizations:[
            $state[0].legs.restore_active.authorization
          ],
          plan:(
            $state[0].legs.restore_active.plan +
            {receipt:$leg2_plan[0]}
          ),
          approval:$state[0].legs.restore_active.approval,
          apply:(
            ($state[0].legs.restore_active.apply // {}) +
            {passed:$leg2_result,receipt:($leg2_apply[0] // {})}
          ),
          ecs:{
            steady:$leg2_result,
            evidence:($leg2_apply[0].ecs_service_saga_verification_receipt // {})
          },
          run_task_health:{
            passed:$leg2_result,
            evidence:($leg2_apply[0].post_apply_service_probe // {})
          },
          dm_qa:{
            passed:$leg2_result,
            evidence:($leg2_apply[0].openclaw_rollout_result // {})
          },
          started_at_utc:$leg2_started,
          completed_at_utc:$leg2_completed,
          result:(if $leg2_result then "PASSED" else "FAILED" end),
          recovery:{
            attempted:($state[0].failures | length > 0),
            result:(
              if $terminal_classification == "INITIAL_NEW" then
                "INITIAL_NEW_VERIFIED"
              else
                "RECONCILIATION_REQUIRED"
              end
            ),
            last_exact_target:$terminal_classification
          }
        }
      ],
      safe_terminal_state:{
        classification:$terminal_classification,
        steady:$terminal_steady,
        verified_at_utc:$terminal_verified,
        live_snapshot:(
          if $terminal_classification == "INITIAL_NEW" then $new[0]
          elif $terminal_classification == "PREVIOUS_OLD" then $old[0]
          else {}
          end
        )
      },
      artifact_manifest:$contract[0].evidence.artifact_manifest,
      integrity:{
        canonical_sha256:"",
        kms_key_arn:$contract[0].evidence.integrity.kms_key_arn,
        signing_algorithm:"RSASSA_PSS_SHA_256",
        signature:$contract[0].evidence.integrity.signature,
        immutable_object:$contract[0].evidence.integrity.immutable_object
      }
    }' > "$output"
  chmod 600 "$output"
}

validate_aggregate() {
  local source="$1" output="$2"
  PYTHONPATH="$REPO_ROOT/infra/codebuild${PYTHONPATH:+:$PYTHONPATH}" \
    python3 - "$AGGREGATE_VALIDATOR" "$source" "$output" <<'PY'
import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from teamagent_release_approval import canonical_json_bytes

helper_path = Path(sys.argv[1])
source_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])
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
body = copy.deepcopy(aggregate)
body["integrity"].pop("canonical_sha256", None)
body["integrity"].pop("signature", None)
body["integrity"].pop("immutable_object", None)
aggregate["integrity"]["canonical_sha256"] = hashlib.sha256(
    canonical_json_bytes(body)
).hexdigest()
validated = module.validate_drill_aggregate(aggregate)
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
  local aggregate_out validated_out final_sha now terminal_raw terminal_json
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
    terminal_json="$DRILL_DIR/final-live.snapshot.json"
    [ ! -e "$terminal_raw" ] && [ ! -L "$terminal_raw" ] &&
      [ ! -e "$terminal_json" ] && [ ! -L "$terminal_json" ] ||
      die "final live snapshot evidence already exists"
    if ! bash "$TERRAFORM_RUNTIME_GUARD" snapshot > "$terminal_raw"; then
      record_failure restore-active finalize "fresh final live snapshot failed"
      terminal_ok="false"
    else
      chmod 600 "$terminal_raw"
      now="$(date +%s)"
      if ! validate_terminal_live_snapshot \
        "$terminal_raw" "$(target_file new)" "$terminal_json" "$now"; then
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
  aggregate_out="$DRILL_DIR/aggregate.json"
  validated_out="$DRILL_DIR/.aggregate.validated.json"
  [ ! -e "$aggregate_out" ] && [ ! -L "$aggregate_out" ] ||
    die "aggregate evidence already exists"
  rm -f "$aggregate_stage" "$validated_out"
  build_aggregate "$status" "$aggregate_stage"
  if ! validate_aggregate "$aggregate_stage" "$validated_out"; then
    rm -f "$validated_out"
    die "validate_drill_aggregate rejected the aggregate evidence"
  fi
  [ "$(jq -er '.status' "$validated_out")" = "$status" ] ||
    {
      rm -f "$validated_out"
      die "aggregate validator changed the drill status"
    }
  if [ "$(jq -er '.failures | length' "$STATE_FILE")" -ne 0 ]; then
    [ "$(jq -er '.status' "$validated_out")" != "PASSED" ] ||
      {
        rm -f "$validated_out"
        die "a drill with a failed leg cannot be finalized as PASSED"
      }
  fi
  mv "$validated_out" "$aggregate_out"
  rm -f "$aggregate_stage"
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
