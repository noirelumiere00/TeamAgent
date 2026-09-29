"""サムネ（一覧の表紙）の読み取りの並列・締め切り・失敗（cover_read.CoverReader）。

フェイクは本番の失敗の形を再現する:
- 取得: 署名 URL の失効 403 で media worker が返す MediaJobError("MEDIA_THUMBNAIL_FETCH_FAILED")・
  空の bytes
- Gemini: 例外（ResourceExhausted を使い切った形）・JSON の崩れ・止まる（Event で待つ）
止まるフェイクは finally で必ず Event を解く（プールの作業スレッドは Python の終了時に join される）。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gemini_client import GeminiResponse
from teamagent.adapters.media_job import MediaJobError
from teamagent.skills.video_algorithm.cover_read import (
    COVER_USER_PROMPT,
    CoverImage,
    CoverReader,
    CoverSettings,
    image_of,
)
from teamagent.skills.video_algorithm.schema import VideoMeta
from tests.skills.video_algorithm.prod_shape import jpeg

QUERY = "スパイスカレー 作り方"
CAPTION = "スパイスカレーの作り方をまとめました"
_JSON = json.dumps(
    {
        "elements": ["result"],
        "subject_note": "皿のカレー",
        "texts": [{"text": "10分で本格", "box_2d": [100, 50, 250, 950]}],
        "face": {"kind": "none"},
    },
    ensure_ascii=False,
)


def _resp(text: str = _JSON, cost: Any = 0.01) -> GeminiResponse:
    return GeminiResponse(
        text=text,
        input_tokens=2700,
        output_tokens=700,
        cost_usd=cost,
        model_id="gemini-3.5-flash",
        latency_ms=10,
    )


class _Gemini:
    """analyze_image_bytes だけの Gemini（呼び出しを記録）。"""

    model_id = "gemini-3.5-flash"

    def __init__(self, reply: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply = reply or (lambda call: _resp())
        self.lock = threading.Lock()

    def analyze_image_bytes(self, **kwargs: Any) -> GeminiResponse:
        with self.lock:
            self.calls.append(kwargs)
        return self.reply(kwargs)


def _metas(n: int = 3, **kw: Any) -> list[VideoMeta]:
    return [
        VideoMeta(
            rank=i,
            url=f"https://t/{i}",
            desc=CAPTION,
            cover_url=f"https://p16.example/c{i}.jpg",
            duration_sec=30.0,
            **kw,
        )
        for i in range(1, n + 1)
    ]


def _reader(gemini: Any, fetch: Any, **settings: Any) -> CoverReader:
    return CoverReader(
        gemini=gemini,
        system="SYSTEM",
        fetch=fetch,
        request_id="req",
        settings=CoverSettings(**{"wait_s": 5.0, **settings}),
        version="v1-test",
    )


def _ok_fetch(meta: VideoMeta) -> CoverImage:
    return image_of(jpeg(540, 960), via="injected")


def test_one_403_fails_only_that_cover() -> None:
    """壊し方: 1 枚ごとの except を外す → 例外がプールに消えて timeout になり赤。"""

    def fetch(meta: VideoMeta) -> CoverImage:
        if meta.rank == 2:
            raise MediaJobError("MEDIA_THUMBNAIL_FETCH_FAILED")
        if meta.rank == 3:
            return CoverImage(data=b"", mime="image/jpeg", via="media")
        return _ok_fetch(meta)

    gemini = _Gemini()
    reader = _reader(gemini, fetch)
    reader.submit(_metas(3), "top")
    reads, cost = reader.join(5.0)
    assert reads[1].status == "ok" and reads[1].img_w == 540 and reads[1].via == "injected"
    assert reads[2].status == "fetch_failed" and reads[2].reason == "MEDIA_THUMBNAIL_FETCH_FAILED"
    assert reads[3].status == "fetch_failed" and reads[3].reason == "EMPTY_COVER"
    assert len(gemini.calls) == 1 and cost == pytest.approx(0.01)


def test_gemini_exception_is_read_failed_and_json_is_retried_once() -> None:
    images = {1: jpeg(540, 960), 2: jpeg(540, 961), 3: jpeg(540, 962)}
    rank_of = {data: rank for rank, data in images.items()}
    attempts: dict[int, int] = {}

    def reply(call: dict[str, Any]) -> GeminiResponse:
        rank = rank_of[call["data"]]
        attempts[rank] = attempts.get(rank, 0) + 1
        if rank == 1:
            raise RuntimeError("Gemini 動画分析に失敗しました: ResourceExhausted")
        if rank == 2:
            return _resp('{"elements": ["result"], "texts": [')  # 途中で切れる
        # 1 回目だけ崩れる → やり直しで読める
        return _resp("所見のみ") if attempts[rank] == 1 else _resp()

    gemini = _Gemini(reply)
    reader = _reader(gemini, lambda m: image_of(images[m.rank], via="injected"))
    reader.submit(_metas(3), "top")
    reads, cost = reader.join(5.0)
    assert reads[1].status == "read_failed" and reads[1].reason.startswith("gemini:")
    assert reads[2].status == "read_failed" and reads[2].reason == "json"
    assert reads[3].status == "ok"
    assert attempts == {1: 1, 2: 2, 3: 2}
    assert cost == pytest.approx(0.04)  # 返ってきた 4 回分（例外の 1 回は課金なし）


def test_stuck_cover_times_out_and_join_returns_quickly() -> None:
    """壊し方: join でプールを shutdown(wait=True)（with の形）にする → 止まる 1 枚を待って赤。"""
    release = threading.Event()

    images = {1: jpeg(540, 960), 2: jpeg(540, 961)}

    def reply(call: dict[str, Any]) -> GeminiResponse:
        if call["data"] == images[2]:
            release.wait(30)
        return _resp()

    reader = _reader(_Gemini(reply), lambda m: image_of(images[m.rank], via="injected"))
    try:
        reader.submit(_metas(2), "top")
        started = time.monotonic()
        reads, _cost = reader.join(1.0)
        assert time.monotonic() - started < 3.0
        assert reads[1].status == "ok"
        assert reads[2].status == "timeout" and reads[2].reason == "deadline"
    finally:
        release.set()
    time.sleep(0.2)
    assert reader.abandoned_cost() == pytest.approx(0.01)  # 締め切りの後に返った分はログだけ


def test_only_the_image_goes_to_gemini() -> None:
    """user プロンプトに検索 KW もキャプションも入れない（画像に無い文字を読ませない）。

    壊し方: user プロンプトに検索 KW を足す → 赤。
    """
    gemini = _Gemini()
    reader = _reader(gemini, _ok_fetch)
    reader.submit(_metas(1), "top")
    reader.join(5.0)
    call = gemini.calls[0]
    assert call["prompt"] == COVER_USER_PROMPT
    for word in ("スパイスカレー", "作り方", CAPTION, "順位"):
        assert word not in call["prompt"]
    assert call["system"] == "SYSTEM" and call["mime_type"] == "image/jpeg"
    assert call["json_mode"] is True and call["media_resolution"] == "high"
    assert call["thinking_level"] == "low" and call["timeout_s"] == 30.0


def test_image_posts_and_missing_urls_are_not_read_and_ranks_dedupe() -> None:
    fetched: list[int] = []

    def fetch(meta: VideoMeta) -> CoverImage:
        fetched.append(meta.rank)
        return _ok_fetch(meta)

    metas = _metas(3)
    metas[0].duration_sec = 0.0  # 画像投稿
    metas[1].cover_url = None  # acquire_job_id の経路
    reader = _reader(_Gemini(), fetch)
    reader.submit(metas, "top")
    reader.submit(metas, "rest")  # 同じ順位は 2 回読まない
    reads, _cost = reader.join(5.0)
    assert reads[1].status == "skipped" and reads[2].status == "no_cover"
    assert reads[3].status == "ok" and reads[3].group == "top"
    assert fetched == [3]


def test_all_zero_durations_are_read() -> None:
    """尺が全部 0（取得の経路が尺を返さない）なら画像投稿として除かない（分析ゼロに落とさない）。"""
    metas = _metas(2)
    for m in metas:
        m.duration_sec = 0.0
    reader = _reader(_Gemini(), _ok_fetch)
    reader.submit(metas, "top", all_zero=True)
    reads, _cost = reader.join(5.0)
    assert {r.status for r in reads.values()} == {"ok"}


def test_magicmock_cost_is_not_added() -> None:
    """壊し方: float(resp.cost_usd) で足す → MagicMock の 1.0 が入って赤。"""
    gemini = MagicMock()
    gemini.analyze_image_bytes.return_value = MagicMock()
    reader = _reader(gemini, _ok_fetch)
    reader.submit(_metas(2), "top")
    reads, cost = reader.join(5.0)
    assert cost == 0.0
    assert {r.status for r in reads.values()} == {"read_failed"}


def test_image_for_waits_for_the_single_fetch() -> None:
    """表示用の画像は読み取りの取得を待って使う（1 本 1 回の取得）。"""
    fetched: list[int] = []
    gate = threading.Event()

    def fetch(meta: VideoMeta) -> CoverImage:
        gate.wait(5)
        fetched.append(meta.rank)
        return _ok_fetch(meta)

    reader = _reader(_Gemini(), fetch)
    try:
        reader.submit(_metas(1), "top")
        threading.Timer(0.2, gate.set).start()
        image = reader.image_for(1, timeout_s=3.0)
        assert image is not None and image.width == 540
        assert reader.image_for(9, timeout_s=0.1) is None  # 投入していない順位
        reader.join(5.0)
    finally:
        gate.set()
    assert fetched == [1]


def test_remaining_budget_counts_from_run_start() -> None:
    clock = [100.0]
    reader = CoverReader(
        gemini=_Gemini(),
        system="S",
        fetch=_ok_fetch,
        request_id="r",
        settings=CoverSettings(wait_s=45, budget_s=240),
        version="v",
        clock=lambda: clock[0],
    )
    try:
        assert reader.remaining_budget_s(run_started=90.0) == 45.0
        clock[0] = 300.0
        assert reader.remaining_budget_s(run_started=90.0) == pytest.approx(30.0)
        clock[0] = 400.0
        assert reader.remaining_budget_s(run_started=90.0) == 0.0
    finally:
        reader.join(0.0)


@pytest.mark.parametrize(
    ("env", "enabled", "board", "thinking"),
    [
        ({}, True, False, "low"),
        ({"VIDEO_ALGO_COVER_READ": "0"}, False, False, "low"),
        ({"VIDEO_ALGO_COVER_READ": "off", "VIDEO_ALGO_COVER_BOARD": "1"}, False, True, "low"),
        ({"VIDEO_ALGO_COVER_BOARD": "yes", "VIDEO_ALGO_COVER_THINKING": ""}, True, True, None),
    ],
)
def test_settings_from_env(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    enabled: bool,
    board: bool,
    thinking: str | None,
) -> None:
    for name in (
        "VIDEO_ALGO_COVER_READ",
        "VIDEO_ALGO_COVER_BOARD",
        "VIDEO_ALGO_COVER_THINKING",
        "VIDEO_ALGO_COVER_WORKERS",
    ):
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = CoverSettings.from_env()
    assert (s.enabled, s.board, s.thinking) == (enabled, board, thinking)
    assert s.mode == ("off" if not enabled else "board" if board else "top")
    monkeypatch.setenv("VIDEO_ALGO_COVER_WORKERS", "99")
    assert CoverSettings.from_env().workers == 8


# ── Gemini の後の例外・締め切りの後の課金（09-29 反証）──────────────────────


class _RaisingText:
    """text を読むと例外になる応答（Gemini の後の想定外の失敗の形）。"""

    cost_usd = 0.01

    @property
    def text(self) -> str:
        raise RuntimeError("SDK_RESPONSE_BROKEN")


def test_error_after_gemini_fails_only_that_cover_without_waiting() -> None:
    """Gemini の後（parse・結果の書き込み）の例外も、その 1 枚の read_failed（締め切りまで待たない）。

    壊し方: _run_read の外側の except を外す → 例外がプールに消え、締め切りまで待って timeout で赤。
    """
    gemini = _Gemini(lambda call: _RaisingText())
    reader = _reader(gemini, _ok_fetch)
    reader.submit(_metas(1), "top")
    started = time.monotonic()
    reads, cost = reader.join(3.0)
    assert time.monotonic() - started < 2.0
    assert reads[1].status == "read_failed" and reads[1].reason == "parse:RuntimeError"
    assert cost == pytest.approx(0.01)


def test_huge_box_numbers_do_not_break_the_parse() -> None:
    """box_2d の桁の大きな整数（float にできない）は枠なしにする（1 枚を落とさない・例外にしない）。"""
    big = "1" + "0" * 400
    text = (
        '{"elements":["result"],"texts":[{"text":"10分で本格","box_2d":['
        + big
        + ',0,1,1]}],"face":{"kind":"none"}}'
    )
    reader = _reader(_Gemini(lambda call: _resp(text)), _ok_fetch)
    reader.submit(_metas(1), "top")
    reads, _cost = reader.join(3.0)
    assert reads[1].status == "ok"
    assert reads[1].texts is not None and reads[1].texts[0].box is None
    deep = '{"elements":' + "[" * 60000 + "]" * 60000 + ',"texts":[],"face":null}'
    from teamagent.skills.video_algorithm.cover_read import parse_cover

    assert parse_cover(deep) is None  # 深い入れ子（RecursionError）も None
    # 3.14 の json は深い入れ子も読めてしまう。どの版でも json が読める 20 段で、自前の上限を確かめる
    mid = '{"elements":' + "[" * 20 + "]" * 20 + ',"texts":[],"face":null}'
    assert parse_cover(mid) is None
    ok4 = '{"elements":["result"],"texts":[{"text":"a","box_2d":[1,2,3,4]}],"face":{"kind":"none"}}'
    assert parse_cover(ok4) is not None  # 正しい形（4 段）は読む


def test_no_gemini_retry_after_the_deadline_and_abandoned_cost_is_logged() -> None:
    """締め切りの後に崩れた JSON が返っても、やり直しで Gemini をもう 1 回呼ばない。後の費用はログに出す。

    壊し方: やり直しの前に締め切りを見ない → 2 回目の呼び出しが起きて赤。
    """
    from structlog.testing import capture_logs

    release = threading.Event()
    entered = threading.Event()

    def reply(call: dict[str, Any]) -> GeminiResponse:
        entered.set()
        release.wait(10)
        return _resp("所見のみ")  # 崩れた JSON（本来はやり直す形）

    gemini = _Gemini(reply)
    reader = _reader(gemini, _ok_fetch)
    with capture_logs() as logs:
        try:
            reader.submit(_metas(1), "top")
            assert entered.wait(3)
            reads, cost = reader.join(0.2)
            assert reads[1].status == "timeout" and cost == 0.0
        finally:
            release.set()
        time.sleep(0.3)
    assert len(gemini.calls) == 1
    assert reader.abandoned_cost() == pytest.approx(0.01)
    abandoned = [e for e in logs if e.get("event") == "video_algorithm_cover_abandoned_cost"]
    assert abandoned and abandoned[0]["rank"] == 1 and abandoned[0]["cost_usd"] == 0.01


def test_reads_waiting_at_the_deadline_are_cancelled() -> None:
    """締め切りの時点で読み取りを待っていた表紙は取り消す（後から Gemini を呼ばない・読み取りに入らない）。

    壊し方: read プールを shutdown(cancel_futures=False) にする → 待っていた 1 枚が後から走って赤。
    """
    release = threading.Event()
    entered = threading.Event()
    started_reads: list[int] = []

    class _Spy(CoverReader):
        def _run_read(self, job: Any) -> None:
            started_reads.append(job.rank)
            super()._run_read(job)

    def reply(call: dict[str, Any]) -> GeminiResponse:
        entered.set()
        release.wait(10)
        return _resp()

    reader = _Spy(
        gemini=_Gemini(reply),
        system="SYSTEM",
        fetch=_ok_fetch,
        request_id="req",
        settings=CoverSettings(wait_s=5.0, workers=1),
        version="v1-test",
    )
    try:
        reader.submit(_metas(2), "top")
        assert entered.wait(3)
        deadline = time.monotonic() + 3
        while len(reader.ranks()) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)  # 2 本目の取得が終わり、読み取りのプールで待つまで
        reads, _cost = reader.join(0.1)
        assert {r.status for r in reads.values()} == {"timeout"}
    finally:
        release.set()
    time.sleep(0.3)
    assert started_reads == [1]
