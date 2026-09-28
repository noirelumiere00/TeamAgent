"""2 段目（上位の動画の中身）の Slack 追記文とレポートの章。

Slack の文面は 1 段目（summary.py）と同じ標準 Markdown（太字は `**語**`）で組む。会話へは mcp が
直接投稿するので、投稿の直前に `**語**`→`*語*` へ直す（呼び出し側）。表は使わず、1 行 1 事実・
1 本 1 行。第三者の文字列（アカウント名・AI が要約した動画の中身）は slack_safe で無害化する。

レポートの章は report.py と同じ DADS の部品（結論ブロック・KPI タイル・表・カード）で組む。
"""

from __future__ import annotations

import html as _html
from collections.abc import Callable

from teamagent.skills._shared.text_safety import safe_href, sanitize_llm_text
from teamagent.skills.search_surface_check.display import fmt_duration, fmt_ranks
from teamagent.skills.search_surface_check.report import _kpi
from teamagent.skills.search_surface_check.schema import (
    LabelCount,
    VideoDigest,
    VideoDigestConclusion,
)
from teamagent.skills.search_surface_check.summary import slack_safe
from teamagent.skills.search_surface_check.video_digest import (
    PACING_LABEL,
    cta_label,
    duration_of,
    has_opening_telop,
    has_spoken_kw,
    has_telop_kw,
    hook_label,
    is_cover_only,
    is_watched,
)
from teamagent.skills.video_algorithm.schema import AnalyzedVideo, VideoVSEOAnalysis

_QUOTE_MAX = 20
REPORT_FAILED_LINE = "レポートの発行に失敗しました（上の要約は分析結果どおりです）"
COVER_ONLY_NOTE = "動画を取得できず、サムネだけの分析（テロップ・構成は判定できません）"
FAILED_NOTE = "分析できませんでした"
QUOTA_EXHAUSTED_TEXT = (
    "今月の動画分析の上限に達したため、動画の中身は分析しませんでした（残り 0 本）。"
    "リセットは来月1日（JST）です。"
)
ALL_FAILED_TEXT = "動画を取得できず分析できませんでした。"


def _mark(flag: bool) -> str:
    return "○" if flag else "×"


def _counts_text(items: list[LabelCount]) -> str:
    return "／".join(f"{slack_safe(c.label)} {c.count}本" for c in items)


def _handle(video: AnalyzedVideo) -> str:
    return slack_safe(f"@{video.meta.author}") if video.meta.author else "不明"


def _quote(text: str) -> str:
    text = slack_safe(text)
    if not text:
        return ""
    return "（『" + (text[:_QUOTE_MAX] + "…" if len(text) > _QUOTE_MAX else text) + "』）"


def _shape(video: AnalyzedVideo) -> str:
    a = video.analysis
    parts: list[str] = []
    dur = duration_of(video)
    if dur > 0:
        parts.append(fmt_duration(round(dur)))
    if a is not None and a.cut_count is not None:
        parts.append(f"{a.cut_count}カット")
    if a is not None and a.pacing != "unknown":
        parts.append(PACING_LABEL.get(a.pacing, a.pacing))
    return "・".join(parts)


def _cta(video: AnalyzedVideo) -> str:
    a = video.analysis
    if a is None or not a.has_cta():
        return "なし"
    labels = [cta_label(c) for c in dict.fromkeys(a.cta_type)]
    return slack_safe("・".join(labels)) if labels else "あり"


def video_line(video: AnalyzedVideo) -> str:
    """1 本 1 行（例: 「- 1位 @id フック: 数字（『4つでいい』）／冒頭テロップ○／…」）。"""
    head = f"- {video.meta.rank}位 {_handle(video)}"
    a = video.analysis
    if a is None:
        return f"{head} {FAILED_NOTE}"
    if not is_watched(video):
        return f"{head} {COVER_ONLY_NOTE}"
    parts = [
        f"フック: {hook_label(a.hook_type)}{_quote(a.hook_summary)}",
        f"冒頭テロップ{_mark(has_opening_telop(a))}",
        f"KW テロップ{_mark(has_telop_kw(a))}・発話{_mark(has_spoken_kw(a))}",
    ]
    shape = _shape(video)
    if shape:
        parts.append(shape)
    parts.append(f"CTA: {_cta(video)}")
    return f"{head} " + "／".join(parts)


def _with_ranks(text: str, ranks: list[int]) -> str:
    return slack_safe(text) + (f"（{fmt_ranks(ranks)}）" if ranks else "")


def _digest_lines(digest: VideoDigest) -> list[str]:
    n = digest.watched
    lines: list[str] = []
    if digest.hook_types:
        lines.append(f"- フック: {_counts_text(digest.hook_types)}")
    lines.append(
        f"- テロップと KW: 冒頭にテロップ {digest.opening_telop}/{n}本・"
        f"テロップに KW {digest.telop_kw}/{n}本・発話に KW {digest.spoken_kw}/{n}本"
    )
    shape: list[str] = []
    if digest.median_duration_sec is not None:
        shape.append(f"尺の中央値 {fmt_duration(round(digest.median_duration_sec))}")
    if digest.median_cut_count is not None:
        shape.append(f"カット数の中央値 {digest.median_cut_count:g}")
    if digest.pacing:
        shape.append(f"テンポ {_counts_text(digest.pacing)}")
    if shape:
        lines.append("- 構成: " + "・".join(shape))
    cta = f"- CTA: あり {digest.cta}/{n}本"
    if digest.cta_types:
        cta += f"（{'・'.join(f'{slack_safe(c.label)} {c.count}本' for c in digest.cta_types)}）"
    lines.append(cta)
    lines.append(
        f"- 音: ナレーション {digest.narration}/{n}本・流行の音源 {digest.trending_sound}/{n}本"
    )
    if digest.median_coherence is not None:
        lines.append(
            f"- テロップ・本文・映像の一致度の中央値: {digest.median_coherence:g}（100 が一致）"
        )
    if digest.save_top_ranks:
        common = "・".join(digest.save_top_common) or "目立った共通点はなし"
        lines.append(f"- 保存率の高い2本（{fmt_ranks(digest.save_top_ranks)}）に共通: {common}")
    return lines


def build_followup_slack_text(
    *,
    keyword: str,
    digest: VideoDigest,
    conclusion: VideoDigestConclusion | None,
    videos: list[AnalyzedVideo],
    report_url: str | None,
    total_cost_usd: float,
) -> str:
    """動画を分析できた（サムネだけを含む）ときの追記文。"""
    lines = [f"**上位{len(videos)}本の動画の中身**「{slack_safe(keyword)}」TikTok"]
    if conclusion is not None and conclusion.headline:
        lines.append(f"**結論** {slack_safe(conclusion.headline)}")
    if conclusion is not None and conclusion.winning is not None:
        lines.append("- 勝ち筋: " + _with_ranks(conclusion.winning.text, conclusion.winning.ranks))
    if digest.watched > 0:
        lines.extend(_digest_lines(digest))
    else:
        lines.append(
            "- 動画を取得できず、サムネだけの分析です（テロップ・構成・音は判定できません）"
        )
    if conclusion is not None and conclusion.save_reason is not None:
        lines.append(
            "- 保存の理由: "
            + _with_ranks(conclusion.save_reason.text, conclusion.save_reason.ranks)
        )
    lines.append("**1本ずつ**")
    lines.extend(video_line(v) for v in videos)
    notes: list[str] = []
    if digest.reserved < digest.requested:
        notes.append(
            f"今月の動画分析の残りの都合で、{digest.requested}本のうち{digest.reserved}本だけ"
            "分析しました（リセットは来月1日・JST）"
        )
    if digest.cover_only_ranks:
        notes.append(
            f"{fmt_ranks(digest.cover_only_ranks)}は動画を取得できず、サムネだけの分析です"
            "（集計には入れていません）"
        )
    if digest.failed_ranks:
        notes.append(f"{fmt_ranks(digest.failed_ranks)}は分析できませんでした")
    notes.append("数本の観測なので、傾向として読んでください（順位の理由の断定ではありません）")
    lines.append("")
    lines.append("注意: " + " / ".join(notes))
    if report_url:
        lines.append(f"レポート（上位{len(videos)}本の動画の中身つき・7日有効）: {report_url}")
    else:
        lines.append(REPORT_FAILED_LINE)
    lines.append(f"_概算 ${total_cost_usd:.4f}_")
    return "\n".join(lines).strip()


def build_quota_exhausted_text(keyword: str) -> str:
    return f"**上位の動画の中身**「{slack_safe(keyword)}」TikTok\n{QUOTA_EXHAUSTED_TEXT}"


def build_all_failed_text(keyword: str, *, count: int, total_cost_usd: float) -> str:
    return (
        f"**上位{count}本の動画の中身**「{slack_safe(keyword)}」TikTok\n{ALL_FAILED_TEXT}\n"
        f"_概算 ${total_cost_usd:.4f}_"
    )


# ── レポートの章（DADS）─────────────────────────────────────────────────

CHAPTER_ID = "top-videos"
CHAPTER_CSS = """
.vcards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;
  margin:16px 0 0;padding:0;list-style:none}
.vcard{border:1px solid var(--color-neutral-solid-gray-420);border-radius:var(--border-radius-8);
  padding:16px}
.vcard h4{margin:0 0 8px;font-size:16px}
.vcard dl{margin:0;display:grid;grid-template-columns:max-content 1fr;gap:4px 12px;font-size:14px}
.vcard dt{color:var(--color-neutral-solid-gray-600)}
.vcard dd{margin:0}
.vcard .note{font-size:14px;color:var(--color-neutral-solid-gray-700);margin:8px 0 0}
table.axes td.agg{font-weight:700;white-space:nowrap}
"""


def _esc(s: object) -> str:
    return _html.escape(str(s), quote=True)


def _clean(text: str, max_len: int = 200) -> str:
    return _esc(sanitize_llm_text(" ".join((text or "").split()), max_len=max_len))


def _chapter_conclusion(c: VideoDigestConclusion | None) -> str:
    if c is None:
        return ""
    points: list[tuple[str, str, list[int]]] = []
    if c.winning:
        points.append(("勝ち筋", c.winning.text, c.winning.ranks))
    if c.save_reason:
        points.append(("保存の理由", c.save_reason.text, c.save_reason.ranks))
    body = "".join(
        f"<dt>{_esc(label)}</dt><dd>{_clean(text)}"
        + (f" <span class='ranks'>（{_esc(fmt_ranks(ranks))}）</span>" if ranks else "")
        + "</dd>"
        for label, text, ranks in points
    )
    by = (
        "AI が下の集計と 1 本ずつの分析だけを根拠に書いた読みです。文中の数字は集計と照合済みです。"
        if c.generated_by == "llm"
        else "AI の読みを作れなかったため、集計から自動で作った見出しだけを出しています。"
    )
    return (
        "<div class='conclusion'><p class='label'>動画の中身から見た結論</p>"
        f"<p class='headline'>{_clean(c.headline, 80)}</p>"
        f"{f'<dl class=points>{body}</dl>' if body else ''}"
        f"<p class='by'>{by}</p></div>"
    )


def _chapter_kpis(d: VideoDigest) -> str:
    n = d.watched
    tiles = [_kpi("動画を見て分析", f"{n}本", f"サムネのみ {len(d.cover_only_ranks)}本")]
    if d.hook_types:
        top = d.hook_types[0]
        tiles.append(_kpi("いちばん多いフック", top.label, f"{top.count}/{n}本", text=True))
    tiles.append(_kpi("冒頭にテロップ", f"{d.opening_telop}/{n}本", "最初の3秒"))
    tiles.append(_kpi("テロップに KW", f"{d.telop_kw}/{n}本", f"発話に KW {d.spoken_kw}/{n}本"))
    if d.median_duration_sec is not None:
        note = f"カット数の中央値 {d.median_cut_count:g}" if d.median_cut_count is not None else ""
        tiles.append(_kpi("尺の中央値", fmt_duration(round(d.median_duration_sec)), note))
    tiles.append(_kpi("CTA あり", f"{d.cta}/{n}本"))
    if d.median_coherence is not None:
        tiles.append(_kpi("一致度の中央値", f"{d.median_coherence:g}", "テロップ・本文・映像"))
    return f"<dl class='kpis'>{''.join(tiles)}</dl>"


def _cell(video: AnalyzedVideo, value: str) -> str:
    return f"<td>{_esc(value) if is_watched(video) else '—'}</td>"


def _axes_table(d: VideoDigest, videos: list[AnalyzedVideo]) -> str:
    n = d.watched

    def row(axis: str, agg: str, per: list[str]) -> str:
        cells = "".join(_cell(v, text) for v, text in zip(videos, per, strict=True))
        return f"<tr><th scope='row'>{_esc(axis)}</th><td class='agg'>{_esc(agg)}</td>{cells}</tr>"

    def per(fn: Callable[[AnalyzedVideo, VideoVSEOAnalysis], str]) -> list[str]:
        return [fn(v, v.analysis) if v.analysis is not None else "" for v in videos]

    cta_agg = f"{d.cta}/{n}本"
    rows = [
        row(
            "フック",
            "・".join(f"{h.label}{h.count}" for h in d.hook_types) or "—",
            per(lambda v, a: hook_label(a.hook_type)),
        ),
        row(
            "冒頭テロップ",
            f"{d.opening_telop}/{n}本",
            per(lambda v, a: _mark(has_opening_telop(a))),
        ),
        row("テロップに KW", f"{d.telop_kw}/{n}本", per(lambda v, a: _mark(has_telop_kw(a)))),
        row("発話に KW", f"{d.spoken_kw}/{n}本", per(lambda v, a: _mark(has_spoken_kw(a)))),
        row(
            "尺",
            fmt_duration(round(d.median_duration_sec)) if d.median_duration_sec else "—",
            per(lambda v, a: fmt_duration(round(duration_of(v))) or "—"),
        ),
        row(
            "カット数",
            f"{d.median_cut_count:g}" if d.median_cut_count is not None else "—",
            per(lambda v, a: str(a.cut_count) if a.cut_count is not None else "—"),
        ),
        row(
            "テンポ",
            "・".join(f"{p.label}{p.count}" for p in d.pacing) or "—",
            per(lambda v, a: PACING_LABEL.get(a.pacing, a.pacing)),
        ),
        row("CTA", cta_agg, per(lambda v, a: _cta(v))),
        row("ナレーション", f"{d.narration}/{n}本", per(lambda v, a: _mark(a.has_narration))),
        row(
            "流行の音源",
            f"{d.trending_sound}/{n}本",
            per(
                lambda v, a: {"yes": "○", "no": "×"}.get(a.is_trending_sound, "不明"),
            ),
        ),
        row(
            "一致度",
            f"{d.median_coherence:g}" if d.median_coherence is not None else "—",
            per(lambda v, a: str(a.message_coherence) if a.message_coherence is not None else "—"),
        ),
        row(
            "保存の理由",
            "—",
            per(lambda v, a: sanitize_llm_text(a.save_share_motivation, max_len=60) or "—"),
        ),
    ]
    head = "".join(f"<th scope='col'>{v.meta.rank}位</th>" for v in videos)
    return (
        "<h3>評価軸ごとの比較</h3>"
        "<p>集計の列は「動画を見て分析できた本数」を分母にしています。"
        "— は、動画を取得できず判定できなかった項目です。</p>"
        "<div class='dads-table-wrap'><table class='dads-table axes'><thead><tr>"
        f"<th scope='col'>評価軸</th><th scope='col'>集計</th>{head}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _video_card(v: AnalyzedVideo) -> str:
    handle = f"@{v.meta.author}" if v.meta.author else "不明"
    href = safe_href(v.meta.url)
    title = f"{v.meta.rank}位 " + (
        f"<a href='{_esc(href)}'>{_esc(handle)}</a>" if href else _esc(handle)
    )
    a = v.analysis
    if a is None:
        return (
            f"<li class='vcard'><h4>{title}</h4>"
            f"<p class='note'>{_esc(FAILED_NOTE)}（{_esc(v.error or '理由不明')}）。</p></li>"
        )
    rows: list[tuple[str, str]] = [
        ("フック", f"{hook_label(a.hook_type)}：{sanitize_llm_text(a.hook_summary, max_len=120)}"),
        ("主なメッセージ", sanitize_llm_text(a.main_message, max_len=160)),
        ("勝因", " ／ ".join(sanitize_llm_text(w, max_len=60) for w in a.win_factors[:3])),
        ("保存の理由", sanitize_llm_text(a.save_share_motivation, max_len=160)),
    ]
    body = "".join(
        f"<dt>{_esc(label)}</dt><dd>{_esc(' '.join(text.split()))}</dd>"
        for label, text in rows
        if text.strip()
    )
    note = f"<p class='note'>{_esc(COVER_ONLY_NOTE)}。</p>" if is_cover_only(v) else ""
    return f"<li class='vcard'><h4>{title}</h4><dl>{body}</dl>{note}</li>"


def render_video_chapter(
    *,
    keyword: str,
    digest: VideoDigest,
    conclusion: VideoDigestConclusion | None,
    videos: list[AnalyzedVideo],
) -> str:
    """レポートに足す章（TikTok 面の節の直後に置く）。"""
    parts = [
        f"<section id='{CHAPTER_ID}' aria-labelledby='{CHAPTER_ID}-h'>"
        f"<h2 id='{CHAPTER_ID}-h'>「{_esc(keyword)}」TikTok 上位{len(videos)}本の動画の中身</h2>"
        "<p>検索上位の動画を、動画分析 AI が 1 本ずつ実際に視聴して、フック・テロップ・構成・"
        "CTA などショート動画の評価軸で分析しました。数本の観測なので、傾向として読んでください"
        "（順位の理由の断定ではありません）。</p>",
        _chapter_conclusion(conclusion),
    ]
    if digest.watched > 0:
        parts.append(_chapter_kpis(digest))
    else:
        parts.append(
            "<div class='dads-notice' role='note'><b>動画を取得できませんでした。</b>"
            "サムネイルだけの分析のため、テロップ・構成・音は判定していません。</div>"
        )
    parts.append(_axes_table(digest, videos))
    if digest.save_top_ranks:
        common = "・".join(digest.save_top_common) or "目立った共通点はありません"
        parts.append(
            f"<p>保存率の高い 2 本（{_esc(fmt_ranks(digest.save_top_ranks))}）に共通すること: "
            f"{_esc(common)}。</p>"
        )
    parts.append("<h3>1 本ずつ</h3>")
    parts.append(f"<ul class='vcards'>{''.join(_video_card(v) for v in videos)}</ul>")
    parts.append("</section>")
    return "".join(parts)


__all__ = [
    "ALL_FAILED_TEXT",
    "CHAPTER_CSS",
    "CHAPTER_ID",
    "COVER_ONLY_NOTE",
    "QUOTA_EXHAUSTED_TEXT",
    "REPORT_FAILED_LINE",
    "build_all_failed_text",
    "build_followup_slack_text",
    "build_quota_exhausted_text",
    "render_video_chapter",
    "video_line",
]
