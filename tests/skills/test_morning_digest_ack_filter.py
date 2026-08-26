"""朝ダイジェスト「確認済み」除外フィルタのテスト（DB なし・課金 0）。

検証観点:
  - 既定 OFF では ack ストアに一切触らない（後方互換）
  - 確認済み かつ 新着なし → 隠す
  - 確認済み だが **新着あり** → 隠さない（裁定: 返信が来たら再浮上）
  - ストア障害は fail-open（1 件も隠さない）＝見逃し防止の向きを守る
  - 一括トークンに、その朝出した項目がちょうど載る
  - G3: 生 thread_id / channel_id がトークンにも出力にも現れない
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import pytest

from teamagent.adapters.digest_ack_store import DigestAckStore
from teamagent.skills.base import SkillContext
from teamagent.skills.morning_digest.ack_token import decode_ack_token
from teamagent.skills.morning_digest.schema import MorningDigestInput
from teamagent.skills.morning_digest.skill import MorningDigestSkill

ME = "me@vectorinc.co.jp"
_HMAC_SECRET = "mail-primary-" + "m" * 32
#: fake gmail の internal_date_ms（ミリ秒）。anchor は秒に落として持つ。
_MAIL_MS = 1_718_681_400_000
_MAIL_ANCHOR = _MAIL_MS // 1000


# ── fakes ────────────────────────────────────────────────────────────────


@dataclass
class _Ref:
    id: str


@dataclass
class _Msg:
    headers: dict[str, str]
    payload: dict[str, Any]
    internal_date_ms: int
    label_ids: tuple[str, ...] = ()


class _Gmail:
    """スレッド 2 本（m0 / m1）を返す最小 fake。get_thread は空＝アンカー1通に落ちる。"""

    def __init__(self, msgs: list[_Msg]) -> None:
        self._msgs = msgs

    def list_messages(self, query: str, request_id: str, max_results: int = 30) -> Any:
        return ([_Ref(id=f"m{i}") for i in range(len(self._msgs))], None)

    def get_message(self, msg_id: str, request_id: str) -> _Msg:
        return self._msgs[int(msg_id.lstrip("m"))]

    def get_thread(self, thread_id: str, request_id: str) -> list[_Msg]:
        return []

    def list_drafts(self, request_id: str, **_: Any) -> list[Any]:
        return []


class _GCal:
    def list_events(self, request_id: str, **kwargs: Any) -> list[Any]:
        return []


class _TokenStore:
    def get(self, user_email: str) -> Any:
        return object()

    def has(self, user_email: str) -> bool:
        return True

    def put(self, user_email: str, token: Any) -> None:  # pragma: no cover - 未使用
        raise AssertionError("put は呼ばれない")


class _Bedrock:
    def converse(self, **kwargs: Any) -> Any:
        body = json.dumps(
            [{"id": "x", "importance": "high", "summary": ""}], ensure_ascii=False
        )
        return type(
            "R", (), {"text": body, "usage": type("U", (), {"cost_usd": 0.0})()}
        )()


@dataclass
class _Unreplied:
    channel_id: str
    channel_name: str
    ts: str
    text: str
    permalink: str
    occurred_at: str
    thread_message_count: int
    user: str | None = None
    user_display: str | None = None
    channel_kind: str = "channel"
    thread_participant_ids: tuple[str, ...] = ()
    thread_last_user_id: str | None = None
    thread_last_at: str | None = None
    answered_by_other: bool = False
    sender_followed_up: bool = False
    mentioned_user_ids: tuple[str, ...] = ()


class _SlackProvider:
    def __init__(self, items: list[_Unreplied]) -> None:
        self._items = items

    def collect_detailed(self, requester: str, days: int, request_id: str) -> Any:
        return type(
            "C",
            (),
            {
                "items": self._items,
                "total_unreplied": len(self._items),
                "scan_truncated": False,
                "scanned": True,
            },
        )()


class _AckStore:
    """DigestAckStore の差し替え。呼ばれた回数と返す状態を制御する。"""

    def __init__(self, active: dict[tuple[str, str], int] | None = None, *, boom: bool = False):
        self.active_state = active or {}
        self.boom = boom
        self.active_calls = 0
        self.purge_calls = 0

    def active(self, user_email: str, *, request_id: str) -> dict[tuple[str, str], int]:
        self.active_calls += 1
        if self.boom:
            # 本番の store は内部で握るが、握り漏れが起きても DM を落とさないことを見る。
            raise RuntimeError("ack store unavailable")
        return dict(self.active_state)

    def purge_expired(self, user_email: str, *, request_id: str) -> int:
        self.purge_calls += 1
        return 0


# ── fixtures ─────────────────────────────────────────────────────────────


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _msgs() -> list[_Msg]:
    return [
        _Msg(
            headers={"From": "a@x.com", "To": ME, "Subject": "件名A", "Message-ID": "<a>"},
            payload={"body": {"data": _b64("hello a")}},
            internal_date_ms=_MAIL_MS,
        ),
        _Msg(
            headers={"From": "b@x.com", "To": ME, "Subject": "件名B", "Message-ID": "<b>"},
            payload={"body": {"data": _b64("hello b")}},
            internal_date_ms=_MAIL_MS,
        ),
    ]


def _slack_items() -> list[_Unreplied]:
    return [
        _Unreplied(
            channel_id="C111",
            channel_name="general",
            ts="1718681400.000100",
            text="これお願いできますか？",
            permalink="https://x.slack.com/archives/C111/p1718681400000100",
            occurred_at="2026-08-25T10:00:00+09:00",
            thread_message_count=3,
        )
    ]


@pytest.fixture(autouse=True)
def _hmac_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _HMAC_SECRET)
    monkeypatch.delenv("MAIL_ACTION_TTL_S", raising=False)


def _install_store(monkeypatch: pytest.MonkeyPatch, store: _AckStore) -> None:
    """skill が関数内 import する DigestAckStore を差し替える。

    ``DigestAckStore`` は「インスタンス生成」と「``item_key`` の staticmethod 呼び出し」の
    両方で使われる。鍵化は本物をそのまま通す（ここを fake にすると、テストが検証している
    鍵が本番の鍵と別物になり、除外判定の一致を確かめたことにならない）。
    """

    class _Factory:
        item_key = staticmethod(DigestAckStore.item_key)

        def __call__(self, *a: Any, **k: Any) -> _AckStore:
            return store

    monkeypatch.setattr("teamagent.adapters.digest_ack_store.DigestAckStore", _Factory())


def _skill(**kw: Any) -> MorningDigestSkill:
    return MorningDigestSkill(
        token_store=_TokenStore(),
        gmail=_Gmail(_msgs()),
        gcalendar=_GCal(),
        bedrock=_Bedrock(),
        **kw,
    )


def _run(skill: MorningDigestSkill) -> Any:
    return skill.run(
        MorningDigestInput(max_drafts=0),
        SkillContext(request_id="req-ack", metadata={"user_email": ME}),
    )


def _mail_key(raw_id: str) -> str:
    return DigestAckStore.item_key("m", ME, raw_id)


def _slack_key(raw_id: str) -> str:
    return DigestAckStore.item_key("s", ME, raw_id)


# ── テスト ───────────────────────────────────────────────────────────────


def test_filter_off_never_touches_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定 OFF では ack ストアを一度も呼ばない（後方互換・DB 依存を増やさない）。"""
    monkeypatch.delenv("MORNING_DIGEST_ACK_FILTER", raising=False)
    store = _AckStore()
    _install_store(monkeypatch, store)

    out = _run(_skill())

    assert store.active_calls == 0
    assert store.purge_calls == 0
    assert len(out.mail_digest) == 2
    assert out.ack_all_token == ""
    assert all(m.ack_token == "" for m in out.mail_digest)


def test_settled_thread_is_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """確認済み かつ 新着なし → 翌朝は出さない。"""
    monkeypatch.setenv("MORNING_DIGEST_ACK_FILTER", "true")
    _install_store(monkeypatch, _AckStore({("m", _mail_key("m0")): _MAIL_ANCHOR}))

    out = _run(_skill())

    assert [m.subject_display for m in out.mail_digest] == ["件名B"]
    assert out.ack_excluded_mail == 1


def test_new_message_resurfaces_acked_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """確認済みでも、その後に新着があれば隠さない（裁定の本体）。

    ack 時点より 1 秒でも新しいメッセージが来ていれば再浮上する。ここが False に
    倒れると「返信が来たのに気づけない」＝この機能が見逃しを作る側に回る。
    """
    monkeypatch.setenv("MORNING_DIGEST_ACK_FILTER", "true")
    _install_store(monkeypatch, _AckStore({("m", _mail_key("m0")): _MAIL_ANCHOR - 1}))

    out = _run(_skill())

    assert {m.subject_display for m in out.mail_digest} == {"件名A", "件名B"}
    assert out.ack_excluded_mail == 0


def test_store_failure_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """ストア障害では 1 件も隠さない（隠す側に倒さない）。"""
    monkeypatch.setenv("MORNING_DIGEST_ACK_FILTER", "true")
    _install_store(monkeypatch, _AckStore(boom=True))

    out = _run(_skill())

    assert len(out.mail_digest) == 2
    assert out.ack_excluded_mail == 0
    assert out.ack_all_token == ""  # 状態が読めない朝は一括ボタンも出さない


def test_slack_card_hidden_and_resurfaced_by_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slack 返信漏れも同じ規則。基準値はスレッドのメッセージ総数。"""
    monkeypatch.setenv("MORNING_DIGEST_ACK_FILTER", "true")
    key = _slack_key("C111:1718681400.000100")

    _install_store(monkeypatch, _AckStore({("s", key): 3}))
    hidden = _run(_skill(slack=_SlackProvider(_slack_items())))
    assert hidden.slack_unread == []
    assert hidden.ack_excluded_slack == 1

    # 返信が 1 件積まれた（3 → 4）→ 再浮上する
    _install_store(monkeypatch, _AckStore({("s", key): 2}))
    shown = _run(_skill(slack=_SlackProvider(_slack_items())))
    assert len(shown.slack_unread) == 1
    assert shown.ack_excluded_slack == 0


def test_ack_all_token_covers_exactly_shown_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """一括トークンには、その朝に出した項目がちょうど載る（隠した項目は載らない）。"""
    monkeypatch.setenv("MORNING_DIGEST_ACK_FILTER", "true")
    _install_store(monkeypatch, _AckStore({("m", _mail_key("m0")): _MAIL_ANCHOR}))

    out = _run(_skill(slack=_SlackProvider(_slack_items())))

    payload = decode_ack_token(out.ack_all_token, ME)
    assert payload is not None
    assert payload.kind == "ackall"
    assert {(i.item_kind, i.item_key) for i in payload.items} == {
        ("m", _mail_key("m1")),
        ("s", _slack_key("C111:1718681400.000100")),
    }


def test_failed_section_is_excluded_from_ack_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """途中で落ちたセクションの項目を一括ボタンに含めない。

    含めてしまうと「☑️ 全部確認した」が **画面に出ていない項目まで確認済みにする**＝
    押した人からは見えない副作用になる。Slack 側が落ちた朝は、メールぶんだけが載る。
    """
    monkeypatch.setenv("MORNING_DIGEST_ACK_FILTER", "true")
    _install_store(monkeypatch, _AckStore())

    class _BoomSlack:
        def collect_detailed(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("slack provider exploded")

    out = _run(_skill(slack=_BoomSlack()))

    assert out.slack_unread == []
    assert any(e.startswith("slack:") for e in out.errors)
    payload = decode_ack_token(out.ack_all_token, ME)
    assert payload is not None
    assert {i.item_kind for i in payload.items} == {"m"}, "Slack 項目は載らない"
    assert len(payload.items) == 2


def test_tokens_never_carry_raw_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """G3 回帰: 生 thread_id / channel_id / permalink がトークンに出ない。

    トークンは base64url を戻せば読める（秘匿ではない）ので、**中身を復号して**
    生 ID が入っていないことを見る。長さや形だけの検査では素通りする。
    """
    monkeypatch.setenv("MORNING_DIGEST_ACK_FILTER", "true")
    _install_store(monkeypatch, _AckStore())

    out = _run(_skill(slack=_SlackProvider(_slack_items())))

    tokens = [m.ack_token for m in out.mail_digest]
    tokens += [s.ack_token for s in out.slack_unread]
    tokens.append(out.ack_all_token)
    assert all(tokens), "全項目と一括のトークンが発行されていること"

    for token in tokens:
        body = token.split(".", 1)[0]
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
        for secret in ("m0", "m1", "C111", "1718681400.000100", "x.slack.com", ME):
            assert secret not in raw, f"{secret!r} がトークン payload に載っている"
