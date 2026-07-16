"""TeamAgent MCP final imageのruntime境界を静的に固定する回帰テスト。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "infra" / "docker" / "Dockerfile.teamagent-mcp"
TEXT = DOCKERFILE.read_text(encoding="utf-8")
TIKTOK_PACKAGE = ROOT / "tools" / "tiktok_scraper" / "package.json"
TIKTOK_LOCK = ROOT / "tools" / "tiktok_scraper" / "package-lock.json"

PYTHON_ARM64_DIGEST = "4fcdc7a15c936b4ffb35dac253215a8e679ea4d25d5e0ffa025fca0d6a702448"
UV_ARM64_DIGEST = "9941e2d8e06ff884d328905091eac0a6bc1e40e5ce12e6dd0de4ef4ee26baac4"
NODE_ARM64_DIGEST = "af01d58b748ec92b1d6e8e11429aad424fd1e68c848185399dca0596a1ab8f5c"
NODE_BINARY_SHA256 = "6bf69d0eda41a12030d5f28d958cd09ce323bc0c13f1ab4d8bb426933aa08812"
PLAYWRIGHT_CORE_ARCHIVE_SHA256 = "3740bba3a3e93b4c7bd7c0f6e3c0e5f8cdce498bef13511e4d534cc99a8ce198"
TORCH_ARM64_WHEEL_SHA256 = "4ecd8ecdb9ea1affa5f35d10501809d62dc713f7de9635e8098e760ddbeb852c"
E5_MODEL_REVISION = "3d7cfbdacd47fdda877c5cd8a79fbcc4f2a574f3"
CHROMIUM_ARCHIVE_SHA256 = "ec044b50ed065adeb4c5ffdb42d1529901cbaf897cdf542bfef8af01d6e0cc79"
CHROMIUM_ARM64_SHA256 = "c1aa0fb5b6c60eb093df69d9e40dd50ab2039d3ccba8836ef21880340a77af64"


def _stage(name: str, next_name: str | None = None) -> str:
    """Dockerfileからname stageだけを返す。"""

    start = re.search(rf"^FROM .+ AS {re.escape(name)}$", TEXT, re.MULTILINE)
    assert start is not None, f"missing Docker stage: {name}"
    if next_name is None:
        return TEXT[start.start() :]
    end = re.search(rf"^FROM .+ AS {re.escape(next_name)}$", TEXT[start.end() :], re.MULTILINE)
    assert end is not None, f"missing Docker stage after {name}: {next_name}"
    return TEXT[start.start() : start.end() + end.start()]


def test_external_images_are_pinned_to_verified_arm64_manifests() -> None:
    python_refs = re.findall(
        r"^FROM public\.ecr\.aws/docker/library/python:3\.11-slim@sha256:([0-9a-f]+)",
        TEXT,
        re.MULTILINE,
    )
    assert python_refs == [PYTHON_ARM64_DIGEST, PYTHON_ARM64_DIGEST]
    assert len(PYTHON_ARM64_DIGEST) == 64

    uv_refs = re.findall(
        r"^COPY --from=ghcr\.io/astral-sh/uv:latest@sha256:([0-9a-f]+)",
        TEXT,
        re.MULTILINE,
    )
    assert uv_refs == [UV_ARM64_DIGEST]
    assert len(UV_ARM64_DIGEST) == 64

    node_refs = re.findall(
        r"^FROM public\.ecr\.aws/docker/library/node:24-bookworm-slim@sha256:([0-9a-f]+)",
        TEXT,
        re.MULTILINE,
    )
    assert node_refs == [NODE_ARM64_DIGEST]
    assert len(NODE_ARM64_DIGEST) == 64


def test_builder_tools_do_not_cross_the_runtime_boundary() -> None:
    builder = _stage("builder-common", "builder-false")
    runtime = _stage("runtime-common")

    assert "build-essential" in builder
    assert "/usr/local/bin/uv" in builder
    assert "/usr/local/bin/uv" not in runtime

    forbidden_runtime_packages = {
        "build-essential",
        "curl",
        "dpkg-dev",
        "g++",
        "gcc",
        "libpq5",
        "make",
        "npm",
        "perl",
    }
    runtime_packages = set(re.findall(r"^\s{6}([a-z0-9][a-z0-9+.-]*) \\$", runtime, re.MULTILINE))
    assert runtime_packages.isdisjoint(forbidden_runtime_packages)
    assert not any(package.startswith("perl-modules") for package in runtime_packages)


def test_optional_build_ca_is_secret_mounted_and_never_copied() -> None:
    builder = _stage("builder-common", "builder-false")
    runtime = _stage("runtime-common")

    assert "--mount=type=secret,id=teamagent_ca,required=false" in builder
    assert "COPY /run/secrets" not in TEXT
    assert "/run/secrets/teamagent_ca" not in runtime


def test_optional_torch_wheel_cache_is_hash_checked_and_builder_only() -> None:
    builder = _stage("builder-common", "builder-false")
    runtime = _stage("runtime-common")

    assert "--mount=type=cache,id=teamagent-torch-wheels" in builder
    assert TORCH_ARM64_WHEEL_SHA256 in builder
    assert "sha256sum -c -" in builder
    assert "/var/cache/teamagent-wheels" not in runtime


def test_scrape_flag_selects_separate_builder_and_runtime_stages() -> None:
    for stage in ("builder-false", "builder-true", "runtime-false", "runtime-true"):
        assert re.search(rf"^FROM .+ AS {stage}$", TEXT, re.MULTILINE)

    assert "FROM runtime-${WITH_SCRAPE_TOOLS} AS final" in TEXT
    runtime_false = _stage("runtime-false", "runtime-true")
    runtime_true = _stage("runtime-true", "final")
    assert "/opt/pw" not in runtime_false
    assert "apt-get install" not in runtime_false
    assert 'test "$TEAMAGENT_WITH_SCRAPE_TOOLS" = "false"' in runtime_false
    assert "local_files_only=True" in runtime_false
    assert "uv pip uninstall --python /app/.venv/bin/python playwright" in _stage(
        "builder-false", "builder-true"
    )
    assert "COPY --from=builder-true /opt/pw/ /opt/pw/" in runtime_true
    assert (
        'CHROME_BIN="/opt/pw/chromium-${TEAMAGENT_CHROMIUM_REVISION}/chrome-linux/chrome"'
        in runtime_true
    )
    assert 'ln -sf "$CHROME_BIN" /usr/bin/chromium' in runtime_true
    assert 'test "$TEAMAGENT_WITH_SCRAPE_TOOLS" = "true"' in runtime_true
    assert "COPY --from=node-toolchain /usr/local/bin/node /usr/local/bin/node" in runtime_true
    assert "ffmpeg" in runtime_true
    assert "fonts-noto-cjk" in runtime_true
    assert "node -e \"require.resolve('puppeteer-core'" in runtime_true
    assert "puppeteer.launch" in runtime_true
    assert "teamagent-python-playwright-smoke" in runtime_true
    assert "chromium --headless" in runtime_true
    assert "local_files_only=True" in runtime_true


def test_scrape_toolchain_versions_are_pinned_and_smoke_tested() -> None:
    builder_true = _stage("builder-true", "runtime-common")
    node_toolchain = _stage("node-toolchain", "builder-common")
    runtime_true = _stage("runtime-true", "final")

    assert f"@sha256:{NODE_ARM64_DIGEST} AS node-toolchain" in node_toolchain
    assert "ARG NODE_VERSION=24.18.0" in TEXT
    assert f"ARG NODE_BINARY_SHA256={NODE_BINARY_SHA256}" in TEXT
    assert 'test "$(node --version)" = "v${NODE_VERSION}"' in node_toolchain
    assert f"ARG PLAYWRIGHT_CHROMIUM_ARCHIVE_SHA256={CHROMIUM_ARCHIVE_SHA256}" in TEXT
    assert f"ARG PLAYWRIGHT_CHROMIUM_SHA256={CHROMIUM_ARM64_SHA256}" in TEXT
    assert "ARG PLAYWRIGHT_VERSION=1.61.1" in TEXT
    assert 'test "$PLAYWRIGHT_VERSION" = "1.61.1"' in builder_true
    assert f"ARG PLAYWRIGHT_CORE_ARCHIVE_SHA256={PLAYWRIGHT_CORE_ARCHIVE_SHA256}" in TEXT
    assert "playwright-core-${PLAYWRIGHT_VERSION}.tgz" in builder_true
    assert 'archive.extractfile("package/browsers.json")' in builder_true
    assert "ARG PLAYWRIGHT_CHROMIUM_REVISION=1228" in TEXT
    assert "ARG PLAYWRIGHT_CHROMIUM_BROWSER_VERSION=149.0.7827.55" in TEXT
    assert "ARG PLAYWRIGHT_CHROMIUM_VERSION=149.0.7827.0" in TEXT
    assert "chromium/${PLAYWRIGHT_CHROMIUM_REVISION}/chromium-linux-arm64.zip" in builder_true
    assert "npx" not in builder_true
    assert '"$PLAYWRIGHT_CHROMIUM_ARCHIVE_SHA256" "$CHROMIUM_ARCHIVE"' in builder_true
    assert '"$PLAYWRIGHT_CHROMIUM_SHA256" "$CHROME_BIN"' in builder_true
    assert "sha256sum -c -" in builder_true
    assert "nodejs=" not in runtime_true
    assert "npm=" not in runtime_true
    assert 'test "$(node --version)" = "v${TEAMAGENT_NODE_VERSION}"' in runtime_true
    assert 'test "$(chromium --version' in runtime_true
    assert "chromium --headless" in runtime_true
    assert "puppeteer.launch" in runtime_true
    assert "playwright.sync_api" in runtime_true
    assert "node --check /app/tools/tiktok_scraper/search.mjs" in runtime_true
    assert "ffmpeg -v error -f lavfi" in runtime_true
    assert "playwright/driver/node" in builder_true
    assert "ln -s /usr/local/bin/node" in builder_true
    assert "find \"$CHROME_DIR\" -type f -name '*.info' -delete" in builder_true
    assert NODE_BINARY_SHA256 in TEXT
    assert CHROMIUM_ARCHIVE_SHA256 in TEXT
    assert CHROMIUM_ARM64_SHA256 in TEXT


def test_tiktok_scraper_puppeteer_is_exactly_locked_for_node_24() -> None:
    package = TIKTOK_PACKAGE.read_text(encoding="utf-8")
    lock = TIKTOK_LOCK.read_text(encoding="utf-8")

    assert '"puppeteer-core": "25.3.0"' in package
    assert lock.count('"puppeteer-core": "25.3.0"') == 1
    assert '"node_modules/puppeteer-core": {' in lock
    assert '"version": "25.3.0"' in lock
    assert (
        '"resolved": "https://registry.npmjs.org/puppeteer-core/-/puppeteer-core-25.3.0.tgz"'
        in lock
    )


def test_e5_model_is_commit_pinned_and_runtime_offline() -> None:
    builder = _stage("builder-common", "builder-false")
    runtime = _stage("runtime-common")
    final = _stage("final")

    assert len(E5_MODEL_REVISION) == 40
    assert f"ARG E5_MODEL_REVISION={E5_MODEL_REVISION}" in TEXT
    assert "revision=os.environ['TEAMAGENT_E5_MODEL_REVISION']" in builder
    assert 'printf \'%s\' "$E5_MODEL_REVISION" > "$MODEL_CACHE/refs/main"' in builder
    assert "HF_HUB_OFFLINE=1" in runtime
    assert "TRANSFORMERS_OFFLINE=1" in runtime
    assert "local_files_only=True" in builder
    assert "io.teamagent.build.e5-model-revision" in final


def test_scrape_build_arg_is_recorded_in_runtime_env_and_oci_label() -> None:
    runtime_common = _stage("runtime-common", "runtime-false")

    assert "TEAMAGENT_WITH_SCRAPE_TOOLS=$WITH_SCRAPE_TOOLS" in runtime_common
    assert "org.opencontainers.image.revision=$GIT_COMMIT" in runtime_common
    assert "io.teamagent.build.with-scrape-tools=$WITH_SCRAPE_TOOLS" in runtime_common


def test_app_html_provenance_is_required_verified_and_labeled() -> None:
    provenance = _stage("app-html-provenance", "runtime-false")
    final = _stage("final")

    assert re.search(r"^ARG APP_HTML_SHA256$", TEXT, re.MULTILINE)
    assert not re.search(r"^ARG APP_HTML_VERSION_ID=", TEXT, re.MULTILINE)
    assert (
        "--mount=type=bind,source=src/teamagent/connect_web/static/app.html,target=/tmp/app.html"
        in provenance
    )
    assert "*[!0-9a-f]*" in provenance
    assert 'test "${#APP_HTML_SHA256}" -eq 64' in provenance
    assert "sha256sum -c -" in provenance
    assert "*[!A-Za-z0-9._~+/=-]*" in provenance
    assert 'test "${#APP_HTML_VERSION_ID}" -le 1024' in provenance
    assert "from=app-html-provenance" in final
    assert "/app/src/teamagent/connect_web/static/app.html | sha256sum -c -" in final
    assert 'io.teamagent.build.app-html-sha256="$APP_HTML_SHA256"' in final
    assert 'io.teamagent.build.app-html-version-id="$APP_HTML_VERSION_ID"' in final
    assert 'io.teamagent.build.chromium-sha256="$PLAYWRIGHT_CHROMIUM_SHA256"' in final
    assert "io.teamagent.build.playwright-core-archive-sha256" in final
    assert "io.teamagent.build.chromium-browser-version" in final


def test_runtime_contract_stays_non_root_and_uses_python_healthcheck() -> None:
    final = _stage("final")
    assert "USER mcp" in final
    assert 'CMD ["python", "scripts/run_mcp_http_server.py"]' in final
    assert "HEALTHCHECK --interval=30s --timeout=5s --retries=5 --start-period=40s" in final
    assert "urllib.request.urlopen" in final
    assert "curl" not in final
    assert "ENTRYPOINT" not in TEXT


def test_runtime_removes_perl_and_other_unneeded_tools() -> None:
    runtime_false = _stage("runtime-false", "runtime-true")
    runtime_true = _stage("runtime-true", "final")

    for runtime in (runtime_false, runtime_true):
        assert "apt-get purge -y --allow-remove-essential perl-base" in runtime
        assert "! command -v perl" in runtime
        assert "! dpkg-query -W perl-base" in runtime
        assert "for package in build-essential dpkg-dev perl perl-base libpq5 curl npm" in runtime
        assert "unexpected package: perl-modules" in runtime
        assert "rm -rf /var/cache/apt /var/lib/apt/lists" in runtime
        assert "test ! -e /var/cache/apt" in runtime
        assert "test ! -e /var/lib/apt/lists" in runtime


def test_runtime_removes_global_python_packagers() -> None:
    runtime_false = _stage("runtime-false", "runtime-true")
    runtime_true = _stage("runtime-true", "final")

    for runtime in (runtime_false, runtime_true):
        assert "/usr/local/bin/python -m pip uninstall -y pip setuptools wheel packaging" in runtime
        assert "rm -rf /root/.cache/pip" in runtime
        assert "! /usr/local/bin/python -m pip --version" in runtime
