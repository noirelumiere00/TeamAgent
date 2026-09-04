"""tiktok_acquire_status の二段構え（worker 失敗分を mcp 側 Apify で補完）の Skill 層テスト。

AWS/Apify には触れず、フェイク store（status dict + media client）とフェイク Apify で
opt-in OFF の完全同一出力・不足分だけの発火・冪等（S3 既存の再利用）・fail-open・上限を固定する。
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.apify_client import ApifyError, TikTokVideoBytes
from teamagent.adapters.media_job import MediaMarkerExistsError
from teamagent.media.contracts import S3ObjectRef, TikTokAcquireOperation, make_job_request
from teamagent.skills.base import ASYNC_JOB_POLL_METADATA_KEY, SkillContext
from teamagent.skills.tiktok_acquire.apify_fallback import (
    ENV_FLAG,
    ApifyVideoFallback,
    _job_lock,
    plan_fallback,
)
from teamagent.skills.tiktok_acquire.schema import TikTokAcquireStatusInput
from teamagent.skills.tiktok_acquire.skill import TikTokAcquireStatusSkill

_BUCKET = "teamagent-media-test"
_JOB = "tk_0123456789ab"
_MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40
_EMAIL = "a@vectorinc.co.jp"
_AUDIT = hashlib.sha256(_EMAIL.encode()).hexdigest()
_URL = {
    "p01001": "https://www.tiktok.com/@a/video/1",
    "p01002": "https://www.tiktok.com/@a/video/2",
    "p02001": "https://www.tiktok.com/@b/video/3",
    "p02002": "https://www.tiktok.com/@b/video/4",
}


def _row(pid: str, kw: str, *, downloaded: bool, url: str | None = None) -> dict[str, Any]:
    return {
        "pid": pid,
        "kw": kw,
        "downloaded": downloaded,
        "s3_key": f"media-jobs/{_JOB}/attempts/1/a/output/videos/{pid}.mp4" if downloaded else None,
        "url": f"https://signed/{pid}" if downloaded else None,
        "thumb_url": f"https://signed/thumb-{pid}",
        "tiktok_url": url if url is not None else _URL[pid],
    }


def _done_status() -> dict[str, Any]:
    return {
        "job_id": _JOB,
        "status": "done",
        "progress": None,
        "counts": {"kw": 2, "posts": 4, "videos": 1, "per_kw": {"x": 2, "y": 2}},
        "error_code": None,
        "stop_reason": None,
        "warnings": ["y:MEDIA_TIKTOK_RESULT_SHORTFALL"],
        "shortfalls": [{"kw": "y", "requested": 10, "actual": 2}],
        "s3_bucket": _BUCKET,
        "s3_prefix": f"media-jobs/{_JOB}/",
        "posts_json_url": "https://signed/posts",
        "config_json_url": "https://signed/config",
        "manifest_url": "https://signed/manifest",
        "videos": [
            _row("p01001", "x", downloaded=True),
            _row("p01002", "x", downloaded=False),
            _row("p02001", "y", downloaded=False),
            _row("p02002", "y", downloaded=False),
        ],
    }


def _request(*, videos_per_kw: int = 2) -> Any:
    return make_job_request(
        operation=TikTokAcquireOperation(
            kind="tiktok_acquire", keywords=("x", "y"), n_per_kw=1, videos_per_kw=videos_per_kw
        ),
        output_bucket=_BUCKET,
        request_fingerprint="fp",
        now_epoch_s=100,
        timeout_s=900,
        job_id=_JOB,
        output_prefix=f"media-jobs/{_JOB}/",
        audit_principal_hash=_AUDIT,
    )


def _ref(name: str, body: bytes = _MP4) -> S3ObjectRef:
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
        request: Any | None,
        *,
        existing: dict[str, S3ObjectRef] | None = None,
        request_exc: Exception | None = None,
    ) -> None:
        self._request = request
        self._request_exc = request_exc
        self.existing = existing or {}
        self.staged: dict[str, bytes] = {}

    def get_request(
        self, job_id: str, *, deadline_epoch_s: int, expected_audit_principal_hash: str
    ):
        assert job_id == _JOB
        assert expected_audit_principal_hash == _AUDIT
        if self._request_exc is not None:
            raise self._request_exc
        return self._request

    def find_staged(self, *, job_id: str, name: str, deadline_epoch_s: int) -> S3ObjectRef | None:
        if name in self.staged:  # 本番同様、自分が置いた object（mp4 / 試行済みマーカー）も見える
            return _ref(name, self.staged[name])
        return self.existing.get(name)

    def stage_bytes(self, *, job_id: str, name: str, body: bytes, content_type: str, **kw: Any):
        assert job_id == _JOB
        # mp4 本体は video/mp4 で無条件 PUT（上書き可）
        assert content_type == "video/mp4", (name, content_type)
        self.staged[name] = body
        return _ref(name, body)

    def stage_marker(self, *, job_id: str, name: str, body: bytes, content_type: str, **kw: Any):
        """試行済みマーカーは text/plain の条件付き PUT（既にあれば 412 = 先着に負け）。"""

        assert job_id == _JOB
        assert content_type == "text/plain", (name, content_type)
        if name in self.staged or name in self.existing:
            raise MediaMarkerExistsError("MEDIA_INPUT_MARKER_EXISTS")
        self.staged[name] = body
        return _ref(name, body)

    def presign_get(self, ref: S3ObjectRef, *, deadline_epoch_s: int, expires_s: int) -> str:
        return f"https://signed.example/{ref.key}"


class _FakeStore:
    def __init__(self, status: dict[str, Any] | None, media: _FakeMediaClient) -> None:
        self._status = status
        self._media = media

    def get_status(self, job_id: str, *, audit_principal_hash: str) -> dict[str, Any] | None:
        assert audit_principal_hash == _AUDIT
        return copy.deepcopy(self._status)

    def media_client(self) -> _FakeMediaClient:
        return self._media


class _FakeApify:
    def __init__(self, bodies: dict[str, bytes], *, raise_exc: Exception | None = None) -> None:
        self.bodies = bodies
        self.raise_exc = raise_exc
        self.calls: list[list[str]] = []

    def tiktok_download_videos(self, post_urls: list[str], *, max_videos: int, **kw: Any):
        self.calls.append(list(post_urls))
        if self.raise_exc is not None:
            raise self.raise_exc
        got = [
            TikTokVideoBytes(post_url=u, video_id=u[-1], kvs_key=f"video-{u[-1]}.mp4", body=b)
            for u in post_urls[:max_videos]
            if (b := self.bodies.get(u)) is not None
        ]
        return got, 0.004 * len(post_urls[:max_videos])


def _ctx() -> SkillContext:
    return SkillContext(request_id="req-test", user_id="U123", metadata={"user_email": _EMAIL})


def _skill(
    status: dict[str, Any] | None,
    *,
    apify: _FakeApify,
    media: _FakeMediaClient,
) -> tuple[TikTokAcquireStatusSkill, ApifyVideoFallback]:
    store = _FakeStore(status, media)
    fallback = ApifyVideoFallback(apify=apify, media_client_factory=store.media_client)
    return TikTokAcquireStatusSkill(store=store, apify_fallback=fallback), fallback  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# opt-in OFF: 従来と同一出力（1 バイトも変わらない・Apify にも S3 にも触れない）
# ---------------------------------------------------------------------------


_LEGACY_FIXTURE = Path(__file__).parent / "fixtures" / "status_done_legacy_origin_dev.json"


def test_flag_off_matches_origin_dev_legacy_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF の出力は origin/dev（6da90f9）の status 写像で固定した JSON フィクスチャと完全一致。

    フィクスチャは dev 側の ``TikTokAcquireStatusSkill.run``（store の dict を
    TikTokAcquireStatusOutput の同名フィールドへそのまま写す・message は done 固定文）から
    導いたもので、新コード同士の自己参照比較ではない。
    """
    monkeypatch.delenv(ENV_FLAG, raising=False)
    apify = _FakeApify({url: _MP4 for url in _URL.values()})
    media = _FakeMediaClient(_request())
    skill, _ = _skill(_done_status(), apify=apify, media=media)

    out = skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    expected = json.loads(_LEGACY_FIXTURE.read_text(encoding="utf-8"))
    assert json.loads(out.model_dump_json()) == expected
    assert apify.calls == []
    assert media.staged == {}


def test_flag_off_never_touches_apify_or_s3_and_adds_no_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    apify = _FakeApify({url: _MP4 for url in _URL.values()})
    media = _FakeMediaClient(_request())
    skill, _ = _skill(_done_status(), apify=apify, media=media)
    out = skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    assert apify.calls == []
    assert media.staged == {}
    assert all("acquired_via" not in row for row in out.videos)
    assert out.counts == {"kw": 2, "posts": 4, "videos": 1, "per_kw": {"x": 2, "y": 2}}


# ---------------------------------------------------------------------------
# opt-in ON: 不足分だけ補完し videos[] に合流（acquired_via 付き）
# ---------------------------------------------------------------------------


def test_flag_on_fills_only_deficit_and_merges_with_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    apify = _FakeApify({_URL["p01002"]: _MP4, _URL["p02001"]: _MP4})  # p02002 は取れない
    media = _FakeMediaClient(_request(videos_per_kw=2))
    skill, _ = _skill(_done_status(), apify=apify, media=media)

    out = skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    assert out.status == "done"
    # 不足 = x:1本(p01002) + y:2本(p02001,p02002) → その3本だけを Apify に渡す（取得済みは渡さない）
    assert apify.calls == [[_URL["p01002"], _URL["p02001"], _URL["p02002"]]]
    by_pid = {row["pid"]: row for row in out.videos}
    assert by_pid["p01001"]["acquired_via"] == "worker"
    assert by_pid["p01001"]["s3_key"].endswith("/output/videos/p01001.mp4")  # 既存は不変
    for pid in ("p01002", "p02001"):
        assert by_pid[pid]["downloaded"] is True
        assert by_pid[pid]["acquired_via"] == "apify"
        assert by_pid[pid]["s3_key"] == f"media-jobs/{_JOB}/input/apify-{pid}.mp4"
        assert (
            by_pid[pid]["url"] == f"https://signed.example/media-jobs/{_JOB}/input/apify-{pid}.mp4"
        )
    assert by_pid["p02002"]["downloaded"] is False
    assert by_pid["p02002"]["acquired_via"] is None
    assert media.staged["apify-p01002.mp4"] == _MP4
    assert media.staged["apify-p02001.mp4"] == _MP4
    # 3 本とも run の前に試行済みマーカーが置かれている（取れなかった p02002 も再試行しない）
    for pid in ("p01002", "p02001", "p02002"):
        assert f"apify-{pid}.attempted" in media.staged
    assert out.counts["videos"] == 3
    assert out.counts["videos_apify"] == 2
    assert "y:MEDIA_TIKTOK_RESULT_SHORTFALL" in out.warnings  # 従来の警告は保持
    assert "p02002:APIFY_FALLBACK_MISSING" in out.warnings
    assert "APIFY_FALLBACK_OK:fetched=2,reused=0,est_cost_usd=0.0120" in out.warnings
    assert "2 本は Apify で補完" in out.message
    assert out.shortfalls == [{"kw": "y", "requested": 10, "actual": 2}]


def test_flag_on_is_idempotent_and_reuses_staged_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    apify = _FakeApify({url: _MP4 for url in _URL.values()})
    existing = {
        f"apify-{pid}.mp4": _ref(f"apify-{pid}.mp4") for pid in ("p01002", "p02001", "p02002")
    }
    media = _FakeMediaClient(_request(), existing=existing)
    skill, _ = _skill(_done_status(), apify=apify, media=media)

    first = skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    second = skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    assert apify.calls == []  # S3 に既にある＝Apify を二重実行しない
    assert first.counts["videos"] == 4 and second.counts["videos"] == 4
    assert first.counts["videos_apify"] == 3
    assert "APIFY_FALLBACK_OK:fetched=0,reused=3,est_cost_usd=0.0000" in first.warnings
    assert [row["acquired_via"] for row in second.videos] == ["worker", "apify", "apify", "apify"]


def test_flag_on_no_deficit_never_calls_apify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    status = _done_status()
    for row in status["videos"]:
        row.update(downloaded=True, s3_key=f"k-{row['pid']}", url=f"https://signed/{row['pid']}")
    apify = _FakeApify({url: _MP4 for url in _URL.values()})
    skill, _ = _skill(status, apify=apify, media=_FakeMediaClient(_request()))

    out = skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    assert apify.calls == []
    assert all(row["acquired_via"] == "worker" for row in out.videos)
    assert not any(w.startswith("APIFY_FALLBACK") for w in out.warnings)
    assert "Apify で補完" not in out.message


def test_flag_on_apify_failure_is_fail_open_with_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    apify = _FakeApify({}, raise_exc=ApifyError("APIFY_RUN_FAILED: clockworks status=FAILED"))
    skill, _ = _skill(_done_status(), apify=apify, media=_FakeMediaClient(_request()))

    out = skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    assert out.status == "done"
    assert [row["downloaded"] for row in out.videos] == [
        True,
        False,
        False,
        False,
    ]  # 従来結果を保持
    assert out.counts["videos"] == 1
    assert "APIFY_FALLBACK_FAILED:APIFY_RUN_FAILED" in out.warnings
    assert "clockworks" not in " ".join(out.warnings)  # 生メッセージは載せない（コードだけ）
    assert "Apify で補完" not in out.message


def test_flag_on_budget_exhaustion_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.adapters.cost_guard import CostLimitExceededError

    monkeypatch.setenv(ENV_FLAG, "1")
    apify = _FakeApify({}, raise_exc=CostLimitExceededError("今月の枠を使い切りました"))
    skill, _ = _skill(_done_status(), apify=apify, media=_FakeMediaClient(_request()))
    out = skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    assert out.status == "done"
    assert "APIFY_FALLBACK_FAILED:CostLimitExceededError" in out.warnings


def test_flag_on_request_unavailable_skips_without_touching_apify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    apify = _FakeApify({url: _MP4 for url in _URL.values()})
    media = _FakeMediaClient(None, request_exc=RuntimeError("MEDIA_JOB_REQUEST_INVALID"))
    skill, _ = _skill(_done_status(), apify=apify, media=media)
    out = skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    assert apify.calls == []
    assert "APIFY_FALLBACK_SKIPPED:REQUEST_UNAVAILABLE:MEDIA_JOB_REQUEST_INVALID" in out.warnings
    assert out.counts["videos"] == 1


def test_flag_on_hard_cap_env_limits_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    monkeypatch.setenv("TIKTOK_APIFY_FALLBACK_MAX_VIDEOS", "1")
    apify = _FakeApify({url: _MP4 for url in _URL.values()})
    skill, _ = _skill(_done_status(), apify=apify, media=_FakeMediaClient(_request()))
    out = skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    assert apify.calls == [[_URL["p01002"]]]
    assert out.counts["videos_apify"] == 1


def test_flag_on_non_tiktok_post_url_is_never_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    status = _done_status()
    status["videos"][1]["tiktok_url"] = "https://www.instagram.com/reel/abc/"
    apify = _FakeApify({url: _MP4 for url in _URL.values()})
    skill, _ = _skill(status, apify=apify, media=_FakeMediaClient(_request()))
    skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    assert apify.calls == [[_URL["p02001"], _URL["p02002"]]]  # allowlist 外は候補にも入らない


def test_flag_on_ignores_non_done_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    apify = _FakeApify({url: _MP4 for url in _URL.values()})
    skill, _ = _skill(
        {"status": "running", "progress": {"kw_done": 1}},
        apify=apify,
        media=_FakeMediaClient(_request()),
    )
    out = skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    assert out.status == "running" and apify.calls == []


# ---------------------------------------------------------------------------
# plan_fallback（不足の決定論）
# ---------------------------------------------------------------------------


def test_plan_fallback_orders_by_keyword_then_display_and_caps() -> None:
    videos = _done_status()["videos"]
    plan = plan_fallback(videos, videos_per_kw=2, keywords=["y", "x"], hard_cap=20)
    assert [c.pid for c in plan] == ["p02001", "p02002", "p01002"]  # KW順 → 表示順
    assert plan_fallback(videos, videos_per_kw=1, keywords=["x", "y"], hard_cap=20) == [
        plan_fallback(videos, videos_per_kw=1, keywords=["x", "y"], hard_cap=20)[0]
    ]
    assert [
        c.pid for c in plan_fallback(videos, videos_per_kw=1, keywords=["x", "y"], hard_cap=20)
    ] == ["p02001"]  # x は既に1本ある
    assert plan_fallback(videos, videos_per_kw=2, keywords=["x", "y"], hard_cap=0) == []
    assert [
        c.pid for c in plan_fallback(videos, videos_per_kw=5, keywords=["x", "y"], hard_cap=2)
    ] == [
        "p01002",
        "p02001",
    ]


# ---------------------------------------------------------------------------
# 二重課金の防波堤: 再照会で再試行しない / 見張り経路では発火しない / 同一 job は直列
# ---------------------------------------------------------------------------


def test_second_status_call_does_not_rerun_apify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    apify = _FakeApify({_URL["p01002"]: _MP4, _URL["p02001"]: _MP4})  # p02002 は取れない
    media = _FakeMediaClient(_request())
    skill, _ = _skill(_done_status(), apify=apify, media=media)

    first = skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    second = skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    assert len(apify.calls) == 1  # 2 回目の status では Apify を呼ばない
    assert first.counts["videos"] == 3 and second.counts["videos"] == 3
    assert second.counts["videos_apify"] == 2  # S3 の既存を再利用
    assert "p02002:APIFY_FALLBACK_SKIPPED:ATTEMPTED" in second.warnings
    assert "p02002:APIFY_FALLBACK_MISSING" not in second.warnings
    assert [row["acquired_via"] for row in second.videos] == ["worker", "apify", "apify", None]


def test_async_poll_context_never_triggers_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    apify = _FakeApify({url: _MP4 for url in _URL.values()})
    media = _FakeMediaClient(_request())
    skill, _ = _skill(_done_status(), apify=apify, media=media)
    poll_ctx = SkillContext(
        request_id="req-test",
        user_id="U123",
        metadata={"user_email": _EMAIL, ASYNC_JOB_POLL_METADATA_KEY: True},
    )
    out = skill.run(TikTokAcquireStatusInput(job_id=_JOB), poll_ctx)
    assert apify.calls == []  # 見張り（30秒ポーリング）経路では課金を伴う補完を発火させない
    assert media.staged == {}
    assert all("acquired_via" not in row for row in out.videos)
    assert out.counts["videos"] == 1


def test_concurrent_apply_for_same_job_is_skipped_while_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    apify = _FakeApify({url: _MP4 for url in _URL.values()})
    media = _FakeMediaClient(_request())
    skill, _ = _skill(_done_status(), apify=apify, media=media)
    lock = _job_lock(_JOB)
    assert lock.acquire(blocking=False)  # 別スレッドの補完が走行中、を再現
    try:
        out = skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    finally:
        lock.release()
    assert apify.calls == []
    assert "APIFY_FALLBACK_SKIPPED:IN_PROGRESS" in out.warnings
    assert out.counts["videos"] == 1
    # 解放後は通常どおり補完できる（lock が漏れていない）
    out2 = skill.run(TikTokAcquireStatusInput(job_id=_JOB), _ctx())
    assert len(apify.calls) == 1 and out2.counts["videos"] == 4
