"""レポート短縮リンク用 署名トークン（HMAC-SHA256）。

openclaw(@AiLa) の LLM が長い presigned URL のクエリ（``?X-Amz-Signature…``）を
削って壊す問題を根治するため、配布URLを **クエリ無しの短い**
``https://connect.newstv.co.jp/r/<token>`` にする。``<token>`` は S3 の
``bucket/key`` を HMAC 署名で埋めた不透明文字列。connect-web の ``/r`` が署名検証して
毎回新鮮な presigned を生成し 302 する（S3 オブジェクトが生きていれば期限切れしない）。

信頼境界は現行 presigned URL（＝リンクを知る人が時限で閲覧）と同一。発行側（mcp/skill）と
復号側（connect-web）はレポート専用主鍵 ``REPORT_LINK_HMAC_SECRET`` を共有する。新規発行は
必ず主鍵だけを使い、移行前 token は ``REPORT_LINK_HMAC_PREVIOUS_SECRET`` と明示的な
``..._VALID_UNTIL``（最大7日）を設定した期間だけ検証する。``MAIL_ACTION_HMAC_SECRET`` /
``DATABASE_URL`` / ``SLACK_BOT_TOKEN`` への fallback は一切しない。鍵が無い・空・短すぎる・
他資格情報/用途と同じ・previous 設定不正の環境では fail-closed。発行側は短縮URLを出す前に
:func:`has_secret` で有効な keyring を確認し、無ければ presigned へ落とす。

多層防御: たとえ有効な署名でも、用途タグ ``typ`` 不一致・key が許可プレフィックス
（``vseo-reports/`` / ``vseo-proposals/``）以外・bucket が許可バケット以外なら decode は None を
返す（他用途トークン(draft/event)の転用・任意 S3 オブジェクトの読み取り転用を封じる）。
draft_token.py の作法を踏襲。
"""

from __future__ import annotations

import base64
import json
import os
import time

from teamagent.hmac_keyring import (
    load_report_link_hmac_keyring,
    require_report_link_hmac_keyring,
)

_TOKEN_TYPE = "r"  # 用途タグ（同一鍵の draft/event 等とドメイン分離＝クロス転用を封じる）
_DEFAULT_BUCKET = "teamagent-dev-raw-files"  # report_publish._DEFAULT_BUCKET と一致
_ALLOWED_KEY_PREFIXES = ("vseo-reports/", "vseo-proposals/")  # 発行しうる prefix のみ許可
# 既定 7日（旧 presigned と同等の露出窓）。トークンはアクセスログに残りうる capability なので
# 恒久寿命にしない（過去施策の恒久記録は AiLaVault(Part1)側が担う）。REPORT_LINK_TTL_S で調整可。
_DEFAULT_TTL_S = 60 * 60 * 24 * 7
_SIG_LEN = 16  # HMAC-SHA256 の先頭16バイト（トークンを短く保つ・draft_token と同じ）


def _default_ttl_s() -> int:
    """トークン失効までの秒数。env REPORT_LINK_TTL_S(正整数)があれば優先、無ければ既定7日。"""
    raw = os.environ.get("REPORT_LINK_TTL_S", "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else _DEFAULT_TTL_S


def has_secret() -> bool:
    """有効なレポート専用 keyring が設定済みか。短縮URL発行前のゲートに使う。

    未設定・不正なら発行側は短縮URL化せず従来 presigned へ落とす（不正署名の発行を防止）。
    """
    return load_report_link_hmac_keyring() is not None


def is_allowed_key(key: str) -> bool:
    """短縮リンクを発行してよい key か（decode と同一 allowlist）。

    発行側の事前チェック用。カスタム VSEO_REPORT_PREFIX 等で許可外 prefix に置かれた成果物に
    /r トークンを出すと decode が拒否して 404 になるため、発行前にここで弾き presigned へ落とす。
    """
    return bool(key) and str(key).startswith(_ALLOWED_KEY_PREFIXES)


def _allowed_bucket() -> str:
    return os.environ.get("VSEO_REPORT_BUCKET") or _DEFAULT_BUCKET


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encode_report_token(
    bucket: str,
    key: str,
    *,
    region: str = "",
    now: int | None = None,
    ttl_s: int | None = None,
) -> str:
    """S3 の bucket/key(+発行時 region) を失効付きで HMAC 署名し、``/r/<token>`` 用文字列にする。

    region は発行側が把握しているバケットのリージョン。/r が presigned を再生成する際に使い、
    バケットがサービスの AWS_REGION と別リージョンでも署名リージョン不一致(403)を避ける。空可。
    """
    issued = int(now if now is not None else time.time())
    ttl = int(ttl_s) if ttl_s is not None else _default_ttl_s()
    payload = {
        "typ": _TOKEN_TYPE,
        "b": str(bucket),
        "k": str(key),
        "r": str(region or ""),
        "e": issued + ttl,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    # require は秘密を含まない固定例外を投げる。空鍵で token を発行する旧挙動は許可しない。
    sig = require_report_link_hmac_keyring(now=issued).sign(raw, digest_bytes=_SIG_LEN)
    return _b64e(raw) + "." + _b64e(sig)


def decode_report_token(token: str, *, now: int | None = None) -> tuple[str, str, str] | None:
    """検証して (bucket, key, region) を返す。以下はすべて None（fail-closed）:

    鍵未設定 / 形式不正 / 署名不一致 / 失効 / typ 不一致 / key が許可 prefix 外 / bucket が許可外。
    region は埋込が無ければ ""（/r 側で AWS_REGION にフォールバック）。
    """
    cur = int(now if now is not None else time.time())
    keyring = load_report_link_hmac_keyring(now=cur)
    if keyring is None:
        return None  # 鍵が無い/設定不正なら何も信用しない
    try:
        body_b64, sig_b64 = (token or "").split(".", 1)
        raw = _b64d(body_b64)
        if not keyring.verify(raw, _b64d(sig_b64), digest_bytes=_SIG_LEN):
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        expires = int(payload.get("e", 0))
    except Exception:
        return None
    if payload.get("typ") != _TOKEN_TYPE:
        return None  # 他用途トークン(draft/event 等・同一鍵)の転用を封じる
    if expires < cur:
        return None  # 失効
    bucket = str(payload.get("b", ""))
    key = str(payload.get("k", ""))
    if not bucket or not key:
        return None
    if bucket != _allowed_bucket():  # 任意バケットへの転用を封じる
        return None
    if not key.startswith(_ALLOWED_KEY_PREFIXES):  # 任意 key への転用を封じる
        return None
    return bucket, key, str(payload.get("r", ""))
