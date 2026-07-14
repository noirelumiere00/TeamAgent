"""画像URL → base64 data URI 内包（提案書貼付カード用の自己完結HTML）。

なぜ内包するか:
- SNS/CDN の画像URLは署名付き・期限切れ（提案書に貼った後に画像が死ぬ）＋トークンが
  機微。取得時に bytes を DL → data URI 化して HTML/カードに閉じ込める（⑥ video_algorithm
  の cover/frames と同じ方式）。署名URLそのものは出力に残さない。

方針:
- graceful: 取得失敗・非画像・サイズ超過は空文字を返す（呼び側はモノグラム等でフォールバック）。
- 上限: 1画像 _MAX_BYTES、per-image タイムアウト、バッチは ThreadPool。テストは client 注入。
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import httpx

logger = structlog.get_logger(__name__)

_FETCH_TIMEOUT_S = 4.0
_MAX_BYTES = 3_000_000  # 1画像3MB上限（HTML肥大・巨大/悪意URL防御）
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# マジックバイト → MIME（拡張子非依存で判定）。
_SIGS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # RIFF....WEBP
)


def _sniff_mime(b: bytes) -> str:
    for sig, mime in _SIGS:
        if b.startswith(sig):
            return mime
    return "image/jpeg"


def fetch_data_uri(url: str, *, request_id: str = "img", http: httpx.Client | None = None) -> str:
    """画像URL 1本を data URI 化。非http/失敗/非画像/サイズ超過は空文字（graceful）。"""
    if not url or not url.startswith(("http://", "https://")):
        return ""
    try:
        import httpx

        if http is not None:
            resp = http.get(url, follow_redirects=True)
        else:
            with httpx.Client(
                follow_redirects=True, timeout=_FETCH_TIMEOUT_S, headers={"User-Agent": _UA}
            ) as client:
                resp = client.get(url)
        content = resp.content
        if resp.status_code == 200 and content and len(content) <= _MAX_BYTES:
            mime = _sniff_mime(content)
            return f"data:{mime};base64," + base64.b64encode(content).decode("ascii")
        logger.info("image_embed_nonok", request_id=request_id, status=resp.status_code)
    except Exception as e:  # ネットワーク/SSL/プロキシ等は握りつぶし、フォールバックに委ねる
        logger.warning("image_embed_failed", request_id=request_id, error=type(e).__name__)
    return ""


def fetch_data_uris(
    urls: list[str],
    *,
    request_id: str = "img",
    max_workers: int = 6,
    http: httpx.Client | None = None,
) -> list[str]:
    """複数URLを並列で data URI 化（入力と同順・失敗は空文字）。"""
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(urls)))) as ex:
        return list(ex.map(lambda u: fetch_data_uri(u, request_id=request_id, http=http), urls))
