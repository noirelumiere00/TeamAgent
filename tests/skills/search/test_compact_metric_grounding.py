"""再生中央値と保存合計を、モデルが同一指標のように比較しない。"""

from teamagent.skills.search import fanout
from teamagent.skills.search.compact import render
from tests.skills.search.test_comparison import _campaign
from tests.skills.search.test_fanout import META, _converse, make, run
from tests.skills.search.test_fanout import setup as setup


def test_model_cannot_compare_retrieved_median_plays_against_total_saves():
    answer = render(
        "中央値153,300回再生に対し保存261が多く効きました。[S:1]\n"
        "投稿文は調理アレンジを紹介しています。[S:1]",
        {
            "1": (
                "施策シート",
                "https://example.test/sheet",
                "再生中央値153,300回再生。保存合計261。調理アレンジ",
            )
        },
    )
    assert "153,300" not in answer and "261" not in answer
    assert "調理アレンジ" in answer
    assert "https://example.test/sheet" in answer


def test_compact_campaign_comparison_inserts_only_code_generated_matching_metrics(monkeypatch):
    hits = [
        _campaign(
            1,
            "施策A",
            "再生数: 合計 1,230,000、平均 205,000、中央値 153,300\n反応: 保存 合計 261、平均 43.5",
        ),
        _campaign(2, "施策B", "再生数: 合計 685,300、平均 171,325\n反応: 保存 合計 110、平均 27.5"),
    ]
    for hit in hits:
        hit.metadata["source_uri"] = f"gdrive://CAMPAIGN{hit.chunk_id}"
    skill, _ = make(hits)
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    skill._bedrock.converse.return_value = _converse("調理の手軽さを紹介しています。[S:1]")
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = run(
        skill,
        query="施策Aと施策Bを比較して何が効いた？",
        meta={**META, "_search_compact_answer": True},
    )
    assert "205,000" in out.answer and "171,325" in out.answer
    assert "再生平均" in out.answer and "施策Aが上" in out.answer
    assert "153,300" not in out.answer
    assert out.answer.count("https://") == 2
