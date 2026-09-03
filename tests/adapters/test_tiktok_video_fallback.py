"""共有ヘルパー ``fill_missing_videos``（tiktok_acquire_status / video_algorithm 共用）の単体テスト。

フェイク Apify（0本/1本/N本/例外）とフェイク MediaJobClient（S3 既存の再利用・stage 失敗）で、
冪等・fail-open・allowlist・上限・検査素通り禁止（stage 失敗時は bytes も渡さない）を固定する。
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from teamagent.adapters.apify_client import ApifyError, TikTokVideoBytes
from teamagent.adapters.tiktok_video_fallback import (
    ENV_FLAG,
    apify_fallback_enabled,
    fallback_job_id,
    fill_missing_videos,
    staged_name,
)
from teamagent.media.contracts import S3ObjectRef

_BUCKET = "teamagent-media-test"
_JOB = "tk_0123456789ab"
_MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40
_URL1 = "https://www.tiktok.com/@a/video/1"
_URL2 = "https://www.tiktok.com/@a/video/2"
_URL3 = "https://www.tiktok.com/@a/video/3"


class _FakeApify:
    def __init__(
        self, bodies: dict[str, bytes], *, raise_exc: Exception | None = None, unit: float = 0.004
    ) -> None:
        self.bodies = bodies
        self.raise_exc = raise_exc
        self.unit = unit
        self.calls: list[dict[str, Any]] = []

    def tiktok_download_videos(
        self,
        post_urls: list[str],
        *,
        max_videos: int,
        deadline_s: int,
        max_bytes_per_video: int,
        request_id: str,
        user_email: str,
    ) -> tuple[list[TikTokVideoBytes], float]:
        self.calls.append(
            {
                "urls": list(post_urls),
                "max_videos": max_videos,
                "deadline_s": deadline_s,
                "max_bytes": max_bytes_per_video,
                "request_id": request_id,
                "user_email": user_email,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        got = [
            TikTokVideoBytes(
                post_url=url,
                video_id=url.rsplit("/", 1)[1],
                kvs_key=f"video-{url[-1]}.mp4",
                body=body,
            )
            for url in post_urls[:max_videos]
            if (body := self.bodies.get(url)) is not None
        ]
        return got, self.unit * len(post_urls[:max_videos])


def _ref(name: str, body: bytes) -> S3ObjectRef:
    return S3ObjectRef(
        bucket=_BUCKET,
        key=f"media-jobs/{_JOB}/input/{name}",
        version_id="v1",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
        content_type="video/mp4",
    )


class _FakeMediaClient:
    def __init__(
        self,
        existing: dict[str, S3ObjectRef] | None = None,
        *,
        stage_fail: frozenset[str] = frozenset(),
        head_fail: frozenset[str] = frozenset(),
    ) -> None:
        self.existing = existing or {}
        self.stage_fail = stage_fail
        self.head_fail = head_fail
        self.staged: dict[str, bytes] = {}
        self.stage_calls: list[dict[str, Any]] = []
        self.presigned: list[str] = []

    def find_staged(self, *, job_id: str, name: str, deadline_epoch_s: int) -> S3ObjectRef | None:
        assert job_id == _JOB
        if name in self.head_fail:
            raise RuntimeError("MEDIA_ARTIFACT_HEAD_FAILED")
        return self.existing.get(name)

    def stage_bytes(
        self,
        *,
        job_id: str,
        name: str,
        body: bytes,
        content_type: str,
        deadline_epoch_s: int,
        max_bytes: int,
    ) -> S3ObjectRef:
        assert job_id == _JOB
        assert 0 < len(body) <= max_bytes
        self.stage_calls.append({"name": name, "content_type": content_type, "size": len(body)})
        if name in self.stage_fail:
            raise RuntimeError("MEDIA_INPUT_SIZE_INVALID")
        self.staged[name] = body
        return _ref(name, body)

    def presign_get(self, ref: S3ObjectRef, *, deadline_epoch_s: int, expires_s: int) -> str:
        assert expires_s == 600
        self.presigned.append(ref.key)
        return f"https://signed.example/{ref.key}"


def test_flag_is_opt_in_and_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    assert apify_fallback_enabled() is False
    for value in ("1", "true", "YES"):
        monkeypatch.setenv(ENV_FLAG, value)
        assert apify_fallback_enabled() is True
    monkeypatch.setenv(ENV_FLAG, "0")
    assert apify_fallback_enabled() is False


def test_fallback_job_id_matches_media_job_shape() -> None:
    job_id = fallback_job_id("req:apify-fallback:abc")
    assert job_id.startswith("mj_") and len(job_id) == 27
    assert staged_name("p01002") == "apify-p01002.mp4"


def test_fetches_stages_and_presigns_n_videos() -> None:
    apify = _FakeApify({_URL1: _MP4, _URL2: _MP4})
    media = _FakeMediaClient()
    outcome = fill_missing_videos(
        _JOB,
        {"p1": _URL1, "p2": _URL2},
        media_client=media,
        apify=apify,
        deadline_s=120,
        request_id="req",
        user_email="a@example.com",
        max_videos=10,
    )
    assert [v.key for v in outcome.videos] == ["p1", "p2"]
    assert outcome.fetched == 2 and outcome.reused == 0 and outcome.requested == 2
    assert outcome.est_cost_usd == pytest.approx(0.008)
    assert apify.calls[0]["urls"] == [_URL1, _URL2]
    assert apify.calls[0]["max_bytes"] == 30 * 1024 * 1024
    assert apify.calls[0]["deadline_s"] == 120
    assert apify.calls[0]["user_email"] == "a@example.com"
    assert media.staged == {"apify-p1.mp4": _MP4, "apify-p2.mp4": _MP4}
    for video in outcome.videos:
        assert video.ref is not None and video.ref.key == f"media-jobs/{_JOB}/input/{video.name}"
        assert video.url == f"https://signed.example/{video.ref.key}"
        assert video.body is None  # keep_body=False（既定）: 大きい bytes を抱えない
        assert video.reused is False
    assert outcome.warnings == ["APIFY_FALLBACK_OK:fetched=2,reused=0,est_cost_usd=0.0080"]


def test_reuses_existing_s3_object_without_calling_apify() -> None:
    apify = _FakeApify({_URL1: _MP4, _URL2: _MP4})
    media = _FakeMediaClient({"apify-p1.mp4": _ref("apify-p1.mp4", _MP4)})
    outcome = fill_missing_videos(
        _JOB, {"p1": _URL1, "p2": _URL2}, media_client=media, apify=apify, max_videos=10
    )
    assert apify.calls[0]["urls"] == [_URL2]  # p1 は S3 既存を再利用＝Apify に渡さない
    assert [(v.key, v.reused) for v in outcome.videos] == [("p1", True), ("p2", False)]
    assert outcome.reused == 1 and outcome.fetched == 1
    # 全部が既存なら Apify は一切呼ばれない（冪等）
    apify2 = _FakeApify({})
    media2 = _FakeMediaClient(
        {"apify-p1.mp4": _ref("apify-p1.mp4", _MP4), "apify-p2.mp4": _ref("apify-p2.mp4", _MP4)}
    )
    outcome2 = fill_missing_videos(
        _JOB, {"p1": _URL1, "p2": _URL2}, media_client=media2, apify=apify2, max_videos=10
    )
    assert apify2.calls == []
    assert outcome2.reused == 2 and outcome2.est_cost_usd == 0.0


def test_apify_exception_is_fail_open_with_reason_codes() -> None:
    apify = _FakeApify({}, raise_exc=ApifyError("APIFY_RUN_FAILED: clockworks status=FAILED"))
    media = _FakeMediaClient()
    outcome = fill_missing_videos(
        _JOB, {"p1": _URL1, "p2": _URL2}, media_client=media, apify=apify, max_videos=10
    )
    assert outcome.videos == []
    assert "APIFY_FALLBACK_FAILED:APIFY_RUN_FAILED" in outcome.warnings
    assert "p1:APIFY_FALLBACK_MISSING" in outcome.warnings
    assert "p2:APIFY_FALLBACK_MISSING" in outcome.warnings
    assert media.stage_calls == []


def test_zero_results_marks_every_pending_key_missing() -> None:
    apify = _FakeApify({})
    outcome = fill_missing_videos(
        _JOB, {"p1": _URL1}, media_client=_FakeMediaClient(), apify=apify, max_videos=10
    )
    assert outcome.videos == []
    assert outcome.warnings == ["p1:APIFY_FALLBACK_MISSING"]


def test_cap_and_allowlist_and_key_shape_are_enforced_before_apify() -> None:
    apify = _FakeApify({_URL1: _MP4, _URL2: _MP4, _URL3: _MP4})
    outcome = fill_missing_videos(
        _JOB,
        {
            "p1": _URL1,
            "p2": "https://www.instagram.com/reel/abc/",  # allowlist 外
            "P3": _URL3,  # key の形が不正（大文字）
            "p4": _URL2,
            "p5": _URL3,  # cap=2 で打ち切り
        },
        media_client=_FakeMediaClient(),
        apify=apify,
        max_videos=2,
    )
    assert apify.calls[0]["urls"] == [_URL1, _URL2]
    assert "p2:APIFY_FALLBACK_SKIPPED:URL_NOT_ALLOWED" in outcome.warnings
    assert "P3:APIFY_FALLBACK_SKIPPED:KEY_INVALID" in outcome.warnings
    assert "p5:APIFY_FALLBACK_SKIPPED:CAP" in outcome.warnings
    assert [v.key for v in outcome.videos] == ["p1", "p4"]


def test_stage_failure_withholds_bytes_and_head_failure_is_recorded() -> None:
    apify = _FakeApify({_URL1: _MP4, _URL2: _MP4})
    media = _FakeMediaClient(
        stage_fail=frozenset({"apify-p1.mp4"}), head_fail=frozenset({"apify-p2.mp4"})
    )
    outcome = fill_missing_videos(
        _JOB,
        {"p1": _URL1, "p2": _URL2},
        media_client=media,
        apify=apify,
        max_videos=10,
        keep_body=True,
    )
    # p2 は HEAD 失敗＝候補から外れて Apify にも渡さない（黙って再取得しない）
    assert apify.calls[0]["urls"] == [_URL1]
    assert "p2:APIFY_FALLBACK_FAILED:S3_HEAD:MEDIA_ARTIFACT_HEAD_FAILED" in outcome.warnings
    # p1 は取得できたが S3 検査（stage_bytes）に落ちた＝bytes も渡さない
    assert outcome.videos == []
    assert "p1:APIFY_FALLBACK_FAILED:S3_STAGE:MEDIA_INPUT_SIZE_INVALID" in outcome.warnings
    assert "p1:APIFY_FALLBACK_MISSING" in outcome.warnings


def test_without_media_client_returns_bytes_only() -> None:
    apify = _FakeApify({_URL1: _MP4})
    outcome = fill_missing_videos(
        _JOB, {"p1": _URL1}, media_client=None, apify=apify, max_videos=1, keep_body=True
    )
    assert len(outcome.videos) == 1
    video = outcome.videos[0]
    assert video.body == _MP4 and video.ref is None and video.url is None
    assert video.content_type == "video/mp4"


def test_keep_body_true_returns_bytes_alongside_ref() -> None:
    apify = _FakeApify({_URL1: _MP4})
    outcome = fill_missing_videos(
        _JOB,
        {"p1": _URL1},
        media_client=_FakeMediaClient(),
        apify=apify,
        max_videos=1,
        keep_body=True,
    )
    assert outcome.videos[0].body == _MP4
    assert outcome.videos[0].ref is not None


def test_env_defaults_bound_deadline_and_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_APIFY_FALLBACK_DEADLINE_S", "9999")  # 上限 240 に丸める
    monkeypatch.setenv("TIKTOK_APIFY_FALLBACK_MAX_VIDEOS", "1")
    apify = _FakeApify({_URL1: _MP4, _URL2: _MP4})
    outcome = fill_missing_videos(
        _JOB, {"p1": _URL1, "p2": _URL2}, media_client=_FakeMediaClient(), apify=apify
    )
    assert apify.calls[0]["deadline_s"] == 240
    assert apify.calls[0]["urls"] == [_URL1]
    assert "p2:APIFY_FALLBACK_SKIPPED:CAP" in outcome.warnings
