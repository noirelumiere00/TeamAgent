"""古いカルテと予定・正本の状態を、固定した現在日付で区別する。"""

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.clientkarte import skill as karte
from teamagent.skills.clientkarte.schema import ClientKarteInput
from teamagent.skills.clientkarte.status import case_state_lines, dated_answer
from teamagent.skills.morning_digest.calendar_window import JST
from tests.skills.clientkarte.test_clientkarte_skill import _pgvector_with_hits
from tests.skills.search.test_case_state_evidence import row


@pytest.fixture
def status_skill(monkeypatch: pytest.MonkeyPatch) -> tuple[karte.ClientKarteSkill, MagicMock]:
    monkeypatch.setattr(karte, "now_jst", lambda: datetime(2026, 11, 5, 10, 13, tzinfo=JST))
    monkeypatch.setenv("KARTE_ATTACH_DOCS", "0")
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", "owner@example.test")
    bedrock = MagicMock()
    bedrock.converse.return_value.text = (
        "### JTB\n"
        "- 宿題: 8/26 に自主提案を予定していた\n"
        "- コンペ決定: 10月中旬〜下旬\n"
        "- 先方担当者が9月下旬退職予定\n"
        "- 次回: 11/10 に提案予定"
    )
    bedrock.converse.return_value.usage.cost_usd = 0.0
    pg = _pgvector_with_hits(
        [
            SearchHit(1, "自主提案を予定", 1.0, {"occurred_at": "2026-08-24"}),
            SearchHit(2, "過去の報告", 1.0, {"occurred_at": "2026-08-10"}),
        ]
    )
    pg.search_drive_by_client_names.return_value = []
    return karte.ClientKarteSkill(bedrock=bedrock, pgvector=pg), pg


def test_old_karte_is_dated_and_past_plans_are_not_current(status_skill: tuple) -> None:
    skill, _ = status_skill
    out = skill.run(ClientKarteInput(client_name="JTB"), SkillContext())
    assert out.answer.startswith("### JTB（8/24 時点）\n- 宿題:")
    assert out.answer.count("（8/24 時点）") == 1
    assert "8/26 に自主提案の予定だった（結果は未確認）" in out.answer
    assert "10月中旬〜下旬 予定だった（結果は未確認）" in out.answer
    assert "9月下旬退職予定だった（結果は未確認）" in out.answer
    assert "11/10 に提案予定" in out.answer
    assert out.answer.count("予定だった（結果は未確認）") == 3


@pytest.mark.parametrize(
    "text,expected",
    [
        ("提案予定日: 8/26", "提案予定日: 8/26 予定だった（結果は未確認）"),
        ("期日: 2026-08-26", "期日: 2026-08-26 予定だった（結果は未確認）"),
        ("決定時期: 10月", "決定時期: 10月 予定だった（結果は未確認）"),
        ("8月26日提案予定", "8月26日提案予定だった（結果は未確認）"),
        ("7/26 提案予定", "7/26 提案予定だった（結果は未確認）"),
        ("提案予定: 11/5", "提案予定: 11/5"),
        ("決定時期: 11月上旬", "決定時期: 11月上旬"),
        ("提案予定: 2/30", "提案予定: 2/30"),
        ("8/26 提案済み（当初予定）", "8/26 提案済み（当初予定）"),
        ("8/26 予定だった（結果は未確認）", "8/26 予定だった（結果は未確認）"),
        ("8/26 に提案した", "8/26 に提案した"),
        ("提案日: 8/26", "提案日: 8/26"),
        ("2027/1/10 提案予定", "2027/1/10 提案予定"),
        ("2025/8/26 提案予定", "2025/8/26 提案予定だった（結果は未確認）"),
        ("予定は未定（8/24 時点）", "予定は未定（8/24 時点）"),
        (
            "8/26 提案予定。11/10 再提案予定",
            "8/26 提案予定だった（結果は未確認）。11/10 再提案予定",
        ),
        (
            "8/26 提案予定、11/10 再提案予定",
            "8/26 提案予定だった（結果は未確認）、11/10 再提案予定",
        ),
    ],
)
def test_plan_dates_use_jst_calendar_day(text: str, expected: str) -> None:
    hits = [SearchHit(1, "FB", 1.0, {"occurred_at": "2026-08-24"})]
    answer = dated_answer(text, hits, "JTB", date(2026, 11, 5))
    assert answer == expected + ("" if "（8/24 時点）" in expected else "（8/24 時点）")


def test_each_case_keeps_its_own_latest_contact_date() -> None:
    hits = [
        SearchHit(1, "FB", 1.0, {"campaign": "企業広報", "occurred_at": "2026-08-24"}),
        SearchHit(2, "FB", 1.0, {"campaign": "企業広報", "occurred_at": "2026-08-10"}),
        SearchHit(3, "FB", 1.0, {"campaign": "採用広報", "occurred_at": "2026-09-01"}),
    ]
    answer = dated_answer(
        "### 企業広報\n- 8/26 提案予定\n### 採用広報\n- 9/10 提案予定",
        hits,
        "JTB",
        date(2026, 11, 5),
    )
    assert "### 企業広報（8/24 時点）" in answer
    assert "### 採用広報（9/1 時点）" in answer
    assert answer.count("予定だった（結果は未確認）") == 2
    assert answer.count("時点）") == 2


def test_no_date_does_not_invent_asof_or_plan_year() -> None:
    assert (
        dated_answer("8/26 提案予定", [SearchHit(1, "FB", 1.0, {})], "JTB", date(2026, 11, 5))
        == "8/26 提案予定"
    )


def test_existing_asof_is_not_repeated_in_the_heading() -> None:
    text = "### JTB\n最新の記録（8/24 時点）\n- 8/26 提案予定"
    answer = dated_answer(
        text,
        [SearchHit(1, "FB", 1.0, {"occurred_at": "2026-08-24"})],
        "JTB",
        date(2026, 11, 5),
    )
    assert answer.count("（8/24 時点）") == 1
    assert "8/26 提案予定だった（結果は未確認）" in answer


@pytest.mark.parametrize(
    "email,request_text,expected",
    [
        ("owner@example.test", "JTBの案件って今どうなってる？", True),
        ("other@example.test", "JTBの案件って今どうなってる？", False),
        (None, "JTBの案件って今どうなってる？", False),
        ("owner@example.test", "JTBの過去の提案資料を見せて", False),
        ("owner@example.test", "", False),
        ("owner@example.test", "   ", False),
        ("owner@example.test", None, False),
    ],
)
def test_state_line_requires_both_email_gate_and_status_question(
    status_skill: tuple, email: str | None, request_text: str | None, expected: bool
) -> None:
    skill, pg = status_skill
    source = row(state="lost", state_as_of="2026-09-11", evidence_date="2026-09-11")
    source.metadata.update({"client_name": "JTB", "title": "JTB 企業広報の提案"})
    pg.search_drive_by_client_names.return_value = [source]
    with capture_logs() as logs:
        out = skill.run(
            ClientKarteInput(client_name="JTB"),
            SkillContext(
                metadata={
                    "user_email": email,
                    "user_groups": ["sales"],
                    "user_role": "member",
                    **({"request_text": request_text} if request_text is not None else {}),
                }
            ),
        )
    event = next(log for log in logs if log["event"] == "clientkarte_done")
    assert event["has_request_text"] is bool(request_text and request_text.strip())
    assert event["asks_status"] is bool(request_text and "今どうなってる" in request_text)
    assert event["case_state_lines"] == int(expected)
    assert event["asof_lines"] == 1
    assert "request_text" not in event and "answer" not in event
    assert "JTB" not in str(event) and "https://" not in str(event)
    assert ("\n状態:" in out.answer) is expected
    if expected:
        assert "JTB 企業広報の提案 / 2026-09-11 時点の正式状態: 失注" in out.answer
        assert "根拠日: 2026-09-11" in out.answer
        assert "https://example.test/source#1" in out.answer
        kwargs = pg.search_drive_by_client_names.call_args.kwargs
        assert pg.search_drive_by_client_names.call_args.args == (
            pg.connection.return_value.__enter__.return_value,
            ["JTB"],
        )
        assert kwargs["request_id"]
        pg.connection.assert_called_once_with(
            app_role="teamagent_app", user_email=email, user_groups=["sales"], user_role="member"
        )
    else:
        pg.search_drive_by_client_names.assert_not_called()


@pytest.mark.parametrize("count", [0, 1, 2])
def test_done_log_counts_dated_cases(status_skill: tuple, count: int) -> None:
    skill, pg = status_skill
    pg.list_client_timeline_recent.return_value = [
        SearchHit(i, "過去の報告", 1.0, {"campaign": f"案件{i}", "occurred_at": "2026-08-24"})
        for i in range(count)
    ]
    skill._bedrock.converse.return_value.text = "\n".join(f"### 案件{i}" for i in range(count))
    with capture_logs() as logs:
        skill.run(ClientKarteInput(client_name="JTB"), SkillContext())
    event = next(log for log in logs if log["event"] == "clientkarte_done")
    assert event["asof_lines"] == count
    assert event["case_state_lines"] == 0
    assert event["has_request_text"] is False and event["asks_status"] is False


@pytest.mark.parametrize("kind", ["empty", "legacy", "other_client", "failed"])
def test_no_matching_state_does_not_add_a_line(status_skill: tuple, kind: str) -> None:
    skill, pg = status_skill
    if kind == "legacy":
        pg.search_drive_by_client_names.return_value = [SearchHit(3, "見送り", 1.0, {})]
    elif kind == "other_client":
        source = row(state="lost")
        source.metadata["client_name"] = "HIS"
        pg.search_drive_by_client_names.return_value = [source]
    elif kind == "failed":
        pg.search_drive_by_client_names.side_effect = RuntimeError("unavailable")
    out = skill.run(
        ClientKarteInput(client_name="JTB"),
        SkillContext(
            metadata={"user_email": "owner@example.test", "request_text": "JTBの現状は？"}
        ),
    )
    assert "\n状態:" not in out.answer
    assert "（8/24 時点）" in out.answer


def test_state_resolution_keeps_conflict_instead_of_choosing_one_source() -> None:
    answer = case_state_lines([row(state="won"), row(2, state="lost")])
    assert "根拠が競合" in answer
    assert "正式状態: 受注" not in answer
    assert "正式状態: 失注" not in answer


def test_report_keeps_its_evidence_date_without_becoming_official_state() -> None:
    answer = case_state_lines([row(state="lost", authority="reported")])
    assert "正式状態は不明" in answer
    assert "根拠日: 2026-09-01" in answer
    assert "2026-10-01" not in answer  # 資料の更新日は、状態の根拠日と取り違えない。
    assert "正式状態: 失注" not in answer
