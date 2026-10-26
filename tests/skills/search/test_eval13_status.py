"""現況はモデルの要約形式に依存せず、取得済み根拠の日付とリンクで返す。"""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import drive_first
from teamagent.skills.search.schema import SearchInput
from tests.skills.search.test_drive_only_case_list import _case
from tests.skills.search.test_fanout import META, make
from tests.skills.search.test_fanout import setup as setup


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("summary", ["昨年の評価は低い状況です。[S:1]", ""])
def test_status_survives_compact_and_normal_fallback(monkeypatch, fanout_enabled, summary):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setenv("SEARCH_TIMELINE_STATUS", "1")
    monkeypatch.setattr(
        drive_first, "resolve_saved_files", lambda skill, hits, ctx: [None] * len(hits)
    )
    hit = _case(
        1,
        title="青空社 ハンバーグ報告書",
        client_name="青空社",
        campaign="ハンバーグ",
        campaign_aggregate="true",
        video_count="6",
        last_posted_on="2026-06-15",
    )
    skill, pg = make([hit])
    skill._summarize = MagicMock(return_value=(summary, 0.0))
    out = skill.run(
        SearchInput(query="青空社のハンバーグの施策は今どうなっている？"),
        SkillContext(metadata=META),
    )
    assert "2026-06-15 時点では実施済み" in out.answer
    assert "現在の状態は資料からは確認できません" in out.answer
    assert "<https://drive.google.com/file/d/FILE1/view|資料を見る>" in out.answer
    assert any(ref.drive_file_id == "FILE1" for ref in out.displayed_refs)
    assert pg.connection.call_args.kwargs["user_email"] == META["user_email"]


@pytest.mark.parametrize("missing", [True, False])
def test_status_uses_its_own_source_not_newer_other_campaign(missing):
    from teamagent.skills.search import timeline_status as timeline

    hit = _case(
        1,
        title="青空社 ハンバーグ",
        client_name="青空社",
        campaign="ハンバーグ",
        campaign_aggregate="true",
        video_count="6",
        last_posted_on="" if missing else "2026-06-15",
    )
    other = replace(
        hit,
        chunk_id=2,
        metadata={**hit.metadata, "campaign": "別施策", "last_posted_on": "2026-10-26"},
    )
    text = timeline.status_answer("ハンバーグの今の状態は？", [hit, other], lambda h: None)
    assert "2026-10-26" not in text and "別施策" not in text
    assert ("日付不明" if missing else "2026-06-15 時点では実施済み") in text
    assert "http" not in text
    assert timeline.status_answer("何が効いた？", [hit], lambda h: None) == ""


@pytest.mark.parametrize("phase", ["進行中", ""])
def test_karte_date_and_link_belong_to_same_record(phase):
    from teamagent.skills.search import timeline_status as timeline

    hit = _case(
        1,
        title="青空社 営業FB",
        client_name="青空社",
        campaign="ハンバーグ",
        is_sales_fb=True,
        occurred_at="2026-06-15",
        deal_phase=phase,
    )
    text = timeline.status_answer(
        "ハンバーグの進捗は？",
        [hit],
        lambda h: f"https://drive.google.com/file/d/FILE{h.chunk_id}/view",
    )
    assert "2026-06-15 時点では" in text
    assert ("進行中" if phase else "状態は資料からは確認できません") in text
    assert "<https://drive.google.com/file/d/FILE1/view|資料を見る>" in text


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_status_flag_off_keeps_existing_answer(monkeypatch, fanout_enabled):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    hit = _case(
        1,
        title="青空社 ハンバーグ",
        client_name="青空社",
        campaign="ハンバーグ",
        campaign_aggregate="true",
        video_count="6",
        last_posted_on="2026-06-15",
    )
    outputs = []
    for flag in ("", "0"):
        monkeypatch.setenv("SEARCH_TIMELINE_STATUS", flag)
        skill, _ = make([hit])
        skill._summarize = MagicMock(return_value=("昨年の評価です。[S:1]", 0.0))
        outputs.append(
            skill.run(
                SearchInput(query="青空社のハンバーグは今どうなっている？"),
                SkillContext(metadata=META),
            ).answer
        )
    assert outputs[0] == outputs[1]
    assert "時点では実施済み" not in outputs[0]


def test_posted_status_keeps_its_date_and_source_despite_newer_proposal():
    from teamagent.skills.search import timeline_status as timeline

    actual = _case(
        1,
        title="青空社 ハンバーグ",
        client_name="青空社",
        campaign="ハンバーグ",
        campaign_aggregate="true",
        video_count="6",
        last_posted_on="2026-06-15",
    )
    proposal = _case(
        2,
        title="青空社 ハンバーグの新規提案",
        client_name="青空社",
        campaign="ハンバーグ",
        occurred_at="2026-10-26",
        deal_phase="提案",
    )
    text = timeline.status_answer(
        "ハンバーグは今どうなっている？",
        [proposal, actual],
        lambda h: f"https://drive.google.com/file/d/FILE{h.chunk_id}/view",
    )
    assert "2026-06-15 時点では実施済み" in text
    assert "FILE1" in text
    assert "2026-10-26" not in text and "FILE2" not in text
