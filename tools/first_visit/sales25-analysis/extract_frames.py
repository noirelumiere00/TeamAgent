#!/usr/bin/env python3
"""Pinned adaptation of sales25's scene + four-second floor extraction.

The worker owns the deadline/process group. Inputs are local files only; excess
duration, frame count, decode failures and timestamp gaps fail without clipping.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from itertools import pairwise
from pathlib import Path

SCENE = 0.4
FLOOR_SEC = 4.0
MAX_GAP_ALLOWED = 6.0
MAX_FRAMES = 180
MAX_DURATION_SEC = 180.0
LOCAL_INPUT = [
    "-protocol_whitelist",
    "file,pipe",
    "-protocol_blacklist",
    "http,https,tcp,tls,udp,rtp,ftp,gopher,sftp,ssh",
]


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=120, check=True)


def extract(video: Path, out: Path) -> dict[str, object]:
    if not video.is_file() or video.is_symlink() or out.exists():
        raise ValueError("fresh local output and regular input required")
    probe = run(
        [
            "ffprobe",
            "-v",
            "error",
            *LOCAL_INPUT,
            "-i",
            str(video),
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
        ]
    )
    duration = float(probe.stdout.strip())
    if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION_SEC:
        raise ValueError("duration exceeds complete-analysis limit")
    out.mkdir(mode=0o700)
    # Keep selection before scaling: scene sensitivity must match the kit.
    expr = f"gt(scene,{SCENE})+lt(prev_selected_t,t-{FLOOR_SEC})+eq(n,0)"
    # The pinned checker also needs three views and an ending in the last 3s.
    checkpoints = [duration / 2, duration - 1] if duration > 3 else []
    for timestamp in checkpoints:
        expr += f"+gte(t,{timestamp})*lt(prev_selected_t,{timestamp})"
    selected = run(
        [
            "ffmpeg",
            "-nostdin",
            "-xerror",
            "-loglevel",
            "info",
            *LOCAL_INPUT,
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            f"select='{expr}',showinfo,scale=960:960:force_original_aspect_ratio=decrease",
            "-fps_mode",
            "vfr",
            "-frames:v",
            str(MAX_FRAMES + 1),
            "-q:v",
            "4",
            str(out / "%03d.jpg"),
        ]
    )
    times = [float(v) for v in re.findall(r"pts_time:([0-9.eE+-]+)", selected.stderr)]
    frames = sorted(out.glob("*.jpg"))
    if not frames or len(frames) != len(times) or len(frames) > MAX_FRAMES:
        raise ValueError("frame count/timestamps exceed complete-analysis contract")
    if duration > 3 and (len(frames) < 3 or duration - times[-1] > 3):
        raise ValueError("opening/development/ending views are incomplete")
    if any(not math.isfinite(t) or not 0 <= t <= duration for t in times):
        raise ValueError("invalid frame timestamp")
    sequence = [0.0, *times, duration]
    gaps = [b - a for a, b in pairwise(sequence)]
    max_gap = max(gaps)
    if any(gap < 0 for gap in gaps) or max_gap > MAX_GAP_ALLOWED or times[0] > 0.1:
        raise ValueError("incomplete timeline coverage")
    # Reuse the actual extracted files so sheet order cannot drift from metadata.
    run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            *LOCAL_INPUT,
            "-framerate",
            "1",
            "-i",
            str(out / "%03d.jpg"),
            "-vf",
            f"scale=200:-1,tile=8x{math.ceil(len(frames) / 8)}",
            "-frames:v",
            "1",
            str(out / "contact.jpg"),
        ]
    )
    manifest: dict[str, object] = {
        "duration_sec": duration,
        "frames": len(frames),
        "scene_threshold": SCENE,
        "floor_sec": FLOOR_SEC,
        "timestamps": times,
        "max_gap_sec": max_gap,
        "coverage_ok": True,
        "checkpoint_targets_sec": checkpoints,
    }
    (out / "frames.json").write_text(json.dumps(manifest, allow_nan=False), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    extract(args.video, args.out)


if __name__ == "__main__":
    main()
