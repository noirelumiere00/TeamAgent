"""運用スクリプト5本（入れ込み v2・C6）の静的検証＋引数パス実行テスト。

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
ALL_SCRIPTS = [RUN_INGEST, PUBLISH, BOOTSTRAP, REGISTER, UNIFIED]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", *args], capture_output=True, text=True, timeout=30)


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
    for script in (RUN_INGEST, PUBLISH, REGISTER):
        r = _run(str(script), "--help")
        assert r.returncode == 0, f"{script.name} --help が exit {r.returncode}: {r.stderr}"
        assert "usage" in r.stdout.lower()


def test_unknown_arg_exits_nonzero() -> None:
    for script in (RUN_INGEST, PUBLISH, REGISTER):
        r = _run(str(script), "--no-such-flag")
        assert r.returncode == 1, f"{script.name} が不明引数で exit {r.returncode}"
        assert "不明な引数" in r.stdout + r.stderr


def test_register_requires_image_tag() -> None:
    r = _run(str(REGISTER))
    assert r.returncode == 1
    assert "--image-tag" in r.stdout + r.stderr
