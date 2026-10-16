"""fan-out 検索の答えの節と「探した範囲」の 1 行（answer_sections）のテスト。

背景（2026-10-14・設計 fanout_design.json の answer_format）: 資料検索 1 回で金庫・本人の Slack・
カルテ・事例・案件・言い換えを並行に引き、各探し先の結果はコードが 1 件 1 行で並べる。最後の
1 行で「見つかった／探して無かった／探せなかった／使わなかった」を書き分ける。

ここで固定すること:
  - 節の並び（📁 🏆 🤝 🗂 💬 🗃 🔎）と、0 件の節を出さないこと
  - 業種はメタデータの正準値だけ・無ければ「業種不明」、事例の社外利用の印（判定できなければ要確認）
  - 探した範囲の各状態（✓・0件・✗・—）。✗ は「0件」と書かない（S3 と同じ死守ライン）
  - answer は 2,500 字以下。超えたときに削る順番（段 1〜4）
  - 利用者の文・ヒットの題名・Slack の抜粋に入った <!channel> などの記法を無害化すること
  - 📊 の節を直接投稿で切り出しても、後ろの節を巻き込まないこと（今の extract_section で）

偽物は本番の形をそのまま使う（SearchHit の metadata は ingest が付ける鍵、Slack の状態は
composite.SlackLookup の理由コード、Slack の一致は SlackHitOut、事例は list_case_studies の行）。
実 DB 0・実 Bedrock 0・実 Slack 0。
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.mcp_gateway import payload_offload
from teamagent.mcp_gateway.direct_video_metrics import extract_section
from teamagent.skills._shared.next_step import DELIVER_SUGGESTION
from teamagent.skills.search import answer_sections as sec
from teamagent.skills.search.answer_sections import (
    ANSWER_MAX_CHARS,
    CASE_NG_MARK,
    CASE_UNKNOWN_MARK,
    DEAL_MARK,
    SCOPE_PREFIX,
    Entry,
    LaneStatus,
    Sections,
    build_sections,
    case_external_use_mark,
    classify_hit,
    compose_answer,
    lane_from_slack,
    render_sections,
    scope_line,
)
from teamagent.skills.search.campaign_files import VIDEO_METRICS_HEADING
from teamagent.skills.search.composite import SlackItem, SlackLookup
from teamagent.skills.search.schema import SlackHitOut

# ── 本番の形の材料 ───────────────────────────────────────────────────────────

DRIVE_URL = "https://drive.google.com/file/d/1PropFileId/view"
SLACK_PERMALINK = "https://vectorinc.slack.com/archives/C0PROJ01/p1727900000000300"


def _hit(score: float, chunk_id: int = 1, content: str = "本文", **meta: Any) -> SearchHit:
    return SearchHit(chunk_id=chunk_id, content=content, score=score, metadata=dict(meta))


def _proposal(score: float = 0.9, **extra: Any) -> SearchHit:
    """Drive の提案書（ingest が付ける cls_industry・gdrive の source_uri）。"""
    meta: dict[str, Any] = {
        "title": "宅配サービス_TikTok提案書_20260812.pptx",
        "source_uri": "gdrive://1PropFileId",
        "source_type": "gdrive",
        "cls_industry": "食品",
        "cls_doc_type": "提案書",
        "updated_at": "2026-09-01",
    }
    meta.update(extra)
    return _hit(score, **meta)


def _case_deck(external_use: str | None = "ng", score: float = 0.8) -> SearchHit:
    """事例集 PPTX を事例ごとに切った文書（ingest/case_deck.py の metadata）。"""
    meta: dict[str, Any] = {
        "title": "ショート動画事例集_2026.pptx",
        "source_uri": "gdrive://1CaseDeck",
        "case_corpus": "true",
        "case_source": "deck",
        "client_name": "北都フーズ",
        "case_brand": "まるごと宅配便",
        "case_effect": "投稿 12 本・合計 340 万回再生",
        "case_industry": "食品",
    }
    if external_use is not None:
        meta["case_external_use"] = external_use
    return _hit(score, **meta)


def _campaign(score: float = 0.7) -> SearchHit:
    """施策実績（campaign_aggregate.campaign_case_metadata の鍵・対外利用は unknown 固定）。"""
    return _hit(
        score,
        title="施策実績 株式会社南海飲料 夏の新作",
        source_uri="https://docs.google.com/spreadsheets/d/1Sheet/edit#gid=0&range=3:3",
        campaign_aggregate="true",
        case_corpus="true",
        case_source="campaign_db",
        client_name="南海飲料",
        case_company="株式会社南海飲料",
        case_product="夏の新作",
        case_effect="投稿 8 本・合計 120 万回再生",
        case_external_use="unknown",
        campaign_industry="飲料",
        updated_at="2026-07-01",
    )


def _deal(score: float = 0.6) -> SearchHit:
    """#proj-01 の案件決定（topic='案件決定'・search_topic_by_terms が文書の metadata を載せる形）。"""
    return _hit(
        score,
        title="#proj-01 案件決定 北都フーズ",
        source_uri="https://vectorinc.slack.com/archives/C0PROJ01/p1727000000000100",
        source_type="slack",
        topic="案件決定",
        channel_name="proj-01",
        updated_at="2026-09-20",
    )


def _sales_fb(
    score: float = 0.5, *, occurred: str = "2026-10-01", phase: str = "提案中", n: int = 2
) -> SearchHit:
    """営業 FB（list_client_timeline_recent の SearchHit の形）。"""
    return _hit(
        score,
        chunk_id=n,
        source_uri=f"https://vectorinc.slack.com/archives/C0SALESFB/p172770000000020{n}",
        source_type="slack",
        title="営業FB",
        occurred_at=occurred,
        is_sales_fb=True,
        client_name="北都フーズ",
        deal_phase=phase,
        next_action="見積の再提出",
    )


def _research(score: float = 0.5) -> SearchHit:
    """X リサーチの保存物（research_persist: source_type='other'・x_research_tool）。"""
    return _hit(
        score,
        title="Xリサーチ まるごと宅配便 2026-10-02",
        source_uri="https://example.test/research/1",
        source_type="other",
        cls_project="まるごと宅配便",
        cls_solution="Xリサーチ",
        x_research_tool="x_voice_search",
    )


def _near(score: float = 0.5) -> SearchHit:
    """言い換えレーンのヒット（業種のエイリアス『物流』→ 正準『運輸・交通』）。"""
    return _hit(
        score,
        title="物流A社 ショート動画提案書",
        source_uri="gdrive://1NearFile",
        cls_industry="物流",
    )


def _case_row(external_use: str | None = "ng", **extra: Any) -> dict[str, Any]:
    """list_case_studies の 1 行（_CASE_COLUMNS の列名）。"""
    row: dict[str, Any] = {
        "title": "ショート動画事例集_2026.pptx",
        "source_uri": "gdrive://1CaseRow",
        "case_client": "北都フーズ",
        "case_product": None,
        "case_effect": "再生 300 万回",
        "case_external_use": external_use,
        "case_source": "deck",
        "case_brand": "まるごと宅配便",
        "case_industry": "食品",
        "updated_at": "2026-06-01",
    }
    row.update(extra)
    return row


def _slack_hit(
    n: int = 1, excerpt: str = "宅配の事例の件、北都さんの資料を共有します"
) -> SlackHitOut:
    return SlackHitOut(
        channel="#proj-01",
        posted_on="2026-10-03",
        excerpt=excerpt,
        permalink=f"https://vectorinc.slack.com/archives/C0PROJ01/p172790000000030{n}",
    )


def _url_of(hit: SearchHit) -> str | None:
    """search の _hit_url の代わり（gdrive:// と https をそのまま整形する）。"""
    uri = str(hit.metadata.get("source_uri") or "")
    if uri.startswith("gdrive://"):
        return f"https://drive.google.com/file/d/{uri.removeprefix('gdrive://')}/view"
    return uri or None


def _all_sections(**overrides: Any) -> Sections:
    kwargs: dict[str, Any] = {
        "vault_hits": [_proposal(), _case_deck(), _campaign(), _deal(), _sales_fb(), _research()],
        "expand_hits": [_near()],
        "slack_hits": [_slack_hit()],
        "url_of": _url_of,
        "karte_client": "北都フーズ",
        "expand_subject": "宅配",
    }
    kwargs.update(overrides)
    return build_sections(**kwargs)


LANES_ALL: dict[str, LaneStatus] = {
    "vault": LaneStatus.found(6, queries=["宅配 事例"]),
    "expand": LaneStatus.found(1, queries=["物流 事例", "配送 事例"]),
    "slack": LaneStatus.found(1),
    "karte": LaneStatus.found(1, queries=["北都フーズ"]),
    "case": LaneStatus.zero(queries=["北都フーズ"]),
    "sns": LaneStatus.skipped("not_auto"),
}

HEADERS = {
    "vault": "📁 ",
    "case": "🏆 ",
    "deal": "🤝 ",
    "karte": "🗂 ",
    "slack": "💬 ",
    "research": "🗃 ",
    "expand": "🔎 ",
}


def _section_counts(answer: str) -> dict[str, int]:
    """答えの節ごとの行数（見出しの直後の「- 」の行を数える）。出ていない節は 0。"""
    counts = dict.fromkeys(HEADERS, 0)
    current: str | None = None
    for line in answer.split("\n"):
        key = next((k for k, h in HEADERS.items() if line.startswith(h)), None)
        if key is not None:
            current = key
            continue
        if current is not None and line.startswith("- "):
            counts[current] += 1
        else:
            current = None
    return counts


_MD_LINK = re.compile(r"\[([^\[\]\n]*)\]\(([^()\s]*)\)")


# ── 並びと 0 件の節 ─────────────────────────────────────────────────────────


def test_sections_follow_fixed_order_and_scope_line_is_last() -> None:
    answer = compose_answer(
        head="要約です。", sections=_all_sections(), lanes=LANES_ALL, tail=DELIVER_SUGGESTION
    )
    positions = [answer.index("\n" + HEADERS[key]) for key in sec.SECTION_ORDER]
    assert positions == sorted(positions)
    assert answer.startswith("要約です。\n\n📁 金庫の資料（1件）\n")
    # tail（📎 実ファイルの提案）→ 探した範囲（最後の 1 行）。
    last_lines = answer.split("\n\n")[-2:]
    assert last_lines[0] == DELIVER_SUGGESTION
    assert last_lines[1].startswith(SCOPE_PREFIX)
    assert "\n" not in last_lines[1]


def test_sections_with_no_entries_are_not_rendered() -> None:
    sections = build_sections(vault_hits=[_proposal()], url_of=_url_of)
    answer = compose_answer(head="要約", sections=sections, lanes={"vault": LaneStatus.found(1)})
    assert _section_counts(answer) == {
        "vault": 1,
        "case": 0,
        "deal": 0,
        "karte": 0,
        "slack": 0,
        "research": 0,
        "expand": 0,
    }
    for key in ("case", "deal", "karte", "slack", "research", "expand"):
        assert HEADERS[key] not in answer
    assert render_sections(Sections()) == ""


def test_scope_line_is_always_added_even_without_head_or_sections() -> None:
    answer = compose_answer(
        head="", sections=Sections(), lanes={"vault": LaneStatus.zero(queries=["宅配 事例"])}
    )
    assert answer == "探した範囲（まとめて検索）: 金庫 0件（『宅配 事例』）"
    assert (
        compose_answer(head="", sections=Sections(), lanes={})
        == "探した範囲（まとめて検索）: 記録なし"
    )


def test_each_entry_is_one_line_with_date_industry_and_link() -> None:
    answer = render_sections(_all_sections())
    assert (
        "📁 金庫の資料（1件）\n"
        f"- [宅配サービス_TikTok提案書_20260812.pptx]({DRIVE_URL})（2026-08-12・業種: 食品）"
    ) in answer
    assert (
        "🤝 案件（決定・進行中の案件。実績ではありません）\n"
        "- [#proj-01 案件決定 北都フーズ]"
        "(https://vectorinc.slack.com/archives/C0PROJ01/p1727000000000100)（2026-09-20）"
    ) in answer
    assert (
        "💬 Slack（あなたの Slack・1件）\n"
        "- [#proj-01](https://vectorinc.slack.com/archives/C0PROJ01/p1727900000000301)"
        "（2026-10-03） 「宅配の事例の件、北都さんの資料を共有します」"
    ) in answer
    # 1 件 1 行: 見出し以外の行は「- 」で始まり、リンクは本番と同じ Markdown の形。
    for block in answer.split("\n\n"):
        lines = block.split("\n")
        assert all(line.startswith("- ") for line in lines[1:])
        for line in lines[1:]:
            assert _MD_LINK.search(line), line


def test_top_two_per_section_and_min_score_drops_weak_hits() -> None:
    hits = [
        _proposal(0.9),
        _proposal(0.8, title="宅配B_提案書.pptx", source_uri="gdrive://1B"),
        _proposal(0.7, title="宅配C_提案書.pptx", source_uri="gdrive://1C"),
        _proposal(0.29, title="弱いヒット.pptx", source_uri="gdrive://1Weak"),
    ]
    sections = build_sections(vault_hits=hits, url_of=_url_of, min_score=0.3)
    assert [e.title for e in sections.of("vault")] == [
        "宅配サービス_TikTok提案書_20260812.pptx",
        "宅配B_提案書.pptx",
        "宅配C_提案書.pptx",
    ]
    answer = render_sections(sections)
    assert "📁 金庫の資料（2件）" in answer
    assert "宅配C_提案書" not in answer  # 3 件目は載せない（上位 2 件）
    assert "弱いヒット" not in answer
    # 言い換えのヒットにも同じしきい値を掛ける。
    weak_near = build_sections(expand_hits=[_near(0.2)], url_of=_url_of, min_score=0.3)
    assert weak_near.of("expand") == ()


def test_same_document_is_listed_once_in_the_earlier_section() -> None:
    # 同じ資料の別チャンクが金庫と言い換えの両方に出た → 📁 だけに 1 行、🔎 は出さない。
    sections = build_sections(
        vault_hits=[_proposal(0.9, chunk_id=1), _proposal(0.85, chunk_id=2)],
        expand_hits=[_proposal(0.6, chunk_id=3)],
        url_of=_url_of,
        expand_subject="宅配",
    )
    answer = render_sections(sections)
    assert answer.count(DRIVE_URL) == 1
    assert _section_counts(answer)["vault"] == 1
    assert "🔎" not in answer


# ── 分類（metadata だけで決める）────────────────────────────────────────────


@pytest.mark.parametrize(
    ("meta", "key"),
    [
        ({"campaign_aggregate": "true", "case_corpus": "true"}, "case"),
        ({"case_corpus": "true", "case_source": "deck"}, "case"),
        ({"topic": "案件決定"}, "deal"),
        ({"is_sales_fb": True}, "karte"),
        ({"is_sales_fb": "true"}, "karte"),
        ({"x_research_tool": "x_voice_search", "source_type": "other"}, "research"),
        ({"source_type": "other"}, "vault"),
        ({"topic": "議事録"}, "vault"),
        ({"case_corpus": "false"}, "vault"),
        ({}, "vault"),
    ],
)
def test_classify_hit_by_metadata(meta: dict[str, Any], key: str) -> None:
    assert classify_hit(meta) == key


def test_karte_section_uses_timeline_newest_first() -> None:
    timeline = [  # list_client_timeline_recent は古い順で返す
        _sales_fb(occurred="2026-09-01", phase="初回訪問", n=1),
        _sales_fb(occurred="2026-10-01", phase="提案中", n=2),
    ]
    sections = build_sections(karte_hits=timeline, url_of=_url_of, karte_client="北都フーズ")
    answer = render_sections(sections)
    assert answer.startswith("🗂 北都フーズのカルテ（最新2件）\n- [提案中]")
    lines = answer.split("\n")
    assert lines[1].endswith("（2026-10-01） 次: 見積の再提出")
    assert lines[2].startswith("- [初回訪問]")
    # 取引先の名指しが無いときは営業 FB の記録として出す（取引先名は行に入る）。
    no_client = render_sections(build_sections(vault_hits=[_sales_fb()], url_of=_url_of))
    assert no_client.startswith("🗂 営業FBの記録（1件）\n- [北都フーズ 提案中]")


def test_research_section_header() -> None:
    answer = render_sections(build_sections(vault_hits=[_research()], url_of=_url_of))
    assert answer.startswith(
        "🗃 過去の調べもの（X・TikTok の調査結果）\n- [Xリサーチ まるごと宅配便"
    )


# ── 業種（メタデータの正準値だけ）─────────────────────────────────────────────


def test_industry_comes_from_metadata_only() -> None:
    sections = build_sections(
        vault_hits=[
            _proposal(cls_industry=None),  # 業種メタなし
            _proposal(0.8, title="B.pptx", source_uri="gdrive://1B", cls_industry="宅配"),
        ],
        expand_hits=[_near()],  # エイリアス「物流」→ 正準「運輸・交通」
        url_of=_url_of,
    )
    answer = render_sections(sections)
    assert "（2026-08-12・業種: 業種不明）" in answer
    assert (
        "[B.pptx](https://drive.google.com/file/d/1B/view)（2026-09-01・業種: 業種不明）" in answer
    )
    assert "（業種: 運輸・交通）" in answer
    # 描画側でも正準化する（推測した業種を直接渡されても書かない）。
    guessed = Sections(entries={"vault": (Entry(title="資料X", industry="宅配っぽい業種"),)})
    assert "業種: 業種不明" in render_sections(guessed)


def test_industry_is_not_written_for_karte_slack_or_deal() -> None:
    answer = render_sections(
        build_sections(vault_hits=[_deal(), _sales_fb()], slack_hits=[_slack_hit()], url_of=_url_of)
    )
    assert "業種" not in answer


# ── 事例の社外利用の印 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "mark"),
    [
        ("ng", CASE_NG_MARK),
        ("NG", CASE_NG_MARK),
        ("unknown", CASE_UNKNOWN_MARK),
        (None, CASE_UNKNOWN_MARK),
        ("", CASE_UNKNOWN_MARK),
        ("要確認", CASE_UNKNOWN_MARK),
        ("ok", ""),
    ],
)
def test_case_external_use_mark_is_fail_closed(value: str | None, mark: str) -> None:
    assert case_external_use_mark(value) == mark


@pytest.mark.parametrize(
    ("external_use", "expected"),
    [("ng", CASE_NG_MARK), ("unknown", CASE_UNKNOWN_MARK), (None, CASE_UNKNOWN_MARK)],
)
def test_case_lines_always_carry_the_external_use_mark(
    external_use: str | None, expected: str
) -> None:
    sections = build_sections(
        vault_hits=[_case_deck(external_use)],
        case_rows=[_case_row(external_use)],
        near_case_rows=[_case_row(external_use, source_uri="gdrive://1NearCase")],
        url_of=_url_of,
        expand_subject="宅配",
    )
    answer = render_sections(sections)
    case_lines = [line for line in answer.split("\n") if line.startswith("- ")]
    assert len(case_lines) == 3  # 🏆 2 件（事例の行・金庫のヒット）＋🔎 の近い業種の事例 1 件
    for line in case_lines:
        assert line.endswith(f" {expected}"), line
    assert (
        "🔎 近い業種・言い換え（『宅配』そのものの資料ではありません）\n- [北都フーズ様：まるごと宅配便]"
        in answer
    )


def test_campaign_hits_are_marked_as_needing_confirmation() -> None:
    answer = render_sections(build_sections(vault_hits=[_campaign()], url_of=_url_of))
    assert answer == (
        "🏆 事例・施策実績（1件）\n"
        "- [施策実績 株式会社南海飲料 夏の新作]"
        "(https://docs.google.com/spreadsheets/d/1Sheet/edit#gid=0&range=3:3)"
        "（2026-07-01・業種: 飲料） 投稿 8 本・合計 120 万回再生 社外利用は要確認"
    )


def test_iter4_same_sheet_campaigns_keep_names_and_deduplicate_the_case_lane() -> None:
    first = _campaign()
    first.metadata["document_id"] = "first-campaign"
    second = SearchHit(
        999,
        "冬の施策",
        0.8,
        {
            **first.metadata,
            "document_id": "second-campaign",
            "title": "施策実績 株式会社南海飲料 冬の新作",
            "case_product": "冬の新作",
        },
    )
    row = {
        "title": first.metadata["title"],
        "source_uri": first.metadata["source_uri"],
        "case_client": "南海飲料",
        "case_product": "夏の新作",
        "case_source": "campaign_db",
    }
    answer = render_sections(build_sections(vault_hits=[first, second], case_rows=[row]))
    assert "🏆 事例・施策実績（2件）" in answer
    assert answer.count("夏の新作") == 1
    assert answer.count("冬の新作") == 1


def test_ok_case_has_no_mark_and_case_in_expand_is_still_marked() -> None:
    ok_case = build_sections(vault_hits=[_case_deck("ok")], url_of=_url_of)
    assert CASE_NG_MARK not in render_sections(ok_case)
    assert CASE_UNKNOWN_MARK not in render_sections(ok_case)
    # 言い換えレーンに事例が出ても印は落とさない（出す節ではなく行の種類で付ける）。
    expanded = build_sections(expand_hits=[_case_deck("ng", score=0.5)], url_of=_url_of)
    assert render_sections(expanded).endswith(CASE_NG_MARK)


def test_deal_outside_its_section_is_marked_not_a_result() -> None:
    answer = render_sections(build_sections(expand_hits=[_deal(0.5)], url_of=_url_of))
    assert answer.split("\n")[1].endswith(f" {DEAL_MARK}")
    in_own_section = render_sections(build_sections(vault_hits=[_deal()], url_of=_url_of))
    assert DEAL_MARK not in in_own_section
    assert "実績ではありません" in in_own_section.split("\n")[0]


# ── 見出し ────────────────────────────────────────────────────────────────


def test_expand_header_without_subject() -> None:
    answer = render_sections(build_sections(expand_hits=[_near()], url_of=_url_of))
    assert answer.startswith("🔎 近い業種・言い換え（問いそのものの資料ではありません）\n")


def test_near_case_comes_first_in_expand_then_expanded_hits() -> None:
    sections = build_sections(
        expand_hits=[_near()],
        near_case_rows=[_case_row("unknown"), _case_row("unknown", source_uri="gdrive://1Case2")],
        url_of=_url_of,
    )
    titles = [e.title for e in sections.of("expand")]
    assert titles == [
        "北都フーズ様：まるごと宅配便",
        "物流A社 ショート動画提案書",
        "北都フーズ様：まるごと宅配便",
    ]


# ── 無害化 ────────────────────────────────────────────────────────────────

_HOSTILE = "<!channel> 至急 <@U012AB3CD> [偽](https://evil.example) *太字* `x` |y\n2行目"


def test_special_markup_in_titles_excerpts_and_names_is_neutralized() -> None:
    sections = build_sections(
        vault_hits=[_proposal(title=_HOSTILE)],
        slack_hits=[_slack_hit(excerpt="<!here> <https://evil.example|資料はこちら>")],
        url_of=_url_of,
        karte_client="<@U999>北都",
        expand_subject="<!everyone>宅配",
        expand_hits=[_near()],
    )
    lanes = {
        "vault": LaneStatus.found(1, queries=["<!channel> 宅配\n事例", "『宅配』"]),
        "slack": LaneStatus.found(1),
    }
    answer = compose_answer(head="要約", sections=sections, lanes=lanes)
    for bad in ("<!", "<@", "<https", "](https://evil", "`", "*太字*", "|y"):
        assert bad not in answer, bad
    # 我々が組んだリンクだけが Markdown のリンクになる（宛先は渡した URL だけ）。
    allowed = {
        DRIVE_URL,
        SLACK_PERMALINK[:-1] + "1",
        "https://drive.google.com/file/d/1NearFile/view",
    }
    assert {url for _, url in _MD_LINK.findall(answer)} <= allowed
    # 題名の改行は 1 行に潰す（1 件 1 行が崩れない）。
    assert not any(line.startswith("2行目") for line in answer.split("\n"))
    assert _section_counts(answer)["vault"] == 1
    # 取引先名・主題語は見出しに無害化して入る。
    assert "🔎 近い業種・言い換え（『＜!everyone＞宅配』そのものの資料ではありません）" in answer
    # 問いの語の『』は外して 1 組だけで囲む。
    assert answer.endswith(
        "探した範囲（まとめて検索）: 金庫 ✓1件（『＜!channel＞ 宅配 事例』『宅配』）・Slack ✓1件"
        "・Slack: 公開チャンネル全体＋参加している非公開・DM"
    )


def test_query_echo_masks_pii_like_scope_footer() -> None:
    line = scope_line({"vault": LaneStatus.zero(queries=["taro.yamada@example.com の資料"])})
    assert "taro.yamada@example.com" not in line
    assert line.startswith("探した範囲（まとめて検索）: 金庫 0件（『")


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://x.example/a b",
        "https://x.example/a)b",
        "https://x.example/a(b",
        "https://x.example/a|b",
        "https://x.example/<b>",
        "https://x.example/a]b",
        "ftp://x.example/file",
        "https:///nohost",
        "https://x.example/" + "a" * 600,
        "slack://C0PROJ01/1727000000.000100",
    ],
)
def test_unsafe_urls_are_shown_as_text_only(url: str) -> None:
    sections = Sections(entries={"vault": (Entry(title="資料A", url=url, date="2026-10-01"),)})
    line = render_sections(sections).split("\n")[1]
    assert line == "- 資料A（2026-10-01・業種: 業種不明）"


def test_dates_are_only_shown_when_well_formed() -> None:
    entries = (
        Entry(title="A", date="2026-10-03 12:34"),
        Entry(title="B", date="2026-13-01"),
    )
    lines = render_sections(Sections(entries={"slack": entries})).split("\n")
    assert lines[1] == "- A（2026-10-03）"
    assert lines[2] == "- B"


# ── 探した範囲の各状態 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "status", "text"),
    [
        ("vault", LaneStatus.found(3, queries=["宅配 事例"]), "金庫 ✓3件（『宅配 事例』）"),
        (
            "vault",
            LaneStatus.found(2, queries=["株式会社北都フーズ", "北都フーズ"], partial=True),
            "金庫 ✓2件（『株式会社北都フーズ』『北都フーズ』・再検索の一部は時間切れ・失敗）",
        ),
        ("vault", LaneStatus.zero(queries=["宅配 事例"]), "金庫 0件（『宅配 事例』）"),
        (
            "vault",
            LaneStatus.failed("search_failed"),
            "金庫 ✗ 検索に失敗しました（金庫に無いという意味ではありません）",
        ),
        (
            "expand",
            LaneStatus.found(2, queries=["物流 事例", "配送 事例"]),
            "言い換え ✓2件（『物流 事例』『配送 事例』）",
        ),
        ("expand", LaneStatus.zero(), "言い換え 0件"),
        ("expand", LaneStatus.failed("timeout"), "言い換え ✗ 時間切れ"),
        (
            "expand",
            LaneStatus.skipped("not_shown"),
            "言い換え —（主な結果で足りたため載せていません）",
        ),
        ("slack", LaneStatus.found(2), "Slack ✓2件"),
        ("slack", LaneStatus.zero(queries=["宅配 事例"]), "Slack 0件（『宅配 事例』）"),
        (
            "slack",
            LaneStatus.failed("timeout"),
            "Slack ✗ 時間切れ（Slack に無いという意味ではありません）",
        ),
        (
            "slack",
            LaneStatus.failed("not_connected"),
            "Slack ✗ 未連携（@Aico に「連携」と話しかけると一緒に探せます）",
        ),
        (
            "slack",
            LaneStatus.failed("reconnect_required"),
            "Slack ✗ 連携切れ（@Aico に「連携」と話しかけて連携し直してください）",
        ),
        ("karte", LaneStatus.found(3, queries=["北都フーズ"]), "カルテ ✓最新3件（『北都フーズ』）"),
        ("karte", LaneStatus.zero(queries=["北都フーズ"]), "カルテ 0件（『北都フーズ』）"),
        ("karte", LaneStatus.skipped("no_client"), "カルテ —（取引先の名指しがないため）"),
        (
            "karte",
            LaneStatus.failed("search_failed"),
            "カルテ ✗ 検索に失敗しました（カルテに無いという意味ではありません）",
        ),
        ("case", LaneStatus.found(1), "事例 ✓1件"),
        ("case", LaneStatus.skipped("no_industry"), "事例 —（業種が特定できないため）"),
        (
            "case",
            LaneStatus.failed("timeout"),
            "事例 ✗ 時間切れ（事例集に無いという意味ではありません）",
        ),
        (
            "sns",
            LaneStatus.skipped("not_auto"),
            "X・TikTok —（自動では探していません。「X の声も見て」と言えば調べます）",
        ),
    ],
)
def test_scope_line_states(name: str, status: LaneStatus, text: str) -> None:
    if name == "slack" and status.kind in ("found", "zero"):
        text += "・Slack: 公開チャンネル全体＋参加している非公開・DM"
    assert scope_line({name: status}) == SCOPE_PREFIX + text


def test_scope_line_order_is_fixed_and_counts_deals() -> None:
    lanes = dict(reversed(list(LANES_ALL.items())))  # 渡す順に依らない
    assert scope_line(lanes, deal_count=1) == (
        "探した範囲（まとめて検索）: 金庫 ✓6件（『宅配 事例』・うち案件 1件）"
        "・言い換え ✓1件（『物流 事例』『配送 事例』）"
        "・Slack ✓1件"
        "・カルテ ✓最新1件（『北都フーズ』）"
        "・事例 0件（『北都フーズ』）"
        "・X・TikTok —（自動では探していません。「X の声も見て」と言えば調べます）"
        "・Slack: 公開チャンネル全体＋参加している非公開・DM"
    )
    # compose_answer は 🤝 の件数から「うち案件」を数える。
    answer = compose_answer(head="要約", sections=_all_sections(), lanes=LANES_ALL)
    assert "金庫 ✓6件（『宅配 事例』・うち案件 1件）" in answer.rsplit("\n", 1)[-1]


def test_deal_count_counts_the_same_document_once() -> None:
    sections = build_sections(
        vault_hits=[_deal(0.7), _hit(0.6, chunk_id=2, **_deal().metadata)], url_of=_url_of
    )
    answer = compose_answer(head="要約", sections=sections, lanes={"vault": LaneStatus.found(2)})
    assert answer.endswith("探した範囲（まとめて検索）: 金庫 ✓2件（うち案件 1件）")
    assert _section_counts(answer)["deal"] == 1


@pytest.mark.parametrize(
    "reason",
    ["timeout", "search_failed", "internal_error", "no_identity", "not_connected", "x"],
)
def test_failed_lane_is_never_written_as_zero(reason: str) -> None:
    text = scope_line({"slack": LaneStatus.failed(reason)})
    assert "✗" in text
    assert "0件" not in text


def test_unknown_reason_codes_use_fixed_words_not_the_code() -> None:
    failed = scope_line({"vault": LaneStatus.failed("psycopg.errors.QueryCanceled: secret")})
    assert (
        failed
        == "探した範囲（まとめて検索）: 金庫 ✗ 探せませんでした（金庫に無いという意味ではありません）"
    )
    skipped = scope_line({"case": LaneStatus.skipped("<!channel>")})
    assert skipped == "探した範囲（まとめて検索）: 事例 —（条件に合わず使っていません）"


def test_found_with_zero_count_is_zero() -> None:
    assert LaneStatus.found(0, queries=["a"]) == LaneStatus.zero(queries=["a"])
    assert "✓0件" not in scope_line({"vault": LaneStatus.found(0)})


def test_queries_are_capped_and_deduplicated() -> None:
    status = LaneStatus.found(1, queries=["a", "a", "b", "c", "d", "x" * 80])
    text = scope_line({"vault": status})
    assert text == "探した範囲（まとめて検索）: 金庫 ✓1件（『a』『b』『c』）"


# ── Slack の状態（composite.SlackLookup の理由コード）──────────────────────────


def _slack_item() -> SlackItem:
    return SlackItem(hit=_slack_hit(), text="本文")


@pytest.mark.parametrize(
    ("lookup", "expected"),
    [
        (SlackLookup(status="ok", reason="ok", items=(_slack_item(),)), LaneStatus.found(1)),
        (
            SlackLookup(status="ok", reason="ok"),
            LaneStatus.zero(queries=("宅配 事例",)),
        ),
        (
            SlackLookup(status="not_connected", reason="not_connected"),
            LaneStatus.failed("not_connected"),
        ),
        (
            SlackLookup(status="not_connected", reason="reconnect_required"),
            LaneStatus.failed("reconnect_required"),
        ),
        (SlackLookup(status="error", reason="timeout"), LaneStatus.failed("timeout")),
        (SlackLookup(status="error", reason="search_failed"), LaneStatus.failed("search_failed")),
        (SlackLookup(status="error", reason="internal_error"), LaneStatus.failed("search_failed")),
        (SlackLookup(status="error", reason="no_identity"), LaneStatus.failed("no_identity")),
    ],
)
def test_lane_from_slack_maps_lookup_states(lookup: SlackLookup, expected: LaneStatus) -> None:
    assert lane_from_slack(lookup, query="宅配の事例を教えて") == expected


def test_lane_from_slack_none_means_slack_not_searched() -> None:
    assert lane_from_slack(None, query="宅配") is None


def test_lane_from_slack_takes_over_status_note_roles() -> None:
    # ℹ️ 未連携（直し方）・ℹ️ 失敗（無いという意味ではない）・🔒 非公開の一致 を 1 行で言う。
    not_connected = lane_from_slack(SlackLookup(status="not_connected", reason="not_connected"))
    failed = lane_from_slack(SlackLookup(status="error", reason="search_failed"))
    hidden = lane_from_slack(SlackLookup(status="ok", reason="ok", hidden_count=2), query="宅配")
    assert not_connected is not None and failed is not None and hidden is not None
    assert "「連携」と話しかけると一緒に探せます" in scope_line({"slack": not_connected})
    assert "Slack に無いという意味ではありません" in scope_line({"slack": failed})
    assert scope_line({"slack": hidden}) == (
        "探した範囲（まとめて検索）: Slack 0件（『宅配』・非公開の場所の一致 2件は DM でだけ出せます）"
        "・Slack: 公開チャンネル全体＋参加している非公開・DM"
    )


# ── 字数（2,500 字以下）と削る順番 ─────────────────────────────────────────────


def test_limit_matches_payload_offload_summary_cap() -> None:
    # 退避で切られても全部残る長さ（要約系は 500×5 まで残る）。
    assert ANSWER_MAX_CHARS == payload_offload._DEFAULT_FIELD_CHARS * 5 == 2500


def _two_per_section() -> Sections:
    def many(prefix: str, kind: Any, **kw: Any) -> tuple[Entry, ...]:
        return tuple(
            Entry(
                title=f"{prefix}{i}_" + "長い資料名" * 6,
                url=f"https://drive.google.com/file/d/{prefix}{i}/view",
                date="2026-10-01",
                industry="食品",
                detail="詳細" * 20,
                kind=kind,
                **kw,
            )
            for i in range(2)
        )

    return Sections(
        entries={
            "vault": many("V", "vault"),
            "case": many("C", "case", case_external_use="ng"),
            "deal": many("D", "deal"),
            "karte": many("K", "karte"),
            "slack": many("S", "slack"),
            "research": many("R", "research"),
            "expand": many("E", "expand"),
        },
        karte_client="北都フーズ",
        expand_subject="宅配",
    )


def test_trim_order_is_fixed() -> None:
    """1 字ずつ上限を下げて、どの節が先に削られるかの順番を固定する。"""
    sections = _two_per_section()
    head = "要約です。"
    full = compose_answer(head=head, sections=sections, lanes=LANES_ALL, limit=100_000)
    states = [_section_counts(full)]
    limit = len(full) - 1
    while True:
        answer = compose_answer(head=head, sections=sections, lanes=LANES_ALL, limit=limit)
        assert len(answer) <= limit
        counts = _section_counts(answer)
        if counts != states[-1]:
            states.append(counts)
        if not any(counts.values()):
            break
        limit = len(answer) - 1
    order = ["expand", "research", "slack", "karte", "deal", "case", "vault"]
    expected = [dict.fromkeys(order, 2)]
    for key in order:  # 段 1: 各節を 2→1 件
        expected.append({**expected[-1], key: 1})
    for key in order:  # 段 2（🔎）・段 3: 節ごと外す
        expected.append({**expected[-1], key: 0})
    assert states == expected


def test_answer_never_exceeds_limit_and_keeps_tail_and_scope() -> None:
    long_head = "要約の文です。" * 600  # 要約・📊 だけで上限を超える異常時
    answer = compose_answer(
        head=long_head, sections=_two_per_section(), lanes=LANES_ALL, tail=DELIVER_SUGGESTION
    )
    assert len(answer) <= ANSWER_MAX_CHARS
    assert answer.endswith(scope_line(LANES_ALL, deal_count=2))
    assert DELIVER_SUGGESTION in answer
    assert "…" in answer
    assert not any(answer.startswith(h) for h in HEADERS.values())


def test_typical_answer_fits_without_trimming() -> None:
    answer = compose_answer(
        head="要約です。" * 100, sections=_all_sections(), lanes=LANES_ALL, tail=DELIVER_SUGGESTION
    )
    assert len(answer) <= ANSWER_MAX_CHARS
    counts = _section_counts(answer)
    assert all(counts[key] >= 1 for key in sec.SECTION_ORDER)


def test_head_is_clipped_at_line_boundary_without_breaking_links() -> None:
    link_line = "- [資料A の長い名前](https://drive.google.com/file/d/AAAAAAAAAAAAAAAA/view)"
    head = "要約です。\n" + "\n".join([link_line] * 60)
    answer = compose_answer(head=head, sections=Sections(), lanes={"vault": LaneStatus.found(60)})
    assert len(answer) <= ANSWER_MAX_CHARS
    body = answer.rsplit("\n\n", 1)[0]
    assert body.endswith("…")
    for line in body.removesuffix("…").rstrip().split("\n")[1:]:
        assert line == link_line  # 途中で切れたリンクの行を残さない


def test_head_clip_never_leaves_a_partial_url_or_angle_link() -> None:
    head = "要約 " + " ".join(["https://drive.google.com/file/d/BBBBBBBBBBBBBBBBBBBB/view"] * 80)
    answer = compose_answer(head=head, sections=Sections(), lanes={"vault": LaneStatus.found(1)})
    body = answer.rsplit("\n\n", 1)[0]
    for token in body.removesuffix("…").split():
        if token.startswith("http"):
            assert token == "https://drive.google.com/file/d/BBBBBBBBBBBBBBBBBBBB/view"
    angle = "要約 " + "<https://example.test/x|ラベル> " * 200
    clipped = compose_answer(head=angle, sections=Sections(), lanes={"vault": LaneStatus.found(1)})
    body = clipped.rsplit("\n\n", 1)[0]
    assert body.count("<") == body.count(">")


# ── 📊 の直接投稿の切り出しと合わせる（mcp_gateway.direct_video_metrics）─────────────


def test_video_metrics_section_does_not_swallow_following_sections() -> None:
    video = (
        f"{VIDEO_METRICS_HEADING}\n"
        "株式会社南海飲料 / 夏の新作（投稿 8 本・合計 120 万 再生）\n"
        "1. 507,000 再生・保存 120　夏の新作 https://www.tiktok.com/@a/video/1"
    )
    head = (
        "要約です。\n\n📎 施策のレポート・提案書\n- 南海飲料 夏の新作: 『報告書』 https://x.test/r\n\n"
        + video
    )
    answer = compose_answer(
        head=head, sections=_all_sections(), lanes=LANES_ALL, tail=DELIVER_SUGGESTION
    )
    cut = extract_section(answer)
    assert cut is not None
    _before, section, after = cut
    assert section == video  # 📊 の節だけを切り出す（📁 以降を DM への直接投稿に巻き込まない）
    assert after.lstrip("\n").startswith("📁 金庫の資料")
    assert after.rstrip().endswith(scope_line(LANES_ALL, deal_count=1))


@pytest.mark.parametrize("required", [False, True])
def test_iter3_metrics_survive_long_head_with_or_without_named_files(required: bool) -> None:
    video = f"{VIDEO_METRICS_HEADING}\n施策 A\n1. 331,300 再生・保存 80　洋食 https://x.test/v/1"
    sections = Sections(
        entries={"vault": [Entry(title="報告書", url="https://x.test/report", kind="vault")]},
        required_counts={"vault": 1} if required else {},
    )
    answer = compose_answer(
        head="説明です。" * 600 + "\n\n" + video,
        sections=sections,
        lanes={"vault": LaneStatus.found(1)},
        limit=500,
    )
    cut = extract_section(answer)
    assert cut is not None and cut[1] == video
    assert len(answer) <= 500
    if required:
        assert "https://x.test/report" in answer


def test_iter3_metrics_survive_named_files_and_optional_lane_pressure() -> None:
    video = f"{VIDEO_METRICS_HEADING}\n施策 A\n1. 331,300 再生・保存 80　洋食 https://x.test/v/1"
    base = _two_per_section()
    named = [
        Entry(
            title=f"報告書{i}" + "長い資料名" * 12,
            url=f"https://drive.google.com/file/d/named{i}/view",
            detail="報告書",
            kind="vault",
        )
        for i in range(8)
    ]
    sections = Sections(
        entries={**base.entries, "vault": named}, required_counts={"vault": len(named)}
    )
    answer = compose_answer(
        head="説明です。" * 600 + "\n\n" + video, sections=sections, lanes=LANES_ALL
    )
    cut = extract_section(answer)
    assert cut is not None and cut[1] == video
    assert all(entry.url in answer for entry in named)
    assert len(answer) <= ANSWER_MAX_CHARS


def test_sections_after_video_metrics_use_heading_boundary_without_extra_blank_line() -> None:
    video = f"{VIDEO_METRICS_HEADING}\n施策 A\n1. 507,000 再生・保存 120　洋食 https://x.test/v/1"
    answer = compose_answer(
        head="要約です。\n\n" + video, sections=_all_sections(), lanes=LANES_ALL
    )
    cut = extract_section(answer)
    assert cut is not None and cut[1] == video
    assert f"{video}\n\n📁 金庫の資料" in answer
    assert "\n\n\n" not in answer


def test_without_video_metrics_sections_follow_head_with_one_blank_line() -> None:
    answer = compose_answer(head="要約です。", sections=_all_sections(), lanes=LANES_ALL)
    assert "要約です。\n\n📁" in answer
    assert "\n\n\n" not in answer


# ── 組み立ての部品 ─────────────────────────────────────────────────────────


def test_case_row_entry_from_list_case_studies() -> None:
    sections = build_sections(case_rows=[_case_row("ng")])
    assert render_sections(sections) == (
        "🏆 事例・施策実績（1件）\n"
        "- [北都フーズ様：まるごと宅配便](https://drive.google.com/file/d/1CaseRow/view)"
        "（2026-06-01・業種: 食品） 再生 300 万回 社外展開NG（社内のみ）"
    )


def test_default_url_uses_source_url_rules() -> None:
    # url_of を渡さなければ source_url.hit_doc_url（gdrive:// → Drive の view URL）。
    sections = build_sections(vault_hits=[_proposal()])
    assert f"]({DRIVE_URL})" in render_sections(sections)
