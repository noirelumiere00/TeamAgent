"""2 段目のレポートの章: 上位の動画 1 本ずつの詳しい構成（タブ）と、5 本の比較。

作り（report.py と同じ DADS の部品・色だけに頼らない）:
- 上段: 上位 N 本のサムネ（実際の表紙 cover_data_uri・無ければ先頭のコマ）をタブとして並べ、最後に
  「N本の比較」タブ。選んでいるタブは下線と太字（形）でも分かる。
- JS はタブの切り替えだけ（WAI-ARIA の tabs・矢印キー・Home/End）。JS が無い・止まっているときは
  タブはページ内リンクになり、全パネルが縦に並ぶ（どの内容も読める）。印刷でも全部出す。
- 動画ごとのパネル: 見出し（1 段目の投稿の数字）→ 構成の全体図（役割ごとの時間配分と主要な数字）
  → 全場面の構成表（最大 12 行・コマつき）→ 評価（◎○△—・コードが数字の基準で決める）
  → 学べること・弱点（LLM・数字は照合済み）。
- 比較タブ: 評価軸 × N 本の表 → 共通点（決定的）→ クライアント向けの絵コンテ案（LLM・照合済み）。

コマ画像は data URI で埋め込む。1 ファイルの大きさを抑えるため、形の正しい JPEG/PNG/WebP の
data URI だけを使い、1 枚の上限（``FRAME_MAX_CHARS``）と章全体の上限（``IMAGE_BUDGET_CHARS``）を
超えたコマは埋め込まずに「コマ省略」と書く。
"""

from __future__ import annotations

import html as _html
import re

from teamagent.skills._shared.text_safety import safe_href, sanitize_llm_text
from teamagent.skills.search_surface_check.display import (
    category_label,
    fmt_count,
    fmt_duration,
)
from teamagent.skills.search_surface_check.schema import SurfacePost
from teamagent.skills.search_surface_check.video_digest import (
    duration_of,
    hook_label,
    is_cover_only,
    is_watched,
)
from teamagent.skills.search_surface_check.video_notes import (
    STORYBOARD_STAGES,
    StructureNotes,
)
from teamagent.skills.search_surface_check.video_structure import (
    AXES,
    GRADE_RULES,
    MARK_WORD,
    ROLE_LABEL,
    Grade,
    RoleShare,
    SceneRow,
    VideoKeys,
    fmt_sec,
    grade_video,
    omitted_scenes,
    others_text,
    prominence_label,
    relation_label,
    role_flow,
    role_shares,
    scene_rows,
    video_keys,
)
from teamagent.skills.video_algorithm.schema import AnalyzedVideo

TABS_ID = "vtabs"
# 1 枚のコマ（data URI の文字数）の上限。幅 180px の JPEG は 1 万字前後。
FRAME_MAX_CHARS = 80_000
# 章全体に埋め込む画像（表紙＋コマ）の文字数の上限。5 本 × 12 コマ × 1.4 万字 ≈ 84 万字の想定の
# 3 倍強。超えた分は埋め込まない（レポートの配信と表示を重くしない）。
IMAGE_BUDGET_CHARS = 3_000_000
_DATA_URI_RE = re.compile(r"^data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/]+=*$")
NOTES_SOURCE = (
    "AI が上の構成表と評価だけを根拠に書いたメモです。文中の数字と順位は、その動画の構成表の値と"
    "照合済みです（合わない文は載せていません）。"
)
NOTES_MISSING = "AI のメモを作れませんでした（構成表と評価はそのまま読めます）。"
STORYBOARD_SOURCE = (
    "上位の共通点から AI が組んだ案です。文中の数字は上位の構成表と共通点の値と照合済みです。"
)

ROLE_COLOR: dict[str, str] = {
    "hook": "var(--color-primitive-blue-900)",
    "problem": "var(--color-primitive-purple-700)",
    "steps": "var(--color-primitive-cyan-900)",
    "result": "var(--color-primitive-green-800)",
    "proof": "var(--color-primitive-orange-800)",
    "cta": "var(--color-primitive-magenta-900)",
    "other": "var(--color-neutral-solid-gray-600)",
}
_MARK_CLASS = {"◎": "m-good", "○": "m-ok", "△": "m-weak", "—": "m-none"}

CHAPTER_CSS = """
.vt-hint{font-size:14px;color:var(--color-neutral-solid-gray-600)}
.vtab-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(120px,100%),1fr));
  gap:8px;margin:8px 0 0;padding:0 0 8px;
  border-bottom:1px solid var(--color-neutral-solid-gray-420)}
.vtab{display:flex;flex-direction:column;align-items:center;gap:4px;padding:8px 4px 6px;
  border:1px solid var(--color-neutral-solid-gray-420);border-radius:var(--border-radius-8);
  border-bottom:4px solid transparent;color:var(--color-neutral-solid-gray-800);
  text-decoration:none;font-size:14px;line-height:1.4;text-align:center;min-width:0;
  background:var(--color-neutral-white)}
.vtab img,.vtab .noimg{width:72px;height:96px;object-fit:cover;border-radius:var(--border-radius-4);
  background:var(--color-neutral-solid-gray-100)}
.vtab .noimg{display:flex;align-items:center;justify-content:center;font-size:12px;
  color:var(--color-neutral-solid-gray-600)}
.vtab .vt-handle{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  color:var(--color-neutral-solid-gray-600)}
.vtab .vt-rank{font-weight:700}
.vtab-compare{justify-content:center;font-weight:700}
.vtab:hover{background:var(--color-primitive-blue-50)}
.vtab:focus-visible{outline:4px solid var(--color-neutral-black);outline-offset:2px;
  background:var(--color-primitive-yellow-300)}
.vtab[aria-selected=true]{border-color:var(--color-primitive-blue-900);
  border-bottom-color:var(--color-primitive-blue-900);background:var(--color-primitive-blue-50);
  font-weight:700;color:var(--color-primitive-blue-1000)}
.vtab[aria-selected=true] .vt-rank{text-decoration:underline;text-underline-offset:4px;
  text-decoration-thickness:3px}
.vpanel{padding:8px 0 24px;border-bottom:1px solid var(--color-neutral-solid-gray-420)}
.vtabs.is-js .vpanel{border-bottom:0}
.vpanel:focus-visible{outline:4px solid var(--color-neutral-black);outline-offset:4px}
.vpanel h3{margin-top:24px}
.vpanel h4{font-size:18px;margin:24px 0 8px}
.vfacts{display:flex;flex-wrap:wrap;gap:4px 20px;margin:0 0 8px;font-size:14px}
.vfacts div{display:flex;gap:6px}.vfacts dt{color:var(--color-neutral-solid-gray-600)}
.vfacts dd{margin:0;font-weight:700}
.vgist{margin:8px 0 0}
.rolebar{display:flex;height:40px;border-radius:var(--border-radius-4);overflow:hidden;
  margin:8px 0;background:var(--color-neutral-solid-gray-50)}
.rolebar .seg{display:flex;align-items:center;justify-content:center;
  color:var(--color-neutral-white);font-size:13px;font-weight:700;white-space:nowrap;
  overflow:hidden;border-right:2px solid var(--color-neutral-white);min-width:0}
.rolebar .seg:last-child{border-right:0}
.rolelegend{display:flex;flex-wrap:wrap;gap:4px 16px;margin:0 0 16px;padding:0;list-style:none;
  font-size:14px}
.rolelegend .swatch{display:inline-block;width:12px;height:12px;border-radius:2px;
  margin-right:6px;vertical-align:-1px}
table.scenes td.shot{width:112px}
table.scenes img{display:block;width:96px;height:auto;border-radius:var(--border-radius-4)}
table.scenes .noshot{font-size:12px;color:var(--color-neutral-solid-gray-600)}
table.scenes td.sec{white-space:nowrap;font-variant-numeric:tabular-nums}
.role-tag{display:inline-block;border-radius:var(--border-radius-4);padding:0 6px;
  color:var(--color-neutral-white);font-weight:700;font-size:13px;white-space:nowrap}
.inferred{display:block;font-size:12px;color:var(--color-neutral-solid-gray-600)}
.mark{display:inline-block;min-width:1.6em;font-size:18px;font-weight:700;line-height:1.2}
.m-good{color:var(--color-primitive-green-900)}.m-ok{color:var(--color-primitive-blue-900)}
.m-weak{color:var(--color-primitive-orange-800)}.m-none{color:var(--color-neutral-solid-gray-600)}
.mark-word{font-size:12px;color:var(--color-neutral-solid-gray-700);margin-left:2px}
table.compare td{min-width:9em}
.notes{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(320px,100%),1fr));gap:16px}
.notes ul{margin:0;padding-left:1.4em}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
  clip:rect(0,0,0,0);white-space:nowrap;border:0}
.rules{font-size:14px;margin:0;padding-left:1.4em}
@media (max-width:640px){
  table.scenes thead{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}
  table.scenes,table.scenes tbody,table.scenes td{display:block;width:auto}
  table.scenes tr{display:grid;grid-template-columns:88px minmax(0,1fr);gap:0 8px;
    border-bottom:1px solid var(--color-neutral-solid-gray-420);padding:8px}
  table.scenes td{border:0;padding:1px 0;grid-column:2;overflow-wrap:anywhere}
  table.scenes td.shot{grid-column:1;grid-row:1 / span 7;width:auto}
  table.scenes td[data-label]::before{content:attr(data-label) "：";font-weight:700;
    color:var(--color-neutral-solid-gray-600)}
  table.scenes img{width:88px}
  .vtab-list{grid-template-columns:repeat(3,minmax(0,1fr))}
  .vtab img,.vtab .noimg{width:56px;height:75px}
  .rolebar .seg{font-size:0}
}
@media print{.vtabs .vpanel[hidden]{display:block !important}.vtab-list{display:none}}
"""

TABS_JS = """
(function(){var root=document.getElementById('vtabs');if(!root){return;}
var tabs=[].slice.call(root.querySelectorAll('.vtab'));
var panels=tabs.map(function(t){return document.getElementById(t.getAttribute('aria-controls'));});
if(!tabs.length||panels.indexOf(null)>=0){return;}
root.classList.add('is-js');root.querySelector('.vtab-list').setAttribute('role','tablist');
function select(i,focus){tabs.forEach(function(t,j){var on=i===j;
t.setAttribute('aria-selected',on?'true':'false');t.tabIndex=on?0:-1;panels[j].hidden=!on;});
if(focus){tabs[i].focus();}}
tabs.forEach(function(t,i){t.setAttribute('role','tab');panels[i].setAttribute('role','tabpanel');
panels[i].setAttribute('aria-labelledby',t.id);panels[i].tabIndex=0;
t.addEventListener('click',function(e){e.preventDefault();select(i,false);});
t.addEventListener('keydown',function(e){var n=tabs.length,j=null;
if(e.key==='ArrowRight'){j=(i+1)%n;}else if(e.key==='ArrowLeft'){j=(i-1+n)%n;}
else if(e.key==='Home'){j=0;}else if(e.key==='End'){j=n-1;}
if(j!==null){e.preventDefault();select(j,true);}});});
function fromHash(){var h=location.hash.slice(1),k=-1;
tabs.forEach(function(t,i){if(t.getAttribute('aria-controls')===h){k=i;}});return k;}
window.addEventListener('hashchange',function(){var k=fromHash();if(k>=0){select(k,false);}});
select(Math.max(0,fromHash()),false);})();
"""


def _esc(s: object) -> str:
    return _html.escape(str(s), quote=True)


def _text(text: str, max_len: int = 160) -> str:
    return _esc(sanitize_llm_text(" ".join((text or "").split()), max_len=max_len))


class _ImageBudget:
    """埋め込む画像の文字数を数え、上限を超えたら埋め込まない。"""

    def __init__(self, limit: int = IMAGE_BUDGET_CHARS) -> None:
        self.limit = limit
        self.used = 0
        self.skipped = 0

    def take(self, uri: str) -> str | None:
        if not uri or len(uri) > FRAME_MAX_CHARS or not _DATA_URI_RE.match(uri):
            return None
        if self.used + len(uri) > self.limit:
            self.skipped += 1
            return None
        self.used += len(uri)
        return uri


def _role_color(role: str) -> str:
    return ROLE_COLOR.get(role, ROLE_COLOR["other"])


def _panel_id(rank: int) -> str:
    return f"vp-{rank}"


def _handle(video: AnalyzedVideo) -> str:
    return f"@{video.meta.author}" if video.meta.author else "不明"


# ── タブ ──────────────────────────────────────────────────────────────


def _tab(video: AnalyzedVideo, budget: _ImageBudget) -> str:
    rank = video.meta.rank
    head = video.cover_data_uri or next((f.data_uri for f in video.frames if f.data_uri), "")
    uri = budget.take(head)
    img = (
        f"<img src='{_esc(uri)}' alt='' width='72' height='96' loading='lazy'>"
        if uri
        else "<span class='noimg' aria-hidden='true'>画像なし</span>"
    )
    return (
        f"<a class='vtab' id='vt-{rank}' href='#{_panel_id(rank)}' "
        f"aria-controls='{_panel_id(rank)}'>{img}"
        f"<span class='vt-rank'>{rank}位</span>"
        f"<span class='vt-handle'>{_esc(_handle(video))}</span></a>"
    )


# ── 動画ごとのパネル ─────────────────────────────────────────────────────


def _facts(video: AnalyzedVideo, post: SurfacePost | None) -> str:
    meta = video.meta
    items: list[tuple[str, str]] = []
    if post is not None and post.author_name:
        items.append(("表示名", post.author_name))
    items.append(("再生", f"{fmt_count(meta.play_count)}回" if meta.play_count else "—"))
    save = f"{meta.save_rate():.1f}%" if meta.play_count and meta.collect_count else "—"
    items.append(("保存率", save))
    dur = duration_of(video)
    items.append(("尺", fmt_duration(round(dur)) or "—"))
    items.append(("フォロワー", fmt_count(meta.follower_count) if meta.follower_count else "—"))
    if post is not None:
        items.append(("タイプ", category_label(post.category)))
    return (
        "<dl class='vfacts'>"
        + "".join(f"<div><dt>{_esc(k)}</dt><dd>{_esc(v)}</dd></div>" for k, v in items)
        + "</dl>"
    )


def _panel_heading(video: AnalyzedVideo) -> str:
    rank = video.meta.rank
    href = safe_href(video.meta.url)
    handle = _esc(_handle(video))
    name = f"<a href='{_esc(href)}'>{handle}</a>" if href else handle
    return f"<h3 id='{_panel_id(rank)}-h'>{rank}位 {name}</h3>"


def _role_bar(shares: list[RoleShare]) -> str:
    if not shares:
        return "<p>場面の区切りを取得できなかったため、時間配分は出していません。</p>"
    segs = "".join(
        f"<div class='seg' style='width:{s.pct}%;background:{_role_color(s.role)}'"
        f" title='{_esc(s.label)} {_esc(fmt_sec(s.sec))}（{s.pct}%）'>"
        f"{_esc(s.label) if s.pct >= 12 else ''}</div>"
        for s in shares
        if s.pct > 0
    )
    legend = "".join(
        f"<li><span class='swatch' style='background:"
        f"{ROLE_COLOR.get(s.role, ROLE_COLOR['other'])}'></span>{_esc(s.label)} "
        f"{_esc(fmt_sec(s.sec))}（{s.pct}%）</li>"
        for s in shares
    )
    label = "、".join(f"{s.label} {s.pct}%" for s in shares)
    return (
        f"<div class='rolebar' role='img' aria-label='役割ごとの時間配分: {_esc(label)}'>"
        f"{segs}</div>"
        f"<ul class='rolelegend'>{legend}</ul>"
    )


def _kpi(label: str, value: str, note: str = "") -> str:
    note_html = f"<span class='n'>{_esc(note)}</span>" if note else ""
    return (
        f"<div class='kpi'><dt>{_esc(label)}</dt>"
        f"<dd><span class='v v-text'>{_esc(value)}</span>{note_html}</dd></div>"
    )


def _key_tiles(k: VideoKeys) -> str:
    cuts = f"{k.cut_count}カット" if k.cut_count is not None else "不明"
    avg = f"平均 {fmt_sec(k.avg_cut_sec)}/カット" if k.avg_cut_sec is not None else ""
    telop = fmt_sec(k.first_telop_sec) if k.first_telop_sec is not None else "なし"
    if k.kw_first_sec is not None:
        kw, kw_note = fmt_sec(k.kw_first_sec), k.kw_first_layer
    elif k.kw_matched_no_sec:
        kw, kw_note = "秒は不明", "テロップか発話に出る"
    else:
        kw, kw_note = "出ない", "テロップ・発話"
    if k.kw_in_caption:
        kw_note += "・キャプションにもあり"
    if k.has_brand:
        brand = f"初出 {fmt_sec(k.brand_first_sec)}" if k.brand_first_sec is not None else "映る"
        brand_note = "・".join(
            p
            for p in (
                k.brand_name,
                f"合計 {fmt_sec(k.brand_total_sec)}",
                prominence_label(k.brand_prominence),
                relation_label(k.brand_relation),
                f"ほかに{others_text(k.brand_others)}" if k.brand_others else "",
            )
            if p
        )
    else:
        brand, brand_note = "映らない", ""
    if k.has_cta:
        cta = fmt_sec(k.cta_sec) if k.cta_sec is not None else "秒は不明"
        cta_note = "・".join(k.cta_types) or "型は不明"
    else:
        cta, cta_note = "なし", ""
    sound = "ナレーションあり" if k.narration else "ナレーションなし"
    sound_note = {"yes": "流行の音源", "no": "流行の音源ではない"}.get(k.trending, "音源は不明")
    tiles = [
        _kpi("カット数", cuts, avg),
        _kpi("最初のテロップ", telop),
        _kpi("検索 KW の初出", kw, kw_note),
        _kpi("商品（ブランド）", brand, brand_note),
        _kpi("CTA", cta, cta_note),
        _kpi("音", sound, sound_note),
    ]
    return f"<dl class='kpis'>{''.join(tiles)}</dl>"


def _scene_table(video: AnalyzedVideo, rows: list[SceneRow], budget: _ImageBudget) -> str:
    if not rows:
        return "<p>場面の区切りを取得できなかったため、構成表は出していません。</p>"
    body: list[str] = []
    for r in rows:
        uri = budget.take(r.frame.data_uri) if r.frame is not None else None
        shot = (
            f"<img src='{_esc(uri)}' alt='{_esc(fmt_sec(r.start))}の場面' loading='lazy'>"
            if uri
            else "<span class='noshot'>コマ省略</span>"
        )
        color = ROLE_COLOR.get(r.role, ROLE_COLOR["other"])
        inferred = "<span class='inferred'>（推定）</span>" if r.role_inferred else ""
        body.append(
            "<tr>"
            f"<td class='sec' data-label='秒'>{_esc(f'{r.start:g}〜{r.end:g}秒')}</td>"
            f"<td class='shot'>{shot}</td>"
            f"<td data-label='役割'><span class='role-tag' style='background:{color}'>"
            f"{_esc(r.role_label)}</span>{inferred}</td>"
            f"<td data-label='画面'>{_text(r.desc) or '—'}</td>"
            f"<td data-label='テロップ'>{_text(r.telop) or '—'}</td>"
            f"<td data-label='発話'>{_text(r.speech) or '—'}</td>"
            f"<td data-label='狙い'>{_text(r.intent) or '—'}</td>"
            "</tr>"
        )
    omitted = omitted_scenes(video)
    note = (
        f"<p class='vt-hint'>場面が多いため、最初の{len(rows) - 1}場面と最後の場面を出しています"
        f"（{omitted}場面を省略）。</p>"
        if omitted
        else ""
    )
    return (
        "<div class='dads-table-wrap'><table class='dads-table scenes'><thead><tr>"
        "<th scope='col'>秒</th><th scope='col'>コマ</th><th scope='col'>役割</th>"
        "<th scope='col'>画面</th><th scope='col'>テロップ</th><th scope='col'>発話</th>"
        f"<th scope='col'>狙い</th></tr></thead><tbody>{''.join(body)}</tbody></table></div>{note}"
    )


def _mark(mark: str) -> str:
    return (
        f"<span class='mark {_MARK_CLASS.get(mark, 'm-none')}' aria-hidden='true'>"
        f"{_esc(mark)}</span>"
        f"<span class='mark-word'>{_esc(MARK_WORD.get(mark, ''))}</span>"
    )


def _grade_table(grades: list[Grade]) -> str:
    rows = "".join(
        f"<tr><th scope='row'>{_esc(g.axis)}</th><td>{_mark(g.mark)}</td>"
        f"<td>{_esc(g.reason)}</td></tr>"
        for g in grades
    )
    return (
        "<div class='dads-table-wrap'><table class='dads-table grades'><thead><tr>"
        "<th scope='col'>評価軸</th><th scope='col'>評価</th><th scope='col'>理由（数字）</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _notes_block(rank: int, notes: StructureNotes | None) -> str:
    note = notes.videos.get(rank) if notes is not None else None
    if note is None or not (note.learn or note.weak):
        return f"<p class='vt-hint'>{_esc(NOTES_MISSING)}</p>"

    def items(values: list[str], empty: str) -> str:
        if not values:
            return f"<p>{_esc(empty)}</p>"
        return "<ul>" + "".join(f"<li>{_esc(v)}</li>" for v in values) + "</ul>"

    return (
        "<div class='notes'>"
        f"<div><h5>学べること</h5>{items(note.learn, '（なし）')}</div>"
        f"<div><h5>弱点</h5>{items(note.weak, '目立った弱点はありません')}</div>"
        f"</div><p class='vt-hint'>{_esc(NOTES_SOURCE)}</p>"
    )


def _video_panel(
    video: AnalyzedVideo,
    post: SurfacePost | None,
    notes: StructureNotes | None,
    budget: _ImageBudget,
) -> str:
    rank = video.meta.rank
    parts = [
        f"<section class='vpanel' id='{_panel_id(rank)}' aria-labelledby='{_panel_id(rank)}-h'>",
        _panel_heading(video),
        _facts(video, post),
    ]
    a = video.analysis
    if a is None:
        parts.append(
            "<div class='dads-notice' role='note'><b>この動画は分析できませんでした。</b>"
            f"（{_esc(video.error or '理由不明')}）</div></section>"
        )
        return "".join(parts)
    if is_cover_only(video) or not is_watched(video):
        parts.append(
            "<div class='dads-notice' role='note'><b>動画を取得できず、サムネだけの分析です。</b>"
            "テロップ・構成・音は判定できないため、構成表と評価は出していません。</div>"
            f"<p>サムネから読めたこと: {_text(a.hook_summary or a.main_message)}</p></section>"
        )
        return "".join(parts)
    keys = video_keys(video)
    flow = "、".join(ROLE_LABEL.get(r, r) for r in role_flow(a))
    gist = [
        ("フック", f"{hook_label(a.hook_type)}：{a.hook_summary}" if a.hook_summary else ""),
        ("主なメッセージ", a.main_message),
        ("保存の動機", a.save_share_motivation),
    ]
    rows = "".join(
        f"<dt>{_esc(label)}</dt><dd>{_text(value)}</dd>" for label, value in gist if value.strip()
    )
    if rows:
        parts.append(f"<dl class='points vgist'>{rows}</dl>")
    parts.append("<h4>構成の全体図</h4>")
    if flow:
        parts.append(f"<p>流れ: {_esc(flow)}</p>")
    parts.append(_role_bar(role_shares(a)))
    if keys is not None:
        parts.append(_key_tiles(keys))
    parts.append("<h4>全場面の構成表</h4>")
    parts.append(_scene_table(video, scene_rows(video), budget))
    parts.append("<h4>評価</h4>")
    parts.append(_grade_table(grade_video(video)))
    parts.append("<h4>学べること・弱点</h4>")
    parts.append(_notes_block(rank, notes))
    parts.append("</section>")
    return "".join(parts)


# ── 比較タブ ──────────────────────────────────────────────────────────


def _compare_panel(
    videos: list[AnalyzedVideo], common: list[str], notes: StructureNotes | None
) -> str:
    n = len(videos)
    head = "".join(
        f"<th scope='col'>{v.meta.rank}位<br><span class='vt-hint'>{_esc(_handle(v))}</span></th>"
        for v in videos
    )
    grades = {v.meta.rank: {g.axis: g for g in grade_video(v)} for v in videos}
    rows: list[str] = []
    for axis in AXES:
        cells = []
        for v in videos:
            g = grades[v.meta.rank].get(axis)
            cells.append(
                f"<td>{_mark(g.mark)}<br>{_esc(g.reason)}</td>" if g is not None else "<td>—</td>"
            )
        rows.append(f"<tr><th scope='row'>{_esc(axis)}</th>{''.join(cells)}</tr>")
    flows = []
    for v in videos:
        a = v.analysis
        text = "、".join(ROLE_LABEL.get(r, r) for r in role_flow(a)) if a and is_watched(v) else ""
        flows.append(f"<td>{_esc(text) or '—'}</td>")
    rows.append(f"<tr><th scope='row'>構成の流れ</th>{''.join(flows)}</tr>")
    table = (
        "<div class='dads-table-wrap'><table class='dads-table compare'><thead><tr>"
        f"<th scope='col'>評価軸</th>{head}</tr></thead><tbody>{''.join(rows)}</tbody>"
        "</table></div>"
    )
    common_html = (
        "<ul class='angles'>" + "".join(f"<li>{_esc(c)}</li>" for c in common) + "</ul>"
        if common
        else "<p>動画を見て分析できた本が無いため、共通点は出していません。</p>"
    )
    steps = notes.storyboard if notes is not None else []
    if steps:
        sb_rows = "".join(
            f"<tr><th scope='row'>{_esc(s.stage)}</th><td>{_esc(s.show) or '—'}</td>"
            f"<td>{_esc(s.telop) or '—'}</td></tr>"
            for s in steps
        )
        storyboard = (
            "<div class='dads-table-wrap'><table class='dads-table storyboard'><thead><tr>"
            "<th scope='col'>段</th><th scope='col'>映すもの</th><th scope='col'>テロップの案</th>"
            f"</tr></thead><tbody>{sb_rows}</tbody></table></div>"
            f"<p class='vt-hint'>{_esc(STORYBOARD_SOURCE)}</p>"
        )
    else:
        storyboard = "<p class='vt-hint'>AI の絵コンテ案を作れませんでした。</p>"
    return (
        f"<section class='vpanel' id='vp-compare' aria-labelledby='vp-compare-h'>"
        f"<h3 id='vp-compare-h'>{n}本の比較</h3>"
        "<h4>評価軸ごとの比較</h4>"
        "<p>記号はコードが数字の基準で付けたものです（基準は章の最後）。"
        "— は判定できなかった項目です。</p>"
        f"{table}<h4>上位に共通すること</h4>{common_html}"
        f"<h4>クライアント向けの絵コンテ案</h4>{storyboard}</section>"
    )


def render_tabs(
    videos: list[AnalyzedVideo],
    *,
    posts: dict[int, SurfacePost] | None = None,
    notes: StructureNotes | None = None,
    common: list[str] | None = None,
    image_budget: int = IMAGE_BUDGET_CHARS,
) -> str:
    """タブ（サムネ）と、動画ごとのパネル・比較パネル。JS が無ければ縦に並ぶ。"""
    budget = _ImageBudget(image_budget)
    posts = posts or {}
    tabs = [_tab(v, budget) for v in videos]
    tabs.append(
        "<a class='vtab vtab-compare' id='vt-compare' href='#vp-compare' "
        f"aria-controls='vp-compare'><span class='vt-rank'>{len(videos)}本の比較</span></a>"
    )
    panels = [_video_panel(v, posts.get(v.meta.rank), notes, budget) for v in videos]
    panels.append(_compare_panel(videos, common or [], notes))
    skipped = (
        f"<p class='vt-hint'>レポートを軽くするため、{budget.skipped}コマは埋め込んでいません。</p>"
        if budget.skipped
        else ""
    )
    rules = "".join(f"<li><b>{_esc(axis)}</b>: {_esc(rule)}</li>" for axis, rule in GRADE_RULES)
    return (
        "<h3>1 本ずつの構成</h3>"
        "<p class='vt-hint'>サムネを選ぶと、その動画の構成に切り替わります。最後のタブで"
        f"{len(videos)}本を比べられます。</p>"
        f"<div class='vtabs' id='{TABS_ID}'>"
        f"<nav class='vtab-list' aria-label='上位の動画'>{''.join(tabs)}</nav>"
        f"{''.join(panels)}</div>{skipped}"
        f"<script>{TABS_JS}</script>"
        "<h4>評価（◎○△—）の基準</h4>"
        f"<ul class='rules'>{rules}</ul>"
        f"<p class='vt-hint'>役割の「（推定）」は、動画分析 AI が役割を出さなかった場面を、"
        "最初の場面＝フック、CTA の秒を含む場面＝CTA、ほかは手順としてコードが補ったものです。"
        f"絵コンテ案の段は {'・'.join(STORYBOARD_STAGES)} です。</p>"
    )


__all__ = [
    "CHAPTER_CSS",
    "FRAME_MAX_CHARS",
    "IMAGE_BUDGET_CHARS",
    "NOTES_MISSING",
    "NOTES_SOURCE",
    "ROLE_COLOR",
    "STORYBOARD_SOURCE",
    "TABS_ID",
    "TABS_JS",
    "render_tabs",
]
