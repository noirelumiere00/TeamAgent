"""10-19 の長文回答を、取得済みの使用出典だけで短くする。"""

import pytest

from teamagent.skills.search import fanout
from tests.skills.search.test_fanout import META, _converse, _vault_hit, make, run
from tests.skills.search.test_fanout import setup as setup


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_compact_keeps_only_cited_sources_and_no_inventory(monkeypatch, fanout_enabled):
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    hits = [_vault_hit(i, title=f"資料{i}", source_uri=f"gdrive://DOC{i}") for i in range(1, 9)]
    skill, _ = make(hits)
    skill._bedrock.converse.return_value = _converse(
        "施策は実施済みです。[S:1]\n顧客は改善を求めています。[S:2]\n"
        "次回提案を準備します。[S:1]\n\n📁 金庫の資料\n- 不使用の資料8\n"
        "📎 資料リンク\nhttps://fabricated.test/file\n探した範囲: 長い一覧"
    )
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = run(
        skill, query="施策は今どうなっている？", meta={**META, "_search_compact_answer": True}
    )
    assert "施策は実施済みです。" in out.answer
    assert "出典:" in out.answer
    assert out.answer.count("https://") == 2
    assert "DOC1" in out.answer and "DOC2" in out.answer
    assert not any(s in out.answer for s in ("📁", "資料リンク", "探した範囲", "fabricated", "[S:"))
    assert len(out.answer.splitlines()) <= 8
    assert len(out.answer) <= 2500
    assert out.answer_format == "compact"


def test_compact_long_body_drops_whole_sentences_and_unused_sources(monkeypatch):
    skill, _ = make(
        [_vault_hit(i, title=f"資料{i}", source_uri=f"gdrive://DOC{i}") for i in range(1, 5)]
    )
    skill._bedrock.converse.return_value = _converse(
        "\n".join(f"{'要点' * 60}です。[S:{i}]" for i in range(1, 5))
    )
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = run(skill, query="状況は？", meta={**META, "_search_compact_answer": True})
    assert out.answer.count("https://") <= 3
    assert "DOC4" not in out.answer
    assert len(out.answer.split("出典:")[0]) <= 550


def test_compact_does_not_invent_source_for_uncited_sentence(monkeypatch):
    skill, _ = make()
    skill._bedrock.converse.return_value = _converse("根拠のない主張です。[S:999]")
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = run(skill, query="状況は？", meta={**META, "_search_compact_answer": True})
    assert "根拠のない主張" not in out.answer
    assert "https://" not in out.answer


@pytest.mark.parametrize(
    "query,expected", [("施策は今どうなっている？", False), ("動画ごとの結果を教えて", True)]
)
def test_compact_video_metrics_only_when_requested(monkeypatch, query, expected):
    from tests.skills.search.test_campaign_files import _video, _video_campaign

    hit = _video_campaign(
        1,
        "みなと食品",
        "ハンバーグ",
        [_video("https://example.test/video1", "調理の手軽さ", 507700, 129)],
    )
    hit.metadata["source_uri"] = "gdrive://CAMPAIGN1"
    skill, _ = make([hit])
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    skill._campaign_files = True
    skill._bedrock.converse.return_value = _converse("調理の手軽さを紹介しました。[S:1]")
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = run(skill, query=query, meta={**META, "_search_compact_answer": True})
    assert ("📊" in out.answer) is expected
    assert ("507,700" in out.answer) is expected
    assert "施策の反応合計" not in out.answer
    assert "シートのみ" not in out.answer
    assert "金庫の資料" not in out.answer
    assert len(out.answer) <= 2500


def test_compact_explicit_list_preserves_existing_inventory(monkeypatch):
    skill, _ = make([_vault_hit(1, title="提案書", source_uri="gdrive://PROPOSAL")])
    skill._summarize = pytest.fail
    out = run(skill, query="資料を全部一覧で出して", meta={**META, "_search_compact_answer": True})
    assert out.answer_format == "list"
    assert "PROPOSAL" in out.answer and "📁" in out.answer


def test_compact_annotated_inventory_cannot_return_as_prose():
    from teamagent.skills.search.compact import render

    sources = {"1": ("報告書", "https://example.test/report", "実施済みです。")}
    answer = render(
        "実施済みです。[S:1]\n📁 金庫の資料[S:1]\n- 報告書[S:1]\n施策の数字はシートのみ[S:1]",
        sources,
    )
    assert "金庫の資料" not in answer
    assert "シートのみ" not in answer
    assert "- 報告書" not in answer
    assert answer.count("https://") == 1


@pytest.mark.parametrize(
    "url", ["", "javascript:alert(1)", "https://[invalid", "https://user:pass@example.test"]
)
def test_compact_drops_claims_and_derived_facts_without_a_safe_source_link(url):
    from teamagent.skills.search.compact import render

    answer = render(
        "確認済みです。[S:1]\n別の根拠があります。[S:2]",
        {
            "1": ("未解決資料", url, "確認済みです。"),
            "2": ("報告書", "https://example.test/report", "別の根拠があります。"),
        },
        facts="再生平均はAが100回、Bが50回です。",
        fact_keys=["1"],
    )
    assert "確認済み" not in answer
    assert "再生平均" not in answer
    assert "未解決資料" not in answer
    assert "別の根拠" in answer and answer.count("https://") == 1


def test_compact_source_urls_cannot_exceed_the_final_message_limit():
    from teamagent.skills.search.compact import render

    answer = render(
        "確認済みです。[S:1]",
        {"1": ("長いURL", "https://example.test/" + "a" * 2600, "確認済みです。")},
        facts="再生平均はAが100回、Bが50回です。",
        fact_keys=["1"],
    )
    assert len(answer) <= 2500
    assert "確認済み" not in answer and "再生平均" not in answer
    assert "https://" not in answer


def test_compact_sentence_limit_does_not_depend_on_model_line_breaks():
    from teamagent.skills.search.compact import render

    answer = render(
        "。".join(["要点です"] * 7) + "。[S:1]",
        {"1": ("報告書", "https://example.test/report", "要点です。")},
    )
    assert answer.count("。") <= 6


@pytest.mark.parametrize("link", ["HTTPS://fabricated.test/file", "[資料](//fabricated.test/file)"])
def test_compact_prose_cannot_introduce_its_own_links(link):
    from teamagent.skills.search.compact import render

    answer = render(
        f"詳細は{link}です。[S:1]",
        {"1": ("報告書", "https://example.test/report", "詳細があります。")},
    )
    assert "fabricated" not in answer
