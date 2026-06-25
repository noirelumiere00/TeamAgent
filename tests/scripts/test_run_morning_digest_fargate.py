"""scripts/run_morning_digest_fargate.py の Block Kit 整形ロジック単体テスト。

UI/UX 改善（スコアボード / 要返信の独立 section / 下書き昇格 / カレンダー会議室 /
アクションボタン）を固定する。scripts/ は package でないため importlib でロードする。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

from teamagent.skills.morning_digest.schema import (
    CalendarEventItem,
    MailDigestItem,
    MorningDigestOutput,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_morning_digest_fargate.py"


def _load() -> Any:
    mod_name = "run_morning_digest_under_test"
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()


def _digest() -> MorningDigestOutput:
    return MorningDigestOutput(
        user_email_masked="s***@vectorinc.co.jp",
        mail_digest=[
            MailDigestItem(
                counterpart_masked="え***@nobel.co.jp",
                subject_scrubbed="動画制作の件",
                importance="high",
                summary="25日投稿に向け判断待ち。",
                has_draft=True,
            ),
            MailDigestItem(
                counterpart_masked="k***@gmo.com",
                subject_scrubbed="振込自動化",
                importance="high",
                summary="回答待ち。",
                has_draft=False,
            ),
            MailDigestItem(
                counterpart_masked="a***@ex.com", subject_scrubbed="請求書", importance="medium"
            ),
            MailDigestItem(
                counterpart_masked="b***@ex.com", subject_scrubbed="日程調整", importance="medium"
            ),
            MailDigestItem(
                counterpart_masked="c***@ex.com", subject_scrubbed="お知らせ", importance="low"
            ),
        ],
        calendar_events=[
            CalendarEventItem(
                summary_scrubbed="ノーベル定例",
                start_at="2026-06-19T01:00:00+00:00",
                location_scrubbed="渋谷オフィス 3F会議室A",
            ),
            CalendarEventItem(
                summary_scrubbed="社内レビュー", start_at="2026-06-19T05:00:00+00:00"
            ),
        ],
        drafts_created=1,
    )


def test_scoreboard_counts() -> None:
    _text, blocks = mod._format_block_kit(_digest(), "s-komata@vectorinc.co.jp")
    # 2番目の block が fields スコアボード。high=2 / drafts=1 / medium=2（カレンダーは除外）。
    fields_text = " ".join(f["text"] for f in blocks[1]["fields"])
    assert "要返信*  `2件`" in fields_text
    assert "下書き済*  `1件`" in fields_text
    assert "要確認*  `2件`" in fields_text
    assert "今日の予定" not in fields_text


def test_high_priority_section_and_draft_elevated() -> None:
    _text, blocks = mod._format_block_kit(_digest(), "s-komata@vectorinc.co.jp")
    dump = str(blocks)
    assert "いますぐ返信したい（2件）" in dump
    # has_draft=True の high 項目に「Gmailを開く（確認して送信）」リンクが出る。
    # （Slack 上では送信せず Gmail を開くだけ。）
    assert "Gmailを開く（確認して送信）" in dump


def test_draft_body_shown_for_review() -> None:
    # draft_preview があれば、その本文を Slack でそのまま確認できる（未送信）。
    d = MorningDigestOutput(
        user_email_masked="s***@vectorinc.co.jp",
        drafts_created=1,
        mail_digest=[
            MailDigestItem(
                counterpart_masked="t***@tategata.co.jp",
                subject_scrubbed="動画提出",
                importance="high",
                has_draft=True,
                thread_id="thr_X",
                draft_preview="タテガタ ご担当者様\nお世話になっております。本日中に審査します。",
            ),
        ],
    )
    _text, blocks = mod._format_block_kit(d, "s-komata@vectorinc.co.jp")
    dump = str(blocks)
    assert "返信下書き（未送信" in dump  # 確認用の下書きセクションが出る
    assert "本日中に審査します" in dump  # 本文がそのまま読める
    assert "Slackでは送信しません" in dump  # 送信は Slack でしない明示
    assert "Gmailを開く" in dump


def test_draft_links_to_thread_with_authuser() -> None:
    # thread_id を持つ has_draft 項目は、汎用 #drafts ではなくスレッド deep link
    # (#all/<tid>) を「Gmailを開く」で開く。複数ログイン対策に authuser を付ける。
    d = MorningDigestOutput(
        user_email_masked="s***@vectorinc.co.jp",
        mail_digest=[
            MailDigestItem(
                counterpart_masked="e***@nobel.co.jp",
                subject_scrubbed="動画提出の件",
                importance="high",
                has_draft=True,
                thread_id="thr_ABC123",
            ),
        ],
        drafts_created=1,
    )
    _text, blocks = mod._format_block_kit(d, "s-komata@vectorinc.co.jp")
    dump = str(blocks)
    assert "Gmailを開く" in dump
    assert "https://mail.google.com/mail/?authuser=s-komata@vectorinc.co.jp#all/thr_ABC123" in dump


def test_action_buttons_pin_account_with_authuser() -> None:
    _text, blocks = mod._format_block_kit(_digest(), "s-komata@vectorinc.co.jp")
    actions = next(b for b in blocks if b.get("type") == "actions")
    urls = [e.get("url", "") for e in actions["elements"]]
    assert all("authuser=s-komata@vectorinc.co.jp" in u for u in urls)
    assert any(u.endswith("#drafts") for u in urls)  # ✏️ 下書きを確認
    assert any(u.endswith("#inbox") for u in urls)  # 📥 受信トレイ


def test_links_fall_back_to_u0_when_email_unknown() -> None:
    # email 不明時は従来どおり u/0（authuser を付けない）。
    _text, blocks = mod._format_block_kit(_digest(), "")
    dump = str(blocks)
    assert "mail/u/0/#inbox" in dump
    assert "authuser=" not in dump


def test_gmail_thread_url_helper() -> None:
    assert mod._gmail_thread_url(None, "x@y.com") is None
    assert mod._gmail_thread_url("", "x@y.com") is None
    assert (
        mod._gmail_thread_url("T1", "u@vectorinc.co.jp")
        == "https://mail.google.com/mail/?authuser=u@vectorinc.co.jp#all/T1"
    )
    assert mod._gmail_thread_url("T1", "") == "https://mail.google.com/mail/u/0/#all/T1"
    assert mod._gmail_account_base("") == "https://mail.google.com/mail/u/0/"


def test_calendar_section_removed() -> None:
    # カレンダー（今日の予定）は digest に含めない。
    _text, blocks = mod._format_block_kit(_digest(), "s-komata@vectorinc.co.jp")
    dump = str(blocks)
    assert "今日の予定" not in dump
    assert "渋谷オフィス" not in dump


def test_action_buttons_present() -> None:
    _text, blocks = mod._format_block_kit(_digest(), "s-komata@vectorinc.co.jp")
    actions = [b for b in blocks if b.get("type") == "actions"]
    assert actions, "アクションバーが無い"
    labels = [e["text"]["text"] for e in actions[0]["elements"]]
    assert any("下書きを確認" in label for label in labels)  # drafts>0 なので出る
    assert any("受信トレイ" in label for label in labels)
    assert not any("カレンダー" in label for label in labels)  # カレンダーボタンは除去


def test_medium_compressed_with_remaining() -> None:
    _text, blocks = mod._format_block_kit(_digest(), "s-komata@vectorinc.co.jp")
    dump = str(blocks)
    # medium 2件 + low 1件。medium[:3] で2件表示・残り low 1件は「+1件」省略。
    assert "未確認・未返信（2件）" in dump
    assert "〈+1件〉" in dump


def test_fmt_time_parses_iso_to_jst() -> None:
    assert mod._fmt_time("2026-06-19T01:00:00+00:00") == "10:00"  # UTC 01:00 → JST 10:00
    assert mod._fmt_time(None) == "?"


class _FakeAsyncClient:
    """SlackClient._client (AsyncWebClient) の最小フェイク（async メソッド）。"""

    async def users_lookupByEmail(self, *, email: str) -> dict[str, Any]:  # noqa: N802
        assert email == "s-komata@vectorinc.co.jp"
        return {"ok": True, "user": {"id": "U09CX1CCBLN"}}

    async def conversations_open(self, *, users: str) -> dict[str, Any]:
        assert users == "U09CX1CCBLN"
        return {"ok": True, "channel": {"id": "D0BA1TWN6AC"}}


class _FakeSlack:
    """SlackClient のフェイク（WebClient を _client で保持）。"""

    def __init__(self) -> None:
        self._client = _FakeAsyncClient()


def test_email_to_slack_user_id_uses_underscore_client() -> None:
    # 回帰固定: _web_client/client でなく _client から AsyncWebClient を取り await する。
    uid = asyncio.run(mod._email_to_slack_user_id(_FakeSlack(), "s-komata@vectorinc.co.jp"))
    assert uid == "U09CX1CCBLN"


def test_open_im_channel_uses_underscore_client() -> None:
    ch = asyncio.run(mod._open_im_channel(_FakeSlack(), "U09CX1CCBLN"))
    assert ch == "D0BA1TWN6AC"


# ── MORNING_DIGEST_EXCLUDE（テストユーザー停止）──────────────────────────────


def test_apply_exclude_removes_listed_users(monkeypatch: Any) -> None:
    monkeypatch.setenv("MORNING_DIGEST_EXCLUDE", "test1@vectorinc.co.jp, Test2@VectorInc.co.jp")
    users = ["a@vectorinc.co.jp", "test1@vectorinc.co.jp", "test2@vectorinc.co.jp"]
    assert mod._apply_exclude(users) == ["a@vectorinc.co.jp"]


def test_apply_exclude_noop_when_unset(monkeypatch: Any) -> None:
    monkeypatch.delenv("MORNING_DIGEST_EXCLUDE", raising=False)
    assert mod._apply_exclude(["a@vectorinc.co.jp"]) == ["a@vectorinc.co.jp"]


def test_resolve_target_users_applies_exclude_to_rds(monkeypatch: Any) -> None:
    monkeypatch.delenv("MORNING_DIGEST_USERS", raising=False)
    monkeypatch.setenv("MORNING_DIGEST_EXCLUDE", "test2@vectorinc.co.jp")
    monkeypatch.setattr(
        mod,
        "_fetch_connected_users_from_rds",
        lambda: ["owner@vectorinc.co.jp", "test2@vectorinc.co.jp"],
    )
    assert mod._resolve_target_users() == ["owner@vectorinc.co.jp"]


# ── マスキング緩和: 本人 DM は実名表示 ───────────────────────────────────────


def test_block_kit_renders_display_fields() -> None:
    d = _digest()
    top = d.mail_digest[0]
    top.subject_display = "【ノーベル】動画制作の最終確認"
    top.counterpart_display = "江田 真希"
    top.deadline = "6/25まで"
    top.ask = "サムネ案の確定"
    top.sender_label = "重要"
    top.thread_count = 4
    _text, blocks = mod._format_block_kit(d, "s-komata@vectorinc.co.jp")
    dump = str(blocks)
    assert "【ノーベル】動画制作の最終確認" in dump  # 実件名（未マスク）
    assert "江田 真希" in dump  # 実名（未マスク）
    assert "6/25まで" in dump and "サムネ案の確定" in dump
    assert "4通" in dump
