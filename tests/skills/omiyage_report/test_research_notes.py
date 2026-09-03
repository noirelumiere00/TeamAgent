"""設計C（2026-09-03）: research_notes（先行調査メモ）の受け口・反映・上限・description のトリガー文言。

- 出典URL（https）付きの行だけを「先行調査」D スライドとして露出シェア導入の直後に併記する。
- 出典の無い主張は採用しない（捏造防止）。H の再掲行（最大6）には含めない。
- 空なら構成は従来と完全に同一。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.omiyage_report.contract import SlideDataD, SlideDataH
from teamagent.skills.omiyage_report.deck_plan import (
    build_audit,
    build_deck_plan,
    parse_research_notes,
)
from teamagent.skills.omiyage_report.fmt.contract import validate_deck_content
from teamagent.skills.omiyage_report.fmt.editable import EDIT_MARKER
from teamagent.skills.omiyage_report.fmt.spec import load_fmt_spec
from teamagent.skills.omiyage_report.schema import (
    RESEARCH_NOTES_MAX_CHARS,
    OmiyageReportSubmitInput,
)
from teamagent.skills.omiyage_report.skill import OmiyageReportSubmitSkill

from .test_deck import _analysis, _measurement
from .test_integration import _KaoLikeSearcher, _ReleasedLauncher, _Slack, _Uploader

_NOTES = "\n".join(
    [
        "- 生活者の声: 「まとまりが良い」「香りが続く」の投稿が多い https://x.com/a/status/111",
        "・検索面の勢力図: 「ヘアケア」上位10件中、競合ラサーナが3件 出典: https://www.tiktok.com/search?q=%E3%83%98%E3%82%A2%E3%82%B1%E3%82%A2",
        "- 出典の無い主張（これは採用されない）",
        "http://example.com/plain-http-only",  # https でない出典は採用しない
    ]
)


def test_parse_keeps_only_https_sourced_lines() -> None:
    adopted, dropped = parse_research_notes(_NOTES)
    assert [note.source_url for note in adopted] == [
        "https://x.com/a/status/111",
        "https://www.tiktok.com/search?q=%E3%83%98%E3%82%A2%E3%82%B1%E3%82%A2",
    ]
    assert adopted[0].text == "生活者の声: 「まとまりが良い」「香りが続く」の投稿が多い"
    assert adopted[1].text == "検索面の勢力図: 「ヘアケア」上位10件中、競合ラサーナが3件"
    assert dropped == 2


def test_research_slide_is_inserted_after_exposure_and_kept_out_of_summary() -> None:
    plan = build_deck_plan(
        _measurement(),
        _analysis(),
        generated_on="2026-09-03",
        search_depth=120,
        research_notes=_NOTES,
    )
    types = [slide.type for slide in plan.slide_plan]
    assert types == [
        "A",
        "B",
        "D",
        "D",
        "D",
        "C",
        "C",
        "D",
        "E",
        "H",
    ]  # 露出シェア導入の直後に D が1枚増える
    research = plan.slide_plan[3]
    assert research.q_number == ""
    assert "先行調査" in research.heading and "生活者の声" in research.heading
    assert "出典URLの無い主張は載せていない" in research.lead
    assert isinstance(research.data, SlideDataD)
    assert research.data.columns == ["要点（先行調査）", "出典"]
    assert [row[1] for row in research.data.rows] == [
        "https://x.com/a/status/111",
        "https://www.tiktok.com/search?q=%E3%83%98%E3%82%A2%E3%82%B1%E3%82%A2",
    ]
    assert research.tag is not None and research.tag.variant == "所見"
    assert "要点2件（すべて出典URL付き）" in research.tag.text
    assert "出典URLの無い2件は採用していない" in research.tag.text
    # 章扉の Q一覧と H の再掲行は従来どおり（先行調査は Q でも再掲でもない）
    h_slide = plan.slide_plan[-1]
    assert isinstance(h_slide.data, SlideDataH)
    assert len(h_slide.data.summary_rows) == 6
    assert not any("先行調査" in row.pattern for row in h_slide.data.summary_rows)
    # レンダラ入力契約（fmt）をそのまま通る
    content = validate_deck_content(json.loads(plan.model_dump_json()), load_fmt_spec())
    assert [slide.type for slide in content.slides] == types
    # 監査JSONに採否を記録
    audit = build_audit(
        _measurement(),
        _analysis(),
        plan,
        generated_on="2026-09-03",
        search_depth=120,
        research_notes=_NOTES,
    )
    assert audit["research_notes"] == {
        "provided": True,
        "adopted": 2,
        "shown": 2,
        "dropped_without_source": 2,
        "sources": [
            "https://x.com/a/status/111",
            "https://www.tiktok.com/search?q=%E3%83%98%E3%82%A2%E3%82%B1%E3%82%A2",
        ],
    }


def test_empty_or_unsourced_notes_leave_composition_unchanged() -> None:
    base = build_deck_plan(_measurement(), _analysis(), generated_on="2026-09-03", search_depth=120)
    assert (
        build_deck_plan(
            _measurement(),
            _analysis(),
            generated_on="2026-09-03",
            search_depth=120,
            research_notes="",
        )
        == base
    )
    unsourced = "- 根拠なしの主張A\n- 根拠なしの主張B"
    assert (
        build_deck_plan(
            _measurement(),
            _analysis(),
            generated_on="2026-09-03",
            search_depth=120,
            research_notes=unsourced,
        )
        == base
    )
    audit = build_audit(
        _measurement(), _analysis(), base, generated_on="2026-09-03", search_depth=120
    )
    assert audit["research_notes"]["provided"] is False


def test_research_slide_goes_first_when_exposure_slide_is_absent() -> None:
    # 一般KW軸が無い（露出シェア導入なし）でも先行調査は Q1 の前に置かれる
    from teamagent.skills.omiyage_report.metrics import AxisData, measure

    from .test_deck import _post

    axes = [
        AxisData(
            role="brand",
            label="ブランド名「エムキュア」検索",
            query="エムキュア",
            requested=120,
            posts=(_post("5", "エムキュアでヘアケア", rank=1),),
        ),
        AxisData(
            role="competitor",
            label="競合「ラサーナ」検索",
            query="ラサーナ",
            requested=120,
            posts=(_post("7", "ラサーナのヘアケア #PR", rank=1),),
        ),
    ]
    measurement = measure(axes, brand="エムキュア", competitors=["ラサーナ"], keywords=["ヘアケア"])
    plan = build_deck_plan(
        measurement, None, generated_on="2026-09-03", search_depth=120, research_notes=_NOTES
    )
    assert "先行調査" in plan.slide_plan[2].heading
    assert plan.slide_plan[3].q_number == "Q1"


def test_organic_word_in_notes_gets_definition_footnote_and_passes_pr_gate() -> None:
    notes = "- オーガニック投稿が上位を占める https://x.com/a/status/1"
    plan = build_deck_plan(
        _measurement(), None, generated_on="2026-09-03", search_depth=120, research_notes=notes
    )
    research = next(slide for slide in plan.slide_plan if "先行調査" in slide.heading)
    assert "#PR等の表記が確認できない投稿" in research.footnote
    validate_deck_content(json.loads(plan.model_dump_json()), load_fmt_spec())


def test_research_rows_are_capped_and_overflow_is_disclosed() -> None:
    notes = "\n".join(f"- 要点{i} https://x.com/a/status/{i}" for i in range(12))
    plan = build_deck_plan(
        _measurement(), None, generated_on="2026-09-03", search_depth=120, research_notes=notes
    )
    research = next(slide for slide in plan.slide_plan if "先行調査" in slide.heading)
    assert isinstance(research.data, SlideDataD)
    assert len(research.data.rows) == 8
    assert research.tag is not None and "4件は監査記録のみ" in research.tag.text
    audit = build_audit(
        _measurement(),
        None,
        plan,
        generated_on="2026-09-03",
        search_depth=120,
        research_notes=notes,
    )
    assert audit["research_notes"]["adopted"] == 12 and audit["research_notes"]["shown"] == 8


# ---------------------------------------------------------------------------
# 入力スキーマ（任意・上限 4000 字・正規化）と description のルーティング文言
# ---------------------------------------------------------------------------


def test_research_notes_is_optional_bounded_and_normalized() -> None:
    assert OmiyageReportSubmitInput().research_notes == ""
    assert RESEARCH_NOTES_MAX_CHARS == 4000
    ok = OmiyageReportSubmitInput(research_notes="x" * 4000)
    assert len(ok.research_notes) == 4000
    with pytest.raises(ValidationError):
        OmiyageReportSubmitInput(research_notes="x" * 4001)
    normalized = OmiyageReportSubmitInput(
        research_notes="  - 要点A https://x.com/a/status/1  \r\n\r\n\n- 要点B https://x.com/a/status/2\n"
    )
    assert (
        normalized.research_notes
        == "- 要点A https://x.com/a/status/1\n- 要点B https://x.com/a/status/2"
    )
    schema = OmiyageReportSubmitInput.model_json_schema()
    assert "research_notes" not in (schema.get("required") or [])
    assert schema["properties"]["research_notes"]["maxLength"] == 4000


def test_description_routes_final_pptx_here_and_asks_for_research_notes() -> None:
    description = OmiyageReportSubmitSkill.description
    assert description.startswith("お土産資料の最終成果物（PPTX）はこのツールでのみ生成する")
    assert "research_notes" in description
    for tool in ("x_voice_search", "search_surface_check", "web_research", "tiktok_acquire"):
        assert tool in description
    assert "出典URL" in description
    assert "生活者の声" in description and "検索面の勢力図" in description


# ---------------------------------------------------------------------------
# submit → 背景ジョブで research_notes が計測JSON（S3 保存物）まで届く
# ---------------------------------------------------------------------------


def test_submit_carries_research_notes_into_deck_plan_and_audit() -> None:
    store = ProposalJobStore(table_name="", memory={})
    launcher = _ReleasedLauncher()
    events: list[str] = []
    uploader = _Uploader()

    def builder(deck_plan_json: str, out_dir: str, request_id: str) -> tuple[str, str]:
        validate_deck_content(json.loads(deck_plan_json), load_fmt_spec())
        image = Path(out_dir) / "omiyage_fmt_x.pptx"
        image.write_bytes(b"PK-image")
        editable = Path(out_dir) / f"omiyage_fmt_x_{EDIT_MARKER}.pptx"
        editable.write_bytes(b"PK-editable")
        return str(image), str(editable)

    skill = OmiyageReportSubmitSkill(
        store=store,
        searcher=_KaoLikeSearcher(events=events),
        deck_builder=builder,
        slack=_Slack(events=events),
        thread_launcher=launcher,
        analyzer_factory=lambda request_id: None,
        plan_uploader=uploader,
        heartbeat_seconds=0,
        search_depth=120,
    )
    from teamagent.skills.base import SkillContext

    accepted = skill.run(
        OmiyageReportSubmitInput(
            brand="エムキュア",
            competitors=["ラサーナ"],
            keywords=["ヘアケア"],
            research_notes=_NOTES,
        ),
        SkillContext(
            request_id="omiyage-research-notes-test",
            user_id="U123",
            metadata={"channel_id": "C123", "thread_ts": "1.2", "user_email": "s@example.com"},
        ),
    )
    assert accepted.status == "queued"
    assert launcher.finished.wait(timeout=60)

    plan_key = next(key for key in uploader.objects if key.endswith("deck_plan.json"))
    plan: dict[str, Any] = json.loads(uploader.objects[plan_key])
    headings = [slide["heading"] for slide in plan["slide_plan"]]
    assert any("先行調査" in heading for heading in headings)
    audit_key = next(key for key in uploader.objects if key.endswith("audit.json"))
    audit = json.loads(uploader.objects[audit_key])
    assert audit["research_notes"]["adopted"] == 2
    row = store.get_job(accepted.job_id)
    assert row is not None
    assert json.loads(str(row["request_summary"]))["research_notes_chars"] == len(
        OmiyageReportSubmitInput(research_notes=_NOTES).research_notes
    )


# ---------------------------------------------------------------------------
# レビュー指摘（PR #375）: 制御文字で編集用 PPTX の XML が壊れる / 出典 URL 内の「オーガニック」/
# 120 字切り詰め
# ---------------------------------------------------------------------------


def test_xml_illegal_control_chars_are_stripped_but_tab_is_kept() -> None:
    raw = "- 要点\x00A\x01\x0b\x0c\x1f https://x.com/a/status/1\r\n- 要点\tB https://x.com/a/status/2\r"
    cleaned = OmiyageReportSubmitInput(research_notes=raw).research_notes
    assert cleaned == "- 要点A https://x.com/a/status/1\n- 要点\tB https://x.com/a/status/2"
    assert not any(ord(ch) < 0x20 and ch not in "\t\n" for ch in cleaned)
    # 資料の行にも制御文字が残らない（fmt/ooxml の escape は制御文字を通すため、入口で落とす）
    adopted, _ = parse_research_notes(cleaned)
    assert adopted[0].text == "要点A"


def test_organic_word_only_in_source_url_still_gets_footnote_without_q2() -> None:
    # 競合軸が取得失敗 → Q2（#PR 比較・定義注記の初出）が無い構成。URL だけに「オーガニック」が
    # 含まれても fmt の pr_labels ゲート（D の全セル走査）を通ること。
    notes = "- 検索面の勢力図（上位10件） https://x.com/search?q=オーガニック"
    plan = build_deck_plan(
        _measurement(with_failure=True),
        None,
        generated_on="2026-09-03",
        search_depth=120,
        research_notes=notes,
    )
    assert "Q2" not in [slide.q_number for slide in plan.slide_plan]
    research = next(slide for slide in plan.slide_plan if "先行調査" in slide.heading)
    assert isinstance(research.data, SlideDataD)
    assert "オーガニック" not in research.data.rows[0][0]  # 本文には無く URL にだけある
    assert "#PR等の表記が確認できない投稿" in research.footnote
    validate_deck_content(json.loads(plan.model_dump_json()), load_fmt_spec())  # 失敗しない


def test_research_text_is_truncated_to_120_chars() -> None:
    long_text = "あ" * 200
    adopted, dropped = parse_research_notes(f"- {long_text} https://x.com/a/status/1")
    assert dropped == 0
    assert len(adopted[0].text) == 120
    assert adopted[0].text == "あ" * 120
    plan = build_deck_plan(
        _measurement(),
        None,
        generated_on="2026-09-03",
        search_depth=120,
        research_notes=f"- {long_text} https://x.com/a/status/1",
    )
    research = next(slide for slide in plan.slide_plan if "先行調査" in slide.heading)
    assert isinstance(research.data, SlideDataD)
    assert len(research.data.rows[0][0]) == 120
