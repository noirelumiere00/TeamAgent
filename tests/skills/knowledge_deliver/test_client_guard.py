"""knowledge_deliver の取引先ガード（KNOWLEDGE_DELIVER_CLIENT_GUARD）のテスト。

本番事故（2026-10-06）を再現する:
  利用者が DM で「日本コカ・コーラの紅茶花伝 無糖 アールグレイアイスティー（サンリオ限定
  ボトル）のショート動画施策事例を探して。過去の実績と、根拠のPDFを添付して。」と依頼。
  本文は紅茶花伝を要約したのに、添付 PDF 3 件は東洋水産・アイホン・日立という別取引先の
  提案書だった。top1（本当の記録）は管理シートの行で Drive 実体が無く、配信基準
  （スコア・低信頼・業界）だけを見る選定で下位の別取引先の高スコア PDF が回った。

フェイクは本番の形に合わせる: top1 = 該当取引先の管理シート行（Drive 実体なし）、
2〜4 位 = 別取引先の gdrive PDF（高スコア・低信頼でない・業界未設定）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.knowledge_deliver.schema import KnowledgeDeliverInput
from teamagent.skills.knowledge_deliver.skill import KnowledgeDeliverSkill
from teamagent.skills.search.schema import SearchHitOut, SearchInput, SearchOutput
from teamagent.skills.search.skill import SearchSkill

GUARD_ENV = "KNOWLEDGE_DELIVER_CLIENT_GUARD"

INCIDENT_QUERY = (
    "日本コカ・コーラの紅茶花伝 無糖 アールグレイアイスティー（サンリオ限定ボトル）の"
    "ショート動画施策事例を探して。過去の実績と、根拠のPDFを添付して。"
    "確認できないことは不明として。"
)
_ROW_LINK = "https://docs.google.com/spreadsheets/d/SHEET1/edit?gid=1#gid=1&range=10:10"
_OTHER_CLIENTS = ("東洋水産", "アイホン", "日立製作所")


@pytest.fixture(autouse=True)
def _guard_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定（未設定＝ON）から始める。"""
    monkeypatch.delenv(GUARD_ENV, raising=False)
    monkeypatch.delenv("KNOWLEDGE_DELIVER_MIN_SCORE", raising=False)


# ── フェイク ───────────────────────────────────────────────────────────────


def _hit(**kw: object) -> SearchHitOut:
    base: dict[str, object] = {"chunk_id": 1, "content": "本文", "score": 0.9}
    base.update(kw)
    return SearchHitOut(**base)  # type: ignore[arg-type]


def _incident_hits(*, top1_project: str = "日本コカ・コーラ") -> list[SearchHitOut]:
    top1 = _hit(
        chunk_id=1,
        score=0.95,
        source_type="gsheets",
        source_uri=_ROW_LINK,
        title="紅茶花伝 無糖 アールグレイ サンリオ限定ボトル ショート動画",
        project=top1_project,
    )
    others = [
        _hit(
            chunk_id=i + 2,
            score=0.9 - i * 0.05,
            source_type="gdrive",
            source_uri=f"gdrive://OTHER{i}",
            url=f"https://drive.google.com/file/d/OTHER{i}/view",
            title=f"{name}_ショート動画提案書.pdf",
            project=name,
        )
        for i, name in enumerate(_OTHER_CLIENTS)
    ]
    return [top1, *others]


def _search_mock(
    hits: list[SearchHitOut],
    *,
    answer: str = "紅茶花伝のショート動画施策の要約です",
    found: bool = True,
    query_client: str | None = None,
) -> MagicMock:
    m = MagicMock()
    m.run.return_value = SearchOutput(
        answer=answer, hits=hits, total_cost_usd=0.01, found=found, query_client=query_client
    )
    return m


def _slack_mock() -> MagicMock:
    m = MagicMock()
    m.lookup_user_id_by_email = AsyncMock(return_value="U1")
    m.open_dm = AsyncMock(return_value="D1")
    m.upload_file = AsyncMock(return_value=True)
    return m


def _gdrive_mock() -> MagicMock:
    m = MagicMock()
    m.download_file_bytes.return_value = b"%PDF-1.4 fake"
    return m


def _ctx() -> SkillContext:
    """本番の DM 依頼の形（D 始まり・身元検証済み）。"""
    return SkillContext(
        metadata={
            "identity_verified": True,
            "user_email": "u@vectorinc.co.jp",
            "channel_id": "D0SELF",
        }
    )


def _run(
    hits: list[SearchHitOut], query: str = INCIDENT_QUERY, **kw: Any
) -> tuple[Any, MagicMock, MagicMock]:
    filter_client = kw.pop("filter_client", None)
    slack = _slack_mock()
    gdrive = _gdrive_mock()
    skill = KnowledgeDeliverSkill(search=_search_mock(hits, **kw), slack=slack, gdrive=gdrive)
    out = skill.run(KnowledgeDeliverInput(query=query, filter_client=filter_client), _ctx())
    return out, slack, gdrive


def _uploaded_names(slack: MagicMock) -> list[str]:
    names: list[str] = []
    for call in slack.upload_file.await_args_list:
        # upload_file(channel, path, request_id, title=<添付ファイル名>, ...)
        names.append(str(call.kwargs.get("title")))
    return names


# ── 本番事故の再現 ─────────────────────────────────────────────────────────


def test_incident_other_clients_pdfs_are_not_attached() -> None:
    """top1 = 該当取引先（Drive 実体なし）、2〜4 位 = 別取引先の高スコア PDF。

    修正後: 添付 0 件・「日本コカ・コーラの資料のファイル本体は見つかりませんでした」・
    refs に別取引先を並べない。変異: 取引先判定を外すと 3 件添付されて赤。
    """
    out, slack, gdrive = _run(_incident_hits(), query_client="日本コカ・コーラ")

    assert out.delivered_count == 0
    slack.upload_file.assert_not_awaited()
    gdrive.download_file_bytes.assert_not_called()
    assert "日本コカ・コーラの資料のファイル本体は見つかりませんでした" in out.note
    assert "別の取引先の資料" in out.note
    for name in _OTHER_CLIENTS:
        assert name not in out.note
        assert all(name not in (r.title or "") for r in out.references)
    # 該当取引先の記録（top1）だけが根拠として残る
    assert [r.title for r in out.references] == [
        "紅茶花伝 無糖 アールグレイ サンリオ限定ボトル ショート動画"
    ]
    # 利用者に検索や確認を頼む文を書かない（条件緩和の再検索提案もしない）
    assert "再検索" not in out.note and "確認して" not in out.note


def test_incident_reproduces_with_guard_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """フラグ "0" で従来どおり＝事故の挙動（別取引先 3 件を添付）に戻る。"""
    monkeypatch.setenv(GUARD_ENV, "0")
    out, slack, _ = _run(_incident_hits(), query_client="日本コカ・コーラ")

    assert out.delivered_count == 3
    assert slack.upload_file.await_count == 3
    assert len(out.references) == 4


def test_incident_detects_nakaguro_client_from_hits_without_search_value() -> None:
    """search が取引先を返さない構成でも、ヒットの取引先名（中黒入り・法人格つき）から
    質問文の「日本コカ・コーラ」を語境界つきで検出する（result_guard の fallback と同じ規則）。
    """
    out, slack, _ = _run(_incident_hits(top1_project="日本コカ・コーラ株式会社"))

    assert out.delivered_count == 0
    slack.upload_file.assert_not_awaited()
    assert "日本コカ・コーラの資料のファイル本体は見つかりませんでした" in out.note


def test_nakaguro_filter_client_matches_hit_written_without_nakaguro() -> None:
    """filter_client「日本コカ・コーラ」と cls_project「コカコーラ」は同じ取引先（名寄せ）。"""
    hits = [
        _hit(
            source_type="gdrive",
            source_uri="gdrive://COKE1",
            title="紅茶花伝_施策レポート.pdf",
            project="コカコーラ",
        ),
        *_incident_hits()[1:],
    ]
    out, slack, _ = _run(hits, filter_client="日本コカ・コーラ")

    assert out.delivered_count == 1
    assert _uploaded_names(slack) == ["紅茶花伝_施策レポート.pdf"]


# ── 取引先一致・別名・取引先なし ───────────────────────────────────────────


def test_matching_client_pdf_is_attached_and_others_are_not() -> None:
    hits = [
        _hit(
            chunk_id=1,
            source_type="gdrive",
            source_uri="gdrive://COKE1",
            url="https://drive.google.com/file/d/COKE1/view",
            title="紅茶花伝_ショート動画_実績報告.pdf",
            project="日本コカ・コーラ",
        ),
        *_incident_hits()[1:],
    ]
    out, slack, _ = _run(hits, query_client="日本コカ・コーラ")

    assert out.delivered_count == 1
    assert _uploaded_names(slack) == ["紅茶花伝_ショート動画_実績報告.pdf"]
    assert "該当資料 1 件" in out.note
    assert [r.title for r in out.references] == ["紅茶花伝_ショート動画_実績報告.pdf"]


def test_alias_brand_counts_as_the_same_client() -> None:
    """アース製薬 ⇔ ハビットプロ（result_guard の別名 seed）は一致扱いで添付する。"""
    hits = [
        _hit(
            source_type="gdrive",
            source_uri="gdrive://HB1",
            title="ブランド施策_提案.pdf",
            project="ハビットプロ",
        ),
        _hit(
            chunk_id=2,
            source_type="gdrive",
            source_uri="gdrive://KAO1",
            title="花王_提案.pdf",
            project="花王",
        ),
    ]
    out, slack, _ = _run(hits, query="アース製薬の提案資料出して", filter_client="アース製薬")

    assert out.delivered_count == 1
    assert _uploaded_names(slack) == ["ブランド施策_提案.pdf"]


def test_entities_tag_counts_as_the_client() -> None:
    """cls_project が相手側（コラボ先）でも、cls_entities に名指しの取引先があれば一致。"""
    hits = [
        _hit(
            source_type="gdrive",
            source_uri="gdrive://COLLAB1",
            title="コラボ施策.pdf",
            project="祇園辻利",
            entities=["サンマルクカフェ", "祇園辻利"],
        ),
    ]
    out, _, _ = _run(hits, query="サンマルクカフェの事例", query_client="サンマルクカフェ")
    assert out.delivered_count == 1


def test_unknown_client_hit_is_not_attached_when_client_is_named() -> None:
    """取引先メタも題名の一致も無いファイルは「その取引先の資料」と言えないので添付しない。"""
    hits = [_hit(source_type="gdrive", source_uri="gdrive://X1", title="a.pdf")]
    out, slack, _ = _run(hits, query_client="日本コカ・コーラ")
    assert out.delivered_count == 0
    slack.upload_file.assert_not_awaited()
    assert "日本コカ・コーラの資料のファイル本体は見つかりませんでした" in out.note


def test_query_without_client_keeps_previous_behaviour() -> None:
    """取引先名の無い質問は従来どおり（取引先が混在していても配信基準だけで添付）。"""
    hits = _incident_hits()[1:]
    out, slack, _ = _run(hits, query="食品業界のショート動画事例ある？")
    assert out.delivered_count == 3
    assert slack.upload_file.await_count == 3
    assert len(out.references) == 3


def test_self_org_name_is_not_treated_as_client() -> None:
    """自社名（ベクトル・NewsTV 等）の filter_client は取引先指定として扱わない。"""
    hits = _incident_hits()[1:]
    out, _, _ = _run(hits, query="ベクトルの事例", filter_client="ベクトル")
    assert out.delivered_count == 3


# ── 該当なし（found=False）────────────────────────────────────────────────


def test_not_found_attaches_nothing() -> None:
    """検索側が「該当なし」と判定したら、配信基準を満たすヒットがあっても添付しない。"""
    hits = _incident_hits()[1:]
    out, slack, gdrive = _run(
        hits,
        query="ショート動画の事例",
        answer="金庫に該当する資料は見つかりませんでした（近いもの: 『A』）。",
        found=False,
    )
    assert out.delivered_count == 0
    slack.upload_file.assert_not_awaited()
    gdrive.download_file_bytes.assert_not_called()
    assert out.references == []
    assert "見つかりませんでした" in out.note


def test_not_found_with_client_says_client_files_are_missing() -> None:
    out, slack, _ = _run(_incident_hits(), found=False, query_client="日本コカ・コーラ")
    assert out.delivered_count == 0
    slack.upload_file.assert_not_awaited()
    assert out.references == []
    assert "日本コカ・コーラの資料のファイル本体は見つかりませんでした" in out.note


def test_not_found_with_guard_off_keeps_previous_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GUARD_ENV, "0")
    out, _, _ = _run(_incident_hits()[1:], query="ショート動画の事例", found=False)
    assert out.delivered_count == 3


# ── 回答末尾の資料リンク ───────────────────────────────────────────────────


def test_other_client_links_are_removed_from_answer_links_block() -> None:
    """search の「📎 資料リンク」に別取引先の資料が並んでいたら、根拠として残さない。"""
    hits = [
        _hit(
            chunk_id=1,
            source_type="gdrive",
            source_uri="gdrive://COKE1",
            url="https://drive.google.com/file/d/COKE1/view",
            title="紅茶花伝_実績.pdf",
            project="日本コカ・コーラ",
        ),
        *_incident_hits()[1:],
    ]
    answer = (
        "紅茶花伝の要約です。\n\n📎 *資料リンク*\n"
        "- [紅茶花伝_実績.pdf](https://drive.google.com/file/d/COKE1/view)\n"
        "- [東洋水産_ショート動画提案書.pdf](https://drive.google.com/file/d/OTHER0/view)\n"
        "- [アイホン_ショート動画提案書.pdf](https://drive.google.com/file/d/OTHER1/view)"
    )
    out, slack, _ = _run(hits, answer=answer, query_client="日本コカ・コーラ")

    assert "COKE1" in out.answer
    assert "OTHER0" not in out.answer and "OTHER1" not in out.answer
    assert "📎 *資料リンク*" in out.answer
    # 添付に乗せる要約（initial_comment）も同じ
    comment = slack.upload_file.await_args.kwargs.get("initial_comment")
    assert comment == out.answer


def test_links_heading_is_dropped_when_all_links_are_other_clients() -> None:
    answer = (
        "要約です。\n\n📎 *資料リンク*\n"
        "- [東洋水産_ショート動画提案書.pdf](https://drive.google.com/file/d/OTHER0/view)"
    )
    out, _, _ = _run(_incident_hits(), answer=answer, query_client="日本コカ・コーラ")
    assert out.answer == "要約です。"


# ── 内部フィールドはツール結果に出ない ─────────────────────────────────────


def test_internal_fields_are_not_serialized() -> None:
    out = SearchOutput(
        answer="a",
        hits=[_hit(entities=["日本コカ・コーラ"])],
        total_cost_usd=0.0,
        query_client="日本コカ・コーラ",
    )
    dumped = out.model_dump()
    assert "query_client" not in dumped
    assert "entities" not in dumped["hits"][0]


# ── search → knowledge_deliver の結線（実 SearchSkill・DB/Bedrock はフェイク）─────


def _real_search(hits: list[SearchHit], vocab: list[str]) -> SearchSkill:
    bedrock = MagicMock()
    bedrock.converse.return_value = ConverseResponse(
        text="紅茶花伝のショート動画施策の要約です。",
        usage=TokenUsage(
            input_tokens=10,
            output_tokens=10,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.001,
        ),
        model_id="jp.anthropic.claude-haiku-4-5",
        latency_ms=10,
        stop_reason="end_turn",
    )
    pg = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    pg.connection.return_value = cm
    pg.search_similar.return_value = hits
    pg.list_client_names.return_value = vocab

    class _Embedder:
        def embed(self, text: str) -> list[float]:
            return [0.1] * 1024

    return SearchSkill(
        bedrock=bedrock, pgvector=pg, embedder=_Embedder(), target_table="proposal_chunks"
    )


def _db_hit(chunk_id: int, score: float, **meta: Any) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id, content="ショート動画施策の本文", score=score, metadata=meta
    )


def test_end_to_end_search_detects_client_and_deliver_attaches_nothing() -> None:
    """実 SearchSkill が DB 語彙から「日本コカ・コーラ」を検出し、query_client で渡す。"""
    hits = [
        _db_hit(
            1,
            0.95,
            source_type="gsheets",
            source_uri=_ROW_LINK,
            title="紅茶花伝 無糖 アールグレイ サンリオ限定ボトル",
            cls_project="日本コカ・コーラ",
        ),
        *[
            _db_hit(
                i + 2,
                0.9,
                source_type="gdrive",
                source_uri=f"gdrive://OTHER{i}",
                title=f"{name}_ショート動画提案書.pdf",
                cls_project=name,
                cls_entities=[name],
            )
            for i, name in enumerate(_OTHER_CLIENTS)
        ],
    ]
    search = _real_search(hits, vocab=["日本コカ・コーラ", *_OTHER_CLIENTS])
    s_out = search.run(
        input=SearchInput(query=INCIDENT_QUERY, top_k=5),
        ctx=SkillContext(metadata={}),
    )
    assert s_out.query_client == "日本コカ・コーラ"
    assert s_out.found is True  # top1 が該当取引先＝検索側は「有り」（事故と同じ状態）
    assert any(h.entities == ["東洋水産"] for h in s_out.hits)

    slack = _slack_mock()
    skill = KnowledgeDeliverSkill(search=search, slack=slack, gdrive=_gdrive_mock())
    out = skill.run(KnowledgeDeliverInput(query=INCIDENT_QUERY), _ctx())
    assert out.delivered_count == 0
    slack.upload_file.assert_not_awaited()
    assert "日本コカ・コーラの資料のファイル本体は見つかりませんでした" in out.note


# ── 取引先が既知の語彙に無いとき（10-06 実データ QA）────────────────────────────


def _itoen_report(**kw: Any) -> SearchHitOut:
    """本物の金庫で事故の質問に返った候補の形（別の会社の報告書・問いの固有名詞を含まない）。"""
    base: dict[str, Any] = {
        "chunk_id": 9,
        "score": 0.9,
        "source_type": "gdrive",
        "source_uri": "gdrive://ITOEN",
        "url": "https://drive.google.com/file/d/ITOEN/view",
        "title": "レポート_0622_伊藤園様_ショート動画施策報告書.pptx",
        "project": "伊藤園",
        "content": "お茶飲料のショート動画施策の報告。ペットボトル飲料の再生数と保存数。",
    }
    base.update(kw)
    return _hit(**base)


def test_unknown_client_other_company_report_is_not_attached() -> None:
    """取引先（日本コカ・コーラ）が語彙に無く名指しを検出できなくても、問いの固有名詞
    （コーラ・アールグレイアイスティー・サンリオ）を含まない別会社の報告書は添付しない。"""
    out, slack, _ = _run([_itoen_report()])
    assert slack.upload_file.await_count == 0
    assert out.delivered_count == 0
    assert out.references == []
    assert "伊藤園" not in out.note
    assert "「コーラ」の資料のファイル本体は見つかりませんでした" in out.note


def test_unknown_client_report_that_mentions_the_subject_is_attached() -> None:
    """問いの固有名詞を本文に含む資料は、語彙に無い取引先でも添付する。"""
    hit = _itoen_report(
        title="紅茶花伝_サンリオ限定ボトル_ショート動画報告.pdf",
        project="",
        content="日本コカ・コーラ 紅茶花伝 サンリオ限定ボトルのショート動画施策",
    )
    out, slack, _ = _run([hit])
    assert slack.upload_file.await_count == 1
    assert out.delivered_count == 1


def test_unknown_client_guard_follows_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GUARD_ENV, "0")
    _, slack, _ = _run([_itoen_report()])
    assert slack.upload_file.await_count == 1
