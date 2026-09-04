"""共有ヘルパー ``fill_missing_videos``（tiktok_acquire_status / video_algorithm 共用）の単体テスト。

フェイク Apify（0本/1本/N本/例外）とフェイク MediaJobClient（S3 既存の再利用・stage 失敗）で、
冪等・fail-open・allowlist・上限・検査素通り禁止（stage 失敗時は bytes も渡さない）を固定する。
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import pytest
from structlog.testing import capture_logs

from teamagent.adapters import media_job as media_job_module
from teamagent.adapters.apify_client import ApifyError, TikTokVideoBytes
from teamagent.adapters.media_job import MediaJobClient, MediaMarkerExistsError
from teamagent.adapters.tiktok_video_fallback import (
    ENV_FLAG,
    apify_fallback_enabled,
    attempted_name,
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
        marker_taken: frozenset[str] = frozenset(),
    ) -> None:
        self.existing = existing or {}
        self.stage_fail = stage_fail
        self.head_fail = head_fail
        # 「HEAD には見えないが PUT では 412 になる」名前。HEAD→PUT の窓で他プロセスが
        # 先に置いた状態（と、ListBucket 無しロールで HEAD 403 が「無い」に読み替わる
        # PR #379 の経路）の再現。
        self.marker_taken = marker_taken
        self.staged: dict[str, bytes] = {}
        self.stage_calls: list[dict[str, Any]] = []
        self.presigned: list[str] = []

    def find_staged(self, *, job_id: str, name: str, deadline_epoch_s: int) -> S3ObjectRef | None:
        assert job_id == _JOB
        if name in self.head_fail:
            raise RuntimeError("MEDIA_ARTIFACT_HEAD_FAILED")
        if name in self.staged:  # 本番同様、自分が置いた object（mp4 / 試行済みマーカー）も見える
            return _ref(name, self.staged[name])
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

    def stage_marker(
        self,
        *,
        job_id: str,
        name: str,
        body: bytes,
        content_type: str,
        deadline_epoch_s: int,
        max_bytes: int,
    ) -> S3ObjectRef:
        """本番の条件付き PUT（If-None-Match: *）を再現する。

        既に同名があれば S3 は 412 を返す＝``MediaMarkerExistsError``。
        無条件 PUT だった頃はここが黙って上書きし、並走した 2 プロセスが両方
        Apify run へ進めていた。
        """

        assert job_id == _JOB
        assert 0 < len(body) <= max_bytes
        self.stage_calls.append({"name": name, "content_type": content_type, "size": len(body)})
        if name in self.stage_fail:
            raise RuntimeError("MEDIA_INPUT_SIZE_INVALID")
        if name in self.staged or name in self.existing or name in self.marker_taken:
            raise MediaMarkerExistsError("MEDIA_INPUT_MARKER_EXISTS")
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
    assert media.staged["apify-p1.mp4"] == _MP4 and media.staged["apify-p2.mp4"] == _MP4
    # run の前に試行済みマーカーが置かれている（再照会・並行呼び出しの二重 run 防止）
    assert "apify-p1.attempted" in media.staged and "apify-p2.attempted" in media.staged
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
    # 置かれるのは試行済みマーカーだけ（mp4 は 1 本も置かれない）
    assert sorted(call["name"] for call in media.stage_calls) == [
        "apify-p1.attempted",
        "apify-p2.attempted",
    ]


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


# ---------------------------------------------------------------------------
# 再試行上限（1 (job, key) につき Apify run は 1 回）と並行呼び出しの防波堤
# ---------------------------------------------------------------------------


def test_second_call_never_retries_a_key_already_attempted() -> None:
    apify = _FakeApify({_URL1: _MP4})  # p2 は取れない
    media = _FakeMediaClient()
    first = fill_missing_videos(
        _JOB, {"p1": _URL1, "p2": _URL2}, media_client=media, apify=apify, max_videos=10
    )
    assert [v.key for v in first.videos] == ["p1"]
    assert "p2:APIFY_FALLBACK_MISSING" in first.warnings
    assert attempted_name("p2") == "apify-p2.attempted"
    assert "apify-p2.attempted" in media.staged  # 取れなかった key にも試行済みマーカーが残る

    second = fill_missing_videos(
        _JOB, {"p1": _URL1, "p2": _URL2}, media_client=media, apify=apify, max_videos=10
    )
    assert len(apify.calls) == 1  # 2 回目は Apify を呼ばない（p1 は S3 再利用・p2 は試行済み）
    assert [(v.key, v.reused) for v in second.videos] == [("p1", True)]
    assert second.skipped_attempted == 1
    assert "p2:APIFY_FALLBACK_SKIPPED:ATTEMPTED" in second.warnings
    assert "p2:APIFY_FALLBACK_MISSING" not in second.warnings
    assert second.est_cost_usd == 0.0


def test_marker_is_placed_before_run_even_when_apify_fails() -> None:
    apify = _FakeApify({}, raise_exc=ApifyError("APIFY_TIMEOUT: 期限内に完了しませんでした"))
    media = _FakeMediaClient()
    fill_missing_videos(_JOB, {"p1": _URL1}, media_client=media, apify=apify, max_videos=10)
    assert media.stage_calls[0]["name"] == "apify-p1.attempted"  # run より前にマーカー
    assert media.stage_calls[0]["content_type"] == "text/plain"
    # 失敗した run も「試行済み」＝再照会で再課金しない
    again = fill_missing_videos(_JOB, {"p1": _URL1}, media_client=media, apify=apify, max_videos=10)
    assert len(apify.calls) == 1
    assert "p1:APIFY_FALLBACK_SKIPPED:ATTEMPTED" in again.warnings


def test_marker_write_failure_keeps_key_out_of_the_run() -> None:
    apify = _FakeApify({_URL1: _MP4, _URL2: _MP4})
    media = _FakeMediaClient(stage_fail=frozenset({"apify-p1.attempted"}))
    outcome = fill_missing_videos(
        _JOB, {"p1": _URL1, "p2": _URL2}, media_client=media, apify=apify, max_videos=10
    )
    assert apify.calls[0]["urls"] == [_URL2]  # マーカーを置けない key は run に含めない（保守側）
    assert "p1:APIFY_FALLBACK_SKIPPED:MARKER_WRITE:MEDIA_INPUT_SIZE_INVALID" in outcome.warnings
    assert [v.key for v in outcome.videos] == ["p2"]


def test_non_numeric_port_url_is_skipped_without_raising() -> None:
    apify = _FakeApify({_URL1: _MP4})
    outcome = fill_missing_videos(
        _JOB,
        {"p1": "https://www.tiktok.com:abc/@a/video/1", "p2": _URL1},
        media_client=_FakeMediaClient(),
        apify=apify,
        max_videos=10,
    )
    assert "p1:APIFY_FALLBACK_SKIPPED:URL_NOT_ALLOWED" in outcome.warnings
    assert apify.calls[0]["urls"] == [_URL1]  # 1 行の不正 URL でジョブ全体の補完は止まらない


# ---------------------------------------------------------------------------
# 本番 IAM の再現: mcp タスクロールは s3:ListBucket を持たず、存在しないキーへの HEAD が
# 404 でなく 403 で返る（実測 2026-09-03 mcp:98・tk_6df108251c93）。実物の MediaJobClient
# を通し、「初回 HEAD が 403 でも Apify run が 1 回走り、2 回目の照会では attempted
# マーカー（HEAD 200）でスキップされる」を固定する。
# ---------------------------------------------------------------------------


class _HeadForbiddenError(Exception):
    def __init__(self) -> None:
        super().__init__("Forbidden")
        self.response = {"Error": {"Code": "403"}, "ResponseMetadata": {"HTTPStatusCode": 403}}


class _NoListBucketS3:
    """Get/Put は通るが ListBucket が無い＝未存在キーの HEAD は 403 を返す S3。"""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.head_calls: list[str] = []
        self.head_forbidden: list[str] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        body = bytes(kwargs["Body"])
        self.objects[kwargs["Key"]] = {
            "ContentLength": len(body),
            "ContentType": kwargs["ContentType"],
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(body).digest()).decode("ascii"),
            "ServerSideEncryption": kwargs["ServerSideEncryption"],
            "VersionId": "v1",
            "Metadata": dict(kwargs["Metadata"]),
        }
        return {"VersionId": "v1"}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        self.head_calls.append(key)
        if key not in self.objects:
            self.head_forbidden.append(key)
            raise _HeadForbiddenError()
        return dict(self.objects[key])


class _Session:
    def __init__(self, s3: Any) -> None:
        self._s3 = s3

    def client(self, service: str, **_kwargs: Any) -> Any:
        assert service == "s3"
        return self._s3


def _real_media_client(s3: _NoListBucketS3) -> MediaJobClient:
    return MediaJobClient(
        session=_Session(s3), queue_url="queue", table="jobs", bucket=_BUCKET, clock=lambda: 100.0
    )


def test_head_403_from_missing_list_bucket_still_runs_apify_once_and_skips_on_requery() -> None:
    media_job_module._reset_head_forbidden_warning()
    s3 = _NoListBucketS3()
    media = _real_media_client(s3)
    apify = _FakeApify({_URL1: _MP4})  # p2 は取れない

    with capture_logs() as logs:
        first = fill_missing_videos(
            _JOB,
            {"p1": _URL1, "p2": _URL2},
            media_client=media,
            apify=apify,
            max_videos=10,
            clock=lambda: 100.0,
        )
    # 初回: mp4 / attempted の HEAD は全て 403（ListBucket 無し）だが「無い」に読み替え、Apify は走る
    assert s3.head_forbidden == [
        f"media-jobs/{_JOB}/input/apify-p1.mp4",
        f"media-jobs/{_JOB}/input/apify-p1.attempted",
        f"media-jobs/{_JOB}/input/apify-p2.mp4",
        f"media-jobs/{_JOB}/input/apify-p2.attempted",
    ]
    assert len(apify.calls) == 1 and apify.calls[0]["urls"] == [_URL1, _URL2]
    assert [v.key for v in first.videos] == ["p1"]
    assert first.fetched == 1 and first.reused == 0 and first.requested == 2
    assert not any("S3_HEAD" in warning for warning in first.warnings)
    assert "p2:APIFY_FALLBACK_MISSING" in first.warnings
    # 自分で置いた object（marker / mp4）への HEAD 再検証は 200 で通っている
    assert f"media-jobs/{_JOB}/input/apify-p1.attempted" in s3.objects
    assert f"media-jobs/{_JOB}/input/apify-p2.attempted" in s3.objects
    assert f"media-jobs/{_JOB}/input/apify-p1.mp4" in s3.objects
    # 403→不在の読み替えは warning 1 回だけ（4 回の 403 に対して）
    events = [e for e in logs if e.get("event") == "media_artifact_head_forbidden_as_absent"]
    assert [e["log_level"] for e in events].count("warning") == 1

    second = fill_missing_videos(
        _JOB,
        {"p1": _URL1, "p2": _URL2},
        media_client=media,
        apify=apify,
        max_videos=10,
        clock=lambda: 100.0,
    )
    # 2 回目: p1 は HEAD 200 で再利用、p2 は attempted マーカー（HEAD 200）でスキップ＝Apify 不再走
    assert len(apify.calls) == 1
    assert [(v.key, v.reused) for v in second.videos] == [("p1", True)]
    assert second.skipped_attempted == 1
    assert "p2:APIFY_FALLBACK_SKIPPED:ATTEMPTED" in second.warnings
    assert not any("S3_HEAD" in warning for warning in second.warnings)
    assert second.est_cost_usd == 0.0
    # 2 回目に 403 が返ったのは p2 の mp4 だけ（それ以外は自分で置いた object＝200）
    assert s3.head_forbidden[4:] == [f"media-jobs/{_JOB}/input/apify-p2.mp4"]


# ---------------------------------------------------------------------------
# 試行済みマーカーの排他: HEAD→PUT の窓で並走しても Apify run は 1 プロセスだけ
# ---------------------------------------------------------------------------


def test_head_miss_then_put_412_skips_apify_like_an_existing_marker() -> None:
    """HEAD が「無い」でも条件付き PUT が 412 なら Apify を呼ばない（二重課金の防止）。

    無条件 PUT だった頃は、HEAD(attempted)→PUT の窓で並走した 2 プロセスが両方とも
    マーカーを書けてしまい、同じ URL で Apify run が 2 回走りえた。
    """
    apify = _FakeApify({_URL1: _MP4, _URL2: _MP4})
    # p1 のマーカーは「他プロセスが先に置いた」＝HEAD には見えないが PUT で 412
    media = _FakeMediaClient(marker_taken=frozenset({attempted_name("p1")}))
    outcome = fill_missing_videos(
        _JOB, {"p1": _URL1, "p2": _URL2}, media_client=media, apify=apify, max_videos=10
    )
    # 負けた p1 は HEAD で見つけた時と同じ扱い
    assert "p1:APIFY_FALLBACK_SKIPPED:ATTEMPTED" in outcome.warnings
    assert outcome.skipped_attempted == 1
    assert "p1:APIFY_FALLBACK_SKIPPED:MARKER_WRITE:MediaMarkerExistsError" not in outcome.warnings
    # 通常経路（p2）は従来どおり 1 回だけ走る
    assert len(apify.calls) == 1
    assert apify.calls[0]["urls"] == [_URL2]
    assert [v.key for v in outcome.videos] == ["p2"]
    assert attempted_name("p2") in media.staged


def test_marker_race_lost_on_every_key_calls_apify_zero_times() -> None:
    apify = _FakeApify({_URL1: _MP4, _URL2: _MP4})
    media = _FakeMediaClient(marker_taken=frozenset({attempted_name("p1"), attempted_name("p2")}))
    outcome = fill_missing_videos(
        _JOB, {"p1": _URL1, "p2": _URL2}, media_client=media, apify=apify, max_videos=10
    )
    assert apify.calls == []  # run は 0 回＝課金なし
    assert outcome.skipped_attempted == 2
    assert outcome.videos == [] and outcome.est_cost_usd == 0.0
    assert sorted(w for w in outcome.warnings if "ATTEMPTED" in w) == [
        "p1:APIFY_FALLBACK_SKIPPED:ATTEMPTED",
        "p2:APIFY_FALLBACK_SKIPPED:ATTEMPTED",
    ]


def test_non_412_marker_write_failure_still_reports_marker_write() -> None:
    """412 以外の PUT 失敗は「試行済み」ではない＝従来どおり MARKER_WRITE 警告のまま。"""
    apify = _FakeApify({_URL1: _MP4, _URL2: _MP4})
    media = _FakeMediaClient(stage_fail=frozenset({attempted_name("p1")}))
    outcome = fill_missing_videos(
        _JOB, {"p1": _URL1, "p2": _URL2}, media_client=media, apify=apify, max_videos=10
    )
    assert "p1:APIFY_FALLBACK_SKIPPED:MARKER_WRITE:MEDIA_INPUT_SIZE_INVALID" in outcome.warnings
    assert "p1:APIFY_FALLBACK_SKIPPED:ATTEMPTED" not in outcome.warnings
    assert outcome.skipped_attempted == 0  # 試行済みとして数えない
    assert apify.calls[0]["urls"] == [_URL2]


def test_video_body_staging_still_uses_unconditional_stage_bytes() -> None:
    """動画本体の staging は無条件 PUT のまま（同名の上書きが要る＝条件付きにしない）。"""
    apify = _FakeApify({_URL1: _MP4})
    media = _FakeMediaClient()
    fill_missing_videos(_JOB, {"p1": _URL1}, media_client=media, apify=apify, max_videos=10)
    # 同じ job/key でもう一度 stage_bytes を呼べば通る（412 にならない）
    ref = media.stage_bytes(
        job_id=_JOB,
        name=staged_name("p1"),
        body=_MP4,
        content_type="video/mp4",
        deadline_epoch_s=400,
        max_bytes=len(_MP4),
    )
    assert ref.key.endswith(staged_name("p1"))
    # 一方、マーカーの再 PUT は 412
    with pytest.raises(MediaMarkerExistsError):
        media.stage_marker(
            job_id=_JOB,
            name=attempted_name("p1"),
            body=b"x",
            content_type="text/plain",
            deadline_epoch_s=400,
            max_bytes=1024,
        )
