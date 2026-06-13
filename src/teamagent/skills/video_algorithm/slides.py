"""VSEO 分析 → 提案資料向け「要点スライドHTML」生成（16:9・営業がノーコード編集可）。

report.py の自己完結ダッシュボード（縦長・動画base64埋込・6〜15MB）とは別物。ここは
**提案資料に組み込むための軽量スライド**を出す:
  - 1 <section class="slide"> = 1スライド = 16:9 固定（1280x720）。PPTX 変換時の解像度と一致。
  - テキストは全て contenteditable 付きの素タグ＋意味ベースのクラス名で、営業がブラウザで直接編集可。
  - 画像は軽量サムネ（cover_data_uri 240px）だけ。**video_data_uri（数MBの動画base64）は絶対に載せない**
    （提案資料は静的・軽量であるべき）。
  - データ選択ロジック（どの値をどう選ぶか）は report.py から流用し、レイアウトのみスライド向けに再構成。

スライド割り（データがある分だけ・最大7枚。空セクションは描画しない＝report.py の早期return規約に倣う）:
  1 表紙        2 結論・勝ち筋   3 クリエイティブ指示
  4 Top5比較    5 サムネ色       6 横断シンセシス   7 CTA/提案
"""

from __future__ import annotations

from teamagent.skills.video_algorithm.report import (
    _analyzed,
    _conf_dot,
    _esc,
    _fmt,
    _next_actions,
    _shorten,
    _verdict_big,
)
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    VideoAlgorithmOutput,
)

# 16:9 スライドの論理サイズ（px）。PPTX 変換時の要素スクショ解像度（1280x720）と一致させる。
SLIDE_W = 1280
SLIDE_H = 720

_STYLE = f"""
:root{{--w:{SLIDE_W}px;--h:{SLIDE_H}px;--ink:#16181d;--mut:#6b7280;--line:#e5e7eb;
  --accent:#e8362f;--accent2:#1f2a44;--bg:#fff;--chip:#f3f4f6;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#54585f;font-family:-apple-system,'Hiragino Kaku Gothic ProN',Meiryo,
  'Noto Sans JP',sans-serif;color:var(--ink);padding:24px;display:flex;flex-direction:column;
  align-items:center;gap:24px}}
.slide{{width:var(--w);height:var(--h);background:var(--bg);position:relative;
  padding:56px 64px;overflow:hidden;box-shadow:0 6px 24px rgba(0,0,0,.35);
  page-break-after:always}}
.slide::after{{content:attr(data-no);position:absolute;right:28px;bottom:18px;
  color:#c2c6cc;font-size:13px;font-weight:700}}
.kicker{{color:var(--accent);font-weight:800;font-size:18px;letter-spacing:.06em;
  text-transform:uppercase}}
.slide-title{{font-size:38px;font-weight:800;line-height:1.25;margin:6px 0 18px;color:var(--accent2)}}
.lead{{font-size:21px;line-height:1.6;color:#33373e;max-width:1000px}}
.slide-points{{list-style:none;display:flex;flex-direction:column;gap:14px;margin-top:18px}}
.slide-points li{{font-size:21px;line-height:1.5;padding-left:30px;position:relative}}
.slide-points li::before{{content:"";position:absolute;left:4px;top:11px;width:11px;height:11px;
  background:var(--accent);border-radius:2px}}
.chips{{display:flex;flex-wrap:wrap;gap:10px;margin-top:22px}}
.chip{{background:var(--chip);border:1px solid var(--line);border-radius:999px;
  padding:8px 16px;font-size:17px;display:flex;align-items:center;gap:8px}}
.chip b{{font-weight:800}}.chip i{{color:var(--mut);font-style:normal;font-size:14px}}
.cdot{{font-size:13px}}.cdot.hi{{color:#16a34a}}.cdot.mid{{color:#d97706}}.cdot.lo{{color:#9ca3af}}
.pitch{{margin-top:24px;background:#fff7f7;border-left:5px solid var(--accent);
  padding:16px 20px;font-size:20px;border-radius:0 8px 8px 0}}
.cover-h{{display:flex;flex-direction:column;justify-content:center;height:100%}}
.cover-h .slide-title{{font-size:52px;margin:10px 0 14px}}
.cover-meta{{color:var(--mut);font-size:20px;margin-top:8px}}
.warn{{margin-top:18px;background:#fff8e6;border:1px solid #f5d98a;border-radius:8px;
  padding:12px 16px;font-size:16px;color:#7a5b00}}
/* Top5 grid */
.grid{{display:grid;gap:14px;margin-top:8px}}
.gcell{{border:1px solid var(--line);border-radius:10px;padding:12px;text-align:center;
  display:flex;flex-direction:column;gap:6px}}
.gcell.top{{border-color:var(--accent);box-shadow:0 0 0 2px rgba(232,54,47,.12)}}
.gcell .rk{{font-weight:800;color:var(--accent2)}}
.gcell img{{width:100%;height:120px;object-fit:cover;border-radius:6px;background:#eef0f3}}
.gcell .au{{font-size:13px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.gcell .mt{{font-size:14px}}.gcell .mt b{{font-size:18px}}
.bar{{height:7px;background:#eef0f3;border-radius:4px;overflow:hidden;margin-top:3px}}
.bar i{{display:block;height:100%;background:var(--accent)}}
/* thumb colors */
.trow{{display:grid;gap:14px;margin-top:10px}}
.tcell{{border:1px solid var(--line);border-radius:10px;padding:10px;text-align:center}}
.tcell img{{width:100%;height:110px;object-fit:cover;border-radius:6px}}
.sw{{display:flex;gap:6px;justify-content:center;margin:8px 0}}
.sw span{{width:26px;height:26px;border-radius:6px;border:1px solid rgba(0,0,0,.08)}}
.tline{{font-size:13px;color:var(--mut)}}
.foot-note{{position:absolute;left:64px;bottom:18px;color:#aab;font-size:13px}}
.edit-tip{{position:fixed;top:8px;left:8px;background:#111;color:#fff;font-size:12px;
  padding:6px 10px;border-radius:6px;opacity:.78;z-index:9}}
[contenteditable]:hover{{outline:2px dashed #c7ccd3;outline-offset:3px;border-radius:3px}}
[contenteditable]:focus{{outline:2px solid var(--accent);outline-offset:3px;border-radius:3px}}
@media print{{body{{background:#fff;padding:0;gap:0}}.slide{{box-shadow:none}}.edit-tip{{display:none}}}}
@page{{size:{SLIDE_W}px {SLIDE_H}px;margin:0}}
"""

_EDIT_TIP = (
    '<div class="edit-tip" data-noexport>✎ 文字をクリックして直接編集できます'
    "（保存はブラウザの印刷→PDF / または担当AIに「ここ直して」）</div>"
)


def _slide(no: int, total: int, kind: str, inner: str, *, cls: str = "") -> str:
    return (
        f'<section class="slide {cls}" data-slide="{kind}" '
        f'data-no="{no} / {total}">{inner}</section>'
    )


def _cover(out: VideoAlgorithmOutput, generated_at: str) -> str:
    n = _analyzed(out)
    meta = f"分析対象: 検索上位 {n} 本／TikTok　{_esc(generated_at)}".strip()
    return (
        '<div class="cover-h">'
        '<div class="kicker">VSEO 動画アルゴリズム分析</div>'
        f'<h1 class="slide-title" contenteditable>「{_esc(out.query)}」の勝ち筋分析</h1>'
        '<div class="lead" contenteditable>検索上位動画を構造分析し、この検索面で'
        "“なぜ上位か”＝再現すべき勝ちパターンを抽出しました。</div>"
        f'<div class="cover-meta">{meta}</div>'
        "</div>"
    )


def _conclusion(out: VideoAlgorithmOutput) -> str:
    c = out.cross
    syn = c.synthesis
    n = _analyzed(out)
    big = syn.headline if (syn and syn.headline) else _shorten(_verdict_big(out))
    sub = (syn.strategy if (syn and syn.strategy) else "") or ""
    chips = "".join(
        f'<span class="chip">{_conf_dot(w.confidence)}<b>{_esc(w.factor)}</b>'
        f"<i>{w.observed_in}/{w.total}本</i></span>"
        for w in c.win_factors[:4]
    )
    warn = (
        f'<div class="warn">⚠ 分析成立 n={n}（極小サンプル）。断定でなく観測仮説として、'
        "テスト投稿での検証前提でお読みください。</div>"
        if n < 3
        else ""
    )
    pitch = (
        f'<div class="pitch">💬 <b>クライアント提案</b>　'
        f"<span contenteditable>{_esc(syn.client_pitch)}</span></div>"
        if syn and syn.client_pitch
        else ""
    )
    sub_html = f'<div class="lead" contenteditable>{_esc(sub)}</div>' if sub else ""
    chips_html = f'<div class="chips">{chips}</div>' if chips else ""
    return (
        '<div class="kicker">結論 / 勝ち筋</div>'
        f'<h2 class="slide-title" contenteditable>{_esc(big)}</h2>'
        f"{sub_html}{chips_html}{pitch}{warn}"
    )


def _creative(out: VideoAlgorithmOutput) -> str:
    syn = out.cross.synthesis
    brief = syn.creative_brief if (syn and syn.creative_brief) else _next_actions(out)
    head = "クリエイティブ指示" if (syn and syn.creative_brief) else "次の一手（テスト投稿の仮説）"
    items = "".join(f"<li contenteditable>{_esc(x)}</li>" for x in brief[:6])
    posting = (
        f'<div class="pitch" style="background:#f4f6fb;border-left-color:#1f2a44">'
        f"📐 <b>投稿設計</b>　<span contenteditable>{_esc(syn.posting_design)}</span></div>"
        if syn and syn.posting_design
        else ""
    )
    return (
        '<div class="kicker">提案アクション</div>'
        f'<h2 class="slide-title" contenteditable>{_esc(head)}</h2>'
        f'<ul class="slide-points">{items}</ul>{posting}'
    )


def _top5(out: VideoAlgorithmOutput) -> str:
    vids = [v for v in out.videos if v.analysis]
    if not vids:
        return ""
    n = len(vids)
    max_save = max((v.meta.save_rate() for v in vids), default=0.0)

    def cell(v: AnalyzedVideo) -> str:
        m, a = v.meta, v.analysis
        assert a is not None
        save = m.save_rate()
        w = 0.0 if max_save <= 0 else min(100.0, save / max_save * 100)
        img = (
            f'<img src="{_esc(v.cover_data_uri)}" alt="#{m.rank}">'
            if v.cover_data_uri
            else '<img alt="">'
        )
        top = " top" if m.rank == 1 else ""
        return (
            f'<div class="gcell{top}"><div class="rk">#{m.rank}</div>{img}'
            f'<div class="au">@{_esc(m.author) or "—"}</div>'
            f'<div class="mt">保存率 <b>{save:.2f}%</b><div class="bar"><i style="width:{w:.0f}%"></i></div></div>'
            f'<div class="mt">{_fmt(m.play_count)}再生 ・ {a.duration_sec:.0f}s</div>'
            f'<div class="mt">フック: {_esc(a.hook_type)}</div></div>'
        )

    cells = "".join(cell(v) for v in vids)
    return (
        '<div class="kicker">Top比較</div>'
        f'<h2 class="slide-title" contenteditable>上位{n}本の当たり/外れの差</h2>'
        f'<div class="grid" style="grid-template-columns:repeat({n},1fr)">{cells}</div>'
    )


def _thumbs(out: VideoAlgorithmOutput) -> str:
    vids = [v for v in out.videos if v.analysis and (v.thumb or v.cover_data_uri)]
    if not vids:
        return ""
    c = out.cross
    consensus = c.thumb_consensus or "サムネ色の傾向"

    def cell(v: AnalyzedVideo) -> str:
        t = v.thumb
        img = (
            f'<img src="{_esc(v.cover_data_uri)}" alt="#{v.meta.rank}">'
            if v.cover_data_uri
            else '<img alt="">'
        )
        sw = (
            '<div class="sw">'
            + "".join(f'<span style="background:{_esc(h)}"></span>' for h in t.swatches[:3])
            + "</div>"
            if t and t.swatches
            else ""
        )
        line = (
            f'<div class="tline">明度 {t.brightness01:.2f} ・ {t.tone_jp()}</div>'
            if t
            else '<div class="tline">色データなし</div>'
        )
        return f'<div class="tcell"><div class="rk">#{v.meta.rank}</div>{img}{sw}{line}</div>'

    cells = "".join(cell(v) for v in vids)
    return (
        '<div class="kicker">サムネ色（クリック前の勝負）</div>'
        '<h2 class="slide-title" contenteditable>検索一覧での目立ち方</h2>'
        f'<div class="lead" contenteditable>{_esc(consensus)}</div>'
        f'<div class="trow" style="grid-template-columns:repeat({len(vids)},1fr)">{cells}</div>'
    )


def _synthesis(out: VideoAlgorithmOutput) -> str:
    s = out.cross.synthesis
    if s is None or not s.win_hypotheses:
        return ""
    items = ""
    for h in s.win_hypotheses[:4]:
        counter = (
            f'<div class="tline">反例: {_esc(h.counter_example)}</div>' if h.counter_example else ""
        )
        sw = f" → <span contenteditable>{_esc(h.so_what)}</span>" if h.so_what else ""
        items += (
            f"<li>{_conf_dot(h.confidence)} <span contenteditable>{_esc(h.hypothesis)}</span>"
            f"{sw}{counter}</li>"
        )
    return (
        '<div class="kicker">横断シンセシス（AI解釈層）</div>'
        '<h2 class="slide-title" contenteditable>勝ちパターン仮説（提案書の核）</h2>'
        f'<ul class="slide-points">{items}</ul>'
    )


def _cta(out: VideoAlgorithmOutput) -> str:
    syn = out.cross.synthesis
    pitch = (
        _esc(syn.client_pitch)
        if (syn and syn.client_pitch)
        else f"「{_esc(out.query)}」の検索面は、上記の勝ちパターンで攻略可能です。"
    )
    funnel = (
        f"<li contenteditable>{_esc(syn.shared_funnel.pattern)}</li>"
        if syn and syn.shared_funnel and syn.shared_funnel.pattern
        else ""
    )
    posting = (
        f"<li contenteditable>{_esc(syn.posting_design)}</li>" if syn and syn.posting_design else ""
    )
    return (
        '<div class="kicker">提案 / 次のステップ</div>'
        '<h2 class="slide-title" contenteditable>このプランで検索面を獲りにいく</h2>'
        f'<div class="pitch">💬 <span contenteditable>{pitch}</span></div>'
        f'<ul class="slide-points">{funnel}{posting}'
        "<li contenteditable>まずは上記の型で2〜3本テスト投稿し、保存率で検証する</li></ul>"
    )


def render_slides(out: VideoAlgorithmOutput, *, generated_at: str = "") -> str:
    """VideoAlgorithmOutput → 提案資料向け要点スライドHTML（16:9・編集可・軽量）。

    純関数（I/O無し）。動画base64は載せない＝1MB未満。空セクションは描画しない。
    """
    builders = [
        ("cover", _cover(out, generated_at)),
        ("conclusion", _conclusion(out)),
        ("creative", _creative(out)),
        ("top5", _top5(out)),
        ("thumbs", _thumbs(out)),
        ("synthesis", _synthesis(out)),
        ("cta", _cta(out)),
    ]
    filled = [(kind, body) for kind, body in builders if body]
    total = len(filled)
    sections = "".join(
        _slide(i + 1, total, kind, body, cls="cover" if kind == "cover" else "")
        for i, (kind, body) in enumerate(filled)
    )
    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>VSEO提案スライド｜{_esc(out.query)}</title>"
        f"<style>{_STYLE}</style></head><body>"
        f"{_EDIT_TIP}{sections}</body></html>"
    )
