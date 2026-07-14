"""画像URL → base64 data URI 内包（提案書貼付カード用の自己完結HTML）。

なぜ内包するか:
- SNS/CDN の画像URLは署名付き・期限切れ（提案書に貼った後に画像が死ぬ）＋トークンが
  機微。取得時に bytes を DL → data URI 化して HTML/カードに閉じ込める（⑥ video_algorithm
  の cover/frames と同じ方式）。署名URLそのものは出力に残さない。

セキュリティ（scrape 由来URLを mcp サーバ側で fetch する＝SSRF面）:
- **許可ホストのみ** fetch（SNS画像CDNのサフィックス許可リスト）。内部/メタIPへ到達不可。
- **https のみ**・**リダイレクト追従しない**（redirect で内部へ飛ばす攻撃を封じる）。
- **ストリームで累積サイズ即断**（巨大body の OOM を防ぐ・DL後判定にしない）。
- **画像シグネチャ確定時のみ内包**（既定 jpeg フォールバックを廃止＝非画像を貼らない）。
- バッチは**総デッドライン**で打ち切り（残 URL は空＝フォールバック描画）。
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import structlog

if TYPE_CHECKING:
    import httpx

logger = structlog.get_logger(__name__)

_CONNECT_TIMEOUT_S = 2.0
_READ_TIMEOUT_S = 3.0
_MAX_BYTES = 3_000_000  # 1画像3MB上限（HTML肥大・巨大/悪意URL防御）
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# SSRF対策: fetch を許すホストのサフィックス（SNS画像CDNのみ）。ここに無いホストは fetch しない。
_ALLOWED_HOST_SUFFIXES: tuple[str, ...] = (
    ".twimg.com",  # X（pbs/video/abs/ton.twimg.com）
    ".cdninstagram.com",  # Instagram
    ".fbcdn.net",  # Meta CDN（IG/FB）
    ".tiktokcdn.com",  # TikTok
    ".tiktokcdn-us.com",
    ".ttwstatic.com",  # TikTok 静的
)
# マジックバイト → MIME（拡張子非依存・確定時のみ内包）。
_SIGS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _sniff_image_mime(b: bytes) -> str | None:
    """画像シグネチャに一致した時のみ MIME を返す。非画像は None（内包しない）。"""
    for sig, mime in _SIGS:
        if b.startswith(sig):
            return mime
    # WebP は RIFF....WEBP（12バイト目以降に WEBP）。
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return None


def _host_allowed(url: str) -> bool:
    """https かつ許可サフィックスのホストのみ True（SSRF 面を閉じる）。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    return any(host == s.lstrip(".") or host.endswith(s) for s in _ALLOWED_HOST_SUFFIXES)


def fetch_data_uri(url: str, *, request_id: str = "img", http: httpx.Client | None = None) -> str:
    """許可ホストの画像URL 1本を data URI 化。非許可/失敗/非画像/サイズ超過は空文字。"""
    if not _host_allowed(url):
        return ""
    try:
        import httpx

        own = http is None
        client = http or httpx.Client(
            follow_redirects=False,  # redirect 追従禁止（内部への飛ばしを封じる）
            timeout=httpx.Timeout(_READ_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S),
            headers={"User-Agent": _UA},
        )
        try:
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return ""
                total = 0
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_BYTES:  # 累積で即断（巨大body の OOM 防止）
                        return ""
                    buf.extend(chunk)
        finally:
            if own:
                client.close()
        mime = _sniff_image_mime(bytes(buf))
        if not mime:  # 非画像（HTMLエラーページ等）は貼らない
            return ""
        return f"data:{mime};base64," + base64.b64encode(bytes(buf)).decode("ascii")
    except Exception as e:  # ネットワーク/SSL/プロキシ等は握りつぶし、フォールバックに委ねる
        logger.warning("image_embed_failed", request_id=request_id, error=type(e).__name__)
    return ""


def fetch_data_uris(
    urls: list[str],
    *,
    request_id: str = "img",
    max_workers: int = 8,
    deadline_s: float | None = None,
    http: httpx.Client | None = None,
) -> list[str]:
    """複数URLを並列で data URI 化（入力と同順）。deadline_s 超過で未完は空文字で打ち切り。"""
    if not urls:
        return []
    results = [""] * len(urls)
    ex = ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(urls))))
    futs = {
        ex.submit(fetch_data_uri, u, request_id=request_id, http=http): i
        for i, u in enumerate(urls)
    }
    try:
        for fut in as_completed(futs, timeout=deadline_s):
            results[futs[fut]] = fut.result()
    except FuturesTimeout:
        logger.warning("image_embed_batch_deadline", request_id=request_id, total=len(urls))
    finally:
        # 未完（in-flight）は各自の read timeout で早々に終わる。pending はキャンセル。
        ex.shutdown(wait=False, cancel_futures=True)
    return results
