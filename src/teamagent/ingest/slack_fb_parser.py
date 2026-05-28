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
"""

from __future__ import annotations

import re

# Slack の bold marker `*Label*\n` を検出する正規表現。
# - 行頭の `*` で開始
# - `*` を含まないラベル名
# - 改行後の値（次のラベル直前または文末まで）
# 値は複数行に渡る (ポジ反応 / ネガ反応 / ネクストアクション が箇条書きになるため)。
_FB_FIELD_PATTERN = re.compile(
    r"^\*([^*\n]+)\*[ \t]*\n(.+?)(?=\n\*[^*\n]+\*|\Z)",
    re.MULTILINE | re.DOTALL,
)

# ラベル文字列 (Slack 表示) → metadata JSONB key の写像。
# 営業 FB の現行フォーム (2026-05 時点) を実投稿から確認して作成。
# 新規ラベルが追加されたらここに足すだけ。
_LABEL_TO_METADATA_KEY: dict[str, str] = {
    "商流": "channel_type",
    "顧客名": "agency_name",
    "顧客名/案件名": "client_case",
    "商談フェーズ": "deal_phase",
    "提案メニュー": "proposed_menu",
    "商談感触（BANT）": "bant_score",
    "顧客反応（ポジティブ）": "positive_reaction",
    "顧客反応（質問事項、ネガティブ）": "negative_reaction",
    "ネクストアクション": "next_action",
    "共有メモ": "shared_memo",
}

# FB 投稿らしさを判定するための最小条件。
# このうち N 個以上 hit したら「営業 FB 投稿」と認定する。
# 短い雑談・通常投稿が誤分類されないよう、商談関連の必須コアラベルを 3 つ要求。
_FB_CORE_LABELS = frozenset(
    {"商流", "顧客名", "顧客名/案件名", "商談フェーズ", "商談感触（BANT）"}
)
_FB_MIN_CORE_HITS = 3

# Slack workflow bot が投稿末尾に自動付与する定型フッターマーカー。
# このマーカー以降は parser 対象外として切り捨てる (Spreadsheet リンク等が値に混入するのを防ぐ)。
_FOOTER_MARKERS: tuple[str, ...] = (
    "これまで共有されたフィードバック",
    "続けて案件相談をする場合",
)


def parse_fb_post(content: str) -> dict[str, str]:
    """Slack 営業 FB 投稿の `*ラベル*` 形式を解析し、metadata JSONB 用 dict を返す。

    Args:
        content: Slack 投稿本文（thread parent text、format_thread_as_document の出力でも可）

    Returns:
        - FB 投稿と判定された場合: known label → 値 の dict (例: {"client_case": "SCSK/スモカ歯磨", ...})
        - FB 投稿でない場合: 空 dict {}

    既知ラベル以外は無視する (未知ラベルが追加されたら _LABEL_TO_METADATA_KEY に追記)。
    値は前後 whitespace を strip するが、改行・箇条書きは保持する。
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

    # FB 投稿判定: コアラベルが N 個以上 hit するか
    found_labels = {label.strip() for label, _ in matches}
    core_hits = len(found_labels & _FB_CORE_LABELS)
    if core_hits < _FB_MIN_CORE_HITS:
        return {}

    # 既知ラベルだけ拾って metadata 化
    out: dict[str, str] = {}
    for label, value in matches:
        key = _LABEL_TO_METADATA_KEY.get(label.strip())
        if key is None:
            continue
        cleaned = value.strip()
        if not cleaned:
            continue
        out[key] = cleaned

    return out


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
