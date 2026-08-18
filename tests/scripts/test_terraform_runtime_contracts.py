"""監査済みTerraform runtime契約の静的回帰テスト。"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TF_ROOT = PROJECT_ROOT / "infra" / "terraform"
GUARD = PROJECT_ROOT / "infra" / "deploy" / "terraform_runtime_guard.sh"
FORCED_ROLLBACK_DM_QA_PROBE = PROJECT_ROOT / "infra" / "deploy" / "forced_rollback_dm_qa_probe.py"
MIGRATIONS = PROJECT_ROOT / "infra" / "deploy" / "terraform_runtime_migrations.json"


def _block(path: Path, kind: str, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    marker = f'resource "{kind}" "{name}" {{'
    start = text.index(marker)
    brace = text.index("{", start)
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unterminated Terraform block: {path}:{kind}.{name}")


TEAMAGENT_TASKS = [
    ("fargate.tf", "mcp"),
    ("connect_web.tf", "connect_web"),
    ("ingest_schedule.tf", "ingest"),
    ("morning_digest_schedule.tf", "morning_digest"),
    ("canary_schedule.tf", "canary"),
    ("tiktok_acquire.tf", "tiktok_acquire"),
    ("x_research.tf", "x_buzz_worker"),
]


@pytest.mark.parametrize(("filename", "name"), TEAMAGENT_TASKS)
def test_exact_task_definitions_keep_runtime_security_contract(
    filename: str,
    name: str,
) -> None:
    block = _block(TF_ROOT / filename, "aws_ecs_task_definition", name)
    for expected in (
        "skip_destroy",
        "terraform_data.runtime_guard",
        'cpu_architecture        = "ARM64"',
        "merge(local.teamagent_runtime_container",
        'name = "runtime-tmp"',
        "create_before_destroy = true",
    ):
        assert expected in block, f"{name} is missing {expected}"


def test_shared_teamagent_runtime_container_contract_is_exact() -> None:
    body = (TF_ROOT / "fargate.tf").read_text(encoding="utf-8")
    start = body.index("teamagent_runtime_container = {")
    shared = body[start : start + 900]
    for expected in (
        'user                   = "10001:10001"',
        "readonlyRootFilesystem = true",
        "privileged             = false",
        "initProcessEnabled = true",
        'drop = ["ALL"]',
        'sourceVolume  = "runtime-tmp"',
        'containerPath = "/tmp"',
        "readOnly      = false",
    ):
        assert expected in shared


def test_main_media_and_x_writable_cache_contracts_are_exact() -> None:
    mcp = _block(TF_ROOT / "fargate.tf", "aws_ecs_task_definition", "mcp")
    for value in (
        'HOME", value = "/tmp/home"',
        'TMPDIR", value = "/tmp"',
        'XDG_CACHE_HOME", value = "/tmp/.cache"',
        'PYTHONPYCACHEPREFIX", value = "/tmp/.pycache"',
        'UV_CACHE_DIR", value = "/tmp/.uv-cache"',
    ):
        assert value in mcp

    media = _block(
        TF_ROOT / "tiktok_acquire.tf",
        "aws_ecs_task_definition",
        "tiktok_acquire",
    )
    for value in (
        "image       = local.media_worker_image",
        "stopTimeout = 30",
    ):
        assert value in media
    assert "task_role_arn" not in media
    assert 'name = "MEDIA_JOB_BUCKET"' not in media
    assert 'name = "MEDIA_JOBS_TABLE"' not in media
    assert re.search(r"(?m)^\s*command\s*=", media) is None

    x_buzz = _block(
        TF_ROOT / "x_research.tf",
        "aws_ecs_task_definition",
        "x_buzz_worker",
    )
    assert re.search(r"(?m)^\s*image\s*=\s*var\.x_buzz_image$", x_buzz)
    assert 'PYTHONPYCACHEPREFIX", value = "/tmp/.pycache"' in x_buzz


@pytest.mark.parametrize(
    ("filename", "task_definition"),
    [
        ("connect_web.tf", "connect_web"),
        ("canary_schedule.tf", "canary"),
    ],
)
def test_slack_identity_consumers_receive_exact_team_id(
    filename: str,
    task_definition: str,
) -> None:
    block = _block(
        TF_ROOT / filename,
        "aws_ecs_task_definition",
        task_definition,
    )
    exact_environment = '{ name = "SLACK_TEAM_ID", value = var.slack_team_id },'
    assert block.count(exact_environment) == 1


def test_fargate_preflight_executes_every_distinct_image_contract() -> None:
    body = GUARD.read_text(encoding="utf-8")
    migration = json.loads(MIGRATIONS.read_text(encoding="utf-8"))["migrations"][
        "2026-07-wolfi-runtime-v1"
    ]
    assert sorted(migration["required_preflight_profiles"]) == [
        "main",
        "openclaw",
        "tiktok",
        "x_buzz",
    ]
    for profile in ("main", "tiktok", "x_buzz", "openclaw"):
        assert f"{profile})" in body
    for runtime_probe in (
        "printf ok > /tmp/teamagent-preflight",
        'test "$(stat -c %a /tmp)" = 1777',
        'printf writable > "$path/.teamagent-write-probe"',
        'python -c "import sys; assert sys.version_info[:2] == (3, 14)"',
        '/app/.venv/bin/python -c "import playwright, teamagent.media.tool_worker, yt_dlp"',
        "command -v node",
        "command -v yt-dlp",
        "command -v chromium-browser",
        "command -v ffmpeg",
        'test -f "$TIKTOK_SCRAPER_PATH"',
        "/tmp/teamagent-openclaw/state/preflight",
        "(state.mode & 0o777) !== 0o700",
        "entry_point_json='[\"/nodejs/bin/node\"]'",
        "environment:$environment",
        '.config.Volumes["/tmp"]',
        'PREFLIGHT_EFS_ROLE_NAME="${PROJECT}-runtime-preflight-${ENVIRONMENT}-$$"',
        "Key=Purpose,Value=TeamAgentRuntimePreflight",
        "def exact_command",
        "def exact_health",
        '["/app/.venv/bin/python", "/app/scripts/run_ingest_fargate.py"]',
        '["/app/.venv/bin/python", "/app/scripts/run_morning_digest_fargate.py"]',
        '["/app/.venv/bin/python", "/app/scripts/run_canary_health.py"]',
        '["/app/.venv/bin/python", "-m", "teamagent.workers.x_buzz_job"]',
        '[ "sha256:$(sha256_file "$manifest_file")" = "$ECR_DIGEST" ]',
        '[ "sha256:$(sha256_file "$output")" = "$config_digest" ]',
    ):
        assert runtime_probe in body
    assert 'PREFLIGHT_EFS_ROLE_NAME="${PROJECT}-${ENVIRONMENT}-efs-preflight-$$"' not in body


def test_tiktok_preflight_import_matches_media_image_contract() -> None:
    guard = GUARD.read_text(encoding="utf-8")
    media_dockerfile = (
        PROJECT_ROOT / "infra" / "docker" / "Dockerfile.teamagent-media-worker"
    ).read_text(encoding="utf-8")
    media_dockerignore = (
        PROJECT_ROOT / "infra" / "docker" / "Dockerfile.teamagent-media-worker.dockerignore"
    ).read_text(encoding="utf-8")
    core_smoke = (PROJECT_ROOT / "infra" / "docker" / "smoke_core.py").read_text(encoding="utf-8")

    expected_import = "import playwright, teamagent.media.tool_worker, yt_dlp"
    assert expected_import in guard
    assert 'ENTRYPOINT ["/app/.venv/bin/python", "-m", "teamagent.media.tool_worker"]' in (
        media_dockerfile
    )
    assert "import teamagent.media.contracts, teamagent.media.operations, " in media_dockerfile
    assert "teamagent.media.tool_contracts, teamagent.media.tool_worker" in media_dockerfile
    assert "test ! -e /app/src/teamagent/media/worker.py" in media_dockerfile
    assert "!src/teamagent/media/tool_worker.py" in media_dockerignore
    assert "!src/teamagent/media/worker.py" not in media_dockerignore
    assert '"teamagent.media.worker",' in core_smoke


def test_divergent_live_allowlist_and_signed_core_gate_are_fail_closed() -> None:
    body = GUARD.read_text(encoding="utf-8")
    migration = json.loads(MIGRATIONS.read_text(encoding="utf-8"))["migrations"][
        "2026-07-wolfi-runtime-v1"
    ]
    assert migration["enabled"] is False
    assert migration["from"]["task_definition_arns"]["connect_web"].endswith(":53")
    assert migration["from"]["task_definition_arns"]["canary"].endswith(":14")
    assert migration["from"]["task_definition_arns"]["ingest"].endswith(":42")
    assert migration["from"]["active_task_counts"] == {
        "ingest_active": 0,
    }
    assert set(migration["from"]["images"]) == {
        "openclaw",
        "mcp",
        "connect_web",
        "ingest",
        "morning",
        "canary",
        "x_buzz",
        "tiktok",
    }
    assert migration["from"]["images"]["connect_web"] == (
        "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
        "teamagent-mcp@sha256:"
        "0f23860dc382e29d2051f3e6e415a427c853182d90ef05cce0935c3c7cecc144"
    )
    assert migration["from"]["images"]["canary"] == (
        "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
        "teamagent-mcp@sha256:"
        "fb44f7cdb19c7f683768fe074aa85ba3a99fdefe7b6c9e49422e46055bb458b5"
    )
    # Prefix-only observations must never be promoted into an invented full digest.
    assert migration["from"]["images"]["ingest"] == ""
    assert set(migration["from"]["dispatcher_code_sha256"]) == {
        "tiktok",
        "x_buzz",
    }
    assert all(
        re.fullmatch(r"[A-Za-z0-9+/]{43}=", value)
        for value in migration["from"]["dispatcher_code_sha256"].values()
    )
    assert migration["to"]["dispatcher_code_sha256"] == {
        "tiktok": "",
        "x_buzz": "",
    }
    assert migration["to"]["main_signature"] == {
        "minimum_source_commit": "0ff2ca8c7ca9b556cf590f531896055f962780fd",
        "required_hmac_contract_commit": ("2de3b15632bb2d671a4836d5cf3f252dd9b25727"),
        "kms_key_arn": "",
        "annotation_name": "org.opencontainers.image.revision",
        "rekor_transparency_log_required": True,
    }
    assert migration["to"]["required_contract_labels"]["main"] == {
        "io.teamagent.runtime.uid": "10001",
        "io.teamagent.runtime.gid": "10001",
        "io.teamagent.runtime.volume": "/tmp",
        "io.teamagent.runtime.contract": "fargate-readonly-v1",
    }
    assert migration["to"]["required_contract_labels"]["tiktok"] == {
        "io.teamagent.runtime.uid": "10001",
        "io.teamagent.runtime.gid": "10001",
        "io.teamagent.runtime.volume": "/tmp",
        "io.teamagent.runtime.contract": "fargate-readonly-v1",
    }
    assert migration["to"]["required_contract_labels"]["openclaw"] == {
        "io.teamagent.runtime.architecture": "linux/arm64",
        "io.teamagent.runtime.readonly-rootfs-required": "true",
    }
    for required in (
        "validate_signed_main_image",
        "required_hmac_contract_commit",
        "HMAC separation 2de3b156",
        "cosign verify",
        "--insecure-ignore-tlog=false",
        "awskms:///$kms_key_arn",
        "validate_sync_consumer_images",
        "image_deployment_consumers.json",
        "検証済みafter.imageとの個別一致",
        "$live.taskdefs.connect_web.image",
        "$live.taskdefs.ingest.image",
        "$live.taskdefs.morning.image",
        "$live.taskdefs.canary.image",
        "ecs list-tasks",
        "$live.active_tasks.ingest",
    ):
        assert required in body


def test_connect_app_html_uses_current_exact_version_and_sha_contract() -> None:
    exact = {
        "bucket": "teamagent-dev-raw-files",
        "key": "codebuild/connect-web-app.html",
        "version_id": "D5zzWNc44db5i5lz1DxeRxAkmnf1x8jZ",
        "sha256": ("fd62e56e51d3cee25c3d3a87085048fae08ce3babc82480411c61df36e467e28"),
        "vault_manifest_sha256": (
            "1a2834b62a12c60c31ee874820a3a5cfccafb7960193047a17abd426c890568a"
        ),
        "build_inputs_sha256": ("28ea06cf04cf5d0774ea3e9635cb7dfcfcc2985e49160521cee412431cf38bba"),
    }
    manifest = json.loads(MIGRATIONS.read_text(encoding="utf-8"))["migrations"]
    runtime = manifest["2026-07-wolfi-runtime-v1"]
    activation = manifest["2026-07-enable-ingest-canary-v1"]
    assert runtime["from"]["connect_app_html"] == exact
    assert runtime["to"]["connect_app_html"] == exact
    assert activation["from"]["connect_app_html"] == exact

    guard = GUARD.read_text(encoding="utf-8")
    for expected in (
        "s3api head-object",
        "s3api get-object",
        "--version-id",
        "connect_app_sha256",
        "vault_manifest_sha256",
        "build_inputs_sha256",
        "$live.connect_app_html == $m.from.connect_app_html",
    ):
        assert expected in guard
    runtime_guard = (TF_ROOT / "runtime_guard.tf").read_text(encoding="utf-8")
    assert "runtime_connect_app_html_contract_valid" in runtime_guard
    assert re.search(
        r"(?m)^\s*connect_app_html\s*=\s*var\.runtime_guard_live\.connect_app_html$",
        runtime_guard,
    )


def test_openclaw_task_efs_service_and_bedrock_contracts() -> None:
    task = _block(TF_ROOT / "fargate.tf", "aws_ecs_task_definition", "openclaw")
    container_prefix = task.split("environment =", maxsplit=1)[0]
    assert re.search(r"(?m)^\s*command\s*=", container_prefix) is None
    assert re.search(r"(?m)^\s*entryPoint\s*=", container_prefix) is None
    for expected in (
        "stopTimeout            = 120",
        'containerPath = "/tmp/teamagent-openclaw/state"',
        'transit_encryption = "ENABLED"',
        'iam             = "ENABLED"',
        "/readyz",
    ):
        assert expected in task
    assert "OPENCLAW_CONFIG_PATH" not in task

    state = (TF_ROOT / "openclaw_state.tf").read_text(encoding="utf-8")
    for expected in (
        "encrypted        = true",
        "uid = 65532",
        "gid = 65532",
        'permissions = "0700"',
        "for_each        = toset(data.aws_subnets.default.ids)",
        "from_port       = 2049",
        "security_groups = [aws_security_group.openclaw.id]",
        '"elasticfilesystem:ClientMount"',
        '"elasticfilesystem:ClientWrite"',
        '"elasticfilesystem:AccessPointArn"',
        '"elasticfilesystem:AccessedViaMountTarget"',
        "aws_efs_access_point.openclaw_state",
        "terraform_data.runtime_guard",
    ):
        assert expected in state

    fargate = (TF_ROOT / "fargate.tf").read_text(encoding="utf-8")
    assert "local.openclaw_bedrock_profile_arn" in fargate
    assert 'variable = "bedrock:InferenceProfileArn"' in fargate
    assert "local.openclaw_bedrock_backing_model_arns" in fargate
    assert "foundation-model/*" not in _block(
        TF_ROOT / "fargate.tf",
        "aws_iam_role_policy",
        "openclaw_task",
    )
    assert '"arn:aws:bedrock:*::foundation-model/*"' not in fargate


def test_bedrock_and_lambda_secret_iam_are_exact() -> None:
    fargate = (TF_ROOT / "fargate.tf").read_text(encoding="utf-8")
    lambda_iam = (TF_ROOT / "lambda_iam.tf").read_text(encoding="utf-8")
    worker = (TF_ROOT / "worker.tf").read_text(encoding="utf-8")
    all_terraform = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(TF_ROOT.glob("*.tf"))
    )
    for exact in (
        "local.haiku_inference_profile_arn",
        "local.sonnet_inference_profile_arn",
        "local.haiku_backing_model_arns",
        "local.sonnet_backing_model_arns",
    ):
        assert exact in fargate
    assert "inference-profile/*" not in fargate
    assert "foundation-model/*" not in fargate
    assert "resources = local.lambda_bedrock_resources" in lambda_iam
    assert "resources = [data.aws_secretsmanager_secret.database_url.arn]" in lambda_iam
    assert "arn:aws:secretsmanager:${var.aws_region}:*" not in lambda_iam
    assert "aws_secretsmanager_secret.db_password.arn" in worker
    assert "local.hmac_secret_iam_arns" in worker
    assert "resources = local.bedrock_resources" in worker
    assert "secret:${var.project_name}/${var.environment}/*" not in worker
    assert "foundation-model/*" not in worker
    assert "inference-profile/*" not in worker
    # Converse/ConverseStream APIs authorize through InvokeModel actions; the
    # similarly named strings are not IAM actions.
    assert '"bedrock:Converse"' not in all_terraform
    assert '"bedrock:ConverseStream"' not in all_terraform

    for filename in (
        "fargate.tf",
        "ingest_schedule.tf",
        "morning_digest_schedule.tf",
    ):
        body = (TF_ROOT / filename).read_text(encoding="utf-8")
        assert "data.aws_kms_alias.oauth_tokens.target_key_arn" in body
        assert 'key/*"' not in body

    manifest = json.loads(MIGRATIONS.read_text(encoding="utf-8"))["migrations"][
        "2026-07-wolfi-runtime-v1"
    ]
    required_iam_addresses = {
        "aws_iam_role_policy.worker_app",
        "aws_iam_role_policy.lambda_app",
        "aws_iam_role_policy.mcp_task",
        "aws_iam_role_policy.connect_web_task[0]",
        "aws_iam_role_policy.ingest_task[0]",
        "aws_iam_role_policy.morning_digest_task[0]",
    }
    assert "allowed_changes" not in manifest
    if manifest["enabled"]:
        assert required_iam_addresses.issubset(
            {row["address"] for row in manifest["reviewed_plan"]["resource_changes"]}
        )
    else:
        assert manifest["reviewed_plan"] is None

    guard = GUARD.read_text(encoding="utf-8")
    assert "validate_exact_runtime_iam_plan" in guard
    assert "exact_secret_arn" in guard
    assert "approved_bedrock_arns" in guard
    assert "exact_pass_service" in guard


def _run_exact_iam_validator(
    tmp_path: Path,
    *,
    mutation: str | None = None,
) -> subprocess.CompletedProcess[str]:
    statements: list[dict[str, object]] = [
        {
            "Effect": "Allow",
            "Action": [
                "secretsmanager:GetSecretValue",
                "secretsmanager:DescribeSecret",
            ],
            "Resource": (
                "arn:aws:secretsmanager:ap-northeast-1:718959508629:"
                "secret:teamagent/dev/database-url-AbC123"
            ),
        },
        {
            "Effect": "Allow",
            "Action": ["bedrock:InvokeModel"],
            "Resource": (
                "arn:aws:bedrock:ap-northeast-1:718959508629:"
                "inference-profile/"
                "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
            ),
        },
        {
            "Effect": "Allow",
            "Action": ["kms:Decrypt"],
            "Resource": (
                "arn:aws:kms:ap-northeast-1:718959508629:key/01234567-89ab-cdef-0123-456789abcdef"
            ),
        },
        {
            "Effect": "Allow",
            "Action": ["iam:PassRole"],
            "Resource": "arn:aws:iam::718959508629:role/exact-role",
            "Condition": {"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
        },
    ]
    if mutation == "secret":
        statements[0]["Resource"] = (
            "arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/*"
        )
    elif mutation == "bedrock":
        statements[1]["Resource"] = "arn:aws:bedrock:ap-northeast-1::foundation-model/*"
    elif mutation == "kms":
        statements[2]["Resource"] = "arn:aws:kms:ap-northeast-1:718959508629:key/*"
    elif mutation == "passrole":
        statements[3].pop("Condition")
    elif mutation is not None:
        raise AssertionError(f"unknown mutation: {mutation}")

    policy = json.dumps(
        {"Version": "2012-10-17", "Statement": statements},
        separators=(",", ":"),
    )
    addresses = [
        "aws_iam_role_policy.worker_app",
        "aws_iam_role_policy.lambda_app",
        "aws_iam_role_policy.mcp_task",
        "aws_iam_role_policy.connect_web_task[0]",
        "aws_iam_role_policy.ingest_task[0]",
        "aws_iam_role_policy.morning_digest_task[0]",
    ]
    plan = {
        "resource_changes": [
            {
                "address": address,
                "type": "aws_iam_role_policy",
                "change": {
                    "actions": ["no-op"],
                    "before": {"policy": policy},
                    "after": {"policy": policy},
                },
            }
            for address in addresses
        ]
    }
    plan_path = tmp_path / "iam-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    guard = GUARD.read_text(encoding="utf-8")
    start = guard.index("validate_exact_runtime_iam_plan() {")
    end = guard.index("\nvalidate_activation_plan() {", start)
    function = guard[start:end]
    runner = tmp_path / "validate-iam.sh"
    runner.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'REGION="ap-northeast-1"',
                'EXPECTED_ACCOUNT_ID="718959508629"',
                'die() { printf "%s\\n" "$*" >&2; exit 1; }',
                function,
                f'validate_exact_runtime_iam_plan "{plan_path}"',
            ]
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        ["bash", str(runner)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_exact_runtime_iam_validator_accepts_only_exact_resources(
    tmp_path: Path,
) -> None:
    result = _run_exact_iam_validator(tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mutation", ["secret", "bedrock", "kms", "passrole"])
def test_exact_runtime_iam_validator_rejects_broad_permissions(
    tmp_path: Path,
    mutation: str,
) -> None:
    result = _run_exact_iam_validator(tmp_path, mutation=mutation)
    assert result.returncode == 1
    assert "runtime IAM plan" in result.stderr


@pytest.mark.parametrize(
    ("filename", "name"),
    [
        ("fargate.tf", "mcp"),
        ("fargate.tf", "openclaw"),
        ("connect_web.tf", "connect_web"),
    ],
)
def test_long_running_services_auto_rollback_and_rebalance(
    filename: str,
    name: str,
) -> None:
    block = _block(TF_ROOT / filename, "aws_ecs_service", name)
    assert re.search(
        r'(?m)^\s*availability_zone_rebalancing\s*=\s*"ENABLED"\s*$',
        block,
    )
    assert re.search(
        r"deployment_circuit_breaker\s*\{\s*enable\s*=\s*true\s*"
        r"rollback\s*=\s*true\s*\}",
        block,
        flags=re.DOTALL,
    )
    assert "prevent_destroy = true" in block
    if name == "openclaw":
        assert re.search(
            r"(?m)^\s*deployment_maximum_percent\s*=\s*100\s*$",
            block,
        )
        assert re.search(
            r"(?m)^\s*deployment_minimum_healthy_percent\s*=\s*0\s*$",
            block,
        )
    else:
        assert re.search(
            r"(?m)^\s*wait_for_steady_state\s*=\s*true\s*$",
            block,
        )
    if name == "connect_web":
        assert "health_check_grace_period_seconds = 60" in block


def test_health_start_periods_cover_initial_deployment_health() -> None:
    mcp = _block(TF_ROOT / "fargate.tf", "aws_ecs_task_definition", "mcp")
    openclaw = _block(TF_ROOT / "fargate.tf", "aws_ecs_task_definition", "openclaw")
    connect = _block(TF_ROOT / "connect_web.tf", "aws_ecs_task_definition", "connect_web")
    assert "startPeriod = 40" in mcp
    assert "startPeriod = 40" in openclaw
    assert "startPeriod = 30" in connect


@pytest.mark.parametrize(
    ("filename", "rule", "target"),
    [
        ("ingest_schedule.tf", "ingest_weekly", "ingest_run_task"),
        (
            "morning_digest_schedule.tf",
            "morning_digest_weekday",
            "morning_digest_run_task",
        ),
        ("canary_schedule.tf", "canary_hourly", "canary_run_task"),
    ],
)
def test_schedule_rules_and_targets_are_guarded_and_protected(
    filename: str,
    rule: str,
    target: str,
) -> None:
    for kind, name in (
        ("aws_cloudwatch_event_rule", rule),
        ("aws_cloudwatch_event_target", target),
    ):
        block = _block(TF_ROOT / filename, kind, name)
        assert "terraform_data.runtime_guard" in block
        assert "prevent_destroy = true" in block


def test_api_gateway_origin_and_safe_access_log_contract() -> None:
    path = TF_ROOT / "api_gateway_hardening.tf"
    body = path.read_text(encoding="utf-8")
    api = _block(path, "aws_apigatewayv2_api", "connect_web")
    stage = _block(path, "aws_apigatewayv2_stage", "connect_web_default")
    assert "disable_execute_api_endpoint = true" in api
    assert "prevent_destroy = true" in api
    assert 'name        = "$default"' in stage
    assert "detailed_metrics_enabled = false" in stage
    assert "aws_cloudwatch_log_group.connect_http_api_access.arn" in stage
    for forbidden in (
        "$context.http.path",
        "$context.http.sourceIp",
        "$context.identity",
        "$context.authorizer",
        "$context.requestOverride",
    ):
        assert forbidden not in body
    assert 'id = "esk97z9grh"' in body
    assert 'id = "esk97z9grh/$default"' in body
    guard = GUARD.read_text(encoding="utf-8")
    assert "connect.newstv.co.jp" in guard
    assert "connect custom-domain root mapping" in guard


@pytest.mark.parametrize(
    ("filename", "resource", "name"),
    [
        (
            "reminders.tf",
            "reminder_notify",
            "/aws/lambda/${local.rem_name}-notify",
        ),
        (
            "tiktok_acquire.tf",
            "tiktok_dispatch",
            "/aws/lambda/${local.tk_name}-dispatch",
        ),
        (
            "x_research.tf",
            "x_dispatch",
            "/aws/lambda/${local.xr_name}-dispatch",
        ),
    ],
)
def test_lambda_log_groups_are_bounded_and_always_present(
    filename: str,
    resource: str,
    name: str,
) -> None:
    block = _block(TF_ROOT / filename, "aws_cloudwatch_log_group", resource)
    assert re.search(
        rf'(?m)^\s*name\s*=\s*"{re.escape(name)}"\s*$',
        block,
    )
    assert "retention_in_days = 30" in block
    assert "count" not in block
    assert "terraform_data.runtime_guard" in block
    assert "prevent_destroy = true" in block
    policy_file = (TF_ROOT / filename).read_text(encoding="utf-8")
    assert '"logs:CreateLogGroup"' not in policy_file


def test_hmac_consumers_and_rotation_deadlines_are_purpose_exact() -> None:
    hmac = (TF_ROOT / "hmac_rotation.tf").read_text(encoding="utf-8")
    guard = GUARD.read_text(encoding="utf-8")
    runtime = (TF_ROOT / "runtime_guard.tf").read_text(encoding="utf-8")
    for name in (
        "MAIL_ACTION_HMAC_SECRET",
        "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
        "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
        "REPORT_LINK_HMAC_SECRET",
        "REPORT_LINK_HMAC_PREVIOUS_SECRET",
        "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    ):
        assert name in hmac
    assert "mail   = 87300" in runtime
    assert "report = 605700" in runtime
    assert 'validate("mail"; 86400)' in guard
    assert 'validate("report"; 604800)' in guard
    assert "get-secret-value" not in guard
    assert "SecretString" not in guard
    assert "xoxp-" not in guard
    assert '"userToken"' not in guard
    assert '"channelId"' not in guard
    assert '"botUserId"' not in guard


def test_forced_rollback_dm_qa_probe_secret_read_is_purpose_exact() -> None:
    probe = FORCED_ROLLBACK_DM_QA_PROBE.read_text(encoding="utf-8")
    tree = ast.parse(probe, filename=str(FORCED_ROLLBACK_DM_QA_PROBE))
    string_literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert string_literals.count("get-secret-value") == 1
    assert string_literals.count("--secret-id") == 1

    canary_secret_assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "CANARY_SECRET"
    ]
    assert len(canary_secret_assignments) == 1
    assert ast.literal_eval(canary_secret_assignments[0].value) == (
        "teamagent/dev/openclaw/rollout-canary"
    )

    aws_json_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "aws_json"
    ]
    assert all(
        len(call.args) == 3
        and not call.keywords
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
        and isinstance(call.args[1], ast.Constant)
        and isinstance(call.args[1].value, str)
        for call in aws_json_calls
    )
    secret_reads = [call for call in aws_json_calls if call.args[0].value == "secretsmanager"]
    assert len(secret_reads) == 1
    secret_read = secret_reads[0]
    assert secret_read.args[1].value == "get-secret-value"
    assert isinstance(secret_read.args[2], ast.List)
    secret_arguments = secret_read.args[2].elts
    assert len(secret_arguments) == 4
    assert ast.literal_eval(secret_arguments[0]) == "--secret-id"
    assert isinstance(secret_arguments[1], ast.Name)
    assert secret_arguments[1].id == "CANARY_SECRET"
    assert ast.literal_eval(secret_arguments[2]) == "--version-stage"
    assert ast.literal_eval(secret_arguments[3]) == "AWSCURRENT"


def test_two_phase_migration_never_enables_schedules_early() -> None:
    manifest = json.loads(MIGRATIONS.read_text(encoding="utf-8"))
    runtime = manifest["migrations"]["2026-07-wolfi-runtime-v1"]
    activation = manifest["migrations"]["2026-07-enable-ingest-canary-v1"]
    assert runtime["enabled"] is False
    assert runtime["to"]["rule_states"] == {
        "ingest": "DISABLED",
        "morning": "DISABLED",
        "canary": "DISABLED",
    }
    assert activation["enabled"] is False
    assert activation["requires_migration"] == "2026-07-wolfi-runtime-v1"
    assert activation["to"]["rule_states"] == {
        "ingest": "ENABLED",
        "morning": "ENABLED",
        "canary": "ENABLED",
    }
    assert activation["required_preflight_profiles"] == [
        "activation-ingest-acl-quarantine",
        "activation-canary",
    ]
    expected_activation_addresses = {
        "terraform_data.runtime_guard",
        "terraform_data.production_image_release_gate",
        "aws_cloudwatch_metric_alarm.canary_heartbeat_missing[0]",
        "aws_cloudwatch_event_rule.ingest_weekly[0]",
        "aws_cloudwatch_event_rule.canary_hourly[0]",
    }
    assert "allowed_changes" not in activation
    if activation["enabled"]:
        assert {
            row["address"] for row in activation["reviewed_plan"]["resource_changes"]
        } == expected_activation_addresses
    else:
        assert activation["reviewed_plan"] is None


def test_production_path_monitoring_is_complete_and_protected() -> None:
    monitoring_path = TF_ROOT / "runtime_monitoring.tf"
    monitoring = monitoring_path.read_text(encoding="utf-8")
    for service in ("mcp", "connect_web", "openclaw"):
        assert re.search(rf"(?m)^\s*{service}\s*=", monitoring)
    for function in ("reminders", "tiktok", "x_buzz"):
        assert re.search(rf"(?m)^\s*{function}\s*=", monitoring)

    expected_alarms = {
        "ecs_running_tasks": (
            'namespace           = "ECS/ContainerInsights"',
            'metric_name         = "RunningTaskCount"',
            'treat_missing_data  = "breaching"',
        ),
        "connect_api_5xx": (
            'namespace           = "AWS/ApiGateway"',
            'metric_name         = "5xx"',
        ),
        "lambda_errors": (
            'namespace           = "AWS/Lambda"',
            'metric_name         = "Errors"',
        ),
        "lambda_throttles": (
            'namespace           = "AWS/Lambda"',
            'metric_name         = "Throttles"',
        ),
        "rds_database_connections_high": (
            'metric_name         = "DatabaseConnections"',
            "threshold           = 80",
        ),
        "rds_freeable_memory_low": (
            'metric_name         = "FreeableMemory"',
            "threshold           = 536870912",
        ),
    }
    for resource, expected in expected_alarms.items():
        block = _block(
            monitoring_path,
            "aws_cloudwatch_metric_alarm",
            resource,
        )
        for contract in expected:
            assert contract in block
        assert "aws_sns_topic.alarms.arn" in block
        assert "terraform_data.runtime_guard" in block
        assert "prevent_destroy = true" in block

    cluster = _block(TF_ROOT / "fargate.tf", "aws_ecs_cluster", "main")
    assert 'name = "containerInsights"' in cluster
    assert 'value = "enabled"' in cluster
    assert "prevent_destroy = true" in cluster


@pytest.mark.parametrize(
    ("filename", "resource"),
    [
        ("reminders.tf", "reminders_dlq"),
        ("tiktok_acquire.tf", "tiktok_jobs_dlq_depth"),
        ("x_research.tf", "x_jobs_dlq_depth"),
    ],
)
def test_every_managed_dlq_has_depth_alarm(
    filename: str,
    resource: str,
) -> None:
    block = _block(
        TF_ROOT / filename,
        "aws_cloudwatch_metric_alarm",
        resource,
    )
    assert 'metric_name         = "ApproximateNumberOfMessagesVisible"' in block
    assert "aws_sns_topic.alarms.arn" in block
    assert 'treat_missing_data = "notBreaching"' in re.sub(r"\s+", " ", block)
    assert "terraform_data.runtime_guard" in block
    assert "prevent_destroy = true" in block


def test_canary_heartbeat_alarm_precedes_schedule_activation() -> None:
    path = TF_ROOT / "canary_schedule.tf"
    alarm = _block(
        path,
        "aws_cloudwatch_metric_alarm",
        "canary_heartbeat_missing",
    )
    assert "var.enable_canary_health && var.canary_rule_enabled" in alarm
    for expected in (
        'metric_name         = "CanaryHeartbeat"',
        "period              = 3600",
        "evaluation_periods  = 2",
        "datapoints_to_alarm = 2",
        'treat_missing_data  = "breaching"',
        "prevent_destroy = true",
    ):
        assert expected in alarm
    rule = _block(path, "aws_cloudwatch_event_rule", "canary_hourly")
    assert "aws_cloudwatch_metric_alarm.canary_heartbeat_missing" in rule
    assert 'state               = var.canary_rule_enabled ? "ENABLED" : "DISABLED"' in rule


def test_canary_security_group_is_an_exact_vpce_https_source() -> None:
    vpce = _block(TF_ROOT / "vpc_endpoints.tf", "aws_security_group", "vpce")
    guard = GUARD.read_text(encoding="utf-8")
    assert "var.enable_canary_health ? [aws_security_group.canary[0].id] : []" in vpce
    assert "terraform_data.runtime_guard" in vpce
    assert "prevent_destroy = true" in vpce
    assert "validate_canary_vpce_plan" in guard
    assert '"aws_security_group.canary[0].id"' in guard
    assert "live canary SGの443追加以外" in guard


def test_alarm_delivery_is_confirmed_fail_closed_and_single_owned() -> None:
    cloudwatch = (TF_ROOT / "cloudwatch.tf").read_text(encoding="utf-8")
    topic = _block(TF_ROOT / "cloudwatch.tf", "aws_sns_topic", "alarms")
    variables = (TF_ROOT / "variables.tf").read_text(encoding="utf-8")
    runtime = (TF_ROOT / "runtime_guard.tf").read_text(encoding="utf-8")
    guard = GUARD.read_text(encoding="utf-8")
    evidence = (PROJECT_ROOT / "infra/deploy/runtime_evidence_guard.py").read_text(encoding="utf-8")
    manifest = json.loads(MIGRATIONS.read_text(encoding="utf-8"))
    handoff = manifest["external_state_handoffs"]["2026-07-alarm-topic-consolidation-v1"]

    assert 'name = "${var.project_name}-${var.environment}-openclaw-alarms"' in topic
    assert "prevent_destroy = true" in topic
    assert "condition     = local.runtime_guard_verified" in topic
    assert 'resource "aws_sns_topic_subscription"' not in cloudwatch
    assert "cannot wait" in cloudwatch
    assert "PendingConfirmation" in cloudwatch
    assert 'variable "require_alarm_delivery"' in variables
    assert re.search(
        r'variable "require_alarm_delivery"\s*\{.*?default\s*=\s*true',
        variables,
        flags=re.DOTALL,
    )
    assert 'variable "alarm_chatbot_configuration_arns"' in variables
    alarm_email = re.search(
        r'variable "alarm_email_endpoints"\s*\{(?P<body>.*?)\n\}',
        variables,
        flags=re.DOTALL,
    )
    assert alarm_email is not None
    assert "sensitive   = true" in alarm_email.group("body")
    assert "confirmed_email_endpoint_sha256" in runtime
    assert "confirmed_subscription_metadata_sha256" in runtime
    assert "legacy_action_reference_count == 0" in runtime
    assert "sha256(endpoint)" in runtime
    assert "sha256(lower(trimspace(endpoint)))" not in runtime
    assert "depends_on = [aws_sns_topic.alarms]" in runtime
    assert "aws_sns_topic_subscription.alarms_email" not in runtime
    assert "local.configured_alarm_email_sha256 == [" in runtime
    assert "length(local.configured_alarm_chatbot_arns) == 0" in runtime
    assert "length(var.alarm_email_endpoints) == 1" in variables
    assert 'var.alarm_email_endpoints[0] == "s-komata@vectorinc.co.jp"' in variables
    assert "trim/lower不可" in variables
    assert "length(var.alarm_chatbot_configuration_arns) == 0" in variables
    assert "list-subscriptions-by-topic" in guard
    assert "get-subscription-attributes" in guard
    assert "subscription-inventory.jsonl" in guard
    assert "subscription_inventory_sha256" in guard
    assert "destination_state_sha256" in guard
    assert "subscription_inventory_count == 1" in guard
    assert "pending_subscription_count == 0" in guard
    assert 'subscription_protocols == ["email"]' in guard
    assert 'has("FilterPolicy") | not' in guard
    assert 'has("FilterPolicyScope") | not' in guard
    assert "verify_alarm_delivery_test_receipt" in guard
    assert "issue-sns-challenge" in guard
    assert "sign-sns-ack" in guard
    assert "verify-sns-delivery" in guard
    assert re.search(r'"sns",\s*"publish"', evidence)
    assert '"kms",\n            "verify"' in evidence
    assert "describe-slack-channel-configurations" in evidence
    assert "list-microsoft-teams-channel-configurations" in evidence
    assert "describe-chime-webhook-configurations" in evidence
    assert "describe-budgets" in evidence
    assert "describe-subscribers-for-notification" in evidence
    assert "get-anomaly-subscriptions" in evidence
    assert 'select(.type == "aws_sns_topic_subscription")' in guard
    assert "configured_email_hash" in guard
    assert "strict syncは確認済みalarm delivery" in guard
    assert 'PendingConfirmation) subscription_state="pending"' in guard
    assert 'Deleted) subscription_state="deleted"' in guard
    assert handoff["canonical_owner"] == "aws_sns_topic.alarms"
    assert handoff["legacy_owner"] == "external-teamagent-state"
    assert handoff["import_legacy_into_this_state"] is False
    assert handoff["activation_requires"] == {
        "confirmed_email_endpoint_sha256": (
            "88c6452f9db04017250aa5728b4815bccb55b5ecc0b35b50a5234170dc08d1e6"
        ),
        "subscription_inventory_count": 1,
        "pending_subscription_count": 0,
        "subscription_protocol": "email",
        "destination_state_sha256": (
            "c942dbb7b97da1f4d9debb1ba241ee89bf8c1d951d8d75bdea3056850838ddc9"
        ),
        "chatbot_configuration_count": 0,
        "legacy_topic_exists": False,
        "legacy_action_reference_count": 0,
        "final_phase": "legacy_retired",
        "final_checkpoint_sha256_required": True,
        "history_sha256_required": True,
    }
    assert handoff["durable_checkpoint_required"] is True
    assert handoff["idempotent_resume_required"] is True
    assert len(re.findall(r'resource "aws_sns_topic" "[^"]+"', cloudwatch)) == 1

    for path in TF_ROOT.glob("*.tf"):
        body = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r"(?m)^\s*(?:alarm_actions|ok_actions)\s*=\s*\[(.*?)\]\s*$",
            body,
        ):
            assert match.group(1).strip() == "aws_sns_topic.alarms.arn"


def test_runtime_guard_has_exact_endpoints_for_every_invoked_aws_service() -> None:
    guard = GUARD.read_text(encoding="utf-8")
    invoked = set(re.findall(r"\baws_cli ([a-z0-9-]+)\b", guard))
    mapped = {
        "apigatewayv2",
        "bedrock",
        "budgets",
        "ce",
        "chatbot",
        "cloudtrail",
        "cloudwatch",
        "codestar-notifications",
        "dynamodb",
        "ec2",
        "ecr",
        "ecs",
        "efs",
        "events",
        "iam",
        "kms",
        "lambda",
        "logs",
        "rds",
        "s3api",
        "scheduler",
        "secretsmanager",
        "sns",
        "sqs",
        "sts",
    }
    assert invoked <= mapped, sorted(invoked - mapped)
    assert "apigatewayv2) printf 'https://apigateway.%s.amazonaws.com" in guard
    assert "ecr) printf 'https://api.ecr.%s.amazonaws.com" in guard
    assert "efs) printf 'https://elasticfilesystem.%s.amazonaws.com" in guard
    assert "iam) printf 'https://iam.amazonaws.com" in guard


def _run_alarm_delivery_validator(
    tmp_path: Path,
    *,
    mode: str = "email",
    mutation: str | None = None,
) -> subprocess.CompletedProcess[str]:
    canonical = "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-openclaw-alarms"
    legacy = "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-alarms"
    email = "s-komata@vectorinc.co.jp"
    email_hash = hashlib.sha256(b"s-komata@vectorinc.co.jp").hexdigest()
    destination_hash = "c942dbb7b97da1f4d9debb1ba241ee89bf8c1d951d8d75bdea3056850838ddc9"
    chat_arn = "arn:aws:chatbot::718959508629:chat-configuration/slack-channel/teamagent-dev-alerts"
    if mode == "email":
        emails = [email]
        chat = []
        live_email_hashes = [email_hash]
        live_chat_arns: list[str] = []
    elif mode == "chat":
        emails = []
        chat = [chat_arn]
        live_email_hashes = []
        live_chat_arns = [chat_arn]
    else:
        raise AssertionError(f"unknown delivery mode: {mode}")

    alarm_actions = [canonical]
    extra_resources: list[dict[str, object]] = []
    inventory_count = 1
    pending_count = 0
    protocols = ["email"]
    inventory_sha = "a" * 64
    if mutation == "zero":
        emails = []
        chat = []
        live_email_hashes = []
        live_chat_arns = []
    elif mutation == "trim":
        emails = [" s-komata@vectorinc.co.jp "]
    elif mutation == "case":
        emails = ["S-KOMATA@VECTORINC.CO.JP"]
    elif mutation == "pending":
        pending_count = 1
    elif mutation == "mismatch":
        live_email_hashes = [hashlib.sha256(b"other@example.com").hexdigest()]
    elif mutation == "different_configured":
        emails = ["other@example.com"]
    elif mutation == "extra":
        inventory_count = 2
        protocols = ["email", "sms"]
    elif mutation == "protocol":
        protocols = ["email-json"]
    elif mutation == "inventory_hash":
        inventory_sha = "not-a-hash"
    elif mutation == "destination":
        destination_hash = "9" * 64
    elif mutation == "chatbot":
        live_chat_arns = [chat_arn]
    elif mutation == "subscription":
        extra_resources.append(
            {
                "address": "aws_sns_topic_subscription.pending",
                "type": "aws_sns_topic_subscription",
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {
                        "topic_arn": canonical,
                        "protocol": "email",
                        "endpoint": "alerts@example.com",
                    },
                },
            }
        )
    elif mutation == "legacy_alarm":
        alarm_actions = [legacy]
    elif mutation is not None:
        raise AssertionError(f"unknown mutation: {mutation}")

    plan = {
        "variables": {
            "alarm_email_endpoints": {"value": emails},
            "alarm_chatbot_configuration_arns": {"value": chat},
            "require_alarm_delivery": {"value": True},
            "runtime_guard_live": {
                "value": {
                    "alarm_delivery": {
                        "confirmed_email_endpoint_sha256": live_email_hashes,
                        "subscription_inventory_count": inventory_count,
                        "pending_subscription_count": pending_count,
                        "subscription_protocols": protocols,
                        "subscription_inventory_sha256": inventory_sha,
                        "confirmed_subscription_metadata_sha256": "b" * 64,
                        "destination_state_sha256": destination_hash,
                        "attached_chatbot_configuration_arns": live_chat_arns,
                    }
                }
            },
        },
        "resource_changes": [
            {
                "address": "aws_sns_topic.alarms",
                "type": "aws_sns_topic",
                "change": {
                    "actions": ["no-op"],
                    "before": {"name": "teamagent-dev-openclaw-alarms"},
                    "after": {"name": "teamagent-dev-openclaw-alarms"},
                },
            },
            {
                "address": "aws_cloudwatch_metric_alarm.example",
                "type": "aws_cloudwatch_metric_alarm",
                "change": {
                    "actions": ["no-op"],
                    "before": {"alarm_actions": alarm_actions},
                    "after": {
                        "alarm_actions": alarm_actions,
                        "ok_actions": [canonical],
                        "insufficient_data_actions": [],
                    },
                },
            },
            *extra_resources,
        ],
    }
    plan_path = tmp_path / "alarm-delivery-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    guard = GUARD.read_text(encoding="utf-8")
    start = guard.index("validate_alarm_delivery_plan() {")
    end = guard.index(
        "\nvalidate_log_bucket_hardening_plan() {",
        start,
    )
    function = guard[start:end]
    runner = tmp_path / "validate-alarm-delivery.sh"
    runner.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                "sha256_text() {",
                "  if command -v sha256sum >/dev/null 2>&1; then",
                "    sha256sum | awk '{print $1}'",
                "  else",
                "    shasum -a 256 | awk '{print $1}'",
                "  fi",
                "}",
                'EXPECTED_ALARM_EMAIL="s-komata@vectorinc.co.jp"',
                (
                    'EXPECTED_ALARM_EMAIL_SHA256="'
                    "88c6452f9db04017250aa5728b4815bcc"
                    'b55b5ecc0b35b50a5234170dc08d1e6"'
                ),
                (
                    'EXPECTED_ALARM_DESTINATION_STATE_SHA256="'
                    "c942dbb7b97da1f4d9debb1ba241ee89"
                    'bf8c1d951d8d75bdea3056850838ddc9"'
                ),
                'die() { printf "%s\\n" "$*" >&2; exit 1; }',
                function,
                f'validate_alarm_delivery_plan "{plan_path}"',
            ]
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        ["bash", str(runner)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_alarm_delivery_validator_accepts_only_live_confirmed_destination(
    tmp_path: Path,
) -> None:
    result = _run_alarm_delivery_validator(tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "zero",
        "trim",
        "case",
        "pending",
        "mismatch",
        "different_configured",
        "extra",
        "protocol",
        "inventory_hash",
        "destination",
        "chatbot",
        "subscription",
        "legacy_alarm",
    ],
)
def test_alarm_delivery_validator_rejects_unconfirmed_or_legacy_delivery(
    tmp_path: Path,
    mutation: str,
) -> None:
    result = _run_alarm_delivery_validator(tmp_path, mutation=mutation)
    assert result.returncode == 1
    assert "alarm delivery plan" in result.stderr


def test_alarm_delivery_validator_rejects_chatbot_only_mode(tmp_path: Path) -> None:
    result = _run_alarm_delivery_validator(tmp_path, mode="chat")
    assert result.returncode == 1
    assert "alarm delivery plan" in result.stderr


def test_ci_contract_forbids_direct_terraform_mutation_scripts() -> None:
    forbidden = re.compile(r"\bterraform(?:\s+-chdir=\S+)?\s+(plan|apply|destroy)\b")
    offenders: list[str] = []
    candidates = [
        *TF_ROOT.glob("*.sh"),
        *(PROJECT_ROOT / "infra" / "deploy").glob("*.sh"),
        *(PROJECT_ROOT / ".github" / "workflows").glob("*.yml"),
        *(PROJECT_ROOT / ".github" / "workflows").glob("*.yaml"),
    ]
    for path in sorted(set(candidates)):
        if path == GUARD:
            continue
        if forbidden.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert offenders == []

    guard = GUARD.read_text(encoding="utf-8")
    assert "apply)" in guard
    assert "assert_trusted_automation_identity" in guard
    assert "acquire_deployment_lock" in guard
    assert 'verify_receipt "$PLAN" "$RECEIPT"' in guard
    assert '"$TMP_ROOT/verify/plan.tfplan"' in guard
    assert "terraform-runtime-apply-receipt" in guard
    assert 'python3 "$DEPLOYMENT_APPLY_FINALIZER" commit' in guard
    assert "--eventbridge-verification" in guard
    assert "--ecs-verification" in guard
    assert "-auto-approve" not in guard
    for retired in ("apply_openclaw.sh", "apply_resilience.sh"):
        body = (TF_ROOT / retired).read_text(encoding="utf-8")
        assert "exit 64" in body

    boundary = (TF_ROOT / "deployment_boundary.tf").read_text(encoding="utf-8")
    for expected in (
        "accepted risk",
        "retain their current permissions",
        "RegisterTaskDefinition",
        "RunTask",
        "PassRole",
        "not an authorization or security boundary",
        "administrator permission",
    ):
        assert expected in boundary
    assert 'resource "aws_iam_policy"' not in boundary
    assert 'resource "aws_iam_user_policy_attachment"' not in boundary
    assert "teamagent-dev-deny-direct-runtime-mutation" not in boundary
    migration = json.loads(MIGRATIONS.read_text(encoding="utf-8"))["migrations"][
        "2026-07-wolfi-runtime-v1"
    ]
    assert "allowed_changes" not in migration
    reviewed_plan = migration["reviewed_plan"]
    reviewed_addresses = (
        {row["address"] for row in reviewed_plan["resource_changes"]}
        if reviewed_plan is not None
        else set()
    )
    assert "aws_iam_policy.runtime_direct_mutation_deny" not in reviewed_addresses
    assert "aws_iam_user_policy_attachment.runtime_direct_mutation_deny" not in reviewed_addresses

    for expected in (
        "validate_dispatcher_migration_plan",
        ".to.dispatcher_code_sha256[$component]",
        "$change.change.after.source_code_hash == $expected_code",
        '$after_lambda.handler == "handler.handler"',
        '$after_lambda.runtime == "python3.12"',
        "$after_lambda.timeout == 30",
        '$after_lambda.kms_key_arn == ""',
        "$after_lambda.vpc_config == null",
        '$filename_references == [($archive_address + ".output_path")]',
    ):
        assert expected in guard


def test_provider_lock_is_git_receipted_and_has_official_cross_platform_hashes() -> None:
    lock_path = TF_ROOT / ".terraform.lock.hcl"
    lock = lock_path.read_text(encoding="utf-8")
    guard = GUARD.read_text(encoding="utf-8")

    for version in ("5.100.0", "2.8.0", "3.9.0"):
        assert re.search(rf'(?m)^\s*version\s*=\s*"{re.escape(version)}"$', lock)
    for expected in (
        # Official archive checksums for linux_amd64 deployment, darwin_arm64
        # validation, and linux_arm64 tooling.
        "zh:1589a2266af699cbd5d80737a0fe02e54ec9cf2ca54e7e00ac51c7359056f274",
        "zh:bb64e8aff37becab373a1a0cc1080990785304141af42ed6aa3dd4913b000421",
        "zh:6330766f1d85f01ae6ea90d1b214b8b74cc8c1badc4696b165b36ddd4cc15f7b",
    ):
        assert expected in lock
    assert "-name '.terraform.lock.hcl'" in guard
    tracked = subprocess.run(
        [
            "git",
            "-C",
            str(PROJECT_ROOT),
            "ls-files",
            "--error-unmatch",
            "infra/terraform/.terraform.lock.hcl",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode == 0, tracked.stderr


def test_runtime_ledger_leading_key_allows_require_key_presence() -> None:
    evidence = (TF_ROOT / "runtime_evidence.tf").read_text(encoding="utf-8")
    null_checks = evidence.count('test     = "Null"')
    assert null_checks >= 5
    assert evidence.count('values   = ["false"]') == null_checks
    assert evidence.count('variable = "dynamodb:LeadingKeys"') > null_checks
    for expected in (
        'sid = "AtomicallyFinalizeExactDeployment"',
        '"apply-finalization#*"',
        '"apply-finalization-chunk#*"',
        '"ecs-service-apply#*"',
    ):
        assert expected in evidence


def test_quarantine_codebuild_is_active_but_cannot_publish_a_release() -> None:
    path = TF_ROOT / "codebuild.tf"
    body = path.read_text(encoding="utf-8")
    project = _block(path, "aws_codebuild_project", "image")
    policy_resource = _block(path, "aws_iam_role_policy", "codebuild")
    policy_document = body.split(
        'data "aws_iam_policy_document" "codebuild" {',
        maxsplit=1,
    )[1].split(
        'resource "aws_iam_role_policy" "codebuild" {',
        maxsplit=1,
    )[0]
    for expected in (
        'description  = "Build and vulnerability-gate TeamAgent MCP candidate images inside AWS"',
        'type            = "ARM_CONTAINER"',
        "privileged_mode = true",
        'type     = "S3"',
        'location = "${aws_s3_bucket.raw_files.id}/codebuild/source.zip"',
        # inline buildspec は CodeBuild の 25,600 字上限で適用不能になったため、
        # content-addressed な S3 参照へ移行済み（2026-08-03）。参照先が
        # evidence バケット配下のプロジェクト名ディレクトリであることまで縛る。
        'buildspec = "${aws_s3_bucket.image_release_evidence.arn}'
        "/codebuild-buildspecs/${local.main_codebuild_project_name}/",
        "terraform_data.runtime_guard",
        "prevent_destroy = true",
    ):
        assert expected in project
    source_block = re.search(
        r"(?ms)^\s*source\s*\{(?P<body>.*?)^\s*\}",
        project,
    )
    assert source_block is not None
    assert "/codebuild/source.zip" in source_block.group("body")
    assert "ECR_REGISTRY" not in project
    assert "prevent_destroy = true" in policy_resource
    for expected in (
        "aws_ecr_repository.mcp_quarantine.arn",
        "aws_ecr_repository.mcp_media_quarantine.arn",
        "DenyMcpCandidateAndReleaseWrite",
        "DenyDynamicEnvironmentAndDebugChannels",
        "DenySourceEvidenceWritesAndSigning",
    ):
        assert expected in policy_document
    assert '"ecs:' not in policy_document
    assert '"iam:PassRole"' not in policy_document
    guard = GUARD.read_text(encoding="utf-8")
    assert "validate_quarantine_builder_and_admin_noninterference_plan" in guard


def test_teamagent_codebuild_contract_wiring_follows_checked_in_bytes() -> None:
    path = TF_ROOT / "codebuild.tf"
    body = path.read_text(encoding="utf-8")
    attestor_template = (
        PROJECT_ROOT / "infra" / "codebuild" / "image-attestor-buildspec.yml"
    ).read_text(encoding="utf-8")
    runtime_contract = PROJECT_ROOT / "infra" / "codebuild" / "teamagent_runtime_contract.json"
    release_contract = (
        PROJECT_ROOT / "infra" / "codebuild" / "teamagent_core_media_release_contract.json"
    )

    assert re.search(
        r"(?m)^\s*runtime_contract_sha256\s*=\s*filesha256\("
        r'"\${path\.module}/\.\./codebuild/teamagent_runtime_contract\.json"\)$',
        body,
    )
    assert re.search(
        r"(?m)^\s*mcp_release_contract_path\s*=\s*"
        r'"\${path\.module}/\.\./codebuild/'
        r'teamagent_core_media_release_contract\.json"$',
        body,
    )
    assert re.search(
        r"(?m)^\s*mcp_release_contract_sha256\s*=\s*"
        r"filesha256\(local\.mcp_release_contract_path\)$",
        body,
    )

    attestor_wiring = body.split(
        "image_attestor_buildspec_1 = replace(",
        maxsplit=1,
    )[1].split(
        "image_promoter_buildspec_1 = replace(",
        maxsplit=1,
    )[0]
    assert attestor_template.count("__MCP_RUNTIME_CONTRACT_BASE64__") == 1
    for placeholder, filename in (
        ("__ACTUAL_IMAGE_EVIDENCE_BASE64__", "actual_image_evidence.py"),
        ("__SOURCE_PROVENANCE_BASE64__", "source_provenance.py"),
        (
            "__TEAMAGENT_BUNDLE_PROVENANCE_BASE64__",
            "teamagent_bundle_provenance.py",
        ),
        ("__VERIFY_ACTUAL_IMAGE_BASE64__", "verify_actual_image.sh"),
        (
            "__MCP_RUNTIME_CONTRACT_BASE64__",
            "teamagent_runtime_contract.json",
        ),
        (
            "__MCP_CONTRACT_BASE64__",
            "teamagent_core_media_release_contract.json",
        ),
    ):
        assert re.search(
            rf'"{re.escape(placeholder)}",\s*'
            rf'filebase64\("\${{path\.module}}/\.\./codebuild/'
            rf'{re.escape(filename)}"\)',
            attestor_wiring,
        )
    assert attestor_wiring.index("__MCP_RUNTIME_CONTRACT_BASE64__") < (
        attestor_wiring.index("__MCP_CONTRACT_BASE64__")
    )

    launcher_core = body.split(
        'data "aws_iam_policy_document" "codebuild_launcher_core" {',
        maxsplit=1,
    )[1].split(
        'resource "aws_iam_policy" "codebuild_launcher_core" {',
        maxsplit=1,
    )[0]
    # 369f1b0 で StartBuild 4文（契約 sha の値 pin を含む）は IAM の 6,144 非空白字上限の
    # ため専用 managed policy codebuild_launcher_start へ分離された。値 pin はそちらを見る。
    launcher_start = body.split(
        'data "aws_iam_policy_document" "codebuild_launcher_start" {',
        maxsplit=1,
    )[1].split(
        'resource "aws_iam_policy" "codebuild_launcher_start" {',
        maxsplit=1,
    )[0]
    for environment_name, local_name in (
        ("SOURCE_MANIFEST_CONTRACT_SHA256", "runtime_contract_sha256"),
        ("RELEASE_CONTRACT_SHA256", "mcp_release_contract_sha256"),
    ):
        assert re.search(
            rf"(?m)^\s*{environment_name}\s*=\s*local\.{local_name}$",
            body,
        )
    # 固定値 map（launcher_fixed_environment_values）を dynamic condition で消費して
    # いること＝契約 sha が「値ごと」IAM に固定される配線が生きていることを縛る。
    assert "local.launcher_fixed_environment_values" in launcher_start
    assert (
        'variable = "codebuild:environment.environmentVariables/${condition.key}.value"'
        in launcher_start
    )
    for environment_name, local_name in (
        ("SOURCE_MANIFEST_CONTRACT_SHA256", "runtime_contract_sha256"),
        ("RELEASE_CONTRACT_SHA256", "mcp_release_contract_sha256"),
    ):
        assert re.search(
            rf'environment\.environmentVariables/{environment_name}\.value"\s*'
            rf"values\s*=\s*\[local\.{local_name}\]",
            launcher_start,
        )

    runtime_sha256 = hashlib.sha256(runtime_contract.read_bytes()).hexdigest()
    release_sha256 = hashlib.sha256(release_contract.read_bytes()).hexdigest()
    # 契約 sha のリテラル焼き込みは core / start のどちらにも存在しないこと
    # （必ず local 経由＝apply で自動追随する形を保つ）。
    for block in (launcher_core, launcher_start):
        assert runtime_sha256 not in block
        assert release_sha256 not in block
