#!/usr/bin/env python3
"""Read-only/non-root smoke checks for the exact TeamAgent core image."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import pwd
import shutil
import stat
import sys
import tempfile
from pathlib import Path

EXPECTED_BAKED_APP_HTML_SHA256 = "716ac25a96516efd6443277c903102d514f3f86729f8706baea41ee48f0ecdeb"
EXPECTED_E5_REVISION = "3d7cfbdacd47fdda877c5cd8a79fbcc4f2a574f3"
EXPECTED_PYTHON_SHA256 = "0d036a463b218cff354adfb9c09a969a9a659698fa376bd3b55fe5bc002e7af8"


def _sha256(path: Path) -> str:
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


def _assert_runtime_directories() -> None:
    expected = {
        "HOME": "/tmp/teamagent/home",
        "TMPDIR": "/tmp/teamagent/tmp",
        "XDG_CACHE_HOME": "/tmp/teamagent/cache",
        "XDG_CONFIG_HOME": "/tmp/teamagent/config",
        "XDG_DATA_HOME": "/tmp/teamagent/data",
        "XDG_STATE_HOME": "/tmp/teamagent/state",
        "VIDEO_APPROVAL_STATE_PATH": "/tmp/teamagent/state/video_approval_processed.json",
        "MEDIA_JOB_TMP_ROOT": "/tmp/teamagent/jobs",
    }
    for name, value in expected.items():
        assert os.environ.get(name) == value, (name, os.environ.get(name))
        Path(value).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    assert stat.S_IMODE(Path("/tmp").stat().st_mode) == 0o1777


def _assert_content_boundary() -> None:
    forbidden_binaries = (
        "sh",
        "curl",
        "node",
        "npm",
        "npx",
        "bun",
        "chromium",
        "chromium-browser",
        "ffmpeg",
        "ffprobe",
        "yt-dlp",
    )
    assert all(shutil.which(binary) is None for binary in forbidden_binaries)

    forbidden_modules = (
        "claude_agent_sdk",
        "playwright",
        "yt_dlp",
        "weasyprint",
        "pptx",
        "teamagent.media.operations",
        "teamagent.media.worker",
    )
    assert all(importlib.util.find_spec(module) is None for module in forbidden_modules)

    for module in (
        "anthropic",
        "boto3",
        "mcp",
        "psycopg",
        "sentence_transformers",
        "teamagent.media.contracts",
    ):
        __import__(module)


def _assert_e5_encode() -> None:
    assert os.environ["TEAMAGENT_E5_MODEL_REVISION"] == EXPECTED_E5_REVISION
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
    from teamagent.adapters.embeddings_client import LocalE5Embedder

    vector = LocalE5Embedder().embed("runtime boundary smoke")
    assert len(vector) == 1024
    norm = math.sqrt(sum(value * value for value in vector))
    assert 0.999 <= norm <= 1.001, norm


def _assert_repeated_temp_cleanup() -> None:
    jobs = Path(os.environ["MEDIA_JOB_TMP_ROOT"])
    jobs.mkdir(mode=0o700, parents=True, exist_ok=True)
    for _ in range(2):
        with tempfile.TemporaryDirectory(prefix="core-smoke-", dir=jobs) as raw:
            Path(raw, "artifact").write_bytes(b"bounded")
    assert list(jobs.iterdir()) == []


def main() -> None:
    assert os.getuid() == 10001
    assert os.getgid() == 10001
    assert os.environ["USER"] == os.environ["LOGNAME"] == "teamagent"
    assert pwd.getpwuid(10001).pw_name == "teamagent"
    assert os.environ["TEAMAGENT_RUNTIME_KIND"] == "core"
    assert sys.version.startswith("3.14.6 ")
    assert _sha256(Path("/usr/bin/python3.14")) == EXPECTED_PYTHON_SHA256
    assert (
        _sha256(Path("/app/src/teamagent/connect_web/static/app.html"))
        == EXPECTED_BAKED_APP_HTML_SHA256
    )
    _assert_read_only_root()
    _assert_runtime_directories()
    _assert_content_boundary()
    _assert_repeated_temp_cleanup()
    _assert_e5_encode()
    _assert_repeated_temp_cleanup()
    print(
        json.dumps(
            {
                "runtime": "core",
                "uid": os.getuid(),
                "python": sys.version.split()[0],
                "e5_dimension": 1024,
                "root_read_only": True,
                "tmp_mode": "1777",
                "media_binaries_absent": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
