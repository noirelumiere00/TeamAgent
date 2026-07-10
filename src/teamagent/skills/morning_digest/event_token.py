"""朝ダイジェスト「📅 カレンダーに登録」ボタン用の署名トークン（HMAC-SHA256）。

draft_token.py と同じ方式（鍵・署名・base64url・fail-closed）で、確定 MTG の
日時・タイトルを Slack button value に載せる。HMAC は **改竄・鋳造・他人使用の防止**
（完全性と所有者束縛）であり **秘匿ではない**（base64 なので読める。本人 DM 内にのみ
置かれる前提）＋24h 失効。

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


def stable_event_id(start_iso: str, end_iso: str, owner_email: str) -> str:
    """冪等 event_id（base32hex 小文字）を導出する（連打＋翌日再ダイジェスト対策）。

    トークン全体でなく **安定フィールド（所有者×開始×終了）** から導出する:
    同一スレッドは lookback（既定3日）の間ダイジェストに再登場し、都度 token の
    失効時刻が変わる。token ハッシュだと毎日別 id＝翌日押すと二重登録になるため
    （反対尋問レビュー F3）、日時が同じなら同じ id → Google 側 409 で冪等になる。
    title は LLM の揺れがあるため含めない。
    hexdigest（0-9a-f）は base32hex アルファベット [a-v0-9] の部分集合＝形式安全。
    ⚠️ トレードオフ: UI から手動削除した同一予定を再登録しようとしても 409
    （「登録済み」案内）になる。その場合は手動作成が必要（既知の制限・adapter docstring 参照）。
    """
    basis = f"{_owner_hash(owner_email)}|{start_iso}|{end_iso}"
    return "aila" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:40]


__all__ = [
    "MeetingEventPayload",
    "decode_event_token",
    "encode_event_token",
    "stable_event_id",
]
