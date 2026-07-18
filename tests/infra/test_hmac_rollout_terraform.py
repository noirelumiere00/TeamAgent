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
            "local.mail_action_hmac_transition_valid",
            "local.report_link_hmac_transition_valid",
        },
        ("morning_digest_schedule.tf", "morning_digest"): {
            "local.mail_action_hmac_environment",
            "local.mail_action_hmac_secrets",
            "local.mail_action_hmac_transition_valid",
        },
        ("connect_web.tf", "connect_web"): {
            "local.report_link_hmac_environment",
            "local.report_link_hmac_secrets",
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
    all_tf = "\n".join(path.read_text(encoding="utf-8") for path in sorted(TF_ROOT.glob("*.tf")))

    assert 'resource "aws_secretsmanager_secret" "mail_action_hmac"' in hmac_tf
    assert 'resource "aws_secretsmanager_secret" "report_link_hmac"' in hmac_tf
    assert "aws_secretsmanager_secret_version" not in hmac_tf
    assert "secret_string" not in hmac_tf
    assert 'output "' not in hmac_tf
    assert 'name      = "MAIL_ACTION_HMAC_SECRET"' in hmac_tf
    assert 'name      = "REPORT_LINK_HMAC_SECRET"' in hmac_tf
    assert 'name  = "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY"' in hmac_tf
    assert 'name  = "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY"' in hmac_tf
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

    assert "aws_secretsmanager_secret.mail_action_hmac.arn" in mcp_policy
    assert "aws_secretsmanager_secret.report_link_hmac.arn" in mcp_policy
    assert "aws_secretsmanager_secret.report_link_hmac.arn" in connect_policy
    assert "aws_secretsmanager_secret.mail_action_hmac.arn" not in connect_policy
    assert "aws_secretsmanager_secret.mail_action_hmac.arn" in digest_policy
    assert "aws_secretsmanager_secret.report_link_hmac.arn" not in digest_policy
    assert "aws_secretsmanager_secret.mail_action_hmac.arn" in worker_policy
    assert "aws_secretsmanager_secret.report_link_hmac.arn" in worker_policy
    assert "data.aws_secretsmanager_secret.database_url.arn" in worker_policy
    assert "${var.project_name}/${var.environment}/*" not in worker_policy


def test_legacy_worker_and_direct_deploy_paths_cannot_bypass_preflight() -> None:
    worker = (TF_ROOT / "worker.tf").read_text(encoding="utf-8")
    loader = (ROOT / "scripts" / "load_secrets.sh").read_text(encoding="utf-8")
    connect_deploy = (ROOT / "infra" / "deploy" / "deploy_connectweb_unified.sh").read_text(
        encoding="utf-8"
    )
    resilience = (TF_ROOT / "apply_resilience.sh").read_text(encoding="utf-8")
    worker_deploy = (ROOT / "scripts" / "deploy_to_ec2.sh").read_text(encoding="utf-8")
    preflight = (ROOT / "scripts" / "preflight_hmac_rotation.py").read_text(encoding="utf-8")
    assert (ROOT / "scripts" / "preflight_hmac_rotation.py").stat().st_mode & 0o111

    assert "source /opt/teamagent/hmac.env; source scripts/load_secrets.sh" in worker
    assert worker.index("source /opt/teamagent/teamagent.env.base") < worker.index(
        "source /opt/teamagent/hmac.env"
    )
    assert "local.mail_action_hmac_transition_valid" in worker
    assert "local.report_link_hmac_transition_valid" in worker
    assert "_get_secret_version" in loader
    assert "_load_hmac_keyring MAIL_ACTION && _load_hmac_keyring REPORT_LINK" in loader
    assert "TEAMAGENT_HMAC_REQUIRED_DOMAINS" in loader
    assert "primary cannot be a database credential secret" in loader
    assert "legacy previous must be the pinned database-url secret" in loader
    assert "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY" in worker
    assert "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY" in worker
    assert '"worker": frozenset({"mail_action", "report_link"})' in preflight

    assert connect_deploy.index("HMAC_PREFLIGHT_MANIFEST") < connect_deploy.index(
        "aws codebuild start-build"
    )
    assert "--task-definition-json connect_web=/tmp/cwu_new.json" in connect_deploy
    assert connect_deploy.index("--task-definition-json") < connect_deploy.index(
        "aws ecs register-task-definition"
    )
    assert resilience.index("HMAC_PREFLIGHT_MANIFEST") < resilience.index("terraform plan")
    assert worker_deploy.index("HMAC_PREFLIGHT_MANIFEST") < worker_deploy.index("aws s3 cp")
    assert worker_deploy.index("--worker-env") < worker_deploy.index("aws s3 cp")
    assert worker_deploy.count("source /opt/teamagent/hmac.env") >= 2
    assert worker_deploy.count("source scripts/load_secrets.sh || exit $?") >= 2
    assert "source scripts/load_secrets.sh || exit $?" in worker
    assert "TEAMAGENT_HMAC_REQUIRED_DOMAINS=MAIL_ACTION,REPORT_LINK" in worker
    assert "TEAMAGENT_HMAC_REQUIRED_DOMAINS=REPORT_LINK" in worker_deploy


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
        "Deploy one MCP revision containing both complete keyrings"
    )
    assert "every old MCP task is drained" in runbook
