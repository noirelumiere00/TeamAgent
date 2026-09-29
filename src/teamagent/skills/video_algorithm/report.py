"""VSEO 動画アルゴリズム分析の HTML レポート生成（自己完結・横長SaaSダッシュボード）。

設計思想（docs/v3.2/ui_design_principles_anti_ai.md）= 引き算・余白・結論ファースト。
情報を詰め込まず、上から「結論 → 比較 → 概念 → 一貫性 → 個別ドリルダウン → 統計付録」の
段階的開示（progressive disclosure）にする。

レイアウト（上から）:
  B 結論バンド（段階つきの共通点＋根拠つきの指示。スライドと同じ facts・synthesis v3）
  C 上位n本の比較ボード（サムネ＋主要指標の格子・強調は最も見られ保存された1本）
  D サムネ色比較ボード（検索一覧での目立ち方）
  共通の導線（保存・誘導。多数派はコードの集計）
  E AI の読み解き（synthesis v3 の検査を通した仮説・概念・訴求角度だけ）
  F 一貫性マトリクス（テロップ↔キャプ↔映像中身・N本一望）
  G 各動画ドリルダウン（大型インタラクティブ・タイムライン＋タブ・既定折りたたみ）
  H 統計付録（特徴×表示順位の相関は本文に出さずここだけ・分布/カバレッジ・既定クローズ）

数字・本数・段階の名前・区分（クライアント／競合）・KW の一致は事実層（facts / evidence）が
コードで決める。Gemini の申告（kw_match・brand_relation）はそのまま描かない。

タイムラインは「秒クリック→抽出フレームへスクラブ＋実動画ディープリンク」のSaaS的UX
（自己完結・外部ライブラリ無し・閲覧時ネットワーク無し）。

見た目はデジタル庁デザインシステム（DADS）の共通部品（skills/_html/dads.py）に沿わせる:
本文 16px・表 14px・見出し 32/24/20px、色は DADS トークン経由、文字のコントラスト 4.5:1 以上、
色だけで意味を伝えない、スマホ幅では幅の要る部品だけ枠内で横スクロール。出典はフッタに表示する。
"""

from __future__ import annotations

import html
import json
import os
import re
from urllib.parse import urlsplit

from teamagent.skills._html.dads import DADS_CREDIT, dads_style
from teamagent.skills.search_surface_check.video_digest import PACING_LABEL
from teamagent.skills.search_surface_check.video_structure import ROLE_LABEL, infer_roles
from teamagent.skills.video_algorithm.evidence import (
    TIER_MAJORITY,
    TIER_REQUIRED,
    Roster,
    ranks_text,
)
from teamagent.skills.video_algorithm.facts import (
    CTA_KIND_LABEL,
    Feature,
    VideoFacts,
    category_known,
    cta_consensus,
    detect_pr,
    kw_matrix,
    posted_date,
    product_brands,
    rank_runs,
    scene_index_at,
    unanalyzed_ranks,
    visible_brands,
)
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    FrameShot,
    StatsAnalysis,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
)
from teamagent.skills.video_algorithm.slides import (
    Deck,
    build_deck,
    footer_text,
    ordered_features,
    type_line,
)
from teamagent.skills.video_algorithm.synthesis_checks import (
    code_directives,
    deny_hit,
    directive_line,
)

_POS_JP = {"top": "上", "center": "中", "bottom": "下", "full": "全", "unknown": "?"}
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_PROM_JP = {"hero": "主役級", "prominent": "目立つ", "incidental": "付随", "background": "背景"}
_SRC_JP = {
    "signboard": "看板",
    "product_package": "商品パッケージ",
    "logo_on_clothing": "衣服ロゴ",
    "storefront": "店頭",
    "screen_ui": "画面UI",
    "menu": "メニュー",
    "other": "その他",
}
_INTENT_JP = {
    "likely_sponsored": "タイアップ濃厚",
    "organic_mention": "自然言及",
    "incidental": "偶発",
    "unknown": "不明",
}
_HOOK_JP = {
    "question": "問いかけ",
    "number": "数字",
    "shock": "衝撃",
    "visual": "ビジュアル",
    "pov": "POV",
    "dialogue": "会話",
    "problem": "問題提起",
    "other": "その他",
}


def _analyzed(out: VideoAlgorithmOutput) -> int:
    return sum(1 for v in out.videos if v.analysis)


def _head(s: str, n: int = 46) -> str:
    """一覧のキャプション用: 句点で切らず、先頭 n 字＋「…」（「【4つでいい。…】」を断片にしない）。"""
    text = " ".join((s or "").split())
    return text if len(text) <= n else text[:n] + "…"


def _esc(s: object) -> str:
    return html.escape(str(s if s is not None else ""))


def _http_image_url(value: str | None) -> str:
    """外部画像として描画できる http(s) URL だけを返す。"""
    url = value or ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _image_post_top_n() -> int:
    """画像投稿タブを出す順位上限。0 は機能 OFF。"""
    raw = os.environ.get("VIDEO_ALGO_IMAGE_POST_TOP_N", "5")
    try:
        return max(0, int(raw))
    except ValueError:
        return 5


def _image_post_metas(out: VideoAlgorithmOutput) -> list[VideoMeta]:
    """取得ボードから、動画深掘り済みではない画像投稿を順位順で返す。"""
    analyzed_ranks = {v.meta.rank for v in out.videos if v.analysis}
    return sorted(
        (
            meta
            for meta in out.board
            if meta.rank > 0 and meta.duration_sec == 0.0 and meta.rank not in analyzed_ranks
        ),
        key=lambda meta: meta.rank,
    )


def _hex(h: str) -> str:
    return h if _HEX_RE.match(h or "") else "#cccccc"


def _fmt(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def _pct(sec: float, dur: float) -> float:
    if dur <= 0:
        return 0.0
    return max(0.0, min(100.0, sec / dur * 100.0))


def _json_attr(obj: object) -> str:
    """JSON を <script type=application/json> に安全に埋める（</script> 早期終端対策）。"""
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")


def _kw_flags(f: VideoFacts | None) -> list[tuple[str, bool]]:
    """KW の 4 層（照合済みの事実。Gemini の kw_match は使わない）。動画を見ていなければ全部 False。"""
    layers = (("テロップ", "telop"), ("音声", "speech"), ("キャプ", "caption"), ("HT", "hashtag"))
    if f is None:
        return [(name, False) for name, _layer in layers]
    return [(name, any(h.layer == layer for h in f.kw)) for name, layer in layers]


def _tier_chip(f: Feature) -> str:
    who = "" if f.tier == TIER_REQUIRED else f"（{ranks_text(f.ranks)}）"
    rate = (
        f"・上位{f.board_rate[1]}本では{f.board_rate[0]}/{f.board_rate[1]}" if f.board_rate else ""
    )
    return (
        f'<span class="chip"><span class="tname">{_esc(f.tier)}</span><b>{_esc(f.label)}</b>'
        f"<i>{f.count}/{f.n}{_esc(who)}{_esc(rate)}</i></span>"
    )


# ===========================================================
# B 結論（スライドと同じ事実層と synthesis v3 から描く）
# ===========================================================
def _verdict_band(out: VideoAlgorithmOutput, d: Deck) -> str:
    """結論の帯。見出し・段階つきの共通点・指示は、スライドと同じ Deck（facts と検査済みの
    synthesis v3）から描く。v3 の検査を通していない文（旧キャッシュの v2 の文）は出さない。"""
    n = d.n
    syn = d.syn
    if n:
        big, _by_code = type_line(d)
        best = d.ctx.fact(d.ctx.best_rank)
        why = f"（{'・'.join(d.ctx.best_metrics)}が{n}本で最大）" if d.ctx.best_metrics else ""
        sub = f"最も見られ保存された1本は#{best.rank}{why}。" if best is not None else ""
        if syn is not None and syn.summary_lines is not None and syn.summary_lines.best_reason:
            sub += syn.summary_lines.best_reason
    else:
        big = out.cross.summary or f"「{out.query}」上位動画の共通パターン"
        sub = ""
    feats = (
        ordered_features(d.ctx.features, TIER_REQUIRED)[:4]
        + ordered_features(d.ctx.features, TIER_MAJORITY)[:4]
    )
    chips = "".join(_tier_chip(f) for f in feats) or (
        '<span class="muted small">多数派以上の共通点なし</span>'
    )
    if n < 3:
        gate = (
            f'<div class="nbanner">⚠ 分析成立 n={n}（極小サンプル）。下記は<b>断定でなく観測仮説</b>。'
            "テスト投稿での検証前提でお読みください。</div>"
        )
    elif n < 6:
        gate = (
            f'<div class="nbanner">△ 分析成立 n={n}（小サンプル）。下記は傾向の参考値で、'
            "<b>断定には本数が足りません</b>。テスト投稿での検証を推奨します。</div>"
        )
    else:
        gate = ""
    sub_html = f'<div class="vsub">{_esc(sub)}</div>' if sub else ""
    directives = list(syn.directives) if syn is not None else code_directives(d.ctx)
    items = "".join(f"<li>{_esc(directive_line(x, n))}</li>" for x in directives[:6]) or (
        '<li class="muted">多数派以上の事実が無いため、指示は出していません</li>'
    )
    sl = syn.summary_lines if syn is not None else None
    pitch_html = (
        f'<div class="pitch">💬 <b>次の一手（案）</b>　{_esc(sl.client_move)}</div>'
        if sl is not None and sl.client_move
        else ""
    )
    plan = syn.posting.caption_plan if syn is not None and syn.posting is not None else ""
    posting_html = f'<div class="kvrow"><b>投稿設計</b>{_esc(plan)}</div>' if plan else ""
    missing = unanalyzed_ranks(d.ctx.ranks, out.board)
    rest = f"{rank_runs(missing)}は動画を未分析" if missing else f"上位{n}本だけの観測"
    return (
        f"{gate}"
        '<section class="verdict planner">'
        '<div class="vleft"><div class="th">🎬 プランナーの戦略サマリ（上位の観測にもとづく仮説）</div>'
        f'<div class="vbig">{_esc(big)}</div>{sub_html}'
        f'<div class="chips">{chips}</div>'
        f'<div class="muted small mtop">差の要因: 未特定（{_esc(rest)}）。段階の名前（必須条件＝全部・'
        "多数派＝6割以上・事例＝それ未満）はコードが本数から付けたもの。</div>"
        f"{pitch_html}</div>"
        '<div class="vright"><div class="th">クリエイティブ指示（根拠つき・段階はコードの集計）</div>'
        f'<ul class="nexts">{items}</ul>{posting_html}</div>'
        "</section>"
    )


# ===========================================================
# C Top5比較ボード（行=指標 / 列=動画）
# ===========================================================
def _mini_bar(value: float, vmax: float, *, accent: bool) -> str:
    w = 0.0 if vmax <= 0 else max(0.0, min(100.0, value / vmax * 100.0))
    cls = "miniba acc" if accent else "miniba"
    return f'<span class="{cls}"><i style="width:{w:.0f}%"></i></span>'


def _scrape_board(
    out: VideoAlgorithmOutput, *, image_post_ranks: frozenset[int] | None = None
) -> str:
    """取得（スクレイプ）した上位 board_size 本のメタ一覧（深掘り分析の有無に依らず全件）。

    提案書の「上位N動画ボード(03-5)」の素材。営業がここから提案に載せる動画を選定する。
    深掘り分析（DL+Gemini）した上位本には ★ を付ける（取得≠分析を明示）。
    画像投稿タブ機能が有効な場合は、画像投稿に 📷 を付ける。
    """
    metas = out.board
    if not metas:
        return ""
    n = len(metas)
    analyzed_ranks = {v.meta.rank for v in out.videos if v.analysis}

    def row(m: VideoMeta) -> str:
        deep = (
            '<span class="sbdeep" title="DL+Gemini深掘り分析対象">★</span>'
            if m.rank in analyzed_ranks
            else ""
        )
        is_image_post = image_post_ranks is not None and m.rank in image_post_ranks
        image_post = (
            '<span class="sbimage" title="画像投稿（動画深掘り対象外）">📷</span>'
            if is_image_post
            else ""
        )
        cover_url = _http_image_url(m.cover_url) if is_image_post else (m.cover_url or "")
        thumb = (
            f'<img class="sbth" src="{_esc(cover_url)}" alt="#{m.rank}" loading="lazy">'
            if cover_url
            else '<div class="sbth ph"></div>'
        )
        auth = _esc(m.author) or "—"
        pr = '<span class="sbpr" title="タイアップ表記">PR</span>' if detect_pr(m)[0] else ""
        posted, estimated = posted_date(m)
        when = f"{posted.isoformat()}{'（換算）' if estimated else ''}" if posted else "—"
        return (
            "<tr>"
            f'<td class="sbr">#{m.rank}{deep}{image_post}{pr}</td>'
            f'<td class="sbtdth">{thumb}</td>'
            f'<td class="sbauth"><a href="{_esc(m.url)}" target="_blank" rel="noopener">@{auth}</a></td>'
            f'<td class="sbnum">{_fmt(m.follower_count)}</td>'
            f'<td class="sbnum">{_fmt(m.play_count)}</td>'
            f'<td class="sbnum">{m.save_rate():.1f}%</td>'
            f'<td class="sbnum">{_fmt(m.digg_count)}</td>'
            f'<td class="sbnum">{_esc(when)}</td>'
            f'<td class="sbcap">{_esc(_head(m.desc))}</td>'
            "</tr>"
        )

    body = "".join(row(m) for m in metas)
    image_legend = "・📷＝画像投稿（動画深掘り対象外）" if image_post_ranks else ""
    return (
        '<section><div class="th big">検索上位 取得ボード'
        f"（「{_esc(out.query)}」上位{n}本のメタ一覧・★＝深掘り分析対象・PR＝タイアップ表記"
        f"{image_legend}）</div>"
        '<div class="sbwrap"><table class="sboard">'
        "<thead><tr><th>#</th><th>サムネ</th><th>アカウント</th><th>フォロワー</th>"
        "<th>再生</th><th>保存率</th><th>いいね</th><th>投稿日</th><th>キャプション</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
        '<div class="muted small">※ サムネはTikTok署名URL（時間経過で失効する場合あり）。'
        "営業はこの一覧から提案に載せる動画を選定。</div></section>"
    )


def _pr_mark(f: VideoFacts | None) -> str:
    """PR＝キャプションのタイアップ表記・PR?＝AI の推定だけ（表記なし）。"""
    if f is None or not f.pr:
        return ""
    if f.pr_marked:
        return '<span class="sbpr" title="キャプションのタイアップ表記">PR</span>'
    return '<span class="sbpr" title="AIの推定（キャプションに表記なし）">PR?</span>'


def _top5_board(out: VideoAlgorithmOutput, d: Deck) -> str:
    vids = [v for v in out.videos if v.analysis]
    if not vids:
        return ""
    n = len(vids)
    max_save = max((v.meta.save_rate() for v in vids), default=0.0)
    max_play = max((v.meta.play_count for v in vids), default=0)
    known = category_known(d.ctx.facts)

    def col(v: AnalyzedVideo) -> str:
        m, a = v.meta, v.analysis
        assert a is not None
        f = d.ctx.fact(m.rank)
        # 強調は「最も見られ保存された 1 本」（再生→保存率→シェア）。1 位とは限らない。
        top1 = " is-top" if m.rank == d.ctx.best_rank else ""
        thumb = (
            f'<a href="{_esc(m.url)}" target="_blank" rel="noopener" class="bthumb">'
            f'<img src="{_esc(v.cover_data_uri)}" alt="#{m.rank}"></a>'
            if v.cover_data_uri
            else '<div class="bthumb ph"></div>'
        )
        kwd = "".join(
            f'<span class="d {"on" if ok else "off"}" title="{_esc(name)}"></span>'
            for name, ok in _kw_flags(f)
        )
        save = m.save_rate()
        dur = f.duration_sec if f is not None else a.duration_sec
        cta = f is not None and f.cta_in_video is not None
        # 「商品」は名簿かカテゴリで商品と分かったものだけ（背景のビール缶を数えない）。
        # 分からなければ「目立つ映り込み」として数える（見出しもそう呼ぶ）。
        brands = (product_brands(f) if known else visible_brands(f)) if f is not None else []
        brand = any(b.prominent for b in brands)
        pr = _pr_mark(f)
        return (
            f'<div class="bcol{top1}">'
            f'<div class="brank">#{m.rank}{pr}</div>{thumb}'
            f'<div class="bauth">@{_esc(m.author) or "—"}</div>'
            f'<div class="bm"><span class="bv">{save:.2f}%</span>{_mini_bar(save, max_save, accent=(save >= max_save))}</div>'
            f'<div class="bm"><span class="bv">{_fmt(m.play_count)}</span>{_mini_bar(float(m.play_count), float(max_play), accent=(m.play_count >= max_play))}</div>'
            f'<div class="bm"><span class="bv">{dur:.0f}秒</span></div>'
            f'<div class="bm"><span class="btag">{_esc(_HOOK_JP.get(a.hook_type, _HOOK_JP["other"]))}</span></div>'
            f'<div class="bm kwd">{kwd}</div>'
            f'<div class="bm"><span class="bv">{"✓" if cta else "—"} / {"✓" if brand else "—"}</span></div>'
            "</div>"
        )

    labels = (
        '<div class="blab"><div class="brank">&nbsp;</div><div class="bthumb-lab">サムネ</div>'
        '<div class="bauth">&nbsp;</div>'
        '<div class="bm rl">保存率</div><div class="bm rl">再生</div><div class="bm rl">尺</div>'
        '<div class="bm rl">フック型</div><div class="bm rl">KW層(4)</div>'
        f'<div class="bm rl">CTA/{"映る商品" if known else "目立つ映り込み"}</div></div>'
    )
    cols = "".join(col(v) for v in vids)
    return (
        f'<section><div class="th big">上位{n}本の比較（上の青線＝最も見られ保存された1本・'
        "KW層は照合済み・CTA は文言か秒があるものだけ）</div>"
        f'<div class="board" style="grid-template-columns:138px repeat({n},1fr)">{labels}{cols}</div></section>'
    )


# ===========================================================
# D サムネ色比較ボード
# ===========================================================
def _thumb_board(out: VideoAlgorithmOutput) -> str:
    vids = [v for v in out.videos if v.analysis and (v.thumb or v.cover_data_uri)]
    if not vids:
        return ""
    c = out.cross
    consensus = c.thumb_consensus or "サムネ色の比較"

    def cell(v: AnalyzedVideo) -> str:
        t = v.thumb
        shot = (
            f'<div class="tbshot"><img src="{_esc(v.cover_data_uri)}" alt="#{v.meta.rank}"></div>'
            if v.cover_data_uri
            else '<div class="tbshot ph"></div>'
        )
        if t is None:
            return f'<div class="tbcell"><div class="brank">#{v.meta.rank}</div>{shot}<div class="muted small">色データなし</div></div>'
        sw = "".join(f'<span style="background:{_hex(h)}"></span>' for h in t.swatches[:3]) or ""
        bri = max(0.0, min(100.0, t.brightness01 * 100))
        warm_left = max(0.0, min(100.0, (t.warmth + 1) / 2 * 100))
        near = t.borderline()
        src = {"cover": "表紙", "frame": "コマで代用"}.get(v.cover_source, "出どころ不明")
        tone = f"{t.tone_jp()}（境界）" if "暖寒" in near else t.tone_jp()
        bright = f"{t.brightness01:.2f}（境界）" if "明度" in near else f"{t.brightness01:.2f}"
        return (
            f'<div class="tbcell"><div class="brank">#{v.meta.rank}'
            f'<span class="muted small"> {_esc(src)}</span></div>{shot}'
            f'<div class="tbsw">{sw}</div>'
            f'<div class="tbm"><span class="tbl">明度</span><span class="tbar"><i style="left:{bri:.0f}%"></i></span><span class="tbv">{_esc(bright)}</span></div>'
            f'<div class="tbm"><span class="tbl">暖寒</span><span class="tbar wt"><i style="left:{warm_left:.0f}%"></i></span><span class="tbv">{_esc(tone)}</span></div>'
            "</div>"
        )

    cells = "".join(cell(v) for v in vids)
    framed = [v.meta.rank for v in vids if v.cover_source == "frame"]
    unknown = [v.meta.rank for v in vids if v.cover_source == ""]
    notes = []
    if framed:
        notes.append(f"{ranks_text(framed)}は表紙を取れず冒頭のコマで代用（表紙の色ではない）")
    if unknown:
        notes.append(f"{ranks_text(unknown)}は表紙か冒頭のコマか不明（以前の分析）")
    notes.append("しきい値から0.03以内は「境界」と表示（区分は断定しない）")
    return (
        '<section><div class="th big">サムネ色の比較（表紙の色・参考）</div>'
        f'<div class="tbconsensus"><b>{_esc(consensus)}</b>'
        f'<span class="muted small">　{_esc("・".join(notes))}</span></div>'
        f'<div class="tbrow" style="grid-template-columns:repeat({len(vids)},1fr)">{cells}</div></section>'
    )


# ===========================================================
# E 横断シンセシス（概念の関連性・Gemini解釈層）
# ===========================================================
def _funnel_block(d: Deck) -> str:
    """共通の導線（保存・誘導）。多数派はコードが数える（過半数の型が無ければ「なし」）。

    本番で、文言も秒も無い comment を数え、来店（visit）・保存を多数派と書いた誤りがあった。
    動画内の呼びかけとキャプション内の呼びかけは分けて数える。
    """
    n = d.n
    if n == 0:
        return ""
    facts = d.ctx.facts
    consensus = cta_consensus(facts)
    majority = (
        "、".join(f"{CTA_KIND_LABEL.get(k, k)}（{ranks_text(r)}）" for k, r in consensus)
        or "なし（過半数の型が無い）"
    )
    video = (
        "・".join(
            f"#{f.rank} {CTA_KIND_LABEL.get(f.cta_in_video[0], f.cta_in_video[0])}"
            + ("（最後のテロップから検出）" if f.cta_source == "telop" else "")
            for f in facts
            if f.cta_in_video is not None
        )
        or "なし"
    )
    dropped = "・".join(
        f"#{f.rank} {'・'.join(CTA_KIND_LABEL.get(k, k) for k in f.cta_dropped)}"
        for f in facts
        if f.cta_dropped
    )
    caption: list[str] = []
    for kind in ("save", "follow", "comment", "link_bio"):
        ranks = [f.rank for f in facts if kind in f.cta_in_caption]
        if ranks:
            caption.append(
                f"{CTA_KIND_LABEL.get(kind, kind)} {len(ranks)}/{n}（{ranks_text(ranks)}）"
            )
    places: dict[str, list[int]] = {}
    for f in facts:
        places.setdefault(f.qty_place, []).append(f.rank)
    qty = "・".join(f"{k} {ranks_text(v)}" for k, v in places.items())
    drop_html = (
        f'<div class="kvrow"><b>無効にした CTA</b>{_esc(dropped)}（文言も秒も無い型だけの申告）</div>'
        if dropped
        else ""
    )
    return (
        '<section class="syn"><div class="th big">共通の導線（保存・誘導）</div>'
        f'<div class="kvrow"><b>動画内 CTA の多数派</b>{_esc(majority)}</div>'
        f'<div class="kvrow"><b>動画内 CTA</b>{_esc(video)}</div>{drop_html}'
        f'<div class="kvrow"><b>キャプションでの呼びかけ</b>{_esc("・".join(caption) or "なし")}</div>'
        f'<div class="kvrow"><b>分量の置き場所</b>{_esc(qty)}</div></section>'
    )


def _synthesis_block(d: Deck) -> str:
    """AI の読み解き。synthesis v3 の検査を通したものだけ（旧キャッシュの v2 の文は出さない）。"""
    s = d.syn
    if s is None:
        return ""
    n = d.n
    hyps = "".join(
        f'<div class="hyp"><div class="hyphead"><b>{_esc(h.text)}</b>'
        f'<span class="prev">{_esc(h.tier)} {len(h.ranks)}/{n}</span>'
        f'<span class="prev">{_vrefs(h.ranks)}</span></div>'
        + (f'<div class="small muted">{_esc(h.metric_note)}</div>' if h.metric_note else "")
        + (f'<div class="sowhat">検証: {_esc(h.test)}</div>' if h.test else "")
        + "</div>"
        for h in s.hypotheses
    )
    hyp_block = (
        '<div class="th">仮説（A/B で確かめるもの・該当と段階はコードが数え直し）</div>'
        f'<div class="hyps">{hyps}</div>'
        if hyps
        else ""
    )
    concepts = "".join(
        f'<div class="concept"><div class="cphead"><b>{_esc(cc.concept)}</b>'
        f'<span class="prev">{_esc(cc.prevalence)}</span></div>'
        f'<div class="small">{_esc(cc.gist)}</div>'
        f'<div class="vrefs">{_vrefs(cc.videos)}</div></div>'
        for cc in s.common_concepts
    )
    concepts_block = (
        '<div class="th">共通する概念（該当はコードが数え直し）</div>'
        f'<div class="concepts">{concepts}</div>'
        if concepts
        else ""
    )
    angles = "".join(
        f"<tr><td><b>{_esc(ac.label_jp) or _esc(ac.angle)}</b></td>"
        f"<td>{_vrefs(ac.videos)}</td><td>{_esc(ac.why_works)}</td></tr>"
        for ac in s.angle_clusters
    )
    angle_block = (
        '<div class="th">訴求角度のクラスタ</div><div class="tblwrap"><table class="tbl">'
        "<thead><tr><th>角度</th><th>該当</th><th>効く理由（AI の所見）</th></tr></thead>"
        f"<tbody>{angles}</tbody></table></div>"
        if angles
        else ""
    )
    board_block = _board_angle_block(d)
    if not (hyp_block or concepts_block or angle_block or board_block):
        return ""
    return (
        f'<section class="syn"><div class="th big">AI の読み解き（上位{n}本・根拠は照合済み）</div>'
        f"{hyp_block}{concepts_block}{angle_block}{board_block}</section>"
    )


def _board_angle_block(d: Deck) -> str:
    """上位一覧の切り口（語は AI・該当はコードがキャプションで数えた）と、該当キャプションの先頭。

    数の語（「4つ」）は情報の数（「NG談を4つ」）を除いて数えるが、語で数える以上は取り違えが
    残りうるので、該当した順位ごとにキャプションの先頭 46 字を出して人が確かめられるようにする。
    """
    s = d.syn
    if s is None or not s.board_angles:
        return ""
    heads = {m.rank: _head(m.desc) for m in d.ctx.board}
    rows = "".join(
        f"<tr><td><b>{_esc(b.label)}</b><div class='small muted'>語: "
        f"{_esc('・'.join(b.match_terms))}</div></td><td>{len(b.ranks)}本</td>"
        "<td>"
        + "".join(f"<div class='small'>#{r} {_esc(heads.get(r, ''))}</div>" for r in b.ranks)
        + "</td></tr>"
        for b in s.board_angles
    )
    return (
        '<div class="th">上位一覧の切り口（該当はキャプションの語でコードが数えた・先頭46字で確認）'
        '</div><div class="tblwrap"><table class="tbl"><thead><tr><th>切り口</th><th>本数</th>'
        f"<th>該当したキャプションの先頭</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _vrefs(ranks: list[int]) -> str:
    return "".join(
        f'<span class="rk">#{int(r)}</span>'
        for r in ranks
        if isinstance(r, int) or str(r).isdigit()
    )


# ===========================================================
# F 一貫性マトリクス（テロップ↔キャプ↔映像中身・N本一望）
# ===========================================================
_BAND_CLS = {"一貫": "ok", "概ね一貫": "ok", "部分的": "mid", "乖離": "lo", "—": "na"}
_BAND_DOWN = {"一貫": "概ね一貫", "概ね一貫": "部分的", "部分的": "乖離", "乖離": "乖離"}


def _coherence_band(a: VideoVSEOAnalysis) -> str:
    """一致度の帯。分析 AI が食い違いを自分で書いた（divergence_note）なら 1 段下げる。

    スライドの評価（video_structure._grade_coherence）と同じ規則（本番の #2 は「5つと4つで
    乖離」と書きながら 95 点だった）。
    """
    band = a.coherence_band()
    if (a.divergence_note or "").strip():
        return _BAND_DOWN.get(band, band)
    return band


def _kw_consensus(d: Deck) -> str:
    """語ごと×層ごとの本数（「スパイスカレー」テロップ5/5…／「作り方」テロップ0/5（言い換え1）…）。"""
    n = d.n
    parts: list[str] = []
    rows = kw_matrix(d.ctx.facts, d.ctx.board, d.out.query)
    for term in dict.fromkeys(r.term for r in rows):
        cells = []
        for r in rows:
            if r.term != term or r.layer == "hashtag":
                continue
            text = f"{r.layer_label} {len(r.exact)}/{n}"
            if r.synonym and r.layer in ("telop", "speech"):  # スライドの表と同じ層だけ
                text += f"（言い換え{len(r.synonym)}）"
            cells.append(text)
        parts.append(f"「{term}」" + "・".join(cells))
    return "／".join(parts)


def _matrix_block(out: VideoAlgorithmOutput, d: Deck) -> str:
    vids = [v for v in out.videos if v.analysis]
    if not vids:
        return ""

    def cellmark(ok: bool) -> str:
        return '<span class="mk on">●</span>' if ok else '<span class="mk off">○</span>'

    rows = ""
    for v in vids:
        a = v.analysis
        assert a is not None
        flags = dict(_kw_flags(d.ctx.fact(v.meta.rank)))
        band = _coherence_band(a)
        score = a.message_coherence if a.message_coherence is not None else "—"
        note = a.divergence_note or a.reinforcement_note or ""
        lm = a.layer_messages
        tip = ""
        if lm:
            tip = _esc(f"テロップ:{lm.telop} / キャプ:{lm.caption} / 映像:{lm.visual}")
        rows += (
            f'<tr><td class="rkc">#{v.meta.rank}</td>'
            f"<td>{cellmark(flags['テロップ'])}</td><td>{cellmark(flags['キャプ'])}</td>"
            f"<td>{cellmark(flags['音声'])}</td>"
            f'<td title="{tip}"><span class="band {_BAND_CLS.get(band, "na")}">{_esc(band)}</span>'
            f'<span class="muted small"> {score}</span></td>'
            f'<td class="notc">{_esc(note)}</td></tr>'
        )
    consensus = _kw_consensus(d) + "（テロップ・キャプションは照合済み・発話は AI 聞き取り）"
    bands = [_coherence_band(v.analysis) for v in vids if v.analysis]
    # 上位が一貫性で横並びなら「順位を分けた要因ではない＝前提」と正直に読ませる
    read = (
        "上位は一貫性で横並び＝これは上位に共通する<b>前提</b>で、順位の差の要因は未特定"
        "（下位の動画は未分析）。"
        if bands and all(b in ("一貫", "概ね一貫") for b in bands)
        else "KW一致は検索適合の必要条件と仮定（TikTok内部重みは非公開で断定不可）。"
    )
    return (
        '<section><div class="th big">一貫性マトリクス（テロップ↔キャプション↔映像中身・検索KW）</div>'
        '<div class="tblwrap"><table class="tbl matrix2"><thead><tr><th>順</th><th>テロップ↔KW</th>'
        "<th>キャプ↔KW</th><th>音声↔KW</th><th>メッセージ一貫性</th><th>補強 / ズレ（一言）</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
        f'<div class="muted small mtop">共通解: {_esc(consensus)}。{read}</div>'
        "</section>"
    )


# ===========================================================
# G 各動画ドリルダウン（大型インタラクティブ・タイムライン＋タブ）
# ===========================================================
def _filtered_frames(v: AnalyzedVideo) -> list[FrameShot]:
    return [f for f in v.frames if f.data_uri.startswith("data:image/")]


def frame_label(a: VideoVSEOAnalysis | None, sec: float) -> str:
    """コマの見出し「24.0秒｜手順」（秒と場面の役割だけ）。

    ブランド名・「KWテロップ」は見出しに付けない（Gemini の秒は 1〜2 秒ずれ、そのコマに写って
    いないことがあった）。役割は場面の欄が無ければコードの推定。
    """
    role = ""
    if a is not None and a.scenes:
        scenes = sorted(a.scenes, key=lambda sc: (sc.start_sec, sc.end_sec))
        roles = infer_roles(a)
        # 場面の隙間の秒は、境目がいちばん近い場面の役割にする（開始秒だけで比べない）。
        idx = scene_index_at(scenes, sec)
        if idx is not None:
            role = ROLE_LABEL.get(roles[idx][0], ROLE_LABEL["other"])
    return f"{sec:.1f}秒｜{role}" if role else f"{sec:.1f}秒"


def _kw_secs(f: VideoFacts | None) -> tuple[set[float], set[float]]:
    """テロップの秒（検索語の完全一致・照合済みの言い換え）。Gemini の kw_match は使わない。"""
    if f is None:
        return set(), set()
    exact = {s for h in f.kw if h.layer == "telop" and h.match == "exact" for s in h.secs}
    syn = {s for h in f.kw if h.layer == "telop" and h.match == "synonym" for s in h.secs}
    return exact, syn - exact


def _tc(sec: float) -> str:
    """秒 → タイムコード mm:ss.s（Premiere風表示）。"""
    sec = max(0.0, sec)
    return f"{int(sec // 60):02d}:{sec % 60:04.1f}"


def _nice_step(dur: float) -> float:
    """ルーラ目盛の丸い間隔（5〜8目盛に収める）。"""
    for s in (1, 2, 5, 10, 15, 30, 60):
        if dur / s <= 8:
            return float(s)
    return 60.0


def _clip(left: float, width: float, label: str, cls: str, *, kw: bool = False) -> str:
    w = max(0.8, min(100.0 - left, width))
    k = " kw" if kw else ""
    return (
        f'<div class="nclip {cls}{k}" style="left:{left:.2f}%;width:{w:.2f}%">'
        f'<span class="nclab">{label}</span></div>'
    )


def _timeline_hero(v: AnalyzedVideo, idx: int, d: Deck) -> str:
    a = v.analysis
    if a is None or a.duration_sec <= 0:
        return '<div class="muted small">タイムライン: 尺不明のため省略</div>'
    dur = a.duration_sec
    exact, syn = _kw_secs(d.ctx.fact(v.meta.rank))
    kw_secs = exact | syn
    lim = dur * 1.02  # 尺超過の秒（Gemini推定ブレ）は描かない
    fr = _filtered_frames(v)

    # V1 構成（フック0-3s + CTA）
    comp = _clip(0.0, _pct(3.0, dur), "フック", "c-hook")
    if a.cta_sec is not None and a.cta_sec <= lim:
        comp += _clip(_pct(a.cta_sec, dur), 4.0, "CTA", "c-cta")

    # V2 テロップ（字幕クリップ＝各テロップを次のテロップ秒まで伸ばす）
    tl = sorted((t for t in a.telops if t.sec <= lim), key=lambda x: x.sec)
    telop = ""
    for i, t in enumerate(tl):
        nxt = tl[i + 1].sec if i + 1 < len(tl) else dur
        left = _pct(t.sec, dur)
        telop += _clip(
            left, _pct(nxt, dur) - left, _esc(t.text[:18]), "c-telop", kw=t.sec in kw_secs
        )

    # V3 ブランド/物体
    brand = ""
    for b in a.brand_detections:
        for s in b.appear_sec or [0.0]:
            if s > lim:
                continue
            # 区分は名簿（コード）で決める。Gemini の brand_relation は使わない。
            relation = d.roster.relation(b.brand_name)
            cls = "c-brand comp" if relation == "competitor" else "c-brand"
            brand += _clip(
                _pct(s, dur),
                _pct(max(b.total_screen_time_sec, 1.5), dur),
                _esc(b.brand_name or "ブランド"),
                cls,
            )

    # V4 シーン
    scene = ""
    for sc in a.scenes:
        if sc.start_sec > lim:
            continue
        left = _pct(sc.start_sec, dur)
        scene += _clip(left, _pct(min(sc.end_sec, dur), dur) - left, _esc(sc.desc[:20]), "c-scene")

    # ルーラ（タイムコード）＋抽出フレーム位置◆
    step = _nice_step(dur)
    n_ticks = int(dur / step) + 1
    tick_secs = [i * step for i in range(n_ticks + 1) if i * step <= dur + 0.001]
    # 最後の目盛りはラベルを左側へ寄せる（尺が目盛り間隔の倍数だと右端 100% に来て、
    # 中央寄せのラベルが枠の外へはみ出し、PC 幅でもタイムラインに横スクロールが出るため）
    ticks = "".join(
        f'<span class="ntick{" nend" if i == len(tick_secs) - 1 and i > 0 else ""}" '
        f'style="left:{_pct(s, dur):.2f}%">{_tc(s)}</span>'
        for i, s in enumerate(tick_secs)
    )
    fticks = "".join(
        f'<span class="nftick" style="left:{_pct(f.sec, dur):.2f}%" title="抽出フレーム {f.sec:.1f}s"></span>'
        for f in fr
    )
    payload = {
        "dur": round(dur, 2),
        "url": v.meta.url,
        "frames": [
            {"sec": round(f.sec, 2), "cap": frame_label(a, f.sec), "fi": i}
            for i, f in enumerate(fr)
        ],
        "telops": [
            {
                "sec": round(t.sec, 2),
                "pos": _POS_JP.get(t.position, "?"),
                "text": t.text,
                "kw": t.sec in kw_secs,
            }
            for t in a.telops
        ],
    }
    if v.video_data_uri:
        # 実再生できる軽量プレビュー動画（タイムラインの再生ヘッドと双方向同期）
        screen = (
            f'<video class="nvid" src="{v.video_data_uri}" preload="metadata" playsinline></video>'
            '<button class="nplay" type="button" aria-label="再生 / 一時停止">▶</button>'
            f'<div class="ntcbar"><span class="ncur">00:00.0</span><span class="ndur">/ {_tc(dur)}</span></div>'
        )
        label = "PROGRAM ▶ 実再生"
    else:
        screen = (
            '<img class="nimg" alt="">'
            f'<div class="ntcbar"><span class="ncur">00:00.0</span><span class="ndur">/ {_tc(dur)}</span></div>'
        )
        label = "PROGRAM（静止フレーム）"
    monitor = (
        '<div class="nmon" hidden>'
        f'<div class="nscreen">{screen}</div>'
        f'<div class="nside"><div class="nslabel">{label}</div>'
        '<div class="ncap"></div><div class="nnote muted small"></div>'
        '<a class="ndeep" href="#" target="_blank" rel="noopener">▶ 元動画を開く</a></div></div>'
    )
    tracks = (
        '<div class="nbody"><div class="ntheads">'
        '<div class="nthead">V1 構成</div><div class="nthead">V2 テロップ</div>'
        '<div class="nthead">V3 ブランド</div><div class="nthead">V4 シーン</div></div>'
        '<div class="nlanes">'
        f'<div class="nlane">{comp}</div><div class="nlane">{telop}</div>'
        f'<div class="nlane">{brand}</div><div class="nlane">{scene}</div>'
        '<div class="nplayhead" hidden></div><div class="nscrub" tabindex="0" role="slider" '
        f'aria-label="タイムライン 0〜{dur:.0f}秒" aria-valuemin="0" aria-valuemax="{dur:.0f}" '
        'aria-valuenow="0" aria-valuetext="00:00.0"></div></div></div>'
    )
    legend = (
        '<div class="nlegend"><span class="lg c-hook"></span>フック'
        '<span class="lg c-telop"></span>テロップ<span class="lg c-telop kw"></span>KW一致'
        '<span class="lg c-brand"></span>ブランド<span class="lg c-brand comp"></span>競合ブランド（縞）'
        '<span class="lg c-scene"></span>シーン'
        '<span class="nft">◆</span>抽出フレーム'
        '<span class="muted">　ルーラ/トラックをクリック・ドラッグで再生ヘッドを移動→該当フレーム表示</span></div>'
    )
    return (
        f'<div class="nle" data-vtl data-i="{idx}">{monitor}'
        f'<div class="nrulerwrap"><div class="ncorner">TC</div>'
        f'<div class="nruler">{ticks}{fticks}</div></div>{tracks}'
        f'<script type="application/json" class="tldata">{_json_attr(payload)}</script>'
        f"{_frame_strip(fr, a)}{legend}</div>"
    )


def _frame_strip(fr: list[FrameShot], a: VideoVSEOAnalysis | None) -> str:
    if not fr:
        return ""
    cells = "".join(
        f'<figure class="frm" data-fi="{i}"><img src="{f.data_uri}" alt="{_esc(frame_label(a, f.sec))}">'
        f"<figcaption>{_esc(frame_label(a, f.sec))}</figcaption></figure>"
        for i, f in enumerate(fr)
    )
    return f'<div class="frmstrip" role="tablist" aria-label="抽出フレーム">{cells}</div>'


def _kw_mark(sec: float, exact: set[float], syn: set[float]) -> str:
    if sec in exact:
        return "✓"
    return "≈" if sec in syn else ""


def _tabs(v: AnalyzedVideo, idx: int, d: Deck) -> str:
    a = v.analysis
    assert a is not None
    # テロップは動画の流れのとおり秒の昇順（KW 一致を先頭に並べ替えない）。KW 列は照合済み:
    # ✓＝検索語がテロップ本文に実在（完全一致）、≈＝言い換え（前後 2 秒のテロップに実在）。
    exact, syn = _kw_secs(d.ctx.fact(v.meta.rank))
    telop_rows = "".join(
        f'<tr class="{"hit" if _kw_mark(t.sec, exact, syn) else ""}"><td>{t.sec:.1f}秒</td>'
        f"<td>{_kw_mark(t.sec, exact, syn)}</td><td>{_esc(t.text)}</td></tr>"
        for t in sorted(a.telops, key=lambda x: x.sec)
    )
    telop_tbl = (
        '<div class="tscroll"><table class="tbl"><thead><tr><th>秒</th><th>KW</th><th>内容</th>'
        "</tr></thead>"
        f"<tbody>{telop_rows or '<tr><td colspan=3 class=muted>検出なし</td></tr>'}</tbody></table></div>"
        '<div class="muted small">KW: ✓＝検索語がテロップにそのまま出る（完全一致）・'
        "≈＝言い換え（前後2秒のテロップに実在するものだけ）</div>"
    )
    comp = _competitor_html(a, d.roster)
    brand_rows = "".join(
        f'<tr class="{"comp" if d.roster.relation(b.brand_name) == "competitor" else ""}">'
        f"<td>{','.join(f'{s:.0f}' for s in b.appear_sec) or '?'}s</td><td>{_esc(b.brand_name)}</td>"
        f"<td>{_esc(_SRC_JP.get(b.detection_source, b.detection_source))}</td>"
        f"<td>{_esc(_PROM_JP.get(b.prominence, b.prominence))}</td>"
        f"<td>{_esc(_INTENT_JP.get(b.is_intentional, b.is_intentional))}</td></tr>"
        for b in a.brand_detections
    )
    brand_tbl = (
        f'{comp}<div class="tblwrap"><table class="tbl"><thead><tr><th>秒</th><th>名称</th><th>場所</th>'
        "<th>目立ち</th><th>意図</th></tr></thead>"
        f"<tbody>{brand_rows or '<tr><td colspan=5 class=muted>検出なし</td></tr>'}</tbody></table></div>"
    )
    # 数値KPI・勝因は pane 上部に移したので、ここは解説テキストのみ
    metrics_pane = (
        f'<div class="kvrow"><b>主訴求</b>{_esc(a.main_message) or "—"}　<b>テンポ</b>'
        f"{_esc(PACING_LABEL.get(a.pacing, a.pacing))}</div>"
        f'<div class="kvrow"><b>フック</b>{_esc(a.hook_summary) or "—"}</div>'
        f'<div class="kvrow"><b>キャプション関連性</b>{_esc(a.caption_relevance) or "—"}</div>'
    )
    # 既定タブは「主訴求・解説」（テロップ逐語ダンプを初手で見せない）
    return (
        f'<div class="tabs" data-tabs data-i="{idx}">'
        '<button class="tab on" data-tab="m">主訴求・解説</button>'
        '<button class="tab" data-tab="t">テロップ全文</button>'
        '<button class="tab" data-tab="b">ブランド/物体</button></div>'
        f'<div class="tabpane show" data-pane="m" data-i="{idx}">{metrics_pane}</div>'
        f'<div class="tabpane" data-pane="t" data-i="{idx}">{telop_tbl}</div>'
        f'<div class="tabpane" data-pane="b" data-i="{idx}">{brand_tbl}</div>'
    )


def _competitor_html(a: VideoVSEOAnalysis, roster: Roster) -> str:
    comp = [b for b in a.brand_detections if roster.relation(b.brand_name) == "competitor"]
    if not comp:
        return ""
    items = "、".join(
        f"{_esc(b.brand_name)}（{_esc(_PROM_JP.get(b.prominence, b.prominence))}/"
        f"{','.join(f'{s:.0f}' for s in b.appear_sec) or '?'}s）"
        for b in comp
    )
    return (
        f'<div class="warn">⚠️ <b>競合ブランドの映り込み</b>: {items}'
        '<span class="muted small">　背景の競合看板/ロゴもOCR/ロゴ検出の対象になりうる</span></div>'
    )


def _stat(value: str, label: str, *, kpi: bool = False) -> str:
    return f'<div class="st{" kpi" if kpi else ""}"><b>{value}</b><i>{label}</i></div>'


def _video_tab_btn(v: AnalyzedVideo, idx: int) -> str:
    """トップタブの個別動画ボタン。"""
    return (
        f'<button class="toptab" type="button" data-tt="v{idx}">'
        f'<span class="ttrank">#{v.meta.rank}</span>@{_esc(v.meta.author) or "—"}</button>'
    )


def _image_post_tab_btn(meta: VideoMeta, idx: int) -> str:
    """トップタブの画像投稿ボタン。動画タブとはアイコンと配色で区別する。"""
    return (
        f'<button class="toptab imageposttab" type="button" data-tt="i{idx}">'
        f"📷 #{meta.rank}</button>"
    )


def _image_post_pane(meta: VideoMeta) -> str:
    """取得済みメタと1枚目サムネだけで画像投稿の個別 pane を作る。"""
    cover_url = _http_image_url(meta.cover_url)
    cover = (
        f'<div class="ipcover"><img src="{_esc(cover_url)}" '
        f'alt="画像投稿 #{meta.rank} の1枚目サムネ" loading="lazy"></div>'
        if cover_url
        else '<div class="ipcover ph"><span>サムネイルを表示できません</span></div>'
    )
    head = (
        f'<div class="vphead iphead"><span class="rank iprank">📷 #{meta.rank}</span>'
        f'<span class="ipauthor"><b>投稿者</b> @{_esc(meta.author) or "—"}</span></div>'
    )
    kpi = (
        '<div class="vpkpi"><div class="engage big">'
        + _stat(_fmt(meta.follower_count), "フォロワー")
        + _stat(_fmt(meta.play_count), "再生")
        + _stat(_fmt(meta.digg_count), "いいね")
        + _stat(_fmt(meta.collect_count), "保存")
        + _stat(f"{meta.save_rate():.2f}%", "保存率", kpi=True)
        + _stat(f"{meta.engagement_rate:.1f}%", "エンゲージメント率", kpi=True)
        + "</div></div>"
    )
    caption = (
        '<div class="ipcaption"><div class="th big">キャプション全文</div>'
        f'<div class="ipcaptionbody">{_esc(meta.desc) or "—"}</div></div>'
    )
    notice = (
        '<div class="ipnotice">この投稿は画像投稿（カルーセル）のため、動画の深掘り分析'
        "（テロップ・フック・カメラワーク）は行っていません。</div>"
    )
    return f'<div class="vpane imagepostpane">{head}{cover}{kpi}{caption}{notice}</div>'


def _video_pane(v: AnalyzedVideo, idx: int, d: Deck) -> str:
    """個別レポート1本分（上部に数値KPI → 大型タイムライン動画プレーヤー → 詳細タブ）。"""
    m = v.meta
    a = v.analysis
    assert a is not None
    fact = d.ctx.fact(m.rank)
    pr = (
        f'{_pr_mark(fact)}<span class="muted small">{_esc(fact.pr_evidence)}</span>'
        if fact is not None and fact.pr
        else ""
    )
    head = (
        f'<div class="vphead"><span class="rank">#{m.rank}</span>'
        f'<a href="{_esc(m.url)}" target="_blank" rel="noopener">@{_esc(m.author) or "—"}</a>'
        f"{pr}"
        f'<span class="vpmsg">{_esc(a.main_message) or _esc(a.hook_summary)}</span></div>'
    )
    # 上部に実数値を大きく（エンゲージ/保存率など）→ その下にタイムライン
    kpi = (
        '<div class="vpkpi"><div class="engage big">'
        + _stat(_fmt(m.play_count), "再生")
        + _stat(_fmt(m.digg_count), "いいね")
        + _stat(_fmt(m.collect_count), "保存")
        + _stat(_fmt(m.share_count), "シェア")
        + _stat(f"{m.save_rate():.2f}%", "保存率", kpi=True)
        + (
            _stat(f"{m.engagement_rate:.1f}%", "エンゲージメント率", kpi=True)
            if m.engagement_rate > 0
            else ""
        )
        + _stat(f"{f.duration_sec if (f := d.ctx.fact(m.rank)) else a.duration_sec:.0f}秒", "尺")
        + "</div></div>"
    )
    # 勝因は AI の所見。測っていない指標（視聴維持率・離脱など）や統計の語を含むものは出さない。
    wins = "".join(
        f'<span class="wchip">{_esc(w)}</span>' for w in a.win_factors[:4] if deny_hit(w) is None
    )
    wins_html = (
        f'<div class="vpwins"><span class="muted small">AI の所見:</span>{wins}</div>'
        if wins
        else ""
    )
    return (
        f'<div class="vpane">{head}{kpi}{wins_html}{_timeline_hero(v, idx, d)}'
        f"{_tabs(v, idx, d)}</div>"
    )


# ===========================================================
# H 統計付録（既定クローズ）
# ===========================================================
_RHO_WEAK = 0.3


def _corr_bar(rho: float | None) -> str:
    if rho is None:
        return '<span class="muted small">データ不足</span>'
    w = min(60.0, abs(rho) * 60)
    fill = (
        f'<i class="fillpos" style="width:{w:.0f}px"></i>'
        if rho >= 0
        else f'<i class="fillneg" style="width:{w:.0f}px"></i>'
    )
    return f'<span class="bar"><span class="mid"></span>{fill}</span>'


def _frac(val: str) -> int:
    try:
        a, b = val.split("/")
        return int(int(a) / int(b) * 100) if int(b) else 0
    except (ValueError, ZeroDivisionError):
        return 0


def rho_direction(rho: float | None) -> str:
    """相関の向きの文（コードが作る）。順位は 1 が最上位なので ρ<0 は「値が大きいほど上位」。"""
    if rho is None:
        return "判定できない"
    if abs(rho) < _RHO_WEAK:
        return "向きは弱い（参考にならない）"
    return "値が大きい動画ほど上位（参考）" if rho < 0 else "値が小さい動画ほど上位（参考）"


def _stats_block(s: StatsAnalysis | None) -> str:
    """統計付録。相関（特徴×表示順位）は本文に出さず、ここにだけ表で出す（有意性なし・参考）。"""
    if s is None or s.sample_size == 0:
        return ""
    # n<3 で相関が全て算出不能なら「空の相関表」を見せない（恥/不信を避ける）
    has_rho = any(c.rho is not None for c in s.correlations)
    if has_rho:
        corr_rows = "".join(
            f"<tr><td>{_esc(c.feature)}</td><td>{'' if c.rho is None else f'{c.rho:+.2f}'}</td>"
            f"<td>{_corr_bar(c.rho)} {_esc(rho_direction(c.rho))}</td>"
            f"<td>{c.monotonic_hits}/{c.monotonic_total}</td></tr>"
            for c in s.correlations
        )
        corr_tbl = (
            f'<div class="th">特徴×表示順位（参考・n={s.sample_size}・有意性なし）</div>'
            '<div class="tblwrap"><table class="tbl"><thead><tr><th>特徴</th><th>ρ</th>'
            "<th>向き（コードの判定）</th><th>単調性</th></tr></thead>"
            f"<tbody>{corr_rows}</tbody></table></div>"
            '<div class="muted small">Spearman の順位相関。本数が少なく因果ではないため、本文の'
            "結論・指示には使っていません。</div>"
        )
    else:
        corr_tbl = (
            '<div class="th">特徴量 × 順位の相関</div>'
            f'<div class="muted small">相関分析には n≥3 が必要（現在 n={s.sample_size}）。'
            "本数が増えると Spearman ρ で順位への効きを点推定します。</div>"
        )
    dist_rows = "".join(
        f"<tr><td>{_esc(d.feature)}</td><td>中央値 {d.median}</td><td>範囲 {d.min}–{d.max}</td>"
        f"<td>{('#' + str(d.outlier_rank) + ' 突出(' + str(d.outlier_value) + ' / ' + _esc(d.outlier_note) + ')') if d.outlier_rank else ''}</td></tr>"
        for d in s.distributions
    )
    dist_tbl = (
        '<div class="th">分布と外れ値（中央値中心）</div>'
        f'<div class="tblwrap"><table class="tbl"><tbody>{dist_rows}</tbody></table></div>'
    )
    kc = s.kw_coverage
    fill_bars = "".join(
        f'<div class="cov"><span class="covlab">{_esc(name)}</span>'
        f'<span class="hbar"><i style="width:{_frac(val)}%"></i></span>'
        f'<span class="covn">{_esc(val)}</span></div>'
        for name, val in kc.layer_fill
    )
    cov_block = (
        f'<div class="th">KWカバレッジ（4層・平均 {kc.avg_score_0_100:.0f}/100）</div>{fill_bars}'
        f'<div class="muted small">動画別: {_esc(" / ".join(kc.per_video))}</div>'
    )
    hooks = "　".join(f"{_esc(_HOOK_JP.get(h, h))} {c}本" for h, c in s.hook_counts)
    hook_block = (
        f'<div class="th">フック型の分布（本数）</div><div class="kvrow">{hooks or "—"}</div>'
    )
    # 特徴量マトリクスは Top5比較ボードと重複するので付録では出さない。
    caveats_html = (
        '<ul class="caveats">'
        + "".join(f"<li>{_esc(caveat)}</li>" for caveat in s.caveats)
        + "</ul>"
        if s.caveats
        else ""
    )
    return (
        '<details class="appendix"><summary class="th big">'
        f"統計付録（n={s.sample_size}・有意性なし／クリックで展開）</summary>"
        f'<div class="stats-grid"><div>{corr_tbl}{dist_tbl}</div>'
        f"<div>{cov_block}{hook_block}</div></div>{caveats_html}</details>"
    )


# DADS（_html/dads.py）のトークン＋基本部品の上に載せる、このレポート固有の部品。
# 色はすべて DADS トークン経由（直書きの hex を持たない）。--va-* は用途別の別名で、値は必ず
# DADS トークンを指す。文字は 14px 以上・白地で 4.5:1 以上、線は solid-gray-420（3:1 以上）。
# 色だけで意味を伝えない（確信度の丸は形も変える・KW 一致は ✓ を添える・競合は縞模様）。
_STYLE = """
:root{--va-ink:var(--color-neutral-solid-gray-800);--va-ink-strong:var(--color-neutral-solid-gray-900);
 --va-sub:var(--color-neutral-solid-gray-600);--va-line:var(--color-neutral-solid-gray-420);
 --va-line-soft:var(--color-neutral-solid-gray-200);--va-soft:var(--color-neutral-solid-gray-50);
 --va-accent:var(--color-primitive-blue-900);--va-link:var(--color-primitive-blue-1000);
 --va-warn:var(--color-primitive-orange-800);--va-warn-ink:var(--color-primitive-yellow-1000);
 --va-good:var(--color-primitive-green-800);--va-white:var(--color-neutral-white);--va-maxw:1440px}
body{max-width:var(--va-maxw);margin:0 auto;padding:40px 32px 80px;overflow-wrap:break-word}
h1{margin:0 0 4px}
.meta{color:var(--va-sub);font-size:16px;margin:0 0 16px}
section{margin:0 0 56px}
button{font-family:inherit}
button:focus-visible,.nscrub:focus-visible,.frm:focus-visible,summary:focus-visible{
 outline:4px solid var(--color-neutral-black);outline-offset:2px}
/* トップタブ（統計 ⇄ 個別レポート） */
.toptabs{display:flex;gap:4px;flex-wrap:wrap;border-bottom:1px solid var(--va-line);margin:8px 0 32px;
 position:sticky;top:0;background:var(--va-white);z-index:20;padding-top:8px}
.toptab{background:none;border:none;border-bottom:4px solid transparent;padding:8px 16px;font-size:16px;
 font-weight:700;color:var(--va-sub);cursor:pointer;border-radius:var(--border-radius-8) var(--border-radius-8) 0 0;
 display:inline-flex;align-items:center;gap:8px;min-width:0;max-width:100%;overflow-wrap:anywhere;text-align:left}
.toptab:hover{background:var(--va-soft);color:var(--va-ink)}
.toptab.on{color:var(--va-accent);border-bottom-color:var(--va-accent);background:var(--color-primitive-blue-50)}
.toptab .ttrank{font-weight:700;background:var(--va-sub);color:var(--va-white);
 border-radius:var(--border-radius-4);padding:0 6px;font-size:14px;flex:none;white-space:nowrap}
.toptab.on .ttrank{background:var(--va-accent)}
.ttpane{display:none}.ttpane.show{display:block;animation:ttfade .18s ease}
@keyframes ttfade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.vpane{margin:0 0 8px}
.vphead{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding-bottom:12px;margin-bottom:16px;
 border-bottom:1px solid var(--va-line)}
.vphead a{font-weight:700;font-size:20px;min-width:0;overflow-wrap:anywhere}
.vpmsg{color:var(--va-sub);font-size:16px;flex:1;min-width:160px}
.vpwins{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}
.wchip{background:var(--va-white);border:1px solid var(--va-line);border-radius:999px;padding:2px 12px;
 font-size:14px;color:var(--va-ink)}
.vpkpi{background:var(--va-soft);border:1px solid var(--va-line);border-radius:var(--border-radius-8);
 padding:16px;margin:0 0 16px}
.engage.big{grid-template-columns:repeat(auto-fit,minmax(104px,1fr));gap:12px;margin-bottom:0}
.engage.big .st b{font-size:28px;line-height:1.4}.engage.big .st i{font-size:14px}
.th{font-size:20px;line-height:1.5;font-weight:700;color:var(--va-ink-strong);margin:0 0 12px}
.th.big{font-size:24px;border-bottom:1px solid var(--va-line);padding-bottom:8px;margin-bottom:16px}
.small{font-size:14px}.muted{color:var(--va-sub)}
/* 確信度: 高=塗り・中=半分・低=輪郭のみ（色＋形で区別） */
.cdot{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:6px;vertical-align:middle;
 border:2px solid var(--va-good);box-sizing:border-box}
.cdot.c-hi{background:var(--va-good)}
.cdot.c-mid{border-color:var(--color-primitive-yellow-900);
 background:linear-gradient(90deg,var(--color-primitive-yellow-900) 50%,transparent 50%)}
.cdot.c-lo{border-color:var(--color-neutral-solid-gray-536);background:transparent}
.rk{display:inline-block;background:var(--va-soft);color:var(--va-sub);border:1px solid var(--va-line-soft);
 border-radius:var(--border-radius-4);padding:0 6px;margin:0 4px 2px 0;font-size:14px;font-weight:700}
/* B 結論バンド */
.verdict{display:grid;grid-template-columns:1.55fr 1fr;gap:32px;border:2px solid var(--va-accent);
 border-radius:var(--border-radius-12);background:var(--color-primitive-blue-50);padding:24px;margin-bottom:56px}
.verdict .th{font-size:16px;color:var(--va-accent);margin-bottom:8px}
.vbig{font-size:32px;font-weight:700;line-height:1.5;margin:0 0 16px;color:var(--va-ink-strong)}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;gap:2px;background:var(--va-white);border:1px solid var(--va-line);
 border-radius:var(--border-radius-8);padding:4px 12px;font-size:14px}
.chip b{font-weight:700}.chip i{font-style:normal;color:var(--va-sub);font-size:14px;margin-left:6px;white-space:nowrap}
.chip .tname{font-weight:700;color:var(--va-accent);margin-right:6px;white-space:nowrap}
.sbpr{display:inline-block;margin-left:4px;border:1px solid var(--color-primitive-yellow-900);
 background:var(--color-primitive-yellow-50);color:var(--va-warn-ink);border-radius:var(--border-radius-4);
 padding:0 4px;font-size:14px;font-weight:700;line-height:1.3}
.nexts{margin:0;padding:0;list-style:none}
.nexts li{position:relative;padding:8px 0 8px 28px;font-size:16px;border-bottom:1px solid var(--va-line)}
.nexts li:before{content:'☐';position:absolute;left:2px;color:var(--va-accent);font-size:16px}
.nbanner{background:var(--color-primitive-yellow-50);border:1px solid var(--color-primitive-yellow-900);
 border-left-width:8px;color:var(--va-ink-strong);border-radius:var(--border-radius-8);padding:12px 16px;
 font-size:16px;margin:0 0 24px}
.vsub{font-size:16px;color:var(--va-ink);line-height:1.7;margin:-8px 0 16px}
.verdict.planner .vbig{font-size:28px}
.pitch{margin-top:16px;background:var(--va-white);border:1px solid var(--va-accent);border-left-width:8px;
 border-radius:var(--border-radius-8);padding:12px 16px;font-size:16px;color:var(--va-link);line-height:1.7}
.tscroll{max-height:320px;overflow:auto;border:1px solid var(--va-line);border-radius:var(--border-radius-8)}
.drill-fail{border:1px solid var(--va-line);border-radius:var(--border-radius-8);padding:12px 16px;
 color:var(--va-sub);font-size:16px;margin:0 0 12px;background:var(--va-white)}
/* B0 取得ボード（メタ一覧 board_size 本） */
.sbwrap{max-height:520px;overflow:auto;border:1px solid var(--va-line);border-radius:var(--border-radius-8)}
.sboard{width:100%;border-collapse:collapse;font-size:14px;line-height:1.5}
.sboard thead th{position:sticky;top:0;background:var(--va-soft);color:var(--va-ink);font-weight:700;
 padding:8px 12px;text-align:left;border-bottom:1px solid var(--va-line);white-space:nowrap;z-index:1}
.sboard td{padding:8px 12px;border-bottom:1px solid var(--va-line-soft);vertical-align:middle}
.sboard tbody tr:hover{background:var(--va-soft)}
.sbr{font-weight:700;white-space:nowrap;color:var(--va-ink)}
.sbdeep{margin-left:4px;color:var(--va-accent)}
.sbtdth{padding:4px 12px!important}
.sbth{width:42px;height:56px;object-fit:cover;border-radius:var(--border-radius-4);display:block;
 background:var(--va-soft)}
.sbth.ph{background:repeating-linear-gradient(45deg,var(--va-soft),var(--va-soft) 6px,
 var(--color-neutral-solid-gray-100) 6px,var(--color-neutral-solid-gray-100) 12px)}
.sbauth a{font-weight:700;white-space:nowrap}
.sbnum{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.sbcap{color:var(--va-sub);min-width:200px;max-width:320px}
/* C Top5ボード（ラベル列と各動画の列を subgrid で同じ 9 行に載せ、どこかのセルが 2 行に
   折り返しても横一列の高さがそろう。狭い画面は枠内で横スクロール） */
.board{display:grid;gap:0;border:1px solid var(--va-line);border-radius:var(--border-radius-8);overflow-x:auto}
.blab,.bcol{display:grid;grid-row:span 9;grid-template-rows:subgrid;min-width:112px}
.blab{background:var(--va-soft)}
.bcol{border-left:1px solid var(--va-line);text-align:center}
.bcol.is-top{box-shadow:inset 0 4px 0 var(--va-accent)}
.blab>div,.bcol>div{padding:8px;border-bottom:1px solid var(--va-line-soft);min-height:44px;display:flex;
 align-items:center;justify-content:center}
.blab>div:last-child,.bcol>div:last-child{border-bottom:0}
.blab .rl{justify-content:flex-end;color:var(--va-ink);font-size:14px;font-weight:700;text-align:right}
.brank{font-weight:700;font-size:16px}.bcol.is-top .brank{color:var(--va-accent)}
.bthumb,.bthumb-lab{padding:8px!important;height:156px!important}
.bthumb-lab{font-size:14px;color:var(--va-ink);font-weight:700;justify-content:flex-end!important}
.bthumb img{width:78px;aspect-ratio:9/16;object-fit:cover;border-radius:var(--border-radius-4);
 border:1px solid var(--va-line);display:block}
.bthumb.ph{width:78px;height:138px;background:repeating-linear-gradient(45deg,var(--va-soft) 0 7px,
 var(--color-neutral-solid-gray-100) 7px 14px);border-radius:var(--border-radius-4);margin:0 auto}
.bauth{font-size:14px;color:var(--va-link);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
 display:block!important;line-height:28px}
.bm{font-size:14px;gap:8px}.bv{font-weight:700;white-space:nowrap}
.btag{background:var(--va-soft);border:1px solid var(--va-line-soft);border-radius:var(--border-radius-4);
 padding:0 8px;font-size:14px;color:var(--va-ink);min-width:0;overflow-wrap:anywhere}
.miniba{position:relative;width:46px;height:8px;background:var(--color-neutral-solid-gray-100);
 border-radius:var(--border-radius-4);overflow:hidden}
.miniba>i{position:absolute;left:0;top:0;bottom:0;background:var(--va-line);border-radius:var(--border-radius-4)}
.miniba.acc>i{background:var(--va-accent)}
/* KW 層: 一致=塗り・不一致=輪郭のみ（色＋形） */
.kwd{gap:6px}.kwd .d{width:12px;height:12px;border-radius:50%;box-sizing:border-box}
.kwd .d.on{background:var(--va-accent);border:2px solid var(--va-accent)}
.kwd .d.off{background:transparent;border:2px solid var(--color-neutral-solid-gray-536)}
/* D サムネ色 */
.tbconsensus{font-size:16px;margin-bottom:12px}
.tbrow{display:grid;gap:16px;overflow-x:auto;padding-bottom:4px}
.tbcell{border:1px solid var(--va-line);border-radius:var(--border-radius-8);padding:8px;display:flex;
 flex-direction:column;gap:8px;align-items:center;min-width:168px}
.tbshot img{width:100%;aspect-ratio:9/16;object-fit:cover;border-radius:var(--border-radius-4);display:block}
.tbshot.ph{width:100%;aspect-ratio:9/16;background:repeating-linear-gradient(45deg,var(--va-soft) 0 8px,
 var(--color-neutral-solid-gray-100) 8px 16px);border-radius:var(--border-radius-4)}
.tbsw{display:flex;width:100%;height:16px;border-radius:var(--border-radius-4);overflow:hidden;
 border:1px solid var(--va-line)}
.tbsw span{flex:1}
.tbm{display:flex;align-items:center;gap:6px;width:100%;font-size:14px}
.tbm .tbl{flex:0 0 32px;color:var(--va-sub)}
.tbar{position:relative;flex:1;height:8px;border-radius:var(--border-radius-4);
 background:linear-gradient(90deg,var(--va-ink-strong),var(--va-line),var(--va-white));border:1px solid var(--va-line)}
.tbar.wt{background:linear-gradient(90deg,var(--color-primitive-blue-800),var(--va-line),var(--va-warn))}
.tbar>i{position:absolute;top:-4px;width:2px;height:14px;background:var(--va-ink-strong);transform:translateX(-1px)}
.tbv{width:48px;text-align:right;color:var(--va-sub)}
/* E シンセシス */
.syn .th{margin-top:32px}.syn .th.big{margin-top:0}
.concepts{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.concept{border:1px solid var(--va-line);border-radius:var(--border-radius-8);padding:12px 16px}
.cphead{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:4px}
.cphead b{font-size:16px}.prev{font-size:14px;color:var(--va-sub);font-weight:700}
.vrefs{margin-top:8px}
.diffs{margin:4px 0 0;padding:0;list-style:none}.diffs li{font-size:16px;margin:4px 0}
.hyps{display:flex;flex-direction:column;gap:12px}
.hyp{border-left:4px solid var(--va-accent);padding:4px 0 4px 16px}
.hyphead{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}.hyphead b{font-size:18px}
.counter{font-size:16px;color:var(--va-warn);margin-top:4px}
.sowhat{font-size:16px;color:var(--va-ink-strong);margin-top:4px;font-weight:700}
/* F 一貫性マトリクス */
.matrix2 td,.matrix2 th{text-align:center}.matrix2 .rkc{font-weight:700}
.matrix2 .notc{text-align:left;color:var(--va-ink)}
.mk.on{color:var(--va-accent);font-size:16px}.mk.off{color:var(--color-neutral-solid-gray-536);font-size:16px}
.band{display:inline-block;border:1px solid currentColor;border-radius:var(--border-radius-4);padding:0 8px;
 font-size:14px;font-weight:700}
.band.ok{background:var(--color-primitive-green-50);color:var(--color-primitive-green-900)}
.band.mid{background:var(--color-primitive-yellow-50);color:var(--va-warn-ink)}
.band.lo{background:var(--color-primitive-red-50);color:var(--color-primitive-red-900)}
.band.na{background:var(--va-soft);color:var(--va-sub)}
.mtop{margin-top:8px}
/* G ドリルダウン */
.drill{border:1px solid var(--va-line);border-radius:var(--border-radius-8);margin:0 0 12px;background:var(--va-white)}
.drill>summary{cursor:pointer;list-style:none;padding:12px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.drill>summary::-webkit-details-marker{display:none}
.drill>summary:before{content:'▸';color:var(--va-sub);font-size:16px}
.drill[open]>summary:before{content:'▾'}
.rank{font-weight:700;background:var(--va-ink-strong);color:var(--va-white);border-radius:var(--border-radius-4);
 padding:2px 10px;font-size:16px}
.drill summary a{font-weight:700}
.sm-kpi{font-size:14px;color:var(--va-sub);background:var(--va-soft);border-radius:var(--border-radius-4);padding:0 8px}
.sm-msg{font-size:14px;color:var(--va-sub);flex:1;min-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.drillbody{padding:4px 16px 18px}
/* タイムライン（NLE構造）。狭い画面ではトラック部分だけ枠内で横スクロール */
.nle{background:var(--va-white);border:1px solid var(--va-line);border-radius:var(--border-radius-8);
 padding:16px;margin:8px 0 4px;color:var(--va-ink);overflow-x:auto}
.nmon{display:grid;grid-template-columns:300px minmax(0,1fr);gap:16px;margin-bottom:16px}
.nscreen{position:relative;width:300px;height:200px;background:var(--va-ink-strong);border-radius:var(--border-radius-8);
 overflow:hidden;display:flex;align-items:center;justify-content:center;border:1px solid var(--va-line)}
.nimg{max-width:100%;max-height:100%;object-fit:contain}
.nvid{max-width:100%;max-height:100%;background:var(--color-neutral-black)}
.nplay{position:absolute;left:8px;top:8px;width:40px;height:40px;border-radius:50%;border:2px solid var(--va-white);
 background:var(--va-ink-strong);color:var(--va-white);font-size:16px;line-height:1;cursor:pointer;z-index:3}
.nplay:hover{background:var(--va-accent)}
.ntcbar{position:absolute;left:8px;bottom:8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:14px;
 background:var(--va-ink-strong);padding:0 8px;border-radius:var(--border-radius-4);color:var(--va-white)}
.ncur{color:var(--color-primitive-yellow-300);font-weight:700}.ndur{color:var(--va-white);margin-left:4px}
.nside{display:flex;flex-direction:column;gap:8px}
.nslabel{font-size:14px;letter-spacing:.08em;color:var(--va-sub);font-weight:700}
.ncap{font-size:18px;font-weight:700;line-height:1.5;color:var(--va-ink-strong)}
.nnote{color:var(--va-sub)!important}
.ndeep{align-self:flex-start;display:inline-flex;padding:8px 16px;border-radius:var(--border-radius-8);
 background:var(--va-accent);color:var(--va-white);font-weight:700;text-decoration:none;font-size:16px}
.ndeep:hover{background:var(--color-primitive-blue-1100);color:var(--va-white)}
.nrulerwrap,.nbody{display:grid;grid-template-columns:104px 1fr;min-width:640px}
.ncorner{display:flex;align-items:center;padding-left:8px;font:700 14px ui-monospace,monospace;color:var(--va-sub);
 background:var(--va-soft);border-radius:var(--border-radius-4) 0 0 0}
.nruler{position:relative;height:30px;background:var(--va-soft);border-radius:0 var(--border-radius-4) 0 0;
 border-left:1px solid var(--va-line)}
.ntick{position:absolute;top:2px;transform:translateX(-50%);font:14px/1.2 ui-monospace,monospace;color:var(--va-sub)}
.ntick:after{content:'';position:absolute;left:50%;top:19px;width:1px;height:7px;background:var(--va-line)}
.ntick:first-child{transform:none;padding-left:4px}.ntick:first-child:after{left:0}
.ntick.nend{transform:translateX(-100%);padding-right:4px}.ntick.nend:after{left:auto;right:0}
.nftick{position:absolute;bottom:0;width:0;transform:translateX(-50%)}
.nftick:after{content:'◆';position:absolute;left:0;bottom:-2px;transform:translateX(-50%);font-size:14px;
 color:var(--va-accent)}
.ntheads{display:flex;flex-direction:column;gap:4px;padding-top:4px}
.nthead{height:32px;background:var(--va-soft);border:1px solid var(--va-line);border-right:none;
 border-radius:var(--border-radius-4) 0 0 var(--border-radius-4);display:flex;align-items:center;padding:0 8px;
 font-size:14px;color:var(--va-ink);font-weight:700;white-space:nowrap}
.nlanes{position:relative;display:flex;flex-direction:column;gap:4px;padding-top:4px;border-left:1px solid var(--va-line)}
.nlane{position:relative;height:32px;background:var(--va-soft);border-radius:0 var(--border-radius-4) var(--border-radius-4) 0;
 overflow:hidden}
.nclip{position:absolute;top:2px;bottom:2px;border-radius:var(--border-radius-4);display:flex;align-items:center;
 padding:0 6px;overflow:hidden;font-size:14px;color:var(--va-white);pointer-events:none;
 border:1px solid var(--va-white)}
.nclab{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.c-hook{background:var(--va-good)}.c-cta{background:var(--va-warn)}
.c-telop{background:var(--color-primitive-light-blue-900)}
.c-telop.kw{background:var(--va-accent)}
.c-telop.kw .nclab:before,.nlegend .lg.c-telop.kw:before{content:'✓';font-weight:700;margin-right:2px}
.c-brand{background:var(--color-primitive-purple-700)}
.c-brand.comp{background:repeating-linear-gradient(135deg,var(--color-primitive-purple-700) 0 6px,
 var(--va-ink-strong) 6px 12px);box-shadow:inset 0 0 0 2px var(--color-primitive-yellow-300)}
.c-scene{background:var(--va-sub)}
.nplayhead{position:absolute;top:0;bottom:0;width:2px;background:var(--color-primitive-red-900);
 box-shadow:0 0 0 1px var(--va-white);pointer-events:none;z-index:6;transition:left .06s linear}
.nplayhead:before{content:'';position:absolute;top:-2px;left:-5px;border-left:6px solid transparent;
 border-right:6px solid transparent;border-top:8px solid var(--color-primitive-red-900)}
.nscrub{position:absolute;inset:0;z-index:7;cursor:col-resize;background:transparent}
.frmstrip{display:flex;gap:8px;overflow-x:auto;padding:12px 2px 4px}
.frm{flex:0 0 auto;margin:0;width:104px;cursor:pointer}
.frm img{display:block;width:104px;height:auto;max-height:170px;object-fit:cover;border-radius:var(--border-radius-4);
 border:1px solid var(--va-line);background:var(--va-soft)}
.frm[aria-selected=true] img{outline:3px solid var(--va-accent);outline-offset:-1px}
.frm figcaption{font-size:14px;color:var(--va-sub);text-align:center;margin-top:4px;line-height:1.4}
.frm figcaption b{display:block;color:var(--va-ink-strong);font-size:14px}
.nlegend{font-size:14px;color:var(--va-ink);margin-top:12px;display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.nlegend .lg{display:inline-flex;align-items:center;justify-content:center;width:22px;height:16px;
 border-radius:var(--border-radius-4);margin-right:4px;vertical-align:middle;color:var(--va-white);font-size:14px;
 line-height:1}
.nlegend .lg.c-telop.kw:before{margin:0}
.nlegend .nft{color:var(--va-accent);margin-right:2px}.nlegend .muted{color:var(--va-sub)!important}
/* タブ */
.tabs{display:flex;gap:4px;flex-wrap:wrap;border-bottom:1px solid var(--va-line);margin-top:24px}
.tab{background:none;border:none;border-bottom:4px solid transparent;padding:8px 16px;font-size:16px;
 color:var(--va-sub);cursor:pointer}
.tab:hover{background:var(--va-soft);color:var(--va-ink)}
.tab.on{color:var(--va-accent);border-bottom-color:var(--va-accent);font-weight:700}
.tabpane{display:none;padding-top:16px}.tabpane.show{display:block}
.engage{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-bottom:8px}
.st{padding:4px 2px;text-align:center}.st b{display:block;font-size:20px;font-weight:700;line-height:1.3;
 font-variant-numeric:tabular-nums}
.st i{font-style:normal;font-size:14px;color:var(--va-sub)}.st.kpi b{color:var(--va-accent)}
/* 行・タグ・テーブル */
.kvrow{font-size:16px;margin:8px 0;color:var(--va-ink)}.kvrow b{color:var(--va-ink-strong);margin-right:8px}
.warn{font-size:16px;margin:0 0 12px;padding:12px 16px;background:var(--color-primitive-yellow-50);
 border:1px solid var(--color-primitive-yellow-900);border-left-width:8px;border-radius:var(--border-radius-8);
 color:var(--va-ink-strong)}
.tblwrap{overflow-x:auto}
.tbl{width:100%;border-collapse:collapse;font-size:14px;line-height:1.5;margin-bottom:4px}
.tbl th,.tbl td{border-bottom:1px solid var(--va-line-soft);padding:8px 12px;text-align:left;vertical-align:top}
.tbl th{background:var(--va-soft);color:var(--va-ink);font-weight:700;font-size:14px;
 border-bottom-color:var(--va-line);white-space:nowrap}
.tbl tr.hit td{background:var(--color-primitive-blue-50)}.tbl tr.comp td{background:var(--color-primitive-yellow-50)}
.wins{margin:4px 0 0;padding-left:18px}.wins li{font-size:16px;margin:2px 0}
.err{color:var(--color-primitive-red-900);font-size:16px}
/* H 統計付録 */
.appendix{border:1px solid var(--va-line);border-radius:var(--border-radius-8);padding:4px 24px;background:var(--va-white)}
.appendix>summary.th{cursor:pointer;padding:12px 0;margin-bottom:0;border-bottom:0}
.caveats{margin:16px 0 12px;padding-left:1.4em;color:var(--va-ink);font-size:14px;line-height:1.7}
.stats-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:24px;margin-top:8px}
.stats-grid .th{font-size:18px;margin-top:16px}
.bar{position:relative;height:8px;background:var(--color-neutral-solid-gray-100);border-radius:var(--border-radius-4);
 width:64px;display:inline-block;vertical-align:middle}
.bar .fillpos{position:absolute;left:50%;height:100%;background:var(--va-accent);border-radius:0 4px 4px 0}
.bar .fillneg{position:absolute;right:50%;height:100%;background:var(--va-warn);border-radius:4px 0 0 4px}
.bar .mid{position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--va-ink)}
.cov{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:14px}
.covlab{width:80px;color:var(--va-ink)}.covn{color:var(--va-sub);font-size:14px}
.hbar{position:relative;height:8px;width:120px;background:var(--color-neutral-solid-gray-100);
 border-radius:var(--border-radius-4);display:inline-block}
.hbar>i{position:absolute;left:0;top:0;bottom:0;background:var(--va-accent);border-radius:var(--border-radius-4)}
.note{font-size:14px;color:var(--va-sub)}
.dads-footnote p{margin:0 0 8px}
@media(max-width:1080px){.stats-grid{grid-template-columns:minmax(0,1fr)}.verdict{grid-template-columns:minmax(0,1fr)}
 .nmon{grid-template-columns:minmax(0,1fr)}.nscreen{width:100%}}
@media(max-width:640px){body{padding:24px 16px 64px}h1{font-size:28px}
 .vbig,.verdict.planner .vbig{font-size:24px}.verdict{padding:16px;gap:16px}.th.big{font-size:20px}
 .toptab{padding:8px 12px}.appendix{padding:4px 16px}}
@media(prefers-reduced-motion:reduce){.nplayhead{transition:none}.ttpane.show{animation:none}}
@media print{.toptabs{position:static}.board,.tbrow,.nle,.tblwrap{overflow:visible}}
"""

_IMAGE_POST_STYLE = """
.toptab.imageposttab{color:var(--va-warn-ink)}
.toptab.imageposttab.on{color:var(--va-warn-ink);border-bottom-color:var(--color-primitive-yellow-900);
 background:var(--color-primitive-yellow-50)}
.sbimage{margin-left:4px}
.imagepostpane{max-width:980px;margin:0 auto}
.iphead{border-bottom-color:var(--color-primitive-yellow-900)}
.iprank{background:var(--va-warn-ink)}
.ipauthor{font-size:20px;min-width:0;overflow-wrap:anywhere}.ipauthor b{font-size:14px;color:var(--va-sub);margin-right:8px}
.ipcover{display:flex;align-items:center;justify-content:center;min-height:360px;max-height:680px;
 background:var(--va-ink-strong);border:1px solid var(--va-line);border-radius:var(--border-radius-8);overflow:hidden;
 margin-bottom:16px}
.ipcover img{display:block;max-width:100%;max-height:680px;object-fit:contain}
.ipcover.ph{background:var(--va-soft);color:var(--va-sub);font-size:16px}
.ipcaption{border:1px solid var(--va-line);border-radius:var(--border-radius-8);padding:16px;
 background:var(--va-white);margin-top:16px}
.ipcaptionbody{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.8}
.ipnotice{margin-top:16px;padding:12px 16px;background:var(--color-primitive-yellow-50);
 border:1px solid var(--color-primitive-yellow-900);border-left-width:8px;border-radius:var(--border-radius-8);
 color:var(--va-ink-strong);font-size:16px}
"""

_TIMELINE_JS = r"""
(function(){
  function nearest(frames, sec){var b=0,bd=1e9;for(var i=0;i<frames.length;i++){var d=Math.abs(frames[i].sec-sec);if(d<bd){bd=d;b=i;}}return b;}
  function setupTabs(root){
    var box=root.closest('.vpane')||document;
    var btns=root.querySelectorAll('.tab');
    var panes=box.querySelectorAll('.tabpane');
    btns.forEach(function(b){b.addEventListener('click',function(){
      var k=b.getAttribute('data-tab');
      btns.forEach(function(x){x.classList.toggle('on',x===b);});
      panes.forEach(function(p){p.classList.toggle('show',p.getAttribute('data-pane')===k);});
    });});
  }
  function setupTL(root){
    var el=root.querySelector('script.tldata'); if(!el) return;
    var data; try{data=JSON.parse(el.textContent);}catch(e){return;}
    var frames=(data.frames||[]).slice().sort(function(a,b){return a.sec-b.sec;});
    var telops=(data.telops||[]).slice().sort(function(a,b){return a.sec-b.sec;});
    var imgs=root.querySelectorAll('.frmstrip img');
    var figs=root.querySelectorAll('.frmstrip .frm');
    var scrub=root.querySelector('.nscrub');
    var head=root.querySelector('.nplayhead');
    var mon=root.querySelector('.nmon');
    var mImg=root.querySelector('.nimg');
    var mCap=root.querySelector('.ncap');
    var mCur=root.querySelector('.ncur');
    var mNote=root.querySelector('.nnote');
    var mDeep=root.querySelector('.ndeep');
    var vid=root.querySelector('.nvid');
    if(!scrub||!mon||(!frames.length&&!vid)){return;}
    mon.removeAttribute('hidden'); if(head)head.removeAttribute('hidden');
    var dur=data.dur||0, win=Math.max(1.0,dur*0.06), cur=0;
    function tc(s){s=Math.max(0,s);var m=Math.floor(s/60),r=s%60;return (m<10?'0':'')+m+':'+(r<10?'0':'')+r.toFixed(1);}
    function srcFor(fi){var im=imgs[fi];return im?im.getAttribute('src'):'';}
    function nearTelop(sec){var b=null,bd=1e9;for(var i=0;i<telops.length;i++){var d=Math.abs(telops[i].sec-sec);if(d<bd){bd=d;b=telops[i];}}return (b&&bd<=Math.max(1.2,dur*0.04))?b:null;}
    function deep(sec){var u=data.url||''; if(!u){if(mDeep)mDeep.style.display='none';return;} mDeep.style.display='';
      mDeep.href=u+(u.indexOf('?')>=0?'&':'?')+'t='+Math.floor(sec);
      mDeep.textContent='▶ 実動画を開く（該当 '+tc(sec)+'）';}
    function moveHead(sec){if(head)head.style.left=(dur?Math.max(0,Math.min(1,sec/dur))*100:0)+'%';}
    function showNote(sec,gap,f){var tp=nearTelop(sec),parts=[];
      if(gap)parts.push('最寄りフレーム '+tc(f.sec)+'（この付近は実フレームなし）');
      if(tp)parts.push('テロップ: 「'+tp.text+'」'+(tp.kw?' ✓KW':''));
      mNote.textContent=parts.join('　');}
    function aria(sec,ex){scrub.setAttribute('aria-valuenow',sec.toFixed(1));scrub.setAttribute('aria-valuetext',tc(sec)+(ex?(' '+ex):''));}
    function selectFrame(i){cur=i;var f=frames[i];mImg.src=srcFor(f.fi);mImg.alt=f.cap||'';
      mCap.textContent=f.cap||'';mCur.textContent=tc(f.sec);
      figs.forEach(function(fg,j){fg.setAttribute('aria-selected', j===f.fi?'true':'false');});
      deep(f.sec);moveHead(f.sec);showNote(f.sec,false,f);aria(f.sec,f.cap||'');}
    function hoverAt(sec){var i=nearest(frames,sec),f=frames[i];
      var interior=(sec>frames[0].sec&&sec<frames[frames.length-1].sec);
      var gap=interior&&Math.abs(f.sec-sec)>win;
      mImg.src=srcFor(f.fi);mImg.alt=f.cap||'';mCap.textContent=f.cap||'';mCur.textContent=tc(sec);
      deep(sec);moveHead(sec);showNote(sec,gap,f);aria(sec,'');}
    function secAt(x){var r=scrub.getBoundingClientRect();return Math.max(0,Math.min(1,(x-r.left)/r.width))*dur;}
    // ===== VIDEO MODE: 実プレビュー動画を再生ヘッドと双方向同期 =====
    if(vid){
      var playBtn=root.querySelector('.nplay');
      var vdur=dur||0;
      function pctv(s){return vdur?Math.max(0,Math.min(1,s/vdur))*100:0;}
      function secAtV(x){var r=scrub.getBoundingClientRect();return Math.max(0,Math.min(1,(x-r.left)/r.width))*(vdur||dur||1);}
      function nearFi(sec){if(!frames.length)return -2;var b=0,bd=1e9;for(var i=0;i<frames.length;i++){var d=Math.abs(frames[i].sec-sec);if(d<bd){bd=d;b=i;}}return frames[b].fi;}
      function reflect(sec){if(head)head.style.left=pctv(sec)+'%';if(mCur)mCur.textContent=tc(sec);
        if(mCap){var tp=nearTelop(sec);mCap.textContent=tp?('テロップ: 「'+tp.text+'」'+(tp.kw?' ✓KW':'')):'';}
        deep(sec);aria(sec,'');var fi=nearFi(sec);
        figs.forEach(function(fg){fg.setAttribute('aria-selected',parseInt(fg.getAttribute('data-fi'),10)===fi?'true':'false');});}
      function seek(sec){try{vid.currentTime=Math.max(0,Math.min((vdur||0.2)-0.03,sec));}catch(e){}reflect(sec);}
      vid.addEventListener('loadedmetadata',function(){if(vid.duration&&isFinite(vid.duration)){vdur=vid.duration;}reflect(0);});
      vid.addEventListener('timeupdate',function(){reflect(vid.currentTime);});
      vid.addEventListener('play',function(){if(playBtn)playBtn.textContent='⏸';});
      vid.addEventListener('pause',function(){if(playBtn)playBtn.textContent='▶';});
      if(playBtn)playBtn.addEventListener('click',function(){if(vid.paused){vid.play();}else{vid.pause();}});
      var vdrag=false,vraf=0;
      function vsc(x){seek(secAtV(x));}
      scrub.addEventListener('mousedown',function(e){vdrag=true;e.preventDefault();vid.pause();vsc(e.clientX);});
      document.addEventListener('mousemove',function(e){if(vdrag){if(vraf)return;var x=e.clientX;vraf=requestAnimationFrame(function(){vsc(x);vraf=0;});}});
      document.addEventListener('mouseup',function(){vdrag=false;});
      scrub.addEventListener('click',function(e){vsc(e.clientX);});
      scrub.addEventListener('keydown',function(e){
        if(e.key==='ArrowRight'){e.preventDefault();seek((vid.currentTime||0)+(e.shiftKey?5:1));}
        else if(e.key==='ArrowLeft'){e.preventDefault();seek((vid.currentTime||0)-(e.shiftKey?5:1));}
        else if(e.key===' '){e.preventDefault();if(vid.paused){vid.play();}else{vid.pause();}}
        else if(e.key==='Home'){e.preventDefault();seek(0);}
        else if(e.key==='End'){e.preventDefault();seek(vdur||0);}});
      figs.forEach(function(fg){var f=null,fi=parseInt(fg.getAttribute('data-fi'),10),i;
        for(i=0;i<frames.length;i++){if(frames[i].fi===fi){f=frames[i];break;}}
        fg.addEventListener('click',function(){if(f){vid.pause();seek(f.sec);}});
        fg.setAttribute('tabindex','0');fg.setAttribute('role','button');});
      reflect(0);
      return;
    }
    var raf=0,drag=false;
    function onMove(x){if(raf)return;raf=requestAnimationFrame(function(){hoverAt(secAt(x));raf=0;});}
    scrub.addEventListener('mousemove',function(e){if(!drag)onMove(e.clientX);});
    scrub.addEventListener('mouseleave',function(){if(!drag)selectFrame(cur);});
    scrub.addEventListener('mousedown',function(e){drag=true;e.preventDefault();onMove(e.clientX);});
    document.addEventListener('mousemove',function(e){if(drag)onMove(e.clientX);});
    document.addEventListener('mouseup',function(e){if(drag){drag=false;selectFrame(nearest(frames,secAt(e.clientX)));}});
    scrub.addEventListener('click',function(e){selectFrame(nearest(frames,secAt(e.clientX)));});
    scrub.addEventListener('keydown',function(e){
      if(e.key==='ArrowRight'){e.preventDefault();selectFrame(Math.min(frames.length-1,cur+1));}
      else if(e.key==='ArrowLeft'){e.preventDefault();selectFrame(Math.max(0,cur-1));}
      else if(e.key==='Home'){e.preventDefault();selectFrame(0);}
      else if(e.key==='End'){e.preventDefault();selectFrame(frames.length-1);}
      else if(e.key==='Enter'&&data.url){window.open(mDeep.href,'_blank','noopener');}
    });
    figs.forEach(function(fg){
      var fi=parseInt(fg.getAttribute('data-fi'),10);
      var idx=frames.findIndex(function(fr){return fr.fi===fi;});
      var go=function(){if(idx>=0){selectFrame(idx);}};
      fg.addEventListener('click',go);
      fg.setAttribute('tabindex','0');fg.setAttribute('role','tab');
      fg.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();go();}});});
    selectFrame(0);
  }
  function setupTopTabs(){
    var btns=document.querySelectorAll('.toptab');
    var panes=document.querySelectorAll('.ttpane');
    btns.forEach(function(b){b.addEventListener('click',function(){
      var k=b.getAttribute('data-tt');
      btns.forEach(function(x){x.classList.toggle('on',x===b);});
      panes.forEach(function(p){p.classList.toggle('show',p.getAttribute('data-ttp')===k);});
      window.scrollTo(0,0);
    });});
  }
  function init(){
    document.querySelectorAll('.nle[data-vtl]').forEach(setupTL);
    document.querySelectorAll('.tabs[data-tabs]').forEach(setupTabs);
    setupTopTabs();
  }
  if(document.readyState!=='loading'){init();}else{document.addEventListener('DOMContentLoaded',init);}
})();
"""


def render_report(out: VideoAlgorithmOutput, *, generated_at: str = "") -> str:
    """VideoAlgorithmOutput → 自己完結 HTML。

    トップタブで「📊 統計レポート（全体横断）」と「各動画の個別レポート」を切り替える。
    結論・共通点・指示・導線はスライドと同じ事実層（facts）と検査済みの synthesis v3 から描く。
    generated_at は検索結果を取得した日時（JST・ISO 8601）。冒頭とフッタに出す。
    """
    d = build_deck(out, generated_at=generated_at)
    analyzed = [(i, v) for i, v in enumerate(out.videos) if v.analysis]
    image_post_top_n = _image_post_top_n()
    image_post_metas = _image_post_metas(out) if image_post_top_n > 0 else []
    image_posts = [meta for meta in image_post_metas if meta.rank <= image_post_top_n]
    # トップタブ（統計＋分析成立した各動画）
    tabs = '<button class="toptab on" type="button" data-tt="ov">📊 統計レポート</button>'
    tabs += "".join(_video_tab_btn(v, i) for i, v in analyzed)
    tabs += "".join(_image_post_tab_btn(meta, i) for i, meta in enumerate(image_posts))
    # 統計（全体横断）pane
    scrape_board = (
        _scrape_board(out, image_post_ranks=frozenset(meta.rank for meta in image_post_metas))
        if image_post_metas
        else _scrape_board(out)
    )
    overview = (
        f"{_verdict_band(out, d)}{scrape_board}{_top5_board(out, d)}{_thumb_board(out)}"
        f"{_funnel_block(d)}{_synthesis_block(d)}{_matrix_block(out, d)}"
        f"{_stats_block(out.cross.stats)}"
    )
    # 個別レポート pane（動画ごと）
    panes = "".join(
        f'<div class="ttpane" data-ttp="v{i}">{_video_pane(v, i, d)}</div>' for i, v in analyzed
    )
    panes += "".join(
        f'<div class="ttpane" data-ttp="i{i}">{_image_post_pane(meta)}</div>'
        for i, meta in enumerate(image_posts)
    )
    note = (
        f"※ {footer_text(d)}。TikTok内部のランキング重みは非公開で、ここで測るのは表層特徴の"
        "共通性のみ。生存者バイアスがあるため、テスト投稿での検証を推奨します。"
    )
    stamp = f"　/　取得 {_esc(d.stamp)}" if d.stamp else ""
    scraped = len(out.board) or len(out.videos)
    n = _analyzed(out)
    scope = f"取得{scraped}本・深掘り分析{n}本"
    report_style = _STYLE + (_IMAGE_POST_STYLE if image_post_metas else "")
    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>VSEO動画アルゴリズム分析: {_esc(out.query)}</title>{dads_style(report_style)}</head><body>"
        "<h1>VSEO 動画アルゴリズム分析</h1>"
        f"<div class='meta'>検索KW「{_esc(out.query)}」 {scope}を読み解き{stamp}</div>"
        f'<div class="toptabs">{tabs}</div>'
        f'<div class="ttpane show" data-ttp="ov">{overview}</div>'
        f"{panes}"
        f"<footer class='dads-footnote'><div class='note'>{note}</div>"
        f"<p>{_esc(DADS_CREDIT)}</p></footer>"
        f"<script>{_TIMELINE_JS}</script></body></html>"
    )
