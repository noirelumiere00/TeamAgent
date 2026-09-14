"""data/ingest_sources.yaml をパースして型安全な dataclass list に変換する。

Sprint 3 / PR-6 で導入。pipeline.py / scripts/ingest_sources.py から呼ばれる。

設計:
- pydantic は重いので標準の dataclass + 手動バリデーション
- プレースホルダ（REPLACE_WITH_...）を検知して fail-fast
- yaml は PyYAML (PyYAML 既に依存に入ってるか確認、入ってなければ追加)
"""

from __future__ import annotations

import os
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
    """gsheets[].tabs[] の 1 件。

    ``gid_env``（yaml で gid を env 後入れにする宣言）は **loader 内で解決しきる**ため
    ここには持たない。解決できなかったタブは spec に現れない（_parse_gsheet_tab）ので、
    読み出し側は「gid は常に確定値」として扱ってよい。
    """

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
    # ID 後入れ（2026-09-11・B-10）: sheet_id/gid が未確定のソースを yaml に置けるようにする。
    # yaml の sheet_id がプレースホルダのときだけ env を見る（実 ID を env で黙って
    # 差し替えられる口は作らない）。解決後は普通の spec と区別が無い＝取込経路は不変。
    sheet_id_env: str | None = None


@dataclass(frozen=True)
class SharedDriveCrawlSpec:
    """共有ドライブ全自動 crawl の設定（Day 7, 2026-05-27 追加）。

    yaml で:
        shared_drives_crawl:
          enabled: true
          name_filter: ["営業", "ナレッジ"]  # 名前 substring match (空 [] なら全部)
          sales_relevance_filter: true
          max_files_per_drive: 5000
          modified_within_days: 730   # 過去 2 年（null で全期間）
          extra_metadata:
            topic: "共有ドライブ横断"
    """

    enabled: bool = False
    name_filter: tuple[str, ...] = ()
    sales_relevance_filter: bool = True
    max_files_per_drive: int = 5000
    modified_within_days: int | None = 730
    extra_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestSources:
    """ingest_sources.yaml 全体。"""

    version: int
    slack_channels: tuple[SlackChannelSpec, ...]
    gdrive_folders: tuple[GDriveFolderSpec, ...]
    gsheets: tuple[GSheetSpec, ...]
    shared_drives_crawl: SharedDriveCrawlSpec | None = None
    # 入れ込み v2 (2026-07-10) グローバルキー:
    # - gdrive_exclude_folder_name_re: walk 時のサブフォルダ名除外 regex の上書き。
    #   None（キー未記載）ならコード既定（gdrive_client.DEFAULT_EXCLUDE_FOLDER_NAME_RE）、
    #   空文字 "" を明示すると除外なし。
    # - gdrive_rulebook_root_folder_id: ルールブック用ルートフォルダ ID（設定時のみ
    #   pipeline が gdrive kind 実行冒頭で NN_ フォルダのカバレッジ検査を行う）。
    gdrive_exclude_folder_name_re: str | None = None
    gdrive_rulebook_root_folder_id: str | None = None


# -----------------------------------------------------------
# プレースホルダ検知
# -----------------------------------------------------------
_PLACEHOLDER_MARKERS = ("REPLACE_WITH_", "__RDS_", "<aws_account>", "TODO_FILL")


def _is_placeholder(value: str) -> bool:
    """REPLACE_WITH_... 等の未置換マーカーか判定。

    入れ込み v2 (2026-07-10): ``REPLACE_`` **始まり**も placeholder 扱いに拡張
    （yaml に ID 未確定エントリを安全に置けるようにする。従来の substring 判定は維持）。
    """
    return value.startswith("REPLACE_") or any(marker in value for marker in _PLACEHOLDER_MARKERS)


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
    import hashlib

    import yaml  # 遅延 import（PyYAML は重くないが慣例で）

    if not yaml_path.exists():
        raise FileNotFoundError(f"ingest sources yaml not found: {yaml_path}")

    raw_bytes = yaml_path.read_bytes()
    raw: dict[str, Any] = yaml.safe_load(raw_bytes.decode("utf-8")) or {}

    version = int(raw.get("version", 1))

    slack_channels = _parse_slack_channels(
        raw.get("slack_channels", []) or [], skip_placeholder=skip_placeholder
    )
    gdrive_folders = _parse_gdrive_folders(
        raw.get("gdrive_folders", []) or [], skip_placeholder=skip_placeholder
    )
    gsheets = _parse_gsheets(raw.get("gsheets", []) or [], skip_placeholder=skip_placeholder)
    shared_crawl = _parse_shared_drives_crawl(raw.get("shared_drives_crawl"))
    exclude_folder_name_re = _parse_exclude_folder_name_re(raw.get("gdrive_exclude_folder_name_re"))
    rulebook_root = _parse_rulebook_root_folder_id(raw.get("gdrive_rulebook_root_folder_id"))

    logger.info(
        "ingest_sources_loaded",
        path=str(yaml_path),
        # どの内容の yaml を読んだかを追跡できるよう sha256 を必ず出す（入れ込み v2）。
        sha256=hashlib.sha256(raw_bytes).hexdigest()[:12],
        version=version,
        slack_channels=len(slack_channels),
        gdrive_folders=len(gdrive_folders),
        gsheets=len(gsheets),
        shared_drives_crawl_enabled=shared_crawl is not None and shared_crawl.enabled,
        gdrive_exclude_folder_name_re=exclude_folder_name_re,
        gdrive_rulebook_root_folder_id=rulebook_root,
    )
    return IngestSources(
        version=version,
        slack_channels=slack_channels,
        gdrive_folders=gdrive_folders,
        gsheets=gsheets,
        shared_drives_crawl=shared_crawl,
        gdrive_exclude_folder_name_re=exclude_folder_name_re,
        gdrive_rulebook_root_folder_id=rulebook_root,
    )


def _parse_exclude_folder_name_re(raw: Any) -> str | None:
    """グローバルキー ``gdrive_exclude_folder_name_re`` をパースする。

    キー未記載（None）→ None（コード既定の regex を pipeline が使う）。
    記載あり → str へ正規化（空文字 "" は「除外なし」の明示）。
    """
    if raw is None:
        return None
    return str(raw)


def _parse_rulebook_root_folder_id(raw: Any) -> str | None:
    """グローバルキー ``gdrive_rulebook_root_folder_id`` をパースする。

    placeholder（REPLACE_ 始まり等）は「未設定」として WARNING 付きで None に落とす
    （ID 未確定のまま yaml に置いてもルート検査が誤発火しないように）。
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    if _is_placeholder(value):
        logger.warning(
            "ingest_sources_skip_placeholder",
            section="gdrive_rulebook_root_folder_id",
            folder_id=value,
        )
        return None
    return value


def _parse_shared_drives_crawl(raw: Any) -> SharedDriveCrawlSpec | None:
    """yaml の shared_drives_crawl: セクションをパースする（None / 空なら None を返す）。"""
    if not raw or not isinstance(raw, dict):
        return None
    return SharedDriveCrawlSpec(
        enabled=bool(raw.get("enabled", False)),
        name_filter=tuple(raw.get("name_filter", []) or []),
        sales_relevance_filter=bool(raw.get("sales_relevance_filter", True)),
        max_files_per_drive=int(raw.get("max_files_per_drive", 5000)),
        modified_within_days=(
            int(raw["modified_within_days"])
            if raw.get("modified_within_days") is not None
            else None
        ),
        extra_metadata=dict(raw.get("extra_metadata", {}) or {}),
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


def _resolve_env_id(env_name: str | None) -> str | None:
    """``sheet_id_env`` / ``gid_env`` の env を読む（未設定・空・プレースホルダは None）。

    「env で後入れ」の唯一の読み口。値そのものがプレースホルダ（REPLACE_… 等）の場合も
    未設定と同じ扱いにする（tfvars に雛形をそのまま貼った事故を取り込みへ通さない）。
    """
    if not env_name:
        return None
    raw = os.environ.get(env_name, "")
    value = raw.strip()
    if not value or _is_placeholder(value):
        return None
    return value


def _parse_gsheets(raw: list[dict[str, Any]], *, skip_placeholder: bool) -> tuple[GSheetSpec, ...]:
    out: list[GSheetSpec] = []
    # 既に使われた sheet_id。env で後入れした ID が既存エントリと衝突したら採用しない。
    # 衝突すると external_id = build_external_id(sheet_id, gid, row_idx) が完全に重なり、
    # documents の ON CONFLICT DO UPDATE（metadata = EXCLUDED.metadata の全置換）で
    # 既存 document の metadata が丸ごと差し替わる（cls_doc_type / cls_project が
    # spec の並び順だけで黙って変わる）＋ ingest_source_health の行も衝突する。
    # 「事例集の母集団は既に取り込み済みのナレッジ共有シートで足りる」という実見メモ
    # （case_corpus_columns_20260911.md）があるため、その sheet_id が
    # CASE_CORPUS_SHEET_ID に貼られる確率は現実的に高い。
    claimed: set[str] = set()
    for item in raw:
        sheet_id = str(item.get("sheet_id", ""))
        sheet_id_env = str(item.get("sheet_id_env", "") or "").strip() or None
        if _is_placeholder(sheet_id):
            # ID 後入れ（2026-09-11・B-10）: env が入っていればそれを実 ID として採用する。
            resolved = _resolve_env_id(sheet_id_env)
            if resolved is not None:
                sheet_id = resolved
            elif sheet_id_env:
                # 「env で後から入れる」と yaml で宣言済のソースは、既定（skip_placeholder=
                # True・本番 scripts/ingest_sources.py:95 が使う経路）では warning + skip。
                # strict mode（skip_placeholder=False）は **tests からしか呼ばれない検査
                # モード**（tests/ingest/test_loader.py）なので、ここは従来どおり raise に
                # 戻す。sheet_id_env を 1 行足すだけで貼り忘れプレースホルダが永久に
                # strict 検査を素通りする逃げ道を yaml 側に作らない（逃げ道はテスト側に置く）。
                if not skip_placeholder:
                    raise ValueError(
                        "gsheets entry declares sheet_id_env but it is unset: "
                        f"{sheet_id_env!r} (sheet_name={item.get('sheet_name')!r})"
                    )
                logger.warning(
                    "ingest_sources_skip_unconfigured_env",
                    section="gsheets",
                    sheet_name=item.get("sheet_name"),
                    sheet_id_env=sheet_id_env,
                )
                continue
            elif skip_placeholder:
                logger.warning(
                    "ingest_sources_skip_placeholder", section="gsheets", sheet_id=sheet_id
                )
                continue
            else:
                raise ValueError(f"gsheets entry has placeholder sheet_id: {sheet_id!r}")
        if sheet_id in claimed:
            # 重複は **採用しない**（既存エントリを勝たせる）。env 由来の貼り間違いが
            # 既存 document を書き潰す唯一の経路なので、ここで fail-closed にする。
            logger.error(
                "ingest_sources_duplicate_sheet_id",
                section="gsheets",
                sheet_name=item.get("sheet_name"),
                sheet_id_env=sheet_id_env,
                sheet_id_ref=f"{sheet_id[:6]}…",
            )
            if not skip_placeholder:
                raise ValueError(f"gsheets entry has duplicate sheet_id: {sheet_id!r}")
            continue
        claimed.add(sheet_id)
        tabs_raw: list[dict[str, Any]] = item.get("tabs", []) or []
        tabs = tuple(t for t in (_parse_gsheet_tab(r) for r in tabs_raw) if t is not None)
        if tabs_raw and not tabs:
            # 宣言された全タブが未確定 / 不正で落ちた＝どのタブを読むか決まっていない。
            # gid 0（= 先頭タブ）へ黙って fallback するとマスター表ではないタブが
            # case_corpus として丸ごと取り込まれるので、spec ごと採用しない。
            logger.error(
                "ingest_sources_skip_spec_no_resolvable_tab",
                section="gsheets",
                sheet_name=item.get("sheet_name"),
            )
            claimed.discard(sheet_id)
            continue
        out.append(
            GSheetSpec(
                sheet_id=sheet_id,
                sheet_name=str(item.get("sheet_name", "")),
                description=str(item.get("description", "")),
                tabs=tabs,
                row_unit=bool(item.get("row_unit", True)),
                extra_metadata=dict(item.get("extra_metadata", {}) or {}),
                sheet_id_env=sheet_id_env,
            )
        )
    return tuple(out)


def _parse_gsheet_tab(raw: dict[str, Any]) -> GSheetsTabSpec | None:
    """gsheets[].tabs[] の 1 件。``gid_env`` を宣言したタブは env が唯一の gid 源。

    ``gid_env`` を宣言している場合、env 未設定 / 非数値なら yaml の gid へ **fallback
    しない**（None を返してタブごと落とす）。fallback すると CASE_CORPUS_SHEET_GID の
    typo（例「#gid=123」を貼る）で gid 0 のタブ＝マスター表ではないタブが case_corpus
    として丸ごと取り込まれ、事例として朝の DM に出る。
    """
    gid_env = str(raw.get("gid_env", "") or "").strip() or None
    gid = int(raw.get("gid", 0))
    if gid_env:
        resolved = _resolve_env_id(gid_env)
        if resolved is None:
            logger.warning(
                "ingest_sources_skip_unconfigured_gid_env",
                gid_env=gid_env,
                tab_name=raw.get("tab_name"),
            )
            return None
        try:
            gid = int(resolved)
        except ValueError:
            logger.error(
                "ingest_sources_invalid_gid_env",
                gid_env=gid_env,
                tab_name=raw.get("tab_name"),
            )
            return None
    return GSheetsTabSpec(gid=gid, tab_name=str(raw.get("tab_name", "")))
