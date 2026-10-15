"""関係図リンクは表示した資料だけを使い、字数内に収める。"""

from teamagent.skills.search.answer_sections import Entry, Sections, compose_answer, graph_link
from tests.connect_web.test_search_graph import uid

BASE = "https://connect.newstv.co.jp"


def test_no_documents_no_link() -> None:
    assert "🕸" not in compose_answer(
        head="要約", sections=Sections(), lanes={}, graph_base_url=BASE
    )
    assert "🕸" not in compose_answer(
        head="要約",
        sections=Sections(entries={"karte": [Entry(title="カルテ", document_id=uid(1))]}),
        lanes={},
        graph_base_url=BASE,
    )


def test_shown_ids_dedup_and_limit() -> None:
    entries = [
        Entry(title=str(i), url=f"https://example.test/{i}", document_id=uid(i))
        for i in range(1, 21)
    ]
    sections = Sections(entries={"vault": entries}, required_counts={"vault": 20})
    link = graph_link(sections, {"vault": 20}, BASE)
    assert link.startswith("🕸 関係図で見る: <" + BASE + "/search/graph/focus?seed=")
    assert link.endswith(",".join(uid(i) for i in range(1, 13)) + ">")
    assert graph_link(Sections(entries={"vault": entries}), {"vault": 1}, BASE).endswith(
        uid(1) + ">"
    )
    duplicate = Sections(entries={"vault": entries[:1], "case": entries[:1]})
    assert graph_link(duplicate, {"vault": 2, "case": 2}, BASE).count(uid(1)) == 1


def test_trimming_keeps_limit_and_only_shown_seeds() -> None:
    sections = Sections(
        entries={
            "vault": [
                Entry(title="資料" * 100, document_id=uid(i), url=f"https://example.test/{i}")
                for i in range(1, 4)
            ]
        }
    )
    for size in (0, 2100, 2400, 4000):
        answer = compose_answer(head="あ" * size, sections=sections, lanes={}, graph_base_url=BASE)
        assert len(answer) <= 2500
        if "🕸" in answer:
            assert "📁" in answer
            assert uid(3) not in answer


def test_invalid_uuid_and_base_no_link() -> None:
    sections = Sections(entries={"vault": [Entry(title="資料", document_id="<script>")]})
    assert graph_link(sections, {"vault": 2}, BASE) == ""
    for base in ("", "javascript:alert(1)", BASE + "?q=secret"):
        assert (
            graph_link(
                Sections(entries={"vault": [Entry(title="資料", document_id=uid(1))]}),
                {"vault": 2},
                base,
            )
            == ""
        )


def test_hit_and_case_row_document_ids() -> None:
    from teamagent.adapters.pgvector_client import SearchHit
    from teamagent.skills.search.answer_sections import build_sections

    hits = [
        SearchHit(
            chunk_id=i,
            content="資料",
            score=1.0,
            metadata={"document_id": uid(i), "title": str(i), **meta},
        )
        for i, meta in ((1, {}), (2, {"campaign_aggregate": "true"}), (3, {"topic": "案件決定"}))
    ]
    sections = build_sections(vault_hits=hits, case_rows=[{"document_id": uid(4), "title": "事例"}])
    answer = compose_answer(head="要約", sections=sections, lanes={}, graph_base_url=BASE)
    for i in range(1, 5):
        assert uid(i) in answer
