"""まとめ軸クラスタリング JS の **実行**テスト（文字列 assert ではなく挙動を検証）。

build_app_html.py の埋め込み JS から該当関数を実ソース行として抽出し、Node で実行して
数値を assert する。`assert "<実装文字列>" in html` 形式のテストは、実装が最初から
間違っていてもそのまま緑になる（＝回帰を検知できない）ため、以下の契約はここで固定する:

- 幾何（島の順序・角度・半径）は**全ノード**から決まり、絞り込みで**動かない**
- 件数だけが可視基準（recount）
- CLMAX で溢れた値のノードは**無言で消えず**「その他」島に集約される
- 値が "constructor"/"__proto__" でも島が壊れない（プロトタイプ無し辞書）

node が無い環境では skip する。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BUILD_PY = PROJECT_ROOT / "scripts" / "build_app_html.py"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node が無い環境では skip")


def _extract_js() -> str:
    """build_app_html.py の埋め込み JS から、まとめ軸の実装行をそのまま抜き出す。

    行の**実物**を使うので、実装を書き換えればこのテストも当然影響を受ける（＝変異が効く）。
    """
    src = BUILD_PY.read_text(encoding="utf-8")
    wanted = (
        r"^ const CLBASE=.*$",
        r"^ const CLMAX=.*$",
        r"^ const COTHER=.*$",
        r"^ let clOther=.*$",
        r"^ const AGEBK=.*$",
        r"^ function grpVal\(n,ax\)\{[\s\S]*?\n  return null;\}$",
        r"^ function grp\(n\)\{.*$",
        r"^ function buildCenters\(ax\)\{[\s\S]*?\n  recount\(\);\}$",
        r"^ function grpBin\(n\)\{[\s\S]*?\n  const g=grp\(n\);return \(g!==null&&clOther\.has\(g\)\)\?COTHER:g;\}$",
        r"^ function recount\(\)\{[\s\S]*?cCenters\[g\]\)cCenters\[g\]\.n\+\+;\}\}$",
        r"^ function clusterCap\(\)\{[\s\S]*?\n  return s;\}$",
    )
    out = ["let cCenters=Object.create(null);"]
    for pat in wanted:
        m = re.search(pat, src, re.M)
        assert m, f"JS 抽出に失敗（実装が変わった可能性）: {pat}"
        out.append(m.group(0))
    return "\n".join(out)


_HARNESS = """
// --- テスト用スタブ（本体の外部依存だけを最小に用意）---
const PHASECOLOR = {"ケイパ":"#1","ヒアリング":"#2","提案":"#3"};
let opt = {cluster:null, filter:"", showDocs:true, showTags:true, hideOrphan:false};
let N = [], neigh = [];
const cByStem = Object.create(null), dByStem = Object.create(null);
function ageBucket(){ return ""; }
function vis(i){const n=N[i];if(!opt.showDocs&&n.type==="doc")return false;if(!opt.showTags&&n.type==="tag")return false;if(opt.filter&&!n.label.toLowerCase().includes(opt.filter))return false;return true;}
function esc(value){ return String(value); }
__IMPL__
// --- シナリオ ---
function mkDocs(specs){ // specs: [[solution, label], ...]
  N = specs.map((s,i)=>({id:"d:"+i, type:"doc", label:s[1], x:0, y:0, vx:0, vy:0, r:2}));
  specs.forEach((s,i)=>{ dByStem[String(i)] = {solution:s[0]}; N[i].id = "d:"+i; });
  neigh = N.map(()=>new Set());
}
const out = {};
const specs = [];
// 20 種の施策 × それぞれ件数を変えて投入（CLMAX=12 を確実に超える）
for (let k=0;k<20;k++){ const n = 20-k; for(let j=0;j<n;j++) specs.push(["施策"+String(k).padStart(2,"0"), "資料"+specs.length]); }
mkDocs(specs);
opt.cluster = "solution";
buildCenters("solution");
out.totalDocs = N.length;
out.islandKeys = Object.keys(cCenters);
out.islandCount = out.islandKeys.length;
out.sumIslandCounts = Object.values(cCenters).reduce((s,c)=>s+c.n,0);
out.otherCount = cCenters["その他"] ? cCenters["その他"].n : 0;
out.solutionOverflowCaption = clOther.size > 0 && clusterCap().includes("残り"+clOther.size+"種");
// 幾何スナップショット（絞り込み前）
const geomBefore = Object.fromEntries(Object.entries(cCenters).map(([k,c])=>[k,[c.x,c.y]]));
// 絞り込み → recount のみ
opt.filter = "資料1";
recount();
const geomAfter = Object.fromEntries(Object.entries(cCenters).map(([k,c])=>[k,[c.x,c.y]]));
out.geomMoved = JSON.stringify(geomBefore) !== JSON.stringify(geomAfter);
out.sumAfterFilter = Object.values(cCenters).reduce((s,c)=>s+c.n,0);
out.visibleAfterFilter = N.filter((_,i)=>vis(i)).length;
// 幾何が**全ノード基準**であることの直接検証: 絞り込んだ状態で軸を組み直しても同じ幾何になる
// （半径や順序を可視数に依存させると、ここで座標がズレる）
buildCenters("solution");
const geomRebuiltUnderFilter = Object.fromEntries(Object.entries(cCenters).map(([k,c])=>[k,[c.x,c.y]]));
out.geomDependsOnVisible = JSON.stringify(geomBefore) !== JSON.stringify(geomRebuiltUnderFilter);
// プロトタイプ汚染
opt.filter = "";
mkDocs([["constructor","a"],["__proto__","b"],["toString","c"],["施策X","d"]]);
buildCenters("solution");
out.protoIslands = Object.keys(cCenters).sort();
out.protoSum = Object.values(cCenters).reduce((s,c)=>s+(typeof c.n==="number"?c.n:-999),0);
// phase の正式値「その他」は overflow 用の同名島ではない。
N = [
  {id:"c:0",type:"client",label:"a",phase:"その他",x:0,y:0,vx:0,vy:0,r:2},
  {id:"c:1",type:"client",label:"b",phase:"ケイパ",x:0,y:0,vx:0,vy:0,r:2},
];
neigh = N.map(()=>new Set());
opt.cluster = "phase";
buildCenters("phase");
out.phaseOtherCount = cCenters["その他"] ? cCenters["その他"].n : 0;
out.phaseOverflowSize = clOther.size;
out.phaseCaptionMentionsOverflow = clusterCap().includes("種類が多いため");
console.log(JSON.stringify(out));
"""


def _run_js() -> dict:
    js = _HARNESS.replace("__IMPL__", _extract_js())
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"node 実行失敗:\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_clmax_overflow_goes_to_other_island_not_silently_dropped() -> None:
    """CLMAX で溢れた値のノードは「その他」島へ集約され、**1件も消えない**。

    slice で切るだけだと cCenters に無い→grp()→gc=null→原点（未所属と同じ場所）へ落ち、
    ラベルも件数も出ずに無言で消える。実データの solution は canonical 12種＋自由記述素通しで
    確実に溢れるため、この契約を実行で固定する。
    """
    o = _run_js()
    assert o["islandCount"] <= 12  # CLMAX
    assert "その他" in o["islandKeys"]
    assert o["otherCount"] > 0
    assert o["solutionOverflowCaption"] is True
    # 全ノードがいずれかの島に属する＝取りこぼしゼロ
    assert o["sumIslandCounts"] == o["totalDocs"]


def test_legitimate_phase_named_other_does_not_claim_overflow() -> None:
    """フェーズの正式値「その他」を、自由記述軸の溢れ集約と誤認しない。"""
    o = _run_js()
    assert o["phaseOtherCount"] == 1
    assert o["phaseOverflowSize"] == 0
    assert o["phaseCaptionMentionsOverflow"] is False


def test_geometry_is_stable_under_filtering_and_counts_follow() -> None:
    """絞り込みで島の幾何は**動かず**、件数だけが可視基準で追随する。

    可視集合に幾何（順序・角度・半径）まで連動させると、1 キーストロークごとに島が数百 px
    移動する体験回帰になる（実測で最大 1677px 飛んだ）。幾何は全ノードから 1 回だけ決める。
    """
    o = _run_js()
    assert o["geomMoved"] is False  # recount では島は 1px も動かない
    assert o["sumAfterFilter"] == o["visibleAfterFilter"]  # 件数は可視集合と一致
    # 幾何が全ノード基準である直接証拠: 絞り込み中に組み直しても座標が変わらない
    assert o["geomDependsOnVisible"] is False


def test_prototype_polluting_values_form_real_islands() -> None:
    """値が constructor/__proto__/toString でも島が成立し、件数が数値のまま壊れない。"""
    o = _run_js()
    for v in ("constructor", "__proto__", "toString", "施策X"):
        assert v in o["protoIslands"], v
    assert o["protoSum"] == 4  # 4 ノードが 4 島に 1 件ずつ（-999 が混ざれば壊れている）
