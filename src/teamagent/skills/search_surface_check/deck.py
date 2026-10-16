"""検索上位チェック（1 段目）→ 営業が直せる PowerPoint の DeckSpec（mcp 側・python-pptx 不使用）。

HTML のレポート（report.py）と同じ事実データ（SurfaceFacts・SurfaceConclusion・投稿一覧）から
直接組む。HTML は経由しない。描画は media worker 側（``teamagent.media.deck_render``）。

構成（1 KW×1 媒体の要点版 8 枚・全体版は付録を足す）:
    SS-01 表紙 / SS-04 結論 / SS-05 自社・競合の名簿（名簿か言及があるときだけ）/ SS-06 数字 /
    SS-07 投稿者の構成（100% 積み上げ横棒・タイプが分かるときだけ）/ SS-08 上位 5 本の動画 /
    SS-09 上位 10 本の表 / SS-10 切り口（照合済みの読みがあるときだけ）/ 付録 SS-11〜SS-13
KW×媒体が 2 つ以上なら SS-02（比べる表）を結論の前に置き、SS-04〜SS-10 を面ごとに繰り返す。

見出しは言い切り 1 行 34 字まで。照合済みの AI 見出しが無い・34 字超のときはコードが事実から
定型文で作る。中身の無いページは出さない。取れなかった値は 0 ではなく「未計測」。
"""

from __future__ import annotations

import datetime as _dt
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from teamagent.media.deck_contracts import (
    MAX_DECK_IMAGES,
    MAX_DECK_SLIDES,
    Fill,
    TextFill,
    ThemeColor,
)
from teamagent.skills._deck.layouts import (
    APPENDIX_ROWS,
    APPENDIX_TABLE_PT,
    CARDS_MAX,
    CELL_MAX,
    L_APPENDIX,
    L_CARDS,
    L_CHART,
    L_CONCLUSION,
    L_COVER,
    L_NUMBERS,
    L_TABLE,
    MAIN_TABLE_ROWS,
    POINTS_MAX,
    SUB_MAX,
    TABLE_PT,
    TABLE_PT_FEW_ROWS,
    TITLE_MAX,
)
from teamagent.skills._deck.lint import duplication_problems
from teamagent.skills._deck.spec import (
    SOURCE_AI_GROUNDED,
    SOURCE_AI_GUESS,
    SOURCE_COUNTED,
    SOURCE_FETCHED,
    UNMEASURED,
    ChartSeries,
    CsvSpec,
    DeckBuilder,
    DeckBuildError,
    DeckProperties,
    DeckSpec,
    Notes,
    PictureFill,
    cell,
    chart_fill,
    fit_body,
    line_fill,
    para,
    report_deck_enabled,
    run,
    table_fill,
    text_fill,
)
from teamagent.skills._deck.text import clean, fit_text, wording_problems
from teamagent.skills.search_surface_check.conclusion import rule_conclusion
from teamagent.skills.search_surface_check.display import (
    JST,
    PLATFORM_LABEL,
    category_label,
    fmt_count,
    fmt_date,
    fmt_duration,
)
from teamagent.skills.search_surface_check.insights import (
    compute_facts,
    is_pr_post,
    mentions,
)
from teamagent.skills.search_surface_check.schema import (
    ConclusionPoint,
    KwSurface,
    SurfaceConclusion,
    SurfaceFacts,
    SurfacePost,
)

UNMEASURED_NOTE = "未計測（0 件ではない）"
TYPE_NOTE = "タイプは投稿者のプロフィールと投稿から推定（未照合）"
PR_NOTE = "PR は #PR・#提供 等の表記があるもの。表記が無いことは広告でないことの証明ではない"
_SECTION_COVER = "表紙"
_SECTION_APPENDIX = "付録"
# 色: 注目する 1 本だけ accent1。ほかはグレーの濃淡（3 色以上のアクセントは使わない）。
_OTHER_COLORS: tuple[ThemeColor, ...] = ("accent4", "accent5", "accent3", "accent6")


@dataclass
class SurfaceDeck:
    spec: DeckSpec
    images: dict[str, bytes] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)  # 出さなかったページ（理由つき）


def _measured(facts_epoch: int) -> tuple[str, str]:
    when = _dt.datetime.fromtimestamp(facts_epoch, JST)
    return when.strftime("%Y-%m-%d"), when.strftime("%Y-%m-%d %H:%M JST")


def _platform(surface: KwSurface) -> str:
    return PLATFORM_LABEL.get(surface.platform, surface.platform)


def _is_ig(surface: KwSurface) -> bool:
    return surface.platform == "instagram"


def _order_label(surface: KwSurface) -> str:
    return "出現回数×再生（無ければいいね）の順" if _is_ig(surface) else "検索の表示順のまま"


def _play_median(surface: KwSurface) -> str:
    plays = [p.play_count for p in surface.posts if p.play_count > 0]
    return f"{fmt_count(int(statistics.median(plays)))}回" if plays else UNMEASURED


def _play_basis(surface: KwSurface) -> str:
    n = sum(p.play_count > 0 for p in surface.posts)
    return f"再生が取れた {n} 本の中央値"


def _normalize_handle(account: str) -> str:
    return account.strip().lstrip("@").lower()


def condition_line(surface: KwSurface, measured_epoch: int) -> str:
    day, _ = _measured(measured_epoch)
    return (
        f"「{surface.keyword}」で検索した上位 {len(surface.posts)} 本／取得日 {day}／"
        f"{_platform(surface)}・未ログイン"
        + ("（出現回数×エンゲージ順）" if _is_ig(surface) else "")
    )


def _save_rate(post: SurfacePost) -> float | None:
    if post.platform == "instagram" or post.play_count <= 0:
        return None
    return post.save_count / post.play_count * 100


def _fmt_rate(rate: float | None) -> str:
    if rate is None:
        return UNMEASURED
    return f"{rate:.2f}%" if rate < 1 else f"{rate:.1f}%"


def _ranks_text(ranks: Sequence[int], limit: int = 4) -> str:
    shown = "・".join(str(r) for r in ranks[:limit])
    return f"{shown} 位" + (f" ほか {len(ranks) - limit} 本" if len(ranks) > limit else "")


def _first_fit(*candidates: str) -> str:
    for text in candidates:
        if text and len(text) <= TITLE_MAX:
            return text
    shown, _ = fit_text(candidates[-1], TITLE_MAX)
    return shown


def _handle(post: SurfacePost) -> str:
    return f"@{post.author}" if post.author else UNMEASURED


def _clip(text: str, limit: int = CELL_MAX) -> str:
    shown, _ = fit_text(clean(text), limit)
    return shown


def _ai_headline(conclusion: SurfaceConclusion | None, facts: SurfaceFacts) -> str | None:
    """照合済みの AI 見出し（34 字以内）。集計の見出しで埋めたもの・無いものは None。"""
    if conclusion is None or conclusion.generated_by != "llm" or not conclusion.headline:
        return None
    fallback = rule_conclusion(facts)
    if fallback is not None and conclusion.headline == fallback.headline:
        return None
    headline = clean(conclusion.headline)
    return headline if len(headline) <= TITLE_MAX and not wording_problems(headline) else None


def _rule_headline(surface: KwSurface, facts: SurfaceFacts) -> str:
    holder = facts.holders[0] if facts.holders else None
    candidates: list[str] = []
    if holder is not None and len(holder.ranks) >= 3:
        candidates.append(f"上位 {facts.n} 本のうち {len(holder.ranks)} 本は「@{holder.author}」")
    candidates.append("検索上位の投稿と自社の位置")
    return _first_fit(*candidates)


# ── ページ ────────────────────────────────────────────────────────────────────


@dataclass
class _Ctx:
    builder: DeckBuilder
    client_name: str | None
    client_accounts: list[str]
    competitor_accounts: list[str]
    measured_epoch: int
    covers: Mapping[str, bytes]
    dropped: list[str]


def _image_name(post: SurfacePost, surface_no: int) -> str:
    return f"cover_s{surface_no}_r{post.rank}"


def _sid(base: int, suffix: str) -> str:
    return f"SS-{base:02d}{suffix}"


def _common(slide_id: str, surface: KwSurface, ctx: _Ctx, source: str) -> list[Fill]:
    return [
        line_fill("condition", condition_line(surface, ctx.measured_epoch), f"{slide_id}｜条件"),
        line_fill("source", source, f"{slide_id}｜出どころ"),
    ]


def _sources_of(
    posts: Sequence[SurfacePost], measured_at: str, surface: KwSurface, limit: int = 10
) -> list[str]:
    out = [f"{p.rank} 位 {_handle(p)}: {p.url}" for p in posts[:limit] if p.url]
    out.append(f"取得時点 {measured_at}（{_order_label(surface)}・未ログイン）")
    return out


def _cover_slide(surfaces: Sequence[KwSurface], ctx: _Ctx, addressee: str | None) -> str:
    first = surfaces[0]
    kws = list(dict.fromkeys(s.keyword for s in surfaces))
    platforms = "・".join(dict.fromkeys(_platform(s) for s in surfaces))
    if len(surfaces) == 1:
        title = _first_fit(
            f"「{first.keyword}」{platforms} 検索 上位 {len(first.posts)} 本の顔ぶれ",
            f"「{first.keyword}」{platforms} 検索 上位の顔ぶれ",
            f"{platforms} 検索 上位 {len(first.posts)} 本の顔ぶれ",
        )
    else:
        joined = "".join(f"「{k}」" for k in kws)
        title = _first_fit(
            f"{joined}{platforms} 検索 上位の顔ぶれ",
            f"{platforms} 検索 上位の顔ぶれ（{len(kws)} 語）",
        )
    _, measured_at = _measured(ctx.measured_epoch)
    fills: list[Fill] = [
        line_fill("condition", condition_line(first, ctx.measured_epoch), "SS-01｜条件"),
    ]
    if addressee:
        fills.append(line_fill("addressee", _clip(addressee, SUB_MAX), "SS-01｜宛名"))
    top = first.posts[0] if first.posts else None
    image = None
    if top is not None:
        image = ctx.builder.add_image(
            _image_name(top, 1 if len(surfaces) > 1 else 0), ctx.covers.get(top.url)
        )
    fills.append(
        PictureFill(
            box="cover",
            image=image,
            alt_text=f"{top.rank} 位 {_handle(top)} 表紙" if top else "表紙",
            link=top.url if top and top.url.startswith("https://") else None,
            shape_name="SS-01｜表紙の画像",
        )
    )
    notes = Notes(
        what="検索の条件と、資料の対象（どの語・どの媒体・何本）を示す表紙。",
        talk=[f"「{s.keyword}」{_platform(s)}：{_order_label(s)}で数えた" for s in surfaces],
        sources=[f"取得時点 {measured_at}"],
    )
    slide = ctx.builder.add_slide(
        slide_id="SS-01",
        layout=L_COVER,
        section=_SECTION_COVER,
        title=title,
        fills=fills,
        notes=notes,
    )
    # 表紙だけは検索語の直後で改行し、末尾の数文字だけが次行へ落ちるのを防ぐ。
    # 題の文字数・文言・プロパティは同じまま、表示上の段落だけ分ける。
    if len(surfaces) == 1 and title.startswith(f"「{first.keyword}」"):
        split = title.index("」") + 1
        cover_title = text_fill(
            "title",
            [para(title[:split], bullet=False), para(title[split:].lstrip(), bullet=False)],
            "SS-01｜題",
        )
        ctx.builder.slides[-1] = slide.model_copy(
            update={"fills": tuple(cover_title if f.box == "title" else f for f in slide.fills)}
        )
    return title


def _compare_slide(surfaces: Sequence[KwSurface], ctx: _Ctx) -> None:
    rows = []
    for s in surfaces:
        facts = s.facts or compute_facts(
            s.posts, keyword=s.keyword, client_name=ctx.client_name, now_epoch=ctx.measured_epoch
        )
        known = [c for c in facts.categories if c.category != "unknown"]
        rows.append(
            [
                cell(_clip(s.keyword)),
                cell(_platform(s)),
                cell(f"{len(s.posts)} 本", align="r"),
                cell(
                    f"{category_label(known[0].category)} {known[0].count} 本"
                    if known
                    else UNMEASURED
                ),
                cell(_play_median(s), align="r"),
                cell(_ranks_text(s.client_ranks) if s.client_ranks else "無し"),
            ]
        )
    title = _first_fit(
        f"{len(surfaces)} つの検索面を同じ物差しで比べる",
    )
    first = surfaces[0]
    _, measured_at = _measured(ctx.measured_epoch)
    ctx.builder.add_slide(
        slide_id="SS-02",
        layout=L_TABLE,
        section="比べる",
        title=title,
        fills=[
            *_common("SS-02", first, ctx, SOURCE_COUNTED),
            table_fill(
                "table",
                ["検索語", "媒体", "本数", "最多のタイプ（推定）", "再生の中央値", "自社の順位"],
                rows,
                font_pt=TABLE_PT_FEW_ROWS if len(rows) <= MAIN_TABLE_ROWS else TABLE_PT,
                shape_name="SS-02｜検索面の比べ",
                col_weights=(2.2, 1.1, 0.9, 2.0, 1.4, 1.6),
                numeric_cols=(2, 4),
            ),
            line_fill(
                "note",
                "条件の行は 1 つ目の検索面。ほかの面の条件は各ページの条件の行",
                "SS-02｜注記",
            ),
        ],
        notes=Notes(
            what="検索語×媒体ごとに、本数・最多のタイプ・再生の中央値・自社の順位を並べた。",
            talk=[f"{s.keyword}（{_platform(s)}）" for s in surfaces],
            sources=[f"取得時点 {measured_at}"],
        ),
    )


def _point_text(label: str, point: ConclusionPoint, limit: int, full: list[str]) -> tuple[str, str]:
    body = fit_body(point.text, limit, full)
    basis = f"（根拠 {_ranks_text(point.ranks, 3)}）" if point.ranks else ""
    return label, body + basis


def _rule_points(
    surface: KwSurface, facts: SurfaceFacts, ctx: _Ctx, *, title: str = ""
) -> list[tuple[str, str, list[int]]]:
    """AI の結論が無いときの本文。事実から言えることだけ（打ち手は書かない）。

    題で言った常連（@handle）は本文で繰り返さない。
    """
    posts = surface.posts
    out: list[tuple[str, str, list[int]]] = []
    played = [p for p in posts if p.play_count > 0]
    if played:
        top = max(played, key=lambda p: p.play_count)
        out.append(
            (
                "最も見られている",
                f"再生が最も多いのは {top.rank} 位の「{_handle(top)}」"
                "。数字は主要な数字と上位の一覧を参照",
                [top.rank],
            )
        )
    if facts.holders and f"@{facts.holders[0].author}" not in title:
        h = facts.holders[0]
        out.append(
            (
                "常連",
                f"「@{h.author}」が上位 {len(posts)} 本のうち {len(h.ranks)} 枠を持つ",
                h.ranks[:3],
            )
        )
    if ctx.client_name:
        if facts.client_ranks:
            out.append(
                (
                    "自社",
                    f"「{ctx.client_name}」の投稿は {_ranks_text(facts.client_ranks)}",
                    facts.client_ranks[:3],
                )
            )
        else:
            mention = (
                f"。名前が出る投稿は {_ranks_text(facts.mention_ranks)}"
                if facts.mention_ranks
                else "。名前が出る投稿も無い"
            )
            out.append(
                (
                    "空白",
                    f"「{ctx.client_name}」の公式の投稿は上位 {len(posts)} 本に無い{mention}",
                    facts.mention_ranks[:3],
                )
            )
    if len(out) < POINTS_MAX and facts.save_leaders:
        lead = facts.save_leaders[0]
        out.append(
            (
                "保存されている",
                f"保存率が最も高いのは {lead.rank} 位の「@{lead.author}」"
                "（数字は上位 10 本の表を参照）",
                [lead.rank],
            )
        )
    return out[:POINTS_MAX]


def _conclusion_slide(
    surface: KwSurface, facts: SurfaceFacts, ctx: _Ctx, suffix: str, section: str
) -> None:
    sid = _sid(4, suffix)
    conclusion = surface.conclusion
    ai_title = _ai_headline(conclusion, facts)
    ai_points: list[tuple[str, ConclusionPoint]] = []
    if conclusion is not None and conclusion.generated_by == "llm":
        if conclusion.winning:
            ai_points.append(("勝ち筋", conclusion.winning))
        if conclusion.gap:
            ai_points.append(("空白", conclusion.gap))
        if conclusion.actions:
            ai_points.append(("打ち手", conclusion.actions[0]))
    full: list[str] = []
    if conclusion and conclusion.headline and wording_problems(conclusion.headline):
        ctx.dropped.append(f"{sid} AI の題: 言い方の検査で省略")
    safe_points = []
    for label, point in ai_points:
        if wording_problems(point.text):
            ctx.dropped.append(f"{sid} AI の文（{label}）: 言い方の検査で省略")
        else:
            safe_points.append((label, point))
    ai_points = safe_points
    per_point = 100
    paragraphs = []
    evidence: list[str] = []
    talk: list[str] = []
    if ai_points:
        source = SOURCE_AI_GROUNDED
        for label, point in ai_points[:POINTS_MAX]:
            lab, body = _point_text(label, point, per_point, full)
            paragraphs.append(para(run(f"{lab}：", bold=True), run(body)))
            ctx.builder.author(body)
            talk.append(f"{lab}: {body}")
            evidence.append(f"{lab}: #{'・#'.join(str(r) for r in point.ranks) or 'なし'}")
    title = ai_title or _rule_headline(surface, facts)
    rule_points = [] if ai_points else _rule_points(surface, facts, ctx, title=title)
    if not ai_points:
        source = SOURCE_COUNTED
        for label, text, ranks in rule_points:
            body = fit_body(text, per_point, full)
            basis = f"（根拠 {_ranks_text(ranks, 3)}）" if ranks else ""
            paragraphs.append(para(run(f"{label}：", bold=True), run(body + basis)))
            ctx.builder.author(body)
            talk.append(f"{label}: {body}")
            evidence.append(f"{label}: #{'・#'.join(str(r) for r in ranks) or 'なし'}")
    if not paragraphs:
        ctx.dropped.append(f"{sid} 結論: 言えることが無い")
        return
    by_rank = {p.rank: p for p in surface.posts}
    cited = sorted(
        {r for pt in ai_points for r in pt[1].ranks} | {r for _, _, rs in rule_points for r in rs}
    )
    _, measured_at = _measured(ctx.measured_epoch)
    ctx.builder.add_slide(
        slide_id=sid,
        layout=L_CONCLUSION,
        section=section,
        title=title,
        fills=[
            *_common(sid, surface, ctx, source),
            text_fill("body", paragraphs, f"{sid}｜結論の 3 点"),
        ],
        notes=Notes(
            what=(
                "検索上位の数え上げから、勝ち筋・空白・打ち手を 1 点ずつ出している。"
                if ai_points
                else "検索上位の数え上げ（集計）から、事実として言えることだけを出している。"
                "打ち手は書いていない。"
            ),
            talk=talk,
            evidence=evidence,
            sources=_sources_of([by_rank[r] for r in cited if r in by_rank], measured_at, surface),
            full_text=full,
        ),
    )


def _roster_slide(
    surface: KwSurface, facts: SurfaceFacts, ctx: _Ctx, suffix: str, section: str
) -> None:
    sid = _sid(5, suffix)
    posts = surface.posts
    roster = {k: "自社" for a in ctx.client_accounts if (k := _normalize_handle(a))}
    roster.update({k: "競合" for a in ctx.competitor_accounts if (k := _normalize_handle(a))})
    rows_data: list[tuple[str, str, str, str, str, str]] = []
    seen: set[str] = set()
    for p in posts:
        kind = (
            "自社"
            if p.is_client
            else "競合"
            if p.is_competitor
            else roster.get(_normalize_handle(p.author))
        )
        named = p.mentions_client or mentions(p, ctx.client_name)
        if kind is None and not named:
            continue
        seen.add(_normalize_handle(p.author))
        rows_data.append(
            (
                _handle(p),
                kind or "言及",
                f"{p.rank} 位",
                "あり" if named else "",
                "あり" if (p.is_pr or is_pr_post(p)) else "",
                p.url,
            )
        )
    for account, kind in roster.items():
        if account not in seen:
            rows_data.append(
                (f"@{account}", kind, f"圏外（上位 {len(posts)} 本に無し）", "", "", "")
            )
    if not rows_data:
        ctx.dropped.append(f"{sid} 名簿: 自社・競合の名簿も自社名の言及も無い")
        return
    client = f"「{ctx.client_name}」" if ctx.client_name else "自社"
    own = [r for r in rows_data if r[1] == "自社" and not r[2].startswith("圏外")]
    rival = [r for r in rows_data if r[1] == "競合" and not r[2].startswith("圏外")]
    named_rows = [r for r in rows_data if r[3] == "あり"]
    if own:
        own_ranks = [int(r[2].split()[0]) for r in own]
        head = f"{client} は {_ranks_text(own_ranks)}に {len(own)} 本"
        title = _first_fit(
            f"{head}、競合は {len(rival)} 本" if ctx.competitor_accounts else head, head
        )
    elif named_rows:
        title = _first_fit(
            f"{client} 公式は圏外、名前が出るのは {len(named_rows)} 本",
            f"{client} の名前が出るのは {len(named_rows)} 本",
        )
    else:
        title = _first_fit(
            f"{client} も競合も上位 {len(posts)} 本に出ていない",
            f"{client} は上位 {len(posts)} 本に出ていない",
        )
    shown = rows_data[:MAIN_TABLE_ROWS]
    rest = rows_data[MAIN_TABLE_ROWS:]
    rows = [
        [
            cell(_clip(r[0]), link=r[5] if r[5].startswith("https://") else None),
            cell(r[1]),
            cell(r[2]),
            cell(r[3]),
            cell(r[4]),
        ]
        for r in shown
    ]
    _, measured_at = _measured(ctx.measured_epoch)
    note = "言及＝本文かタグに自社名が出る投稿。" + ("残りはノート" if rest else PR_NOTE)
    ctx.builder.add_slide(
        slide_id=sid,
        layout=L_TABLE,
        section=section,
        title=title,
        fills=[
            *_common(sid, surface, ctx, SOURCE_FETCHED),
            table_fill(
                "table",
                ["アカウント", "区分", "順位", "言及", "PR"],
                rows,
                font_pt=TABLE_PT_FEW_ROWS,
                shape_name=f"{sid}｜自社・競合の名簿",
                col_weights=(3.0, 1.2, 2.6, 1.0, 1.0),
            ),
            line_fill("note", _clip(note, SUB_MAX), f"{sid}｜注記"),
        ],
        notes=Notes(
            what="自社・競合の名簿と、本文に自社名が出る投稿が、検索上位の何位にいるかを並べた。",
            talk=[f"{r[0]}（{r[1]}）: {r[2]}" for r in rows_data],
            evidence=[f"#{r[2]} {r[0]}" for r in rows_data if not r[2].startswith("圏外")],
            sources=[f"{r[0]}: {r[5]}" for r in rows_data if r[5]] + [f"取得時点 {measured_at}"],
            full_text=["（表に入らなかった行）\n" + "\n".join(" ／ ".join(r[:5]) for r in rest)]
            if rest
            else [],
        ),
    )


def _numbers_slide(
    surface: KwSurface, facts: SurfaceFacts, ctx: _Ctx, suffix: str, section: str
) -> None:
    sid = _sid(6, suffix)
    posts = surface.posts
    ig = _is_ig(surface)
    small: list[tuple[str, str]] = []
    if facts.small_in_top10 is not None:
        big = (
            f"{facts.small_in_top10} 本",
            f"上位 {facts.top10_n} 本のうち、フォロワー 1 万人未満のアカウントの投稿",
        )
        title = f"上位 {facts.top10_n} 本中、フォロワー 1 万未満が {facts.small_in_top10} 本"
        small.append((_play_median(surface), _play_basis(surface)))
    elif any(p.play_count > 0 for p in posts):
        big = (_play_median(surface), _play_basis(surface))
        title = f"{_play_basis(surface)}は {_play_median(surface)}"
    else:
        big = (UNMEASURED, "再生の中央値（0 件ではない）")
        title = "主要な数字と取得できた指標"
    if ig or facts.median_save_rate_pct is None:
        small.append(
            (UNMEASURED, f"保存率の中央値（{_platform(surface)}では取れない・0 件ではない）")
        )
    else:
        small.append((_fmt_rate(facts.median_save_rate_pct), "保存率の中央値（保存÷再生）"))
    if facts.recent_90d is not None:
        small.append((f"{facts.recent_90d} 本", f"上位 {len(posts)} 本のうち直近 90 日の投稿"))
    if len(small) < 3 and facts.reach_ratio_median is not None:
        small.append((f"{facts.reach_ratio_median} 倍", "再生÷フォロワーの中央値"))
    if len(small) < 3 and facts.median_duration_sec:
        small.append((fmt_duration(facts.median_duration_sec), "動画の長さの中央値"))
    if ig:
        likes = [p.like_count for p in posts]
        small.extend(
            [
                (fmt_count(int(statistics.median(likes))), "いいねの中央値"),
                (f"{sum(p.appearances for p in posts)} 回", "検索結果への出現回数の合計"),
            ]
        )
    while len(small) < 3:
        small.append((UNMEASURED, "取得できない指標（0 件ではない）"))
    fills: list[Fill] = [
        *_common(sid, surface, ctx, SOURCE_COUNTED),
        line_fill("big_number", big[0], f"{sid}｜大きな数字"),
        line_fill("big_label", big[1], f"{sid}｜大きな数字のラベル"),
    ]
    for i, (value, label) in enumerate(small[:3], start=1):
        fills.append(line_fill(f"small_number_{i}", value, f"{sid}｜小さな数字 {i}"))
        fills.append(line_fill(f"small_label_{i}", _clip(label, SUB_MAX), f"{sid}｜ラベル {i}"))
    _, measured_at = _measured(ctx.measured_epoch)
    top10 = [p for p in posts if p.rank <= 10]
    ctx.builder.add_slide(
        slide_id=sid,
        layout=L_NUMBERS,
        section=section,
        title=_first_fit(title, f"上位 {len(posts)} 本の主要な数字"),
        fills=fills,
        notes=Notes(
            what="検索上位の主要な数字を 4 つ。どれも投稿の取得値を数えたもの（集計）。",
            talk=[f"{big[1]}: {big[0]}"] + [f"{label}: {value}" for value, label in small[:3]],
            evidence=[
                f"#{p.rank} {_handle(p)} フォロワー {p.author_followers:,}"
                for p in top10
                if 0 < p.author_followers < 10_000
            ],
            sources=[f"取得時点 {measured_at}", "フォロワーは取得した時点の値"],
        ),
    )


def _composition_slide(
    surface: KwSurface, facts: SurfaceFacts, ctx: _Ctx, suffix: str, section: str
) -> None:
    sid = _sid(7, suffix)
    known = [c for c in facts.categories if c.category != "unknown"]
    if not known:
        ctx.dropped.append(f"{sid} 投稿者の構成: タイプが分からない（分類していない）")
        return
    cats = facts.categories
    by_count = max(known, key=lambda c: c.count)
    by_play = max(known, key=lambda c: c.play_share)
    has_plays = any(p.play_count > 0 for p in surface.posts)
    series = [
        ChartSeries(
            name=category_label(c.category),
            values=(c.count_share, c.play_share) if has_plays else (c.count_share,),
            color="accent1" if c is by_count else _OTHER_COLORS[i % len(_OTHER_COLORS)],
        )
        for i, c in enumerate(cats)
    ]

    a, b = category_label(by_count.category), category_label(by_play.category)
    title = f"本数は{a}、再生は{b}が最多" if has_plays else f"本数は{a}が最多（再生は未計測）"
    title = _first_fit(title, "投稿者のタイプ別の構成")
    reading = [
        para(run("本数：", bold=True), run(f"{a}が最多")),
        para(run("再生：", bold=True), run(f"{b}が最多" if has_plays else UNMEASURED_NOTE)),
        para(run("注意：", bold=True), run(TYPE_NOTE)),
    ]
    _, measured_at = _measured(ctx.measured_epoch)
    ctx.builder.add_slide(
        slide_id=sid,
        layout=L_CHART,
        section=section,
        title=title,
        fills=[
            *_common(sid, surface, ctx, SOURCE_AI_GUESS),
            chart_fill(
                "chart",
                chart_type="bar_stacked_100",
                categories=["本数", "再生"] if has_plays else ["本数"],
                series=series,
                shape_name=f"{sid}｜投稿者の構成",
                number_format="0%",
                legend=True,
            ),
            text_fill("reading", reading, f"{sid}｜読み方"),
        ],
        notes=Notes(
            what="投稿者のタイプ別に、本数の割合と再生の割合を 100% 積み上げで比べた。",
            talk=[
                f"{a} が本数の {round(by_count.count_share * 100)}%",
                f"再生は {b} が {round(by_play.play_share * 100)}%"
                if has_plays
                else f"再生：{UNMEASURED_NOTE}",
            ],
            evidence=[
                f"{category_label(c.category)}: {c.count} 本（{round(c.count_share * 100)}%）・"
                + (f"再生の {round(c.play_share * 100)}%" if has_plays else f"再生：{UNMEASURED}")
                for c in cats
            ],
            sources=[f"取得時点 {measured_at}", TYPE_NOTE, "数字は付録の表と CSV にも残している"],
        ),
    )


def _cards_slide(surface: KwSurface, ctx: _Ctx, suffix: str, section: str) -> None:
    sid = _sid(8, suffix)
    top = sorted(surface.posts, key=lambda p: p.rank)[:CARDS_MAX]
    if not top:
        ctx.dropped.append(f"{sid} 動画カード: 投稿が無い")
        return
    fills: list[Fill] = list(_common(sid, surface, ctx, SOURCE_FETCHED))
    full: list[str] = []
    for i, p in enumerate(top, start=1):
        link = p.url if p.url.startswith("https://") else None
        image = ctx.builder.add_image(
            _image_name(p, int(suffix[1:]) if suffix else 0), ctx.covers.get(p.url)
        )
        fills.append(
            PictureFill(
                box=f"card_{i}",
                image=image,
                alt_text=f"{p.rank} 位 {_handle(p)} 表紙",
                link=link,
                shape_name=f"{sid}｜{p.rank} 位の表紙",
            )
        )
        lines = [
            para(run(f"{p.rank} 位 {_handle(p)}", bold=True), bullet=False),
            para(
                run(f"再生 {fmt_count(p.play_count)}" if p.play_count else f"再生 {UNMEASURED}"),
                bullet=False,
            ),
            para(run(f"保存率 {_fmt_rate(_save_rate(p))}"), bullet=False),
        ]
        if link:
            lines.append(para(run("元投稿を開く", link=link), bullet=False))
        fills.append(text_fill(f"caption_{i}", lines, f"{sid}｜{p.rank} 位の説明"))
        if p.desc:
            full.append(f"{p.rank} 位 {_handle(p)}: {p.desc}")
    played = [p for p in top if p.play_count > 0]
    if played and not _is_ig(surface):
        best = max(played, key=lambda p: p.play_count)
        title = _first_fit(
            f"上位 {len(top)} 本の再生最多は {best.rank} 位（{fmt_count(best.play_count)}回）",
            f"上位 {len(top)} 本の動画",
        )
    else:
        title = f"上位 {len(top)} 本の動画"
    _, measured_at = _measured(ctx.measured_epoch)
    ctx.builder.add_slide(
        slide_id=sid,
        layout=L_CARDS,
        section=section,
        title=title,
        fills=fills,
        notes=Notes(
            what=f"{_order_label(surface)}で上から {len(top)} 本の表紙と数字。表紙と「元投稿を開く」から元の投稿へ飛べる。",  # noqa: E501
            talk=[
                f"{p.rank} 位 {_handle(p)}: "
                f"再生 {fmt_count(p.play_count) if p.play_count else UNMEASURED}・"
                f"保存率 {_fmt_rate(_save_rate(p))}"
                for p in top
            ],
            sources=_sources_of(top, measured_at, surface),
            full_text=full,
        ),
    )


def _top_table_slide(surface: KwSurface, ctx: _Ctx, suffix: str, section: str) -> None:
    sid = _sid(9, suffix)
    top = sorted(surface.posts, key=lambda p: p.rank)[:10]
    if not top:
        return
    ig = _is_ig(surface)
    has_type = any(p.category != "unknown" for p in top)
    columns: list[tuple[str, float, bool]] = [
        ("順位", 0.7, True),
        ("アカウント（元投稿）", 2.8, False),
    ]
    if has_type:
        columns.append(("タイプ（推定）", 1.5, False))
    if not ig:
        columns.append(("フォロワー", 1.2, True))
    columns.append(("再生", 1.1, True))
    if not ig:
        columns.append(("保存率", 1.0, True))
    columns += [("出現回数", 1.0, True)] if ig else [("投稿日", 1.4, False)]
    columns.append(("PR", 0.7, False))
    rows = []
    for p in top:
        row = [
            cell(str(p.rank)),
            cell(_clip(_handle(p)), link=p.url if p.url.startswith("https://") else None),
        ]
        if has_type:
            row.append(cell(category_label(p.category)))
        if not ig:
            row.append(cell(f"{p.author_followers:,}" if p.author_followers > 0 else UNMEASURED))
        row.append(cell(f"{p.play_count:,}" if p.play_count > 0 else UNMEASURED))
        if not ig:
            row.append(cell(_fmt_rate(_save_rate(p))))
        row += [
            cell(f"{p.appearances} 回" if ig else fmt_date(p.posted_at) or UNMEASURED),
            cell("あり" if p.is_pr or is_pr_post(p) else ""),
        ]
        rows.append(row)
    numeric = tuple(i for i, c in enumerate(columns) if c[2])
    rates = [(p, r) for p in top if (r := _save_rate(p)) is not None and p.play_count >= 1_000]
    if rates:
        best, rate = max(rates, key=lambda pr: pr[1])
        title = _first_fit(
            f"上位 10 本で保存率が最も高いのは {best.rank} 位（{_fmt_rate(rate)}）",
            f"保存率の最高は {best.rank} 位（{_fmt_rate(rate)}）",
        )
        emphasis = [top.index(best)]
    else:
        title = f"上位 {len(top)} 本の一覧"
        emphasis = []
    note_parts = ["保存率＝保存÷再生。"]
    if ig:
        note_parts.append(
            f"フォロワー・保存率・投稿日・長さは {_platform(surface)}では取れないため列を外した"
            f"（{UNMEASURED_NOTE}）。"
        )
    if not has_type:
        note_parts.append("タイプは分類していないため列を外した。")
    note_parts.append("★は保存率が最も高い行（太字）。" if emphasis else "")
    note = "".join(note_parts)
    _, measured_at = _measured(ctx.measured_epoch)
    if emphasis:
        row = rows[emphasis[0]]
        row[0] = cell(f"★{row[0].text}", bold=True)
    ctx.builder.add_slide(
        slide_id=sid,
        layout=L_TABLE,
        section=section,
        title=title,
        fills=[
            *_common(sid, surface, ctx, SOURCE_FETCHED),
            table_fill(
                "table",
                [c[0] for c in columns],
                rows,
                font_pt=TABLE_PT,
                shape_name=f"{sid}｜上位 10 本",
                col_weights=[c[1] for c in columns],
                numeric_cols=numeric,
                emphasis_rows=emphasis,
            ),
            line_fill("note", fit_text(note, 90)[0], f"{sid}｜注記"),
        ],
        notes=Notes(
            what=(
                f"{_order_label(surface)}で上から 10 本の一覧（取得値）。"
                "アカウント名から元の投稿へ飛べる。"
            ),
            talk=[title],
            evidence=[
                "よく付くタグ: "
                + "、".join(
                    f"#{t.tag}（{t.count} 本）"
                    for t in (surface.facts.top_tags if surface.facts else [])
                    if t.tag.casefold().replace(" ", "")
                    != surface.keyword.casefold().replace(" ", "")
                ),
                *[
                    f"#{p.rank} {_handle(p)} "
                    f"再生 {fmt_count(p.play_count) if p.play_count else UNMEASURED}・"
                    f"保存 {UNMEASURED if ig else str(p.save_count)}"
                    for p in top
                ],
            ],
            sources=_sources_of(top, measured_at, surface),
            full_text=[f"{p.rank} 位 {_handle(p)}: {p.desc}" for p in top if p.desc],
        ),
    )


def _angles_slide(
    surface: KwSurface, facts: SurfaceFacts, ctx: _Ctx, suffix: str, section: str
) -> None:
    sid = _sid(10, suffix)
    conclusion = surface.conclusion
    candidates = list(conclusion.angles) if conclusion and conclusion.generated_by == "llm" else []
    angles = []
    for angle in candidates:
        if wording_problems(angle.text):
            ctx.dropped.append(f"{sid} AI の切り口: 言い方の検査で省略")
        elif len(set(angle.ranks)) >= 2:
            angles.append(angle)
    if not angles:
        ctx.dropped.append(f"{sid} 切り口: 照合済みの切り口が無い")
        return
    _, measured_at = _measured(ctx.measured_epoch)
    full: list[str] = []
    ctx.builder.author(*(a.text for a in angles[:4]))
    per_angle = 65 if len(angles) >= 4 else 100
    title = _first_fit(
        f"共通する切り口は「{clean(angles[0].text)}」",
        f"上位に共通する切り口は {len(angles[:4])} つ",
    )
    ctx.builder.add_slide(
        slide_id=sid,
        layout=L_CONCLUSION,
        section=section,
        title=title,
        fills=[
            *_common(sid, surface, ctx, SOURCE_AI_GROUNDED),
            text_fill(
                "body",
                [
                    para(f"{i}. {fit_body(a.text, per_angle, full)}（{_ranks_text(a.ranks, 3)}）")
                    for i, a in enumerate(angles[:4], 1)
                ],
                f"{sid}｜切り口",
            ),
        ],
        notes=Notes(
            what="上位の投稿に共通する切り口（2 本以上、順位は照合済み）。",
            evidence=[f"{a.text}: {_ranks_text(a.ranks, len(a.ranks))}" for a in angles[:4]],
            sources=[f"取得時点 {measured_at}"],
            full_text=full,
        ),
    )


def _appendix(surface: KwSurface, facts: SurfaceFacts, ctx: _Ctx, suffix: str) -> None:
    posts = sorted(surface.posts, key=lambda p: p.rank)
    ig = _is_ig(surface)
    _, measured_at = _measured(ctx.measured_epoch)
    sid = _sid(11, suffix)
    premises = [
        ("検索語", surface.keyword),
        ("媒体と状態", f"{_platform(surface)}・未ログイン（個人のおすすめが混ざらない状態）"),
        ("本数と並び", f"上位 {len(posts)} 本・{_order_label(surface)}"),
        ("取得時点", measured_at),
        ("保存率", "保存数÷再生数" if not ig else UNMEASURED_NOTE),
        ("タイプ", TYPE_NOTE),
        ("PR", PR_NOTE),
        ("未計測", "取れなかった値は 0 ではなく「未計測」と書く"),
    ]
    ctx.builder.add_slide(
        slide_id=sid,
        layout=L_APPENDIX,
        section=_SECTION_APPENDIX,
        title="付録：この資料の前提",
        fills=[
            *_common(sid, surface, ctx, SOURCE_COUNTED),
            table_fill(
                "table",
                ["項目", "内容"],
                [[cell(k), cell(fit_text(v, 60)[0])] for k, v in premises],
                font_pt=APPENDIX_TABLE_PT,
                shape_name=f"{sid}｜前提",
                col_weights=(1.6, 8.0),
            ),
        ],
        notes=Notes(what="数え方と言葉の定義。", talk=[f"{k}: {v}" for k, v in premises]),
    )
    columns: list[tuple[str, float, bool]] = [
        ("順位", 0.6, True),
        ("アカウント（元投稿）", 2.6, False),
    ]
    if not ig:
        columns.append(("フォロワー", 1.1, True))
    columns.append(("再生", 1.1, True))
    if not ig:
        columns.append(("保存率", 0.9, True))
    columns += [("出現回数", 1.0, True)] if ig else [("投稿日", 1.0, False), ("長さ", 0.7, True)]
    columns.append(("PR", 0.6, False))
    numeric = tuple(i for i, c in enumerate(columns) if c[2])
    for page, start in enumerate(range(0, len(posts), APPENDIX_ROWS), start=1):
        chunk = posts[start : start + APPENDIX_ROWS]
        rows = []
        for p in chunk:
            row = [
                cell(str(p.rank)),
                cell(_clip(_handle(p)), link=p.url if p.url.startswith("https://") else None),
            ]
            if not ig:
                row.append(
                    cell(f"{p.author_followers:,}" if p.author_followers > 0 else UNMEASURED)
                )
            row.append(cell(f"{p.play_count:,}" if p.play_count > 0 else UNMEASURED))
            if not ig:
                row.append(cell(_fmt_rate(_save_rate(p))))
            if ig:
                row.append(cell(f"{p.appearances} 回"))
            else:
                row += [
                    cell(fmt_date(p.posted_at) or UNMEASURED),
                    cell(fmt_duration(p.duration_sec) or UNMEASURED),
                ]
            row.append(cell("あり" if p.is_pr or is_pr_post(p) else ""))
            rows.append(row)
        psid = f"SS-12{suffix}-{page}" if suffix else f"SS-12-{page}"
        ctx.builder.add_slide(
            slide_id=psid,
            layout=L_APPENDIX,
            section=_SECTION_APPENDIX,
            title=f"付録：全件の一覧（{chunk[0].rank}〜{chunk[-1].rank} 位）",
            fills=[
                *_common(psid, surface, ctx, SOURCE_FETCHED),
                table_fill(
                    "table",
                    [c[0] for c in columns],
                    rows,
                    font_pt=APPENDIX_TABLE_PT,
                    shape_name=f"{psid}｜全件の一覧",
                    col_weights=[c[1] for c in columns],
                    numeric_cols=numeric,
                ),
            ],
            notes=Notes(
                what=f"{_order_label(surface)}の全件（取得値）。本文の全文は CSV にある。",
                sources=_sources_of(chunk, measured_at, surface, limit=APPENDIX_ROWS),
                full_text=[f"{p.rank} 位 {_handle(p)}: {p.desc}" for p in chunk if p.desc],
            ),
        )
    sid = _sid(13, suffix)
    rows = []
    for h in facts.holders[:4]:
        rows.append(
            [
                cell("常連"),
                cell(_clip(f"@{h.author}")),
                cell(f"{len(h.ranks)} 本", align="r"),
                cell(_ranks_text(h.ranks, 6)),
            ]
        )
    for t in facts.tiers:
        rows.append(
            [
                cell("フォロワー帯"),
                cell(t.tier),
                cell(f"{t.count} 本", align="r"),
                cell(f"再生の {round(t.play_share * 100)}%"),
            ]
        )
    for c in facts.categories:
        rows.append(
            [
                cell("タイプ（推定）"),
                cell(category_label(c.category)),
                cell(f"{c.count} 本", align="r"),
                cell(
                    f"本数の {round(c.count_share * 100)}%・"
                    + (
                        f"再生の {round(c.play_share * 100)}%"
                        if any(p.play_count > 0 for p in posts)
                        else f"再生：{UNMEASURED}"
                    )
                ),
            ]
        )
    if not rows:
        ctx.dropped.append(f"{sid} 常連とフォロワー帯: 中身が無い")
        return
    ctx.builder.add_slide(
        slide_id=sid,
        layout=L_APPENDIX,
        section=_SECTION_APPENDIX,
        title="付録：常連・フォロワー帯・タイプ",
        fills=[
            *_common(sid, surface, ctx, SOURCE_COUNTED),
            table_fill(
                "table",
                ["区分", "項目", "本数", "中身"],
                rows,
                font_pt=APPENDIX_TABLE_PT,
                shape_name=f"{sid}｜常連とフォロワー帯",
                col_weights=(1.6, 3.0, 1.0, 4.0),
                numeric_cols=(2,),
            ),
        ],
        notes=Notes(
            what="同じアカウントが複数の枠を持つ常連、フォロワー帯、タイプ別の本数と再生の割合（グラフの数字もここに残す）。",
            sources=[f"取得時点 {measured_at}", TYPE_NOTE],
        ),
    )


def _csv(surfaces: Sequence[KwSurface]) -> CsvSpec:
    columns = (
        "検索語", "媒体", "順位", "アカウント", "表示名", "フォロワー", "再生", "いいね",
        "コメント",
        "シェア", "保存", "保存率(%)", "投稿日", "長さ(秒)", "タイプ(推定)", "PR表記", "自社",
        "競合", "自社名の言及", "URL", "本文", "タグ", "出現回数",
    )  # fmt: skip
    rows = []
    for s in surfaces:
        ig = _is_ig(s)
        for p in sorted(s.posts, key=lambda x: x.rank):
            rate = _save_rate(p)
            rows.append(
                (
                    s.keyword,
                    _platform(s),
                    str(p.rank),
                    p.author,
                    UNMEASURED if ig else p.author_name,
                    UNMEASURED if ig or p.author_followers <= 0 else str(p.author_followers),
                    str(p.play_count) if p.play_count > 0 else UNMEASURED,
                    str(p.like_count),
                    str(p.comment_count),
                    UNMEASURED if ig else str(p.share_count),
                    UNMEASURED if ig else str(p.save_count),
                    UNMEASURED if rate is None else f"{rate:.2f}",
                    UNMEASURED if ig else fmt_date(p.posted_at) or UNMEASURED,
                    str(p.duration_sec) if not ig and p.duration_sec > 0 else UNMEASURED,
                    category_label(p.category),
                    "あり" if p.is_pr or is_pr_post(p) else "",
                    "はい" if p.is_client else "",
                    "はい" if p.is_competitor else "",
                    "はい" if p.mentions_client else "",
                    p.url,
                    p.desc,
                    UNMEASURED if ig else " ".join(f"#{t}" for t in p.hashtags),
                    str(p.appearances) if ig else UNMEASURED,
                )
            )
    return CsvSpec(columns=columns, rows=tuple(rows))


def _deduplicate_ai(builder: DeckBuilder, dropped: list[str]) -> None:
    """AI 本文で数字が重複した段落を外し、数値の編集箇所を一つにする。"""
    for index, slide in enumerate(builder.slides):
        if slide.slide_id.split("-")[1] != "04":
            continue
        source = next((f for f in slide.fills if getattr(f, "box", "") == "source"), None)
        if not isinstance(source, TextFill) or not any(
            SOURCE_AI_GROUNDED == r.text for p in source.paragraphs for r in p.runs
        ):
            continue
        body = next((f for f in slide.fills if isinstance(f, TextFill) and f.box == "body"), None)
        if body is None:
            continue
        before = duplication_problems(builder.slides)
        fallback_title = line_fill("title", "検索上位の投稿と自社の位置", f"{slide.slide_id}｜題")
        revised = slide.model_copy(
            update={"fills": tuple(fallback_title if f.box == "title" else f for f in slide.fills)}
        )
        builder.slides[index] = revised
        if len(duplication_problems(builder.slides)) < len(before):
            dropped.append(f"{slide.slide_id} AI の題: 題・数字の重複のため省略")
            slide = revised
        else:
            builder.slides[index] = slide
        for paragraph in body.paragraphs:
            before = duplication_problems(builder.slides)
            kept = tuple(p for p in body.paragraphs if p != paragraph)
            candidate = body.model_copy(update={"paragraphs": kept})
            revised = slide.model_copy(
                update={"fills": tuple(candidate if f == body else f for f in slide.fills)}
            )
            builder.slides[index] = revised
            if len(duplication_problems(builder.slides)) < len(before):
                dropped.append(f"{slide.slide_id} AI の文: 数字の重複のため段落を省略")
                body, slide = candidate, revised
            else:
                builder.slides[index] = slide
        if not body.paragraphs:
            fallback = body.model_copy(
                update={"paragraphs": (para("取得値は主要な数字と上位の一覧を参照"),)}
            )
            builder.slides[index] = slide.model_copy(
                update={"fills": tuple(fallback if f == body else f for f in slide.fills)}
            )


def _budget(builder: DeckBuilder, dropped: list[str]) -> None:
    """付録、後ろの面のカード・一覧・切り口の順に上限内へ縮める。"""

    def prune_images() -> None:
        used = {f.image for s in builder.slides for f in s.fills if isinstance(f, PictureFill)}
        builder.images = {k: v for k, v in builder.images.items() if k in used}
        builder.image_bytes = {k: v for k, v in builder.image_bytes.items() if k in used}

    def over() -> bool:
        return len(builder.slides) > MAX_DECK_SLIDES or len(builder.images) > MAX_DECK_IMAGES

    prune_images()
    candidates = [s for s in reversed(builder.slides) if s.section == _SECTION_APPENDIX]
    for base in (8, 9, 10):
        candidates.extend(
            s for s in reversed(builder.slides) if s.slide_id.split("-")[1] == f"{base:02d}"
        )
    for slide in candidates:
        if not over():
            break
        builder.slides.remove(slide)
        dropped.append(
            f"{slide.slide_id}: 枚数 {MAX_DECK_SLIDES}・画像 {MAX_DECK_IMAGES} の上限のため省略"
        )
        prune_images()
    if over():
        raise DeckBuildError("検索語×媒体が多すぎて 1 ファイルに入りません。検索語を分けてください")


def build_surface_deck(
    surfaces: Sequence[KwSurface],
    *,
    client_name: str | None,
    measured_epoch: int,
    report_id: str,
    client_accounts: Sequence[str] = (),
    competitor_accounts: Sequence[str] = (),
    addressee: str | None = None,
    covers: Mapping[str, bytes] | None = None,
    include_appendix: bool = False,
) -> SurfaceDeck:
    """検索上位チェックの結果 → DeckSpec と画像（名前 → bytes）。

    covers は投稿 URL → 表紙の画像（JPEG/PNG）。無い投稿は図の枠だけ残す。
    """
    surfaces = [s for s in surfaces if s.posts]
    if not surfaces:
        raise ValueError("no surface has posts")
    _, measured_at = _measured(measured_epoch)
    kws = list(dict.fromkeys(s.keyword for s in surfaces))
    builder = DeckBuilder(
        properties=DeckProperties(
            title="（未設定）",
            subject="検索上位チェック",
            keywords="、".join(kws)[:200],
            report_id=report_id,
            measured_at=measured_at,
        ),
        footer=_clip(f"検索上位チェック｜{'・'.join(kws)}", 60),
    )
    dropped: list[str] = []
    ctx = _Ctx(
        builder=builder,
        client_name=client_name,
        client_accounts=list(client_accounts),
        competitor_accounts=list(competitor_accounts),
        measured_epoch=measured_epoch,
        covers=covers or {},
        dropped=dropped,
    )
    title = _cover_slide(surfaces, ctx, addressee)
    builder.properties = builder.properties.model_copy(update={"title": title})
    multi = len(surfaces) > 1
    if multi:
        _compare_slide(surfaces, ctx)
    for index, surface in enumerate(surfaces, start=1):
        facts = surface.facts or compute_facts(
            surface.posts,
            keyword=surface.keyword,
            client_name=client_name,
            now_epoch=measured_epoch,
        )
        suffix = f"-{index}" if multi else ""
        section_kw = _clip(surface.keyword, 36 - len(str(index)) - len(_platform(surface)))
        label = f"{index}「{section_kw}」{_platform(surface)}" if multi else ""
        main = f"本編{label}"
        _conclusion_slide(surface, facts, ctx, suffix, main)
        _roster_slide(surface, facts, ctx, suffix, main)
        _numbers_slide(surface, facts, ctx, suffix, main)
        _composition_slide(surface, facts, ctx, suffix, main)
        _cards_slide(surface, ctx, suffix, main)
        _top_table_slide(surface, ctx, suffix, main)
        _angles_slide(surface, facts, ctx, suffix, main)
    if include_appendix:
        for index, surface in enumerate(surfaces, start=1):
            facts = surface.facts or compute_facts(
                surface.posts,
                keyword=surface.keyword,
                client_name=client_name,
                now_epoch=measured_epoch,
            )
            _appendix(surface, facts, ctx, f"-{index}" if multi else "")
    builder.csv = _csv(surfaces)
    _budget(builder, dropped)
    _deduplicate_ai(builder, dropped)
    for i, slide in enumerate(builder.slides):
        omissions = [d for d in dropped if d.startswith(slide.slide_id + " ")]
        if omissions:
            fills = tuple(
                line_fill("source", "AI の文を一部省略（ノート参照）", f.shape_name)
                if isinstance(f, TextFill) and f.box == "source"
                else f
                for f in slide.fills
            )
            builder.slides[i] = slide.model_copy(
                update={
                    "fills": fills,
                    "notes": slide.notes + "\n\n【省略した文】\n" + "\n".join(omissions),
                }
            )
    return SurfaceDeck(spec=builder.build(), images=dict(builder.image_bytes), dropped=dropped)


__all__ = [
    "SurfaceDeck",
    "build_surface_deck",
    "condition_line",
    "report_deck_enabled",
]
