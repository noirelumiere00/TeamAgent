"""朝ダイジェスト DM の「💬 Slack 返信漏れ」セクション描画と skill 側マッピングのテスト。

runner（scripts/run_morning_digest_fargate.py）は test_mail_feature_edge と同じく
importlib でロードする。外部 I/O 無し。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from teamagent.skills._shared.slack_unreplied import UnrepliedMention
from teamagent.skills.base import SkillContext
from teamagent.skills.morning_digest.schema import (
    MorningDigestInput,
    MorningDigestOutput,
    SlackUnreadItem,
)
from teamagent.skills.morning_digest.skill import MorningDigestSkill

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_morning_digest_fargate.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("run_md_slack_section_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_md_slack_section_under_test"] = module
    spec.loader.exec_module(module)
    return module


runner = _load()
ME = "me@vectorinc.co.jp"


def test_slack_unread_section_rendered() -> None:
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        slack_unread=[
            SlackUnreadItem(
                channel_name_masked="s***-a***",
                excerpt_scrubbed="<C>さん 見積の件…",
                channel_name_display="sales-acme",
                excerpt_display="小俣さん 見積の件お願いします & <確認>",
                permalink="https://x.slack.com/archives/C1/p1000",
                occurred_at="2026-07-10T09:00:00+09:00",
            )
        ],
    )
    _t, blocks = runner._format_block_kit(d, ME)
    dump = str(blocks)
    assert "Slack 返信漏れ（1件）" in dump
    assert "sales-acme" in dump  # display 優先
    assert "<https://x.slack.com/archives/C1/p1000|開く>" in dump  # permalink は生リンク
    assert "&amp; &lt;確認&gt;" in dump  # 本文はエスケープ（偽リンク注入防止）


def test_slack_unread_section_absent_when_empty() -> None:
    d = MorningDigestOutput(user_email_masked="m***@x")
    _t, blocks = runner._format_block_kit(d, ME)
    assert "Slack 返信漏れ" not in str(blocks)


def test_skill_maps_provider_output() -> None:
    class _Prov:
        def collect(self, email: str, horizon: int, rid: str) -> list[UnrepliedMention]:
            assert horizon == 7  # input 既定値が伝播
            return [
                UnrepliedMention(
                    channel_id="C1",
                    channel_name="sales-acme",
                    ts="1000.1",
                    text="小俣さん t***@example.com 宛の件",
                    permalink="https://x/p1",
                    occurred_at="2026-07-10T09:00:00+09:00",
                )
            ]

    skill = MorningDigestSkill(slack=_Prov())
    items = skill._collect_slack_unread(
        ME, MorningDigestInput(), SkillContext(request_id="r", metadata={"user_email": ME})
    )
    assert len(items) == 1
    it = items[0]
    assert it.channel_name_display == "sales-acme"
    assert it.excerpt_display.startswith("小俣さん")
    assert it.permalink == "https://x/p1"
    # masked/scrubbed 側も埋まる（ログ・監査用）。
    assert it.channel_name_masked and it.excerpt_scrubbed


def test_skill_returns_empty_when_provider_none() -> None:
    skill = MorningDigestSkill(slack=None)
    items = skill._collect_slack_unread(
        ME, MorningDigestInput(), SkillContext(request_id="r", metadata={"user_email": ME})
    )
    assert items == []


# ── v0.3 Task3: 📅 カレンダー登録ボタンの描画（flag 既定OFF） ────────────────


def _meeting_item(**kw: Any) -> Any:
    from teamagent.skills.morning_digest.schema import MailDigestItem

    return MailDigestItem(
        counterpart_masked="a***@x",
        importance="high",
        to_self=True,
        subject_display="7/15 定例の件",
        draft_token="DTOK",
        meeting_start="2026-07-15T14:00:00+09:00",
        meeting_end="2026-07-15T15:00:00+09:00",
        meeting_title="◯◯様 定例",
        event_token=kw.get("event_token", "ETOK"),
    )


def test_calendar_button_rendered_when_flag_on(monkeypatch: Any) -> None:
    monkeypatch.setenv("MORNING_DIGEST_CALENDAR_BUTTON", "1")
    btns = runner._reply_buttons(_meeting_item())
    cal = [b for b in btns if b.get("action_id") == "calendar_event"]
    assert cal and cal[0]["value"] == "ETOK"


def test_calendar_button_absent_when_flag_off(monkeypatch: Any) -> None:
    monkeypatch.delenv("MORNING_DIGEST_CALENDAR_BUTTON", raising=False)
    btns = runner._reply_buttons(_meeting_item())
    assert not [b for b in btns if b.get("action_id") == "calendar_event"]


def test_calendar_button_absent_without_token(monkeypatch: Any) -> None:
    # 日時未確定/To 本人でない → event_token 空 → flag ON でもボタン無し。
    monkeypatch.setenv("MORNING_DIGEST_CALENDAR_BUTTON", "1")
    btns = runner._reply_buttons(_meeting_item(event_token=""))
    assert not [b for b in btns if b.get("action_id") == "calendar_event"]


# ── v0.3 Task4: 🗓 日程候補を提案ボタンの描画（flag 既定OFF） ────────────────


def test_schedule_button_rendered_when_flag_on(monkeypatch: Any) -> None:
    from teamagent.skills.morning_digest.schema import MailDigestItem

    monkeypatch.setenv("MORNING_DIGEST_SCHEDULE_BUTTON", "1")
    m = MailDigestItem(
        counterpart_masked="a***@x",
        importance="high",
        to_self=True,
        draft_token="DTOK",
        scheduling_request=True,
    )
    btns = runner._reply_buttons(m)
    sched = [b for b in btns if b.get("action_id") == "schedule_propose"]
    assert sched and sched[0]["value"] == "DTOK"  # draft_token を流用（thread_id 由来）

    # flag OFF なら出ない。
    monkeypatch.delenv("MORNING_DIGEST_SCHEDULE_BUTTON", raising=False)
    assert not [b for b in runner._reply_buttons(m) if b.get("action_id") == "schedule_propose"]

    # scheduling_request=False なら flag ON でも出ない。
    monkeypatch.setenv("MORNING_DIGEST_SCHEDULE_BUTTON", "1")
    m2 = MailDigestItem(
        counterpart_masked="a***@x", importance="high", to_self=True, draft_token="DTOK"
    )
    assert not [b for b in runner._reply_buttons(m2) if b.get("action_id") == "schedule_propose"]
