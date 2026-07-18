"""運用スクリプト5本（入れ込み v2・C6）の静的検証＋引数パス実行テスト。

AWS を呼ばない範囲で検証する:
- bash -n（構文）と set -euo pipefail（共通規約）
- fail-loud の要となる定数・env 名の綴り（タスク側 C1/C3 との契約）
- --help は exit 0 / 不明引数・必須欠落は exit 1（aws 呼び出し前に判定される）
"""

from __future__ import annotations

import os
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
    """Immutable VersionId staging/rollback and guarded deployment contract."""
    body = PUBLISH.read_text(encoding="utf-8")
    for needle in (
        "codebuild/connect-web-app.html",
        "s3api put-object",
        "s3api get-object",
        "--version-id",
        "PRODUCTION_APP_PROVENANCE",
        "connect_app_html_s3_version_id",
        "connect_app_html_sha256",
        "fresh active/rollback receipt",
        "core+media",
    ):
        assert needle in body, f"契約文字列が欠落: {needle}"
    for forbidden in (
        "force-new-deployment",
        "update-service",
        "register-task-definition",
        "s3api copy-object",
        "aws s3 cp",
        "/healthz",
    ):
        assert forbidden not in body


def test_unified_deploy_contract_strings() -> None:
    """旧 build+deploy 混在経路は fail-loud stub のまま固定する。"""
    body = UNIFIED.read_text(encoding="utf-8")
    for needle in (
        "permanently disabled",
        "build_teamagent_image.sh",
        "../terraform/README.md",
        "never uploads source",
        "never uploads source, starts CodeBuild, or changes",
    ):
        assert needle in body, f"契約文字列が欠落: {needle}"
    r = _run(str(UNIFIED))
    assert r.returncode == 64


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
    for script in (RUN_INGEST, PUBLISH):
        r = _run(str(script), "--no-such-flag")
        assert r.returncode == 1, f"{script.name} が不明引数で exit {r.returncode}"
        message = r.stdout + r.stderr
        assert "不明な引数" in message or "unknown mode" in message


def test_publish_stage_captures_and_reloads_one_exact_version_without_ecs(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    store = tmp_path / "immutable-object"
    calls = tmp_path / "aws-calls"
    fake_aws = fake_bin / "aws"
    fake_aws.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_AWS_CALLS"
if [ "$1:$2" = "s3api:put-object" ]; then
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--body" ]; then cp "$2" "$FAKE_S3_STORE"; break; fi
    shift
  done
  printf '{"VersionId":"candidate-version-123"}\\n'
elif [ "$1:$2" = "s3api:get-object" ]; then
  destination="${!#}"
  cp "$FAKE_S3_STORE" "$destination"
  printf 'candidate-version-123\\n'
else
  exit 97
fi
""",
        encoding="utf-8",
    )
    fake_aws.chmod(0o755)
    html = tmp_path / "candidate.html"
    html.write_text("<html>new immutable candidate</html>\n", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_S3_STORE": str(store),
        "FAKE_AWS_CALLS": str(calls),
    }

    completed = subprocess.run(
        [
            "bash",
            str(PUBLISH),
            "stage",
            "--src",
            str(html),
            "--manifest-sha256",
            "a" * 64,
            "--build-inputs-sha256",
            "b" * 64,
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "version_id=candidate-version-123" in completed.stdout
    assert "The upload is NOT deployed" in completed.stdout
    assert store.read_bytes() == html.read_bytes()
    call_log = calls.read_text(encoding="utf-8")
    assert "s3api put-object" in call_log
    assert "s3api get-object" in call_log and "--version-id candidate-version-123" in call_log
    assert "ecs " not in call_log


def test_direct_ingest_task_definition_registration_is_retired() -> None:
    r = _run(str(REGISTER))
    assert r.returncode == 64
    assert "permanently disabled" in r.stderr
    assert "plan_image_release.sh" in r.stderr
    body = REGISTER.read_text(encoding="utf-8").lower()
    assert "register-task-definition" not in body
    assert "update-service" not in body
