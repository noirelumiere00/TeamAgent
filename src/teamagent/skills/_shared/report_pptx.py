"""共通レポートの PPTX 化（media worker 経由）。

``python-pptx`` は media extra 専用で mcp image には無い。だが media worker の slides
operation は「``.slide`` 要素をスクショして PPTX 化」するので、**mcp 側は HTML を作るだけ**で
PPTX を出せる（video_algorithm が既にこの経路で pptx を出している。同じ道を通す）。

コスト/遅延の扱い:
    media worker は同期実行（``run_sync``）で数十秒かかりうる。**既定では作らない**。
    呼び出し側が明示的に要求したときだけ（skill の ``outputs`` に ``"pptx"``）生成する。
    失敗は ``None``＝PPTX 無しで HTML レポートは返す（fail-open）。
"""

from __future__ import annotations

import os

import structlog

from teamagent.skills._html.report import Report
from teamagent.skills._html.slides import render_slides
from teamagent.skills._shared.report_delivery import delivery_url

logger = structlog.get_logger(__name__)


def pptx_enabled() -> bool:
    """``USE_HTML_REPORT_PPTX`` が真のときだけ生成できる（既定 OFF・段階ゲート）。"""
    return (os.environ.get("USE_HTML_REPORT_PPTX") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def publish_pptx(report: Report, *, tool: str, request_id: str) -> str | None:
    """レポートを PPTX にして配信URLを返す。無効・未構成・失敗は ``None``。"""
    if not pptx_enabled():
        return None
    try:
        from teamagent.adapters.media_job import MediaJobClient

        if not MediaJobClient.is_configured():
            logger.info("report_pptx_skipped_unconfigured", request_id=request_id, tool=tool)
            return None
        body = MediaJobClient().slides_to_pptx(
            render_slides(report),
            request_fingerprint=f"{request_id}:report-pptx",
            width=1280,
            height=720,
        )
    except Exception as e:
        logger.warning(
            "report_pptx_failed", request_id=request_id, tool=tool, error=type(e).__name__
        )
        return None
    if not body:
        return None

    from teamagent.adapters.report_publish import publish_bytes_result

    result = publish_bytes_result(
        body,
        content_type=("application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ext=".pptx",
        request_id=request_id,
    )
    if result is None:
        return None
    url = delivery_url(result, request_id=request_id)
    logger.info("report_pptx_published", request_id=request_id, tool=tool, key=result.key)
    return url


__all__ = ["pptx_enabled", "publish_pptx"]
