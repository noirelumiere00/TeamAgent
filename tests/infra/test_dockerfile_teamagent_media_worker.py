"""teamagent-media-worker の再現可能性・分離・sanitization契約。"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "infra/docker/Dockerfile.teamagent-media-worker"
DOCKERIGNORE = ROOT / "infra/docker/Dockerfile.teamagent-media-worker.dockerignore"
APK_LOCK = ROOT / "infra/docker/media-apk.lock"
SANITIZER = ROOT / "infra/docker/sanitize_ytdlp.py"
PACKAGE = ROOT / "tools/tiktok_scraper/package.json"
PACKAGE_LOCK = ROOT / "tools/tiktok_scraper/package-lock.json"
SCRAPER = ROOT / "tools/tiktok_scraper/search.mjs"
DNS_PINNED_PROXY = ROOT / "tools/tiktok_scraper/dns_pinned_proxy.mjs"
RAKKO_SCRAPER = ROOT / "tools/rakko_scraper/scrape.mjs"
RENDER_CHILD = ROOT / "src/teamagent/media/render_child.py"
SMOKE_MEDIA_NODE = ROOT / "infra/docker/smoke_media_node.mjs"
TOOL_WORKER = ROOT / "src/teamagent/media/tool_worker.py"
TEXT = DOCKERFILE.read_text(encoding="utf-8")

CHROMIUM_BASE_DIGEST = "ee09ed198c66003a3f15024ca4f8f8613b9a97fdfd0dce8600969fc8a69ecc04"
NODE_BUILDER_DIGEST = "eef73a25205e27bd016ce672af71560ad6b681142ddf00ff63c7b3098eafcd4d"
UV_DIGEST = "9941e2d8e06ff884d328905091eac0a6bc1e40e5ce12e6dd0de4ef4ee26baac4"
# 2026-08-26 `8c01f9d`（wolfi 上流の ncurses ドリフト3件へ台帳を追随）が
# media-apk.lock / Dockerfile の ARG / core_media 契約 / 世代 inputs の 4 つは更新した一方、
# 本定数だけ取り残されて dev tip が赤のままになっていた（3 者一致の不変条件が片肺）。
# 実測: media-apk.lock の sha256 = Dockerfile の ARG MEDIA_APK_LOCK_SHA256 = 下記。
APK_LOCK_SHA256 = "4e147d1445837415a7d9e9569667b39cedbb3c83de71f9082adbcecb487424e5"
CHROMIUM_PATH = "/usr/lib/chromium/chromium"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_media_external_images_are_exact_arm64_children() -> None:
    assert f"ARG CHROMIUM_BASE_ARM64_DIGEST=sha256:{CHROMIUM_BASE_DIGEST}" in TEXT
    assert f"ARG NODE_BUILDER_ARM64_DIGEST=sha256:{NODE_BUILDER_DIGEST}" in TEXT
    assert f"ARG UV_ARM64_DIGEST=sha256:{UV_DIGEST}" in TEXT
    assert (
        "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mirror/"
        "chromium-headless:150-alpine@${CHROMIUM_BASE_ARM64_DIGEST}" in TEXT
    )
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
        "NODE_PACKAGE_VERSION": "24.18.1-r0",
        "NODE_BINARY_SHA256": ("b998f239765321093d8447cde4497fed8107ebd657802c4ebfa831593b17aed2"),
        "PYTHON_PACKAGE_VERSION": "3.14.7-r0",
        "PYTHON_BINARY_SHA256": (
            "cfef52a96ad059b27c76e498cf0e3e973d742a6ecc8ff0214989f16c26bef1e8"
        ),
    }
    for name, value in expected.items():
        assert f"ARG {name}={value}" in TEXT
    for package in ("ffmpeg", "font-liberation", "font-noto", "font-noto-cjk", "font-noto-emoji"):
        assert f'"{package}=$' in TEXT
    assert (
        "test \"$(apk list --installed chromium 2>/dev/null | awk 'NR==1{print $1}')\""
        ' = "chromium-$CHROMIUM_PACKAGE_VERSION"' in TEXT
    )
    assert "apk list --installed" in TEXT
    assert "test -s /lib/apk/db/installed" in TEXT
    assert "rm -rf /lib/apk" not in TEXT
    assert "/usr/lib/chromium/chromium --version" in TEXT
    assert "chromium-browser --version" not in TEXT


def test_media_chromium_path_matches_the_measured_binary_everywhere() -> None:
    assert f"CHROMIUM_PATH={CHROMIUM_PATH}" in TEXT
    assert f'os.environ.get("CHROMIUM_PATH", "{CHROMIUM_PATH}")' in RENDER_CHILD.read_text(
        encoding="utf-8"
    )
    assert f'process.env.CHROMIUM_PATH || "{CHROMIUM_PATH}"' in SMOKE_MEDIA_NODE.read_text(
        encoding="utf-8"
    )
    for scraper in (SCRAPER, RAKKO_SCRAPER):
        scraper_text = scraper.read_text(encoding="utf-8")
        assert f'const candidates = [\n    "{CHROMIUM_PATH}",' in scraper_text


def test_apk_inventory_is_exact_and_hash_pinned() -> None:
    assert (
        len(APK_LOCK.read_text(encoding="utf-8").splitlines()) == 240
    )  # 2026-08-17 edge 依存グラフ変更で expat/gdbm が再び依存から脱落（実ビルド diff で確定）
    assert _sha256(APK_LOCK) == APK_LOCK_SHA256
    assert f"ARG MEDIA_APK_LOCK_SHA256={APK_LOCK_SHA256}" in TEXT
    assert "cmp /tmp/media-apk.lock /tmp/actual-apk.lock" in TEXT
    assert 'io.teamagent.contract.apk-lock-sha256="$MEDIA_APK_LOCK_SHA256"' in TEXT
    # v2: 2026-08-31 に apk キャッシュ汚染を実測（nodejs 24.18.1-r0 の /usr/bin/node が
    # 上流 apk の実測 sha（pin と一致）と異なるバイトで 2 連続供給された）。id を回して
    # 汚染エントリを切り離した。再発時はさらに v3 へ回す（中身の修正ではなく隔離が正解）。
    assert "--mount=type=cache,id=teamagent-media-apk-arm64-v2,target=/var/cache/apk" in TEXT
    assert "https://dl-cdn.alpinelinux.org/alpine/edge" in TEXT
    # 2026-08-31 vendored node: 上流が同一版のままバイト差し替え（CodeBuild 実測
    # 885209fa… ≠ pin b998f239…）。契約 pin の更新は世代 publish 儀式が要るため、
    # pin と一致する正規 apk を同梱して取得元ドリフトから切り離した。
    vendored = ROOT / "infra/docker/vendor/nodejs-24.18.1-r0.apk"
    assert _sha256(vendored) == (
        "594ad7bc48f53f4ad1c6fcb73adee4bc155922d00d4a258cf6e7b3837f8c9850"
    )
    assert (
        "ARG NODE_VENDORED_APK_SHA256="
        "594ad7bc48f53f4ad1c6fcb73adee4bc155922d00d4a258cf6e7b3837f8c9850"
    ) in TEXT
    assert "COPY infra/docker/vendor/nodejs-24.18.1-r0.apk" in TEXT
    assert "add /tmp/vendor/nodejs-24.18.1-r0.apk" in TEXT
    assert "/tmp/vendor; \\" in TEXT  # 後始末（イメージへ残さない）
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
    assert TEXT.count("ARG PLAYWRIGHT_CORE_NPM_INTEGRITY=") == 2
    assert 'grep -F -c "$playwright_integrity" /deps/package-lock.json' in TEXT
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
        "f11f2b11d5a8ac4059f9bdf29fa4407dc7c6bb00c5097e95ca22a7a9db518266",
        "b094813404f87a9dd2186f00815231df32e5fd8a5403be0f807b3bb2d21a4432",
        "f82c1f065f6aa3dd5ce8ee3491d4c49f245d1e7ba921b8cc0cc9c8658a634fbd",
        "ea414688b508a2a77bf006e5928536603a51e7ab3b8664c13dd6d21b1140b80b",
        "32a2d7849c3897ae7c28c3e17853b10e3b74a0d280808177c20407496d52e817",
    ):
        assert digest in TEXT
    assert TEXT.count("ARG YTDLP_WHEEL_SHA256=") == 2
    assert TEXT.count("ARG YTDLP_SDIST_SHA256=") == 2
    assert 'grep -F -c "$ytdlp_wheel" uv.lock' in TEXT
    assert 'grep -F -c "$ytdlp_sdist" uv.lock' in TEXT
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
    for source in (
        "__init__.py",
        "contracts.py",
        "deadline.py",
        "operations.py",
        "render_child.py",
        "security.py",
        "tool_contracts.py",
        "tool_worker.py",
        "url_policy.py",
    ):
        assert f"src/teamagent/media/{source}" in TEXT
    assert "  src/teamagent/media/worker.py \\" not in TEXT
    assert "src/teamagent/media/ /app/src/teamagent/media/" not in TEXT
    assert "COPY src/" not in TEXT
    assert "test ! -e /app/src/teamagent/mcp_gateway" in TEXT
    assert "test ! -e /app/src/teamagent/media/worker.py" in TEXT
    assert "test ! -e /app/.hf-cache" in TEXT
    for blocked in (
        "boto3",
        "botocore",
        "s3transfer",
        "psycopg",
        "slack_sdk",
        "google_auth_oauthlib",
    ):
        assert blocked in TEXT
    assert "assert all(u.find_spec(name) is None for name in blocked)" in TEXT
    assert "MCP_BEARER" not in TEXT
    assert "VERTEX" not in TEXT
    worker = TOOL_WORKER.read_text(encoding="utf-8")
    assert "import boto" not in worker
    assert ".client(" not in worker
    assert "generate_presigned" not in worker
    runtime_group = (
        (ROOT / "pyproject.toml")
        .read_text(encoding="utf-8")
        .split("media-runtime = [", 1)[1]
        .split("]", 1)[0]
    )
    for forbidden_package in ("boto3", "botocore", "s3transfer"):
        assert forbidden_package not in runtime_group


def test_media_runtime_is_uid_10001_read_only_ready_and_sandboxed() -> None:
    assert "USER 10001:10001" in TEXT
    assert 'VOLUME ["/tmp"]' in TEXT
    assert "USER=teamagent" in TEXT
    assert "LOGNAME=teamagent" in TEXT
    assert "HOME=/tmp/teamagent/home" in TEXT
    assert "TMPDIR=/tmp/teamagent/tmp" in TEXT
    assert "TEAMAGENT_RUNTIME_KIND=media-worker" in TEXT
    assert 'ENTRYPOINT ["/app/.venv/bin/python", "-m", "teamagent.media.tool_worker"]' in TEXT
    scraper_text = SCRAPER.read_text(encoding="utf-8")
    # Fargate は unprivileged userns 無効で Chromium 自前サンドボックスが成立しない
    # （実測: No usable sandbox! で起動即死・search.mjs:663-665 のコメント参照）。
    # 隔離はコンテナ側（uid 10001 / cap drop / readonly rootfs）が担う設計へ移行済み。
    # フラグは根拠コメント付きの launch-args 1箇所だけに許し、黙った増殖は赤にする。
    assert scraper_text.count("--no-sandbox") == 1
    assert "隔離は実行コンテナ側" in scraper_text
    assert "chromium_sandbox=True" in (ROOT / "src/teamagent/media/render_child.py").read_text(
        encoding="utf-8"
    )


def test_tiktok_network_guard_is_attached_to_the_correct_browser_paths() -> None:
    scraper = SCRAPER.read_text(encoding="utf-8")
    proxy = DNS_PINNED_PROXY.read_text(encoding="utf-8")
    assert "dnsCache" not in scraper
    assert "await dns.lookup" not in scraper
    assert "startDnsPinnedProxy()" in scraper
    assert "--proxy-bypass-list=<-loopback>" in scraper
    assert "--host-resolver-rules=MAP * ~NOTFOUND" in scraper
    assert "--disable-quic" in scraper
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in scraper
    assert "await lookup(host, { all: true, verbatim: true })" in proxy
    assert "peer !== pinned.address" in proxy
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


def test_dns_pinned_proxy_rebinding_answers_fail_closed_without_second_lookup() -> None:
    script = """
import {resolvePinnedTarget} from './tools/tiktok_scraper/dns_pinned_proxy.mjs';
let calls = 0;
const rebinding = async () => {
  calls += 1;
  return calls === 1
    ? [{address: '8.8.8.8', family: 4}]
    : [{address: '127.0.0.1', family: 4}];
};
const pinned = await resolvePinnedTarget('example.com', {
  lookup: rebinding,
  blockedCidrs: [],
});
if (pinned.address !== '8.8.8.8' || calls !== 1) process.exit(2);
const mixed = async () => [
  {address: '2606:4700:4700::1111', family: 6},
  {address: '169.254.169.254', family: 4},
];
try {
  await resolvePinnedTarget('example.com', {lookup: mixed, blockedCidrs: []});
  process.exit(3);
} catch (error) {
  if (!String(error.message).includes('blocked')) process.exit(4);
}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_media_sources_and_js_lock_are_content_addressed() -> None:
    expected = {
        PACKAGE: "c9aafff461749b7591c810d698736fe33461965d238ed2cfd283229612a7fe28",
        PACKAGE_LOCK: "f0fe7ac3f992960d12dfdaddb14fa06e0b44ed92386c2a7d3fc74cbb98784dc2",
        SCRAPER: "99c1955010a99be0f2921d8b107f849c3fce216f76eaa2ba342531e98407816e",
        DNS_PINNED_PROXY: ("ccac597a429069074d2b362d6b15ad75c00c23941be97e9ea87503d36fd50e19"),
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
        "!src/teamagent/media/tool_worker.py",
        "!src/teamagent/media/tool_contracts.py",
        "!tools/tiktok_scraper/package-lock.json",
        "!infra/docker/media-apk.lock",
    ):
        assert allowed in text
    assert "!src/teamagent/media/**" not in text
    assert "!src/teamagent/media/worker.py" not in text
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
