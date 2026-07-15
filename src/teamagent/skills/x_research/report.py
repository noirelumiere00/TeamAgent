"""X リサーチ成果物の HTML レンダラ（①カード集 / ②ニーズ分類 / ④日別グラフ+TOP投稿）。

ライトテーマ固定（x-reaction-research SKILL.md の実障害: OS ダーク設定で白文字が消える
納品事故があったため、prefers-color-scheme 分岐は作らない）。JSなし・インラインSVGのみ。
Slack へは通知だけを返し、詳細は本HTMLに全て入れる（video_algorithm と同方針）。
"""

from __future__ import annotations

import datetime as _dt
import html as _html
from typing import Any

from teamagent.skills._html.theme import FONT_STACK_JP
from teamagent.skills._shared.text_safety import safe_href, sanitize_llm_text
from teamagent.skills.x_research.schema import NeedCluster, XPostCard

_CSS = f"""
body{{font-family:{FONT_STACK_JP};background:#f6f7f9;color:#1b1f24;margin:0;padding:24px}}
.wrap{{max-width:860px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 4px}}
.meta{{color:#5b6570;font-size:12px;margin-bottom:16px}}
.card{{background:#fff;border:1px solid #e3e7ec;border-radius:10px;padding:14px 16px;margin:10px 0}}
.card .head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}}
.handle{{font-weight:700;font-size:13px}}
.note{{color:#5b6570;font-size:11px;margin-left:6px}}
.likes{{color:#e0245e;font-size:13px;white-space:nowrap}}
.text{{font-size:14px;line-height:1.7;white-space:pre-wrap;word-break:break-word}}
.foot{{margin-top:8px;font-size:11px;display:flex;justify-content:space-between;align-items:center}}
.foot a{{color:#1d6fdc;text-decoration:none;word-break:break-all}}
.badge{{border-radius:4px;padding:1px 6px;font-size:10px;font-weight:700}}
.ok{{background:#e6f4ea;color:#137333}}
.warn{{background:#fdeeee;color:#b3261e}}
.cluster{{background:#eef4fb;border:1px solid #d4e2f4;border-radius:10px;
  padding:12px 16px;margin:18px 0 6px}}
.cluster h2{{font-size:15px;margin:0 0 4px}}
.cluster p{{margin:0;font-size:13px;color:#31405a}}
.summary{{background:#e9f5ef;border:1px solid #cbe7d8;border-radius:10px;padding:12px 16px;
  margin:14px 0;font-size:13px;line-height:1.7}}
.notebox{{background:#fff8e6;border:1px solid #f0e2b6;border-radius:10px;padding:10px 14px;
  margin:12px 0;font-size:12px;color:#6b5b1e}}
.footer{{color:#8a939c;font-size:11px;margin-top:22px;border-top:1px solid #e3e7ec;padding-top:8px}}
/* X投稿再現カード（P1・提案書にスクショ貼付できる純正見た目）。純正配色をカード内で閉じる。 */
.xc{{background:#fff;border:1px solid #cfd9de;border-radius:16px;padding:12px 16px;
  margin:12px 0;max-width:600px}}
.xc .top{{display:flex;align-items:flex-start;gap:10px}}
.xc .av{{width:44px;height:44px;border-radius:50%;flex:none;object-fit:cover;background:#e1e8ed}}
.xc .mono{{width:44px;height:44px;border-radius:50%;flex:none;display:flex;align-items:center;
  justify-content:center;color:#fff;font-weight:700;font-size:18px}}
.xc .id{{min-width:0;flex:1}}
.xc .nm{{font-weight:700;font-size:15px;color:#0f1419;display:flex;align-items:center;
  gap:3px;flex-wrap:wrap}}
.xc .bv{{color:#1d9bf0;font-size:13px}}
.xc .hd{{color:#536471;font-size:14px}}
.xc .bd{{font-size:15px;line-height:1.5;color:#0f1419;white-space:pre-wrap;
  word-break:break-word;margin:8px 0}}
.xc .md{{display:grid;gap:2px;border-radius:14px;overflow:hidden;margin:8px 0;
  border:1px solid #cfd9de}}
.xc .md.n1{{grid-template-columns:1fr}}
.xc .md.n2,.xc .md.n3,.xc .md.n4{{grid-template-columns:1fr 1fr}}
.xc .md img{{width:100%;height:100%;object-fit:cover;display:block;max-height:280px}}
.xc .eng{{display:flex;gap:22px;color:#536471;font-size:13px;margin-top:6px}}
.xstrip{{display:flex;align-items:center;gap:8px;font-size:11px;color:#536471;
  margin:-4px 0 12px 4px;flex-wrap:wrap}}
.xstrip a{{color:#1d6fdc;text-decoration:none}}
.kaiwai-label{{color:#8a939c;font-size:10px}}
.kaiwai{{background:#eef4fb;color:#1d6fdc;border:1px solid #d4e2f4;border-radius:10px;
  padding:0 6px;font-size:10px;font-weight:700;max-width:140px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}}
"""

_MONO_COLORS = (
    "#1d9bf0",
    "#e0245e",
    "#17bf63",
    "#f45d22",
    "#794bc4",
    "#f7b924",
    "#ff6b6b",
    "#00b894",
)


def _esc(s: str) -> str:
    return _html.escape(s, quote=True)


def _today() -> str:
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).strftime("%Y-%m-%d")


_JST = _dt.timezone(_dt.timedelta(hours=9))


def _fmt_date(raw: str) -> str:
    """投稿日時（epoch ms/秒・ISO・Twitter形式）を JST『M月D日』へ。判定不能は空。"""
    s = (raw or "").strip()
    if not s:
        return ""
    if s.isdigit():
        try:
            ts = int(s)
            if ts > 10_000_000_000:  # ミリ秒
                ts //= 1000
            return _dt.datetime.fromtimestamp(ts, _JST).strftime("%-m月%-d日")
        except (ValueError, OverflowError, OSError):
            return ""
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        d = d.replace(tzinfo=_dt.UTC) if d.tzinfo is None else d
        return d.astimezone(_JST).strftime("%-m月%-d日")
    except ValueError:
        pass
    try:  # Twitter "Wed Jul 01 12:00:00 +0000 2026"
        return (
            _dt.datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
            .astimezone(_JST)
            .strftime("%-m月%-d日")
        )
    except ValueError:
        return ""


def _monogram(name: str, handle: str) -> str:
    """アイコン未取得時のフォールバック丸（頭文字＋@ハッシュ由来の決定論色）。"""
    base = (name or handle or "?").strip()
    ch = base[0].upper() if base else "?"
    key = handle or name or "?"
    color = _MONO_COLORS[sum(ord(c) for c in key) % len(_MONO_COLORS)]
    return f"<div class='mono' style='background:{color}'>{_esc(ch)}</div>"


def _page(title: str, sub: str, body: str, footer_note: str) -> str:
    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head><body><div class='wrap'>"
        f"<h1>{_esc(title)}</h1><div class='meta'>{_esc(sub)}</div>{body}"
        f"<div class='footer'>{_esc(footer_note)}</div></div></body></html>"
    )


def _card(p: XPostCard) -> str:
    """1投稿を『X投稿画面そっくり』の再現カード＋枠外の検証ストリップで描く。

    フレーム内は純正の見た目だけ（アイコン/名前/青✔/@/日時/本文/画像/💬🔁❤️👁）。
    検証チップ・属性メモ・元投稿リンクは枠外(.xstrip)へ逃がしスクショ感を保つ。
    """
    # アイコン: 取得できていれば実画像、無ければモノグラム丸（@不明は出さない）。
    avatar = (
        f"<img class='av' src='{_esc(p.avatar_data)}' alt=''>"
        if p.avatar_data
        else _monogram(p.author_name, p.author_handle)
    )
    bv = "<span class='bv'>✔</span>" if p.is_verified else ""
    handle_txt = f"@{_esc(p.author_handle)}" if p.author_handle else ""
    date = _fmt_date(p.created_at)
    # 表示名優先。名前が無ければ @handle を主に。どちらも無ければ「投稿者不明」。
    if p.author_name:
        nm = _esc(p.author_name)
        sub = " · ".join(x for x in (handle_txt, date) if x)
    elif p.author_handle:
        nm = handle_txt
        sub = date
    else:
        nm = "投稿者不明"
        sub = date
    # 添付画像（data URI 内包・最大4）。
    imgs = [m for m in (p.media_data or []) if m][:4]
    media = ""
    if imgs:
        tiles = "".join(f"<img src='{_esc(m)}' alt=''>" for m in imgs)
        media = f"<div class='md n{len(imgs)}'>{tiles}</div>"
    # エンゲージ行: いいねは常時、他は取得できた(>0)時のみ＝捏造しない。
    eng = [f"❤️ {p.like_count:,}"]
    if p.retweet_count:
        eng.insert(0, f"🔁 {p.retweet_count:,}")
    if p.reply_count:
        eng.insert(0, f"💬 {p.reply_count:,}")
    if p.view_count:
        eng.append(f"👁 {p.view_count:,}")
    eng_html = "<div class='eng'>" + "".join(f"<span>{e}</span>" for e in eng) + "</div>"
    card = (
        "<div class='xc'><div class='top'>"
        f"{avatar}<div class='id'>"
        # sub は handle_txt(エスケープ済)＋date(安全)の連結なので再エスケープしない。
        f"<div class='nm'>{nm}{bv}</div><div class='hd'>{sub}</div></div></div>"
        f"<div class='bd'>{_esc(p.text)}</div>{media}{eng_html}</div>"
    )
    # 枠外ストリップ: 検証チップ＋属性メモ＋元投稿リンク。
    badge = (
        "<span class='badge ok'>✅ 実在検証済み</span>"
        if p.verified
        else f"<span class='badge warn'>⚠️ {_esc(p.verify_note or '要再確認')}</span>"
    )
    # 界隈タグ（Part4・マルチラベル・bio/投稿からのLLM推定）。事実でなく「推定」と明示し、
    # 営業が「どの界隈の人か」の目安として使えるようにする（断定表示にしない）。
    valid_circles = [c for c in (p.author_circles or [])[:3] if c]
    circles = ""
    if valid_circles:
        chips = "".join(f"<span class='kaiwai'>#{_esc(c)}</span>" for c in valid_circles)
        circles = f"<span class='kaiwai-label'>推定界隈</span>{chips}"
    note = f"<span class='note'>{_esc(p.author_note)}</span>" if p.author_note else ""
    href = safe_href(p.url)
    link = f"<a href='{_esc(href)}'>元投稿→</a>" if href else ""
    return card + f"<div class='xstrip'>{badge}{circles}{note}{link}</div>"


def render_voice_cards(
    *,
    product_name: str,
    posts: list[XPostCard],
    noise_note: str,
    searched: int,
) -> str:
    """① 世の中の声集め: 1投稿1カードのHTMLカード集。"""
    body = "".join(_card(p) for p in posts)
    if noise_note:
        body = (
            f"<div class='notebox'>🔎 検索メモ: {_esc(sanitize_llm_text(noise_note))}</div>" + body
        )
    unverified = sum(1 for p in posts if not p.verified)
    sub = (
        f"取得 {searched}件 → 厳選 {len(posts)}件（実在検証済み {len(posts) - unverified}件"
        f"／要再確認 {unverified}件）・作成 {_today()}"
    )
    return _page(
        f"世の中の声集め: {product_name}",
        sub,
        body,
        "全投稿は投稿ID単位で実在検証を通しています（⚠️付きは納品前に要再確認）。"
        "URLをクリックすると元投稿を確認できます。",
    )


def render_needs_report(
    *,
    theme: str,
    clusters: list[NeedCluster],
    posts: list[XPostCard],
    hypothesis_summary: str,
    searched: int,
) -> str:
    """② ニーズ発掘: インサイト仮説 → 分類ごとの投稿カード。"""
    by_id = {p.post_id: p for p in posts}
    parts: list[str] = []
    if hypothesis_summary:
        parts.append(
            "<div class='summary'>💡 <b>インサイト仮説</b><br>"
            f"{_esc(sanitize_llm_text(hypothesis_summary))}</div>"
        )
    used: set[str] = set()
    for c in clusters:
        parts.append(
            f"<div class='cluster'><h2>{_esc(sanitize_llm_text(c.label, max_len=60))}</h2>"
            f"<p>{_esc(sanitize_llm_text(c.insight))}</p></div>"
        )
        for pid in c.post_ids:
            p = by_id.get(pid)
            if p is not None:
                parts.append(_card(p))
                used.add(pid)
    rest = [p for p in posts if p.post_id not in used]
    if rest:
        parts.append("<div class='cluster'><h2>その他の注目投稿</h2><p></p></div>")
        parts.extend(_card(p) for p in rest)
    sub = f"取得 {searched}件 → 厳選 {len(posts)}件・分類 {len(clusters)}軸・作成 {_today()}"
    return _page(
        f"ニーズ発掘: {theme}",
        sub,
        "".join(parts),
        "感情ワード掛け合わせ検索（いいね数下限つき）。投稿は実在検証済み・原文のまま掲載。",
    )


def _bar_chart_svg(daily: list[dict[str, Any]], campaign_date: str | None) -> str:
    """日別発話数のインラインSVG棒グラフ（JSなし・campaign_dateに縦線）。"""
    if not daily:
        return ""
    w, h, pad_l, pad_b = 800, 220, 40, 34
    n = len(daily)
    max_c = max(int(d.get("count", 0) or 0) for d in daily) or 1
    bw = max(2.0, (w - pad_l - 8) / n - 2)
    bars: list[str] = []
    for i, d in enumerate(daily):
        c = int(d.get("count", 0) or 0)
        date = str(d.get("date", ""))
        bh = (h - pad_b - 12) * c / max_c
        x = pad_l + i * ((w - pad_l - 8) / n)
        y = h - pad_b - bh
        is_camp = campaign_date is not None and date == campaign_date
        color = "#e0245e" if is_camp else ("#1d6fdc" if c >= max_c * 0.6 else "#9db8d8")
        bars.append(
            f"<rect x='{x:.1f}' y='{y:.1f}' width='{bw:.1f}' height='{bh:.1f}' fill='{color}'>"
            f"<title>{_esc(date)}: {c}件</title></rect>"
        )
        if is_camp:
            bars.append(
                f"<line x1='{x + bw / 2:.1f}' y1='8' x2='{x + bw / 2:.1f}' y2='{h - pad_b}' "
                "stroke='#e0245e' stroke-dasharray='4 3' stroke-width='1'/>"
                f"<text x='{x + bw / 2:.1f}' y='16' font-size='10' fill='#e0245e' "
                "text-anchor='middle'>施策日</text>"
            )
        # 目盛りは間引く（最大10ラベル）
        if n <= 10 or i % max(1, n // 10) == 0:
            bars.append(
                f"<text x='{x + bw / 2:.1f}' y='{h - pad_b + 14}' font-size='9' fill='#5b6570' "
                f"text-anchor='middle'>{_esc(date[5:])}</text>"
            )
    axis = (
        f"<line x1='{pad_l}' y1='{h - pad_b}' x2='{w - 4}' y2='{h - pad_b}' stroke='#c4ccd4'/>"
        f"<text x='4' y='{h - pad_b}' font-size='10' fill='#5b6570'>0</text>"
        f"<text x='4' y='18' font-size='10' fill='#5b6570'>{max_c}</text>"
    )
    return (
        f"<svg viewBox='0 0 {w} {h}' width='100%' role='img' "
        f"aria-label='日別発話数'>{axis}{''.join(bars)}</svg>"
    )


def render_buzz_report(
    *,
    keyword: str,
    start_date: str,
    end_date: str,
    campaign_date: str | None,
    daily_counts: list[dict[str, Any]],
    top_posts: list[XPostCard],
    spike_analysis: str,
) -> str:
    """④ 効果測定: 日別推移グラフ + 読み方 + バズ投稿TOP全文カード。"""
    total = sum(int(d.get("count", 0) or 0) for d in daily_counts)
    parts: list[str] = [
        f"<div class='card'>{_bar_chart_svg(daily_counts, campaign_date)}</div>",
    ]
    if spike_analysis:
        parts.append(
            "<div class='summary'>📖 <b>読み方</b><br>"
            f"{_esc(sanitize_llm_text(spike_analysis, max_len=2000))}</div>"
        )
    if top_posts:
        parts.append("<div class='cluster'><h2>バズ投稿 TOP（全文）</h2><p></p></div>")
        parts.extend(_card(p) for p in top_posts)
    camp = f"・施策日 {campaign_date}" if campaign_date else ""
    sub = f"{start_date} 〜 {end_date}{camp}・総発話 {total:,}件・作成 {_today()}"
    return _page(
        f"X発話量 効果測定: {keyword}",
        sub,
        "".join(parts),
        "日別に分割取得した実測値（検索面の露出変動の影響を受けるため傾向・前後比較用途）。",
    )


__all__ = ["render_buzz_report", "render_needs_report", "render_voice_cards"]
