"""朝ダイジェスト「確認済み」操作用の署名トークン（HMAC-SHA256）。

Gmail スレッドと Slack 返信漏れカードの確認状態を操作するため、種別・ハッシュ済み
item key・基準値を Slack button ``value`` に載せる。生の Gmail ``thread_id``、
Slack permalink、``channel_id`` は載せない。HMAC が守るのは改竄防止と所有者束縛
（完全性）であり、秘匿ではない（payload は base64url を戻せば読める）。本人の
Slack DM 内だけに置かれる前提である。

ダイジェスト描画側が encode、押下処理側が decode する。draft/event と同じメール
action 主鍵・TTL を共有する一方、専用 purpose と厳密な ``typ`` を持つ version 2 とし、
他用途への転用を防ぐ。新規機能なので legacy 検証分岐は持たない。通常の確認 token は
未設定時24h、設定時1..24hの ``MAIL_ACTION_TTL_S`` に従い、取り消し token は明示的に
1時間とする。鍵・TTL・形式が不正なら常に None（fail-closed）で、
``SLACK_BOT_TOKEN`` / ``DATABASE_URL`` への fallback はしない。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from teamagent.hmac_keyring import (
    HMAC_PURPOSE_DIGEST_ACK,
    add_token_ttl,
    coerce_epoch_seconds,
    load_mail_action_hmac_keyring,
    load_mail_action_token_ttl_s,
    validate_epoch_seconds,
)
from teamagent.skills.morning_digest.draft_token import (
    _SIG_LEN,
    _b64d,
    _b64e,
    _owner_hash,
)

_TOKEN_VERSION = 2
_TOKEN_TYPES = frozenset({"ack", "ackall", "unack"})
_PAYLOAD_FIELDS = frozenset({"v", "typ", "n", "o", "e"})
_ITEM_KINDS = frozenset({"m", "s"})
_ITEM_KEY_RE = re.compile(r"[0-9a-f]{16}")
_UNACK_TTL_S = 60 * 60
_MAX_BUTTON_VALUE = 1900


@dataclass(frozen=True)
class AckItem:
    """確認状態を識別する、ハッシュ済み項目鍵と新着判定の基準値。"""

    item_kind: str
    item_key: str
    anchor: int


@dataclass(frozen=True)
class AckTokenPayload:
    """検証済みトークンから復元した確認操作。"""

    kind: str
    items: tuple[AckItem, ...]


def _valid_item(item: AckItem) -> bool:
    """生 ID や曖昧な値を載せないため、AckItem を厳密に検証する。"""
    return (
        type(item.item_kind) is str
        and item.item_kind in _ITEM_KINDS
        and type(item.item_key) is str
        and _ITEM_KEY_RE.fullmatch(item.item_key) is not None
        and type(item.anchor) is int
        and item.anchor >= 0
    )


def _compact_items(
    items: Sequence[AckItem], *, include_anchor: bool
) -> list[list[str | int]] | None:
    compact: list[list[str | int]] = []
    try:
        for item in items:
            if not _valid_item(item):
                return None
            if include_anchor:
                compact.append([item.item_kind, item.item_key, item.anchor])
            else:
                compact.append([item.item_kind, item.item_key])
    except Exception:
        return None
    return compact


def _encode_token(
    kind: str,
    items: Sequence[AckItem],
    owner_email: str,
    *,
    now: int | None,
    ttl_s: int | None,
) -> str | None:
    try:
        if kind not in _TOKEN_TYPES:
            return None
        item_count = len(items)
        if (kind == "ack" and item_count != 1) or (kind != "ack" and item_count == 0):
            return None
        compact = _compact_items(items, include_anchor=kind != "unack")
        issued = coerce_epoch_seconds(now)
        ttl = load_mail_action_token_ttl_s(explicit_ttl_s=ttl_s)
        if compact is None or issued is None or ttl is None:
            return None
        expires = add_token_ttl(issued, ttl)
        keyring = load_mail_action_hmac_keyring(now=issued)
        if expires is None or keyring is None:
            return None
        payload = {
            "v": _TOKEN_VERSION,
            "typ": kind,
            "n": compact,
            "o": _owner_hash(owner_email),
            "e": expires,
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signature = keyring.sign(
            raw,
            purpose=HMAC_PURPOSE_DIGEST_ACK,
            digest_bytes=_SIG_LEN,
        )
        token = _b64e(raw) + "." + _b64e(signature)
        if len(token.encode("utf-8")) > _MAX_BUTTON_VALUE:
            return None
        return token
    except Exception:
        return None


def encode_ack_token(
    items: Sequence[AckItem],
    owner_email: str,
    *,
    now: int | None = None,
    ttl_s: int | None = None,
) -> str | None:
    """単一項目の確認操作を所有者・失効付きで署名する。"""
    return _encode_token("ack", items, owner_email, now=now, ttl_s=ttl_s)


def encode_ack_all_token(
    items: Sequence[AckItem],
    owner_email: str,
    *,
    now: int | None = None,
    ttl_s: int | None = None,
) -> str | None:
    """一括確認操作を所有者・失効付きで署名する。"""
    return _encode_token("ackall", items, owner_email, now=now, ttl_s=ttl_s)


def encode_unack_token(
    items: Sequence[AckItem],
    owner_email: str,
    *,
    now: int | None = None,
) -> str | None:
    """押下直後の取り消し操作を、明示的な1時間 TTL で署名する。"""
    return _encode_token("unack", items, owner_email, now=now, ttl_s=_UNACK_TTL_S)


def decode_ack_token(
    token: str,
    owner_email: str,
    *,
    now: int | None = None,
) -> AckTokenPayload | None:
    """検証済み操作を返す。不一致・失効・所有者違い・形式不正は None。"""
    try:
        cur = coerce_epoch_seconds(now)
        if cur is None:
            return None
        keyring = load_mail_action_hmac_keyring(now=cur)
        if keyring is None or type(token) is not str or token.count(".") != 1:
            return None
        body_b64, sig_b64 = token.split(".", 1)
        if not body_b64 or not sig_b64:
            return None
        raw = _b64d(body_b64)
        signature = _b64d(sig_b64)
        # urlsafe_b64decode は一部の非正規表現も受理するので、元表現との一致も要求する。
        if _b64e(raw) != body_b64 or _b64e(signature) != sig_b64:
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != _PAYLOAD_FIELDS:
            return None
        version = payload.get("v")
        kind = payload.get("typ")
        if type(version) is not int or version != _TOKEN_VERSION:
            return None
        if type(kind) is not str or kind not in _TOKEN_TYPES:
            return None
        if not keyring.verify(
            raw,
            signature,
            purpose=HMAC_PURPOSE_DIGEST_ACK,
            digest_bytes=_SIG_LEN,
        ):
            return None
        expires = validate_epoch_seconds(payload.get("e"))
        if expires is None or expires <= cur:
            return None
        if payload.get("o") != _owner_hash(owner_email):
            return None
        compact = payload.get("n")
        if type(compact) is not list:
            return None
        if (kind == "ack" and len(compact) != 1) or (kind != "ack" and not compact):
            return None

        expected_item_len = 2 if kind == "unack" else 3
        items: list[AckItem] = []
        for value in compact:
            if type(value) is not list or len(value) != expected_item_len:
                return None
            anchor = 0 if kind == "unack" else value[2]
            item = AckItem(item_kind=value[0], item_key=value[1], anchor=anchor)
            if not _valid_item(item):
                return None
            items.append(item)
        return AckTokenPayload(kind=kind, items=tuple(items))
    except Exception:
        return None


def ack_hmac_configured() -> bool:
    """確認 token を発行できる有効なメール action keyring と TTL があるか。"""
    try:
        return (
            load_mail_action_hmac_keyring() is not None
            and load_mail_action_token_ttl_s() is not None
        )
    except Exception:
        return False


__all__ = [
    "AckItem",
    "AckTokenPayload",
    "ack_hmac_configured",
    "decode_ack_token",
    "encode_ack_all_token",
    "encode_ack_token",
    "encode_unack_token",
]
