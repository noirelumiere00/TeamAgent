#!/usr/bin/env python3
"""Read-only/non-root media worker smoke with network disabled by Compose."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import pwd
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import teamagent.media.operations as media_operations
from teamagent.media.contracts import (
    AcquireOperation,
    FrameOperation,
    ProxyOperation,
    S3ObjectRef,
    SlidesOperation,
    ThumbnailOperation,
)
from teamagent.media.deadline import DeadlineBudget
from teamagent.media.operations import execute_operation
from teamagent.media.security import validate_acquire_url

EXPECTED_BINARY_HASHES = {
    "/usr/bin/python3": "cfef52a96ad059b27c76e498cf0e3e973d742a6ecc8ff0214989f16c26bef1e8",
    "/usr/bin/node": "b9fb3beb6d397b33284966e6c1efb2056d6bc9ccc030f6f92caaac121c87a8e1",
    "/usr/bin/ffmpeg": "43aff9d9b8d8becd14f9e2a36a8497aa5c0e12454e60f7c0d3350ed5bef945ba",
    "/usr/lib/chromium/chromium": (
        "13eaa3cbe73f39b5feafcd767db0771c4f25d626a3927ba216ac43cde3abaf79"
    ),
}
REMOVED_YTDLP_EXTRACTORS = (
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
)
EXPECTED_REMOVED_EXTRACTOR_SET_SHA256 = (
    "ea414688b508a2a77bf006e5928536603a51e7ab3b8664c13dd6d21b1140b80b"
)
JOB_ID = "mj_000000000000000000000001"
BUCKET = "teamagent-smoke-artifacts"


def _budget() -> DeadlineBudget:
    return DeadlineBudget(deadline_epoch_s=time.time() + 300)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_read_only_root() -> None:
    probe = Path("/teamagent-runtime-smoke-write-probe")
    try:
        probe.write_text("must fail", encoding="utf-8")
    except OSError:
        return
    probe.unlink(missing_ok=True)
    raise AssertionError("root filesystem accepted a write")


def _runtime_root() -> Path:
    expected = {
        "HOME": "/tmp/teamagent/home",
        "TMPDIR": "/tmp/teamagent/tmp",
        "XDG_CACHE_HOME": "/tmp/teamagent/cache",
        "XDG_CONFIG_HOME": "/tmp/teamagent/config",
        "XDG_DATA_HOME": "/tmp/teamagent/data",
        "XDG_STATE_HOME": "/tmp/teamagent/state",
        "MEDIA_JOB_TMP_ROOT": "/tmp/teamagent/jobs",
    }
    for name, value in expected.items():
        assert os.environ.get(name) == value, (name, os.environ.get(name))
        Path(value).mkdir(mode=0o700, parents=True, exist_ok=True)
    assert stat.S_IMODE(Path("/tmp").stat().st_mode) == 0o1777
    return Path("/tmp/teamagent")


def _assert_content_boundary() -> None:
    blocked = (
        "psycopg",
        "sqlalchemy",
        "slack_sdk",
        "google_auth_oauthlib",
        "mcp",
        "sentence_transformers",
    )
    assert all(importlib.util.find_spec(module) is None for module in blocked)
    for name in (
        "DATABASE_URL",
        "SLACK_BOT_TOKEN",
        "TEAMAGENT_MCP_BEARER",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "VERTEX_CREDENTIALS",
        "ANTHROPIC_API_KEY",
    ):
        assert name not in os.environ
    assert not Path("/app/.hf-cache").exists()
    assert not Path("/app/src/teamagent/mcp_gateway").exists()
    assert Path("/lib/apk/db/installed").is_file()


def _assert_exact_runtime() -> None:
    assert sys.version.startswith("3.14.5 ")
    assert subprocess.check_output(["node", "--version"], text=True).strip() == "v24.18.0"
    assert importlib.metadata.version("playwright") == "1.60.0"
    assert importlib.metadata.version("yt-dlp") == "2026.6.9"
    for raw_path, expected in EXPECTED_BINARY_HASHES.items():
        assert _sha256_file(Path(raw_path)) == expected


def _make_ref(path: Path, key: str, content_type: str) -> S3ObjectRef:
    body = path.read_bytes()
    return S3ObjectRef(
        bucket=BUCKET,
        key=f"media-jobs/{JOB_ID}/input/{key}",
        sha256=_sha256_bytes(body),
        size=len(body),
        content_type=content_type,
    )


def _loader(objects: dict[str, Path]):
    def load(ref: S3ObjectRef, destination: Path) -> Path:
        source = objects[ref.key]
        body = source.read_bytes()
        assert len(body) == ref.size
        assert _sha256_bytes(body) == ref.sha256
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(body)
        destination.chmod(0o600)
        return destination

    return load


def _generate_video(destination: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=24:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(destination),
        ],
        check=True,
        timeout=60,
    )
    assert destination.stat().st_size > 1000


def _assert_ytdlp_sanitized_and_allowed(work_root: Path) -> None:
    import yt_dlp
    from yt_dlp.extractor import list_extractor_classes

    package_root = Path(yt_dlp.__file__).resolve().parent
    extractor_root = package_root / "extractor"
    for name in REMOVED_YTDLP_EXTRACTORS:
        assert not (extractor_root / f"{name}.py").exists()
        assert not list(extractor_root.rglob(f"{name}*.pyc"))
    manifest = json.loads(
        Path("/usr/share/teamagent/provenance/yt-dlp-sanitization.json").read_text(encoding="utf-8")
    )
    assert manifest["removed_extractor_set_sha256"] == EXPECTED_REMOVED_EXTRACTOR_SET_SHA256
    assert set(manifest["allowlisted_extractors"]) == {"youtube", "tiktok", "instagram"}
    assert len(manifest["removed"]) == len(REMOVED_YTDLP_EXTRACTORS)
    classes = list(list_extractor_classes())
    names = {cls.IE_NAME for cls in classes}
    assert {"youtube", "TikTok", "Instagram"} <= names
    samples = {
        "youtube": "https://www.youtube.com/watch?v=BaW_jenozKc",
        "TikTok": "https://www.tiktok.com/@scout2015/video/6718335390845095173",
        "Instagram": "https://www.instagram.com/reel/Cx123456789/",
    }
    by_name = {cls.IE_NAME: cls for cls in classes}
    assert all(by_name[name].suitable(url) for name, url in samples.items())

    original_extract_info = yt_dlp.YoutubeDL.extract_info
    original_validate_acquire_url = media_operations.validate_acquire_url

    def offline_public_resolver(
        _host: str,
        port: int,
        *,
        type: int,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        return [(2, type, 6, "", ("8.8.8.8", port))]

    def offline_validate(url: str) -> str:
        return validate_acquire_url(url, resolver=offline_public_resolver)

    def fake_extract_info(self: Any, url: str, *, download: bool = True) -> dict[str, str]:
        assert url == samples["youtube"]
        assert download is True
        params = self.params
        assert params["hls_prefer_native"] is True
        assert params["external_downloader"] is None
        assert params["ffmpeg_location"] == "/nonexistent/teamagent-ffmpeg-disabled"
        assert params["fixup"] == "never"
        assert params["postprocessors"] == []
        template = params["outtmpl"]
        if isinstance(template, dict):
            template = template["default"]
        output = Path(str(template).replace("%(ext)s", "mp4"))
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        output.write_bytes(b"offline yt-dlp smoke")
        return {"extractor_key": "Youtube", "ext": "mp4"}

    yt_dlp.YoutubeDL.extract_info = fake_extract_info
    media_operations.validate_acquire_url = offline_validate
    try:
        with tempfile.TemporaryDirectory(prefix="acquire-", dir=work_root) as raw:
            output = execute_operation(
                AcquireOperation(kind="acquire", url=samples["youtube"], max_bytes=4096),
                workdir=Path(raw),
                load_object=lambda _ref, _destination: (_ for _ in ()).throw(
                    AssertionError("acquire must not load S3 input")
                ),
                budget=_budget(),
            )
            assert output.metadata["extractor"] == "youtube"
            assert output.artifacts[0].path.read_bytes() == b"offline yt-dlp smoke"
    finally:
        yt_dlp.YoutubeDL.extract_info = original_extract_info
        media_operations.validate_acquire_url = original_validate_acquire_url


def _assert_python_playwright(work_root: Path) -> int:
    from playwright.sync_api import sync_playwright

    destination = work_root / "python-playwright.png"
    blocked = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ["CHROMIUM_PATH"],
            headless=True,
            # 本番 render_child と同じ起動プロファイル。Fargate では Chromium 自前サンド
            # ボックスが成立しないためサンドボックス無効で起動し、隔離は実行コンテナ側が
            # 担う（根拠は render_child.py の launch-args コメント参照）。
            chromium_sandbox=False,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
            ],
        )
        try:
            page = browser.new_page(viewport={"width": 640, "height": 360})

            def abort(route: Any) -> None:
                nonlocal blocked
                request = route.request
                if str(request.url).startswith(("http://", "https://")):
                    blocked += 1
                route.abort()

            page.route("**/*", abort)
            page.set_content(
                "<main style='font:48px sans-serif'>Python Playwright"
                "<img src='https://example.invalid/no-egress.png'></main>",
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(100)
            page.screenshot(path=str(destination), type="png")
        finally:
            browser.close()
    assert blocked >= 1
    assert destination.stat().st_size > 1000
    return blocked


def _assert_node_playwright(work_root: Path) -> int:
    destination = work_root / "node-playwright.png"
    completed = subprocess.run(
        ["node", "/smoke/smoke_media_node.mjs", str(destination)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    detail = json.loads(completed.stdout)
    assert detail["nodePlaywright"] == "1.60.0"
    assert destination.stat().st_size > 1000
    return int(detail["blockedRequests"])


def _assert_node_network_guard() -> None:
    completed = subprocess.run(
        [
            "node",
            "/app/tools/tiktok_scraper/search.mjs",
            "--network-guard-self-test",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    detail = json.loads(completed.stdout)
    assert detail["ok"] is True
    assert "100.64.0.1" in detail["blocked"]
    assert "::ffff:127.0.0.1" in detail["blocked"]


def _assert_ffmpeg_protocol_guard(work_root: Path) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    accepted: list[bool] = []

    def intercept() -> None:
        try:
            connection, _address = listener.accept()
        except TimeoutError:
            return
        accepted.append(True)
        connection.close()

    thread = threading.Thread(target=intercept, daemon=True)
    thread.start()
    port = listener.getsockname()[1]
    playlist = work_root / "crafted.m3u8"
    playlist.write_text(
        (
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
            f"#EXTINF:10,\nhttp://127.0.0.1:{port}/private.ts\n"
            "#EXT-X-ENDLIST\n"
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-protocol_blacklist",
            "http,https,tcp,tls,udp,rtp,ftp,gopher,sftp,ssh",
            "-i",
            str(playlist),
            "-f",
            "null",
            "-",
        ],
        check=False,
        capture_output=True,
        timeout=10,
    )
    thread.join(timeout=3)
    listener.close()
    assert completed.returncode != 0
    assert accepted == []


def _assert_transforms(work_root: Path) -> dict[str, int]:
    from pptx import Presentation

    source = work_root / "source.mp4"
    html = work_root / "slides.html"
    _generate_video(source)
    html.write_text(
        """
        <!doctype html><style>
        body{margin:0}.slide{width:1280px;height:720px;display:flex;
        align-items:center;justify-content:center;font:64px sans-serif}
        .a{background:#123;color:white}.b{background:#fed;color:#321}
        </style><section class="slide a">one</section>
        <section class="slide b">two</section>
        """,
        encoding="utf-8",
    )
    video_ref = _make_ref(source, "video.mp4", "video/mp4")
    html_ref = _make_ref(html, "slides.html", "text/html")
    objects = {video_ref.key: source, html_ref.key: html}
    load = _loader(objects)

    with tempfile.TemporaryDirectory(prefix="proxy-", dir=work_root) as raw:
        proxy = execute_operation(
            ProxyOperation(
                kind="proxy",
                source=video_ref,
                limit_bytes=8 * 1024 * 1024,
                long_edge=640,
                preview=True,
            ),
            workdir=Path(raw),
            load_object=load,
            budget=_budget(),
        )
        assert proxy.metadata["transcoded"] is True
        assert proxy.artifacts[0].path.stat().st_size > 1000

    with tempfile.TemporaryDirectory(prefix="frames-", dir=work_root) as raw:
        frames = execute_operation(
            FrameOperation(kind="frame", source=video_ref, timecodes=(0.1, 1.0), width=320),
            workdir=Path(raw),
            load_object=load,
            budget=_budget(),
        )
        assert frames.metadata["count"] == 2
        assert len(frames.artifacts) == 3

    with tempfile.TemporaryDirectory(prefix="thumbnail-", dir=work_root) as raw:
        thumbnail = execute_operation(
            ThumbnailOperation(kind="thumbnail", source=video_ref, width=320),
            workdir=Path(raw),
            load_object=load,
            budget=_budget(),
        )
        assert len(thumbnail.metadata["swatches"]) >= 1
        assert thumbnail.artifacts[0].path.stat().st_size > 1000

    with tempfile.TemporaryDirectory(prefix="slides-", dir=work_root) as raw:
        slides = execute_operation(
            SlidesOperation(
                kind="slides",
                html=html_ref,
                selector=".slide",
                width=1280,
                height=720,
                device_scale_factor=1,
            ),
            workdir=Path(raw),
            load_object=load,
            budget=_budget(),
        )
        assert slides.metadata == {"slides": 2, "network_requests_allowed": 0}
        deck = Presentation(str(slides.artifacts[0].path))
        assert len(deck.slides) == 2

    return {"frames": 2, "slides": 2}


def main() -> None:
    assert os.getuid() == 10001
    assert os.getgid() == 10001
    assert os.environ["USER"] == os.environ["LOGNAME"] == "teamagent"
    assert pwd.getpwuid(10001).pw_name == "teamagent"
    assert os.environ["TEAMAGENT_RUNTIME_KIND"] == "media-worker"
    _assert_read_only_root()
    runtime_root = _runtime_root()
    _assert_content_boundary()
    _assert_exact_runtime()

    jobs = runtime_root / "jobs"
    with tempfile.TemporaryDirectory(prefix="media-suite-", dir=jobs) as raw:
        work_root = Path(raw)
        _assert_ytdlp_sanitized_and_allowed(work_root)
        _assert_node_network_guard()
        _assert_ffmpeg_protocol_guard(work_root)
        python_blocked = _assert_python_playwright(work_root)
        node_blocked = _assert_node_playwright(work_root)
        transformed = _assert_transforms(work_root)
    assert list(jobs.iterdir()) == []

    print(
        json.dumps(
            {
                "runtime": "media-worker",
                "uid": os.getuid(),
                "root_read_only": True,
                "tmp_mode": "1777",
                "container_network": "none",
                "python_playwright_blocked_requests": python_blocked,
                "node_playwright_blocked_requests": node_blocked,
                "node_ssrf_non_global_blocked": True,
                "ffmpeg_network_protocols_blocked": True,
                "yt_dlp_allowed_sites": ["youtube", "TikTok", "Instagram"],
                **transformed,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
