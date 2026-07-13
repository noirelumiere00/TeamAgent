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
                to_self=True,
                summary="25日投稿に向け判断待ち。",
                has_draft=True,  # 既に下書き有り→「開く」ボタン
            ),
            MailDigestItem(
                counterpart_masked="k***@gmo.com",
                subject_scrubbed="振込自動化",
                importance="high",
                to_self=True,
                summary="回答待ち。",
                has_draft=False,
                draft_token="TOKB",  # 未作成→「下書きを作成」ボタン
                thread_gmail_url="https://mail.google.com/mail/u/0/#all/tB",
            ),
            MailDigestItem(
                counterpart_masked="a***@ex.com",
                subject_scrubbed="請求書",
                importance="medium",
                is_unread=True,  # 未開封
            ),
            MailDigestItem(
                counterpart_masked="b***@ex.com",
                subject_scrubbed="日程調整",
                importance="medium",
                is_unread=True,  # 未開封
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


def test_preamble_and_no_scoreboard() -> None:
    """冒頭は固定の枕詞。旧スコアボード（要返信/下書き済/要確認 カウント）は無い（v2）。"""
    text, blocks = mod._format_block_kit(_digest(), "s-komata@vectorinc.co.jp")
    assert text == "メールと本日の予定をお送りします。"
    dump = str(blocks)
    assert "メールと本日の予定をお送りします" in dump
    assert "下書き済" not in dump and "要確認" not in dump  # スコアボード削除


def test_reply_section_has_per_mail_buttons() -> None:
    """要返信メール（high 2件）: 行は「✅ 下書きを確認」1つ（未作成のみ「作成」追加）。
    行内の「下書きを開く」は廃止し、末尾に「📁 下書き一覧を開く」を1つだけ集約する。"""
    _text, blocks = mod._format_block_kit(_digest(), "s-komata@vectorinc.co.jp")
    dump = str(blocks)
    assert "要返信メール（2件）" in dump
    actions = [b for b in blocks if b.get("type") == "actions"]
    all_el = [e for b in actions for e in b["elements"]]
    # 未作成メール（TOKB）のみ「作成」アクションが出る。
    assert any(e.get("action_id") == "mail_draft" and e.get("value") == "TOKB" for e in all_el)
    # 各行に「✅ 下書きを確認」（スレッド直行）。
    assert any("下書きを確認" in str(e) for e in all_el)
    # 旧・行内の「📨 下書きを開く」（下書きフォルダ直行）は廃止。
    assert not any("下書きを開く" in str(e) for e in all_el)
    # 末尾に「📁 下書き一覧を開く」が1つだけ（下書きフォルダ #drafts）。
    list_btns = [e for e in all_el if "下書き一覧を開く" in str(e)]
    assert len(list_btns) == 1
    assert list_btns[0].get("url", "").endswith("#drafts")


def test_calendar_section_rendered() -> None:
    """カレンダー（今日の予定）が DM に描画される。display 未設定時は scrubbed にフォールバック。"""
    _text, blocks = mod._format_block_kit(_digest(), "s-komata@vectorinc.co.jp")
    dump = str(blocks)
    assert "今日の予定（2件）" in dump
    assert "ノーベル定例" in dump  # 件名
    assert "渋谷オフィス" in dump  # 会議室


def test_unread_section_lists_unread() -> None:
    """未確認セクション: is_unread かつ非 high の medium 2件が出る（low/既読は出ない）。"""
    _text, blocks = mod._format_block_kit(_digest(), "s-komata@vectorinc.co.jp")
    dump = str(blocks)
    assert "未確認（2件）" in dump
    assert "請求書" in dump and "日程調整" in dump
    assert "お知らせ" not in dump  # low かつ既読 は未開封に出ない


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


def test_fetch_connected_users_sets_admin_guc(monkeypatch: Any) -> None:
    """動的抽出は SELECT 前に admin GUC を立てる（FORCE RLS で 0 行になる事故の再発防止）。

    oauth_tokens は「本人 GUC or admin」の FORCE RLS。GUC 無し接続だと接続ロールに
    よっては全行不可視＝自動モードで誰にも配信されない（2026-07-13 監査で検出）。
    """
    executed: list[str] = []

    class _Cur:
        def execute(self, sql: str, *a: Any) -> None:
            executed.append(sql)

        def fetchall(self) -> list[tuple[str]]:
            return [("A@ex.com",), ("b@ex.com",)]

        def __enter__(self) -> _Cur:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    class _Conn:
        def cursor(self) -> _Cur:
            return _Cur()

        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    import types

    fake_psycopg = types.SimpleNamespace(connect=lambda dsn: _Conn())
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/db")
    users = mod._fetch_connected_users_from_rds()
    assert users == ["a@ex.com", "b@ex.com"]  # 小文字正規化
    set_idx = next(i for i, q in enumerate(executed) if "app.user_role" in q)
    sel_idx = next(i for i, q in enumerate(executed) if q.strip().startswith("SELECT"))
    assert set_idx < sel_idx, "admin GUC は SELECT より先に立てる"
