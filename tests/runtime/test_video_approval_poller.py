"""video_approval_poller の単体テスト（注入 callable で Slack/シート無しに検証）。

Phase2 の安全性の肝＝冪等性・初回ベースライン・エラー隔離・永続ストア を分岐ごとに確認。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from teamagent.runtime.video_approval_poller import ProcessedStore, poll_once


@dataclass
class _Ref:
    management_no: str
    has_drive_video: bool = True


def _fakes(refs: list[_Ref]) -> tuple:
    calls: dict[str, list[str]] = {"run": [], "post": []}

    async def _list() -> list[_Ref]:
        return refs

    async def _run(mgmt: str) -> str:
        calls["run"].append(mgmt)
        return f"FB for {mgmt}"

    async def _post(text: str) -> None:
        calls["post"].append(text)

    return _list, _run, _post, calls


# --- ProcessedStore（再起動耐性 / 破損耐性）---
def test_store_roundtrip(tmp_path: Path) -> None:
    p = str(tmp_path / "s.json")
    s = ProcessedStore(p)
    assert not s.seen("E01-01")
    s.mark("E01-01")
    s.save()
    s2 = ProcessedStore(p)  # 再読込（プロセス再起動を模擬）
    assert s2.seen("E01-01") and len(s2) == 1


def test_store_corrupt_file_starts_empty(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text("{not json", encoding="utf-8")
    s = ProcessedStore(str(p))
    assert len(s) == 0  # 破損は空で開始（落ちない）


# --- 初回ベースライン: バックログを一斉投稿しない ---
async def test_baseline_marks_without_processing(tmp_path: Path) -> None:
    refs = [_Ref("E01-01"), _Ref("E01-02")]
    _list, _run, _post, calls = _fakes(refs)
    store = ProcessedStore(str(tmp_path / "s.json"))
    stats = await poll_once(
        list_creatives=_list, run_one=_run, post=_post, store=store, baseline=True
    )
    assert stats["baselined"] == 2 and stats["processed"] == 0
    assert calls["run"] == [] and calls["post"] == []  # 既存バックログを審査も投稿もしない
    assert store.seen("E01-01") and store.seen("E01-02")


# --- 通常処理: 納品済み×未処理のみ ---
async def test_processes_only_new_delivered(tmp_path: Path) -> None:
    refs = [
        _Ref("E01-01", has_drive_video=True),
        _Ref("E01-02", has_drive_video=False),  # 未納品 → 対象外
        _Ref("", has_drive_video=True),  # 管理番号なし → 対象外
    ]
    _list, _run, _post, calls = _fakes(refs)
    store = ProcessedStore(str(tmp_path / "s.json"))
    stats = await poll_once(
        list_creatives=_list, run_one=_run, post=_post, store=store, baseline=False
    )
    assert stats["processed"] == 1 and stats["new"] == 1
    assert calls["run"] == ["E01-01"]
    assert len(calls["post"]) == 1 and "E01-01" in calls["post"][0]


# --- 冪等性: 既処理は二重審査しない ---
async def test_idempotent_skip_already_seen(tmp_path: Path) -> None:
    _list, _run, _post, calls = _fakes([_Ref("E01-01")])
    store = ProcessedStore(str(tmp_path / "s.json"))
    store.mark("E01-01")
    stats = await poll_once(
        list_creatives=_list, run_one=_run, post=_post, store=store, baseline=False
    )
    assert stats["new"] == 0 and stats["processed"] == 0
    assert calls["run"] == []


# --- エラー隔離: 失敗は既読化せず再試行余地を残す ---
async def test_error_isolation_does_not_mark(tmp_path: Path) -> None:
    refs = [_Ref("E01-01"), _Ref("E01-02")]

    async def _list() -> list[_Ref]:
        return refs

    async def _run(mgmt: str) -> str:
        if mgmt == "E01-01":
            raise RuntimeError("Gemini timeout")
        return "ok"

    posted: list[str] = []

    async def _post(text: str) -> None:
        posted.append(text)

    store = ProcessedStore(str(tmp_path / "s.json"))
    stats = await poll_once(
        list_creatives=_list, run_one=_run, post=_post, store=store, baseline=False
    )
    assert stats["processed"] == 1 and stats["errors"] == 1
    assert not store.seen("E01-01")  # 失敗は既読化しない＝次ティックで再試行
    assert store.seen("E01-02")
    assert len(posted) == 1  # 成功した1件のみ投稿（失敗分は投稿しない）


# --- 並行 poll_once でも二重処理しない（負荷テストの敵対シナリオが発見した race の回帰）---
async def test_concurrent_poll_once_no_double_processing(tmp_path: Path) -> None:
    import asyncio

    refs = [_Ref(f"E{i:02d}") for i in range(1, 11)]  # 10件
    run_calls: list[str] = []

    async def _list() -> list[_Ref]:
        return refs

    async def _run(mgmt: str) -> str:
        await asyncio.sleep(0)  # await 境界で並行を誘発
        run_calls.append(mgmt)
        return "ok"

    async def _post(text: str) -> None:
        await asyncio.sleep(0)

    store = ProcessedStore(str(tmp_path / "s.json"))
    # 同一 store に対し 2 つの poll_once を同時実行（claim-before-await が効くか）
    await asyncio.gather(
        poll_once(list_creatives=_list, run_one=_run, post=_post, store=store, baseline=False),
        poll_once(list_creatives=_list, run_one=_run, post=_post, store=store, baseline=False),
    )
    # 各 management_no がちょうど 1 回だけ処理（二重ゼロ）
    assert len(run_calls) == 10
    assert sorted(run_calls) == sorted(r.management_no for r in refs)
    assert len(set(run_calls)) == 10
