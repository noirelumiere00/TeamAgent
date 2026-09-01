"""共通 HTML レポートの発行口（唯一のチョークポイント）。

``Report`` を HTML 化 → 非公開 S3 へ put → 配信URL（短縮URL ``/r`` か presigned）を返す。
発行経路そのものは既存の :mod:`report_publish` / :mod:`report_delivery` をそのまま使い、
新しい配信面は作らない（信頼境界を増やさないため）。

段階ゲート ``USE_HTML_REPORTS``:
    既定 OFF。OFF の間は :func:`publish_report` が必ず ``None`` を返し、呼び出し側は
    現行どおりの結果を返す（＝1バイトも挙動が変わらない）。ツール単位で開けられるよう
    ``USE_HTML_REPORTS=tiktok_search,video_analysis`` のようなカンマ区切りも受ける
    （``1``/``true`` は全ツール ON）。

fail-open:
    レンダリング・S3・署名のいずれで失敗しても例外を投げず ``None`` を返す。レポートが
    出せないことは「検索結果を返せない」より軽い障害であり、本体機能を止めない。
"""

from __future__ import annotations

import dataclasses
import os
import tempfile

import structlog

from teamagent.identity import shared_company_domains_from_env
from teamagent.skills._html.headline import headline_enabled, make_headline
from teamagent.skills._html.report import Report, render_report
from teamagent.skills._shared.report_delivery import delivery_url

logger = structlog.get_logger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


def html_reports_enabled(tool: str) -> bool:
    """``tool`` に対して HTML レポート発行が有効か。

    ``USE_HTML_REPORTS`` が truthy なら全ツール ON。カンマ区切りならその名前だけ ON。
    未設定・空は OFF（＝既定は現行挙動のまま）。
    """
    raw = (os.environ.get("USE_HTML_REPORTS") or "").strip()
    if not raw:
        return False
    if raw.lower() in _TRUTHY:
        return True
    return tool.strip() in {part.strip() for part in raw.split(",") if part.strip()}


def _with_headline(report: Report, *, request_id: str, tool: str) -> Report:
    """本文の 1 行要約を見出しに載せる（無効・失敗時は元の report をそのまま返す）。"""
    if not headline_enabled() or report.headline or not report.body_md:
        return report
    try:
        from teamagent.adapters.bedrock_client import BedrockClient

        client = BedrockClient.from_env()
    except Exception as e:
        logger.warning(
            "report_headline_client_failed", request_id=request_id, error=type(e).__name__
        )
        return report
    line = make_headline(report.body_md, bedrock=client, request_id=request_id, tool=tool)
    return dataclasses.replace(report, headline=line) if line else report


def publish_report(
    report: Report,
    *,
    tool: str,
    request_id: str,
    query: str = "",
    rls_derived: bool = False,
) -> str | None:
    """レポートを発行し配信URLを返す。無効・失敗時は ``None``（呼び出し側は従来動作）。

    Args:
        rls_derived: 本文が **依頼者の RLS を通した社内ナレッジ**を含むか。True のときは
            会社共有モード（``TEAMAGENT_SHARED_COMPANY_DOMAINS`` 設定時）でのみ発行する。
            配信URLは「リンクを知る人が開ける」方式で受け手を見ないため、STRICT per-user
            構成では本人フィルタ済みの結果を無認証URL化することになる。payload_offload が
            同じ理由で会社共有モード限定になっている（レビュー F6）のと同じ線を引く。
    """
    if not html_reports_enabled(tool):
        return None
    if rls_derived and shared_company_domains_from_env() is None:
        logger.info("html_report_skipped_strict_rls", request_id=request_id, tool=tool)
        return None
    if not os.environ.get("VSEO_REPORT_BUCKET"):
        # バケット未設定の環境（ローカル/テスト）では発行しない。report_publish の既定値に
        # 頼ると開発機から本番バケットへ書きに行くため、明示設定を必須にする。
        logger.info("html_report_skipped_no_bucket", request_id=request_id, tool=tool)
        return None

    report = _with_headline(report, request_id=request_id, tool=tool)

    path = ""
    try:
        html = render_report(report)
        with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as fh:
            fh.write(html)
            path = fh.name

        from teamagent.adapters.report_publish import publish_html_file_result

        result = publish_html_file_result(path, request_id=request_id, query=query or report.title)
        if result is None:
            logger.warning("html_report_publish_failed", request_id=request_id, tool=tool)
            return None
        url = delivery_url(result, request_id=request_id)
        logger.info("html_report_published", request_id=request_id, tool=tool, key=result.key)
        return url
    except Exception as e:  # レポートは付加価値。本体の結果は必ず返す。
        logger.warning(
            "html_report_error", request_id=request_id, tool=tool, error=type(e).__name__
        )
        return None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


__all__ = ["html_reports_enabled", "publish_report"]
