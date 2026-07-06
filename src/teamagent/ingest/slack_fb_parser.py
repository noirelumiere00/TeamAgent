"""Slack #proj-ショート動画_営業フィードバック情報 投稿の構造化 parser。

Day 8 (2026-05-28) で追加。投稿は Workflow Bot がフォーム形式で投げており、
本文が `*ラベル*\\n値\\n*次のラベル*\\n値...` の Slack bold marker で構造化されている。

現状の Slack ingest は本文を 1 つの plain text として embedding するだけで、
列の構造を完全に捨てていた。この parser を通すことで以下が手に入る:

1. 検索: `WHERE metadata->>'industry'='エネルギー'` で行レベル絞り込み
2. 集計: 業界 × 商談フェーズ × BANT のクロス集計
3. クライアントカルテ: `metadata->>'client_name'='INPEX'` で時系列横断
4. Drive 自動マッチング: client_name で Drive PDF を裏で検索

設計:
- chunk content (embedding 対象) は **変更しない** → 既存検索に副作用なし
- 非 FB 投稿は parse_fb_post() が空 dict を返す → 通常の Slack ingest 経路と同じ
- migration 不要 (documents.metadata JSONB に append するだけ)

2026-07-03: gsheets フォーム回答シート (row_unit) からも同品質のメタを付けるため、
「ラベル/ヘッダ → 値 dict を metadata dict へ写像する」コア (map_fb_fields) を
切り出した。parse_fb_post は Slack bold marker の抽出だけ担当し、写像・FB 判定は
map_fb_fields に委譲する (後方互換)。_ingest_gsheet はシート行のヘッダ → 値 dict を
map_fb_fields に直接渡す。シートヘッダの表記ゆれ (半角括弧・`顧客名・案件名` 等) は
_normalize_fb_label で canonical ラベルに正規化する。
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# Slack の bold marker `*Label*\n` を検出する正規表現。
# - 行頭の `*` で開始 (MULTILINE で `^` は各行先頭)
# - `*` を含まないラベル名
# - 改行後の値（次のラベル行直前または文末まで）。空 value も許可。
# 値は複数行に渡る (ポジ反応 / ネガ反応 / ネクストアクション が箇条書きになるため、DOTALL)。
#
# Day 8 (2026-05-28) fix: value `(.+?)` を `(.*?)` に変更して空欄を許可。
# それまでは `*顧客名/案件名*` の値が空のとき、次の `*商談フェーズ*` 行が value に
# 吸い込まれて parsed client_name が `*商談フェーズ*\nケイパ` のような壊れた値になる
# バグがあった (19 件の FB が影響)。lookahead を MULTILINE `^\*Label*` 形式にすることで
# 「次のラベル行の先頭」を厳密に判定する。
_FB_FIELD_PATTERN = re.compile(
    r"^\*([^*\n]+)\*[ \t]*\n(.*?)(?=^\*[^*\n]+\*|\Z)",
    re.MULTILINE | re.DOTALL,
)

# ラベル文字列 (Slack 表示 / シートヘッダの canonical 形) → metadata JSONB key の写像。
# 営業 FB の現行フォーム (2026-05 時点) を実投稿から確認して作成。
# 新規ラベルが追加されたらここに足すだけ。lookup 前に _normalize_fb_label を通すこと。
_LABEL_TO_METADATA_KEY: dict[str, str] = {
    "商流": "channel_type",
    "顧客名": "agency_name",
    "顧客名/案件名": "client_case",
    "商談フェーズ": "deal_phase",
    "提案メニュー": "proposed_menu",
    "商談感触（BANT）": "bant_score",
    "顧客反応（ポジティブ）": "positive_reaction",
    "顧客反応（質問事項、ネガティブ）": "negative_reaction",
    # gsheets フォーム回答シートはポジ/ネガが 1 列に統合されている (2026-07-03 実データ確認)。
    # positive_reaction / negative_reaction へ寄せると意味が壊れる (ネガ内容がポジ扱いに
    # なる) ので、統合列専用の新キーにする。
    "顧客反応（ポジ・ネガ）": "client_reaction",
    "ネクストアクション": "next_action",
    "共有メモ": "shared_memo",
}

# ラベル/ヘッダの表記ゆれ → canonical ラベルの alias。
# _normalize_fb_label で括弧・スラッシュを正規化した **後** に適用する。
_LABEL_ALIASES: dict[str, str] = {
    # gsheets フォーム回答シートのヘッダは '・' 区切り (Slack フォームは '/')
    "顧客名・案件名": "顧客名/案件名",
}

# FB 投稿らしさを判定するための最小条件。
# このうち N 個以上 hit したら「営業 FB 投稿」と認定する。
# 短い雑談・通常投稿が誤分類されないよう、商談関連の必須コアラベルを 3 つ要求。
_FB_CORE_LABELS = frozenset({"商流", "顧客名", "顧客名/案件名", "商談フェーズ", "商談感触（BANT）"})
_FB_MIN_CORE_HITS = 3

# Slack workflow bot が投稿末尾に自動付与する定型フッターマーカー。
# このマーカー以降は parser 対象外として切り捨てる (Spreadsheet リンク等が値に混入するのを防ぐ)。
_FOOTER_MARKERS: tuple[str, ...] = (
    "これまで共有されたフィードバック",
    "続けて案件相談をする場合",
)


def _normalize_fb_label(label: str) -> str:
    """ラベル/シートヘッダの表記ゆれを canonical ラベルへ正規化する。

    - 前後 whitespace 除去
    - 半角括弧 `()` → 全角括弧 `（）` (シート「顧客反応(ポジ・ネガ)」等)
    - 全角スラッシュ `／` → 半角 `/`
    - alias 適用 (「顧客名・案件名」 → 「顧客名/案件名」等)
    """
    normalized = label.strip().replace("(", "（").replace(")", "）").replace("／", "/")
    return _LABEL_ALIASES.get(normalized, normalized)


def map_fb_fields(fields: Mapping[str, str]) -> dict[str, str]:
    """ラベル/ヘッダ → 値 の dict を営業 FB metadata JSONB 用 dict に写像する。

    Slack 投稿 (parse_fb_post 経由) と gsheets フォーム回答行 (_ingest_gsheet) の
    共通コア。ヘッダは _normalize_fb_label で正規化してから照合するので、
    シート特有の表記ゆれ (半角括弧・`顧客名・案件名`) も吸収する。

    Returns:
        - 正規化後のコアラベルが _FB_MIN_CORE_HITS 個以上見つかった場合:
          known label → 非空値 の dict (空値の列/ラベルは含めない)
        - コアラベル不足 (= 営業 FB ではない) の場合: 空 dict {}
          → 非 FB シート/投稿への副作用ゼロ

    コアラベル判定は「ラベル (列) の存在」で行い値の有無は問わない (Slack 経路の
    従来挙動と同一)。既知ラベル以外は無視する。
    """
    if not fields:
        return {}

    normalized: dict[str, str] = {}
    for label, value in fields.items():
        canonical = _normalize_fb_label(label)
        # 表記ゆれで同一 canonical に潰れた場合は非空値を優先 (空値で上書きしない)
        if canonical not in normalized or value.strip():
            normalized[canonical] = value

    core_hits = len(normalized.keys() & _FB_CORE_LABELS)
    if core_hits < _FB_MIN_CORE_HITS:
        return {}

    out: dict[str, str] = {}
    for canonical, value in normalized.items():
        key = _LABEL_TO_METADATA_KEY.get(canonical)
        if key is None:
            continue
        cleaned = value.strip()
        if not cleaned:
            continue
        out[key] = cleaned

    return out


def parse_fb_post(content: str) -> dict[str, str]:
    """Slack 営業 FB 投稿の `*ラベル*` 形式を解析し、metadata JSONB 用 dict を返す。

    Args:
        content: Slack 投稿本文（thread parent text、format_thread_as_document の出力でも可）

    Returns:
        - FB 投稿と判定された場合: known label → 値 の dict
          (例: {"client_case": "SCSK/スモカ歯磨", "deal_phase": "ヒアリング", ...})
        - FB 投稿でない場合: 空 dict {}

    既知ラベル以外は無視する (未知ラベルが追加されたら _LABEL_TO_METADATA_KEY に追記)。
    値は前後 whitespace を strip するが、改行・箇条書きは保持する。
    写像と FB 判定は map_fb_fields (gsheets 経路と共通) に委譲する。
    """
    if not content:
        return {}

    # workflow bot の定型フッター以降を切り捨て (Spreadsheet リンク等の混入防止)
    for marker in _FOOTER_MARKERS:
        idx = content.find(marker)
        if idx >= 0:
            content = content[:idx]

    # 全ラベル/値ペアを抽出
    matches = _FB_FIELD_PATTERN.findall(content)
    if not matches:
        return {}

    # ラベル → 値 dict 化。重複ラベルは従来挙動 (後勝ち・ただし空値では上書きしない) を踏襲。
    fields: dict[str, str] = {}
    for label, value in matches:
        if label not in fields or value.strip():
            fields[label] = value

    return map_fb_fields(fields)


def extract_client_name(metadata: dict[str, str]) -> str | None:
    """parsed metadata から「主クライアント名」を抽出する補助関数。

    `client_case` (例: 'SCSK / スモカ歯磨', 'ニチレイ', '日本ガイシ/リクルーティング')
    の `/` 左側または全体をクライアント名として返す。Drive 自動マッチングや
    クライアントカルテ Skill で利用する想定。

    `client_case` が無く `agency_name` だけある場合は agency_name を返す
    (代理店経由案件で end client が空のケース)。
    """
    client_case = metadata.get("client_case", "").strip()
    if client_case:
        # 'SCSK / スモカ歯磨' → 'SCSK' / 'ニチレイ' → 'ニチレイ'
        primary = client_case.split("/")[0].strip()
        if primary:
            return primary

    agency = metadata.get("agency_name", "").strip()
    if agency:
        return agency

    return None
