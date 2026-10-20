"""/admin/quality 専用。既存管理画面の描画には手を入れない。"""

from __future__ import annotations

import html
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from teamagent.dm_quality import QualityReport


def render_quality_page() -> str:
    report = None
    path = os.environ.get("DM_QUALITY_REPORT_PATH", "").strip()
    state = "未計測"
    if path:
        try:
            if Path(path).stat().st_size > 100_000:
                raise ValueError("report_too_large")
            report = QualityReport.model_validate_json(Path(path).read_text())
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            state = "集計を読み取れません"
    body = f"<p>{state}</p>"
    if report is not None:

        def percent(value: float | None) -> str:
            return "—" if value is None else f"{value:.1%}"

        jst = ZoneInfo("Asia/Tokyo")
        period = (
            f"{report.start.astimezone(jst):%Y-%m-%d %H:%M} 〜 "
            f"{report.end.astimezone(jst):%Y-%m-%d %H:%M}（終端を含まない・JST）"
        )
        status = "取得・判定完了（自動判定）" if report.complete else "欠測あり・未確定"
        ci = report.good_rate_ci95
        interval = f"{ci[0]:.1%}〜{ci[1]:.1%}" if ci else "—"
        body = (
            f"<p>{html.escape(period)}</p><p>{status}</p>"
            f"<p>観測締切：{report.observed_until.astimezone(jst):%Y-%m-%d %H:%M} JST</p>"
            f"<h2>全DM良い率：{percent(report.good_rate)}</h2>"
            f"<p>良い {report.good} / 判定済み依頼 {report.request_count} · 95%区間 {interval}</p>"
            f"<p>一部不足 {report.partial} · 悪い {report.bad} · 無回答 {report.no_reply} · "
            f"依頼ではない {report.not_a_request}</p>"
            f"<p>判定欠測 {report.missing} · 抽出失敗 {report.extraction_failures} · "
            f"発言単位 {report.unit_count} · 利用人数 {report.user_count} · "
            f"DM数 {report.dm_count}</p>"
            "<p>12/5の6割判定は全DM良い率を使用。人による較正・重大違反照合は別途必要です。</p>"
        )
        if report.request_count < 100:
            body += "<p>100依頼未満：単週達成とは扱いません。</p>"
        body += (
            f"<p>判定費用 ${report.cost_usd:.4f}（応答不明分は予約上限を含む） · "
            f"rubric_v1 · {html.escape(report.model_id)}</p>"
            f"<p>基準hash {report.rubric_sha256}</p>"
        )
    return (
        '<!doctype html><html lang="ja"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Aico DM品質</title><body><main>"
        '<p><a href="/admin">管理画面</a></p><h1>全DM良い率・週次測定</h1>'
        + body
        + "</main></body></html>"
    )
