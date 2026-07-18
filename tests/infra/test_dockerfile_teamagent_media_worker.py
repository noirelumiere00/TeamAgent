"""teamagent-media-worker の再現可能性・分離・sanitization契約。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "infra/docker/Dockerfile.teamagent-media-worker"
DOCKERIGNORE = ROOT / "infra/docker/Dockerfile.teamagent-media-worker.dockerignore"
APK_LOCK = ROOT / "infra/docker/media-apk.lock"
SANITIZER = ROOT / "infra/docker/sanitize_ytdlp.py"
PACKAGE = ROOT / "tools/tiktok_scraper/package.json"
PACKAGE_LOCK = ROOT / "tools/tiktok_scraper/package-lock.json"
SCRAPER = ROOT / "tools/tiktok_scraper/search.mjs"
WORKER = ROOT / "src/teamagent/media/worker.py"
TEXT = DOCKERFILE.read_text(encoding="utf-8")

CHROMIUM_BASE_DIGEST = "ee09ed198c66003a3f15024ca4f8f8613b9a97fdfd0dce8600969fc8a69ecc04"
NODE_BUILDER_DIGEST = "eef73a25205e27bd016ce672af71560ad6b681142ddf00ff63c7b3098eafcd4d"
UV_DIGEST = "9941e2d8e06ff884d328905091eac0a6bc1e40e5ce12e6dd0de4ef4ee26baac4"
APK_LOCK_SHA256 = "d3a888e3bfa7e75d2c4c7b6b6dca2a7e0812c1231fdb342e96bb79af9166cca8"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_media_external_images_are_exact_arm64_children() -> None:
    assert f"ARG CHROMIUM_BASE_ARM64_DIGEST=sha256:{CHROMIUM_BASE_DIGEST}" in TEXT
    assert f"ARG NODE_BUILDER_ARM64_DIGEST=sha256:{NODE_BUILDER_DIGEST}" in TEXT
    assert f"ARG UV_ARM64_DIGEST=sha256:{UV_DIGEST}" in TEXT
    assert "akorn/chromium-headless:150-alpine@${CHROMIUM_BASE_ARM64_DIGEST}" in TEXT
    assert "node:24-alpine@${NODE_BUILDER_ARM64_DIGEST}" in TEXT
    assert "ghcr.io/astral-sh/uv:latest@${UV_ARM64_DIGEST}" in TEXT
    assert 'org.opencontainers.image.revision="$GIT_COMMIT"' in TEXT
    assert 'io.teamagent.contract.node-builder-arm64-digest="$NODE_BUILDER_ARM64_DIGEST"' in TEXT
    assert 'io.teamagent.contract.uv-arm64-digest="$UV_ARM64_DIGEST"' in TEXT


def test_media_runtime_packages_versions_and_binaries_are_exact() -> None:
    expected = {
        "CHROMIUM_PACKAGE_VERSION": "150.0.7871.114-r0",
        "CHROMIUM_BINARY_SHA256": (
            "13eaa3cbe73f39b5feafcd767db0771c4f25d626a3927ba216ac43cde3abaf79"
        ),
        "FFMPEG_PACKAGE_VERSION": "8.1.2-r0",
        "FFMPEG_BINARY_SHA256": (
            "43aff9d9b8d8becd14f9e2a36a8497aa5c0e12454e60f7c0d3350ed5bef945ba"
        ),
        "NODE_PACKAGE_VERSION": "24.18.0-r0",
        "NODE_BINARY_SHA256": ("b9fb3beb6d397b33284966e6c1efb2056d6bc9ccc030f6f92caaac121c87a8e1"),
        "PYTHON_PACKAGE_VERSION": "3.14.5-r2",
        "PYTHON_BINARY_SHA256": (
            "95f57c0555bdc6237e2a70f1c88e0bcef04732131f2023728ea9c5baa63964c4"
        ),
    }
    for name, value in expected.items():
        assert f"ARG {name}={value}" in TEXT
    for package in ("ffmpeg", "font-liberation", "font-noto", "font-noto-cjk", "font-noto-emoji"):
        assert f'"{package}=$' in TEXT
    assert "apk list --installed" in TEXT
    assert "test -s /lib/apk/db/installed" in TEXT
    assert "rm -rf /lib/apk" not in TEXT


def test_apk_inventory_is_exact_and_hash_pinned() -> None:
    assert len(APK_LOCK.read_text(encoding="utf-8").splitlines()) == 240
    assert _sha256(APK_LOCK) == APK_LOCK_SHA256
    assert f"ARG MEDIA_APK_LOCK_SHA256={APK_LOCK_SHA256}" in TEXT
    assert "cmp /tmp/media-apk.lock /tmp/actual-apk.lock" in TEXT
    assert 'io.teamagent.contract.apk-lock-sha256="$MEDIA_APK_LOCK_SHA256"' in TEXT
    assert "--mount=type=cache,id=teamagent-media-apk-arm64,target=/var/cache/apk" in TEXT
    assert "https://dl-cdn.alpinelinux.org/alpine/edge" in TEXT
    assert "https://dl-cdn.alpinelinux.org/alpine/v3.24" in TEXT


def test_python_and_js_playwright_are_same_exact_version_and_hashed() -> None:
    assert TEXT.count("ARG PLAYWRIGHT_VERSION=1.60.0") >= 2
    assert "playwright==1.60.0" in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert (
        "PLAYWRIGHT_PYTHON_ARM64_WHEEL_SHA256="
        "43e66564125ee31b07a58cefb21e256d62d67d8d1713e6858df7a3019d8ed353"
    ) in TEXT
    assert (
        "PLAYWRIGHT_CORE_NPM_INTEGRITY="
        "sha512-9bW6zvX/m0lEbgTKJ6YppOKx8H3VOPBMOCFh2irXFOT4BbHgrx5hPjwJYLT40Lu"
        "+4qtD36qKc/Hn56StUW57IA=="
    ) in TEXT
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    assert package["dependencies"]["playwright-core"] == "1.60.0"
    assert package["dependencies"]["puppeteer-core"] == "25.3.0"
    assert "playwright/driver/node" in TEXT
    assert "ln -s /usr/bin/node" in TEXT


def test_ytdlp_sources_are_hash_verified_then_secret_bearing_extractors_are_removed() -> None:
    assert _sha256(SANITIZER) == (
        "4e7464710094be2eb6205fdc1ea207cfe62b99db1b34f9d31691e9a606bcb5db"
    )
    for digest in (
        "442ba4c75724b9496144c8434b617962ee08d0ee7c26ec663848fe9b78d5a3e4",
        "d50fcb95f48d61bedde33e408c1881d4c279e51c31354a599ce09e96ba0f4b86",
        "f82c1f065f6aa3dd5ce8ee3491d4c49f245d1e7ba921b8cc0cc9c8658a634fbd",
        "ea414688b508a2a77bf006e5928536603a51e7ab3b8664c13dd6d21b1140b80b",
        "638d0864a2551a143f29fc8dbe1b4da6aa8dcfb9392f1a8907a6e07f7a05118b",
    ):
        assert digest in TEXT
    sanitizer = SANITIZER.read_text(encoding="utf-8")
    for name in (
        "adultswim",
        "aenetworks",
        "blackboardcollaborate",
        "cloudflarestream",
        "espn",
        "go",
        "nbc",
        "shahid",
        "tbs",
        "vice",
    ):
        assert f'"{name}"' in sanitizer
    assert 'test ! -e "$package_root/extractor/$name.py"' in TEXT
    assert '"$name*.pyc"' in TEXT
    assert "{'youtube','TikTok','Instagram'} <= names" in TEXT
    assert "io.teamagent.contract.yt-dlp-removed-extractor-set-sha256" in TEXT
    assert ".trivyignore" not in TEXT
    assert "vex" not in TEXT.lower()


def test_media_image_copies_only_worker_media_code_and_no_core_secrets_stack() -> None:
    assert "FROM scratch AS final" in TEXT
    assert "COPY --from=runtime-packages / /" in TEXT
    assert "COPY --chown=10001:10001 src/teamagent/media/" in TEXT
    assert "COPY src/" not in TEXT
    assert "test ! -e /app/src/teamagent/mcp_gateway" in TEXT
    assert "test ! -e /app/.hf-cache" in TEXT
    assert "psycopg" in TEXT and "slack_sdk" in TEXT and "google_auth_oauthlib" in TEXT
    assert "assert all(u.find_spec(name) is None for name in blocked)" in TEXT
    assert "MCP_BEARER" not in TEXT
    assert "VERTEX" not in TEXT
    worker = WORKER.read_text(encoding="utf-8")
    assert 'session.client("s3"' in worker
    assert 'session.client("dynamodb"' in worker
    for forbidden_client in ('client("rds"', 'client("sqs"', 'client("secretsmanager"'):
        assert forbidden_client not in worker


def test_media_runtime_is_uid_10001_read_only_ready_and_sandboxed() -> None:
    assert "USER 10001:10001" in TEXT
    assert 'VOLUME ["/tmp"]' in TEXT
    assert "USER=teamagent" in TEXT
    assert "LOGNAME=teamagent" in TEXT
    assert "HOME=/tmp/teamagent/home" in TEXT
    assert "TMPDIR=/tmp/teamagent/tmp" in TEXT
    assert "TEAMAGENT_RUNTIME_KIND=media-worker" in TEXT
    assert 'ENTRYPOINT ["/app/.venv/bin/python", "-m", "teamagent.media.worker"]' in TEXT
    assert "--no-sandbox" not in SCRAPER.read_text(encoding="utf-8")
    assert "chromium_sandbox=True" in (ROOT / "src/teamagent/media/operations.py").read_text(
        encoding="utf-8"
    )


def test_tiktok_network_guard_is_attached_to_the_correct_browser_paths() -> None:
    scraper = SCRAPER.read_text(encoding="utf-8")
    assert "dnsCache" not in scraper
    assert "await dns.lookup(host, { all: true, verbatim: true })" in scraper
    search = scraper.split("async function searchOnce", 1)[1].split(
        "async function scrapeComments", 1
    )[0]
    comments = scraper.split("async function scrapeComments", 1)[1].split(
        "async function downloadVideoFromUrl", 1
    )[0]
    download = scraper.split("async function downloadVideoFromUrl", 1)[1].split(
        "function buildChromeArgs", 1
    )[0]
    assert "videoUrl" not in search
    assert "installPageNetworkGuard(page, { blockHeavy: true })" in search
    for path in (comments, download):
        assert "assertPublicHttps(videoUrl, { tiktokOnly: true })" in path
        assert "installPageNetworkGuard(page)" in path


def test_media_sources_and_js_lock_are_content_addressed() -> None:
    expected = {
        PACKAGE: "c9aafff461749b7591c810d698736fe33461965d238ed2cfd283229612a7fe28",
        PACKAGE_LOCK: "f0fe7ac3f992960d12dfdaddb14fa06e0b44ed92386c2a7d3fc74cbb98784dc2",
        SCRAPER: "c2e9dd93ced889addc83b09bd581ad58c406d9ef296518d2c2dc226c5d81bf16",
    }
    for path, digest in expected.items():
        assert _sha256(path) == digest
        assert digest in TEXT
    assert "npm ci --omit=dev --ignore-scripts --no-audit --no-fund" in TEXT


def test_media_dockerignore_is_deny_by_default() -> None:
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    assert text.startswith("**\n")
    for allowed in (
        "!pyproject.toml",
        "!uv.lock",
        "!src/teamagent/media/**",
        "!tools/tiktok_scraper/package-lock.json",
        "!infra/docker/media-apk.lock",
    ):
        assert allowed in text
    for blocked in ("**/.env", "**/*.pem", "**/*.key", "**/*.tfstate", "**/node_modules/"):
        assert blocked in text


def test_no_vulnerability_suppression_or_package_database_deletion_contract() -> None:
    docker_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (DOCKERFILE, DOCKERIGNORE, ROOT / "infra/docker/sanitize_ytdlp.py")
    )
    assert ".trivyignore" not in docker_sources
    assert "--ignore-unfixed" not in docker_sources
    assert "rm -rf /lib/apk/db" not in docker_sources
    assert not re.search(r"\bVEX\b", docker_sources, re.IGNORECASE)
