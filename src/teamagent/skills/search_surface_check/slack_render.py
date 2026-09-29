"""検索上位チェックの直接投稿（1 段目・2 段目の追記）を Block Kit で描く。

mcp が Slack API へ直接出す経路（``mcp_gateway/direct_summary.py`` と
``mcp_gateway/surface_video_followup.py``）専用。``summary.py`` / ``video_render.py`` の文面
（標準 Markdown・Aico が中継する経路）はそのまま残す。部品と守りは ``_shared/slack_blocks.py``。

並び（1 通完結・スマホで上半分だけ読めば判断できる順）:
  見出し → 集計の日時と取得の経路 → 結論 → クライアントの現状 → 顔ぶれ（欄）→ 常連 →
  入り口の手がかり（段階つき・3 行まで）→ 切り口（AI の分類）→ AI の読み → ── →
  上位 5 本（1 本 1 行・投稿への文字リンク）→ 次に届くもの → レポートの文字リンク → 注記・概算
最上位の text（スクリーンリーダーが読む・通知・会話の履歴）には、blocks と同じ中身をすべて入れる
（``slack_blocks.message_text``）。

言い方の決まり:
- 数字は集計（SurfaceFacts / VideoDigest）からそのまま出し、分母つきで段階（全員に共通・多数派・
  半数・少数派・0本）の語をコードで付ける。LLM の文には段階の語を付けない。
- LLM の文は「AI の要約」「AI の分類」「AI の読み」と書く。「勝ち筋」「空白」「打ち手」の全文は
  レポートに任せる（数字は集計の側に事実として出し、二重に書いて食い違わせない）。
- クライアントの節にはクライアントの事実だけを置き、照合した範囲を添える（本文・タグの表記だけで、
  ほかの表記や公式アカウントは見ていない）。上位全体の数字は別の節に置く。
- キャプションは出さない（第三者の文字列と絵文字コードの羅列を避ける）。
- 「依頼のたびに検索し直した値」と書くのは、この依頼で検索した面だけ（``tiktok_source``）。
  事前の取得ジョブ（acquire_job_id）を読んだ TikTok 面は、そう書かない。
- AI の文の中の保存率（投稿一覧に 1 桁で渡している値）は、1 本に決まるときだけ集計の値（小数 2 桁）
  にそろえる（すぐ上の集計の行と食い違って見せない）。

Slack には出さず、レポートに任せるもの（1 通の長さを抑えるため）:
- 1 段目: 6 位以降の行・キャプション・フォロワー帯・再生÷フォロワー・保存率の上位の一覧・尺・
  よく付くタグ・PR 表記の順位一覧（上位 5 本の行には PR 表記を付ける）・順位と再生の一致度・
  勝ち筋と空白の全文（09-29 小俣さん「Slack での書き方が見づらい」→ 1 通を短くする）
- 2 段目: CTA の種類（分類の検証前）・勝ち筋（winning）の全文
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from teamagent.skills._shared.slack_blocks import (
    MAX_SECTION_TEXT,
    MAX_TOTAL_TEXT,
    Block,
    RichMessage,
    assemble,
    clip,
    context,
    divider,
    esc,
    header,
    link,
    link_url,
    measured_at,
    message_text,
    post_url,
    section,
    stage,
)
from teamagent.skills._shared.text_safety import sanitize_llm_text
from teamagent.skills.search_surface_check.display import (
    PLATFORM_LABEL,
    category_label,
    fmt_age,
    fmt_count,
    fmt_duration,
    fmt_pct,
    fmt_ranks,
)
from teamagent.skills.search_surface_check.schema import (
    FollowupVideo,
    KwSurface,
    SearchSurfaceCheckInput,
    SearchSurfaceCheckOutput,
    SurfaceConclusion,
    SurfaceFacts,
    SurfacePost,
    SurfaceVideoFollowupOutput,
    VideoDigest,
)
from teamagent.skills.search_surface_check.video_digest import (
    PACING_LABEL,
    duration_of,
    has_opening_telop,
    has_spoken_kw,
    has_telop_kw,
    hook_label,
    is_watched,
)
from teamagent.skills.video_algorithm.schema import AnalyzedVideo

_TOP_SINGLE = 5
_TOP_COMPACT = 3
_TOP_URLS_IN_TEXT = 5
_HOLDERS = 3
_SMALL_EXAMPLES = 2
_RANK_LINKS = 5
_SMALL_ACCOUNT_MAX = 10_000

REUSED_NOTE = "24 時間以内の同じ分析の結果です（動画分析の回数は使っていません）"
# 通知文の先頭（文字だけの追記の先頭行＝surface_video_followup.REUSED_PREFIX と同じ）。
REUSED_TEXT_PREFIX = "（24 時間以内の同じ分析の結果です）"
FOLLOWUP_CAVEAT = "数本の観測なので、傾向として読んでください（順位の理由の断定ではありません）"
COVER_ONLY_ROW = "動画を取得できず、サムネだけの分析（テロップ・構成は判定できません）"
FAILED_ROW = "分析できませんでした"
REPORT_FAILED = "レポートの発行に失敗しました（上の要約は分析結果どおりです）"
LIVE_NOTE = "実測（依頼のたびに検索し直した値）"
ACQUIRED_NOTE = "集計（TikTok は事前の取得ジョブの値で、この依頼では検索し直していません）"
ACQUIRED_WITH_IG_NOTE = (
    "集計（TikTok は事前の取得ジョブの値で、この依頼では検索し直していません。"
    "Instagram はこの依頼で検索した値）"
)
ALIGNED_NOTE = "数字は集計の値にそろえています"


# ── 共通 ─────────────────────────────────────────────────────────────────


def _platform(surface: KwSurface) -> str:
    return PLATFORM_LABEL.get(surface.platform, surface.platform)


def _rank_links(
    ranks: list[int], posts: dict[int, SurfacePost], *, limit: int = _RANK_LINKS
) -> str:
    """順位を投稿への文字リンクにする（``<url|27位>・<url|11位>``）。URL が無い順位は文字だけ。"""
    shown = [link(post_url(posts[r].url) if r in posts else None, f"{r}位") for r in ranks[:limit]]
    rest = f" ほか{len(ranks) - limit}本" if len(ranks) > limit else ""
    return "・".join(shown) + rest


def _handle(author: str) -> str:
    return f"@{author}" if author else "不明"


def _ai(conclusion: SurfaceConclusion | None, llm: str, rule: str) -> str:
    """LLM の文なら「AI の…」、集計だけで作った文なら別の語（見出しの言い方）。"""
    return llm if conclusion is not None and conclusion.generated_by == "llm" else rule


def _stage_suffix(count: int, total: int) -> str:
    word = stage(count, total)
    return f"（{word}）" if word else ""


def _measured_note(out: SearchSurfaceCheckOutput) -> str:
    """集計の日時と、データの取り方（この依頼で検索したか・事前の取得ジョブか）。

    「依頼のたびに検索し直した値」と書くのは、この依頼で検索した面だけ。acquire_job_id の経路は
    以前の tiktok_acquire の成果物を読んでいるだけで、時刻も取得ではなく集計の時刻になる。
    """
    when = measured_at(out.measured_epoch)
    if not when:
        return ""
    tiktok = any(s.platform == "tiktok" for s in out.surfaces)
    if tiktok and out.tiktok_source == "acquire_job":
        others = any(s.platform != "tiktok" for s in out.surfaces)
        return f"{when} {ACQUIRED_WITH_IG_NOTE if others else ACQUIRED_NOTE}"
    if tiktok and out.tiktok_source != "direct":
        return f"{when} 集計"  # 取り方が分からない（古い出力）ときは言い切らない
    return f"{when} {LIVE_NOTE}"


# 「2.9%」「1.6～2.9%」のような % の表記（範囲の両端を含む）。
_PCT_EXPR = re.compile(r"\d+(?:\.\d+)?(?:\s*[～〜~]\s*\d+(?:\.\d+)?)?\s*[%％]")
_ONE_DECIMAL = re.compile(r"(?<![\d.])\d+\.\d(?![\d.])")


def _save_rates(posts: Sequence[SurfacePost]) -> list[float]:
    return [p.save_count / p.play_count * 100 for p in posts if p.play_count > 0 and p.save_count]


def _align_pct(text: str, rates: Sequence[float]) -> tuple[str, bool]:
    """AI の文の保存率（小数 1 桁）を、1 本に決まるときだけ集計の値（小数 2 桁）にそろえる。

    LLM には投稿一覧の保存率を小数 1 桁で渡している（``conclusion.posts_payload``）ので、AI の文は
    「1.6～2.9%」になり、すぐ上の集計の行（27位 2.88%・21位 1.62%）と食い違って見える。
    AI の文の小数 1 桁の % は、照合（grounding）で入力にある値に限られる＝投稿の保存率。
    同じ値に丸まる投稿が 2 本以上あるときは、どれか決められないので変えない。
    """
    by_shown: dict[str, set[str]] = {}
    for rate in rates:
        by_shown.setdefault(f"{round(rate, 1):.1f}", set()).add(f"{round(rate, 2):g}")
    changed = False

    def one(match: re.Match[str]) -> str:
        nonlocal changed
        value = match.group(0)
        precise = by_shown.get(value, set())
        if len(precise) == 1 and (aligned := next(iter(precise))) != value:
            changed = True
            return aligned
        return value

    out = _PCT_EXPR.sub(lambda m: _ONE_DECIMAL.sub(one, m.group(0)), text)
    return out, changed


# ── 1 段目（検索上位チェック）────────────────────────────────────────────


def _headline(c: SurfaceConclusion, rates: Sequence[float]) -> str:
    return esc(_align_pct(c.headline, rates)[0] if c.generated_by == "llm" else c.headline)


def _conclusion_block(c: SurfaceConclusion | None, rates: Sequence[float]) -> Block | None:
    if c is None or not c.headline:
        return None
    title = _ai(c, "結論（AI の要約）", "結論（集計から）")
    return section(f":mag: *{title}*\n{_headline(c, rates)}")


def _client_parts(
    surface: KwSurface, input: SearchSurfaceCheckInput | None, posts: dict[int, SurfacePost]
) -> tuple[str, list[str], str] | None:
    """クライアントの節の（見出し, 行, 照合の範囲）。クライアントの指定が無ければ None。"""
    if input is None or not (input.client_name or input.client_accounts):
        return None
    facts = surface.facts
    n = len(surface.posts)
    lines: list[str] = []
    if input.client_accounts:
        if surface.client_ranks:
            lines.append(
                f"• クライアントの投稿: {_rank_links(surface.client_ranks, posts)}"
                f"（上位{n}本に {len(surface.client_ranks)}本）"
            )
        else:
            lines.append(f"• クライアントの投稿は上位{n}本に無し")
    name = input.client_name or ""
    if name and facts is not None:
        if facts.mention_ranks:
            lines.append(
                f"• 「{esc(name)}」に触れた投稿 {len(facts.mention_ranks)}本"
                f"（{_rank_links(facts.mention_ranks, posts)}）"
            )
        else:
            lines.append(f"• 上位{n}本に「{esc(name)}」に触れた投稿は無し")
    if not lines:
        return None
    title = f"{esc(name)} の現状" if name else "クライアントの現状"
    scope: list[str] = []
    if name:
        scope.append(f"本文・タグにある「{esc(name)}」の表記")
    if input.client_accounts:
        accounts = "・".join(esc(_handle(a.lstrip("@"))) for a in input.client_accounts[:5])
        scope.append(f"アカウント {accounts} ")
    missing = "ほかの表記" + ("" if input.client_accounts else "・公式アカウント")
    note = f"照合したのは{'と'.join(scope)}だけです（{missing}は照合していません）"
    return title, lines, note


def _client_blocks(
    surface: KwSurface, input: SearchSurfaceCheckInput | None, posts: dict[int, SurfacePost]
) -> list[Block]:
    parts = _client_parts(surface, input, posts)
    if parts is None:
        return []
    title, lines, note = parts
    return [section(f":office: *{title}*\n" + "\n".join(lines)), context(note)]


def _lineup_block(facts: SurfaceFacts) -> Block | None:
    if not facts.categories:
        return None
    cats = sorted(facts.categories, key=lambda c: (-c.play_share, -c.count))
    fields = [
        f"*{category_label(c.category)}*\n{c.count}本・再生の{fmt_pct(c.play_share)}"
        for c in cats[:8]
    ]
    fields.append(f"*再生の中央値*\n{fmt_count(facts.median_plays)}回")
    if facts.median_save_rate_pct is not None:
        fields.append(f"*保存率の中央値*\n{facts.median_save_rate_pct:g}%")
    return section(
        f":busts_in_silhouette: *上位{facts.n}本の顔ぶれ* （投稿者の区分・再生の割合が大きい順）",
        fields=fields,
    )


def _holder_block(facts: SurfaceFacts, posts: dict[int, SurfacePost]) -> Block | None:
    if not facts.holders:
        if not facts.n:
            return None
        return section(f":dart: *常連*\n• なし（{facts.n}本すべて別のアカウント）")
    followers = {p.author.lower(): p.author_followers for p in posts.values() if p.author}
    lines = []
    for h in facts.holders[:_HOLDERS]:
        who = [category_label(h.category)]
        count = followers.get(h.author.lower(), 0)
        if count > 0:
            who.append(f"{fmt_count(count)}人")
        lines.append(
            f"• {esc(_handle(h.author))}（{'・'.join(who)}） {len(h.ranks)}枠: {fmt_ranks(h.ranks)}"
        )
    return section(":dart: *常連* （複数の枠を持つアカウント）\n" + "\n".join(lines))


def _overall_block(
    facts: SurfaceFacts, keyword: str, posts: dict[int, SurfacePost]
) -> Block | None:
    lines: list[str] = []
    if facts.small_in_top10 is not None and facts.top10_n:
        line = (
            f"• フォロワー1万人未満の投稿者: 上位{facts.top10_n}本中 {facts.small_in_top10}本"
            f"{_stage_suffix(facts.small_in_top10, facts.top10_n)}"
        )
        small = [
            p
            for p in sorted(posts.values(), key=lambda p: p.rank)
            if p.rank <= 10 and 0 < p.author_followers < _SMALL_ACCOUNT_MAX
        ][:_SMALL_EXAMPLES]
        if small:
            examples = "・".join(
                f"{link(post_url(p.url), f'{p.rank}位')} {esc(_handle(p.author))} "
                f"{fmt_count(p.author_followers)}人"
                for p in small
            )
            line += f"\n　例: {examples}"
        lines.append(line)
    if facts.kw_in_text is not None and facts.n:
        lines.append(
            f"• 本文かタグに「{esc(keyword)}」の語をすべて含む: {facts.kw_in_text}/{facts.n}本"
            f"{_stage_suffix(facts.kw_in_text, facts.n)}"
        )
    if facts.recent_90d is not None and facts.n:
        line = (
            f"• 直近90日の投稿: {facts.recent_90d}/{facts.n}本"
            f"{_stage_suffix(facts.recent_90d, facts.n)}"
        )
        if facts.median_age_days is not None:
            line += f"・投稿時期の中央値 {fmt_age(facts.median_age_days)}"
        lines.append(line)
    if not lines:
        return None
    return section(f":mag_right: *入り口の手がかり* （上位{facts.n}本）\n" + "\n".join(lines))


def _angle_block(c: SurfaceConclusion | None) -> Block | None:
    if c is None or not c.angles:
        return None
    lines = [
        f"• {esc(a.text)}" + (f"（{fmt_ranks(a.ranks, limit=4)}）" if a.ranks else "")
        for a in c.angles
    ]
    title = _ai(c, "AI の分類", "集計から")
    return section(f":bulb: *上位に見られる切り口* （{title}・該当する順位）\n" + "\n".join(lines))


def _reading_block(c: SurfaceConclusion | None, posts: dict[int, SurfacePost]) -> Block | None:
    if c is None or not c.actions:
        return None
    rates = _save_rates(list(posts.values()))
    lines: list[str] = []
    aligned = False
    for a in c.actions:
        text, changed = _align_pct(a.text, rates) if c.generated_by == "llm" else (a.text, False)
        aligned = aligned or changed
        lines.append(
            f"• {esc(text)}" + (f"（根拠: {_rank_links(a.ranks, posts)}）" if a.ranks else "")
        )
    title = _ai(c, "AI の読み（次の一手の候補）", "次の一手の候補（集計から）")
    note = f" （{ALIGNED_NOTE}）" if aligned else ""
    return section(f":speech_balloon: *{title}*{note}\n" + "\n".join(lines))


def _post_line(post: SurfacePost, *, now_epoch: int) -> str:
    who = [category_label(post.category)]
    if post.author_followers > 0:
        who.append(f"{fmt_count(post.author_followers)}人")
    metrics = [f"{fmt_count(post.play_count)}回"]
    if post.appearances > 1:
        metrics.append(f"出現{post.appearances}回")
    if post.play_count > 0 and post.save_count > 0:
        metrics.append(f"保存{post.save_count / post.play_count * 100:.1f}%")
    if post.posted_at > 0 and now_epoch > 0:
        metrics.append(fmt_age(max(0, (now_epoch - post.posted_at) // 86_400)))
    marks = ("［クライアント］" if post.is_client else "") + ("［PR表記］" if post.is_pr else "")
    handle = link(post_url(post.url), _handle(post.author))
    return f"*{post.rank}位* {handle}（{'・'.join(who)}）{marks} {'・'.join(metrics)}"


def _top_block(surface: KwSurface, *, top: int, now_epoch: int) -> Block | None:
    if not surface.posts:
        return None
    unit = "再生・保存率・投稿時期" if surface.platform == "tiktok" else "再生・出現回数"
    shown = sorted(surface.posts, key=lambda p: p.rank)[:top]
    lines = [_post_line(p, now_epoch=now_epoch) for p in shown]
    return section(f":clipboard: *上位{len(shown)}本* （{unit}）\n" + "\n".join(lines))


def _single_head(
    out: SearchSurfaceCheckOutput, surface: KwSurface, input: SearchSurfaceCheckInput | None
) -> tuple[list[Block], list[Block]]:
    """1 語 1 媒体の本文と、最上位の text で要点の行に含めた（本文では繰り返さない）blocks。"""
    posts = {p.rank: p for p in surface.posts}
    note = _measured_note(out)
    title = header(f"検索上位チェック「{surface.keyword}」")
    conclusion = _conclusion_block(surface.conclusion, _save_rates(surface.posts))
    blocks: list[Block | None] = [
        title,
        context(
            f"{esc(_platform(surface))} 上位{len(surface.posts)}本" + (f"・{note}" if note else "")
        ),
        conclusion,
        *_client_blocks(surface, input, posts),
    ]
    facts = surface.facts
    if facts is not None:
        blocks += [
            _lineup_block(facts),
            _holder_block(facts, posts),
            _overall_block(facts, surface.keyword, posts),
        ]
    blocks += [_angle_block(surface.conclusion), _reading_block(surface.conclusion, posts)]
    blocks += [divider(), _top_block(surface, top=_TOP_SINGLE, now_epoch=out.measured_epoch)]
    return [b for b in blocks if b], [b for b in (title, conclusion) if b]


def _compact_section(
    surface: KwSurface, input: SearchSurfaceCheckInput | None, *, now_epoch: int, limit: int
) -> Block:
    posts = {p.rank: p for p in surface.posts}
    lines = [f"*「{esc(surface.keyword)}」{esc(_platform(surface))} 上位{len(surface.posts)}本*"]
    c = surface.conclusion
    if c is not None and c.headline:
        headline = _headline(c, _save_rates(surface.posts))
        lines.append(f"{_ai(c, '結論（AI の要約）', '結論（集計から）')}: {headline}")
    client = _client_parts(surface, input, posts)
    if client is not None:
        lines.extend(client[1])
    facts = surface.facts
    if facts is not None and facts.categories:
        cats = sorted(facts.categories, key=lambda x: (-x.play_share, -x.count))[:4]
        lines.append(
            "• 投稿者: "
            + "／".join(
                f"{category_label(x.category)} {x.count}本（再生の{fmt_pct(x.play_share)}）"
                for x in cats
            )
        )
    if facts is not None and facts.holders:
        lines.append(
            "• 常連: "
            + "・".join(
                f"{esc(_handle(h.author))} {len(h.ranks)}枠" for h in facts.holders[:_HOLDERS]
            )
        )
    shown = sorted(surface.posts, key=lambda p: p.rank)[:_TOP_COMPACT]
    if shown:
        lines.append(
            "• 上位: "
            + "・".join(link(post_url(p.url), f"{p.rank}位 {_handle(p.author)}") for p in shown)
        )
    return section(clip("\n".join(lines), limit))


def _compact_head(
    out: SearchSurfaceCheckOutput, input: SearchSurfaceCheckInput | None
) -> tuple[list[Block], list[Block]]:
    keywords = list(dict.fromkeys(s.keyword for s in out.surfaces))
    platforms = list(dict.fromkeys(_platform(s) for s in out.surfaces))
    note = _measured_note(out)
    title = header("検索上位チェック" + "".join(f"「{k}」" for k in keywords))
    blocks: list[Block] = [
        title,
        context("・".join(esc(p) for p in platforms) + (f"・{note}" if note else "")),
    ]
    # 面が多くても後ろの面を丸ごと落とさないよう、1 面の節を合計の上限から割り当てた長さで切る
    # （見出しの行と結論は頭にあるので残る）。
    limit = max(600, min(MAX_SECTION_TEXT, (MAX_TOTAL_TEXT - 3000) // len(out.surfaces)))
    for i, surface in enumerate(out.surfaces):
        if i:
            blocks.append(divider())
        blocks.append(_compact_section(surface, input, now_epoch=out.measured_epoch, limit=limit))
    client = _client_parts(out.surfaces[0], input, {}) if out.surfaces else None
    if client is not None:
        blocks.append(context(client[2]))
    return blocks, [title]


def _surface_tail(out: SearchSurfaceCheckOutput, missing: list[str]) -> list[Block]:
    blocks: list[Block] = []
    notes: list[str] = []
    if out.followup_note:
        notes.append(f":hourglass_flowing_sand: {esc(out.followup_note)}")
    notes.extend(missing)
    if notes:
        blocks.append(context(*notes))
    report = link_url(out.report_url) if out.report_url else None
    if out.report_url and report is None:
        # 自前の URL なのにリンクにできない＝想定外。文字だけの投稿（URL を <URL> で出す）に戻す。
        raise ValueError("report url is not linkable")
    if report:
        total = sum(len(s.posts) for s in out.surfaces)
        blocks.append(
            section(
                f":page_facing_up: {link(report, 'レポートを開く')}"
                f" （全{total}本の一覧つき・7日有効）"
            )
        )
    tail_notes: list[str] = []
    if out.warnings:
        tail_notes.append(
            "注意: " + " / ".join(esc(sanitize_llm_text(w, max_len=120)) for w in out.warnings)
        )
    tail_notes.append(f"概算 ${out.total_cost_usd:.4f}")
    blocks.append(context(*tail_notes))
    return blocks


def _missing_lines(
    out: SearchSurfaceCheckOutput, input: SearchSurfaceCheckInput | None
) -> list[str]:
    if input is None:
        return []
    have = {(s.keyword, s.platform) for s in out.surfaces}
    return [
        f"{esc(PLATFORM_LABEL.get(p, p))}「{esc(k)}」はデータを取得できませんでした"
        "（取得できた媒体だけで分析しています）"
        for k in input.keywords
        for p in input.platforms
        if (k, p) not in have
    ]


def _surface_lead(out: SearchSurfaceCheckOutput) -> str:
    """最上位の text の 1 行目（通知のプレビュー）: 何の結果か＋結論。"""
    if len(out.surfaces) != 1:
        keywords = list(dict.fromkeys(s.keyword for s in out.surfaces))
        return "検索上位チェック" + "".join(f"「{esc(k)}」" for k in keywords)
    surface = out.surfaces[0]
    head = (
        f"検索上位チェック「{esc(surface.keyword)}」{esc(_platform(surface))} "
        f"上位{len(surface.posts)}本"
    )
    c = surface.conclusion
    if c is not None and c.headline:
        head += f": {_headline(c, _save_rates(surface.posts))}"
    return head


def surface_message(
    out: SearchSurfaceCheckOutput, input: SearchSurfaceCheckInput | None = None
) -> RichMessage | None:
    """1 段目の直接投稿（Block Kit）。面が無ければ None（文字だけの投稿に戻す）。

    最上位の text は、要点の 1 行＋blocks の全文（見出しと結論は 1 行目に入れたので繰り返さない）
    ＋レポート・注記。上位の投稿 URL が text から削れたときは最後に足す（会話の履歴から辿れる
    ように）。
    """
    if not out.surfaces:
        return None
    if len(out.surfaces) == 1:
        head, in_lead = _single_head(out, out.surfaces[0], input)
    else:
        head, in_lead = _compact_head(out, input)
    tail = _surface_tail(out, _missing_lines(out, input))
    blocks = assemble(head, tail)
    body = [b for b in blocks[: len(blocks) - len(tail)] if not any(b is x for x in in_lead)]
    first = sorted(out.surfaces[0].posts, key=lambda p: p.rank)[:_TOP_URLS_IN_TEXT]
    urls = [(f"{p.rank}位", u) for p in first if (u := post_url(p.url))]
    text = message_text([_surface_lead(out)], body, blocks[len(blocks) - len(tail) :], urls=urls)
    return RichMessage(text=text, blocks=blocks)


# ── 2 段目（上位の動画の中身）───────────────────────────────────────────


def followup_rows(videos: list[AnalyzedVideo]) -> list[FollowupVideo]:
    """分析した動画を 1 本 1 行の値にする（決定的に数えた値だけ・LLM の文は持たない）。"""
    rows: list[FollowupVideo] = []
    for v in videos:
        a = v.analysis
        if a is None:
            rows.append(FollowupVideo(rank=v.meta.rank, author=v.meta.author, url=v.meta.url))
            continue
        if not is_watched(v):
            rows.append(
                FollowupVideo(
                    rank=v.meta.rank, author=v.meta.author, url=v.meta.url, state="cover_only"
                )
            )
            continue
        rows.append(
            FollowupVideo(
                rank=v.meta.rank,
                author=v.meta.author,
                url=v.meta.url,
                state="watched",
                hook=hook_label(a.hook_type),
                opening_telop=has_opening_telop(a),
                telop_kw=has_telop_kw(a),
                spoken_kw=has_spoken_kw(a),
                duration_sec=round(duration_of(v)),
                cut_count=a.cut_count,
                pacing="" if a.pacing == "unknown" else PACING_LABEL.get(a.pacing, ""),
                has_cta=a.has_cta(),
                narration=a.has_narration,
            )
        )
    return rows


_STAGE_ORDER = ("全員に共通", "多数派", "半数", "少数派", "0本")


def _digest_items(d: VideoDigest, rows: list[FollowupVideo]) -> list[tuple[str, int, str]]:
    """段階の表に載せる項目（項目名, 本数, 該当する順位）。

    CTA の種類（来店など）は分類の検証前なので載せない（レシピ動画で「来店」が多数派に見える）。
    """
    items: list[tuple[str, int, str]] = [
        ("冒頭にテロップ", d.opening_telop, ""),
        ("テロップに検索KW", d.telop_kw, ""),
        ("発話に検索KW", d.spoken_kw, ""),
        ("CTA あり", d.cta, ""),
        ("ナレーション", d.narration, ""),
        ("流行の音源", d.trending_sound, ""),
    ]
    watched = [r for r in rows if r.state == "watched"]
    for h in d.hook_types:
        ranks = [r.rank for r in watched if r.hook == h.label]
        items.append((f"フックが{h.label}", h.count, f"（{fmt_ranks(ranks)}）" if ranks else ""))
    for p in d.pacing:
        items.append((f"テンポ {p.label}", p.count, ""))
    return items


def _digest_block(d: VideoDigest, rows: list[FollowupVideo]) -> Block | None:
    n = d.watched
    if n <= 0:
        return None
    groups: dict[str, list[tuple[str, int, str]]] = {}
    for label, count, ranks in _digest_items(d, rows):
        key = stage(count, n) or f"{count}/{n}本"
        groups.setdefault(key, []).append((label, count, ranks))
    # 1 本だけのとき（段階の語が無い）は本数の多い順。
    order = list(_STAGE_ORDER) + sorted(
        (k for k in groups if k not in _STAGE_ORDER), key=lambda k: -int(k.split("/")[0])
    )
    fields: list[str] = []
    for key in order:
        items = groups.get(key)
        if not items:
            continue
        if key in ("多数派", "少数派"):
            # 同じ段階でも本数が違うことがあるので、項目ごとに本数を書く。
            title = f"*{key}*"
            body = "・".join(f"{esc(x)} {c}/{n}本{r}" for x, c, r in items)
        else:
            count = items[0][1]
            title = {
                "全員に共通": f"*全員に共通（{n}/{n}本）*",
                "0本": f"*0/{n}本*",
                "半数": f"*半数（{count}/{n}本）*",
            }.get(key, f"*{key}*")
            body = "・".join(f"{esc(x)}{r}" for x, _, r in items)
        fields += [title, body]
    return section(
        f":bar_chart: *動画を見て分析した{n}本の集計* （本数の多い順）", fields=fields[:10]
    )


def _shape_context(d: VideoDigest) -> Block | None:
    parts: list[str] = []
    if d.median_duration_sec is not None:
        parts.append(f"尺の中央値 {fmt_duration(round(d.median_duration_sec))}")
    if d.median_cut_count is not None:
        parts.append(f"カット数の中央値 {d.median_cut_count:g}")
    if d.median_coherence is not None:
        parts.append(f"テロップ・本文・映像の一致度の中央値 {d.median_coherence:g}（100 が一致）")
    return context("・".join(parts)) if parts else None


def _save_block(result: SurfaceVideoFollowupOutput, rows: dict[int, FollowupVideo]) -> Block | None:
    d = result.digest
    if d is None or not d.save_top_ranks:
        return None
    ranks = "・".join(
        link(post_url(rows[r].url) if r in rows else None, f"{r}位") for r in d.save_top_ranks
    )
    title = f":floppy_disk: *保存率の高い{len(d.save_top_ranks)}本* （{ranks}）"
    # 集計で決まる共通点を先に、AI の読みは後に（数字・事実は集計から出す）。
    common = "・".join(esc(x) for x in d.save_top_common) or "目立った共通点はなし"
    lines = [title, f"共通点（集計）: {common}"]
    c = result.conclusion
    if c is not None and c.save_reason is not None:
        label = "AI の読み" if c.generated_by == "llm" else "読み（集計から）"
        lines.append(f"{label}: {esc(c.save_reason.text)}")
    return section("\n".join(lines))


def _yes_no(flag: bool) -> str:
    return "あり" if flag else "なし"


def _video_lines(rows: list[FollowupVideo]) -> tuple[list[str], bool]:
    """1 本 1 行。全員に同じ値の項目は省く（段階の表に 1 回だけ書いてある）。"""
    watched = [r for r in rows if r.state == "watched"]

    def varies(get: Callable[[FollowupVideo], object]) -> bool:
        return len(watched) >= 2 and len({get(r) for r in watched}) > 1

    flags: list[tuple[str, Callable[[FollowupVideo], object]]] = [
        ("冒頭テロップ", lambda r: r.opening_telop),
        ("テロップの検索KW", lambda r: r.telop_kw),
        ("発話の検索KW", lambda r: r.spoken_kw),
        ("CTA", lambda r: r.has_cta),
        ("ナレーション", lambda r: r.narration),
    ]
    show_hook = len(watched) < 2 or varies(lambda r: r.hook)
    show_pacing = len(watched) < 2 or varies(lambda r: r.pacing)
    shown_flags = [(label, get) for label, get in flags if varies(get)]
    omitted = len(watched) >= 2 and (
        not show_hook or not show_pacing or len(shown_flags) < len(flags)
    )
    lines: list[str] = []
    for r in rows:
        head = f"*{r.rank}位* {link(post_url(r.url), _handle(r.author))}"
        if r.state == "cover_only":
            lines.append(f"{head}　{COVER_ONLY_ROW}")
            continue
        if r.state == "failed":
            lines.append(f"{head}　{FAILED_ROW}")
            continue
        parts: list[str] = []
        if show_hook and r.hook:
            parts.append(f"フック: {esc(r.hook)}")
        parts += [f"{label} {_yes_no(bool(get(r)))}" for label, get in shown_flags]
        shape = [fmt_duration(r.duration_sec)] if r.duration_sec > 0 else []
        if r.cut_count is not None:
            shape.append(f"{r.cut_count}カット")
        if show_pacing and r.pacing:
            shape.append(f"テンポ {esc(r.pacing)}")
        if shape:
            parts.append("・".join(shape))
        lines.append(f"{head}　" + "／".join(parts) if parts else head)
    return lines, omitted


def _followup_lead(result: SurfaceVideoFollowupOutput, *, reused: bool) -> list[str]:
    """最上位の text の 1 行目（通知のプレビュー）。使い回しなら先頭に断り書き。"""
    d = result.digest
    c = result.conclusion
    head = f"上位{len(result.videos)}本の動画の中身「{esc(result.keyword)}」TikTok"
    if c is not None and c.headline:
        head += f": {esc(c.headline)}"
    elif d is not None:
        head += f": 動画を見て分析 {d.watched}本"
    return [REUSED_TEXT_PREFIX if reused else "", head]


def followup_message(
    result: SurfaceVideoFollowupOutput, *, reused: bool = False
) -> RichMessage | None:
    """2 段目の追記（Block Kit）。分析できた結果（status=ok）だけ。ほかは None（文字だけ）。"""
    d = result.digest
    if result.status != "ok" or d is None or not result.videos:
        return None
    rows = result.videos
    by_rank = {r.rank: r for r in rows}
    when = measured_at(result.measured_epoch)
    about = ["TikTok"]
    if when:
        # 1 段目の集計の時刻（この追記を出した時刻ではない）。取り方は 1 段目の注記のとおり。
        about.append(f"{when} の検索上位チェックの続き")
    about.append(f"動画を見て分析 {d.watched}本")
    if d.cover_only_ranks:
        about.append(f"{fmt_ranks(d.cover_only_ranks)}はサムネだけの分析のため集計外")
    if d.failed_ranks:
        about.append(f"{fmt_ranks(d.failed_ranks)}は分析できず")
    c = result.conclusion
    title_block = header(f"上位{len(rows)}本の動画の中身「{result.keyword}」")
    head: list[Block | None] = [
        title_block,
        context(*([REUSED_NOTE] if reused else []), "・".join(about)),
    ]
    in_lead: list[Block] = [title_block]
    if c is not None and c.headline:
        title = "結論（AI の要約）" if c.generated_by == "llm" else "結論（集計から）"
        conclusion = section(f":mag: *{title}*\n{esc(c.headline)}")
        head.append(conclusion)
        in_lead.append(conclusion)
    head += [_digest_block(d, rows), _shape_context(d), _save_block(result, by_rank)]
    lines, omitted = _video_lines(rows)
    head += [
        divider(),
        section(
            ":clipboard: *1本ずつ* "
            + (f"（{d.watched}本すべてに共通の項目は省略）" if omitted else "")
            + "\n"
            + "\n".join(lines)
        ),
    ]
    report = link_url(result.report_url) if result.report_url else None
    if result.report_url and report is None:
        raise ValueError("report url is not linkable")
    notes: list[str] = []
    if d.reserved < d.requested:
        notes.append(
            f"今月の動画分析の残りの都合で、{d.requested}本のうち{d.reserved}本だけ分析しました"
            "（リセットは来月1日・JST）"
        )
    notes.append(FOLLOWUP_CAVEAT)
    notes.append(
        "概算 $0.0000（前回の分析を使い回しました）"
        if reused
        else f"概算 ${result.total_cost_usd:.4f}"
    )
    tail: list[Block] = [
        section(
            f":page_facing_up: {link(report, 'レポートを開く')}"
            f" （上位{len(rows)}本の動画の中身つき・7日有効）"
        )
        if report
        else section(f":page_facing_up: {REPORT_FAILED}"),
        context(*notes),
    ]
    blocks = assemble([b for b in head if b], tail)
    body = [b for b in blocks[: len(blocks) - len(tail)] if not any(b is x for x in in_lead)]
    urls = [(f"{r.rank}位", u) for r in rows if (u := post_url(r.url))]
    text = message_text(
        _followup_lead(result, reused=reused), body, blocks[len(blocks) - len(tail) :], urls=urls
    )
    return RichMessage(text=text, blocks=blocks)


__all__ = [
    "COVER_ONLY_ROW",
    "FAILED_ROW",
    "FOLLOWUP_CAVEAT",
    "REPORT_FAILED",
    "REUSED_NOTE",
    "REUSED_TEXT_PREFIX",
    "followup_message",
    "followup_rows",
    "surface_message",
]
