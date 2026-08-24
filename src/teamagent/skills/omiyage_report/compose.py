"""配信メッセージ（要点3行・次の一手・失敗時文言）の決定論生成。

数値は全て ``metrics.OmiyageMeasurement`` の実データ由来。文はテンプレート、
数字は計測値のみ（作文しない）。デッキ本体の組成は deck_plan.build_deck_plan。
"""

from __future__ import annotations

from teamagent.skills.omiyage_report.metrics import OmiyageMeasurement
from teamagent.skills.omiyage_report.video_analysis import VideoAnalysisReport


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value}%"


def _kw_label(measurement: OmiyageMeasurement) -> str:
    return "・".join(f"「{kw}」" for kw in measurement.keywords)


def build_summary_lines(measurement: OmiyageMeasurement) -> list[str]:
    """開かず分かる要点3行（数値は全て実測値）。"""
    lines: list[str] = []

    general = measurement.general_axes
    if general:
        axis_measurement = general[0]
        axis = axis_measurement.axis
        own = next(
            (e for e in axis_measurement.brand_exposure if e.brand == measurement.brand),
            None,
        )
        competitor_exposures = [
            e for e in axis_measurement.brand_exposure if e.brand != measurement.brand
        ]
        top_competitor = max(competitor_exposures, key=lambda e: e.videos, default=None)
        own_text = (
            f"{measurement.brand}関連は{own.videos}本（{_pct(own.share_pct)}）" if own else ""
        )
        if top_competitor is not None:
            own_text += (
                f"、競合最多は{top_competitor.brand} "
                f"{top_competitor.videos}本（{_pct(top_competitor.share_pct)}）"
            )
        lines.append(f"{axis.label}の上位{len(axis.posts)}本中、{own_text}でした。")
    else:
        lines.append("一般キーワード検索は取得できなかったため、露出シェアは未計測です。")

    brand_axis = measurement.brand_axis
    competitor_best: tuple[str, float] | None = None
    for axis_measurement in measurement.competitor_axes:
        rate = axis_measurement.keyword.combined_rate_pct
        if rate is not None and (competitor_best is None or rate > competitor_best[1]):
            competitor_best = (axis_measurement.axis.label, rate)
    if brand_axis is not None:
        brand_rate = _pct(brand_axis.keyword.combined_rate_pct)
        line = (
            f"一般キーワード{_kw_label(measurement)}の登場率（caption+hashtag計）は、"
            f"{brand_axis.axis.label}で{brand_rate}"
            f"（分母{brand_axis.keyword.denominator}本）"
        )
        if competitor_best is not None:
            line += f"、競合最高は{competitor_best[0]}の{competitor_best[1]}%"
        lines.append(line + "でした。")
    else:
        lines.append("ブランド名検索は取得できなかったため、自社側の登場率は未計測です。")

    pr_parts: list[str] = []
    if brand_axis is not None:
        pr = brand_axis.pr
        pr_parts.append(
            f"{brand_axis.axis.label}で{pr.pr_videos + pr.no_pr_videos}本中{pr.pr_videos}本"
        )
    competitor_pr_max: tuple[str, int] | None = None
    for axis_measurement in measurement.competitor_axes:
        count = axis_measurement.pr.pr_videos
        if competitor_pr_max is None or count > competitor_pr_max[1]:
            competitor_pr_max = (axis_measurement.axis.label, count)
    if competitor_pr_max is not None:
        pr_parts.append(f"競合最多は{competitor_pr_max[0]}の{competitor_pr_max[1]}本")
    if pr_parts:
        lines.append("#PR表記ありは" + "、".join(pr_parts) + "でした。")
    else:
        lines.append("#PR表記の比較は、対象軸を取得できなかったため未計測です。")
    return lines


def build_next_step() -> str:
    return (
        "次の一手: 上位投稿の実画面・構成比較まで踏み込んだ詳細版も作成できます。"
        "ご希望ならこのスレッドでお知らせください。"
    )


def build_partial_message(failed_labels: list[str]) -> str:
    """場面4: 一部失敗時の定型文（部分結果 or 再実行の選択肢を明示）。"""
    labels = "、".join(failed_labels)
    return (
        f"一部の検索（{labels}）が失敗したため、取得できた範囲の部分結果で資料を作成しました。"
        "失敗した軸も含めたい場合は、同じ内容でもう一度ご依頼ください（再実行します）。"
    )


def build_analysis_note(analysis: VideoAnalysisReport | None) -> str:
    """動画解析の実施状況の開示（未実施・部分実施を黙って埋めない）。"""
    if analysis is None:
        return (
            "動画解析（界隈クラスタ・テロップ読取）は今回実施できなかったため、"
            "該当ページは未収録です（0件ではありません）。"
        )
    if analysis.analyzed == 0:
        return (
            "動画解析はすべて失敗したため、界隈クラスタ・テロップ経路は未収録です"
            "（取得失敗一覧は監査記録に残しています）。"
        )
    failed = len(analysis.failures) + len(analysis.skipped_video_ids)
    if failed:
        return (
            f"動画解析は{analysis.analyzed}本で実施しました"
            f"（未解析{failed}本は分母に含めず監査記録に記載）。"
        )
    return ""


def build_all_failed_message() -> str:
    """場面4: 全滅時の定型文（選択肢つき・黙って消えない）。"""
    return (
        "TikTok検索がすべて失敗したため、資料を作成できませんでした。"
        "時間をおいて同じ内容で再依頼いただく（再実行）か、"
        "検索語・競合名を変えてご依頼ください。"
    )


def build_delivery_failed_note() -> str:
    return (
        "資料の生成は完了しましたが、Slackへの添付に失敗しました。"
        "同じ内容でもう一度ご依頼いただければ再生成・再添付します。"
    )


__all__ = [
    "build_all_failed_message",
    "build_analysis_note",
    "build_delivery_failed_note",
    "build_next_step",
    "build_partial_message",
    "build_summary_lines",
]
