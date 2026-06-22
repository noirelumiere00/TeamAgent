"""knowledge_deliver: file_id 抽出 ＋ 検索→DM配信スキルのテスト（adapters はモック）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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
    assert "見つかりません" in out.note
    slack.upload_file.assert_not_awaited()


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
    assert "見つかりません" in out.note
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
