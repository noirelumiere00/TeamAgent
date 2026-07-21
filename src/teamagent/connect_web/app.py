"""連携コールバック FastAPI アプリ（``/oauth2/callback``）。

Google の同意後リダイレクトを受け、state 検証 → code 交換 → KMS暗号化して RDS 保存する。
exchange_fn / store はテストで注入可能（実 Google / 実 KMS / 実 DB を排除）。

P4: 同一アプリに「小俣さん専用 資料検索 Web UI」を追加する。dashboard.auth の
Google id_token 検証 + HMAC 署名 cookie を再利用し、SearchSkill を呼び出して
AI 要約 + 結果カードを返す。👍/👎 は search_feedback テーブルへ INSERT する。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import html
import json
import logging
import os
import re
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from psycopg.errors import UndefinedColumn
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from teamagent.adapters.google_oauth_flow import OAuthConsentFlow, verify_state
from teamagent.adapters.oauth_token_store import (
    OAuthToken,
    SlackOAuthToken,
    SlackTokenStore,
)
from teamagent.adapters.slack_oauth_flow import (
    SlackOAuthConsentFlow,
)
from teamagent.adapters.slack_oauth_flow import (
    verify_state as slack_verify_state,
)
from teamagent.dashboard.auth import (
    Verifier,
    authenticate_id_token,
    make_session,
    verify_session,
)
from teamagent.dashboard.config import DashboardConfig

logger = structlog.get_logger(__name__)


_FEEDBACK_RATE_LIMIT = 30
_FEEDBACK_RATE_WINDOW_S = 60.0
_feedback_rate_windows: dict[str, deque[float]] = {}
_feedback_rate_lock = threading.Lock()


def _feedback_rate_limited(user_email: str) -> bool:
    """評価送信がプロセス内スライディングウィンドウの上限を超えたかを返す。

    プロセス再起動でリセットされる仕様（暴走クライアントの行増殖抑止が目的で、
    厳密なレート制限は非ゴール）。リミッタ内部の失敗は fail-open とし、
    評価の保存経路を止めない。
    """
    try:
        now = time.monotonic()
        with _feedback_rate_lock:
            window = _feedback_rate_windows.setdefault(user_email, deque())
            cutoff = now - _FEEDBACK_RATE_WINDOW_S
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= _FEEDBACK_RATE_LIMIT:
                return True
            window.append(now)
    except Exception as exc:
        logger.warning(
            "feedback_rate_limit_failed",
            user_email=user_email,
            error=type(exc).__name__,
            detail=str(exc)[:200],
        )
    return False


def _feedback_insert_sql(columns: list[str]) -> str:
    """search_feedback 用の INSERT SQL を列名から組み立てる。"""
    placeholders = ", ".join("%s" for _ in columns)
    return f"INSERT INTO search_feedback ({', '.join(columns)}) VALUES ({placeholders})"


def _execute_feedback_insert(
    execute: Callable[[str, list[Any]], None],
    columns: list[str],
    values: list[Any],
    *,
    prepare_legacy_retry: Callable[[], None] | None = None,
) -> None:
    """評価INSERTを実行し、未適用の0022列だけは旧スキーマへ退避する。"""
    try:
        execute(_feedback_insert_sql(columns), values)
    except UndefinedColumn:
        legacy_columns = [
            "user_email",
            "query",
            "target_type",
            "doc_id",
            "chunk_id",
            "rating",
            "note",
        ]
        dropped_columns = [column for column in columns if column not in legacy_columns]
        if not dropped_columns:
            raise
        legacy_values = [values[columns.index(column)] for column in legacy_columns]
        if prepare_legacy_retry is not None:
            prepare_legacy_retry()
        logger.warning("feedback_save_legacy_fallback", dropped_columns=dropped_columns)
        execute(_feedback_insert_sql(legacy_columns), legacy_values)


# Every route carries the same browser-facing baseline.  The application serves a large,
# self-contained HTML artifact and Google Identity Services, so inline script/style support and
# the Google origins are intentional.  Everything else remains default-deny, and framing this
# app is never required.
_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "script-src 'self' 'unsafe-inline' https://accounts.google.com",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob: https:",
        "font-src 'self' data:",
        "connect-src 'self' https://accounts.google.com https://oauth2.googleapis.com",
        "frame-src https://accounts.google.com",
        "worker-src 'self' blob:",
        "media-src 'self' data: blob: https:",
        "manifest-src 'self'",
        "upgrade-insecure-requests",
    )
)

_SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Content-Security-Policy": _CONTENT_SECURITY_POLICY,
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), browsing-topics=()"
    ),
    # Google Sign-In can use a popup, hence same-origin-allow-popups rather than same-origin.
    "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
    "Cross-Origin-Resource-Policy": "same-site",
    "X-Permitted-Cross-Domain-Policies": "none",
    "X-DNS-Prefetch-Control": "off",
    "Origin-Agent-Cluster": "?1",
}


class _SecurityHeadersMiddleware:
    """Pure ASGI response middleware that does not consume or buffer the request body.

    Starlette's decorator middleware is based on ``BaseHTTPMiddleware``.  Wrapping the request in
    that layer can hide an already-delivered ``http.disconnect`` message from the search route,
    which would let an abandoned request start costly embedding/model work.  Editing only the
    ``http.response.start`` message preserves the original receive channel exactly.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    # Route-specific policy may be stricter (for example /r already sets no-store).
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, _send)


class _RedactShortLinkAccessLog(logging.Filter):
    """uvicorn アクセスログの ``/r/<token>`` パスを ``/r/<redacted>`` に伏せる。

    短縮リンクのトークンは capability（＝リンクを知る人が時限で閲覧できる bearer 相当）なので、
    CloudWatch のアクセスログに平文で残すとログ閲覧者が失効まで再利用できてしまう。パスだけ
    伏せ、他ルートのアクセスログは通常どおり残す（観測性は維持）。uvicorn.access のレコードは
    args=(client, method, full_path, http_version, status) 形式（args[2]=パス）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (
            isinstance(args, tuple)
            and len(args) >= 3
            and isinstance(args[2], str)
            and args[2].startswith("/r/")
        ):
            redacted = list(args)
            redacted[2] = "/r/<redacted>"
            record.args = tuple(redacted)
        return True


def build_uvicorn_log_config() -> dict[str, Any]:
    """uvicorn の既定ログ設定に、/r/<token> をアクセスログで伏せるフィルタを足して返す。

    __main__ が ``uvicorn.run(log_config=build_uvicorn_log_config())`` で使う。dictConfig が
    確実にフィルタを登録するよう、uvicorn 起動時の設定として渡す（後付け addFilter は
    uvicorn の dictConfig 適用で消えうるため）。
    """
    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["redact_shortlink"] = {
        "()": f"{__name__}._RedactShortLinkAccessLog",
    }
    access = cfg.get("loggers", {}).get("uvicorn.access")
    if access is not None:
        access["filters"] = [*access.get("filters", []), "redact_shortlink"]
    return cfg


# /r が都度再発行する presigned の有効期限（秒）。302 後にブラウザが即取得する前提の短命値。
# トークン TTL(7日)＋この値 が実効的な閲覧窓の上限＝旧 presigned(7日)と実質同等に抑える
# （長命 presigned を毎回配ると窓が token TTL＋7日 に伸びるため）。
_SHORTLINK_PRESIGN_TTL_S = 900


_APP_HTML_MISSING = (
    "<!doctype html><meta charset=utf-8><title>準備中</title>"
    "<div style='font-family:system-ui,-apple-system,sans-serif;max-width:640px;"
    "margin:80px auto;padding:0 24px;color:#333;line-height:1.7'>"
    "<h1 style='font-weight:800'>Obsidian ビューは準備中です</h1>"
    "<p>このイメージには静的ビュー（<code>static/app.html</code>）が同梱されていません。"
    "最新の再デプロイで反映されます。それまでは検索を "
    "<a href='/search' style='color:#5b4fd6'>/search</a> からご利用ください。</p></div>"
)


@lru_cache(maxsize=1)
def _static_app_html() -> str:
    """Obsidian 風 単一 HTML（自己完結・約3MB）をパッケージ相対で1回だけ読む。

    ``COPY src/ ./src/``（Dockerfile.teamagent-mcp）でイメージに同梱される
    ``static/app.html`` を返す。cwd 非依存・全リクエスト共有。

    ``app.html`` は機密ナレッジ埋め込みのため git 管理外（``.gitignore``）。
    署名済み source publisher が承認済みの exact S3 VersionId を検証して
    release image に同梱する。未同梱の開発 image では 404/500 ではなく
    「準備中」プレースホルダを返す（``/app`` ルートは main 常在可）。
    """
    p = Path(__file__).resolve().parent / "static" / "app.html"
    try:
        return p.read_text("utf-8")
    except OSError as exc:  # FileNotFoundError 等。ルートだけ在る launch イメージ向け。
        logger.warning("static_app_html_missing", path=str(p), error=str(exc))
        return _APP_HTML_MISSING


# --- /app immutable S3 object contract -----------------------------------------
# A configured production task must provide an exact VersionId and all three
# content hashes. The mutable latest object is never read. The process cache is
# intentionally permanent; changing the contract creates a new task definition.
_APP_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_APP_VERSION_ID_RE = re.compile(r"[A-Za-z0-9._~+/=-]{1,1024}")
_APP_HTML_BUCKET = "teamagent-dev-raw-files"
_APP_HTML_KEY = "codebuild/connect-web-app.html"
_APP_HTML_EXPECTED_OWNER = "718959508629"
_app_html_state: dict[str, Any] = {
    "html": None,
    "source": None,
    "sha256": None,
    "sha12": None,
    "version_id": None,
    "expected_version_id": None,
    "manifest_sha256": None,
    "build_inputs_sha256": None,
    "contract_ok": None,
    "error": None,
}
_app_html_lock = threading.Lock()


def _reset_app_html_cache() -> None:
    """テスト用: /app 配信 HTML のプロセス内キャッシュを破棄する（本番では呼ばない）。"""
    with _app_html_lock:
        _app_html_state.update(
            {
                "html": None,
                "source": None,
                "sha256": None,
                "sha12": None,
                "version_id": None,
                "expected_version_id": None,
                "manifest_sha256": None,
                "build_inputs_sha256": None,
                "contract_ok": None,
                "error": None,
            }
        )


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """``s3://bucket/key`` を (bucket, key) に分解する（不正形式は ValueError）。"""
    if not uri.startswith("s3://"):
        raise ValueError(f"s3:// 形式の URI ではありません: {uri}")
    bucket, _, key = uri[len("s3://") :].partition("/")
    if not bucket or not key:
        raise ValueError(f"bucket/key を解決できません: {uri}")
    if bucket != _APP_HTML_BUCKET or key != _APP_HTML_KEY:
        raise ValueError("application S3 location is outside the fixed contract")
    return bucket, key


def _app_html_contract() -> dict[str, str]:
    """Load and validate the complete immutable application contract."""

    names = {
        "version_id": "CONNECT_APP_HTML_S3_VERSION_ID",
        "sha256": "CONNECT_APP_HTML_SHA256",
        "manifest_sha256": "CONNECT_APP_HTML_MANIFEST_SHA256",
        "build_inputs_sha256": "CONNECT_APP_HTML_BUILD_INPUTS_SHA256",
        "baked_sha256": "CONNECT_APP_HTML_BAKED_SHA256",
    }
    values = {key: os.environ.get(name, "").strip() for key, name in names.items()}
    missing = sorted(names[key] for key, value in values.items() if not value)
    if missing:
        raise ValueError(f"immutable app contract is incomplete: {missing}")
    if not _APP_VERSION_ID_RE.fullmatch(values["version_id"]):
        raise ValueError("CONNECT_APP_HTML_S3_VERSION_ID is invalid")
    for key in ("sha256", "manifest_sha256", "build_inputs_sha256", "baked_sha256"):
        if not _APP_SHA256_RE.fullmatch(values[key]):
            raise ValueError(f"{names[key]} is not a lowercase SHA-256")
    if values["sha256"] == values["baked_sha256"]:
        raise ValueError("live app and baked fallback must be distinct artifacts")
    return values


def _fetch_app_html_from_s3(uri: str, version_id: str) -> tuple[str, str]:
    """Fetch one exact S3 app.html VersionId and return its decoded bytes/version.

    boto3 は遅延 import（モジュール先頭で import しない）＝ boto3 の無いテスト環境でも
    既存テストが壊れない。VersionId の省略や latest へのフォールバックは禁止。
    """
    import boto3

    bucket, key = _parse_s3_uri(uri)
    obj = boto3.client("s3").get_object(
        Bucket=bucket,
        Key=key,
        VersionId=version_id,
        ExpectedBucketOwner=_APP_HTML_EXPECTED_OWNER,
    )
    returned_version_id = obj.get("VersionId")
    if returned_version_id != version_id:
        raise ValueError("S3 returned a different app.html VersionId")
    body: bytes = obj["Body"].read()
    return body.decode("utf-8"), returned_version_id


def _resolve_app_html() -> dict[str, Any]:
    """/app で配信する HTML を初回アクセス時に1回だけ解決しキャッシュする（スレッドセーフ）。

    Configured production mode reads one exact S3 VersionId and verifies its
    full hash. A fetch failure may serve only the independently hash-checked
    baked fallback, while /healthz becomes unhealthy. Without the S3 URI the
    legacy baked/missing behavior remains available for local tests.

    複数 worker スレッドの同時初回アクセスは _get_search_skill と同じ
    double-checked locking で S3 取得を1回に保つ。
    """
    if _app_html_state["html"] is not None:
        return _app_html_state
    with _app_html_lock:
        if _app_html_state["html"] is not None:
            return _app_html_state
        html_text: str | None = None
        source = ""
        sha256 = ""
        version_id: str | None = None
        expected_version_id: str | None = None
        manifest_sha256: str | None = None
        build_inputs_sha256: str | None = None
        contract_ok = True
        resolution_error: str | None = None
        uri = os.environ.get("CONNECT_APP_HTML_S3_URI", "").strip()
        if uri:
            try:
                contract = _app_html_contract()
                expected_version_id = contract["version_id"]
                manifest_sha256 = contract["manifest_sha256"]
                build_inputs_sha256 = contract["build_inputs_sha256"]
                fetched_html, fetched_version_id = _fetch_app_html_from_s3(
                    uri,
                    contract["version_id"],
                )
                fetched_sha256 = hashlib.sha256(fetched_html.encode("utf-8")).hexdigest()
                if fetched_sha256 != contract["sha256"]:
                    raise ValueError("S3 app.html bytes do not match the expected SHA-256")
                html_text = fetched_html
                version_id = fetched_version_id
                sha256 = fetched_sha256
                source = "s3"
            except Exception as exc:
                contract_ok = False
                resolution_error = type(exc).__name__
                logger.error(
                    "app_html_s3_fetch_failed",
                    uri=uri,
                    expected_version_id=expected_version_id,
                    error=type(exc).__name__,
                    detail=str(exc)[:200],
                )
        if html_text is None:
            html_text = _static_app_html()
            source = "missing" if html_text == _APP_HTML_MISSING else "baked"
            if uri and source == "baked":
                baked_sha256 = hashlib.sha256(html_text.encode("utf-8")).hexdigest()
                expected_baked_sha256 = os.environ.get(
                    "CONNECT_APP_HTML_BAKED_SHA256",
                    "",
                ).strip()
                if baked_sha256 != expected_baked_sha256:
                    logger.error(
                        "app_html_baked_fallback_hash_mismatch",
                        actual_sha256=baked_sha256,
                        expected_sha256=expected_baked_sha256,
                    )
                    html_text = _APP_HTML_MISSING
                    source = "missing"
                    resolution_error = "BakedFallbackMismatch"
                else:
                    source = "baked-fallback"
        data = html_text.encode("utf-8")
        sha256 = hashlib.sha256(data).hexdigest()
        sha12 = sha256[:12]
        # "html" を最後に書く（ロック外の先読みが half-populated な状態を見ないための番兵）。
        _app_html_state.update(
            {
                "source": source,
                "sha256": sha256,
                "sha12": sha12,
                "version_id": version_id,
                "expected_version_id": expected_version_id,
                "manifest_sha256": manifest_sha256,
                "build_inputs_sha256": build_inputs_sha256,
                "contract_ok": contract_ok,
                "error": resolution_error,
                "html": html_text,
            }
        )
        logger.info(
            "app_html_resolved",
            source=source,
            sha256=sha256,
            version_id=version_id,
            contract_ok=contract_ok,
            bytes=len(data),
        )
        return _app_html_state


def _safe_next(raw: str | None) -> str:
    """ログイン後の戻り先を検証（オープンリダイレクト防止）。

    既知の内部ページ（``/app`` / ``/search``）のみ許可し、それ以外は既定 ``/app``。
    ＝ログイン後は原則 Obsidian 風 UI(/app) に着地する（旧 /search UI は明示遷移時のみ）。
    外部 URL・``//host``・スキーム付き等は一切通さない（ホワイトリスト方式）。
    """
    candidate = (raw or "").strip()
    if candidate in {"/app", "/search"}:
        return candidate
    return "/app"


_SEARCH_COOKIE = "ta_search_session"
_SESSION_TTL_S = 8 * 3600
_DEFAULT_SEARCH_EMAILS = "s-komata@vectorinc.co.jp"


def _env_int(name: str, default: int) -> int:
    """env を int で読む（未設定/空/不正は default）。task-def の env 差し替えだけで較正可能。"""
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """env を float で読む（未設定/空/不正は default）。concept しきい値の再ビルド無し較正用。"""
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _envflag(name: str, default: str = "false") -> bool:
    """env を bool で読む（"1"/"true"/"yes" を True・前後空白は除去）。

    skills/search/skill._envflag と同流儀（末尾改行や ``"1 "`` でも ON 判定）。
    """
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def _page(title: str, body: str, *, accent: str = "#36c08a") -> str:
    t = html.escape(title)
    b = html.escape(body)
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{t}</title><style>"
        "body{margin:0;background:#0f1420;color:#e8edf7;font-family:-apple-system,"
        "'Hiragino Sans','Noto Sans JP',sans-serif;display:flex;min-height:100vh;"
        "align-items:center;justify-content:center}"
        ".card{background:#1a2233;border:1px solid #283450;border-radius:14px;"
        "padding:36px 40px;max-width:520px;text-align:center}"
        f".card h1{{font-size:22px;margin:0 0 14px;color:{accent}}}"
        ".card p{color:#93a1bd;line-height:1.7;margin:6px 0}"
        "</style></head><body>"
        f'<div class="card"><h1>{t}</h1><p>{b}</p></div></body></html>'
    )


def _js_str(value: str) -> str:
    """文字列を安全な JS 文字列リテラルにする（インライン <script> への埋め込み用）。

    json.dumps は ``</script>`` の ``<`` をエスケープしないため、値に閉じタグが入ると
    HTML パーサが script を早期終了させて XSS が成立し得る（カルテのクライアント名
    のような任意文字列を埋め込むようになったため顕在化）。``<`` ``>`` ``&`` を
    \\uXXXX へ置換して防ぐ（JS 文字列リテラルとしては同値・挙動不変）。
    """
    return (
        json.dumps(value, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _load_search_config(env: dict[str, str] | None = None) -> DashboardConfig:
    """資料検索 Web UI 用の認証設定を env から作る（dashboard.config を流用）。

    env:
      - CONNECT_SEARCH_ALLOWED_EMAILS: カンマ区切り（既定 s-komata@vectorinc.co.jp）
      - CONNECT_SEARCH_ALLOWED_HD: 会社ドメイン（hd クレーム照合・任意）
      - CONNECT_SEARCH_SESSION_SECRET: cookie 署名鍵（未設定なら起動毎ランダム）
      - CONNECT_GOOGLE_CLIENT_ID: Google Sign-In の client_id（id_token の aud）
      - CONNECT_SEARCH_COOKIE_SECURE: 1 で Secure Cookie（HTTPS 公開時）
    """
    e = env if env is not None else dict(os.environ)
    emails_raw = e.get("CONNECT_SEARCH_ALLOWED_EMAILS", _DEFAULT_SEARCH_EMAILS).strip()
    allowed_emails = frozenset(x.strip().lower() for x in emails_raw.split(",") if x.strip())
    hd = e.get("CONNECT_SEARCH_ALLOWED_HD", "").strip() or None
    secret_raw = e.get("CONNECT_SEARCH_SESSION_SECRET", "").strip()
    secret = secret_raw.encode("utf-8") if secret_raw else os.urandom(32)
    secure_raw = e.get("CONNECT_SEARCH_COOKIE_SECURE", "").strip().lower()
    return DashboardConfig(
        allowed_emails=allowed_emails,
        allowed_hd=hd.lower() if hd else None,
        google_client_id=e.get("CONNECT_GOOGLE_CLIENT_ID", "").strip() or None,
        session_secret=secret,
        dev_bypass=False,
        cookie_secure=secure_raw in {"1", "true", "yes", "on"},
        # CONNECT_SEARCH_ALLOWED_HD を設定したら「会社ドメイン全体に開放」を意図する
        # （＝@vectorinc.co.jp 全員可）。ダッシュボード側は load_config が本フラグを
        # 渡さず既定 False。
        allowed_hd_opens_domain=hd is not None,
    )


# Obsidian 風シェルのデザイントークン（既存パレットを :root 変数化し両エンジンで継承）。
# 色は既存の literal をそのまま採用＝ブルーアクセントを維持（紫には切替えない）。
_ROOT_TOKENS = (
    ":root{--bg-primary:#0f1420;--bg-secondary:#0c111c;--bg-elev:#1a2233;"
    "--bg-hover:#222d44;--border:#283450;--accent:#4f8cff;--text:#e8edf7;"
    "--muted:#93a1bd;--faint:#5b6b86;--radius:8px;--ribbon-w:44px;--side-w:270px;"
    "--right-w:320px}"
)


# 検索結果カードの HTML を Python で生成（jinja2 不要・値は html.escape で XSS 防御）。
_SEARCH_STYLE = (
    "body{margin:0;background:var(--bg-primary);color:var(--text);font-family:-apple-system,"
    "'Hiragino Sans','Noto Sans JP',sans-serif}"
    "main{max-width:840px;margin:0 auto;padding:24px 18px}"
    "h1{font-size:20px;margin:0 0 4px}.sub{color:var(--muted);font-size:13px;margin:0 0 18px}"
    ".searchbar{display:flex;gap:8px;margin-bottom:18px}"
    ".searchbar input{flex:1;background:var(--bg-elev);border:1px solid var(--border);"
    "border-radius:10px;color:var(--text);padding:11px 14px;font-size:15px}"
    ".searchbar button{background:var(--accent);color:#fff;border:0;border-radius:10px;"
    "padding:0 20px;font-weight:600;cursor:pointer}"
    ".filterbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:14px}"
    ".filterbar input[type=text],.filterbar select{background:var(--bg-elev);"
    "border:1px solid var(--border);border-radius:8px;color:var(--text);"
    "padding:7px 10px;font-size:13px}"
    ".filterbar input[type=text]{min-width:220px}"
    ".filterbar .ck{display:inline-flex;align-items:center;gap:4px;font-size:12px;"
    "color:var(--muted);cursor:pointer}"
    ".answer{background:#16203a;border:1px solid #2b3a5e;border-radius:12px;padding:16px 18px;"
    "margin-bottom:18px;line-height:1.7;white-space:pre-wrap}"
    ".answer h2{font-size:13px;color:var(--muted);margin:0 0 8px;font-weight:600}"
    ".card{background:var(--bg-elev);border:1px solid var(--border);border-radius:12px;"
    "padding:14px 16px;margin-bottom:12px}"
    ".card .title{font-weight:700;font-size:15px;margin-bottom:6px}"
    ".card .title.clk{cursor:pointer}.card .title.clk:hover{color:var(--accent)}"
    ".chips{margin-bottom:8px}"
    ".chip{display:inline-block;background:rgba(79,140,255,.16);color:#9fc1ff;border-radius:999px;"
    "padding:2px 9px;font-size:11px;margin:0 6px 4px 0}"
    ".chip.tag{background:rgba(45,170,90,.18);color:#7fd6a0;cursor:pointer}"
    ".chip.tag:hover{background:rgba(45,170,90,.32)}"
    ".chip.tag.on{background:rgba(45,170,90,.45);color:#dff7e8}"
    ".toplinks{display:flex;gap:14px;margin:0 0 14px;font-size:13px}"
    ".toplinks a{color:var(--accent);text-decoration:none}"
    ".toplinks a:hover{text-decoration:underline}"
    ".filters{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:12px}"
    ".filters .lbl{font-size:12px;color:var(--muted)}"
    ".fchip{display:inline-flex;align-items:center;gap:6px;background:rgba(79,140,255,.22);"
    "color:#bcd4ff;border-radius:999px;padding:3px 10px;font-size:12px}"
    ".fchip button{background:none;border:0;color:#bcd4ff;cursor:pointer;font-size:13px;padding:0}"
    ".excerpt{color:#c5d0e6;font-size:13px;line-height:1.6;margin-bottom:8px}"
    ".meta{display:flex;align-items:center;gap:12px;font-size:12px;color:var(--muted)}"
    ".meta a{color:var(--accent);text-decoration:none}.meta a:hover{text-decoration:underline}"
    ".score{font-variant-numeric:tabular-nums}"
    ".fb{margin-left:auto;display:flex;gap:6px}"
    ".fb button{background:var(--bg-hover);border:1px solid #34425f;color:#c5d0e6;"
    "border-radius:8px;padding:3px 9px;font-size:13px;cursor:pointer}"
    ".fb button:hover{background:#2c3a58}"
    ".fb button.on{background:#2a5}.fb button.off{background:#a33}"
    ".empty{color:var(--muted);padding:8px}"
    # 0 件時のグレースフルな空状態（§4）。
    ".emptyx{background:var(--bg-elev);border:1px solid var(--border);border-radius:12px;"
    "padding:22px 20px;text-align:center;color:#c5d0e6}"
    ".emptyx .ei{font-size:30px;margin-bottom:6px}"
    ".emptyx .et{font-size:15px;font-weight:600;color:var(--text);margin-bottom:6px}"
    ".emptyx .es{color:var(--muted);font-size:13px;line-height:1.7;margin:2px 0}"
    ".emptyx .eb{margin-top:12px;display:flex;flex-wrap:wrap;gap:8px;justify-content:center}"
    ".emptyx .eb button{background:var(--bg-hover);border:1px solid #34425f;color:#cdd9f0;"
    "border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer}"
    ".emptyx .eb button:hover{background:#2c3a58}"
    ".emptyx .es .lead{color:var(--muted);font-size:12px;margin-right:4px}"
    ".errx{border-color:#7a3b3b;background:rgba(120,50,50,.18)}"
    # 検索中の視覚状態（#4）: ボタン disable + スケルトンカード（shimmer アニメ）。
    ".searchbar button:disabled{opacity:.55;cursor:progress}"
    ".card.skel{pointer-events:none}"
    ".skl{border-radius:6px;background:linear-gradient(90deg,var(--bg-hover) 25%,"
    "#2c3a58 37%,var(--bg-hover) 63%);background-size:400% 100%;"
    "animation:skshine 1.4s ease infinite}"
    ".skl-t{height:16px;width:55%;margin-bottom:10px}"
    ".skl-l{height:12px;width:90%;margin-bottom:8px}"
    ".skl-l.short{width:70%;margin-bottom:0}"
    "@keyframes skshine{0%{background-position:100% 0}100%{background-position:0 0}}"
    # アクセシビリティ: 動きを減らす設定ではシマーを止める（静的プレースホルダ化）。
    "@media (prefers-reduced-motion:reduce){.skl{animation:none}}"
    # クエリ語ハイライト（#2）: excerpt 中の検索語を span.hl で強調（textContent 分割挿入）。
    ".hl{background:rgba(255,193,79,.18);color:#ffd27a;border-radius:3px;padding:0 1px}"
    # 二段レスポンス（#1）: AI要約の生成中プレースホルダ＋失敗時の再試行ボタン。
    ".answer .abody.pending{color:var(--muted)}"
    ".answer .aretry{margin-left:8px;background:var(--bg-hover);border:1px solid #34425f;"
    "color:#c5d0e6;border-radius:8px;padding:2px 10px;font-size:12px;cursor:pointer}"
    ".answer .aretry:hover{background:#2c3a58}"
    ".rate4{margin-top:14px;border-top:1px solid #2b3a5e;padding-top:12px;white-space:normal}"
    ".rate4Question{font-size:13px;color:#c5d0e6;margin-bottom:8px}"
    ".rate4Choices{display:flex;flex-wrap:wrap;gap:6px}"
    ".rate4Choice{background:var(--bg-hover);border:1px solid #34425f;color:#c5d0e6;"
    "border-radius:8px;padding:5px 9px;font-size:13px;cursor:pointer}"
    ".rate4Choice:hover{background:#2c3a58}.rate4Choice.selected{background:#2a5;border-color:#53c77b;color:#fff}"
    ".rate4Status{min-height:1.5em;margin-top:8px;color:var(--muted);font-size:12px}"
    ".rate4Status.error{color:#ffaaaa}.rate4Retry{margin-left:8px;background:none;border:0;color:var(--accent);"
    "font-size:12px;cursor:pointer;text-decoration:underline}"
    ".rate4Note{max-height:0;opacity:0;overflow:hidden;transition:max-height .2s ease,"
    "opacity .2s ease}"
    ".rate4Note.open{max-height:180px;opacity:1;margin-top:10px}"
    ".rate4Note textarea{box-sizing:border-box;width:100%;min-height:68px;resize:vertical;"
    "background:var(--bg-elev);border:1px solid #34425f;border-radius:8px;color:var(--text);"
    "padding:8px;font:inherit;font-size:12px}"
    ".rate4Note button{margin-top:6px;background:var(--bg-hover);border:1px solid #34425f;"
    "color:#c5d0e6;"
    "border-radius:8px;padding:4px 10px;font-size:12px;cursor:pointer}"
    ".rate4Help{margin-top:8px;color:var(--muted);font-size:11px}"
)


# 予算バンド allowlist（api_search の filter_budget / sort_budget_near 受けで使う）。
# UI の <select> option と一致させる。'不明' はフィルタ対象外（ソート末尾扱いのみ）。
_BUDGET_BANDS = ("〜100万", "100〜500万", "500万〜")

# 資料種別 allowlist（api_search の filter_doc_type 受けで使う）。ingest.classify._DOC_TYPES
# と同値の literal を connect_web に置く（_BUDGET_BANDS と同じ作法）。UI の <select> option と
# 一致させる必要があるためモジュール定数化する。drift は test_search_doc_type_filter で検知。
_DOC_TYPES = ("提案書", "議事録", "報告書", "価格表", "契約", "その他")


# 検索 UI の本体 DOM フラグメント（シェルの #mainList に mount する。_SEARCH_JS が参照）。
# 見出し/サブは textContent ではなく静的文字列だが「社内ナレッジ検索」をルートテストが参照。
_SEARCH_DOM = (
    "<main>"
    "<h1>📚 社内ナレッジ検索</h1>"
    '<p class="sub">過去の提案書・議事録・Slack など社内ナレッジを自然文で検索します。</p>'
    '<div class="searchbar">'
    '<input id="q" type="text" placeholder="例: 飲料メーカー向けの保存率訴求の提案">'
    '<button id="go" type="button">検索</button></div>'
    '<div class="filterbar">'
    '<input id="fclient" type="text" list="clientlist" '
    'placeholder="取引先で絞る（例: 日本ガイシ）">'
    '<datalist id="clientlist"></datalist>'
    '<select id="fbudget" aria-label="予算で絞る">'
    '<option value="">予算（指定なし）</option>'
    "<option>〜100万</option><option>100〜500万</option><option>500万〜</option>"
    "</select>"
    '<label class="ck"><input id="bsort" type="checkbox">予算が近い順</label>'
    '<label class="ck"><input id="bunknown" type="checkbox">予算不明も含める</label>'
    # 詳細絞り込み（任意）。doc_type は _DOC_TYPES 固定 option・solution は自由入力。
    # 静的 literal・依存ゼロ（予算 select の作法に倣う）。
    '<select id="fdoctype" aria-label="資料種別で絞る">'
    '<option value="">種別（指定なし）</option>'
    "<option>提案書</option><option>議事録</option><option>報告書</option>"
    "<option>価格表</option><option>契約</option><option>その他</option>"
    "</select>"
    '<input id="fsolution" type="text" maxlength="50" '
    'placeholder="施策で絞る（例: 動画広告）">'
    "</div>"
    '<div id="filters" class="filters"></div>'
    '<div id="results"></div>'
    "</main>"
)


# fetch ベースの最小フロント（依存ゼロ・テキストは textContent で安全に差し込む）。
_SEARCH_JS = r"""
const q=document.getElementById('q');
const go=document.getElementById('go');
const results=document.getElementById('results');
const filters=document.getElementById('filters');
const fclient=document.getElementById('fclient');
const fbudget=document.getElementById('fbudget');
const bsort=document.getElementById('bsort');
const bunknown=document.getElementById('bunknown');
const fdoctype=document.getElementById('fdoctype');
const fsolution=document.getElementById('fsolution');
const clientlist=document.getElementById('clientlist');
let lastQuery='';
let activeIndustry=null;
// 連打対策（#4）: 進行中 fetch の中止用 controller と「最新の検索か」を判定する世代カウンタ。
// abort で旧リクエストを止め（サーバー側は is_disconnected で Bedrock 前に破棄）、
// 万一 abort が間に合わず resolve しても世代不一致なら描画しない（二重防御）。
let currentSearch=null;
let searchGen=0;
function safeUrl(u){return (typeof u==='string'&&/^(https?|slack|gdrive):/i.test(u))?u:null;}
// 取引先 datalist を facets から遅延充填（graph fetch 後にのみ __facets.client が埋まる）。
// 未定義時は何もしない＝free-text フォールバック（部分一致が効くので候補なしでも検索可）。
function populateClientList(){
  if(!clientlist)return;
  const fac=window.__facets;
  if(!fac||!fac.client)return;
  clientlist.textContent='';
  for(const row of fac.client.slice(0,50)){
    if(!row||!row.value)continue;
    const o=document.createElement('option');o.value=row.value;clientlist.appendChild(o);
  }
}
function chip(text){
  const s=document.createElement('span');s.className='chip';s.textContent=text;return s;
}
function tagChip(label,kind,value){
  const s=document.createElement('span');s.className='chip tag';s.textContent=label;
  s.title='このタグで絞り込む';
  s.onclick=()=>{
    if(kind==='industry'){activeIndustry=value;}
    else{q.value=value;activeIndustry=null;}
    search();
  };
  return s;
}
function renderFilters(){
  filters.textContent='';
  if(!activeIndustry)return;
  const lbl=document.createElement('span');lbl.className='lbl';lbl.textContent='絞り込み:';
  filters.appendChild(lbl);
  const f=document.createElement('span');f.className='fchip';
  const txt=document.createElement('span');txt.textContent='業界 '+activeIndustry;
  f.appendChild(txt);
  const x=document.createElement('button');x.type='button';x.textContent='×';
  x.setAttribute('aria-label','業界フィルタを外す');
  x.onclick=()=>{activeIndustry=null;search();};
  f.appendChild(x);filters.appendChild(f);
}
function fbButtons(target,doc_id,chunk_id,sessionId){
  const wrap=document.createElement('div');wrap.className='fb';
  for(const r of [['👍',1,'on'],['👎',-1,'off']]){
    const b=document.createElement('button');b.type='button';b.textContent=r[0];
    b.onclick=async()=>{
      b.disabled=true;
      try{
        const resp=await fetch('/api/v1/feedback',{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({query:lastQuery,target_type:target,
            doc_id:doc_id,chunk_id:chunk_id,rating:r[1],note:null,
            search_session_id:sessionId})});
        if(!resp.ok)throw new Error('http '+resp.status);
        b.classList.add(r[2]);
      }catch(e){
        b.classList.remove(r[2]);b.disabled=false;
        b.title='評価を送信できませんでした';
      }
    };
    wrap.appendChild(b);
  }
  return wrap;
}
// AI要約向けの4段階評価。検索世代とは切り離し、生成時の値だけをクロージャに保持する。
function rate4Bar(query,sessionId,answerId){
  const wrap=document.createElement('div');wrap.className='rate4';
  let currentScore=null;
  let lastSentScore=null;
  let lastSentNote=null;
  let pendingPayload=null;
  let pendingKind=null;
  let pendingWasUpdate=false;
  let hasChosenScore=false;
  let requestNumber=0;
  const question=document.createElement('div');question.className='rate4Question';
  question.textContent='この回答は期待に合いましたか？';wrap.appendChild(question);
  const choices=document.createElement('div');choices.className='rate4Choices';
  wrap.appendChild(choices);
  const status=document.createElement('div');status.className='rate4Status';
  status.setAttribute('aria-live','polite');
  wrap.appendChild(status);
  const retry=document.createElement('button');retry.type='button';retry.className='rate4Retry';
  retry.textContent='再試行';retry.hidden=true;status.appendChild(retry);
  const noteBox=document.createElement('div');noteBox.className='rate4Note';
  const note=document.createElement('textarea');note.maxLength=500;
  note.placeholder='欲しかった資料の種類・足りなかった観点など。クライアント名・個人名を書いたり資料本文を貼ったりしないでください';
  noteBox.appendChild(note);
  const noteSend=document.createElement('button');noteSend.type='button';
  noteSend.textContent='コメントを送る';
  noteBox.appendChild(noteSend);wrap.appendChild(noteBox);
  const help=document.createElement('div');help.className='rate4Help';
  help.textContent='評価は検索の改善にだけ使います';wrap.appendChild(help);
  const buttons=[];
  function currentNote(){const value=note.value.trim();return value||null;}
  function payloadFor(score,noteValue){
    return {query:query,target_type:'answer',doc_id:null,chunk_id:null,score:score,note:noteValue,
      search_session_id:sessionId,answer_id:answerId};
  }
  function select(score){
    currentScore=score;
    for(const item of buttons)item.button.classList.toggle('selected',item.score===score);
    noteBox.classList.add('open');
  }
  function showError(message){
    status.className='rate4Status error';status.textContent=message;
    status.appendChild(retry);retry.hidden=false;
  }
  async function postRate4(payload,kind,silent,wasUpdate=false){
    pendingPayload=payload;pendingKind=kind;pendingWasUpdate=wasUpdate;
    const thisRequest=++requestNumber;
    if(!silent){status.className='rate4Status';status.textContent='送信中…';status.appendChild(retry);retry.hidden=true;}
    try{
      const response=await fetch('/api/v1/feedback',{
        method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload)});
      if(response.status===401){
        if(!silent&&thisRequest===requestNumber)showError('セッションが切れました。再ログインしてください');
        return false;
      }
      if(!response.ok)throw new Error('http '+response.status);
      if(thisRequest!==requestNumber)return true;
      const wasRated=lastSentScore!==null;
      lastSentScore=payload.score;lastSentNote=payload.note;pendingPayload=null;pendingKind=null;
      if(!silent){
        status.className='rate4Status';
        status.textContent=kind==='score'&&!wasRated&&!wasUpdate?'評価を送信しました（あとから変更できます）':'評価を更新しました';
        status.appendChild(retry);retry.hidden=true;
      }
      return true;
    }catch(error){
      if(!silent&&thisRequest===requestNumber)showError('送信できませんでした');
      return false;
    }
  }
  retry.onclick=function(){
    if(pendingPayload)postRate4(pendingPayload,pendingKind,false,pendingWasUpdate);
  };
  for(const item of [[4,'◎ 期待どおり'],[3,'○ おおむね'],[2,'△ 物足りない'],[1,'× 見当違い']]){
    const button=document.createElement('button');button.type='button';
    button.className='rate4Choice';
    button.textContent=item[1];
    const score=item[0];
    button.onclick=function(){
      if(score===currentScore)return;
      const wasUpdate=hasChosenScore;hasChosenScore=true;
      select(score);postRate4(payloadFor(score,currentNote()),'score',false,wasUpdate);
    };
    buttons.push({button:button,score:score});choices.appendChild(button);
  }
  noteSend.onclick=function(){
    if(currentScore===null)return;
    postRate4(payloadFor(currentScore,currentNote()),'note',false);
  };
  // 呼び出し側が要約カードを除去する直前に実行するための best-effort の退避処理。
  wrap.rate4Teardown=function(){
    const value=currentNote();
    if(currentScore!==null&&value!==null&&value!==lastSentNote){
      postRate4(payloadFor(currentScore,value),'note',true);
    }
  };
  return wrap;
}
function renderError(){
  results.textContent='';
  const c=document.createElement('div');c.className='emptyx errx';
  const i=document.createElement('div');i.className='ei';i.textContent='⚠️';c.appendChild(i);
  const t=document.createElement('div');t.className='et';
  t.textContent='検索に失敗しました';c.appendChild(t);
  const s=document.createElement('div');s.className='es';
  s.textContent='通信エラーが発生しました。少し待って再試行してください。';c.appendChild(s);
  const b=document.createElement('div');b.className='eb';
  const rb=document.createElement('button');rb.type='button';rb.textContent='再試行';
  rb.onclick=search;b.appendChild(rb);c.appendChild(b);
  results.appendChild(c);
}
function renderEmpty(query){
  const c=document.createElement('div');c.className='emptyx';
  const i=document.createElement('div');i.className='ei';i.textContent='🔍';c.appendChild(i);
  const t=document.createElement('div');t.className='et';
  t.textContent='「'+query+'」に一致する社内ナレッジは見つかりませんでした';
  c.appendChild(t);
  const s1=document.createElement('div');s1.className='es';
  s1.textContent='キーワードを減らす・別の言い方を試すと見つかることがあります。';
  c.appendChild(s1);
  const b=document.createElement('div');b.className='eb';
  if(activeIndustry){
    const fb=document.createElement('button');fb.type='button';
    fb.textContent='業界フィルタ「'+activeIndustry+'」を外して再検索';
    fb.onclick=()=>{activeIndustry=null;search();};b.appendChild(fb);
  }
  const hasClient=fclient&&fclient.value.trim();
  const hasBudget=fbudget&&fbudget.value;
  const hasDocType=fdoctype&&fdoctype.value;
  const hasSolution=fsolution&&fsolution.value.trim();
  if(hasClient||hasBudget||hasDocType||hasSolution){
    const cb=document.createElement('button');cb.type='button';
    cb.textContent='絞り込み条件を外して再検索';
    cb.onclick=()=>{
      if(fclient)fclient.value='';
      if(fbudget)fbudget.value='';
      if(bsort)bsort.checked=false;
      if(bunknown)bunknown.checked=false;
      if(fdoctype)fdoctype.value='';
      if(fsolution)fsolution.value='';
      search();
    };
    b.appendChild(cb);
  }
  const gb=document.createElement('button');gb.type='button';
  gb.textContent='グラフで関連資料を探す';
  gb.onclick=()=>{if(window.shellSetMode)window.shellSetMode('graph');};
  b.appendChild(gb);c.appendChild(b);
  // facets から「近いタグ」候補を出す（§4.2・追加 fetch なし）。
  const fac=window.__facets;
  if(fac){
    const sug=[];
    for(const fam of ['industry','client','doc_type']){
      for(const row of (fac[fam]||[]).slice(0,3)){
        sug.push([fam,row.value]);
      }
    }
    if(sug.length){
      const line=document.createElement('div');line.className='es';
      const lead=document.createElement('span');lead.className='lead';
      lead.textContent='近いタグ:';line.appendChild(lead);
      for(const [fam,val] of sug.slice(0,6)){
        const kind=fam==='industry'?'industry':(fam==='doc_type'?'doc_type':'client');
        line.appendChild(tagChip('# '+val,kind,val));
      }
      c.appendChild(line);
    }
  }
  results.appendChild(c);
}
function setSearching(on){
  if(go){go.disabled=on;go.textContent=on?'検索中…':'検索';}
}
function renderSkeleton(){
  results.textContent='';
  for(let i=0;i<3;i++){
    const c=document.createElement('div');c.className='card skel';
    c.setAttribute('aria-hidden','true');
    const t=document.createElement('div');t.className='skl skl-t';c.appendChild(t);
    const l1=document.createElement('div');l1.className='skl skl-l';c.appendChild(l1);
    const l2=document.createElement('div');l2.className='skl skl-l short';c.appendChild(l2);
    results.appendChild(c);
  }
}
// クエリ語ハイライト（#2）: 空白区切りで 2 文字以上の語だけを対象にする。
function hlTerms(query){
  return String(query||'').split(/\s+/).filter(function(w){return w.length>=2;});
}
// text をクエリ語で分割し、text node と span.hl を交互に append する。
// createTextNode/createElement/textContent のみ使用（innerHTML 不使用＝XSS 安全）。
// 大小文字は区別しない。同位置に複数語が一致したら長い語を優先する。
function appendHighlighted(el,text,terms){
  el.textContent='';
  const t=String(text||'');
  if(!terms||!terms.length){el.textContent=t;return;}
  const lower=t.toLowerCase();
  let pos=0;
  while(pos<t.length){
    let at=-1,ln=0;
    for(const w of terms){
      const lw=w.toLowerCase();
      const idx=lower.indexOf(lw,pos);
      if(idx===-1)continue;
      if(at===-1||idx<at||(idx===at&&lw.length>ln)){at=idx;ln=lw.length;}
    }
    if(at===-1){el.appendChild(document.createTextNode(t.slice(pos)));break;}
    if(at>pos)el.appendChild(document.createTextNode(t.slice(pos,at)));
    const sp=document.createElement('span');sp.className='hl';
    sp.textContent=t.slice(at,at+ln);el.appendChild(sp);
    pos=at+ln;
  }
}
// シェル側（右プレビュー/hover ポップ）から使う公開フック。直近クエリの語で強調し、
// 未検索（lastQuery 空）なら素の textContent と等価＝グラフ発プレビューは無強調。
window.searchHighlight=function(el,text){appendHighlighted(el,text,hlTerms(lastQuery));};
function buildBody(query){
  const body={query:query,top_k:8};
  if(activeIndustry)body.filter_industry=activeIndustry;
  const fc=fclient?fclient.value.trim():'';
  const fb=fbudget?fbudget.value:'';
  if(fc)body.filter_client=fc;
  if(fb)body.filter_budget=fb;
  if(bunknown&&bunknown.checked&&fb)body.include_unknown_budget=true;
  if(bsort&&bsort.checked&&fb)body.sort_budget_near=fb;
  if(fdoctype&&fdoctype.value)body.filter_doc_type=fdoctype.value;
  const fs=fsolution?fsolution.value.trim():'';
  if(fs)body.filter_solution=fs;
  return body;
}
// /api/v1/search を 1 回叩く。二段レスポンス（#1）の (a)fast=include_answer:false と
// (b)answer=include_answer:true の両方がここを通り、AbortController を共有する。
async function fetchSearch(body,withAnswer,ctl){
  const resp=await fetch('/api/v1/search',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(Object.assign({include_answer:withAnswer},body)),
    signal:ctl.signal});
  if(resp.status===401){
    location.href='/search/login';
    const e=new Error('unauthorized');e.name='UnauthorizedError';throw e;
  }
  if(!resp.ok)throw new Error('http '+resp.status);
  return resp.json();
}
// AI要約カード（プレースホルダ状態）。(b) 到着で .abody だけ差し替える。
function renderAnswerPending(sessionId){
  const a=document.createElement('div');a.className='answer';
  const h=document.createElement('h2');h.textContent='AI 要約';a.appendChild(h);
  const b=document.createElement('div');b.className='abody pending';
  b.textContent='AI要約を生成中…';a.appendChild(b);
  /*ANSWER_RATING_PENDING*/
  return a;
}
// (b) answer フェッチの結果を要約カードへ差し込む。(b) の hits は使わない
// （(a) の描画を維持＝ちらつき防止）。失敗時は再試行ボタンで (b) だけ再実行。
function attachAnswer(card,promise,body,ctl,gen,query,sessionId){
  const el=card.querySelector('.abody');
  promise.then(function(data){
    if(gen!==searchGen)return;
    if(data&&data.answer){el.classList.remove('pending');el.textContent=data.answer;/*ANSWER_RATING_ATTACH*/}
    else{card.remove();}
  }).catch(function(e){
    if(e&&(e.name==='AbortError'||e.name==='UnauthorizedError'))return;
    if(gen!==searchGen)return;
    el.classList.remove('pending');
    el.textContent='要約の生成に失敗しました ';
    const rb=document.createElement('button');rb.type='button';rb.className='aretry';
    rb.textContent='再試行';
    rb.onclick=function(){
      if(gen!==searchGen)return;
      el.classList.add('pending');el.textContent='AI要約を生成中…';
      attachAnswer(card,fetchSearch(body,true,ctl),body,ctl,gen,query,sessionId);
    };
    el.appendChild(rb);
  });
}
async function search(){
  const sessionId=crypto.randomUUID();
  const query=q.value.trim();if(!query)return;
  if(currentSearch)currentSearch.abort();
  const ctl=new AbortController();currentSearch=ctl;
  const gen=++searchGen;
  lastQuery=query;renderFilters();setSearching(true);/*ANSWER_RATING_TEARDOWN*/renderSkeleton();
  const body=buildBody(query);
  // 二段レスポンス（#1）: (a) hits 即描画（include_answer:false）と (b) AI要約
  // （include_answer:true）を並行フェッチ。ctl/gen を共有し、連打時は 2 本とも中止。
  const answerPromise=fetchSearch(body,true,ctl);
  answerPromise.catch(function(){});// 早期 reject の unhandledrejection 抑止（attachAnswer で処理）
  let data;
  try{
    data=await fetchSearch(body,false,ctl);
  }catch(e){
    if(e&&(e.name==='AbortError'||e.name==='UnauthorizedError'))return;
    if(gen===searchGen){setSearching(false);renderError();ctl.abort();}
    return;
  }
  if(gen!==searchGen)return;
  setSearching(false);
  results.textContent='';
  const answerCard=renderAnswerPending(sessionId);
  results.appendChild(answerCard);
  attachAnswer(answerCard,answerPromise,body,ctl,gen,query,sessionId);
  const terms=hlTerms(query);
  const hits=data.hits||[];
  if(!hits.length){renderEmpty(query);return;}
  for(const h of hits){
    const card=document.createElement('div');card.className='card';
    const t=document.createElement('div');t.className='title';
    t.textContent=h.title||'(無題)';
    // シェル統合時はタイトルクリックで右プレビューパネルを開く（§3.2）。
    if(typeof window.openPreview==='function'){
      t.className='title clk';
      t.onclick=()=>{window.openPreview({doc_id:h.doc_id,title:h.title,
        source_uri:h.source_uri,source_type:h.source_type,client_name:h.client_name,
        industry:h.industry,project:h.project,doc_type:h.doc_type,
        deal_phase:h.deal_phase,excerpt:h.excerpt});};
    }
    card.appendChild(t);
    const chips=document.createElement('div');chips.className='chips';
    for(const c of [h.client_name,h.source_type]){if(c)chips.appendChild(chip(c));}
    if(h.budget)chips.appendChild(chip(h.budget));
    if(h.industry)chips.appendChild(tagChip('# '+h.industry,'industry',h.industry));
    if(h.doc_type)chips.appendChild(tagChip('# '+h.doc_type,'doc_type',h.doc_type));
    if(h.project)chips.appendChild(tagChip('# '+h.project,'project',h.project));
    if(h.deal_phase)chips.appendChild(tagChip('# '+h.deal_phase,'deal_phase',h.deal_phase));
    if(chips.childNodes.length)card.appendChild(chips);
    const ex=document.createElement('div');ex.className='excerpt';
    appendHighlighted(ex,h.excerpt||'',terms);card.appendChild(ex);
    const meta=document.createElement('div');meta.className='meta';
    const su=safeUrl(h.source_uri);
    if(su){
      const link=document.createElement('a');link.href=su;
      link.target='_blank';link.rel='noopener noreferrer';
      link.textContent='出典を開く';meta.appendChild(link);
    }
    // カルテ導線: client_name / cls_project がある hit はクライアントカルテへ。
    // アプリ内固定パス（相対）なので safeUrl（絶対 scheme allowlist）は通さず、
    // encodeURIComponent + textContent の既存流儀で安全に組む。
    const kn=h.client_name||h.project;
    if(kn){
      const kl=document.createElement('a');
      kl.href='/search/client/'+encodeURIComponent(kn);
      kl.textContent='カルテを見る';meta.appendChild(kl);
    }
    const sc=document.createElement('span');sc.className='score';
    const sv=(typeof h.score==='number'?h.score.toFixed(3):h.score);
    sc.textContent='score '+sv;meta.appendChild(sc);
    meta.appendChild(fbButtons('chunk',h.doc_id||null,h.chunk_id||null,sessionId));
    card.appendChild(meta);results.appendChild(card);
  }
}
go.onclick=search;
q.addEventListener('keydown',e=>{if(e.key==='Enter')search();});
// シェル統合用: タグ explorer からの絞り込み/検索を駆動する。
window.searchRun=search;
window.searchSetIndustry=function(value){activeIndustry=value;search();};
window.searchSetQuery=function(value){q.value=value;activeIndustry=null;search();};
// 取引先 datalist の遅延充填フック（_POWER_JS の __powerReady から graph fetch 後に呼ぶ）。
window.populateClientList=populateClientList;
// 既に __facets が用意済（graph 先読み）なら即時充填も試みる（フック取りこぼし保険）。
populateClientList();
"""


def _render_search_js(*, answer_rating_enabled: bool) -> str:
    """サーバサイドフラグが ON のときだけ評価UIの配線コードを JS に差し込む。

    OFF 時は従来どおり answer 用 👍/👎 を pending カードへ出す（機能同一）。
    フラグ値はクライアントへ env として露出せず、生成 JS の形でのみ反映される。
    """
    if answer_rating_enabled:
        pending = ""
        attach = (
            "if(!card.querySelector('.rate4')){"
            "card.appendChild(rate4Bar(query,sessionId,data.answer_id||null));}"
        )
        teardown = (
            "const oldRate4=results.querySelector('.rate4');"
            "if(oldRate4&&typeof oldRate4.rate4Teardown==='function'){"
            "try{oldRate4.rate4Teardown();}catch(e){}}"
        )
    else:
        pending = "a.appendChild(fbButtons('answer',null,null,sessionId));"
        attach = ""
        teardown = ""
    return (
        _SEARCH_JS.replace("/*ANSWER_RATING_PENDING*/", pending)
        .replace("/*ANSWER_RATING_ATTACH*/", attach)
        .replace("/*ANSWER_RATING_TEARDOWN*/", teardown)
    )


_GRAPH_STYLE = (
    "body{margin:0;background:var(--bg-primary);color:var(--text);font-family:-apple-system,"
    "'Hiragino Sans','Noto Sans JP',sans-serif}"
    "main{max-width:980px;margin:0 auto;padding:24px 18px}"
    "h1{font-size:20px;margin:0 0 4px}.sub{color:var(--muted);font-size:13px;margin:0 0 12px}"
    ".toplinks{margin-bottom:12px;font-size:13px}"
    ".toplinks a{color:var(--accent);text-decoration:none}"
    ".toplinks a:hover{text-decoration:underline}"
    ".legend{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:10px;font-size:12px;color:#c5d0e6}"
    ".legend .lg{display:inline-flex;align-items:center;gap:5px}"
    ".legend .dot{width:10px;height:10px;border-radius:50%}"
    ".graphwrap{position:relative;height:600px;background:var(--bg-secondary);"
    "border:1px solid var(--border);border-radius:14px;overflow:hidden}"
    "#cv{width:100%;height:100%;display:block;cursor:grab}"
    ".tip{position:absolute;pointer-events:none;background:var(--bg-elev);border:1px solid #34425f;"
    "border-radius:8px;padding:6px 9px;font-size:12px;color:var(--text);max-width:280px;"
    "display:none;line-height:1.5;z-index:2;white-space:pre-line}"
    ".status{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);"
    "color:var(--muted);font-size:13px}"
    # --- 大規模グラフ時の隅ヒント（ズーム/ホバーでラベル表示）---
    ".ghint{position:absolute;left:10px;bottom:10px;z-index:2;pointer-events:none;"
    "background:rgba(20,28,46,.78);border:1px solid #2b3a5e;border-radius:8px;"
    "padding:4px 9px;font-size:11px;color:#8fa3c6;max-width:calc(100% - 20px)}"
    ".ghint[hidden]{display:none}"
    # --- Obsidian 風グラフ設定パネル（左上オーバーレイ）---
    ".gtoggle{position:absolute;top:10px;left:10px;z-index:4;width:30px;height:30px;"
    "display:flex;align-items:center;justify-content:center;background:rgba(20,28,46,.92);"
    "border:1px solid #2b3a5e;border-radius:8px;color:#c5d0e6;cursor:pointer;font-size:15px;"
    "line-height:1;user-select:none}"
    ".gtoggle:hover{background:#222d44}"
    ".gpanel{position:absolute;top:50px;left:10px;width:236px;max-height:calc(100% - 60px);"
    "overflow:auto;background:rgba(20,28,46,.92);border:1px solid #2b3a5e;border-radius:10px;"
    "font-size:12px;color:#c5d0e6;z-index:3;backdrop-filter:blur(2px);padding:4px 0 8px}"
    ".gpanel[hidden]{display:none}"
    ".gsec{border-top:1px solid #243152}.gsec:first-child{border-top:0}"
    ".gsec>h3{cursor:pointer;margin:0;padding:8px 12px;font-size:12px;font-weight:600;"
    "color:#dbe4f5;display:flex;justify-content:space-between;align-items:center;user-select:none}"
    ".gsec>h3 .chev{color:#7d8cab;font-size:10px}"
    ".gsec.collapsed .gbody{display:none}"
    ".gbody{padding:2px 12px 8px}"
    ".grow{margin:7px 0}.grow label{display:block;margin-bottom:2px}"
    ".grow .gval{color:#8fa3c6;float:right}"
    ".gpanel input[type=range]{width:100%;accent-color:#4f8cff;margin:0}"
    ".gpanel input[type=search],.gpanel select{width:100%;box-sizing:border-box;"
    "background:#101827;border:1px solid #2b3a5e;border-radius:6px;color:#e8edf7;"
    "padding:4px 6px;font-size:12px}"
    ".gchk{display:flex;align-items:center;gap:6px;margin:4px 0;cursor:pointer}"
    ".gchk input{accent-color:#4f8cff;margin:0}"
    ".gchk .sw{width:9px;height:9px;border-radius:50%;flex:0 0 auto}"
    ".greset{display:block;text-align:center;color:#7fb0ff;cursor:pointer;padding:8px 0 2px;"
    "font-size:12px;text-decoration:none}"
    ".greset:hover{text-decoration:underline}"
    ".grule{display:flex;align-items:center;gap:5px;margin:5px 0}"
    ".grule input[type=text]{flex:1 1 auto;min-width:0;background:#101827;border:1px solid #2b3a5e;"
    "border-radius:6px;color:#e8edf7;padding:3px 6px;font-size:12px}"
    ".grule input[type=color]{width:24px;height:24px;border:0;background:none;padding:0;"
    "cursor:pointer}"
    ".grule .x{color:#8a99b8;cursor:pointer;padding:0 2px}"
    ".gbtn{background:#1b2740;border:1px solid #2b3a5e;border-radius:6px;color:#cdd9f0;"
    "padding:4px 8px;font-size:12px;cursor:pointer}"
    ".gbtn:hover{background:#243556}"
    # --- ツールバー（キャンバス右上）---
    ".gtools{position:absolute;top:10px;right:10px;z-index:4;display:flex;gap:6px}"
    # --- フォーカス時のノードカード ---
    ".ncard{position:absolute;z-index:4;background:rgba(20,28,46,.96);border:1px solid #2b3a5e;"
    "border-radius:10px;padding:10px 12px;font-size:12px;color:#dbe4f5;max-width:240px;"
    "box-shadow:0 6px 20px rgba(0,0,0,.4)}"
    ".ncard[hidden]{display:none}"
    ".ncard .nt{font-weight:600;font-size:13px;margin-bottom:4px;color:#fff;line-height:1.35}"
    ".ncard .nm{color:#93a1bd;margin:2px 0}"
    ".ncard .nopen{margin-top:8px;background:#2553a6;border:0;border-radius:6px;color:#eef4ff;"
    "padding:5px 10px;font-size:12px;cursor:pointer}"
    ".ncard .nopen:hover{background:#2d63c2}"
)


# Obsidian 風アプリシェル（リボン + タグ explorer + 本体タブ + 右プレビューパネル）。
# 両エンジン（検索/グラフ）の DOM をこの中に mount し、JS で setMode して切替える。
_SHELL_STYLE = (
    "*{box-sizing:border-box}"
    "body{margin:0;background:var(--bg-primary);color:var(--text);font-family:-apple-system,"
    "'Hiragino Sans','Noto Sans JP',sans-serif}"
    ".shell{display:grid;grid-template-columns:var(--ribbon-w) var(--side-w) 1fr 0;"
    "height:100vh;transition:grid-template-columns .14s ease}"
    ".shell.left-closed{grid-template-columns:var(--ribbon-w) 0 1fr 0}"
    ".shell.right-open{grid-template-columns:var(--ribbon-w) var(--side-w) 1fr var(--right-w)}"
    ".shell.left-closed.right-open{grid-template-columns:var(--ribbon-w) 0 1fr var(--right-w)}"
    # --- 左リボン ---
    ".ribbon{background:var(--bg-secondary);border-right:1px solid var(--border);"
    "display:flex;flex-direction:column;align-items:center;padding:8px 0;gap:4px}"
    ".rib{width:32px;height:32px;display:flex;align-items:center;justify-content:center;"
    "border-radius:8px;color:var(--muted);cursor:pointer;border:0;background:none;"
    "transition:background .14s,color .14s}"
    ".rib:hover{background:var(--bg-hover);color:var(--text)}"
    ".rib.active{color:var(--accent);background:rgba(79,140,255,.14)}"
    ".rib svg{width:19px;height:19px;display:block}"
    ".ribspace{flex:1}"
    ".ribmail{font-size:9px;color:var(--faint);writing-mode:vertical-rl;text-orientation:mixed;"
    "max-height:160px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;margin-bottom:4px}"
    # --- 左サイドバー（検索ボックス + タグ explorer）---
    ".side{background:var(--bg-secondary);border-right:1px solid var(--border);"
    "overflow:auto;display:flex;flex-direction:column}"
    ".shell.left-closed .side{display:none}"
    ".side-search{padding:12px 12px 8px}"
    ".side-search input{width:100%;background:var(--bg-elev);border:1px solid var(--border);"
    "border-radius:8px;color:var(--text);padding:8px 10px;font-size:13px}"
    ".tagexp{padding:4px 6px 16px}"
    ".tgrp{border-top:1px solid #1c2740}.tgrp:first-child{border-top:0}"
    ".tgrp>h4{margin:0;padding:9px 8px;font-size:12px;font-weight:600;color:#dbe4f5;cursor:pointer;"
    "display:flex;justify-content:space-between;align-items:center;user-select:none}"
    ".tgrp>h4 .cv{color:var(--faint);font-size:10px}"
    ".tgrp.collapsed .tbody{display:none}"
    ".tbody{padding:0 4px 6px}"
    ".trow{display:flex;justify-content:space-between;align-items:center;gap:8px;"
    "padding:4px 8px;border-radius:6px;cursor:pointer;font-size:12px;color:#c5d0e6}"
    ".trow:hover{background:var(--bg-hover)}"
    ".trow.on{background:rgba(79,140,255,.16);border-left:2px solid var(--accent)}"
    ".trow .tv{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    ".trow .tc{color:var(--faint);font-variant-numeric:tabular-nums;flex:0 0 auto}"
    ".tempty{color:var(--faint);font-size:11px;padding:4px 8px}"
    ".tactive{padding:6px 10px;display:flex;align-items:center;gap:8px}"
    ".tactive .pill{display:inline-flex;align-items:center;gap:6px;background:rgba(79,140,255,.22);"
    "color:#bcd4ff;border-radius:999px;padding:3px 10px;font-size:12px}"
    ".tactive .pill button{background:none;border:0;color:#bcd4ff;cursor:pointer;padding:0}"
    # --- 本体（タブ + 2エンジン）---
    ".work{overflow:auto;min-width:0;position:relative;display:flex;flex-direction:column}"
    ".ws-tabs{display:flex;gap:4px;padding:8px 12px 0;border-bottom:1px solid var(--border);"
    "flex:0 0 auto}"
    ".ws-tab{background:none;border:0;border-bottom:2px solid transparent;color:var(--muted);"
    "padding:8px 14px;font-size:13px;cursor:pointer;transition:color .14s}"
    ".ws-tab:hover{color:var(--text)}"
    ".ws-tab.active{color:var(--accent);border-bottom-color:var(--accent)}"
    ".ws-body{flex:1;overflow:auto;min-height:0}"
    "#mainList{padding:6px 0}"
    "#mainList main{max-width:760px}"
    "#mainGraph[hidden]{display:none}"
    "#mainGraph main{max-width:none;padding:14px 18px}"
    # シェル内ではグラフを本体いっぱいに広げる（スタンドアロンの 600px を上書き）。
    "#mainGraph .graphwrap{height:calc(100vh - 150px);min-height:360px}"
    # --- 右プレビューパネル（grid 4列目）---
    ".right{background:var(--bg-elev);border-left:1px solid var(--border);overflow:auto}"
    ".shell:not(.right-open) .right{display:none}"
    ".pv{padding:16px 18px}"
    ".pv .pvhd{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}"
    ".pv h2{font-size:16px;margin:0 0 8px;line-height:1.4}"
    ".pv .pvx{background:none;border:0;color:var(--muted);font-size:18px;cursor:pointer;"
    "line-height:1;flex:0 0 auto}"
    ".pv .pvchips{margin:4px 0 10px}"
    ".pv .pvex{color:#c5d0e6;font-size:13px;line-height:1.7;margin:8px 0 12px}"
    ".pv .pvopen{background:var(--accent);border:0;border-radius:8px;color:#fff;"
    "padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer;margin-bottom:14px}"
    ".pv .pvopen:hover{background:#3f7cef}"
    ".relhdr{display:flex;justify-content:space-between;align-items:center;width:100%;"
    "background:none;border:0;color:#cfe0ff;font-size:13px;font-weight:600;padding:8px 0;"
    "cursor:pointer}"
    ".relcount{background:rgba(79,140,255,.22);color:#bcd4ff;border-radius:999px;"
    "padding:1px 8px;font-size:11px}"
    ".relrow{padding:6px 0;border-top:1px solid #1f2940}"
    ".relrow .t{color:#cfe0ff;cursor:pointer;font-size:13px}.relrow .t:hover{color:var(--accent)}"
    ".relrow .rv{display:block;color:var(--faint);font-size:11px;margin-top:2px}"
    ".relsec.folded .relbody{display:none}"
    ".relhead{font-size:13px;font-weight:600;color:#dbe4f5;margin:10px 0 2px}"
    ".relnone{color:var(--muted);font-size:12px;padding:6px 0}"
)


# パワー機能（クイックスイッチャー/コマンドパレット/ホバー/ショートカット）の見た目。
# モーダルは中央オーバーレイ・ポップオーバーは pointer-events:none のフロート。
_POWER_STYLE = (
    ".pmodal{position:fixed;inset:0;z-index:50;display:flex;align-items:flex-start;"
    "justify-content:center;background:rgba(6,9,16,.55);backdrop-filter:blur(2px);"
    "opacity:0;transition:opacity .14s ease}"
    ".pmodal.show{opacity:1}"
    ".pmodal[hidden]{display:none}"
    ".pbox{margin-top:12vh;width:min(560px,92vw);background:var(--bg-elev);"
    "border:1px solid var(--border);border-radius:12px;overflow:hidden;"
    "box-shadow:0 18px 50px rgba(0,0,0,.5);transform:translateY(-6px);"
    "transition:transform .14s ease}"
    ".pmodal.show .pbox{transform:translateY(0)}"
    ".pbox input{width:100%;box-sizing:border-box;background:none;border:0;"
    "border-bottom:1px solid var(--border);color:var(--text);font-size:15px;"
    "padding:14px 16px;outline:none}"
    ".pbox input::placeholder{color:var(--faint)}"
    ".plist{max-height:46vh;overflow:auto;padding:6px}"
    ".prow{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:8px;"
    "cursor:pointer;color:var(--text);font-size:13px}"
    ".prow .pt{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    ".prow .ps{color:var(--faint);font-size:11px;flex:0 0 auto;"
    "max-width:45%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    ".prow.sel{background:rgba(79,140,255,.18)}"
    ".prow:hover{background:var(--bg-hover)}"
    ".pempty{color:var(--muted);font-size:13px;padding:14px 16px}"
    ".phint{padding:7px 14px;border-top:1px solid var(--border);color:var(--faint);"
    "font-size:11px;display:flex;gap:14px;flex-wrap:wrap}"
    ".phint b{color:var(--muted);font-weight:600}"
    # --- ホバーポップオーバー（リスト/グラフ共通）---
    ".phover{position:fixed;z-index:48;pointer-events:none;max-width:320px;"
    "background:var(--bg-elev);border:1px solid var(--border);border-radius:10px;"
    "padding:10px 12px;box-shadow:0 8px 28px rgba(0,0,0,.45);font-size:12px;"
    "color:var(--text);opacity:0;transition:opacity .14s ease}"
    ".phover.show{opacity:1}"
    ".phover[hidden]{display:none}"
    ".phover .ht{font-weight:600;font-size:13px;color:#fff;margin-bottom:5px;"
    "line-height:1.35}"
    ".phover .hpills{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px}"
    ".phover .hp{background:rgba(79,140,255,.16);color:#bcd4ff;border-radius:999px;"
    "padding:1px 8px;font-size:11px}"
    ".phover .hx{color:#c5d0e6;line-height:1.6;margin-bottom:5px}"
    ".phover .hd{color:var(--faint);font-size:11px}"
    # --- ショートカット一覧オーバーレイ ---
    ".pcheat{max-width:520px}"
    ".pcheat .cgrid{display:grid;grid-template-columns:1fr 1fr;gap:6px 22px;"
    "padding:14px 16px}"
    ".pcheat .crow{display:flex;justify-content:space-between;align-items:center;"
    "gap:12px;font-size:13px;color:var(--text)}"
    ".pcheat .crow kbd{background:var(--bg-secondary);border:1px solid var(--border);"
    "border-bottom-width:2px;border-radius:5px;padding:1px 7px;font-size:11px;"
    "font-family:ui-monospace,Menlo,monospace;color:#cfe0ff}"
    ".pcheat .chd{padding:14px 16px 4px;font-weight:600;color:#fff;font-size:15px}"
    # --- アクセシビリティ: フォーカスリング ---
    ".prow:focus-visible,.pbox input:focus-visible,button:focus-visible,"
    "a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}"
    # --- グラフ取得中のローディング骨格 ---
    ".graphwrap .status.loading::after{content:'';display:inline-block;width:12px;"
    "height:12px;margin-left:8px;border:2px solid var(--faint);border-top-color:"
    "var(--accent);border-radius:50%;vertical-align:-2px;animation:pspin .8s linear "
    "infinite}"
    "@keyframes pspin{to{transform:rotate(360deg)}}"
)


# グラフ UI の本体 DOM フラグメント（シェルの #mainGraph に mount する。_GRAPH_JS が参照）。
# 「社内ナレッジグラフ」「id="cv"」はグラフルートテストが参照するので維持。
_GRAPH_DOM = (
    "<main>"
    "<h1>🕸 社内ナレッジグラフ</h1>"
    '<p class="sub">社内ナレッジを業界・案件・取引先のつながりで俯瞰します。</p>'
    '<div id="legend" class="legend"></div>'
    '<div class="graphwrap"><canvas id="cv"></canvas>'
    '<button id="gToggle" class="gtoggle" title="グラフ設定" '
    'aria-label="グラフ設定">⚙</button>'
    '<div id="gPanel" class="gpanel" hidden></div>'
    '<div class="gtools">'
    '<button id="fitBtn" class="gbtn" title="全体表示 (f)">全体表示</button>'
    '<button id="resetBtn" class="gbtn" title="リセット (r)">リセット</button>'
    "</div>"
    '<div id="ncard" class="ncard" hidden></div>'
    '<div id="tip" class="tip"></div>'
    '<div id="status" class="status">読み込み中…</div></div>'
    '<p class="sub" style="margin-top:8px">'
    "クリックで近傍にフォーカス・ダブルクリック（または右クリック）で出典を開く。"
    "ドラッグで固定、Option+クリックで固定解除。</p>"
    "</main>"
)


# 依存ゼロの canvas force-directed グラフ（CDN 不要＝社内プロキシ下でも動く）。
_GRAPH_JS = r"""
const cv=document.getElementById('cv');
const ctx=cv.getContext('2d');
const tip=document.getElementById('tip');
const statusEl=document.getElementById('status');
const legendEl=document.getElementById('legend');
const ncard=document.getElementById('ncard');
const gPanel=document.getElementById('gPanel');
const gToggle=document.getElementById('gToggle');
const fitBtn=document.getElementById('fitBtn');
const resetBtn=document.getElementById('resetBtn');
let searchEl=null,localChk=null;
const PALETTE=['#4f8cff','#2dd4a7','#f0997b','#c084fc','#f7c14b',
  '#ec6a9c','#7fd6a0','#6ea8fe','#e2725b','#9aa7bd'];
let nodes=[],edges=[],adj=new Map();
let view={x:0,y:0,scale:1};
let target={x:0,y:0,scale:1};
let alpha=0,alphaTarget=0;
let panVel={x:0,y:0};
let dirty=true;
let hover=null,dragNode=null,panning=false,moved=false;
let lastX=0,lastY=0,downX=0,downY=0;
let lastClickT=0,lastClickNode=null;
let focusNode=null,followNode=null,followUntil=0,pendingFit=false;
const groupColors=new Map();
// 本家 Obsidian 分析に基づくパラメータ: velocity Verlet + velocityDecay 0.4（v*=0.6）で
// 臨界減衰させジッタを消す。alpha は冷却し alphaTarget で再加熱（ドラッグ 0.3 / 離す 0）。
// ズームは lerp でカーソル基点に滑らか、パンは慣性付き。位置に alpha は掛けない。
const VDECAY=0.4,ADECAY=0.0228,AMIN=0.001,ZL=0.2;
// フォース調整スライダで実時間変更する可変パラメータ（旧 const → let）。
let REPEL=-150,LINKD=46,CENTER=0.05,LINKK=1;
// 表示系の可変パラメータ（ラベル LOD の基点 / ノード倍率 / リンク太さ / アニメ ON）。
let LABEL_PIVOT=0.7,NODE_SCALE=1,LINK_W=1,ANIMATE=true;
// フィルタ・グループ・ローカルグラフの状態。
let filterText='',showOrphans=true,colorBy='group';
let localOn=false,depth=1,colorEdges=false;
const hideSources=new Set(),hideTypes=new Set();
const linkedIds=new Set();
let rules=[];
// 大規模グラフ（ノード過多）判定とハブ集合。hairball を避けるための LOD/疎開に使う。
// bigGraph 時は反発を増やして広げ、ラベルは hub+近傍のみ、薄いエッジはズームで段階表示。
let bigGraph=false;
const hubIds=new Set();
const BIG_N=120,HUB_K=15;
// ズーム閾値: これ未満では大規模グラフのラベル/エッジを描かず「点」だけにする。
const HUB_LABEL_SCALE=1.15,EDGE_MIN_SCALE=0.55;
// ノード数に応じた実効反発（スライダ値 REPEL を基準に密集を防ぐ）。
function effRepel(){
  return bigGraph?REPEL*(1+nodes.length/200):REPEL;
}
const REASON_COLORS={project:'rgba(127,214,160,.65)',client:'rgba(240,153,123,.6)',
  industry:'rgba(110,168,254,.55)',concept:'rgba(189,147,249,.5)'};
function safeUrl(u){return (typeof u==='string'&&/^(https?|slack|gdrive):/i.test(u))?u:null;}
function colorOf(g){
  if(!groupColors.has(g))groupColors.set(g,PALETTE[groupColors.size%PALETTE.length]);
  return groupColors.get(g);
}
function ckeyOf(n){return colorBy==='group'?(n.group||'other'):(n[colorBy]||'その他');}
function ruleColor(n){
  const s=searchBlob(n);
  for(const ru of rules){if(ru.q&&s.includes(ru.q))return ru.color;}
  return null;
}
function nodeColor(n){return ruleColor(n)||colorOf(n.ckey);}
function searchBlob(n){
  return (n.title+' '+(n.industry||'')+' '+(n.project||'')+' '+(n.doc_type||'')
    +' '+(n.client_name||'')).toLowerCase();
}
function matches(n){return !filterText||searchBlob(n).includes(filterText);}
function visible(n){
  if(n._hidden)return false;
  if(!showOrphans&&!linkedIds.has(n.id))return false;
  if(n.source_type&&hideSources.has(n.source_type))return false;
  if(n.doc_type&&hideTypes.has(n.doc_type))return false;
  return true;
}
function resize(){
  const dpr=window.devicePixelRatio||1;
  const r=cv.getBoundingClientRect();
  cv.width=Math.max(1,Math.floor(r.width*dpr));
  cv.height=Math.max(1,Math.floor(r.height*dpr));
  ctx.setTransform(dpr,0,0,dpr,0,0);
  dirty=true;
}
function W(){return cv.getBoundingClientRect().width;}
function H(){return cv.getBoundingClientRect().height;}
function toWorld(sx,sy){return {x:(sx-view.x)/view.scale,y:(sy-view.y)/view.scale};}
function nodeAt(sx,sy){
  const w=toWorld(sx,sy);let best=null,bd=1e9;
  for(const n of nodes){
    if(!visible(n))continue;
    const dx=n.x-w.x,dy=n.y-w.y,d=dx*dx+dy*dy;
    const r=(n.r||6)+5;if(d<r*r&&d<bd){bd=d;best=n;}}
  return best;
}
function tick(){
  alpha+=(alphaTarget-alpha)*ADECAY;
  if(alpha<AMIN&&alphaTarget===0)alpha=0;
  const cx=W()/2,cy=H()/2,nn=nodes.length;
  const repel=effRepel();
  for(let i=0;i<nn;i++){
    const a=nodes[i];if(a._hidden)continue;
    for(let j=i+1;j<nn;j++){
      const b=nodes[j];if(b._hidden)continue;
      let dx=b.x-a.x,dy=b.y-a.y,d2=dx*dx+dy*dy;
      if(d2<1e-4){dx=(Math.random()-0.5)*0.1;dy=(Math.random()-0.5)*0.1;d2=dx*dx+dy*dy;}
      const w=repel*alpha/d2;
      a.vx+=dx*w;a.vy+=dy*w;b.vx-=dx*w;b.vy-=dy*w;
    }
  }
  for(const e of edges){
    const a=e.s,b=e.t;if(a._hidden||b._hidden)continue;
    let dx=b.x-a.x,dy=b.y-a.y;const l=Math.sqrt(dx*dx+dy*dy)||1e-6;
    const k=LINKK*(l-LINKD)/l*alpha/Math.min(e.da,e.db);const bias=e.da/(e.da+e.db);
    a.vx+=dx*k*(1-bias);a.vy+=dy*k*(1-bias);b.vx-=dx*k*bias;b.vy-=dy*k*bias;
  }
  const fr=1-VDECAY;
  for(const n of nodes){
    if(n._hidden)continue;
    if(n===dragNode){n.vx=0;n.vy=0;n.x=n.fx;n.y=n.fy;continue;}
    if(n.pinned){n.vx=0;n.vy=0;n.x=n.px;n.y=n.py;continue;}
    n.vx+=(cx-n.x)*CENTER*alpha;n.vy+=(cy-n.y)*CENTER*alpha;
    n.vx*=fr;n.vy*=fr;n.x+=n.vx;n.y+=n.vy;
  }
}
function easeCamera(){
  if(followNode){
    if(performance.now()>followUntil)followNode=null;
    else{target.x=W()/2-followNode.x*target.scale;
      target.y=H()/2-followNode.y*target.scale;}
  }
  view.scale+=(target.scale-view.scale)*ZL;
  view.x+=(target.x-view.x)*ZL;view.y+=(target.y-view.y)*ZL;
  if(!panning&&!dragNode&&(Math.abs(panVel.x)>0.04||Math.abs(panVel.y)>0.04)){
    target.x+=panVel.x;target.y+=panVel.y;view.x+=panVel.x;view.y+=panVel.y;
    panVel.x*=0.9;panVel.y*=0.9;
  }
}
function reheat(){alpha=Math.max(alpha,0.3);alphaTarget=0;dirty=true;}
function flyTo(n){
  const s=Math.max(view.scale,1.6);
  target.scale=s;target.x=W()/2-n.x*s;target.y=H()/2-n.y*s;
  panVel.x=0;panVel.y=0;followNode=n;followUntil=performance.now()+700;dirty=true;
}
function zoomToFit(pad){
  pad=pad||60;
  let minx=1e9,miny=1e9,maxx=-1e9,maxy=-1e9,any=false;
  for(const n of nodes){
    if(!visible(n))continue;any=true;
    minx=Math.min(minx,n.x-n.r);miny=Math.min(miny,n.y-n.r);
    maxx=Math.max(maxx,n.x+n.r);maxy=Math.max(maxy,n.y+n.r);
  }
  if(!any)return;
  const bw=Math.max(1,maxx-minx),bh=Math.max(1,maxy-miny);
  const s=Math.min(4,Math.max(0.2,Math.min((W()-pad*2)/bw,(H()-pad*2)/bh)));
  const cx=(minx+maxx)/2,cy=(miny+maxy)/2;
  target.scale=s;target.x=W()/2-cx*s;target.y=H()/2-cy*s;
  followNode=null;panVel.x=0;panVel.y=0;dirty=true;
}
function resetView(){
  alpha=1;alphaTarget=0;focusNode=null;hideNodeCard();
  localOn=false;if(localChk)localChk.checked=false;
  for(const n of nodes){n.pinned=false;n._hidden=false;}
  filterText='';if(searchEl)searchEl.value='';
  showOrphans=true;hideSources.clear();hideTypes.clear();
  REPEL=-150;LINKD=46;CENTER=0.05;LINKK=1;
  LABEL_PIVOT=0.7;NODE_SCALE=1;LINK_W=1;ANIMATE=true;
  colorBy='group';colorEdges=false;rules=[];depth=1;
  for(const n of nodes){n.r=n.r0*NODE_SCALE;n.ckey=ckeyOf(n);}
  recolor();buildPanel();dirty=true;
  setTimeout(function(){zoomToFit();},400);
}
function recolor(){
  groupColors.clear();
  for(const n of nodes){n.ckey=ckeyOf(n);colorOf(n.ckey);}
  buildLegend();dirty=true;
}
function camMoving(){
  return Math.abs(target.scale-view.scale)>1e-3||Math.abs(target.x-view.x)>0.4
    ||Math.abs(target.y-view.y)>0.4||Math.abs(panVel.x)>0.04||Math.abs(panVel.y)>0.04;
}
function draw(){
  ctx.clearRect(0,0,W(),H());
  ctx.save();ctx.translate(view.x,view.y);ctx.scale(view.scale,view.scale);
  const emph=hover||focusNode;
  const hl=emph?adj.get(emph.id):null;
  ctx.lineWidth=LINK_W/view.scale;ctx.lineCap='round';
  // エッジ de-haze: 大規模グラフはベースを薄く、ズームアウト時は地のエッジを省略して
  // 「点だけ」にする（淡いエッジの重なりで暗いキャンバスが白っぽく霞むのを防ぐ）。
  const baseEdge=bigGraph?'rgba(120,140,180,.05)':'rgba(120,140,180,.15)';
  const drawBase=!bigGraph||view.scale>=EDGE_MIN_SCALE;
  for(const e of edges){
    if(!visible(e.s)||!visible(e.t))continue;
    const on=emph&&(e.s===emph||e.t===emph);
    // フォーカス/ホバーの接続線は常に明るく上書き描画。地のエッジはズーム閾値で省略可。
    if(!on&&!drawBase)continue;
    if(on)ctx.strokeStyle='rgba(127,214,160,.7)';
    else if(colorEdges)ctx.strokeStyle=REASON_COLORS[e.kind]||baseEdge;
    else ctx.strokeStyle=baseEdge;
    ctx.beginPath();ctx.moveTo(e.s.x,e.s.y);ctx.lineTo(e.t.x,e.t.y);ctx.stroke();
  }
  for(const n of nodes){
    if(!visible(n))continue;
    const dim=(emph&&n!==emph&&!(hl&&hl.has(n.id)))||!matches(n);
    const r=(n===hover?n.r+3:n.r);
    ctx.beginPath();ctx.arc(n.x,n.y,r,0,6.2832);
    ctx.fillStyle=nodeColor(n);ctx.globalAlpha=dim?0.12:1;ctx.fill();
    if(n===emph){ctx.lineWidth=2/view.scale;ctx.strokeStyle='#fff';ctx.stroke();}
    if(n.pinned){ctx.lineWidth=1.5/view.scale;
      ctx.strokeStyle='rgba(255,255,255,.55)';
      ctx.beginPath();ctx.arc(n.x,n.y,r+2,0,6.2832);ctx.stroke();}
    ctx.globalAlpha=1;
  }
  // ラベル LOD（最重要）: 数百ラベルを一斉描画して重なる「文字の壁」を防ぐ。
  // ラベルを描くのは — emph（ホバー/フォーカス）か focus の近傍、または
  // 小規模グラフで十分ズーム、または大規模グラフで hub かつ十分ズーム — のみ。
  const la=Math.max(0,Math.min(1,(view.scale-LABEL_PIVOT)/0.6));
  // 大規模グラフ: hub は scale>=1.15 で、非 hub は更にズームインした時だけ徐々に出す
  // （深くズームすると Obsidian のように全ラベルが見える）。emph 周辺は常に表示。
  const hubLabels=bigGraph&&view.scale>=HUB_LABEL_SCALE;
  const allLa=bigGraph?Math.max(0,Math.min(1,(view.scale-2.0)/1.0)):0;
  const smallLabels=!bigGraph&&la>0.02;
  if(emph||hubLabels||smallLabels||allLa>0.02){
    ctx.font='11px sans-serif';ctx.fillStyle='#c5d0e6';
    for(const n of nodes){
      if(!visible(n))continue;
      const near=emph&&(n===emph||(hl&&hl.has(n.id)));
      let show=false,a=la;
      if(near){show=true;a=1;}
      else if(emph){show=false;}
      else if(smallLabels){show=true;a=la;}
      else if(bigGraph){
        if(hubLabels&&hubIds.has(n.id)){show=true;a=1;}
        else if(allLa>0.02){show=true;a=allLa;}
      }
      if(!show)continue;
      ctx.globalAlpha=a;
      ctx.fillText((n.title||'').slice(0,24),n.x+n.r+3,n.y+3);
    }
    ctx.globalAlpha=1;
  }
  if(emph&&view.scale>0.9){
    ctx.font='10px sans-serif';ctx.fillStyle='rgba(197,208,230,.7)';
    for(const e of edges){
      if((e.s!==emph&&e.t!==emph)||!visible(e.s)||!visible(e.t))continue;
      if(e.reason)ctx.fillText(e.reason,(e.s.x+e.t.x)/2,(e.s.y+e.t.y)/2);
    }
  }
  ctx.restore();
}
function loop(){
  const sim=ANIMATE&&(alpha>AMIN||alphaTarget>0);
  if(sim)tick();
  if(pendingFit&&alpha<0.6){pendingFit=false;zoomToFit();}
  easeCamera();
  if(sim||camMoving()||dirty){draw();dirty=false;}
  requestAnimationFrame(loop);
}
// 大規模グラフ時、隅に小さなヒントを出す（ズーム/ホバーでラベル, ⚙で絞り込み）。
// _GRAPH_DOM には要素が無いので .graphwrap に動的生成し textContent で安全に設定する。
let hintEl=null;
function updateHint(){
  const wrap=cv.parentElement;if(!wrap)return;
  if(bigGraph){
    if(!hintEl){
      hintEl=document.createElement('div');hintEl.className='ghint';
      wrap.appendChild(hintEl);
    }
    hintEl.textContent='ノード数 '+nodes.length
      +' — ズーム/ホバーでラベル表示, ⚙で絞り込み';
    hintEl.hidden=false;
  }else if(hintEl){hintEl.hidden=true;}
}
function buildLegend(){
  legendEl.textContent='';
  for(const g of groupColors.keys()){
    const w=document.createElement('span');w.className='lg';
    const d=document.createElement('span');d.className='dot';d.style.background=colorOf(g);
    const t=document.createElement('span');t.textContent=g;
    w.appendChild(d);w.appendChild(t);legendEl.appendChild(w);
  }
}
async function load(){
  let data;
  statusEl.classList.add('loading');
  try{
    const resp=await fetch('/api/v1/graph');
    if(resp.status===401){location.href='/search/login';return;}
    data=await resp.json();
  }catch(e){statusEl.classList.remove('loading');
    statusEl.textContent='グラフの取得に失敗しました。';return;}
  statusEl.classList.remove('loading');
  const ns=(data.nodes||[]);
  if(!ns.length){statusEl.textContent='表示できる社内ナレッジがありません（再取込後に分類タグが付きます）。';return;}
  statusEl.style.display='none';
  resize();
  const cx=W()/2,cy=H()/2;
  const byId=new Map();
  nodes=ns.map((n,i)=>{
    const ang=i*2.399,rad=12+i*0.6;
    const o={id:n.id,title:n.title||'',url:n.url,group:n.group||'other',
      source_type:n.source_type||null,industry:n.industry||null,
      project:n.project||null,doc_type:n.doc_type||null,
      client_name:n.client_name||null,excerpt:n.excerpt||'',
      x:cx+Math.cos(ang)*rad,y:cy+Math.sin(ang)*rad,vx:0,vy:0,fx:0,fy:0,
      r:6,r0:6,deg:0,pinned:false,px:0,py:0,_hidden:false,ckey:''};
    o.ckey=ckeyOf(o);byId.set(n.id,o);colorOf(o.ckey);return o;
  });
  adj=new Map();for(const n of nodes)adj.set(n.id,new Set());
  linkedIds.clear();
  for(const e of (data.edges||[])){
    const s=byId.get(e.source),t=byId.get(e.target);if(!s||!t)continue;
    adj.get(s.id).add(t.id);adj.get(t.id).add(s.id);
    linkedIds.add(s.id);linkedIds.add(t.id);
  }
  for(const n of nodes){
    n.deg=adj.get(n.id).size;n.r0=4+Math.sqrt(n.deg)*1.6;n.r=n.r0*NODE_SCALE;
  }
  // 大規模グラフ判定と hub（高次数 top-K）抽出。ラベル LOD/疎開の基準にする。
  bigGraph=nodes.length>BIG_N;
  hubIds.clear();
  if(bigGraph){
    const ranked=nodes.slice().sort(function(a,b){return b.deg-a.deg;});
    for(let i=0;i<Math.min(HUB_K,ranked.length);i++)hubIds.add(ranked[i].id);
  }
  updateHint();
  edges=[];
  for(const e of (data.edges||[])){
    const s=byId.get(e.source),t=byId.get(e.target);if(!s||!t)continue;
    const kind=(e.reason||'').split(':')[0];
    edges.push({s:s,t:t,reason:e.reason,kind:kind,
      da:Math.max(1,s.deg),db:Math.max(1,t.deg)});
  }
  buildLegend();buildPanel();
  view={x:0,y:0,scale:1};target={x:0,y:0,scale:1};panVel={x:0,y:0};
  alpha=1;alphaTarget=0;dirty=true;pendingFit=true;
}
function openSource(n){
  if(!n)return;const u=safeUrl(n.url);
  if(u)window.open(u,'_blank','noopener,noreferrer');
}
function localSet(rootId,d){
  const seen=new Set([rootId]);let frontier=[rootId];
  for(let i=0;i<d;i++){
    const next=[];
    for(const id of frontier){
      for(const nb of (adj.get(id)||[])){
        if(!seen.has(nb)){seen.add(nb);next.push(nb);}
      }
    }
    frontier=next;if(!frontier.length)break;
  }
  return seen;
}
function applyLocal(){
  if(localOn&&focusNode){
    const keep=localSet(focusNode.id,depth);
    for(const n of nodes)n._hidden=!keep.has(n.id);
  }else{
    for(const n of nodes)n._hidden=false;
  }
  dirty=true;setTimeout(function(){zoomToFit();},50);
}
function setFocus(n){
  focusNode=n;flyTo(n);renderNodeCard(n);applyLocal();dirty=true;
  // シェル統合用の任意フック。スタンドアロン時は未定義で何もしない。
  if(typeof window.onGraphFocus==='function'){
    try{window.onGraphFocus({node_id:n.id,title:n.title,source_uri:n.url,
      industry:n.industry,project:n.project,doc_type:n.doc_type,
      source_type:n.source_type,client_name:n.client_name});}catch(e){}
  }
}
function clearFocus(){
  focusNode=null;hideNodeCard();if(localOn){localOn=false;
    if(localChk)localChk.checked=false;applyLocal();}
  dirty=true;
  if(typeof window.onGraphClear==='function'){try{window.onGraphClear();}catch(e){}}
}
// シェル統合用: タグ explorer / 関連パネルから特定ノードを focus する。
window.graphFocusByTitle=function(title){
  for(const n of nodes){if(n.title===title){setFocus(n);return true;}}
  return false;
};
function renderNodeCard(n){
  ncard.textContent='';
  const t=document.createElement('div');t.className='nt';t.textContent=n.title||'(無題)';
  ncard.appendChild(t);
  const tags=[['取引先',n.client_name],['業界',n.industry],
    ['案件',n.project],['種別',n.doc_type],['出典',n.source_type]];
  for(const [lab,val] of tags){
    if(!val)continue;
    const m=document.createElement('div');m.className='nm';
    m.textContent=lab+': '+val;ncard.appendChild(m);
  }
  const deg=document.createElement('div');deg.className='nm';
  deg.textContent='接続: '+n.deg+'件';ncard.appendChild(deg);
  const u=safeUrl(n.url);
  if(u){
    const b=document.createElement('button');b.className='nopen';
    b.textContent='出典を開く ↗';
    b.addEventListener('click',function(){openSource(n);});
    ncard.appendChild(b);
  }
  const r=cv.getBoundingClientRect();
  let lx=n.x*view.scale+view.x+14,ly=n.y*view.scale+view.y+14;
  lx=Math.max(8,Math.min(r.width-250,lx));
  ly=Math.max(8,Math.min(r.height-150,ly));
  ncard.style.left=lx+'px';ncard.style.top=ly+'px';ncard.hidden=false;
}
function hideNodeCard(){ncard.hidden=true;}
// --- 設定パネル（フィルタ / グループ / 表示 / フォース）の組み立て ---
function mkSection(title,collapsed){
  const sec=document.createElement('div');sec.className='gsec'+(collapsed?' collapsed':'');
  const h=document.createElement('h3');
  const lab=document.createElement('span');lab.textContent=title;
  const chev=document.createElement('span');chev.className='chev';
  chev.textContent=collapsed?'▸':'▾';
  h.appendChild(lab);h.appendChild(chev);
  h.addEventListener('click',function(){
    sec.classList.toggle('collapsed');
    chev.textContent=sec.classList.contains('collapsed')?'▸':'▾';
  });
  const body=document.createElement('div');body.className='gbody';
  sec.appendChild(h);sec.appendChild(body);
  return {sec:sec,body:body};
}
function mkSlider(body,label,min,max,step,val,fmt,onChange){
  const row=document.createElement('div');row.className='grow';
  const lab=document.createElement('label');
  const txt=document.createElement('span');txt.textContent=label;
  const vs=document.createElement('span');vs.className='gval';vs.textContent=fmt(val);
  lab.appendChild(txt);lab.appendChild(vs);
  const inp=document.createElement('input');inp.type='range';
  inp.min=min;inp.max=max;inp.step=step;inp.value=val;
  inp.addEventListener('input',function(e){
    const v=+e.target.value;vs.textContent=fmt(v);onChange(v);
  });
  row.appendChild(lab);row.appendChild(inp);body.appendChild(row);
  return inp;
}
function mkCheck(body,label,checked,onChange,swatch){
  const row=document.createElement('label');row.className='gchk';
  const inp=document.createElement('input');inp.type='checkbox';inp.checked=checked;
  inp.addEventListener('change',function(e){onChange(e.target.checked);});
  row.appendChild(inp);
  if(swatch){const sw=document.createElement('span');sw.className='sw';
    sw.style.background=swatch;row.appendChild(sw);}
  const t=document.createElement('span');t.textContent=label;row.appendChild(t);
  body.appendChild(row);return inp;
}
function buildPanel(){
  gPanel.textContent='';
  // フィルタ
  const f=mkSection('フィルタ',false);
  const sb=document.createElement('input');sb.type='search';
  sb.placeholder='検索（タイトル・タグ）';sb.value=filterText;
  sb.addEventListener('input',function(e){
    filterText=e.target.value.toLowerCase();dirty=true;
  });
  const sbr=document.createElement('div');sbr.className='grow';sbr.appendChild(sb);
  f.body.appendChild(sbr);searchEl=sb;
  mkCheck(f.body,'孤立ノードを表示',showOrphans,function(v){
    showOrphans=v;dirty=true;
  });
  const srcs=Array.from(new Set(nodes.map(function(n){return n.source_type;})
    .filter(Boolean))).sort();
  if(srcs.length){
    const h=document.createElement('div');h.className='grow';
    h.textContent='出典';h.style.color='#8fa3c6';f.body.appendChild(h);
    for(const s of srcs){
      mkCheck(f.body,s,!hideSources.has(s),function(v){
        if(v)hideSources.delete(s);else hideSources.add(s);
        dirty=true;
      });
    }
  }
  const dts=Array.from(new Set(nodes.map(function(n){return n.doc_type;})
    .filter(Boolean))).sort();
  if(dts.length){
    const h=document.createElement('div');h.className='grow';
    h.textContent='資料タイプ';h.style.color='#8fa3c6';f.body.appendChild(h);
    for(const d of dts){
      mkCheck(f.body,d,!hideTypes.has(d),function(v){
        if(v)hideTypes.delete(d);else hideTypes.add(d);
        dirty=true;
      });
    }
  }
  gPanel.appendChild(f.sec);
  // グループ（色分け）
  const g=mkSection('グループ(色分け)',true);
  const sel=document.createElement('select');
  const opts=[['group','資料タイプ(既定)'],['doc_type','資料タイプ'],
    ['source_type','出典'],['industry','業界'],['client_name','取引先'],
    ['project','案件']];
  for(const [v,lab] of opts){
    const o=document.createElement('option');o.value=v;o.textContent=lab;
    if(v===colorBy)o.selected=true;sel.appendChild(o);
  }
  sel.addEventListener('change',function(e){colorBy=e.target.value;recolor();});
  const selr=document.createElement('div');selr.className='grow';
  const sl=document.createElement('label');sl.textContent='色分け';
  selr.appendChild(sl);selr.appendChild(sel);g.body.appendChild(selr);
  mkCheck(g.body,'つながりの理由を色分け',colorEdges,function(v){
    colorEdges=v;dirty=true;
  });
  const rwrap=document.createElement('div');g.body.appendChild(rwrap);
  function renderRules(){
    rwrap.textContent='';
    rules.forEach(function(ru,idx){
      const row=document.createElement('div');row.className='grule';
      const tx=document.createElement('input');tx.type='text';
      tx.placeholder='語句';tx.value=ru.q;
      tx.addEventListener('input',function(e){
        rules[idx].q=e.target.value.toLowerCase();dirty=true;
      });
      const co=document.createElement('input');co.type='color';co.value=ru.color;
      co.addEventListener('input',function(e){
        rules[idx].color=e.target.value;dirty=true;
      });
      const x=document.createElement('span');x.className='x';x.textContent='×';
      x.addEventListener('click',function(){
        rules.splice(idx,1);renderRules();dirty=true;
      });
      row.appendChild(tx);row.appendChild(co);row.appendChild(x);
      rwrap.appendChild(row);
    });
  }
  const addBtn=document.createElement('button');addBtn.className='gbtn';
  addBtn.textContent='色ルール追加';
  addBtn.addEventListener('click',function(){
    if(rules.length>=6)return;
    rules.push({q:'',color:'#ff5577'});renderRules();
  });
  const abr=document.createElement('div');abr.className='grow';abr.appendChild(addBtn);
  g.body.appendChild(abr);renderRules();
  gPanel.appendChild(g.sec);
  // 表示
  const d=mkSection('表示',true);
  mkSlider(d.body,'ラベル表示しきい値',0.2,1.4,0.05,LABEL_PIVOT,
    function(v){return v.toFixed(2);},function(v){LABEL_PIVOT=v;dirty=true;});
  mkSlider(d.body,'ノードサイズ',0.4,2.5,0.1,NODE_SCALE,
    function(v){return v.toFixed(1)+'x';},function(v){
      NODE_SCALE=v;for(const n of nodes)n.r=n.r0*NODE_SCALE;dirty=true;
    });
  mkSlider(d.body,'リンクの太さ',0.3,4,0.1,LINK_W,
    function(v){return v.toFixed(1)+'x';},function(v){LINK_W=v;dirty=true;});
  mkCheck(d.body,'シミュレーション(animate)',ANIMATE,function(v){
    ANIMATE=v;if(!v)alphaTarget=0;else reheat();dirty=true;
  });
  const lr=document.createElement('div');lr.className='grow';
  localChk=mkCheck(lr,'ローカルグラフ',localOn,function(v){
    if(v&&!focusNode){localChk.checked=false;return;}
    localOn=v;applyLocal();
  });
  d.body.appendChild(lr);
  mkSlider(d.body,'深さ',1,3,1,depth,function(v){return ''+v;},function(v){
    depth=v;if(localOn&&focusNode)applyLocal();
  });
  gPanel.appendChild(d.sec);
  // フォース
  const fo=mkSection('フォース',true);
  mkSlider(fo.body,'中心力',0,0.30,0.005,CENTER,
    function(v){return v.toFixed(3);},function(v){CENTER=v;reheat();});
  mkSlider(fo.body,'反発力',0,600,10,-REPEL,
    function(v){return ''+v;},function(v){REPEL=-v;reheat();});
  mkSlider(fo.body,'リンク力',0,2,0.05,LINKK,
    function(v){return v.toFixed(2);},function(v){LINKK=v;reheat();});
  mkSlider(fo.body,'リンク距離',10,200,2,LINKD,
    function(v){return ''+v;},function(v){LINKD=v;reheat();});
  gPanel.appendChild(fo.sec);
  // リセット
  const rs=document.createElement('a');rs.className='greset';
  rs.textContent='リセット';rs.href='javascript:void(0)';
  rs.addEventListener('click',resetView);gPanel.appendChild(rs);
}
// パネル操作中はキャンバスの pan/zoom を発火させない（必須）。
gPanel.addEventListener('mousedown',function(e){e.stopPropagation();});
gPanel.addEventListener('wheel',function(e){e.stopPropagation();});
gToggle.addEventListener('click',function(){gPanel.hidden=!gPanel.hidden;});
fitBtn.addEventListener('click',function(){zoomToFit();});
resetBtn.addEventListener('click',resetView);
cv.addEventListener('mousedown',ev=>{
  const r=cv.getBoundingClientRect();const sx=ev.clientX-r.left,sy=ev.clientY-r.top;
  lastX=sx;lastY=sy;downX=sx;downY=sy;moved=false;panVel.x=0;panVel.y=0;
  followNode=null;
  const n=nodeAt(sx,sy);
  if(n&&(ev.altKey||ev.metaKey)){n.pinned=false;alphaTarget=0.3;dirty=true;return;}
  if(n){dragNode=n;n.fx=n.x;n.fy=n.y;alphaTarget=0.3;}
  else{panning=true;cv.style.cursor='grabbing';}
});
window.addEventListener('mousemove',ev=>{
  const r=cv.getBoundingClientRect();const sx=ev.clientX-r.left,sy=ev.clientY-r.top;
  if(Math.abs(sx-downX)+Math.abs(sy-downY)>4)moved=true;
  if(dragNode){const w=toWorld(sx,sy);dragNode.fx=w.x;dragNode.fy=w.y;
    alphaTarget=0.3;dirty=true;return;}
  if(panning){const dx=sx-lastX,dy=sy-lastY;
    view.x+=dx;view.y+=dy;target.x+=dx;target.y+=dy;
    panVel.x=panVel.x*0.6+dx*0.4;panVel.y=panVel.y*0.6+dy*0.4;
    lastX=sx;lastY=sy;dirty=true;return;}
  const n=nodeAt(sx,sy);
  if(n!==hover){hover=n;dirty=true;}
  if(n){tip.style.display='block';tip.style.left=(sx+12)+'px';tip.style.top=(sy+12)+'px';
    let s=n.title;if(n.group)s+='  ['+n.group+']';
    if(n.industry)s+='\n業界: '+n.industry;
    if(n.project)s+='\n案件: '+n.project;
    if(n.client_name)s+='\n取引先: '+n.client_name;
    s+='\n接続: '+n.deg+'件';
    if(n.excerpt)s+='\n'+n.excerpt.slice(0,140);
    s+='\n(クリックで詳細)';tip.textContent=s;}
  else{tip.style.display='none';}
  cv.style.cursor=n?'pointer':'grab';
});
window.addEventListener('mouseup',ev=>{
  if(dragNode){
    if(!moved){
      const now=performance.now();
      if(now-lastClickT<300&&lastClickNode===dragNode){openSource(dragNode);}
      else{setFocus(dragNode);}
      lastClickT=now;lastClickNode=dragNode;
    }else{dragNode.pinned=true;dragNode.px=dragNode.x;dragNode.py=dragNode.y;}
    dragNode=null;alphaTarget=0;
  }else if(panning&&!moved){clearFocus();}
  panning=false;cv.style.cursor='grab';
});
cv.addEventListener('dblclick',ev=>{
  const r=cv.getBoundingClientRect();
  openSource(nodeAt(ev.clientX-r.left,ev.clientY-r.top));
});
cv.addEventListener('contextmenu',ev=>{
  ev.preventDefault();const r=cv.getBoundingClientRect();
  openSource(nodeAt(ev.clientX-r.left,ev.clientY-r.top));
});
cv.addEventListener('wheel',ev=>{
  ev.preventDefault();
  followNode=null;
  const r=cv.getBoundingClientRect();const sx=ev.clientX-r.left,sy=ev.clientY-r.top;
  const factor=Math.exp(-ev.deltaY*0.0015);
  const next=Math.min(4,Math.max(0.2,target.scale*factor));
  const wx=(sx-target.x)/target.scale,wy=(sy-target.y)/target.scale;
  target.x=sx-wx*next;target.y=sy-wy*next;target.scale=next;dirty=true;
},{passive:false});
window.addEventListener('keydown',ev=>{
  const t=ev.target;if(t&&(t.tagName==='INPUT'||t.tagName==='SELECT'))return;
  if(ev.key==='f')zoomToFit();
  if(ev.key==='r')resetView();
});
window.addEventListener('resize',resize);
// シェル統合用: 外部（タグ explorer）からグラフの絞り込みテキストを設定する。
window.graphSetFilter=function(text){
  filterText=(text||'').toLowerCase();
  if(searchEl)searchEl.value=text||'';dirty=true;
};
// シェル統合用: パワー機能（パレット/永続化/ホバー）が叩く薄い制御 API。
// 既存の関数/状態を関数越しに公開するだけ（エンジン本体のロジックは不変）。
window.__graphApi={
  zoomToFit:function(){zoomToFit();},
  resetView:function(){resetView();},
  setColorBy:function(v){colorBy=v;recolor();buildPanel();},
  getColorBy:function(){return colorBy;},
  toggleOrphans:function(){showOrphans=!showOrphans;dirty=true;buildPanel();
    return showOrphans;},
  setOrphans:function(v){showOrphans=!!v;dirty=true;},
  toggleAnimate:function(){ANIMATE=!ANIMATE;if(!ANIMATE)alphaTarget=0;else reheat();
    dirty=true;buildPanel();return ANIMATE;},
  focusByTitle:function(t){return window.graphFocusByTitle(t);},
  clearFocus:function(){clearFocus();},
  hasFocus:function(){return !!focusNode;},
  // 永続化: フォース/表示/色分け/孤立/ローカル深さの一括 get/set。
  getSettings:function(){return {REPEL:REPEL,LINKD:LINKD,CENTER:CENTER,LINKK:LINKK,
    LABEL_PIVOT:LABEL_PIVOT,NODE_SCALE:NODE_SCALE,LINK_W:LINK_W,ANIMATE:ANIMATE,
    colorBy:colorBy,colorEdges:colorEdges,showOrphans:showOrphans,depth:depth};},
  applySettings:function(s){if(!s)return;
    if(typeof s.REPEL==='number')REPEL=s.REPEL;
    if(typeof s.LINKD==='number')LINKD=s.LINKD;
    if(typeof s.CENTER==='number')CENTER=s.CENTER;
    if(typeof s.LINKK==='number')LINKK=s.LINKK;
    if(typeof s.LABEL_PIVOT==='number')LABEL_PIVOT=s.LABEL_PIVOT;
    if(typeof s.NODE_SCALE==='number')NODE_SCALE=s.NODE_SCALE;
    if(typeof s.LINK_W==='number')LINK_W=s.LINK_W;
    if(typeof s.ANIMATE==='boolean')ANIMATE=s.ANIMATE;
    if(typeof s.colorBy==='string')colorBy=s.colorBy;
    if(typeof s.colorEdges==='boolean')colorEdges=s.colorEdges;
    if(typeof s.showOrphans==='boolean')showOrphans=s.showOrphans;
    if(typeof s.depth==='number')depth=s.depth;
    for(const n of nodes){n.r=n.r0*NODE_SCALE;n.ckey=ckeyOf(n);}
    recolor();buildPanel();reheat();dirty=true;},
  // ホバーポップオーバー用に node/接続数を提供（参照のみ・変更しない）。
  nodeByTitle:function(t){for(const n of nodes){if(n.title===t)return n;}return null;},
  degreeOf:function(t){for(const n of nodes){if(n.title===t)return n.deg;}return 0;}
};
// 起動はシェルから1度だけ呼ぶ（hidden な間に sim を回さないため遅延開始）。
// スタンドアロン（後方互換）なら自動起動する。
let __graphStarted=false;
window.startGraph=function(){
  if(__graphStarted)return;__graphStarted=true;
  resize();load();loop();
};
if(!window.__shellMode){window.startGraph();}
"""


# Obsidian 風シェルの統合 JS。両エンジン（検索/グラフ）の DOM を mount 済みの前提で、
# モード切替・タグ explorer・右プレビュー/関連パネルを束ねる。新 API は使わず
# /api/v1/graph を 1 度だけ取得してタグ件数（nodes）と関連（edges 隣接）を導出する。
_SHELL_JS = r"""
window.__shellMode=true;
const shell=document.getElementById('shell');
const ribList=document.getElementById('ribList');
const ribGraph=document.getElementById('ribGraph');
const tabList=document.getElementById('tabList');
const tabGraph=document.getElementById('tabGraph');
const mainList=document.getElementById('mainList');
const mainGraph=document.getElementById('mainGraph');
const sideSearch=document.getElementById('sideSearch');
const tagexp=document.getElementById('tagexp');
const leftToggle=document.getElementById('leftToggle');
const rightPanel=document.getElementById('rightPanel');
const LS='ta_shell_v1';
let st={mode:'list',leftOpen:true,rightOpen:false};
try{const raw=localStorage.getItem(LS);if(raw)st=Object.assign(st,JSON.parse(raw));}catch(e){}
let currentMode=st.mode==='graph'?'graph':'list';
let graphStarted=false;
function persist(){try{localStorage.setItem(LS,JSON.stringify(
  {mode:currentMode,leftOpen:st.leftOpen,rightOpen:st.rightOpen}));}catch(e){}}
function applyLeft(){
  if(st.leftOpen)shell.classList.remove('left-closed');
  else shell.classList.add('left-closed');
}
function setMode(m){
  currentMode=(m==='graph')?'graph':'list';
  const g=currentMode==='graph';
  mainGraph.hidden=!g;mainList.hidden=g;
  ribGraph.classList.toggle('active',g);ribList.classList.toggle('active',!g);
  tabGraph.classList.toggle('active',g);tabList.classList.toggle('active',!g);
  if(g&&!graphStarted){graphStarted=true;
    // hidden を解いた次フレームで起動（canvas の実寸が出てから resize するため）。
    requestAnimationFrame(function(){requestAnimationFrame(function(){
      if(window.startGraph)window.startGraph();});});
  }
  persist();
}
window.shellSetMode=setMode;
ribList.addEventListener('click',function(){setMode('list');});
ribGraph.addEventListener('click',function(){setMode('graph');});
tabList.addEventListener('click',function(){setMode('list');});
tabGraph.addEventListener('click',function(){setMode('graph');});
leftToggle.addEventListener('click',function(){
  st.leftOpen=!st.leftOpen;applyLeft();persist();
});
// サイドバー検索ボックス（本体の #q と同期して既存検索エンジンを駆動）。
// _SEARCH_JS が宣言した global の q と衝突しないよう再宣言せず getElementById で参照する。
sideSearch.addEventListener('keydown',function(e){
  if(e.key!=='Enter')return;
  const v=sideSearch.value.trim();if(!v)return;
  const qEl=document.getElementById('q');if(qEl)qEl.value=v;
  setMode('list');if(window.searchRun)window.searchRun();
});

// ---- タグ explorer + 関連パネル: /api/v1/graph を 1 度だけ取得 ----
let GNODES=[],GEDGES=[];
let adjByTitle=new Map();   // title -> [{title,url,reason,industry,project,...}]
let activeTag=null;          // {fam,value}
const FAMS=[['industry','業界'],['doc_type','資料タイプ'],
  ['client_name','取引先'],['source_type','出典']];
function countTags(){
  const fac={industry:[],doc_type:[],client:[],source_type:[]};
  const maps={industry:new Map(),doc_type:new Map(),
    client_name:new Map(),source_type:new Map()};
  for(const n of GNODES){
    for(const k of ['industry','doc_type','client_name','source_type']){
      const v=n[k];if(!v)continue;
      maps[k].set(v,(maps[k].get(v)||0)+1);
    }
  }
  function rows(m){
    return Array.from(m.entries()).map(function(e){
      return {value:e[0],count:e[1]};
    }).sort(function(a,b){return b.count-a.count;});
  }
  fac.industry=rows(maps.industry);fac.doc_type=rows(maps.doc_type);
  fac.client=rows(maps.client_name);fac.source_type=rows(maps.source_type);
  // §4.2 の did-you-mean が読む（検索エンジン側で参照）。
  window.__facets=fac;
  return maps;
}
function tagGroupCollapsed(fam){
  // 既定: 業界/資料タイプ は開く、その他は畳む。
  return !(fam==='industry'||fam==='doc_type');
}
function clearActiveTag(){activeTag=null;buildTagExplorer();
  if(window.graphSetFilter)window.graphSetFilter('');}
function applyTag(fam,value){
  activeTag={fam:fam,value:value};
  if(currentMode==='graph'){
    if(window.graphSetFilter)window.graphSetFilter(value);
  }else{
    if(fam==='industry'){if(window.searchSetIndustry)window.searchSetIndustry(value);}
    else{if(window.searchSetQuery)window.searchSetQuery(value);}
  }
  buildTagExplorer();
}
function buildTagExplorer(){
  tagexp.textContent='';
  const maps=countTags();
  if(activeTag){
    const a=document.createElement('div');a.className='tactive';
    const pill=document.createElement('span');pill.className='pill';
    const lab=document.createElement('span');
    lab.textContent=activeTag.value;pill.appendChild(lab);
    const x=document.createElement('button');x.type='button';x.textContent='×';
    x.setAttribute('aria-label','フィルタを外す');
    x.addEventListener('click',clearActiveTag);pill.appendChild(x);
    a.appendChild(pill);tagexp.appendChild(a);
  }
  for(const [key,label] of FAMS){
    const grp=document.createElement('div');grp.className='tgrp';
    const collapsed=tagGroupCollapsed(key);
    if(collapsed)grp.classList.add('collapsed');
    const h=document.createElement('h4');
    const ht=document.createElement('span');ht.textContent=label;
    const cv=document.createElement('span');cv.className='cv';
    cv.textContent=collapsed?'▸':'▾';
    h.appendChild(ht);h.appendChild(cv);
    h.addEventListener('click',function(){
      grp.classList.toggle('collapsed');
      cv.textContent=grp.classList.contains('collapsed')?'▸':'▾';
    });
    grp.appendChild(h);
    const body=document.createElement('div');body.className='tbody';
    const rows=Array.from(maps[key].entries()).map(function(e){
      return {value:e[0],count:e[1]};
    }).sort(function(a,b){return b.count-a.count;});
    if(!rows.length){
      const e=document.createElement('div');e.className='tempty';
      e.textContent='タグ未付与（再取込待ち）';body.appendChild(e);
    }
    const famKind=key==='industry'?'industry':
      (key==='doc_type'?'doc_type':(key==='client_name'?'client':'source_type'));
    for(const row of rows.slice(0,40)){
      const r=document.createElement('div');r.className='trow';
      if(activeTag&&activeTag.fam===famKind&&activeTag.value===row.value){
        r.classList.add('on');
      }
      const v=document.createElement('span');v.className='tv';
      v.textContent=row.value;r.appendChild(v);
      const c=document.createElement('span');c.className='tc';
      c.textContent=String(row.count);r.appendChild(c);
      r.addEventListener('click',function(){applyTag(famKind,row.value);});
      body.appendChild(r);
    }
    grp.appendChild(body);tagexp.appendChild(grp);
  }
}
function buildAdjacency(){
  adjByTitle=new Map();
  const byId=new Map();
  for(const n of GNODES)byId.set(n.id,n);
  for(const e of GEDGES){
    const s=byId.get(e.source),t=byId.get(e.target);
    if(!s||!t)continue;
    const kind=(e.reason||'').split(':')[0];
    const val=(e.reason||'').split(':').slice(1).join(':');
    if(!adjByTitle.has(s.title))adjByTitle.set(s.title,[]);
    if(!adjByTitle.has(t.title))adjByTitle.set(t.title,[]);
    adjByTitle.get(s.title).push({node:t,kind:kind,val:val});
    adjByTitle.get(t.title).push({node:s,kind:kind,val:val});
  }
}
async function loadGraphData(){
  let data;
  try{
    const resp=await fetch('/api/v1/graph');
    if(resp.status===401){location.href='/search/login';return;}
    data=await resp.json();
  }catch(e){return;}
  GNODES=data.nodes||[];GEDGES=data.edges||[];
  buildAdjacency();buildTagExplorer();
  // パワー機能（スイッチャー/ホバー）が読む共有キャッシュ。再 fetch しない。
  window.__graphNodes=GNODES;window.__adjByTitle=adjByTitle;
  if(window.__powerReady)window.__powerReady();
}

// ---- 右プレビュー / 関連パネル ----
function openRight(){shell.classList.add('right-open');st.rightOpen=true;persist();}
function closeRight(){shell.classList.remove('right-open');st.rightOpen=false;persist();}
function relSection(parent,headLabel,items){
  if(!items.length)return;
  const sec=document.createElement('div');sec.className='relsec';
  const hb=document.createElement('button');hb.type='button';hb.className='relhdr';
  const hl=document.createElement('span');
  hl.textContent=headLabel;hb.appendChild(hl);
  const cnt=document.createElement('span');cnt.className='relcount';
  cnt.textContent=String(items.length);hb.appendChild(cnt);
  hb.addEventListener('click',function(){sec.classList.toggle('folded');});
  sec.appendChild(hb);
  const body=document.createElement('div');body.className='relbody';
  for(const it of items){
    const row=document.createElement('div');row.className='relrow';
    const t=document.createElement('span');t.className='t';
    t.textContent=it.node.title||'(無題)';
    t.addEventListener('click',function(){openPreviewFromNode(it.node);});
    row.appendChild(t);
    if(it.val){
      const rv=document.createElement('span');rv.className='rv';
      rv.textContent=it.val;row.appendChild(rv);
    }
    body.appendChild(row);
  }
  sec.appendChild(body);parent.appendChild(sec);
}
function openPreviewFromNode(node){
  openPreview({doc_id:node.url,title:node.title,source_uri:node.url,
    source_type:node.source_type,client_name:node.client_name,
    industry:node.industry,project:node.project,doc_type:node.doc_type});
  if(currentMode==='graph'&&window.graphFocusByTitle){
    window.graphFocusByTitle(node.title);
  }
}
function safeUrlS(u){
  return (typeof u==='string'&&/^(https?|slack|gdrive):/i.test(u))?u:null;
}
function previewTagChip(label,fam,value){
  const s=document.createElement('span');s.className='chip tag';s.textContent=label;
  s.addEventListener('click',function(){applyTag(fam,value);});
  return s;
}
window.openPreview=function(doc){
  rightPanel.textContent='';
  const wrap=document.createElement('div');wrap.className='pv';
  const hd=document.createElement('div');hd.className='pvhd';
  const h=document.createElement('h2');h.textContent=doc.title||'(無題)';hd.appendChild(h);
  const x=document.createElement('button');x.className='pvx';x.type='button';
  x.textContent='×';x.setAttribute('aria-label','閉じる');
  x.addEventListener('click',closeRight);hd.appendChild(x);
  wrap.appendChild(hd);
  const chips=document.createElement('div');chips.className='pvchips';
  if(doc.client_name){const c=document.createElement('span');c.className='chip';
    c.textContent=doc.client_name;chips.appendChild(c);}
  if(doc.source_type){const c=document.createElement('span');c.className='chip';
    c.textContent=doc.source_type;chips.appendChild(c);}
  if(doc.industry)chips.appendChild(
    previewTagChip('# '+doc.industry,'industry',doc.industry));
  if(doc.doc_type)chips.appendChild(
    previewTagChip('# '+doc.doc_type,'doc_type',doc.doc_type));
  if(doc.project)chips.appendChild(
    previewTagChip('# '+doc.project,'client',doc.project));
  if(chips.childNodes.length)wrap.appendChild(chips);
  if(doc.excerpt){const ex=document.createElement('div');ex.className='pvex';
    // 直近クエリの語を span.hl で強調（未検索なら素の textContent と等価・XSS 安全）。
    if(window.searchHighlight)window.searchHighlight(ex,doc.excerpt);
    else ex.textContent=doc.excerpt;
    wrap.appendChild(ex);}
  const su=safeUrlS(doc.source_uri);
  if(su){const ob=document.createElement('button');ob.className='pvopen';ob.type='button';
    ob.textContent='出典を開く ↗';
    ob.addEventListener('click',function(){
      window.open(su,'_blank','noopener,noreferrer');});
    wrap.appendChild(ob);}
  // カルテ導線: 検索カード/グラフノード両方の openPreview をここ1箇所でカバー。
  const kn=doc.client_name||doc.project;
  if(kn){const kb=document.createElement('button');kb.className='pvopen';kb.type='button';
    kb.textContent='カルテを見る →';
    kb.addEventListener('click',function(){
      location.href='/search/client/'+encodeURIComponent(kn);});
    wrap.appendChild(kb);}
  // 関連資料（同じ /api/v1/graph の隣接から導出。新 API 不要）。
  const relhead=document.createElement('div');relhead.className='relhead';
  relhead.textContent='🔗 関連資料';wrap.appendChild(relhead);
  const neighbors=adjByTitle.get(doc.title)||[];
  const groups={project:[],client:[],industry:[]};
  for(const it of neighbors){
    if(groups[it.kind])groups[it.kind].push(it);
  }
  relSection(wrap,'同じ案件',groups.project);
  relSection(wrap,'同じ取引先',groups.client);
  relSection(wrap,'同じ業界',groups.industry);
  if(!neighbors.length){
    const none=document.createElement('div');none.className='relnone';
    none.textContent='関連資料は見つかりませんでした。';wrap.appendChild(none);
  }
  rightPanel.appendChild(wrap);openRight();
};
// グラフのフォーカス時に右パネルを開く（graph engine が呼ぶ任意フック）。
window.onGraphFocus=function(doc){window.openPreview(doc);};
window.onGraphClear=function(){closeRight();};
// 注: Esc 処理は _POWER_JS 側に集約（モーダル優先で閉じてから closeRight）。

// パワー機能（パレット/キーボード）が叩くシェル制御 API。
window.__shellApi={
  setMode:function(m){setMode(m);},
  getMode:function(){return currentMode;},
  toggleLeft:function(){st.leftOpen=!st.leftOpen;applyLeft();persist();
    return st.leftOpen;},
  toggleRight:function(){if(st.rightOpen)closeRight();else openRight();
    return st.rightOpen;},
  closeRight:function(){closeRight();},
  isRightOpen:function(){return !!st.rightOpen;},
  focusSearch:function(){if(sideSearch){sideSearch.focus();sideSearch.select();}},
  runTitleSearch:function(title){
    const qEl=document.getElementById('q');if(qEl)qEl.value=title;
    setMode('list');if(window.searchRun)window.searchRun();}
};

applyLeft();setMode(currentMode);
// 永続化された右パネル状態を復元（中身は空でも開いた骨格を出す）。
if(st.rightOpen)shell.classList.add('right-open');
loadGraphData();
"""


# Obsidian パリティ・フェーズ2: 上級機能（パワー機能）。既存シェル/エンジンの
# グローバル（window.__shellApi / window.__graphApi / window.__graphNodes /
# window.__adjByTitle / window.searchRun 等）の「上に」乗るだけで、エンジン本体の
# ロジックは一切書き換えない。共有グローバルスコープでの const/let 衝突を避けるため
# 全体を IIFE で包む（変数は内部に閉じ込める）。文字列差し込みは textContent のみ。
_POWER_JS = r"""
(function(){
'use strict';
// ---- ユーティリティ ----
function isTyping(){
  var el=document.activeElement;if(!el)return false;
  var tag=el.tagName;
  return tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT'||el.isContentEditable;
}
function isMac(){return /Mac|iPhone|iPad/.test(navigator.platform||'');}
function nodes(){return window.__graphNodes||[];}
function adjMap(){return window.__adjByTitle||new Map();}
function degreeOf(title){
  var a=adjMap();var list=a.get?a.get(title):null;return list?list.length:0;
}
// 部分列（subsequence）スコア: 連続一致・先頭一致を加点する簡易 fuzzy。
function fuzzyScore(text,q){
  if(!q)return 1;
  var t=text.toLowerCase(),s=q.toLowerCase();
  var idx=t.indexOf(s);
  if(idx>=0)return 1000-idx;  // 部分一致は最優先（位置が早いほど高得点）
  var ti=0,score=0,run=0,matched=0;
  for(var qi=0;qi<s.length;qi++){
    var found=-1;
    for(var j=ti;j<t.length;j++){if(t[j]===s[qi]){found=j;break;}}
    if(found<0)return -1;
    matched++;run=(found===ti)?run+1:0;score+=run*2+1;ti=found+1;
  }
  return matched===s.length?score:-1;
}

// ---- 汎用パレット/モーダル（スイッチャー・コマンド・チート共通）----
var overlay=null;     // 現在開いているモーダル要素
var palInput=null,palList=null,palItems=[],palSel=0,palPick=null,palMeta=null;
function closeModal(){
  if(!overlay)return;
  var o=overlay;overlay=null;palInput=null;palList=null;palPick=null;palMeta=null;
  o.classList.remove('show');
  setTimeout(function(){if(o&&o.parentNode)o.parentNode.removeChild(o);},150);
}
function renderPalRows(q){
  if(!palList)return;
  palList.textContent='';
  var scored=[];
  for(var i=0;i<palItems.length;i++){
    var it=palItems[i];
    var sc=fuzzyScore(it.label+' '+(it.sub||''),q);
    if(sc>=0)scored.push({it:it,sc:sc});
  }
  scored.sort(function(a,b){return b.sc-a.sc;});
  var top=scored.slice(0,20);
  if(palSel>=top.length)palSel=Math.max(0,top.length-1);
  if(!top.length){
    var e=document.createElement('div');e.className='pempty';
    e.textContent='一致なし';palList.appendChild(e);return;
  }
  for(var k=0;k<top.length;k++){
    (function(it,idx){
      var row=document.createElement('div');
      row.className='prow'+(idx===palSel?' sel':'');
      var t=document.createElement('span');t.className='pt';
      t.textContent=it.label;row.appendChild(t);
      if(it.sub){
        var s=document.createElement('span');s.className='ps';
        s.textContent=it.sub;row.appendChild(s);
      }
      row.addEventListener('mouseenter',function(){palSel=idx;markSel();});
      row.addEventListener('click',function(){pick(it,false);});
      palList.appendChild(row);
    })(top[k].it,k);
  }
}
function markSel(){
  if(!palList)return;
  var rows=palList.querySelectorAll('.prow');
  for(var i=0;i<rows.length;i++){
    if(i===palSel){rows[i].classList.add('sel');
      rows[i].scrollIntoView({block:'nearest'});}
    else rows[i].classList.remove('sel');
  }
}
function visibleItems(){
  // 現在の絞り込み結果（描画と同じスコアリング）を返す。
  var q=palInput?palInput.value:'';
  var scored=[];
  for(var i=0;i<palItems.length;i++){
    var it=palItems[i];var sc=fuzzyScore(it.label+' '+(it.sub||''),q);
    if(sc>=0)scored.push({it:it,sc:sc});
  }
  scored.sort(function(a,b){return b.sc-a.sc;});
  return scored.slice(0,20).map(function(x){return x.it;});
}
function pick(it,alt){if(palPick)palPick(it,alt);}
function openPalette(items,onPick,opts){
  closeModal();
  opts=opts||{};
  palItems=items;palPick=onPick;palSel=0;
  overlay=document.createElement('div');overlay.className='pmodal';
  var box=document.createElement('div');box.className='pbox';
  palInput=document.createElement('input');palInput.type='text';
  palInput.setAttribute('placeholder',opts.placeholder||'検索…');
  box.appendChild(palInput);
  palList=document.createElement('div');palList.className='plist';
  box.appendChild(palList);
  if(opts.hint){
    var h=document.createElement('div');h.className='phint';
    for(var i=0;i<opts.hint.length;i++){
      var seg=document.createElement('span');
      var b=document.createElement('b');b.textContent=opts.hint[i][0];
      seg.appendChild(b);seg.appendChild(document.createTextNode(' '+opts.hint[i][1]));
      h.appendChild(seg);
    }
    box.appendChild(h);
  }
  overlay.appendChild(box);
  overlay.addEventListener('mousedown',function(e){if(e.target===overlay)closeModal();});
  document.body.appendChild(overlay);
  requestAnimationFrame(function(){if(overlay)overlay.classList.add('show');});
  renderPalRows('');
  palInput.addEventListener('input',function(){palSel=0;renderPalRows(palInput.value);});
  palInput.addEventListener('keydown',function(e){
    if(e.key==='ArrowDown'){e.preventDefault();palSel++;
      var n=visibleItems().length;if(palSel>=n)palSel=n-1;if(palSel<0)palSel=0;markSel();}
    else if(e.key==='ArrowUp'){e.preventDefault();palSel--;if(palSel<0)palSel=0;markSel();}
    else if(e.key==='Enter'){e.preventDefault();
      var vis=visibleItems();if(vis[palSel])pick(vis[palSel],e.metaKey||e.ctrlKey);}
  });
  palInput.focus();
}

// ---- P2-1 クイックスイッチャー ----
function openSwitcher(){
  var items=nodes().map(function(n){
    var tags=[n.industry,n.doc_type,n.client_name].filter(Boolean).join(' · ');
    return {label:n.title||'(無題)',sub:tags,node:n};
  });
  openPalette(items,function(it,alt){
    var n=it.node;closeModal();
    if(alt){
      var u=n.url;
      if(typeof u==='string'&&/^(https?|slack|gdrive):/i.test(u))
        window.open(u,'_blank','noopener,noreferrer');
      return;
    }
    var mode=window.__shellApi?window.__shellApi.getMode():'list';
    if(mode==='graph'&&window.graphFocusByTitle){window.graphFocusByTitle(n.title);}
    else if(window.__shellApi){window.__shellApi.runTitleSearch(n.title);}
  },{placeholder:'資料タイトルへジャンプ…',
     hint:[['↑↓','選択'],['↵','開く'],[(isMac()?'⌘':'Ctrl')+'↵','出典']]});
}

// ---- P2-2 コマンドパレット ----
function gApi(){return window.__graphApi;}
function sApi(){return window.__shellApi;}
function commandList(){
  var cmds=[
    ['グラフ表示',function(){if(sApi())sApi().setMode('graph');}],
    ['ノート表示',function(){if(sApi())sApi().setMode('list');}],
    ['全体表示 (zoom-to-fit)',function(){if(gApi())gApi().zoomToFit();}],
    ['ビューをリセット',function(){if(gApi())gApi().resetView();}],
    ['左サイドバー開閉',function(){if(sApi())sApi().toggleLeft();}],
    ['右パネル開閉',function(){if(sApi())sApi().toggleRight();}],
    ['色分け: 資料タイプ',function(){if(gApi())gApi().setColorBy('doc_type');}],
    ['色分け: 出典',function(){if(gApi())gApi().setColorBy('source_type');}],
    ['色分け: 業界',function(){if(gApi())gApi().setColorBy('industry');}],
    ['色分け: 案件',function(){if(gApi())gApi().setColorBy('project');}],
    ['孤立ノード表示切替',function(){if(gApi())gApi().toggleOrphans();}],
    ['シミュレーション停止・再開',function(){if(gApi())gApi().toggleAnimate();}],
    ['ショートカット一覧',function(){openCheat();}]
  ];
  return cmds.map(function(c){return {label:c[0],run:c[1]};});
}
function openCommands(){
  openPalette(commandList(),function(it){
    closeModal();
    // グラフ系コマンドは graph 未起動だと __graphApi 不在。先にグラフへ切替えて遅延実行。
    var needsGraph=/全体表示|リセット|色分け|孤立|シミュ/.test(it.label);
    if(needsGraph&&!gApi()&&sApi()){
      sApi().setMode('graph');
      var tries=0;var iv=setInterval(function(){
        tries++;if(gApi()){clearInterval(iv);it.run();}
        else if(tries>40)clearInterval(iv);
      },50);
      return;
    }
    it.run();
  },{placeholder:'コマンドを実行…',hint:[['↑↓','選択'],['↵','実行']]});
}

// ---- P2 ショートカット一覧オーバーレイ ----
function openCheat(){
  closeModal();
  var mod=isMac()?'⌘':'Ctrl';
  overlay=document.createElement('div');overlay.className='pmodal';
  var box=document.createElement('div');box.className='pbox pcheat';
  var hd=document.createElement('div');hd.className='chd';
  hd.textContent='キーボードショートカット';box.appendChild(hd);
  var grid=document.createElement('div');grid.className='cgrid';
  var rows=[
    ['g','グラフ表示'],['l','ノート表示'],
    ['f','全体表示'],['r','ビューをリセット'],
    ['/','検索にフォーカス'],['Esc','閉じる/解除'],
    [mod+'+O','クイックスイッチャー'],[mod+'+P','コマンドパレット'],
    ['o','クイックスイッチャー'],['?','このヘルプ']
  ];
  for(var i=0;i<rows.length;i++){
    var row=document.createElement('div');row.className='crow';
    var lab=document.createElement('span');lab.textContent=rows[i][1];
    var kb=document.createElement('kbd');kb.textContent=rows[i][0];
    row.appendChild(lab);row.appendChild(kb);grid.appendChild(row);
  }
  box.appendChild(grid);
  var hint=document.createElement('div');hint.className='phint';
  var hb=document.createElement('b');hb.textContent='Esc';
  hint.appendChild(hb);hint.appendChild(document.createTextNode(' 閉じる'));
  box.appendChild(hint);
  overlay.appendChild(box);
  overlay.addEventListener('mousedown',function(e){if(e.target===overlay)closeModal();});
  document.body.appendChild(overlay);
  requestAnimationFrame(function(){if(overlay)overlay.classList.add('show');});
}

// ---- P2-3 ホバープレビュー（リストカード）----
var pop=null,popTimer=0,popX=0,popY=0;
function ensurePop(){
  if(pop)return pop;
  pop=document.createElement('div');pop.className='phover';pop.hidden=true;
  document.body.appendChild(pop);return pop;
}
function fillPop(d){
  var p=ensurePop();p.textContent='';
  var t=document.createElement('div');t.className='ht';
  t.textContent=d.title||'(無題)';p.appendChild(t);
  var pills=document.createElement('div');pills.className='hpills';
  var tg=[d.industry,d.doc_type,d.client_name,d.source_type].filter(Boolean);
  for(var i=0;i<tg.length;i++){
    var s=document.createElement('span');s.className='hp';s.textContent=tg[i];
    pills.appendChild(s);
  }
  if(pills.childNodes.length)p.appendChild(pills);
  if(d.excerpt){var ex=document.createElement('div');ex.className='hx';
    // 直近クエリの語を span.hl で強調（未検索なら素の textContent と等価・XSS 安全）。
    if(window.searchHighlight)window.searchHighlight(ex,d.excerpt.slice(0,200));
    else ex.textContent=d.excerpt.slice(0,200);
    p.appendChild(ex);}
  var deg=document.createElement('div');deg.className='hd';
  deg.textContent='接続 '+degreeOf(d.title)+'件';p.appendChild(deg);
}
function showPop(){
  var p=ensurePop();p.hidden=false;
  var vw=window.innerWidth,vh=window.innerHeight;
  var pr=p.getBoundingClientRect();
  var x=popX+16,y=popY+16;
  if(x+pr.width>vw-8)x=popX-pr.width-16;
  if(y+pr.height>vh-8)y=vh-pr.height-8;
  if(x<8)x=8;if(y<8)y=8;
  p.style.left=x+'px';p.style.top=y+'px';
  requestAnimationFrame(function(){if(p)p.classList.add('show');});
}
function hidePop(){
  if(popTimer){clearTimeout(popTimer);popTimer=0;}
  if(pop){pop.classList.remove('show');pop.hidden=true;}
}
// 検索結果カードに hover-intent（~250ms）でポップオーバー。データは __graphNodes から。
function nodeByTitle(t){
  var ns=nodes();for(var i=0;i<ns.length;i++){if(ns[i].title===t)return ns[i];}
  return null;
}
var resultsEl=document.getElementById('results');
if(resultsEl){
  resultsEl.addEventListener('mouseover',function(e){
    var card=e.target.closest?e.target.closest('.card'):null;if(!card)return;
    var tEl=card.querySelector('.title');if(!tEl)return;
    var title=tEl.textContent;var nd=nodeByTitle(title);
    var exEl=card.querySelector('.excerpt');
    var d={title:title,excerpt:(nd&&nd.excerpt)||(exEl?exEl.textContent:'')};
    if(nd){d.industry=nd.industry;d.doc_type=nd.doc_type;
      d.client_name=nd.client_name;d.source_type=nd.source_type;}
    if(popTimer)clearTimeout(popTimer);
    popTimer=setTimeout(function(){fillPop(d);showPop();},250);
  });
  resultsEl.addEventListener('mousemove',function(e){popX=e.clientX;popY=e.clientY;});
  resultsEl.addEventListener('mouseout',function(e){
    var to=e.relatedTarget;
    if(to&&to.closest&&to.closest('.card'))return;
    hidePop();
  });
  resultsEl.addEventListener('scroll',hidePop,true);
}
window.addEventListener('scroll',hidePop,true);

// ---- P2-5 設定の永続化（グラフ設定）----
var GLS='ta_graph_v1';
function saveGraphSettings(){
  if(!gApi()||!gApi().getSettings)return;
  try{localStorage.setItem(GLS,JSON.stringify(gApi().getSettings()));}catch(e){}
}
function loadGraphSettings(){
  try{var raw=localStorage.getItem(GLS);return raw?JSON.parse(raw):null;}
  catch(e){return null;}
}
// グラフは遅延起動。startGraph を一度だけラップし、起動・描画が乗った後に復元する。
(function(){
  var orig=window.startGraph;
  if(typeof orig!=='function')return;
  window.startGraph=function(){
    orig();
    var saved=loadGraphSettings();
    if(saved){
      var tries=0;var iv=setInterval(function(){
        tries++;
        if(gApi()&&gApi().applySettings&&(window.__graphNodes||tries>4)){
          clearInterval(iv);try{gApi().applySettings(saved);}catch(e){}
        }else if(tries>40)clearInterval(iv);
      },60);
    }
  };
})();
// 離脱時とコマンド/操作後にスナップショット保存（過剰書込みを避け beforeunload で確定）。
window.addEventListener('beforeunload',saveGraphSettings);
// パレット経由のグラフ変更後にも保存（即時反映の保険）。
var _saveSoon=null;
function scheduleSave(){if(_saveSoon)clearTimeout(_saveSoon);
  _saveSoon=setTimeout(saveGraphSettings,400);}

// ---- P2-6 検索オペレータ構文 ----
// industry の既知値（facets から動的に判定）。tag: が業界と一致すれば industry に流す。
function knownIndustries(){
  var fac=window.__facets;var set={};
  if(fac&&fac.industry){for(var i=0;i<fac.industry.length;i++)
    set[fac.industry[i].value]=true;}
  return set;
}
// "tag:食品 source:gdrive 保存率" → {industry, query}
function parseOperators(raw){
  var inds=knownIndustries();
  var industry=null;var terms=[];
  var re=/(\w+):("[^"]+"|\S+)/g;var m;var consumed=[];
  while((m=re.exec(raw))!==null){
    var key=m[1].toLowerCase();var val=m[2].replace(/^"|"$/g,'');
    var known=['tag','source','client','type','industry'].indexOf(key)>=0;
    if(!known)continue;  // 未知オペレータはそのまま本文に残す（forgiving）
    consumed.push([m.index,m.index+m[0].length]);
    if(key==='industry'){industry=val;}
    else if(key==='tag'&&inds[val]){industry=val;}
    else{terms.push(val);}  // source/client/type/その他 tag は強い検索語として畳む
  }
  // consumed 区間を除いた残りを本文として連結。
  var rest='';var pos=0;
  consumed.sort(function(a,b){return a[0]-b[0];});
  for(var c=0;c<consumed.length;c++){
    rest+=raw.slice(pos,consumed[c][0]);pos=consumed[c][1];
  }
  rest+=raw.slice(pos);
  var query=(rest.trim()+' '+terms.join(' ')).trim();
  return {industry:industry,query:query||rest.trim()};
}
// 既存検索エンジン（window.searchRun 等）を壊さず、入力直前にオペレータを解釈する。
function runWithOperators(raw){
  var p=parseOperators(raw);
  var qEl=document.getElementById('q');
  if(qEl)qEl.value=p.query;
  if(sApi())sApi().setMode('list');
  if(p.industry&&window.searchSetIndustry){window.searchSetIndustry(p.industry);}
  else if(window.searchRun){window.searchRun();}
}
// サイドバー検索ボックスの Enter をオペレータ対応に差し替える（capture で先取り）。
var sideSearchEl=document.getElementById('sideSearch');
if(sideSearchEl){
  sideSearchEl.placeholder='検索  tag: source: client: type: industry:';
  sideSearchEl.addEventListener('keydown',function(e){
    if(e.key!=='Enter')return;
    var v=sideSearchEl.value.trim();if(!v)return;
    if(!/\w+:\S/.test(v))return;  // オペレータ無しは既存ハンドラに任せる
    e.preventDefault();e.stopPropagation();runWithOperators(v);
  },true);
}
// 本体検索ボックス側もオペレータ対応（go ボタン/Enter）。
var qBox=document.getElementById('q');var goBtn=document.getElementById('go');
function maybeOps(){
  var v=qBox?qBox.value.trim():'';
  if(v&&/\w+:\S/.test(v)){runWithOperators(v);return true;}
  return false;
}
if(qBox){
  qBox.placeholder='例: 保存率訴求  /  tag:食品 source:gdrive 保存率';
  qBox.addEventListener('keydown',function(e){
    if(e.key==='Enter'&&maybeOps()){e.preventDefault();e.stopPropagation();}
  },true);
}
if(goBtn){goBtn.addEventListener('click',function(e){
  if(maybeOps()){e.preventDefault();e.stopPropagation();}
},true);}

// ---- P2-4 キーボードナビゲーション（グローバル）----
document.addEventListener('keydown',function(e){
  var k=e.key;
  var cmd=e.metaKey||e.ctrlKey;
  // モーダル表示中: Esc は閉じる、他はモーダル内入力に任せる。
  if(overlay){
    if(k==='Escape'){e.preventDefault();closeModal();}
    return;
  }
  // Cmd/Ctrl ショートカット（入力中でも有効）。
  if(cmd&&(k==='o'||k==='O')){e.preventDefault();openSwitcher();return;}
  if(cmd&&(k==='p'||k==='P')){e.preventDefault();openCommands();return;}
  if(isTyping())return;  // 以下の素キーは入力中は無効。
  if(k==='Escape'){
    // モーダルは上で処理済み。次にグラフフォーカス、最後に右パネル。
    if(gApi()&&gApi().hasFocus()){gApi().clearFocus();}
    else if(sApi()&&sApi().isRightOpen()){sApi().closeRight();}
    hidePop();return;
  }
  if(k==='g'){if(sApi())sApi().setMode('graph');return;}
  if(k==='l'){if(sApi())sApi().setMode('list');return;}
  if(k==='f'){if(gApi()){gApi().zoomToFit();scheduleSave();}return;}
  if(k==='r'){if(gApi()){gApi().resetView();scheduleSave();}return;}
  if(k==='/'){e.preventDefault();if(sApi())sApi().focusSearch();return;}
  if(k==='o'){e.preventDefault();openSwitcher();return;}
  if(k==='?'){e.preventDefault();openCheat();return;}
});

// 起動完了フック（loadGraphData 後に呼ばれる）。__facets が埋まったので
// 検索 UI の取引先 datalist を遅延充填する（未定義時は free-text フォールバック）。
window.__powerReady=function(){
  if(typeof window.populateClientList==='function'){window.populateClientList();}
};
})();
"""


def _shell_page(email: str, *, mode: str) -> str:
    """Obsidian 風シェル。/search は list・/search/graph は graph で起動する。

    両エンジン（検索/グラフ）の DOM を #mainList / #mainGraph に mount し、
    _SHELL_JS が setMode で切替える。グラフ sim は初回グラフ表示時のみ起動。
    """
    boot = "graph" if mode == "graph" else "list"
    list_active = "" if boot == "graph" else " active"
    graph_active = " active" if boot == "graph" else ""
    list_hidden = " hidden" if boot == "graph" else ""
    graph_hidden = "" if boot == "graph" else " hidden"
    search_js = _render_search_js(answer_rating_enabled=_envflag("CONNECT_ANSWER_RATING"))
    # リボン用インライン SVG グリフ（CDN 不要＝社内プロキシ下でも動く）。
    icon_list = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<line x1="8" y1="6" x2="21" y2="6"></line>'
        '<line x1="8" y1="12" x2="21" y2="12"></line>'
        '<line x1="8" y1="18" x2="21" y2="18"></line>'
        '<line x1="3" y1="6" x2="3.01" y2="6"></line>'
        '<line x1="3" y1="12" x2="3.01" y2="12"></line>'
        '<line x1="3" y1="18" x2="3.01" y2="18"></line></svg>'
    )
    icon_graph = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="5" cy="6" r="2.4"></circle>'
        '<circle cx="18" cy="5" r="2.4"></circle>'
        '<circle cx="12" cy="18" r="2.4"></circle>'
        '<line x1="7" y1="7" x2="10.5" y2="16"></line>'
        '<line x1="16" y1="6.5" x2="13.5" y2="16.2"></line></svg>'
    )
    icon_chev = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<polyline points="15 18 9 12 15 6"></polyline></svg>'
    )
    style = _ROOT_TOKENS + _SEARCH_STYLE + _GRAPH_STYLE + _SHELL_STYLE + _POWER_STYLE
    head = (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>社内ナレッジ検索</title><style>" + style + "</style></head><body>"
    )
    ribbon = (
        '<nav class="ribbon">'
        '<button id="leftToggle" class="rib" title="サイドバー開閉" '
        'aria-label="サイドバー開閉">' + icon_chev + "</button>"
        '<button id="ribList" class="rib' + list_active + '" title="ノート" '
        'aria-label="ノート">' + icon_list + "</button>"
        '<button id="ribGraph" class="rib' + graph_active + '" title="グラフ" '
        'aria-label="グラフ">' + icon_graph + "</button>"
        '<span class="ribspace"></span>'
        '<span id="ribMail" class="ribmail"></span></nav>'
    )
    sidebar = (
        '<aside class="side">'
        '<div class="side-search">'
        '<input id="sideSearch" type="search" '
        'placeholder="社内ナレッジを検索…"></div>'
        '<div id="tagexp" class="tagexp"></div></aside>'
    )
    work = (
        '<section class="work">'
        '<div class="ws-tabs">'
        '<button id="tabList" class="ws-tab' + list_active + '">ノート</button>'
        '<button id="tabGraph" class="ws-tab' + graph_active + '">グラフ</button>'
        "</div>"
        '<div class="ws-body">'
        '<div id="mainList"' + list_hidden + ">" + _SEARCH_DOM + "</div>"
        '<div id="mainGraph"' + graph_hidden + ">" + _GRAPH_DOM + "</div>"
        "</div></section>"
    )
    right = '<aside id="rightPanel" class="right"></aside>'
    scripts = (
        # __shellMode を先に立て、_GRAPH_JS の自動起動を抑止（シェルから遅延起動する）。
        "<script>window.__shellMode=true;</script>"
        "<script>" + search_js + "</script>"
        "<script>" + _GRAPH_JS + "</script>"
        "<script>" + _SHELL_JS + "</script>"
        # 上級機能（パワー機能）はシェル/エンジンの後に乗せる（IIFE で隔離）。
        "<script>" + _POWER_JS + "</script>"
        # email は textContent で安全に差し込む（XSS 防御）。
        "<script>document.getElementById('ribMail').textContent=" + _js_str(email) + ";</script>"
    )
    body = (
        '<div id="shell" class="shell">'
        + ribbon
        + sidebar
        + work
        + right
        + "</div>"
        + scripts
        + "</body></html>"
    )
    return head + body


# ============================================================
# クライアントカルテページ（GET /search/client/{client}）。
# Karpathy 式 Second Brain の「wiki 層」: 1 クライアントの最新状況ヘッダ +
# FB 時系列（日付降順）+ 関連資料一覧を 1 画面に集約する。AI 要約はスコープ外。
# スタイルは _ROOT_TOKENS + _SEARCH_STYLE を再利用（.card/.chip/.meta/.excerpt）。
# ============================================================
_KARTE_STYLE = (
    ".ksec{font-size:14px;color:var(--text);font-weight:600;margin:24px 0 10px}"
    ".kdate{color:var(--muted);font-size:12px;margin-right:8px;"
    "font-variant-numeric:tabular-nums}"
    ".kv{font-size:12.5px;color:#c5d0e6;margin:3px 0;line-height:1.6}"
    ".kvl{color:var(--muted);margin-right:6px}"
)


# カルテページの最小フロント（依存ゼロ・textContent/createElement のみ＝XSS 安全）。
# クライアント名は window.__karteClient（_js_str で安全に注入）から読む。
_KARTE_JS = r"""
const CLIENT=window.__karteClient||'';
const khead=document.getElementById('khead');
const ktl=document.getElementById('ktimeline');
const kdocs=document.getElementById('kdocs');
const ksecFb=document.getElementById('ksecFb');
const ksecDocs=document.getElementById('ksecDocs');
function kSafeUrl(u){return (typeof u==='string'&&/^(https?|slack|gdrive):/i.test(u))?u:null;}
function kChip(text){
  const s=document.createElement('span');s.className='chip';s.textContent=text;return s;
}
function kv(parent,label,value){
  if(!value)return;
  const p=document.createElement('div');p.className='kv';
  const l=document.createElement('span');l.className='kvl';l.textContent=label;p.appendChild(l);
  const v=document.createElement('span');v.textContent=value;p.appendChild(v);
  parent.appendChild(p);
}
function kEmptyCard(msg){
  const c=document.createElement('div');c.className='emptyx';
  const i=document.createElement('div');i.className='ei';i.textContent='🗂';c.appendChild(i);
  const t=document.createElement('div');t.className='et';t.textContent=msg;c.appendChild(t);
  return c;
}
function renderHeader(h){
  khead.textContent='';
  const card=document.createElement('div');card.className='card';
  const chips=document.createElement('div');chips.className='chips';
  if(h.industry)chips.appendChild(kChip('業界 '+h.industry));
  if(h.deal_phase)chips.appendChild(kChip('フェーズ '+h.deal_phase));
  if(h.bant_score)chips.appendChild(kChip('BANT '+h.bant_score));
  if(h.last_contact)chips.appendChild(kChip('最終接触 '+h.last_contact));
  chips.appendChild(kChip('FB '+(h.fb_count||0)+'件'));
  chips.appendChild(kChip('資料 '+(h.doc_count||0)+'件'));
  card.appendChild(chips);
  khead.appendChild(card);
}
function renderTimeline(items){
  ktl.textContent='';
  if(!items.length){
    const e=document.createElement('div');e.className='empty';
    e.textContent='FB の記録はまだありません。';ktl.appendChild(e);return;
  }
  for(const it of items){
    const card=document.createElement('div');card.className='card';
    const t=document.createElement('div');t.className='title';
    const d=document.createElement('span');d.className='kdate';
    d.textContent=it.occurred_at||'----';t.appendChild(d);
    t.appendChild(document.createTextNode(it.title||'(無題)'));
    card.appendChild(t);
    const chips=document.createElement('div');chips.className='chips';
    if(it.deal_phase)chips.appendChild(kChip(it.deal_phase));
    if(it.bant_score)chips.appendChild(kChip('BANT '+it.bant_score));
    if(it.channel_type)chips.appendChild(kChip(it.channel_type));
    if(chips.childNodes.length)card.appendChild(chips);
    if(it.content){
      const ex=document.createElement('div');ex.className='excerpt';
      ex.textContent=it.content;card.appendChild(ex);
    }
    kv(card,'ポジ反応',it.positive_reaction);
    kv(card,'ネガ反応',it.negative_reaction);
    kv(card,'次アクション',it.next_action);
    kv(card,'提案メニュー',it.proposed_menu);
    const su=kSafeUrl(it.source_uri);
    if(su){
      const meta=document.createElement('div');meta.className='meta';
      const a=document.createElement('a');a.href=su;
      a.target='_blank';a.rel='noopener noreferrer';
      a.textContent='出典を開く';meta.appendChild(a);
      card.appendChild(meta);
    }
    ktl.appendChild(card);
  }
}
function renderDocs(items){
  kdocs.textContent='';
  if(!items.length){
    const e=document.createElement('div');e.className='empty';
    e.textContent='関連資料はまだありません。';kdocs.appendChild(e);return;
  }
  for(const it of items){
    const card=document.createElement('div');card.className='card';
    const t=document.createElement('div');t.className='title';
    t.textContent=it.title||'(無題)';card.appendChild(t);
    const chips=document.createElement('div');chips.className='chips';
    if(it.doc_type)chips.appendChild(kChip(it.doc_type));
    if(it.source_type)chips.appendChild(kChip(it.source_type));
    if(it.solution)chips.appendChild(kChip(it.solution));
    if(it.modified_at)chips.appendChild(kChip(it.modified_at));
    if(chips.childNodes.length)card.appendChild(chips);
    if(it.excerpt){
      const ex=document.createElement('div');ex.className='excerpt';
      ex.textContent=it.excerpt;card.appendChild(ex);
    }
    const su=kSafeUrl(it.open_url);
    if(su){
      const meta=document.createElement('div');meta.className='meta';
      const a=document.createElement('a');a.href=su;
      a.target='_blank';a.rel='noopener noreferrer';
      a.textContent='資料を開く';meta.appendChild(a);
      card.appendChild(meta);
    }
    kdocs.appendChild(card);
  }
}
async function loadKarte(){
  let data;
  try{
    const resp=await fetch('/api/v1/client/'+encodeURIComponent(CLIENT));
    if(resp.status===401){location.href='/search/login';return;}
    if(!resp.ok)throw new Error('http '+resp.status);
    data=await resp.json();
  }catch(e){
    khead.textContent='';
    khead.appendChild(kEmptyCard('カルテの取得に失敗しました。少し待って再読み込みしてください。'));
    ktl.textContent='';kdocs.textContent='';
    ksecFb.hidden=true;ksecDocs.hidden=true;
    return;
  }
  const tl=data.timeline||[];
  const docs=data.documents||[];
  if(!tl.length&&!docs.length){
    khead.textContent='';
    khead.appendChild(kEmptyCard('まだ記録がありません'));
    ktl.textContent='';kdocs.textContent='';
    ksecFb.hidden=true;ksecDocs.hidden=true;
    return;
  }
  renderHeader(data.header||{});
  renderTimeline(tl);
  renderDocs(docs);
}
loadKarte();
"""


def _karte_page(client: str) -> str:
    """クライアントカルテの HTML を組む（_shell_page と同じ Python 文字列連結流儀）。

    動的値の差し込みは HTML 側 html.escape / JS 側 _js_str の二流儀のみ（XSS 防御）。
    データは /api/v1/client/{client} から fetch し、DOM は textContent だけで組む。
    """
    client_e = html.escape(client)
    style = _ROOT_TOKENS + _SEARCH_STYLE + _KARTE_STYLE
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>クライアントカルテ: {client_e}</title><style>" + style + "</style></head><body>"
        "<main>"
        '<div class="toplinks"><a href="/search">← 検索に戻る</a>'
        '<a href="/search/graph">グラフ</a></div>'
        f"<h1>📇 クライアントカルテ: {client_e}</h1>"
        '<p class="sub">営業FBの時系列と関連資料を 1 画面に集約します（AI要約は後続）。</p>'
        '<div id="khead"><div class="empty">読み込み中…</div></div>'
        '<div id="ksecFb" class="ksec">📈 営業FB時系列（新しい順）</div>'
        '<div id="ktimeline"></div>'
        '<div id="ksecDocs" class="ksec">📎 関連資料</div>'
        '<div id="kdocs"></div>'
        "</main>"
        "<script>window.__karteClient=" + _js_str(client) + ";</script>"
        "<script>" + _KARTE_JS + "</script>"
        "</body></html>"
    )


def _karte_payload(
    client: str,
    timeline: list[Any],
    documents: list[dict[str, Any]],
) -> dict[str, Any]:
    """カルテ API のレスポンス JSON を組む（純関数・DB 非依存）。

    - timeline: ``PgVectorClient.list_client_timeline_recent`` の SearchHit list
      （**最新 N 件を古い順**。ASC LIMIT の list_client_timeline だと FB 多数クライアントで
      「最古の N 件」になりヘッダ/時系列が誤るため recent 版を使う）。
      表示は日付降順が要件なので ``reversed()`` し、最新状況ヘッダ（直近フェーズ/BANT/
      最終接触）は末尾＝最新要素から取る。
    - documents: ``PgVectorClient.list_documents_for_client`` の行 dict list
      （modified_at 降順）。timeline は cls_industry を返さないため、ヘッダの業界は
      資料側 cls_industry の最頻値で補完する。
    - gdrive:// は api_search の _open_url イディオムどおり Drive view URL へ整形
      （抽出失敗時は元 source_uri に fail-open）。
    """
    from teamagent.skills.knowledge_deliver.skill import extract_drive_file_id

    latest = timeline[-1] if timeline else None
    industries = [str(d["cls_industry"]) for d in documents if d.get("cls_industry")]
    industry = Counter(industries).most_common(1)[0][0] if industries else None

    header: dict[str, Any] = {
        "client": client,
        "industry": industry,
        "deal_phase": latest.metadata.get("deal_phase") if latest else None,
        "bant_score": latest.metadata.get("bant_score") if latest else None,
        "last_contact": latest.metadata.get("occurred_at") if latest else None,
        "fb_count": len(timeline),
        "doc_count": len(documents),
    }

    timeline_out: list[dict[str, Any]] = []
    for h in reversed(timeline):
        m = h.metadata
        timeline_out.append(
            {
                "occurred_at": m.get("occurred_at"),
                "title": m.get("title"),
                "content": (h.content or "")[:300],
                "deal_phase": m.get("deal_phase"),
                "bant_score": m.get("bant_score"),
                "channel_type": m.get("channel_type"),
                "positive_reaction": m.get("positive_reaction"),
                "negative_reaction": m.get("negative_reaction"),
                "next_action": m.get("next_action"),
                "proposed_menu": m.get("proposed_menu"),
                "source_uri": m.get("source_uri"),
            }
        )

    docs_out: list[dict[str, Any]] = []
    for d in documents:
        open_url: str | None = d.get("source_uri")
        if d.get("source_type") == "gdrive":
            fid = extract_drive_file_id(d.get("source_uri"))
            if fid:
                open_url = f"https://drive.google.com/file/d/{fid}/view"
        docs_out.append(
            {
                "title": d.get("title"),
                "doc_type": d.get("cls_doc_type"),
                "source_type": d.get("source_type"),
                "modified_at": d.get("modified_at"),
                "open_url": open_url,
                "excerpt": d.get("excerpt"),
                "solution": d.get("cls_solution"),
                "industry": d.get("cls_industry"),
                "project": d.get("cls_project"),
            }
        )

    return {"client": client, "header": header, "timeline": timeline_out, "documents": docs_out}


def create_app(
    *,
    redirect_uri: str | None = None,
    kms_key_id: str | None = None,
    app_role: str = "teamagent_app",
    exchange_fn: Callable[[str], OAuthToken] | None = None,
    store: Any | None = None,
    slack_redirect_uri: str | None = None,
    slack_exchange_fn: Callable[[str], SlackOAuthToken] | None = None,
    slack_store: Any | None = None,
    search_skill_factory: Callable[[], Any] | None = None,
    search_config: DashboardConfig | None = None,
    search_verifier: Verifier | None = None,
    feedback_store: Any | None = None,
    graph_docs_provider: Callable[[str], list[dict[str, Any]]] | None = None,
    client_karte_provider: Callable[[str, str], dict[str, Any]] | None = None,
) -> FastAPI:
    """連携コールバックアプリを構築する。redirect_uri/kms_key_id は env 既定、注入も可。

    P4 資料検索 Web UI 用の注入口（すべてテスト用・本番は None で遅延生成）:
      - search_skill_factory: SearchSkill を返す factory（embedder が重いのでプロセス内
        lazy-singleton 化する）。本番未指定時は orchestrator.factory._build_search_skill を流用。
      - search_config: 認証設定（未指定時は env から _load_search_config）。
      - search_verifier: Google id_token 検証器（テストでネットワーク排除）。
      - feedback_store: search_feedback への保存器（未指定時は RDS に遅延生成）。
      - client_karte_provider: (email, client) -> {"timeline": SearchHit list（古い順）,
        "documents": dict list} を返す callable（テストで実 DB を排除・graph_docs_provider
        と同列）。本番未指定時は RLS 接続で pgvector から実取得。

    Slack per-user 連携（/slack/oauth/callback・Google 版と対称）:
      - slack_redirect_uri: Slack 認可の redirect_uri（未指定時 env SLACK_OAUTH_REDIRECT_URI）。
      - slack_exchange_fn: code→SlackOAuthToken 交換（テストで注入・実 Slack API を排除）。
      - slack_store: xoxp の保管器（未指定時 KMS+RDS に遅延生成した SlackTokenStore）。
    """
    redirect = redirect_uri or os.environ.get("OAUTH_REDIRECT_URI", "")
    slack_redirect = slack_redirect_uri or os.environ.get("SLACK_OAUTH_REDIRECT_URI", "")
    app = FastAPI(title="TeamAgent Connect", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_SecurityHeadersMiddleware)

    search_cfg = search_config or _load_search_config()
    # SearchSkill は embedder が重いのでプロセス内 lazy-singleton（初回検索時に1度だけ生成）。
    # api_search が run() を to_thread へオフロードするため、初回検索が同時に来ると複数の
    # worker スレッドが同時到達しうる。Lock の double-checked locking で構築を1回に保つ。
    search_state: dict[str, Any] = {"skill": None}
    search_skill_lock = threading.Lock()
    feedback_state: dict[str, Any] = {"store": feedback_store}
    # 検索の同時実行上限（LocalE5 CPU 推論の worker スレッド暴走防止）。env で再ビルド無し較正。
    search_concurrency = max(1, _env_int("SEARCH_CONCURRENCY", 4))
    # asyncio.Semaphore は最初に await したイベントループに bind される。TestClient は
    # リクエストごとに新しいループを作るため、ループ単位で lazy 生成する（本番 uvicorn は
    # 単一ループなので実質プロセスに1個＝全検索リクエストで共有される）。
    search_sema_state: dict[str, Any] = {"sema": None, "loop": None}

    def _get_search_semaphore() -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if search_sema_state["sema"] is None or search_sema_state["loop"] is not loop:
            search_sema_state["sema"] = asyncio.Semaphore(search_concurrency)
            search_sema_state["loop"] = loop
        sema: asyncio.Semaphore = search_sema_state["sema"]
        return sema

    def _exchange(code: str) -> OAuthToken:
        if exchange_fn is not None:
            return exchange_fn(code)
        return OAuthConsentFlow(redirect_uri=redirect).exchange(code)

    def _get_store() -> Any:
        if store is not None:
            return store
        # 遅延 import（テストは store 注入で本番依存を回避）
        from teamagent.adapters.oauth_token_store import KmsCipher, RdsTokenStore
        from teamagent.adapters.pgvector_client import PgVectorClient

        key_id = kms_key_id or os.environ.get("OAUTH_KMS_KEY_ID")
        if not key_id:
            raise RuntimeError("OAUTH_KMS_KEY_ID が未設定です")
        return RdsTokenStore(PgVectorClient.from_env(), KmsCipher(key_id), app_role=app_role)

    def _slack_exchange(code: str) -> SlackOAuthToken:
        if slack_exchange_fn is not None:
            return slack_exchange_fn(code)
        return SlackOAuthConsentFlow(redirect_uri=slack_redirect).exchange(code)

    def _get_slack_store() -> Any:
        if slack_store is not None:
            return slack_store
        # 遅延 import（テストは slack_store 注入で本番依存を回避）。
        from teamagent.adapters.oauth_token_store import KmsCipher
        from teamagent.adapters.pgvector_client import PgVectorClient

        key_id = kms_key_id or os.environ.get("OAUTH_KMS_KEY_ID")
        if not key_id:
            raise RuntimeError("OAUTH_KMS_KEY_ID が未設定です")
        return SlackTokenStore(PgVectorClient.from_env(), KmsCipher(key_id), app_role=app_role)

    def _get_search_skill() -> Any:
        """SearchSkill を lazy-singleton で取得（embedder の二重ロードを避ける）。

        to_thread オフロード後は複数 worker スレッドから並行到達しうるため、
        double-checked locking で重い初回構築（LocalE5 ロード）を1回に保つ。
        """
        skill = search_state["skill"]
        if skill is None:
            with search_skill_lock:
                skill = search_state["skill"]
                if skill is None:
                    if search_skill_factory is not None:
                        skill = search_skill_factory()
                    else:
                        # 本番: orchestrator.factory の構築ロジックを流用（env フラグ一致）。
                        from teamagent.orchestrator.factory import _build_search_skill

                        skill = _build_search_skill()
                    search_state["skill"] = skill
        return skill

    def _save_feedback(row: dict[str, Any]) -> None:
        """search_feedback に 1 行 INSERT する（store 注入時はそれを使う）。"""
        st = feedback_state["store"]
        if st is not None:
            st.save(row)
            return
        # 本番: RDS に遅延生成した psycopg 接続で INSERT（teamagent_app role）。
        from teamagent.adapters.pgvector_client import PgVectorClient

        pg = PgVectorClient.from_env()
        columns = ["user_email", "query", "target_type", "doc_id", "chunk_id", "rating", "note"]
        values = [
            row["user_email"],
            row["query"],
            row["target_type"],
            row.get("doc_id"),
            row.get("chunk_id"),
            row["rating"],
            row.get("note"),
        ]
        for column in ("score", "search_session_id", "answer_id"):
            value = row.get(column)
            if value is not None:
                columns.append(column)
                values.append(value)
        with pg.connection(app_role=app_role, user_email=row["user_email"]) as conn:
            with conn.cursor() as cur:
                if len(columns) > 7:
                    # PostgreSQL は UndefinedColumn 後のtransactionを中断するため、旧列への
                    # 再試行前に savepoint まで戻す。0022適用済みならそのまま release するだけ。
                    cur.execute("SAVEPOINT feedback_save")
                    try:
                        _execute_feedback_insert(
                            cur.execute,
                            columns,
                            values,
                            prepare_legacy_retry=lambda: cur.execute(
                                "ROLLBACK TO SAVEPOINT feedback_save"
                            ),
                        )
                    finally:
                        cur.execute("RELEASE SAVEPOINT feedback_save")
                else:
                    _execute_feedback_insert(cur.execute, columns, values)
            conn.commit()

    def _list_graph_docs(email: str, *, with_embeddings: bool = False) -> list[dict[str, Any]]:
        """グラフ用の資料一覧を取得する（provider 注入時はそれ・本番は RLS 接続で実取得）。

        ``with_embeddings`` が True のときだけ各資料の代表ベクトルも引く（concept edges 用・
        やや重い）。provider 注入経路はベクトルを返さないため concept は出ない（OK）。

        重複排除（suppressed 除外）の整合性: 検索側（skills/search）は ``DOC_DEDUP_EXCLUDE_SEARCH``
        が ON のときだけ suppressed を除外する（既定 OFF・後方互換）。グラフ側も同じ env を読み、
        検索とグラフで「重複資料を隠す/見せる」を連動させる（OFF なら両方見せる）。
        env 読み取りはこの呼び出し側で行い、pgvector へ ``exclude_duplicates`` として渡す。

        ⚠️ pgvector ``list_documents_for_graph`` は現状 ``exclude_duplicates`` 引数を持たず
        suppressed を**無条件**で除外する（pgvector_client.py は本タスクで編集禁止）。そのため
        引数対応の有無を実行時に検査し、対応していれば値を渡す／未対応なら**従来どおり呼ぶ**
        （= 引数を渡さず TypeError を回避）。env 値はログに出して、未対応時に「グラフが env を
        無視している」ことを観測可能にする。pgvector に ``exclude_duplicates: bool=False`` が
        追加され次第、本関数は無改修で連動が効くようになる（ハンドオフ参照）。
        """
        if graph_docs_provider is not None:
            return graph_docs_provider(email)
        import inspect

        from teamagent.adapters.pgvector_client import PgVectorClient

        # 検索側と同じ env で suppressed 除外を連動（既定 OFF・後方互換）。
        exclude_duplicates = _envflag("DOC_DEDUP_EXCLUDE_SEARCH")
        # pgvector が exclude_duplicates 引数を受けられるかを実行時に判定（編集禁止のため）。
        graph_fn = PgVectorClient.list_documents_for_graph
        pg_supports_exclude = "exclude_duplicates" in inspect.signature(graph_fn).parameters

        # pgvector が当該引数に対応していれば値を渡す。未対応なら空 dict＝従来呼び出し
        # （TypeError 回避）。dict 経由の splat にして mypy の静的シグネチャ検査も満たす
        # （現状の pgvector は exclude_duplicates を持たない＝静的には未対応のため）。
        extra: dict[str, Any] = {}
        if pg_supports_exclude:
            extra["exclude_duplicates"] = exclude_duplicates
        else:
            # env と実挙動の乖離（env ON でも pgvector が無条件 suppressed 除外）を観測可能に。
            logger.info(
                "graph_dedup_flag_pending_pgvector_support",
                user_email=email,
                exclude_duplicates=exclude_duplicates,
                note=(
                    "pgvector.list_documents_for_graph に exclude_duplicates 引数が未追加。"
                    "現状は suppressed を無条件除外。引数追加で env と連動可。"
                ),
            )

        domain = email.split("@", 1)[1] if "@" in email else email
        pg = PgVectorClient.from_env()
        with pg.connection(
            app_role=app_role,
            user_email=email,
            user_groups=[domain],
            user_role="user",
        ) as conn:
            return pg.list_documents_for_graph(conn, with_embeddings=with_embeddings, **extra)

    def _client_karte_data(email: str, client: str) -> dict[str, Any]:
        """カルテ用の生データを取得する（provider 注入時はそれ・本番は RLS 接続で実取得）。

        返り値: {"timeline": SearchHit list（list_client_timeline_recent・最新50件を古い順）,
                 "documents": dict list（list_documents_for_client・新しい順）}。
        timeline に ASC LIMIT の list_client_timeline を使うと FB が 50 件を超える
        クライアントで「最古の50件」になり最新 FB/ヘッダが誤るため recent 版を使う。
        接続の流儀は _list_graph_docs と同一（app_role + user_email/user_groups/user_role）。
        """
        if client_karte_provider is not None:
            return client_karte_provider(email, client)
        from teamagent.adapters.pgvector_client import PgVectorClient

        domain = email.split("@", 1)[1] if "@" in email else email
        pg = PgVectorClient.from_env()
        with pg.connection(
            app_role=app_role,
            user_email=email,
            user_groups=[domain],
            user_role="user",
        ) as conn:
            return {
                "timeline": pg.list_client_timeline_recent(conn, client, limit=50),
                "documents": pg.list_documents_for_client(conn, client, limit=50),
            }

    def _search_email(request: Request) -> str | None:
        """検索 cookie セッションから本人 email を取り出す（未認証/期限切れは None）。"""
        cookie = request.cookies.get(_SEARCH_COOKIE)
        if not cookie:
            return None
        return verify_session(cookie, search_cfg.session_secret)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        # Expose the exact immutable object contract so rollout verification can
        # compare VersionId and all content anchors, not a short observational hash.
        state = _resolve_app_html()
        return JSONResponse(
            {
                "ok": state["contract_ok"],
                "app_html_contract_ok": state["contract_ok"],
                "app_html_sha256": state["sha256"],
                "app_html_sha256_12": state["sha12"],
                "app_html_source": state["source"],
                "app_html_s3_version_id": state["version_id"],
                "app_html_expected_s3_version_id": state["expected_version_id"],
                "app_html_manifest_sha256": state["manifest_sha256"],
                "app_html_build_inputs_sha256": state["build_inputs_sha256"],
                "app_html_error": state["error"],
            }
        )

    @app.get("/oauth2/callback")
    def oauth2_callback(request: Request) -> Response:
        params = request.query_params
        err = params.get("error", "")
        if err:
            logger.warning("connect_callback_user_denied", error=err)
            return HTMLResponse(
                _page(
                    "認可がキャンセルされました",
                    "もう一度 Slack で /teamagent connect をお試しください。",
                ),
                status_code=400,
            )
        code = params.get("code", "")
        state = params.get("state", "")
        if not code or not state:
            return HTMLResponse(
                _page(
                    "不正なリクエスト",
                    "リンクが壊れています。Slack で /teamagent connect をやり直してください。",
                ),
                status_code=400,
            )
        email = verify_state(state)
        if not email:
            logger.warning("connect_callback_bad_state")
            return HTMLResponse(
                _page(
                    "検証に失敗しました",
                    "リンクが古いか不正です。Slack で /teamagent connect をやり直してください。",
                    accent="#f9667a",
                ),
                status_code=400,
            )
        try:
            token = _exchange(code)
            _get_store().put(email, token)
        except Exception as exc:
            # トークン/本文は出さない。診断用に例外の型と短い説明のみ。
            logger.warning(
                "connect_callback_store_failed",
                user_email=email,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return HTMLResponse(
                _page(
                    "連携に失敗しました",
                    "時間をおいて Slack で /teamagent connect をやり直してください。",
                    accent="#f9667a",
                ),
                status_code=500,
            )
        logger.info("connect_callback_ok", user_email=email, scopes=len(token.scopes))
        return HTMLResponse(
            _page(
                "✅ 連携が完了しました",
                f"{email} の Google 連携が完了しました。Slack に戻って AI に話しかけてください。"
                "このタブは閉じて大丈夫です。",
            ),
            status_code=200,
        )

    @app.get("/slack/oauth/callback")
    def slack_oauth_callback(request: Request) -> Response:
        params = request.query_params
        err = params.get("error", "")
        if err:
            logger.warning("connect_slack_callback_user_denied", error=err)
            return HTMLResponse(
                _page(
                    "認可がキャンセルされました",
                    "もう一度 Slack で /teamagent connect をお試しください。",
                ),
                status_code=400,
            )
        code = params.get("code", "")
        state = params.get("state", "")
        if not code or not state:
            return HTMLResponse(
                _page(
                    "不正なリクエスト",
                    "リンクが壊れています。Slack で /teamagent connect をやり直してください。",
                ),
                status_code=400,
            )
        email = slack_verify_state(state)
        if not email:
            logger.warning("connect_slack_callback_bad_state")
            return HTMLResponse(
                _page(
                    "検証に失敗しました",
                    "リンクが古いか不正です。Slack で /teamagent connect をやり直してください。",
                    accent="#f9667a",
                ),
                status_code=400,
            )
        try:
            token = _slack_exchange(code)
            # 外部WSの xoxp を他 email に紐付けないよう team_id 照合（設定時のみ）。
            expected_team = os.environ.get("SLACK_TEAM_ID", "").strip()
            if expected_team and token.team_id and token.team_id != expected_team:
                logger.warning(
                    "connect_slack_callback_team_mismatch",
                    user_email=email,
                    got_team=token.team_id,
                )
                return HTMLResponse(
                    _page(
                        "対象ワークスペースが違います",
                        "所属ワークスペースの Slack で /teamagent connect をお試しください。",
                        accent="#f9667a",
                    ),
                    status_code=403,
                )
            _get_slack_store().put(email, token)
        except Exception as exc:
            # xoxp/code/secret を露出させない。診断は例外型のみ（G8・str(exc) は出さない）。
            logger.warning(
                "connect_slack_callback_store_failed",
                user_email=email,
                error=type(exc).__name__,
            )
            return HTMLResponse(
                _page(
                    "連携に失敗しました",
                    "時間をおいて Slack で /teamagent connect をやり直してください。",
                    accent="#f9667a",
                ),
                status_code=500,
            )
        logger.info("connect_slack_callback_ok", user_email=email, scopes=len(token.scopes))
        return HTMLResponse(
            _page(
                "✅ Slack連携が完了しました",
                f"{email} の Slack 連携が完了しました。Slack に戻って AI に話しかけてください。"
                "このタブは閉じて大丈夫です。",
            ),
            status_code=200,
        )

    # ============================================================
    # P4: 小俣さん専用 資料検索 Web UI（dashboard.auth 再利用 + SearchSkill）
    # ============================================================

    @app.get("/search/login", response_class=HTMLResponse)
    def search_login(request: Request) -> HTMLResponse:
        """Google Sign-In ボタンを出す（許可アカウントのみ）。

        ``?next=`` でログイン後の戻り先を引き継ぐ（/app から来たら /app へ戻す）。
        """
        cid = search_cfg.google_client_id
        if not cid:
            return HTMLResponse(
                _page(
                    "ログイン未設定",
                    "CONNECT_GOOGLE_CLIENT_ID が未設定です。管理者に連絡してください。",
                    accent="#f9667a",
                )
            )
        cid_e = html.escape(cid)
        nxt_e = html.escape(_safe_next(request.query_params.get("next")))
        # AiLaVault ディープリンク（/app#client:… 等）の維持:
        # URL フラグメントはサーバに送信されないが、303 リダイレクトではブラウザが
        # 引き継ぐため、この login ページの URL には残っている。一方この後の
        # Google Sign-In → form POST → 303 /app の遷移でフラグメントは消えるので、
        # ここで sessionStorage に退避し、/app 側（app.html の初期化 JS）が復元する。
        # キー名 'ailavault.pendingHash' は app.html 生成器側と対（変えるなら両方）。
        scripts = (
            '<script src="https://accounts.google.com/gsi/client" async></script>'
            "<script>function onCred(r){"
            "document.getElementById('credential').value=r.credential;"
            "document.getElementById('idform').submit();}"
            "try{if(location.hash&&location.hash.length>1){"
            "sessionStorage.setItem('ailavault.pendingHash',location.hash);}"
            "else{sessionStorage.removeItem('ailavault.pendingHash');}}catch(e){}"
            "</script>"
        )
        body = (
            "<p>許可されたアカウントのみ社内ナレッジ検索を利用できます。</p>"
            '<div id="g_id_onload" data-client_id="' + cid_e + '" data-callback="onCred"></div>'
            '<div class="g_id_signin" data-type="standard" data-size="large"></div>'
            '<form id="idform" method="post" action="/search/auth/verify">'
            '<input type="hidden" id="credential" name="credential" value="">'
            '<input type="hidden" name="next" value="' + nxt_e + '"></form>'
        )
        html_doc = (
            '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>社内ナレッジ検索ログイン</title><style>"
            "body{margin:0;background:#0f1420;color:#e8edf7;font-family:-apple-system,"
            "'Hiragino Sans','Noto Sans JP',sans-serif;display:flex;min-height:100vh;"
            "align-items:center;justify-content:center}"
            ".card{background:#1a2233;border:1px solid #283450;border-radius:14px;"
            "padding:36px 40px;max-width:520px;text-align:center}"
            ".card h1{font-size:22px;margin:0 0 14px;color:#4f8cff}"
            ".card p{color:#93a1bd;line-height:1.7;margin:6px 0}"
            "</style>" + scripts + "</head><body>"
            '<div class="card"><h1>📚 社内ナレッジ検索ログイン</h1>' + body + "</div></body></html>"
        )
        return HTMLResponse(html_doc)

    @app.post("/search/auth/verify")
    async def search_auth_verify(request: Request) -> Response:
        """id_token を検証 → 許可判定 → 署名 cookie を発行して next（既定 /search）へ。"""
        form = await request.form()
        credential = str(form.get("credential", ""))
        nxt = _safe_next(str(form.get("next", "")))
        ok, email = authenticate_id_token(credential, search_cfg, verifier=search_verifier)
        if not ok or email is None:
            logger.warning("search_login_denied", email=email)
            return HTMLResponse(
                _page(
                    "ログインできませんでした",
                    "許可されていないアカウントか、検証に失敗しました。",
                    accent="#f9667a",
                ),
                status_code=403,
            )
        logger.info("search_login_ok", user_email=email, next=nxt)
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(
            _SEARCH_COOKIE,
            make_session(email, search_cfg.session_secret, ttl_s=_SESSION_TTL_S),
            max_age=_SESSION_TTL_S,
            httponly=True,
            samesite="lax",
            secure=search_cfg.cookie_secure,
        )
        return resp

    @app.get("/search")
    def search_ui(request: Request) -> Response:
        """検索 UI（未認証は /search/login へリダイレクト）。"""
        email = _search_email(request)
        if email is None:
            return RedirectResponse("/search/login", status_code=303)
        return HTMLResponse(_shell_page(email, mode="list"))

    @app.get("/app")
    def obsidian_app(request: Request) -> Response:
        """Obsidian 風 単一 HTML UI（allowlist 認証の内側でのみ配信）。

        ⚠️ 認証必須。単一 HTML のため per-user RLS は掛からない（allowlist を
        通った全員が同一内容を見る）。埋め込むのは共有可のナレッジのみ。
        配信内容は _resolve_app_html が S3 オーバーライド→イメージ同梱→準備中の
        優先順位で初回アクセス時に1回だけ解決する（No-AI 配信は維持）。
        """
        email = _search_email(request)
        if email is None:
            return RedirectResponse("/search/login?next=%2Fapp", status_code=303)
        return HTMLResponse(_resolve_app_html()["html"])

    @app.get("/r/{rid}")
    def report_redirect(rid: str) -> Response:
        """レポート短縮リンク: 署名トークンを検証し、都度新鮮な presigned S3 へ 302。

        openclaw(@AiLa) が長い presigned URL のクエリ(?X-Amz-Signature…)を削って壊す問題の根治。
        認証は掛けない（トークンが不透明・時限＝現行 presigned と同一の信頼境界。Slack 受信者は
        ログイン不要）。token 不正/失効/prefix・bucket 外は 404（fail-closed）。presigned は毎回
        再生成するため Cache-Control: no-store で中間キャッシュに期限切れURLを残さない。
        """
        from teamagent.adapters.report_link_token import decode_report_token
        from teamagent.adapters.report_publish import presign_get

        decoded = decode_report_token(rid)
        if decoded is None:
            return Response(
                "リンクが無効か期限切れです。",
                status_code=404,
                media_type="text/plain; charset=utf-8",
            )
        bucket, key, region = decoded
        url = presign_get(
            bucket,
            key,
            region=region or os.environ.get("AWS_REGION"),
            expires_s=_SHORTLINK_PRESIGN_TTL_S,  # 短命（実効閲覧窓を token TTL 内に抑える）
        )
        if not url:
            return Response(
                "レポートを取得できませんでした。",
                status_code=404,
                media_type="text/plain; charset=utf-8",
            )
        return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/search")
    async def api_search(request: Request) -> JSONResponse:
        """検索クエリを SearchSkill に渡し、要約 + 結果カードを JSON で返す。"""
        email = _search_email(request)
        if email is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "bad_request"}, status_code=400)
        query = str(payload.get("query", "")).strip()
        if not query:
            return JSONResponse({"error": "empty_query"}, status_code=400)
        try:
            top_k = int(payload.get("top_k", 8))
        except (TypeError, ValueError):
            top_k = 8
        top_k = max(1, min(top_k, 50))
        # タグchipクリックでの絞り込み（Obsidianのタグクリック相当）。空文字は無視。
        filter_industry = str(payload.get("filter_industry", "")).strip() or None
        # 取引先（部分一致 ILIKE・cls_project 主体）。空文字は無視。
        filter_client = str(payload.get("filter_client", "")).strip() or None
        # 予算バンドは allowlist（3バンド literal のみ）で二重防御。不正値は無視。
        _b = str(payload.get("filter_budget", "")).strip()
        filter_budget = _b if _b in _BUDGET_BANDS else None
        include_unknown_budget = bool(payload.get("include_unknown_budget", False))
        _s = str(payload.get("sort_budget_near", "")).strip()
        sort_budget_near = _s if _s in _BUDGET_BANDS else None
        # 資料種別は allowlist（_DOC_TYPES literal のみ）で二重防御。不正値は無視。
        _dt = str(payload.get("filter_doc_type", "")).strip()
        filter_doc_type = _dt if _dt in _DOC_TYPES else None
        # 施策/ソリューションは自由語彙（cls_solution は固定語彙に閉じない）。strip-or-None で
        # 受け、長すぎは SearchInput.filter_solution の max_length=50 に合わせて切り詰める。
        _sol = str(payload.get("filter_solution", "")).strip()
        filter_solution = _sol[:50] or None
        # 二段レスポンス (#1): False なら要約（Bedrock 数秒）をスキップし hits を即返す。
        # 既定 True＝完全後方互換（旧フロント・API 直叩きは無変更で従来挙動）。
        include_answer = bool(payload.get("include_answer", True))

        from teamagent.skills.base import SkillContext
        from teamagent.skills.search.schema import SearchHitOut, SearchInput

        domain = email.split("@", 1)[1] if "@" in email else email
        ctx = SkillContext(
            metadata={
                "user_email": email,
                "user_groups": [domain],
                "user_role": "user",
            }
        )

        def _run_search() -> Any:
            # worker スレッド側: skill 取得（初回のみ重い構築・Lock で単一初期化）+ 同期 run。
            return _get_search_skill().run(
                SearchInput(
                    query=query,
                    top_k=top_k,
                    filter_industry=filter_industry,
                    filter_client=filter_client,
                    filter_budget=filter_budget,
                    include_unknown_budget=include_unknown_budget,
                    sort_budget_near=sort_budget_near,
                    filter_doc_type=filter_doc_type,
                    filter_solution=filter_solution,
                    include_answer=include_answer,
                ),
                ctx,
            )

        try:
            # 同時実行を制限（セマフォ取得は async 側・run 本体は下の to_thread 側）。
            async with _get_search_semaphore():
                # キュー待ちの間にクライアントが abort/切断済みなら、embed/Bedrock 要約を
                # 走らせる前に破棄する（捨てられたリクエストへの課金回避）。499 は nginx の
                # Client Closed Request 慣行に合わせた非標準コード。
                if await request.is_disconnected():
                    logger.info("search_api_client_disconnected", user_email=email)
                    return JSONResponse({"error": "client_closed_request"}, status_code=499)
                # 同期 run をイベントループ外へ（1人の遅い検索が他の全リクエストを
                # 止めないようにする）。healthz/graph/feedback はブロックされなくなる。
                out = await asyncio.to_thread(_run_search)
        except Exception as exc:
            logger.warning(
                "search_api_failed",
                user_email=email,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return JSONResponse({"error": "search_failed"}, status_code=500)
        # gdrive:// は実ブラウザで開けないため、Drive の view リンクへ整形して「出典を開く」を
        # 実クリック可能にする（資料提出 段階1）。file_id 抽出失敗時は従来 source_uri に
        # fail-open。doc_id（FB 識別子）は元の h.source_uri のままに保つ。
        from teamagent.skills.knowledge_deliver.skill import extract_drive_file_id

        def _open_url(h: SearchHitOut) -> str | None:
            uri: str | None = h.source_uri or h.drive_url
            if h.source_type == "gdrive":
                fid = extract_drive_file_id(h.source_uri)
                if fid:
                    return f"https://drive.google.com/file/d/{fid}/view"
            return uri

        hits = [
            {
                "title": h.title or h.file_name or h.source or "(無題)",
                "excerpt": (h.content or "")[:120],
                "source_uri": _open_url(h),
                "source_type": h.source_type,
                "score": h.score,
                "client_name": h.client_name,
                # ナレッジ自動分類タグ（cls_*）。再取込で付与され、UI ではタグ chip 化する。
                "doc_type": h.doc_type,
                "industry": h.industry,
                "project": h.project,
                "budget": h.budget,
                "deal_phase": h.deal_phase,
                "doc_id": h.source_uri,
                "chunk_id": h.chunk_id,
            }
            for h in out.hits
        ]
        response: dict[str, Any] = {"answer": out.answer, "hits": hits}
        # 要約評価の突合キー。要約を返した場合にだけ本文から決定論的に生成し、fast path
        # （include_answer=False）および空要約の既存レスポンス形は変更しない。
        if include_answer and out.answer:
            response["answer_id"] = hashlib.sha256(out.answer.encode("utf-8")).hexdigest()[:16]
        return JSONResponse(response)

    @app.post("/api/v1/feedback")
    async def api_feedback(request: Request) -> JSONResponse:
        """👍/👎 を search_feedback に保存（user_email は cookie から・本文は保存しない）。"""
        email = _search_email(request)
        if email is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "bad_request"}, status_code=400)
        target_type = str(payload.get("target_type", ""))
        if target_type not in ("answer", "chunk"):
            return JSONResponse({"error": "bad_target_type"}, status_code=400)
        score: int | None = None
        if "score" in payload:
            try:
                score = int(payload["score"])
            except (TypeError, ValueError):
                return JSONResponse({"error": "bad_score"}, status_code=400)
            if score not in (1, 2, 3, 4):
                return JSONResponse({"error": "bad_score"}, status_code=400)
            if target_type != "answer":
                return JSONResponse({"error": "bad_score_target"}, status_code=400)
            rating = 1 if score >= 3 else -1
        else:
            try:
                rating = int(payload.get("rating"))
            except (TypeError, ValueError):
                return JSONResponse({"error": "bad_rating"}, status_code=400)
            if rating not in (-1, 1):
                return JSONResponse({"error": "bad_rating"}, status_code=400)
        query = str(payload.get("query", ""))[:1000]
        chunk_raw = payload.get("chunk_id")
        try:
            chunk_id = int(chunk_raw) if chunk_raw is not None else None
        except (TypeError, ValueError):
            chunk_id = None
        doc_id = payload.get("doc_id")
        note = payload.get("note")
        row = {
            "user_email": email,
            "query": query,
            "target_type": target_type,
            "doc_id": str(doc_id)[:512] if doc_id is not None else None,
            "chunk_id": chunk_id,
            "rating": rating,
            "note": str(note)[:500] if note is not None else None,
        }
        search_session_id = payload.get("search_session_id")
        if search_session_id is not None:
            search_session_id = str(search_session_id)[:64]
            if not re.fullmatch(r"[A-Za-z0-9-]+", search_session_id):
                search_session_id = None
        row["search_session_id"] = search_session_id
        answer_id = payload.get("answer_id")
        if answer_id is not None:
            answer_id = str(answer_id)[:32]
            if not re.fullmatch(r"[0-9A-Fa-f]+", answer_id):
                answer_id = None
        row["answer_id"] = answer_id
        row["score"] = score
        if score is not None and _feedback_rate_limited(email):
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        try:
            _save_feedback(row)
        except Exception as exc:
            logger.warning(
                "feedback_save_failed",
                user_email=email,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return JSONResponse({"error": "save_failed"}, status_code=500)
        return JSONResponse({"ok": True})

    @app.get("/search/graph")
    def search_graph_ui(request: Request) -> Response:
        """グラフビュー UI（未認証は /search/login へリダイレクト）。"""
        email = _search_email(request)
        if email is None:
            return RedirectResponse("/search/login", status_code=303)
        return HTMLResponse(_shell_page(email, mode="graph"))

    @app.get("/api/v1/graph")
    def api_graph(request: Request) -> JSONResponse:
        """RLS スコープのドキュメントを Obsidian 風グラフ（nodes/edges）にして返す。"""
        email = _search_email(request)
        if email is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from teamagent.connect_web.graph import build_graph

        # 意味クラスタ・エッジ（埋め込み kNN で「タグは違うが意味的に近い」資料を弱リンク）。
        # 既定 OFF。ON のときだけ代表ベクトルを引いて build_graph に渡す。
        concept_flag = os.environ.get("GRAPH_CONCEPT_EDGES", "").strip().lower()
        concept_on = concept_flag in ("1", "true", "yes")
        try:
            docs = _list_graph_docs(email, with_embeddings=concept_on)
        except Exception as exc:
            logger.warning(
                "graph_api_failed",
                user_email=email,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return JSONResponse({"error": "graph_failed"}, status_code=500)
        concept_vectors: dict[int, list[float]] | None = None
        if concept_on:
            concept_vectors = {
                int(d["node_id"]): d["embedding"]
                for d in docs
                if d.get("node_id") is not None and d.get("embedding")
            }
        # しきい値/k は env で上書き可（再ビルド無しで較正）。E5 はベースライン cosine が
        # 高めなので、団子化したら GRAPH_CONCEPT_THRESHOLD を上げる運用にする。
        return JSONResponse(
            build_graph(
                docs,
                concept_vectors=concept_vectors,
                concept_k=_env_int("GRAPH_CONCEPT_K", 4),
                concept_threshold=_env_float("GRAPH_CONCEPT_THRESHOLD", 0.85),
            )
        )

    @app.get("/search/client/{client:path}")
    def search_client_karte_ui(client: str, request: Request) -> Response:
        """クライアントカルテ UI（未認証は /search/login へリダイレクト）。

        client は FastAPI のパスパラメータで受ける（%エンコードは ASGI 層で復号済）。
        ``:path`` コンバータにするのは「A/B商事」のようにスラッシュを含むクライアント名が
        %2F 復号後の 1 セグメントマッチでは 404 になるため。値はデータとしてのみ扱い、
        HTML への差し込みは _karte_page 内で html.escape / _js_str により XSS 防御する。
        """
        email = _search_email(request)
        if email is None:
            return RedirectResponse("/search/login", status_code=303)
        name = client.strip()
        if not name:
            return RedirectResponse("/search", status_code=303)
        return HTMLResponse(_karte_page(name))

    @app.get("/api/v1/client/{client:path}")
    def api_client_karte(client: str, request: Request) -> JSONResponse:
        """カルテデータ API（ヘッダ + FB 時系列（日付降順）+ 関連資料一覧）。

        api_graph と同じ形（sync def・純 SQL 読み・401 JSON・500 は warning ログ）。
        ``:path`` はスラッシュ入りクライアント名対応（UI route と同じ理由）。
        データが 0 件でも 200 で空 list を返し、空状態の文言は UI 側で出す。
        """
        email = _search_email(request)
        if email is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        name = client.strip()
        if not name:
            return JSONResponse({"error": "empty_client"}, status_code=400)
        try:
            data = _client_karte_data(email, name)
            payload = _karte_payload(
                name,
                list(data.get("timeline") or []),
                list(data.get("documents") or []),
            )
        except Exception as exc:
            logger.warning(
                "client_karte_api_failed",
                user_email=email,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return JSONResponse({"error": "karte_failed"}, status_code=500)
        return JSONResponse(payload)

    return app


__all__ = ["create_app"]
