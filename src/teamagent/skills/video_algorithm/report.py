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
"""

from __future__ import annotations

import html
import json
import os
import re
from urllib.parse import urlsplit

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

    - 「27-27秒」のような min==max レンジは _win_ranges 側で既に除外済み。
    - フックは過半数（>n/2）の型のときだけ推奨。
    - サムネ色は thumb_agree（過半数一致）のときだけ「○○で作る」と言う。
    - thumb_consensus 文字列の機械分割は廃止し、構造値（dominant_*）から組む。
    """
    c = out.cross
    n = c.video_count or _analyzed(out)
    acts: list[str] = ["冒頭3秒のテロップに「" + out.query + "」を焼き込む"]
    st = c.stats
    if st:
        dur = next((r.text for r in st.win_ranges if r.label == "尺"), "")
        if dur:
            acts.append(f"尺は {dur} に収める")
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
        '<div class="th">訴求角度のクラスタ</div><table class="tbl">'
        "<thead><tr><th>角度</th><th>該当</th><th>効く理由（観測）</th></tr></thead>"
        f"<tbody>{angles}</tbody></table>"
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
        '<table class="tbl matrix2"><thead><tr><th>順</th><th>テロップ↔KW</th><th>キャプ↔KW</th>'
        "<th>音声↔KW</th><th>メッセージ一貫性</th><th>補強 / ズレ（一言）</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
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
    ticks = "".join(
        f'<span class="ntick" style="left:{_pct(i * step, dur):.2f}%">{_tc(i * step)}</span>'
        for i in range(n_ticks + 1)
        if i * step <= dur + 0.001
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
        '<span class="lg c-brand"></span>ブランド<span class="lg c-scene"></span>シーン'
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
        f'{comp}<table class="tbl"><thead><tr><th>秒</th><th>名称</th><th>場所</th><th>目立ち</th>'
        "<th>意図</th></tr></thead>"
        f"<tbody>{brand_rows or '<tr><td colspan=5 class=muted>検出なし</td></tr>'}</tbody></table>"
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
            '<table class="tbl"><thead><tr><th>特徴</th><th>ρ</th><th>効きの方向</th>'
            "<th>単調性</th></tr></thead>"
            f"<tbody>{corr_rows}</tbody></table>"
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
        f'<table class="tbl"><tbody>{dist_rows}</tbody></table>'
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


_STYLE = """
:root{--ink:#15191e;--sub:#5b6470;--line:#e6e8ec;--soft:#f6f7f9;--accent:#2563eb;
 --warn:#b45309;--good:#15803d;--maxw:1440px}
*{box-sizing:border-box}
body{font-family:-apple-system,'Hiragino Kaku Gothic ProN',Meiryo,system-ui,sans-serif;color:var(--ink);
 max-width:var(--maxw);margin:0 auto;padding:28px 32px 80px;line-height:1.7;background:#fbfbfc;font-size:13px}
h1{font-size:20px;font-weight:700;margin:0 0 2px}
.meta{color:var(--sub);font-size:12px;margin-bottom:8px}
section{margin:0 0 40px}
/* トップタブ（統計 ⇄ 個別レポート） */
.toptabs{display:flex;gap:4px;flex-wrap:wrap;border-bottom:2px solid var(--line);margin:8px 0 26px;position:sticky;top:0;background:#fbfbfc;z-index:20;padding-top:6px}
.toptab{background:none;border:none;border-bottom:3px solid transparent;padding:9px 14px;font-size:13px;font-weight:600;color:var(--sub);cursor:pointer;font-family:inherit;border-radius:7px 7px 0 0;display:inline-flex;align-items:center;gap:6px}
.toptab:hover{background:var(--soft)}
.toptab.on{color:var(--ink);border-bottom-color:var(--accent);background:var(--soft)}
.toptab .ttrank{font-weight:800;background:var(--sub);color:#fff;border-radius:5px;padding:0 6px;font-size:11px}
.toptab.on .ttrank{background:var(--accent)}
.ttpane{display:none}.ttpane.show{display:block;animation:ttfade .18s ease}
@keyframes ttfade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.vpane{margin:0 0 8px}
.vphead{display:flex;align-items:center;gap:11px;flex-wrap:wrap;padding-bottom:12px;margin-bottom:10px;border-bottom:1px solid var(--line)}
.vphead a{color:var(--accent);text-decoration:none;font-weight:700;font-size:15px}
.vpmsg{color:var(--sub);font-size:13px;flex:1;min-width:160px}
.vpwins{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 14px}
.wchip{background:var(--soft);border:1px solid var(--line);border-radius:6px;padding:3px 10px;font-size:11.5px;color:#2b333c}
.vpkpi{background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:13px 16px;margin:0 0 14px}
.engage.big{grid-template-columns:repeat(auto-fit,minmax(82px,1fr));gap:12px;margin-bottom:0}
.engage.big .st b{font-size:22px}.engage.big .st i{font-size:11px}
.th{font-size:11.5px;font-weight:700;color:var(--sub);letter-spacing:.04em;margin:0 0 10px}
.th.big{font-size:15px;color:var(--ink);border-bottom:1px solid var(--line);padding-bottom:7px}
.small{font-size:11.5px}.muted{color:var(--sub)}
.cdot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:middle}
.cdot.c-hi{background:var(--good)}.cdot.c-mid{background:#d19a3a}.cdot.c-lo{background:#9aa3ad}
.rk{display:inline-block;background:var(--soft);color:var(--sub);border-radius:5px;padding:0 6px;margin:0 3px 2px 0;font-size:11px;font-weight:700}
/* B 結論バンド */
.verdict{display:grid;grid-template-columns:1.55fr 1fr;gap:34px;border-bottom:2px solid var(--ink);padding-bottom:26px;margin-bottom:38px}
.vbig{font-size:26px;font-weight:700;line-height:1.36;margin:2px 0 14px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;gap:2px;background:var(--soft);border-radius:8px;padding:5px 11px;font-size:12px}
.chip b{font-weight:700}.chip i{font-style:normal;color:var(--sub);font-size:11px;margin-left:5px}
.nexts{margin:0;padding:0;list-style:none}
.nexts li{position:relative;padding:6px 0 6px 24px;font-size:13px;border-bottom:1px solid var(--line)}
.nexts li:before{content:'☐';position:absolute;left:2px;color:var(--accent);font-size:14px}
.nbanner{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:8px;padding:9px 14px;font-size:12.5px;margin:0 0 18px}
.vsub{font-size:13px;color:var(--sub);line-height:1.55;margin:-8px 0 12px}
.verdict.planner .vbig{font-size:24px}
.pitch{margin-top:13px;background:#eef5ff;border:1px solid #cfe0fb;border-radius:8px;padding:9px 13px;font-size:13px;color:#1e40af;line-height:1.55}
.tscroll{max-height:260px;overflow:auto;border:1px solid var(--line);border-radius:6px}
.drill-fail{border:1px solid var(--line);border-radius:8px;padding:11px 16px;color:var(--sub);font-size:13px;margin:0 0 12px;background:#fff}
/* B0 取得ボード（メタ一覧 board_size 本） */
.sbwrap{max-height:430px;overflow:auto;border:1px solid var(--line);border-radius:8px}
.sboard{width:100%;border-collapse:collapse;font-size:12px}
.sboard thead th{position:sticky;top:0;background:var(--soft);color:var(--sub);font-weight:600;padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap;z-index:1}
.sboard td{padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
.sboard tbody tr:hover{background:var(--soft)}
.sbr{font-weight:700;white-space:nowrap;color:var(--ink)}
.sbdeep{margin-left:3px;color:var(--accent)}
.sbtdth{padding:3px 8px!important}
.sbth{width:42px;height:56px;object-fit:cover;border-radius:4px;display:block;background:var(--soft)}
.sbth.ph{background:repeating-linear-gradient(45deg,var(--soft),var(--soft) 6px,#fff 6px,#fff 12px)}
.sbauth a{color:var(--ink);text-decoration:none;font-weight:600}
.sbauth a:hover{text-decoration:underline}
.sbnum{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.sbcap{color:var(--sub);max-width:300px}
/* C Top5ボード */
.board{display:grid;gap:0;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.blab,.bcol{display:flex;flex-direction:column}
.blab{background:var(--soft)}
.bcol{border-left:1px solid var(--line);text-align:center}
.bcol.is-top{box-shadow:inset 0 3px 0 var(--accent)}
.blab>div,.bcol>div{padding:6px 8px;border-bottom:1px solid var(--line);min-height:32px;display:flex;align-items:center;justify-content:center}
.blab .rl{justify-content:flex-end;color:var(--sub);font-size:11px;font-weight:600;text-align:right}
.brank{font-weight:800;font-size:13px}.bcol.is-top .brank{color:var(--accent)}
.bthumb,.bthumb-lab{padding:6px!important}
.bthumb img{width:78px;aspect-ratio:9/16;object-fit:cover;border-radius:6px;border:1px solid var(--line);display:block}
.bthumb.ph{width:78px;height:138px;background:repeating-linear-gradient(45deg,#f0f1f3 0 7px,#e6e8ec 7px 14px);border-radius:6px;margin:0 auto}
.bauth{font-size:11px;color:var(--accent);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bm{font-size:12px;gap:6px}.bv{font-weight:700}.btag{background:var(--soft);border-radius:5px;padding:1px 7px;font-size:11px;color:var(--sub)}
.miniba{position:relative;width:46px;height:6px;background:var(--soft);border-radius:4px;overflow:hidden}
.miniba>i{position:absolute;left:0;top:0;bottom:0;background:#c2c8d0;border-radius:4px}
.miniba.acc>i{background:var(--accent)}
.kwd{gap:4px}.kwd .d{width:9px;height:9px;border-radius:50%}.kwd .d.on{background:var(--accent)}.kwd .d.off{background:#dfe3e8}
/* D サムネ色 */
.tbconsensus{font-size:13px;margin-bottom:12px}
.tbrow{display:grid;gap:14px}
.tbcell{border:1px solid var(--line);border-radius:10px;padding:8px;display:flex;flex-direction:column;gap:6px;align-items:center}
.tbshot img{width:100%;aspect-ratio:9/16;object-fit:cover;border-radius:8px;display:block}
.tbshot.ph{width:100%;aspect-ratio:9/16;background:repeating-linear-gradient(45deg,#f0f1f3 0 8px,#e6e8ec 8px 16px);border-radius:8px}
.tbsw{display:flex;width:100%;height:16px;border-radius:5px;overflow:hidden;border:1px solid var(--line)}
.tbsw span{flex:1}
.tbm{display:flex;align-items:center;gap:6px;width:100%;font-size:11px}
.tbl{width:30px;color:var(--sub)}
.tbar{position:relative;flex:1;height:7px;border-radius:5px;background:linear-gradient(90deg,#1f2937,#9aa3ad,#fff)}
.tbar.wt{background:linear-gradient(90deg,#2563eb,#9aa3ad,#ea7317)}
.tbar>i{position:absolute;top:-3px;width:2px;height:13px;background:var(--ink);transform:translateX(-1px)}
.tbv{width:54px;text-align:right;color:var(--sub)}
/* E シンセシス */
.syn .th{margin-top:18px}.syn .th.big{margin-top:0}
.concepts{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.concept{border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.cphead{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px}
.cphead b{font-size:14px}.prev{font-size:11px;color:var(--sub);font-weight:700}
.vrefs{margin-top:6px}
.diffs{margin:4px 0 0;padding:0;list-style:none}.diffs li{font-size:12.5px;margin:4px 0}
.hyps{display:flex;flex-direction:column;gap:10px}
.hyp{border-left:3px solid var(--line);padding:2px 0 2px 12px}
.hyphead{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}.hyphead b{font-size:13.5px}
.counter{font-size:12px;color:var(--warn);margin-top:3px}
.sowhat{font-size:12.5px;color:var(--ink);margin-top:3px;font-weight:600}
/* F 一貫性マトリクス */
.matrix2 td,.matrix2 th{text-align:center}.matrix2 .rkc{font-weight:700}.matrix2 .notc{text-align:left;color:var(--sub);font-size:11.5px}
.mk.on{color:var(--accent);font-size:13px}.mk.off{color:#cfd5db}
.band{display:inline-block;border-radius:5px;padding:1px 8px;font-size:11px;font-weight:700}
.band.ok{background:#ecfdf5;color:#047857}.band.mid{background:#fffbeb;color:#92400e}.band.lo{background:#fef2f2;color:#b91c1c}.band.na{background:var(--soft);color:var(--sub)}
.mtop{margin-top:8px}
/* G ドリルダウン */
.drill{border:1px solid var(--line);border-radius:8px;margin:0 0 12px;background:#fff;box-shadow:0 1px 2px rgba(20,25,30,.04)}
.drill>summary{cursor:pointer;list-style:none;padding:13px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.drill>summary::-webkit-details-marker{display:none}
.drill>summary:before{content:'▸';color:var(--sub);font-size:13px}
.drill[open]>summary:before{content:'▾'}
.rank{font-weight:800;background:var(--ink);color:#fff;border-radius:7px;padding:2px 9px;font-size:13px}
.drill summary a{color:var(--accent);text-decoration:none;font-weight:600}
.sm-kpi{font-size:12px;color:var(--sub);background:var(--soft);border-radius:5px;padding:1px 8px}
.sm-msg{font-size:12px;color:var(--sub);flex:1;min-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.drillbody{padding:4px 16px 18px}
/* タイムライン（NLE構造・配色は本文の明色テーマに統一） */
.nle{background:#fff;border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:6px 0 4px;color:var(--ink);box-shadow:0 1px 2px rgba(20,25,30,.04)}
.nmon{display:grid;grid-template-columns:300px minmax(0,1fr);gap:16px;margin-bottom:12px}
.nscreen{position:relative;width:300px;height:200px;background:#0d0f12;border-radius:8px;overflow:hidden;display:flex;align-items:center;justify-content:center;border:1px solid var(--line)}
.nimg{max-width:100%;max-height:100%;object-fit:contain}
.nvid{max-width:100%;max-height:100%;background:#000}
.nplay{position:absolute;left:8px;top:8px;width:34px;height:34px;border-radius:50%;border:none;background:rgba(15,18,22,.62);color:#fff;font-size:13px;line-height:1;cursor:pointer;z-index:3}
.nplay:hover{background:var(--accent)}
.ntcbar{position:absolute;left:7px;bottom:6px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;background:rgba(13,15,18,.6);padding:1px 7px;border-radius:4px;color:#fff}
.ncur{color:#9fd4ff;font-weight:700}.ndur{color:#c4ccd4;margin-left:3px}
.nside{display:flex;flex-direction:column;gap:8px}
.nslabel{font-size:10px;letter-spacing:.12em;color:var(--sub);font-weight:700}
.ncap{font-size:14px;font-weight:600;line-height:1.5;color:var(--ink)}
.nnote{color:var(--sub)!important}
.ndeep{align-self:flex-start;display:inline-flex;padding:7px 13px;border-radius:7px;background:var(--accent);color:#fff;font-weight:700;text-decoration:none;font-size:12.5px}
.nrulerwrap,.nbody{display:grid;grid-template-columns:84px 1fr}
.ncorner{display:flex;align-items:center;padding-left:8px;font:600 10px ui-monospace,monospace;color:var(--sub);background:var(--soft);border-radius:4px 0 0 0}
.nruler{position:relative;height:22px;background:var(--soft);border-radius:0 4px 0 0;border-left:1px solid var(--line)}
.ntick{position:absolute;top:4px;transform:translateX(-50%);font:10px ui-monospace,monospace;color:var(--sub)}
.ntick:after{content:'';position:absolute;left:50%;top:13px;width:1px;height:5px;background:#cbd2d9}
.nftick{position:absolute;bottom:0;width:0;transform:translateX(-50%)}
.nftick:after{content:'◆';position:absolute;left:0;bottom:-1px;transform:translateX(-50%);font-size:8px;color:var(--accent)}
.ntheads{display:flex;flex-direction:column;gap:3px;padding-top:3px}
.nthead{height:30px;background:var(--soft);border:1px solid var(--line);border-right:none;border-radius:3px 0 0 3px;display:flex;align-items:center;padding:0 9px;font-size:10.5px;color:var(--sub);font-weight:700;letter-spacing:.02em}
.nlanes{position:relative;display:flex;flex-direction:column;gap:3px;padding-top:3px;border-left:1px solid var(--line)}
.nlane{position:relative;height:30px;background:#eef1f4;border-radius:0 3px 3px 0;overflow:hidden}
.nclip{position:absolute;top:2px;bottom:2px;border-radius:3px;display:flex;align-items:center;padding:0 6px;overflow:hidden;font-size:10px;color:#fff;pointer-events:none;border:1px solid rgba(0,0,0,.06)}
.nclab{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.c-hook{background:#15803d}.c-cta{background:#b45309}
.c-telop{background:#3a6ea5}.c-telop.kw{background:var(--accent);box-shadow:inset 0 0 0 1px rgba(255,255,255,.4)}
.c-brand{background:#7c3aed}.c-brand.comp{box-shadow:inset 0 0 0 2px #f0b429}
.c-scene{background:#64748b}
.nplayhead{position:absolute;top:0;bottom:0;width:2px;background:var(--accent);box-shadow:0 0 0 1px rgba(255,255,255,.7);pointer-events:none;z-index:6;transition:left .06s linear}
.nplayhead:before{content:'';position:absolute;top:-2px;left:-5px;border-left:6px solid transparent;border-right:6px solid transparent;border-top:8px solid var(--accent)}
.nscrub{position:absolute;inset:0;z-index:7;cursor:col-resize;background:transparent}
.nscrub:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.frmstrip{display:flex;gap:6px;overflow-x:auto;padding:11px 2px 2px}
.frm{flex:0 0 auto;margin:0;width:88px;cursor:pointer}
.frm img{display:block;width:88px;height:auto;max-height:150px;object-fit:cover;border-radius:5px;border:1px solid var(--line);background:var(--soft)}
.frm[aria-selected=true] img{outline:2px solid var(--accent);outline-offset:-1px}
.frm figcaption{font-size:9.5px;color:var(--sub);text-align:center;margin-top:2px;line-height:1.3}
.frm figcaption b{display:block;color:var(--ink);font-size:10px}
.nlegend{font-size:10.5px;color:var(--sub);margin-top:9px;display:flex;gap:11px;flex-wrap:wrap;align-items:center}
.nlegend .lg{display:inline-block;width:15px;height:9px;border-radius:2px;margin-right:3px;vertical-align:middle}
.nlegend .nft{color:var(--accent);margin-right:2px}.nlegend .muted{color:var(--sub)!important}
/* タブ */
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-top:16px}
.tab{background:none;border:none;border-bottom:2px solid transparent;padding:7px 12px;font-size:12.5px;color:var(--sub);cursor:pointer;font-family:inherit}
.tab.on{color:var(--ink);border-bottom-color:var(--accent);font-weight:700}
.tabpane{display:none;padding-top:12px}.tabpane.show{display:block}
.engage{display:grid;grid-template-columns:repeat(6,1fr);gap:7px;margin-bottom:8px}
.st{padding:5px 2px;text-align:center}.st b{display:block;font-size:15px;font-weight:700;line-height:1.2}
.st i{font-style:normal;font-size:10px;color:var(--sub)}.st.kpi b{color:#0d9488}
/* 行・タグ・テーブル */
.kvrow{font-size:12.5px;margin:7px 0;color:#2b333c}.kvrow b{color:var(--ink);margin-right:5px}
.warn{font-size:12.5px;margin:0 0 8px;padding:7px 11px;background:#fffbeb;border:1px solid #fde68a;border-radius:8px;color:#92400e}
.tbl{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:4px}
.tbl th,.tbl td{border-bottom:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}
.tbl th{color:var(--sub);font-weight:600;font-size:11px}
.tbl tr.hit td{background:#eff6ff}.tbl tr.comp td{background:#fffbeb}
.wins{margin:4px 0 0;padding-left:18px}.wins li{font-size:12.5px;margin:2px 0}
.err{color:#b91c1c;font-size:13px}
/* H 統計付録 */
.appendix{border:1px solid var(--line);border-radius:8px;padding:4px 18px;background:#fff}
.appendix>summary{cursor:pointer;padding:12px 0}
.caveats{margin:14px 0 12px;padding-left:18px;color:var(--warn);font-size:11.5px;line-height:1.7}
.stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-top:8px}
@media(max-width:1080px){.stats-grid{grid-template-columns:1fr}.verdict{grid-template-columns:1fr}.nmon{grid-template-columns:1fr}.nscreen{width:100%}}
.bar{position:relative;height:8px;background:var(--soft);border-radius:5px;width:64px;display:inline-block;vertical-align:middle}
.bar .fillpos{position:absolute;left:50%;height:100%;background:var(--accent);border-radius:0 5px 5px 0}
.bar .fillneg{position:absolute;right:50%;height:100%;background:#ea7317;border-radius:5px 0 0 5px}
.bar .mid{position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--line)}
.cov{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px}
.covlab{width:74px;color:var(--sub)}.covn{color:var(--sub);font-size:11px}
.hbar{position:relative;height:8px;width:120px;background:var(--soft);border-radius:5px;display:inline-block}
.hbar>i{position:absolute;left:0;top:0;bottom:0;background:var(--accent);border-radius:5px}
.note{font-size:11.5px;color:var(--sub);border-top:1px solid var(--line);margin-top:24px;padding-top:12px}
@media(prefers-reduced-motion:reduce){.nplayhead{transition:none}}
"""

_IMAGE_POST_STYLE = """
.toptab.imageposttab{color:var(--warn)}
.toptab.imageposttab.on{color:#92400e;border-bottom-color:var(--warn);background:#fffbeb}
.sbimage{margin-left:3px}
.imagepostpane{max-width:980px;margin:0 auto}
.iphead{border-bottom-color:#fde68a}
.iprank{background:var(--warn)}
.ipauthor{font-size:15px}.ipauthor b{font-size:11px;color:var(--sub);margin-right:6px}
.ipcover{display:flex;align-items:center;justify-content:center;min-height:360px;max-height:680px;
 background:#111827;border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-bottom:14px}
.ipcover img{display:block;max-width:100%;max-height:680px;object-fit:contain}
.ipcover.ph{background:var(--soft);color:var(--sub);font-size:12px}
.ipcaption{border:1px solid var(--line);border-radius:10px;padding:14px 16px;background:#fff;margin-top:14px}
.ipcaptionbody{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.8}
.ipnotice{margin-top:14px;padding:11px 14px;background:#fffbeb;border:1px solid #fde68a;
 border-radius:8px;color:#92400e;font-size:12.5px}
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
        f"<title>VSEO動画アルゴリズム分析: {_esc(out.query)}</title><style>{report_style}</style></head><body>"
        "<h1>VSEO 動画アルゴリズム分析</h1>"
        f"<div class='meta'>検索KW「{_esc(out.query)}」 {scope}を読み解き{stamp}</div>"
        f'<div class="toptabs">{tabs}</div>'
        f'<div class="ttpane show" data-ttp="ov">{overview}</div>'
        f"{panes}"
        f"<div class='note'>{note}</div>"
        f"<script>{_TIMELINE_JS}</script></body></html>"
    )
