"""朝ダイジェスト「☑️ 確認済み」ボタンの描画テスト（旧描画・compact 描画の両方）。

固定する仕様:
  - フラグ OFF では **ack_token を持つ digest を渡しても** 出力が 1 バイトも変わらない
  - ON では要返信メール行に ☑️ボタンが付く（token が空の項目には付かない）
  - ON では 💬 セクションが 1 カード = 1 section + accessory ボタンになる
  - 「☑️ 全部確認した」は末尾にちょうど 1 つ・打ち切りでも消えない
  - 💬 の描画が例外を出しても DM 全体は生成される（fail-safe 境界）
  - G3: 生 channel_id / permalink が blocks に 1 文字も出ない
"""

from __future__ import annotations

import importlib.util
import json
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

_ACK = "digest_ack"
_CHANNEL_ID = "C08CHAN0001"
_TS = "1718681400.000100"


def _load() -> Any:
    mod_name = "run_morning_digest_ack_under_test"
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()


# ── フィクスチャ ─────────────────────────────────────────────────────────


def _mail(i: int, *, ack_token: str = "") -> MailDigestItem:
    return MailDigestItem(
        counterpart_masked=f"u{i}***@ex.com",
        counterpart_display=f"担当{i}",
        subject_scrubbed=f"件名{i}",
        subject_display=f"件名{i}",
        importance="high",
        to_self=True,
        is_unread=False,
        summary="要約",
        ack_token=ack_token,
    )


def _slack_item(i: int, *, ack_token: str = "") -> SlackUnreadItem:
    return SlackUnreadItem(
        channel_id=_CHANNEL_ID,
        channel_kind="channel",
        channel_name_display=f"ch-{i}",
        excerpt_display="確認お願いします。",
        occurred_at="2026-07-13T09:00:00+09:00",
        permalink=f"https://vector.slack.com/archives/{_CHANNEL_ID}/p{_TS.replace('.', '')}",
        thread_message_count=2,
        ack_token=ack_token,
    )


def _digest(
    *,
    n_high: int = 2,
    n_slack: int = 2,
    mail_tokens: bool = True,
    slack_tokens: bool = True,
    ack_all: str = "",
) -> MorningDigestOutput:
    return MorningDigestOutput(
        user_email_masked="s***@vectorinc.co.jp",
        mail_digest=[
            _mail(i, ack_token=f"tok-mail-{i}" if mail_tokens else "") for i in range(n_high)
        ],
        calendar_events=[
            CalendarEventItem(summary_scrubbed="予定", start_at="2026-07-14T01:00:00+09:00")
        ],
        calendar_date="2026-07-14",
        slack_unread_scanned=True,
        slack_unread=[
            _slack_item(i, ack_token=f"tok-slack-{i}" if slack_tokens else "")
            for i in range(n_slack)
        ],
        ack_all_token=ack_all,
    )


def _render(digest: MorningDigestOutput, *, compact: bool) -> list[dict[str, Any]]:
    fn = mod._format_block_kit_compact if compact else mod._format_block_kit
    return fn(digest, "me@vectorinc.co.jp")[1]


def _actions(blocks: list[dict[str, Any]], action_id: str) -> list[dict[str, Any]]:
    """actions.elements と section.accessory の両方から該当ボタンを集める。"""
    found: list[dict[str, Any]] = []
    for b in blocks:
        for e in b.get("elements", []) or []:
            if isinstance(e, dict) and e.get("action_id") == action_id:
                found.append(e)
        acc = b.get("accessory")
        if isinstance(acc, dict) and acc.get("action_id") == action_id:
            found.append(acc)
    return found


@pytest.fixture
def ack_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MORNING_DIGEST_ACK_BUTTON", "1")


@pytest.fixture(autouse=True)
def _ack_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MORNING_DIGEST_ACK_BUTTON", raising=False)


# ── 後方互換 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("compact", [False, True])
def test_flag_off_output_is_byte_identical(compact: bool) -> None:
    """OFF のとき、ack_token を持つ digest でも従来と完全に同じ blocks を出す。

    「新機能を入れたら、OFF のはずの本番の朝 DM が微妙に変わっていた」を防ぐ回帰。
    トークンの有無だけが違う 2 つの digest を描画して、出力が一致することを見る。
    """
    with_tokens = _render(_digest(ack_all="tok-all"), compact=compact)
    without_tokens = _render(
        _digest(mail_tokens=False, slack_tokens=False, ack_all=""), compact=compact
    )
    assert json.dumps(with_tokens, ensure_ascii=False, sort_keys=True) == json.dumps(
        without_tokens, ensure_ascii=False, sort_keys=True
    )
    assert _actions(with_tokens, _ACK) == []


# ── メール行のボタン ─────────────────────────────────────────────────────


@pytest.mark.parametrize("compact", [False, True])
def test_mail_rows_get_ack_button(ack_on: None, compact: bool) -> None:
    blocks = _render(_digest(n_high=2, n_slack=0), compact=compact)
    buttons = _actions(blocks, _ACK)
    assert [b["value"] for b in buttons] == ["tok-mail-0", "tok-mail-1"]
    # ⚠️ 同じ行の「✅ 下書きを確認」と見分けが付くこと（✅ の使い回しを禁じている）
    assert all(b["text"]["text"] == "☑️ 確認済みにする" for b in buttons)


@pytest.mark.parametrize("compact", [False, True])
def test_empty_token_renders_no_button(ack_on: None, compact: bool) -> None:
    blocks = _render(_digest(n_high=2, n_slack=0, mail_tokens=False), compact=compact)
    assert _actions(blocks, _ACK) == []


# ── 💬 Slack 返信漏れ ────────────────────────────────────────────────────


@pytest.mark.parametrize("compact", [False, True])
def test_slack_cards_become_sections_with_accessory(ack_on: None, compact: bool) -> None:
    blocks = _render(_digest(n_high=0, n_slack=2), compact=compact)
    accessories = [
        b["accessory"]
        for b in blocks
        if isinstance(b.get("accessory"), dict) and b["accessory"].get("action_id") == _ACK
    ]
    assert [a["value"] for a in accessories] == ["tok-slack-0", "tok-slack-1"]
    assert all(a["text"]["text"] == "☑️ 確認済み" for a in accessories)


@pytest.mark.parametrize("compact", [False, True])
def test_slack_card_without_token_keeps_section(ack_on: None, compact: bool) -> None:
    """token が無いカードもカード自体は出す（ボタンだけ付かない）。"""
    blocks = _render(_digest(n_high=0, n_slack=2, slack_tokens=False), compact=compact)
    assert _actions(blocks, _ACK) == []
    assert any("ch-0" in json.dumps(b, ensure_ascii=False) for b in blocks)


@pytest.mark.parametrize("compact", [False, True])
def test_handoff_failure_does_not_kill_the_dm(
    ack_on: None, compact: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """💬 の描画が落ちても DM 全体は生成される（fail-safe 境界の回帰）。

    毎朝 9:30 の配信で、💬 の判定層の想定外例外がメールも予定も巻き添えにして
    「1 通も届かない」になるのが最悪の壊れ方。ここを守れているかを直接見る。
    """

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("triage exploded")

    monkeypatch.setattr(mod._handoff, "triage_slack_handoff", _boom)
    blocks = _render(_digest(n_high=1, n_slack=2), compact=compact)
    dumped = json.dumps(blocks, ensure_ascii=False)
    assert mod._HANDOFF_FAILED_LINE in dumped  # 💬 は 1 行へ縮退
    assert "件名0" in dumped  # メールは残る
    assert "予定" in dumped  # 予定も残る


# ── 一括ボタン ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("compact", [False, True])
def test_ack_all_button_appears_once_at_the_end(ack_on: None, compact: bool) -> None:
    blocks = _render(_digest(ack_all="tok-all"), compact=compact)
    bulk = [b for b in _actions(blocks, _ACK) if b["value"] == "tok-all"]
    assert len(bulk) == 1
    assert bulk[0]["text"]["text"] == "☑️ 全部確認した"
    # 脚注（context）より前・本文より後＝末尾側にいること
    idx = next(
        i
        for i, b in enumerate(blocks)
        if any(e.get("value") == "tok-all" for e in b.get("elements", []) or [])
    )
    assert any(b.get("type") == "context" for b in blocks[idx:])


@pytest.mark.parametrize("compact", [False, True])
def test_ack_all_absent_when_token_empty(ack_on: None, compact: bool) -> None:
    """トークンがサイズ超過などで空なら一括ボタンは出さない（個別ボタンは残る）。"""
    blocks = _render(_digest(ack_all=""), compact=compact)
    assert [b["value"] for b in _actions(blocks, _ACK) if b["value"] == "tok-all"] == []
    assert _actions(blocks, _ACK), "個別ボタンは出ている"


def test_legacy_render_stays_under_slack_block_limit(ack_on: None) -> None:
    """旧描画には打ち切りが無いので、最大負荷でも Slack の 50 ブロック上限を割らないこと。

    💬 をカード単位の section に割ったぶん（+4）と一括ボタン（+1）が効いてくるのは
    ここ。上限を越えると Slack が message ごと拒否＝その朝は 1 通も届かない。
    """
    digest = _digest(n_high=40, n_slack=25, ack_all="tok-all")
    blocks = _render(digest, compact=False)
    assert len(blocks) < 50, f"blocks={len(blocks)}"
    assert len([b for b in _actions(blocks, _ACK) if b["value"] == "tok-all"]) == 1


def test_compact_truncation_never_drops_the_bulk_button(ack_on: None) -> None:
    """blocks 打ち切りが起きても一括ボタンは残る。

    ここが消えると「押したつもりが押せていない」という見えない失敗になる。
    """
    digest = _digest(n_high=40, n_slack=5, ack_all="tok-all")
    blocks = _render(digest, compact=True)
    assert len(blocks) <= mod._COMPACT_MAX_BLOCKS
    assert len([b for b in _actions(blocks, _ACK) if b["value"] == "tok-all"]) == 1


# ── G3 ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("compact", [False, True])
def test_no_bare_slack_ids_outside_link_markup(ack_on: None, compact: bool) -> None:
    """生 Slack ID が **本文として** 出ない（既存 G3 ガードの回帰）。

    ⚠️ permalink（`〔<https://…/archives/C…/p…|開く>〕`）は意図的な導線で、
    `_LINK_MARKUP_RE` が生 ID 検査から URL を退避する設計。ここを「ID が 1 度も
    出ない」と書くと、実装ではなく仕様のほうを誤って固定してしまう。守るべきは
    「リンクの外に裸の ID が出ないこと」。
    """
    blocks = _render(_digest(n_high=1, n_slack=2), compact=compact)
    dumped = json.dumps(blocks, ensure_ascii=False)
    outside_links = mod._LINK_MARKUP_RE.sub("", dumped)
    assert _CHANNEL_ID not in outside_links
    assert _TS.replace(".", "") not in outside_links


@pytest.mark.parametrize("compact", [False, True])
def test_button_values_carry_no_raw_ids(ack_on: None, compact: bool) -> None:
    """ボタンの value は署名トークンだけ。生 ID を載せる経路が無いことを見る。"""
    blocks = _render(_digest(n_high=1, n_slack=2, ack_all="tok-all"), compact=compact)
    values = [b["value"] for b in _actions(blocks, _ACK)]
    assert values, "ボタンが描画されていること"
    for value in values:
        assert _CHANNEL_ID not in value
        assert "slack.com" not in value
        assert "@" not in value
