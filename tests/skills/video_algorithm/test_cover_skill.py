"""skill.run のサムネ（一覧の表紙）の読み取りの配線（取得の注入・投入・待ち合わせ・費用・キャッシュ）。

- 取得は 1 本 1 回（表示と色も同じ取得を使う・thumbnails.fetch_cover を別に呼ばない）
- 置き場所は上位ボードの各行だけ（videos[].meta には載せない）
- quota で止まる依頼では表紙を取らない（1 波目の予約の後に始める）
- 止めている設定では従来のキャッシュのキー・読み取りなし
壊し方（→ 赤）は各テストの docstring。
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from teamagent.adapters.gemini_client import GeminiResponse
from teamagent.adapters.media_job import MediaJobError
from teamagent.adapters.video_algorithm_cache import VideoAlgorithmResultCache
from teamagent.skills.base import SkillContext
from teamagent.skills.video_algorithm.cover_read import CoverSettings, cover_version
from teamagent.skills.video_algorithm.schema import VideoAlgorithmInput, VideoMeta
from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill
from tests.skills.video_algorithm.prod_shape import jpeg
from tests.skills.video_algorithm.test_video_algorithm import _json_block

_COVER = {
    "elements": ["result"],
    "subject_note": "皿の料理",
    "texts": [{"text": "新宿ランチ\n3選", "box_2d": [100, 50, 300, 950]}],
    "face": {"kind": "none"},
    "closeup": True,
    "sizzle": ["steam"],
    "product": "none",
    "clutter": "simple",
    "legibility": "good",
    "appeals": ["ranking"],
}


def _resp(text: str, cost: float) -> GeminiResponse:
    return GeminiResponse(
        text=text,
        input_tokens=100,
        output_tokens=100,
        cost_usd=cost,
        model_id="gemini-3.5-flash",
        latency_ms=1,
    )


class _Gemini:
    model_id = "gemini-3.5-flash"

    def __init__(self) -> None:
        self.video_calls = 0
        self.image_calls: list[bytes] = []
        self.lock = threading.Lock()

    def analyze_video_bytes(self, **kwargs: Any) -> GeminiResponse:
        with self.lock:
            self.video_calls += 1
        return _resp(_json_block(kw_telop=True, cta=True, brand=False, dur=18), 0.0014)

    def analyze_image_bytes(self, **kwargs: Any) -> GeminiResponse:
        with self.lock:
            self.image_calls.append(kwargs["data"])
        return _resp(json.dumps(_COVER, ensure_ascii=False), 0.01)

    def generate_text(self, *args: Any, **kwargs: Any) -> GeminiResponse:
        return _resp("```json\n{}\n```", 0.002)


def _metas(n: int = 6) -> list[VideoMeta]:
    return [
        VideoMeta(
            rank=i,
            url=f"https://t/{i}",
            desc="新宿 ランチ",
            play_count=100000 - i,
            collect_count=1500,
            duration_sec=20.0,
            cover_url=f"https://p16.example/{i}.jpg",
        )
        for i in range(1, n + 1)
    ]


class _Fetcher:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.fail = fail or set()
        self.lock = threading.Lock()

    def __call__(self, url: str) -> bytes:
        with self.lock:
            self.calls.append(url)
        if url in self.fail:
            raise MediaJobError("MEDIA_THUMBNAIL_FETCH_FAILED")
        return jpeg(540, 960)


def _skill(
    tmp_path: Any, gemini: _Gemini, fetcher: _Fetcher | None, **settings: Any
) -> VideoAlgorithmSkill:
    metas = _metas()
    return VideoAlgorithmSkill(
        gemini=gemini,  # type: ignore[arg-type]
        searcher=lambda q, n, r: metas[:n],
        downloader=lambda url: (b"vid", "video/mp4"),
        proxy=lambda d, m: (d, m),
        report_dir=str(tmp_path),
        cover_fetcher=fetcher,
        cover_settings=CoverSettings(**{"wait_s": 5.0, **settings}),
    )


@pytest.fixture(autouse=True)
def _local_media(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")


def _input(**kw: Any) -> VideoAlgorithmInput:
    return VideoAlgorithmInput(query="新宿 ランチ", max_videos=3, board_size=6, **kw)


def test_run_reads_top_covers_once_and_keeps_them_on_the_board_only(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """壊し方: _build_thumb で prefetched を使わない → fetch_cover が別に呼ばれて赤。"""
    from teamagent.skills.video_algorithm import thumbnails

    extra: list[str] = []
    monkeypatch.setattr(thumbnails, "fetch_cover", lambda url, **k: extra.append(str(url)) or None)
    gemini, fetcher = _Gemini(), _Fetcher()
    out = _skill(tmp_path, gemini, fetcher).run(_input(), SkillContext())
    assert out.cover_read_mode == "top"
    assert sorted(fetcher.calls) == [f"https://p16.example/{i}.jpg" for i in (1, 2, 3)]
    assert extra == []  # 表示用に取り直さない（1 本 1 回）
    assert gemini.video_calls == 3 and len(gemini.image_calls) == 3
    reads = {m.rank: m.cover_read for m in out.board}
    assert {r: (x.status if x else None) for r, x in reads.items()} == {
        1: "ok",
        2: "ok",
        3: "ok",
        4: None,
        5: None,
        6: None,
    }
    assert all(v.meta.cover_read is None for v in out.videos)  # 置き場所はボードだけ
    dumped = out.model_dump(mode="json")
    assert all("cover_read" not in v["meta"] for v in dumped["videos"])
    assert all(v.cover_source == "cover" and v.cover_data_uri for v in out.videos)
    # 動画 3 本（0.0014）＋表紙 3 枚（0.01）＋統合（0.002）
    assert out.total_cost_usd == pytest.approx(3 * 0.0014 + 3 * 0.01 + 0.002, abs=1e-6)
    assert "サムネ（一覧の表紙）" in out.slack_summary


def test_failed_fetch_falls_back_without_fetching_again(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """403 の 1 枚は fetch_failed。表示は取り直さず（コマで代用か無し）、ほかは読める。"""
    from teamagent.skills.video_algorithm import thumbnails

    extra: list[str] = []
    monkeypatch.setattr(thumbnails, "fetch_cover", lambda url, **k: extra.append(str(url)) or None)
    fetcher = _Fetcher(fail={"https://p16.example/2.jpg"})
    out = _skill(tmp_path, _Gemini(), fetcher).run(_input(), SkillContext())
    two = next(m for m in out.board if m.rank == 2).cover_read
    assert two is not None and two.status == "fetch_failed"
    assert two.reason == "MEDIA_THUMBNAIL_FETCH_FAILED"
    assert extra == []
    video2 = next(v for v in out.videos if v.meta.rank == 2)
    assert video2.analysis is not None and video2.cover_source != "cover"


def test_quota_block_on_the_first_wave_fetches_no_cover(tmp_path: Any) -> None:
    """quota で止まる依頼では表紙を取らない（課金しない）。

    壊し方: 読み取りの投入を quota の予約の前に移す → 取得が呼ばれて赤。
    """

    class _Blocked(VideoAlgorithmSkill):
        @staticmethod
        def _reserve_quota(ctx: SkillContext, count: int, *, allow_partial: bool) -> int:
            raise RuntimeError("VIDEO_QUOTA_EXCEEDED")

    metas = _metas()
    fetcher = _Fetcher()
    skill = _Blocked(
        gemini=_Gemini(),  # type: ignore[arg-type]
        searcher=lambda q, n, r: metas[:n],
        downloader=lambda url: (b"vid", "video/mp4"),
        proxy=lambda d, m: (d, m),
        report_dir=str(tmp_path),
        cover_fetcher=fetcher,
        cover_settings=CoverSettings(),
    )
    with pytest.raises(RuntimeError, match="VIDEO_QUOTA_EXCEEDED"):
        skill.run(_input(), SkillContext())
    assert fetcher.calls == []


def test_off_reads_nothing_and_keeps_the_old_cache_key(tmp_path: Any) -> None:
    """壊し方: 止めているときも cover_version をキーに入れる → 以前のキーと変わって赤。"""
    gemini, fetcher = _Gemini(), _Fetcher()
    out = _skill(tmp_path, gemini, fetcher, enabled=False).run(_input(), SkillContext())
    assert out.cover_read_mode == "off" and fetcher.calls == [] and gemini.image_calls == []
    assert all(m.cover_read is None for m in out.board)
    args: dict[str, Any] = {
        "query": "q",
        "max_videos": 5,
        "prompt_version": "v2",
        "model_id": "gemini-3.5-flash",
        "board_size": 30,
        "outputs": ["report"],
        "kw_set": None,
        "synthesis_version": "v3",
    }
    old = VideoAlgorithmResultCache.cache_key(**args)
    off = cover_version(CoverSettings(enabled=False), "gemini-3.5-flash")
    assert (
        off == "" and VideoAlgorithmResultCache.cache_key(**args, cover_version=off or None) == old
    )
    on = cover_version(CoverSettings(), "gemini-3.5-flash")
    board = cover_version(CoverSettings(board=True), "gemini-3.5-flash")
    assert on and board == f"{on}+board"
    assert VideoAlgorithmResultCache.cache_key(**args, cover_version=on) != old
    assert cover_version(CoverSettings(thinking=None), "gemini-3.5-flash") != on
    assert cover_version(CoverSettings(), "gemini-3.6-flash") != on


def test_board_flag_reads_the_rest_group(tmp_path: Any) -> None:
    out = _skill(tmp_path, _Gemini(), _Fetcher(), board=True).run(_input(), SkillContext())
    assert out.cover_read_mode == "board"
    groups = {m.rank: m.cover_read.group for m in out.board if m.cover_read is not None}
    assert groups == {1: "top", 2: "top", 3: "top", 4: "rest", 5: "rest", 6: "rest"}


def test_gemini_client_is_created_on_the_main_thread(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2 つのプールから同時に遅延生成されないよう、表紙の読み取りの前に main で作る。"""
    from teamagent.adapters import gemini_client

    made: list[str] = []
    gemini = _Gemini()

    def from_env() -> Any:
        made.append(threading.current_thread().name)
        return gemini

    monkeypatch.setattr(gemini_client.GeminiClient, "from_env", staticmethod(from_env))
    metas = _metas()
    skill = VideoAlgorithmSkill(
        searcher=lambda q, n, r: metas[:n],
        downloader=lambda url: (b"vid", "video/mp4"),
        proxy=lambda d, m: (d, m),
        report_dir=str(tmp_path),
        cover_fetcher=_Fetcher(),
        cover_settings=CoverSettings(),
    )
    skill.run(_input(), SkillContext())
    assert made == ["MainThread"]


def test_wave_failure_stops_the_cover_reads(tmp_path: Any) -> None:
    """動画の波が例外で止まったら、表紙の読み取りも片付ける（待ちの取得を取り消し、裏で課金しない）。

    壊し方: 例外のときの reader.join(0.0) を外す → 残りの取得が裏で進んで赤。
    """
    gate = threading.Event()
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        gate.wait(5)
        return jpeg(540, 960)

    class _Boom(VideoAlgorithmSkill):
        def _analyze_one(self, meta: VideoMeta, **kwargs: Any) -> Any:  # type: ignore[override]
            raise RuntimeError("MEDIA_FRAME_JOB_FAILED")

    metas = _metas()
    skill = _Boom(
        gemini=_Gemini(),  # type: ignore[arg-type]
        searcher=lambda q, n, r: metas[:n],
        downloader=lambda url: (b"vid", "video/mp4"),
        proxy=lambda d, m: (d, m),
        report_dir=str(tmp_path),
        cover_fetcher=fetch,
        cover_settings=CoverSettings(fetch_workers=1),
    )
    try:
        with pytest.raises(RuntimeError, match="MEDIA_FRAME_JOB_FAILED"):
            skill.run(_input(), SkillContext())
    finally:
        gate.set()
    import time

    time.sleep(0.3)
    assert len(calls) == 1  # 待っていた 2 本は取り消した
