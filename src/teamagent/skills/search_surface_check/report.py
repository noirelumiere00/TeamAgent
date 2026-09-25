"""検索上位チェックの HTML レポート（デジタル庁デザインシステム準拠・JS なし・ライト固定）。

読む順に並べる: 結論 → 主要な数字 → 投稿者の構成 → 常連とフォロワー帯 → 共通する切り口
→ よく付くタグ → 全件の一覧。取得できなかった媒体は空の列を出さず、冒頭の注意書きにまとめる。
"""

from __future__ import annotations

import html as _html

from teamagent.skills._html.dads import DADS_CREDIT, dads_style
from teamagent.skills._shared.text_safety import safe_href, sanitize_llm_text
from teamagent.skills.search_surface_check.display import (
    PLATFORM_LABEL,
    category_label,
    fmt_age,
    fmt_count,
    fmt_date,
    fmt_duration,
    fmt_pct,
    fmt_ranks,
)
from teamagent.skills.search_surface_check.schema import (
    KwSurface,
    SurfaceConclusion,
    SurfaceFacts,
    SurfacePost,
)
from teamagent.skills.search_surface_check.summary import rho_words

# 白地で文字のコントラスト 4.5:1 以上になる DADS の色（色だけで区別させず、必ず文字を添える）。
CATEGORY_COLOR: dict[str, str] = {
    "creator": "var(--color-primitive-blue-800)",
    "influencer": "var(--color-primitive-purple-700)",
    "ugc": "var(--color-primitive-cyan-900)",
    "media": "var(--color-primitive-orange-800)",
    "brand_official": "var(--color-primitive-green-800)",
    "news": "var(--color-primitive-magenta-900)",
    "other": "var(--color-neutral-solid-gray-600)",
    "unknown": "var(--color-neutral-solid-gray-536)",
}
_DESC_MAX = 90
_FOOTNOTES = "".join(
    f"<li>{line}</li>"
    for line in (
        "検索結果は見る人ごとに変わります（パーソナライズ）。傾向と定点比較に使ってください。",
        "投稿者のタイプは AI の推定です（アカウント名・表示名・フォロワー数・本文から判定）。",
        "保存率は保存数÷再生数。順位と再生数の一致度は順位相関（1 に近いほど再生の多い順）。",
        "Instagram は同じ人気リールが繰り返し出るため、出現回数×エンゲージの順に並べています。",
    )
)

_CSS = """
.overview{margin:24px 0 0;padding:0;list-style:none;display:grid;gap:8px}
.overview li{border:1px solid var(--color-neutral-solid-gray-420);
  border-radius:var(--border-radius-8);padding:12px 16px}
.overview .kw{font-weight:700}
.conclusion{border:2px solid var(--color-primitive-blue-900);border-radius:var(--border-radius-12);
  background:var(--color-primitive-blue-50);padding:24px;margin:16px 0 24px}
.conclusion .label{font-size:14px;font-weight:700;color:var(--color-primitive-blue-900);
  margin:0 0 4px}
.conclusion .headline{font-size:24px;line-height:1.5;font-weight:700;margin:0 0 16px;
  color:var(--color-neutral-solid-gray-900)}
.points{display:grid;grid-template-columns:max-content 1fr;gap:12px 16px;margin:0}
.points dt{align-self:start;background:var(--color-primitive-blue-900);
  color:var(--color-neutral-white);border-radius:var(--border-radius-4);padding:0 8px;
  font-size:14px;font-weight:700;line-height:1.7}
.points dd{margin:0}
.ranks{color:var(--color-neutral-solid-gray-600);font-size:14px;white-space:nowrap}
.by{font-size:14px;color:var(--color-neutral-solid-gray-600);margin:16px 0 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;
  margin:0 0 8px}
.kpi{border:1px solid var(--color-neutral-solid-gray-420);border-radius:var(--border-radius-8);
  padding:12px 16px;margin:0}
.kpi dt{font-size:14px;color:var(--color-neutral-solid-gray-600)}
.kpi dd{margin:0}
.kpi .v{display:block;font-size:28px;line-height:1.4;font-weight:700;
  font-variant-numeric:tabular-nums}
.kpi .v-text{font-size:22px;line-height:1.6;overflow-wrap:anywhere}
.kpi .n{display:block;font-size:14px;color:var(--color-neutral-solid-gray-600)}
.bars{margin:8px 0 16px}
.bar-row{display:grid;grid-template-columns:4.5em 1fr;gap:12px;align-items:center;margin:8px 0}
.bar-row .axis{font-size:14px;color:var(--color-neutral-solid-gray-600)}
.bar{display:flex;height:32px;border-radius:var(--border-radius-4);overflow:hidden;
  background:var(--color-neutral-solid-gray-50)}
.seg{display:flex;align-items:center;justify-content:center;color:var(--color-neutral-white);
  font-size:14px;font-weight:700;white-space:nowrap;overflow:hidden;
  border-right:2px solid var(--color-neutral-white)}
.seg:last-child{border-right:0}
.swatch{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:6px;
  vertical-align:-1px}
.two-col{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:24px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:0;padding:0;list-style:none}
.chip{border:1px solid var(--color-neutral-solid-gray-420);border-radius:999px;padding:2px 12px;
  font-size:14px}
.angles{margin:0;padding-left:1.4em}
.angles li{margin:4px 0}
.who .name{font-weight:700}
.who .handle{color:var(--color-neutral-solid-gray-600)}
.desc{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
  color:var(--color-neutral-solid-gray-700);max-width:28em}
tr.is-client td{background:var(--color-primitive-yellow-50)}
.tag-client{color:var(--color-primitive-yellow-1000)}
.tag-pr{color:var(--color-neutral-solid-gray-700)}
@media (max-width:640px){.points{grid-template-columns:1fr}.conclusion{padding:16px}
  h1{font-size:28px}.conclusion .headline{font-size:20px}
  .kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.kpi{padding:12px}
  .kpi .v{font-size:22px}.kpi .v-text{font-size:18px}.seg{font-size:0}}
"""


def _esc(s: object) -> str:
    return _html.escape(str(s), quote=True)


def _cat_tag(category: str) -> str:
    color = CATEGORY_COLOR.get(category, CATEGORY_COLOR["unknown"])
    return f"<span class='dads-tag' style='color:{color}'>{_esc(category_label(category))}</span>"


def _section_id(i: int) -> str:
    return f"surface-{i + 1}"


def _ranks_note(ranks: list[int]) -> str:
    return f" <span class='ranks'>（{_esc(fmt_ranks(ranks))}）</span>" if ranks else ""


def _conclusion_block(c: SurfaceConclusion | None) -> str:
    if c is None:
        return ""
    points: list[tuple[str, str, list[int]]] = []
    if c.winning:
        points.append(("勝ち筋", c.winning.text, c.winning.ranks))
    if c.gap:
        points.append(("空白", c.gap.text, c.gap.ranks))
    for a in c.actions:
        points.append(("打ち手", a.text, a.ranks))
    body = "".join(
        f"<dt>{_esc(label)}</dt><dd>{_esc(sanitize_llm_text(text))}{_ranks_note(ranks)}</dd>"
        for label, text, ranks in points
    )
    by = (
        "AI が下の集計と一覧だけを根拠に書いた読みです。文中の数字は集計と照合済みです。"
        if c.generated_by == "llm"
        else "AI の読みを作れなかったため、集計から自動で作った見出しだけを出しています。"
    )
    return (
        "<div class='conclusion'><p class='label'>結論</p>"
        f"<p class='headline'>{_esc(sanitize_llm_text(c.headline))}</p>"
        f"{f'<dl class=points>{body}</dl>' if body else ''}"
        f"<p class='by'>{by}</p></div>"
    )


def _kpi(label: str, value: str, note: str = "", *, text: bool = False) -> str:
    note_html = f"<span class='n'>{_esc(note)}</span>" if note else ""
    cls = "v v-text" if text else "v"
    return (
        f"<div class='kpi'><dt>{_esc(label)}</dt>"
        f"<dd><span class='{cls}'>{_esc(value)}</span>{note_html}</dd></div>"
    )


def _kpis(facts: SurfaceFacts, posts: list[SurfacePost]) -> str:
    tiles = [_kpi("上位の本数", f"{facts.n}本", f"{facts.unique_authors}アカウント")]
    if facts.categories:
        top = facts.categories[0]
        tiles.append(
            _kpi(
                "いちばん多い投稿者",
                category_label(top.category),
                f"{top.count}本・再生の{fmt_pct(top.play_share)}",
                text=True,
            )
        )
    if facts.small_in_top10 is not None:
        note = (
            f"再生÷フォロワー 中央値{facts.reach_ratio_median:g}倍"
            if facts.reach_ratio_median is not None
            else ""
        )
        tiles.append(_kpi("上位10本の1万人未満", f"{facts.small_in_top10}/{facts.top10_n}本", note))
    most = next((p for p in posts if p.rank == facts.most_played_rank), None)
    tiles.append(
        _kpi(
            "再生の中央値",
            f"{fmt_count(facts.median_plays)}回",
            f"最多は{most.rank}位 {fmt_count(most.play_count)}回" if most else "",
        )
    )
    if facts.median_save_rate_pct is not None:
        lead = facts.save_leaders[0] if facts.save_leaders else None
        tiles.append(
            _kpi(
                "保存率の中央値",
                f"{facts.median_save_rate_pct:g}%",
                f"最高は{lead.rank}位 {lead.save_rate_pct:g}%" if lead else "",
            )
        )
    if facts.median_age_days is not None:
        tiles.append(
            _kpi(
                "投稿時期の中央値",
                fmt_age(facts.median_age_days),
                f"直近90日の投稿 {facts.recent_90d}本" if facts.recent_90d is not None else "",
            )
        )
    if facts.median_duration_sec is not None:
        tiles.append(_kpi("尺の中央値", fmt_duration(facts.median_duration_sec)))
    if facts.kw_in_text is not None:
        tiles.append(_kpi("本文かタグに KW", f"{facts.kw_in_text}/{facts.n}本", "語をすべて含む"))
    return f"<dl class='kpis'>{''.join(tiles)}</dl>"


def _bar(label: str, shares: list[tuple[str, float]]) -> str:
    segs = "".join(
        f"<div class='seg' style='width:{share * 100:.1f}%;background:"
        f"{CATEGORY_COLOR.get(cat, CATEGORY_COLOR['unknown'])}' "
        f"title='{_esc(category_label(cat))} {fmt_pct(share)}'>"
        f"{_esc(category_label(cat)) if share >= 0.12 else ''}</div>"
        for cat, share in shares
        if share > 0
    )
    return (
        f"<div class='bar-row'><span class='axis'>{_esc(label)}</span>"
        f"<div class='bar' role='img' aria-label='{_esc(label)}の構成'>{segs}</div></div>"
    )


def _composition(facts: SurfaceFacts) -> str:
    if not facts.categories:
        return ""
    bars = _bar("本数", [(c.category, c.count_share) for c in facts.categories]) + _bar(
        "再生数", [(c.category, c.play_share) for c in facts.categories]
    )
    rows = "".join(
        "<tr>"
        f"<td><span class='swatch' style='background:"
        f"{CATEGORY_COLOR.get(c.category, CATEGORY_COLOR['unknown'])}'></span>"
        f"{_esc(category_label(c.category))}</td>"
        f"<td class='num'>{c.count}本</td><td class='num'>{fmt_pct(c.count_share)}</td>"
        f"<td class='num'>{fmt_pct(c.play_share)}</td>"
        f"<td class='num'>{fmt_count(c.median_plays)}回</td></tr>"
        for c in facts.categories
    )
    return (
        "<h3>投稿者の構成</h3>"
        "<p>本数の割合と再生数の割合を並べています。再生の割合が本数の割合より大きいタイプが、"
        "この面で再生を取っているタイプです。</p>"
        f"<div class='bars'>{bars}</div>"
        "<div class='dads-table-wrap'><table class='dads-table'><thead><tr><th>投稿者のタイプ</th>"
        "<th class='num'>本数</th><th class='num'>本数の割合</th><th class='num'>再生の割合</th>"
        f"<th class='num'>再生の中央値</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _holders_and_tiers(facts: SurfaceFacts) -> str:
    if facts.holders:
        items = "".join(
            f"<li><span class='name'>{_esc(h.author_name or '@' + h.author)}</span> "
            f"<span class='handle'>@{_esc(h.author)}</span> {_cat_tag(h.category)} "
            f"{len(h.ranks)}枠<span class='ranks'>（{_esc(fmt_ranks(h.ranks))}）</span></li>"
            for h in facts.holders
        )
        holders = f"<ul class='angles who'>{items}</ul>"
    else:
        holders = f"<p>{facts.n}本すべて別のアカウントです（複数の枠を持つ常連はいません）。</p>"
    tiers = ""
    if facts.tiers:
        rows = "".join(
            f"<tr><td>{_esc(t.tier)}</td><td class='num'>{t.count}本</td>"
            f"<td class='num'>{fmt_pct(t.play_share)}</td></tr>"
            for t in facts.tiers
        )
        tiers = (
            "<div class='dads-table-wrap'><table class='dads-table'><thead><tr>"
            "<th>フォロワー帯</th><th class='num'>本数</th><th class='num'>再生の割合</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
        )
    rho = ""
    if facts.rank_play_rho is not None:
        rho = (
            f"<p class='ranks'>順位と再生数の一致度 {facts.rank_play_rho:g}"
            f"（{_esc(rho_words(facts.rank_play_rho))}）</p>"
        )
    tiers = tiers or "<p>フォロワー数を取得できませんでした。</p>"
    return (
        "<div class='two-col'>"
        f"<div><h3>常連（複数の枠を持つアカウント）</h3>{holders}</div>"
        f"<div><h3>フォロワー帯</h3>{tiers}{rho}</div>"
        "</div>"
    )


def _angles_and_tags(c: SurfaceConclusion | None, facts: SurfaceFacts) -> str:
    parts: list[str] = []
    if c is not None and c.angles:
        items = "".join(
            f"<li>{_esc(sanitize_llm_text(a.text))}{_ranks_note(a.ranks)}</li>" for a in c.angles
        )
        parts.append(f"<div><h3>上位に共通する切り口</h3><ul class='angles'>{items}</ul></div>")
    if facts.top_tags:
        chips = "".join(
            f"<li class='chip'>#{_esc(t.tag)} <b>{t.count}</b>本</li>" for t in facts.top_tags
        )
        parts.append(f"<div><h3>よく付くタグ（2本以上）</h3><ul class='chips'>{chips}</ul></div>")
    return f"<div class='two-col'>{''.join(parts)}</div>" if parts else ""


def _who_cell(p: SurfacePost) -> str:
    name = _esc(p.author_name or (f"@{p.author}" if p.author else "不明"))
    href = safe_href(p.url)
    name_html = f"<a href='{_esc(href)}'>{name}</a>" if href else name
    handle = f" <span class='handle'>@{_esc(p.author)}</span>" if p.author_name else ""
    tags = ""
    if p.is_client:
        tags += " <span class='dads-tag tag-client'>クライアント</span>"
    if p.is_pr:
        tags += " <span class='dads-tag tag-pr'>PR表記</span>"
    if p.mentions_client and not p.is_client:
        tags += " <span class='dads-tag tag-client'>クライアント名に言及</span>"
    return f"<td class='who'><span class='name'>{name_html}</span>{handle}{tags}</td>"


def _desc_cell(p: SurfacePost) -> str:
    text = " ".join(p.desc.split())
    short = text[:_DESC_MAX] + ("…" if len(text) > _DESC_MAX else "")
    return f"<td><span class='desc' title='{_esc(text[:300])}'>{_esc(short)}</span></td>"


def _posts_table(surface: KwSurface) -> str:
    if surface.platform == "tiktok":
        head = (
            "<th class='num'>順位</th><th>投稿者</th><th>タイプ</th><th class='num'>フォロワー</th>"
            "<th class='num'>再生</th><th class='num'>保存率</th><th class='num'>投稿日</th>"
            "<th class='num'>尺</th><th>投稿の冒頭</th>"
        )
    else:
        head = (
            "<th class='num'>序列</th><th>投稿者</th><th>タイプ</th><th class='num'>出現回数</th>"
            "<th class='num'>再生・いいね</th><th>投稿の冒頭</th>"
        )
    rows: list[str] = []
    for p in surface.posts:
        cls = " class='is-client'" if p.is_client else ""
        if surface.platform == "tiktok":
            save = (
                f"{p.save_count / p.play_count * 100:.1f}%"
                if p.play_count and p.save_count
                else "—"
            )
            followers = fmt_count(p.author_followers) if p.author_followers else "—"
            cells = (
                f"<td class='num'>{p.rank}</td>{_who_cell(p)}<td>{_cat_tag(p.category)}</td>"
                f"<td class='num'>{followers}</td>"
                f"<td class='num'>{fmt_count(p.play_count)}</td><td class='num'>{save}</td>"
                f"<td class='num'>{fmt_date(p.posted_at) or '—'}</td>"
                f"<td class='num'>{fmt_duration(p.duration_sec) or '—'}</td>{_desc_cell(p)}"
            )
        else:
            cells = (
                f"<td class='num'>{p.rank}</td>{_who_cell(p)}<td>{_cat_tag(p.category)}</td>"
                f"<td class='num'>{p.appearances}回</td>"
                f"<td class='num'>{fmt_count(p.play_count or p.like_count)}</td>{_desc_cell(p)}"
            )
        rows.append(f"<tr{cls}>{cells}</tr>")
    order = "検索結果の表示順" if surface.platform == "tiktok" else "出現回数×エンゲージの順"
    return (
        f"<h3>上位の一覧（全{len(surface.posts)}本・{order}）</h3>"
        "<div class='dads-table-wrap'><table class='dads-table'>"
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _surface_section(i: int, s: KwSurface) -> str:
    platform = PLATFORM_LABEL.get(s.platform, s.platform)
    sid = _section_id(i)
    parts = [
        f"<section id='{sid}' aria-labelledby='{sid}-h'><h2 id='{sid}-h'>"
        f"「{_esc(s.keyword)}」{_esc(platform)}の上位{len(s.posts)}本</h2>",
        _conclusion_block(s.conclusion),
    ]
    if s.facts is not None:
        parts.append(_kpis(s.facts, s.posts))
        parts.append(_composition(s.facts))
        parts.append(_holders_and_tiers(s.facts))
        parts.append(_angles_and_tags(s.conclusion, s.facts))
    parts.append(_posts_table(s))
    parts.append("</section>")
    return "".join(parts)


def render_surface_report(
    *,
    keywords: list[str],
    surfaces: list[KwSurface],
    client_name: str | None,
    measured_epoch: int,
    missing: list[tuple[str, str]] | None = None,
    comparison_summary: str = "",
) -> str:
    """comparison_summary は互換のために受けるだけ（結論は各面の conclusion から出す）。"""
    del comparison_summary
    platforms = sorted({PLATFORM_LABEL.get(s.platform, s.platform) for s in surfaces})
    meta = [
        ("実測日", fmt_date(measured_epoch)),
        ("検索KW", "・".join(keywords)),
        ("媒体", "・".join(platforms) or "なし"),
    ]
    if client_name:
        meta.append(("クライアント", client_name))
    meta_html = "".join(f"<div><dt>{_esc(k)}</dt><dd>{_esc(v)}</dd></div>" for k, v in meta)
    notices = "".join(
        f"<div class='dads-notice' role='note'><b>{_esc(PLATFORM_LABEL.get(pf, pf))}"
        f"「{_esc(kw)}」はデータを取得できませんでした。</b>取得できた媒体だけで分析しています。</div>"
        for kw, pf in (missing or [])
    )
    overview = ""
    if len(surfaces) > 1:
        items = "".join(
            f"<li><a class='kw' href='#{_section_id(i)}'>「{_esc(s.keyword)}」"
            f"{_esc(PLATFORM_LABEL.get(s.platform, s.platform))}</a><br>"
            f"{_esc(sanitize_llm_text(s.conclusion.headline)) if s.conclusion else ''}</li>"
            for i, s in enumerate(surfaces)
        )
        overview = f"<h2>KW 別の結論</h2><ul class='overview'>{items}</ul>"
    sections = "".join(_surface_section(i, s) for i, s in enumerate(surfaces))
    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>検索上位チェック {_esc('・'.join(keywords))}</title>{dads_style(_CSS)}</head>"
        "<body><main class='dads-container'>"
        "<h1>検索上位チェック</h1>"
        "<p class='dads-lead'>検索結果の上位に、どんな投稿者のどんな動画が出ているかを数え、"
        "次の一手を読み解いた資料です。</p>"
        f"<dl class='dads-meta'>{meta_html}</dl>{notices}{overview}{sections}"
        f"<footer class='dads-footnote'><ul>{_FOOTNOTES}</ul><p>{_esc(DADS_CREDIT)}</p></footer>"
        "</main></body></html>"
    )


__all__ = ["CATEGORY_COLOR", "render_surface_report"]
