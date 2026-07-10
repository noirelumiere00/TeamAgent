"""朝ダイジェスト「📅 カレンダーに登録」ボタン用の署名トークン（HMAC-SHA256）。

draft_token.py と同じ方式（鍵・署名・base64url・fail-closed）で、確定 MTG の
日時・タイトルを Slack button value に安全に載せる。生の予定情報を value に
平文で置かない（G3）＋押下者と所有者の照合＋24h 失効。

Fargate（digest 描画）が encode、calendar_event skill（押下処理）が decode する。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from teamagent.skills.morning_digest.draft_token import (
    _SIG_LEN,
    _b64d,
    _b64e,
    _owner_hash,
    _secret,
)

_DEFAULT_TTL_S = 60 * 60 * 24  # 24h（draft_token と同一の運用境界）


@dataclass(frozen=True)
class MeetingEventPayload:
    """検証済みトークンから復元した予定情報。"""

    start_iso: str
    end_iso: str
    title: str


def encode_event_token(
    *,
    start_iso: str,
    end_iso: str,
    title: str,
    owner_email: str,
    now: int | None = None,
    ttl_s: int = _DEFAULT_TTL_S,
) -> str:
    """確定 MTG の日時/タイトルを所有者・失効付きで署名し button value 用文字列にする。"""
    issued = int(now if now is not None else time.time())
    payload = {
        "s": str(start_iso),
        "n": str(end_iso),
        "l": str(title)[:60],
        "o": _owner_hash(owner_email),
        "e": issued + int(ttl_s),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_secret(), raw, hashlib.sha256).digest()[:_SIG_LEN]
    return _b64e(raw) + "." + _b64e(sig)


def decode_event_token(
    token: str, owner_email: str, *, now: int | None = None
) -> MeetingEventPayload | None:
    """検証して予定情報を返す。署名不一致/失効/所有者不一致/形式不正は None（fail-closed）。"""
    secret = _secret()
    if not secret:
        return None
    try:
        body_b64, sig_b64 = (token or "").split(".", 1)
        raw = _b64d(body_b64)
        expected = hmac.new(secret, raw, hashlib.sha256).digest()[:_SIG_LEN]
        if not hmac.compare_digest(expected, _b64d(sig_b64)):
            return None
        payload = json.loads(raw)
    except Exception:
        return None
    cur = int(now if now is not None else time.time())
    if int(payload.get("e", 0)) < cur:
        return None
    if payload.get("o") != _owner_hash(owner_email):
        return None
    start = str(payload.get("s") or "")
    end = str(payload.get("n") or "")
    if not start or not end:
        return None
    return MeetingEventPayload(start_iso=start, end_iso=end, title=str(payload.get("l") or ""))


def stable_event_id(token: str) -> str:
    """トークンから冪等 event_id（base32hex 小文字）を導出する（ボタン連打対策）。

    同一トークン＝同一予定なので、二度押しは Google 側で 409 duplicate になる。
    hexdigest（0-9a-f）は base32hex アルファベット [a-v0-9] の部分集合＝形式安全。
    署名部ではなく本文込み全体をハッシュする（本文が同じなら同じ id）。
    """
    return "aila" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:40]


__all__ = [
    "MeetingEventPayload",
    "decode_event_token",
    "encode_event_token",
    "stable_event_id",
]
