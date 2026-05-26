"""data/ingest_sources.yaml をパースして型安全な dataclass list に変換する。

Sprint 3 / PR-6 で導入。pipeline.py / scripts/ingest_sources.py から呼ばれる。

設計:
- pydantic は重いので標準の dataclass + 手動バリデーション
- プレースホルダ（REPLACE_WITH_...）を検知して fail-fast
- yaml は PyYAML (PyYAML 既に依存に入ってるか確認、入ってなければ追加)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# -----------------------------------------------------------
# 設定 dataclass（ingest_sources.yaml の各セクション）
# -----------------------------------------------------------
@dataclass(frozen=True)
class SlackChannelSpec:
    """slack_channels[] の 1 件。"""

    channel_id: str
    channel_name: str
    description: str
    include_files: bool = True
    oldest_days: int | None = 90
    extra_acl_emails: tuple[str, ...] = ()
    extra_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GDriveFolderSpec:
    """gdrive_folders[] の 1 件。"""

    folder_id: str
    folder_name: str
    description: str
    include_subfolders: bool = False
    mime_type_filter: str | None = None
    extra_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GSheetsTabSpec:
    """gsheets[].tabs[] の 1 件。"""

    gid: int
    tab_name: str


@dataclass(frozen=True)
class GSheetSpec:
    """gsheets[] の 1 件。"""

    sheet_id: str
    sheet_name: str
    description: str
    tabs: tuple[GSheetsTabSpec, ...]
    row_unit: bool = True
    extra_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestSources:
    """ingest_sources.yaml 全体。"""

    version: int
    slack_channels: tuple[SlackChannelSpec, ...]
    gdrive_folders: tuple[GDriveFolderSpec, ...]
    gsheets: tuple[GSheetSpec, ...]


# -----------------------------------------------------------
# プレースホルダ検知
# -----------------------------------------------------------
_PLACEHOLDER_MARKERS = ("REPLACE_WITH_", "__RDS_", "<aws_account>", "TODO_FILL")


def _is_placeholder(value: str) -> bool:
    """REPLACE_WITH_... 等の未置換マーカーか判定。"""
    return any(marker in value for marker in _PLACEHOLDER_MARKERS)


# -----------------------------------------------------------
# loader 本体
# -----------------------------------------------------------
def load_ingest_sources(
    yaml_path: Path,
    *,
    skip_placeholder: bool = True,
) -> IngestSources:
    """yaml を読んで IngestSources に変換する。

    Args:
        yaml_path: data/ingest_sources.yaml への絶対 / 相対パス
        skip_placeholder: True なら channel_id 等にプレースホルダがある source を skip
            （fail-fast したい場合は False で渡すと ValueError）

    Returns:
        IngestSources（プレースホルダ source 除外済）

    Raises:
        FileNotFoundError: yaml が存在しない
        ValueError: skip_placeholder=False でプレースホルダ検出
    """
    import yaml  # 遅延 import（PyYAML は重くないが慣例で）

    if not yaml_path.exists():
        raise FileNotFoundError(f"ingest sources yaml not found: {yaml_path}")

    raw: dict[str, Any] = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}

    version = int(raw.get("version", 1))

    slack_channels = _parse_slack_channels(
        raw.get("slack_channels", []) or [], skip_placeholder=skip_placeholder
    )
    gdrive_folders = _parse_gdrive_folders(
        raw.get("gdrive_folders", []) or [], skip_placeholder=skip_placeholder
    )
    gsheets = _parse_gsheets(raw.get("gsheets", []) or [], skip_placeholder=skip_placeholder)

    logger.info(
        "ingest_sources_loaded",
        path=str(yaml_path),
        version=version,
        slack_channels=len(slack_channels),
        gdrive_folders=len(gdrive_folders),
        gsheets=len(gsheets),
    )
    return IngestSources(
        version=version,
        slack_channels=slack_channels,
        gdrive_folders=gdrive_folders,
        gsheets=gsheets,
    )


def _parse_slack_channels(
    raw: list[dict[str, Any]], *, skip_placeholder: bool
) -> tuple[SlackChannelSpec, ...]:
    out: list[SlackChannelSpec] = []
    for item in raw:
        channel_id = str(item.get("channel_id", ""))
        if _is_placeholder(channel_id):
            if skip_placeholder:
                logger.warning(
                    "ingest_sources_skip_placeholder",
                    section="slack_channels",
                    channel_id=channel_id,
                    name=item.get("channel_name"),
                )
                continue
            raise ValueError(f"slack_channels entry has placeholder channel_id: {channel_id!r}")
        out.append(
            SlackChannelSpec(
                channel_id=channel_id,
                channel_name=str(item.get("channel_name", "")),
                description=str(item.get("description", "")),
                include_files=bool(item.get("include_files", True)),
                oldest_days=item.get("oldest_days") if item.get("oldest_days") is not None else 90,
                extra_acl_emails=tuple(item.get("extra_acl_emails", []) or ()),
                extra_metadata=dict(item.get("extra_metadata", {}) or {}),
            )
        )
    return tuple(out)


def _parse_gdrive_folders(
    raw: list[dict[str, Any]], *, skip_placeholder: bool
) -> tuple[GDriveFolderSpec, ...]:
    out: list[GDriveFolderSpec] = []
    for item in raw:
        folder_id = str(item.get("folder_id", ""))
        if _is_placeholder(folder_id):
            if skip_placeholder:
                logger.warning(
                    "ingest_sources_skip_placeholder",
                    section="gdrive_folders",
                    folder_id=folder_id,
                )
                continue
            raise ValueError(f"gdrive_folders entry has placeholder folder_id: {folder_id!r}")
        out.append(
            GDriveFolderSpec(
                folder_id=folder_id,
                folder_name=str(item.get("folder_name", "")),
                description=str(item.get("description", "")),
                include_subfolders=bool(item.get("include_subfolders", False)),
                mime_type_filter=item.get("mime_type_filter"),
                extra_metadata=dict(item.get("extra_metadata", {}) or {}),
            )
        )
    return tuple(out)


def _parse_gsheets(raw: list[dict[str, Any]], *, skip_placeholder: bool) -> tuple[GSheetSpec, ...]:
    out: list[GSheetSpec] = []
    for item in raw:
        sheet_id = str(item.get("sheet_id", ""))
        if _is_placeholder(sheet_id):
            if skip_placeholder:
                logger.warning(
                    "ingest_sources_skip_placeholder", section="gsheets", sheet_id=sheet_id
                )
                continue
            raise ValueError(f"gsheets entry has placeholder sheet_id: {sheet_id!r}")
        tabs_raw: list[dict[str, Any]] = item.get("tabs", []) or []
        tabs = tuple(
            GSheetsTabSpec(gid=int(t.get("gid", 0)), tab_name=str(t.get("tab_name", "")))
            for t in tabs_raw
        )
        out.append(
            GSheetSpec(
                sheet_id=sheet_id,
                sheet_name=str(item.get("sheet_name", "")),
                description=str(item.get("description", "")),
                tabs=tabs,
                row_unit=bool(item.get("row_unit", True)),
                extra_metadata=dict(item.get("extra_metadata", {}) or {}),
            )
        )
    return tuple(out)
