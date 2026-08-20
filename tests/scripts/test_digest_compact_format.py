"""密度優先描画（MORNING_DIGEST_COMPACT・2026-07-13 パイロットFB対応）の単体テスト。

固定する仕様:
  - 1件=1行原則（未確認の要約廃止）
  - 💬 Slack 返信漏れは判定層（_shared/slack_handoff）のカードを並べる形
    （本文抜粋は出さない・`#` はチャンネルのときだけ・生 ID は 1 文字も出さない）
  - 見出し=全数・表示=上限・超過=〈他N件〉+リンク の統一
  - フラグ OFF では旧描画（_format_block_kit）が使われ従来挙動が不変
  - `<https://evil|クリック>` 偽装がリンクとして描画されないこと（正規化→escape の順序）
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from teamagent.skills.morning_digest.schema import (
    CalendarEventItem,
    MailDigestItem,
    MorningDigestOutput,
    SlackUnreadItem,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_morning_digest_fargate.py"


def _load() -> Any:
    mod_name = "run_morning_digest_compact_under_test"
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()


def _mail(
    i: int, *, high: bool = False, unread: bool = True, summary: str = "長めの要約文"
) -> MailDigestItem:
    return MailDigestItem(
        counterpart_masked=f"u{i}***@ex.com",
        counterpart_display=f"担当{i}",
        subject_scrubbed=f"件名{i}",
        subject_display=f"件名{i} " + "あ" * 100,  # 60字切詰の検証用に長くする
        importance="high" if high else "medium",
        to_self=high,
        is_unread=unread,
        summary=summary,
        deadline="本日中" if high else None,
        ask="承認可否を返信" if high else "",
    )


def _slack_item(i: int, text: str) -> SlackUnreadItem:
    return SlackUnreadItem(
        channel_id="C08CHAN0001",
        channel_kind="channel",
        channel_name_display=f"ch-{i}",
        excerpt_display=text[:200],
        occurred_at="2026-07-13T09:00:00+09:00",
        permalink=f"https://vector.slack.com/archives/C08CHAN0001/p{i}",
    )


def _digest(
    *, n_high: int = 0, n_unread: int = 0, n_slack: int = 0, n_cal: int = 0
) -> MorningDigestOutput:
    mails = [_mail(i, high=True, unread=False) for i in range(n_high)]
    mails += [_mail(100 + i) for i in range(n_unread)]
    return MorningDigestOutput(
        user_email_masked="s***@vectorinc.co.jp",
        mail_digest=mails,
        calendar_events=[
            CalendarEventItem(summary_scrubbed=f"予定{i}", start_at="2026-07-14T01:00:00+09:00")
            for i in range(n_cal)
        ],
        calendar_date="2026-07-14",  # 見出しの日付明示（「今日の予定」→「7/14(火) の予定」）
        slack_unread_scanned=True,
        slack_unread=[
            _slack_item(
                i,
                "お疲れ様です。\n確認お願いします。\n<https://example.com/x|リンク> " + "追" * 100,
            )
            for i in range(n_slack)
        ],
    )


# ---------------------------------------------------------------------------
# _flatten_slack_text（正規化）
# ---------------------------------------------------------------------------


def test_flatten_mentions_channels_links() -> None:
    raw = "<@U08GGD873QC> と <@U09CX1CCBLN|小俣翔碁> が <#C091ZSVTKF1|l_lifull> で <https://x.com/a|記事> と <https://bare.example.com/path> を共有\n\n改行  空白"
    out = mod._flatten_slack_text(raw)
    # 実名が引けないメンションは架空名を作らず「表示名なし」と明示する（旧: "@メンバー"）。
    assert "@（表示名なし）" in out and "@小俣翔碁" in out
    assert "U08GGD873QC" not in out
    assert "#l_lifull" in out
    assert "記事" in out and "https://x.com/a" not in out
    assert "(リンク)" in out and "bare.example.com" not in out
    assert "\n" not in out and "  " not in out


def test_flatten_resolves_bare_mention_with_real_name() -> None:
    """data 層が users.info で引けた表示名を渡せば実名になる（マスクではなく表示整形）。"""
    out = mod._flatten_slack_text("<@U08GGD873QC> 確認お願いします", {"U08GGD873QC": "森田"})
    assert out == "@森田 確認お願いします"


def test_flatten_then_escape_kills_fake_link() -> None:
    """偽装リンクは「正規化でラベルだけ残す→escape」の順序で無害化される。"""
    raw = "支払いはこちら <https://evil.example/pay|正規の請求ページ>"
    flat = mod._flatten_slack_text(raw)
    rendered = mod._slack_escape(mod._truncate(flat, 60))
    assert "evil.example" not in rendered
    assert "<" not in rendered  # リンクとして描画され得る山括弧が残らない


def test_truncate_boundary() -> None:
    assert mod._truncate("あ" * 60, 60) == "あ" * 60
    out = mod._truncate("あ" * 61, 60)
    assert len(out) == 60 and out.endswith("…")


# ---------------------------------------------------------------------------
# _format_block_kit_compact（描画）
# ---------------------------------------------------------------------------


def test_header_counts_and_fallback_text() -> None:
    text, blocks = mod._format_block_kit_compact(
        _digest(n_high=2, n_unread=7, n_slack=6, n_cal=3), "u@ex.com"
    )
    assert text == "朝ダイジェスト｜要返信2・未確認7・Slack6・予定3"
    header = blocks[0]["text"]["text"]
    assert "🔴2" in header and "📬7" in header and "💬6" in header and "📅3" in header
    assert "件名" not in text  # fallback に PII を載せない


def test_unread_one_line_no_summary_and_overflow_link() -> None:
    _t, blocks = mod._format_block_kit_compact(_digest(n_unread=7), "u@ex.com")
    dump = str(blocks)
    assert "未確認（7件）" in dump
    assert "長めの要約文" not in dump  # 要約は出さない
    assert "〈他2件〉" in dump and "#inbox|受信トレイで見る" in dump
    # 件名は60字に切詰され「…」が付く
    assert "…" in dump


def test_slack_section_is_handoff_cards_not_body_excerpts() -> None:
    """💬 は判定済みカード（1件=1行）。本文抜粋・生 ID は出さず、母数は見出しに出す。"""
    _t, blocks = mod._format_block_kit_compact(_digest(n_slack=6), "u@ex.com")
    dump = str(blocks)
    assert "💬 *Slack 返信漏れ 6件*（うち5件を表示）" in dump  # 母数と表示件数を分けて出す
    # バケット内訳は取得できた全件で数え、並べた件数は分けて言う（表示 5 件の内訳を
    # 母数の内訳と誤読させない）。
    assert "🔴 *あなたの番（6件中5件を表示）*" in dump
    assert "・#ch-0 ・" in dump  # チャンネル（C）にだけ # を付ける
    assert "※ 見出しは原文からの切り出し＋定型の語尾です（要約文は作りません）。" in dump
    assert "|開く>" in dump  # permalink リンクは維持
    assert "C08CHAN0001" not in dump.replace(  # URL の外に生 ID を出さない
        "https://vector.slack.com/archives/C08CHAN0001/", ""
    )
    # 本文（「追」の連打・偽装リンク・改行）はそのまま描かない。
    assert "追追追追追追追追追追" not in dump
    assert "(リンク)" not in dump


def test_reply_section_buttons_and_meta_line() -> None:
    _t, blocks = mod._format_block_kit_compact(_digest(n_high=2), "u@ex.com")
    dump = str(blocks)
    assert "要返信（2件）" in dump
    assert "⏰" in dump and "📌" in dump  # 期限/依頼の構造化行
    actions = [b for b in blocks if b.get("type") == "actions"]
    assert len(actions) >= 2  # 各件にボタン行（旧 _reply_buttons をそのまま使用）


def test_reply_overflow_uses_inbox_link() -> None:
    _t, blocks = mod._format_block_kit_compact(_digest(n_high=7), "u@ex.com")
    dump = str(blocks)
    assert "要返信（7件）" in dump
    assert "〈他2件〉" in dump and "受信トレイで見る" in dump
    # 表示は5件まで（actions は 5 件ぶん）
    actions = [b for b in blocks if b.get("type") == "actions"]
    assert len(actions) == 5


def test_calendar_overflow_and_zero_state() -> None:
    _t, blocks = mod._format_block_kit_compact(_digest(n_cal=12), "u@ex.com")
    dump = str(blocks)
    assert "7/14(火) の予定（12件）" in dump and "〈他2件〉" in dump and "カレンダーを開く" in dump
    assert "今日の予定" not in dump  # 「今日」決め打ちは撤去（日付ずれを隠すため）
    _t2, blocks2 = mod._format_block_kit_compact(_digest(), "u@ex.com")
    assert "7/14(火) の予定*: なし" in str(blocks2)


def test_blocks_under_limit_worst_case() -> None:
    _t, blocks = mod._format_block_kit_compact(
        _digest(n_high=20, n_unread=30, n_slack=20, n_cal=20), "u@ex.com"
    )
    assert len(blocks) <= 48


def test_dispatch_respects_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MORNING_DIGEST_COMPACT", raising=False)
    assert mod._compact_enabled() is False  # 既定 OFF＝旧描画
    monkeypatch.setenv("MORNING_DIGEST_COMPACT", "true")
    assert mod._compact_enabled() is True
