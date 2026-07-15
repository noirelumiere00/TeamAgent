"""ingest/loader.py のテスト。

実 yaml と最小 stub yaml の両方で検証。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from teamagent.ingest.loader import (
    GDriveFolderSpec,
    GSheetSpec,
    IngestSources,
    SlackChannelSpec,
    _is_placeholder,
    load_ingest_sources,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
REAL_YAML = PROJECT_ROOT / "data" / "ingest_sources.yaml"


# -----------------------------------------------------------
# プレースホルダ検知
# -----------------------------------------------------------
def test_is_placeholder_detects_replace_with() -> None:
    assert _is_placeholder("REPLACE_WITH_C_ID_FOR_proj-knowledge-sharing")
    assert _is_placeholder("__RDS_ENDPOINT__")
    assert _is_placeholder("teamagent-dev.<aws_account>.rds.amazonaws.com")


def test_is_placeholder_passes_real_values() -> None:
    assert not _is_placeholder("C0XYZ12345")
    assert not _is_placeholder("teamagent-dev.c164uq6g8u35.ap-northeast-1.rds.amazonaws.com")
    assert not _is_placeholder("12FMLe9XG24wlPrBCHOQ_vcr4uELtMN1E")


# -----------------------------------------------------------
# 実 yaml のパース（既存ファイル）
# -----------------------------------------------------------
def test_load_real_yaml_has_all_sources() -> None:
    """data/ingest_sources.yaml は実 ID 投入済（2026-05-27 で channel_id 確定）。

    Slack 2 ch / Drive 1 folder / Sheets 2 sheets が含まれる前提。
    """
    sources = load_ingest_sources(REAL_YAML, skip_placeholder=True)
    assert isinstance(sources, IngestSources)
    assert len(sources.slack_channels) == 2, (
        f"Slack 2 ch 期待。 channel_id がプレースホルダに戻った場合 skip される。 "
        f"got={[c.channel_id for c in sources.slack_channels]}"
    )
    assert len(sources.gdrive_folders) >= 1
    assert len(sources.gsheets) >= 1


def test_load_real_yaml_strict_mode_passes_after_rulebook_cleanup() -> None:
    """strict mode（skip_placeholder=False）が通ること。

    2026-07-10〜07-15: ルールブック 01〜06 のプレースホルダエントリを宣言していた間は
    strict mode が必ず ValueError になり、この検証経路自体が使えなかった。実 Drive 計測で
    当該エントリが不要（親の再帰 walk で既にカバー）と確定し撤去したため、strict mode が
    再び使えるようになった＝プレースホルダの混入をこのテストで検知できる。

    gdrive_rulebook_root_folder_id のプレースホルダは source ではなくグローバルキーなので、
    _parse_rulebook_root_folder_id が warning + None に落として例外にしない（実 ID を貼ると
    preflight が全断するため、placeholder のままが正しい状態）。
    """
    sources = load_ingest_sources(REAL_YAML, skip_placeholder=False)
    assert not any("REPLACE_WITH_" in f.folder_id for f in sources.gdrive_folders)
    assert sources.gdrive_rulebook_root_folder_id is None  # placeholder → 無効（＝preflight OFF）


# -----------------------------------------------------------
# 最小 yaml で各 spec dataclass マッピング
# -----------------------------------------------------------
def test_load_minimal_slack_channel(tmp_path: Path) -> None:
    yaml_path = tmp_path / "src.yaml"
    yaml_path.write_text(
        """
version: 1
slack_channels:
  - channel_id: "C0REAL"
    channel_name: "#test"
    description: "test ch"
    include_files: false
    oldest_days: 30
    extra_acl_emails: ["alice@x.jp"]
    extra_metadata:
      topic: "test"
gdrive_folders: []
gsheets: []
""",
        encoding="utf-8",
    )
    s = load_ingest_sources(yaml_path)
    assert len(s.slack_channels) == 1
    ch = s.slack_channels[0]
    assert isinstance(ch, SlackChannelSpec)
    assert ch.channel_id == "C0REAL"
    assert ch.include_files is False
    assert ch.oldest_days == 30
    assert ch.extra_acl_emails == ("alice@x.jp",)
    assert ch.extra_metadata == {"topic": "test"}


def test_load_minimal_gdrive_folder(tmp_path: Path) -> None:
    yaml_path = tmp_path / "src.yaml"
    yaml_path.write_text(
        """
version: 1
slack_channels: []
gdrive_folders:
  - folder_id: "12FMLe9XG24"
    folder_name: "ナレッジ"
    description: "ナレッジ folder"
    include_subfolders: true
    mime_type_filter: "application/pdf"
    extra_metadata: {topic: "ナレッジ"}
gsheets: []
""",
        encoding="utf-8",
    )
    s = load_ingest_sources(yaml_path)
    assert len(s.gdrive_folders) == 1
    f = s.gdrive_folders[0]
    assert isinstance(f, GDriveFolderSpec)
    assert f.folder_id == "12FMLe9XG24"
    assert f.include_subfolders is True
    assert f.mime_type_filter == "application/pdf"


def test_load_minimal_gsheet_with_tabs(tmp_path: Path) -> None:
    yaml_path = tmp_path / "src.yaml"
    yaml_path.write_text(
        """
version: 1
slack_channels: []
gdrive_folders: []
gsheets:
  - sheet_id: "1VukC"
    sheet_name: "FB"
    description: "..."
    tabs:
      - {gid: 537831563, tab_name: "フォーム回答 1"}
      - {gid: 999, tab_name: "集計"}
    row_unit: true
    extra_metadata: {priority: "high"}
""",
        encoding="utf-8",
    )
    s = load_ingest_sources(yaml_path)
    assert len(s.gsheets) == 1
    sh = s.gsheets[0]
    assert isinstance(sh, GSheetSpec)
    assert sh.sheet_id == "1VukC"
    assert len(sh.tabs) == 2
    assert sh.tabs[0].gid == 537831563
    assert sh.tabs[0].tab_name == "フォーム回答 1"


def test_load_skips_placeholder_slack(tmp_path: Path) -> None:
    yaml_path = tmp_path / "src.yaml"
    yaml_path.write_text(
        """
version: 1
slack_channels:
  - channel_id: "REPLACE_WITH_C_ID_FOR_x"
    channel_name: "#x"
    description: "x"
  - channel_id: "C0REAL"
    channel_name: "#real"
    description: "real"
gdrive_folders: []
gsheets: []
""",
        encoding="utf-8",
    )
    s = load_ingest_sources(yaml_path, skip_placeholder=True)
    assert len(s.slack_channels) == 1
    assert s.slack_channels[0].channel_id == "C0REAL"


def test_load_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_ingest_sources(Path("/nonexistent/x.yaml"))


def test_load_empty_yaml_returns_empty_sections(tmp_path: Path) -> None:
    yaml_path = tmp_path / "empty.yaml"
    yaml_path.write_text("version: 1\n", encoding="utf-8")
    s = load_ingest_sources(yaml_path)
    assert s.slack_channels == ()
    assert s.gdrive_folders == ()
    assert s.gsheets == ()
