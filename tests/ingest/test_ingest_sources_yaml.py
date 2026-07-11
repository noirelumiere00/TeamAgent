"""data/ingest_sources.yaml のルールブック再編エントリ（入れ込み v2・C6）のテスト。

2026-07-10 追加分の検証:
- 01〜06 のプレースホルダエントリが宣言されていること（folder_id 未確定でも安全に skip）
- 既存エントリ（実 ID の 2 フォルダ・Slack 2 ch・Sheets 2 件・crawl）が壊れていないこと
- グローバルキー gdrive_rulebook_root_folder_id が存在すること
- 99_ 系のエントリを書いていないこと（ルート検査 / 名前除外の前提）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from teamagent.ingest.loader import load_ingest_sources

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
REAL_YAML = PROJECT_ROOT / "data" / "ingest_sources.yaml"

_RULEBOOK_FOLDER_NAMES = (
    "01_提案事例",
    "02_テンプレ・雛形",
    "03_定期報告・実績データ",
    "04_会社紹介・ケイパ資料",
    "05_議事録・商談メモ",
    "06_価格・契約",
)


def _raw() -> dict[str, Any]:
    return yaml.safe_load(REAL_YAML.read_text(encoding="utf-8"))


def test_rulebook_entries_declared_with_placeholders() -> None:
    """01〜06 の 6 エントリが REPLACE_WITH_KNOWLEDGE_NN で宣言されている。"""
    raw = _raw()
    by_name = {f["folder_name"]: f for f in raw["gdrive_folders"]}
    for i, name in enumerate(_RULEBOOK_FOLDER_NAMES, start=1):
        assert name in by_name, f"ルールブックフォルダが未宣言: {name}"
        entry = by_name[name]
        assert entry["folder_id"] == f"REPLACE_WITH_KNOWLEDGE_{i:02d}"
        assert entry["include_subfolders"] is True
        assert entry["extra_metadata"]["rulebook_category"], name


def test_rulebook_placeholders_are_skipped_by_loader() -> None:
    """プレースホルダのままの 6 エントリは loader が skip し、既存 2 フォルダだけ残る。

    ＝folder_id 未確定のまま merge/デプロイしても既存 ingest を壊さない。
    """
    sources = load_ingest_sources(REAL_YAML, skip_placeholder=True)
    folder_ids = [f.folder_id for f in sources.gdrive_folders]
    assert "12FMLe9XG24wlPrBCHOQ_vcr4uELtMN1E" in folder_ids  # ナレッジ共有 - 添付ファイル
    assert "14Wfp6GVCwaJROGhmEbd-r4_CfUymjwDL" in folder_ids  # ショート動画資料全般
    assert not any("REPLACE_WITH_" in fid for fid in folder_ids)
    assert len(sources.gdrive_folders) == 2


def test_existing_sections_unchanged() -> None:
    """既存の Slack / Sheets / crawl 設定を変更していない（v2 は純加算）。"""
    sources = load_ingest_sources(REAL_YAML, skip_placeholder=True)
    assert len(sources.slack_channels) == 2
    assert len(sources.gsheets) == 2
    assert sources.shared_drives_crawl is not None
    assert sources.shared_drives_crawl.enabled is True
    assert sources.shared_drives_crawl.max_files_per_drive == 3000


def test_rulebook_root_folder_id_global_key() -> None:
    """ルート検査用グローバルキーがプレースホルダで宣言されている。"""
    raw = _raw()
    assert raw.get("gdrive_rulebook_root_folder_id") == "REPLACE_WITH_KNOWLEDGE_ROOT"


def test_no_99_warehouse_entry() -> None:
    """99_一次倉庫（検索対象外）のエントリを書かない（ルールブック運用の前提）。"""
    raw = _raw()
    for f in raw["gdrive_folders"]:
        assert not str(f["folder_name"]).startswith("99"), f["folder_name"]
        assert "一次倉庫" not in str(f["folder_name"]), f["folder_name"]
