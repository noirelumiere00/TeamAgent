"""運用スクリプトの静的検証＋引数パス実行テスト。

AWS を呼ばない範囲で検証する:
- bash -n（構文）と set -euo pipefail（共通規約）
- fail-loud の要となる定数・env 名の綴り（タスク側 C1/C3 との契約）
- --help は exit 0 / 不明引数・必須欠落は exit 1（aws 呼び出し前に判定される）
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

RUN_INGEST = PROJECT_ROOT / "scripts" / "aws" / "run_ingest_task.sh"
PUBLISH = PROJECT_ROOT / "infra" / "deploy" / "publish_app_html.sh"
BOOTSTRAP = PROJECT_ROOT / "infra" / "deploy" / "bootstrap_apphtml_s3_iam.sh"
REGISTER = PROJECT_ROOT / "infra" / "deploy" / "register_ingest_td.sh"
UNIFIED = PROJECT_ROOT / "infra" / "deploy" / "deploy_connectweb_unified.sh"
TERRAFORM_GUARD = PROJECT_ROOT / "infra" / "deploy" / "terraform_runtime_guard.sh"
APPLY_RESILIENCE = PROJECT_ROOT / "infra" / "terraform" / "apply_resilience.sh"
ALL_SCRIPTS = [
    RUN_INGEST,
    PUBLISH,
    BOOTSTRAP,
    REGISTER,
    UNIFIED,
    TERRAFORM_GUARD,
    APPLY_RESILIENCE,
]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", *args], capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_syntax_ok(script: Path) -> None:
    assert script.exists(), f"スクリプトが無い: {script}"
    r = _run("-n", str(script))
    assert r.returncode == 0, f"構文エラー: {r.stderr}"


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_fail_loud_shell_options(script: Path) -> None:
    """共通規約: set -euo pipefail（黙って続行しない）。"""
    assert "set -euo pipefail" in script.read_text(encoding="utf-8")


def test_run_ingest_task_contract_strings() -> None:
    """タスク側（C3）と publish 先の契約が綴りレベルで一致していること。"""
    body = RUN_INGEST.read_text(encoding="utf-8")
    for needle in (
        "INGEST_SOURCES_S3_URI",
        "INGEST_SOURCES_SHA256",
        "USE_DOC_KIND_RULES",
        "INGEST_MARK_STALE",
        "INGEST_STALE_ALLOW_MASS",
        "INGEST_ROOT_CHECK_WARN_ONLY",  # --root-check-warn-only が注入する env
        "--root-check-warn-only",  # ルート検査降格をタスクへ届ける唯一の経路
        "${YAML_SHA:0:12}",  # タスクログは sha を12hex短縮出力（full 64hex grep は恒久偽警告）
        "teamagent-dev-ingest-weekly",
        "config/ingest_sources.yaml",
        "shared_drives",  # crawl の kind トークンを usage に明記
    ):
        assert needle in body, f"契約文字列が欠落: {needle}"


def test_publish_contract_strings() -> None:
    """healthz 検証（C1）と S3 配置先の契約。"""
    body = PUBLISH.read_text(encoding="utf-8")
    for needle in (
        "app_html_sha256",
        "app_html_source",
        "CONNECT_APP_HTML_S3_URI",
        "codebuild/connect-web-app.html",
        "force-new-deployment",
        "apphtml-s3-read",
    ):
        assert needle in body, f"契約文字列が欠落: {needle}"


def test_unified_deploy_contract_strings() -> None:
    """unified bake が td へ宣言的に固定する env と image tag 表示の契約。

    - CONNECT_APP_HTML_S3_URI: publish_app_html.sh ホットスワップの受け口
      （これが無いと publish が preflight で恒久 exit 1）
    - USE_QUERY_PLANNER / USE_COHERE_RERANK: T1 No-AI 化の恒久化（bake での巻き戻り防止）
    - image tag 表示: register_ingest_td.sh --image-tag へ渡すタグの唯一の出所
    """
    body = UNIFIED.read_text(encoding="utf-8")
    for needle in (
        "CONNECT_APP_HTML_S3_URI",
        "USE_QUERY_PLANNER",
        "USE_COHERE_RERANK",
        "codebuild/connect-web-app.html",  # publish_app_html.sh の配置先と同一定数
        "image tag: ${TAG}",
        "register_ingest_td.sh --image-tag ${TAG}",
    ):
        assert needle in body, f"契約文字列が欠落: {needle}"


def test_bootstrap_contract_strings() -> None:
    body = BOOTSTRAP.read_text(encoding="utf-8")
    assert "apphtml-s3-read" in body
    assert "ingest-yaml-s3-read" in body
    assert "s3:GetObject" in body


def test_help_exits_zero_without_aws() -> None:
    for script in (RUN_INGEST, PUBLISH, REGISTER, TERRAFORM_GUARD):
        r = _run(str(script), "--help")
        assert r.returncode == 0, f"{script.name} --help が exit {r.returncode}: {r.stderr}"
        assert "usage" in r.stdout.lower()


def test_unknown_arg_exits_nonzero() -> None:
    for script in (RUN_INGEST, PUBLISH, REGISTER):
        r = _run(str(script), "--no-such-flag")
        assert r.returncode == 1, f"{script.name} が不明引数で exit {r.returncode}"
        assert "不明な引数" in r.stdout + r.stderr

    r = _run(str(TERRAFORM_GUARD), "snapshot", "--no-such-flag")
    assert r.returncode == 1
    assert "不明な引数" in r.stdout + r.stderr


def test_register_requires_image_tag() -> None:
    r = _run(str(REGISTER))
    assert r.returncode == 1
    assert "--image-tag" in r.stdout + r.stderr


def test_terraform_guard_requires_saved_complete_plan() -> None:
    """AWS CLI を呼ぶ前にplain/targeted planとunbound applyを拒否する。"""
    r = _run(str(TERRAFORM_GUARD), "plan")
    assert r.returncode == 1
    assert "--var-file" in r.stdout + r.stderr

    r = _run(
        str(TERRAFORM_GUARD),
        "plan",
        "--var-file",
        str(PROJECT_ROOT / "infra" / "terraform" / "terraform.tfvars.example"),
        "--out",
        "/tmp/should-not-exist.tfplan",
    )
    assert r.returncode == 1
    assert "--runtime-sync" in r.stdout + r.stderr

    r = _run(str(TERRAFORM_GUARD), "apply")
    assert r.returncode == 1
    assert "--plan" in r.stdout + r.stderr


def test_terraform_guard_contract_strings() -> None:
    body = TERRAFORM_GUARD.read_text(encoding="utf-8")
    for needle in (
        "runtime_guard_live",
        "live_fingerprint_sha256",
        "runtime_guard_sha256",
        "plan_sha256",
        "--runtime-sync",
        "--runtime-migration",
        "enable-log-versioning",
        "--versioning-receipt",
        "--preflight-receipt",
        "preflight_receipt_sha256",
        "hmac_transition_sha256",
        "desired_openclaw_image",
        "desired_x_image",
        "desired rules",
        "非許可の destroy/replace",
        "env/secretsがliveと完全一致",
        "plan 作成中に live runtime が変化",
        "read-only検証完了",
        "assert_clean_terraform_environment",
        ".complete == true",
        "capture_state_contract",
        "workspace show",
        "state pull",
        "state list",
        "acquire_deployment_lock",
        "verify_versioning_enable_receipt",
        "verify_log_readiness_receipt",
        "verify_alarm_delivery_test_receipt",
        "umask 077",
        "assert_git_tracked_clean",
        "ls-files --error-unmatch",
        'rev-parse "HEAD:$relative"',
        'hash-object -- "$path"',
        "diff --quiet HEAD",
        '-path "$TF_DIR/.terraform" -prune',
        '-path "$TF_DIR/build" -prune',
    ):
        assert needle in body, f"Terraform guard契約が欠落: {needle}"
    assert "-auto-approve" not in body
    assert 'terraform -chdir="$TF_DIR" apply' in body
    assert '"$TMP_ROOT/verify/plan.tfplan"' in body
    assert "--confirm-plan-sha" not in body
    assert "--target)" not in body
    assert "-target=" not in body
    assert "--allow-runtime" not in body


def test_terraform_runtime_preconditions_cover_all_managed_runtimes() -> None:
    tf_dir = PROJECT_ROOT / "infra" / "terraform"
    runtime_guard = (tf_dir / "runtime_guard.tf").read_text(encoding="utf-8")
    assert "runtime_guard_verified" in runtime_guard
    assert "default  = null" in runtime_guard

    for filename in ("fargate.tf", "connect_web.tf"):
        body = (tf_dir / filename).read_text(encoding="utf-8")
        assert body.count("local.runtime_guard_verified") >= 2, (
            f"task/service precondition欠落: {filename}"
        )
        assert "create_before_destroy = true" in body, (
            f"安全なtask definition置換順が欠落: {filename}"
        )

    for filename in ("tiktok_acquire.tf", "x_research.tf"):
        body = (tf_dir / filename).read_text(encoding="utf-8")
        assert "create_before_destroy = true" in body
        assert body.count("local.runtime_guard_verified") >= 3


def test_resilience_helper_is_explicitly_retired() -> None:
    body = APPLY_RESILIENCE.read_text(encoding="utf-8")
    assert "terraform plan" not in body
    assert "terraform apply" not in body
    assert "retired" in body.lower()

    result = _run(str(APPLY_RESILIENCE))
    assert result.returncode == 64
    assert "retired" in result.stderr.lower()


def test_worker_runtime_and_iam_hardening_contracts() -> None:
    tf_dir = PROJECT_ROOT / "infra" / "terraform"
    tiktok = (tf_dir / "tiktok_acquire.tf").read_text(encoding="utf-8")
    x_buzz = (tf_dir / "x_research.tf").read_text(encoding="utf-8")

    for body in (tiktok, x_buzz):
        for needle in (
            "@sha256:[0-9a-f]{64}",
            'user                   = "10001:10001"',
            "readonlyRootFilesystem = true",
            "initProcessEnabled = true",
            'drop = ["ALL"]',
            'name = "tmp"',
            'containerPath = "/tmp"',
            "ephemeral_storage",
            "size_in_gib",
        ):
            assert needle in body

    tiktok_task_policy = tiktok.split('data "aws_iam_policy_document" "tiktok_task_app"', 1)[
        1
    ].split('resource "aws_iam_role_policy" "tiktok_task_app"', 1)[0]
    assert 'actions   = ["dynamodb:UpdateItem", "dynamodb:GetItem"]' in (tiktok_task_policy)
    assert "dynamodb:PutItem" not in tiktok_task_policy
    assert "logs:PutLogEvents" not in tiktok_task_policy

    tiktok_mcp_policy = tiktok.split('data "aws_iam_policy_document" "tiktok_mcp_policy"', 1)[
        1
    ].split('resource "aws_iam_role_policy" "tiktok_mcp_policy"', 1)[0]
    assert 'actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]' in (tiktok_mcp_policy)

    x_worker_policy = x_buzz.split('data "aws_iam_policy_document" "x_buzz_task_app"', 1)[1].split(
        'resource "aws_iam_role_policy" "x_buzz_task_app"', 1
    )[0]
    assert 'actions   = ["dynamodb:UpdateItem", "dynamodb:GetItem"]' in (x_worker_policy)
    assert "logs:PutLogEvents" not in x_worker_policy


def test_worker_vpc_endpoint_and_python_healthcheck_contracts() -> None:
    tf_dir = PROJECT_ROOT / "infra" / "terraform"
    endpoints = (tf_dir / "vpc_endpoints.tf").read_text(encoding="utf-8")
    assert "aws_security_group.tiktok_tasks[0].id" in endpoints
    assert "aws_security_group.x_buzz_tasks[0].id" in endpoints

    for filename, port in (("fargate.tf", "8787"), ("connect_web.tf", "8788")):
        body = (tf_dir / filename).read_text(encoding="utf-8")
        assert "urllib.request.urlopen" in body
        assert f"127.0.0.1:{port}/healthz" in body
        assert "curl -fsS" not in body


def test_x_research_disabled_path_has_counted_policy_documents() -> None:
    """enable_x_research=falseでもcount=0 resourceを[0]参照してplan評価エラーにしない。"""
    body = (PROJECT_ROOT / "infra" / "terraform" / "x_research.tf").read_text(encoding="utf-8")
    for name in (
        "x_buzz_exec_secrets",
        "x_buzz_task_app",
        "x_dispatch_policy",
        "x_mcp_policy",
    ):
        declaration = f'data "aws_iam_policy_document" "{name}" {{\n  count = local.xr_enabled'
        assert declaration in body, f"count gate欠落: {name}"
        assert f"data.aws_iam_policy_document.{name}[0].json" in body
        assert f"from = data.aws_iam_policy_document.{name}" in body
        assert f"to   = data.aws_iam_policy_document.{name}[0]" in body
