"""レポート短縮リンク用 署名トークン（HMAC-SHA256）。

openclaw(@AiLa) の LLM が長い presigned URL のクエリ（``?X-Amz-Signature…``）を
削って壊す問題を根治するため、配布URLを **クエリ無しの短い**
``https://connect.newstv.co.jp/r/<token>`` にする。``<token>`` は S3 の
``bucket/key`` を HMAC 署名で埋めた不透明文字列。connect-web の ``/r`` が署名検証して
毎回新鮮な presigned を生成し 302 する（S3 オブジェクトが生きていれば期限切れしない）。

信頼境界は現行 presigned URL（＝リンクを知る人が時限で閲覧）と同一。発行側（mcp/skill）と
復号側（connect-web）は同一イメージ・**同一鍵 ``MAIL_ACTION_HMAC_SECRET``（=database_url
secret）** を使う。draft_token と違い ``SLACK_BOT_TOKEN`` への fallback は**使わない**:
connect-web は SLACK_BOT_TOKEN を持たないため、発行側だけが fallback すると鍵不一致で全件 404
になる footgun を断つ（署名鍵を単一化）。鍵が無い環境では fail-closed（decode が常に None）。
発行側は短縮URLを出す前に :func:`has_secret` で鍵存在を確認し、無ければ presigned へ落とす。

多層防御: たとえ有効な署名でも、用途タグ ``typ`` 不一致・key が許可プレフィックス
（``vseo-reports/`` / ``vseo-proposals/``）以外・bucket が許可バケット以外なら decode は None を
返す（他用途トークン(draft/event)の転用・任意 S3 オブジェクトの読み取り転用を封じる）。
draft_token.py の作法を踏襲。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

_TOKEN_TYPE = "r"  # 用途タグ（同一鍵の draft/event 等とドメイン分離＝クロス転用を封じる）
_DEFAULT_BUCKET = "teamagent-dev-raw-files"  # report_publish._DEFAULT_BUCKET と一致
_ALLOWED_KEY_PREFIXES = ("vseo-reports/", "vseo-proposals/")  # 発行しうる prefix のみ許可
_DEFAULT_TTL_S = 60 * 60 * 24 * 30  # 30日（旧 presigned 7日より長い恒久寄りリンク）
_SIG_LEN = 16  # HMAC-SHA256 の先頭16バイト（トークンを短く保つ・draft_token と同じ）


def _secret() -> bytes:
    # 発行(mcp)↔復号(connect-web)で同一値になる MAIL_ACTION_HMAC_SECRET のみ。SLACK_BOT_TOKEN
    # への fallback はしない（connect-web が持たず鍵不一致→全件404 を招くため）。
    return os.environ.get("MAIL_ACTION_HMAC_SECRET", "").encode("utf-8")


def has_secret() -> bool:
    """署名鍵(MAIL_ACTION_HMAC_SECRET)が設定済みか。短縮URL発行前のゲートに使う。

    未設定なら発行側は短縮URL化せず従来 presigned へ落とす（鍵不一致による全件404の回避）。
    """
    return bool(os.environ.get("MAIL_ACTION_HMAC_SECRET", "").strip())


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
    ttl_s: int = _DEFAULT_TTL_S,
) -> str:
    """S3 の bucket/key(+発行時 region) を失効付きで HMAC 署名し、``/r/<token>`` 用文字列にする。

    region は発行側が把握しているバケットのリージョン。/r が presigned を再生成する際に使い、
    バケットがサービスの AWS_REGION と別リージョンでも署名リージョン不一致(403)を避ける。空可。
    """
    issued = int(now if now is not None else time.time())
    payload = {
        "typ": _TOKEN_TYPE,
        "b": str(bucket),
        "k": str(key),
        "r": str(region or ""),
        "e": issued + int(ttl_s),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_secret(), raw, hashlib.sha256).digest()[:_SIG_LEN]
    return _b64e(raw) + "." + _b64e(sig)


def decode_report_token(token: str, *, now: int | None = None) -> tuple[str, str, str] | None:
    """検証して (bucket, key, region) を返す。以下はすべて None（fail-closed）:

    鍵未設定 / 形式不正 / 署名不一致 / 失効 / typ 不一致 / key が許可 prefix 外 / bucket が許可外。
    region は埋込が無ければ ""（/r 側で AWS_REGION にフォールバック）。
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
    if payload.get("typ") != _TOKEN_TYPE:
        return None  # 他用途トークン(draft/event 等・同一鍵)の転用を封じる
    if int(payload.get("e", 0)) < cur:
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
