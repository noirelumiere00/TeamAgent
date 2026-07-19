"""Static completeness gates for HMAC issuers, verifiers, services, and rollout bypasses."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TF_ROOT = ROOT / "infra" / "terraform"

_TOKEN_CALLS = {
    "encode_draft_token",
    "decode_draft_token",
    "encode_event_token",
    "decode_event_token",
    "encode_report_token",
    "decode_report_token",
}
_EXPECTED_TOKEN_CALLERS = {
    "src/teamagent/runtime/slack_bot.py": {"decode_draft_token"},
    "src/teamagent/connect_web/app.py": {"decode_report_token"},
    "src/teamagent/skills/calendar_event/skill.py": {"decode_event_token"},
    "src/teamagent/skills/schedule_propose/skill.py": {"decode_draft_token"},
    "src/teamagent/skills/morning_digest/skill.py": {
        "encode_draft_token",
        "encode_event_token",
    },
    "src/teamagent/skills/_shared/report_delivery.py": {"encode_report_token"},
    "src/teamagent/skills/mail_draft/skill.py": {"decode_draft_token"},
}


def _terraform_block(path: Path, resource_type: str, resource_name: str) -> str:
    body = path.read_text(encoding="utf-8")
    marker = f'{resource_type} "{resource_name}"'
    start = body.index(marker)
    opening = body.index("{", start)
    depth = 0
    for index in range(opening, len(body)):
        if body[index] == "{":
            depth += 1
        elif body[index] == "}":
            depth -= 1
            if depth == 0:
                return body[start : index + 1]
    raise AssertionError(f"unterminated Terraform block: {path.name}:{resource_name}")


def _token_callers() -> dict[str, set[str]]:
    callers: dict[str, set[str]] = {}
    for base in (ROOT / "src" / "teamagent", ROOT / "scripts"):
        for path in base.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            calls: set[str] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                name = (
                    function.id
                    if isinstance(function, ast.Name)
                    else function.attr
                    if isinstance(function, ast.Attribute)
                    else None
                )
                if name in _TOKEN_CALLS:
                    calls.add(name)
            if calls:
                callers[str(path.relative_to(ROOT))] = calls
    return callers


def test_discovers_every_token_issuer_and_verifier_callsite() -> None:
    assert _token_callers() == _EXPECTED_TOKEN_CALLERS


def test_only_required_ecs_tasks_receive_each_hmac_domain() -> None:
    expected = {
        ("fargate.tf", "mcp"): {
            "local.mail_action_hmac_environment",
            "local.mail_action_hmac_secrets",
            "local.report_link_hmac_environment",
            "local.report_link_hmac_secrets",
            "local.mcp_hmac_runtime_environment",
            "local.mail_action_hmac_transition_valid",
            "local.report_link_hmac_transition_valid",
        },
        ("morning_digest_schedule.tf", "morning_digest"): {
            "local.mail_action_hmac_environment",
            "local.mail_action_hmac_secrets",
            "local.morning_digest_hmac_runtime_environment",
            "local.mail_action_hmac_transition_valid",
        },
        ("connect_web.tf", "connect_web"): {
            "local.report_link_hmac_environment",
            "local.report_link_hmac_secrets",
            "local.connect_web_hmac_runtime_environment",
            "local.report_link_hmac_transition_valid",
        },
    }
    discovered: dict[tuple[str, str], str] = {}
    marker = re.compile(r'resource "aws_ecs_task_definition" "([^"]+)"')
    for path in TF_ROOT.glob("*.tf"):
        body = path.read_text(encoding="utf-8")
        for match in marker.finditer(body):
            block = _terraform_block(path, 'resource "aws_ecs_task_definition"', match.group(1))
            if "_hmac_" in block:
                discovered[(path.name, match.group(1))] = block

    assert frozenset(discovered) == frozenset(expected)
    for task, required_references in expected.items():
        block = discovered[task]
        for reference in required_references:
            assert reference in block, f"{task} is missing {reference}"

    assert "mail_action_hmac" not in discovered[("connect_web.tf", "connect_web")]
    assert "report_link_hmac" not in discovered[("morning_digest_schedule.tf", "morning_digest")]


def test_hmac_secrets_are_dedicated_version_pinned_and_never_stored_in_state() -> None:
    hmac_tf = (TF_ROOT / "hmac_keyrings.tf").read_text(encoding="utf-8")
    rotation_tf = (TF_ROOT / "hmac_rotation.tf").read_text(encoding="utf-8")
    all_tf = "\n".join(path.read_text(encoding="utf-8") for path in sorted(TF_ROOT.glob("*.tf")))

    assert 'resource "aws_secretsmanager_secret" "mail_action_hmac"' not in all_tf
    assert 'resource "aws_secretsmanager_secret" "report_link_hmac"' not in all_tf
    assert 'variable "mail_action_hmac_secret_arn"' in rotation_tf
    assert 'variable "report_link_hmac_secret_arn"' in rotation_tf
    assert "${var.mail_action_hmac_secret_arn}:::" in hmac_tf
    assert "${var.report_link_hmac_secret_arn}:::" in hmac_tf
    assert "aws_secretsmanager_secret_version" not in hmac_tf
    assert "secret_string" not in hmac_tf
    assert 'output "' not in hmac_tf
    assert 'name      = "MAIL_ACTION_HMAC_SECRET"' in hmac_tf
    assert 'name      = "REPORT_LINK_HMAC_SECRET"' in hmac_tf
    assert 'name  = "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY"' in hmac_tf
    assert 'name  = "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY"' in hmac_tf
    assert 'variable "hmac_legacy_slack_bot_version_id"' in hmac_tf
    assert 'name  = "MAIL_ACTION_HMAC_LEGACY_WORKER_GENERATION"' in hmac_tf
    assert 'name      = "MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET"' in hmac_tf
    assert ":::${var.hmac_legacy_slack_bot_version_id}" in hmac_tf
    assert hmac_tf.count('== "legacy_migration" ? [') >= 2
    assert ":::${var.mail_action_hmac_primary_version_id}" in hmac_tf
    assert ":::${var.report_link_hmac_primary_version_id}" in hmac_tf
    assert "hmac_preflight_epoch_valid" in hmac_tf
    assert "floor(var.mail_action_hmac_ttl_s) == var.mail_action_hmac_ttl_s" in hmac_tf
    assert "floor(var.report_link_hmac_ttl_s) == var.report_link_hmac_ttl_s" in hmac_tf
    assert "can(regex(local.hmac_t0_pattern, var.hmac_preflight_epoch_s))" in hmac_tf
    assert hmac_tf.count('var.mail_action_hmac_rotation_started_at == ""') >= 1
    assert hmac_tf.count('var.report_link_hmac_rotation_started_at == ""') >= 1
    assert (
        "var.mail_action_hmac_deployed_previous_generation\n"
        "      != var.mail_action_hmac_deployed_primary_generation"
    ) in hmac_tf
    assert (
        "var.report_link_hmac_deployed_previous_generation\n"
        "      != var.report_link_hmac_deployed_primary_generation"
    ) in hmac_tf
    assert "local.hmac_max_epoch_s - 900 - 86400" in hmac_tf
    assert "local.hmac_max_epoch_s - 900 - 604800" in hmac_tf
    assert 'resource "aws_dynamodb_table" "hmac_state"' in hmac_tf
    assert "point_in_time_recovery" in hmac_tf
    assert 'attribute_name = "expires_at"' in hmac_tf
    assert "deletion_protection_enabled = true" in hmac_tf
    assert 'resource "terraform_data" "hmac_live_task_gate"' in hmac_tf
    assert "timestamp()" in hmac_tf
    assert "terraform_hmac_gate.py" in hmac_tf
    assert 'variable "hmac_gate_mode"' in hmac_tf
    assert "HMAC_GATE_MODE" in hmac_tf
    for field in (
        "mail_primary",
        "mail_previous",
        "mail_t0",
        "report_primary",
        "report_previous",
        "report_t0",
    ):
        assert field in hmac_tf

    database_primary = re.compile(
        r'name\s*=\s*"(?:MAIL_ACTION|REPORT_LINK)_HMAC_SECRET"'
        r"[^\n]*data\.aws_secretsmanager_secret\.database_url\.arn"
    )
    assert database_primary.search(all_tf) is None


def test_execution_roles_have_only_the_hmac_domains_their_tasks_need() -> None:
    mcp_policy = _terraform_block(
        TF_ROOT / "fargate.tf",
        'data "aws_iam_policy_document"',
        "ecs_execution_mcp_secrets",
    )
    connect_policy = _terraform_block(
        TF_ROOT / "connect_web.tf",
        'data "aws_iam_policy_document"',
        "ecs_execution_connect_web_secrets",
    )
    digest_policy = _terraform_block(
        TF_ROOT / "morning_digest_schedule.tf",
        'data "aws_iam_policy_document"',
        "ecs_execution_morning_digest_secrets",
    )
    worker_policy = _terraform_block(
        TF_ROOT / "worker.tf",
        'data "aws_iam_policy_document"',
        "worker_app",
    )
    runtime_policies = (
        _terraform_block(
            TF_ROOT / "fargate.tf",
            'data "aws_iam_policy_document"',
            "mcp_task",
        ),
        _terraform_block(
            TF_ROOT / "connect_web.tf",
            'data "aws_iam_policy_document"',
            "connect_web_task",
        ),
        _terraform_block(
            TF_ROOT / "morning_digest_schedule.tf",
            'data "aws_iam_policy_document"',
            "morning_digest_task",
        ),
        worker_policy,
    )

    assert "local.hmac_secret_iam_arns" in mcp_policy
    assert "local.hmac_report_secret_iam_arns" in connect_policy
    assert "local.hmac_mail_secret_iam_arns" not in connect_policy
    assert "local.hmac_mail_secret_iam_arns" in digest_policy
    assert "local.hmac_report_secret_iam_arns" not in digest_policy
    assert "local.hmac_secret_iam_arns" in worker_policy
    assert "data.aws_secretsmanager_secret.slack_bot.arn" in worker_policy
    assert "${var.project_name}/${var.environment}/*" not in worker_policy
    for policy in runtime_policies:
        assert "aws_dynamodb_table.hmac_state.arn" in policy
        assert "dynamodb:GetItem" in policy
        assert "dynamodb:UpdateItem" in policy
    assert "dynamodb:TransactWriteItems" in worker_policy


def test_full_saved_plan_owns_candidate_rollback_worker_and_event_mutations() -> None:
    hmac_tf = (TF_ROOT / "hmac_keyrings.tf").read_text(encoding="utf-8")
    promotion = (TF_ROOT / "hmac_runtime_promotion.tf").read_text(encoding="utf-8")
    worker = (TF_ROOT / "hmac_worker_deploy.tf").read_text(encoding="utf-8")
    image_gate = (TF_ROOT / "image_release_gate.tf").read_text(encoding="utf-8")
    fargate = (TF_ROOT / "fargate.tf").read_text(encoding="utf-8")
    connect = (TF_ROOT / "connect_web.tf").read_text(encoding="utf-8")
    morning = (TF_ROOT / "morning_digest_schedule.tf").read_text(encoding="utf-8")

    assert 'contains(["candidate", "cleanup", "rollback"], var.hmac_gate_mode)' in hmac_tf
    assert "hmac_rollback_gate_ready" in hmac_tf
    assert "hmac_rollback_task_definition_arns" in hmac_tf
    assert "hmac_release_intent_bindings" in hmac_tf
    assert "hmac_runtime_promotion_tasks" in hmac_tf
    assert "worker_provenance_key_arn" in hmac_tf
    assert "hmac_release_bindings" in image_gate
    assert "local.hmac_promoted_task_definition_arns.mcp" in fargate
    assert "local.hmac_promoted_task_definition_arns.connect_web" in connect

    assert "TEAMAGENT_HMAC_PROMOTION_FROM_TERRAFORM" in promotion
    assert 'HMAC_GATE_ACTION         = "event-transaction"' in promotion
    assert "Input   = jsonencode({})" in promotion
    assert "RetryPolicy" in promotion
    assert "NetworkConfiguration" in promotion
    assert "terraform_data.production_image_release_gate" in promotion
    assert "aws_cloudwatch_event_rule.morning_digest_weekday" in promotion
    assert 'contains(var.hmac_runtime_promotion_tasks, "mcp")' in promotion
    assert 'contains(var.hmac_runtime_promotion_tasks, "connect_web")' in promotion
    assert 'contains(var.hmac_runtime_promotion_tasks, "morning_digest")' in promotion

    assert 'resource "aws_cloudwatch_event_target" "morning_digest_run_task"' in morning
    assert "terraform_data.runtime_guard" in morning
    assert "prevent_destroy = true" in morning
    assert "morning_digest_rule_enabled" in morning
    runtime_guard = (ROOT / "infra" / "deploy" / "terraform_runtime_guard.sh").read_text(
        encoding="utf-8"
    )
    assert "EVENTBRIDGE_APPLY_SAGA" in runtime_guard
    assert "--outcome failed" in runtime_guard
    assert "--outcome applied" in runtime_guard

    assert "TEAMAGENT_HMAC_DEPLOY_FROM_TERRAFORM" in worker
    assert "HMAC_WORKER_EXPECTED_HASHES" in worker
    assert "HMAC_CLEANUP_DOMAIN" in worker
    assert "var.hmac_worker_deploy_mode == var.hmac_gate_mode" in worker
    assert "candidate_artifact" in worker
    assert "rollback_artifact" in worker
    assert "aws_kms_key.mcp_source_publisher_signing.arn" in worker
    assert "terraform_data.production_image_release_gate" in worker
    assert "terraform_data.hmac_live_task_gate" in worker


def test_rollout_gate_policy_covers_exact_reconciliation_dependencies() -> None:
    policy = _terraform_block(
        TF_ROOT / "hmac_keyrings.tf",
        'data "aws_iam_policy_document"',
        "hmac_rollout_gate",
    )

    for action in (
        "events:DescribeRule",
        "events:ListTargetsByRule",
        "events:PutRule",
        "events:PutTargets",
        "events:RemoveTargets",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:TransactWriteItems",
        "dynamodb:UpdateItem",
        "kms:Verify",
    ):
        assert action in policy
    assert "aws_dynamodb_table.hmac_state.arn" in policy
    assert "aws_dynamodb_table.image_deployment_intents.arn" in policy
    assert "aws_kms_key.mcp_source_publisher_signing.arn" in policy
    assert '"intent#*"' in policy


def test_legacy_worker_and_direct_deploy_paths_cannot_bypass_preflight() -> None:
    worker = (TF_ROOT / "worker.tf").read_text(encoding="utf-8")
    loader = (ROOT / "scripts" / "load_secrets.sh").read_text(encoding="utf-8")
    connect_deploy = (ROOT / "infra" / "deploy" / "deploy_connectweb_unified.sh").read_text(
        encoding="utf-8"
    )
    resilience = (TF_ROOT / "apply_resilience.sh").read_text(encoding="utf-8")
    worker_deploy = (ROOT / "scripts" / "deploy_to_ec2.sh").read_text(encoding="utf-8")
    atomic_switch = (ROOT / "scripts" / "worker_atomic_release_switch.sh").read_text(
        encoding="utf-8"
    )
    promote = (ROOT / "infra" / "deploy" / "promote_hmac_task.sh").read_text(encoding="utf-8")
    preflight = (ROOT / "scripts" / "preflight_hmac_rotation.py").read_text(encoding="utf-8")
    live_gate = (ROOT / "scripts" / "hmac_rollout_gate.py").read_text(encoding="utf-8")
    terraform_gate = (ROOT / "scripts" / "terraform_hmac_gate.py").read_text(encoding="utf-8")
    hmac_tf = (TF_ROOT / "hmac_keyrings.tf").read_text(encoding="utf-8")
    assert (ROOT / "scripts" / "preflight_hmac_rotation.py").stat().st_mode & 0o111

    assert "only by the signed, saved-plan-bound atomic release flow" in worker
    assert "pip install" not in worker
    assert "npm install" not in worker
    assert "npx " not in worker
    assert "local.mail_action_hmac_transition_valid" in worker
    assert "local.report_link_hmac_transition_valid" in worker
    assert "_get_secret_version" in loader
    assert "_load_hmac_keyring MAIL_ACTION && _load_hmac_keyring REPORT_LINK" in loader
    assert "TEAMAGENT_HMAC_REQUIRED_DOMAINS" in loader
    assert "primary cannot be a database credential secret" in loader
    assert "legacy previous must be the pinned database-url secret" in loader
    assert "legacy worker key must be the pinned Slack bot secret" in loader
    assert "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY" in hmac_tf
    assert "MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET_NAME" in preflight
    assert "MAIL_ACTION_HMAC_LEGACY_WORKER_VERSION_ID" in preflight
    assert "MAIL_ACTION_HMAC_LEGACY_WORKER_GENERATION" in hmac_tf
    assert "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY" in hmac_tf
    assert '"worker": frozenset({"mail_action", "report_link"})' in preflight

    assert "permanently disabled" in connect_deploy
    assert "build_teamagent_image.sh" in connect_deploy
    assert "authorize_image_release.sh" in connect_deploy
    assert "terraform/README.md" in connect_deploy
    assert "aws codebuild start-build" not in connect_deploy
    assert "aws s3 cp" not in connect_deploy
    assert "force-new-deployment" not in connect_deploy
    assert "aws ecs register-task-definition" not in connect_deploy
    assert "aws ecs update-service" not in connect_deploy
    assert "exit 64" in connect_deploy
    assert "aws_ecs_task_definition.mcp" not in resilience
    assert "aws_ecs_task_definition.canary" not in resilience
    assert "targeted plans/applies could bypass the production image gate" in resilience
    assert "single guarded full" in resilience
    assert "infra/terraform/README.md" in resilience
    assert "exit 64" in resilience
    assert worker_deploy.index("HMAC_PREFLIGHT_MANIFEST") < worker_deploy.index(
        "aws s3api put-object"
    )
    assert worker_deploy.index("--worker-env") < worker_deploy.index("aws s3api put-object")
    assert worker_deploy.count("source /opt/teamagent/current/hmac.env") >= 2
    assert "source scripts/load_secrets.sh MAIL_ACTION,REPORT_LINK" in worker_deploy
    assert "source scripts/load_secrets.sh REPORT_LINK" in worker_deploy
    assert "Environment=TEAMAGENT_HMAC_REQUIRED_DOMAINS" not in worker
    assert "Environment=TEAMAGENT_HMAC_REQUIRED_DOMAINS" not in worker_deploy
    assert worker_deploy.index("--action pre-worker-upload") < worker_deploy.index(
        "aws s3api put-object"
    )
    assert worker_deploy.index("verify-worker-bindings") < worker_deploy.index(
        "aws s3api put-object"
    )
    assert worker_deploy.index("--action pre-restart") < worker_deploy.index(
        'RESTART_CID="$(aws ssm send-command'
    )
    assert "HMAC_WORKER_MODE" in worker_deploy
    assert "HMAC_WORKER_ROLLBACK_ENV" in worker_deploy
    assert 'SELECTED_WORKER_ARTIFACT="$HMAC_WORKER_ROLLBACK_ARTIFACT"' in worker_deploy
    assert 'cp "$SELECTED_WORKER_ARTIFACT"' in worker_deploy
    assert "git archive" not in worker_deploy
    assert "verify_worker_bundle_provenance.py" in worker_deploy
    assert "render_ec2_base_env.py" in worker_deploy
    assert "measure_worker_release.py" in worker_deploy
    assert "worker_promotion_attest.sh" in worker_deploy
    assert "--require-hashes --only-binary=:all:" in worker_deploy
    assert "npm ci --ignore-scripts" in worker_deploy
    assert "npx " not in worker_deploy
    assert "aws s3api put-object" in worker_deploy
    assert "--version-id" in worker_deploy
    assert "RELEASE_ROOT=/opt/teamagent/releases" in worker_deploy
    assert 'FINAL_RELEASE="$RELEASE_ROOT/$RELEASE_TREE_DIGEST"' in worker_deploy
    assert "worker_atomic_release_switch.sh" in worker_deploy
    assert 'mv -Tf "$INSTALL_ROOT/.current-new-$$" "$CURRENT_LINK"' in atomic_switch
    assert 'mv -Tf "$INSTALL_ROOT/.current-rollback-$$" "$CURRENT_LINK"' in atomic_switch
    assert "restore_transaction" in atomic_switch
    assert "release-transactions" in atomic_switch
    assert "snapshot_units" in atomic_switch
    assert "restore_units" in atomic_switch
    assert "--query StandardOutputContent" in worker_deploy
    assert "StandardErrorContent" not in worker_deploy
    assert "list_secret_version_ids" in live_gate
    assert "get_secret_value" not in live_gate.casefold()
    assert "transact_write_items" in live_gate
    assert "secret_reference_unpinned" in live_gate
    assert "legacy_task_definition" in live_gate
    assert "def retire_previous(" in live_gate
    assert '"post-update"' in live_gate
    assert "def _full_task_inventory(" in live_gate
    assert 'desired_status="RUNNING"' in live_gate
    assert 'desired_status="STOPPED"' in live_gate
    assert "task_inventory_count_drift" in live_gate
    assert "def prepare_cleanup(" in live_gate
    assert "def complete_cleanup(" in live_gate
    assert "cleanup_mode_required" in live_gate
    assert "time.monotonic()" in live_gate
    assert "gate_client_error" in live_gate
    assert '{"code":"gate_client_error","ok":false}' in terraform_gate
    assert "gate.terraform_pre_register(" in live_gate
    assert "config_digest" in live_gate
    assert worker_deploy.index("--action post-restart") > worker_deploy.index(
        'RESTART_CID="$(aws ssm send-command'
    )
    assert 'if [[ -z "${RESTART_CID:-}" || "$RESTART_CID" == "None" ]]' in worker_deploy
    assert 'if [[ "$RESTART_STATUS" != "Success" ]]' in worker_deploy
    assert '"TimedOut"' in worker_deploy
    assert '"Cancelled"' in worker_deploy
    assert "reconcile_restart_rollback" in worker_deploy
    assert "--action reconcile-restart" in worker_deploy
    assert "RELEASE_TRANSACTION_STATUS_JSON" in worker_deploy
    assert '"current":"new","status":"ready"' in worker_deploy
    assert "systemctl is-active --quiet teamagent-bot" in atomic_switch
    assert "systemctl is-active --quiet teamagent-connect" in atomic_switch
    assert 'grep -F "pid=$CONNECT_MAIN_PID,"' in atomic_switch
    assert "curl -fsS http://127.0.0.1:8788/healthz" in atomic_switch
    assert "fresh_attestation=true" in worker_deploy
    assert "permanently disabled" in promote
    assert "plan_image_release.sh" in promote
    assert "apply_image_release_plan.sh" in promote
    assert "aws ecs update-service" not in promote
    assert "aws events put-targets" not in promote
    assert "force-new-deployment" not in promote
    assert "exit 64" in promote


def test_live_connect_and_canary_anchors_remain_documented_and_canary_unmodified() -> None:
    deploy_log = (ROOT / "infra" / "deploy_log.md").read_text(encoding="utf-8")
    runbook = (ROOT / "docs" / "runbooks" / "hmac_domain_migration.md").read_text(encoding="utf-8")
    canary = _terraform_block(
        TF_ROOT / "canary_schedule.tf",
        'resource "aws_ecs_task_definition"',
        "canary",
    )

    assert "task definition `:50`→`:53`" in deploy_log
    assert "task definition `:13`→`:14`" in deploy_log
    assert "rollbackはconnect-web`:50`、canary`:13`" in deploy_log
    assert "HMAC" not in canary
    assert "Use a short issuance/action maintenance window" in runbook
    assert runbook.index("Deploy the live legacy worker first") < runbook.index(
        "Register and promote one MCP revision containing both complete keyrings"
    )
    assert "every old MCP task is drained" in runbook
    assert "Do not infer it from the secret's current `AWSCURRENT`" in runbook
    assert "scripts/hmac_rollout_gate.py" in runbook
    assert "--action prepare-cleanup" in runbook
    assert "--action complete-cleanup" in runbook
    assert "`cleanup_staging_required`" in runbook
    assert "--action reconcile-cleanup" in runbook
    assert "--reconcile-decision rebind" in runbook
    assert "--reconcile-decision abort" in runbook
    assert "HMAC_WORKER_MODE=rollback" in runbook
    assert "exact approved rollback artifact" in runbook
    assert "canary `:14`" in runbook
