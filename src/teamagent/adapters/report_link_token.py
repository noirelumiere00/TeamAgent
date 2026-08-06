"""レポート短縮リンク用 署名トークン（HMAC-SHA256）。

openclaw(@NewsTV AI) の LLM が長い presigned URL のクエリ（``?X-Amz-Signature…``）を
削って壊す問題を根治するため、配布URLを **クエリ無しの短い**
``https://connect.newstv.co.jp/r/<token>`` にする。``<token>`` は S3 の
``bucket/key`` を HMAC 署名で埋めた不透明文字列。connect-web の ``/r`` が署名検証して
毎回新鮮な presigned を生成し 302 する（S3 オブジェクトが生きていれば期限切れしない）。

信頼境界は現行 presigned URL（＝リンクを知る人が時限で閲覧）と同一。発行側（mcp/skill）と
復号側（connect-web）はレポート専用主鍵 ``REPORT_LINK_HMAC_SECRET`` を共有する。新規発行は
必ず主鍵だけを使い、移行前 token は ``REPORT_LINK_HMAC_PREVIOUS_SECRET`` と固定した
``..._PREVIOUS_ROTATION_STARTED_AT`` に加えて ``..._PREVIOUS_IS_LEGACY=1`` を設定した
verifier-first 移行期間だけ検証する。新規tokenはレポート専用HMAC目的のversion 2である。
``REPORT_LINK_TTL_S`` は未設定時7日、設定時は ASCII 10進数の1..7日のみ。設定不正や明示TTLの
範囲外では発行せず None を返し、呼出元は presigned へ落とす。``MAIL_ACTION_HMAC_SECRET`` /
``DATABASE_URL`` / ``SLACK_BOT_TOKEN`` への fallback は一切しない。鍵が無い・空・短すぎる・
他資格情報/用途と同じ・previous 設定不正の環境でも fail-closed。

多層防御: たとえ有効な署名でも、用途タグ ``typ`` 不一致・key が許可プレフィックス
（``vseo-reports/`` / ``vseo-proposals/``）以外・bucket が許可バケット以外なら decode は None を
返す（他用途トークン(draft/event)の転用・任意 S3 オブジェクトの読み取り転用を封じる）。
draft_token.py の作法を踏襲。
"""

from __future__ import annotations

import base64
import json
import os

from teamagent.hmac_keyring import (
    HMAC_PURPOSE_REPORT_LINK,
    add_token_ttl,
    coerce_epoch_seconds,
    load_report_link_hmac_keyring,
    load_report_link_token_ttl_s,
    validate_epoch_seconds,
)

_TOKEN_TYPE = "r"  # 用途タグ（同一鍵の draft/event 等とドメイン分離＝クロス転用を封じる）
_TOKEN_VERSION = 2
_LEGACY_FIELDS = frozenset({"typ", "b", "k", "r", "e"})
_DEFAULT_BUCKET = "teamagent-dev-raw-files"  # report_publish._DEFAULT_BUCKET と一致
_ALLOWED_KEY_PREFIXES = ("vseo-reports/", "vseo-proposals/")  # 発行しうる prefix のみ許可
# 既定 7日（旧 presigned と同等の露出窓）。トークンはアクセスログに残りうる capability なので
# 恒久寿命にしない（過去施策の恒久記録は AiLaVault(Part1)側が担う）。REPORT_LINK_TTL_S で調整可。
_SIG_LEN = 16  # HMAC-SHA256 の先頭16バイト（トークンを短く保つ・draft_token と同じ）


def _default_ttl_s() -> int | None:
    """設定済み発行TTL。未設定時7日、設定不正時は None（既定値へ黙って戻さない）。"""
    return load_report_link_token_ttl_s()


def has_secret() -> bool:
    """有効なレポート専用 keyring と発行TTLが設定済みか。

    未設定・不正なら発行側は短縮URL化せず従来 presigned へ落とす。
    """
    try:
        return (
            load_report_link_hmac_keyring() is not None
            and load_report_link_token_ttl_s() is not None
        )
    except Exception:
        return False


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
) -> str | None:
    """S3 の bucket/key(+発行時 region) を失効付きで HMAC 署名し、``/r/<token>`` 用文字列にする。

    region は発行側が把握しているバケットのリージョン。/r が presigned を再生成する際に使い、
    バケットがサービスの AWS_REGION と別リージョンでも署名リージョン不一致(403)を避ける。空可。
    """
    try:
        issued = coerce_epoch_seconds(now)
        ttl = load_report_link_token_ttl_s(explicit_ttl_s=ttl_s)
        if issued is None or ttl is None:
            return None
        expires = add_token_ttl(issued, ttl)
        keyring = load_report_link_hmac_keyring(now=issued)
        if expires is None or keyring is None:
            return None
        payload = {
            "v": _TOKEN_VERSION,
            "typ": _TOKEN_TYPE,
            "b": str(bucket),
            "k": str(key),
            "r": str(region or ""),
            "e": expires,
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        sig = keyring.sign(raw, purpose=HMAC_PURPOSE_REPORT_LINK, digest_bytes=_SIG_LEN)
        return _b64e(raw) + "." + _b64e(sig)
    except Exception:
        return None


def decode_report_token(token: str, *, now: int | None = None) -> tuple[str, str, str] | None:
    """検証して (bucket, key, region) を返す。以下はすべて None（fail-closed）:

    鍵未設定 / 形式不正 / 署名不一致 / 失効 / typ 不一致 / key が許可 prefix 外 / bucket が許可外。
    region は埋込が無ければ ""（/r 側で AWS_REGION にフォールバック）。
    """
    cur = coerce_epoch_seconds(now)
    if cur is None:
        return None
    keyring = load_report_link_hmac_keyring(now=cur)
    if keyring is None:
        return None  # 鍵が無い/設定不正なら何も信用しない
    try:
        body_b64, sig_b64 = (token or "").split(".", 1)
        raw = _b64d(body_b64)
        signature = _b64d(sig_b64)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        if payload.get("v") == _TOKEN_VERSION and payload.get("typ") == _TOKEN_TYPE:
            if not keyring.verify(
                raw,
                signature,
                purpose=HMAC_PURPOSE_REPORT_LINK,
                digest_bytes=_SIG_LEN,
            ):
                return None
        elif "v" not in payload and set(payload) == _LEGACY_FIELDS:
            if not keyring.verify_legacy_previous(raw, signature, digest_bytes=_SIG_LEN):
                return None
        else:
            return None
        expires = validate_epoch_seconds(payload.get("e"))
    except Exception:
        return None
    if expires is None:
        return None
    try:
        if payload.get("typ") != _TOKEN_TYPE:
            return None  # 他用途トークン(draft/event 等・同一鍵)の転用を封じる
        if expires <= cur:
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
    except Exception:
        return None
