"""knowledge_deliver: file_id 抽出 ＋ 検索→DM配信スキルのテスト（adapters はモック）。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.knowledge_deliver.schema import KnowledgeDeliverInput
from teamagent.skills.knowledge_deliver.skill import (
    KnowledgeDeliverSkill,
    extract_drive_file_id,
)
from teamagent.skills.search.schema import SearchHitOut, SearchOutput


# ── file_id 抽出 ────────────────────────────────────────────────
def test_extract_file_id_gdrive_scheme() -> None:
    assert extract_drive_file_id("gdrive://ABC123_-xyz") == "ABC123_-xyz"


def test_extract_file_id_web_link() -> None:
    assert (
        extract_drive_file_id("https://drive.google.com/file/d/FILE_ID_42/view?usp=sharing")
        == "FILE_ID_42"
    )


def test_extract_file_id_query_id() -> None:
    assert extract_drive_file_id("https://drive.google.com/open?id=ZZZ999") == "ZZZ999"


def test_extract_file_id_none() -> None:
    assert extract_drive_file_id(None) is None
    assert extract_drive_file_id("slack://C1/123.456") is None
    assert extract_drive_file_id("") is None


# ── スキル ──────────────────────────────────────────────────────
def _hit(**kw: object) -> SearchHitOut:
    base: dict[str, object] = {"chunk_id": 1, "content": "本文", "score": 0.9}
    base.update(kw)
    return SearchHitOut(**base)  # type: ignore[arg-type]


def _search_mock(
    hits: list[SearchHitOut], *, answer: str = "要約です", cost: float = 0.01
) -> MagicMock:
    m = MagicMock()
    m.run.return_value = SearchOutput(answer=answer, hits=hits, total_cost_usd=cost)
    return m


def _slack_mock(
    *, user_id: str | None = "U1", dm: str | None = "D1", upload: bool = True
) -> MagicMock:
    m = MagicMock()
    m.lookup_user_id_by_email = AsyncMock(return_value=user_id)
    m.open_dm = AsyncMock(return_value=dm)
    m.upload_file = AsyncMock(return_value=upload)
    return m


def _gdrive_mock() -> MagicMock:
    m = MagicMock()
    m.download_file_bytes.return_value = b"%PDF-1.4 fake"
    return m


def _ctx(
    email: str | None = "u@vectorinc.co.jp",
    *,
    channel_id: str | None = None,
    thread_ts: str | None = None,
) -> SkillContext:
    md: dict[str, str] = {}
    if email:
        md["user_email"] = email
    if channel_id:
        md["channel_id"] = channel_id
    if thread_ts:
        md["thread_ts"] = thread_ts
    return SkillContext(metadata=md)


def test_delivers_file_to_dm_happy_path() -> None:
    hits = [
        _hit(
            source_type="gdrive",
            source_uri="gdrive://F1",
            title="アース製薬_提案.pdf",
            doc_type="提案書",
        ),
    ]
    slack = _slack_mock()
    gdrive = _gdrive_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=gdrive)
    out = skill.run(KnowledgeDeliverInput(query="アース製薬の提案資料出して"), _ctx())

    assert out.delivered_count == 1
    assert "DM にお送りしました" in out.note
    assert out.references[0].delivered is True
    # channel 無し → DM 解決 → upload まで通った
    slack.lookup_user_id_by_email.assert_awaited_once()
    slack.open_dm.assert_awaited_once()
    slack.upload_file.assert_awaited_once()
    # 要約が最初の添付の initial_comment に乗る
    assert slack.upload_file.await_args.kwargs.get("initial_comment") == "要約です"


def test_delivers_file_to_channel_thread() -> None:
    hits = [_hit(source_type="gdrive", source_uri="gdrive://F1", title="提案.pdf")]
    slack = _slack_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=_gdrive_mock())
    out = skill.run(
        KnowledgeDeliverInput(query="提案資料出して"),
        _ctx(channel_id="C123", thread_ts="111.222"),
    )
    assert out.delivered_count == 1
    assert "このスレッド" in out.note
    # チャンネル直添付＝DM 解決は使わない
    slack.lookup_user_id_by_email.assert_not_awaited()
    slack.open_dm.assert_not_awaited()
    # そのチャンネル/スレッドに upload
    assert slack.upload_file.await_args.args[0] == "C123"
    assert slack.upload_file.await_args.kwargs.get("thread_ts") == "111.222"


def test_channel_upload_failure_falls_back_to_dm() -> None:
    hits = [_hit(source_type="gdrive", source_uri="gdrive://F1", title="提案.pdf")]
    slack = MagicMock()
    slack.lookup_user_id_by_email = AsyncMock(return_value="U1")
    slack.open_dm = AsyncMock(return_value="D1")
    # 1回目=channel への upload 失敗 / 2回目=DM への upload 成功
    slack.upload_file = AsyncMock(side_effect=[False, True])
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=_gdrive_mock())
    out = skill.run(KnowledgeDeliverInput(query="x"), _ctx(channel_id="C123", thread_ts="1.2"))
    assert out.delivered_count == 1
    assert "DM" in out.note  # channel 失敗 → DM フォールバック
    slack.open_dm.assert_awaited_once()


def test_dedup_same_file_id() -> None:
    hits = [
        _hit(source_type="gdrive", source_uri="gdrive://F1", title="a.pdf"),
        _hit(chunk_id=2, source_type="gdrive", source_uri="gdrive://F1", title="a.pdf"),
    ]
    slack = _slack_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=_gdrive_mock())
    out = skill.run(KnowledgeDeliverInput(query="a"), _ctx())
    assert out.delivered_count == 1
    assert slack.upload_file.await_count == 1


def test_respects_top_k_limit() -> None:
    hits = [
        _hit(chunk_id=i, source_type="gdrive", source_uri=f"gdrive://F{i}", title=f"{i}.pdf")
        for i in range(5)
    ]
    slack = _slack_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=_gdrive_mock())
    out = skill.run(KnowledgeDeliverInput(query="x", top_k=2), _ctx())
    assert out.delivered_count == 2
    assert slack.upload_file.await_count == 2


def test_no_gdrive_hits_returns_answer_only() -> None:
    hits = [_hit(source_type="slack", source_uri="slack://C1/1.2", title="FB スレッド")]
    slack = _slack_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=_gdrive_mock())
    out = skill.run(KnowledgeDeliverInput(query="x"), _ctx())
    assert out.delivered_count == 0
    # 落ちた段を明示する（FB はあるが Drive 実ファイルに紐づかない）。
    assert "Drive の実ファイルに紐づきませんでした" in out.note
    slack.upload_file.assert_not_awaited()


_ROW_LINK = "https://docs.google.com/spreadsheets/d/SHEETID99/edit?gid=278#gid=278&range=126:126"


def test_row_hit_with_resolved_drive_url_is_delivered() -> None:
    """ナレッジ行(gsheets)ヒットでも、検索側のタイトル照合が解決した原本 Drive URL
    （SearchHitOut.url / drive_url）があれば資料本体を添付する（2026-08-14 指示:
    「しっかり資料本体を出すように」。従来は source_type=='gdrive' 限定で常に候補外だった）。
    """
    hits = [
        _hit(
            source_type="gsheets",
            source_uri=_ROW_LINK,
            url="https://drive.google.com/file/d/FILE67ID/view",
            title="社内共有情報_花王__KANEBO提案.pdf",
        ),
    ]
    slack = _slack_mock()
    gdrive = _gdrive_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=gdrive)
    out = skill.run(KnowledgeDeliverInput(query="花王の提案資料を探して"), _ctx())

    assert out.delivered_count == 1
    assert gdrive.download_file_bytes.call_args.kwargs.get("file_id") == "FILE67ID"
    assert out.references[0].delivered is True


def test_row_hit_drive_url_field_also_delivers() -> None:
    hits = [
        _hit(
            source_type="gsheets",
            source_uri=_ROW_LINK,
            drive_url="https://drive.google.com/file/d/FILEDU1/view",
            title="社内共有情報_花王__melt施策.pdf",
        ),
    ]
    gdrive = _gdrive_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=_slack_mock(), gdrive=gdrive)
    out = skill.run(KnowledgeDeliverInput(query="花王"), _ctx())
    assert out.delivered_count == 1
    assert gdrive.download_file_bytes.call_args.kwargs.get("file_id") == "FILEDU1"


def test_row_hit_without_resolution_never_attaches_the_sheet_itself() -> None:
    """解決失敗時の url はシート行リンクへフォールバックする。汎用 /d/ 正規表現だと
    シート本体の id を誤抽出し、ナレッジシートごと添付する事故になるため、
    実体ファイル形（drive.google.com/file/d/）限定の抽出で防ぐ。"""
    hits = [
        _hit(
            source_type="gsheets",
            source_uri=_ROW_LINK,
            url=_ROW_LINK,
            title="社内共有情報_花王__melt",
        )
    ]
    slack = _slack_mock()
    gdrive = _gdrive_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=gdrive)
    out = skill.run(KnowledgeDeliverInput(query="花王"), _ctx())

    assert out.delivered_count == 0
    gdrive.download_file_bytes.assert_not_called()
    slack.upload_file.assert_not_awaited()


def test_slack_permalink_url_is_not_a_candidate() -> None:
    permalink = "https://vector.slack.com/archives/C1/p1786000000000000"
    hits = [_hit(source_type="slack", source_uri="slack://C1/1.2", url=permalink, title="FB")]
    gdrive = _gdrive_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=_slack_mock(), gdrive=gdrive)
    out = skill.run(KnowledgeDeliverInput(query="x"), _ctx())
    assert out.delivered_count == 0
    gdrive.download_file_bytes.assert_not_called()


def test_no_channel_no_email_skips_delivery() -> None:
    hits = [_hit(source_type="gdrive", source_uri="gdrive://F1", title="a.pdf")]
    slack = _slack_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=_gdrive_mock())
    out = skill.run(KnowledgeDeliverInput(query="x"), _ctx(email=None))
    assert out.delivered_count == 0
    assert "配信先が分からず" in out.note
    slack.lookup_user_id_by_email.assert_not_awaited()
    assert out.answer == "要約です"  # 要約は返る（fail-open）


def test_dm_resolution_failure_is_failopen() -> None:
    hits = [_hit(source_type="gdrive", source_uri="gdrive://F1", title="a.pdf")]
    slack = _slack_mock(user_id=None)  # email→user_id 解決失敗
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=_gdrive_mock())
    out = skill.run(KnowledgeDeliverInput(query="x"), _ctx())
    assert out.delivered_count == 0
    assert "失敗" in out.note
    slack.upload_file.assert_not_awaited()


def test_download_failure_skips_that_file() -> None:
    hits = [
        _hit(source_type="gdrive", source_uri="gdrive://F1", title="ok.pdf"),
        _hit(chunk_id=2, source_type="gdrive", source_uri="gdrive://F2", title="bad.pdf"),
    ]
    gdrive = MagicMock()
    gdrive.download_file_bytes.side_effect = [b"%PDF ok", RuntimeError("403")]
    slack = _slack_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=gdrive)
    out = skill.run(KnowledgeDeliverInput(query="x"), _ctx())
    assert out.delivered_count == 1  # 落ちた1件はスキップ、1件は配信


def test_low_score_hits_not_delivered() -> None:
    # 弱いヒット（無関係・本文なし）は score ゲートで添付しない＝間違ったファイルを送らない。
    hits = [_hit(source_type="gdrive", source_uri="gdrive://F1", title="無関係.pdf", score=0.45)]
    slack = _slack_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=_gdrive_mock())
    out = skill.run(KnowledgeDeliverInput(query="サンマルクの資料"), _ctx())
    assert out.delivered_count == 0
    # 落ちた段を明示する（実ファイルには紐づいたが関連度基準に届かない）。
    assert "配信の関連度基準に届きませんでした" in out.note
    slack.upload_file.assert_not_awaited()


def test_score_gate_threshold_env() -> None:
    # KNOWLEDGE_DELIVER_MIN_SCORE を下げれば弱いヒットも配信対象になる（env 駆動の確認）。
    import os

    os.environ["KNOWLEDGE_DELIVER_MIN_SCORE"] = "0.3"
    try:
        hits = [_hit(source_type="gdrive", source_uri="gdrive://F1", title="a.pdf", score=0.45)]
        slack = _slack_mock()
        skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=_gdrive_mock())
        out = skill.run(KnowledgeDeliverInput(query="x"), _ctx())
        assert out.delivered_count == 1
    finally:
        del os.environ["KNOWLEDGE_DELIVER_MIN_SCORE"]


def test_low_confidence_hit_not_delivered() -> None:
    # 低信頼ヒット（2段階しきい値で救出された borderline）は添付しない。
    hits = [
        _hit(
            source_type="gdrive",
            source_uri="gdrive://F1",
            title="a.pdf",
            is_low_confidence=True,
        )
    ]
    slack = _slack_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=_gdrive_mock())
    out = skill.run(KnowledgeDeliverInput(query="x"), _ctx())
    assert out.delivered_count == 0
    slack.upload_file.assert_not_awaited()


def test_industry_mismatch_not_delivered() -> None:
    # クエリが「化粧品」を指定、ヒットの業界が「飲食」→ 別業界なので添付しない。
    hits = [_hit(source_type="gdrive", source_uri="gdrive://F1", title="a.pdf", industry="飲食")]
    slack = _slack_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=_gdrive_mock())
    out = skill.run(KnowledgeDeliverInput(query="化粧品の提案資料出して"), _ctx())
    assert out.delivered_count == 0
    slack.upload_file.assert_not_awaited()


def test_industry_match_delivered() -> None:
    # クエリ「化粧品」とヒット業界「化粧品」が一致 → 配信される。
    hits = [_hit(source_type="gdrive", source_uri="gdrive://F1", title="a.pdf", industry="化粧品")]
    slack = _slack_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=_gdrive_mock())
    out = skill.run(KnowledgeDeliverInput(query="化粧品の提案資料出して"), _ctx())
    assert out.delivered_count == 1


# ── 明示フィルタ（取引先/資料種別/施策/業界）→ SearchInput 伝播 ──────────────
def test_filters_propagate_to_search_input() -> None:
    # KnowledgeDeliverInput の4フィルタが SearchInput にそのまま渡る。
    hits = [_hit(source_type="gdrive", source_uri="gdrive://F1", title="a.pdf")]
    search = _search_mock(hits)
    skill = KnowledgeDeliverSkill(search=search, slack=_slack_mock(), gdrive=_gdrive_mock())
    skill.run(
        KnowledgeDeliverInput(
            query="電通への動画広告の提案資料",
            filter_client="電通",
            filter_doc_type="提案書",
            filter_solution="動画広告",
            filter_industry="食品",
        ),
        _ctx(),
    )
    si = search.run.call_args.args[0]  # SearchInput
    assert si.filter_client == "電通"
    assert si.filter_doc_type == "提案書"
    assert si.filter_solution == "動画広告"
    assert si.filter_industry == "食品"


def test_filters_default_none_backward_compat() -> None:
    # 未指定なら SearchInput の各フィルタは None（従来挙動）。
    hits = [_hit(source_type="gdrive", source_uri="gdrive://F1", title="a.pdf")]
    search = _search_mock(hits)
    skill = KnowledgeDeliverSkill(search=search, slack=_slack_mock(), gdrive=_gdrive_mock())
    skill.run(KnowledgeDeliverInput(query="資料出して"), _ctx())
    si = search.run.call_args.args[0]
    assert si.filter_client is None
    assert si.filter_doc_type is None
    assert si.filter_solution is None
    assert si.filter_industry is None


# ── 明示フィルタ優先（industry-mismatch ゲート）─────────────────────────────
def test_explicit_filter_industry_overrides_query_for_mismatch() -> None:
    # 明示 filter_industry=化粧品 → ヒット業界「飲食」は別業界なので添付しない
    # （クエリ文には業界語が無くても明示が効く）。
    hits = [_hit(source_type="gdrive", source_uri="gdrive://F1", title="a.pdf", industry="飲食")]
    slack = _slack_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=_gdrive_mock())
    out = skill.run(
        KnowledgeDeliverInput(query="提案資料出して", filter_industry="化粧品"),
        _ctx(),
    )
    assert out.delivered_count == 0
    slack.upload_file.assert_not_awaited()


def test_explicit_filter_industry_match_delivers() -> None:
    # 明示 filter_industry=化粧品 とヒット業界「化粧品」が一致 → 配信。
    hits = [_hit(source_type="gdrive", source_uri="gdrive://F1", title="a.pdf", industry="化粧品")]
    slack = _slack_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=_gdrive_mock())
    out = skill.run(
        KnowledgeDeliverInput(query="提案資料出して", filter_industry="化粧品"),
        _ctx(),
    )
    assert out.delivered_count == 1


# ── note 文言（適用フィルタ表示・0件緩和提案）──────────────────────────────
def test_note_shows_applied_filters_on_delivery() -> None:
    # 配信成功 note に「電通 × 提案書 で」のような適用フィルタが出る。
    hits = [_hit(source_type="gdrive", source_uri="gdrive://F1", title="a.pdf")]
    skill = KnowledgeDeliverSkill(
        search=_search_mock(hits), slack=_slack_mock(), gdrive=_gdrive_mock()
    )
    out = skill.run(
        KnowledgeDeliverInput(
            query="電通への提案資料", filter_client="電通", filter_doc_type="提案書"
        ),
        _ctx(),
    )
    assert out.delivered_count == 1
    assert "電通 × 提案書 で" in out.note
    assert "DM にお送りしました" in out.note


def test_note_zero_hit_states_filters_and_relaxation() -> None:
    # 0件 note に適用フィルタと緩和提案が出る。
    hits = [_hit(source_type="slack", source_uri="slack://C1/1.2", title="FB")]  # gdrive なし
    skill = KnowledgeDeliverSkill(
        search=_search_mock(hits), slack=_slack_mock(), gdrive=_gdrive_mock()
    )
    out = skill.run(
        KnowledgeDeliverInput(
            query="電通への提案資料", filter_client="電通", filter_doc_type="提案書"
        ),
        _ctx(),
    )
    assert out.delivered_count == 0
    assert "電通 × 提案書 で" in out.note
    assert "緩めて再検索" in out.note


def test_note_zero_hit_no_filter_keeps_plain_message() -> None:
    # フィルタ未指定の 0件は素の文言（緩和提案を足さない）。
    # 2026-08-27: 「FB 行だけがヒットして Drive 実ファイルが 1 件も紐づかなかった」場合に
    # 「資料が見つかりません」と読める文言を返していたのを、落ちた段を明示する文言へ変更。
    hits = [_hit(source_type="slack", source_uri="slack://C1/1.2", title="FB")]
    skill = KnowledgeDeliverSkill(
        search=_search_mock(hits), slack=_slack_mock(), gdrive=_gdrive_mock()
    )
    out = skill.run(KnowledgeDeliverInput(query="資料出して"), _ctx())
    assert out.delivered_count == 0
    assert out.note == (
        "社内のやり取り（Slack / 管理シートの行）は見つかりましたが、"
        "添付できる Drive の実ファイルに紐づきませんでした（要約のみお返しします）。"
    )


def test_note_zero_hit_distinguishes_no_hits_from_no_drive_file() -> None:
    """ヒットが 0 件のときと「FB はあるが Drive 実ファイルに紐づかない」ときを別文言にする。

    本番実測（2026-08-27）: hits=2 / candidates=0 / delivered=0 のとき、ユーザーには
    「資料そのものが存在しない」と読める文言だけが返っていた。実際には資料は Drive に
    存在し、検索プールに入っていなかっただけ（＝リコールの問題）。段が違えば文言も違う。
    """
    empty = KnowledgeDeliverSkill(
        search=_search_mock([]), slack=_slack_mock(), gdrive=_gdrive_mock()
    )
    out_empty = empty.run(KnowledgeDeliverInput(query="資料出して"), _ctx())
    assert out_empty.note == "関連する記録・資料が見つかりませんでした（要約のみお返しします）。"

    fb_only = KnowledgeDeliverSkill(
        search=_search_mock([_hit(source_type="slack", source_uri="slack://C1/1.2", title="FB")]),
        slack=_slack_mock(),
        gdrive=_gdrive_mock(),
    )
    out_fb = fb_only.run(KnowledgeDeliverInput(query="資料出して"), _ctx())
    assert out_fb.note != out_empty.note
    assert "Drive の実ファイル" in out_fb.note


def test_note_zero_hit_low_relevance_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive 実ファイルには紐づいたが関連度が基準未満、は「基準に届かなかった」と言う。"""
    monkeypatch.setenv("KNOWLEDGE_DELIVER_MIN_SCORE", "0.9")
    hits = [_hit(source_type="gdrive", source_uri="gdrive://FILE1", title="提案書", score=0.4)]
    skill = KnowledgeDeliverSkill(
        search=_search_mock(hits), slack=_slack_mock(), gdrive=_gdrive_mock()
    )
    out = skill.run(KnowledgeDeliverInput(query="資料出して"), _ctx())
    assert out.delivered_count == 0
    assert out.note == (
        "関連資料は見つかりましたが、配信の関連度基準に届きませんでした（要約のみお返しします）。"
    )


# ── 管理シート行の解決済み Drive 実体配信 ────────────────────────────────
def test_gsheet_row_hit_with_resolved_url_delivers() -> None:
    resolved_url = "https://drive.google.com/file/d/RESOLVED1/view?usp=drivesdk"
    hits = [
        _hit(
            source_type="gsheets",
            source_uri=("https://docs.google.com/spreadsheets/d/SHEET1/edit?gid=1#gid=1&range=5:5"),
            url=resolved_url,
            resolved_file_name="社内共有情報_花王株式会社__縦型提案.pdf",
            title="花王株式会社 縦型ソリューション",
            doc_type="提案書",
        )
    ]
    slack = _slack_mock()
    gdrive = _gdrive_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=gdrive)

    out = skill.run(KnowledgeDeliverInput(query="花王の提案資料を探して"), _ctx())

    assert out.delivered_count == 1
    gdrive.download_file_bytes.assert_called_once()
    assert gdrive.download_file_bytes.call_args.kwargs["file_id"] == "RESOLVED1"
    assert slack.upload_file.await_args.kwargs["title"] == (
        "社内共有情報_花王株式会社__縦型提案.pdf"
    )
    assert out.references[0].delivered is True
    assert out.references[0].url == resolved_url


def test_gsheet_row_hit_unresolved_url_not_candidate() -> None:
    sheet_row_url = "https://docs.google.com/spreadsheets/d/SHEET1/edit?gid=1#gid=1&range=5:5"
    hits = [
        _hit(
            source_type="gsheets",
            source_uri=sheet_row_url,
            url=sheet_row_url,
            title="花王株式会社 縦型ソリューション",
        )
    ]
    gdrive = _gdrive_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=_slack_mock(), gdrive=gdrive)

    out = skill.run(KnowledgeDeliverInput(query="花王の提案資料を探して"), _ctx())

    assert out.delivered_count == 0
    gdrive.download_file_bytes.assert_not_called()


def test_gsheet_resolved_native_google_doc_not_candidate() -> None:
    hits = [
        _hit(
            source_type="gsheets",
            source_uri="https://docs.google.com/spreadsheets/d/SHEET1/edit?gid=1",
            url="https://docs.google.com/presentation/d/PRES1/edit",
            title="花王株式会社 縦型ソリューション",
        )
    ]
    gdrive = _gdrive_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=_slack_mock(), gdrive=gdrive)

    out = skill.run(KnowledgeDeliverInput(query="花王の提案資料を探して"), _ctx())

    assert out.delivered_count == 0
    gdrive.download_file_bytes.assert_not_called()


def test_gsheet_resolved_dedups_with_gdrive_hit() -> None:
    hits = [
        _hit(source_type="gdrive", source_uri="gdrive://F1", title="提案.pdf"),
        _hit(
            chunk_id=2,
            source_type="gsheets",
            source_uri="https://docs.google.com/spreadsheets/d/SHEET1/edit?gid=1",
            url="https://drive.google.com/file/d/F1/view",
            resolved_file_name="提案.pdf",
            title="花王株式会社 縦型ソリューション",
        ),
    ]
    slack = _slack_mock()
    gdrive = _gdrive_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=gdrive)

    out = skill.run(KnowledgeDeliverInput(query="花王の提案資料を探して"), _ctx())

    assert out.delivered_count == 1
    assert gdrive.download_file_bytes.call_count == 1
    assert slack.upload_file.await_count == 1
    assert all(ref.delivered for ref in out.references)


def test_same_filename_different_files_do_not_overwrite_each_other() -> None:
    """sanitize 後に同名となる別 file_id の候補同士が一時ファイルを上書きし合わない。

    旧実装は tmpdir 直下にファイル名だけで書いており、同名別ファイルが2候補あると
    2件目が1件目を上書きし、両添付とも2件目のバイト列になっていた。

    バイト列は**アップロード時点**で読む。run() は tmpdir を必ず削除するため
    （常駐タスクの /tmp に残さない契約）、戻った後にパスを読むことはできない。"""

    hits = [
        _hit(source_type="gdrive", source_uri="gdrive://F1", title="提案書_共通フォーマット.pdf"),
        _hit(
            chunk_id=2,
            source_type="gdrive",
            source_uri="gdrive://F2",
            title="提案書_共通フォーマット.pdf",
        ),
    ]
    uploaded_bytes: list[bytes] = []
    uploaded_paths: list[str] = []

    async def _capture(channel: str, path: str, request_id: str, **kwargs: object) -> bool:
        uploaded_paths.append(path)
        uploaded_bytes.append(Path(path).read_bytes())
        return True

    slack = _slack_mock()
    slack.upload_file = AsyncMock(side_effect=_capture)
    gdrive = MagicMock()
    gdrive.download_file_bytes.side_effect = lambda *, file_id, request_id: (
        b"%PDF-1.4 " + file_id.encode()
    )
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=slack, gdrive=gdrive)

    out = skill.run(KnowledgeDeliverInput(query="提案書", top_k=2), _ctx())

    assert out.delivered_count == 2
    assert set(uploaded_bytes) == {b"%PDF-1.4 F1", b"%PDF-1.4 F2"}
    # 添付が済んだら一時ファイルは残さない（共通部品の契約を呼び出し側が守っている）。
    assert uploaded_paths and not any(Path(p).exists() for p in uploaded_paths)


def test_gsheet_resolved_respects_confidence_gates() -> None:
    hits = [
        _hit(
            source_type="gsheets",
            source_uri="https://docs.google.com/spreadsheets/d/SHEET1/edit?gid=1",
            url="https://drive.google.com/file/d/RESOLVED1/view",
            resolved_file_name="提案.pdf",
            title="花王株式会社 縦型ソリューション",
            score=0.2,
        )
    ]
    gdrive = _gdrive_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits), slack=_slack_mock(), gdrive=gdrive)

    out = skill.run(KnowledgeDeliverInput(query="花王の提案資料を探して"), _ctx())

    assert out.delivered_count == 0
    gdrive.download_file_bytes.assert_not_called()
