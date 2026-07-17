"""teamagent-mcp-core の再現可能なruntime分離契約。"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "infra/docker/Dockerfile.teamagent-mcp"
DOCKERIGNORE = ROOT / "infra/docker/Dockerfile.teamagent-mcp.dockerignore"
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "uv.lock"
TEXT = DOCKERFILE.read_text(encoding="utf-8")

PYTHON_BUILDER_DIGEST = "2eac0b3ef42685b2d45d57633364aaa87ec54bf29960dcf7ecd0eed20e14d124"
PYTHON_RUNTIME_DIGEST = "b7fda4f2d99284fe078f751034a0c858676f3456c4d75f1e935527c1951b5ba9"
UV_DIGEST = "9941e2d8e06ff884d328905091eac0a6bc1e40e5ce12e6dd0de4ef4ee26baac4"
PYTHON_BINARY_SHA256 = "0d036a463b218cff354adfb9c09a969a9a659698fa376bd3b55fe5bc002e7af8"
UV_BINARY_SHA256 = "f32f61ced7feb20342032cdac4d0825cebbda61911554f5de5231ec72821812e"
TORCH_WHEEL_SHA256 = "797c066367792c92eb97cafba7fd0caa8d7455e6078a4ee880630077378dc372"
E5_MODEL_REVISION = "3d7cfbdacd47fdda877c5cd8a79fbcc4f2a574f3"
BAKED_APP_HTML_SHA256 = "716ac25a96516efd6443277c903102d514f3f86729f8706baea41ee48f0ecdeb"
APP_HTML_SHA256 = "46f0079783cde24b066c7823b7d6672bad12b33debf933a4d7a7ff04b7a3b067"
APP_HTML_VERSION_ID = "I1qOb7Kwl.pMg71wqFxbHnbbTqMWjQcY"
APP_HTML_MANIFEST_SHA256 = "15663a838b1bd648443949244c02e66ccfd6cb7b684390baeb1a86efcdd6d4a2"
APP_HTML_BUILD_INPUTS_SHA256 = "1ca6f0213155d8d4dbef4220f641dbb38310fe79473f6c013ef4e54dfa6a87e2"


def _stage(name: str, next_name: str | None = None) -> str:
    start = re.search(rf"^FROM .+ AS {re.escape(name)}$", TEXT, re.MULTILINE)
    assert start is not None, f"missing Docker stage: {name}"
    if next_name is None:
        return TEXT[start.start() :]
    tail = TEXT[start.end() :]
    end = re.search(rf"^FROM .+ AS {re.escape(next_name)}$", tail, re.MULTILINE)
    assert end is not None, f"missing Docker stage after {name}: {next_name}"
    return TEXT[start.start() : start.end() + end.start()]


def test_core_uses_exact_arm64_child_digests_and_binary_hashes() -> None:
    assert f"ARG PYTHON_BUILDER_ARM64_DIGEST=sha256:{PYTHON_BUILDER_DIGEST}" in TEXT
    assert f"ARG PYTHON_RUNTIME_ARM64_DIGEST=sha256:{PYTHON_RUNTIME_DIGEST}" in TEXT
    assert f"ARG UV_ARM64_DIGEST=sha256:{UV_DIGEST}" in TEXT
    assert f"ARG PYTHON_BINARY_SHA256={PYTHON_BINARY_SHA256}" in TEXT
    assert f"ARG UV_BINARY_SHA256={UV_BINARY_SHA256}" in TEXT
    assert "ARG PYTHON_VERSION=3.14.6" in TEXT
    assert "cgr.dev/chainguard/python:latest-dev@${PYTHON_BUILDER_ARM64_DIGEST}" in TEXT
    assert "cgr.dev/chainguard/python:latest@${PYTHON_RUNTIME_ARM64_DIGEST}" in TEXT
    assert "ghcr.io/astral-sh/uv:latest@${UV_ARM64_DIGEST}" in TEXT
    assert TEXT.count("sha256sum -c -") >= 6


def test_core_contains_e5_mcp_db_aws_but_no_media_or_js_runtime() -> None:
    builder = _stage("builder", "final")
    assert "--extra mcp --extra embeddings" in builder
    for required in ("anthropic", "boto3", "psycopg", "sentence_transformers"):
        assert required in builder
    assert "teamagent.media.contracts" in builder
    for blocked in ("playwright", "yt_dlp", "weasyprint", "pptx", "claude_agent_sdk"):
        assert f"'{blocked}'" in builder
    for binary in ("node", "bun", "npm", "npx", "chromium", "ffmpeg", "yt-dlp"):
        assert f"-name {binary}" in builder
    assert "claude_agent_sdk/_bundled/claude" in builder
    assert "COPY tools/" not in TEXT
    assert "apt-get" not in TEXT
    assert "apk add" not in TEXT


def test_core_model_torch_and_app_html_are_content_addressed() -> None:
    assert f"ARG E5_MODEL_REVISION={E5_MODEL_REVISION}" in TEXT
    assert f"ARG TORCH_ARM64_WHEEL_SHA256={TORCH_WHEEL_SHA256}" in TEXT
    assert "torch-2.12.0%2Bcpu-cp314-cp314-manylinux_2_28_aarch64.whl" in TEXT
    assert "revision=os.environ['TEAMAGENT_E5_MODEL_REVISION']" in TEXT
    assert TEXT.count("local_files_only=True") >= 2
    assert "HF_HUB_OFFLINE=1" in TEXT
    assert "TRANSFORMERS_OFFLINE=1" in TEXT
    for name, value in {
        "BAKED_APP_HTML_SHA256": BAKED_APP_HTML_SHA256,
        "APP_HTML_SHA256": APP_HTML_SHA256,
        "APP_HTML_VERSION_ID": APP_HTML_VERSION_ID,
        "APP_HTML_MANIFEST_SHA256": APP_HTML_MANIFEST_SHA256,
        "APP_HTML_BUILD_INPUTS_SHA256": APP_HTML_BUILD_INPUTS_SHA256,
    }.items():
        assert f"ARG {name}={value}" in TEXT
    assert "ARG APP_HTML_SOURCE=s3" in TEXT
    assert "/app/src/teamagent/connect_web/static/app.html" in TEXT
    assert 'io.teamagent.contract.baked-app-html-sha256="$BAKED_APP_HTML_SHA256"' in TEXT
    assert 'io.teamagent.contract.app-html-source="$APP_HTML_SOURCE"' in TEXT
    assert 'io.teamagent.contract.app-html-sha256="$APP_HTML_SHA256"' in TEXT
    assert 'io.teamagent.contract.app-html-version-id="$APP_HTML_VERSION_ID"' in TEXT
    assert 'io.teamagent.contract.app-html-manifest-sha256="$APP_HTML_MANIFEST_SHA256"' in TEXT
    assert (
        'io.teamagent.contract.app-html-build-inputs-sha256="$APP_HTML_BUILD_INPUTS_SHA256"' in TEXT
    )
    assert 'org.opencontainers.image.revision="$GIT_COMMIT"' in TEXT


def test_core_runtime_is_uid_10001_read_only_ready_and_python_health_checked() -> None:
    final = _stage("final")
    assert "USER 10001:10001" in final
    assert "COPY --from=builder /runtime-etc/passwd /etc/passwd" in final
    assert "teamagent:x:10001:10001:" in TEXT
    assert "USER=teamagent" in final
    assert "LOGNAME=teamagent" in final
    assert 'VOLUME ["/tmp"]' in final
    assert "HOME=/tmp/teamagent/home" in final
    assert "TMPDIR=/tmp/teamagent/tmp" in final
    assert "XDG_CACHE_HOME=/tmp/teamagent/cache" in final
    assert "VIDEO_APPROVAL_STATE_PATH=/tmp/teamagent/state/video_approval_processed.json" in final
    assert "TEAMAGENT_RUNTIME_KIND=core" in final
    assert 'ENTRYPOINT ["/app/.venv/bin/python"]' in final
    assert 'CMD ["scripts/run_mcp_http_server.py"]' in final
    assert "urllib.request.urlopen('http://127.0.0.1:8787/healthz'" in final
    assert "curl" not in final


def test_core_build_ca_is_secret_mounted_and_package_provenance_is_retained() -> None:
    builder = _stage("builder", "final")
    assert "--mount=type=secret,id=teamagent_ca,required=false" in builder
    assert "COPY /run/secrets" not in TEXT
    assert "uv pip freeze" in builder
    assert "python-builder-arm64.digest" in builder
    assert "python-runtime-arm64.digest" in builder
    assert "uv-arm64.digest" in builder


def test_core_dockerignore_is_deny_by_default_and_excludes_sensitive_state() -> None:
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    assert text.startswith("**\n")
    for allowed in ("!pyproject.toml", "!uv.lock", "!src/**", "!scripts/**"):
        assert allowed in text
    for blocked in (
        "**/.env",
        "**/*.pem",
        "**/*.key",
        "**/*.tfstate",
        "**/__pycache__/",
        "**/node_modules/",
        "**/*.sqlite",
    ):
        assert blocked in text


def test_core_lock_has_no_bundled_agent_or_media_runtime_dependencies() -> None:
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    lock = LOCK.read_text(encoding="utf-8")
    base = tomllib.loads(pyproject)["project"]["dependencies"]
    for dependency in ("claude-agent-sdk", "yt-dlp", "playwright", "weasyprint", "python-pptx"):
        assert not any(item.startswith(dependency) for item in base)
    assert 'name = "claude-agent-sdk"' not in lock
    assert '"playwright==1.60.0"' in pyproject
    assert '"yt-dlp==2026.6.9"' in pyproject
