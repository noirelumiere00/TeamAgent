"""video_algorithm の二段構え: DL経路の失敗（video_algorithm_fetch_failed）を mcp 側 Apify で補完。

opt-in ON なら失敗分を Apify（フェイク）で取り直して動画ベースの分析に戻り、OFF / Apify 失敗なら
従来どおり cover-only 縮退に落ちる（Apify には触れない）ことを固定する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.apify_client import ApifyError, TikTokVideoBytes
from teamagent.adapters.gemini_client import GeminiResponse
from teamagent.adapters.tiktok_video_fallback import ENV_FLAG
from teamagent.skills.base import SkillContext
from teamagent.skills.video_algorithm.schema import VideoAlgorithmInput, VideoMeta
from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill

_MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40
_URL = "https://www.tiktok.com/@a/video/7000000000000000001"


@pytest.fixture(autouse=True)
def _local_media_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")


def _json_block() -> str:
    return (
        "### 所見\n\n```json\n"
        '{"duration_sec":18,"hook_type":"question","hook_summary":"問いかけ",'
        '"telop_density":"heavy","telops":[{"sec":1.0,"text":"新宿","position":"center","kw_match":true}],'
        '"main_objects":["寿司"],"brand_detections":[],'
        '"scenes":[{"start_sec":0,"end_sec":3,"desc":"導入"}],"pacing":"fast",'
        '"main_message":"新宿の名店","cta_type":["save"],"cta_sec":18.0,'
        '"keyword_matches":[{"keyword":"新宿","matched":true,"match_type":"exact","layer":"telop"}],'
        '"caption_relevance":"一致","win_factors":["冒頭フック強"]}\n```'
    )


def _gemini() -> MagicMock:
    gemini = MagicMock()
    gemini.analyze_video_bytes.return_value = GeminiResponse(
        text=_json_block(),
        input_tokens=6000,
        output_tokens=400,
        cost_usd=0.0014,
        model_id="gemini-2.5-flash",
        latency_ms=18000,
    )
    return gemini


class _FakeApify:
    def __init__(self, bodies: dict[str, bytes], *, raise_exc: Exception | None = None) -> None:
        self.bodies = bodies
        self.raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    def tiktok_download_videos(self, post_urls: list[str], *, max_videos: int, **kw: Any):
        self.calls.append({"urls": list(post_urls), "max_videos": max_videos, **kw})
        if self.raise_exc is not None:
            raise self.raise_exc
        got = [
            TikTokVideoBytes(
                post_url=u, video_id=u.rsplit("/", 1)[1], kvs_key="video-x.mp4", body=b
            )
            for u in post_urls[:max_videos]
            if (b := self.bodies.get(u)) is not None
        ]
        return got, 0.004 * len(post_urls[:max_videos])


def _boom(url: str) -> tuple[bytes, str]:
    raise RuntimeError("MEDIA_ACQUIRE_FAILED")


def _skill(tmp_path: object, apify: _FakeApify, gemini: MagicMock) -> VideoAlgorithmSkill:
    metas = [VideoMeta(rank=1, url=_URL, play_count=1000, cover_url="https://cdn/1.jpg")]
    return VideoAlgorithmSkill(
        gemini=gemini,
        searcher=lambda q, n, r: metas,
        downloader=_boom,
        proxy=lambda d, m: (d, m),
        report_dir=str(tmp_path),
        apify_fallback=apify,
    )


def test_fetch_failure_is_recovered_via_apify_when_opted_in(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    apify = _FakeApify({_URL: _MP4})
    gemini = _gemini()
    out = _skill(tmp_path, apify, gemini).run(
        VideoAlgorithmInput(query="新宿 ランチ", max_videos=1),
        ctx=SkillContext(metadata={"user_email": "a@vectorinc.co.jp"}),
    )
    video = out.videos[0]
    assert video.analysis is not None
    assert video.error is None  # cover-only 縮退（"動画取得失敗・サムネのみ軽量分析"）ではない
    assert video.acquired_via == "apify"  # 出所の明示
    call = gemini.analyze_video_bytes.call_args
    assert call.kwargs["mime_type"] == "video/mp4"  # 動画ベースの分析に戻っている
    assert call.kwargs["data"] == _MP4
    assert apify.calls[0]["urls"] == [_URL]
    assert apify.calls[0]["max_videos"] == 1
    assert apify.calls[0]["user_email"] == "a@vectorinc.co.jp"


def test_fetch_failure_keeps_cover_only_when_flag_off(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from teamagent.skills.video_algorithm import thumbnails

    monkeypatch.delenv(ENV_FLAG, raising=False)
    monkeypatch.setattr(thumbnails, "fetch_cover", lambda *a, **k: b"\xff\xd8\xff\xe0jpegdata")
    apify = _FakeApify({_URL: _MP4})
    gemini = _gemini()
    out = _skill(tmp_path, apify, gemini).run(
        VideoAlgorithmInput(query="新宿 ランチ", max_videos=1), ctx=SkillContext()
    )
    assert apify.calls == []  # 既定 OFF: Apify には一切触れない
    assert out.videos[0].error == "動画取得失敗・サムネのみ軽量分析"
    assert out.videos[0].acquired_via == ""
    assert gemini.analyze_video_bytes.call_args.kwargs["mime_type"] == "image/jpeg"


def test_apify_failure_degrades_to_cover_only(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from teamagent.skills.video_algorithm import thumbnails

    monkeypatch.setenv(ENV_FLAG, "1")
    monkeypatch.setattr(thumbnails, "fetch_cover", lambda *a, **k: b"\xff\xd8\xff\xe0jpegdata")
    apify = _FakeApify({}, raise_exc=ApifyError("APIFY_TIMEOUT: 期限内に完了しませんでした"))
    gemini = _gemini()
    out = _skill(tmp_path, apify, gemini).run(
        VideoAlgorithmInput(query="新宿 ランチ", max_videos=1), ctx=SkillContext()
    )
    assert apify.calls  # 試みてはいる
    assert out.videos[0].error == "動画取得失敗・サムネのみ軽量分析"  # fail-open で従来縮退
    assert out.videos[0].acquired_via == ""


def test_apify_zero_result_degrades_to_cover_only(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from teamagent.skills.video_algorithm import thumbnails

    monkeypatch.setenv(ENV_FLAG, "1")
    monkeypatch.setattr(thumbnails, "fetch_cover", lambda *a, **k: b"\xff\xd8\xff\xe0jpegdata")
    out = _skill(tmp_path, _FakeApify({}), _gemini()).run(
        VideoAlgorithmInput(query="新宿 ランチ", max_videos=1), ctx=SkillContext()
    )
    assert out.videos[0].error == "動画取得失敗・サムネのみ軽量分析"


# ---------------------------------------------------------------------------
# request 単位の予算: 集約本数上限（TIKTOK_APIFY_FALLBACK_MAX_VIDEOS）と壁時計
# ---------------------------------------------------------------------------

_URL2 = "https://www.tiktok.com/@a/video/7000000000000000002"


def _two_video_skill(tmp_path: object, apify: _FakeApify, gemini: MagicMock) -> VideoAlgorithmSkill:
    metas = [
        VideoMeta(rank=1, url=_URL, play_count=1000, cover_url="https://cdn/1.jpg"),
        VideoMeta(rank=2, url=_URL2, play_count=900, cover_url="https://cdn/2.jpg"),
    ]
    return VideoAlgorithmSkill(
        gemini=gemini,
        searcher=lambda q, n, r: metas,
        downloader=_boom,
        proxy=lambda d, m: (d, m),
        report_dir=str(tmp_path),
        apify_fallback=apify,
    )


def test_aggregate_cap_bounds_apify_runs_per_request(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from teamagent.skills.video_algorithm import thumbnails

    monkeypatch.setenv(ENV_FLAG, "1")
    monkeypatch.setenv("TIKTOK_APIFY_FALLBACK_MAX_VIDEOS", "1")
    monkeypatch.setattr(thumbnails, "fetch_cover", lambda *a, **k: b"\xff\xd8\xff\xe0jpegdata")
    apify = _FakeApify({_URL: _MP4, _URL2: _MP4})
    out = _two_video_skill(tmp_path, apify, _gemini()).run(
        VideoAlgorithmInput(query="新宿 ランチ", max_videos=2), ctx=SkillContext()
    )
    assert (
        len(apify.calls) == 1
    )  # DL 経路全滅でも request 全体で 1 run（個別 max_videos=1 の抜け道なし）
    via = sorted(video.acquired_via for video in out.videos)
    assert via == ["", "apify"]
    cover_only = next(video for video in out.videos if video.acquired_via == "")
    assert cover_only.error == "動画取得失敗・サムネのみ軽量分析"


def test_wallclock_budget_skips_fallback_and_degrades(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from teamagent.skills.video_algorithm import thumbnails

    monkeypatch.setenv(ENV_FLAG, "1")
    # 残り壁時計（30s）< 1 run に要る予算（既定 150s + 余裕）→ Apify を呼ばず cover-only へ
    monkeypatch.setenv("VIDEO_ALGORITHM_APIFY_WALLCLOCK_S", "30")
    monkeypatch.setattr(thumbnails, "fetch_cover", lambda *a, **k: b"\xff\xd8\xff\xe0jpegdata")
    apify = _FakeApify({_URL: _MP4, _URL2: _MP4})
    out = _two_video_skill(tmp_path, apify, _gemini()).run(
        VideoAlgorithmInput(query="新宿 ランチ", max_videos=2), ctx=SkillContext()
    )
    assert apify.calls == []
    assert all(video.error == "動画取得失敗・サムネのみ軽量分析" for video in out.videos)


def test_budget_object_takes_until_cap_then_refuses() -> None:
    from teamagent.skills.video_algorithm.skill import _ApifyFallbackBudget

    clock = [0.0]
    budget = _ApifyFallbackBudget(max_videos=2, wallclock_s=240, monotonic=lambda: clock[0])
    assert budget.try_take(180) == (True, "")
    assert budget.try_take(180) == (True, "")
    assert budget.try_take(180) == (False, "cap")
    clock[0] = 100.0  # 残り 140s < 180s → 壁時計で拒否（cap より先に判定）
    assert budget.try_take(180) == (False, "wallclock")
    assert budget.remaining_s() == 140.0
