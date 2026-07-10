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
