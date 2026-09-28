"""VSEO 分析 → 提案資料向け「要点スライドHTML」生成（16:9・営業がノーコード編集可）。

report.py の自己完結ダッシュボード（縦長・動画base64埋込）とは別物。ここは**提案資料に組み込む
スライド**を出す（仕様 v3 §2）:
  - 1 <section class="slide"> = 1スライド = 16:9 固定（1280x720）。PPTX 変換時の撮影サイズと一致。
  - テキストは contenteditable 付きの素タグ＋意味ベースのクラス名で、営業がブラウザで直接編集可。
  - 画像は表紙とコマ（data URI）だけ。**video_data_uri（数MBの動画base64）は絶対に載せない**。
    外部 URL（http/https の src・href）も載せない（media worker は外部参照のある HTML を拒否する）。

流れ（10＋n 枚。n＝動画を見て分析できた本数・サムネだけの縮退は数えない。空のスライドは出さない）:
  S1 表紙 → S2 結論 → S3 検索面の地図 → S4 ブランド露出マップ → S5 上位n本の比較
  → S6 n本の構成比較 → S7〜 構成分解（1本1枚）→ 共通する構成の型 → クリエイティブ指示／やらないこと
  → 絵コンテ案A（→ 案B）→ 投稿設計と検証

数字・本数・段階の名前（必須条件／多数派／事例）・区分・PR は事実層（facts / evidence）がコードで
決める。LLM 由来の文は synthesis v3 の検査を通したもの（CrossSynthesis.version == "v3"）だけを使い、
無ければコードの事実で代わりの文を出すか、その欄を省く（旧キャッシュの v2 の文は出さない）。

版面の約束（T22 で実描画を測る）: 中身は y≤664 に収め、フッタ（y≈684・14px）を全スライドに出す。
文字は最小 14px。長い文は -webkit-line-clamp で行数を決めて切る（PPTX は撮影なので、はみ出した分は
黙って切れる）。画像は 9:16 のまま全体を見せる（object-fit:contain）。横長の動画は横長の枠に入れる。
"""

from __future__ import annotations

import html
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from teamagent.skills._html.dads import DADS_LICENSE_COMMENT, DADS_TOKENS_CSS
from teamagent.skills.search_surface_check.display import fmt_count
from teamagent.skills.search_surface_check.video_chapter import ROLE_COLOR
from teamagent.skills.search_surface_check.video_digest import HOOK_LABEL
from teamagent.skills.search_surface_check.video_structure import (
    ROLE_LABEL,
    Grade,
    grade_video,
    infer_roles,
)
from teamagent.skills.video_algorithm.evidence import (
    TIER_MAJORITY,
    TIER_REQUIRED,
    Roster,
    contains,
    majority_min,
    norm,
    query_terms,
    ranks_text,
    ref_frame,
    tier_text,
)
from teamagent.skills.video_algorithm.facts import (
    CTA_KIND_LABEL,
    EVENT_LABEL,
    ORIENTATION_LABEL,
    POSITION_LABEL,
    PROMINENCE_LABEL,
    STAGE_LABELS,
    BrandFact,
    Feature,
    KwRow,
    StageRow,
    VideoFacts,
    category_known,
    fmt_stamp,
    kw_matrix,
    surface_map,
    template,
)
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    CrossSynthesis,
    FrameShot,
    PerVideoNote,
    Scene,
    Storyboard,
    SynthRef,
    VideoAlgorithmOutput,
    VideoVSEOAnalysis,
)
from teamagent.skills.video_algorithm.synthesis_checks import (
    SYNTHESIS_V3,
    alt_type_line,
    code_directives,
    evidence_text,
)
from teamagent.skills.video_algorithm.synthesis_input import SynthesisContext

# 16:9 スライドの論理サイズ（px）。PPTX 変換時の要素スクショ解像度（1280x720）と一致させる。
SLIDE_W = 1280
SLIDE_H = 720
# 中身の下端（フッタより上）。T22 で全スライドの子要素がこれ以内かを測る。
CONTENT_BOTTOM = 664
# 撮影の前に隠す要素（編集ヒント）。pptx_export と media/render_child が同じ CSS を入れる。
NOEXPORT_CSS = "[data-noexport]{display:none!important}"

_PLACEHOLDER_LOGO = "ロゴ（不明）"
# 構成バーで役割の名前を入れる区間の最小幅（px）と、バーの最小の高さ（それ未満は名前を入れない）。
_ROLE_NAME_MIN_PX = 56
_ROLE_NAME_MIN_H = 20
# 印（▼＋文字・14px）の 1 文字あたりの幅の目安（近い印をまとめる判定に使う）。
_MARK_PX_PER_CHAR = 15
# 構成分解のコマの枚数（縦・横）。
_FRAMES_PORTRAIT = 6
_FRAMES_LANDSCAPE = 4
# 評価の軸の短い名前（構成分解の 1 行）。
_AXIS_SHORT = {
    "冒頭3秒の掴み": "掴み",
    "テンポ": "テンポ",
    "KWの露出": "KW",
    "保存の仕掛け": "保存",
    "CTA": "CTA",
    "商品の見せ方": "商品",
    "一致度": "一致",
}
# 構成バーの印（形＝文字で区別し、色だけに頼らない）。
_MARK_LETTER = {
    "first_telop": "テ",
    "kw_telop": "検",
    "brand_first": "商",
    "result_first": "完",
    "cta": "締",
}
_MARK_LEGEND = {
    "first_telop": "最初のテロップ",
    "kw_telop": "検索語のテロップ",
    "brand_first": "目立つ商品",
    "result_first": "完成品",
    "cta": "CTA",
}
_MARK_SHORT = {
    "first_telop": "最初のテロップ",
    "kw_telop": "検索語",
    "brand_first": "商品",
    "result_first": "完成品",
    "cta": "CTA",
}
# 結論のチップに出す特徴（この順・前方一致）。タイアップ・横長・CTA の型は別の欄で出す。
_CHIP_ORDER: tuple[str, ...] = (
    "first_telop_0s",
    "kw_telop_3s:",
    "kw_telop:",
    "kw_telop_syn:",
    "qty_anywhere",
    "qty_telop",
    "narration",
    "hook:",
    "brand_category_prominent",
    "kw_caption:",
    "kw_hashtag:",
    "cta_video",
    "result_5s",
    "orientation:portrait",
)
_MAX_CHIPS = 4
# 共通する構成の型: 段ごとの出来事の数・拾われる条件の表の語の数（版面に収める上限）。
_STAGE_EVENTS = 2
_KW_TABLE_TERMS = 3
# 絵コンテの段の呼び名（秒は隣の列に出すので、段は短い名前にする）。
_STAGE_SHORT = {
    STAGE_LABELS[0]: "冒頭",
    STAGE_LABELS[1]: "導入",
    STAGE_LABELS[2]: "本編",
    STAGE_LABELS[3]: "締め",
}
_VERIFY_TEXT = (
    "投稿後、このKWでの表示順位を翌日と7日後に確認する。保存率は補助の指標として見る{basis}。"
)
_ORDER_FIELDS: tuple[str, ...] = (
    "投稿するアカウント",
    "本数",
    "投稿日",
    "成功の基準（何位・いつ）",
    "予算",
    "担当",
)

# 配色はデジタル庁デザインシステム（DADS）のトークンに寄せる（キーカラー blue-900）。
# 撮影（1280x720）前提のため、dads_style() の基本部品は入れず、トークンと MIT の出典だけを載せる。
_STYLE = (
    DADS_LICENSE_COMMENT
    + DADS_TOKENS_CSS
    + f"""
:root{{--w:{SLIDE_W}px;--h:{SLIDE_H}px;--ink:var(--color-neutral-solid-gray-900);
  --sub:var(--color-neutral-solid-gray-800);--mut:var(--color-neutral-solid-gray-600);
  --line:var(--color-neutral-solid-gray-420);--soft:var(--color-neutral-solid-gray-200);
  --accent:var(--color-primitive-blue-900);--accent2:var(--color-primitive-blue-1000);
  --bg:var(--color-neutral-white);--chip:var(--color-neutral-solid-gray-50);
  --tint:var(--color-primitive-blue-50);--dark:var(--color-neutral-solid-gray-900)}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--color-neutral-solid-gray-700);font-family:-apple-system,'Hiragino Kaku Gothic ProN',
  Meiryo,'Noto Sans JP',sans-serif;color:var(--ink);padding:24px;display:flex;flex-direction:column;
  align-items:center;gap:24px;word-break:auto-phrase;line-break:strict}}
.slide{{width:var(--w);height:var(--h);background:var(--bg);position:relative;
  padding:56px 64px;overflow:hidden;box-shadow:0 6px 24px rgba(0,0,0,.35);
  page-break-after:always}}
.kicker{{color:var(--accent);font-weight:800;font-size:18px;line-height:22px;letter-spacing:.06em}}
.slide-title{{font-size:32px;font-weight:800;line-height:1.3;margin:6px 0 12px;color:var(--accent2);
  text-spacing-trim:trim-start}}
.lead{{font-size:20px;line-height:1.6;color:var(--sub)}}
.foot{{position:absolute;left:64px;right:132px;top:674px;height:20px;font-size:14px;
  line-height:20px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.sno{{position:absolute;right:28px;top:674px;font-size:14px;line-height:20px;font-weight:700;
  color:var(--mut)}}
.c1,.c2,.c3,.c4,.c5{{display:-webkit-box;-webkit-box-orient:vertical;overflow:hidden}}
.c1{{-webkit-line-clamp:1}}.c2{{-webkit-line-clamp:2}}.c3{{-webkit-line-clamp:3}}
.c4{{-webkit-line-clamp:4}}.c5{{-webkit-line-clamp:5}}
.one{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.nw{{white-space:nowrap}}.mut{{color:var(--mut)}}
.pr{{display:inline-block;background:var(--color-primitive-yellow-50);color:var(--color-primitive-yellow-1000);
  border:1px solid var(--color-primitive-yellow-900);border-radius:4px;padding:0 6px;font-size:14px;
  line-height:18px;font-weight:800;margin-left:6px;vertical-align:1px}}
.ph{{background:var(--dark);color:var(--bg);font-size:14px;line-height:20px;display:flex;
  align-items:center;justify-content:center;text-align:center;border-radius:6px}}
.tier{{display:inline-block;border-radius:4px;padding:0 6px;font-size:14px;line-height:20px;
  font-weight:800;white-space:nowrap}}
.t-req{{background:var(--accent);color:var(--bg)}}
.t-maj{{background:var(--tint);color:var(--accent2);border:1px solid var(--accent)}}
.t-case{{background:var(--chip);color:var(--sub);border:1px solid var(--line)}}
.box{{border:1px solid var(--line);border-radius:10px;padding:10px 14px}}
.h{{font-size:16px;line-height:22px;font-weight:800;color:var(--accent2)}}
.sm{{font-size:14px;line-height:20px}}.md{{font-size:16px;line-height:24px}}
.edit-tip{{position:fixed;top:8px;left:8px;background:var(--color-neutral-solid-gray-900);
  color:var(--color-neutral-white);font-size:14px;padding:6px 10px;border-radius:6px;opacity:.78;z-index:9}}
[contenteditable]:hover{{outline:2px dashed var(--line);outline-offset:3px;border-radius:3px}}
[contenteditable]:focus{{outline:2px solid var(--accent);outline-offset:3px;border-radius:3px}}
/* S1 表紙 */
.cover-h{{display:flex;flex-direction:column;justify-content:center;height:100%}}
.cover-h .slide-title{{font-size:44px;margin:10px 0 14px}}
.cover-meta{{display:grid;grid-template-columns:max-content 1fr;gap:4px 18px;margin-top:22px;
  font-size:18px;line-height:26px;color:var(--sub)}}
.cover-meta dt{{color:var(--mut);font-weight:700}}
/* S2 結論 */
.tiers{{display:flex;flex-direction:column;gap:8px;margin-top:4px}}
.tierrow{{display:flex;gap:12px;align-items:flex-start}}
.tierrow .tname{{flex:none;width:118px;padding-top:6px}}
.chips{{display:flex;flex-wrap:wrap;gap:8px;max-height:84px;overflow:hidden}}
.chip{{background:var(--chip);border:1px solid var(--line);border-radius:999px;padding:6px 14px;
  font-size:16px;line-height:22px;display:flex;align-items:center;gap:8px;max-width:100%}}
.chip b{{font-weight:800}}.chip i{{color:var(--mut);font-style:normal;font-size:14px;white-space:nowrap}}
.pair{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:14px}}
.pitch{{margin-top:12px;background:var(--tint);border-left:5px solid var(--accent);
  padding:10px 16px;font-size:18px;line-height:28px;border-radius:0 8px 8px 0}}
.note{{margin-top:10px;font-size:14px;line-height:20px;color:var(--mut)}}
.warn{{margin-top:10px;background:var(--color-primitive-yellow-50);
  border:1px solid var(--color-primitive-yellow-900);border-radius:8px;
  padding:8px 14px;font-size:16px;line-height:24px;color:var(--color-primitive-yellow-1000)}}
/* S3 地図・S4 ブランド */
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:12px 20px}}
.grid2 .box .h{{margin-bottom:4px}}
.years{{display:flex;align-items:flex-end;gap:10px;height:62px;margin-top:4px}}
.years div{{display:flex;flex-direction:column;align-items:center;justify-content:flex-end;
  font-size:14px;line-height:18px}}
.years i{{display:block;width:26px;background:var(--accent);border-radius:3px 3px 0 0}}
table.bt{{width:100%;border-collapse:collapse;font-size:15px;line-height:22px;table-layout:fixed}}
table.bt th{{background:var(--chip);color:var(--sub);font-weight:800;text-align:left;padding:6px 8px;
  border-bottom:1px solid var(--line);font-size:14px}}
table.bt td{{padding:5px 8px;border-bottom:1px solid var(--soft);vertical-align:top}}
table.bt tr.others td{{color:var(--mut)}}
/* S5 比較 */
.cmpg{{display:grid;gap:0;border:1px solid var(--line);border-radius:10px;overflow:hidden}}
.cmpg>div{{padding:2px 6px;font-size:14px;line-height:20px;border-bottom:1px solid var(--soft);
  min-width:0}}
.cmpg .lab{{background:var(--chip);color:var(--sub);font-weight:700}}
.cmpg .hd{{font-weight:800;color:var(--accent2);text-align:center;background:var(--chip)}}
.cmpg .hd.best{{background:var(--accent);color:var(--bg)}}
.cmpg .cv{{display:flex;justify-content:center;padding:4px}}
.cmpg .cv img,.cmpg .cv .ph{{width:62px;height:110px;object-fit:contain;background:var(--dark);
  border-radius:4px}}
.cmpg .mx{{color:var(--accent);font-weight:800}}
.mrow{{display:flex;align-items:center;gap:6px;min-width:0}}
.mrow .mb{{flex:1;min-width:16px}}
.mb{{display:block;height:6px;background:var(--color-neutral-solid-gray-100);border-radius:3px;
  overflow:hidden}}
.mb i{{display:block;height:100%;background:var(--line)}}.mb i.mx{{background:var(--accent)}}
/* S6 構成比較・構成分解のバー */
.legend{{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:14px;line-height:20px;color:var(--sub)}}
.legend .sw{{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:4px;
  vertical-align:-1px}}
.srow{{display:grid;grid-template-columns:110px 1fr;gap:10px;align-items:end}}
.srow .rl{{font-size:14px;line-height:18px;font-weight:700}}
.track{{position:relative}}
.marks{{position:relative;height:18px}}
.marks span{{position:absolute;bottom:0;transform:translateX(-50%);font-size:14px;line-height:18px;
  font-weight:800;color:var(--accent2);white-space:nowrap}}
.rbar{{position:relative;background:var(--chip);border-radius:4px;overflow:hidden}}
.rbar .seg{{position:absolute;top:0;bottom:0;border-right:2px solid var(--bg);color:var(--bg);
  font-size:14px;line-height:28px;font-weight:700;text-align:center;overflow:hidden;white-space:nowrap}}
.ticks{{position:relative;height:18px}}
.ticks span{{position:absolute;top:0;transform:translateX(-50%);font-size:14px;line-height:18px;
  color:var(--mut);white-space:nowrap}}
.ticks span.t0{{transform:none}}.ticks span.tend{{transform:translateX(-100%)}}
/* S7〜 構成分解 */
.bd .krow{{position:absolute;left:64px;right:64px;top:56px;height:22px;display:flex;
  justify-content:space-between;align-items:center;gap:16px}}
.bd .krow .kicker{{flex:none}}
.bd .kmeta{{font-size:16px;line-height:22px;color:var(--sub);min-width:0;display:flex;
  align-items:center}}
.bd .ttl{{position:absolute;left:64px;right:64px;top:84px;font-size:30px;line-height:36px;
  font-weight:800;color:var(--accent2)}}
.bd .open{{position:absolute;left:64px;top:134px;width:190px}}
.bd .open img,.bd .open .ph{{display:block;width:150px;height:243px;object-fit:contain;
  background:var(--dark);border-radius:6px}}
.bd .cap{{font-size:14px;line-height:20px;color:var(--mut);margin-top:4px}}
.bd .hook3{{position:absolute;left:64px;top:411px;width:190px;padding:8px 10px}}
.bd .hook3 p{{font-size:14px;line-height:20px;margin-top:2px}}
.bd .bar{{position:absolute;left:278px;right:64px;top:134px}}
.bd .bar .rbar{{height:28px}}
.bd .frames{{position:absolute;left:278px;right:64px;top:206px;display:flex;gap:19px}}
.bd .frames figure{{width:140px}}
.bd .frames img,.bd .frames .ph{{display:block;width:140px;height:249px;object-fit:contain;
  background:var(--dark);border-radius:6px}}
.bd .frames figcaption{{font-size:14px;line-height:20px;margin-top:2px}}
.bd .facts{{position:absolute;left:278px;right:64px;top:485px;display:grid;
  grid-template-columns:repeat(3,minmax(0,1fr));gap:6px 16px}}
.bd .facts .fl{{font-size:14px;line-height:18px;font-weight:800;color:var(--accent2)}}
.bd .facts p{{font-size:14px;line-height:21px}}
.bd .grades{{position:absolute;left:278px;right:64px;top:640px;font-size:14px;line-height:20px;
  color:var(--sub)}}
.bd.land .open{{width:214px}}
.bd.land .open img,.bd.land .open .ph{{width:210px;height:118px}}
.bd.land .hook3{{top:290px;width:214px}}
.bd.land .bar,.bd.land .frames,.bd.land .facts,.bd.land .grades{{left:302px}}
.bd.land .frames{{gap:18px}}
.bd.land .frames figure{{width:214px}}
.bd.land .frames img,.bd.land .frames .ph{{width:214px;height:120px}}
.bd.land .facts{{top:362px;gap:10px 16px}}
/* 共通する構成の型 */
.band{{font-size:15px;line-height:22px;color:var(--sub)}}
.stages{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:8px}}
.stage{{border:1px solid var(--line);border-radius:10px;padding:8px 10px;min-width:0}}
.stage .h{{border-bottom:1px solid var(--soft);padding-bottom:2px;margin-bottom:4px}}
.stage p{{font-size:14px;line-height:20px;margin-top:2px}}
.stage .lbl{{font-size:14px;line-height:20px;font-weight:800;color:var(--sub);margin-top:4px}}
.ex{{display:flex;gap:8px;margin-top:4px;align-items:flex-start}}
.ex img,.ex .ph{{flex:none;width:40px;height:71px;object-fit:contain;background:var(--dark);
  border-radius:4px}}
.ex.land img,.ex.land .ph{{width:96px;height:54px}}
.tplfoot{{margin-top:8px}}
table.kw{{width:100%;border-collapse:collapse;font-size:14px;line-height:20px;table-layout:fixed}}
table.kw th,table.kw td{{padding:2px 6px;border-bottom:1px solid var(--soft);text-align:left;
  vertical-align:top}}
table.kw th{{background:var(--chip);font-weight:800;color:var(--sub)}}
/* 指示・やらないこと */
.dircols{{display:grid;grid-template-columns:minmax(0,2.1fr) minmax(0,1fr);gap:20px}}
.dgrid{{display:grid;grid-template-columns:minmax(0,1fr);gap:4px 14px}}
.dgrid.two{{grid-template-columns:repeat(2,minmax(0,1fr))}}
.drow{{display:flex;gap:10px;padding:6px 0;border-bottom:1px solid var(--soft)}}
.drow img,.drow .ph{{flex:none;width:40px;height:71px;object-fit:contain;background:var(--dark);
  border-radius:4px}}
.drow .ph{{font-size:14px}}
.drow .dt{{font-size:16px;line-height:22px;font-weight:700}}
.drow .ev{{font-size:14px;line-height:20px;color:var(--sub)}}
.avoid{{background:var(--chip);border-radius:10px;padding:10px 14px}}
.avoid li{{list-style:none;padding:6px 0;border-bottom:1px solid var(--soft)}}
.avoid .at{{font-size:16px;line-height:23px;font-weight:700}}
.avoid .ev{{font-size:14px;line-height:20px;color:var(--sub)}}
/* 絵コンテ */
table.sb{{width:100%;border-collapse:collapse;table-layout:fixed;font-size:14px;line-height:20px}}
table.sb th{{background:var(--chip);color:var(--sub);font-weight:800;text-align:left;padding:4px 8px;
  border-bottom:1px solid var(--line)}}
table.sb td{{padding:4px 8px;border-bottom:1px solid var(--soft);vertical-align:top}}
table.sb .ref{{display:flex;gap:6px}}
table.sb .ref img,table.sb .ref .ph{{flex:none;width:36px;height:64px;object-fit:contain;
  background:var(--dark);border-radius:3px}}
table.sb td.sbt{{font-size:15px;line-height:21px;font-weight:700}}
/* 投稿設計と検証 */
.cols3{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}}
.cols3 .box p{{font-size:15px;line-height:22px;margin-top:4px}}
.order{{display:grid;grid-template-columns:max-content 1fr;gap:10px 10px;margin-top:6px;font-size:15px;
  line-height:22px}}
.order dt{{font-weight:700;color:var(--sub)}}
.order dd{{border-bottom:1px solid var(--line);min-height:22px}}
@media print{{body{{background:var(--color-neutral-white);padding:0;gap:0}}.slide{{box-shadow:none}}
  .edit-tip{{display:none}}}}
@page{{size:{SLIDE_W}px {SLIDE_H}px;margin:0}}
"""
)

_EDIT_TIP = (
    '<div class="edit-tip" data-noexport>✎ 文字をクリックして直接編集できます'
    "（保存はブラウザの印刷→PDF / または担当AIに「ここ直して」）</div>"
)


def _esc(s: object) -> str:
    return html.escape(str(s if s is not None else ""))


# ── 共有する事実 ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Deck:
    """スライド全体が使う事実（synthesis の入力・検査と同じ SynthesisContext を使う）。"""

    out: VideoAlgorithmOutput
    ctx: SynthesisContext
    syn: CrossSynthesis | None  # v3 の検査を通したものだけ（旧キャッシュは None）
    stamp: str
    roster: Roster

    @property
    def n(self) -> int:
        return self.ctx.n

    def video(self, rank: int) -> AnalyzedVideo | None:
        return next((v for v in self.ctx.videos if v.meta.rank == rank), None)

    def note(self, rank: int) -> PerVideoNote | None:
        if self.syn is None:
            return None
        return next((p for p in self.syn.per_video if p.rank == rank), None)

    def frames(self, rank: int) -> list[FrameShot]:
        v = self.video(rank)
        return [f for f in (v.frames if v else []) if f.data_uri.startswith("data:image/")]


def build_deck(out: VideoAlgorithmOutput, *, generated_at: str = "") -> Deck:
    roster = Roster.of(out.client_name, out.competitors)
    ctx = SynthesisContext.build(
        out.videos, out.query, board=out.board, roster=roster, avoid_terms=out.avoid_terms
    )
    syn = out.cross.synthesis
    checked = syn if syn is not None and syn.version == SYNTHESIS_V3 else None
    return Deck(out=out, ctx=ctx, syn=checked, stamp=fmt_stamp(generated_at), roster=roster)


def footer_text(d: Deck) -> str:
    """全スライドのフッタ（観測の仮説・相関≠因果・取得時点・秒は AI 推定・タイアップ）。"""
    head = f"上位{d.n}本の観測にもとづく仮説" if d.n else "検索上位の観測にもとづく仮説"
    when = f"順位は{d.stamp}時点" if d.stamp else "順位は取得時点"
    parts = [head, "相関は因果ではない", when, "秒はAI推定（±2秒）"]
    pr = [f.rank for f in d.ctx.facts if f.pr]
    if pr:
        parts.append(f"上位にタイアップ表記{len(pr)}本（{ranks_text(pr)}）")
    return "・".join(parts)


# ── 小さな部品 ─────────────────────────────────────────────────────────


def _img(frame: FrameShot | None, alt: str, *, empty: str = "コマなし") -> str:
    """コマの画像（data URI だけ）。無ければ黒い枠に empty の文字（小さい枠は「—」）。"""
    if frame is None or not frame.data_uri.startswith("data:image/"):
        return f'<div class="ph">{_esc(empty)}</div>'
    return f'<img src="{_esc(frame.data_uri)}" alt="{_esc(alt)}">'


def _uri_img(uri: str, alt: str) -> str:
    if not uri.startswith("data:image/"):
        return '<div class="ph">画像なし</div>'
    return f'<img src="{_esc(uri)}" alt="{_esc(alt)}">'


def _mmss(sec: float, *, tenths: bool = False) -> str:
    sec = max(0.0, sec)
    minutes, rest = divmod(sec, 60)
    return f"{int(minutes)}:{rest:04.1f}" if tenths else f"{int(minutes)}:{int(rest):02d}"


def _span(first: float | None, last: float | None) -> str:
    if first is None:
        return "秒不明"
    if last is None or last == first:
        return f"{first:g}秒"
    return f"{first:g}〜{last:g}秒"


def _tier_badge(ranks: Iterable[int], n: int) -> str:
    text = tier_text(ranks, n)
    name = text.split(" ", 1)[0]
    cls = {TIER_REQUIRED: "t-req", TIER_MAJORITY: "t-maj"}.get(name, "t-case")
    return f'<span class="tier {cls}">{_esc(text)}</span>'


def _alias(entry: str) -> str:
    """「S&B|エスビー食品」→「S&B（エスビー食品）」。"""
    names = [x.strip() for x in entry.split("|") if x.strip()]
    if len(names) <= 1:
        return names[0] if names else ""
    return f"{names[0]}（{'・'.join(names[1:])}）"


def _hook(hook_type: str) -> str:
    return HOOK_LABEL.get(hook_type, HOOK_LABEL["other"])


def _role(role: str | None) -> str:
    return ROLE_LABEL.get(role or "other", ROLE_LABEL["other"])


def _pr_badge(f: VideoFacts) -> str:
    return '<span class="pr" title="タイアップ表記">PR</span>' if f.pr else ""


def _slide(no: int, total: int, kind: str, inner: str, footer: str, *, cls: str = "") -> str:
    return (
        f'<section class="slide {cls}" data-slide="{kind}" data-no="{no} / {total}">{inner}'
        f'<div class="foot" data-foot>{_esc(footer)}</div>'
        f'<div class="sno" data-foot>{no} / {total}</div></section>'
    )


# ── S1 表紙 ─────────────────────────────────────────────────────────────


def _cover(d: Deck) -> str:
    out = d.out
    n = d.n
    title = f"「{out.query}」検索上位{n}本の構成分析" if n else f"「{out.query}」検索上位の構成分析"
    client = _alias(out.client_name or "")
    rows: list[tuple[str, str]] = [
        ("媒体", "TikTok"),
        ("取得日時", d.stamp or "不明（取得日時の記録が無いデータ）"),
        ("対象", f"深掘り{n}本／一覧{len(out.board)}本"),
        ("クライアント", client or "未指定（自社/競合の区分なし）"),
    ]
    if out.competitors:
        rows.append(("競合", "、".join(_alias(c) for c in out.competitors)))
    if out.avoid_terms:
        rows.append(("避けたい訴求", "、".join(out.avoid_terms)))
    meta = "".join(
        f'<dt>{_esc(k)}</dt><dd class="c2" contenteditable>{_esc(v)}</dd>' for k, v in rows
    )
    return (
        '<div class="cover-h">'
        '<div class="kicker">VSEO 動画アルゴリズム分析</div>'
        f'<h1 class="slide-title c2" contenteditable>{_esc(title)}</h1>'
        '<div class="lead c2" contenteditable>上位動画に共通する特徴（仮説）と、1本ずつの構成を'
        "整理しました</div>"
        f'<dl class="cover-meta">{meta}</dl>'
        "</div>"
    )


# ── S2 結論 ─────────────────────────────────────────────────────────────


def ordered_features(features: Sequence[Feature], want: str) -> list[Feature]:
    out: list[Feature] = []
    for prefix in _CHIP_ORDER:
        for f in features:
            if f.tier != want or f in out:
                continue
            if f.id == prefix or (prefix.endswith(":") and f.id.startswith(prefix)):
                out.append(f)
    return out


def _chip(f: Feature, *, required: bool) -> str:
    who = "" if required else f"（{ranks_text(f.ranks)}）"
    rate = (
        f"・上位{f.board_rate[1]}本では{f.board_rate[0]}/{f.board_rate[1]}" if f.board_rate else ""
    )
    return (
        f'<span class="chip"><b class="one">{_esc(f.label)}</b>'
        f"<i>{f.count}/{f.n}{_esc(who)}{_esc(rate)}</i></span>"
    )


def type_line(d: Deck) -> tuple[str, bool]:
    """結論の見出し（v3 の検査済みの文・無ければコードが必須条件と多数派から作った文）。"""
    sl = d.syn.summary_lines if d.syn is not None else None
    if sl is not None and sl.type_line:
        return sl.type_line, sl.type_line_by_code
    alt, _ids = alt_type_line(d.ctx)
    return (alt or f"上位{d.n}本の共通点（仮説）"), True


def _brand_status(d: Deck) -> tuple[str, str]:
    facts = d.ctx.facts
    if d.roster.specified:
        client = [(f.rank, b) for f in facts for b in f.brands if b.relation == "client"]
        comp = [(f.rank, b) for f in facts for b in f.brands if b.relation == "competitor"]
        name = d.ctx.client_label
        mine = (
            "・".join(f"#{r} {b.prominence_label}" for r, b in client)
            if client
            else f"上位{d.n}本には映らない"
        )
        rivals = "、".join(
            f"{b.name} #{r}（{b.prominence_label}{'・PR' if b.sponsored else ''}）" for r, b in comp
        )
        return "ブランドの現在地", f"{name}: {mine}／競合: {rivals or '映らない'}"
    shown = [
        f"{b.name} #{f.rank}{'（PR）' if b.sponsored else ''}"
        for f in facts
        for b in f.brands
        if b.prominent and b.name != _PLACEHOLDER_LOGO
    ]
    text = "・".join(shown) if shown else "主役・目立つ大きさで映るブランドは無い"
    return "ブランドの現在地（区分は未指定）", f"目立って映る: {text}"


def _best_text(d: Deck) -> tuple[str, str]:
    f = d.ctx.fact(d.ctx.best_rank)
    if f is None:
        return "", ""
    why = f"（{'・'.join(d.ctx.best_metrics)}が{d.n}本で最大）" if d.ctx.best_metrics else ""
    head = f"#{f.rank} @{f.author or '不明'}{why}"
    facts = f"{fmt_count(f.plays)}再生・保存率{f.save_rate:.2f}%・シェア{f.shares:,}"
    sl = d.syn.summary_lines if d.syn is not None else None
    reason = sl.best_reason if sl is not None else ""
    return head, f"{facts}。{reason}" if reason else facts


def _conclusion(d: Deck) -> str:
    n = d.n
    if n == 0:
        return ""
    title, by_code = type_line(d)
    req = ordered_features(d.ctx.features, TIER_REQUIRED)[:_MAX_CHIPS]
    maj = ordered_features(d.ctx.features, TIER_MAJORITY)[:_MAX_CHIPS]
    rows = ""
    if req:
        rows += (
            '<div class="tierrow"><span class="tname"><span class="tier t-req">必須条件</span>'
            f'<div class="sm mut">{n}本すべて</div></span><div class="chips">'
            + "".join(_chip(f, required=True) for f in req)
            + "</div></div>"
        )
    if maj:
        rows += (
            '<div class="tierrow"><span class="tname"><span class="tier t-maj">多数派</span>'
            f'<div class="sm mut">{majority_min(n)}本以上</div></span><div class="chips">'
            + "".join(_chip(f, required=False) for f in maj)
            + "</div></div>"
        )
    tiers = f'<div class="tiers">{rows}</div>' if rows else ""
    best_head, best_body = _best_text(d)
    brand_head, brand_body = _brand_status(d)
    pair = (
        '<div class="pair">'
        f'<div class="box"><div class="h">最も見られ保存された1本</div>'
        f'<div class="md one">{_esc(best_head)}</div>'
        f'<div class="md c2" contenteditable>{_esc(best_body)}</div></div>'
        f'<div class="box"><div class="h">{_esc(brand_head)}</div>'
        f'<div class="md c3" contenteditable>{_esc(brand_body)}</div></div>'
        "</div>"
    )
    sl = d.syn.summary_lines if d.syn is not None else None
    pitch = (
        f'<div class="pitch c2"><b>次の一手（案）</b>　<span contenteditable>'
        f"{_esc(sl.client_move)}</span></div>"
        if sl is not None and sl.client_move
        else ""
    )
    board = len(d.out.board)
    rest = f"{n + 1}〜{board}位は動画を未分析" if board > n else f"上位{n}本だけの観測"
    note = (
        f'<div class="note">差の要因: 未特定（{_esc(rest)}）'
        + ("・見出しはコードの集計から作成" if by_code else "")
        + "</div>"
    )
    warn = (
        f'<div class="warn">分析できたのは{n}本（極小サンプル）。断定でなく観測仮説として、'
        "テスト投稿での検証を前提にお読みください。</div>"
        if n < 3
        else ""
    )
    return (
        f'<div class="kicker">結論（上位{n}本の観測・仮説）</div>'
        f'<h2 class="slide-title c2" contenteditable>{_esc(title)}</h2>'
        f"{tiers}{pair}{pitch}{note}{warn}"
    )


# ── S3 検索面の地図 ──────────────────────────────────────────────────────


def _surface(d: Deck) -> str:
    board = d.out.board
    if not board:
        return ""
    sm = surface_map(board, query=d.out.query)
    size = sm.size
    creators = "<br>".join(
        _esc(f"@{a} {len(r)}本（{ranks_text(r)}）") for a, r in sm.creators[:4]
    ) or _esc("2本以上の作り手はいない")
    angles_src = d.syn.board_angles if d.syn is not None else []
    angles = "<br>".join(
        _esc(f"{b.label}「{'・'.join(b.match_terms)}」 {len(b.ranks)}本（{ranks_text(b.ranks)}）")
        for b in angles_src[:4]
    ) or _esc("—（AI の切り口の語が無い）")
    pr = (
        f"{len(sm.pr_ranks)}/{size}本（{ranks_text(sm.pr_ranks)}）"
        if sm.pr_ranks
        else f"0/{size}本"
    )
    save = "—"
    if sm.median_save_rate is not None:
        top = "・".join(f"#{r} {v:.2f}%" for r, v in sm.top_save)
        save = f"中央値{sm.median_save_rate:.2f}%／高い3本 {top}"
    peak = max((c for _y, c in sm.years), default=0)
    years = "".join(
        f'<div><span>{c}本</span><i style="height:{max(4, round(c / peak * 36))}px"></i>'
        f"<span>{y}</span></div>"
        for y, c in sm.years[-8:]
    )
    kw = "<br>".join(
        _esc(
            f"「{t}」キャプション {c}/{size}・ハッシュタグ "
            f"{next((h for tt, lay, h, _s in sm.kw_rates if tt == t and lay == 'hashtag'), 0)}"
            f"/{size}"
        )
        for t, layer, c, _s in sm.kw_rates
        if layer == "caption"
    )
    cells = [
        ("作り手の集中（2本以上）", f'<div class="sm c4">{creators}</div>'),
        ("切り口の頻度（語は AI・本数はコード）", f'<div class="sm c4">{angles}</div>'),
        ("タイアップ表記", f'<div class="sm c2">{_esc(pr)}</div>'),
        ("保存率", f'<div class="sm c2">{_esc(save)}</div>'),
        ("投稿年の分布", f'<div class="years">{years}</div>'),
        (
            "検索語をキャプション・ハッシュタグに持つ率（前提の水準）",
            f'<div class="sm c3">{kw}</div>',
        ),
    ]
    body = "".join(
        f'<div class="box"><div class="h one">{_esc(h)}</div>{c}</div>' for h, c in cells
    )
    return (
        f'<div class="kicker">検索面の地図（上位{size}本・メタのみ）</div>'
        f'<h2 class="slide-title one" contenteditable>上位{size}本の作り手・切り口・タイアップ</h2>'
        f'<div class="grid2">{body}</div>'
        f'<div class="note">{d.n + 1}位以下は動画を見ていない（キャプション・再生などのメタだけ）。'
        "切り口の本数はキャプションの語でコードが数えた値。</div>"
    )


# ── S4 ブランド露出マップ ────────────────────────────────────────────────


@dataclass
class _BrandRow:
    name: str
    relation: str
    prominence: str
    spans: list[tuple[int, BrandFact]]
    in_telop: bool = False
    in_caption: bool = False
    sponsored: bool = False
    category: bool | None = None

    @property
    def ranks(self) -> list[int]:
        return sorted({r for r, _b in self.spans})

    @property
    def prominent(self) -> bool:
        return self.prominence in ("hero", "prominent")


_PROM_ORDER = {"hero": 0, "prominent": 1, "incidental": 2, "background": 3}
_REL_ORDER = {"client": 0, "competitor": 1}


def _roster_entry(name: str, roster: Roster | None) -> str | None:
    """名簿の 1 件（別名を | で区切った元の文字列）のうち、名前が当たるもの。"""
    if roster is None or not roster.specified:
        return None
    key = norm(name)
    for entry in (roster.client_name or "", *roster.competitors):
        if key and key in {norm(x) for x in entry.split("|") if x.strip()}:
            return entry
    return None


def brand_rows(
    facts: Sequence[VideoFacts], roster: Roster | None = None
) -> tuple[list[_BrandRow], list[_BrandRow]]:
    """ブランドごとの行（主な行・その他の映り込み）。カテゴリが分かれば該当だけを主な行にする。

    名簿の別名（「S&B|エスビー食品」）で当たるブランドは 1 行にまとめる。
    """
    rows: dict[str, _BrandRow] = {}
    for f in facts:
        for b in f.brands:
            entry = _roster_entry(b.name, roster)
            key = f"roster:{norm(entry)}" if entry else norm(b.name)
            row = rows.get(key)
            if row is None:
                name = _alias(entry) if entry else b.name
                row = rows[key] = _BrandRow(name, b.relation, b.prominence, [])
            row.spans.append((f.rank, b))
            if _PROM_ORDER.get(b.prominence, 4) < _PROM_ORDER.get(row.prominence, 4):
                row.prominence = b.prominence
            row.in_telop |= b.in_telop
            row.in_caption |= b.in_caption
            row.sponsored |= b.sponsored
            if b.category_match is not None:
                row.category = bool(row.category) or b.category_match
    known = category_known(facts)
    ordered = sorted(
        rows.values(),
        key=lambda r: (
            _REL_ORDER.get(r.relation, 2),
            _PROM_ORDER.get(r.prominence, 4),
            r.ranks[0] if r.ranks else 99,
        ),
    )
    main = [
        r
        for r in ordered
        if r.name != _PLACEHOLDER_LOGO and (bool(r.category) if known else r.prominent)
    ]
    others = [r for r in ordered if r not in main]
    return main, others


def _brands(d: Deck) -> str:
    main, others = brand_rows(d.ctx.facts, d.roster)
    if not main and not others:
        return ""
    limit = 8
    if len(main) > limit:
        others = main[limit:] + others
        main = main[:limit]
    body = ""
    for r in main:
        secs = "・".join(
            f"#{rank} {_span(b.first_sec, b.last_sec)}（計{b.total_sec:g}秒）"
            for rank, b in r.spans
        )
        mention = "・".join(
            w for w, on in (("テロップ", r.in_telop), ("キャプション", r.in_caption)) if on
        )
        body += (
            "<tr>"
            f'<td class="one"><b>{_esc(r.name)}</b></td>'
            f'<td class="one">{_esc(_relation_label(r.relation))}</td>'
            f'<td class="one">{_esc(ranks_text(r.ranks))}</td>'
            f'<td><div class="c2">{_esc(secs)}</div></td>'
            f'<td class="one">{_esc(PROMINENCE_LABEL.get(r.prominence, "不明"))}</td>'
            f'<td class="one">{_esc(mention or "—")}</td>'
            f'<td class="one">{"PR" if r.sponsored else "—"}</td>'
            "</tr>"
        )
    if others:
        names = "・".join(
            f"{r.name} {ranks_text(r.ranks)}（{PROMINENCE_LABEL.get(r.prominence, '不明')}）"
            for r in others
        )
        body += (
            '<tr class="others"><td class="one">その他の映り込み</td><td>—</td>'
            f'<td class="one">{_esc(ranks_text(rk for r in others for rk in r.ranks))}</td>'
            f'<td colspan="4"><div class="c2">{_esc(names)}</div></td></tr>'
        )
    how = (
        "区分は名簿（クライアント・競合）でコードが決めた値。"
        if d.roster.specified
        else "区分は未指定（クライアント名と競合を入れると区分が付きます）。"
    )
    basis = (
        "商材カテゴリの商品を上に、それ以外は「その他の映り込み」にまとめた"
        if category_known(d.ctx.facts)
        else "カテゴリは未判定のため、主役・目立つ大きさで映るものを上に、付随・背景は「その他」"
    )
    return (
        '<div class="kicker">ブランド露出マップ</div>'
        f'<h2 class="slide-title one" contenteditable>上位{d.n}本に映るブランドと区分</h2>'
        '<table class="bt"><colgroup><col style="width:180px"><col style="width:116px">'
        '<col style="width:120px"><col><col style="width:86px"><col style="width:180px">'
        '<col style="width:86px"></colgroup>'
        "<thead><tr><th>ブランド</th><th>区分</th><th>動画</th><th>映る秒（AI推定）</th>"
        "<th>目立ち方</th><th>名前の言及</th><th>タイアップ</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
        f'<div class="note c2">{_esc(how + basis)}。秒は動画分析 AI の推定で、実際の映像と'
        "1〜2秒ずれることがあります。</div>"
    )


def _relation_label(relation: str) -> str:
    return {
        "client": "クライアント",
        "competitor": "競合",
        "other": "その他",
        "unspecified": "未指定",
    }.get(relation, "未指定")


# ── S5 比較 ─────────────────────────────────────────────────────────────


def _rank_pos(values: Sequence[float], value: float) -> int:
    return 1 + sum(1 for v in values if v > value)


def _compare(d: Deck) -> str:
    facts = list(d.ctx.facts)
    n = len(facts)
    if n == 0:
        return ""
    plays = [float(f.plays) for f in facts]
    saves = [f.save_rate for f in facts]
    shares = [float(f.shares) for f in facts]
    cols = f"110px repeat({n},minmax(0,1fr))"
    cells: list[str] = []

    def row(label: str, values: Iterable[str], *, cls: str = "c1") -> None:
        cells.append(f'<div class="lab {cls}">{_esc(label)}</div>')
        cells.extend(f'<div class="{cls}">{v}</div>' for v in values)

    head = ['<div class="lab"></div>']
    for f in facts:
        best = " best" if f.rank == d.ctx.best_rank else ""
        head.append(
            f'<div class="hd one{best}">#{f.rank}{" 最多" if best and n <= 6 else ""}'
            f"{_pr_badge(f)}</div>"
        )
    cells.extend(head)
    covers = [v.cover_data_uri if (v := d.video(f.rank)) else "" for f in facts]
    if any(c.startswith("data:image/") for c in covers):
        cells.append('<div class="lab">表紙</div>')
        cells.extend(
            f'<div class="cv">{_uri_img(c, f"#{f.rank}")}</div>'
            for f, c in zip(facts, covers, strict=True)
        )

    def metric(values: list[float], value: float, text: str) -> str:
        top = max(values) if values else 0.0
        w = 0.0 if top <= 0 else min(100.0, value / top * 100)
        mx = " mx" if top > 0 and value == top else ""
        return (
            f'<span class="mrow"><b class="nw{mx}">{_esc(text)}</b>'
            f'<span class="mb"><i class="{mx.strip()}" style="width:{w:.0f}%"></i></span></span>'
        )

    row("アカウント", (_esc(f"@{f.author}") if f.author else "—" for f in facts))
    row("フォロワー", (_esc(fmt_count(f.followers)) if f.followers else "不明" for f in facts))
    row(
        "投稿日",
        (
            _esc(f.posted_at.isoformat() + ("（換算）" if f.posted_estimated else ""))
            if f.posted_at
            else "不明"
            for f in facts
        ),
    )
    row("再生", (metric(plays, float(f.plays), fmt_count(f.plays)) for f in facts), cls="")
    row("保存率", (metric(saves, f.save_rate, f"{f.save_rate:.2f}%") for f in facts), cls="")
    row("シェア", (metric(shares, float(f.shares), f"{f.shares:,}") for f in facts), cls="")
    row(
        "尺・向き",
        (
            _esc(f"{f.duration_sec:g}秒・{ORIENTATION_LABEL.get(f.orientation, '不明')}")
            for f in facts
        ),
    )
    row(
        "0〜3秒のテロップ",
        (_esc("".join(f"「{t}」" for _s, t in f.opening_telops) or "なし") for f in facts),
        cls="c2",
    )
    row("フックの型", (_esc(_hook(f.hook_type)) for f in facts))
    row("語り", ("あり" if f.narration else "なし" for f in facts))
    row("分量の置き場所", (_esc(f.qty_place) for f in facts))
    row("目立つブランド", (_esc(_top_brand_name(f)) for f in facts))
    row("締め・CTA", (_esc(_cta_short(f)) for f in facts))
    return (
        f'<div class="kicker">上位{n}本の比較</div>'
        f'<h2 class="slide-title one" contenteditable>上位{n}本を同じ項目で並べる</h2>'
        f'<div class="cmpg" style="grid-template-columns:{cols}">{"".join(cells)}</div>'
        f'<div class="note">最多＝再生・保存率・シェアの順で最大の1本（#{d.ctx.best_rank}）。'
        "青字は各行の最大。PR＝キャプションのタイアップ表記・@ブランド・AI判定の提供の可能性。</div>"
    )


def _top_brand_name(f: VideoFacts) -> str:
    named = [b for b in f.brands if b.name != _PLACEHOLDER_LOGO and b.prominent]
    return named[0].name if named else "—"


def _cta_short(f: VideoFacts) -> str:
    if f.cta_in_video is not None:
        kind, _text, sec = f.cta_in_video
        when = f"{sec:g}秒 " if sec is not None else ""
        return f"{when}{CTA_KIND_LABEL.get(kind, kind)}"
    if f.cta_dropped:
        return "なし（型だけの申告は無効）"
    return "なし"


# ── S6 構成比較・構成バー ────────────────────────────────────────────────


def _ordered_scenes(a: VideoVSEOAnalysis) -> list[Scene]:
    return sorted(a.scenes, key=lambda sc: (sc.start_sec, sc.end_sec))


def _axis_len(f: VideoFacts, a: VideoVSEOAnalysis | None) -> float:
    ends = [max(sc.end_sec, sc.start_sec) for sc in (a.scenes if a else [])]
    return max([f.duration_sec, *ends, 1.0])


def _markers(d: Deck, f: VideoFacts) -> list[tuple[float, str]]:
    use_cat = category_known(d.ctx.facts)
    brand = [
        b.first_sec
        for b in f.brands
        if b.prominent and (b.category_match if use_cat else True) and b.first_sec is not None
    ]
    raw: list[tuple[str, float | None]] = [
        ("first_telop", f.first_telop_sec),
        ("kw_telop", f.kw_first_telop_sec),
        ("brand_first", min(brand) if brand else None),
        ("result_first", f.result_first_sec),
        ("cta", f.cta_in_video[2] if f.cta_in_video is not None else None),
    ]
    return [(sec, key) for key, sec in raw if sec is not None]


def _marks_html(marks: list[tuple[float, str]], axis: float, width_px: float) -> str:
    """印（▼＋文字）。近い印（前の印の文字幅より近いもの）は 1 つにまとめ、重ねて描かない。"""
    groups: list[tuple[float, list[str]]] = []
    for sec, key in sorted(marks):
        px = max(0.0, min(1.0, sec / axis)) * width_px
        if groups and px - groups[-1][0] < _MARK_PX_PER_CHAR * (len(groups[-1][1]) + 1) + 6:
            groups[-1][1].append(_MARK_LETTER[key])
        else:
            groups.append((px, [_MARK_LETTER[key]]))
    out = ""
    for px, letters in groups:
        pct = px / width_px * 100 if width_px > 0 else 0.0
        pos = min(97.0, max(1.5, pct))
        out += f'<span style="left:{pos:.1f}%">▼{"".join(dict.fromkeys(letters))}</span>'
    return out


def _role_bar(a: VideoVSEOAnalysis, axis: float, width_px: float, height: int) -> str:
    scenes = _ordered_scenes(a)
    roles = infer_roles(a)
    segs = ""
    for sc, (role, _inferred) in zip(scenes, roles, strict=True):
        start = max(0.0, sc.start_sec)
        end = min(axis, max(sc.end_sec, sc.start_sec))
        if end <= start:
            continue
        left = start / axis * 100
        width = (end - start) / axis * 100
        wide = width / 100 * width_px >= _ROLE_NAME_MIN_PX and height >= _ROLE_NAME_MIN_H
        name = _role(role) if wide else ""
        segs += (
            f'<div class="seg" style="left:{left:.2f}%;width:{width:.2f}%;line-height:{height}px;'
            f'background:{ROLE_COLOR.get(role, ROLE_COLOR["other"])}">{_esc(name)}</div>'
        )
    return f'<div class="rbar" style="height:{height}px">{segs}</div>'


def _ticks(axis: float, step: float) -> str:
    secs: list[float] = []
    s = 0.0
    while s < axis - step * 0.4:
        secs.append(s)
        s += step
    secs.append(axis)
    out = ""
    for i, sec in enumerate(secs):
        cls = "t0" if i == 0 else "tend" if i == len(secs) - 1 else ""
        out += (
            f'<span class="{cls}" style="left:{sec / axis * 100:.2f}%">{sec:g}'
            f"{'秒' if i == len(secs) - 1 else ''}</span>"
        )
    return f'<div class="ticks">{out}</div>'


def _step(axis: float) -> float:
    for s in (5.0, 10.0, 15.0, 30.0, 60.0):
        if axis / s <= 7:
            return s
    return 120.0


def _role_legend(roles: Iterable[str]) -> str:
    return "".join(
        f'<span><span class="sw" style="background:{ROLE_COLOR.get(r, ROLE_COLOR["other"])}"></span>'
        f"{_esc(_role(r))}</span>"
        for r in dict.fromkeys(roles)
    )


def _mark_legend(keys: Iterable[str], *, short: bool = False) -> str:
    names = _MARK_SHORT if short else _MARK_LEGEND
    wanted = set(keys)
    order = [k for k in _MARK_LETTER if k in wanted]
    return "・".join(f"{_MARK_LETTER[k]}={names[k]}" for k in order)


def _structure(d: Deck) -> str:
    facts = list(d.ctx.facts)
    n = len(facts)
    if n == 0:
        return ""
    axis = max(_axis_len(f, d.ctx.analysis(f.rank)) for f in facts)
    track_px = SLIDE_W - 128 - 110 - 10
    avail = CONTENT_BOTTOM - 206 - 22
    row_h = max(40, min(72, avail // n))
    bar_h = max(18, row_h - 26)
    rows = ""
    roles_seen: list[str] = []
    marks_seen: list[str] = []
    inferred = False
    for f in facts:
        a = d.ctx.analysis(f.rank)
        if a is None:
            continue
        roles = infer_roles(a)
        roles_seen += [r for r, _i in roles]
        inferred |= any(i for _r, i in roles)
        marks = _markers(d, f)
        marks_seen += [k for _s, k in marks]
        own_px = max(40.0, track_px * f.duration_sec / axis)
        rows += (
            f'<div class="srow" style="height:{row_h}px">'
            f'<div class="rl">#{f.rank}{_pr_badge(f)}<div class="mut">{f.duration_sec:g}秒</div></div>'
            f'<div class="track" style="width:{f.duration_sec / axis * 100:.2f}%;min-width:40px">'
            f'<div class="marks">{_marks_html(marks, max(f.duration_sec, 1.0), own_px)}</div>'
            f"{_role_bar(a, max(f.duration_sec, 1.0), own_px, bar_h)}"
            "</div></div>"
        )
    legend = _role_legend(r for r in ROLE_LABEL if r in roles_seen)
    how = "役割は推定（最初の場面＝フック、CTA の秒の場面＝CTA、ほかは手順）" if inferred else ""
    axis_row = f'<div class="srow"><div></div><div>{_ticks(axis, _step(axis))}</div></div>'
    return (
        f'<div class="kicker">{n}本の構成比較</div>'
        f'<h2 class="slide-title one" contenteditable>{n}本の構成を同じ秒の物差しで並べる</h2>'
        f'<div class="legend c2">{legend}<span>▼ {_esc(_mark_legend(marks_seen))}</span>'
        f"{f'<span>{_esc(how)}</span>' if how else ''}</div>"
        f'<div style="margin-top:6px">{rows}{axis_row}</div>'
    )


# ── S7〜 構成分解 ────────────────────────────────────────────────────────


def _scene_at(scenes: list[Scene], sec: float) -> int | None:
    if not scenes:
        return None
    for i, sc in enumerate(scenes):
        end = max(sc.end_sec, sc.start_sec)
        if sc.start_sec <= sec < end or (i == len(scenes) - 1 and sec >= sc.start_sec):
            return i
    return min(range(len(scenes)), key=lambda i: abs(scenes[i].start_sec - sec))


def pick_scene_frames(
    a: VideoVSEOAnalysis, frames: Sequence[FrameShot], limit: int, *, exclude: FrameShot | None
) -> list[tuple[FrameShot, str | None]]:
    """場面のコマ（最初・最後・役割が変わる・長い場面の順に選び、時刻の順に並べる）と、その役割。

    コマは既に抜いたものだけを使う（場面の内側にあるコマ・無い場面は飛ばす）。見出しに使うのは
    「秒｜場面の役割」だけ（ブランド名・KW の見出しは付けない。Gemini の秒は 1〜2 秒ずれる）。
    """
    usable = sorted(
        (f for f in frames if f.data_uri.startswith("data:image/") and f is not exclude),
        key=lambda f: f.sec,
    )
    if not usable or limit <= 0:
        return []
    scenes = _ordered_scenes(a)
    roles = [r for r, _i in infer_roles(a)]
    order: list[int] = []
    if scenes:
        order += [0, len(scenes) - 1]
        order += [i for i in range(1, len(scenes)) if roles[i] != roles[i - 1]]
        order += sorted(
            range(len(scenes)), key=lambda i: -(scenes[i].end_sec - scenes[i].start_sec)
        )
    picked: list[FrameShot] = []
    for i in dict.fromkeys(order):
        sc = scenes[i]
        end = max(sc.end_sec, sc.start_sec)
        cands = [f for f in usable if f not in picked and sc.start_sec - 0.5 <= f.sec <= end + 0.5]
        if not cands:
            continue
        mid = (sc.start_sec + end) / 2
        picked.append(min(cands, key=lambda f: (abs(f.sec - mid), f.sec)))
        if len(picked) >= limit:
            break
    for f in usable:
        if len(picked) >= limit:
            break
        if f not in picked:
            picked.append(f)
    picked.sort(key=lambda f: f.sec)
    out: list[tuple[FrameShot, str | None]] = []
    for f in picked:
        at = _scene_at(scenes, f.sec)
        out.append((f, roles[at] if at is not None else None))
    return out


def _telop_design(f: VideoFacts, a: VideoVSEOAnalysis) -> str:
    pos = POSITION_LABEL.get(f.telop_position_major, "不明")
    text = f"{f.telop_count}枚・1秒あたり{f.telops_per_sec:.2f}枚・{pos}"
    if f.qty_telops:
        s, t = f.qty_telops[0]
        text += f"。分量テロップ{len(f.qty_telops)}枚（例: {s:g}秒「{t}」）"
    else:
        text += "。分量テロップなし"
    reasons = sum(1 for t in a.telops if getattr(t, "role", None) == "reason")
    if reasons:
        text += f"。理由のテロップ{reasons}枚"
    return text


def _product(d: Deck, f: VideoFacts, a: VideoVSEOAnalysis) -> str:
    brands = [b for b in f.brands if b.name != _PLACEHOLDER_LOGO]
    if not brands:
        return "商品・ブランドの映り込みなし（AI の検出）"
    use_cat = category_known(d.ctx.facts)
    pick = next(
        (b for b in brands if b.prominent and (b.category_match if use_cat else True)), brands[0]
    )
    head = pick.name
    if pick.relation in ("client", "competitor"):
        head += f"（{pick.relation_label}）"
    parts = [
        head,
        pick.prominence_label or "目立ち方不明",
        f"{_span(pick.first_sec, pick.last_sec)}（AI推定）",
    ]
    telop = next(
        (t for t in sorted(a.telops, key=lambda t: t.sec) if contains(t.text, pick.name)), None
    )
    if telop is not None:
        parts.append(f"テロップ {telop.sec:g}秒「{telop.text.strip()}」")
    if contains(f.desc, f"@{pick.name}"):
        parts.append(f"キャプション @{pick.name}")
    elif pick.in_caption:
        parts.append("キャプションに名前")
    if pick.sponsored:
        parts.append("PR")
    if len(brands) > 1:
        parts.append(f"ほか{len(brands) - 1}件")
    return "・".join(parts)


def _close_cta(f: VideoFacts, a: VideoVSEOAnalysis) -> str:
    parts: list[str] = []
    if f.cta_in_video is not None:
        kind, text, sec = f.cta_in_video
        when = f"{sec:g}秒" if sec is not None else "秒不明"
        quote = f"「{text}」" if text else "（文言なし）"
        parts.append(f"{when}{quote}（{CTA_KIND_LABEL.get(kind, kind)}）")
    else:
        if f.cta_dropped:
            kinds = "・".join(CTA_KIND_LABEL.get(k, k) for k in f.cta_dropped)
            parts.append(f"{kinds}は文言も秒も無いため無効")
        else:
            parts.append("動画内のCTAなし")
        telops = sorted((t for t in a.telops if t.text.strip()), key=lambda t: t.sec)
        if telops:
            last = telops[-1]
            parts.append(f"最後のテロップ {last.sec:g}秒「{last.text.strip()}」")
    if f.cta_in_caption:
        kinds = "・".join(CTA_KIND_LABEL.get(k, k) for k in f.cta_in_caption)
        parts.append(f"キャプションで{kinds}の呼びかけ")
    return "・".join(parts)


def _save_device(f: VideoFacts) -> str:
    video_save = f.cta_in_video is not None and f.cta_in_video[0] == "save"
    caption_save = "save" in f.cta_in_caption
    where = "・".join(
        w for w, on in (("映像内", video_save), ("キャプション内", caption_save)) if on
    )
    return f"分量の置き場所: {f.qty_place}・保存の呼びかけ: {where or 'なし'}"


def _why_top(d: Deck, f: VideoFacts, note: PerVideoNote | None) -> str:
    if note is not None and (note.why_fact or note.why_guess):
        return " ".join(t for t in (note.why_fact, note.why_guess) if t)
    facts = list(d.ctx.facts)
    plays = [float(x.plays) for x in facts]
    saves = [x.save_rate for x in facts]
    shares = [float(x.shares) for x in facts]
    return (
        f"{len(facts)}本中 再生{_rank_pos(plays, float(f.plays))}位・保存率"
        f"{_rank_pos(saves, f.save_rate)}位・シェア{_rank_pos(shares, float(f.shares))}位（事実）"
    )


def _caption_facts(d: Deck, f: VideoFacts) -> str:
    parts: list[str] = []
    for term in query_terms(d.out.query):
        cap = f.has_kw(term, "caption", "exact")
        tag = f.has_kw(term, "hashtag", "exact")
        parts.append(f"「{term}」{'あり' if cap else 'なし'}・#{term} {'あり' if tag else 'なし'}")
    parts.append(f"分量{'あり' if f.qty_in_caption else 'なし'}（{len(f.desc)}字）")
    return "・".join(parts)


def _grades_line(grades: Sequence[Grade], f: VideoFacts) -> str:
    """評価の 1 行。CTA は事実層に合わせる（文言も秒も無い型だけの申告は無効＝△）。"""
    marks = []
    for g in grades:
        mark = g.mark
        if g.axis == "CTA" and f.cta_in_video is None:
            mark = "△"
        marks.append(f"{_AXIS_SHORT.get(g.axis, g.axis)}{mark}")
    return "　".join(marks)


def _breakdown(d: Deck, v: AnalyzedVideo, f: VideoFacts) -> str:
    a = v.analysis
    assert a is not None
    land = f.orientation == "landscape"
    note = d.note(f.rank)
    frames = d.frames(f.rank)
    posted = (
        f"{f.posted_at.isoformat()}{'（換算）' if f.posted_estimated else ''}投稿"
        if f.posted_at
        else "投稿日不明"
    )
    meta = "｜".join(
        [
            f"{fmt_count(f.plays)}再生",
            f"保存率{f.save_rate:.2f}%",
            f"シェア{f.shares:,}",
            f"{f.duration_sec:g}秒・{ORIENTATION_LABEL.get(f.orientation, '不明')}",
            posted,
            f"フォロワー{fmt_count(f.followers)}" if f.followers else "フォロワー不明",
        ]
    )
    flow = [_role(r) for r in dict.fromkeys(r for r, _i in infer_roles(a))][1:]
    fallback = f"{_hook(f.hook_type)}のフックから" + ("→".join(flow) + "へ" if flow else "本編へ")
    title = note.win_line if note is not None and note.win_line else fallback
    opening = ref_frame(frames, 0.8)
    axis = _axis_len(f, a)
    bar_px = SLIDE_W - 64 - (302 if land else 278)
    marks = _markers(d, f)
    inferred = any(i for _r, i in infer_roles(a))
    limit = _FRAMES_LANDSCAPE if land else _FRAMES_PORTRAIT
    scene_frames = pick_scene_frames(a, frames, limit, exclude=opening)
    figs = (
        "".join(
            f"<figure>{_img(fr, f'{_mmss(fr.sec)} {_role(role)}')}"
            f'<figcaption class="one">{_esc(_mmss(fr.sec))}｜{_esc(_role(role))}</figcaption></figure>'
            for fr, role in scene_frames
        )
        or '<div class="sm mut">抜いたコマが無い</div>'
    )
    opening_lines = (
        "".join(f'<p class="c2">{s:g}秒「{_esc(t)}」</p>' for s, t in f.opening_telops[:3])
        or '<p class="c1">0〜3秒のテロップなし</p>'
    )
    spoken = [h for h in f.kw if h.layer == "speech"]
    spoken_text = (
        "声に出た検索語: "
        + "・".join(
            f"{h.term}" + (f" {'・'.join(f'{s:g}' for s in h.secs)}秒" if h.secs else "")
            for h in spoken
        )
        + "（AI聞き取り）"
        if spoken
        else "検索語の発話なし（AI聞き取り）"
    )
    hook_box = (
        '<div class="hook3 box"><div class="h">冒頭3秒</div>'
        f"{opening_lines}"
        f'<p class="c1">フック: {_esc(_hook(f.hook_type))}</p>'
        f'<p class="c1">{"語りあり" if f.narration else "語りなし"}</p>'
        f'<p class="c2">{_esc(spoken_text)}</p></div>'
    )
    steal = note.steal if note is not None else []
    facts_cells: list[tuple[str, str, str]] = [
        ("テロップ設計", _telop_design(f, a), "c2"),
        ("商品の見せ方", _product(d, f, a), "c2"),
        ("締め・CTA", _close_cta(f, a), "c2"),
        ("保存の仕掛け", _save_device(f), "c2"),
        (
            "なぜ上位か" + ("（事実｜推測）" if note is not None and note.why_guess else ""),
            _why_top(d, f, note),
            "c2",
        ),
        (
            ("盗める点", "　".join(f"{'①②'[i]}{t}" for i, t in enumerate(steal[:2])), "c2")
            if steal
            else ("キャプション", _caption_facts(d, f), "c2")
        ),
    ]
    if land:
        facts_cells = [(k, t, "c3") for k, t, _c in facts_cells]
    cells = "".join(
        f'<div><div class="fl">{_esc(k)}</div><p class="{c}" contenteditable>{_esc(t)}</p></div>'
        for k, t, c in facts_cells
    )
    grades = grade_video(v, query=d.out.query, roster=d.roster)
    legend = _mark_legend((k for _s, k in marks), short=True)
    grade_line = f"評価（コードの基準）: {_grades_line(grades, f)}" + (
        f"　▼ {legend}" if legend else ""
    )
    return (
        '<div class="krow">'
        f'<div class="kicker">構成分解 #{f.rank} / {d.n}</div>'
        f'<div class="kmeta"><span class="one">{_esc(meta)}</span>{_pr_badge(f)}</div></div>'
        f'<h2 class="ttl one" contenteditable>{_esc(title)}</h2>'
        f'<div class="open">{_img(opening, "冒頭のコマ")}'
        f'<div class="cap one">{_esc(_mmss(opening.sec, tenths=True)) if opening else "—"}'
        " 冒頭のコマ</div></div>"
        f"{hook_box}"
        '<div class="bar">'
        f'<div class="marks">{_marks_html(marks, axis, bar_px)}</div>'
        f"{_role_bar(a, axis, bar_px, 28)}"
        f"{_ticks(axis, 15.0)}"
        "</div>"
        f'<div class="frames">{figs}</div>'
        f'<div class="facts">{cells}</div>'
        f'<div class="grades one">{_esc(grade_line)}'
        f"{'　（役割は推定）' if inferred else ''}</div>"
    )


# ── 共通する構成の型 ─────────────────────────────────────────────────────


def _example(d: Deck, rank: int, sec: float, text: str, *, frame: bool) -> str:
    f = d.ctx.fact(rank)
    land = f is not None and f.orientation == "landscape"
    shot = _img(ref_frame(d.frames(rank), sec), f"#{rank} {sec:g}秒", empty="—") if frame else ""
    return (
        f'<div class="ex{" land" if land and frame else ""}">{shot}'
        f'<p class="{"c4" if frame else "c1"}">#{rank} {sec:g}秒「{_esc(text)}」</p></div>'
    )


def _stage_extra(d: Deck, row: StageRow) -> str:
    """段に足す 1 行（冒頭＝フックの型の割れ方・本編＝分量の置き場所の残り）。"""
    if row.index == 0:
        counts: dict[str, int] = {}
        for f in d.ctx.facts:
            counts[_hook(f.hook_type)] = counts.get(_hook(f.hook_type), 0) + 1
        split = "・".join(f"{k}{c}" for k, c in sorted(counts.items(), key=lambda kv: -kv[1]))
        return f"フックの型: {split}"
    if row.index == 2:
        cap_only = [f.rank for f in d.ctx.facts if f.qty_in_caption and not f.qty_telops]
        none = [f.rank for f in d.ctx.facts if not f.qty_anywhere]
        bits = []
        if cap_only:
            bits.append(f"分量はキャプションだけ {ranks_text(cap_only)}")
        if none:
            bits.append(f"分量なし {ranks_text(none)}")
        return "・".join(bits)
    return ""


def _stage_col(d: Deck, row: StageRow, *, last: bool) -> str:
    n = d.n
    roles = "".join(
        f'<p class="c1">{_esc(_role(role))} {_esc(tier_text(ranks, n))}</p>'
        for role, ranks, _t in row.roles[:2]
    )
    head_note = ""
    if row.roles_inferred:
        head_note = "（推定・v3で計測）" if last else "（役割は推定）"
    use_cat = category_known(d.ctx.facts)
    events = ""
    # 本数の多い出来事から 2 つ（段の順は EVENT_LABEL の順）。
    for key, ranks, _t in sorted(row.events, key=lambda e: -len(e[1]))[:_STAGE_EVENTS]:
        label = EVENT_LABEL[key]
        if key == "brand_first" and not use_cat:
            label += "（カテゴリ未判定）"
        events += f'<p class="c2">{_esc(label)} {_esc(tier_text(ranks, n))}</p>'
    extra = _stage_extra(d, row)
    extra_html = f'<p class="c2">{_esc(extra)}</p>' if extra else ""
    ex = row.examples
    examples = _example(d, *ex[0], frame=True) if ex else ""
    return (
        f'<div class="stage"><div class="h one">{_esc(row.label)}'
        f'<span class="sm mut">{_esc(head_note)}</span></div>'
        f'<div class="lbl">主な役割</div>{roles or "<p>—</p>"}'
        f'<div class="lbl">出来事</div>{events or "<p>—</p>"}{extra_html}'
        f'<div class="lbl">代表例（再生が最多の動画）</div>{examples or "<p>—</p>"}</div>'
    )


def _kw_cell(r: KwRow) -> str:
    text = f"{len(r.exact)}/{r.n}"
    if r.layer == "telop" and r.synonym:
        text = f"完全{len(r.exact)}・言い換え{len(r.synonym)}（{ranks_text(r.synonym)}）"
    if r.board:
        text += f"（上位{r.board[1]}本で{r.board[0]}）"
    return text


def _template(d: Deck) -> str:
    n = d.n
    if n == 0:
        return ""
    rows = template(list(d.ctx.videos), list(d.ctx.facts))
    if not rows:
        return ""
    b = d.ctx.band
    band: list[str] = []
    if b.duration:
        dur = b.duration
        band.append(f"尺 中央値{dur.median:g}秒（{dur.min:g}〜{dur.max:g}秒・固定しない）")
    if b.telops_per_sec:
        t = b.telops_per_sec
        band.append(f"1秒あたりのテロップ 中央値{t.median:.2f}枚（{t.min:.2f}〜{t.max:.2f}）")
    band.append(
        f"語りあり {tier_text(b.narration_ranks, n)}" if b.narration_ranks else "語りあり 0本"
    )
    outs = "／".join(f"#{r} {'・'.join(why)}" for r, why in d.ctx.outliers) or "なし"
    cols = "".join(_stage_col(d, r, last=r.index == len(rows) - 1) for r in rows)
    kw_rows = kw_matrix(d.ctx.facts, d.ctx.board, d.out.query)
    layers = ("telop", "caption", "hashtag", "speech")
    head = "".join(
        f"<th>{h}</th>"
        for h in ("語", "テロップ", "キャプション", "ハッシュタグ", "発話（AI聞き取り）")
    )
    body = ""
    for term in query_terms(d.out.query)[:_KW_TABLE_TERMS]:
        cells = "".join(
            f'<td class="one">{_esc(_kw_cell(r))}</td>'
            for layer in layers
            for r in kw_rows
            if r.term == term and r.layer == layer
        )
        body += f'<tr><td class="one"><b>{_esc(term)}</b></td>{cells}</tr>'
    return (
        '<div class="kicker">共通する構成の型（秒つき）</div>'
        f'<h2 class="slide-title one" contenteditable>上位{n}本の段ごとの観測と代表例</h2>'
        f'<div class="band one">{_esc("｜".join(band))}</div>'
        f'<div class="band one">外れ値（事実）: {_esc(outs)}</div>'
        f'<div class="stages">{cols}</div>'
        '<div class="tplfoot"><div class="h one">拾われる条件（語×層・テロップは本文に実在する'
        "ものだけ・キャプションとハッシュタグは上位ボード全体の本数を併記）</div>"
        f'<table class="kw"><colgroup><col style="width:130px"></colgroup><thead><tr>{head}</tr>'
        f"</thead><tbody>{body}</tbody></table></div>"
    )


# ── クリエイティブ指示／やらないこと ────────────────────────────────────────


def _ref_frame(d: Deck, ref: SynthRef) -> FrameShot | None:
    if ref.source == "caption":
        return None
    sec = ref.found_sec if ref.found_sec is not None else ref.sec
    return ref_frame(d.frames(ref.rank), sec)


def _evidence(refs: Sequence[SynthRef]) -> str:
    return "／".join(evidence_text(r) for r in refs[:2])


def _directives(d: Deck) -> str:
    if d.n == 0:
        return ""
    dirs = list(d.syn.directives) if d.syn is not None else code_directives(d.ctx)
    avoid = list(d.syn.avoid) if d.syn is not None else []
    if not dirs and not avoid:
        return ""
    rows = ""
    for item in dirs[:6]:
        frame = _ref_frame(d, item.refs[0]) if item.refs else None
        who = "コードの集計" if item.origin == "code" else "AI の指示（根拠は照合済み）"
        kind = f"{item.kind}｜" if item.kind else ""
        rows += (
            f'<div class="drow">{_img(frame, "根拠のコマ", empty="—")}<div style="min-width:0">'
            f'<div class="sm one">{_tier_badge(item.ranks, d.n)} '
            f'<span class="mut">{_esc(kind + who)}</span></div>'
            f'<div class="dt c3" contenteditable>{_esc(item.text)}</div>'
            f'<div class="ev c2">根拠 {_esc(_evidence(item.refs) or "—")}</div></div></div>'
        )
    items = (
        "".join(
            f'<li><div class="at c2" contenteditable>{_esc(a.text)}</div>'
            f'<div class="ev c2">{_esc(a.reason or ("根拠 " + _evidence(a.refs)))}</div></li>'
            for a in avoid[:4]
        )
        or '<li><div class="ev">該当なし</div></li>'
    )
    note = (
        ""
        if d.syn is not None
        else '<div class="note">AI の指示は無いため、コードが集計した事実の指示だけを出しています。</div>'
    )
    return (
        '<div class="kicker">クリエイティブ指示（根拠つき）／やらないこと</div>'
        '<h2 class="slide-title one" contenteditable>撮る前に決めること</h2>'
        '<div class="dircols">'
        f'<div><div class="dgrid{" two" if len(dirs[:6]) > 3 else ""}">{rows}</div>{note}</div>'
        f'<div class="avoid"><div class="h">やらないこと</div><ul>{items}</ul></div>'
        "</div>"
    )


# ── 絵コンテ ────────────────────────────────────────────────────────────


def _storyboard(d: Deck, sb: Storyboard, label: str) -> str:
    plan = {c.cut: c for c in d.ctx.cuts}
    cuts = {c.cut: c for c in sb.cuts}
    order = sorted(set(plan) | set(cuts))
    body = ""
    for k in order[:6]:
        cut = cuts.get(k)
        slot = plan.get(k)
        start = (
            cut.start_sec
            if cut is not None and cut.start_sec is not None
            else (slot.start if slot else None)
        )
        end = (
            cut.end_sec
            if cut is not None and cut.end_sec is not None
            else (slot.end if slot else None)
        )
        stage = (cut.stage if cut is not None and cut.stage else "") or (slot.stage if slot else "")
        stage = _STAGE_SHORT.get(stage, stage)
        when = f"{start:g}〜{end:g}秒" if start is not None and end is not None else "—"
        if cut is None:
            body += (
                f"<tr><td>{k}</td><td>{_esc(when)}<div class='mut'>{_esc(stage)}</div></td>"
                '<td colspan="4" class="mut">（案なし）</td></tr>'
            )
            continue
        ref = cut.refs[0] if cut.refs else None
        refs = (
            f'<div class="ref">{_img(_ref_frame(d, ref), "参考のコマ", empty="—")}'
            f'<div class="c3">{_esc(evidence_text(ref))}</div></div>'
            if ref is not None
            else "—"
        )
        body += (
            f"<tr><td>{k}</td><td>{_esc(when)}<div class='mut c1'>{_esc(stage)}</div></td>"
            f'<td class="sbt"><div class="c3" contenteditable>{_esc(cut.show)}</div></td>'
            f'<td><div class="c3" contenteditable>{_esc(cut.telop or "—")}</div></td>'
            f'<td><div class="c3" contenteditable>{_esc(cut.aim or "—")}</div></td>'
            f"<td>{refs}</td></tr>"
        )
    target = (
        f"目安の尺{sb.target_sec:g}秒（{d.n}本の尺の中央値・固定しない）" if sb.target_sec else ""
    )
    sub = "｜".join(t for t in (target, sb.basis_note, "秒は段の目安（叩き台）") if t)
    return (
        f'<div class="kicker">絵コンテ{_esc(label)}</div>'
        f'<h2 class="slide-title one" contenteditable>{_esc(sb.name or "絵コンテ")}</h2>'
        f'<div class="band one">{_esc(sub)}</div>'
        '<table class="sb" style="margin-top:8px"><colgroup><col style="width:64px">'
        '<col style="width:118px"><col><col style="width:210px"><col style="width:200px">'
        '<col style="width:260px"></colgroup>'
        "<thead><tr><th class='nw'>カット</th><th>秒</th><th>画</th><th>テロップ案</th><th>狙い</th>"
        f"<th>参考（照合済み）</th></tr></thead><tbody>{body}</tbody></table>"
    )


# ── 投稿設計と検証 ───────────────────────────────────────────────────────


def _feature_line(d: Deck, fid: str, label: str) -> str:
    f = d.ctx.feature(fid)
    if f is None:
        return f"{label} 0/{d.n}"
    rate = (
        f"（上位{f.board_rate[1]}本では{f.board_rate[0]}/{f.board_rate[1]}）"
        if f.board_rate
        else ""
    )
    return f"{label} {tier_text(f.ranks, d.n)}{rate}"


def _posting(d: Deck) -> str:
    n = d.n
    if n == 0:
        return ""
    lines = []
    for term in query_terms(d.out.query):
        lines.append(_feature_line(d, f"kw_caption:{term}", f"キャプションに「{term}」"))
        lines.append(_feature_line(d, f"kw_hashtag:{term}", f"ハッシュタグに「{term}」"))
    lines.append(_feature_line(d, "qty_caption", "分量をキャプションに載せる"))
    lines.append(_feature_line(d, "cta_caption:save", "キャプションで保存の呼びかけ"))
    facts_html = "".join(f'<p class="c2">{_esc(t)}</p>' for t in lines[:6])
    plan = d.syn.posting.caption_plan if d.syn is not None and d.syn.posting else ""
    plan_html = f'<p class="c3" contenteditable><b>案:</b> {_esc(plan)}</p>' if plan else ""
    saves = [f.save_rate for f in d.ctx.facts if f.plays > 0]
    basis = (
        f"（上位{n}本の中央値{statistics.median(saves):.2f}%・最大{max(saves):.2f}%）"
        if saves
        else ""
    )
    verify = _VERIFY_TEXT.format(basis=basis)
    hyps = ""
    for h in (d.syn.hypotheses if d.syn is not None else [])[:3]:
        test = f"検証: {h.test}" if h.test else ""
        hyps += (
            f'<p class="c2" contenteditable><b>{_esc(h.text)}</b></p>'
            f'<p class="sm mut c1">{_esc(tier_text(h.ranks, n))}</p>'
            + (f'<p class="sm c1" contenteditable>{_esc(test)}</p>' if test else "")
        )
    ab = d.syn.posting.ab_plan if d.syn is not None and d.syn.posting else ""
    if ab:
        hyps += f'<p class="c1" contenteditable>A/B: {_esc(ab)}</p>'
    if not hyps:
        hyps = '<p class="mut">A/B の案は無し（AI の仮説が無い）</p>'
    order = "".join(f"<dt>{_esc(k)}</dt><dd contenteditable></dd>" for k in _ORDER_FIELDS)
    return (
        '<div class="kicker">投稿設計と検証</div>'
        '<h2 class="slide-title one" contenteditable>撮る・投稿する・確かめる</h2>'
        '<div class="cols3">'
        f'<div class="box"><div class="h">キャプション設計（事実）</div>{facts_html}{plan_html}</div>'
        f'<div class="box"><div class="h">検証のしかた</div><p class="c4" contenteditable>'
        f"{_esc(verify)}</p>{hyps}</div>"
        f'<div class="box"><div class="h">発注の枠（空欄に記入）</div><dl class="order">{order}</dl></div>'
        "</div>"
    )


# ── 組み立て ────────────────────────────────────────────────────────────


def slide_sections(d: Deck) -> list[tuple[str, str, str]]:
    """(種類, 中身, 追加のクラス) の並び（空のスライドは除く）。"""
    items: list[tuple[str, str, str]] = [
        ("cover", _cover(d), "cover"),
        ("conclusion", _conclusion(d), ""),
        ("surface", _surface(d), ""),
        ("brands", _brands(d), ""),
        ("compare", _compare(d), ""),
        ("structure", _structure(d), ""),
    ]
    for v in d.ctx.videos:
        f = d.ctx.fact(v.meta.rank)
        if f is None or v.analysis is None:
            continue
        cls = "bd land" if f.orientation == "landscape" else "bd"
        items.append((f"video-{f.rank}", _breakdown(d, v, f), cls))
    items.append(("template", _template(d), ""))
    items.append(("directives", _directives(d), ""))
    boards = d.syn.storyboards if d.syn is not None else []
    for label, sb in zip(("案A", "案B"), boards, strict=False):
        items.append((f"storyboard-{label}", _storyboard(d, sb, label), ""))
    items.append(("posting", _posting(d), ""))
    return [(kind, body, cls) for kind, body, cls in items if body]


def render_slides(out: VideoAlgorithmOutput, *, generated_at: str = "") -> str:
    """VideoAlgorithmOutput → 提案資料向けスライドHTML（16:9・編集可）。

    純関数（I/O無し）。動画base64・外部 URL は載せない。空のスライドは描画しない。
    generated_at は検索結果を取得した日時（JST・ISO 8601）。表紙とフッタに出す。
    """
    d = build_deck(out, generated_at=generated_at)
    footer = footer_text(d)
    filled = slide_sections(d)
    total = len(filled)
    sections = "".join(
        _slide(i + 1, total, kind, body, footer, cls=cls)
        for i, (kind, body, cls) in enumerate(filled)
    )
    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>VSEO提案スライド｜{_esc(out.query)}</title>"
        f"<style>{_STYLE}</style></head><body>"
        f"{_EDIT_TIP}{sections}</body></html>"
    )


__all__ = [
    "CONTENT_BOTTOM",
    "NOEXPORT_CSS",
    "SLIDE_H",
    "SLIDE_W",
    "Deck",
    "brand_rows",
    "build_deck",
    "footer_text",
    "ordered_features",
    "pick_scene_frames",
    "render_slides",
    "slide_sections",
    "type_line",
]
