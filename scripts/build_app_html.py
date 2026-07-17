#!/usr/bin/env python3
"""connect-web「Obsidian完全再現」app.html 生成器（repo 版 v6）。

~/Documents/Claude/Artifacts/connect-web-obsidian_build.py v6 の repo 取り込み。
一次情報(docs.obsidian.md CSS変数)に基づく正確なトークン + Lucideアイコン + macOS枠 +
ペイン化サイドバー(ファイル/検索(演算子)/タグ(ネスト)/ブックマーク) + Properties新UI +
インラインタイトル + リーディングビュー + リンクされた言及 + グラフ。機密は端末外に出さない。

フィルタ5機構（source identity / exclude_stems / dedup_drop_map / 分割断片折り畳み /
weird_rename）と
HTML 生成ロジックは元スクリプトと同一。repo 版で加えたのは運用ガードのみ:

- argparse: ``--vault`` / ``--out`` / ``--allow-shrink``
- サイドカーは repo の ``data/connect_web_filters/`` から読む（``__file__`` 相対）。
  exists() フォールバックは全廃: サイドカー欠落・Vault 不在・clients==0・docs==0 は
  理由を明示して exit 1（「黙って劣化」を作らない）
- サニティゲート: 統計を ``<out>.stats.json`` に保存し、取引先数/資料数/バイト数の
  いずれかが前回比 20% 超減なら ``--allow-shrink`` 無しで exit 1（既存 out は上書きしない）
- ACL公開境界: 完全export manifestのactive pathだけを読み、全noteのSHA-256一致を要求する。
  未管理/手編集/prune保護note、partial/旧manifest、export後に変わったnoteは公開せずfail-closed。
- フッタ焼き込み: ステータスバー末尾に「更新: YYYY-MM-DD JST・取引先N・資料M」を表示
  （利用 16 名にデータ鮮度が見える）
- PII 決定論除外: 正規化後 stem に「請求」を含む資料（請求書/請求金額系）はサイドカーに
  個人名入り stem を列挙せずルールで除外（_is_excluded()。exclude_stems.json の平文 PII 廃止）
- タグ第1弾: 資料=媒体/動画形式/形式/横断、クライアント=温度感/宿題（いずれも決定論判定・
  LLM 不使用）＋テーブル「次アクション」列。タグペイン/検索のみでグラフ用 _ctags/_dtags には
  載せない（タグノード爆発防止）
- タグ第2弾: クライアント=担当/（フォーム由来FB引用の「送信者:」から決定論抽出・正規化）＋
  FB日付の第3フォールバック（引用の「タイムスタンプ: YYYY/MM/DD」。引用は300字で切断される
  ため、行末で終わる日付は切断の可能性があり棄却）＋テーブル「担当」列・カルテprops「担当」

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
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- repo 内サイドカー（フィルタ4ファイル + フォント）。欠落は即 exit 1（fallback 禁止） ---
_REPO_ROOT = Path(__file__).resolve().parents[1]
SIDECAR_DIR = _REPO_ROOT / "data" / "connect_web_filters"
SIDECAR_FILES = (
    "exclude_stems.json",      # Agent 分類の非ナレッジ除外（タイトル stem のみ）
    "exclude_source_keys.json",  # 不変な source_type:external_id による除外
    "dedup_drop_map.json",     # 別形式/旧版の非正本折り畳み（stem → 正本 stem）
    "weird_rename_high.json",  # 不明瞭命名 → 推奨タイトル（表示のみ・可逆）
    "inter-var.b64",           # InterVar フォント（woff2 base64）
)
OPTIONAL_SIDECAR_FILES = ("tag_alias.json", "client_alias.json")

DEFAULT_VAULT = Path.home() / "AiLaVault"
DEFAULT_OUT = Path.home() / "Documents" / "Claude" / "Artifacts" / "connect-web-obsidian-preview.html"

JST = timezone(timedelta(hours=9))
SHRINK_LIMIT = 0.20  # サニティゲート: 前回比これを超える減少で停止

# main() が argparse から設定するモジュールグローバル
# （client_md/_compute_chunk_drop 等の helper が参照するため module scope に置く）
VAULT = DEFAULT_VAULT
CLIENTS, DOCS, REPORTS = VAULT / "clients", VAULT / "docs", VAULT / "_reports"
ACTIVE_MANAGED_PATHS: set[str] = set()
EXPORT_MANIFEST_SHA256 = ""
BUILD_INPUTS_SHA256 = ""
SIDECAR_SNAPSHOTS: dict[str, bytes | None] = {}
_EXPORT_MANIFEST_NAME = ".export-vault-manifest.json"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

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

# 担当タグ第2弾: フォーム由来FBの引用（quote）から送信者とタイムスタンプを拾う。
# 実データの引用は1行に全フィールドが連結される
# （例「… 送信者: 清水達哉 タイムスタンプ: 2026/05/11 17:38:30 連携ステータス: …」）うえ
# 300字で切断されるため、送信者値は次フィールド見出し（タイムスタンプ/連携ステータス）か
# 行末まで、日付は「直後に非数字が続く」完全なものだけ採用する
# （行末で終わる「2026/06/1」は /1X の途中切断の可能性があるため棄却）。
FB_SENDER_RE = re.compile(
    r"送信者[ \t　]*[:：][ \t　]*([^\n]*?)(?=[ \t　]*(?:タイムスタンプ|連携ステータス)[ \t　]*[:：]|\n|$)"
)
FB_TS_RE = re.compile(r"タイムスタンプ[ \t　]*[:：][ \t　]*(\d{4})/(\d{1,2})/(\d{1,2})(?=[^\d\n])")
TANS_MAX = 5  # カルテ/テーブルに出す担当者数の上限


def _norm_sender(raw):
    """送信者名の正規化: （/(/_ 以降を切除 → 全半角スペース除去 → 先頭20字。

    実データの表記ゆれ（「佐藤杏香(Sato」「川上壮汰_KawakamiSota」「小倉　岳之（ogura…」）を
    同一人物へ寄せる決定論ルール。取れなければ空文字。
    """
    s = re.split(r"[（(_]", raw, maxsplit=1)[0]
    return s.replace(" ", "").replace("　", "").strip()[:20]


def _quote_text(sec):
    """セクション中の引用（> 行）本文を連結して返す（送信者/タイムスタンプの抽出対象）。"""
    return "\n".join(
        ln.lstrip()[1:].lstrip() for ln in sec.splitlines() if ln.lstrip().startswith(">")
    )


def _fb_dedup_key(ev):
    """Slack/フォーム二重登録の同定キー: 正規化した (ポジ+ネガ)[:120]。空は dedup 対象外。"""
    raw = unicodedata.normalize("NFKC", (ev.get("pos", "") + ev.get("neg", "")))
    return re.sub(r"\s+", "", raw).lower()[:120]


def dedup_fb_events(events):
    """(ポジ+ネガ) が一致する重複 FB を折り畳む。日付を持つ方を正として残す。

    送信者（by）は同一FBでも片ルートにしか載らない（Slack転送=<@UID>のみ／フォーム行=実名）
    ため、残す側が空なら消える側から補完する（どのイベントを残すかの規則は従来どおり不変。
    これが無いと先出のSlack転送が実名入りフォーム行を握り潰し、担当タグの実カバレッジが
    91社→60社へ落ちる）。
    """
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
            continue
        # 残す側: 日付を持つ方（両方あり/両方なしは先勝ち）＝従来規則
        keep, drop = (ev, out[j]) if (ev.get("d") and not out[j].get("d")) else (out[j], ev)
        if not keep.get("by") and drop.get("by"):
            keep = {**keep, "by": drop["by"]}  # 共有 dict を汚さないよう copy して補完
        out[j] = keep
    return out


def _sort_fb_events(events):
    """日付降順（日付なしは末尾）・最大 FB_MAX_EVENTS 件。"""
    events.sort(key=lambda e: e.get("d") or "", reverse=True)
    return events[:FB_MAX_EVENTS]


def _parse_fb_events_raw(body: str) -> list[dict]:
    """クライアント md 本文の営業FB時系列を未dedup・未capでパースする（fail-open）。

    見出し `### ---- <ソース名> <slack ts epoch|row N>` で区切り、
    各 FB の `- フェーズ:` 等のフィールド行と日付（`> [YYYY-MM-DD HH:MM]` 行
    → 見出し末尾の epoch 秒 → UTC+9 → 引用の「タイムスタンプ: YYYY/MM/DD」の3段
    フォールバック）を拾う。担当タグ用に引用の「送信者:」も抽出する（ev["by"]・
    フォーム由来のみ実名が入る。Slack直投稿は <@UID> のみ → 空文字）。
    壊れた見出し/欠損フィールドは空文字で許容し、例外は漏らさない
    （このパースの失敗で build を止めない）。
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
                ev = {"d": "", "src": "", "by": "", "ph": "", "bant": "", "menu": "", "pos": "", "neg": "", "next": ""}
                ev["src"] = "フォーム" if ("フォーム" in head or re.search(r"row\s*\d+\s*$", head)) else "Slack"
                quote = _quote_text(sec)
                sm = FB_SENDER_RE.search(quote)
                if sm:
                    ev["by"] = _norm_sender(sm.group(1))
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
                if not ev["d"]:  # 第3フォールバック: 引用のタイムスタンプ（末尾切断された日付は棄却）
                    tm = FB_TS_RE.search(quote)
                    if tm:
                        try:
                            ev["d"] = datetime(int(tm.group(1)), int(tm.group(2)), int(tm.group(3))).strftime("%Y-%m-%d")
                        except ValueError:  # 13月/32日等の不正日付は捨てる
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
        return events
    except Exception:  # タイムラインはベストエフォート（このパースで build を止めない）
        return []


def parse_fb_events(body: str) -> list[dict]:
    """営業FBをパースし、重複排除・日付降順・表示件数capを適用する。"""
    return _sort_fb_events(dedup_fb_events(_parse_fb_events_raw(body)))


# === タグ第1弾: 資料タグ4軸（媒体/動画形式/形式/横断）＋クライアントタグ（温度感/宿題） ===
# 判定は決定論 regex/計数のみ（LLM 不使用・再実行で同一結果）。付与先は検索・タグペイン用の
# docTags/clientTags（JS）だけで、グラフ用 _ctags/_dtags には加えない（タグノード爆発防止）。

# 媒体/: title+excerpt から検出（複数付与可・MEDIA_RES の定義順）。誤爆対策:
# - X: 「X（旧Twitter）/Twitter/ツイッター」を主とし、単独 X は英数字境界かつ大文字限定
#   （DX・URL の x.com・コラボ表記「A x B」の小文字 x を拾わない）
# - LINE: 大文字限定+英字境界（online/deadline/GUIDELINE を除外）。カタカナ「ライン」は
#   ラインナップ/デッドライン等の誤爆源のため対象外
# - Facebook: 略記 FB は本 Vault で「フィードバック」の意で頻出するため対象外
# - テレビは TVer を含む ／ OOH はサイネージを含む
# 出典URL行は判定対象外（excerpt は先頭の「> 」行のみで「- 出典:」行を含まない。main() 参照）
MEDIA_RES: list[tuple[str, re.Pattern]] = [
    ("TikTok", re.compile(r"tiktok|ティックトック", re.I)),
    ("YouTube", re.compile(r"youtube|ユーチューブ", re.I)),
    ("Instagram", re.compile(r"instagram|インスタ", re.I)),
    ("X", re.compile(r"X[（(]旧Twitter[)）]|旧Twitter|[Tt]witter|ツイッター|(?<![A-Za-z0-9])X(?![A-Za-z0-9])")),
    ("LINE", re.compile(r"(?<![A-Za-z])LINE(?![A-Za-z])")),
    ("Facebook", re.compile(r"facebook|フェイスブック", re.I)),
    ("テレビ", re.compile(r"テレビ|tver", re.I)),
    ("OOH", re.compile(r"(?<![A-Za-z])OOH(?![A-Za-z])|サイネージ")),
]

# 動画形式/: title+excerpt+solution から検出（複数付与可）。「ショート」単体は
# ショートカット等の誤爆源のため「ショート動画|縦型」のみ（値域規定どおり）
VIDEO_FORMAT_RES: list[tuple[str, re.Pattern]] = [
    ("ショート", re.compile(r"ショート動画|縦型")),
    ("切り抜き", re.compile(r"切り抜き")),
    ("ライブ配信", re.compile(r"ライブ配信|ライブコマース")),
    ("長尺", re.compile(r"長尺")),
]

# 形式/: doc の stem 末尾の拡張子表記（「〜.pptx」のような名前）から判定。
# 大文字小文字無視・拡張子なしはタグなし
FILE_FORMAT_RE = re.compile(r"\.(pptx?|pdf|xlsx?|docx?)\s*$", re.I)
FILE_FORMAT_LABEL = {"ppt": "PPTX", "pptx": "PPTX", "pdf": "PDF",
                     "xls": "Excel", "xlsx": "Excel", "doc": "Word", "docx": "Word"}


def media_tags(text):
    """媒体/ タグ判定。NFKC 正規化してから regex（全角英字/全角括弧の表記ゆれを吸収）。"""
    t = unicodedata.normalize("NFKC", text or "")
    return [name for name, rx in MEDIA_RES if rx.search(t)]


def video_format_tags(text):
    """動画形式/ タグ判定（媒体と同じく NFKC 正規化後に regex）。"""
    t = unicodedata.normalize("NFKC", text or "")
    return [name for name, rx in VIDEO_FORMAT_RES if rx.search(t)]


def file_format_tag(stem):
    """形式/ タグ: stem 末尾の拡張子から PPTX/PDF/Excel/Word。該当なしは空文字。"""
    m = FILE_FORMAT_RE.search(stem or "")
    return FILE_FORMAT_LABEL[m.group(1).lower()] if m else ""


# === ナレッジ共有メタ4軸: カテゴリ/クライアント種別/提案プロダクト/施策手法 + 代理店 ===
# フォーム回答/ファイル記録シート由来（export_vault が frontmatter category/client_tier/
# product に載せる）。クライアント種別・提案プロダクトは複数選択の ASCII/全角カンマ区切りで、
# ents（frontmatter CSV を Python 側で split）と同じく **ここで分割して配列化** し、
# docTags(JS) は media/vfmt/ents 同様に配列を素通しでタグ化する。
# 読点「、」は正規値の内部（官公庁、自治体 / ショート動画提案（UGCや切り抜き、メディア））に
# しか出ないため **ASCII/全角カンマのみで分割**（読点で割ると 官公庁 と 自治体 を誤分割する）。
_META_SPLIT_RE = re.compile(r"[,，]")
# クライアント種別の表記ゆれ→正規値（読点内包の 1 値を保持する）。
_CLIENT_TIER_ALIAS = {"官公庁、自治体": "官公庁・自治体"}
# 値として意味を成さないプレースホルダ（クライアント種別では付与しない）。
_TIER_DROP = frozenset({"-", ""})
# 提案プロダクトの表記ゆれ（タイポのみ修正・他は素通り。その他 も含めて付与する）。
_PRODUCT_ALIAS = {"InsigtFinder": "InsightFinder"}


def _split_meta_multi(value):
    """ASCII/全角カンマのみで分割し trim（読点では割らない）。空要素は捨てる。"""
    return [p.strip() for p in _META_SPLIT_RE.split(value or "") if p.strip()]


def client_tier_tags(value):
    """クライアント種別/ タグ（多値・非排他）。読点で割らず alias 正規化・順序保持で重複除去。"""
    out = []
    for v in _split_meta_multi(value):
        v = _CLIENT_TIER_ALIAS.get(v, v)
        if v in _TIER_DROP or v in out:
            continue
        out.append(v)
    return out


def product_tags(value):
    """提案プロダクト/ タグ（多値）。タイポ InsigtFinder→InsightFinder のみ修正・順序保持で重複除去。"""
    out = []
    for v in _split_meta_multi(value):
        v = _PRODUCT_ALIAS.get(v, v)
        if v in out:
            continue
        out.append(v)
    return out


def category_tags(value):
    """カテゴリ/ タグ（多値）。資料の概要は複数選択（提案,レポート 等）があるため分割・重複除去する。

    ファイル記録の保管先フォルダミラー（01_提案 等・単値）はそのまま 1 要素になる（Codex #215-カテゴリ複数値）。
    """
    out = []
    for v in _split_meta_multi(value):
        if v in out:
            continue
        out.append(v)
    return out


# 施策手法/: title+excerpt+product+category(+本文) を対象に決定論キーワードで付与（複数可）。
# 誤爆しにくい語に限定（分析準拠）。NFKC 正規化＋大文字化で全角/小文字ゆれを吸収してから包含判定。
_METHOD_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("切り抜き・TTO", ("切り抜き", "TTO")),
    ("縦型・タテガタ", ("タテガタ", "縦型")),
    ("インフルエンサー", ("インフルエンサー", "KOL", "キャスティング")),
    ("UGC", ("UGC",)),
    ("ビデオリリース", ("ビデオリリース",)),
    ("VSEO", ("VSEO", "指名検索")),
    ("ライブ配信", ("ライブ配信", "ライブコマース")),
]


def method_tags(text):
    """施策手法/ タグ（多値・決定論キーワード）。NFKC+大文字化で全角/小文字ゆれを吸収。"""
    t = unicodedata.normalize("NFKC", text or "").upper()
    return [name for name, kws in _METHOD_KEYWORDS if any(k.upper() in t for k in kws)]


def agency_flag(text):
    """代理店/あり: 本文/コメントに「代理店」を含めば True（代理店名は取らない）。"""
    return "代理店" in (text or "")


# 温度感/: ネガ反応が実質空（「特になし」系）は 高。先頭の中黒・末尾の句点は許容
TEMP_NEG_NONE_RE = re.compile(r"^[・\s]*(特になし|特に無し|なし|-)[。、.\s]*$")


def temperature_tag(tl):
    """温度感/ タグ: 最新FBイベント（tl 先頭＝日付降順ソート済・日付なしは末尾）で決定論判定。

    tl は parse_fb_events 済み（Slack/フォーム二重登録は dedup 済なのでそのまま使ってよい）。
    pos/neg は FB_FIELD_MAP で 120 字に切り詰め済み → 長文FBでは比率が飽和し得るが許容
    （両方 120 字で頭打ち＝拮抗側に倒れる保守的な挙動）。tl が空ならタグなし。
    """
    if not tl:
        return ""
    pos, neg = tl[0].get("pos", ""), tl[0].get("neg", "")
    if not neg.strip() or TEMP_NEG_NONE_RE.match(neg):
        return "高"
    if len(pos) >= 2 * len(neg):
        return "ポジ優勢"
    if len(neg) >= 2 * len(pos):
        return "ネガ優勢"
    return "拮抗"


# 宿題/あり: 「次アクションが書いてあるのに最終接点が古い」の炙り出し
HOMEWORK_STALE_DAYS = 31


def next_action(tl):
    """テーブル「次アクション」列: 最新FBイベントの次アクション先頭40字（無ければ空）。"""
    return (tl[0].get("next", "") if tl else "")[:40]


def tans_of(tl):
    """担当/: tl の distinct な非空 ev.by（名寄せ統合後の tl に適用・ソート済・最大 TANS_MAX 名）。"""
    return sorted({ev.get("by", "") for ev in tl or []} - {""})[:TANS_MAX]


def homework_flag(nx, last, today):
    """宿題/あり 判定: 次アクション非空 かつ 最終接点が HOMEWORK_STALE_DAYS 日超過去 or 日付なし。

    接点が新しい（追えている）クライアントには付けない。判定はビルド時点
    （月次再生成の間の経日は次回ビルドで反映）。不正な日付は「日付なし」扱い＝炙り出し側に倒す。
    """
    if not nx:
        return False
    if not last:
        return True
    try:
        last_d = datetime.strptime(last, "%Y-%m-%d").date()
    except ValueError:
        return True
    return (today - last_d).days > HOMEWORK_STALE_DAYS


# === タグUX: ホーム「クイックフィルタ」プリセット件数（<out>.stats.json へ焼き込み・回帰検知用） ===
# JS 側 QF 配列（HTML テンプレート内）と対。タグ値の出力文字列を変えるとここが 0 になり
# 「プリセットが静かに消えた」ことを stats 差分で検知できる。ageBucket は JS が閲覧時
# Date.now 基準なのに対し、ここではビルド日基準の近似（回帰検知用途のため許容）。
QF_PRESET_TAGS = (
    "宿題/あり", "温度感/ネガ優勢", "温度感/高", "最終接点/1年以上前",
    "更新/1ヶ月以内", "横断", "動画形式/ショート", "資料種別/提案書",
)


def _age_bucket(ds, today):
    """JS ageBucket のビルド日基準ミラー（閾値 31/92/183/366 は JS と同一）。"""
    if not ds:
        return ""
    try:
        d = datetime.strptime(ds, "%Y-%m-%d").date()
    except ValueError:
        return ""
    dd = (today - d).days
    if dd <= 31: return "1ヶ月以内"
    if dd <= 92: return "3ヶ月以内"
    if dd <= 183: return "半年以内"
    if dd <= 366: return "1年以内"
    return "1年以上前"


def _quickfilter_counts(clients, docs, today):
    """QF プリセットごとのアイテム件数（JS の tagMatch と同じ「子タグも一致」判定）。"""
    def ctags(c):
        t = []
        if c["industry"]: t.append("業種/" + c["industry"])
        if c["phase"]: t.append("フェーズ/" + c["phase"])
        if c["bantg"]: t.append("BANT/" + c["bantg"])
        t.append("最終接点/" + (_age_bucket(c["last"], today) or "記録なし"))
        if c["temp"]: t.append("温度感/" + c["temp"])
        if c["hw"]: t.append("宿題/あり")
        return t

    def dtags(d):
        t = []
        if d["doc_type"]: t.append("資料種別/" + d["doc_type"])
        if d["industry"]: t.append("業種/" + d["industry"])
        if d["solution"]: t.append("施策/" + d["solution"])
        a = _age_bucket(d["modified"], today)
        if a: t.append("更新/" + a)
        if d["src"]: t.append("情報源/" + d["src"])
        t += ["媒体/" + m for m in d["media"]]
        t += ["動画形式/" + v for v in d["vfmt"]]
        if d["fmt"]: t.append("形式/" + d["fmt"])
        if d["xc"]: t.append("横断/" + d["xc"])
        return t

    tag_lists = [ctags(c) for c in clients] + [dtags(d) for d in docs]
    return {
        preset: sum(
            1 for tags in tag_lists
            if any(t == preset or t.startswith(preset + "/") for t in tags)
        )
        for preset in QF_PRESET_TAGS
    }


EXCL: set[str] = set()  # exclude_stems.json（main() で読み込み・欠落は exit 1）
EXCL_SOURCE_KEYS: set[str] = set()  # exclude_source_keys.json（source_type:external_id）
SOURCE_EXCLUDED_STEMS: set[str] = set()  # 現 Vault で source key に一致した stem（payload へ載せない）
_exn = lambda s: re.sub(r"[\s_]+", "", s).lower()
EXCL_N: set[str] = set()


def _source_key(fm):
    """Vault frontmatter の内部メタから安定した source identity を作る。"""
    source_type = str(fm.get("source_type") or "").strip().lower()
    external_id = str(fm.get("external_id") or "").strip()
    if not source_type or not external_id:
        return ""
    return f"{source_type}:{external_id}"


def _source_excluded_stems():
    """source key 除外に一致する現 Vault の stem を返す（旧 Vault は空＝stem 除外へ移行）。"""
    excluded = set()
    for f in DOCS.glob("*.md"):
        if _portable_path_key(f"docs/{f.name}") not in ACTIVE_MANAGED_PATHS:
            continue
        fm = front(f.read_text(errors="replace"))
        if _source_key(fm) in EXCL_SOURCE_KEYS:
            excluded.add(f.stem)
    return excluded


def _is_excluded(stem):
    # 除外判定: 不変 source identity / 旧Vault向けstem列挙（EXCL/EXCL_N）に加え、
    # 正規化後 stem に「請求」を含む
    # 請求書/請求金額系タイトルは決定論で除外（個人名PIIの stem を repo に列挙しない）
    return (
        stem in SOURCE_EXCLUDED_STEMS
        or stem in EXCL
        or _exn(stem) in EXCL_N
        or "請求" in _exn(stem)
    )

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
TITLE_OVERRIDE: dict[str, str] = {}  # weird_rename_high.json（portable stem key、main() で読み込み）

# === 表示名寄せ（任意適用・可逆）: tag_alias.json / client_alias.json（main() で読み込み） ===
# 必須サイドカー(_read_sidecar)と扱いを分ける: 欠落/空/破損は空 dict＝素通り（fail-loud にしない）。
# サイドカー削除で元挙動へ戻る可逆設計のため、名寄せは「載っている値だけ正本へ寄せる」。
TAG_ALIAS: dict = {}       # {"industry":{variant:canonical,...},"solution":{...}}
CLIENT_ALIAS: dict = {}    # client_alias.json の "client": {variant:canonical,...}


def _canon_industry(v):
    """業種の表示名寄せ: variant→canonical（未登録/空は素通り）。"""
    return TAG_ALIAS.get("industry", {}).get(v, v)


def _canon_solution(v):
    """施策の表示名寄せ: variant→canonical（未登録/空は素通り）。"""
    return TAG_ALIAS.get("solution", {}).get(v, v)


def _canon_client(v):
    """取引先名の表示名寄せ: variant→canonical（未登録/空は素通り）。"""
    return CLIENT_ALIAS.get(v, v)


_CHUNK_RE = re.compile(r"_\d{1,2}$")
_EXPORT_VAULT_GENERATOR = "scripts/export_vault.py"


def _chunk_key(stem):
    k = stem
    while _CHUNK_RE.search(k):
        k = _CHUNK_RE.sub("", k)
    return k
def _compute_chunk_drop():
    # 旧Vaultの分割断片(_2/_3…)は同一baseで束ね、代表1件(base優先/最短)のみ残す。
    # export_vault の note は1ファイル=1論理documentであり、同名衝突や元タイトル由来の
    # `_2` は chunk ではない。generated_by marker がある生成noteはstemに関係なく保持する。
    groups = defaultdict(list)
    for f in DOCS.glob("*.md"):
        s = f.stem
        if _portable_path_key(f"docs/{f.name}") not in ACTIVE_MANAGED_PATHS:
            continue
        if _is_excluded(s):
            continue
        fm = front(f.read_text(errors="replace"))
        if fm.get("generated_by") == _EXPORT_VAULT_GENERATOR:
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
 --text:#22232a;--muted:#5c5d64;--faint:#71737b;--on-accent:#fff;
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
 --text:#dadada;--muted:#999;--faint:#8a8a8a;
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
.sfield input:focus{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent-bg)}
.sfield input:focus:not(:focus-visible){outline:none}   /* マウス時のみ抑止（キーボードの :focus-visible リングは殺さない） */
/* ツリー */
.tree{padding:0 6px}
.trow{display:flex;align-items:center;gap:5px;padding:3px 6px;border-radius:var(--r-s);cursor:pointer;color:var(--muted);font-size:var(--f-ui-smaller);white-space:nowrap;overflow:hidden;user-select:none;position:relative}
.trow:hover{background:var(--hover);color:var(--text)}
.trow.active{background:var(--active);color:var(--text)}
.tchildren{position:relative}
.tchildren::before{content:"";position:absolute;left:19px;top:0;bottom:0;width:1px;background:var(--border)}
.trow .tw{width:14px;height:14px;display:flex;align-items:center;justify-content:center;color:var(--faint);transition:transform .12s;transform:rotate(90deg)}   /* 開=下向き（ベースicon chevは右向き） */
.trow.closed .tw{transform:rotate(0)}   /* 閉=右向き（Obsidian慣習） */
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
.sr .srt{font-size:var(--f-ui-small);font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sr .srx{font-size:11px;color:var(--muted);line-height:1.5;margin-top:2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sr .srx mark{background:var(--mark);color:var(--mark-fg);border-radius:2px;padding:0 1px}
.scount{padding:6px 12px;color:var(--faint);font-size:var(--f-ui-smaller);border-bottom:1px solid var(--border);margin-bottom:4px}
.qhelp{padding:8px 12px;color:var(--faint);font-size:11px;line-height:1.7}
.qhelp code{background:var(--b10);padding:1px 4px;border-radius:3px;color:var(--muted);font-family:var(--mono)}
/* 統一タグチップ（色は7pxドットのみ・文字色はCSS変数=両テーマ両立・hover枠が系統色） */
.tagchip{display:inline-flex;align-items:center;gap:5px;background:var(--hover);border:1px solid var(--border);border-radius:9px;padding:2px 8px;font-size:11px;line-height:16px;cursor:pointer;white-space:nowrap;max-width:100%;user-select:none}
.tagchip:hover{border-color:var(--cc2,var(--accent))}
.tcdot{display:inline-block;width:7px;height:7px;border-radius:50%;flex:none}
.tagchip .tck{color:var(--muted);flex:none}
.tagchip .tcv{color:var(--text);overflow:hidden;text-overflow:ellipsis}
.tagchip .tcneg{text-decoration:line-through}
.chiprow{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.chiprow.tzrow{flex-wrap:nowrap;overflow-x:auto;padding-bottom:4px}   /* テーザーは1行固定（はみ出しは横スクロール） */
.tcmore{color:var(--faint);font-size:11px;padding:2px 4px;align-self:center}
.qfn{color:var(--muted);font-variant-numeric:tabular-nums}
/* 検索ペイン: アクティブフィルタバー（scount 内） */
.fbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:6px 0 2px}
.fchip .fx{margin:-4px -4px -4px -3px;padding:5px 8px;color:var(--faint);cursor:pointer;border-radius:3px;font-size:12px;line-height:14px}   /* 実効ヒット領域24px（見た目は負marginで相殺） */
.fchip .fx:hover{color:var(--err);background:var(--hover)}
.fclear{color:var(--accent-2);cursor:pointer;font-size:11px;padding:2px 6px;border-radius:var(--r-s);white-space:nowrap}
.fclear:hover{background:var(--hover)}
.frec{padding-top:4px;color:var(--muted)}
.qex .tagchip{margin:2px 4px 2px 0}
/* 検索結果行のタグチップ */
.sr .srtags{display:flex;flex-wrap:wrap;gap:6px;margin-top:3px}
/* タグペイン（意味群見出し・畳み行）/ 右パネルタグ */
.tghead{padding:8px 10px 3px;color:var(--faint);font-size:11px;font-weight:600;letter-spacing:.03em}
.trow.tmore{color:var(--accent-2)}
.rtagwrap{display:flex;flex-wrap:wrap;gap:6px;padding:4px 10px 8px}
/* テーブル: 宿題チェック+アクティブ表示チップ */
.tbv .bar .hwck{display:inline-flex;align-items:center;gap:5px;color:var(--muted);font-size:var(--f-ui-smaller);cursor:pointer;user-select:none}
.tbv .bar .hwck input{accent-color:var(--accent);margin:0}
.tfl{display:inline-flex;gap:6px;margin-left:4px}
.tfl .tagchip{cursor:default}
/* 中央 */
.main{background:var(--bg-primary);display:flex;flex-direction:column;min-width:0;min-height:0;position:relative}
.tabbar{display:flex;align-items:stretch;height:40px;background:var(--bg-sidebar);border-bottom:1px solid var(--border);padding-left:6px;flex:none;overflow-x:auto}
.tab{display:flex;align-items:center;gap:7px;height:32px;margin-top:8px;padding:0 10px;color:var(--muted);font-size:var(--f-ui-smaller);border-radius:var(--r-s) var(--r-s) 0 0;cursor:pointer;white-space:nowrap;max-width:220px}
.tab.on{background:var(--bg-primary);color:var(--text)}
.tab:not(.on):hover{background:var(--hover)}
.tab .lbl{overflow:hidden;text-overflow:ellipsis}
.tab .x{width:24px;height:24px;margin:-4px;display:flex;align-items:center;justify-content:center;border-radius:3px;color:var(--faint)}   /* 常時表示（タッチでも見える）+24pxヒット領域 */
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
/* Properties(実Obsidian準拠: 生キー+値がありません+追加) */
.props{margin:8px 0 24px;padding:0}
.props-h{font-size:var(--f-ui-small);color:var(--muted);padding:4px 2px 6px}
.prow{display:flex;align-items:center;gap:8px;padding:4px 6px;font-size:var(--f-ui-small);border-radius:var(--r-s)}
.prow .pk{display:flex;align-items:center;gap:9px;color:var(--muted);width:180px;flex:none}
.prow .pk svg{width:15px;height:15px;color:var(--faint)}
.prow .pkn{overflow:hidden;text-overflow:ellipsis}
.prow .pv{color:var(--text);flex:1;min-width:0}
/* P1 サマリーヘッダ「商談スナップショット」（propsPanel 撤去の受け皿・条件付き描画・色は既存トークン流用） */
.khdr{display:flex;flex-wrap:wrap;align-items:center;gap:7px 16px;margin:10px 0 20px;padding:11px 14px;background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--r-m)}
.khdr.khmin{color:var(--muted);font-size:var(--f-ui-small);padding:9px 14px}
.khc{display:inline-flex;align-items:baseline;gap:6px;font-size:var(--f-ui-small);min-width:0;max-width:100%;white-space:nowrap}
.khc.khnx{flex:1 1 240px;white-space:normal;align-items:baseline}
.khc .khi{font-size:13px;line-height:1;flex:none}
.khc .khl{color:var(--faint);font-size:var(--f-ui-smaller);flex:none}
.khc .khv{color:var(--text);font-weight:600;overflow:hidden;text-overflow:ellipsis;min-width:0}
.khc.khnx .khv{white-space:normal;overflow-wrap:anywhere}
.khc.ok .khv{color:var(--ok)}
.khc.warn .khv{color:var(--warn)}
.khc.err .khv{color:var(--err)}
.khc.err .khi{color:var(--err)}
.khc.muted .khv{color:var(--faint);font-weight:400}
.khdot{display:inline-block;width:8px;height:8px;border-radius:50%;flex:none;align-self:center}
.pempty{color:var(--faint)}
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
.ri,.trow,.sr,.wcard,.tab,.bl,.olink,.act,.qi,.side-h .act,.tagchip,.fchip .fx,.fclear,.tlmorebtn,.tbv thead th{transition:background .1s ease,color .1s ease,border-color .1s ease}
:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
/* welcome */
.welcome{max-width:var(--line-w);margin:0 auto;padding:40px}
.welcome h1{font-size:1.9em;font-weight:800;display:flex;align-items:center;gap:12px;margin:0 0 6px}
.welcome .sub{color:var(--muted);margin:0 0 28px;font-size:var(--f-ui-med)}
.wsec{color:var(--faint);font-size:var(--f-ui-smaller);text-transform:uppercase;letter-spacing:.06em;font-weight:600;margin:24px 0 10px}
.wgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.wcard{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--r-m);padding:13px 15px;cursor:pointer;display:flex;gap:10px;align-items:flex-start}
.wcard:hover{border-color:var(--border-focus);background:var(--hover)}
.wcard svg{color:var(--accent-2);margin-top:1px}
.wcard .wt{font-size:var(--f-ui-med);color:var(--text);font-weight:700;line-height:1.35}
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
.rsec-h .tw{width:12px;height:12px;color:var(--faint);transition:transform .12s;transform:rotate(90deg)}
.rsec-h.closed .tw{transform:rotate(0)}
.rsec-h .cnt{margin-left:auto;color:var(--faint)}
.rsec.closed .rsec-body{display:none}
.rsec-body{padding:2px 8px 8px}
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
.gpanel .gin:focus{border-color:var(--accent)}
.gpanel .gin:focus:not(:focus-visible){outline:none}
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
.qs .qi:hover{background:var(--hover)}
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
.gph .gpx{cursor:pointer;color:var(--faint);width:24px;height:24px;margin:-2px;display:flex;align-items:center;justify-content:center;border-radius:4px}
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
.tbv .bar input:focus,.tbv .bar select:focus{border-color:var(--accent)}
.tbv .bar input:focus:not(:focus-visible),.tbv .bar select:focus:not(:focus-visible){outline:none}
.tbv .bar .n{color:var(--faint);font-size:var(--f-ui-smaller);margin-left:auto}
.tblwrap{overflow:auto;border:1px solid var(--border);border-radius:var(--r-m);max-height:calc(100vh - 220px)}
.tbv table{border-collapse:collapse;width:100%;font-size:var(--f-ui-small)}
.tbv thead th{position:sticky;top:0;background:var(--bg-elev);text-align:left;padding:9px 12px;color:var(--muted);font-weight:600;border-bottom:1px solid var(--border);cursor:pointer;white-space:nowrap;user-select:none;z-index:1}
.tbv thead th:hover{color:var(--text)}
.tbv thead th .ar{color:var(--accent-2);margin-left:5px;font-size:10px}
.tbv td{padding:8px 12px;border-bottom:1px solid var(--b10);color:var(--muted);white-space:nowrap}
/* 先頭列（取引先名）固定: 横スクロールしても常時可視。背景は不透明必須（hover 時も破綻させない） */
.tbv td:first-child{position:sticky;left:0;background:var(--bg-primary);z-index:1}
.tbv thead th:first-child{left:0;z-index:3}
.tbv tbody tr:hover td:first-child{background:var(--bg-elev)}
.tbv tbody tr{cursor:pointer}
.tbv tbody tr:hover{background:var(--hover)}
.tbv tbody td:first-child{color:var(--text);font-weight:700}
.tbv .num{text-align:right;font-variant-numeric:tabular-nums}
.tbv .bp{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px;color:var(--text)}
.tbv .dotc{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;vertical-align:middle}
/* P5: テーブル最終接点=経過日数バッジ（相対＋重症度色ドット/淡背景・生日付は title に温存） */
.tbv .age{display:inline-flex;align-items:center;gap:6px;padding:1px 8px;border-radius:9px;font-variant-numeric:tabular-nums}
.tbv .age .agedot{width:7px;height:7px;border-radius:50%;flex:none;background:var(--faint)}
.tbv .age.ok{background:color-mix(in srgb,var(--ok) 12%,transparent);color:var(--ok)}
.tbv .age.ok .agedot{background:var(--ok)}
.tbv .age.warn{background:color-mix(in srgb,var(--warn) 14%,transparent);color:var(--warn)}
.tbv .age.warn .agedot{background:var(--warn)}
.tbv .age.err{background:color-mix(in srgb,var(--err) 13%,transparent);color:var(--err)}
.tbv .age.err .agedot{background:var(--err)}
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
.tld{width:72px;flex:none;text-align:right;font-size:var(--f-ui-smaller);color:var(--muted);font-variant-numeric:tabular-nums;padding-top:1px}
.tlcard{flex:1;min-width:0;background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--r-m);padding:8px 12px 9px;margin-left:26px}
.tlbadge{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px;font-weight:600;margin-right:6px;white-space:nowrap;color:var(--text)}
.tlbadge.tlfb{background:var(--accent-bg);color:var(--accent-2)}
.tlchip{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;background:var(--hover);color:var(--muted);margin-right:4px;white-space:nowrap}
.tlchip.tlby{color:var(--muted)}
.tlt{font-weight:600;font-size:var(--f-ui-small);color:var(--text);margin-top:5px;line-height:1.45;overflow-wrap:anywhere}
.tlt .wl{color:var(--accent-2);cursor:pointer}
.tlt .wl:hover{text-decoration:underline}
.tlx{font-size:var(--f-ui-smaller);color:var(--muted);margin-top:4px;line-height:1.55;overflow-wrap:anywhere}
.tlnx{font-size:var(--f-ui-smaller);color:var(--accent-2);margin-top:4px;line-height:1.5;overflow-wrap:anywhere}
.tlmorebtn{display:inline-block;margin:0 0 2px 98px;padding:4px 12px;font-size:var(--f-ui-smaller);color:var(--accent-2);cursor:pointer;border:1px solid var(--border);border-radius:var(--r-s)}
.tlmorebtn:hover{background:var(--hover);border-color:var(--border-focus)}
.tlwrap.folded .tlrow.tlhid{display:none}
/* P3: 日付チップ横の相対表現（日付ある行のみ・捏造なし） */
.tld .tlrel{display:block;font-size:10px;color:var(--faint);margin-top:1px;line-height:1.3}
/* P2: FBカード圧縮（既定=1行プレビュー＝次アクション、クリックで全文展開） */
.tlfbcard{cursor:pointer;position:relative}
.tlfbcard:hover{border-color:var(--border-focus)}
.tlhd{display:flex;align-items:center;flex-wrap:wrap;gap:0;padding-right:18px}
.tltog{position:absolute;top:7px;right:9px;color:var(--faint);font-size:14px;line-height:1;user-select:none}
.tlprev{font-size:var(--f-ui-smaller);color:var(--accent-2);margin-top:5px;line-height:1.5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tlfbcard.open .tlprev{display:none}
.tlfull{display:none;margin-top:5px}
.tlfbcard.open .tlfull{display:block}
.tlfull .tlt{margin-top:0}
.tlx .tlxl{display:inline-block;color:var(--faint);font-weight:600;margin-right:6px;font-size:11px}
.tlx.tlneg .tlxl{color:var(--err)}
.tlt.tltinline{display:inline;margin-top:0}
/* P3: 日付不明の記録（軸・日付列を持たないフラットな圧縮カード列） */
.tlundated{margin-top:14px;padding-top:12px;border-top:1px dashed var(--border)}
.tluh{display:flex;align-items:center;gap:6px;font-size:var(--f-ui-smaller);color:var(--muted);font-weight:600;padding:0 0 9px}
.tluh svg.ic{color:var(--faint);width:14px;height:14px}
.tluh .cnt{color:var(--faint);font-weight:400}
.tlulist{display:flex;flex-direction:column;gap:8px}
.tlcard.tluitem{margin-left:0}
/* 商談ジャーニーバー（カルテ）+ ホームのパイプライン節。トークンは tagchip/tl 系流用・両テーマCSS変数のみ */
.jbwrap{margin:4px 0 22px}
.jbh{display:flex;align-items:center;gap:7px;font-size:var(--f-ui-small);color:var(--muted);font-weight:600;padding:4px 0 9px;border-bottom:1px solid var(--border)}
.jbh svg.ic{color:var(--accent-2)}
.jbbar{display:flex;flex-wrap:wrap;align-items:center;gap:6px;padding:11px 0 2px}
.jbstep{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--border);border-radius:9px;padding:2px 8px;font-size:11px;line-height:16px;white-space:nowrap;color:var(--faint)}
.jbstep.past{background:var(--hover);color:var(--muted)}
.jbstep.cur{background:var(--accent-bg);border-color:var(--accent);color:var(--text);font-weight:600}
.jbstep.cur.jblost{background:transparent;border-color:var(--err);color:var(--err)}
.jbstep.reg{border-style:dashed;border-color:var(--warn);color:var(--muted)}
.jbstep .jbd{color:var(--faint);font-size:10px;font-weight:400;font-variant-numeric:tabular-nums}
.jbsep,.plarr{color:var(--faint);font-size:11px;flex:none;user-select:none}
.jbreg{color:var(--warn);font-size:11px;white-space:nowrap}
.plun{color:var(--faint);font-size:11px;white-space:nowrap;margin-left:2px}
.tagchip.pld{cursor:default;opacity:.55}
.tagchip.pld:hover{border-color:var(--border)}
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
/* stem/名前をキーにする索引は Object.create(null)（プロトタイプ無し）で作る。素の {} だと
   stem が "constructor"/"toString"/"__proto__" 等のとき Object.prototype のメンバが引けてしまい、
   cByStem[stem] が関数を返す→ grpVal が別グループに誤配置する（まとめ軸の島が消える）。 */
const cByStem=Object.create(null),dByStem=Object.create(null),rByStem=Object.create(null),cByNorm=Object.create(null);
DATA.clients.forEach(c=>{cByStem[c.stem]=c;if(c.name)cByNorm[nrm(c.name)]=c;});
DATA.docs.forEach(d=>dByStem[d.stem]=d);
DATA.reports.forEach(r=>rByStem[r.stem]=r);
/* 閲覧時点からの経過でバケット化（ビルド日でなく Date.now 基準 = 月次再生成の間も鮮度が生きる） */
function ageBucket(ds){if(!ds)return"";const t=Date.parse(ds);if(isNaN(t))return"";const dd=(Date.now()-t)/864e5;return dd<=31?"1ヶ月以内":dd<=92?"3ヶ月以内":dd<=183?"半年以内":dd<=366?"1年以内":"1年以上前";}
/* 経過日数の共通ヘルパ（P1 サマリーヘッダ / P5 テーブル経過日数バッジ で共用）。
   閾値 31/92 は ageBucket・HOMEWORK_STALE_DAYS と一致。日付なし/不正は null＝「出せない」を明示（捏造しない）。 */
function daysAgo(ds){if(!ds)return null;const t=Date.parse(ds);if(isNaN(t))return null;return Math.floor((Date.now()-t)/864e5);}
function ageSev(n){return n==null?"":n<=31?"ok":n<=92?"warn":"err";}
function agoLabel(n){return n==null?"":n<=0?"今日":n+"日前";}
function agoCoarse(ds){const n=daysAgo(ds);if(n==null)return"";if(n<=0)return"今日";if(n<7)return"約"+n+"日前";if(n<31)return"約"+Math.round(n/7)+"週前";if(n<365)return"約"+Math.round(n/30)+"ヶ月前";return"約"+Math.round(n/365)+"年前";}
function clientTags(c){const t=[];if(c.industry)t.push("業種/"+c.industry);if(c.phase)t.push("フェーズ/"+c.phase);if(c.bantg)t.push("BANT/"+c.bantg);t.push("最終接点/"+(ageBucket(c.last)||"記録なし"));if(c.temp)t.push("温度感/"+c.temp);if(c.hw)t.push("宿題/あり");(c.tans||[]).forEach(n=>t.push("担当/"+n));return t;}
function docTags(d){const t=[];if(d.doc_type)t.push("資料種別/"+d.doc_type);if(d.industry)t.push("業種/"+d.industry);if(d.solution)t.push("施策/"+d.solution);const a=ageBucket(d.modified);if(a)t.push("更新/"+a);if(d.src)t.push("情報源/"+d.src);(d.media||[]).forEach(m=>t.push("媒体/"+m));(d.vfmt||[]).forEach(v=>t.push("動画形式/"+v));if(d.fmt)t.push("形式/"+d.fmt);if(d.xc)t.push("横断/"+d.xc);(d.ents||[]).forEach(e=>{if(nrm(e)!==nrm(d.client))t.push("関係先/"+e.split(/[\/／]/).join("・"));});
 /* ナレッジ共有メタ4軸（分割/正規化は Python 側で済・ここは配列を素通しでタグ化）: カテゴリ(多値)/クライアント種別(多値)/提案プロダクト(多値)/施策手法(多値)/代理店(bool) */
 (d.category||[]).forEach(v=>t.push("カテゴリ/"+v));(d.client_tier||[]).forEach(v=>t.push("クライアント種別/"+v));(d.product||[]).forEach(v=>t.push("提案プロダクト/"+v));(d.method||[]).forEach(v=>t.push("施策手法/"+v));if(d.agency)t.push("代理店/あり");return t;}
const IDX=[];
DATA.clients.forEach(c=>IDX.push({kind:"client",stem:c.stem,name:c.name,folder:"clients",tags:clientTags(c),
 props:{"業界":c.industry,"フェーズ":c.phase,"BANT":c.bant,"最終接点":c.last||"—"},hay:(c.name+" "+c.industry+" "+c.phase+" "+c.bant+" "+(c.md||"")).toLowerCase(),ex:c.industry?("業種: "+c.industry):"",obj:c}));
DATA.docs.forEach(d=>IDX.push({kind:"doc",stem:d.stem,name:d.title,folder:"docs",tags:docTags(d),
 props:{"種別":d.doc_type,"取引先":d.client,"業界":d.industry,"施策":d.solution,"更新":d.modified||"—","情報源":d.src||"—"},hay:(d.title+" "+d.client+" "+d.industry+" "+d.solution+" "+d.doc_type+" "+d.src+" "+d.ex+" "+(d.ents||[]).join(" ")).toLowerCase(),ex:d.ex,obj:d}));
DATA.reports.forEach(r=>IDX.push({kind:"report",stem:r.stem,name:r.name,folder:"_reports",tags:[],props:{},hay:(r.name+" "+r.md).toLowerCase(),ex:"AI洗い出しレポート",obj:r}));
// タグ集計(親も加算)
const tagCount={};
IDX.forEach(it=>it.tags.forEach(t=>{const p=t.split("/");for(let i=1;i<=p.length;i++){const k=p.slice(0,i).join("/");tagCount[k]=(tagCount[k]||0)+1;}}));
const tagTree={};
Object.keys(tagCount).forEach(t=>{const p=t.split("/");if(p.length===1){tagTree[p[0]]=tagTree[p[0]]||{count:tagCount[p[0]]||0,children:{}};}
 else{tagTree[p[0]]=tagTree[p[0]]||{count:tagCount[p[0]]||0,children:{}};tagTree[p[0]].children[t]=tagCount[t];}});

/* ===== タグUX基盤: 系統色辞書(CATMETA) + 統一チップ部品(chipHtml) + runQuery 集約 ===== */
/* 色は既存パレット（PHASECOLOR/DTCOLOR/INDUSTRY_COLORS の hex）流用・中間明度。
   意味は常にラベルが担い、色は7pxドットのみ（色非依存原則・両テーマ共通） */
const CATMETA={"宿題":"#e0685f","温度感":"#e07a5f","担当":"#d0a24c","フェーズ":"#4f9df5","BANT":"#c98bdb","業種":"#54b981","最終接点":"#5fc9c9","資料種別":"#8a7cf5","カテゴリ":"#8a7cf5","クライアント種別":"#c98bdb","提案プロダクト":"#e05f8f","施策":"#e05f8f","施策手法":"#b5c94a","代理店":"#c98b6a","関係先":"#c98b6a","媒体":"#7f9cf5","動画形式":"#b5c94a","形式":"#d0a24c","横断":"#e0b34c","更新":"#d0912f","情報源":"#8a8a8a"};
/* タグペイン/ホームの意味順: 先頭7=取引先のタグ（行動を促す軸を先頭に）・残り=資料のタグ */
const TAGORDER=["宿題","温度感","担当","フェーズ","BANT","業種","最終接点","資料種別","カテゴリ","クライアント種別","提案プロダクト","施策","施策手法","代理店","関係先","媒体","動画形式","形式","横断","更新","情報源"];
function catColor(t){return CATMETA[(t||"").split("/")[0]]||"var(--accent)";}
/* 空白入りタグ値は tag:"値" で発行（parseQuery の引用符対応を利用。空白なしは従来と同一文字列） */
function tagQ(t){return /\s/.test(t)?'tag:"'+t+'"':"tag:"+t;}
function chipHtml(tag,mode){const p=tag.split("/"),cat=p[0],val=p.slice(1).join("/");
 return '<span class="tagchip" tabindex="0" role="button" data-tag="'+esc(tag)+'" style="--cc2:'+catColor(tag)+'" title="'+(mode==="and"?"#"+esc(tag)+" — クリックで絞り込みに追加":"#"+esc(tag)+" で検索")+'"><span class="tcdot" style="background:'+catColor(tag)+'"></span>'
  +(val?'<span class="tck">'+esc(cat)+'</span><span class="tcv">'+esc(val)+'</span>':'<span class="tcv">'+esc(cat)+'</span>')+'</span>';}
/* タグ/クエリ発行の単一路（従来4重複コードと同一手順: 検索ペインへ→input反映→__lastQ→再実行） */
function runQuery(q){setPane("search");setTimeout(()=>{const i=$("#searchInput");i.value=q;window.__lastQ=q;runSearchPane(q,$("#searchOut"));},30);}

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
 RIBBON.forEach(([id,label,kind])=>{const el=document.createElement("div");el.className="ri";el.dataset.id=id;el.title=label||id;el.innerHTML=ic(id);el.tabIndex=0;el.setAttribute("role","button");
  el.onclick=()=>{if(kind==="view")openGraph();else if(kind==="view2")tableView();else if(kind==="pane")setPane(id);else if(id==="vault")qsOpen();};r.appendChild(el);});
 const sp=document.createElement("div");sp.className="sp";r.appendChild(sp);
 const th=document.createElement("div");th.className="ri";th.id="themeBtn";th.title="テーマ切替（ライト/ダーク）";th.innerHTML=ic(document.documentElement.getAttribute("data-theme")==="dark"?"sun":"moon");th.tabIndex=0;th.setAttribute("role","button");th.onclick=toggleTheme;r.appendChild(th);
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
 $("#sideAct1").onclick=null;$("#sideAct2").onclick=null;   /* ペイン跨ぎのハンドラ残留防止（タグペインの並び順トグル等） */
 $("#sideAct1").title="";$("#sideAct2").title="";   /* title 残留防止 */
 if(pane==="files")return renderFiles(b);
 if(pane==="search")return renderSearchPane(b);
 if(pane==="tags")return renderTags(b);
 if(pane==="bookmark")return renderBookmarks(b);
}
function treeRow(opts){// {chevron,icon,label,count,indent,active,onclick,onchev}
 const d=document.createElement("div");d.className="trow"+(opts.active?" active":"")+(opts.closed?" closed":"");
 d.tabIndex=0;d.setAttribute("role","button");
 d.style.paddingLeft=(6+(opts.indent||0)*14)+"px";
 d.innerHTML=(opts.chevron?'<span class="tw">'+ic("chev")+'</span>':'<span class="tw"></span>')
  +(opts.icon?(opts.iconColor?'<span style="color:'+opts.iconColor+';opacity:.85;display:flex">'+ic(opts.icon)+'</span>':ic(opts.icon,opts.iconCls||"")):"")   /* iconColor 指定時は色クラスを外し系統色を効かせる */
  +(opts.dot?'<span class="tcdot" style="background:'+opts.dot+'"></span>':"")
  +'<span class="lbl"'+(opts.bold?' style="color:var(--text)"':"")+' title="'+esc(opts.label)+'">'+esc(opts.label)+'</span>'
  +(opts.count!=null?'<span class="cnt">'+opts.count+'</span>':"");
 if(opts.key){d.dataset.key=opts.key;d.classList.add("filerow");}
 if(opts.onclick)d.onclick=opts.onclick;return d;
}
const _grpState={_reports:true,clients:false,docs:false};   // フォルダ開閉状態を保持
function renderFiles(b,revealKey){
 const wrap=document.createElement("div");wrap.className="tree";b.appendChild(wrap);
 const revGrp=revealKey?({c:"clients",d:"docs",r:"_reports"}[revealKey[0]]):null;
 const groups=[...(DATA.reports.length?[["_reports",DATA.reports.map(r=>({name:r.name,stem:r.stem,k:"r"}))]]:[]),
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
function openByK(k,stem){k==="c"?openClient(stem):k==="r"?openReport(stem):k==="table"?tableView():openDoc(stem);}   /* table=疑似ビュー復元（tblFilter はモジュール変数で残存するので呼び直しで足りる） */

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
 let dt=null;   /* 120ms debounce（1525件再フィルタの体感改善）。__lastQ は即時更新=状態保持は不変 */
 inp.addEventListener("input",()=>{window.__lastQ=inp.value;clearTimeout(dt);dt=setTimeout(()=>runSearchPane(inp.value,out),120);});
 runSearchPane(inp.value,out);
}
/* ---- アクティブフィルタ可視化: parseQuery と同一 regex 1パスで raw トークンを抽出（表示専用・
   parseQuery/matchItem は不改造）。表示が万一ズレても検索結果は parseQuery 側で不変（fail-safe） ---- */
function qTokens(q){const T=[];
 q=q.replace(/(^|\s)\/((?:\\.|[^\/\\])+)\/(?=\s|$)/g,(m,pre,pat)=>{T.push({raw:"/"+pat+"/",t:"re",v:pat});return pre+" ";});
 const re=/\[([^\]:]+)(?::([^\]]*))?\]|(-?)(tag|path|file):("[^"]*"|\S+)|"([^"]+)"|(\S+)/g;let m;
 while(m=re.exec(q)){
  if(m[1]!=null)T.push({raw:m[0],t:"prop",k:m[1].trim(),v:(m[2]||"").trim()});
  else if(m[4]){const v=(m[5]||"").replace(/^"|"$/g,"");if(v)T.push({raw:m[0],t:m[4],v:v,neg:m[3]==="-"});}
  else if(m[6]!=null)T.push({raw:m[0],t:"exact",v:m[6]});
  else if(m[7]&&m[7]!=="-")T.push(m[7][0]==="-"?{raw:m[0],t:"word",v:m[7].slice(1),neg:true}:{raw:m[0],t:"word",v:m[7]});
 }return T;}
/* raw トークンの完全一致除去（前後が空白/端の出現のみ）。見つからなければ何もしない（fail-safe） */
function rmToken(val,raw){let i=-1;
 while((i=val.indexOf(raw,i+1))!==-1){
  const b=i===0||/\s/.test(val[i-1]),a=i+raw.length===val.length||/\s/.test(val[i+raw.length]);
  if(b&&a)return (val.slice(0,i).trimEnd()+" "+val.slice(i+raw.length).trimStart()).trim();
 }return val;}
const TYPELBL={path:"フォルダ",file:"ファイル名",exact:"完全一致",re:"正規表現",word:"含む"};
function fbarHtml(q){const T=qTokens(q);if(!T.length)return "";
 let h='<div class="fbar">';
 T.forEach(tk=>{let inner;
  if(tk.t==="tag")inner='<span class="tcdot" style="background:'+catColor(tk.v)+'"></span>'+(tk.neg?'<span class="tck">除外</span>':'')+'<span class="tcv'+(tk.neg?' tcneg':'')+'">'+esc(tk.v)+'</span>';
  else if(tk.t==="prop")inner='<span class="tck">'+esc(tk.k)+'</span><span class="tcv">'+esc(tk.v==="null"?"（値なし）":tk.v===""?"（値あり）":tk.v)+'</span>';
  else inner='<span class="tck">'+(tk.neg?"除外":TYPELBL[tk.t]||"")+'</span><span class="tcv'+(tk.neg?' tcneg':'')+'">'+esc(tk.v)+'</span>';
  h+='<span class="tagchip fchip" tabindex="0" role="button" data-rm="'+esc(tk.raw)+'"'+(tk.t==="tag"?' style="--cc2:'+catColor(tk.v)+'"':'')+'>'+inner+'<span class="fx" tabindex="0" role="button" title="この条件を外す">×</span></span>';});
 if(T.length>=2)h+='<span class="fclear" tabindex="0" role="button">すべて解除</span>';
 return h+'</div>';}
/* 空クエリ時ヘルプ: 実データ（tagTree/tagCount）由来のクリック実行例 + 構文リファレンス。
   例は matchItem で実件数を検証し 1件以上ヒットするものだけ載せる（件数併記は tag 例のみ） */
function qhelpHtml(){
 const hitN=q=>{const P=parseQuery(q);return IDX.filter(it=>matchItem(it,P)).length;};
 const leaves=Object.keys(tagCount).filter(t=>t.includes("/")).sort((a,b)=>tagCount[b]-tagCount[a]);
 const ex=[],seen=new Set();
 for(const t of leaves){const c=t.split("/")[0];if(seen.has(c))continue;seen.add(c);ex.push({q:tagQ(t),n:tagCount[t]});if(ex.length>=2)break;}
 const top=leaves.slice(0,8);
 outer:for(let i=0;i<top.length;i++)for(let j=0;j<top.length;j++){
  if(i===j||top[i].split("/")[0]===top[j].split("/")[0])continue;
  const q=tagQ(top[i])+" "+tagQ(top[j]),n=hitN(q);
  if(n>0){ex.push({q:q,n:n});break outer;}}
 outer2:for(let i=0;i<top.length;i++)for(let j=0;j<top.length;j++){
  if(i===j)continue;
  const q=tagQ(top[i])+" -"+tagQ(top[j]),n=hitN(q);
  if(n>0&&n<tagCount[top[i]]){ex.push({q:q,n:n});break outer2;}}
 const chips=ex.map(e=>'<span class="tagchip qxc" data-q="'+esc(e.q)+'"><span class="tcv">'+esc(e.q)+'</span><span class="qfn">'+e.n+'</span></span>').join(" ");
 return '<div class="qhelp qex">'+(chips?'<b style="color:var(--muted)">例（クリックで実行）</b><br>'+chips+'<br><br>':'')
  +'演算子が使えます：<br><code>tag:業種/食品</code> タグ(子も一致)<br><code>-tag:横断</code> タグ除外<br><code>path:clients</code> フォルダ<br><code>file:提案</code> ファイル名<br><code>[業界:IT]</code> プロパティ<br><code>-除外語</code> / <code>"完全一致"</code> / <code>/正規表現/</code></div>';}
function hl(text,terms){let s=esc(text);terms.forEach(t=>{if(t&&t.length>1){s=s.replace(new RegExp("("+t.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")+")","ig"),"<mark>$1</mark>");}});return s;}
function runSearchPane(q,out){
 q=q.trim();
 if(!q){if(!window.__qhelp)window.__qhelp=qhelpHtml();out.innerHTML=window.__qhelp;
  out.querySelectorAll(".qxc").forEach(el=>el.onclick=()=>{const i=$("#searchInput");i.value=el.dataset.q;window.__lastQ=i.value;runSearchPane(i.value,out);});
  return;}
 const P=parseQuery(q);const hits=IDX.filter(it=>matchItem(it,P));
 const byFolder={clients:[],docs:[],_reports:[]};hits.forEach(h=>byFolder[h.folder].push(h));
 let html='<div class="scount">'+hits.length+' 件ヒット'+fbarHtml(q)+(hits.length?'':'<div class="frec">×で条件を外してください</div>')+'</div>';
 [["_reports","レポート"],["clients","取引先"],["docs","資料"]].forEach(([fk,label])=>{
  const arr=byFolder[fk];if(!arr.length)return;
  html+='<div class="sgroup"><div class="sg-h">'+ic("folder","folder-ico")+' '+label+'<span class="cnt">'+arr.length+'</span></div><div class="sres">';
  arr.slice(0,60).forEach(it=>{const tg=it.tags.slice(0,3).map(t=>chipHtml(t,"and")).join("")+(it.tags.length>3?'<span class="tcmore">+'+(it.tags.length-3)+'</span>':'');
   html+='<div class="sr" tabindex="0" role="button" data-k="'+it.kind[0]+'" data-s="'+esc(it.stem)+'"><div class="srt">'+esc(it.name)+'</div>'+(tg?'<div class="srtags">'+tg+'</div>':'')+(it.ex?'<div class="srx">'+hl(it.ex,P.plain)+'</div>':"")+'</div>';});
  if(arr.length>60)html+='<div class="scount">…他 '+(arr.length-60)+' 件</div>';
  html+='</div></div>';
 });
 out.innerHTML=html;
 out.querySelectorAll(".sr").forEach(el=>el.onclick=()=>openByK(el.dataset.k,el.dataset.s));
 out.querySelectorAll(".sr .tagchip").forEach(ch=>ch.onclick=e=>{e.stopPropagation();addTagToQuery(ch.dataset.tag);});   /* 結果行チップ=AND追加（行openとは分離） */
 out.querySelectorAll(".fchip .fx").forEach(x=>x.onclick=e=>{e.stopPropagation();const i=$("#searchInput");i.value=rmToken(i.value,x.parentElement.dataset.rm);window.__lastQ=i.value;runSearchPane(i.value,out);});
 const fc=out.querySelector(".fclear");if(fc)fc.onclick=()=>{const i=$("#searchInput");i.value="";window.__lastQ="";runSearchPane("",out);};
 out.querySelectorAll(".sg-h").forEach(h=>h.onclick=()=>{const r=h.nextElementSibling;r.style.display=r.style.display==="none"?"":"none";});
}
/* 一貫則: 結果一覧の中=絞り込み継続（AND追記・重複チェック）。それ以外のタグ面=新しい検索（runQuery置換） */
function addTagToQuery(tag){const i=$("#searchInput");if(!i)return;
 if(qTokens(i.value).some(tk=>tk.t==="tag"&&!tk.neg&&tk.v.toLowerCase()===tag.toLowerCase()))return;
 i.value=(i.value.trim()+" "+tagQ(tag)).trim();window.__lastQ=i.value;runSearchPane(i.value,$("#searchOut"));}
/* ---- タグ(ネスト・意味順+区切り見出し・系統色・ペイン内絞り込み・件数/五十音トグル・12超畳み・アクティブ表示)
   クリック挙動は既存置換（runQuery）のまま完全不変。凡例パネルは作らない（意味は常にラベルが担う） ---- */
let tagSortAlpha=false;
function renderTags(b){
 const a1=$("#sideAct1");a1.innerHTML=ic("list");a1.title="並び順を切替（件数順⇄五十音順）";
 const f=document.createElement("div");f.className="sfield";f.innerHTML=ic("search")+'<input placeholder="タグを絞り込み…">';b.appendChild(f);
 const wrap=document.createElement("div");wrap.className="tree";b.appendChild(wrap);
 const inp=f.querySelector("input");
 const openSet=new Set();if(window.__tagFocus){openSet.add(window.__tagFocus);delete window.__tagFocus;}   /* ホームのテーザー経由の初期展開 */
 try{const act0=parseQuery(window.__lastQ||"").tag;   /* active タグの系統を初回のみ seed（再訪時に active 葉が見える。以後の手動畳みは既存トグルが優先） */
  if(act0.length)Object.keys(tagTree).forEach(top=>{if(Object.keys(tagTree[top].children).some(full=>act0.some(a=>tagMatch([full],a))))openSet.add(top);});}catch(e){}
 const moreSet=new Set();
 function build(){
  wrap.innerHTML="";const q=(inp.value||"").trim().toLowerCase();
  let act=[];try{act=parseQuery(window.__lastQ||"").tag;}catch(e){}
  const isAct=t=>act.some(a=>tagMatch([t],a));   /* __lastQ 内のタグと tagMatch 照合 */
  const known=new Set(TAGORDER);
  const extra=Object.keys(tagTree).filter(t=>!known.has(t)).sort();   /* 未知系統は末尾へ（防御） */
  let total=0;   /* 絞り込み0件の空状態表示用 */
  [["取引先のタグ",TAGORDER.slice(0,7)],["資料のタグ",TAGORDER.slice(7).concat(extra)]].forEach(([gl,cats])=>{
   const present=cats.filter(c=>tagTree[c]);if(!present.length)return;
   let ghead=null;
   if(!q){ghead=document.createElement("div");ghead.className="tghead";ghead.textContent=gl;wrap.appendChild(ghead);}
   let shown=0;
   present.forEach(top=>{
    const node=tagTree[top],col=CATMETA[top]||"var(--accent)";
    const kids=Object.keys(node.children);
    kids.sort(tagSortAlpha?(x,y)=>x.localeCompare(y,"ja"):(x,y)=>node.children[y]-node.children[x]||x.localeCompare(y,"ja"));
    let vk=kids;
    if(q&&!top.toLowerCase().includes(q)){vk=kids.filter(k=>k.toLowerCase().includes(q));if(!vk.length)return;}   /* ヒット枝のみ+自動展開 */
    shown++;
    let open=q?true:openSet.has(top);
    const fr=treeRow({chevron:kids.length>0,icon:"hash",iconCls:"tag-ico",iconColor:col,label:top,count:node.count,closed:!open,active:isAct(top)});
    const ch=document.createElement("div");ch.className="tchildren"+(open?"":" hidden");
    const fold=!q&&vk.length>12&&!moreSet.has(top);   /* 葉12超は畳む（progressive disclosure） */
    (fold?vk.slice(0,12):vk).forEach(full=>{const leaf=full.split("/").slice(1).join("/");
     ch.appendChild(treeRow({dot:col,label:leaf,count:node.children[full],indent:1,active:isAct(full),onclick:()=>runQuery(tagQ(full))}));});
    if(fold){const mr=treeRow({label:"他 "+(vk.length-12)+" 件を表示",indent:1});mr.classList.add("tmore");
     mr.onclick=()=>{moreSet.add(top);openSet.add(top);build();};ch.appendChild(mr);}
    fr.onclick=()=>{if(!kids.length){runQuery(tagQ(top));return;}open=!open;open?openSet.add(top):openSet.delete(top);fr.classList.toggle("closed",!open);ch.classList.toggle("hidden",!open);};
    wrap.appendChild(fr);wrap.appendChild(ch);
   });
   if(!shown&&ghead)ghead.remove();
   total+=shown;
  });
  if(!total){const d=document.createElement("div");d.className="qhelp";d.textContent="『"+(inp.value||"").trim()+"』に一致するタグはありません";wrap.appendChild(d);}
 }
 let dt=null;inp.addEventListener("input",()=>{clearTimeout(dt);dt=setTimeout(build,80);});
 a1.onclick=()=>{tagSortAlpha=!tagSortAlpha;build();};
 build();
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
 const home=document.createElement("div");home.className="tab"+(!lastNote?" on":"");home.tabIndex=0;home.setAttribute("role","button");home.innerHTML=ic("files")+'<span class="lbl">ホーム</span>';
 home.onclick=()=>{lastNote=null;showDoc();welcome();renderTabs();renderVhead();};tb.appendChild(home);
 if(lastNote){const t=document.createElement("div");t.className="tab on";t.tabIndex=0;t.setAttribute("role","button");t.title=lastNote.title;
  t.innerHTML=ic(lastNote.icon)+'<span class="lbl">'+esc(lastNote.title)+'</span><span class="x" tabindex="0" role="button">'+ic("x")+'</span>';
  t.querySelector(".x").onclick=e=>{e.stopPropagation();lastNote=null;welcome();renderTabs();renderVhead();};tb.appendChild(t);}
 const nt=document.createElement("div");nt.className="tab newtab";nt.title="新しいタブ (⌘O)";nt.tabIndex=0;nt.setAttribute("role","button");nt.innerHTML=ic("plus");nt.onclick=()=>qsOpen("nav");tb.appendChild(nt);
}
function renderVhead(){
 const v=$("#vhead");
 v.innerHTML='<span class="nav" id="navBack" title="戻る">'+ic("back")+'</span>'+
  '<span class="crumb">'+(lastNote?esc((lastNote.folder||"")+" / "+lastNote.title):"ホーム")+'</span><span class="sp"></span>'+
  (lastNote&&/^[cdr]:/.test(lastNote.key)?'<span class="nav" id="bmBtn" title="ブックマーク">'+ic("bookmark")+'</span>':"");   /* 実ノートのみ（table/graph 疑似ビューには出さない） */
 const back=v.querySelector("#navBack");if(back)back.onclick=()=>{history.pop();const p=history.pop();if(p)openByK(p[0],p[1]);else{lastNote=null;welcome();renderTabs();}};
 const bm=v.querySelector("#bmBtn");if(bm){const key=lastNote.key;bm.style.color=bookmarks.has(key)?"var(--accent-2)":"";
  bm.onclick=()=>{bookmarks.has(key)?bookmarks.delete(key):bookmarks.add(key);saveBm();renderVhead();if(pane==="bookmark")renderPane();};}
}

/* ===== 中央表示 ===== */
function showDoc(){$("#graphWrap").style.display="none";$("#docPane").style.display="block";ribbonActive(pane);}
function showGraphView(){$("#graphWrap").style.display="block";$("#docPane").style.display="none";ribbonActive("graph");}
/* ホーム「クイックフィルタ」プリセット（1箇所定義）。タグ値名は直書きに頼らず tagCount の
   動的存在チェックで防御（生成器の出力文字列変更で 0件/消滅しても静かに壊れない）。
   件数バッジ=tagCount は全プリセットが「1アイテム1タグ」の系統のみなので検索ヒット件数と一致 */
const QF=[["宿題あり","宿題/あり"],["温度感ネガ優勢","温度感/ネガ優勢"],["温度感高","温度感/高"],["フォロー漏れ","最終接点/1年以上前"],["更新1ヶ月以内","更新/1ヶ月以内"],["横断ナレッジ","横断"],["ショート動画","動画形式/ショート"],["提案書","資料種別/提案書"]];
function welcome(){
 showDoc();$("#docPane").scrollTop=0;   /* 前ノートの scrollTop 残留防止 */
 const notable=DATA.clients.filter(c=>c.fb>0).sort((a,b)=>(b.fb+b.doc)-(a.fb+a.doc)).slice(0,6);
 const cards=k=>k.map(c=>`<div class="wcard" data-k="c" data-s="${esc(c.stem)}">`+ic("building")+`<div><div class="wt">${esc(c.name)}</div><div class="wx">${esc(c.industry||"業界未設定")} ・ FB${c.fb} / 資料${c.doc}</div></div></div>`).join("");
 const rep=DATA.reports.map(r=>`<div class="wcard" data-k="r" data-s="${esc(r.stem)}">`+ic("report")+`<div><div class="wt">${esc(r.name)}</div><div class="wx">AI洗い出しレポート</div></div></div>`).join("");
 const qf=QF.filter(([l,t])=>tagCount[t]>0).slice(0,8).map(([l,t])=>{const tb=l==="宿題あり";   /* 宿題triageだけ作業面=テーブル着地（次アクション/最終接点列でさばく）。他は従来どおり検索着地 */
  return '<span class="tagchip '+(tb?"qft":"qfc")+'" tabindex="0" role="button" data-q="'+esc(tagQ(t))+'" style="--cc2:'+catColor(t)+'" title="'+(tb?"宿題ありのみで取引先テーブルを開く":"#"+esc(t)+" で検索")+'"><span class="tcdot" style="background:'+catColor(t)+'"></span><span class="tcv">'+esc(l)+'</span><span class="qfn">'+tagCount[t]+'</span></span>';}).join("");
 /* パイプライン節: PHASESTEPS 順の分布チップ（クリック=テーブルにフェーズ絞り込み着地）。
    集計は都度 reduce（860件で無視できる）。0社フェーズは非クリック（テーブル select に無い値を選択状態にしない）。
    「未設定」は c.phase==="" でテーブルの phase フィルタが表現できないため muted 非クリック */
 const pcnt={};DATA.clients.forEach(c=>{if(c.phase)pcnt[c.phase]=(pcnt[c.phase]||0)+1;});
 const unset=DATA.clients.filter(c=>!c.phase).length;
 const plChip=p=>{const n=pcnt[p]||0,col=PHASECOLOR[p]||"#8a8a8a";
  return n?'<span class="tagchip plc" tabindex="0" role="button" data-p="'+esc(p)+'" style="--cc2:'+col+'" title="'+esc(p)+' の取引先をテーブルで開く"><span class="tcdot" style="background:'+col+'"></span><span class="tcv">'+esc(phShort(p))+'</span><span class="qfn">'+n+'</span></span>'
   :'<span class="tagchip pld" title="'+esc(p)+'・該当なし"><span class="tcdot" style="background:'+col+'"></span><span class="tcv">'+esc(phShort(p))+'</span><span class="qfn">0</span></span>';};
 const pl=PHASESTEPS.map(plChip).join('<span class="plarr">→</span>')
  +(pcnt["失注"]?'<span class="plarr">・</span>'+plChip("失注"):"")
  +'<span class="plun">未設定 '+unset+'</span>';
 const tz=TAGORDER.filter(c=>tagTree[c]).map(c=>'<span class="tagchip tzc" tabindex="0" role="button" data-c="'+esc(c)+'" style="--cc2:'+(CATMETA[c]||"var(--accent)")+'" title="タグペインで「'+esc(c)+'」を開く"><span class="tcdot" style="background:'+(CATMETA[c]||"var(--accent)")+'"></span><span class="tcv">'+esc(c)+'</span><span class="qfn">'+(tagCount[c]||0)+'</span></span>').join("");
 $("#inner").innerHTML=`<div class="welcome"><h1>${ic("vault")} AiLaVault</h1>
  <p class="sub">営業16名の社内ナレッジ — ${DATA.stats.clients} 取引先 / ${DATA.stats.docs} 資料。左の検索・タグ・グラフで分類・回遊できます。取引先カルテには資料と商談FBを時系列で一望できる<b>施策タイムライン</b>付き。<kbd>⌘O</kbd> でどこへでもジャンプ。</p>
  ${qf?`<div class="wsec">クイックフィルタ</div><div class="chiprow">${qf}</div>`:""}
  <div class="wsec">パイプライン</div><div class="chiprow plrow">${pl}</div>
  ${tz?`<div class="wsec">タグで探す</div><div class="chiprow tzrow">${tz}</div>`:""}
  ${DATA.reports.length?`<div class="wsec">AI洗い出しレポート</div><div class="wgrid">${rep}</div>`:""}
  <div class="wsec">主要な取引先</div><div class="wgrid">${cards(notable)}</div></div>`;
 $("#inner").querySelectorAll(".wcard").forEach(el=>el.onclick=()=>openByK(el.dataset.k,el.dataset.s));
 $("#inner").querySelectorAll(".qfc").forEach(el=>el.onclick=()=>runQuery(el.dataset.q));   /* 置換で検索ペインへ（チップバー付きで着地） */
 $("#inner").querySelectorAll(".qft").forEach(el=>el.onclick=()=>{tblFilter={q:"",ind:"",phase:"",temp:"",hw:true};tableView();});   /* 宿題あり→テーブル（hwのみON・他条件リセット） */
 $("#inner").querySelectorAll(".plc").forEach(el=>el.onclick=()=>{tblFilter={q:"",ind:"",phase:el.dataset.p,temp:"",hw:false};tableView();});   /* リセット着地=チップ社数と表示件数の一致を保証（select にも selected 復元） */
 $("#inner").querySelectorAll(".tzc").forEach(el=>el.onclick=()=>{window.__tagFocus=el.dataset.c;setPane("tags");});   /* 該当系統を初期展開 */
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
 +'</div>';}
function navTarget(t){if(t.indexOf("clients/")===0)return openClient(t.slice(8));if(t.indexOf("docs/")===0)return openDoc(t.slice(5));if(cByStem[t])return openClient(t);if(dByStem[t])return openDoc(t);const c=cByNorm[nrm(t)];if(c)return openClient(c.stem);}
function afterOpen(){
 $("#inner").querySelectorAll(".wl").forEach(el=>el.onclick=()=>navTarget(el.dataset.t));
 $("#inner").querySelectorAll(".tg,.ntag,.note-tags .tagchip").forEach(el=>el.onclick=()=>runQuery(tagQ(el.dataset.tag)));
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
/* FBカード=既定1行プレビュー(=次アクション。無ければ menu→pos)＋クリックで全文展開(menu/ポジ/ネガ/次)。
   資料カードは元々1行(バッジ＋タイトルwl)なので展開対象外。P2＝観察1(中央206字/枚)の密度改善。 */
function tlCardInner(x){
 if(x.kind==="doc"){const d=x.e,col=dtColor(d.doc_type);
  return '<span class="tlbadge" style="background:'+col+'22">'+esc(d.doc_type||"資料")+'</span>'
   +'<span class="tlt tltinline"><span class="wl" data-t="docs/'+esc(d.stem)+'">'+esc(d.title)+'</span></span>';
 }
 const f=x.e;
 const head='<div class="tlhd"><span class="tlbadge tlfb">商談FB'+(f.src?"・"+esc(f.src):"")+'</span>'
  +(f.ph?'<span class="tlchip">'+esc(f.ph)+'</span>':"")
  +(f.bant?'<span class="tlchip">'+esc(f.bant)+'</span>':"")
  +(f.by?'<span class="tlchip tlby" title="FB送信者">'+esc(f.by)+'</span>':"")
  +'<span class="tltog" aria-hidden="true">＋</span></div>';
 const prevText=f.next?("→ "+f.next):(f.menu||f.pos||"詳細を表示");
 const prev='<div class="tlprev">'+esc(prevText)+'</div>';
 const full='<div class="tlfull">'
  +(f.menu?'<div class="tlt">'+esc(f.menu)+'</div>':"")
  +(f.pos?'<div class="tlx"><span class="tlxl">ポジ</span>'+esc(f.pos)+'</div>':"")
  +(f.neg?'<div class="tlx tlneg"><span class="tlxl">ネガ</span>'+esc(f.neg)+'</div>':"")
  +(f.next?'<div class="tlnx">→ 次: '+esc(f.next)+'</div>':"")
  +'</div>';
 return head+prev+full;
}
function tlSection(c){
 const ev=tlEvents(c);
 if(!ev.length)return '<div class="tlwrap"><div class="tlh">'+ic("cal")+'施策タイムライン<span class="cnt">0</span></div><div class="tlbody"><div class="qhelp">記録済みFB・資料なし</div></div></div>';   /* 0件でも見出しごと消さない */
 /* P3: 日付あり/なしで分割。dated=従来の縦軸(相対を任意添付)、undated=軸下に別ブロック(「—」列を根絶)。推測日付は生成しない */
 const dated=ev.filter(x=>x.d),undated=ev.filter(x=>!x.d);
 /* B: 外側 fold は 3件目以降(dated軸側のみ)。tlmorebtn 保持 */
 const drows=dated.map((x,i)=>{
  const isfb=x.kind==="fb";
  const cls="tlrow"+(x.kind==="doc"?" tldoc":"")+(i>=3?" tlhid":"");
  return '<div class="'+cls+'"><div class="tld">'+esc(x.d)+'<span class="tlrel">'+esc(agoCoarse(x.d))+'</span></div>'
   +'<div class="tlcard'+(isfb?" tlfbcard":"")+'">'+tlCardInner(x)+'</div></div>';
 }).join("");
 const datedHtml=dated.length?'<div class="tlbody">'+drows+'</div>':"";
 const more=dated.length>3?'<div class="tlmorebtn">さらに'+(dated.length-3)+'件を表示</div>':"";
 const ucards=undated.map(x=>'<div class="tlcard tluitem'+(x.kind==="fb"?" tlfbcard":" tludoc")+'">'+tlCardInner(x)+'</div>').join("");
 const ublock=undated.length?'<div class="tlundated"><div class="tluh">'+ic("help")+'日付不明の記録 <span class="cnt">'+undated.length+'件</span></div><div class="tlulist">'+ucards+'</div></div>':"";
 return '<div class="tlwrap'+(dated.length>3?" folded":"")+'"><div class="tlh">'+ic("cal")+'施策タイムライン<span class="cnt">'+ev.length+'</span></div>'+datedHtml+more+ublock+'</div>';
}
function bindTl(){const mb=$("#inner").querySelector(".tlmorebtn");
 if(mb){const lbl=mb.textContent;   /* 展開⇄再畳みの往復トグル（remove しない） */
  mb.onclick=()=>{const w=mb.closest(".tlwrap");if(!w)return;const folded=w.classList.toggle("folded");mb.textContent=folded?lbl:"畳む";};}
 /* P2: FBカードのクリックで全文トグル（カード内に wikilink 無し＝全体クリックに競合なし） */
 $("#inner").querySelectorAll(".tlfbcard").forEach(el=>el.onclick=()=>{const o=el.classList.toggle("open");const t=el.querySelector(".tltog");if(t)t.textContent=o?"−":"＋";});}
/* ===== 商談ジャーニーバー（カルテ内: PHASESTEPS 順の現在地 + 到達日） =====
   到達日 = tl の同フェーズ dated イベントの最小日（決定論・payload 増ゼロ・架空日付は出さない）。
   既知の限界: FB_MAX_EVENTS=30 cap で FB30件超のクライアントは最古が切られ初出日が後ろにずれ得る */
function phaseDates(c){const m={};(c.tl||[]).forEach(e=>{if(e.ph&&e.d&&(!m[e.ph]||e.d<m[e.ph]))m[e.ph]=e.d;});return m;}
function jbSection(c){
 const dates=phaseDates(c),lost=c.phase==="失注",idx=PHASESTEPS.indexOf(c.phase);
 if(!lost&&idx<0&&!PHASESTEPS.some(p=>dates[p]))return "";   /* フェーズ未設定/その他かつ dated ph 無し → バー非表示（データが無い演出をしない） */
 let reg=false;
 const cells=PHASESTEPS.map((p,i)=>{
  const fail=lost&&i===PHASESTEPS.length-1;                  /* 失注は終端を err 色で差し替え */
  const name=fail?"失注":p,d=dates[name]||"";
  let st="todo";
  if(fail||i===idx)st="cur";
  else if(lost||i<idx||(idx<0&&d))st="past";
  else if(idx>=0&&d){st="reg";reg=true;}                     /* 現在地より後に到達日=後退（点線+日付・色のみに依存しない） */
  return '<span class="jbstep '+st+(fail?" jblost":"")+'" title="'+esc(name)+(st==="cur"?"（現在地）":"")+(d?"・初出 "+esc(d):"")+'">'
   +'<span class="tcdot" style="background:'+(PHASECOLOR[name]||"#8a8a8a")+'"></span><span class="jbl">'+esc(phShort(name))+'</span>'
   +(d&&st!=="todo"?'<span class="jbd">'+esc(d)+'</span>':"")+'</span>';
 }).join('<span class="jbsep">→</span>');
 return '<div class="jbwrap"><div class="jbh">'+ic("trend")+'商談ジャーニー</div><div class="jbbar">'+cells
  +(reg?'<span class="jbreg">↩ 後退あり</span>':"")+'</div></div>';
}
/* ===== P1 サマリーヘッダ「商談スナップショット」（propsPanel 撤去の受け皿・朝の商談前に見る面） =====
   全項目 条件付き描画（データが無い項目は枠ごと出さない＝演出禁止）。色は既存 --ok/warn/err・CATMETA 流用。 */
function khc(emo,label,val,sev,dotcat){
 const dot=dotcat?'<span class="khdot" style="background:'+(CATMETA[dotcat]||"var(--accent)")+'"></span>':"";
 return '<div class="khc'+(sev?" "+sev:"")+(label==="次の一手"?" khnx":"")+'">'+(emo?'<span class="khi">'+emo+'</span>':"")+dot
  +'<span class="khl">'+esc(label)+'</span><span class="khv">'+esc(val)+'</span></div>';
}
function summaryHeader(c){
 /* 資料だけ取引先(62%)の劣化形: tl 空なら1行に畳む（空ラベルを並べない） */
 if(!(c.tl&&c.tl.length)){
  const bits=[];if(c.industry)bits.push("業種/"+c.industry);if(c.doc)bits.push("資料"+c.doc+"件");bits.push("商談FBは未記録");
  return '<div class="khdr khmin">'+esc(bits.join(" ・ "))+'</div>';
 }
 const n=daysAgo(c.last),parts=[];
 parts.push(n!=null?khc("⏱","最終接点",agoLabel(n),ageSev(n)):khc("⏱","最終接点","接点記録なし","muted"));
 if(c.nx)parts.push(khc("🎯","次の一手",c.nx,c.hw?"err":""));   /* 宿題(hw)なら err で放置を主張＝「次の一手提示」を本項に吸収 */
 if(c.tans&&c.tans.length)parts.push(khc("👤","担当",c.tans.join("・"),""));
 if(c.temp)parts.push(khc("","温度感",c.temp,"","温度感"));      /* CATMETA 色ドット */
 parts.push(khc("💬","活動","FB"+c.fb+"・資料"+c.doc,""));       /* 撤去された fb/doc_count の受け */
 return '<div class="khdr">'+parts.join("")+'</div>';
}
function ageCell(ds){if(!ds)return '<span style="color:var(--faint)">—</span>';   /* P5: テーブル最終接点=相対+重症度色。ソートは lastOf 不変(表示のみ) */
 const n=daysAgo(ds);return '<span class="age '+ageSev(n)+'" title="'+esc(ds)+'"><span class="agedot"></span>'+esc(agoLabel(n))+'</span>';}
function openClient(stem){const c=cByStem[stem];if(!c)return;showDoc();pushHist("c",stem);
 lastNote={key:"c:"+stem,title:c.name,icon:"building",folder:"clients"};
 const bodyMd=c.md?md(c.md):"";
 const ctg=clientTags(c);   /* 「このタグの仲間を探す」チップ行（クリック=runQuery置換・afterOpenでバインド） */
 $("#inner").innerHTML='<div class="inline-title">'+esc(c.name)+'</div>'
  +(ctg.length?'<div class="note-tags">'+ctg.map(t=>chipHtml(t)).join("")+'</div>':'')
  +summaryHeader(c)   /* propsPanel 撤去の受け皿（P1・経過日数/次の一手/担当/温度感/活動を条件付きで一望） */
  +jbSection(c)
  +tlSection(c)
  +'<div class="md">'+bodyMd+'</div>';
 bindTl();
 afterOpen();
}
function renderClientBody(m){return md(m).replace(/<h3>/g,'<div class="fbcard"><h3 style="border:none">').replace(/(<\/h3>[\s\S]*?)(?=<div class="fbcard">|$)/g,'$1</div>');}
function openDoc(stem){const d=dByStem[stem];if(!d)return;showDoc();pushHist("d",stem);
 lastNote={key:"d:"+stem,title:d.title,icon:"filetext",folder:"docs"};
 const dtg=docTags(d);
 $("#inner").innerHTML='<div class="inline-title">'+esc(d.title)+'</div>'
  +(dtg.length?'<div class="note-tags">'+dtg.map(t=>chipHtml(t)).join("")+'</div>':'')
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
 return '<div class="bl" tabindex="0" role="button" data-k="'+meta.k+'" data-s="'+esc(meta.stem)+'"><div class="blt">'+ic(meta.icon)+esc(meta.title)+'</div>'+c+'</div>';}
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
 if(!n||!n.key||n.key==="table"||n.key==="graph"){b.innerHTML='<div class="qhelp">ノートを開くと、ここに<b>バックリンク</b>・アウトゴーイングリンク・タグ・アウトラインが表示されます。<br><br>上部タブで切替できます。</div>';return;}
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
  b.innerHTML='<div class="bkgrp"><div class="bkgrp-h">タグ · '+tags.length+'</div>'+(tags.length?'<div class="rtagwrap">'+tags.map(t=>chipHtml(t)).join("")+'</div>':'<div class="qhelp">タグなし</div>')+'</div>';
  b.querySelectorAll(".tagchip").forEach(el=>el.onclick=()=>tagJump(el.dataset.tag));
 }else{renderOutlineInto(b);}
}
function tagJump(t){runQuery(tagQ(t));}

/* ===== Bases風テーブル ===== */
/* 最終接点 = max(最終FB日, 関連docsの最新modified)。cnorm単位で事前集計 */
const LASTDOC={};
DATA.docs.forEach(d=>{if(d.cnorm&&d.modified&&(!(d.cnorm in LASTDOC)||d.modified>LASTDOC[d.cnorm]))LASTDOC[d.cnorm]=d.modified;});
function lastOf(c){const a=c.lastfb||"",b=(c.cnorm&&LASTDOC[c.cnorm])||"";return a>b?a:b;}
let tblSort={key:"act",dir:-1},tblFilter={q:"",ind:"",phase:"",temp:"",hw:false};   /* モジュール変数=テーブル⇄カルテ往復で選択状態維持 */
/* 列順: 実務列（フェーズ/最終接点/担当/次アクション）を初期表示域へ。各エントリ定義は不変・順序のみ */
const TCOLS=[["name","取引先",c=>c.name,0],["phase","フェーズ",c=>c.phase,0],["last","最終接点",c=>lastOf(c),0],["tans","担当",c=>(c.tans||[]).join("・"),0],["nx","次アクション",c=>c.nx||"",0],["industry","業界",c=>c.industry,0],["bant","BANT",c=>c.bant,0],["fb","FB",c=>c.fb,1],["doc","資料",c=>c.doc,1],["act","活動",c=>c.fb+c.doc,1]];
/* 実Vaultの deal_phase/FB ph 実語彙（ケイパ/ヒアリング/1回目提案/2回目以降提案/最終交渉/成約/失注）。
   「その他」等の未知値は辞書に載せず fallback グレー（テーブル #8a8a8a / グラフ #9a9a9a） */
const PHASECOLOR={"ケイパ":"#e0b34c","ヒアリング":"#c98bdb","1回目提案":"#4f9df5","2回目以降提案":"#8a7cf5","最終交渉":"#d0912f","成約（口頭内示以上）":"#54b981","失注":"#e0685f"};
/* 商談ジャーニーの順序定義はこの1定数のみ。文字列は PHASECOLOR のキーと完全一致（回帰テストで担保）。
   実データの時系列遷移はケイパ→ヒアリング方向（ユーザー確認済み）のためケイパが先頭。失注は順序外の終端差し替え */
const PHASESTEPS=["ケイパ","ヒアリング","1回目提案","2回目以降提案","最終交渉","成約（口頭内示以上）"];
const PHASESHORT={"1回目提案":"提案①","2回目以降提案":"提案②","成約（口頭内示以上）":"成約"};   /* 短縮表示（title に正式名） */
function phShort(p){return PHASESHORT[p]||p;}
function tableView(){showDoc();$("#docPane").scrollTop=0;pushHist("table","");lastNote={key:"table",title:"取引先テーブル",icon:"table",folder:""};ribbonActive("table");   /* 履歴に積む=カルテから「戻る」でテーブル復帰（朝のtriageループ） */
 renderTableShell();renderTabs();renderVhead();
 $("#rightBody").innerHTML='<div class="qhelp">Obsidian <b>Bases</b> 風テーブル。列見出しでソート、上部で業界/フェーズ絞り込み、行クリックでカルテ。</div>';}
function tblRows(){let rows=DATA.clients.slice();
 if(tblFilter.q){const q=tblFilter.q.toLowerCase();rows=rows.filter(c=>(c.name+c.industry+c.phase+c.bant).toLowerCase().includes(q));}
 if(tblFilter.ind)rows=rows.filter(c=>c.industry===tblFilter.ind);
 if(tblFilter.phase)rows=rows.filter(c=>c.phase===tblFilter.phase);
 if(tblFilter.temp)rows=rows.filter(c=>c.temp===tblFilter.temp);
 if(tblFilter.hw)rows=rows.filter(c=>c.hw);
 const col=TCOLS.find(c=>c[0]===tblSort.key)||TCOLS[6];
 rows.sort((a,b)=>{let x=col[2](a),y=col[2](b);return col[3]?(x-y)*tblSort.dir:(""+x).localeCompare(""+y,"ja")*tblSort.dir;});return rows;}
function tblBody(shown){return shown.map(c=>{const pc=PHASECOLOR[c.phase]||"#8a8a8a";
 return '<tr data-s="'+esc(c.stem)+'"><td>'+esc(c.name)+'</td>'
  +'<td>'+(c.phase?'<span class="bp" style="background:'+pc+'22">'+esc(c.phase)+'</span>':'<span style="color:var(--faint)">—</span>')+'</td>'
  +'<td>'+ageCell(lastOf(c))+'</td>'
  +'<td>'+((c.tans&&c.tans.length)?esc(c.tans.join("・")):'<span style="color:var(--faint)">—</span>')+'</td>'
  +'<td>'+(c.nx?esc(c.nx):'<span style="color:var(--faint)">—</span>')+'</td>'
  +'<td>'+(c.industry?'<span class="dotc" style="background:'+colorOf(c.industry)+'"></span>'+esc(c.industry):'<span style="color:var(--faint)">—</span>')+'</td>'
  +'<td>'+esc(c.bant||"—")+'</td><td class="num">'+c.fb+'</td><td class="num">'+c.doc+'</td><td class="num">'+(c.fb+c.doc)+'</td></tr>';}).join("");}
function updateTable(){const rows=tblRows();const tb=$("#inner").querySelector("tbody");if(!tb)return;
 tb.innerHTML=rows.length?tblBody(rows.slice(0,500)):'<tr><td colspan="'+TCOLS.length+'" style="color:var(--faint);cursor:default">条件に一致する取引先がありません — フィルタを1つ外してください</td></tr>';
 const act=$("#inner").querySelector("#tblact");   /* native select にドットが付かない補完: 適用中フィルタをチップで可視化 */
 if(act)act.innerHTML=(tblFilter.temp?chipHtml("温度感/"+tblFilter.temp):"")+(tblFilter.hw?chipHtml("宿題/あり"):"");
 $("#inner").querySelector(".bar .n").textContent=rows.length+" 件"+(rows.length>500?"（先頭500表示）":"");
 $("#inner").querySelectorAll("thead th").forEach(th=>{const a=th.querySelector(".ar");if(a)a.remove();th.removeAttribute("aria-sort");
  if(th.dataset.k===tblSort.key){th.insertAdjacentHTML("beforeend",'<span class="ar">'+(tblSort.dir>0?"▲":"▼")+'</span>');th.setAttribute("aria-sort",tblSort.dir>0?"ascending":"descending");}});
 tb.querySelectorAll("tr[data-s]").forEach(tr=>tr.onclick=()=>openClient(tr.dataset.s));}
function renderTableShell(){
 const inds=[...new Set(DATA.clients.map(c=>c.industry).filter(Boolean))].sort();
 const phases=[...new Set(DATA.clients.map(c=>c.phase).filter(Boolean))].sort();
 const optI='<option value="">業界（すべて）</option>'+inds.map(i=>'<option'+(tblFilter.ind===i?" selected":"")+'>'+esc(i)+'</option>').join("");
 const optP='<option value="">フェーズ（すべて）</option>'+phases.map(p=>'<option'+(tblFilter.phase===p?" selected":"")+'>'+esc(p)+'</option>').join("");
 /* 温度感は実値集合から生成（ラベルはタグペインと同一文字列）。宿題はチェック1つの最小構成 */
 const temps=["高","ポジ優勢","拮抗","ネガ優勢"].filter(t=>DATA.clients.some(c=>c.temp===t));
 const optT='<option value="">温度感（すべて）</option>'+temps.map(t=>'<option'+(tblFilter.temp===t?" selected":"")+'>'+esc(t)+'</option>').join("");
 const head=TCOLS.map(c=>'<th tabindex="0" role="button" data-k="'+c[0]+'"'+(c[3]?' class="num"':'')+'>'+c[1]+'</th>').join("");
 $("#inner").innerHTML='<div class="tbv"><div class="th1">'+ic("table")+'取引先テーブル</div>'
  +'<p class="sub">'+DATA.stats.clients+' 取引先を業界・フェーズ・BANT・FB数で分類。列見出しでソート、行クリックでカルテを開く（Bases風）。</p>'
  +'<div class="bar"><input id="tblq" placeholder="絞り込み…" value="'+esc(tblFilter.q)+'"><select id="tblind">'+optI+'</select><select id="tblph">'+optP+'</select><select id="tbltemp">'+optT+'</select><label class="hwck"><input type="checkbox" id="tblhw"'+(tblFilter.hw?" checked":"")+'> 宿題ありのみ</label><span class="tfl" id="tblact"></span><span class="n"></span></div>'
  +'<div class="tblwrap"><table><thead><tr>'+head+'</tr></thead><tbody></tbody></table></div></div>';
 $("#tblq").addEventListener("input",e=>{tblFilter.q=e.target.value;updateTable();});
 $("#tblind").addEventListener("change",e=>{tblFilter.ind=e.target.value;updateTable();});
 $("#tblph").addEventListener("change",e=>{tblFilter.phase=e.target.value;updateTable();});
 $("#tbltemp").addEventListener("change",e=>{tblFilter.temp=e.target.value;updateTable();});
 $("#tblhw").addEventListener("change",e=>{tblFilter.hw=e.target.checked;updateTable();});
 $("#inner").querySelectorAll("thead th").forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(tblSort.key===k)tblSort.dir*=-1;else{tblSort.key=k;tblSort.dir=(k==="fb"||k==="doc"||k==="act"||k==="last")?-1:1;}updateTable();});
 updateTable();}

/* ===== グラフ ===== */
let G=null;
function openGraph(){showGraphView();lastNote={key:"graph",title:"グラフビュー",icon:"graph",folder:""};
 renderTabs();renderVhead();updateStatus(0,0);renderRight();   /* タブ/パンくず/ステータスバー/右パネルを疑似ビューへ同期 */
 if(!G){G="init";setTimeout(initGraph,30);}}
function initGraph(){
 const cv=$("#graph"),ctx=cv.getContext("2d"),tip=$("#gtip");const dpr=window.devicePixelRatio||1;
 function resize(){cv.width=cv.clientWidth*dpr;cv.height=cv.clientHeight*dpr;}
 resize();window.addEventListener("resize",resize);
 const N=DATA.graph.nodes.map((n,i)=>{const a=i*2.399963,rad=Math.sqrt(i)*16;return {...n,x:Math.cos(a)*rad+.01,y:Math.sin(a)*rad,vx:0,vy:0};});
 const L=DATA.graph.links.map(p=>({s:p[0],t:p[1]}));
 N.forEach(n=>n.r=n.type==="doc"?2.2:Math.min(3+Math.sqrt(n.deg)*1.5,n.type==="tag"?20:14));
 const neigh={};L.forEach(l=>{(neigh[l.s]=neigh[l.s]||new Set()).add(l.t);(neigh[l.t]=neigh[l.t]||new Set()).add(l.s);});
 const opt={nodeSize:1,linkW:1,textFade:1,repel:46,center:.011,linkDist:26,showDocs:true,showTags:true,hideOrphan:false,groupBy:"industry",cluster:null,filter:""};
 function vis(i){const n=N[i];if(!opt.showDocs&&n.type==="doc")return false;if(!opt.showTags&&n.type==="tag")return false;if(opt.hideOrphan&&!neigh[i])return false;if(opt.filter&&!n.label.toLowerCase().includes(opt.filter))return false;return true;}
 /* ===== まとめる軸（clustering axis）: 色分け(ncol)は不変・配置のみ島化。既定 opt.cluster=null＝取引先ごと（現状の単一原点重力） ===== */
 let cCenters=Object.create(null);                      // 値→{x,y,n} のリング中心。可視集合から決定的に算出（プロトタイプ無し＝"constructor"等の値でも島が消えない）
 const AGEBK=["1ヶ月以内","3ヶ月以内","半年以内","1年以内","1年以上前"];   // 最終接点の新→旧順（ageBucket の返す値と一致）
 const CLBASE=30;                                        // リング半径係数 R=CLBASE*√(全ノード数)（≒自然スプレッドの約2倍で島を分離。切替時に再フィットで全島を画角内へ）
 const CLMAX=12;                                         // 自由記述軸(資料種類/施策)の島数上限。無制限だと表記ゆれで島とラベルが重なり判読不能になる（凡例の slice(0,9) と同趣旨）
 const COTHER="その他";                                  // CLMAX で溢れた値の受け皿島（無言で消さず件数もラベルも出す）
 let clOther=new Set();                                  // 「その他」島へ寄せる値の集合（buildCenters で確定）
 function grpVal(n,ax){   // 軸ごとの所属グループ。対象type以外/空はnull=グループ中心に引かず既存リンクばねに委ねる（空値は未設定/記録なし島へ）
  // last/doc_type/solution はノードに埋め込まず、既存の索引 cByStem/dByStem（DATA.clients/docs）から stem で引く（ペイロード増ゼロ）。phase はノードの既存フィールド。
  if(ax==="phase")   return n.type==="client"?(n.phase||"未設定"):null;
  if(ax==="last")    return n.type==="client"?(ageBucket((cByStem[n.id.slice(2)]||{}).last)||"記録なし"):null;
  if(ax==="doc_type")return n.type==="doc"?((dByStem[n.id.slice(2)]||{}).doc_type||"未設定"):null;
  if(ax==="solution")return n.type==="doc"?((dByStem[n.id.slice(2)]||{}).solution||"未設定"):null;
  return null;}
 function grp(n){return opt.cluster?grpVal(n,opt.cluster):null;}
 function buildCenters(ax){   // 選択軸の実在値のみ列挙（フェーズ=PHASECOLORキー順/最終接点=新→旧/他=件数降順・未設定は末尾）→リング状に決定的配置
  /* 幾何（島の順序・角度・半径）は **全ノード** から軸切替時に1回だけ決める（＝絞り込みしても
     島は動かない）。可視集合に連動させると 1 キーストロークごとに島が数百px移動して体験が壊れる。
     件数は recount() が可視基準で入れる。cnt/cCenters はプロトタイプ無しの辞書にする
     （"constructor" 等の値で cnt[g]==null が false になり島が壊れるのを防ぐ）。 */
  const cnt=Object.create(null),seen=[];clOther=new Set();   // 軸ごとに「その他」集約をリセット
  for(let i=0;i<N.length;i++){const g=grpVal(N[i],ax);if(g===null)continue;if(cnt[g]==null){cnt[g]=0;seen.push(g);}cnt[g]++;}
  let vals;
  if(ax==="phase"){vals=Object.keys(PHASECOLOR).filter(p=>cnt[p]!=null);seen.forEach(g=>{if(g!=="未設定"&&vals.indexOf(g)<0)vals.push(g);});}
  else if(ax==="last"){vals=AGEBK.filter(p=>cnt[p]!=null);}
  else{  /* 自由記述軸(資料種類/施策)は件数降順の上位 CLMAX-1 種＋あふれを「その他」島へ集約。
            単純に slice で切ると、切られた値のノードは cCenters に無く grp()→gc=null で原点
            （未所属と同じ場所）へ落ち、ラベルも件数も出ない＝**無言で消える**。実データの
            solution は canonical がちょうど12種＋自由記述素通しで確実に溢れる。 */
   const rest=seen.filter(g=>g!=="未設定").sort((a,b)=>cnt[b]-cnt[a]);
   vals=rest.slice(0,CLMAX-1);
   const over=rest.slice(CLMAX-1);
   if(over.length){clOther=new Set(over);cnt[COTHER]=over.reduce((s,g)=>s+cnt[g],0);vals.push(COTHER);}
  }
  ["未設定","記録なし"].forEach(u=>{if(cnt[u]!=null)vals.push(u);});   // 未設定/記録なし島は必ず末尾
  cCenters=Object.create(null);
  const tot=vals.length||1,R=CLBASE*Math.sqrt(N.length);
  vals.forEach((v,i)=>{const a=i/tot*Math.PI*2;cCenters[v]={x:Math.cos(a)*R,y:Math.sin(a)*R,n:0};});
  recount();}
 function grpBin(n){  /* 島の割当先。CLMAX で溢れた値は「その他」島へ寄せる（無言で消さない） */
  const g=grp(n);return (g!==null&&clOther.has(g))?COTHER:g;}
 function recount(){   // 件数だけ可視基準で更新（幾何は不変＝島は動かない・リヒート不要）
  for(const v in cCenters)cCenters[v].n=0;
  for(let i=0;i<N.length;i++){if(!vis(i))continue;const g=grpBin(N[i]);if(g!==null&&cCenters[g])cCenters[g].n++;}}
 function clusterCap(){   // cluster有効時のみ: 軸名＋未設定件数を明示。doc基準は体験変化（取引先が資料に引かれ周辺配置）を併記
  if(!opt.cluster)return"";
  const LB={phase:"フェーズ",last:"最終接点",doc_type:"資料の種類",solution:"施策"},uk=opt.cluster==="last"?"記録なし":"未設定";
  const un=cCenters[uk]?cCenters[uk].n:0;
  let tot=0;for(const v in cCenters)tot+=cCenters[v].n;
  if(!tot)return esc(LB[opt.cluster])+"でまとめています（表示中の対象がありません）";   /* 全島が空（対象typeを非表示にした等）で「まとめています」だけ出すと嘘になる */
  let s=esc(LB[opt.cluster])+"でまとめています";
  if(un)s+="（"+uk+" "+un+"件は"+uk+"島）";
  if(cCenters[COTHER])s+="<br>種類が多いため上位"+(CLMAX-1)+"件のみ島にし、残り"+clOther.size+"種は「"+COTHER+"」島にまとめています";
  if(opt.cluster==="doc_type"||opt.cluster==="solution")s+="<br>資料でまとめています（取引先は資料に引かれ周辺に配置）";
  return s;}
 function ncol(n){if(n.type==="tag")return "hsl(254,42%,72%)";if(n.type==="doc")return "rgba(150,150,156,.6)";return opt.groupBy==="phase"?(PHASECOLOR[n.phase]||"#9a9a9a"):colorOf(n.industry);}
 let view={x:0,y:0,z:1},hover=-1,drag=-1,pan=false,px=0,py=0,alpha=1,fitPending=false;const aMin=.02,aDec=.977;   // fitPending: まとめ軸切替後に収束したら一度だけ再フィット（全島を画角に収める）
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
  for(let i=0;i<N.length;i++){if(!vis(i))continue;const n=N[i];
   const gk=grpBin(n),gc=gk!==null?cCenters[gk]:null;   // まとめ軸ON かつ所属あり: 所属グループ中心への引力に差し替え（未所属=従来の弱い原点重力）。CLMAX 溢れは「その他」島へ
   if(gc){n.vx-=(n.x-gc.x)*opt.center*a;n.vy-=(n.y-gc.y)*opt.center*a;}
   else{n.vx-=n.x*opt.center*a;n.vy-=n.y*opt.center*a;}
   n.vx*=.82;n.vy*=.82;
   const sp=n.vx*n.vx+n.vy*n.vy;if(sp>2500){const s=50/Math.sqrt(sp);n.vx*=s;n.vy*=s;}
   n.x+=n.vx;n.y+=n.vy;
   if(!isFinite(n.x)||!isFinite(n.y)){n.x=(i%60-30)*8;n.y=(((i/60)|0)%60-30)*8;n.vx=n.vy=0;}}}
 for(let k=0;k<160;k++)step(1);
 /* フィットは **可視ノードのみ** の外接矩形に合わせる（非表示ノードの座標まで含めると、絞り込み後に
    画角が無関係な余白へ引っ張られる）。可視0件なら現在の画角を維持（描画側が空状態を出す）。 */
 function fit(){let a=1e9,b=1e9,c=-1e9,d=-1e9,any=false;N.forEach((n,i)=>{if(!vis(i))return;any=true;a=Math.min(a,n.x);b=Math.min(b,n.y);c=Math.max(c,n.x);d=Math.max(d,n.y);});if(!any)return false;const w=cv.clientWidth,h=cv.clientHeight;view.z=Math.max(.25,Math.min(2.2,.82*Math.min(w/((c-a)||1),h/((d-b)||1))));view.x=-(a+c)/2;view.y=-(b+d)/2;return true;}
 fit();
 const S=n=>({x:cv.width/2+(n.x+view.x)*view.z*dpr,y:cv.height/2+(n.y+view.y)*view.z*dpr});
 function draw(){const DK=document.documentElement.getAttribute("data-theme")==="dark";ctx.clearRect(0,0,cv.width,cv.height);ctx.lineWidth=dpr*opt.linkW;
  const lNorm=DK?"rgba(255,255,255,.06)":"rgba(0,0,0,.085)",lHov=DK?"hsla(254,74%,74%,.6)":"hsla(254,60%,52%,.7)",lbl=DK?"#c8c9cf":"#33343a";
  if(!N.some((n,i)=>vis(i))){ctx.fillStyle=lbl;ctx.font=(13*dpr)+"px InterVar,Inter,-apple-system,sans-serif";ctx.textAlign="center";ctx.fillText("一致するノードがありません",cv.width/2,cv.height/2);ctx.textAlign="left";return;}   /* 可視0件の空状態（白紙化防止） */
  L.forEach(l=>{if(!vis(l.s)||!vis(l.t))return;const A=S(N[l.s]),B=S(N[l.t]);const on=hover>=0&&(l.s===hover||l.t===hover);ctx.strokeStyle=on?lHov:lNorm;ctx.beginPath();ctx.moveTo(A.x,A.y);ctx.lineTo(B.x,B.y);ctx.stroke();});
  N.forEach((n,i)=>{if(!vis(i))return;const p=S(n);const dim=hover>=0&&i!==hover&&!(neigh[hover]&&neigh[hover].has(i));ctx.globalAlpha=dim?.2:1;
   const rr=Math.max(n.r*opt.nodeSize*view.z*dpr,.5*dpr);ctx.fillStyle=ncol(n);ctx.beginPath();ctx.arc(p.x,p.y,rr,0,7);ctx.fill();  /* 遠景でも点として残す下限 */
   if((n.type==="client"&&rr>6.5/opt.textFade)||i===hover){ctx.globalAlpha=dim?.3:1;ctx.fillStyle=lbl;ctx.font=(11*dpr)+"px InterVar,Inter,-apple-system,sans-serif";ctx.fillText(n.label,p.x+rr+4,p.y+4*dpr);}});ctx.globalAlpha=1;
  if(opt.cluster){ctx.textAlign="center";ctx.font="600 "+(12*dpr)+"px InterVar,Inter,-apple-system,sans-serif";ctx.lineWidth=3*dpr;   /* まとめ軸: 各島の中心にラベル＋件数（未設定/記録なし島も明示）。ハロー付きでノード上でも可読 */
   for(const v in cCenters){const c=cCenters[v];if(!c.n)continue;const p=S(c),tx=v+" ("+c.n+")";ctx.strokeStyle=DK?"rgba(18,18,22,.85)":"rgba(255,255,255,.92)";ctx.fillStyle=DK?"rgba(232,232,238,.92)":"rgba(28,28,34,.9)";ctx.strokeText(tx,p.x,p.y-6*dpr);ctx.fillText(tx,p.x,p.y-6*dpr);}   /* 可視0件の島はラベルを出さない（絞り込みで空になった島の "(0)" を残さない） */
   ctx.textAlign="left";ctx.lineWidth=dpr*opt.linkW;}
 }
 function pick(mx,my){let best=-1,bd=15*dpr;N.forEach((n,i)=>{if(!vis(i))return;const p=S(n);const dd=Math.hypot(p.x-mx,p.y-my);if(dd<Math.max(n.r*opt.nodeSize*view.z*dpr+5*dpr,15*dpr)&&dd<bd){bd=dd;best=i;}});return best;}
 let run=false;function ensure(){if(!run){run=true;requestAnimationFrame(loop);}}
 function loop(){if(alpha>aMin){step(alpha);alpha*=aDec;draw();requestAnimationFrame(loop);}else if(drag>=0){step(.25);draw();requestAnimationFrame(loop);}else{if(fitPending&&fit())fitPending=false;draw();run=false;}}   /* 再フィットは成功時のみ消費（可視0件で空振りすると軸切替の再フィットが永久に来なくなる） */
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
 function legend(){let base;
  if(opt.groupBy==="industry")base=inds.map(i=>'<div class="grow"><span class="dot" style="background:'+colorOf(i)+'"></span>'+esc(i)+'</div>').join("");
  else{const ph=[...new Set(N.filter(n=>n.type==="client"&&n.phase).map(n=>n.phase))];  /* 実データに存在するフェーズのみ動的列挙 */
   base=ph.filter(p=>PHASECOLOR[p]).map(p=>'<div class="grow"><span class="dot" style="background:'+PHASECOLOR[p]+'"></span>'+esc(p)+'</div>').join("");
   if(ph.some(p=>!PHASECOLOR[p])||N.some(n=>n.type==="client"&&!n.phase))base+='<div class="grow"><span class="dot" style="background:#9a9a9a"></span>その他</div>';}   /* 未知/未設定はグレー1行に集約 */
  return base+'<div class="grow"><span class="dot" style="background:hsl(254,42%,72%)"></span>タグ</div><div class="grow"><span class="dot" style="background:rgba(150,150,156,.7)"></span>資料</div>';}
 $("#gpanel").innerHTML=
  '<div class="gph"><span class="gpt">グラフ設定</span><span class="gpx" id="gpClose">'+ic("x")+'</span></div>'
  +'<div class="gsec"><div class="gh">フィルター</div><input class="gin" id="gFilter" placeholder="ノードを検索…">'
  +'<label class="gck"><input type="checkbox" id="gTags" checked> タグを表示</label>'
  +'<label class="gck"><input type="checkbox" id="gDocs" checked> 資料ノードを表示</label>'
  +'<label class="gck"><input type="checkbox" id="gOrph"> 孤立ノードを隠す</label></div>'
  +'<div class="gsec"><div class="gh">グループ（色分け）</div>'
  +'<label class="gck"><input type="radio" name="gb" id="gbInd" checked> 業種で色分け</label>'
  +'<label class="gck"><input type="radio" name="gb" id="gbPh"> フェーズで色分け</label>'
  +'<div class="gleg" id="gLeg">'+legend()+'</div></div>'
  +'<div class="gsec"><div class="gh" id="gClusterLbl">まとめる（配置）</div>'
  /* a11y: 見出しは div なので暗黙のラベル付けが効かない。aria-labelledby で見出しと結び、
     aria-describedby で「何が起きるか」の補足文を読み上げに載せる（スクリーンリーダで
     "まとめる（配置） コンボボックス" と読める）。 */
  +'<select class="gin" id="gCluster" aria-labelledby="gClusterLbl" aria-describedby="gClusterCap">'
   +'<option value="">取引先ごと（既定）</option>'
   +'<option value="phase">フェーズ（取引先を島に）</option>'
   +'<option value="last">最終接点（取引先を島に）</option>'
   +'<option value="doc_type">資料の種類（資料を島に）</option>'
   +'<option value="solution">施策（資料を島に）</option>'
  +'</select>'
  +'<div style="color:var(--faint);font-size:10px;line-height:1.45">色分け＝何者か / まとめ＝どこに溜まるか</div>'
  +'<div id="gClusterCap" style="color:var(--muted);font-size:10px;line-height:1.45;margin-top:5px"></div></div>'
  +'<div class="gsec"><div class="gh">表示</div>'
  +'<div class="gsl"><span>ノード径</span><input type="range" id="gNs" min="0.5" max="2.5" step="0.1" value="1"></div>'
  +'<div class="gsl"><span>リンク太さ</span><input type="range" id="gLw" min="0.5" max="3" step="0.1" value="1"></div>'
  +'<div class="gsl"><span>ラベル閾値</span><input type="range" id="gTf" min="0.4" max="2" step="0.1" value="1"></div></div>'
  +'<div class="gsec"><div class="gh">力の強さ</div>'
  +'<div class="gsl"><span>反発</span><input type="range" id="gRe" min="15" max="150" step="1" value="46"></div>'
  +'<div class="gsl"><span>中心力</span><input type="range" id="gCe" min="0" max="0.03" step="0.001" value="0.011"></div>'
  +'<div class="gsl"><span>リンク距離</span><input type="range" id="gLd" min="10" max="80" step="2" value="26"></div></div>';
 const dr=()=>draw(),rh=()=>{alpha=Math.max(alpha,.5);ensure();};
 /* 可視集合が変わったら **件数だけ** 数え直す（島の位置は動かさない＝リヒート不要）。
    これをしないと絞り込み後もラベルの件数が非表示分を含んだまま固まる。 */
 const visChanged=()=>{if(opt.cluster){recount();$("#gClusterCap").innerHTML=clusterCap();}dr();};
 $("#gFilter").oninput=e=>{opt.filter=e.target.value.toLowerCase();visChanged();};
 $("#gTags").onchange=e=>{opt.showTags=e.target.checked;visChanged();};
 $("#gDocs").onchange=e=>{opt.showDocs=e.target.checked;visChanged();};
 $("#gOrph").onchange=e=>{opt.hideOrphan=e.target.checked;visChanged();};
 $("#gbInd").onchange=()=>{opt.groupBy="industry";$("#gLeg").innerHTML=legend();dr();};
 $("#gbPh").onchange=()=>{opt.groupBy="phase";$("#gLeg").innerHTML=legend();dr();};
 $("#gCluster").onchange=e=>{opt.cluster=e.target.value||null;if(opt.cluster)buildCenters(opt.cluster);$("#gClusterCap").innerHTML=clusterCap();alpha=Math.max(alpha,.5);fitPending=true;ensure();};   /* まとめ軸切替: 既存リヒートに乗せ島がふわっと再収束（160step同期再計算は使わない）→収束後に一度だけ再フィット */
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
const CMDS=[["グラフビューを開く","graph",()=>openGraph()],["取引先テーブル(Bases)を開く","table",()=>tableView()],["ホームを開く","files",()=>{lastNote=null;showDoc();welcome();renderTabs();renderVhead();}],["検索を開く","search",()=>setPane("search")],["タグを開く","tags",()=>setPane("tags")],["ブックマークを開く","bookmark",()=>setPane("bookmark")],...(DATA.reports.length?[["レポート: フォロー漏れ洗い出し","report",()=>openReport("followup_gaps")],["レポート: クライアント名寄せ","report",()=>openReport("name_merge_candidates")],["レポート: テンプレ検出","report",()=>openReport("boilerplate_detected")]]:[])];
const QI=DATA.clients.map(c=>({k:"c",stem:c.stem,name:c.name,pt:c.industry||"取引先",ico:"building"})).concat(DATA.docs.map(d=>({k:"d",stem:d.stem,name:d.title,pt:d.client||"資料",ico:"filetext"}))).concat(DATA.reports.map(r=>({k:"r",stem:r.stem,name:r.name,pt:"レポート",ico:"report"})));
function qsOpen(mode){qMode=mode||"nav";$("#qsov").classList.add("on");const i=$("#qsin");i.value="";i.placeholder=qMode==="cmd"?"コマンドを実行…  (⌘P)":"ノートに移動…  (⌘O)";qsR("");i.focus();}
function qsClose(){$("#qsov").classList.remove("on");}
function qsR(q){q=q.toLowerCase().trim();
 if(qMode==="cmd"){qItems=(q?CMDS.filter(c=>c[0].toLowerCase().includes(q)):CMDS).slice(0,20);qSel=0;
  $("#qslist").innerHTML=qItems.map((x,i)=>'<div class="qi'+(i===0?" sel":"")+'" data-i="'+i+'">'+ic(x[1])+'<span class="nm">'+esc(x[0])+'</span></div>').join("")||'<div class="qi">一致なし</div>';
  $("#qslist").querySelectorAll(".qi[data-i]").forEach(el=>el.onclick=()=>qsP(+el.dataset.i));qsHoverBind();return;}
 qItems=(q?QI.filter(x=>x.name&&x.name.toLowerCase().includes(q)):QI.filter(x=>x.k==="c")).slice(0,20);qSel=0;
 $("#qslist").innerHTML=qItems.map((x,i)=>'<div class="qi'+(i===0?" sel":"")+'" data-i="'+i+'">'+ic(x.ico)+'<span class="nm">'+esc(x.name.slice(0,56))+'</span><span class="pt">'+esc(x.pt)+'</span></div>').join("")||'<div class="qi">一致なし</div>';
 $("#qslist").querySelectorAll(".qi[data-i]").forEach(el=>el.onclick=()=>qsP(+el.dataset.i));qsHoverBind();}
/* hover=選択=Enter対象を常に一致させる（マウスとカーソル選択のズレ解消） */
function qsHoverBind(){$("#qslist").querySelectorAll(".qi[data-i]").forEach(el=>el.onmousemove=()=>{const i=+el.dataset.i;if(qSel!==i){qSel=i;qsMove(0);}});}
function qsMove(d){qSel=Math.max(0,Math.min(qItems.length-1,qSel+d));$("#qslist").querySelectorAll(".qi").forEach((el,i)=>el.classList.toggle("sel",i===qSel));const s=$("#qslist").querySelector(".qi.sel");if(s)s.scrollIntoView({block:"nearest"});}
function qsP(i){const x=qItems[i];if(!x)return;qsClose();if(qMode==="cmd")x[2]();else openByK(x.k,x.stem);}
$("#qsin").addEventListener("input",e=>qsR(e.target.value));
$("#qsin").addEventListener("keydown",e=>{if(e.key==="ArrowDown"){e.preventDefault();qsMove(1);}else if(e.key==="ArrowUp"){e.preventDefault();qsMove(-1);}else if(e.key==="Enter"){e.preventDefault();qsP(qSel);}else if(e.key==="Escape")qsClose();});
$("#qsov").addEventListener("click",e=>{if(e.target.id==="qsov")qsClose();});
document.addEventListener("keydown",e=>{const k=e.key.toLowerCase();if((e.metaKey||e.ctrlKey)&&k==="o"){e.preventDefault();qsOpen("nav");}else if((e.metaKey||e.ctrlKey)&&k==="p"){e.preventDefault();qsOpen("cmd");}else if(e.key==="Escape")qsClose();});
/* キーボード操作: role="button" 要素は Enter/Space でクリック相当（委譲1本・:focus-visible リングは既存定義） */
document.addEventListener("keydown",e=>{if(e.key!=="Enter"&&e.key!==" ")return;if(e.metaKey||e.ctrlKey||e.altKey)return;
 const t=e.target instanceof Element?e.target.closest('[role="button"]'):null;if(t){e.preventDefault();t.click();}});
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
    """main冒頭で固定した同一bytes snapshotを読む（途中変更の新旧混在を作らない）。"""
    raw = SIDECAR_SNAPSHOTS.get(name)
    if raw is None:
        _die(f"必須サイドカー {name} のsnapshotがありません")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        _die(f"必須サイドカー {name} がUTF-8ではありません")


def _build_inputs_bundle_sha256(
    manifest_sha256: str,
    snapshots: dict[str, bytes | None],
) -> str:
    """manifest + 全sidecar/font bytesを決定論bundle SHAへ畳み込む。"""
    digest = hashlib.sha256(b"connect-web-build-inputs-v1\0")
    digest.update(b"manifest\0sha256:")
    digest.update(manifest_sha256.encode("ascii"))
    digest.update(b"\0")
    for name in sorted((*SIDECAR_FILES, *OPTIONAL_SIDECAR_FILES)):
        raw = snapshots.get(name)
        marker = "missing" if raw is None else f"sha256:{hashlib.sha256(raw).hexdigest()}"
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(marker.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _snapshot_sidecars() -> tuple[dict[str, bytes | None], str]:
    """required/optional sidecarを1回だけreadし、bundle SHAと共に返す。"""
    resolved_dir = SIDECAR_DIR.resolve()
    snapshots: dict[str, bytes | None] = {}
    for name in (*SIDECAR_FILES, *OPTIONAL_SIDECAR_FILES):
        path = SIDECAR_DIR / name
        required = name in SIDECAR_FILES
        if path.is_symlink():
            _die(f"サイドカー {name} はsymlink不可です")
        if not path.is_file():
            if required:
                _die(
                    f"サイドカー欠落: {name}（{SIDECAR_DIR}）。"
                    "git checkout で復元してください"
                )
            snapshots[name] = None
            continue
        try:
            if path.resolve().parent != resolved_dir:
                _die(f"サイドカー {name} のpathが安全ではありません")
            snapshots[name] = path.read_bytes()
        except OSError:
            _die(f"サイドカー {name} が読めません")
    return snapshots, _build_inputs_bundle_sha256(EXPORT_MANIFEST_SHA256, snapshots)


def _load_alias_sidecar(name: str, subkey: str | None = None) -> dict:
    """名寄せサイドカー（任意適用）を読む。_read_sidecar（必須・fail-loud）とは意図的に
    扱いを分ける: 名寄せは削除で元挙動へ戻る可逆設計なので、欠落/空/破損/型不一致は
    黙って空 dict＝素通り（fail-loud にしない）。subkey 指定時はその配下 dict を返す。"""
    raw = SIDECAR_SNAPSHOTS.get(name)
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if subkey is not None:
        sub = data.get(subkey, {})
        return sub if isinstance(sub, dict) else {}
    return data


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_path_key(path: str) -> str:
    """exporter と同じ NFC + casefold で manifest/実ファイルの別名を照合する。"""
    return unicodedata.normalize("NFC", path).casefold()


def _portable_title_overrides(value: object) -> dict[str, str]:
    """表示名sidecarをbuilderとQA共通のNFC+casefold stem lookupへ固定する。"""
    if not isinstance(value, dict):
        _die("サイドカー weird_rename_high.json のrootがobjectではありません")
    result: dict[str, str] = {}
    for stem, title in value.items():
        if (
            not isinstance(stem, str)
            or not stem.strip()
            or not isinstance(title, str)
            or not title.strip()
        ):
            _die("サイドカー weird_rename_high.json のkey/value形式が不正です")
        key = _portable_path_key(stem)
        if key in result:
            _die("サイドカー weird_rename_high.json にportable stem衝突があります")
        result[key] = title
    return result


def _load_active_export_paths(vault: Path) -> set[str]:
    """直近のACL付き完全exportで生成され、hash一致する公開対象のportable keyを返す。"""
    global EXPORT_MANIFEST_SHA256
    manifest = vault / _EXPORT_MANIFEST_NAME
    resolved_vault = vault.resolve()
    if manifest.is_symlink() or manifest.resolve().parent != resolved_vault or not manifest.is_file():
        _die(
            f"完全export manifestがありません: {manifest}。"
            "export_vault.py を --commit（完全export）で実行してください"
        )
    try:
        manifest_bytes = manifest.read_bytes()
        payload = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _die(f"export manifestが読めません: {manifest}: {exc}")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if not isinstance(payload, dict):
        _die("export manifestのrootがobjectではありません")
    if payload.get("version") != 1 or payload.get("generator") != _EXPORT_VAULT_GENERATOR:
        _die("export manifestのversion/generatorが不正です")
    if payload.get("complete_export") is not True:
        _die("直近manifestは完全exportではありません。--client/--limitなしで再exportしてください")
    files = payload.get("files")
    active_raw = payload.get("active_files")
    if not isinstance(files, dict) or not isinstance(active_raw, list):
        _die("export manifestのfiles/active_files形式が不正です")

    manifest_hashes: dict[str, str] = {}
    manifest_paths: dict[str, str] = {}
    for rel, expected_hash in files.items():
        if not isinstance(rel, str) or "\\" in rel:
            _die(f"export manifestのfiles pathが不正です: {rel!r}")
        parts = rel.split("/")
        if (
            len(parts) != 2
            or parts[0] not in {"clients", "docs"}
            or not parts[1]
            or parts[1] == ".md"
            or not parts[1].endswith(".md")
        ):
            _die(f"export manifestのfiles pathが管理範囲外です: {rel!r}")
        if not isinstance(expected_hash, str) or _SHA256_RE.fullmatch(expected_hash) is None:
            _die(f"export manifestのactive hashが不正です: {rel}")
        key = _portable_path_key(rel)
        if key in manifest_hashes:
            _die(
                "export manifestのfilesにUnicode正規化または大文字小文字の衝突があります: "
                f"{manifest_paths[key]!r} / {rel!r}"
            )
        manifest_hashes[key] = expected_hash
        manifest_paths[key] = rel

    if not active_raw:
        _die("export manifestのactive_filesが空または重複しています")
    active: set[str] = set()
    active_paths: dict[str, str] = {}
    for rel in active_raw:
        if not isinstance(rel, str) or "\\" in rel:
            _die(f"export manifestのactive pathが不正です: {rel!r}")
        parts = rel.split("/")
        if (
            len(parts) != 2
            or parts[0] not in {"clients", "docs"}
            or not parts[1]
            or parts[1] == ".md"
            or not parts[1].endswith(".md")
        ):
            _die(f"export manifestのactive pathが管理範囲外です: {rel!r}")
        key = _portable_path_key(rel)
        if key in active:
            _die(
                "export manifestのactive_filesにUnicode正規化または大文字小文字の衝突があります: "
                f"{active_paths[key]!r} / {rel!r}"
            )
        if key not in manifest_hashes:
            _die(f"export manifestのactive hashが不正です: {rel}")
        active.add(key)
        active_paths[key] = rel

    physical_paths: dict[str, Path] = {}
    physical_relpaths: dict[str, str] = {}
    for prefix in ("clients", "docs"):
        for target in (vault / prefix).glob("*.md"):
            rel = f"{prefix}/{target.name}"
            key = _portable_path_key(rel)
            if key in physical_paths:
                _die(
                    "Vault内のMarkdownにUnicode正規化または大文字小文字の衝突があります: "
                    f"{physical_relpaths[key]!r} / {rel!r}"
                )
            physical_paths[key] = target
            physical_relpaths[key] = rel

    for key in active:
        rel = active_paths[key]
        target = physical_paths.get(key)
        if target is None:
            _die(f"公開対象noteが無いか安全なregular fileではありません: {rel}")
        parts = rel.split("/")
        expected_parent = resolved_vault / parts[0]
        if (
            target.is_symlink()
            or not target.is_file()
            or target.resolve().parent != expected_parent
        ):
            _die(f"公開対象noteが無いか安全なregular fileではありません: {rel}")
        if _file_sha256(target) != manifest_hashes[key]:
            _die(f"公開対象noteがexport後に変更されています: {rel}。完全exportを再実行してください")
    EXPORT_MANIFEST_SHA256 = manifest_sha256
    return active


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
    parser.add_argument(
        "--include-reports",
        action="store_true",
        help="廃止済み。_reports はACL manifest外のため公開HTMLへ搭載不可",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global VAULT, CLIENTS, DOCS, REPORTS, EXCL, EXCL_N, EXCL_SOURCE_KEYS
    global SOURCE_EXCLUDED_STEMS, DOC_DROP, TITLE_OVERRIDE, CHUNK_DROP
    global TAG_ALIAS, CLIENT_ALIAS, ACTIVE_MANAGED_PATHS, EXPORT_MANIFEST_SHA256
    global BUILD_INPUTS_SHA256, SIDECAR_SNAPSHOTS

    args = _parse_args(argv)
    if args.include_reports:
        _die("--include-reports はACL manifest外の資料を公開し得るため廃止しました")
    VAULT = Path(args.vault).expanduser()
    CLIENTS, DOCS, REPORTS = VAULT / "clients", VAULT / "docs", VAULT / "_reports"
    out = Path(args.out).expanduser()

    # --- fail-loud: Vault 不在 ---
    if not VAULT.is_dir():
        _die(f"Vault が見つかりません: {VAULT}。scripts/export_vault.py --commit 済みか確認してください")
    for sub in (CLIENTS, DOCS):
        if not sub.is_dir():
            _die(f"Vault 構造が不正です: {sub} がありません（export_vault.py の出力形式を確認）")

    # Vaultに残す手作業noteやprune保護noteを、ACL済み公開集合と混同しない。
    # hash不一致もfail-loudにして、export後のローカル編集を静的/appへ流さない。
    ACTIVE_MANAGED_PATHS = _load_active_export_paths(VAULT)

    # manifestと同様、全sidecar/fontを同一bytes snapshotへ固定してHTML/statsへbindする。
    SIDECAR_SNAPSHOTS, BUILD_INPUTS_SHA256 = _snapshot_sidecars()

    # --- サイドカー読み込み（元スクリプトの exists() フォールバックを全廃） ---
    EXCL = set(json.loads(_read_sidecar("exclude_stems.json")))
    EXCL_N = {_exn(s) for s in EXCL}
    EXCL_SOURCE_KEYS = set(json.loads(_read_sidecar("exclude_source_keys.json")))
    # client_md の関連資料リンク除去と chunk 折り畳みにも同じ判定を効かせるため、
    # client/doc の payload 構築前に source key 一致 stem を確定する。
    SOURCE_EXCLUDED_STEMS = _source_excluded_stems()
    DOC_DROP = set((json.loads(_read_sidecar("dedup_drop_map.json")) or {}).get("drop", {}).keys())
    TITLE_OVERRIDE = _portable_title_overrides(json.loads(_read_sidecar("weird_rename_high.json")))
    font_b64 = "".join(_read_sidecar("inter-var.b64").split())
    if not font_b64:
        _die(f"サイドカー inter-var.b64 が空です: {SIDECAR_DIR / 'inter-var.b64'}")
    CHUNK_DROP = _compute_chunk_drop()

    # --- 表示名寄せサイドカー（任意適用・可逆）: 欠落は空 dict＝素通り（必須サイドカーと扱いを分ける） ---
    TAG_ALIAS = _load_alias_sidecar("tag_alias.json")
    CLIENT_ALIAS = _load_alias_sidecar("client_alias.json", "client")

    # --- 以下、元スクリプト L149-332 のパイプラインをそのまま実行（ロジック不変） ---
    clients = []
    for f in sorted(CLIENTS.glob("*.md")):
        if _portable_path_key(f"clients/{f.name}") not in ACTIVE_MANAGED_PATHS:
            continue
        if _is_self_org(f.stem) or f.stem in JUNK_CLIENTS:   # 自社(NewsTV)/テスト・ダミーカルテは取引先化しない
            continue
        t = f.read_text(errors="replace")
        fm = front(t)
        if _is_self_org(fm.get("client") or ""):
            continue
        _raw_cname = fm.get("client") or f.stem
        _cname = _canon_client(_raw_cname)  # 取引先名寄せ: 正本化してから norm()/dedup へ
        _client_body = body_of(t)
        _tl_raw = _parse_fb_events_raw(_client_body)
        clients.append({
            "stem": f.stem, "name": _cname, "cnorm": norm(_cname),
            "industry": _canon_industry(fm.get("industry", "")), "phase": fm.get("deal_phase", ""),
            "bant": fm.get("bant_score", ""), "bantg": bant_short(fm.get("bant_score", "")),
            "fb": to_int(fm.get("fb_count", "0")), "doc": to_int(fm.get("doc_count", "0")),
            "md": client_md(t), "tl": _sort_fb_events(dedup_fb_events(list(_tl_raw))),
            "_raw_name": _raw_cname, "_tl_raw": _tl_raw, "_wl": parse_links(_client_body),
        })

    # 取引先名寄せ(Tier1): 正規化が一致する表記ゆれ(法人格/敬称/空白/中黒)を統合。
    # 既存bookmark用stemは従来代表のまま保ち、表示property/リンク/FBだけを合流する（元Vault不変）。
    _cgroups = defaultdict(list)
    for _c in clients:
        _cgroups[norm(_c["name"])].append(_c)
    clients = []
    _explicit_canonical_names = {
        unicodedata.normalize("NFC", value) for value in CLIENT_ALIAS.values()
    }
    for _grp in _cgroups.values():
        # stem/idは従来ロジックを固定し、localStorage bookmarkの c:<stem> を壊さない。
        _canon = min(_grp, key=lambda c: (len(c["name"]), c["name"]))
        # explicit aliasのcanonical target noteが実在する場合、stemは変えずに
        # md/industry/phase/BANTのみ正式noteをfallback sourceにする。
        _property_source = next(
            (
                c
                for c in _grp
                if unicodedata.normalize("NFC", c["name"]) in _explicit_canonical_names
                and unicodedata.normalize("NFC", c["_raw_name"])
                == unicodedata.normalize("NFC", c["name"])
            ),
            _canon,
        )
        if _property_source is not _canon:
            for _field in ("md", "industry", "phase", "bant", "bantg"):
                _canon[_field] = _property_source[_field]
        _raw_fb_count = sum(c["fb"] for c in _grp)
        _member_raw_tl = [c.pop("_tl_raw", []) for c in _grp]
        _flat_tl = [event for events in _member_raw_tl for event in events]
        _unique_tl = dedup_fb_events(_flat_tl)
        # 同一note内のSlack+Sheets二重登録は元fb_countの尺度として維持し、variantを跨ぐ
        # コピーだけ減算する。key別に「全member合計 - 1 member内の最大多重度」を取ると、
        # 各note raw2×2 variantsは overlap2 となり、4→2へ正しく戻る。空keyは同定不能。
        _key_total: Counter[str] = Counter()
        _key_member_max: Counter[str] = Counter()
        for _events in _member_raw_tl:
            _member_counts = Counter(filter(None, (_fb_dedup_key(event) for event in _events)))
            _key_total.update(_member_counts)
            for _key, _count in _member_counts.items():
                _key_member_max[_key] = max(_key_member_max[_key], _count)
        _cross_variant_overlap = sum(
            _key_total[key] - _key_member_max[key] for key in _key_total
        )
        # frontmatter はparserが拾えない旧形式も含むためrawを基準にする。event無しはraw維持。
        _canon["fb"] = (
            max(len(_unique_tl), _raw_fb_count - _cross_variant_overlap)
            if _flat_tl
            else _raw_fb_count
        )
        # 全variantのwikilinkをtargetごと先勝ちで集合和する。正式noteを先に
        # 読むため、同targetのcontextとindustry同数tieも決定的になる。
        _merged_wl = {}
        _members_for_union = [_property_source] + [c for c in _grp if c is not _property_source]
        for _member in _members_for_union:
            for _target, _ctx in _member.get("_wl", []):
                _target_key = _portable_path_key(_target)
                _merged_wl.setdefault(_target_key, (_target, _ctx))
        _canon["_wl"] = list(_merged_wl.values())
        _canon["_is_multi_client_group"] = len(_grp) > 1
        _canon.pop("_raw_name", None)
        # unique化してから日付順・30件cap（cap前の件数をfb propertyへ使う）。
        _canon["tl"] = _sort_fb_events(_unique_tl)
        if len(_grp) > 1 and _canon["tl"]:
            # exporterの本来契約に合わせ、multi groupの商談propertyはmerged最新FBから復元。
            # timelineが無い場合だけ、上で選んだ正式note/従来代表のfrontmatterを保つ。
            _latest_event = _canon["tl"][0]
            _canon["phase"] = _latest_event.get("ph", "")
            _canon["bant"] = _latest_event.get("bant", "")
            _canon["bantg"] = bant_short(_canon["bant"])
        # 最終FB日（日付降順ソート済なので先頭が最新。全件日付なしなら ""）
        _canon["lastfb"] = _canon["tl"][0]["d"] if _canon["tl"] else ""
        clients.append(_canon)

    docs = []
    for f in sorted(DOCS.glob("*.md")):
        if _portable_path_key(f"docs/{f.name}") not in ACTIVE_MANAGED_PATHS:
            continue
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
        _dclient = _canon_client(fm.get("client", ""))  # doc側 client も同じ正本化（doc→client リンク一致）
        if _is_self_org(_dclient):         # 自社は取引先に出さない（資料自体は残す）
            _dclient = ""
        _msrc = re.search(r"^- 出典: \[(\w+)\]", t, re.M)
        _src = {"gdrive": "Drive", "gsheets": "フォーム", "slack": "Slack"}.get(
            _msrc.group(1) if _msrc else "", ""
        )
        _dtitle = TITLE_OVERRIDE.get(_portable_path_key(f.stem)) or fm.get("title", f.stem)
        _dsol = fm.get("solution", "")  # 施策: 格納値/施策タグは正本化・vfmt 検出には生値（動画形式判定の回帰防止）
        _dbody = doc_md(t)
        # ナレッジ共有メタ（フォーム回答/ファイル記録シート由来）。カテゴリは単値素通し、
        # クライアント種別/提案プロダクトは ents 同様ここで分割・正規化して配列化する。
        _dcategory = fm.get("category", "")
        _dtier_raw = fm.get("client_tier", "")
        _dprod_raw = fm.get("product", "")
        docs.append({
            "stem": f.stem, "title": _dtitle,
            "client": _dclient, "cnorm": norm(_dclient),
            "industry": _canon_industry(fm.get("industry", "")), "solution": _canon_solution(_dsol),
            "doc_type": fm.get("doc_type", ""), "modified": fm.get("modified_at", ""),
            "src": _src,
            # タグ第1弾（資料側）: 媒体=title+excerpt / 動画形式=+solution / 形式=stem 末尾拡張子。
            # 横断/（xc）は実 wikilink 網の構築後に付与
            "media": media_tags(_dtitle + "\n" + ex),
            "vfmt": video_format_tags(_dtitle + "\n" + ex + "\n" + _dsol),
            "fmt": file_format_tag(f.stem), "xc": "",
            # 名寄せタグ（cls_entities）: export_vault が frontmatter entities に CSV で載せる。
            # /app の「関係先/」タグ・検索へ展開（親クライアント名で子コラボ資料を出す）。
            "ents": [e.strip() for e in fm.get("entities", "").split(",") if e.strip()],
            # ナレッジ共有メタ4軸: カテゴリ(多値)/クライアント種別(多値)/提案プロダクト(多値)/
            # 施策手法(title+excerpt+product+category+本文の決定論キーワード)/代理店(bool)。
            "category": category_tags(_dcategory),
            "client_tier": client_tier_tags(_dtier_raw),
            "product": product_tags(_dprod_raw),
            "method": method_tags(
                _dtitle + "\n" + ex + "\n" + _dprod_raw + "\n" + _dcategory + "\n" + _dbody
            ),
            "agency": agency_flag(_dbody + "\n" + ex),
            "ex": ex, "md": _dbody, "_wl": parse_links(body_of(t)),
        })

    # graphとpropertyが同じ優先順位で解決するため、client/doc lookupをここで共通化する。
    _c_stems = {c["stem"] for c in clients}
    _c_norm = {}
    for _client in clients:
        _c_norm[norm(_client["name"])] = _client["stem"]
        _c_norm.setdefault(norm(_client["stem"]), _client["stem"])
    for _variant, _canonical_name in CLIENT_ALIAS.items():
        _canonical_stem = _c_norm.get(norm(_canonical_name))
        if _canonical_stem:
            _c_norm[norm(_variant)] = _canonical_stem
    _d_stems = {d["stem"] for d in docs}
    _d_portable = {}
    for _stem in sorted(_d_stems):
        _portable_key = _portable_path_key(_stem)
        if _portable_key in _d_portable and _d_portable[_portable_key] != _stem:
            _die(
                "表示資料のstemがNFC/casefold後に衝突しています: "
                f"{_d_portable[_portable_key]} / {_stem}"
            )
        _d_portable[_portable_key] = _stem
    _docs_by_stem = {d["stem"]: d for d in docs}

    def _resolve_visible_doc_stem(target: str) -> str | None:
        """graph resolverと同じ優先順位で、visible docだけを解決する。"""
        if target.startswith("clients/"):
            return None
        if target.startswith("docs/"):
            stem = target[5:]
            return stem if stem in _d_stems else _d_portable.get(_portable_path_key(stem))
        # bare targetがclient/docの両方に一致する場合は、従来resolverどおりclient優先。
        if target in _c_stems or norm(target) in _c_norm:
            return None
        return target if target in _d_stems else _d_portable.get(_portable_path_key(target))

    # 全client cardの資料数を、最終的に公開される資料（active manifest適合かつ
    # exclude/dedup後）へ解決できるoutgoing wikilinkのdistinct数で再計数する。
    for _client in clients:
        _linked_doc_stems = []
        _seen_doc_stems = set()
        for _target, _ctx in _client.get("_wl", []):
            _resolved_stem = _resolve_visible_doc_stem(_target)
            if _resolved_stem and _resolved_stem not in _seen_doc_stems:
                _seen_doc_stems.add(_resolved_stem)
                _linked_doc_stems.append(_resolved_stem)
        _client["doc"] = len(_linked_doc_stems)
        if _client.pop("_is_multi_client_group", False):
            # exporterのindustry契約と同様に、merged visible linked docsの最頻値を使う。
            # link順は正式note先頭なので、同数時のCounter先勝ちも決定的。
            _linked_industries = [
                _docs_by_stem[_stem]["industry"]
                for _stem in _linked_doc_stems
                if _docs_by_stem[_stem]["industry"]
            ]
            if _linked_industries:
                _client["industry"] = Counter(_linked_industries).most_common(1)[0][0]

    # 最終接点 = max(最終FB日, 関連資料の最新更新日)。タグ用に事前計算（JS テーブル列 lastOf と同式）
    _lastdoc: dict[str, str] = {}
    for _d in docs:
        if _d["cnorm"] and _d["modified"] and _d["modified"] > _lastdoc.get(_d["cnorm"], ""):
            _lastdoc[_d["cnorm"]] = _d["modified"]
    for _c in clients:
        _c["last"] = max(_c["lastfb"], _lastdoc.get(_c["cnorm"], ""))

    # タグ第1弾（クライアント側）: 温度感/（最新FBのポジネガ比）・テーブル「次アクション」列・
    # 宿題/あり（次アクションが残っているのに最終接点が31日超過去 or 日付なし）
    # タグ第2弾: 担当/（FB送信者の名寄せ統合後 distinct・フォーム由来のみ実名あり）
    _today = datetime.now(JST).date()
    for _c in clients:
        _c["temp"] = temperature_tag(_c["tl"])
        _c["nx"] = next_action(_c["tl"])
        _c["hw"] = 1 if homework_flag(_c["nx"], _c["last"], _today) else 0
        _c["tans"] = tans_of(_c["tl"])

    # AI洗い出しレポート（_reports/）はACL付きactive/hash manifestの管理外なので搭載しない。
    # followup_gaps の実用価値は 宿題/あり タグ＋次アクション列が置き換え済み。
    reports = []

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


    # まとめ軸(client基準=最終接点/doc基準=資料の種類・施策)の値はノードへ再埋め込みせず、
    # 既に payload に載る DATA.clients/DATA.docs を stem で引く（JS grpVal・軸用の追加埋め込みなし）。
    # phase/industry はノードの既存フィールドをそのまま流用する。
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
    # 表示中(ナレッジ)のdocのみ = 非ナレッジ宛リンクは自動で落ちる。
    # _c_stems/_c_norm/_d_stems/_d_portableはproperty再計数と共通。
    _r_stems = {r["stem"] for r in reports}


    def _resolve(tgt):
        if tgt.startswith("clients/"):
            s = tgt[8:]
            if s in _c_stems:
                return "c:" + s
            st = _c_norm.get(norm(s))
            return "c:" + st if st else None
        if tgt.startswith("docs/"):
            stem = _resolve_visible_doc_stem(tgt)
            return "d:" + stem if stem else None
        if tgt in _c_stems:
            return "c:" + tgt
        st = _c_norm.get(norm(tgt))
        if st:
            return "c:" + st
        resolved_doc = _resolve_visible_doc_stem(tgt)
        if resolved_doc:
            return "d:" + resolved_doc
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

    # 横断/: 相異なる取引先（名寄せ後）2社以上の実 wikilink から参照される資料に付与。
    # links は (src,tgt) 単位で dedup 済み → 同一クライアントからの重複リンクは1社と数える
    _xref = defaultdict(set)
    for _sk, _tk, _ctx in links:
        if _sk.startswith("c:") and _tk.startswith("d:"):
            _xref[_tk[2:]].add(_sk[2:])
    for _d in docs:
        _n = len(_xref.get(_d["stem"], ()))
        _d["xc"] = "3社以上" if _n >= 3 else ("2社" if _n == 2 else "")

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

    payload = {"manifest_sha256": EXPORT_MANIFEST_SHA256,
               "build_inputs_sha256": BUILD_INPUTS_SHA256,
               "clients": clients, "docs": docs, "reports": reports, "links": links,
               "graph": {"nodes": gnodes, "links": glinks}, "colors": INDUSTRY_COLORS,
               "stats": {"clients": len(clients), "docs": len(docs), "reports": len(reports),
                         "manifest_sha256": EXPORT_MANIFEST_SHA256,
                         "build_inputs_sha256": BUILD_INPUTS_SHA256}}
    DATA = json.dumps(payload, ensure_ascii=False)
    # インライン<script>内へ安全に埋め込む: </script> ブレイクアウトと行区切り文字を無害化
    DATA = (DATA.replace("<", "\\u003c").replace(">", "\\u003e")
                .replace(" ", "\\u2028").replace(" ", "\\u2029"))
    data_sha256 = hashlib.sha256(DATA.encode("utf-8")).hexdigest()

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
    qf_counts = _quickfilter_counts(clients, docs, _today)   # 横断/（xc）確定後に算出
    new_stats = {
        "clients": len(clients),
        "docs": len(docs),
        "bytes": len(html_bytes),
        "manifest_sha256": EXPORT_MANIFEST_SHA256,
        "build_inputs_sha256": BUILD_INPUTS_SHA256,
        "data_sha256": data_sha256,
        "built_at": datetime.now(JST).isoformat(),
        "qf": qf_counts,
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
    _tans_clients = sum(1 for c in clients if c.get("tans"))
    _tans_names = {n for c in clients for n in c.get("tans") or []}
    _dated_fb = sum(1 for c in clients for ev in c.get("tl") or [] if ev.get("d"))
    print(
        f"   担当タグ: {_tans_clients} クライアント / 担当名 {len(_tans_names)} 種 / "
        f"日付ありFB {_dated_fb}/{_tl_events} 件"
    )
    print("   クイックフィルタ件数: " + " / ".join(f"{k}={v}" for k, v in qf_counts.items()))
    print(f"   統計保存: {stats_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
