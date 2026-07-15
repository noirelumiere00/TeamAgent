"""data/ingest_sources.yaml の gdrive セクションの回帰テスト。

2026-07-15 更新: ルールブック 01〜06 のプレースホルダ 6 エントリを撤去した
（実 Drive 計測で「親の再帰 walk が既にカバー＝足しても増えない」と確定）。
そのため「宣言されていること」を固定していた旧テストは削除し、代わりに
- プレースホルダを二度と足さないこと
- gdrive_rulebook_root_folder_id に実 ID を貼らせないこと（貼ると preflight が
  再帰カバレッジを見ずに誤検知 → SystemExit(1) で slack/gsheets ごと全断する）
を守る回帰テストを置く。既存エントリ / 99_ 非宣言の検証は従来どおり。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from teamagent.ingest.loader import load_ingest_sources

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
REAL_YAML = PROJECT_ROOT / "data" / "ingest_sources.yaml"


def _raw() -> dict[str, Any]:
    return yaml.safe_load(REAL_YAML.read_text(encoding="utf-8"))


def test_no_placeholder_gdrive_entries() -> None:
    """gdrive_folders にプレースホルダ（REPLACE_WITH_）を残さない。

    loader が skip するので無害に見えるが、「実 ID を貼れば有効になる」という誤解を生み、
    貼った瞬間に preflight が全断する導線になっていた（2026-07-15 撤去）。
    """
    raw = _raw()
    for f in raw["gdrive_folders"]:
        assert "REPLACE_WITH_" not in str(f["folder_id"]), f["folder_name"]


def test_gdrive_folders_are_the_two_real_ones() -> None:
    """gdrive_folders は実 ID の 2 フォルダのみ（撤去前後で loader 結果が不変＝挙動差分ゼロ）。

    14Wfp6… は 01〜08 の親で include_subfolders: true。再帰 walk が全カテゴリをカバーする。
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
    """ルート検査キーは **プレースホルダのまま**（実 ID を貼らせない）。

    _check_rulebook_root の不足判定は親の include_subfolders 再帰カバレッジを見ないため、
    実ルート(14Wfp6…)を貼ると取り込み済みの 01〜08 が missing 誤検知 → SystemExit(1)。
    検査は slack より前・try/except 無し・単一プロセスなので全ソースが巻き添えになる。
    有効化にはコード修正（再帰カバレッジ考慮＋fail-close の gdrive 内封じ込め）が前提。
    """
    raw = _raw()
    assert raw.get("gdrive_rulebook_root_folder_id") == "REPLACE_WITH_KNOWLEDGE_ROOT"


def test_no_99_warehouse_entry() -> None:
    """99_一次倉庫（検索対象外）のエントリを書かない（ルールブック運用の前提）。"""
    raw = _raw()
    for f in raw["gdrive_folders"]:
        assert not str(f["folder_name"]).startswith("99"), f["folder_name"]
        assert "一次倉庫" not in str(f["folder_name"]), f["folder_name"]
