"""VSEO 分析 → 提案資料向け「要点スライドHTML」生成（16:9・営業がノーコード編集可）。

report.py の自己完結ダッシュボード（縦長・動画base64埋込）とは別物。ここは**提案資料に組み込む
スライド**を出す（仕様 v3 §2）:
  - 1 <section class="slide"> = 1スライド = 16:9 固定（1280x720）。PPTX 変換時の撮影サイズと一致。
  - テキストは contenteditable 付きの素タグ＋意味ベースのクラス名で、営業がブラウザで直接編集可。
  - 画像は表紙とコマ（data URI）だけ。**video_data_uri（数MBの動画base64）は絶対に載せない**。
    外部 URL（http/https の src・href）も載せない（media worker は外部参照のある HTML を拒否する）。

流れ（10＋n 枚。n＝動画を見て分析できた本数・サムネだけの縮退は数えない。空のスライドは出さない）:
  S1 表紙 → S2 結論 → S3 検索面の地図 → S4 ブランド露出マップ → S5 上位n本の比較
  →（サムネ＝一覧の表紙を読めたときだけ＋2 枚: サムネの比較 → サムネの作り方）
  → S6 n本の構成比較 → S7〜 構成分解（1本1枚）→ 共通する構成の型 → クリエイティブ指示／やらないこと
  → 絵コンテ案A（→ 案B）→ 投稿設計と検証
サムネの 2 枚の kind と CSS は thumb- で始める（資料の 1 枚目の cover と混ぜない）。6〜30 位の表紙は
外部 URL なので画像を載せず、文字だけで出す。

数字・本数・段階の名前（必須条件／多数派／事例）・区分・PR は事実層（facts / evidence）がコードで
決める。LLM 由来の文は synthesis v3 の検査を通したもの（CrossSynthesis.version == "v3"）だけを使い、
無ければコードの事実で代わりの文を出すか、その欄を省く（旧キャッシュの v2 の文は出さない）。

版面の約束（T22 で実描画を測る）: 中身は y≤664 に収め、フッタ（y≈684・14px）を全スライドに出す。
文字は最小 14px。長い文は -webkit-line-clamp で行数を決めて切る（PPTX は撮影なので、はみ出した分は
黙って切れる）。画像は 9:16 のまま全体を見せる（object-fit:contain）。横長の動画は横長の枠に入れる。
"""

from __future__ import annotations

import html
import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from teamagent.skills._html.dads import DADS_LICENSE_COMMENT, DADS_TOKENS_CSS
from teamagent.skills.search_surface_check.display import fmt_count
from teamagent.skills.search_surface_check.video_chapter import ROLE_COLOR
from teamagent.skills.search_surface_check.video_digest import HOOK_LABEL
from teamagent.skills.search_surface_check.video_structure import (
    INFERRED_MIDDLE_ROLE,
    MARK_GOOD,
    MARK_NONE,
    MARK_OK,
    MARK_WEAK,
    MARK_WORD,
    ROLE_LABEL,
    Grade,
    grade_video,
    infer_roles,
)
from teamagent.skills.video_algorithm.cover_facts import (
    ELEMENT_LABEL,
    FACE_KIND_LABEL,
    GAZE_LABEL,
    LEGIBILITY_LABEL,
    MATCH_LABEL,
    POSITION_JP,
    PRODUCT_LABEL,
    SIZZLE_LABEL,
    STATUS_LABEL,
    CoverFacts,
    CoverView,
    GapRow,
    code_cover_directives,
    cover_line,
    dist_text,
)
from teamagent.skills.video_algorithm.evidence import (
    COVER_SOURCES,
    MIN_TIER_N,
    TIER_MAJORITY,
    TIER_OBSERVED,
    TIER_REQUIRED,
    KwHit,
    Roster,
    at_least_majority,
    contains,
    majority_min,
    norm,
    query_terms,
    quote_frame,
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
    MetaGap,
    StageRow,
    VideoFacts,
    category_known,
    fmt_stamp,
    kw_matrix,
    product_brands,
    rank_runs,
    scene_index_at,
    surface_map,
    template,
    top_vs_rest,
    unanalyzed_ranks,
    visible_brands,
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
# 構成分解のコマの枚数（縦・横）と版面（px）。コマを 190 に抑えて事実欄を 4 行にする
# （「なぜ上位か」の推測が 2 行で切れていた。事実と推測は別の行にする）。
_FRAMES_PORTRAIT = 6
_FRAMES_LANDSCAPE = 4
_FRAME_H = 190
_FACTS_TOP = 426
_GRADES_TOP = 634
# コマが無い場面を「コマ未取得」の枠で見せる最小の長さ（秒）。最後の場面はこれより短くても出す。
_GAP_MIN_SEC = 6.0
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
# 構成バーの印（形＝文字で区別し、色だけに頼らない）。カテゴリを判定できないときの
# ブランドの印は「映」（目立つ映り込み。商品とは呼ばない）。
_MARK_LETTER = {
    "first_telop": "テ",
    "kw_telop": "検",
    "brand_first": "商",
    "brand_seen": "映",
    "result_first": "完",
    "cta": "締",
}
_MARK_LEGEND = {
    "first_telop": "最初のテロップ",
    "kw_telop": "検索語のテロップ",
    "brand_first": "目立つ商品",
    "brand_seen": "目立つ映り込み",
    "result_first": "完成品",
    "cta": "CTA",
}
_MARK_SHORT = {
    "first_telop": "最初のテロップ",
    "kw_telop": "検索語",
    "brand_first": "商品",
    "brand_seen": "映り込み",
    "result_first": "完成品",
    "cta": "CTA",
}
# 推定の役割（v2 の出力）では、最初と CTA 以外の場面を 1 色の「本編」にする（手順と決めつけない）。
_BODY_ROLE = "body"
_BODY_LABEL = "本編"
_BODY_COLOR = ROLE_COLOR["other"]
# 評価の記号と軸の説明（S6 に 1 回だけ出す）。
_GRADE_LEGEND = (
    f"評価（構成分解の最下行）: {MARK_GOOD}{MARK_WORD[MARK_GOOD]}・{MARK_OK}{MARK_WORD[MARK_OK]}・"
    f"{MARK_WEAK}{MARK_WORD[MARK_WEAK]}・{MARK_NONE}{MARK_WORD[MARK_NONE]}。"
    "掴み＝冒頭3秒・テンポ＝平均カット秒・KW＝検索語の初出・保存＝保存の仕掛け・CTA＝締めの"
    "呼びかけ・商品（名簿が無いときは映り込み）＝商品の見せ方・一致＝テロップ・キャプション・映像の一致"
)
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
# 検証のしかた（固定文・行ごとに出す）。{basis} は上位 n 本の保存率の中央値と最大。
_VERIFY_LINES: tuple[str, ...] = (
    "一次指標: このKWでの表示順位（投稿の翌日と7日後・同じ端末でログアウトして検索）",
    "二次指標: TikTokの分析画面の「検索からの流入」の割合と保存率{basis}",
    "A/Bは条件ごとに複数本を投稿して比べる（1本ずつの比較では差が偶然か分からない）",
)
_SUCCESS_PLACEHOLDER = "例: 7日後に上位10位以内"
_SUCCESS_FIELD = "成功の基準（何位・いつ）"
# 投稿設計のスライドに出す仮説の数（全部はレポートの「AI の読み解き」に出る）。
_SLIDE_HYPOTHESES = 2
# ブランド露出マップで行が少ないとき（この行数以下）は大きい文字にする（下に空白を残さない）。
_ROOMY_ROWS = 5
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
.c1{{-webkit-line-clamp:1;word-break:break-all}}
.c2{{-webkit-line-clamp:2}}.c3{{-webkit-line-clamp:3}}
.c4{{-webkit-line-clamp:4}}.c5{{-webkit-line-clamp:5}}
.one{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.nw{{white-space:nowrap}}.mut{{color:var(--mut)}}
.pr{{display:inline-block;background:var(--color-primitive-yellow-50);color:var(--color-primitive-yellow-1000);
  border:1px solid var(--color-primitive-yellow-900);border-radius:4px;padding:0 6px;font-size:14px;
  line-height:18px;font-weight:800;margin-left:6px;vertical-align:1px}}
.pr.ai{{border-style:dashed;background:var(--bg)}}
.ph{{background:var(--dark);color:var(--bg);font-size:14px;line-height:20px;display:flex;
  align-items:center;justify-content:center;text-align:center;border-radius:6px}}
.tier{{display:inline-block;border-radius:4px;padding:0 6px;font-size:14px;line-height:20px;
  font-weight:800;white-space:nowrap}}
.t-obs{{background:var(--bg);color:var(--sub);border:1px dashed var(--line)}}
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
.grid2 .wide,.grid3 .wide{{grid-column:1 / -1}}
.grid3{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px 18px}}
.grid3 .box .h{{margin-bottom:4px}}
.grid3 .wide .grid2{{gap:0 18px}}
table.gap{{width:100%;border-collapse:collapse;font-size:14px;line-height:20px;table-layout:fixed}}
table.gap th,table.gap td{{padding:1px 6px;border-bottom:1px solid var(--soft);text-align:left;
  vertical-align:top}}
table.gap th{{color:var(--sub);font-weight:700}}
table.bt.roomy td{{padding:12px 8px}}
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
.cmpg .cv img,.cmpg .cv .ph{{width:54px;height:96px;object-fit:contain;background:var(--dark);
  border-radius:4px}}
.cmpg .mx{{color:var(--accent);font-weight:800}}
.mrow{{display:flex;align-items:center;gap:6px;min-width:0}}
.mrow .mb{{flex:1;min-width:16px}}
.mb{{display:block;height:6px;background:var(--color-neutral-solid-gray-100);border-radius:3px;
  overflow:hidden}}
.mb i{{display:block;height:100%;background:var(--line)}}.mb i.mx{{background:var(--accent)}}
/* サムネ（一覧の表紙）の比較・作り方 */
.thumb-g{{display:grid;gap:0;border:1px solid var(--line);border-radius:10px;overflow:hidden}}
.thumb-g>div{{padding:2px 6px;font-size:14px;line-height:20px;border-bottom:1px solid var(--soft);
  min-width:0}}
.thumb-g .lab{{background:var(--chip);color:var(--sub);font-weight:700}}
.thumb-g .hd{{font-weight:800;color:var(--accent2);text-align:center;background:var(--chip)}}
.thumb-g .cv{{display:flex;justify-content:center;padding:4px}}
.thumb-g .cv img,.thumb-g .cv .ph{{width:74px;height:130px;object-fit:contain;background:var(--dark);
  border-radius:4px}}
.thumb-top{{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr);gap:16px}}
.thumb-top .chips{{max-height:102px;gap:6px}}
.thumb-top .chip{{padding:4px 12px;font-size:14px;line-height:20px}}
.thumb-dirs{{margin-top:6px}}
.thumb-dirs .drow{{padding:3px 0}}
.thumb-dirs .drow figure.rf{{width:34px}}
.thumb-dirs .drow img,.thumb-dirs .drow .ph{{width:34px;height:60px}}
.thumb-top .box{{padding:8px 12px}}
.thumb-top table.gap td,.thumb-top table.gap th{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
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
.bd .frames img,.bd .frames .ph{{display:block;width:140px;height:{_FRAME_H}px;object-fit:contain;
  background:var(--dark);border-radius:6px}}
.bd .frames .gap{{display:flex;flex-direction:column;align-items:center;justify-content:center;
  width:140px;height:{_FRAME_H}px;border:2px dashed var(--line);border-radius:6px;
  background:var(--chip);color:var(--sub);font-size:14px;line-height:20px;text-align:center}}
.bd .frames figcaption{{font-size:14px;line-height:20px;margin-top:2px}}
.bd .facts{{position:absolute;left:278px;right:64px;top:{_FACTS_TOP}px;display:grid;
  grid-template-columns:repeat(3,minmax(0,1fr));gap:6px 16px}}
.bd .facts .fl{{font-size:14px;line-height:18px;font-weight:800;color:var(--accent2)}}
.bd .facts p{{font-size:14px;line-height:20px}}
.bd .grades{{position:absolute;left:278px;right:64px;top:{_GRADES_TOP}px;font-size:14px;
  line-height:20px;color:var(--sub)}}
.bd.land .open{{width:214px}}
.bd.land .open img,.bd.land .open .ph{{width:210px;height:118px}}
.bd.land .hook3{{top:290px;width:214px}}
.bd.land .bar,.bd.land .frames,.bd.land .facts,.bd.land .grades{{left:302px}}
.bd.land .frames{{gap:18px}}
.bd.land .frames figure{{width:214px}}
.bd.land .frames img,.bd.land .frames .ph,.bd.land .frames .gap{{width:214px;height:120px}}
.bd.land .facts{{top:362px;gap:10px 16px}}
/* 共通する構成の型 */
.band{{font-size:15px;line-height:22px;color:var(--sub)}}
.stages{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:8px}}
.stage{{border:1px solid var(--line);border-radius:10px;padding:8px 10px;min-width:0}}
.stage .h{{border-bottom:1px solid var(--soft);padding-bottom:2px;margin-bottom:4px}}
.stage p{{font-size:14px;line-height:20px;margin-top:2px}}
.stage .lbl{{font-size:14px;line-height:20px;font-weight:800;color:var(--sub);margin-top:4px}}
.ex{{display:flex;gap:8px;margin-top:4px;align-items:flex-start}}
.ex figure{{flex:none;width:40px}}.ex.land figure{{width:96px}}
.ex img,.ex .ph{{display:block;width:40px;height:71px;object-fit:contain;background:var(--dark);
  border-radius:4px}}
figure.rf{{position:relative;overflow:hidden;border-radius:4px}}
figure.rf figcaption{{position:absolute;left:0;right:0;bottom:0;background:rgba(0,0,0,.72);
  color:var(--bg);font-size:14px;line-height:16px;text-align:center;white-space:nowrap}}
.ex.land img,.ex.land .ph{{width:96px;height:54px}}
.tplfoot{{margin-top:8px}}
table.kw{{width:100%;border-collapse:collapse;font-size:14px;line-height:20px;table-layout:fixed}}
table.kw th,table.kw td{{padding:2px 6px;border-bottom:1px solid var(--soft);text-align:left;
  vertical-align:top}}
table.kw th{{background:var(--chip);font-weight:800;color:var(--sub)}}
/* 指示・やらないこと */
.dircols{{display:grid;grid-template-columns:minmax(0,2.1fr) minmax(0,1fr);gap:20px}}
.dircols.wide{{grid-template-columns:minmax(0,3fr) minmax(0,1fr)}}
.dgrid{{display:grid;grid-template-columns:minmax(0,1fr);gap:4px 14px}}
.dgrid.two{{grid-template-columns:repeat(2,minmax(0,1fr))}}
.drow{{display:flex;gap:10px;padding:6px 0;border-bottom:1px solid var(--soft)}}
.drow figure.rf{{flex:none;width:40px}}
.drow img,.drow .ph{{display:block;width:40px;height:71px;object-fit:contain;background:var(--dark);
  border-radius:4px}}
.drow .dt{{font-size:16px;line-height:22px;font-weight:700}}
.drow .ev{{font-size:14px;line-height:20px;color:var(--sub)}}
.avoid{{background:var(--chip);border-radius:10px;padding:10px 14px}}
.avoid li{{list-style:none;padding:6px 0;border-bottom:1px solid var(--soft)}}
.avoid .at{{font-size:16px;line-height:23px;font-weight:700}}
.avoid .ev{{font-size:14px;line-height:20px;color:var(--sub)}}
.avoid .given{{font-size:14px;line-height:20px;color:var(--sub);padding:4px 0 6px;
  border-bottom:1px solid var(--soft)}}
/* 絵コンテ */
table.sb{{width:100%;border-collapse:collapse;table-layout:fixed;font-size:14px;line-height:20px}}
table.sb th{{background:var(--chip);color:var(--sub);font-weight:800;text-align:left;padding:4px 8px;
  border-bottom:1px solid var(--line)}}
table.sb td{{padding:4px 8px;border-bottom:1px solid var(--soft);vertical-align:top}}
table.sb .ref{{display:flex;gap:6px}}
table.sb .ref figure.rf{{flex:none;width:36px}}
table.sb .ref img,table.sb .ref .ph{{display:block;width:36px;height:64px;object-fit:contain;
  background:var(--dark);border-radius:3px}}
table.sb td.sbt{{font-size:15px;line-height:21px;font-weight:700}}
/* 投稿設計と検証 */
.cols3{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}}
.cols3 .box p{{font-size:15px;line-height:22px;margin-top:4px}}
.order{{display:grid;grid-template-columns:max-content 1fr;gap:10px 10px;margin-top:6px;font-size:15px;
  line-height:22px}}
.order dt{{font-weight:700;color:var(--sub)}}
.order dd{{border-bottom:1px solid var(--line);min-height:22px}}
.order dd .mut{{font-size:14px}}
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
    """全スライドのフッタ（観測の仮説・相関≠因果・取得時点・秒は AI 推定・タイアップ）。

    タイアップはキャプションの表記（#PR・@ブランド）と、AI の推定だけのものを分けて書く。
    """
    head = f"上位{d.n}本の観測にもとづく仮説" if d.n else "検索上位の観測にもとづく仮説"
    when = f"順位は{d.stamp}時点" if d.stamp else "順位は取得時点"
    parts = [head, "相関は因果ではない", when, "秒はAI推定（±2秒）"]
    marked = [f.rank for f in d.ctx.facts if f.pr_marked]
    ai_only = [f.rank for f in d.ctx.facts if f.pr_ai_only]
    if marked:
        parts.append(f"上位にタイアップ表記{len(marked)}本（{ranks_text(marked)}）")
    if ai_only:
        parts.append(f"AI推定の提供の可能性{len(ai_only)}本（{ranks_text(ai_only)}）")
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
    cls = {TIER_REQUIRED: "t-req", TIER_MAJORITY: "t-maj", TIER_OBSERVED: "t-obs"}.get(
        name, "t-case"
    )
    return f'<span class="tier {cls}">{_esc(text)}</span>'


def _display_roles(a: VideoVSEOAnalysis) -> list[tuple[str, bool]]:
    """場面の役割（表示用）。推定の役割では、最初と CTA 以外を 1 色の「本編」にする。

    v2 の出力（役割の欄が無い）で真ん中の場面を全部「手順」と描くと、構成の違いが読めない
    うえに、推定の規則から必ず出る値が観測に見える。
    """
    return [
        (_BODY_ROLE if inferred and role == INFERRED_MIDDLE_ROLE else role, inferred)
        for role, inferred in infer_roles(a)
    ]


def _role_color(role: str) -> str:
    if role == _BODY_ROLE:
        return _BODY_COLOR
    return ROLE_COLOR.get(role, ROLE_COLOR["other"])


def _alias(entry: str) -> str:
    """「S&B|エスビー食品」→「S&B（エスビー食品）」。"""
    names = [x.strip() for x in entry.split("|") if x.strip()]
    if len(names) <= 1:
        return names[0] if names else ""
    return f"{names[0]}（{'・'.join(names[1:])}）"


def _hook(hook_type: str) -> str:
    return HOOK_LABEL.get(hook_type, HOOK_LABEL["other"])


def _role(role: str | None) -> str:
    if role == _BODY_ROLE:
        return _BODY_LABEL
    return ROLE_LABEL.get(role or "other", ROLE_LABEL["other"])


def _pr_badge(f: VideoFacts) -> str:
    """PR＝キャプションのタイアップ表記。PR?＝AI の推定だけ（キャプションに表記なし）。"""
    if f.pr_marked:
        return '<span class="pr" title="キャプションのタイアップ表記">PR</span>'
    if f.pr:
        return '<span class="pr ai" title="AIの推定（キャプションに表記なし）">PR?</span>'
    return ""


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
    if 0 < d.n < MIN_TIER_N:
        return f"上位{d.n}本の観測（本数が少ないため共通点の段階は付けない）", True
    sl = d.syn.summary_lines if d.syn is not None else None
    if sl is not None and sl.type_line:
        return sl.type_line, sl.type_line_by_code
    alt, _ids = alt_type_line(d.ctx)
    return (alt or f"上位{d.n}本の共通点（仮説）"), True


def _row_spans(row: _BrandRow) -> str:
    """「#2（主役）・#3（付随）」（同じ動画は 1 回・最も目立つ方）。"""
    best: dict[int, BrandFact] = {}
    for rank, b in row.spans:
        cur = best.get(rank)
        if cur is None or _PROM_ORDER.get(b.prominence, 4) < _PROM_ORDER.get(cur.prominence, 4):
            best[rank] = b
    return "・".join(
        f"#{rank}（{b.prominence_label or '目立ち方不明'}{'・PR' if b.sponsored else ''}）"
        for rank, b in sorted(best.items())
    )


def _brand_status(d: Deck) -> tuple[str, str]:
    """ブランドの現在地。名簿があれば別名を 1 つにまとめて（S4 と同じ行）区分ごとに書く。"""
    facts = d.ctx.facts
    if d.roster.specified:
        main, _others = brand_rows(facts, d.roster)
        name = d.ctx.client_label
        mine = "・".join(_row_spans(r) for r in main if r.relation == "client")
        rivals = "、".join(f"{r.name} {_row_spans(r)}" for r in main if r.relation == "competitor")
        return (
            "ブランドの現在地",
            f"{name}: {mine or f'上位{d.n}本には映らない'}／競合: {rivals or '映らない'}",
        )
    shown = [
        f"{b.name} #{f.rank}{'（PR）' if b.sponsored else ''}"
        for f in facts
        for b in visible_brands(f)
    ]
    text = "・".join(shown) if shown else "主役・目立つ大きさで映るブランドは無い"
    return "ブランドの現在地（区分・カテゴリは未指定）", f"目立って映る: {text}"


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


def best_alert(d: Deck) -> str:
    """最も見られ保存された 1 本がタイアップ（競合なら競合名も）のとき、その事実の 1 文。"""
    f = d.ctx.fact(d.ctx.best_rank)
    if f is None or not f.pr:
        return ""
    rivals = [
        b.name for b in f.brands if b.relation == "competitor" and (b.sponsored or b.prominent)
    ]
    what = "タイアップ投稿" if f.pr_marked else "提供の可能性がある投稿（AI推定）"
    basis = f.pr_caption_evidence if f.pr_marked else f.pr_evidence
    if rivals:
        return (
            f"最も見られ保存された#{f.rank}は、競合（{'・'.join(dict.fromkeys(rivals))}）の"
            f"{what}（{basis}）。検索上位の最良枠を競合が取っている"
        )
    return f"最も見られ保存された#{f.rank}は{what}（{basis}）"


def meta_gap_text(d: Deck) -> str:
    """上位 n 本と残りのメタの差のうち、いちばん大きいもの（2 倍以上か半分以下のときだけ）。"""
    rows = [
        r
        for r in top_vs_rest(d.out.board, d.ctx.ranks, d.out.query)
        if r.ratio is not None and r.ratio > 0 and (r.ratio >= 2 or r.ratio <= 0.5)
    ]
    if not rows:
        return ""
    r = max(rows, key=lambda x: abs(math.log(x.ratio or 1.0)))
    return f"メタの差: {r.label} 上位{d.n}本 {r.top}・ほか {r.rest}（{r.ratio:g}倍）"


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
            f'<div class="sm mut">{n}本すべて＝前提</div></span><div class="chips">'
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
    if n < MIN_TIER_N:
        rows = (
            f'<div class="note">分析できたのが{n}本のため、段階（必須条件・多数派）と'
            "共通点のチップは出していません。</div>"
        )
    tiers = f'<div class="tiers">{rows}</div>' if rows else ""
    best_head, best_body = _best_text(d)
    best = d.ctx.fact(d.ctx.best_rank)
    brand_head, brand_body = _brand_status(d)
    pair = (
        '<div class="pair">'
        f'<div class="box"><div class="h">最も見られ保存された1本</div>'
        f'<div class="md one">{_esc(best_head)}{_pr_badge(best) if best else ""}</div>'
        f'<div class="md c2" contenteditable>{_esc(best_body)}</div></div>'
        f'<div class="box"><div class="h">{_esc(brand_head)}</div>'
        f'<div class="md c3" contenteditable>{_esc(brand_body)}</div></div>'
        "</div>"
    )
    alert = best_alert(d)
    alert_html = f'<div class="warn c2">{_esc(alert)}</div>' if alert else ""
    sl = d.syn.summary_lines if d.syn is not None else None
    pitch = (
        f'<div class="pitch c2"><b>次の一手（案）</b>　<span contenteditable>'
        f"{_esc(sl.client_move)}</span></div>"
        if sl is not None and sl.client_move
        else ""
    )
    missing = unanalyzed_ranks(d.ctx.ranks, d.out.board)
    rest = f"{rank_runs(missing)}は動画を未分析" if missing else f"上位{n}本だけの観測"
    gap = meta_gap_text(d)
    note = (
        f'<div class="note c2">差の要因: 未特定（{_esc(rest)}）'
        + (f"。{_esc(gap)}（動画の中身の差ではない）" if gap else "")
        + ("・見出しはコードの集計から作成" if by_code else "")
        + "</div>"
    )
    thumb = cover_line(d.ctx.cover) if d.ctx.cover.any_ok else ""
    if thumb:
        note += f'<div class="note c1">{_esc(thumb)}</div>'
    warn = (
        f'<div class="warn">分析できたのは{n}本（極小サンプル）。断定でなく観測仮説として、'
        "テスト投稿での検証を前提にお読みください。</div>"
        if n < 3
        else ""
    )
    return (
        f'<div class="kicker">結論（上位{n}本の観測・仮説）</div>'
        f'<h2 class="slide-title c2" contenteditable>{_esc(title)}</h2>'
        f"{tiers}{pair}{alert_html}{pitch}{note}{warn}"
    )


# ── S3 検索面の地図 ──────────────────────────────────────────────────────


def _gap_table(d: Deck) -> str:
    """上位 n 本（動画を分析した本）とボードの残りのメタの差（2 つの表を横に並べる）。"""
    rows = top_vs_rest(d.out.board, d.ctx.ranks, d.out.query, max_terms=1)
    if not rows:
        return ""
    rest_n = len(unanalyzed_ranks(d.ctx.ranks, d.out.board))
    head = f"<tr><th>項目</th><th>上位{d.n}本</th><th>ほか{rest_n}本</th></tr>"
    half = (len(rows) + 1) // 2

    def table(part: list[MetaGap]) -> str:
        body = "".join(
            f'<tr><td class="one">{_esc(r.label)}</td><td class="one">{_esc(r.top)}</td>'
            f'<td class="one">{_esc(r.rest)}</td></tr>'
            for r in part
        )
        return (
            '<table class="gap"><colgroup><col style="width:52%"><col><col></colgroup>'
            f"<thead>{head}</thead><tbody>{body}</tbody></table>"
        )

    return (
        f'<div class="box wide"><div class="h one">上位{d.n}本とほかの{rest_n}本の差'
        "（メタだけ・コードの集計。動画の中身は上位だけ分析）</div>"
        f'<div class="grid2">{table(rows[:half])}{table(rows[half:])}</div></div>'
    )


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
    )
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
        ("タイアップ表記（キャプション）", f'<div class="sm c2">{_esc(pr)}</div>'),
        ("保存率", f'<div class="sm c3">{_esc(save)}</div>'),
        ("投稿年の分布", f'<div class="years">{years}</div>'),
        (
            "検索語を持つ率（前提の水準）",
            f'<div class="sm c3">{kw}</div>',
        ),
    ]
    if angles:  # AI の切り口の語が無ければ欄ごと出さない
        cells.insert(
            1, ("切り口の頻度（語は AI・本数はコード）", f'<div class="sm c4">{angles}</div>')
        )
    body = "".join(
        f'<div class="box"><div class="h one">{_esc(h)}</div>{c}</div>' for h, c in cells
    )
    missing = unanalyzed_ranks(d.ctx.ranks, board)
    rest = (
        f"{rank_runs(missing)}は動画を見ていない（キャプション・再生などのメタだけ）。"
        if missing
        else ""
    )
    return (
        f'<div class="kicker">検索面の地図（上位{size}本・メタのみ）</div>'
        f'<h2 class="slide-title one" contenteditable>上位{size}本の作り手・切り口・タイアップ</h2>'
        f'<div class="grid3">{body}{_gap_table(d)}</div>'
        f'<div class="note">{_esc(rest)}切り口の本数はキャプションの語でコードが数えた値'
        "（「NG談を4つ」のような情報の数は除く。該当の先頭はレポートに）。</div>"
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
    # 行が少ないときは文字と行間を大きくして、下に大きな空白を残さない。
    roomy = " roomy" if len(main) + (1 if others else 0) <= _ROOMY_ROWS else ""
    return (
        '<div class="kicker">ブランド露出マップ</div>'
        f'<h2 class="slide-title one" contenteditable>上位{d.n}本に映るブランドと区分</h2>'
        f'<table class="bt{roomy}"><colgroup><col style="width:220px"><col style="width:116px">'
        '<col style="width:130px"><col><col style="width:86px"><col style="width:190px">'
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


def _cover_label(d: Deck, facts: Sequence[VideoFacts]) -> str:
    """サムネの行の名前。表紙の画像のときだけ「表紙」と呼ぶ（コマで代用したものは表紙ではない）。"""
    sources = {
        v.cover_source
        for f in facts
        if (v := d.video(f.rank)) is not None and v.cover_data_uri.startswith("data:image/")
    }
    if sources == {"cover"}:
        return "表紙"
    if sources == {"frame"}:
        return "冒頭のコマ（表紙の代用）"
    if sources == {"cover", "frame"}:
        return "表紙（一部はコマで代用）"
    return "サムネ（表紙か冒頭のコマ）"


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
        cells.append(f'<div class="lab c2">{_esc(_cover_label(d, facts))}</div>')
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

    known = category_known(facts)
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
    row(
        "映る商品" if known else "目立つ映り込み",
        (_esc(_top_brand_name(f, known=known)) for f in facts),
    )
    row("締め・CTA", (_esc(_cta_short(f)) for f in facts))
    what = "映る商品＝名簿の商品" if known else "映り込みのカテゴリは未判定"
    # 表紙を読めていないときの注記は見出しの上の行に足す（下の注記は 1 行で余白が無い）。
    thumb = _thumb_note(d)
    kicker = f"上位{n}本の比較" + (f"・{thumb}" if thumb else "")
    return (
        f'<div class="kicker">{_esc(kicker)}</div>'
        f'<h2 class="slide-title one" contenteditable>上位{n}本を同じ項目で並べる</h2>'
        f'<div class="cmpg" style="grid-template-columns:{cols}">{"".join(cells)}</div>'
        f'<div class="note c1">最多＝再生→保存率→シェアで最大（#{d.ctx.best_rank}）・青字＝各行の'
        "最大・PR?＝AI推定のみ（表記なし）・（テロップ）＝最後の10秒のテロップ・"
        f"{_esc(what)}</div>"
    )


def _thumb_note(d: Deck) -> str:
    """S5 に足す表紙の注記（読めていないとき・以前の分析のとき）。読めていれば空。"""
    view = d.ctx.cover
    if view.any_ok:
        return ""
    if not view.top:
        mode = d.out.cover_read_mode
        if mode == "":
            return "サムネ（一覧の表紙）の分析なし（以前の分析）"
        if mode == "off":
            return "サムネ（一覧の表紙）の分析は止めている設定"
        return "サムネ（一覧の表紙）の読み取りを始められず"
    if all(c.status == "no_cover" for c in view.top):
        return "表紙の URL が無い取り方のため、サムネ（一覧の表紙）を読めず"
    why = "・".join(dict.fromkeys(STATUS_LABEL.get(c.status, c.status) for c in view.top))
    return f"サムネ（一覧の表紙）を読めず（上位{view.n_top}本すべて・{why}）"


def _thumb_uri(d: Deck, rank: int) -> str:
    """スライドに載せる表紙（分析した動画の表紙の画像だけ。外部 URL は載せない）。"""
    v = next((v for v in d.out.videos if v.meta.rank == rank), None)
    if v is not None and v.cover_source == "cover" and v.cover_data_uri.startswith("data:image/"):
        return v.cover_data_uri
    return ""


def _thumb_rows(c: CoverFacts) -> list[tuple[str, str, str]]:
    """(項目名, 値, 行のクラス)。値は第三者の文字を含む（呼んだ側でエスケープ）。"""
    if not c.ok:
        note = STATUS_LABEL.get(c.status, c.status)
        return [(label, note if i == 0 else "—", "c1") for i, label in enumerate(_THUMB_LABELS)]
    texts = "／".join(f"「{t.replace(chr(10), ' ')}」" for t in c.texts) or "なし"
    size = "不明" if c.large_text is None else "読める大きさ" if c.large_text else "小さい"
    shape = f"{size}・{POSITION_JP.get(c.position, '不明')}・{c.lines}行" if c.has_text else "—"
    marks = [f"「{t}」" for t in c.kw_terms]
    if c.numbers:
        marks.append("数字")
    if c.question:
        marks.append("問い")
    if c.effortless:
        marks.append("手間なし")
    if c.warning:
        marks.append("注意")
    face = FACE_KIND_LABEL.get(c.face_kind, "不明")
    if c.face_real:
        face += f"・{GAZE_LABEL.get(c.gaze, '不明')}"
    sizzle = "・".join(SIZZLE_LABEL.get(x, x) for x in (c.sizzle or ()))
    brands = "・".join(f"{n}{'（未照合）' if r == 'unverified' else ''}" for n, r in c.brands)
    return [
        ("主役", "・".join(ELEMENT_LABEL.get(e, e) for e in (c.elements or ())) or "—", "c1"),
        ("表紙の文字（AI読み取り）", texts, "c2"),
        ("大きさ・位置・行数", shape, "c1"),
        ("文字の中身", "・".join(marks) or "—", "c1"),
        ("顔・視線", face, "c1"),
        (
            "寄り・質感",
            ("寄り" if c.closeup else "引き" if c.closeup is False else "不明")
            + (f"・{sizzle}" if sizzle else ""),
            "c1",
        ),
        (
            "商品・商品名",
            f"{PRODUCT_LABEL.get(c.product, '不明')}" + (f"・{brands}" if brands else ""),
            "c1",
        ),
        ("読みやすさ", LEGIBILITY_LABEL.get(c.legibility, "不明"), "c1"),
        (
            "冒頭テロップ",
            MATCH_LABEL.get(c.opening_match, "—") if c.watched else "未分析",
            "c1",
        ),
        (
            "キャプション冒頭",
            f"{MATCH_LABEL.get(c.caption_match, '—')}「{c.caption_head[:20]}」",
            "c2",
        ),
    ]


_THUMB_LABELS = (
    "主役",
    "表紙の文字（AI読み取り）",
    "大きさ・位置・行数",
    "文字の中身",
    "顔・視線",
    "寄り・質感",
    "商品・商品名",
    "読みやすさ",
    "冒頭テロップ",
    "キャプション冒頭",
)


def _thumb_compare(d: Deck) -> str:
    """サムネ（一覧の表紙）の比較（上位 n 本の格子）。1 本も読めていなければ出さない。"""
    view = d.ctx.cover
    if not view.any_ok:
        return ""
    covers = list(view.top)
    n = len(covers)
    cols = f"150px repeat({n},minmax(0,1fr))"
    cells: list[str] = ['<div class="lab"></div>']
    cells += [f'<div class="hd one">#{c.rank}</div>' for c in covers]
    cells.append('<div class="lab c2">表紙</div>')
    for c in covers:
        uri = _thumb_uri(d, c.rank)
        img = (
            _uri_img(uri, f"#{c.rank} の表紙")
            if uri
            else '<div class="ph">画像なし（動画を分析できず）</div>'
        )
        cells.append(f'<div class="cv">{img}</div>')
    per = [_thumb_rows(c) for c in covers]
    for i, label in enumerate(_THUMB_LABELS):
        cls = per[0][i][2] if per else "c1"
        cells.append(f'<div class="lab {cls}">{_esc(label)}</div>')
        cells.extend(f'<div class="{row[i][2]}">{_esc(row[i][1])}</div>' for row in per)
    ok = len(view.top_ok)
    return (
        '<div class="kicker">サムネ（一覧の表紙）の比較</div>'
        f'<h2 class="slide-title one" contenteditable>上位{n}本の表紙を同じ項目で並べる</h2>'
        f'<div class="thumb-g" style="grid-template-columns:{cols}">{"".join(cells)}</div>'
        f'<div class="note c2">表紙を読めた {ok}/{n}本・文字と要素は AI が表紙の画像から読んだもの'
        "（画像と照合していない）・大きさと位置は AI が示した枠からコードが計算・冒頭テロップとの"
        "一致は AI の読み取り同士・キャプション冒頭（一覧のタイルの下に出る文字）は実データ</div>"
    )


def _thumb_chip(f: Feature) -> str:
    who = "" if f.count == f.n else f"（{ranks_text(f.ranks)}）"
    return (
        f'<span class="chip"><b class="one">{_esc(f.label)}</b>'
        f"<i>{_esc(f.tier)} {f.count}/{f.n}{_esc(who)}</i></span>"
    )


# スライドのチップの順（具体的な言い方と画の要素を先に・「文字がある」だけは出さない）。
_PLAN_CHIP_ORDER = (
    "cover:kw:",
    "cover:number",
    "cover:text_large",
    "cover:el:",
    "cover:face",
    "cover:own_text",
    "cover:opening_match",
    "cover:closeup",
    "cover:sizzle",
    "cover:",
)


def _plan_gap_rows(view: CoverView) -> list[GapRow]:
    """スライドに出す差の行（印のある行 → 差の大きい行・5 行まで）。全部の行はレポートに出す。"""
    rows = sorted(view.gap, key=lambda g: (not g.marked, -abs(g.top_rate - g.rest_rate)))
    return rows[:5]


def _plan_chips(view: CoverView) -> list[Feature]:
    out: list[Feature] = []
    for prefix in _PLAN_CHIP_ORDER:
        for f in view.features:
            if f in out or f.id == "cover:text" or not at_least_majority(f.tier):
                continue
            if f.id.startswith(prefix):
                out.append(f)
    return out[:6]


def _thumb_plan(d: Deck) -> str:
    """サムネ（一覧の表紙）の作り方（共通点・上位とほかの差・根拠つきの指示）。"""
    view = d.ctx.cover
    if not view.any_ok:
        return ""
    feats = _plan_chips(view)
    chips = (
        "".join(_thumb_chip(f) for f in feats)
        or '<span class="sm mut">多数派以上の共通点なし</span>'
    )
    left = (
        '<div class="box"><div class="h">上位の表紙に共通する作り（タップ率は取れない・順位との関係のみ）</div>'
        f'<div class="chips">{chips}</div>'
        f'<div class="note c2">{_esc(dist_text(view.dist))}</div></div>'
    )
    if view.mode == "board":
        rows = "".join(
            f'<tr><td class="one">{_esc(g.label)}</td><td>{g.a}/{g.n}</td><td>{g.b}/{g.m}</td>'
            f"<td>{'差が大きい' if g.marked else '—'}</td></tr>"
            for g in _plan_gap_rows(view)
        )
        right = (
            '<div class="box"><div class="h one">上位とほかの表紙（参考・因果ではない）</div>'
            '<table class="gap"><colgroup><col style="width:52%"><col><col><col></colgroup>'
            f"<thead><tr><th>項目</th><th>上位</th><th>ほか</th><th>印</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )
    else:
        right = (
            '<div class="box"><div class="h">上位とほかの表紙</div>'
            f'<div class="md c3">{_esc(view.gap_note)}。共通点は上位の中の集計で、'
            "6〜30位より多いとは言えない</div></div>"
        )
    dirs = list(d.syn.cover_directives) if d.syn is not None else code_cover_directives(view)
    rows_html = ""
    for item in dirs[:3]:
        ref = item.refs[0] if item.refs else None
        uri = _thumb_uri(d, ref.rank) if ref is not None else ""
        fig = (
            f'<figure class="rf">{_uri_img(uri, "根拠の表紙")}<figcaption>#{ref.rank}</figcaption>'
            "</figure>"
            if ref is not None and uri
            else ""
        )
        who = "コードの集計" if item.origin == "code" else "AI の指示（根拠は照合済み）"
        tag = f"{item.tier}（{ranks_text(item.ranks)}）" if item.ranks else item.tier
        rows_html += (
            f'<div class="drow">{fig}<div style="min-width:0">'
            f'<div class="sm one"><span class="tier t-maj">{_esc(tag)}</span> '
            f'<span class="mut">{_esc(who)}</span></div>'
            f'<div class="dt c2" contenteditable>{_esc(item.text)}</div>'
            f'<div class="ev c1">根拠 {_esc(_evidence(item.refs) or "—")}</div></div></div>'
        )
    if not rows_html:
        rows_html = '<div class="note">多数派以上の特徴が無いため、表紙の指示は出していません</div>'
    return (
        '<div class="kicker">サムネ（一覧の表紙）の作り方</div>'
        '<h2 class="slide-title one" contenteditable>表紙で決めること（根拠つき）</h2>'
        f'<div class="thumb-top">{left}{right}</div>'
        f'<div class="thumb-dirs">{rows_html}</div>'
    )


def _top_brand_name(f: VideoFacts, *, known: bool) -> str:
    """比較の 1 セル: 名簿が分かれば商品（クライアント→競合）、分からなければ目立つ映り込み。"""
    if known:
        prods = product_brands(f)
        if not prods:
            return "なし"
        b = prods[0]
        rel = f"（{b.relation_label}）" if b.relation in ("client", "competitor") else ""
        return f"{b.name}{rel}"
    shown = visible_brands(f)
    return shown[0].name if shown else "—"


def _cta_short(f: VideoFacts) -> str:
    if f.cta_in_video is not None:
        kind, _text, sec = f.cta_in_video
        when = f"{sec:g}秒 " if sec is not None else ""
        src = "（テロップ）" if f.cta_source == "telop" else ""
        return f"{when}{CTA_KIND_LABEL.get(kind, kind)}{src}"
    return "なし"


# ── S6 構成比較・構成バー ────────────────────────────────────────────────


def _ordered_scenes(a: VideoVSEOAnalysis) -> list[Scene]:
    return sorted(a.scenes, key=lambda sc: (sc.start_sec, sc.end_sec))


def _axis_len(f: VideoFacts, a: VideoVSEOAnalysis | None) -> float:
    ends = [max(sc.end_sec, sc.start_sec) for sc in (a.scenes if a else [])]
    return max([f.duration_sec, *ends, 1.0])


def _markers(d: Deck, f: VideoFacts) -> list[tuple[float, str]]:
    """▼の印。商品の印（商）は名簿かカテゴリで商品と分かったものだけ。分からなければ「映」。"""
    known = category_known(d.ctx.facts)
    brands = product_brands(f) if known else visible_brands(f)
    brand = [b.first_sec for b in brands if b.prominent and b.first_sec is not None]
    raw: list[tuple[str, float | None]] = [
        ("first_telop", f.first_telop_sec),
        ("kw_telop", f.kw_first_telop_sec),
        ("brand_first" if known else "brand_seen", min(brand) if brand else None),
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
    """役割のバー。続く同じ役割（推定の「本編」など）は、いちばん長い区間にだけ名前を入れる。"""
    scenes = _ordered_scenes(a)
    roles = _display_roles(a)
    spans: list[tuple[float, float, str]] = []
    for sc, (role, _inferred) in zip(scenes, roles, strict=True):
        start = max(0.0, sc.start_sec)
        end = min(axis, max(sc.end_sec, sc.start_sec))
        if end > start:
            spans.append((start, end, role))
    named: set[int] = set()
    i = 0
    while i < len(spans):
        j = i
        while j + 1 < len(spans) and spans[j + 1][2] == spans[i][2]:
            j += 1
        named.add(max(range(i, j + 1), key=lambda k: spans[k][1] - spans[k][0]))
        i = j + 1
    segs = ""
    for k, (start, end, role) in enumerate(spans):
        left = start / axis * 100
        width = (end - start) / axis * 100
        wide = width / 100 * width_px >= _ROLE_NAME_MIN_PX and height >= _ROLE_NAME_MIN_H
        name = _role(role) if wide and k in named else ""
        segs += (
            f'<div class="seg" style="left:{left:.2f}%;width:{width:.2f}%;line-height:{height}px;'
            f'background:{_role_color(role)}">{_esc(name)}</div>'
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
        f'<span><span class="sw" style="background:{_role_color(r)}"></span>{_esc(_role(r))}</span>'
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
    avail = CONTENT_BOTTOM - 226 - 22
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
        roles = _display_roles(a)
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
    legend = _role_legend(r for r in (*ROLE_LABEL, _BODY_ROLE) if r in roles_seen)
    how = "役割は推定（最初の場面＝フック、CTA の秒の場面＝CTA、ほかは本編）" if inferred else ""
    axis_row = f'<div class="srow"><div></div><div>{_ticks(axis, _step(axis))}</div></div>'
    return (
        f'<div class="kicker">{n}本の構成比較</div>'
        f'<h2 class="slide-title one" contenteditable>{n}本の構成を同じ秒の物差しで並べる</h2>'
        f'<div class="legend c2">{legend}<span>▼ {_esc(_mark_legend(marks_seen))}</span>'
        f"{f'<span>{_esc(how)}</span>' if how else ''}</div>"
        f'<div class="legend c1">{_esc(_GRADE_LEGEND)}</div>'
        f'<div style="margin-top:6px">{rows}{axis_row}</div>'
    )


# ── S7〜 構成分解 ────────────────────────────────────────────────────────


def _scene_at(scenes: list[Scene], sec: float) -> int | None:
    return scene_index_at(scenes, sec)


def pick_scene_frames(
    a: VideoVSEOAnalysis, frames: Sequence[FrameShot], limit: int, *, exclude: FrameShot | None
) -> list[tuple[FrameShot, str | None]]:
    """場面のコマ（最初・最後・役割が変わる・長い場面の順に選び、時刻の順に並べる）と、その役割。

    コマは既に抜いたものだけを使う（場面の内側にあるコマ・無い場面は飛ばす）。見出しに使うのは
    「秒｜場面の役割」だけ（ブランド名・KW の見出しは付けない。Gemini の秒は 1〜2 秒ずれる）。
    役割が推定のとき、最初と CTA 以外の場面は「本編」と呼ぶ（手順と決めつけない）。
    """
    usable = sorted(
        (f for f in frames if f.data_uri.startswith("data:image/") and f is not exclude),
        key=lambda f: f.sec,
    )
    if not usable or limit <= 0:
        return []
    scenes = _ordered_scenes(a)
    roles = [r for r, _i in _display_roles(a)]
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


@dataclass(frozen=True)
class FrameSlot:
    """構成分解のコマの枠 1 つ（コマか、コマが無い場面の「コマ未取得」）。"""

    start: float
    end: float
    role: str | None
    frame: FrameShot | None


def _uncovered(scene: Scene, shown: Sequence[FrameShot]) -> bool:
    """場面にそれを代表するコマが無いか（中央から 場面の長さの 1/4＋3 秒 以内にコマが無い）。"""
    end = max(scene.end_sec, scene.start_sec)
    mid = (scene.start_sec + end) / 2
    reach = (end - scene.start_sec) / 4 + 3.0
    return not any(scene.start_sec <= f.sec <= end and abs(f.sec - mid) <= reach for f in shown)


def frame_slots(
    a: VideoVSEOAnalysis, frames: Sequence[FrameShot], limit: int, *, exclude: FrameShot | None
) -> list[FrameSlot]:
    """構成分解のコマの枠。長い場面（6 秒以上）と最後の場面にコマが無ければ「コマ未取得」の枠を出す。

    旧キャッシュのコマ（pick_timecodes の 6 枚）は前半に偏り、本編と締めのコマが無いことが
    あった（本番の #2 は 46 秒の動画で 6 枚とも 12 秒以内）。偏りを隠さず見せる。
    """
    scenes = _ordered_scenes(a)
    roles = [r for r, _i in _display_roles(a)]
    usable = [f for f in frames if f.data_uri.startswith("data:image/")]
    gaps = [
        i
        for i, sc in enumerate(scenes)
        if (max(sc.end_sec, sc.start_sec) - sc.start_sec >= _GAP_MIN_SEC or i == len(scenes) - 1)
        and _uncovered(sc, usable)
    ]
    # 最後の場面と長い場面を優先して、枠の 3 分の 1 まで（縦は 2 枠・横長は 1 枠。コマを優先する）。
    gaps = sorted(
        gaps,
        key=lambda i: (i != len(scenes) - 1, -(scenes[i].end_sec - scenes[i].start_sec)),
    )[: max(1, limit // 3)]
    picked = pick_scene_frames(a, frames, limit - len(gaps), exclude=exclude)
    slots = [FrameSlot(f.sec, f.sec, role, f) for f, role in picked]
    slots += [
        FrameSlot(scenes[i].start_sec, max(scenes[i].end_sec, scenes[i].start_sec), roles[i], None)
        for i in gaps
    ]
    return sorted(slots, key=lambda s: (s.start, s.frame is None))


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


def _product(d: Deck, f: VideoFacts, a: VideoVSEOAnalysis) -> tuple[str, str]:
    """（欄の名前, 文）。商品と呼ぶのは名簿（クライアント・競合）かカテゴリで分かったものだけ。"""
    named = [b for b in f.brands if b.name != _PLACEHOLDER_LOGO]
    if category_known(d.ctx.facts):
        label = "商品の見せ方"
        cands = product_brands(f)
        if not cands:
            seen = "・".join(b.name for b in named[:2])
            return label, "クライアント・競合の商品は映らない" + (
                f"（映るのは{seen}）" if seen else ""
            )
    else:
        label = "目立つ映り込み（カテゴリ未判定）"
        cands = visible_brands(f)
        if not cands:
            seen = "・".join(b.name for b in named[:2])
            return label, "主役・目立つ大きさで映るブランドは無い" + (
                f"（付随・背景に{seen}）" if seen else ""
            )
    pick = cands[0]
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
    if len(named) > 1:
        parts.append(f"ほか{len(named) - 1}件")
    return label, "・".join(parts)


def _close_cta(f: VideoFacts, a: VideoVSEOAnalysis) -> str:
    parts: list[str] = []
    dropped = "・".join(CTA_KIND_LABEL.get(k, k) for k in f.cta_dropped)
    if f.cta_in_video is not None:
        kind, text, sec = f.cta_in_video
        when = f"{sec:g}秒" if sec is not None else "秒不明"
        quote = f"「{text}」" if text else "（文言なし）"
        src = "・テロップから検出" if f.cta_source == "telop" else ""
        parts.append(f"{when}{quote}（{CTA_KIND_LABEL.get(kind, kind)}{src}）")
        if dropped:
            parts.append(f"AIの申告（{dropped}）は文言も秒も無く除外")
    else:
        parts.append(
            f"動画内の呼びかけなし（AIの申告（{dropped}）は文言も秒も無く除外）"
            if dropped
            else "動画内のCTAなし"
        )
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


def _why_top(d: Deck, f: VideoFacts, note: PerVideoNote | None) -> list[str]:
    """なぜ上位か（事実の 1 文と「推測:」の 1 文を別の行に。無ければコードの順位の事実）。"""
    if note is not None and (note.why_fact or note.why_guess):
        return [t for t in (note.why_fact, note.why_guess) if t]
    facts = list(d.ctx.facts)
    plays = [float(x.plays) for x in facts]
    saves = [x.save_rate for x in facts]
    shares = [float(x.shares) for x in facts]
    return [
        f"{len(facts)}本中 再生{_rank_pos(plays, float(f.plays))}位・保存率"
        f"{_rank_pos(saves, f.save_rate)}位・シェア{_rank_pos(shares, float(f.shares))}位（事実）"
    ]


def _caption_facts(d: Deck, f: VideoFacts) -> str:
    parts: list[str] = []
    for term in query_terms(d.out.query):
        cap = f.has_kw(term, "caption", "exact")
        tag = f.has_kw(term, "hashtag", "exact")
        parts.append(f"「{term}」{'あり' if cap else 'なし'}・#{term} {'あり' if tag else 'なし'}")
    parts.append(f"分量{'あり' if f.qty_in_caption else 'なし'}（{len(f.desc)}字）")
    return "・".join(parts)


def _grades_line(grades: Sequence[Grade], f: VideoFacts, *, known: bool) -> str:
    """評価の 1 行（記号と軸の説明は S6 に 1 回だけ出す）。

    CTA は事実層に合わせる（文言も秒も無い型だけの申告は無効＝△）。商品は、名簿かカテゴリで
    商品と分かったものだけで評価し（無ければ—）、分からなければ「映り込み」と呼ぶ。
    """
    marks = []
    for g in grades:
        mark = g.mark
        name = _AXIS_SHORT.get(g.axis, g.axis)
        if g.axis == "CTA" and f.cta_in_video is None:
            mark = MARK_WEAK
        if g.axis == "商品の見せ方":
            if not known:
                name = "映り込み"
            elif not product_brands(f):
                mark = MARK_NONE
        marks.append(f"{name}{mark}")
    return "　".join(marks)


def _spoken_lines(f: VideoFacts) -> list[str]:
    """声に出た検索語（完全一致と言い換えを分けた行・AI 聞き取り）。"""
    exact = [h for h in f.kw if h.layer == "speech" and h.match == "exact"]
    syn = [
        h
        for h in f.kw
        if h.layer == "speech" and h.match == "synonym" and not f.has_kw(h.term, "speech", "exact")
    ]

    def secs(h: KwHit) -> str:
        return f" {'・'.join(f'{s:g}' for s in h.secs)}秒" if h.secs else ""

    lines: list[str] = []
    if exact:
        lines.append("発話の検索語（AI）: " + "・".join(f"{h.term}{secs(h)}" for h in exact))
    if syn:
        lines.append("言い換えだけ（AI）: " + "・".join(f"{h.term}{secs(h)}" for h in syn))
    return lines or ["検索語の発話なし（AI聞き取り）"]


def _slot_html(slot: FrameSlot) -> str:
    if slot.frame is not None:
        fr = slot.frame
        return (
            f"<figure>{_img(fr, f'{_mmss(fr.sec)} {_role(slot.role)}')}"
            f'<figcaption class="one">{_esc(_mmss(fr.sec))}｜{_esc(_role(slot.role))}'
            "</figcaption></figure>"
        )
    return (
        f'<figure><div class="gap">コマ未取得<br>{slot.start:g}〜{slot.end:g}秒</div>'
        f'<figcaption class="one">{_esc(_mmss(slot.start))}｜{_esc(_role(slot.role))}'
        "</figcaption></figure>"
    )


def _breakdown(d: Deck, v: AnalyzedVideo, f: VideoFacts) -> str:
    a = v.analysis
    assert a is not None
    land = f.orientation == "landscape"
    note = d.note(f.rank)
    frames = d.frames(f.rank)
    known = category_known(d.ctx.facts)
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
    flow = [_role(r) for r in dict.fromkeys(r for r, _i in _display_roles(a))][1:]
    fallback = f"{_hook(f.hook_type)}のフックから" + ("→".join(flow) + "へ" if flow else "本編へ")
    title = note.win_line if note is not None and note.win_line else fallback
    first_frame = ref_frame(frames, 0.8)
    axis = _axis_len(f, a)
    bar_px = SLIDE_W - 64 - (302 if land else 278)
    marks = _markers(d, f)
    inferred = any(i for _r, i in infer_roles(a))
    limit = _FRAMES_LANDSCAPE if land else _FRAMES_PORTRAIT
    slots = frame_slots(a, frames, limit, exclude=first_frame)
    figs = "".join(_slot_html(s) for s in slots) or '<div class="sm mut">抜いたコマが無い</div>'
    opening = f.opening_telops[:3]
    line_cls = "c2" if len(opening) <= 2 else "c1"  # 3 つあれば 1 行ずつ（箱の高さに収める）
    opening_lines = (
        "".join(f'<p class="{line_cls}">{s:g}秒「{_esc(t)}」</p>' for s, t in opening)
        or '<p class="c1">0〜3秒のテロップなし</p>'
    )
    narration = "語りあり" if f.narration else "語りなし"
    hook_box = (
        '<div class="hook3 box"><div class="h">冒頭3秒</div>'
        f"{opening_lines}"
        f'<p class="c2">フック: {_esc(_hook(f.hook_type))}・{narration}</p>'
        + "".join(f'<p class="c2">{_esc(t)}</p>' for t in _spoken_lines(f))
        + "</div>"
    )
    steal = note.steal if note is not None else []
    product_label, product_text = _product(d, f, a)
    why = _why_top(d, f, note)
    facts_cells: list[tuple[str, list[str]]] = [
        ("テロップ設計", [_telop_design(f, a)]),
        (product_label, [product_text]),
        ("締め・CTA", [_close_cta(f, a)]),
        ("保存の仕掛け", [_save_device(f)]),
        ("なぜ上位か" + ("（事実｜推測）" if note is not None and note.why_guess else ""), why),
        (
            ("盗める点", [f"{'①②'[i]}{t}" for i, t in enumerate(steal[:2])])
            if steal
            else ("キャプション", [_caption_facts(d, f)])
        ),
    ]
    whole = "c5" if land else "c4"
    part = "c2"
    cells = "".join(
        f'<div><div class="fl">{_esc(k)}</div>'
        + "".join(
            f'<p class="{whole if len(lines) == 1 else part}" contenteditable>{_esc(t)}</p>'
            for t in lines
        )
        + "</div>"
        for k, lines in facts_cells
    )
    grades = grade_video(v, query=d.out.query, roster=d.roster)
    grade_line = f"評価（コードの基準・記号の意味は{d.n}本の構成比較に）: " + _grades_line(
        grades, f, known=known
    )
    return (
        '<div class="krow">'
        f'<div class="kicker">構成分解 #{f.rank} / {d.n}</div>'
        f'<div class="kmeta"><span class="one">{_esc(meta)}</span>{_pr_badge(f)}</div></div>'
        f'<h2 class="ttl one" contenteditable>{_esc(title)}</h2>'
        f'<div class="open">{_img(first_frame, "冒頭のコマ")}'
        f'<div class="cap one">{_esc(_mmss(first_frame.sec, tenths=True)) if first_frame else "—"}'
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
    """代表例 1 つ。コマは引用の秒の直後（0.5 秒前〜2 秒後）にあるときだけ、自分の秒を添えて出す。"""
    f = d.ctx.fact(rank)
    land = f is not None and f.orientation == "landscape"
    shot = quote_frame(d.frames(rank), sec) if frame else None
    fig = (
        f'<figure class="rf">{_img(shot, f"#{rank} {shot.sec:g}秒")}'
        f"<figcaption>{round(shot.sec)}秒</figcaption></figure>"
        if shot is not None
        else ""
    )
    return (
        f'<div class="ex{" land" if land and fig else ""}">{fig}'
        f'<p class="{"c4" if fig else "c2"}">#{rank} {sec:g}秒「{_esc(text)}」</p></div>'
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
    head_note = "（役割は推定）" if row.roles_inferred else ""
    events = ""
    # 本数の多い出来事から 2 つ（段の順は EVENT_LABEL の順）。
    for key, ranks, _t in sorted(row.events, key=lambda e: -len(e[1]))[:_STAGE_EVENTS]:
        label = EVENT_LABEL[key]
        if key == "brand_seen":  # カテゴリ未判定の映り込みには段階の名前を付けない
            count = f"{len(ranks)}/{n}（{ranks_text(ranks)}）"
        else:
            count = tier_text(ranks, n)
        events += f'<p class="c2">{_esc(label)} {_esc(count)}</p>'
    extra = _stage_extra(d, row)
    extra_html = f'<p class="c2">{_esc(extra)}</p>' if extra else ""
    ex = row.examples
    # 代表例は、引用の秒の直後にコマがある例を先に使う（コマが無ければ文だけ）。
    with_frame = [e for e in ex if quote_frame(d.frames(e[0]), e[1]) is not None]
    chosen = (with_frame or list(ex))[:1]
    examples = "".join(_example(d, *e, frame=True) for e in chosen)
    return (
        f'<div class="stage"><div class="h one">{_esc(row.label)}'
        f'<span class="sm mut">{_esc(head_note)}</span></div>'
        # 役割が推定（最初の場面＝フック・ほかは本編）のときは、推定の規則から必ず出る値なので
        # 役割の本数を観測として出さない（帯に 1 行で断る）。
        + (
            f'<div class="lbl">主な役割</div>{roles or "<p>—</p>"}'
            if not row.roles_inferred
            else ""
        )
        + f'<div class="lbl">出来事</div>{events or "<p>—</p>"}{extra_html}'
        f'<div class="lbl">代表例（再生の多い動画）</div>{examples or "<p>—</p>"}</div>'
    )


def _kw_cell(r: KwRow) -> str:
    text = f"{len(r.exact)}/{r.n}"
    if r.synonym and r.layer in ("telop", "speech"):
        # 言い換え（テロップは照合済み・発話は AI 聞き取り）は完全一致と分けて書く
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
        + (
            '<div class="band one">場面の役割は AI の推定（最初の場面＝フック・CTA の秒の場面＝CTA・'
            "ほかは本編）のため、段ごとの役割の本数は出さない</div>"
            if any(r.roles_inferred for r in rows)
            else ""
        )
        + f'<div class="stages">{cols}</div>'
        '<div class="tplfoot"><div class="h one">拾われる条件（語×層・テロップは本文に実在する'
        "ものだけ・キャプションとハッシュタグは上位ボード全体の本数を併記）</div>"
        f'<table class="kw"><colgroup><col style="width:130px"></colgroup><thead><tr>{head}</tr>'
        f"</thead><tbody>{body}</tbody></table></div>"
    )


# ── クリエイティブ指示／やらないこと ────────────────────────────────────────


def _ref_frame(d: Deck, ref: SynthRef) -> FrameShot | None:
    """根拠の横に添えるコマ（根拠の秒の 0.5 秒前〜2 秒後にあるものだけ。無ければ出さない）。"""
    if ref.source == "caption" or ref.source in COVER_SOURCES:
        return None
    sec = ref.found_sec if ref.found_sec is not None else ref.sec
    return quote_frame(d.frames(ref.rank), sec)


def _ref_fig(frame: FrameShot | None, alt: str) -> str:
    """小さなコマとその秒（引用の図ではなく、その秒のコマだと分かるように）。無ければ空。"""
    if frame is None:
        return ""
    return (
        f'<figure class="rf">{_img(frame, alt)}'
        f"<figcaption>{round(frame.sec)}秒</figcaption></figure>"
    )


def _evidence(refs: Sequence[SynthRef]) -> str:
    return "／".join(evidence_text(r) for r in refs[:2])


def _directives(d: Deck) -> str:
    if d.n == 0:
        return ""
    dirs = list(d.syn.directives) if d.syn is not None else code_directives(d.ctx)
    avoid = list(d.syn.avoid) if d.syn is not None else []
    given = list(d.out.avoid_terms)
    if not dirs and not avoid and not given:
        return ""
    rows = ""
    for item in dirs[:6]:
        frame = _ref_frame(d, item.refs[0]) if item.refs else None
        who = "コードの集計" if item.origin == "code" else "AI の指示（根拠は照合済み）"
        kind = f"{item.kind}｜" if item.kind else ""
        rows += (
            f'<div class="drow">{_ref_fig(frame, "根拠の秒のコマ")}<div style="min-width:0">'
            f'<div class="sm one">{_tier_badge(item.ranks, d.n)} '
            f'<span class="mut">{_esc(kind + who)}</span></div>'
            f'<div class="dt c3" contenteditable>{_esc(item.text)}</div>'
            f'<div class="ev c2">根拠 {_esc(_evidence(item.refs) or "—")}</div></div></div>'
        )
    items = "".join(
        f'<li><div class="at c2" contenteditable>{_esc(a.text)}</div>'
        f'<div class="ev c2">{_esc(a.reason or ("根拠 " + _evidence(a.refs)))}</div></li>'
        for a in avoid[:4]
    )
    given_html = (
        f'<div class="given c2">ご指定の避けたい訴求: {_esc("、".join(given))}'
        "（該当する指示・絵コンテは除外済み）</div>"
        if given
        else ""
    )
    if not items and not given_html:
        items = '<li><div class="ev">該当なし</div></li>'
    note = (
        '<div class="note c2">根拠の横のコマは、根拠の秒の直後に抜いたもの（引用のテロップが'
        "写っているとは限らない）。</div>"
        if any(_ref_frame(d, x.refs[0]) for x in dirs[:6] if x.refs)
        else ""
    )
    if d.syn is None:
        note += '<div class="note">AI の指示は無いため、コードが集計した事実の指示だけを出しています。</div>'
    wide = " wide" if len(avoid[:4]) <= 1 else ""
    return (
        '<div class="kicker">クリエイティブ指示（根拠つき）／やらないこと</div>'
        '<h2 class="slide-title one" contenteditable>撮る前に決めること</h2>'
        f'<div class="dircols{wide}">'
        f'<div><div class="dgrid{" two" if len(dirs[:6]) > 3 else ""}">{rows}</div>{note}</div>'
        f'<div class="avoid"><div class="h">やらないこと</div>{given_html}<ul>{items}</ul></div>'
        "</div>"
    )


# ── 絵コンテ ────────────────────────────────────────────────────────────


def _stage_hints(d: Deck) -> dict[str, str]:
    """段ごとの多数派以上の出来事（絵コンテの空いたカットに添える・コードの集計）。

    2 本以下の出来事は「型」と呼ばない（R3）ので添えない。カテゴリ未判定の映り込みも添えない。
    """
    hints: dict[str, str] = {}
    for row in template(list(d.ctx.videos), list(d.ctx.facts)):
        top = [
            e
            for e in sorted(row.events, key=lambda e: -len(e[1]))
            if e[0] != "brand_seen" and at_least_majority(e[2])
        ][:1]
        if top:
            key, ranks, _t = top[0]
            hints[row.label] = f"{EVENT_LABEL[key]} {tier_text(ranks, d.n)}"
    return hints


def _storyboard(d: Deck, sb: Storyboard, label: str) -> str:
    plan = {c.cut: c for c in d.ctx.cuts}
    cuts = {c.cut: c for c in sb.cuts}
    order = sorted(set(plan) | set(cuts))
    hints = _stage_hints(d)
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
        full = (cut.stage if cut is not None and cut.stage else "") or (slot.stage if slot else "")
        stage = _STAGE_SHORT.get(full, full)
        when = f"{start:g}〜{end:g}秒" if start is not None and end is not None else "—"
        if cut is None:
            hint = hints.get(full, "")
            fill = f"（案なし）この段の多数派: {hint}（コードの集計）" if hint else "（案なし）"
            body += (
                f"<tr><td>{k}</td><td>{_esc(when)}<div class='mut'>{_esc(stage)}</div></td>"
                f'<td colspan="4" class="mut">{_esc(fill)}</td></tr>'
            )
            continue
        ref = cut.refs[0] if cut.refs else None
        refs = (
            f'<div class="ref">{_ref_fig(_ref_frame(d, ref), "参考の秒のコマ")}'
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
        f'<div class="band c2">{_esc(sub)}</div>'
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
    facts_html = "".join(f'<p class="c2">{_esc(t)}</p>' for t in lines[:5])
    marked = [f.rank for f in d.ctx.facts if f.pr_marked]
    pr_line = "依頼して投稿する場合は、キャプションに #PR とブランドの @ を付ける（ステマ規制）" + (
        f"。上位のタイアップ {ranks_text(marked)} も表記あり" if marked else ""
    )
    plan = d.syn.posting.caption_plan if d.syn is not None and d.syn.posting else ""
    plan_html = f'<p class="c3" contenteditable><b>案:</b> {_esc(plan)}</p>' if plan else ""
    saves = [f.save_rate for f in d.ctx.facts if f.plays > 0]
    basis = (
        f"（目安: 上位{n}本の保存率の中央値{statistics.median(saves):.2f}%・最大{max(saves):.2f}%）"
        if saves
        else ""
    )
    verify = "".join(
        f'<p class="c3" contenteditable>{_esc(line.format(basis=basis))}</p>'
        for line in _VERIFY_LINES
    )
    hyps = ""
    for h in (d.syn.hypotheses if d.syn is not None else [])[:_SLIDE_HYPOTHESES]:
        test = f"検証: {h.test}" if h.test else ""
        hyps += (
            f'<p class="c2" contenteditable><b>{_esc(h.text)}</b></p>'
            f'<p class="sm mut c2">{_esc(tier_text(h.ranks, n))}'
            + (f"・{_esc(h.metric_note)}" if h.metric_note else "")
            + "</p>"
            + (f'<p class="sm c2" contenteditable>{_esc(test)}</p>' if test else "")
        )
    ab = d.syn.posting.ab_plan if d.syn is not None and d.syn.posting else ""
    if ab:
        hyps += f'<p class="c2" contenteditable>A/B: {_esc(ab)}</p>'
    order = "".join(
        f"<dt>{_esc(k)}</dt><dd contenteditable>"
        + (f'<span class="mut">{_esc(_SUCCESS_PLACEHOLDER)}</span>' if k == _SUCCESS_FIELD else "")
        + "</dd>"
        for k in _ORDER_FIELDS
    )
    return (
        '<div class="kicker">投稿設計と検証</div>'
        '<h2 class="slide-title one" contenteditable>撮る・投稿する・確かめる</h2>'
        '<div class="cols3">'
        f'<div class="box"><div class="h">キャプション設計（事実）</div>{facts_html}'
        f'<p class="c3">{_esc(pr_line)}</p>{plan_html}</div>'
        f'<div class="box"><div class="h">検証のしかた</div>{verify}{hyps}</div>'
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
        ("thumb-compare", _thumb_compare(d), ""),
        ("thumb-plan", _thumb_plan(d), ""),
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
