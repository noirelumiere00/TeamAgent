from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
import zipfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from teamagent.media import operations
from teamagent.media import sales25_media as media
from teamagent.media.contracts import AcquireOperation, Sales25MediaOperation
from teamagent.media.deadline import DeadlineBudget
from teamagent.media.operations import MediaOperationError, execute_operation

POST_ID = "900000000001"
URL = f"https://www.tiktok.com/@anonymous/video/{POST_ID}"


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", *args],
        check=True,
        capture_output=True,
        timeout=30,
    )


def _video(path: Path, *, sound: bool = True, duration: int = 9) -> None:
    arguments = ["-f", "lavfi", "-i", "color=c=blue:size=64x64:rate=5"]
    if sound:
        arguments += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000"]
    _ffmpeg(*arguments, "-t", str(duration), "-c:v", "mpeg4", "-q:v", "5", str(path))


def _fixture_acquire(
    monkeypatch: pytest.MonkeyPatch,
    paths: list[Path],
    *,
    media_type: str = "video",
    duration: float | None = 9,
    post_id: str = POST_ID,
    audio: Path | None = None,
    count: int | None = None,
) -> None:
    metadata = {
        "post_id": post_id,
        "url": URL.replace("/video/", f"/{media_type}/"),
        "media_type": media_type,
        "duration_sec": duration,
        "photo_count": len(paths) if count is None else count,
    }
    monkeypatch.setattr(media, "_acquire_selected", lambda *_: (metadata, paths, audio, None))


def _run(tmp_path: Path, **kwargs: Any) -> Any:
    operation = Sales25MediaOperation(
        kind="sales25_media", post_id=POST_ID, url=URL, media_type="video", **kwargs
    )
    return execute_operation(
        operation,
        workdir=tmp_path,
        load_object=lambda *_: pytest.fail(
            "selected-post acquisition must not read staged objects"
        ),
        budget=DeadlineBudget(time.time() + 60),
    )


@pytest.mark.parametrize("sound", [True, False])
def test_complete_synthetic_video_has_hashes_timestamps_and_original_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sound: bool
) -> None:
    source = tmp_path / "fixture.mp4"
    _video(source, sound=sound)
    _fixture_acquire(monkeypatch, [source])
    output = _run(tmp_path, expected_duration_sec=9)
    assert [a.name for a in output.artifacts] == ["media-manifest.json", "media-bundle.zip"]
    manifest = json.loads(output.artifacts[0].path.read_bytes())
    assert manifest["identity"]["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest["identity"]["verified"] is True
    assert manifest["identity"]["cover_match"] == "unmeasured"
    assert manifest["audio_status"] == ("available" if sound else "absent")
    assert manifest["coverage"]["complete"] is True
    timestamps = manifest["coverage"]["timestamps_sec"]
    assert timestamps[0] == 0
    assert len(timestamps) >= 3
    assert timestamps[-1] >= 6
    assert manifest["coverage"]["max_gap_sec"] <= 6
    with zipfile.ZipFile(output.artifacts[1].path) as archive:
        assert set(archive.namelist()) == {asset["path"] for asset in manifest["assets"]}
        for asset in manifest["assets"]:
            data = archive.read(asset["path"])
            assert len(data) == asset["bytes"]
            assert hashlib.sha256(data).hexdigest() == asset["sha256"]
        audio = [a for a in manifest["assets"] if a["kind"] == "audio"]
        assert bool(audio) is sound
        if sound:
            assert archive.read(audio[0]["path"]).startswith(b"RIFF")
        assert any(a["kind"] == "contact" for a in manifest["assets"])


@pytest.mark.parametrize("with_audio", [True, False])
def test_all_photos_keep_original_bytes_order_and_audio_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_audio: bool
) -> None:
    photos = []
    for index, color in enumerate(("red", "green", "blue"), 1):
        photo = tmp_path / f"fixture-{index}.png"
        _ffmpeg("-f", "lavfi", "-i", f"color=c={color}:size=64x64", "-frames:v", "1", str(photo))
        photos.append(photo)
    audio = tmp_path / "fixture-audio.wav" if with_audio else None
    if audio is not None:
        _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-t", "2", str(audio))
    _fixture_acquire(monkeypatch, photos, media_type="photo", duration=None, audio=audio)
    output = media.acquire_sales25_media(
        Sales25MediaOperation(
            kind="sales25_media",
            post_id=POST_ID,
            url=URL.replace("/video/", "/photo/"),
            media_type="photo",
            expected_photo_count=3,
            expected_sha256=hashlib.sha256(photos[0].read_bytes()).hexdigest(),
        ),
        tmp_path,
        DeadlineBudget(time.time() + 60),
    )
    manifest = json.loads(output.artifacts[0].path.read_bytes())
    assert manifest["photo_count"] == 3
    assert manifest["audio_status"] == ("available" if with_audio else "unmeasured")
    assert manifest["duration_sec"] == (2 if with_audio else None)
    assert manifest["reason"] == ("" if with_audio else "original_audio_unmeasured")
    assets = [a for a in manifest["assets"] if a["kind"] == "photo"]
    assert [a["photo_index"] for a in assets] == [1, 2, 3]
    with zipfile.ZipFile(output.artifacts[1].path) as archive:
        for original, asset in zip(photos, assets, strict=True):
            assert archive.read(asset["path"]) == original.read_bytes()


def test_id_mismatch_is_rejected_before_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "fixture.mp4"
    _video(source)
    _fixture_acquire(monkeypatch, [source], post_id="900000000002")
    with pytest.raises(MediaOperationError) as failure:
        _run(tmp_path)
    assert failure.value.code == "MEDIA_SALES25_IDENTITY_MISMATCH"
    assert not (tmp_path / "assets").exists()


@pytest.mark.parametrize(
    "duration,expected,code", [(181, None, "DURATION_EXCEEDED"), (2, 9, "DURATION_MISMATCH")]
)
def test_long_or_truncated_video_never_produces_partial_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, duration: int, expected: int | None, code: str
) -> None:
    source = tmp_path / "fixture.mp4"
    _video(source, sound=False, duration=duration)
    _fixture_acquire(monkeypatch, [source], duration=expected)
    with pytest.raises(MediaOperationError) as failure:
        _run(tmp_path, expected_duration_sec=expected)
    assert failure.value.code == f"MEDIA_SALES25_{code}"
    assert not (tmp_path / "media-manifest.json").exists()


def test_missing_photo_cannot_become_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = tmp_path / "fixture.png"
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:size=64x64", "-frames:v", "1", str(photo))
    _fixture_acquire(monkeypatch, [photo], media_type="photo", count=3)
    with pytest.raises(MediaOperationError) as failure:
        media.acquire_sales25_media(
            Sales25MediaOperation(
                kind="sales25_media",
                post_id=POST_ID,
                url=URL.replace("/video/", "/photo/"),
                media_type="photo",
                expected_photo_count=3,
            ),
            tmp_path,
            DeadlineBudget(time.time() + 60),
        )
    assert failure.value.code == "MEDIA_SALES25_PHOTO_COUNT_MISMATCH"


def test_expected_source_hash_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "fixture.mp4"
    _video(source, sound=False)
    _fixture_acquire(monkeypatch, [source])
    with pytest.raises(MediaOperationError) as failure:
        _run(tmp_path, expected_sha256="0" * 64)
    assert failure.value.code == "MEDIA_SALES25_HASH_MISMATCH"


def test_fixed_extractor_tampering_fails_before_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = tmp_path / "tools"
    for relative in ["first_visit/sales25-analysis.lock.json", media._EXTRACTOR, media._BROWSER]:
        target = tools / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(media.TOOLS / relative, target)
    (tools / media._EXTRACTOR).write_text("raise RuntimeError('tampered')\n")
    monkeypatch.setattr(media, "TOOLS", tools)
    monkeypatch.setattr(media, "_acquire_selected", lambda *_: pytest.fail("must not acquire"))
    with pytest.raises(MediaOperationError) as failure:
        _run(tmp_path)
    assert failure.value.code == "MEDIA_SALES25_PIN_MISMATCH"


def test_input_and_bundle_limits_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "fixture.mp4"
    _video(source, sound=False)
    _fixture_acquire(monkeypatch, [source])
    with pytest.raises(MediaOperationError) as failure:
        _run(tmp_path, max_bytes=1)
    assert failure.value.code == "MEDIA_SALES25_SIZE_EXCEEDED"
    monkeypatch.setattr(media, "SALES25_MAX_BUNDLE_BYTES", 8)
    with pytest.raises(MediaOperationError) as failure:
        _run(tmp_path)
    assert failure.value.code == "MEDIA_SALES25_SIZE_EXCEEDED"


@pytest.mark.parametrize("include_identity", [False, True])
def test_ytdlp_identity_metadata_is_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, include_identity: bool
) -> None:
    def downloader(options: dict[str, Any]) -> Any:
        def extract(_url: str, *, download: bool) -> dict[str, Any]:
            assert download is True
            Path(options["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"fixture-video")
            return {
                "extractor_key": "TikTok",
                "id": POST_ID,
                "webpage_url": URL,
                "duration": 9,
                "thumbnail": "https://images.example/cover.jpg",
            }

        return nullcontext(SimpleNamespace(extract_info=extract))

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=downloader))
    result = operations._acquire_ytdlp(
        AcquireOperation(kind="acquire", url=URL),
        tmp_path,
        DeadlineBudget(time.time() + 60),
        **({"include_identity": True} if include_identity else {}),
    )
    expected: dict[str, Any] = {"extractor": "tiktok", "size": len(b"fixture-video")}
    if include_identity:
        expected.update(
            post_id=POST_ID, url=URL, duration_sec=9, thumbnail="https://images.example/cover.jpg"
        )
    assert result.metadata == expected


@pytest.mark.parametrize("has_thumbnail", [False, True])
def test_selected_ytdlp_cover_uses_protected_image_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_thumbnail: bool
) -> None:
    monkeypatch.setenv("VIDEO_DL_ORDER", "ytdlp")
    monkeypatch.setattr(media, "validate_acquire_url", lambda value: value)
    thumbnail = "https://images.example/cover.jpg" if has_thumbnail else None

    def acquire(_operation: Any, stage: Path, _budget: Any, *, include_identity: bool) -> Any:
        assert include_identity is True
        source = stage / "video.mp4"
        source.write_bytes(b"fixture-video")
        return operations.OperationOutput(
            (operations.ProducedArtifact("media", source, "video/mp4"),),
            {"post_id": POST_ID, "url": URL, "duration_sec": 9, "thumbnail": thumbnail},
        )

    fetched = []

    def protected_image(url: str, destination: Path, **_kwargs: Any) -> bool:
        fetched.append(url)
        destination.write_bytes(b"sanitized-cover")
        return True

    monkeypatch.setattr(media, "_acquire_ytdlp", acquire)
    monkeypatch.setattr(media, "_fetch_public_image", protected_image)
    metadata, _, _, cover = media._acquire_selected(
        Sales25MediaOperation(kind="sales25_media", post_id=POST_ID, url=URL, media_type="video"),
        tmp_path,
        DeadlineBudget(time.time() + 60),
    )
    assert metadata["post_id"] == POST_ID
    assert fetched == ([thumbnail] if has_thumbnail else [])
    assert (cover is not None) is has_thumbnail
    if cover is not None:
        assert cover.read_bytes() == b"sanitized-cover"


@pytest.mark.parametrize("cover_url", [None, "https://images.example/cover.jpg"])
def test_browser_photo_cover_comes_from_verified_page_open_graph(
    tmp_path: Path, cover_url: str | None
) -> None:
    photo_url = URL.replace("/video/", "/photo/")
    photos = ["https://images.example/photo1.png", "https://images.example/photo2.png"]
    fixture = {
        "script": str(media.TOOLS / media._BROWSER),
        "out": str(tmp_path / "browser"),
        "url": photo_url,
        "cover": cover_url,
        "detail": {
            "id": POST_ID,
            "author": {"uniqueId": "anonymous"},
            "imagePost": {"images": [{"imageURL": {"urlList": [url]}} for url in photos]},
        },
        "resources": {
            photos[0]: "first-photo",
            photos[1]: "second-photo",
            "https://images.example/cover.jpg": "original-post-cover",
        },
    }
    harness = r"""
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
const fixture = JSON.parse(fs.readFileSync(0, "utf8"));
const fetched = [];
let result;
const fakeProcess = {
  argv: ["node", fixture.script, fixture.url, fixture.out, "1048576"],
  env: { CHROMIUM_PATH: "/fixture/chrome" },
  stdout: { write: text => { result = JSON.parse(text); } },
};
const document = {
  getElementById: () => ({ textContent: JSON.stringify({ __DEFAULT_SCOPE__: {
    "webapp.photo-detail": { itemInfo: { itemStruct: fixture.detail } },
  } }) }),
  querySelector: selector => selector === 'meta[property="og:image"]' && fixture.cover
    ? { getAttribute: () => fixture.cover } : null,
};
const page = {
  setRequestInterception: async () => {}, on: () => {}, goto: async () => {},
  url: () => fixture.url, evaluate: async (fn, arg) => fn(arg),
};
const context = vm.createContext({
  process: fakeProcess, URL, Buffer, Uint8Array, btoa, setTimeout, clearTimeout,
  AbortController, document,
  fetch: async target => {
    if (!(target in fixture.resources)) throw new Error("unconfigured network request forbidden");
    fetched.push(target);
    let done = false;
    const bytes = Buffer.from(fixture.resources[target]);
    return { ok: true, headers: { get: () => String(bytes.length) }, body: { getReader: () => ({
      read: async () => { if (done) return { done: true }; done = true; return { value: bytes }; },
      cancel: async () => {},
    }) } };
  },
});
const modules = {
  "node:fs": { default: { ...fs, existsSync: name => name === "/fixture/chrome" || fs.existsSync(name) } },
  "node:path": { default: path },
  "puppeteer-core": { default: { launch: async () => ({ newPage: async () => page, close: async () => {} }) } },
  "./dns_pinned_proxy.mjs": { startDnsPinnedProxy: async () => ({ url: "http://127.0.0.1:9999", close: async () => {} }) },
};
const script = new vm.SourceTextModule(fs.readFileSync(fixture.script, "utf8"), { context });
await script.link(name => {
  const exports = modules[name];
  if (!exports) throw new Error("unconfigured import forbidden");
  return new vm.SyntheticModule(Object.keys(exports), function() {
    for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
  }, { context });
});
await script.evaluate();
process.stdout.write(JSON.stringify({ result, fetched, exitCode: fakeProcess.exitCode || 0 }));
"""
    runner = tmp_path / "browser-harness.mjs"
    runner.write_text(harness)
    completed = subprocess.run(
        ["node", "--experimental-vm-modules", str(runner)],
        input=json.dumps(fixture),
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    actual = json.loads(completed.stdout)
    assert actual["exitCode"] == 0
    assert actual["result"]["post_id"] == POST_ID
    assert actual["result"]["url"] == photo_url
    assert actual["result"]["photo_count"] == 2
    assert actual["fetched"] == [*photos, *([cover_url] if cover_url else [])]
    assert actual["result"]["cover"] == ("cover.raw" if cover_url else None)
    if cover_url:
        assert (tmp_path / "browser/cover.raw").read_bytes() == b"original-post-cover"
