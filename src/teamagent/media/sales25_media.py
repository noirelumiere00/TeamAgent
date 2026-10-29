"""Bounded selected-post acquisition and offline extraction for sales25."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any, NoReturn

from teamagent.media.contracts import (
    SALES25_MAX_BUNDLE_BYTES,
    SALES25_MAX_DURATION_SECONDS,
    SALES25_MAX_FRAMES,
    SALES25_MAX_MANIFEST_BYTES,
    SALES25_MAX_MEDIA_BYTES,
    SALES25_MAX_PHOTOS,
    AcquireOperation,
    Sales25MediaOperation,
)
from teamagent.media.deadline import DeadlineBudget
from teamagent.media.operations import (
    MediaOperationError,
    OperationOutput,
    ProducedArtifact,
    _acquire_ytdlp,
    _fetch_public_image,
    _ffmpeg_input,
    _isolated_child_environment,
    _node_json,
    _remaining,
    _run,
    _run_process,
    _safe_file,
    _worker_acquire_order,
)
from teamagent.media.security import validate_acquire_url

TOOLS = Path(__file__).resolve().parents[3] / "tools"
LOCK_SHA256 = "67741470f8502ae88340154d135985f06ea6aebdc5c40bcb52fea3663c73bb50"
_BROWSER = "tiktok_scraper/sales25_media.mjs"
_EXTRACTOR = "first_visit/sales25-analysis/extract_frames.py"


def _fail(code: str, message: str) -> NoReturn:
    raise MediaOperationError(f"MEDIA_SALES25_{code}", message)


def verify_analysis_bundle() -> None:
    lock_path = TOOLS / "first_visit/sales25-analysis.lock.json"
    if hashlib.sha256(lock_path.read_bytes()).hexdigest() != LOCK_SHA256:
        _fail("PIN_MISMATCH", "sales25 analysis lock SHA-256 mismatch")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if set(lock["files"]) != {_BROWSER, _EXTRACTOR}:
        _fail("PIN_MISMATCH", "sales25 analysis bundle membership mismatch")
    for relative, expected in lock["files"].items():
        path = _safe_file(TOOLS / relative, TOOLS)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            _fail("PIN_MISMATCH", "sales25 analysis script SHA-256 mismatch")


def _acquire_selected(
    operation: Sales25MediaOperation, workdir: Path, budget: DeadlineBudget
) -> tuple[dict[str, Any], list[Path], Path | None, Path | None]:
    validate_acquire_url(operation.url)
    for step in dict.fromkeys(_worker_acquire_order()):
        if step == "ytdlp" and operation.media_type == "photo":
            continue
        stage = workdir / step
        stage.mkdir(mode=0o700)
        try:
            if step == "ytdlp":
                acquired = _acquire_ytdlp(
                    AcquireOperation(
                        kind="acquire", url=operation.url, max_bytes=operation.max_bytes
                    ),
                    stage,
                    budget,
                    include_identity=True,
                )
                cover: Path | None = None
                thumbnail = acquired.metadata.get("thumbnail")
                if (
                    acquired.metadata.get("post_id") == operation.post_id
                    and acquired.metadata.get("url") == operation.url
                    and isinstance(thumbnail, str)
                    and _fetch_public_image(
                        thumbnail, stage / "cover.jpg", budget=budget, width=960
                    )
                ):
                    cover = stage / "cover.jpg"
                return acquired.metadata, [acquired.artifacts[0].path], None, cover
            result = _node_json(
                [
                    os.environ.get("TIKTOK_NODE_BIN", "/usr/bin/node"),
                    str(TOOLS / _BROWSER),
                    operation.url,
                    str(stage / "download"),
                    str(operation.max_bytes),
                ],
                workdir=stage,
                budget=budget,
                timeout_s=240,
            )
            files = result.get("files")
            if (
                not isinstance(files, list)
                or not 1 <= len(files) <= SALES25_MAX_PHOTOS
                or any(not isinstance(name, str) or Path(name).name != name for name in files)
                or len(files) != len(set(files))
            ):
                _fail("OUTPUT_INVALID", "acquired file list is invalid")
            paths = [_safe_file(stage / "download" / name, stage / "download") for name in files]
            optional: list[Path | None] = []
            for key in ("audio", "cover"):
                name = result.get(key)
                if name is not None and (not isinstance(name, str) or Path(name).name != name):
                    _fail("OUTPUT_INVALID", "acquired optional file is invalid")
                optional.append(_safe_file(stage / "download" / name, stage) if name else None)
            return result, paths, optional[0], optional[1]
        except MediaOperationError as exc:
            if exc.code in {"MEDIA_JOB_DEADLINE_EXCEEDED", "MEDIA_SALES25_OUTPUT_INVALID"}:
                raise
    _fail("ACQUIRE_FAILED", "selected-post acquisition failed")


def _probe(source: Path, budget: DeadlineBudget) -> dict[str, Any]:
    completed = _run_process(
        [
            "ffprobe",
            "-v",
            "error",
            *_ffmpeg_input(source),
            "-show_streams",
            "-show_format",
            "-of",
            "json",
        ],
        budget=budget,
        timeout_s=30,
        timeout_code="MEDIA_PROCESS_TIMEOUT",
        timeout_message="media probe timed out",
    )
    if completed.returncode != 0 or len(completed.stdout) > SALES25_MAX_MANIFEST_BYTES:
        _fail("PROBE_FAILED", "cannot inspect acquired media")
    try:
        result: dict[str, Any] = json.loads(completed.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise MediaOperationError("MEDIA_SALES25_PROBE_FAILED", "invalid media probe") from exc
    return result


def _duration(probe: dict[str, Any]) -> float:
    try:
        value = float(probe["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        _fail("DURATION_INVALID", "media duration is unavailable")
    if not math.isfinite(value) or not 0 < value <= SALES25_MAX_DURATION_SECONDS:
        _fail("DURATION_EXCEEDED", "complete media exceeds the duration limit")
    return value


def _image(source: Path, target: Path, budget: DeadlineBudget) -> None:
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-xerror",
            "-v",
            "error",
            *_ffmpeg_input(source),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-map_metadata",
            "-1",
            "-vf",
            "scale=960:960:force_original_aspect_ratio=decrease",
            "-q:v",
            "4",
            str(target),
        ],
        budget=budget,
        timeout_s=30,
    )


def acquire_sales25_media(
    operation: Sales25MediaOperation, workdir: Path, budget: DeadlineBudget
) -> OperationOutput:
    """Produce two declared artifacts; extraction never asserts model observation."""
    verify_analysis_bundle()
    returned, sources, audio_source, cover_source = _acquire_selected(operation, workdir, budget)
    if returned.get("post_id") != operation.post_id or returned.get("url") != operation.url:
        _fail("IDENTITY_MISMATCH", "acquired post ID/URL differs from selection")
    if returned.get("media_type", operation.media_type) != operation.media_type:
        _fail("IDENTITY_MISMATCH", "acquired media type differs from selection")
    raw_paths = [
        *sources,
        *([audio_source] if audio_source else []),
        *([cover_source] if cover_source else []),
    ]
    sizes = [_safe_file(path, workdir).stat().st_size for path in raw_paths]
    if not sizes or any(size < 1 for size in sizes) or sum(sizes) > operation.max_bytes:
        _fail("SIZE_EXCEEDED", "acquired media exceeds the declared byte limit")
    assets_root = workdir / "assets"
    assets_root.mkdir(mode=0o700)
    assets: list[dict[str, Any]] = []

    def add(path: Path, kind: str, timestamp: float | None = None, index: int | None = None) -> str:
        safe = _safe_file(path, assets_root)
        size = safe.stat().st_size
        if (
            not 0 < size <= SALES25_MAX_MEDIA_BYTES
            or sum(a["bytes"] for a in assets) + size > SALES25_MAX_BUNDLE_BYTES
        ):
            _fail("SIZE_EXCEEDED", "uncompressed media bundle exceeds limit")
        digest = hashlib.sha256(safe.read_bytes()).hexdigest()
        assets.append(
            {
                "path": safe.relative_to(assets_root).as_posix(),
                "sha256": digest,
                "bytes": size,
                "kind": kind,
                "timestamp_sec": timestamp,
                "photo_index": index,
            }
        )
        return digest

    duration: float | None = None
    audio_status = "unmeasured"
    coverage: dict[str, Any] = {"timestamps_sec": [], "max_gap_sec": None, "complete": False}
    if operation.media_type == "video":
        if len(sources) != 1:
            _fail("OUTPUT_INVALID", "video requires exactly one source")
        source = assets_root / f"source{sources[0].suffix.lower()}"
        shutil.copyfile(sources[0], source)
        probe = _probe(source, budget)
        duration = _duration(probe)
        if not any(s.get("codec_type") == "video" for s in probe.get("streams", [])):
            _fail("OUTPUT_INVALID", "video stream missing")
        for expected in (operation.expected_duration_sec, returned.get("duration_sec")):
            if expected is not None and (
                type(expected) not in (float, int)
                or not math.isfinite(expected)
                # Search providers may round duration metadata to whole seconds.
                or abs(duration - expected) > (1.0 if float(expected).is_integer() else 0.5)
            ):
                _fail("DURATION_MISMATCH", "acquired duration differs; partial media is rejected")
        source_sha256 = add(source, "video")
        audio_status = "absent"
        if any(s.get("codec_type") == "audio" for s in probe.get("streams", [])):
            audio_source = source
            audio_status = "available"
        frame_dir = assets_root / "frames"
        completed = _run_process(
            [
                sys.executable,
                "-I",
                str(TOOLS / _EXTRACTOR),
                "--video",
                str(source),
                "--out",
                str(frame_dir),
            ],
            cwd=workdir,
            env=_isolated_child_environment(workdir),
            budget=budget,
            timeout_s=300,
            timeout_code="MEDIA_PROCESS_TIMEOUT",
            timeout_message="sales25 extraction timed out",
        )
        if completed.returncode != 0:
            _fail("EXTRACTION_FAILED", "full-timeline extraction failed; no clipping is allowed")
        frames = json.loads((frame_dir / "frames.json").read_text(encoding="utf-8"))
        times = frames["timestamps"]
        paths = sorted(p for p in frame_dir.glob("*.jpg") if p.name != "contact.jpg")
        if (
            not 1 <= len(paths) <= SALES25_MAX_FRAMES
            or len(paths) != len(times)
            or not frames["coverage_ok"]
        ):
            _fail("COVERAGE_INVALID", "complete timestamp coverage is required")
        for path, timestamp in zip(paths, times, strict=True):
            add(path, "frame", timestamp=float(timestamp))
        add(frame_dir / "contact.jpg", "contact")
        coverage = {"timestamps_sec": times, "max_gap_sec": frames["max_gap_sec"], "complete": True}
    else:
        count = len(sources)
        if (
            not 1 <= count <= SALES25_MAX_PHOTOS
            or returned.get("photo_count") != count
            or (
                operation.expected_photo_count is not None
                and operation.expected_photo_count != count
            )
        ):
            _fail("PHOTO_COUNT_MISMATCH", "all original photos must be acquired in order")
        source_sha256 = ""
        for index, photo in enumerate(sources, 1):
            probe = _probe(photo, budget)
            codec = next(
                (
                    s.get("codec_name")
                    for s in probe.get("streams", [])
                    if s.get("codec_type") == "video"
                ),
                "",
            )
            suffix = {"mjpeg": ".jpg", "png": ".png", "webp": ".webp"}.get(codec)
            if suffix is None:
                _fail("OUTPUT_INVALID", "photo must be a decodable JPEG, PNG or WebP")
            decoded = workdir / f"photo-check-{index:03d}.jpg"
            _image(photo, decoded, budget)
            decoded.unlink()
            target = assets_root / f"photo-{index:03d}{suffix}"
            shutil.copyfile(photo, target)
            digest = add(target, "photo", index=index)
            if index == 1:
                source_sha256 = digest
        coverage["complete"] = True
    if operation.expected_sha256 is not None and source_sha256 != operation.expected_sha256:
        _fail("HASH_MISMATCH", "acquired media SHA-256 differs from expected source")
    if audio_source is not None:
        audio_duration = _duration(_probe(audio_source, budget))
        if operation.media_type == "photo":
            duration = audio_duration
            original = assets_root / "original-audio-source.bin"
            shutil.copyfile(audio_source, original)
            add(original, "audio_source")
        if duration is not None and abs(audio_duration - duration) > 0.5:
            _fail("DURATION_MISMATCH", "original audio duration differs from video")
        audio = assets_root / "original-audio.wav"
        _run(
            [
                "ffmpeg",
                "-nostdin",
                "-xerror",
                "-v",
                "error",
                *_ffmpeg_input(audio_source),
                "-map",
                "0:a:0",
                "-vn",
                "-map_metadata",
                "-1",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(audio),
            ],
            budget=budget,
        )
        decoded_duration = _duration(_probe(audio, budget))
        if duration is not None and abs(decoded_duration - duration) > 0.5:
            _fail("DURATION_MISMATCH", "original audio does not cover the entire video")
        add(audio, "audio")
        audio_status = "available"
    if cover_source is not None:
        cover = assets_root / "cover.jpg"
        _image(cover_source, cover, budget)
        add(cover, "cover")
    manifest = {
        "schema_version": 1,
        "post_id": operation.post_id,
        "url": operation.url,
        "media_type": operation.media_type,
        "status": "ready",
        "reason": "original_audio_unmeasured" if audio_status == "unmeasured" else "",
        "duration_sec": duration,
        "photo_count": len(sources) if operation.media_type == "photo" else 0,
        "audio_status": audio_status,
        "assets": assets,
        "identity": {
            "returned_post_id": returned["post_id"],
            "returned_url": returned["url"],
            "source_sha256": source_sha256,
            "expected_sha256": operation.expected_sha256,
            "verified": True,
            "cover_match": "unmeasured",
        },
        "coverage": coverage,
    }
    encoded = json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > SALES25_MAX_MANIFEST_BYTES:
        _fail("SIZE_EXCEEDED", "media manifest exceeds limit")
    manifest_path = workdir / "media-manifest.json"
    manifest_path.write_bytes(encoded)
    bundle = workdir / "media-bundle.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        for asset in assets:
            _remaining(budget)
            archive.write(assets_root / asset["path"], arcname=asset["path"])
    if bundle.stat().st_size > SALES25_MAX_BUNDLE_BYTES:
        _fail("SIZE_EXCEEDED", "media ZIP exceeds limit")
    return OperationOutput(
        (
            ProducedArtifact("media-manifest.json", manifest_path, "application/json"),
            ProducedArtifact("media-bundle.zip", bundle, "application/zip"),
        ),
        {"post_id": operation.post_id, "media_type": operation.media_type, "status": "ready"},
    )
