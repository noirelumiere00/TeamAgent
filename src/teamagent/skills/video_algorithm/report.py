"""VSEO 動画アルゴリズム分析の HTML レポート生成（自己完結・横長SaaSダッシュボード）。

設計思想（docs/v3.2/ui_design_principles_anti_ai.md）= 引き算・余白・結論ファースト。
情報を詰め込まず、上から「結論 → 比較 → 概念 → 一貫性 → 個別ドリルダウン → 統計付録」の
段階的開示（progressive disclosure）にする。

レイアウト（上から）:
  B 結論バンド（勝者の型＋次の一手）
  C Top5比較ボード（サムネ＋主要指標の格子）
  D サムネ色比較ボード（検索一覧での目立ち方）
  E 横断シンセシス（概念の関連性・勝ちパターン仮説 / Gemini解釈層）
  F 一貫性マトリクス（テロップ↔キャプ↔映像中身・N本一望）
  G 各動画ドリルダウン（大型インタラクティブ・タイムライン＋タブ・既定折りたたみ）
  H 統計付録（Spearman/分布/カバレッジ・既定クローズ）

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
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    CrossSynthesis,
    FrameShot,
    StatsAnalysis,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
)

_POS_JP = {"top": "上", "center": "中", "bottom": "下", "full": "全", "unknown": "?"}
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_TONE_JP = {"warm": "暖色", "neutral": "中性", "cool": "寒色", "mixed": "混在"}
_BRIGHT_JP = {
    "dark": "低明度",
    "dim": "やや暗",
    "medium": "中明度",
    "bright": "高明度",
    "very_bright": "高明度",
}
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


def _verdict_big(out: VideoAlgorithmOutput) -> str:
    """結論の主文（synthesis 仮説 > 勝ち筋 > summary）。verdict と synthesis で共有し重複を避ける。"""
    c = out.cross
    if c.synthesis and c.synthesis.win_hypotheses:
        return c.synthesis.win_hypotheses[0].hypothesis
    if c.win_factors:
        return f"勝者の型は『{c.win_factors[0].factor}』"
    return c.summary or f"「{out.query}」上位動画の共通パターン"


def _shorten(s: str, n: int = 40) -> str:
    """結論の大見出し用に第1文・n字で詰める（3秒で読める長さに）。"""
    head = (s or "").split("。")[0].strip()
    return head if len(head) <= n else head[: n - 1] + "…"


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


def _terms(query: str) -> list[str]:
    return [t for t in re.split(r"[\s　,、]+", query.strip()) if t]


def _json_attr(obj: object) -> str:
    """JSON を <script type=application/json> に安全に埋める（</script> 早期終端対策）。"""
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")


def _conf_dot(conf: str) -> str:
    cls = {"高": "c-hi", "中": "c-mid", "低": "c-lo"}.get(conf, "c-mid")
    return f'<span class="cdot {cls}" title="確信度 {_esc(conf)}"></span>'


def _kw_layer_flags(v: AnalyzedVideo, terms: list[str]) -> list[tuple[str, bool]]:
    a = v.analysis
    if a is None:
        return [("テロップ", False), ("音声", False), ("キャプ", False), ("HT", False)]
    telop = a.kw_in_telop()
    spoken = any(m.matched for m in a.spoken_keywords)
    caption = any(t and t in v.meta.desc for t in terms) or any(
        m.matched and m.layer == "caption" for m in a.keyword_matches
    )
    hashtag = any(m.matched and m.layer == "hashtag" for m in a.keyword_matches)
    return [("テロップ", telop), ("音声", spoken), ("キャプ", caption), ("HT", hashtag)]


# ===========================================================
# B プランナー戦略サマリ（ショート動画PRプランナー/ディレクター視点）
# ===========================================================
def _verdict_band(out: VideoAlgorithmOutput) -> str:
    c = out.cross
    syn = c.synthesis
    n = _analyzed(out)
    # 主文: プランナーの headline 優先、無ければ従来の勝ち筋から
    if syn and syn.headline:
        big = syn.headline
        sub = syn.strategy
    else:
        full = _verdict_big(out)
        big = _shorten(full)
        sub = full if full != big and len(full) > len(big) else ""
    chips = (
        "".join(
            f'<span class="chip">{_conf_dot(w.confidence)}<b>{_esc(w.factor)}</b>'
            f"<i>{w.observed_in}/{w.total}本</i></span>"
            for w in c.win_factors[:4]
        )
        or '<span class="muted small">顕著な共通項なし</span>'
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
    # クリエイティブ指示 = プランナーの creative_brief 優先、無ければ次の一手テンプレ
    brief = syn.creative_brief if (syn and syn.creative_brief) else _next_actions(out)
    items = "".join(f"<li>{_esc(x)}</li>" for x in brief[:6])
    pitch_html = (
        f'<div class="pitch">💬 <b>クライアント提案</b>　{_esc(syn.client_pitch)}</div>'
        if syn and syn.client_pitch
        else ""
    )
    posting_html = (
        f'<div class="kvrow"><b>投稿設計</b>{_esc(syn.posting_design)}</div>'
        if syn and syn.posting_design
        else ""
    )
    right_head = (
        "クリエイティブ指示" if (syn and syn.creative_brief) else "次の一手（テスト投稿の仮説）"
    )
    return (
        f"{gate}"
        '<section class="verdict planner">'
        '<div class="vleft"><div class="th">🎬 プランナーの戦略サマリ（この検索面の攻略方針）</div>'
        f'<div class="vbig">{_esc(big)}</div>{sub_html}'
        f'<div class="chips">{chips}</div>{pitch_html}</div>'
        f'<div class="vright"><div class="th">{right_head}</div>'
        f'<ul class="nexts">{items}</ul>{posting_html}</div>'
        "</section>"
    )


def _next_actions(out: VideoAlgorithmOutput) -> list[str]:
    """提案アクション。単一サンプル/過半数未満の助言は誠実さのため出さない。

    - 尺は上位帯（上位 2 本）の幅を指示にしない（旧 _win_ranges は廃止）。全 n 本の分布を
      事実として添えるだけにする（最良の動画を範囲外に追い出さないため）。
    - フックは過半数（>n/2）の型のときだけ推奨。
    - サムネ色は thumb_agree（過半数一致）のときだけ「○○で作る」と言う。
    - thumb_consensus 文字列の機械分割は廃止し、構造値（dominant_*）から組む。
    """
    c = out.cross
    n = c.video_count or _analyzed(out)
    acts: list[str] = ["冒頭3秒のテロップに「" + out.query + "」を焼き込む"]
    st = c.stats
    if st:
        dur = next((d for d in st.distributions if d.feature == "尺(秒)"), None)
        if dur is not None and st.sample_size >= 2 and dur.min != dur.max:
            acts.append(
                f"尺は固定しない（上位{st.sample_size}本は{dur.min:.0f}〜{dur.max:.0f}秒・"
                f"中央値{dur.median:.0f}秒）"
            )
        if st.hook_counts:
            top_hook, hc = st.hook_counts[0]
            if hc * 2 > n:  # 過半数の型だけ推奨（n=1の型は出さない）
                acts.append(f"フックは『{_HOOK_JP.get(top_hook, top_hook)}』型を軸に（{hc}/{n}本）")
    if c.thumb_agree:  # サムネ色が過半数一致のときだけ色を指示
        tone = _TONE_JP.get(c.dominant_temperature, "")
        bright = _BRIGHT_JP.get(c.dominant_brightness, "")
        if tone or bright:
            acts.append(f"サムネは{tone}×{bright}で作る")
    elif c.thumb_consensus:  # 割れている場合は差別化余地として正直に
        acts.append("サムネ色は上位でも割れており差別化の余地")
    acts.append("保存導線（保存/来店CTA）を1つ入れる")
    # synthesis の so_what は補助的に末尾へ（重複・長文は弾く）
    syn = c.synthesis
    if syn and syn.win_hypotheses and (sw := syn.win_hypotheses[0].so_what) and len(sw) <= 40:
        acts.append(sw)
    out_list: list[str] = []
    for a in acts:
        if a and a not in out_list:
            out_list.append(a)
        if len(out_list) >= 5:
            break
    return out_list


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
        return (
            "<tr>"
            f'<td class="sbr">#{m.rank}{deep}{image_post}</td>'
            f'<td class="sbtdth">{thumb}</td>'
            f'<td class="sbauth"><a href="{_esc(m.url)}" target="_blank" rel="noopener">@{auth}</a></td>'
            f'<td class="sbnum">{_fmt(m.follower_count)}</td>'
            f'<td class="sbnum">{_fmt(m.play_count)}</td>'
            f'<td class="sbnum">{m.save_rate():.1f}%</td>'
            f'<td class="sbnum">{_fmt(m.digg_count)}</td>'
            f'<td class="sbcap">{_esc(_shorten(m.desc, 46))}</td>'
            "</tr>"
        )

    body = "".join(row(m) for m in metas)
    image_legend = "・📷＝画像投稿（動画深掘り対象外）" if image_post_ranks else ""
    return (
        '<section><div class="th big">検索上位 取得ボード'
        f"（「{_esc(out.query)}」上位{n}本のメタ一覧・★＝深掘り分析対象{image_legend}）</div>"
        '<div class="sbwrap"><table class="sboard">'
        "<thead><tr><th>#</th><th>サムネ</th><th>アカウント</th><th>フォロワー</th>"
        "<th>再生</th><th>保存率</th><th>いいね</th><th>キャプション</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
        '<div class="muted small">※ サムネはTikTok署名URL（時間経過で失効する場合あり）。'
        "営業はこの一覧から提案に載せる動画を選定。</div></section>"
    )


def _top5_board(out: VideoAlgorithmOutput) -> str:
    vids = [v for v in out.videos if v.analysis]
    if not vids:
        return ""
    terms = _terms(out.query)
    n = len(vids)
    max_save = max((v.meta.save_rate() for v in vids), default=0.0)
    max_play = max((v.meta.play_count for v in vids), default=0)

    def col(v: AnalyzedVideo) -> str:
        m, a = v.meta, v.analysis
        assert a is not None
        top1 = " is-top" if m.rank == 1 else ""
        thumb = (
            f'<a href="{_esc(m.url)}" target="_blank" rel="noopener" class="bthumb">'
            f'<img src="{_esc(v.cover_data_uri)}" alt="#{m.rank}"></a>'
            if v.cover_data_uri
            else '<div class="bthumb ph"></div>'
        )
        kwd = "".join(
            f'<span class="d {"on" if ok else "off"}" title="{_esc(name)}"></span>'
            for name, ok in _kw_layer_flags(v, terms)
        )
        save = m.save_rate()
        return (
            f'<div class="bcol{top1}">'
            f'<div class="brank">#{m.rank}</div>{thumb}'
            f'<div class="bauth">@{_esc(m.author) or "—"}</div>'
            f'<div class="bm"><span class="bv">{save:.2f}%</span>{_mini_bar(save, max_save, accent=(save >= max_save))}</div>'
            f'<div class="bm"><span class="bv">{_fmt(m.play_count)}</span>{_mini_bar(float(m.play_count), float(max_play), accent=False)}</div>'
            f'<div class="bm"><span class="bv">{a.duration_sec:.0f}s</span></div>'
            f'<div class="bm"><span class="btag">{_esc(a.hook_type)}</span></div>'
            f'<div class="bm kwd">{kwd}</div>'
            f'<div class="bm"><span class="bv">{"✓" if a.has_cta() else "—"} / {"✓" if a.has_brand() else "—"}</span></div>'
            "</div>"
        )

    labels = (
        '<div class="blab"><div class="brank">&nbsp;</div><div class="bthumb-lab">サムネ</div>'
        '<div class="bauth">&nbsp;</div>'
        '<div class="bm rl">保存率 ★</div><div class="bm rl">再生</div><div class="bm rl">尺</div>'
        '<div class="bm rl">フック型</div><div class="bm rl">KW層(4)</div><div class="bm rl">CTA/商品</div></div>'
    )
    cols = "".join(col(v) for v in vids)
    return (
        f'<section><div class="th big">Top{n} 比較ボード（同じ検索面の当たり/外れの差）</div>'
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
        return (
            f'<div class="tbcell"><div class="brank">#{v.meta.rank}</div>{shot}'
            f'<div class="tbsw">{sw}</div>'
            f'<div class="tbm"><span class="tbl">明度</span><span class="tbar"><i style="left:{bri:.0f}%"></i></span><span class="tbv">{t.brightness01:.2f}</span></div>'
            f'<div class="tbm"><span class="tbl">暖寒</span><span class="tbar wt"><i style="left:{warm_left:.0f}%"></i></span><span class="tbv">{t.tone_jp()}</span></div>'
            "</div>"
        )

    cells = "".join(cell(v) for v in vids)
    return (
        '<section><div class="th big">サムネ色の比較（検索一覧での目立ち方＝クリック前の勝負）</div>'
        f'<div class="tbconsensus"><b>{_esc(consensus)}</b>'
        '<span class="muted small">　検索結果の縮小タイルでどれに指が止まるか</span></div>'
        f'<div class="tbrow" style="grid-template-columns:repeat({len(vids)},1fr)">{cells}</div></section>'
    )


# ===========================================================
# E 横断シンセシス（概念の関連性・Gemini解釈層）
# ===========================================================
def _synthesis_block(out: VideoAlgorithmOutput) -> str:
    s: CrossSynthesis | None = out.cross.synthesis
    if s is None:
        return ""
    concepts = "".join(
        f'<div class="concept"><div class="cphead"><b>{_esc(cc.concept)}</b>'
        f'<span class="prev">{_esc(cc.prevalence)}</span></div>'
        f'<div class="small">{_esc(cc.gist)}</div>'
        f'<div class="vrefs">{_vrefs(cc.videos)}</div></div>'
        for cc in s.common_concepts
    )
    concepts_block = (
        f'<div class="th">共通する概念（{len(s.common_concepts)}本以上を貫くもの）</div>'
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
        "<thead><tr><th>角度</th><th>該当</th><th>効く理由（観測）</th></tr></thead>"
        f"<tbody>{angles}</tbody></table></div>"
        if angles
        else ""
    )
    funnel = ""
    if s.shared_funnel and s.shared_funnel.pattern:
        f = s.shared_funnel
        cta = "・".join(_esc(x) for x in f.cta_consensus) or "—"
        funnel = (
            '<div class="th">共通の導線（保存→来店設計）</div>'
            f'<div class="kvrow">{_esc(f.pattern)}'
            f'<span class="muted small">　CTA多数派: {cta}　/　{_esc(f.save_logic)}</span></div>'
        )
    diffs = "".join(
        f'<li><span class="rk">#{d.rank}</span>{_esc(d.edge)}</li>' for d in s.differentiators
    )
    diff_block = (
        f'<div class="th">差別化点（同質化の中で何で抜けたか）</div><ul class="diffs">{diffs}</ul>'
        if diffs
        else ""
    )
    # 上部が headline を出す時(プランナー版)は重複しないので全表示。
    # fallback時のみ verdict と同一の仮説を除く（言い換えの二重掲載を防ぐ）
    vbig = _verdict_big(out)
    hlist = (
        s.win_hypotheses if s.headline else [h for h in s.win_hypotheses if h.hypothesis != vbig]
    )
    hyps = "".join(
        f'<div class="hyp"><div class="hyphead">{_conf_dot(h.confidence)}'
        f'<b>{_esc(h.hypothesis)}</b><span class="prev">{_vrefs(h.supported_by)}</span></div>'
        + (
            f'<div class="counter">反例: {_esc(h.counter_example)}</div>'
            if h.counter_example
            else ""
        )
        + (f'<div class="sowhat">→ {_esc(h.so_what)}</div>' if h.so_what else "")
        + "</div>"
        for h in hlist
    )
    hyp_block = (
        '<div class="th">勝ちパターン仮説（提案書の核・確信度つき）</div>'
        f'<div class="hyps">{hyps}</div>'
        if hyps
        else ""
    )
    # 仮説を主役に先頭へ。概念/角度/導線/差別化は根拠として後段に。免責はフッタに一元化
    return (
        '<section class="syn"><div class="th big">横断シンセシス — 概念の関連性と勝ちパターン（AI解釈層）</div>'
        f"{hyp_block}{concepts_block}{angle_block}{funnel}{diff_block}</section>"
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


def _matrix_block(out: VideoAlgorithmOutput) -> str:
    vids = [v for v in out.videos if v.analysis]
    if not vids:
        return ""
    terms = _terms(out.query)

    def cellmark(ok: bool) -> str:
        return '<span class="mk on">●</span>' if ok else '<span class="mk off">○</span>'

    rows = ""
    sums = {"テロップ": 0, "キャプ": 0, "音声": 0}
    for v in vids:
        a = v.analysis
        assert a is not None
        flags = dict(_kw_layer_flags(v, terms))
        sums["テロップ"] += int(flags["テロップ"])
        sums["キャプ"] += int(flags["キャプ"])
        sums["音声"] += int(flags["音声"])
        band = a.coherence_band()
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
    n = len(vids)
    consensus = (
        f"テロップにKW {sums['テロップ']}/{n}本・キャプにKW {sums['キャプ']}/{n}本・"
        f"音声にKW {sums['音声']}/{n}本"
    )
    bands = [v.analysis.coherence_band() for v in vids if v.analysis]
    # 上位が一貫性で横並びなら「順位を分けた要因ではない＝共通前提」と正直に読ませる
    read = (
        "上位は一貫性で横並び＝これは入賞の<b>共通前提</b>であり、順位を分けたのは別要因（差別化点を参照）。"
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


def _timeline_hero(v: AnalyzedVideo, idx: int) -> str:
    a = v.analysis
    if a is None or a.duration_sec <= 0:
        return '<div class="muted small">タイムライン: 尺不明のため省略</div>'
    dur = a.duration_sec
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
        telop += _clip(left, _pct(nxt, dur) - left, _esc(t.text[:18]), "c-telop", kw=t.kw_match)

    # V3 ブランド/物体
    brand = ""
    for b in a.brand_detections:
        for s in b.appear_sec or [0.0]:
            if s > lim:
                continue
            cls = "c-brand comp" if b.brand_relation == "competitor" else "c-brand"
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
        "frames": [{"sec": round(f.sec, 2), "cap": f.caption, "fi": i} for i, f in enumerate(fr)],
        "telops": [
            {
                "sec": round(t.sec, 2),
                "pos": _POS_JP.get(t.position, "?"),
                "text": t.text,
                "kw": t.kw_match,
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
        f"{_frame_strip(fr)}{legend}</div>"
    )


def _frame_strip(fr: list[FrameShot]) -> str:
    if not fr:
        return ""
    cells = "".join(
        f'<figure class="frm" data-fi="{i}"><img src="{f.data_uri}" alt="{_esc(f.caption)}">'
        f"<figcaption><b>{f.sec:.1f}s</b>{_esc(f.caption)}</figcaption></figure>"
        for i, f in enumerate(fr)
    )
    return f'<div class="frmstrip" role="tablist" aria-label="抽出フレーム">{cells}</div>'


def _tabs(v: AnalyzedVideo, idx: int) -> str:
    a = v.analysis
    assert a is not None
    # KW一致テロップを先頭に（営業が見たいのは"KWが乗った瞬間"）
    telop_rows = "".join(
        f'<tr class="{"hit" if t.kw_match else ""}"><td>{t.sec:.1f}s</td>'
        f"<td>{'✓' if t.kw_match else ''}</td><td>{_esc(t.text)}</td></tr>"
        for t in sorted(a.telops, key=lambda x: (not x.kw_match, x.sec))
    )
    telop_tbl = (
        '<div class="tscroll"><table class="tbl"><thead><tr><th>秒</th><th>KW</th><th>内容</th>'
        "</tr></thead>"
        f"<tbody>{telop_rows or '<tr><td colspan=3 class=muted>検出なし</td></tr>'}</tbody></table></div>"
    )
    comp = _competitor_html(a)
    brand_rows = "".join(
        f'<tr class="{"comp" if b.brand_relation == "competitor" else ""}">'
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
        f'<div class="kvrow"><b>主訴求</b>{_esc(a.main_message) or "—"}　<b>テンポ</b>{_esc(a.pacing)}</div>'
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


def _competitor_html(a: VideoVSEOAnalysis) -> str:
    comp = [b for b in a.brand_detections if b.brand_relation == "competitor"]
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


def _video_pane(v: AnalyzedVideo, idx: int) -> str:
    """個別レポート1本分（上部に数値KPI → 大型タイムライン動画プレーヤー → 詳細タブ）。"""
    m = v.meta
    a = v.analysis
    assert a is not None
    head = (
        f'<div class="vphead"><span class="rank">#{m.rank}</span>'
        f'<a href="{_esc(m.url)}" target="_blank" rel="noopener">@{_esc(m.author) or "—"}</a>'
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
            _stat(f"{m.engagement_rate:.1f}%", "エンゲージ", kpi=True)
            if m.engagement_rate > 0
            else ""
        )
        + _stat(f"{a.duration_sec:.0f}s", "尺")
        + "</div></div>"
    )
    wins = "".join(f'<span class="wchip">{_esc(w)}</span>' for w in a.win_factors[:4])
    wins_html = f'<div class="vpwins">{wins}</div>' if wins else ""
    return f'<div class="vpane">{head}{kpi}{wins_html}{_timeline_hero(v, idx)}{_tabs(v, idx)}</div>'


# ===========================================================
# H 統計付録（既定クローズ）
# ===========================================================
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


def _stats_block(s: StatsAnalysis | None) -> str:
    if s is None or s.sample_size == 0:
        return ""
    # n<3 で相関が全て算出不能なら「空の相関表」を見せない（恥/不信を避ける）
    has_rho = any(c.rho is not None for c in s.correlations)
    if has_rho:
        corr_rows = "".join(
            f"<tr><td>{_esc(c.feature)}</td><td>{'' if c.rho is None else f'{c.rho:+.2f}'}</td>"
            f"<td>{_corr_bar(c.rho)} {_esc(c.direction_label)}</td>"
            f"<td>{c.monotonic_hits}/{c.monotonic_total}</td></tr>"
            for c in s.correlations
        )
        corr_tbl = (
            '<div class="th">特徴量 × 順位の効き（Spearman ρ・点推定／有意性なし）</div>'
            '<div class="tblwrap"><table class="tbl"><thead><tr><th>特徴</th><th>ρ</th><th>効きの方向</th>'
            "<th>単調性</th></tr></thead>"
            f"<tbody>{corr_rows}</tbody></table></div>"
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
    hooks = "　".join(f"{_esc(h)} {c}本" for h, c in s.hook_counts)
    hook_block = (
        f'<div class="th">フック型の分布（強フック {_esc(s.strong_hook_ratio)}）</div>'
        f'<div class="kvrow">{hooks or "—"}</div>'
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
    """
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
        f"{_verdict_band(out)}{scrape_board}{_top5_board(out)}{_thumb_board(out)}"
        f"{_synthesis_block(out)}{_matrix_block(out)}{_stats_block(out.cross.stats)}"
    )
    # 個別レポート pane（動画ごと）
    panes = "".join(
        f'<div class="ttpane" data-ttp="v{i}">{_video_pane(v, i)}</div>' for i, v in analyzed
    )
    panes += "".join(
        f'<div class="ttpane" data-ttp="i{i}">{_image_post_pane(meta)}</div>'
        for i, meta in enumerate(image_posts)
    )
    note = (
        "※ 本レポートは上位動画の観測可能な特徴に基づく仮説です。TikTok内部のランキング重みは"
        "非公開で、ここで測るのは表層特徴の共通性のみ。n が小さく相関≠因果・生存者バイアスがあるため、"
        "入賞率はテスト投稿での検証を推奨します。"
    )
    stamp = f"　/　{_esc(generated_at)}" if generated_at else ""
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
