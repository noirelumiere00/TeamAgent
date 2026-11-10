"""件数・選別・リンクはコード、要点だけフェイクLLM。外部接続なし。"""

import json
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import compact, fanout, material_files
from teamagent.skills.search.answer_mode import is_case_list
from teamagent.skills.search.case_list import render
from teamagent.skills.search.composite import SlackItem, SlackLookup
from teamagent.skills.search.fanout_sources import CaseRow, SourceResult
from teamagent.skills.search.schema import SearchInput, SlackHitOut
from tests.skills.search.test_answer_modes import _fake_bedrock, _hit
from tests.skills.search.test_fanout import META, make
from tests.skills.search.test_fanout import setup as setup


def hit(index, *, industry="飲料", score=0.9, **meta):
    original = _hit(index, f"架空企画{index}")
    return replace(
        original,
        content="商品の実演動画を配信。利用場面を紹介。",
        score=score,
        metadata={**original.metadata, "cls_industry": industry, **meta},
    )


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("counted", [False, True])
@pytest.mark.parametrize("fill", [False, True])
def test_thread_attachments_only_use_own_file_points_and_skip_untitled(
    monkeypatch, fanout_enabled, counted, fill
):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    post = "https://example.slack.com/archives/C0SHARED/p1784424000000001"
    points = "架空電力様は視聴回数678万8536回。\n検索数19pt上昇。"
    files = [
        {
            "id": f"F0FILE{i}",
            "name": f"飲料施策{i}.pdf",
            "permalink": f"https://example.slack.com/files/U0OWNER/F0FILE{i}/report.pdf",
        }
        for i in range(1, 4)
    ]
    reader = MagicMock()
    reader.message_files = AsyncMock(return_value=files)
    reader.file_info = AsyncMock(side_effect=lambda fid: next(f for f in files if f["id"] == fid))
    monkeypatch.setattr(material_files, "file_reader", lambda *a, **kw: reader)
    monkeypatch.setattr(
        material_files, "_drive_reader", MagicMock(side_effect=AssertionError("no real Drive"))
    )
    shared = replace(
        hit(10, source_type="slack", source_uri=post, case_effect=points), content=points
    )
    imported = replace(
        hit(
            11,
            title=files[0]["name"],
            source_type="other",
            source_uri=files[0]["permalink"],
            external_id=files[0]["id"],
            files=files,
        ),
        content=points,
    )
    untitled = hit(12, title="", source_type="slack", source_uri=post.replace("000001", "000002"))
    hits = [shared, imported, untitled, *([hit(4), hit(5)] if fill else [])]
    # 題名なしの投稿は添付も持たない。同じスレッドの3資料とは別候補。
    reader.message_files.side_effect = lambda channel, ts: files if ts.endswith("000001") else []
    skill, _ = make(hits)
    monkeypatch.setattr(skill, "_retrieve", lambda *a, **kw: hits)
    monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: SlackLookup("ok", "ok"))
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))
    monkeypatch.setattr(skill, "_resolve_file_urls", lambda *a: {})
    bedrock = _fake_bedrock()

    def summarize(**kw):
        inputs = json.loads(kw["messages"][0]["content"][0]["text"])
        # 空の入力にも誤って要点を返すモデルでも、未読ファイルには表示しない。
        return replace(
            bedrock.converse.return_value,
            text=json.dumps({key: row["content"] or points for key, row in inputs.items()}),
        )

    bedrock.converse.side_effect = summarize
    skill._bedrock = bedrock
    monkeypatch.setattr(fanout, "build_summary_client", lambda: bedrock)
    out = skill.run(
        SearchInput(query="飲料の事例を5件見せて" if counted else "飲料の事例を見せて"),
        SkillContext(metadata=META),
    )
    cards = out.answer.split("\n\n")
    for i, file in enumerate(files, 1):
        card = next(card for card in cards if file["permalink"] in card)
        assert (points.splitlines()[0] in card) == (i == 1)
        assert (points.splitlines()[1] in card) == (i == 1)
    assert out.answer.count(points.splitlines()[0]) == 1
    assert "案件名不明" not in out.answer
    assert "*資料*" not in out.answer and "*記録*" not in out.answer
    assert post.replace("000001", "000002") not in out.answer
    assert out.answer.count("|資料を見る>") == (5 if fill and counted else 3)
    if fill:
        assert "FILE4/view" in out.answer and "FILE5/view" in out.answer
    elif counted:
        assert "見つかったのは 3 件です。" in out.answer


@pytest.mark.parametrize("title", ["", "案件名不明", "<https://example.test/title>"])
def test_untitled_candidate_is_skipped_before_counting_and_next_candidates_fill(title):
    model = MagicMock(return_value="{}")
    refs = []
    answer = render(
        [hit(1, title=title), hit(2), hit(3)],
        "飲料",
        lambda h: None,
        requested_count=2,
        summarize=model,
        displayed_refs=refs,
    )
    assert "案件名不明" not in answer and "FILE1/view" not in answer
    assert [ref.drive_file_id for ref in refs] == ["FILE2", "FILE3"]
    assert [h.chunk_id for h in model.call_args.args[0]] == [2, 3]
    assert "見つかったのは" not in answer


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("compact_enabled", [False, True])
@pytest.mark.parametrize("query", ["飲料系の事例を5件出して", "飲料系の事例資料を５件出して"])
def test_five_vault_seventeen_slack_three_cases_keep_five_with_only_three_points(
    monkeypatch,
    fanout_enabled,
    compact_enabled,
    query,
):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setattr(material_files, "file_reader", lambda *a, **kw: None)
    vault = [
        hit(1),
        hit(2),
        hit(11, industry=None, score=0.99),
        hit(12, industry=None, score=0.98),
        hit(1, score=0.8),
    ]
    slack = SlackLookup(
        "ok",
        "ok",
        items=tuple(
            SlackItem(
                hit=SlackHitOut(
                    permalink=f"https://example.slack.com/archives/C0FAKE/p17844240000000{i:02}",
                    file_names=[f"架空飲料企画{i % 6 + 5}.pdf"],
                    file_ids=[f"F0FAKE{i % 6 + 5}"],
                ),
                text=f"架空飲料企画{i % 6 + 5}の資料。",
            )
            for i in range(17)
        ),
    )
    rows = SourceResult(
        source="case",
        status="ok",
        reason="ok",
        items=tuple(
            CaseRow(
                title=f"架空企画{i}",
                source_uri=f"gdrive://ROW{i}",
                client=f"架空社{i}",
                brand="",
                product=f"架空企画{i}",
                effect="商品の実演動画を配信。",
                source="deck",
                kind="case",
                external_use="unknown",
                stage=2,
                in_main=False,
                industry="化粧品" if i == 99 else "飲料",
                updated_at="2026-09-01",
            )
            for i in (3, 4, 99)
        ),
    )
    skill, _ = make(vault)

    def retrieve(*a, **kw):
        kw["fanout_probe"]["case"] = rows
        return vault

    monkeypatch.setattr(skill, "_retrieve", retrieve)
    monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: slack)
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: slack)
    summary = MagicMock(
        return_value=(
            "各件の要点です。\n```json\n"
            + json.dumps(
                {
                    str(i).translate(
                        str.maketrans("123", "１２３")
                    ): "商品の実演動画を配信。\n利用場面を紹介。\nより詳しく見たい資料はありますか？"
                    for i in range(1, 4)
                }
            )
            + "\n```",
            0.001,
        )
    )
    monkeypatch.setattr(skill, "_summarize", summary)
    out = skill.run(
        SearchInput(query=query),
        SkillContext(
            metadata={
                **META,
                compact.CTX_KEY: compact_enabled,
            }
        ),
    )
    assert is_case_list(query)
    assert out.answer_format == "list" and out.found
    cards = out.answer.split("\n\n")
    assert len(cards) == 5
    assert all(card.startswith("*") and "（2026年9月）" in card for card in cards[:4])
    assert all(card.count("https://") == 1 and "|資料を見る>" in card for card in cards)
    assert "ROW99" not in out.answer and "架空企画99" not in out.answer
    assert "FILE11" not in out.answer and "FILE12" not in out.answer
    assert out.answer.count("FILE1/view") == 1
    assert "見つかったのは" not in out.answer
    assert out.answer.count("商品の実演動画を配信。") == 3
    assert out.answer.count("利用場面を紹介。") == 3
    assert all(card.count("• ") <= 3 for card in cards)
    assert "ありますか" not in out.answer and "？" not in out.answer
    assert out.suggested_next is None
    summary.assert_called_once()
    assert summary.call_args.kwargs["case_points"] is True
    assert len(summary.call_args.args[1]) == 5
    assert out.total_cost_usd >= 0.001


def test_selection_filters_industry_client_deduplicates_and_orders_before_model():
    hits = [
        hit(99, industry="化粧品", score=1),
        hit(1, score=0.6),
        hit(2, score=0.9),
        hit(3, score=0.8),
        hit(4, score=0.7),
        hit(5, score=0.65),
        hit(6, industry=None, score=1),
        hit(7, score=1, client_name="架空別社"),
    ]
    hits = [replace(h, metadata={"client_name": "架空指定社", **h.metadata}) for h in hits]
    hits.extend(
        [
            hit(2),
            hit(8, campaign="同一施策", client_name="架空指定社"),
            hit(9, campaign="同一施策", client_name="架空指定社"),
        ]
    )
    model = MagicMock(return_value="{}")
    answer = render(
        hits,
        "飲料",
        lambda h: None,
        requested_count=5,
        asked_industry="飲料",
        client="架空指定社",
        summarize=model,
    )
    selected = model.call_args.args[0]
    assert [h.chunk_id for h in selected] == [2, 8, 3, 4, 5]
    assert answer.count("|資料を見る>") == 5


@pytest.mark.parametrize("response", ["{}", "[]", "invalid JSON", RuntimeError("fake failure")])
def test_only_two_eligible_keeps_cards_and_one_shortfall_line_on_model_failure(response):
    model = MagicMock(
        **(
            {"side_effect": response}
            if isinstance(response, Exception)
            else {"return_value": response}
        )
    )
    answer = render(
        [hit(1), hit(99, industry="化粧品"), hit(2)],
        "飲料",
        lambda h: None,
        requested_count=5,
        asked_industry="飲料",
        summarize=model,
    )
    assert answer.count("|資料を見る>") == 2
    assert answer.count("見つかったのは 2 件です。") == 1
    assert "FILE99" not in answer


def test_case_points_prompt_sends_only_selected_inputs_and_returns_raw_json():
    skill, _ = make([])
    bedrock = _fake_bedrock()
    bedrock.converse.return_value = replace(
        bedrock.converse.return_value, text='{"1":"実演動画。"}'
    )
    text, cost = skill._summarize(
        "飲料事例を5件", [hit(1), hit(2)], "fake-request", bedrock=bedrock, case_points=True
    )
    assert json.loads(text) == {"1": "実演動画。"}
    call = bedrock.converse.call_args.kwargs
    inputs = json.loads(call["messages"][0]["content"][0]["text"])
    assert set(inputs) == {"1", "2"}
    assert inputs["1"]["title"] == "架空企画1"
    assert "最大2行" in call["system"] and "聞き返し" in call["system"]
    assert cost > 0


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "各件の要点です。\n{}\n以上です。"])
@pytest.mark.parametrize("wide_keys", [False, True])
@pytest.mark.parametrize("case_mark", [False, True])
def test_haiku_points_are_bound_to_each_selected_card(wrapper, wide_keys, case_mark):
    points = {
        (
            str(i).translate(str.maketrans("12345", "１２３４５")) if wide_keys else str(i)
        ): f"架空施策{i}の実演動画を配信。\n架空施策{i}の利用場面を紹介。"
        for i in range(1, 6)
    }
    model = MagicMock(return_value=wrapper.format(json.dumps(points, ensure_ascii=False)))
    answer = render(
        [hit(i, **({"case_corpus": "true"} if case_mark else {})) for i in range(1, 6)],
        "飲料",
        lambda h: None,
        requested_count=5,
        asked_industry="飲料",
        summarize=model,
    )
    cards = answer.split("\n\n")
    assert len(cards) == 5
    for i, card in enumerate(cards, 1):
        assert f"架空施策{i}の実演動画を配信。" in card
        assert f"架空施策{i}の利用場面を紹介。" in card
        assert f"FILE{i}/view" in card
        assert card.count("• ") == 3
        assert ("社外利用は要確認" in card) == case_mark
    model.assert_called_once()


@pytest.mark.parametrize(
    ("older", "newer", "old_meta", "new_meta"),
    [
        ("提案_FY27_PR戦略【自社】架空社.pptx", "提案_v3_FY27_PR戦略【自社】架空社.pptx", {}, {}),
        ("提案_架空社_瓶ストーリー施策_v2.pptx", "提案_架空社_瓶ストーリー施策_v10.pptx", {}, {}),
        ("提案_架空社_瓶ストーリー施策.pptx", "提案_架空社_瓶ストーリー施策_最終.pptx", {}, {}),
        ("提案_架空社_瓶ストーリー施策.pptx", "提案_架空社_瓶ストーリー施策_修正版.pptx", {}, {}),
        (
            "提案_260424_架空社_瓶ストーリー施策.pptx",
            "提案_260501_架空社_瓶ストーリー施策.pptx",
            {},
            {},
        ),
        (
            "旧資料.pptx",
            "新資料.pptx",
            {
                "advertiser": "株式会社架空社",
                "campaign": "瓶ストーリー施策",
                "updated_at": "2026-08-01",
            },
            {"advertiser": "架空社", "campaign": "瓶ストーリー施策", "updated_at": "2026-09-01"},
        ),
    ],
)
def test_revision_duplicates_use_newest_and_fill_five(older, newer, old_meta, new_meta):
    model = MagicMock(return_value="{}")
    refs = []
    answer = render(
        [
            hit(1, title=older, score=1, **old_meta),
            *(hit(i, score=0.9 - i / 100) for i in range(3, 7)),
            hit(2, title=newer, score=0.1, **new_meta),
        ],
        "飲料",
        lambda h: None,
        requested_count=5,
        asked_industry="飲料",
        summarize=model,
        displayed_refs=refs,
    )
    assert [h.chunk_id for h in model.call_args.args[0]] == [2, 3, 4, 5, 6]
    assert [ref.drive_file_id for ref in refs] == ["FILE2", "FILE3", "FILE4", "FILE5", "FILE6"]
    assert answer.count("|資料を見る>") == 5
    assert "見つかったのは" not in answer


@pytest.mark.parametrize(
    ("title", "meta", "heading"),
    [
        (
            "レポート_260424ご提出v2_結果分析_架空社_瓶ストーリー施策.pptx",
            {},
            "架空社 瓶ストーリー施策",
        ),
        ("提案_v3_FY27_PR戦略【自社】架空社.pptx", {}, "FY27 PR戦略架空社"),
        ("2026-04-24_提案_架空社_瓶ストーリー施策_最終.pdf", {}, "架空社 瓶ストーリー施策"),
        (
            "元資料.pptx",
            {"advertiser": "架空社", "campaign": "瓶ストーリー施策"},
            "架空社 瓶ストーリー施策",
        ),
        (
            "元資料.pptx",
            {"case_client": "架空社", "case_product": "瓶ストーリー施策"},
            "架空社 瓶ストーリー施策",
        ),
    ],
)
def test_short_case_heading(title, meta, heading):
    answer = render(
        [hit(1, title=title, **meta)],
        "飲料",
        lambda h: None,
        requested_count=1,
        asked_industry="飲料",
        summarize=lambda selected: "{}",
    )
    assert answer.startswith(f"*{heading}*（2026年")
    assert "FILE1/view" in answer


def test_revision_group_keeps_other_clients_campaigns_and_uses_valid_link():
    model = MagicMock(return_value="{}")
    answer = render(
        [
            hit(1, title="提案_架空社_瓶施策.pptx", advertiser="架空社", campaign="瓶施策"),
            hit(
                2,
                title="提案_架空社_瓶施策_v3.pptx",
                advertiser="架空社",
                campaign="瓶施策",
                source_uri="",
            ),
            hit(3, advertiser="架空別社", campaign="瓶施策"),
            hit(4, advertiser="架空社", campaign="別施策"),
        ],
        "飲料",
        lambda h: None,
        requested_count=5,
        asked_industry="飲料",
        summarize=model,
    )
    assert [h.chunk_id for h in model.call_args.args[0]] == [1, 3, 4]
    assert "見つかったのは 3 件です。" in answer


def test_titles_that_shorten_to_nothing_are_not_merged() -> None:
    from teamagent.adapters.pgvector_client import SearchHit
    from teamagent.skills.search.case_list import _case_key

    a = SearchHit(chunk_id=1, content="", score=0.9, metadata={"title": "提案書.pdf"})
    b = SearchHit(chunk_id=2, content="", score=0.9, metadata={"title": "【自社】提案書_v2.pptx"})
    assert _case_key(a) != _case_key(b)
