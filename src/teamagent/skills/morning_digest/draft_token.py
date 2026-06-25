"""morning_digest のボタン用 署名トークン（HMAC-SHA256）。

生の thread_id を Slack の button `value` やログに出さないため（G3）、
`thread_id + 所有者ハッシュ + 失効時刻` を HMAC 署名し base64url 化する。
Fargate（digest 描画）が encode、worker（押下処理）が decode する。両者で同じ鍵を使う。

鍵は env `MAIL_ACTION_HMAC_SECRET`、無ければ `SLACK_BOT_TOKEN` を流用する
（Fargate / worker のどちらも SLACK_BOT_TOKEN を保持しているため追加インフラ不要）。
鍵が一切無い環境では fail-closed（decode が常に None ＝ ボタンは機能しない）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

_DEFAULT_TTL_S = 60 * 60 * 24  # 24h（朝の DM をその日のうちに押せれば十分）
_SIG_LEN = 16  # HMAC-SHA256 の先頭 16 バイトで十分（トークンを短く保つ）


def _secret() -> bytes:
    s = os.environ.get("MAIL_ACTION_HMAC_SECRET") or os.environ.get("SLACK_BOT_TOKEN") or ""
    return s.encode("utf-8")


def _owner_hash(owner_email: str) -> str:
    """所有者 email を平文で持たず、照合用ハッシュにする（DM 限定だが多層防御）。"""
    norm = (owner_email or "").strip().lower()
    return hashlib.sha256(("owner:" + norm).encode("utf-8")).hexdigest()[:16]


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encode_draft_token(
    thread_id: str,
    owner_email: str,
    *,
    now: int | None = None,
    ttl_s: int = _DEFAULT_TTL_S,
) -> str:
    """thread_id を所有者・失効付きで HMAC 署名し、Slack button value 用文字列にする。"""
    issued = int(now if now is not None else time.time())
    payload = {"t": str(thread_id), "o": _owner_hash(owner_email), "e": issued + int(ttl_s)}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_secret(), raw, hashlib.sha256).digest()[:_SIG_LEN]
    return _b64e(raw) + "." + _b64e(sig)


def decode_draft_token(token: str, owner_email: str, *, now: int | None = None) -> str | None:
    """検証して thread_id を返す。署名不一致 / 失効 / 所有者不一致 は None（fail-closed）。

    鍵未設定や形式不正も None。owner_email は押下した Slack ユーザーから解決した本人 email。
    """
    secret = _secret()
    if not secret:
        return None  # 鍵が無ければ何も信用しない
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
        return None  # 失効
    if payload.get("o") != _owner_hash(owner_email):
        return None  # 押下者と所有者が不一致
    tid = str(payload.get("t", ""))
    return tid or None
