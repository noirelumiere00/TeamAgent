"""検索上位チェックの結果 → 金庫（documents → Aico Vault）に残す要約 markdown。

レポート HTML は 7 日で切れる署名 URL なので、あとから「いつ・どの KW で・誰が上位だったか・
どう読んだか」を振り返れるよう、結論と主要な集計と上位 10 本を素の markdown で残す。
作り方は x_research の persist_body（声集めのノート）に合わせる:
- 1 document = 1 chunk の本文になる（export_vault は研究ノートの本文を全文そのまま Vault に書く）。
- 第三者の文字列（@ID・表示名）と、第三者の本文を読んだ LLM の結論は、x_research と同じ
  ``_clean``（URL 伏字＋改行畳み＋Markdown 記法の無害化）を通す。投稿 URL も同じ
  ``_safe_provenance_url`` を通し、さらに既知 SNS ホストだけに絞る（``safe_href``）。
- Obsidian のタグにならないよう、ハッシュタグの一覧は載せない（``#語`` はタグとして拾われる）。
"""

from __future__ import annotations

from teamagent.skills._shared.text_safety import safe_href
from teamagent.skills.search_surface_check.display import (
    PLATFORM_LABEL,
    account_label,
    category_label,
    fmt_age,
    fmt_count,
    fmt_date,
    fmt_pct,
    fmt_ranks,
)
from teamagent.skills.search_surface_check.schema import (
    ConclusionPoint,
    KwSurface,
    SearchSurfaceCheckOutput,
    SurfaceFacts,
)

# x_research と同じ安全化を使う（関数を共有して、ノートごとに扱いがずれないようにする）。
from teamagent.skills.x_research.persist_body import _clean, _safe_provenance_url

_FOOTER = "\n---\n（@Aico の検索上位チェックが自動生成した実測の記録）"
_TOP_N = 10  # 1 面あたりノートに載せる上位の本数（仕様: 上位 10 本）


def _platforms(out: SearchSurfaceCheckOutput) -> list[str]:
    seen: list[str] = []
    for s in out.surfaces:
        label = PLATFORM_LABEL.get(s.platform, s.platform)
        if label not in seen:
            seen.append(label)
    return seen


def build_surface_title(client_name: str, out: SearchSurfaceCheckOutput) -> str:
    """ノートの題名。媒体名（TikTok/Instagram）を入れて build_app_html の 媒体/ タグを付ける。"""
    kws = "".join(f"「{k}」" for k in out.keywords)
    platforms = "・".join(_platforms(out))
    return f"{client_name} 検索上位チェック{kws}（{platforms}）"


def _point(label: str, p: ConclusionPoint) -> str:
    ranks = f"（{fmt_ranks(p.ranks)}）" if p.ranks else ""
    return f"- {label}: {_clean(p.text, max_len=200)}{ranks}"


def _conclusion_lines(surface: KwSurface) -> list[str]:
    c = surface.conclusion
    if c is None:
        return ["結論: なし（分析を行わなかった）"]
    note = "（AI の読みを作れず、集計だけの見出し）" if c.generated_by == "rule" else ""
    lines = [f"結論: {_clean(c.headline, max_len=200)}{note}"]
    if c.winning:
        lines.append(_point("勝ち筋", c.winning))
    if c.gap:
        lines.append(_point("空白", c.gap))
    lines += [_point("打ち手", a) for a in c.actions]
    if c.angles:
        angles = "／".join(
            f"「{_clean(a.text, max_len=60)}」{fmt_ranks(a.ranks, limit=4)}" for a in c.angles
        )
        lines.append(f"- 上位に共通する切り口: {angles}")
    return lines


def _facts_lines(facts: SurfaceFacts, *, keyword: str, client_name: str) -> list[str]:
    lines: list[str] = []
    if facts.categories:
        parts = [
            f"{category_label(c.category)} {c.count}本（再生の{fmt_pct(c.play_share)}）"
            for c in facts.categories
        ]
        lines.append("- 投稿者の構成: " + "／".join(parts))
    if facts.holders:
        parts = [
            f"{_clean(account_label(h.author, h.author_name), max_len=80)} {len(h.ranks)}枠"
            f"（{fmt_ranks(h.ranks)}）"
            for h in facts.holders[:5]
        ]
        lines.append("- 常連: " + "／".join(parts))
    elif facts.n:
        lines.append(f"- 常連: なし（{facts.n}本すべて別のアカウント）")
    if facts.tiers:
        parts = [f"{t.tier} {t.count}本（再生の{fmt_pct(t.play_share)}）" for t in facts.tiers]
        line = "- フォロワー帯: " + "／".join(parts)
        if facts.small_in_top10 is not None:
            line += f"。上位{facts.top10_n}本中 1万人未満は{facts.small_in_top10}本"
        lines.append(line)
    if facts.median_save_rate_pct is not None:
        line = f"- 保存率の中央値 {facts.median_save_rate_pct:g}%"
        if facts.save_leaders:
            top = facts.save_leaders[0]
            who = _clean(f"@{top.author}", max_len=64)
            line += f"（最高は{top.rank}位 {who} {top.save_rate_pct:g}%）"
        lines.append(line)
    timing: list[str] = []
    if facts.median_age_days is not None:
        timing.append(f"中央値 {fmt_age(facts.median_age_days)}")
    if facts.recent_90d is not None:
        timing.append(f"直近90日 {facts.recent_90d}本")
    if timing:
        lines.append("- 投稿時期: " + "／".join(timing))
    if facts.kw_in_text is not None:
        lines.append(
            f"- 本文かタグに「{_clean(keyword, max_len=60)}」の語をすべて含む: "
            f"{facts.kw_in_text}/{facts.n}本"
        )
    if facts.pr_ranks:
        lines.append(f"- PR表記あり: {fmt_ranks(facts.pr_ranks)}")
    if facts.client_ranks:
        lines.append(f"- クライアントの投稿: {fmt_ranks(facts.client_ranks)}に在圏")
    # 取引先のノートに付く記録なので、触れた投稿が無いことも残す（「無かった」も事実）。
    name = _clean(client_name, max_len=60)
    mentioned = fmt_ranks(facts.mention_ranks) if facts.mention_ranks else "無し"
    lines.append(f"- 「{name}」に触れた投稿: {mentioned}")
    return lines


def _post_line(surface: KwSurface, index: int) -> str:
    p = surface.posts[index]
    who = _clean(f"@{p.author}", max_len=64) if p.author else "不明"
    url = _safe_provenance_url(safe_href(p.url) or "")
    src = f" 〈{url}〉" if url else ""
    return f"- {p.rank}位 {who}（{category_label(p.category)}） {fmt_count(p.play_count)}回{src}"


def build_surface_summary_md(
    out: SearchSurfaceCheckOutput,
    *,
    client_name: str,
    measured_epoch: int,
    missing: list[tuple[str, str]],
) -> str:
    """検索上位チェックの要約 markdown。

    KW・媒体・実測日・結論・主要な集計・上位 10 本・レポート URL を載せる。
    """
    kws = "・".join(f"「{_clean(k, max_len=60)}」" for k in out.keywords)
    lines = [
        f"# {_clean(client_name, max_len=120)} 検索上位チェック",
        "",
        f"KW: {kws}／媒体: {'・'.join(_platforms(out))}／実測 {fmt_date(measured_epoch)}",
    ]
    for keyword, platform in missing:
        lines.append(
            f"取得できなかった面: {PLATFORM_LABEL.get(platform, platform)}"
            f"「{_clean(keyword, max_len=60)}」"
        )
    for s in out.surfaces:
        platform = PLATFORM_LABEL.get(s.platform, s.platform)
        lines += ["", f"## 「{_clean(s.keyword, max_len=60)}」{platform} 上位{len(s.posts)}本", ""]
        lines += _conclusion_lines(s)
        if s.facts is not None:
            lines += ["", "### 主要な集計"]
            lines += _facts_lines(s.facts, keyword=s.keyword, client_name=client_name)
        if s.posts:
            lines += ["", f"### 上位{min(_TOP_N, len(s.posts))}本（順位・@ID・タイプ・再生・URL）"]
            lines += [_post_line(s, i) for i in range(min(_TOP_N, len(s.posts)))]
    report = _safe_provenance_url(out.report_url or "")
    if report:
        lines += ["", f"レポート（署名URL・7日有効。期限後もこのノートに要点が残る）: 〈{report}〉"]
    lines.append(_FOOTER)
    return "\n".join(lines)


__all__ = ["build_surface_summary_md", "build_surface_title"]
