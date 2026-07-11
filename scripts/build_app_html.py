#!/usr/bin/env python3
"""connect-web「Obsidian完全再現」app.html 生成器（repo 版 v6）。

~/Documents/Claude/Artifacts/connect-web-obsidian_build.py v6 の repo 取り込み。
一次情報(docs.obsidian.md CSS変数)に基づく正確なトークン + Lucideアイコン + macOS枠 +
ペイン化サイドバー(ファイル/検索(演算子)/タグ(ネスト)/ブックマーク) + Properties新UI +
インラインタイトル + リーディングビュー + リンクされた言及 + グラフ。機密は端末外に出さない。

フィルタ4機構（exclude_stems / dedup_drop_map / 分割断片折り畳み / weird_rename）と
HTML 生成ロジックは元スクリプトと同一。repo 版で加えたのは運用ガードのみ:

- argparse: ``--vault`` / ``--out`` / ``--allow-shrink``
- サイドカーは repo の ``data/connect_web_filters/`` から読む（``__file__`` 相対）。
  exists() フォールバックは全廃: サイドカー欠落・Vault 不在・clients==0・docs==0 は
  理由を明示して exit 1（「黙って劣化」を作らない）
- サニティゲート: 統計を ``<out>.stats.json`` に保存し、取引先数/資料数/バイト数の
  いずれかが前回比 20% 超減なら ``--allow-shrink`` 無しで exit 1（既存 out は上書きしない）
- フッタ焼き込み: ステータスバー末尾に「更新: YYYY-MM-DD JST・取引先N・資料M」を表示
  （利用 16 名にデータ鮮度が見える）
- PII 決定論除外: 正規化後 stem に「請求」を含む資料（請求書/請求金額系）はサイドカーに
  個人名入り stem を列挙せずルールで除外（_is_excluded()。exclude_stems.json の平文 PII 廃止）

Usage:
    python scripts/build_app_html.py                          # ~/AiLaVault → 既定 out
    python scripts/build_app_html.py --vault ~/AiLaVault --out /tmp/app.html
    python scripts/build_app_html.py --allow-shrink           # 意図した縮小のとき
"""

# 元スクリプトからの行単位コピー（フィルタ・HTML 生成）を byte 不変に保つため、
# フォーマッタと一部リントをファイル単位で抑止する（コピー部の同一性は元ファイルとの diff で担保）。
# ruff: noqa: E701, E731, E741, N806, UP017
# fmt: off
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- repo 内サイドカー（フィルタ3ファイル + フォント）。欠落は即 exit 1（fallback 禁止） ---
_REPO_ROOT = Path(__file__).resolve().parents[1]
SIDECAR_DIR = _REPO_ROOT / "data" / "connect_web_filters"
SIDECAR_FILES = (
    "exclude_stems.json",      # Agent 分類の非ナレッジ除外（タイトル stem のみ）
    "dedup_drop_map.json",     # 別形式/旧版の非正本折り畳み（stem → 正本 stem）
    "weird_rename_high.json",  # 不明瞭命名 → 推奨タイトル（表示のみ・可逆）
    "inter-var.b64",           # InterVar フォント（woff2 base64）
)

DEFAULT_VAULT = Path.home() / "AiLaVault"
DEFAULT_OUT = Path.home() / "Documents" / "Claude" / "Artifacts" / "connect-web-obsidian-preview.html"

JST = timezone(timedelta(hours=9))
SHRINK_LIMIT = 0.20  # サニティゲート: 前回比これを超える減少で停止

# main() が argparse から設定するモジュールグローバル
# （client_md/_compute_chunk_drop 等の helper が参照するため module scope に置く）
VAULT = DEFAULT_VAULT
CLIENTS, DOCS, REPORTS = VAULT / "clients", VAULT / "docs", VAULT / "_reports"

CORP = ["一般社団法人", "公益社団法人", "株式会社", "株式會社", "有限会社", "合同会社", "合資会社"]
CORP_S = ["(株)", "（株）", "㈱", "(有)", "（有）", "㈲"]
HONOR = ["御中", "様", "さま", "殿"]


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").strip()
    for c in CORP + CORP_S + HONOR:
        s = s.replace(c, "")
    return s.replace(" ", "").replace("　", "").replace("・", "").strip().lower()


def front(text):
    m = re.search(r"^---\n(.*?)\n---", text, re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            mm = re.match(r'^(\w+):\s*"?(.*?)"?\s*$', line)
            if mm:
                fm[mm.group(1)] = mm.group(2)
    return fm


def body_of(text):
    return re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S)


WL_RE = re.compile(r"\[\[([^\]]+?)\]\]")


def parse_links(body):
    """本文中の [[target]] を (target, 行コンテキスト) で列挙（実バックリンク用）。"""
    out = []
    for line in body.split("\n"):
        if "[[" not in line:
            continue
        ctx = re.sub(r"\s+", " ", line).strip(" -*>\t")[:130]
        for mm in WL_RE.finditer(line):
            out.append((mm.group(1).split("|")[0].strip(), ctx))
    return out


def to_int(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


def prettify_fb(md):
    def rep(m):
        raw = m.group(1)
        tsm = re.search(r"(1[0-9]{9})(?:\.\d+)?\s*$", raw)
        head = re.sub(r"-{2,}", "", raw).strip(" -")
        if tsm:
            d = datetime.fromtimestamp(int(tsm.group(1)), timezone.utc).strftime("%Y-%m-%d")
            head = re.sub(r"(1[0-9]{9})(?:\.\d+)?\s*$", "", head).strip(" -")
            return f"### 💬 {head}　({d})"
        return f"### {head}"
    return re.sub(r"^###\s*(.+)$", rep, md, flags=re.M)


def client_md(text, limit=2600):
    # 本文丸ごと（タイトル+タグ+FB+関連資料）。ただし関連資料の非ナレッジ行は除去。
    b = prettify_fb(body_of(text).strip())

    def _keep(ln):
        m = re.match(r"^- \[\[docs/(.+?)\]\]", ln)
        return not (m and _is_excluded(m.group(1)))

    return _strip_self_tags("\n".join(l for l in b.split("\n") if _keep(l)))[:limit]


def doc_md(text, limit=900):
    return _strip_self_tags(body_of(text).strip())[:limit]


def bant_short(b):
    if not b:
        return ""
    m = re.match(r"^([A-D])", b)
    return m.group(1) if m else b[:1]


# === 施策タイムライン: 営業FB時系列（### ---- 見出し区切り）のパース ===
FB_HEAD_RE = re.compile(r"^###\s*-{2,}\s*(.*)$", re.M)
FB_EPOCH_RE = re.compile(r"(1[0-9]{9})(?:\.\d+)?\s*$")
FB_DATE_RE = re.compile(r"^>\s*\[(\d{4}-\d{2}-\d{2})[^\]]*\]", re.M)
FB_FIELD_RE = re.compile(
    r"^-\s*(フェーズ|BANT|ポジ反応|ネガ反応|次アクション|提案メニュー)\s*[:：]\s*(.*)$"
)
FB_FIELD_MAP = {
    "フェーズ": ("ph", 80), "BANT": ("bant", 80), "提案メニュー": ("menu", 60),
    # pos/neg は当初 160 → 実 Vault 計測で HTML 増分が +402KB と +400KB を超えたため 120 に抑制
    "ポジ反応": ("pos", 120), "ネガ反応": ("neg", 120), "次アクション": ("next", 120),
}
FB_MAX_EVENTS = 30


def _fb_dedup_key(ev):
    """Slack/フォーム二重登録の同定キー: 正規化した (ポジ+ネガ)[:120]。空は dedup 対象外。"""
    raw = unicodedata.normalize("NFKC", (ev.get("pos", "") + ev.get("neg", "")))
    return re.sub(r"\s+", "", raw).lower()[:120]


def dedup_fb_events(events):
    """(ポジ+ネガ) が一致する重複 FB を折り畳む。日付を持つ方を正として残す。"""
    out, seen = [], {}
    for ev in events:
        key = _fb_dedup_key(ev)
        if not key:  # ポジ/ネガ両方欠損は同定不能 → 別物として残す（誤結合防止）
            out.append(ev)
            continue
        j = seen.get(key)
        if j is None:
            seen[key] = len(out)
            out.append(ev)
        elif ev.get("d") and not out[j].get("d"):
            out[j] = ev  # 日付を持つ方で置き換え（両方あり/両方なしは先勝ち）
    return out


def _sort_fb_events(events):
    """日付降順（日付なしは末尾）・最大 FB_MAX_EVENTS 件。"""
    events.sort(key=lambda e: e.get("d") or "", reverse=True)
    return events[:FB_MAX_EVENTS]


def parse_fb_events(body: str) -> list[dict]:
    """クライアント md 本文の営業FB時系列をイベント列にパースする（fail-open）。

    見出し `### ---- <ソース名> <slack ts epoch|row N>` で区切り、
    各 FB の `- フェーズ:` 等のフィールド行と日付（`> [YYYY-MM-DD HH:MM]` 行
    または見出し末尾の epoch 秒 → UTC+9）を拾う。壊れた見出し/欠損フィールドは
    空文字で許容し、例外は漏らさない（このパースの失敗で build を止めない）。
    """
    try:
        heads = list(FB_HEAD_RE.finditer(body or ""))
        events = []
        for i, m in enumerate(heads):
            try:
                head = m.group(1).strip()
                end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
                sec = body[m.end():end]
                nx = re.search(r"^#{1,6}\s", sec, re.M)  # 次の見出し（## 関連資料 / 非FBのh3 等）で打ち切り
                if nx:
                    sec = sec[:nx.start()]
                ev = {"d": "", "src": "", "ph": "", "bant": "", "menu": "", "pos": "", "neg": "", "next": ""}
                ev["src"] = "フォーム" if ("フォーム" in head or re.search(r"row\s*\d+\s*$", head)) else "Slack"
                dm = FB_DATE_RE.search(sec)
                if dm:
                    ev["d"] = dm.group(1)
                else:
                    tsm = FB_EPOCH_RE.search(head)
                    if tsm:
                        try:
                            ev["d"] = datetime.fromtimestamp(int(tsm.group(1)), JST).strftime("%Y-%m-%d")
                        except (ValueError, OverflowError, OSError):
                            pass
                for ln in sec.splitlines():
                    fm = FB_FIELD_RE.match(ln.strip())
                    if fm:
                        key, limit = FB_FIELD_MAP[fm.group(1)]
                        if not ev[key]:  # 同名フィールド重複は先勝ち
                            ev[key] = fm.group(2).strip()[:limit]
                events.append(ev)
            except Exception:  # 個別 FB の破損は握り潰して次へ（fail-open）
                continue
        return _sort_fb_events(dedup_fb_events(events))
    except Exception:  # タイムラインはベストエフォート（このパースで build を止めない）
        return []


EXCL: set[str] = set()  # exclude_stems.json（main() で読み込み・欠落は exit 1）
_exn = lambda s: re.sub(r"[\s_]+", "", s).lower()
EXCL_N: set[str] = set()


def _is_excluded(stem):
    # 除外判定: サイドカー列挙（EXCL/EXCL_N）に加え、正規化後 stem に「請求」を含む
    # 請求書/請求金額系タイトルは決定論で除外（個人名PIIの stem を repo に列挙しない）
    return stem in EXCL or _exn(stem) in EXCL_N or "請求" in _exn(stem)

# === 自社(NewsTV)除外: 自社部署を取引先/タグ扱いしない（実クライアントの資料自体は残す） ===
SELF_ORG_RE = re.compile(r"news[\s_\-]*tv", re.I)          # NewsTV / news-tv / 株式会社NewsTV / Vector_NewsTV 等の全変種
SELF_TAG_RE = re.compile(r"#[^\s#]*[Nn]ews[_\-]?[Tt][Vv][^\s#]*")  # #NewsTV / #NewsTV_Network 等のインラインタグ
def _is_self_org(s):
    return bool(s) and bool(SELF_ORG_RE.search(str(s)))
def _strip_self_tags(text):
    return SELF_TAG_RE.sub("", text or "")

# === データ品質: junk除外 / 重複折り畳み / 分割断片集約 / 不明瞭命名リネーム（表示のみ・元Vault不変・可逆） ===
JUNK_CLIENTS = {"テスト", "（テスト）松竹", "VECTOR INC", "vectorinc", "Vector", "Vector Group"}
DOC_DROP: set[str] = set()        # dedup_drop_map.json の drop keys（main() で読み込み）
TITLE_OVERRIDE: dict[str, str] = {}  # weird_rename_high.json（main() で読み込み）
_CHUNK_RE = re.compile(r"_\d{1,2}$")
def _chunk_key(stem):
    k = stem
    while _CHUNK_RE.search(k):
        k = _CHUNK_RE.sub("", k)
    return k
def _compute_chunk_drop():
    # 分割断片(_2/_3…)は同一baseで束ね、代表1件(base優先/最短)のみ残す
    groups = defaultdict(list)
    for f in DOCS.glob("*.md"):
        s = f.stem
        if _is_excluded(s):
            continue
        groups[_chunk_key(s)].append(s)
    drop = set()
    for k, ss in groups.items():
        if len(ss) > 1:
            rep = k if k in ss else sorted(ss, key=lambda x: (len(x), x))[0]
            drop.update(s for s in ss if s != rep)
    return drop
CHUNK_DROP: set[str] = set()  # main() で _compute_chunk_drop() を実行して設定

HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>connect-web — Obsidian</title>
<style>
/*FONTFACE*/
:root{
 /* 一次情報: docs.obsidian.md CSS変数(ライト既定・実物に一致) */
 --b00:#ffffff;--b05:#fcfcfc;--b10:#fafafa;--b20:#f6f6f6;--b25:#e3e3e3;--b30:#e0e0e0;--b35:#d4d4d4;--b40:#bdbdbd;--b50:#ababab;--b60:#707070;--b70:#5a5a5a;--b100:#222222;
 --bg-primary:#ffffff;--bg-sidebar:#f7f7f8;--bg-elev:#f1f1f3;--bg-float:#ffffff;
 --border:#e5e5e7;--border-focus:#d2d2d5;--hover:rgba(0,0,0,.045);--active:rgba(0,0,0,.075);
 --text:#22232a;--muted:#5c5d64;--faint:#9b9ca3;--on-accent:#fff;
 --accent-h:254;--accent-s:70%;--accent-l:60%;
 --accent:hsl(254,72%,62%);--accent-2:hsl(254,55%,50%);--accent-hover:hsl(254,72%,55%);--accent-bg:hsla(254,72%,62%,.13);
 --ok:#2e9e63;--warn:#c98a12;--err:#d24a3f;--mark:#ffe08a;--mark-fg:#5a4600;
 --ui:"InterVar","Inter",-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP","Segoe UI",sans-serif;
 --mono:"Source Code Pro","SF Mono",ui-monospace,Menlo,monospace;
 --r-s:4px;--r-m:8px;--r-l:12px;--ribbon:44px;--side:260px;--right:300px;--line-w:700px;
 --f-ui-smaller:12px;--f-ui-small:13px;--f-ui-med:15px;--f-text:16px;
}
:root[data-theme="dark"]{
 --b00:#1e1e1e;--b05:#212121;--b10:#242424;--b20:#262626;--b25:#2a2a2a;--b30:#363636;--b35:#3f3f3f;--b40:#555;--b50:#666;--b60:#999;--b70:#bababa;--b100:#dadada;
 --bg-primary:#1e1e1e;--bg-sidebar:#181818;--bg-elev:#262626;--bg-float:#2a2a2a;
 --border:#333;--border-focus:#484848;--hover:rgba(255,255,255,.05);--active:rgba(255,255,255,.085);
 --text:#dadada;--muted:#999;--faint:#6a6a6a;
 --accent:hsl(254,74%,70%);--accent-2:hsl(254,72%,78%);--accent-hover:hsl(254,74%,64%);--accent-bg:hsla(254,74%,70%,.2);
 --mark:#5a4a17;--mark-fg:#ffe08a;
}
:root[data-theme="dark"] ::-webkit-scrollbar-thumb{background:#3a3a3a}
:root[data-theme="dark"] ::-webkit-scrollbar-thumb:hover{background:#4a4a4a}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--bg-primary);color:var(--text);font-family:var(--ui);font-size:var(--f-ui-small);-webkit-font-smoothing:antialiased;overflow:hidden;letter-spacing:.005em}
svg.ic{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:1.75;stroke-linecap:round;stroke-linejoin:round;flex:none;vertical-align:middle}
::-webkit-scrollbar{width:12px;height:12px}
::-webkit-scrollbar-thumb{background:#cfcfd3;border:3px solid transparent;background-clip:padding-box;border-radius:8px}
::-webkit-scrollbar-thumb:hover{background:#b6b6bb;background-clip:padding-box}
/* レイアウト */
.app{display:grid;grid-template-columns:var(--ribbon) var(--side) 1fr var(--right);grid-template-rows:100vh;height:100vh;overflow:hidden}
.ribbon{background:var(--bg-sidebar);display:flex;flex-direction:column;align-items:center;gap:3px;padding:10px 0 8px;border-right:1px solid var(--border)}
.ribbon .ri{width:30px;height:30px;display:flex;align-items:center;justify-content:center;color:var(--muted);border-radius:var(--r-s);cursor:pointer}
.ribbon .ri:hover{background:var(--hover);color:var(--text)}
.ribbon .ri.on{color:var(--accent-2);background:var(--accent-bg)}
.ribbon .sp{flex:1}
/* 左サイドバー */
.side{background:var(--bg-sidebar);border-right:1px solid var(--border);display:flex;flex-direction:column;min-width:0;min-height:0}
.side-h{display:flex;align-items:center;height:40px;padding:0 8px 0 12px;gap:4px;flex:none}
.side-h .t{font-size:var(--f-ui-smaller);color:var(--muted);font-weight:600;letter-spacing:.03em;flex:1;text-transform:uppercase}
.side-h .act{width:26px;height:26px;display:flex;align-items:center;justify-content:center;color:var(--muted);border-radius:var(--r-s);cursor:pointer}
.side-h .act:hover{background:var(--hover);color:var(--text)}
.side-body{overflow-y:auto;flex:1;padding:2px 0 20px}
.sfield{margin:2px 10px 8px;position:relative;display:flex;align-items:center}
.sfield svg{position:absolute;left:8px;color:var(--faint);width:14px;height:14px}
.sfield input{width:100%;background:var(--b10);border:1px solid var(--border);border-radius:var(--r-s);color:var(--text);padding:6px 8px 6px 28px;font-size:var(--f-ui-smaller);font-family:inherit}
.sfield input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 1px var(--accent-bg)}
/* ツリー */
.tree{padding:0 6px}
.trow{display:flex;align-items:center;gap:5px;padding:3px 6px;border-radius:var(--r-s);cursor:pointer;color:var(--muted);font-size:var(--f-ui-smaller);white-space:nowrap;overflow:hidden;user-select:none;position:relative}
.trow:hover{background:var(--hover);color:var(--text)}
.trow.active{background:var(--active);color:var(--text)}
.tchildren{position:relative}
.tchildren::before{content:"";position:absolute;left:19px;top:0;bottom:0;width:1px;background:var(--border)}
.trow .tw{width:14px;height:14px;display:flex;align-items:center;justify-content:center;color:var(--faint);transition:transform .12s}
.trow.closed .tw{transform:rotate(-90deg)}
.trow .lbl{overflow:hidden;text-overflow:ellipsis}
.trow .cnt{margin-left:auto;color:var(--faint);font-size:11px;font-variant-numeric:tabular-nums;padding-left:6px}
.trow svg.ic{width:15px;height:15px}
.tchildren{}
.tchildren.hidden{display:none}
.folder-ico{color:var(--muted)}
.file-ico{color:var(--faint)}
.tag-ico{color:var(--accent);opacity:.85}
/* 検索結果 */
.sgroup{margin:2px 0 6px}
.sg-h{display:flex;align-items:center;gap:5px;padding:4px 10px;color:var(--muted);font-size:var(--f-ui-smaller);cursor:pointer;font-weight:600}
.sg-h:hover{color:var(--text)}
.sg-h .cnt{margin-left:auto;color:var(--faint)}
.sres{padding:2px 6px 2px 14px}
.sr{padding:5px 8px;border-radius:var(--r-s);cursor:pointer;margin-bottom:1px}
.sr:hover{background:var(--hover)}
.sr .srt{font-size:var(--f-ui-smaller);color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sr .srx{font-size:11px;color:var(--faint);line-height:1.5;margin-top:2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sr .srx mark{background:var(--mark);color:var(--mark-fg);border-radius:2px;padding:0 1px}
.scount{padding:6px 12px;color:var(--faint);font-size:var(--f-ui-smaller);border-bottom:1px solid var(--border);margin-bottom:4px}
.qhelp{padding:8px 12px;color:var(--faint);font-size:11px;line-height:1.7}
.qhelp code{background:var(--b10);padding:1px 4px;border-radius:3px;color:var(--muted);font-family:var(--mono)}
/* 中央 */
.main{background:var(--bg-primary);display:flex;flex-direction:column;min-width:0;min-height:0;position:relative}
.tabbar{display:flex;align-items:stretch;height:40px;background:var(--bg-sidebar);border-bottom:1px solid var(--border);padding-left:6px;flex:none;overflow-x:auto}
.tab{display:flex;align-items:center;gap:7px;height:32px;margin-top:8px;padding:0 10px;color:var(--muted);font-size:var(--f-ui-smaller);border-radius:var(--r-s) var(--r-s) 0 0;cursor:pointer;white-space:nowrap;max-width:220px}
.tab.on{background:var(--bg-primary);color:var(--text)}
.tab:not(.on):hover{background:var(--hover)}
.tab .lbl{overflow:hidden;text-overflow:ellipsis}
.tab .x{width:16px;height:16px;display:flex;align-items:center;justify-content:center;border-radius:3px;color:var(--faint);opacity:0}
.tab:hover .x{opacity:1}
.tab .x:hover{background:var(--active);color:var(--text)}
.vhead{display:flex;align-items:center;height:36px;padding:0 12px;gap:2px;flex:none;color:var(--faint)}
.vhead .nav{width:26px;height:26px;display:flex;align-items:center;justify-content:center;border-radius:var(--r-s);cursor:pointer}
.vhead .nav:hover{background:var(--hover);color:var(--text)}
.vhead .crumb{font-size:var(--f-ui-smaller);color:var(--muted);margin-left:6px}
.vhead .sp{flex:1}
.docwrap{overflow-y:auto;flex:1;min-height:0}
.doc{max-width:var(--line-w);margin:0 auto;padding:8px 40px 120px}
.inline-title{font-size:1.9em;font-weight:800;letter-spacing:-.02em;line-height:1.25;margin:14px 0 2px;color:var(--text)}
.note-tags{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 4px}
.ntag{color:var(--accent-2);font-size:var(--f-ui-smaller);cursor:pointer;background:var(--accent-bg);padding:1px 8px;border-radius:9px}
.ntag:hover{background:hsla(254,80%,68%,.28)}
/* Properties(実Obsidian準拠: 生キー+値がありません+追加) */
.props{margin:8px 0 24px;padding:0}
.props-h{font-size:var(--f-ui-small);color:var(--muted);padding:4px 2px 6px}
.prow{display:flex;align-items:center;gap:8px;padding:4px 6px;font-size:var(--f-ui-small);border-radius:var(--r-s)}
.prow:hover{background:var(--hover)}
.prow .pk{display:flex;align-items:center;gap:9px;color:var(--muted);width:180px;flex:none}
.prow .pk svg{width:15px;height:15px;color:var(--faint)}
.prow .pkn{overflow:hidden;text-overflow:ellipsis}
.prow .pv{color:var(--text);flex:1;min-width:0}
.pempty{color:var(--faint)}
.prow.padd .pk{color:var(--faint)}
/* リーディングビュー md */
.md{font-size:var(--f-text);line-height:1.6;color:var(--text)}
.md h1{font-size:1.9em;font-weight:700;margin:1.1em 0 .5em;line-height:1.3;color:var(--text)}
.md h2{font-size:1.52em;font-weight:600;margin:1.35em 0 .5em;line-height:1.3;color:var(--text)}
.md h3{font-size:1.28em;font-weight:600;margin:1.15em 0 .4em;line-height:1.3;color:var(--text)}
.md p{margin:.75em 0}
.md ul{margin:.5em 0;padding-left:1.5em}
.md li{margin:.28em 0;line-height:1.6}
.md li::marker{color:var(--faint)}
.md li.task{list-style:none;margin-left:-1.2em}
.md li.done{color:var(--faint);text-decoration:line-through}
.md blockquote{border-left:3px solid var(--b40);margin:.9em 0;padding:.4em 0 .4em 1em;color:var(--muted)}
.md strong,.md b{color:var(--text);font-weight:700}
.md a.ext{color:var(--accent-2);text-decoration:none}
.md a.ext:hover{text-decoration:underline}
.md .wl{color:var(--accent-2);cursor:pointer;text-decoration:none}
.md .wl:hover{text-decoration:underline}
.md .tg{color:var(--accent-2);background:var(--accent-bg);border-radius:12px;padding:1px 9px;font-size:.82em;cursor:pointer;text-decoration:none;white-space:nowrap;line-height:1.9}
.md .tg:hover{background:hsla(254,72%,62%,.24)}
.md .tbl{overflow-x:auto;margin:1em 0}
.md table{border-collapse:collapse;font-size:.86em;width:100%}
.md th,.md td{border:1px solid var(--border);padding:6px 11px;text-align:left;vertical-align:top}
.md th{background:var(--bg-elev);color:var(--text);font-weight:600;white-space:nowrap}
.md td{color:var(--muted)}
.fbcard{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--r-m);padding:2px 14px;margin:.7em 0}
.md pre{background:var(--b05);border:1px solid var(--border);border-radius:var(--r-s);padding:11px 13px;overflow-x:auto;margin:.9em 0}
.md pre code{font-family:var(--mono);font-size:.85em;color:var(--text);background:none;padding:0;border:0}
.md code{font-family:var(--mono);font-size:.86em;background:var(--b10);border:1px solid var(--border);border-radius:3px;padding:.5px 5px;color:var(--accent-2)}
.md hr{border:0;border-top:1px solid var(--border);margin:1.5em 0}
.md ol{margin:.5em 0;padding-left:1.6em}
.md ol li{margin:.28em 0;color:var(--muted);line-height:1.6}
.md ol li::marker{color:var(--faint)}
.callout{border:1px solid var(--border);border-left:3px solid var(--cc,#4f9df5);border-radius:var(--r-s);margin:1em 0;background:var(--b05)}
.callout .cot{display:flex;align-items:center;gap:8px;padding:9px 13px 3px;font-weight:600;font-size:.94em;color:var(--cc,#4f9df5)}
.callout .cot svg{color:var(--cc,#4f9df5)}
.callout .cob{padding:2px 13px 10px;color:var(--muted);line-height:1.6}
.callout.warning,.callout.warn{--cc:#e0b34c}.callout.danger,.callout.error,.callout.bug{--cc:#e0685f}.callout.tip,.callout.success{--cc:#54b981}.callout.note,.callout.info,.callout.quote{--cc:#4f9df5}
.ri,.trow,.sr,.ment,.wcard,.card,.tab,.bl,.olink,.act,.qi,.side-h .act{transition:background .1s ease,color .1s ease,border-color .1s ease}
:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
/* welcome */
.welcome{max-width:var(--line-w);margin:0 auto;padding:40px}
.welcome h1{font-size:1.9em;font-weight:800;display:flex;align-items:center;gap:12px;margin:0 0 6px}
.welcome .sub{color:var(--muted);margin:0 0 28px;font-size:var(--f-ui-med)}
.wsec{color:var(--faint);font-size:var(--f-ui-smaller);text-transform:uppercase;letter-spacing:.06em;font-weight:600;margin:24px 0 10px}
.wgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.wcard{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--r-m);padding:13px 15px;cursor:pointer;display:flex;gap:10px;align-items:flex-start}
.wcard:hover{border-color:var(--border-focus);background:#f0f0f3}
.wcard svg{color:var(--accent-2);margin-top:1px}
.wcard .wt{font-size:var(--f-ui-small);color:var(--text);font-weight:600;line-height:1.35}
.wcard .wx{font-size:var(--f-ui-smaller);color:var(--faint);margin-top:3px}
/* 右サイドバー */
.right{background:var(--bg-sidebar);border-left:1px solid var(--border);display:flex;flex-direction:column;min-width:0;min-height:0}
.right .side-body{padding:4px 0 20px}
.rtabs{display:flex;align-items:center;gap:2px;height:40px;padding:0 6px;flex:none;border-bottom:1px solid var(--border)}
.rtabs .rt{width:28px;height:28px;display:flex;align-items:center;justify-content:center;color:var(--faint);border-radius:var(--r-s);cursor:pointer;transition:background .1s,color .1s}
.rtabs .rt:hover{background:var(--hover);color:var(--muted)}
.rtabs .rt.on{color:var(--accent-2);background:var(--accent-bg)}
.rtabs .rt svg{width:16px;height:16px}
.rtitle{font-size:var(--f-ui-smaller);color:var(--muted);font-weight:600;padding:8px 12px 4px}
.bkgrp{margin:2px 0 10px}
.bkgrp-h{font-size:11px;color:var(--faint);letter-spacing:.02em;padding:6px 10px 5px;font-weight:600}
.bl{background:var(--b05);border:1px solid var(--border);border-radius:var(--r-s);padding:7px 10px;margin:0 6px 5px;cursor:pointer;transition:background .1s,border-color .1s}
.bl:hover{border-color:var(--border-focus);background:var(--hover)}
.bl .blt{font-size:var(--f-ui-smaller);color:var(--text);font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center;gap:6px}
.bl .blt svg{width:13px;height:13px;color:var(--faint);flex:none}
.bl .blc{font-size:11px;color:var(--muted);margin-top:4px;line-height:1.5;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.bl .blc .hlq{background:var(--mark);color:var(--mark-fg);border-radius:2px;padding:0 2px}
.md .embed{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:var(--r-s);padding:1px 8px;background:var(--b05);color:var(--accent-2);cursor:pointer}
.md .embed:hover{background:var(--hover)}
.md sup.fnref{color:var(--accent-2);font-size:.7em;cursor:pointer}
.md .fndef{font-size:.85em;color:var(--muted);border-top:1px solid var(--border);margin-top:.5em;padding-top:.4em}
.rsec{margin:2px 0}
.rsec-h{display:flex;align-items:center;gap:6px;padding:6px 12px;color:var(--muted);font-size:var(--f-ui-smaller);font-weight:600;cursor:pointer}
.rsec-h:hover{color:var(--text)}
.rsec-h .tw{width:12px;height:12px;color:var(--faint);transition:transform .12s}
.rsec-h.closed .tw{transform:rotate(-90deg)}
.rsec-h .cnt{margin-left:auto;color:var(--faint)}
.rsec.closed .rsec-body{display:none}
.rsec-body{padding:2px 8px 8px}
.ment{background:var(--b05);border:1px solid var(--border);border-radius:var(--r-s);padding:8px 10px;margin-bottom:6px;cursor:pointer}
.ment:hover{border-color:var(--border-focus)}
.ment .mt{font-size:var(--f-ui-smaller);color:var(--text);font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ment .mx{font-size:11px;color:var(--faint);margin-top:3px;line-height:1.5;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ment .mctx{font-size:11.5px;color:var(--muted);margin-top:5px;line-height:1.5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:var(--mono)}
.ment .wlx{background:var(--mark);color:var(--mark-fg);border-radius:2px;padding:0 2px}
.olink{padding:4px 8px;font-size:var(--f-ui-smaller);color:var(--muted);cursor:pointer;border-radius:var(--r-s);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.olink:hover{background:var(--hover);color:var(--text)}
/* グラフ */
#graphWrap{position:absolute;inset:40px 0 0 0;display:none}
#graph{width:100%;height:100%;display:block;cursor:grab}
.gpanel{position:absolute;right:14px;top:14px;width:220px;max-height:calc(100% - 28px);overflow-y:auto;background:var(--bg-float);border:1px solid var(--border);border-radius:var(--r-m);padding:8px 12px;font-size:var(--f-ui-smaller);color:var(--muted)}
.gpanel .gsec{padding:4px 0 8px;border-bottom:1px solid var(--border)}
.gpanel .gsec:last-child{border-bottom:0;padding-bottom:2px}
.gpanel .gh{color:var(--text);font-weight:600;margin:4px 0 7px;font-size:var(--f-ui-smaller)}
.gpanel .grow{display:flex;align-items:center;gap:7px;padding:1px 0}
.gpanel .dot{width:9px;height:9px;border-radius:50%;flex:none}
.gpanel .gin{width:100%;background:var(--b10);border:1px solid var(--border);border-radius:var(--r-s);color:var(--text);padding:4px 7px;font-size:11px;font-family:inherit;margin-bottom:6px}
.gpanel .gin:focus{outline:none;border-color:var(--accent)}
.gpanel .gck{display:flex;align-items:center;gap:6px;padding:2px 0;cursor:pointer}
.gpanel .gck input{accent-color:var(--accent);margin:0}
.gpanel .gsl{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:2px 0;font-size:11px}
.gpanel .gsl input[type=range]{width:96px;accent-color:var(--accent)}
.gpanel .gleg{margin-top:6px}
#gtip{position:fixed;pointer-events:none;background:var(--bg-float);border:1px solid var(--border-focus);border-radius:var(--r-s);padding:7px 10px;font-size:var(--f-ui-smaller);color:var(--text);display:none;z-index:70;max-width:250px;line-height:1.5;box-shadow:0 8px 24px rgba(0,0,0,.45)}
#gtip .gi{color:var(--muted);font-size:11px}
/* Quick switcher */
.qs-ov{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;z-index:80;justify-content:center}
.qs-ov.on{display:flex}
.qs{margin-top:14vh;width:min(600px,92vw);height:max-content;background:var(--bg-float);border:1px solid var(--border-focus);border-radius:var(--r-l);box-shadow:0 24px 70px rgba(0,0,0,.6);overflow:hidden}
.qs input{width:100%;background:transparent;border:0;border-bottom:1px solid var(--border);color:var(--text);padding:15px 18px;font-size:var(--f-ui-med);font-family:inherit;outline:none}
.qs .list{max-height:52vh;overflow-y:auto;padding:6px}
.qs .qi{display:flex;align-items:center;gap:10px;padding:8px 12px;border-radius:var(--r-s);cursor:pointer;font-size:var(--f-ui-small);color:var(--text)}
.qs .qi svg{color:var(--faint);width:15px;height:15px}
.qs .qi .pt{margin-left:auto;color:var(--faint);font-size:11px;white-space:nowrap}
.qs .qi.sel{background:var(--accent-bg)}
.qs .qi.sel .nm{color:var(--accent-2)}
/* status */
#gGear{position:absolute;right:14px;top:14px;width:32px;height:32px;display:flex;align-items:center;justify-content:center;background:var(--bg-float);border:1px solid var(--border);border-radius:var(--r-m);color:var(--muted);cursor:pointer;z-index:3;padding:0}
#gGear:hover{color:var(--text);border-color:var(--border-focus)}
.gpanel.hidden{display:none}
.gph{display:flex;align-items:center;justify-content:space-between;padding:2px 2px 8px;margin-bottom:2px;border-bottom:1px solid var(--border)}
.gph .gpt{color:var(--text);font-weight:600;font-size:var(--f-ui-smaller)}
.gph .gpx{cursor:pointer;color:var(--faint);width:20px;height:20px;display:flex;align-items:center;justify-content:center;border-radius:4px}
.gph .gpx:hover{background:var(--hover);color:var(--text)}
.side,.right{position:relative}
.rzh{position:absolute;top:0;width:7px;height:100%;cursor:col-resize;z-index:15}
.side .rzh{right:-3px}
.right .rzh{left:-3px}
.rzh:hover,.rzh.drag{background:var(--accent-bg)}
.statusbar{position:fixed;bottom:0;right:0;height:24px;background:var(--bg-sidebar);border-top:1px solid var(--border);border-left:1px solid var(--border);border-top-left-radius:var(--r-s);display:flex;align-items:center;gap:16px;padding:0 14px;font-size:11px;color:var(--faint);z-index:20}
.statusbar b{color:var(--muted);font-weight:500}
.statusbar .demo{color:var(--warn)}
/* Bases風テーブル */
.tbv{padding:14px 30px 90px;max-width:1080px;margin:0 auto}
.tbv .th1{font-size:1.6em;font-weight:800;display:flex;align-items:center;gap:10px;margin:6px 0 4px}
.tbv .sub{color:var(--muted);margin:0 0 14px;font-size:var(--f-ui-small)}
.tbv .bar{display:flex;gap:8px;align-items:center;margin:10px 0 14px;flex-wrap:wrap}
.tbv .bar input,.tbv .bar select{background:var(--b10);border:1px solid var(--border);border-radius:var(--r-s);color:var(--text);padding:6px 10px;font-size:var(--f-ui-smaller);font-family:inherit}
.tbv .bar input:focus,.tbv .bar select:focus{outline:none;border-color:var(--accent)}
.tbv .bar .n{color:var(--faint);font-size:var(--f-ui-smaller);margin-left:auto}
.tblwrap{overflow:auto;border:1px solid var(--border);border-radius:var(--r-m);max-height:calc(100vh - 220px)}
.tbv table{border-collapse:collapse;width:100%;font-size:var(--f-ui-small)}
.tbv thead th{position:sticky;top:0;background:var(--bg-elev);text-align:left;padding:9px 12px;color:var(--muted);font-weight:600;border-bottom:1px solid var(--border);cursor:pointer;white-space:nowrap;user-select:none;z-index:1}
.tbv thead th:hover{color:var(--text)}
.tbv thead th .ar{color:var(--accent-2);margin-left:5px;font-size:10px}
.tbv td{padding:8px 12px;border-bottom:1px solid var(--b10);color:var(--muted);white-space:nowrap}
.tbv tbody tr{cursor:pointer}
.tbv tbody tr:hover{background:var(--hover)}
.tbv tbody td:first-child{color:var(--text);font-weight:500}
.tbv .num{text-align:right;font-variant-numeric:tabular-nums}
.tbv .bp{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px}
.tbv .dotc{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
/* 施策タイムライン（カルテ内・縦タイムライン: 左=日付チップ+縦線 / 右=カード） */
.tlwrap{margin:4px 0 26px}
.tlh{display:flex;align-items:center;gap:7px;font-size:var(--f-ui-small);color:var(--muted);font-weight:600;padding:4px 0 9px;border-bottom:1px solid var(--border)}
.tlh svg.ic{color:var(--accent-2)}
.tlh .cnt{color:var(--faint);font-weight:400;font-size:var(--f-ui-smaller)}
.tlbody{padding:12px 0 2px}
.tlrow{position:relative;display:flex;padding:0 0 12px}
.tlrow::before{content:"";position:absolute;left:86px;top:6px;bottom:-6px;width:1px;background:var(--border)}
.tlrow:last-child::before{display:none}
.tlrow::after{content:"";position:absolute;left:83px;top:6px;width:7px;height:7px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 2px var(--bg-primary)}
.tlrow.tldoc::after{background:var(--b40)}
.tld{width:72px;flex:none;text-align:right;font-size:var(--f-ui-smaller);color:var(--faint);font-variant-numeric:tabular-nums;padding-top:1px}
.tlcard{flex:1;min-width:0;background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--r-m);padding:8px 12px 9px;margin-left:26px}
.tlbadge{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px;font-weight:600;margin-right:6px;white-space:nowrap}
.tlbadge.tlfb{background:var(--accent-bg);color:var(--accent-2)}
.tlchip{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;background:var(--hover);color:var(--muted);margin-right:4px;white-space:nowrap}
.tlt{font-weight:600;font-size:var(--f-ui-small);color:var(--text);margin-top:5px;line-height:1.45;overflow-wrap:anywhere}
.tlt .wl{color:var(--accent-2);cursor:pointer}
.tlt .wl:hover{text-decoration:underline}
.tlx{font-size:var(--f-ui-smaller);color:var(--muted);margin-top:4px;line-height:1.55;overflow-wrap:anywhere}
.tlnx{font-size:var(--f-ui-smaller);color:var(--accent-2);margin-top:4px;line-height:1.5;overflow-wrap:anywhere}
.tlmorebtn{display:inline-block;margin:0 0 2px 98px;padding:4px 12px;font-size:var(--f-ui-smaller);color:var(--accent-2);cursor:pointer;border:1px solid var(--border);border-radius:var(--r-s)}
.tlmorebtn:hover{background:var(--hover);border-color:var(--border-focus)}
.tlwrap.folded .tlrow.tlhid{display:none}
kbd{background:var(--bg-float);border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-size:11px;font-family:var(--mono)}
::selection{background:hsla(254,80%,68%,.35)}
.tab.newtab{margin-top:8px;width:30px;justify-content:center;color:var(--faint)}
.tab.newtab:hover{background:var(--hover);color:var(--text)}
#hoverpop{position:fixed;display:none;z-index:75;width:300px;max-height:158px;overflow:hidden;background:var(--bg-float);border:1px solid var(--border-focus);border-radius:var(--r-m);padding:11px 13px;box-shadow:0 14px 44px rgba(0,0,0,.55);pointer-events:none}
#hoverpop .hpt{font-weight:700;font-size:var(--f-ui-small);color:var(--text);margin-bottom:5px}
#hoverpop .hpx{font-size:var(--f-ui-smaller);color:var(--muted);line-height:1.55;white-space:pre-wrap;max-height:110px;overflow:hidden}
.qs .qi .kb{margin-left:auto;color:var(--faint);font-size:11px;font-family:var(--mono)}
@media(max-width:1180px){.app{grid-template-columns:var(--ribbon) var(--side) 1fr}.right{display:none}}
</style></head><body>
<div class="app">
 <nav class="ribbon" id="ribbon"></nav>
 <aside class="side"><div class="rzh" data-rz="side"></div><div class="side-h"><span class="t" id="sideTitle">ファイル</span><span class="act" id="sideAct1"></span><span class="act" id="sideAct2"></span></div>
  <div class="side-body" id="sideBody"></div></aside>
 <section class="main">
  <div class="tabbar" id="tabbar"></div>
  <div class="vhead" id="vhead"></div>
  <div class="docwrap" id="docPane"><div class="doc" id="inner"></div></div>
  <div id="graphWrap"><canvas id="graph"></canvas><button id="gGear" title="グラフ設定"></button><div class="gpanel hidden" id="gpanel"></div></div>
 </section>
 <aside class="right"><div class="rzh" data-rz="right"></div><div class="rtabs" id="rtabs"></div><div class="side-body" id="rightBody"></div></aside>
</div>
<div id="gtip"></div>
<div class="qs-ov" id="qsov"><div class="qs"><input id="qsin" placeholder="ノートに移動…"><div class="list" id="qslist"></div></div></div>
<div class="statusbar" id="statusbar"></div>
<script>
const DATA=__DATA__;
const $=s=>document.querySelector(s);
const esc=s=>(s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const colorOf=i=>DATA.colors[i]||"var(--accent)";
function nrm(s){return (s||"").normalize("NFKC").replace(/一般社団法人|公益社団法人|株式会社|株式會社|有限会社|合同会社|合資会社|\(株\)|（株）|㈱|\(有\)|（有）|㈲|御中|さま|様|殿|\s|　/g,"").toLowerCase();}
/* ===== Lucide アイコン ===== */
const P={
 search:'<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
 files:'<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
 folder:'<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
 file:'<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v5h5"/>',
 filetext:'<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v5h5"/><path d="M16 13H8"/><path d="M16 17H8"/>',
 chev:'<path d="m9 18 6-6-6-6"/>',
 hash:'<line x1="4" y1="9" x2="20" y2="9"/><line x1="4" y1="15" x2="20" y2="15"/><line x1="10" y1="3" x2="8" y2="21"/><line x1="16" y1="3" x2="14" y2="21"/>',
 tags:'<path d="M9 5H2v7l6.29 6.29c.94.94 2.48.94 3.42 0l3.58-3.58c.94-.94.94-2.48 0-3.42L9 5Z"/><path d="M6 9.5V9"/><path d="m15 5 6.3 6.3a2.4 2.4 0 0 1 0 3.4L17 19"/>',
 graph:'<circle cx="12" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><path d="M18 9v1a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V9"/><path d="M12 12v3"/>',
 bookmark:'<path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>',
 settings:'<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
 help:'<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
 sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
 linkin:'<path d="M9 10 4 15l5 5"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/>',
 linkout:'<path d="m15 14 5-5-5-5"/><path d="M4 20v-7a4 4 0 0 1 4-4h12"/>',
 moon:'<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
 vault:'<path d="M12 2 2 7l10 5 10-5-10-5Z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
 x:'<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
 plus:'<path d="M5 12h14"/><path d="M12 5v14"/>',
 back:'<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
 fwd:'<path d="m12 5 7 7-7 7"/><path d="M5 12h14"/>',
 building:'<path d="M6 22V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v18"/><path d="M2 22h20"/><path d="M10 6h4M10 10h4M10 14h4"/>',
 target:'<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
 trend:'<path d="M22 7 13.5 15.5l-5-5L2 17"/><path d="M16 7h6v6"/>',
 msg:'<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>',
 cal:'<rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18M8 2v4M16 2v4"/>',
 factory:'<path d="M2 20a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8l-7 5V8l-7 5V4a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2Z"/>',
 list:'<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
 report:'<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v5h5"/><path d="M8 13h2v5H8zM14 11h2v7h-2z"/>',
 panel:'<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/>',
 table:'<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18M3 15h18M12 3v18"/>',
 more:'<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
};
function ic(n,cls){return '<svg class="ic '+(cls||"")+'" viewBox="0 0 24 24">'+(P[n]||"")+'</svg>';}

/* ===== 索引・タグ・プロパティ ===== */
const cByStem={},dByStem={},rByStem={},cByNorm={};
DATA.clients.forEach(c=>{cByStem[c.stem]=c;if(c.name)cByNorm[nrm(c.name)]=c;});
DATA.docs.forEach(d=>dByStem[d.stem]=d);
DATA.reports.forEach(r=>rByStem[r.stem]=r);
/* 閲覧時点からの経過でバケット化（ビルド日でなく Date.now 基準 = 月次再生成の間も鮮度が生きる） */
function ageBucket(ds){if(!ds)return"";const t=Date.parse(ds);if(isNaN(t))return"";const dd=(Date.now()-t)/864e5;return dd<=31?"1ヶ月以内":dd<=92?"3ヶ月以内":dd<=183?"半年以内":dd<=366?"1年以内":"1年以上前";}
function clientTags(c){const t=[];if(c.industry)t.push("業種/"+c.industry);if(c.phase)t.push("フェーズ/"+c.phase);if(c.bantg)t.push("BANT/"+c.bantg);t.push("最終接点/"+(ageBucket(c.last)||"記録なし"));return t;}
function docTags(d){const t=[];if(d.doc_type)t.push("資料種別/"+d.doc_type);if(d.industry)t.push("業種/"+d.industry);if(d.solution)t.push("施策/"+d.solution);const a=ageBucket(d.modified);if(a)t.push("更新/"+a);if(d.src)t.push("情報源/"+d.src);return t;}
const IDX=[];
DATA.clients.forEach(c=>IDX.push({kind:"client",stem:c.stem,name:c.name,folder:"clients",tags:clientTags(c),
 props:{"業界":c.industry,"フェーズ":c.phase,"BANT":c.bant,"最終接点":c.last||"—"},hay:(c.name+" "+c.industry+" "+c.phase+" "+c.bant+" "+(c.md||"")).toLowerCase(),ex:c.industry?("業種: "+c.industry):"",obj:c}));
DATA.docs.forEach(d=>IDX.push({kind:"doc",stem:d.stem,name:d.title,folder:"docs",tags:docTags(d),
 props:{"種別":d.doc_type,"取引先":d.client,"業界":d.industry,"施策":d.solution,"更新":d.modified||"—","情報源":d.src||"—"},hay:(d.title+" "+d.client+" "+d.industry+" "+d.solution+" "+d.doc_type+" "+d.src+" "+d.ex).toLowerCase(),ex:d.ex,obj:d}));
DATA.reports.forEach(r=>IDX.push({kind:"report",stem:r.stem,name:r.name,folder:"_reports",tags:[],props:{},hay:(r.name+" "+r.md).toLowerCase(),ex:"AI洗い出しレポート",obj:r}));
// タグ集計(親も加算)
const tagCount={};
IDX.forEach(it=>it.tags.forEach(t=>{const p=t.split("/");for(let i=1;i<=p.length;i++){const k=p.slice(0,i).join("/");tagCount[k]=(tagCount[k]||0)+1;}}));
const tagTree={};
Object.keys(tagCount).forEach(t=>{const p=t.split("/");if(p.length===1){tagTree[p[0]]=tagTree[p[0]]||{count:tagCount[p[0]]||0,children:{}};}
 else{tagTree[p[0]]=tagTree[p[0]]||{count:tagCount[p[0]]||0,children:{}};tagTree[p[0]].children[t]=tagCount[t];}});

/* ===== 実リンク網（バックリンク/アウトゴーイング/ローカルグラフ） ===== */
const OUTL={},BACKL={},NEIGH={};
(DATA.links||[]).forEach(([s,t,ctx])=>{
 (OUTL[s]=OUTL[s]||[]).push([t,ctx]);(BACKL[t]=BACKL[t]||[]).push([s,ctx]);
 (NEIGH[s]=NEIGH[s]||new Set()).add(t);(NEIGH[t]=NEIGH[t]||new Set()).add(s);
});
function keyMeta(key){if(!key)return null;const k=key[0],stem=key.slice(2);
 if(k==="c"){const c=cByStem[stem];return c?{title:c.name,icon:"building",k,stem,sub:c.industry||"取引先"}:null;}
 if(k==="d"){const d=dByStem[stem];return d?{title:d.title,icon:"filetext",k,stem,sub:d.client||d.doc_type||"資料"}:null;}
 if(k==="r"){const r=rByStem[stem];return r?{title:r.name,icon:"report",k,stem,sub:"レポート"}:null;}
 return null;}

let lastNote=null, history=[], bookmarks=new Set(JSON.parse(localStorage.getItem("aila_bm")||"[]"));
function saveBm(){localStorage.setItem("aila_bm",JSON.stringify([...bookmarks]));}

/* ===== リボン ===== */
const RIBBON=[["vault","","top"],["files","ファイル","pane"],["search","検索","pane"],["tags","タグ","pane"],["bookmark","ブックマーク","pane"],["table","テーブル","view2"],["graph","グラフ","view"]];
function renderRibbon(){
 const r=$("#ribbon");r.innerHTML="";
 RIBBON.forEach(([id,label,kind])=>{const el=document.createElement("div");el.className="ri";el.dataset.id=id;el.title=label||id;el.innerHTML=ic(id);
  el.onclick=()=>{if(kind==="view")openGraph();else if(kind==="view2")tableView();else if(kind==="pane")setPane(id);else if(id==="vault")qsOpen();};r.appendChild(el);});
 const sp=document.createElement("div");sp.className="sp";r.appendChild(sp);
 const th=document.createElement("div");th.className="ri";th.id="themeBtn";th.title="テーマ切替（ライト/ダーク）";th.innerHTML=ic(document.documentElement.getAttribute("data-theme")==="dark"?"sun":"moon");th.onclick=toggleTheme;r.appendChild(th);
 [["help"],["settings"]].forEach(([id])=>{const el=document.createElement("div");el.className="ri";el.innerHTML=ic(id);el.title=id;r.appendChild(el);});
}
function applyTheme(t,persist){document.documentElement.setAttribute("data-theme",t);if(persist){try{localStorage.setItem("aila_theme",t);}catch(e){}}
 const b=$("#themeBtn");if(b)b.innerHTML=ic(t==="dark"?"sun":"moon");if(G&&window.__gRedraw)window.__gRedraw();}
function toggleTheme(){applyTheme(document.documentElement.getAttribute("data-theme")==="dark"?"light":"dark",true);}
function ribbonActive(id){document.querySelectorAll("#ribbon .ri").forEach(e=>e.classList.toggle("on",e.dataset.id===id));}

/* ===== 左ペイン ===== */
let pane="files", filterStr="";
const PANE_TITLE={files:"ファイル",search:"検索",tags:"タグ",bookmark:"ブックマーク"};
function setPane(p){pane=p;ribbonActive(p);$("#sideTitle").textContent=PANE_TITLE[p]||p;renderPane();
 if(p==="search")setTimeout(()=>{const i=$("#searchInput");if(i)i.focus();},20);}
function renderPane(){
 const b=$("#sideBody");b.innerHTML="";
 $("#sideAct1").innerHTML="";$("#sideAct2").innerHTML="";
 if(pane==="files")return renderFiles(b);
 if(pane==="search")return renderSearchPane(b);
 if(pane==="tags")return renderTags(b);
 if(pane==="bookmark")return renderBookmarks(b);
}
function treeRow(opts){// {chevron,icon,label,count,indent,active,onclick,onchev}
 const d=document.createElement("div");d.className="trow"+(opts.active?" active":"")+(opts.closed?" closed":"");
 d.style.paddingLeft=(6+(opts.indent||0)*14)+"px";
 d.innerHTML=(opts.chevron?'<span class="tw">'+ic("chev")+'</span>':'<span class="tw"></span>')
  +(opts.icon?ic(opts.icon,opts.iconCls||""):"")+'<span class="lbl"'+(opts.bold?' style="color:var(--text)"':"")+'>'+esc(opts.label)+'</span>'
  +(opts.count!=null?'<span class="cnt">'+opts.count+'</span>':"");
 if(opts.key){d.dataset.key=opts.key;d.classList.add("filerow");}
 if(opts.onclick)d.onclick=opts.onclick;return d;
}
const _grpState={_reports:true,clients:false,docs:false};   // フォルダ開閉状態を保持
function renderFiles(b,revealKey){
 $("#sideAct1").innerHTML=ic("filetext");$("#sideAct1").title="新規ノート";
 $("#sideAct2").innerHTML=ic("folder");$("#sideAct2").title="新規フォルダ";
 const wrap=document.createElement("div");wrap.className="tree";b.appendChild(wrap);
 const revGrp=revealKey?({c:"clients",d:"docs",r:"_reports"}[revealKey[0]]):null;
 const groups=[["_reports",DATA.reports.map(r=>({name:r.name,stem:r.stem,k:"r"}))],
  ["clients",DATA.clients.map(c=>({name:c.name,stem:c.stem,k:"c"}))],
  ["docs",DATA.docs.map(d=>({name:d.title,stem:d.stem,k:"d"}))]];
 groups.forEach(([nm,items])=>{
  if(nm===revGrp)_grpState[nm]=true;
  let open=!!_grpState[nm];const fr=treeRow({chevron:true,label:nm,count:items.length,closed:!open,bold:true});
  const ch=document.createElement("div");ch.className="tchildren"+(open?"":" hidden");
  let built=false;
  function build(){if(built)return;built=true;items.forEach(it=>{
   ch.appendChild(treeRow({label:it.name,indent:1,key:it.k+":"+it.stem,onclick:()=>openByK(it.k,it.stem)}));});}
  if(open)build();
  fr.onclick=()=>{open=!open;_grpState[nm]=open;fr.classList.toggle("closed",!open);ch.classList.toggle("hidden",!open);if(open)build();};
  wrap.appendChild(fr);wrap.appendChild(ch);
 });
}
function markActiveInTree(){const key=lastNote&&lastNote.key;const b=$("#sideBody");if(pane!=="files"||!b)return;
 let hit=null;b.querySelectorAll(".trow.filerow").forEach(el=>{const on=el.dataset.key===key;el.classList.toggle("active",on);if(on)hit=el;});
 if(!hit&&key&&/^[cdr]:/.test(key)){b.innerHTML="";renderFiles(b,key);   // 未展開グループ内 → 再描画して開く
  b.querySelectorAll(".trow.filerow").forEach(el=>{const on=el.dataset.key===key;el.classList.toggle("active",on);if(on)hit=el;});}
 if(hit)hit.scrollIntoView({block:"nearest"});}
function openByK(k,stem){k==="c"?openClient(stem):k==="r"?openReport(stem):openDoc(stem);}

/* ---- 検索ペイン(演算子対応) ---- */
function parseQuery(q){const P={plain:[],neg:[],tag:[],path:[],file:[],ntag:[],npath:[],nfile:[],props:[],regex:[]};
 q=q.replace(/(^|\s)\/((?:\\.|[^\/\\])+)\/(?=\s|$)/g,(m,pre,pat)=>{try{P.regex.push(new RegExp(pat,"i"));}catch(e){}return pre+" ";});  /* /正規表現/ を抽出 */
 const re=/\[([^\]:]+)(?::([^\]]*))?\]|(-?)(tag|path|file):("[^"]*"|\S+)|"([^"]+)"|(\S+)/g;let m;
 while(m=re.exec(q)){
  if(m[1]!=null){P.props.push([m[1].trim(),(m[2]||"").trim()]);}
  else if(m[4]){const v=(m[5]||"").replace(/^"|"$/g,"").toLowerCase();if(v)(m[3]==="-"?P["n"+m[4]]:P[m[4]]).push(v);}
  else if(m[6]){P.plain.push(m[6].toLowerCase());}
  else if(m[7]){if(m[7][0]==="-"){const w=m[7].slice(1).toLowerCase();if(w)P.neg.push(w);}else P.plain.push(m[7].toLowerCase());}
 }return P;}
function tagMatch(itemTags,q){return itemTags.some(t=>t.toLowerCase()===q||t.toLowerCase().startsWith(q+"/"));}
function matchItem(it,P){
 for(const t of P.plain)if(!it.hay.includes(t))return false;
 for(const n of P.neg)if(n&&it.hay.includes(n))return false;
 for(const rx of P.regex)if(!rx.test(it.hay))return false;
 for(const t of P.tag)if(!tagMatch(it.tags,t))return false;
 for(const t of P.ntag)if(tagMatch(it.tags,t))return false;
 for(const p of P.path)if(!it.folder.toLowerCase().includes(p))return false;
 for(const p of P.npath)if(it.folder.toLowerCase().includes(p))return false;
 for(const f of P.file)if(!it.name.toLowerCase().includes(f))return false;
 for(const f of P.nfile)if(it.name.toLowerCase().includes(f))return false;
 for(const[k,v]of P.props){const pv=(it.props[k]||"");if(v==="null"){if(pv)return false;}else if(v===""){if(!pv)return false;}else if(!(""+pv).toLowerCase().includes(v.toLowerCase()))return false;}
 return true;
}
function renderSearchPane(b){
 const f=document.createElement("div");f.className="sfield";f.innerHTML=ic("search")+'<input id="searchInput" placeholder="検索  tag: path: [業界:IT]">';b.appendChild(f);
 const out=document.createElement("div");out.id="searchOut";b.appendChild(out);
 const inp=f.querySelector("input");inp.value=window.__lastQ||"";
 inp.addEventListener("input",()=>{window.__lastQ=inp.value;runSearchPane(inp.value,out);});
 runSearchPane(inp.value,out);
}
function hl(text,terms){let s=esc(text);terms.forEach(t=>{if(t&&t.length>1){s=s.replace(new RegExp("("+t.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")+")","ig"),"<mark>$1</mark>");}});return s;}
function runSearchPane(q,out){
 q=q.trim();
 if(!q){out.innerHTML='<div class="qhelp">演算子が使えます：<br><code>tag:業種/食品</code> タグ(子も一致)<br><code>path:clients</code> フォルダ<br><code>file:提案</code> ファイル名<br><code>[業界:IT]</code> プロパティ<br><code>-除外語</code> / <code>"完全一致"</code> / <code>/正規表現/</code></div>';return;}
 const P=parseQuery(q);const hits=IDX.filter(it=>matchItem(it,P));
 const byFolder={clients:[],docs:[],_reports:[]};hits.forEach(h=>byFolder[h.folder].push(h));
 let html='<div class="scount">'+hits.length+' 件ヒット</div>';
 [["_reports","レポート"],["clients","取引先"],["docs","資料"]].forEach(([fk,label])=>{
  const arr=byFolder[fk];if(!arr.length)return;
  html+='<div class="sgroup"><div class="sg-h">'+ic("folder","folder-ico")+' '+label+'<span class="cnt">'+arr.length+'</span></div><div class="sres">';
  arr.slice(0,60).forEach(it=>{html+='<div class="sr" data-k="'+it.kind[0]+'" data-s="'+esc(it.stem)+'"><div class="srt">'+esc(it.name)+'</div>'+(it.ex?'<div class="srx">'+hl(it.ex,P.plain)+'</div>':"")+'</div>';});
  if(arr.length>60)html+='<div class="scount">…他 '+(arr.length-60)+' 件</div>';
  html+='</div></div>';
 });
 out.innerHTML=html;
 out.querySelectorAll(".sr").forEach(el=>el.onclick=()=>openByK(el.dataset.k,el.dataset.s));
 out.querySelectorAll(".sg-h").forEach(h=>h.onclick=()=>{const r=h.nextElementSibling;r.style.display=r.style.display==="none"?"":"none";});
}
/* ---- タグ(ネスト) ---- */
function renderTags(b){
 const wrap=document.createElement("div");wrap.className="tree";b.appendChild(wrap);
 Object.keys(tagTree).sort().forEach(top=>{
  const node=tagTree[top];const kids=Object.keys(node.children).sort();let open=false;
  const fr=treeRow({chevron:kids.length>0,icon:"hash",iconCls:"tag-ico",label:top,count:node.count,closed:true});
  const ch=document.createElement("div");ch.className="tchildren hidden";
  kids.forEach(full=>{const leaf=full.split("/").slice(1).join("/");
   ch.appendChild(treeRow({icon:"hash",iconCls:"tag-ico",label:leaf,count:node.children[full],indent:1,onclick:()=>{setPane("search");setTimeout(()=>{const i=$("#searchInput");i.value="tag:"+full;window.__lastQ=i.value;runSearchPane(i.value,$("#searchOut"));},30);}}));});
  fr.onclick=()=>{if(!kids.length){setPane("search");setTimeout(()=>{const i=$("#searchInput");i.value="tag:"+top;window.__lastQ=i.value;runSearchPane(i.value,$("#searchOut"));},30);return;}open=!open;fr.classList.toggle("closed",!open);ch.classList.toggle("hidden",!open);};
  wrap.appendChild(fr);wrap.appendChild(ch);
 });
}
/* ---- ブックマーク ---- */
function renderBookmarks(b){
 const wrap=document.createElement("div");wrap.className="tree";b.appendChild(wrap);
 if(!bookmarks.size){wrap.innerHTML='<div class="qhelp">ブックマークは空です。ノートを開いて右上の <b>'+ic("bookmark")+'</b> で追加できます。</div>';return;}
 [...bookmarks].forEach(key=>{const[k,stem]=[key[0],key.slice(2)];const it=k==="c"?cByStem[stem]:k==="d"?dByStem[stem]:rByStem[stem];if(!it)return;
  wrap.appendChild(treeRow({icon:k==="c"?"building":k==="r"?"report":"file",iconCls:"file-ico",label:it.name||it.title,onclick:()=>openByK(k,stem)}));});
}

/* ===== タブ・履歴 ===== */
function renderTabs(){
 const tb=$("#tabbar");tb.innerHTML="";
 const home=document.createElement("div");home.className="tab"+(!lastNote?" on":"");home.innerHTML=ic("files")+'<span class="lbl">ホーム</span>';
 home.onclick=()=>{lastNote=null;showDoc();welcome();renderTabs();};tb.appendChild(home);
 if(lastNote){const t=document.createElement("div");t.className="tab on";
  t.innerHTML=ic(lastNote.icon)+'<span class="lbl">'+esc(lastNote.title)+'</span><span class="x">'+ic("x")+'</span>';
  t.querySelector(".x").onclick=e=>{e.stopPropagation();lastNote=null;welcome();renderTabs();};tb.appendChild(t);}
 const nt=document.createElement("div");nt.className="tab newtab";nt.title="新しいタブ (⌘O)";nt.innerHTML=ic("plus");nt.onclick=()=>qsOpen("nav");tb.appendChild(nt);
}
function renderVhead(){
 const v=$("#vhead");
 v.innerHTML='<span class="nav" id="navBack" title="戻る">'+ic("back")+'</span>'+
  '<span class="crumb">'+(lastNote?esc((lastNote.folder||"")+" / "+lastNote.title):"ホーム")+'</span><span class="sp"></span>'+
  (lastNote?'<span class="nav" id="bmBtn" title="ブックマーク">'+ic("bookmark")+'</span>':"");
 const back=v.querySelector("#navBack");if(back)back.onclick=()=>{history.pop();const p=history.pop();if(p)openByK(p[0],p[1]);else{lastNote=null;welcome();renderTabs();}};
 const bm=v.querySelector("#bmBtn");if(bm){const key=lastNote.key;bm.style.color=bookmarks.has(key)?"var(--accent-2)":"";
  bm.onclick=()=>{bookmarks.has(key)?bookmarks.delete(key):bookmarks.add(key);saveBm();renderVhead();if(pane==="bookmark")renderPane();};}
}

/* ===== 中央表示 ===== */
function showDoc(){$("#graphWrap").style.display="none";$("#docPane").style.display="block";ribbonActive(pane);}
function showGraphView(){$("#graphWrap").style.display="block";$("#docPane").style.display="none";ribbonActive("graph");}
function welcome(){
 showDoc();
 const notable=DATA.clients.filter(c=>c.fb>0).sort((a,b)=>(b.fb+b.doc)-(a.fb+a.doc)).slice(0,6);
 const cards=k=>k.map(c=>`<div class="wcard" data-k="c" data-s="${esc(c.stem)}">`+ic("building")+`<div><div class="wt">${esc(c.name)}</div><div class="wx">${esc(c.industry||"業界未設定")} ・ FB${c.fb} / 資料${c.doc}</div></div></div>`).join("");
 const rep=DATA.reports.map(r=>`<div class="wcard" data-k="r" data-s="${esc(r.stem)}">`+ic("report")+`<div><div class="wt">${esc(r.name)}</div><div class="wx">AI洗い出しレポート</div></div></div>`).join("");
 $("#inner").innerHTML=`<div class="welcome"><h1>${ic("vault")} AiLaVault</h1>
  <p class="sub">営業16名の社内ナレッジ — ${DATA.stats.clients} 取引先 / ${DATA.stats.docs} 資料。左の検索・タグ・グラフで分類・回遊できます。取引先カルテには資料と商談FBを時系列で一望できる<b>施策タイムライン</b>付き。<kbd>⌘O</kbd> でどこへでもジャンプ。</p>
  <div class="wsec">AI洗い出しレポート</div><div class="wgrid">${rep}</div>
  <div class="wsec">主要な取引先</div><div class="wgrid">${cards(notable)}</div></div>`;
 $("#inner").querySelectorAll(".wcard").forEach(el=>el.onclick=()=>openByK(el.dataset.k,el.dataset.s));
 renderRightTabs();renderRight();markActiveInTree();
 if(window.updateStatus)updateStatus(0,0);
}
function pushHist(k,stem){history.push([k,stem]);if(history.length>50)history.shift();}
/* Markdown */
function inl(s){s=esc(s);
 const PH=String.fromCharCode(1),codes=[];
 s=s.replace(/`([^`]+)`/g,(m,c)=>{codes.push(c);return PH+(codes.length-1)+PH;});  /* コードを隔離(二重変換防止) */
 s=s.replace(/\\\|/g,"|");  /* 表セル/リンクのエスケープパイプを実文字に */
 s=s.replace(/!\[\[([^\]]+?)\]\]/g,(m,i)=>{const p=i.split("|");const tg=p[0].trim(),lb=(p[1]||p[0].split("/").pop()).trim();return '<span class="wl embed" data-t="'+esc(tg)+'">'+esc(lb)+'</span>';});  /* 埋め込み ![[ ]] */
 s=s.replace(/\[\^([^\]\s]+)\]/g,'<sup class="fnref">$1</sup>');  /* 脚注参照 */
 s=s.replace(/\[\[([^\]]+?)\]\]/g,(m,i)=>{const p=i.split("|");const tg=p[0].trim(),lb=(p[1]||p[0].split("/").pop()).trim();return '<span class="wl" data-t="'+esc(tg)+'">'+esc(lb)+'</span>';});
 s=s.replace(/\[([^\]]+)\]\(([^)]+)\)/g,(m,t,u)=>'<a class="ext" href="'+esc(u)+'" target="_blank" rel="noopener">'+esc(t)+'</a>');
 s=s.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>");
 s=s.replace(/(^|[\s(（])#([^\s#、。,.\/()（）\[\]]+(?:\/[^\s#、。,.\/()（）\[\]]+)*)/g,(m,p,t)=>p+'<span class="tg" data-tag="'+esc(t)+'">#'+esc(t)+"</span>");
 s=s.replace(new RegExp(PH+"(\\d+)"+PH,"g"),(m,i)=>"<code>"+codes[+i]+"</code>");
 return s;}
function splitRow(ln){ln=ln.trim().replace(/^\|/,"").replace(/\|$/,"");return ln.split(/(?<!\\)\|/).map(c=>c.trim());}
const COIC={note:"file",info:"help",tip:"target",success:"target",warning:"help",warn:"help",danger:"help",error:"help",bug:"help",quote:"msg"};
function md(src){const L=src.split("\n");let h="",i=0,m;
 while(i<L.length){let ln=L[i];
  if(/^```/.test(ln)){i++;const c=[];while(i<L.length&&!/^```/.test(L[i])){c.push(L[i]);i++;}i++;h+='<pre><code>'+esc(c.join("\n"))+'</code></pre>';continue;}
  if(/^\s*\|.*\|\s*$/.test(ln)&&i+1<L.length&&/^\s*\|?[\s:|-]+\|?\s*$/.test(L[i+1])){const hd=splitRow(ln);i+=2;const rows=[];
   while(i<L.length&&/^\s*\|.*\|\s*$/.test(L[i])){rows.push(splitRow(L[i]));i++;}
   h+='<div class="tbl"><table><thead><tr>'+hd.map(c=>"<th>"+inl(c)+"</th>").join("")+"</tr></thead><tbody>"+rows.map(r=>"<tr>"+r.map(c=>"<td>"+inl(c)+"</td>").join("")+"</tr>").join("")+"</tbody></table></div>";continue;}
  if(/^(-{3,}|\*{3,}|_{3,})\s*$/.test(ln)){h+="<hr>";i++;continue;}
  if(m=ln.match(/^(#{1,6})\s+(.*)/)){const lv=Math.max(1,Math.min(m[1].length,3));h+="<h"+lv+">"+inl(m[2])+"</h"+lv+">";i++;continue;}
  if(m=ln.match(/^>\s*\[!(\w+)\][-+]?\s*(.*)$/)){const ty=m[1].toLowerCase();const ti=m[2]||ty;const cb=[];i++;while(i<L.length&&/^>\s?/.test(L[i])){cb.push(L[i].replace(/^>\s?/,""));i++;}h+='<div class="callout '+esc(ty)+'"><div class="cot">'+ic(COIC[ty]||"info")+esc(ti)+'</div>'+(cb.length?'<div class="cob">'+cb.map(inl).join("<br>")+'</div>':"")+'</div>';continue;}
  if(/^>\s?/.test(ln)){const q=[];while(i<L.length&&(m=L[i].match(/^>\s?(.*)/))){q.push(m[1]);i++;}h+="<blockquote>"+q.map(inl).join("<br>")+"</blockquote>";continue;}
  if(/^\s*\d+\.\s+/.test(ln)){const it=[];while(i<L.length&&/^\s*\d+\.\s+/.test(L[i])){it.push(L[i].replace(/^\s*\d+\.\s+/,""));i++;}h+="<ol>"+it.map(x=>"<li>"+inl(x)+"</li>").join("")+"</ol>";continue;}
  if(/^\s*[-*+]\s+/.test(ln)){const it=[];while(i<L.length&&/^\s*[-*+]\s+/.test(L[i])){const ind=(L[i].match(/^\s*/)[0].length>=2);it.push({t:L[i].replace(/^\s*[-*+]\s+/,""),ind});i++;}
   let li="";it.forEach(x=>{const tm=x.t.match(/^\[([ xX])\]\s+(.*)/);
    if(tm){const done=tm[1].toLowerCase()==="x";li+='<li class="task'+(done?" done":"")+'">'+(done?"☑":"☐")+" "+inl(tm[2])+"</li>";}
    else li+='<li'+(x.ind?' style="margin-left:1.2em;list-style:circle"':"")+'>'+inl(x.t)+"</li>";});h+="<ul>"+li+"</ul>";continue;}
  if(ln.trim()===""){i++;continue;}
  const p=[ln];i++;while(i<L.length&&L[i].trim()!==""&&!/^\s*([-*+>#]|\d+\.|\||```)/.test(L[i])){p.push(L[i]);i++;}h+="<p>"+p.map(inl).join(" ")+"</p>";}
 return h;}
function propsPanel(rows){return '<div class="props"><div class="props-h">プロパティ</div>'
 +rows.map(([icn,key,val])=>'<div class="prow"><span class="pk">'+ic(icn)+'<span class="pkn">'+esc(key)+'</span></span><span class="pv">'+((val!==""&&val!=null)?esc(""+val):'<span class="pempty">値がありません</span>')+'</span></div>').join("")
 +'<div class="prow padd"><span class="pk">'+ic("plus")+'<span class="pkn">プロパティを追加</span></span></div></div>';}
function navTarget(t){if(t.indexOf("clients/")===0)return openClient(t.slice(8));if(t.indexOf("docs/")===0)return openDoc(t.slice(5));if(cByStem[t])return openClient(t);if(dByStem[t])return openDoc(t);const c=cByNorm[nrm(t)];if(c)return openClient(c.stem);}
function afterOpen(){
 $("#inner").querySelectorAll(".wl").forEach(el=>el.onclick=()=>navTarget(el.dataset.t));
 $("#inner").querySelectorAll(".tg,.ntag").forEach(el=>el.onclick=()=>{setPane("search");setTimeout(()=>{const i=$("#searchInput");i.value="tag:"+el.dataset.tag;window.__lastQ=i.value;runSearchPane(i.value,$("#searchOut"));},30);});
 renderRightTabs();renderRight();markActiveInTree();
 renderTabs();renderVhead();$("#docPane").scrollTop=0;$("#docPane").querySelector(".doc").scrollIntoView({block:"start"});
 const chars=($("#inner").querySelector(".md")?.textContent||"").length;const bl=((lastNote&&BACKL[lastNote.key])||[]).length;updateStatus(bl,chars);
}
/* ===== 施策タイムライン（カルテ内: Drive資料 + 商談FB をクライアント単位で時系列一元化） ===== */
const DTCOLOR={"提案書":"#8a7cf5","報告書":"#54b981","議事録":"#4f9df5","価格表":"#d0912f","契約":"#d0912f"};
function dtColor(t){t=t||"";if(DTCOLOR[t])return DTCOLOR[t];if(/価格|契約/.test(t))return "#d0912f";return "#8a8a8a";}
function tlEvents(c){
 const ev=(c.tl||[]).map(e=>({d:e.d||"",kind:"fb",e}));
 if(c.cnorm)DATA.docs.forEach(d=>{if(d.cnorm&&d.cnorm===c.cnorm)ev.push({d:d.modified||"",kind:"doc",e:d});});
 ev.sort((a,b)=>(b.d||"").localeCompare(a.d||""));  /* 日付降順・日付なしは末尾 */
 return ev;
}
function tlSection(c){
 const ev=tlEvents(c);if(!ev.length)return "";
 const rows=ev.map((x,i)=>{
  const cls="tlrow"+(x.kind==="doc"?" tldoc":"")+(i>=10?" tlhid":"");
  let card;
  if(x.kind==="doc"){const d=x.e,col=dtColor(d.doc_type);
   card='<span class="tlbadge" style="background:'+col+'22;color:'+col+'">'+esc(d.doc_type||"資料")+'</span>'
    +'<div class="tlt"><span class="wl" data-t="docs/'+esc(d.stem)+'">'+esc(d.title)+'</span></div>';
  }else{const f=x.e;
   card='<span class="tlbadge tlfb">商談FB'+(f.src?"・"+esc(f.src):"")+'</span>'
    +(f.ph?'<span class="tlchip">'+esc(f.ph)+'</span>':"")
    +(f.bant?'<span class="tlchip">'+esc(f.bant)+'</span>':"")
    +(f.menu?'<div class="tlt">'+esc(f.menu)+'</div>':"")
    +(f.pos?'<div class="tlx">'+esc(f.pos)+'</div>':"")
    +(f.next?'<div class="tlnx">→ 次: '+esc(f.next)+'</div>':"");
  }
  return '<div class="'+cls+'"><div class="tld">'+(x.d?esc(x.d):"—")+'</div><div class="tlcard">'+card+'</div></div>';
 }).join("");
 const more=ev.length>10?'<div class="tlmorebtn">さらに'+(ev.length-10)+'件を表示</div>':"";
 return '<div class="tlwrap'+(ev.length>10?" folded":"")+'"><div class="tlh">'+ic("cal")+'施策タイムライン<span class="cnt">'+ev.length+'</span></div><div class="tlbody">'+rows+'</div>'+more+'</div>';
}
function bindTl(){const mb=$("#inner").querySelector(".tlmorebtn");
 if(mb)mb.onclick=()=>{const w=mb.closest(".tlwrap");if(w)w.classList.remove("folded");mb.remove();};}
function openClient(stem){const c=cByStem[stem];if(!c)return;showDoc();pushHist("c",stem);
 lastNote={key:"c:"+stem,title:c.name,icon:"building",folder:"clients"};
 const bodyMd=c.md?md(c.md):"";
 $("#inner").innerHTML='<div class="inline-title">'+esc(c.name)+'</div>'
  +propsPanel([["list","client",c.name],["list","industry",c.industry],["list","deal_phase",c.phase],["list","bant_score",c.bant],["hash","fb_count",c.fb],["hash","doc_count",c.doc]])
  +tlSection(c)
  +'<div class="md">'+bodyMd+'</div>';
 bindTl();
 afterOpen();
}
function renderClientBody(m){return md(m).replace(/<h3>/g,'<div class="fbcard"><h3 style="border:none">').replace(/(<\/h3>[\s\S]*?)(?=<div class="fbcard">|$)/g,'$1</div>');}
function openDoc(stem){const d=dByStem[stem];if(!d)return;showDoc();pushHist("d",stem);
 lastNote={key:"d:"+stem,title:d.title,icon:"filetext",folder:"docs"};
 $("#inner").innerHTML='<div class="inline-title">'+esc(d.title)+'</div>'
  +propsPanel([["list","title",d.title],["list","doc_type",d.doc_type],["list","client",d.client],["list","industry",d.industry],["list","solution",d.solution],["cal","modified_at",d.modified]])
  +'<div class="md">'+md(d.md||("> "+(d.ex||"（要約なし）")))+'</div>';
 afterOpen();
}
function openReport(stem){const r=rByStem[stem];if(!r)return;showDoc();pushHist("r",stem);
 lastNote={key:"r:"+stem,title:r.name,icon:"report",folder:"_reports"};
 $("#inner").innerHTML='<div class="inline-title">'+esc(r.name)+'</div><div class="md">'+md(r.md)+'</div>';
 afterOpen();
}
/* ===== 右サイドバー（Obsidianコアプラグイン風タブ: バックリンク/アウトゴーイング/タグ/アウトライン） ===== */
let rTab="backlinks";
const RTABS=[["backlinks","linkin","バックリンク"],["outgoing","linkout","アウトゴーイングリンク"],["tags","hash","タグ"],["outline","list","アウトライン"]];
function renderRightTabs(){const el=$("#rtabs");if(!el)return;
 el.innerHTML=RTABS.map(([id,icn,t])=>'<div class="rt'+(id===rTab?" on":"")+'" data-rt="'+id+'" title="'+t+'">'+ic(icn)+'</div>').join("");
 el.querySelectorAll(".rt").forEach(t=>t.onclick=()=>{rTab=t.dataset.rt;renderRightTabs();renderRight();});}
function noteTags(){const n=lastNote;if(!n||!n.key)return [];const k=n.key[0],stem=n.key.slice(2);
 if(k==="c"&&cByStem[stem])return clientTags(cByStem[stem]);if(k==="d"&&dByStem[stem])return docTags(dByStem[stem]);return [];}
function keyExcerpt(sk){const m=keyMeta(sk);if(!m)return "";
 if(m.k==="d"){const d=dByStem[m.stem];return d?(d.ex||""):"";}
 if(m.k==="c"){const c=cByStem[m.stem];return c?(c.industry?"業種: "+c.industry+" ・ FB"+c.fb+" / 資料"+c.doc:""):"";}return "";}
function blItem(key,ctx,name){const meta=keyMeta(key);if(!meta)return "";
 let c="";if(ctx){c=esc(ctx);if(name&&name.length>1){try{const rx=new RegExp("("+name.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")+")","i");c=c.replace(rx,'<span class="hlq">$1</span>');}catch(e){}}c='<div class="blc">'+c+'</div>';}
 return '<div class="bl" data-k="'+meta.k+'" data-s="'+esc(meta.stem)+'"><div class="blt">'+ic(meta.icon)+esc(meta.title)+'</div>'+c+'</div>';}
function bindBL(b){b.querySelectorAll(".bl").forEach(el=>el.onclick=()=>openByK(el.dataset.k,el.dataset.s));}
function unlinkedMentions(name,linkedSet){const nm=(name||"").toLowerCase();if(nm.length<2)return [];
 const res=[];for(const it of IDX){const ik=it.kind[0]+":"+it.stem;if(linkedSet.has(ik))continue;if(it.hay.includes(nm)){res.push(ik);if(res.length>=40)break;}}return res;}
function renderOutlineInto(b){const heads=[...$("#inner").querySelectorAll(".md h1,.md h2,.md h3")];
 heads.forEach((h,i)=>h.id="h"+i);
 const out=heads.map((h,i)=>'<div class="olink" data-h="h'+i+'" style="padding-left:'+({H1:8,H2:8,H3:22}[h.tagName]||8)+'px'+(h.tagName!=="H3"?";color:var(--text)":"")+'">'+esc(h.textContent)+'</div>').join("")||'<div class="qhelp">見出しなし</div>';
 b.innerHTML='<div class="rtitle">アウトライン<span style="color:var(--faint);font-weight:400"> · '+heads.length+'</span></div>'+out;
 b.querySelectorAll(".olink[data-h]").forEach(el=>el.onclick=()=>{const t=document.getElementById(el.dataset.h);if(t)t.scrollIntoView({behavior:"smooth",block:"start"});});}
function renderRight(){
 const b=$("#rightBody");if(!b)return;const n=lastNote;
 if(!n||!n.key||n.key==="table"){b.innerHTML='<div class="qhelp">ノートを開くと、ここに<b>バックリンク</b>・アウトゴーイングリンク・タグ・アウトラインが表示されます。<br><br>上部タブで切替できます。</div>';return;}
 const key=n.key,name=n.title;
 if(rTab==="backlinks"){
  const back=(BACKL[key]||[]);const linkedSet=new Set(back.map(x=>x[0]));linkedSet.add(key);
  const unl=unlinkedMentions(name,linkedSet);
  let h='<div class="bkgrp"><div class="bkgrp-h">リンクされたメンション · '+back.length+'</div>';
  h+=back.length?back.map(([sk,ctx])=>blItem(sk,ctx,name)).join(""):'<div class="qhelp">このノートを参照しているノートはありません</div>';
  h+='</div><div class="bkgrp"><div class="bkgrp-h">リンクされていないメンション · '+unl.length+'</div>';
  h+=unl.length?unl.map(sk=>blItem(sk,keyExcerpt(sk),name)).join(""):'<div class="qhelp">なし</div>';
  h+='</div>';b.innerHTML=h;bindBL(b);
 }else if(rTab==="outgoing"){
  const out=(OUTL[key]||[]);
  b.innerHTML='<div class="bkgrp"><div class="bkgrp-h">アウトゴーイングリンク · '+out.length+'</div>'+(out.length?out.map(([tk,ctx])=>blItem(tk,ctx,"")).join(""):'<div class="qhelp">このノートからのリンクはありません</div>')+'</div>';
  bindBL(b);
 }else if(rTab==="tags"){
  const tags=noteTags();
  b.innerHTML='<div class="bkgrp"><div class="bkgrp-h">タグ · '+tags.length+'</div>'+(tags.length?tags.map(t=>'<div class="olink" data-tag="'+esc(t)+'">#'+esc(t)+'</div>').join(""):'<div class="qhelp">タグなし</div>')+'</div>';
  b.querySelectorAll(".olink[data-tag]").forEach(el=>el.onclick=()=>tagJump(el.dataset.tag));
 }else{renderOutlineInto(b);}
}
function tagJump(t){setPane("search");setTimeout(()=>{const i=$("#searchInput");i.value="tag:"+t;window.__lastQ=i.value;runSearchPane(i.value,$("#searchOut"));},30);}

/* ===== Bases風テーブル ===== */
/* 最終接点 = max(最終FB日, 関連docsの最新modified)。cnorm単位で事前集計 */
const LASTDOC={};
DATA.docs.forEach(d=>{if(d.cnorm&&d.modified&&(!(d.cnorm in LASTDOC)||d.modified>LASTDOC[d.cnorm]))LASTDOC[d.cnorm]=d.modified;});
function lastOf(c){const a=c.lastfb||"",b=(c.cnorm&&LASTDOC[c.cnorm])||"";return a>b?a:b;}
let tblSort={key:"act",dir:-1},tblFilter={q:"",ind:"",phase:""};
const TCOLS=[["name","取引先",c=>c.name,0],["industry","業界",c=>c.industry,0],["phase","フェーズ",c=>c.phase,0],["bant","BANT",c=>c.bant,0],["fb","FB",c=>c.fb,1],["doc","資料",c=>c.doc,1],["act","活動",c=>c.fb+c.doc,1],["last","最終接点",c=>lastOf(c),0]];
const PHASECOLOR={"受注":"#54b981","1回目提案":"#4f9df5","2回目提案":"#4f9df5","ケイパ":"#e0b34c","ヒアリング":"#c98bdb","失注":"#e0685f"};
function tableView(){showDoc();lastNote={key:"table",title:"取引先テーブル",icon:"table",folder:""};ribbonActive("table");
 renderTableShell();renderTabs();renderVhead();
 $("#rightBody").innerHTML='<div class="qhelp">Obsidian <b>Bases</b> 風テーブル。列見出しでソート、上部で業界/フェーズ絞り込み、行クリックでカルテ。</div>';}
function tblRows(){let rows=DATA.clients.slice();
 if(tblFilter.q){const q=tblFilter.q.toLowerCase();rows=rows.filter(c=>(c.name+c.industry+c.phase+c.bant).toLowerCase().includes(q));}
 if(tblFilter.ind)rows=rows.filter(c=>c.industry===tblFilter.ind);
 if(tblFilter.phase)rows=rows.filter(c=>c.phase===tblFilter.phase);
 const col=TCOLS.find(c=>c[0]===tblSort.key)||TCOLS[6];
 rows.sort((a,b)=>{let x=col[2](a),y=col[2](b);return col[3]?(x-y)*tblSort.dir:(""+x).localeCompare(""+y,"ja")*tblSort.dir;});return rows;}
function tblBody(shown){return shown.map(c=>{const pc=PHASECOLOR[c.phase]||"#8a8a8a";
 return '<tr data-s="'+esc(c.stem)+'"><td>'+esc(c.name)+'</td>'
  +'<td>'+(c.industry?'<span class="dotc" style="background:'+colorOf(c.industry)+'"></span>'+esc(c.industry):'<span style="color:var(--faint)">—</span>')+'</td>'
  +'<td>'+(c.phase?'<span class="bp" style="background:'+pc+'22;color:'+pc+'">'+esc(c.phase)+'</span>':'<span style="color:var(--faint)">—</span>')+'</td>'
  +'<td>'+esc(c.bant||"—")+'</td><td class="num">'+c.fb+'</td><td class="num">'+c.doc+'</td><td class="num">'+(c.fb+c.doc)+'</td>'
  +'<td>'+(lastOf(c)?esc(lastOf(c)):'<span style="color:var(--faint)">—</span>')+'</td></tr>';}).join("");}
function updateTable(){const rows=tblRows();const tb=$("#inner").querySelector("tbody");if(!tb)return;
 tb.innerHTML=tblBody(rows.slice(0,500));
 $("#inner").querySelector(".bar .n").textContent=rows.length+" 件"+(rows.length>500?"（先頭500表示）":"");
 $("#inner").querySelectorAll("thead th").forEach(th=>{const a=th.querySelector(".ar");if(a)a.remove();if(th.dataset.k===tblSort.key)th.insertAdjacentHTML("beforeend",'<span class="ar">'+(tblSort.dir>0?"▲":"▼")+'</span>');});
 tb.querySelectorAll("tr").forEach(tr=>tr.onclick=()=>openClient(tr.dataset.s));}
function renderTableShell(){
 const inds=[...new Set(DATA.clients.map(c=>c.industry).filter(Boolean))].sort();
 const phases=[...new Set(DATA.clients.map(c=>c.phase).filter(Boolean))].sort();
 const optI='<option value="">業界（すべて）</option>'+inds.map(i=>'<option'+(tblFilter.ind===i?" selected":"")+'>'+esc(i)+'</option>').join("");
 const optP='<option value="">フェーズ（すべて）</option>'+phases.map(p=>'<option'+(tblFilter.phase===p?" selected":"")+'>'+esc(p)+'</option>').join("");
 const head=TCOLS.map(c=>'<th data-k="'+c[0]+'"'+(c[3]?' class="num"':'')+'>'+c[1]+'</th>').join("");
 $("#inner").innerHTML='<div class="tbv"><div class="th1">'+ic("table")+'取引先テーブル</div>'
  +'<p class="sub">'+DATA.stats.clients+' 取引先を業界・フェーズ・BANT・FB数で分類。列見出しでソート、行クリックでカルテを開く（Bases風）。</p>'
  +'<div class="bar"><input id="tblq" placeholder="絞り込み…" value="'+esc(tblFilter.q)+'"><select id="tblind">'+optI+'</select><select id="tblph">'+optP+'</select><span class="n"></span></div>'
  +'<div class="tblwrap"><table><thead><tr>'+head+'</tr></thead><tbody></tbody></table></div></div>';
 $("#tblq").addEventListener("input",e=>{tblFilter.q=e.target.value;updateTable();});
 $("#tblind").addEventListener("change",e=>{tblFilter.ind=e.target.value;updateTable();});
 $("#tblph").addEventListener("change",e=>{tblFilter.phase=e.target.value;updateTable();});
 $("#inner").querySelectorAll("thead th").forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(tblSort.key===k)tblSort.dir*=-1;else{tblSort.key=k;tblSort.dir=(k==="fb"||k==="doc"||k==="act"||k==="last")?-1:1;}updateTable();});
 updateTable();}

/* ===== グラフ ===== */
let G=null;
function openGraph(){showGraphView();if(!G){G="init";setTimeout(initGraph,30);}}
function initGraph(){
 const cv=$("#graph"),ctx=cv.getContext("2d"),tip=$("#gtip");const dpr=window.devicePixelRatio||1;
 function resize(){cv.width=cv.clientWidth*dpr;cv.height=cv.clientHeight*dpr;}
 resize();window.addEventListener("resize",resize);
 const N=DATA.graph.nodes.map((n,i)=>{const a=i*2.399963,rad=Math.sqrt(i)*16;return {...n,x:Math.cos(a)*rad+.01,y:Math.sin(a)*rad,vx:0,vy:0};});
 const L=DATA.graph.links.map(p=>({s:p[0],t:p[1]}));
 N.forEach(n=>n.r=n.type==="doc"?2.2:Math.min(3+Math.sqrt(n.deg)*1.5,n.type==="tag"?20:14));
 const neigh={};L.forEach(l=>{(neigh[l.s]=neigh[l.s]||new Set()).add(l.t);(neigh[l.t]=neigh[l.t]||new Set()).add(l.s);});
 const opt={nodeSize:1,linkW:1,textFade:1,repel:46,center:.011,linkDist:26,showDocs:true,showTags:true,hideOrphan:false,groupBy:"industry",filter:""};
 function vis(i){const n=N[i];if(!opt.showDocs&&n.type==="doc")return false;if(!opt.showTags&&n.type==="tag")return false;if(opt.hideOrphan&&!neigh[i])return false;if(opt.filter&&!n.label.toLowerCase().includes(opt.filter))return false;return true;}
 function ncol(n){if(n.type==="tag")return "hsl(254,42%,72%)";if(n.type==="doc")return "rgba(150,150,156,.6)";return opt.groupBy==="phase"?(PHASECOLOR[n.phase]||"#9a9a9a"):colorOf(n.industry);}
 let view={x:0,y:0,z:1},hover=-1,drag=-1,pan=false,px=0,py=0,alpha=1;const aMin=.02,aDec=.977;
 function mk(x,y,w){return {x:x,y:y,w:w,m:0,sx:0,sy:0,body:-1,kids:null};}
 function qPlace(cell,i,depth){const n=N[i],hw=cell.w/2,q=(n.x>=cell.x+hw?1:0)+(n.y>=cell.y+hw?2:0);qInsert(cell.kids[q],i,depth+1);}
 function qInsert(cell,i,depth){const n=N[i];cell.m++;cell.sx+=n.x;cell.sy+=n.y;
  if(cell.kids!==null){qPlace(cell,i,depth);return;}
  if(cell.body===-1){cell.body=i;return;}
  if(depth>=22)return;
  const hw=cell.w/2;cell.kids=[mk(cell.x,cell.y,hw),mk(cell.x+hw,cell.y,hw),mk(cell.x,cell.y+hw,hw),mk(cell.x+hw,cell.y+hw,hw)];
  const b=cell.body;cell.body=-1;qPlace(cell,b,depth);qPlace(cell,i,depth);}
 function qForce(cell,i,rep,a,th2){if(cell.m===0)return;const n=N[i],cx=cell.sx/cell.m,cy=cell.sy/cell.m;let dx=n.x-cx,dy=n.y-cy,d2=dx*dx+dy*dy+9;
  if(cell.kids===null||cell.w*cell.w/d2<th2){if(cell.body===i)return;const f=rep*cell.m/d2*a;n.vx+=dx*f;n.vy+=dy*f;}
  else for(let q=0;q<4;q++)qForce(cell.kids[q],i,rep,a,th2);}
 function step(a){let mnx=1e9,mny=1e9,mxx=-1e9,mxy=-1e9,any=false;
  for(let i=0;i<N.length;i++){if(!vis(i))continue;any=true;const n=N[i];if(n.x<mnx)mnx=n.x;if(n.y<mny)mny=n.y;if(n.x>mxx)mxx=n.x;if(n.y>mxy)mxy=n.y;}
  if(!any)return;const root=mk(mnx-1,mny-1,Math.max(mxx-mnx,mxy-mny,1)+2);
  for(let i=0;i<N.length;i++)if(vis(i))qInsert(root,i,0);
  for(let i=0;i<N.length;i++)if(vis(i))qForce(root,i,opt.repel,a,.49);
  for(let k=0;k<L.length;k++){const l=L[k];if(!vis(l.s)||!vis(l.t))continue;const A=N[l.s],B=N[l.t];let dx=B.x-A.x,dy=B.y-A.y,d=Math.sqrt(dx*dx+dy*dy)||1,f=(d-opt.linkDist)*.006*a;A.vx+=dx/d*f;A.vy+=dy/d*f;B.vx-=dx/d*f;B.vy-=dy/d*f;}
  for(let i=0;i<N.length;i++){if(!vis(i))continue;const n=N[i];n.vx-=n.x*opt.center*a;n.vy-=n.y*opt.center*a;n.vx*=.82;n.vy*=.82;
   const sp=n.vx*n.vx+n.vy*n.vy;if(sp>2500){const s=50/Math.sqrt(sp);n.vx*=s;n.vy*=s;}
   n.x+=n.vx;n.y+=n.vy;
   if(!isFinite(n.x)||!isFinite(n.y)){n.x=(i%60-30)*8;n.y=(((i/60)|0)%60-30)*8;n.vx=n.vy=0;}}}
 for(let k=0;k<160;k++)step(1);
 function fit(){let a=1e9,b=1e9,c=-1e9,d=-1e9;N.forEach(n=>{a=Math.min(a,n.x);b=Math.min(b,n.y);c=Math.max(c,n.x);d=Math.max(d,n.y);});const w=cv.clientWidth,h=cv.clientHeight;view.z=Math.max(.25,Math.min(2.2,.82*Math.min(w/((c-a)||1),h/((d-b)||1))));view.x=-(a+c)/2;view.y=-(b+d)/2;}
 fit();
 const S=n=>({x:cv.width/2+(n.x+view.x)*view.z*dpr,y:cv.height/2+(n.y+view.y)*view.z*dpr});
 function draw(){const DK=document.documentElement.getAttribute("data-theme")==="dark";ctx.clearRect(0,0,cv.width,cv.height);ctx.lineWidth=dpr*opt.linkW;
  const lNorm=DK?"rgba(255,255,255,.06)":"rgba(0,0,0,.085)",lHov=DK?"hsla(254,74%,74%,.6)":"hsla(254,60%,52%,.7)",lbl=DK?"#c8c9cf":"#33343a";
  L.forEach(l=>{if(!vis(l.s)||!vis(l.t))return;const A=S(N[l.s]),B=S(N[l.t]);const on=hover>=0&&(l.s===hover||l.t===hover);ctx.strokeStyle=on?lHov:lNorm;ctx.beginPath();ctx.moveTo(A.x,A.y);ctx.lineTo(B.x,B.y);ctx.stroke();});
  N.forEach((n,i)=>{if(!vis(i))return;const p=S(n);const dim=hover>=0&&i!==hover&&!(neigh[hover]&&neigh[hover].has(i));ctx.globalAlpha=dim?.2:1;
   const rr=Math.max(n.r*opt.nodeSize*view.z*dpr,.5*dpr);ctx.fillStyle=ncol(n);ctx.beginPath();ctx.arc(p.x,p.y,rr,0,7);ctx.fill();  /* 遠景でも点として残す下限 */
   if((n.type==="client"&&rr>6.5/opt.textFade)||i===hover){ctx.globalAlpha=dim?.3:1;ctx.fillStyle=lbl;ctx.font=(11*dpr)+"px InterVar,Inter,-apple-system,sans-serif";ctx.fillText(n.label,p.x+rr+4,p.y+4*dpr);}});ctx.globalAlpha=1;}
 function pick(mx,my){let best=-1,bd=15*dpr;N.forEach((n,i)=>{if(!vis(i))return;const p=S(n);const dd=Math.hypot(p.x-mx,p.y-my);if(dd<Math.max(n.r*opt.nodeSize*view.z*dpr+5*dpr,15*dpr)&&dd<bd){bd=dd;best=i;}});return best;}
 let run=false;function ensure(){if(!run){run=true;requestAnimationFrame(loop);}}
 function loop(){if(alpha>aMin){step(alpha);alpha*=aDec;draw();requestAnimationFrame(loop);}else if(drag>=0){step(.25);draw();requestAnimationFrame(loop);}else{draw();run=false;}}
 cv.onmousemove=e=>{const r=cv.getBoundingClientRect(),mx=(e.clientX-r.left)*dpr,my=(e.clientY-r.top)*dpr;
  if(drag>=0){N[drag].x=(mx-cv.width/2)/(view.z*dpr)-view.x;N[drag].y=(my-cv.height/2)/(view.z*dpr)-view.y;N[drag].vx=N[drag].vy=0;return;}
  if(pan){view.x+=(mx-px)/(view.z*dpr);view.y+=(my-py)/(view.z*dpr);px=mx;py=my;draw();return;}
  const hh=pick(mx,my);cv.style.cursor=hh>=0?"pointer":"grab";
  if(hh>=0){const n=N[hh];tip.style.display="block";tip.style.left=(e.clientX+14)+"px";tip.style.top=(e.clientY+14)+"px";tip.innerHTML=n.type==="client"?('<b>'+esc(n.label)+'</b><div class="gi">'+esc(n.industry||"業界未設定")+" ・ FB"+n.fb+" / 資料"+n.doc+"</div>"):n.type==="tag"?('<b>'+esc(n.label)+'</b><div class="gi">タグ</div>'):('<b>'+esc(n.label)+'</b><div class="gi">資料</div>');}else tip.style.display="none";
  if(hh!==hover){hover=hh;draw();}};
 cv.onmousedown=e=>{const r=cv.getBoundingClientRect(),mx=(e.clientX-r.left)*dpr,my=(e.clientY-r.top)*dpr;const h=pick(mx,my);if(h>=0){drag=h;alpha=Math.max(alpha,.35);ensure();}else{pan=true;px=mx;py=my;}};
 window.addEventListener("mouseup",()=>{drag=-1;pan=false;});
 cv.onclick=e=>{const r=cv.getBoundingClientRect(),mx=(e.clientX-r.left)*dpr,my=(e.clientY-r.top)*dpr;const h=pick(mx,my);if(h>=0){const n=N[h];tip.style.display="none";n.type==="client"?openClient(n.id.slice(2)):n.type==="tag"?tagJump(n.id.slice(2)):openDoc(n.id.slice(2));}};
 cv.onmouseleave=()=>{tip.style.display="none";if(hover!==-1){hover=-1;draw();}};
 cv.onwheel=e=>{e.preventDefault();view.z*=e.deltaY<0?1.1:.9;view.z=Math.max(.02,Math.min(6,view.z));draw();};  /* 下限0.02で本家並みに遠景まで引ける */
 // 対話コントロールパネル（Filters/Groups/Display/Forces）
 const inds=[...new Set(N.filter(n=>n.type==="client"&&n.industry).map(n=>n.industry))].slice(0,9);
 function legend(){const base=opt.groupBy==="industry"?inds.map(i=>'<div class="grow"><span class="dot" style="background:'+colorOf(i)+'"></span>'+esc(i)+'</div>').join(""):Object.keys(PHASECOLOR).map(p=>'<div class="grow"><span class="dot" style="background:'+PHASECOLOR[p]+'"></span>'+esc(p)+'</div>').join("");return base+'<div class="grow"><span class="dot" style="background:hsl(254,42%,72%)"></span>タグ</div><div class="grow"><span class="dot" style="background:rgba(150,150,156,.7)"></span>資料</div>';}
 $("#gpanel").innerHTML=
  '<div class="gph"><span class="gpt">グラフ設定</span><span class="gpx" id="gpClose">✕</span></div>'
  +'<div class="gsec"><div class="gh">フィルター</div><input class="gin" id="gFilter" placeholder="ノードを検索…">'
  +'<label class="gck"><input type="checkbox" id="gTags" checked> タグを表示</label>'
  +'<label class="gck"><input type="checkbox" id="gDocs" checked> 資料ノードを表示</label>'
  +'<label class="gck"><input type="checkbox" id="gOrph"> 孤立ノードを隠す</label></div>'
  +'<div class="gsec"><div class="gh">グループ（色分け）</div>'
  +'<label class="gck"><input type="radio" name="gb" id="gbInd" checked> 業種で色分け</label>'
  +'<label class="gck"><input type="radio" name="gb" id="gbPh"> フェーズで色分け</label>'
  +'<div class="gleg" id="gLeg">'+legend()+'</div></div>'
  +'<div class="gsec"><div class="gh">表示</div>'
  +'<div class="gsl"><span>ノード径</span><input type="range" id="gNs" min="0.5" max="2.5" step="0.1" value="1"></div>'
  +'<div class="gsl"><span>リンク太さ</span><input type="range" id="gLw" min="0.5" max="3" step="0.1" value="1"></div>'
  +'<div class="gsl"><span>ラベル閾値</span><input type="range" id="gTf" min="0.4" max="2" step="0.1" value="1"></div></div>'
  +'<div class="gsec"><div class="gh">力の強さ</div>'
  +'<div class="gsl"><span>反発</span><input type="range" id="gRe" min="15" max="150" step="1" value="46"></div>'
  +'<div class="gsl"><span>中心力</span><input type="range" id="gCe" min="0" max="0.03" step="0.001" value="0.011"></div>'
  +'<div class="gsl"><span>リンク距離</span><input type="range" id="gLd" min="10" max="80" step="2" value="26"></div></div>';
 const dr=()=>draw(),rh=()=>{alpha=Math.max(alpha,.5);ensure();};
 $("#gFilter").oninput=e=>{opt.filter=e.target.value.toLowerCase();dr();};
 $("#gTags").onchange=e=>{opt.showTags=e.target.checked;dr();};
 $("#gDocs").onchange=e=>{opt.showDocs=e.target.checked;dr();};
 $("#gOrph").onchange=e=>{opt.hideOrphan=e.target.checked;dr();};
 $("#gbInd").onchange=()=>{opt.groupBy="industry";$("#gLeg").innerHTML=legend();dr();};
 $("#gbPh").onchange=()=>{opt.groupBy="phase";$("#gLeg").innerHTML=legend();dr();};
 $("#gNs").oninput=e=>{opt.nodeSize=+e.target.value;dr();};
 $("#gLw").oninput=e=>{opt.linkW=+e.target.value;dr();};
 $("#gTf").oninput=e=>{opt.textFade=+e.target.value;dr();};
 $("#gRe").oninput=e=>{opt.repel=+e.target.value;rh();};
 $("#gCe").oninput=e=>{opt.center=+e.target.value;rh();};
 $("#gLd").oninput=e=>{opt.linkDist=+e.target.value;rh();};
 $("#gGear").innerHTML=ic("settings");
 $("#gGear").onclick=()=>{$("#gpanel").classList.remove("hidden");$("#gGear").style.display="none";};
 $("#gpClose").onclick=()=>{$("#gpanel").classList.add("hidden");$("#gGear").style.display="flex";};
 // レイアウト確定前にresizeするとcanvasが0×0になる→ResizeObserverで確実に同期+再描画（サイドバー幅変更にも追従）
 if(window.ResizeObserver){new ResizeObserver(()=>{const w=cv.clientWidth*dpr,h=cv.clientHeight*dpr;if(w&&(w!==cv.width||h!==cv.height)){cv.width=w;cv.height=h;draw();}}).observe(cv);}
 window.__gRedraw=draw; G={};run=true;requestAnimationFrame(loop);
}

/* ===== Quick switcher ===== */
let qItems=[],qSel=0,qMode="nav";
const CMDS=[["グラフビューを開く","graph",()=>openGraph()],["取引先テーブル(Bases)を開く","table",()=>tableView()],["ホームを開く","files",()=>{lastNote=null;showDoc();welcome();renderTabs();renderVhead();}],["検索を開く","search",()=>setPane("search")],["タグを開く","tags",()=>setPane("tags")],["ブックマークを開く","bookmark",()=>setPane("bookmark")],["レポート: フォロー漏れ洗い出し","report",()=>openReport("followup_gaps")],["レポート: クライアント名寄せ","report",()=>openReport("name_merge_candidates")],["レポート: テンプレ検出","report",()=>openReport("boilerplate_detected")]];
const QI=DATA.clients.map(c=>({k:"c",stem:c.stem,name:c.name,pt:c.industry||"取引先",ico:"building"})).concat(DATA.docs.map(d=>({k:"d",stem:d.stem,name:d.title,pt:d.client||"資料",ico:"filetext"}))).concat(DATA.reports.map(r=>({k:"r",stem:r.stem,name:r.name,pt:"レポート",ico:"report"})));
function qsOpen(mode){qMode=mode||"nav";$("#qsov").classList.add("on");const i=$("#qsin");i.value="";i.placeholder=qMode==="cmd"?"コマンドを実行…  (⌘P)":"ノートに移動…  (⌘O)";qsR("");i.focus();}
function qsClose(){$("#qsov").classList.remove("on");}
function qsR(q){q=q.toLowerCase().trim();
 if(qMode==="cmd"){qItems=(q?CMDS.filter(c=>c[0].toLowerCase().includes(q)):CMDS).slice(0,20);qSel=0;
  $("#qslist").innerHTML=qItems.map((x,i)=>'<div class="qi'+(i===0?" sel":"")+'" data-i="'+i+'">'+ic(x[1])+'<span class="nm">'+esc(x[0])+'</span></div>').join("")||'<div class="qi">一致なし</div>';
  $("#qslist").querySelectorAll(".qi[data-i]").forEach(el=>el.onclick=()=>qsP(+el.dataset.i));return;}
 qItems=(q?QI.filter(x=>x.name&&x.name.toLowerCase().includes(q)):QI.filter(x=>x.k==="c")).slice(0,20);qSel=0;
 $("#qslist").innerHTML=qItems.map((x,i)=>'<div class="qi'+(i===0?" sel":"")+'" data-i="'+i+'">'+ic(x.ico)+'<span class="nm">'+esc(x.name.slice(0,56))+'</span><span class="pt">'+esc(x.pt)+'</span></div>').join("")||'<div class="qi">一致なし</div>';
 $("#qslist").querySelectorAll(".qi[data-i]").forEach(el=>el.onclick=()=>qsP(+el.dataset.i));}
function qsMove(d){qSel=Math.max(0,Math.min(qItems.length-1,qSel+d));$("#qslist").querySelectorAll(".qi").forEach((el,i)=>el.classList.toggle("sel",i===qSel));const s=$("#qslist").querySelector(".qi.sel");if(s)s.scrollIntoView({block:"nearest"});}
function qsP(i){const x=qItems[i];if(!x)return;qsClose();if(qMode==="cmd")x[2]();else openByK(x.k,x.stem);}
$("#qsin").addEventListener("input",e=>qsR(e.target.value));
$("#qsin").addEventListener("keydown",e=>{if(e.key==="ArrowDown"){e.preventDefault();qsMove(1);}else if(e.key==="ArrowUp"){e.preventDefault();qsMove(-1);}else if(e.key==="Enter"){e.preventDefault();qsP(qSel);}else if(e.key==="Escape")qsClose();});
$("#qsov").addEventListener("click",e=>{if(e.target.id==="qsov")qsClose();});
document.addEventListener("keydown",e=>{const k=e.key.toLowerCase();if((e.metaKey||e.ctrlKey)&&k==="o"){e.preventDefault();qsOpen("nav");}else if((e.metaKey||e.ctrlKey)&&k==="p"){e.preventDefault();qsOpen("cmd");}else if(e.key==="Escape")qsClose();});
/* ホバープレビュー */
const hpop=document.createElement("div");hpop.id="hoverpop";document.body.appendChild(hpop);let hpT=null;
const HPSEL=".wl,.sr,.bl";
function prevOf(k,stem){if(k==="c"){const c=cByStem[stem];if(!c)return null;return{t:c.name,x:(c.industry?"業種: "+c.industry+" ・ ":"")+"FB"+c.fb+" / 資料"+c.doc+"\n"+(c.md||"").replace(/[#>*_`]/g," ").replace(/\s+/g," ").slice(0,170)};}
 if(k==="d"){const d=dByStem[stem];if(!d)return null;return{t:d.title,x:(d.client?"取引先: "+d.client+"\n":"")+(d.ex||"（要約なし）")};}
 if(k==="r"){const r=rByStem[stem];if(!r)return null;return{t:r.name,x:r.md.replace(/[#>*_`|=-]/g," ").replace(/\s+/g," ").slice(0,170)};}return null;}
function prevFromEl(el){if(el.classList.contains("wl")){const t=el.dataset.t;if(t.indexOf("clients/")===0)return prevOf("c",t.slice(8));if(t.indexOf("docs/")===0)return prevOf("d",t.slice(5));if(cByStem[t])return prevOf("c",t);if(dByStem[t])return prevOf("d",t);const c=cByNorm[nrm(t)];if(c)return prevOf("c",c.stem);return null;}
 if(el.dataset.s)return prevOf(el.dataset.k||"d",el.dataset.s);return null;}
let hpEl=null,hpHideT=null;   // ちらつき対策: 同一要素内の移動では消さず、離脱は遅延で消す
document.addEventListener("mouseover",e=>{const el=e.target.closest(HPSEL);if(!el)return;
 clearTimeout(hpHideT);
 if(el===hpEl)return;                       // 既に同じ要素をホバー中 → 何もしない
 hpEl=el;clearTimeout(hpT);
 hpT=setTimeout(()=>{if(hpEl!==el)return;const p=prevFromEl(el);if(!p){hpop.style.display="none";return;}
  hpop.innerHTML='<div class="hpt">'+esc(p.t)+'</div><div class="hpx">'+esc(p.x||"")+'</div>';
  const r=el.getBoundingClientRect();hpop.style.display="block";let y=r.bottom+6;if(y+170>innerHeight)y=r.top-170;
  hpop.style.left=Math.min(Math.max(6,r.left),innerWidth-316)+"px";hpop.style.top=Math.max(6,y)+"px";},300);});
document.addEventListener("mouseout",e=>{const el=e.target.closest(HPSEL);if(!el||el!==hpEl)return;
 clearTimeout(hpT);hpHideT=setTimeout(()=>{hpop.style.display="none";hpEl=null;},150);});

/* ===== status ===== */
function updateStatus(bl,chars){$("#statusbar").innerHTML=(chars?'<span><b>'+chars.toLocaleString()+'</b> 文字</span><span><b>'+bl+'</b> バックリンク</span>':'')+'<span><b>'+DATA.stats.clients+'</b> 取引先</span><span><b>'+DATA.stats.docs+'</b> 資料</span><span>__BUILDSTAMP__</span>';}
updateStatus(0,0);

/* ===== 起動 ===== */
/* サイドバー幅ドラッグ調整 */
(function(){let cur=null;const root=document.documentElement;
 document.querySelectorAll(".rzh").forEach(h=>h.addEventListener("mousedown",e=>{e.preventDefault();cur=h.dataset.rz;h.classList.add("drag");document.body.style.cursor="col-resize";document.body.style.userSelect="none";}));
 window.addEventListener("mousemove",e=>{if(!cur)return;
  if(cur==="side")root.style.setProperty("--side",Math.max(180,Math.min(520,e.clientX-44))+"px");
  else root.style.setProperty("--right",Math.max(200,Math.min(560,window.innerWidth-e.clientX))+"px");});
 window.addEventListener("mouseup",()=>{if(!cur)return;document.querySelectorAll(".rzh").forEach(h=>h.classList.remove("drag"));document.body.style.cursor="";document.body.style.userSelect="";cur=null;window.dispatchEvent(new Event("resize"));});
})();
(function(){function os(){return window.matchMedia&&window.matchMedia("(prefers-color-scheme:dark)").matches?"dark":"light";}
 let saved=null;try{saved=localStorage.getItem("aila_theme");}catch(e){}
 applyTheme(saved||os(),false);
 try{window.matchMedia&&window.matchMedia("(prefers-color-scheme:dark)").addEventListener("change",e=>{let s=null;try{s=localStorage.getItem("aila_theme");}catch(_){}if(!s)applyTheme(e.matches?"dark":"light",false);});}catch(e){}
})();
renderRibbon();setPane("files");welcome();renderTabs();renderVhead();

/* ===== ディープリンク（#client: / #doc: / #open:）: Slack 検索結果 → 該当ノート直行 ===== */
/* URL 例: /app#client:%E6%A0%AA%E5%BC%8F...（生成は teamagent の build_app_client_link が真実源）。
   名前解決は既存 navTarget()（stem 直一致 → nrm() 正規化一致の二段）に委譲＝生の日本語名でよい。
   フラグメントはログイン境界（Google Sign-In の form POST → 303 /app）で消えるため、
   /search/login が sessionStorage 'ailavault.pendingHash' へ退避したものをフォールバックで
   読む（connect_web/app.py の search_login と対。キー名を変えるなら両方変える）。
   解決できないターゲットは silent no-op（welcome 表示のまま＝壊れない）。 */
function _dlDecode(s){try{return decodeURIComponent(s);}catch(e){return s;}}
function applyHashTarget(){
 let h=(location.hash||"").replace(/^#/,"");
 if(!h){try{h=(sessionStorage.getItem("ailavault.pendingHash")||"").replace(/^#/,"");if(h)sessionStorage.removeItem("ailavault.pendingHash");}catch(e){}}
 if(!h)return;
 if(h.indexOf("client:")===0){navTarget(_dlDecode(h.slice(7)));return;}
 if(h.indexOf("doc:")===0){const s=_dlDecode(h.slice(4));if(dByStem[s])openDoc(s);else navTarget(s);return;}
 if(h.indexOf("open:")===0){navTarget(_dlDecode(h.slice(5)));return;}
}
window.addEventListener("hashchange",applyHashTarget);
applyHashTarget();
</script></body></html>"""


# ============================================================================
# repo 版で追加した運用ガード（fail-loud / サニティゲート / CLI）
# ============================================================================


def _die(msg: str) -> None:
    """理由を明示して exit 1（silent fallback 禁止・「黙って劣化」を作らない）。"""
    print(f"[ERROR] build_app_html: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _read_sidecar(name: str) -> str:
    """repo 同梱サイドカーを読む。欠落は即 exit 1（exists() フォールバック全廃）。"""
    path = SIDECAR_DIR / name
    if not path.is_file():
        _die(
            f"サイドカー {name} がありません: {path}。"
            "フィルタ無しで生成すると非ナレッジ混入/重複表示で黙って劣化するため中止。"
            "git checkout で data/connect_web_filters/ を復元してから再実行してください"
        )
    return path.read_text(encoding="utf-8")


def _stats_path(out: Path) -> Path:
    return Path(str(out) + ".stats.json")


def _sanity_gate(stats_path: Path, new_stats: dict[str, int], allow_shrink: bool) -> None:
    """前回統計との比較。取引先数/資料数/バイト数のいずれかが 20% 超減なら exit 1。

    典型事故: Vault の部分 export・サイドカーの過剰除外・ingest の取りこぼしで
    データが痩せたまま 16 名へ配信してしまうこと。意図した縮小は --allow-shrink で明示。
    """
    if not stats_path.exists():
        print(f"ℹ️  前回統計なし（初回生成）: サニティゲートはスキップ → {stats_path}")
        return
    try:
        prev = json.loads(stats_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _die(
            f"前回統計 {stats_path} が読めません（破損の可能性）: {exc}。"
            "内容を確認のうえ、意図的に基準をリセットするならファイルを削除して再実行してください"
        )
    if not isinstance(prev, dict):
        _die(f"前回統計 {stats_path} の形式が不正です（dict でない）。確認のうえ削除して再実行してください")
    shrunk: list[str] = []
    for key, label in (("clients", "取引先数"), ("docs", "資料数"), ("bytes", "バイト数")):
        prev_v = prev.get(key)
        if not isinstance(prev_v, (int, float)) or isinstance(prev_v, bool) or prev_v <= 0:
            continue  # 旧形式や欠損キーは比較対象外（新統計の保存で次回から効く）
        new_v = new_stats[key]
        ratio = (prev_v - new_v) / prev_v
        if ratio > SHRINK_LIMIT:
            shrunk.append(f"{label}: {prev_v} → {new_v}（-{ratio:.0%}）")
    if not shrunk:
        return
    detail = " / ".join(shrunk)
    if allow_shrink:
        print(f"⚠️  前回比 {SHRINK_LIMIT:.0%} 超の縮小を検出したが --allow-shrink 指定のため続行: {detail}")
        return
    _die(
        f"サニティゲート: 前回比 {SHRINK_LIMIT:.0%} 超の縮小を検出したため生成を中止（既存 out は保持）: {detail}。"
        "Vault の export 漏れ/サイドカーの過剰除外を疑ってください。"
        "意図した縮小なら --allow-shrink を付けて再実行してください"
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="connect-web app.html 生成器（Obsidian 完全再現・repo 版）",
    )
    parser.add_argument("--vault", default=str(DEFAULT_VAULT), help=f"Vault ルート（既定: {DEFAULT_VAULT}）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"出力 HTML パス（既定: {DEFAULT_OUT}）")
    parser.add_argument(
        "--allow-shrink",
        action="store_true",
        help="サニティゲート（前回比20%%超減で exit 1）を明示的に通過させる",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global VAULT, CLIENTS, DOCS, REPORTS, EXCL, EXCL_N, DOC_DROP, TITLE_OVERRIDE, CHUNK_DROP

    args = _parse_args(argv)
    VAULT = Path(args.vault).expanduser()
    CLIENTS, DOCS, REPORTS = VAULT / "clients", VAULT / "docs", VAULT / "_reports"
    out = Path(args.out).expanduser()

    # --- fail-loud: Vault 不在 ---
    if not VAULT.is_dir():
        _die(f"Vault が見つかりません: {VAULT}。scripts/export_vault.py --commit 済みか確認してください")
    for sub in (CLIENTS, DOCS):
        if not sub.is_dir():
            _die(f"Vault 構造が不正です: {sub} がありません（export_vault.py の出力形式を確認）")

    # --- fail-loud: サイドカー欠落は一括検知 ---
    missing = [name for name in SIDECAR_FILES if not (SIDECAR_DIR / name).is_file()]
    if missing:
        _die(f"サイドカー欠落: {', '.join(missing)}（{SIDECAR_DIR}）。git checkout で復元してください")

    # --- サイドカー読み込み（元スクリプトの exists() フォールバックを全廃） ---
    EXCL = set(json.loads(_read_sidecar("exclude_stems.json")))
    EXCL_N = {_exn(s) for s in EXCL}
    DOC_DROP = set((json.loads(_read_sidecar("dedup_drop_map.json")) or {}).get("drop", {}).keys())
    TITLE_OVERRIDE = json.loads(_read_sidecar("weird_rename_high.json"))
    font_b64 = _read_sidecar("inter-var.b64").strip()
    if not font_b64:
        _die(f"サイドカー inter-var.b64 が空です: {SIDECAR_DIR / 'inter-var.b64'}")
    CHUNK_DROP = _compute_chunk_drop()

    # --- 以下、元スクリプト L149-332 のパイプラインをそのまま実行（ロジック不変） ---
    clients = []
    for f in sorted(CLIENTS.glob("*.md")):
        if _is_self_org(f.stem) or f.stem in JUNK_CLIENTS:   # 自社(NewsTV)/テスト・ダミーカルテは取引先化しない
            continue
        t = f.read_text(errors="replace")
        fm = front(t)
        if _is_self_org(fm.get("client") or ""):
            continue
        _cname = fm.get("client") or f.stem
        clients.append({
            "stem": f.stem, "name": _cname, "cnorm": norm(_cname),
            "industry": fm.get("industry", ""), "phase": fm.get("deal_phase", ""),
            "bant": fm.get("bant_score", ""), "bantg": bant_short(fm.get("bant_score", "")),
            "fb": to_int(fm.get("fb_count", "0")), "doc": to_int(fm.get("doc_count", "0")),
            "md": client_md(t), "tl": parse_fb_events(body_of(t)),
            "_wl": parse_links(body_of(t)),
        })

    # 取引先名寄せ(Tier1): 正規化が一致する表記ゆれ(法人格/敬称/空白/中黒)を正本(最短名)へ統合。件数合算・元Vault不変
    _cgroups = defaultdict(list)
    for _c in clients:
        _cgroups[norm(_c["name"])].append(_c)
    clients = []
    for _grp in _cgroups.values():
        _canon = min(_grp, key=lambda c: (len(c["name"]), c["name"]))
        _canon["fb"] = sum(c["fb"] for c in _grp)
        _canon["doc"] = sum(c["doc"] for c in _grp)
        # 施策タイムライン: グループ全員の FB を結合 → dedup → 日付降順 → 30件cap
        _canon["tl"] = _sort_fb_events(dedup_fb_events([ev for c in _grp for ev in c["tl"]]))
        # 最終FB日（日付降順ソート済なので先頭が最新。全件日付なしなら ""）
        _canon["lastfb"] = _canon["tl"][0]["d"] if _canon["tl"] else ""
        clients.append(_canon)

    docs = []
    for f in sorted(DOCS.glob("*.md")):
        if _is_excluded(f.stem):  # Agent分類の非ナレッジ+請求書系を除外（空白/_差異も吸収・表示のみ）
            continue
        if f.stem in CHUNK_DROP or f.stem in DOC_DROP:  # 分割断片の非代表 / 重複(別形式・旧版)の非正本を折り畳む
            continue
        t = f.read_text(errors="replace")
        fm = front(t)
        ex = ""
        for ln in t.splitlines():
            if ln.startswith("> "):
                ex = _strip_self_tags(ln[2:].strip())[:220]
                break
        _dclient = fm.get("client", "")
        if _is_self_org(_dclient):         # 自社は取引先に出さない（資料自体は残す）
            _dclient = ""
        _msrc = re.search(r"^- 出典: \[(\w+)\]", t, re.M)
        _src = {"gdrive": "Drive", "gsheets": "フォーム", "slack": "Slack"}.get(
            _msrc.group(1) if _msrc else "", ""
        )
        docs.append({
            "stem": f.stem, "title": TITLE_OVERRIDE.get(f.stem) or fm.get("title", f.stem),
            "client": _dclient, "cnorm": norm(_dclient),
            "industry": fm.get("industry", ""), "solution": fm.get("solution", ""),
            "doc_type": fm.get("doc_type", ""), "modified": fm.get("modified_at", ""),
            "src": _src,
            "ex": ex, "md": doc_md(t), "_wl": parse_links(body_of(t)),
        })

    # 最終接点 = max(最終FB日, 関連資料の最新更新日)。タグ用に事前計算（JS テーブル列 lastOf と同式）
    _lastdoc: dict[str, str] = {}
    for _d in docs:
        if _d["cnorm"] and _d["modified"] and _d["modified"] > _lastdoc.get(_d["cnorm"], ""):
            _lastdoc[_d["cnorm"]] = _d["modified"]
    for _c in clients:
        _c["last"] = max(_c["lastfb"], _lastdoc.get(_c["cnorm"], ""))

    reports = []
    if REPORTS.exists():
        for f in sorted(REPORTS.glob("*.md")):
            _rt = f.read_text(errors="replace")
            reports.append({"stem": f.stem, "name": f.stem, "md": _strip_self_tags(body_of(_rt).strip())[:20000], "_wl": parse_links(body_of(_rt))})

    INDUSTRY_COLORS = {"食品": "#54b981", "日用品": "#e0b34c", "金融": "#4f9df5", "IT": "#8a7cf5",
        "メーカー": "#e07a5f", "エネルギー": "#c98bdb", "エンタメ・スポーツ": "#e05f8f",
        "自動車": "#5fc9c9", "小売": "#b5c94a", "メディア": "#7f9cf5", "美容": "#e05f8f", "不動産": "#d0a24c"}
    # === 全vaultグラフ: 全取引先 + 全資料 + タグをノード化（実Obsidianと同密度） ===
    def _ctags(c):
        t = []
        if c["industry"]: t.append("業種/" + c["industry"])
        if c["phase"]: t.append("フェーズ/" + c["phase"])
        if c["bantg"]: t.append("BANT/" + c["bantg"])
        return t


    def _dtags(d):
        t = []
        if d["doc_type"]: t.append("資料種別/" + d["doc_type"])
        if d["industry"]: t.append("業種/" + d["industry"])
        if d["solution"]: t.append("施策/" + d["solution"])
        return t


    gnodes, glinks, _idx = [], [], {}


    def _node(nid, label, ntype, industry="", phase="", **kw):
        if nid in _idx:
            return _idx[nid]
        i = len(gnodes)
        _idx[nid] = i
        n = {"id": nid, "label": label, "type": ntype, "industry": industry, "phase": phase, "deg": 0}
        n.update(kw)
        gnodes.append(n)
        return i


    def _link(a, b):
        glinks.append([a, b])
        gnodes[a]["deg"] += 1
        gnodes[b]["deg"] += 1


    _cidx = {}
    for c in clients:
        i = _node("c:" + c["stem"], c["name"], "client", c["industry"], c["phase"], fb=c["fb"], doc=c["doc"])
        _cidx[norm(c["name"])] = i
        for tg in _ctags(c):
            _link(i, _node("t:" + tg, "#" + tg, "tag"))
    for d in docs:
        di = _node("d:" + d["stem"], d["title"][:40], "doc", d["industry"])
        if d["cnorm"] in _cidx:
            _link(di, _cidx[d["cnorm"]])
        for tg in _dtags(d):
            _link(di, _node("t:" + tg, "#" + tg, "tag"))

    # === 実wikilinkリンク網（バックリンク/アウトゴーイング/ローカルグラフ用） ===
    _c_stems = {c["stem"] for c in clients}
    _c_norm = {}
    for c in clients:
        _c_norm[norm(c["name"])] = c["stem"]
        _c_norm.setdefault(norm(c["stem"]), c["stem"])
    _d_stems = {d["stem"] for d in docs}      # 表示中(ナレッジ)のdocのみ = 非ナレッジ宛リンクは自動で落ちる
    _r_stems = {r["stem"] for r in reports}


    def _resolve(tgt):
        if tgt.startswith("clients/"):
            s = tgt[8:]
            if s in _c_stems:
                return "c:" + s
            st = _c_norm.get(norm(s))
            return "c:" + st if st else None
        if tgt.startswith("docs/"):
            s = tgt[5:]
            return "d:" + s if s in _d_stems else None
        if tgt in _c_stems:
            return "c:" + tgt
        st = _c_norm.get(norm(tgt))
        if st:
            return "c:" + st
        if tgt in _d_stems:
            return "d:" + tgt
        if tgt in _r_stems:
            return "r:" + tgt
        return None


    links = []              # [srcKey, tgtKey, ctx] 表示ノード間のみ
    _seen_edge = set()
    for _note, _sk in ([(c, "c:" + c["stem"]) for c in clients]
                       + [(d, "d:" + d["stem"]) for d in docs]
                       + [(r, "r:" + r["stem"]) for r in reports]):
        for _tgt, _ctx in _note.get("_wl", []):
            _tk = _resolve(_tgt)
            if not _tk or _tk == _sk:
                continue
            _ek = (_sk, _tk)
            if _ek in _seen_edge:
                continue
            _seen_edge.add(_ek)
            links.append([_sk, _tk, _ctx])

    # グラフにも実リンクを追加（既存の名寄せ/タグ辺と重複しない分だけ・ノード数は不変）
    _gpair = set()
    for _a, _b in glinks:
        _gpair.add((_a, _b) if _a < _b else (_b, _a))
    _added = 0
    for _sk, _tk, _ctx in links:
        _ia, _ib = _idx.get(_sk), _idx.get(_tk)
        if _ia is None or _ib is None:
            continue
        _pr = (_ia, _ib) if _ia < _ib else (_ib, _ia)
        if _pr in _gpair:
            continue
        _gpair.add(_pr)
        _link(_ia, _ib)
        _added += 1

    for _coll in (clients, docs, reports):   # 生ターゲットは payload に載せない
        for _n in _coll:
            _n.pop("_wl", None)

    payload = {"clients": clients, "docs": docs, "reports": reports, "links": links,
               "graph": {"nodes": gnodes, "links": glinks}, "colors": INDUSTRY_COLORS,
               "stats": {"clients": len(clients), "docs": len(docs), "reports": len(reports)}}
    DATA = json.dumps(payload, ensure_ascii=False)
    # インライン<script>内へ安全に埋め込む: </script> ブレイクアウトと行区切り文字を無害化
    DATA = (DATA.replace("<", "\\u003c").replace(">", "\\u003e")
                .replace(" ", "\\u2028").replace(" ", "\\u2029"))

    # --- fail-loud: 空生成の禁止（空の app.html を 16 名へ配るくらいなら止める） ---
    if not clients:
        _die(f"clients==0: {CLIENTS} に取引先 note がありません（全滅 export か junk 全除外）。生成を中止")
    if not docs:
        _die(f"docs==0: {DOCS} に資料 note がありません（全滅 export かフィルタ過剰除外）。生成を中止")

    fontface = (
        '@font-face{font-family:"InterVar";font-style:normal;font-weight:100 900;'
        "font-display:swap;src:url(data:font/woff2;base64," + font_b64 + ') format("woff2")}'
    )

    # --- フッタ焼き込み: statusbar 末尾に鮮度表示（既存 UI 不変） ---
    stamp = f"更新: {datetime.now(JST).strftime('%Y-%m-%d')} JST・取引先{len(clients)}・資料{len(docs)}"

    html = (
        HTML.replace("__BUILDSTAMP__", stamp)
        .replace("/*FONTFACE*/", fontface)
        .replace("__DATA__", DATA)
    )
    html_bytes = html.encode("utf-8")

    # --- サニティゲート（out を上書きする前に判定） ---
    stats_path = _stats_path(out)
    new_stats = {
        "clients": len(clients),
        "docs": len(docs),
        "bytes": len(html_bytes),
        "built_at": datetime.now(JST).isoformat(),
    }
    _sanity_gate(stats_path, new_stats, allow_shrink=args.allow_shrink)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(html_bytes)
    stats_path.write_text(json.dumps(new_stats, ensure_ascii=False) + "\n", encoding="utf-8")

    # 施策タイムラインのペイロード寄与（+400KB 超過時は FB_FIELD_MAP の pos/neg を 120 へ落とす）
    _tl_bytes = sum(
        len(json.dumps(c["tl"], ensure_ascii=False).encode("utf-8")) for c in clients if c.get("tl")
    )
    _tl_events = sum(len(c.get("tl") or []) for c in clients)

    print(f"✅ 生成: {out}")
    print(
        f"   サイズ: {len(html_bytes) // 1024} KB / clients={len(clients)} docs={len(docs)} "
        f"reports={len(reports)} graph={len(gnodes)}n {len(glinks)}e / "
        f"実リンク={len(links)}(内グラフ追加={_added}) / {stamp}"
    )
    print(f"   施策タイムライン: FBイベント{_tl_events}件 / tlペイロード {_tl_bytes // 1024} KB")
    print(f"   統計保存: {stats_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
