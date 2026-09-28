"""動画 bytes から代表フレームを ffmpeg で抜き、base64 data URI 化する（レポート埋込用）。

proxy 後の検証済み mp4 bytes を再利用（再DLしない）。Pillow は使わず ffmpeg の
scale + -q:v で縮小・JPEG化まで完結（新規依存ゼロ）。失敗は全段 graceful（空で続行）。
"""

from __future__ import annotations

import base64
import glob
import os
import shutil
import subprocess
import tempfile
from collections import Counter

import structlog

from teamagent.skills.video_algorithm.schema import Scene, VideoVSEOAnalysis

logger = structlog.get_logger(__name__)

_FRAMES_TIMEOUT_S = 60
_JPEG_Q = "6"  # mjpeg quantizer (2=高画質〜31=低)。320px縦型で ~12-18KB/枚


def pick_timecodes(a: VideoVSEOAnalysis, *, max_frames: int = 6) -> list[tuple[float, str]]:
    """意味のある秒（フック/KWテロップ/ブランド/シーン/CTA）を集め近接間引きして返す。"""
    dur = a.duration_sec
    if dur <= 0:
        return []
    cands: list[tuple[int, float, str]] = []  # (priority, sec, caption)
    cands.append((0, min(0.8, dur * 0.05), "フック"))

    kw = next((t for t in a.telops if t.kw_match and t.sec > 0), None)
    if kw:
        cands.append((1, kw.sec, f"KWテロップ {kw.sec:.0f}s"))

    brands = sorted(
        a.brand_detections,
        key=lambda b: (
            {"hero": 0, "prominent": 1}.get(b.prominence, 2),
            b.appear_sec[0] if b.appear_sec else 0.0,
        ),
    )
    for b in brands[:2]:
        if b.appear_sec:
            cands.append(
                (2, b.appear_sec[0], f"{b.brand_name or 'ブランド'} {b.appear_sec[0]:.0f}s")
            )

    if a.telops:
        bucket = Counter(int(t.sec) for t in a.telops if t.sec > 0)
        if bucket:
            top_sec, cnt = bucket.most_common(1)[0]
            if cnt >= 2:
                cands.append((3, float(top_sec) + 0.5, f"テロップ密集 {top_sec}s"))

    for sc in a.scenes:
        mid = (sc.start_sec + sc.end_sec) / 2
        if 0 < mid < dur:
            cands.append((4, mid, f"シーン {mid:.0f}s"))

    cta = a.cta_sec if a.cta_sec is not None else dur * 0.92
    if 0 < cta <= dur:
        label = f"CTA {cta:.0f}s" if a.cta_sec is not None else f"終盤 {cta:.0f}s"
        cands.append((5, cta, label))

    cands.sort(key=lambda c: (c[0], c[1]))
    picked: list[tuple[float, str]] = []
    for _, sec, cap in cands:
        sec = max(0.0, min(sec, max(0.0, dur - 0.05)))
        if all(abs(sec - p) >= 1.2 for p, _ in picked):
            picked.append((sec, cap))
        if len(picked) >= max_frames:
            break
    picked.sort(key=lambda x: x[0])
    return picked


# media job 1 回で抜けるフレームの上限（media/contracts.FrameOperation.timecodes の上限）。
MAX_SCENE_FRAMES = 12


def pick_scene_rows(scenes: list[Scene], limit: int = MAX_SCENE_FRAMES) -> list[Scene]:
    """構成表に出す場面（開始秒の順）。多ければ先頭 ``limit-1`` 個と最後の 1 個（CTA が多い）。"""
    ordered = sorted(scenes, key=lambda sc: (sc.start_sec, sc.end_sec))
    if limit <= 0:
        return []
    if len(ordered) <= limit:
        return ordered
    return [*ordered[: limit - 1], ordered[-1]]


def frame_duration(*durations: float) -> float:
    """コマを抜ける尺（正の値のうち最も短いもの・どれも無ければ 0）。

    Gemini の尺（analysis.duration_sec）は 0 や実尺より長いことがある。media job の _frames は
    尺の外の秒を 1 枚でも渡すと空のコマでジョブごと失敗する（MEDIA_FRAME_EMPTY）ので、検索結果の
    尺（meta.duration_sec・整数に切り捨て）とも比べて短い方に収める。
    """
    positive = [d for d in durations if d > 0]
    return min(positive) if positive else 0.0


def scene_time(scene: Scene, duration_sec: float = 0.0) -> float:
    """場面を代表する秒（場面の中央。尺があれば尺の内側に収める）。"""
    end = max(scene.end_sec, scene.start_sec)
    sec = (scene.start_sec + end) / 2
    if duration_sec > 0:
        sec = min(sec, max(0.0, duration_sec - 0.05))
    return round(max(0.0, sec), 2)


def scene_timecodes(
    a: VideoVSEOAnalysis, *, max_frames: int = MAX_SCENE_FRAMES, duration_sec: float = 0.0
) -> list[tuple[float, str]]:
    """構成表の場面ごとに 1 コマ（場面の中央の秒）。場面が無ければ pick_timecodes に倒す。

    秒は ``frame_duration(Gemini の尺, duration_sec)`` の内側に収める（``duration_sec`` は
    検索結果の実尺）。どちらの尺も分からなければ、pick_timecodes と同じくコマを出さない。
    """
    limit = frame_duration(a.duration_sec, duration_sec)
    if limit <= 0:
        return []
    rows = pick_scene_rows(a.scenes, min(max_frames, MAX_SCENE_FRAMES))
    picked: dict[float, str] = {}
    for sc in rows:
        sec = scene_time(sc, limit)
        picked.setdefault(sec, f"場面 {sc.start_sec:.0f}s")
    if not picked:
        fallback = pick_timecodes(a, max_frames=min(max_frames, MAX_SCENE_FRAMES))
        for sec, cap in fallback:
            picked.setdefault(round(min(sec, max(0.0, limit - 0.05)), 2), cap)
    return sorted(picked.items())


def extract_frames(
    data: bytes,
    mime: str,
    timecodes: list[float],
    *,
    width: int = 320,
    request_id: str = "frames",
) -> list[tuple[float, str]]:
    """動画 bytes から指定秒のフレームを抜き [(sec, data_uri)] を返す（graceful）。"""
    secs = sorted({round(max(0.0, s), 3) for s in timecodes})
    if not data or not secs or not shutil.which("ffmpeg"):
        return []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in")
            with open(src, "wb") as f:
                f.write(data)
            jpgs = _run_select(src, tmp, secs, width, request_id)
            if not jpgs:
                jpgs = _run_per_sec(src, tmp, secs, width, request_id)
            out: list[tuple[float, str]] = []
            for sec, path in zip(secs, sorted(jpgs), strict=False):
                try:
                    with open(path, "rb") as fh:
                        b = fh.read()
                except OSError:
                    continue
                if b:
                    out.append(
                        (sec, "data:image/jpeg;base64," + base64.b64encode(b).decode("ascii"))
                    )
            return out
    except Exception as e:  # レポートは必ず続行する（フレーム抽出失敗で落とさない）
        logger.warning("frames_extract_failed", request_id=request_id, error=type(e).__name__)
        return []


def _run_select(src: str, tmp: str, secs: list[float], width: int, request_id: str) -> list[str]:
    expr = "+".join(f"between(t,{s:.3f},{s + 0.04:.3f})" for s in secs)
    vf = f"select='{expr}',scale=w={width}:h=-2:flags=lanczos"
    cmd = [
        "ffmpeg",
        "-y",
        "-copyts",
        "-i",
        src,
        "-vf",
        vf,
        "-vsync",
        "0",
        "-frames:v",
        str(len(secs)),
        "-q:v",
        _JPEG_Q,
        os.path.join(tmp, "f_%03d.jpg"),
    ]
    _ffmpeg(cmd, request_id)
    return glob.glob(os.path.join(tmp, "f_*.jpg"))


def _run_per_sec(src: str, tmp: str, secs: list[float], width: int, request_id: str) -> list[str]:
    paths: list[str] = []
    for i, s in enumerate(secs):
        dst = os.path.join(tmp, f"p_{i:03d}.jpg")
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{s:.3f}",
            "-i",
            src,
            "-frames:v",
            "1",
            "-vf",
            f"scale=w={width}:h=-2:flags=lanczos",
            "-q:v",
            _JPEG_Q,
            dst,
        ]
        _ffmpeg(cmd, request_id)
        if os.path.exists(dst):
            paths.append(dst)
    return paths


def _ffmpeg(cmd: list[str], request_id: str) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_FRAMES_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        logger.warning("frames_ffmpeg_timeout", request_id=request_id)
        return
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-160:] if proc.stderr else ""
        logger.info("frames_ffmpeg_nonzero", request_id=request_id, stderr_tail=tail)
