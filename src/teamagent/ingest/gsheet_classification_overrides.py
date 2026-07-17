"""監査済み Google Sheets 行の分類ドリフトを exact ID で防ぐ。

Google Form の行は Haiku で ``cls_*`` を再分類するため、同じ入力でもモデル更新や
非決定性により公開プロパティが変わり得る。このモジュールは、人が本文と旧公開値を
突合して誤分類と確定した行だけを ``<sheet_id>:<gid>:<row_idx>`` で固定する。

適用対象は業種 1 軸だけ。``cls_project`` / ``cls_doc_type`` / ``cls_solution`` など
他の分類、人間入力メタ、本文、日時には触れない。検索の後方互換キー ``industry`` は
``DocClassification.as_metadata`` と同じ契約で ``cls_industry`` と同値に保つ。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

_KNOWLEDGE_SHEET_ID = "1jRmoUPo0kAhOGA6secGcwGHILH5LHt7lYvEuxJ5uupo"
_KNOWLEDGE_TAB_GID = 278789217
_EXTERNAL_ID_RE = re.compile(r"^(?P<sheet>[A-Za-z0-9_-]+):(?P<gid>[0-9]+):(?P<row>[0-9]+)$")
_MAX_CLASSIFICATION_VALUE_LENGTH = 80


@dataclass(frozen=True)
class GSheetIndustryOverride:
    """1 行分の監査済み業種 override."""

    external_id: str
    expected_client_name: str
    cls_industry: str
    rationale: str


_RAW_INDUSTRY_OVERRIDES: tuple[GSheetIndustryOverride, ...] = (
    GSheetIndustryOverride(
        external_id=f"{_KNOWLEDGE_SHEET_ID}:{_KNOWLEDGE_TAB_GID}:44",
        expected_client_name="ポート",
        cls_industry="人材",
        rationale="本文は就活エージェントの申込獲得施策。IT への再分類は監査で誤りと確認。",
    ),
    GSheetIndustryOverride(
        external_id=f"{_KNOWLEDGE_SHEET_ID}:{_KNOWLEDGE_TAB_GID}:63",
        expected_client_name="アイホン",
        cls_industry="電子機器",
        rationale="インターホン関連サービスの提案。その他 への再分類は情報量を失う。",
    ),
    GSheetIndustryOverride(
        external_id=f"{_KNOWLEDGE_SHEET_ID}:{_KNOWLEDGE_TAB_GID}:123",
        expected_client_name="東京ドーム",
        cls_industry="エンタテインメント",
        rationale="東京ドーム向け施策。既存の具体業種を空値にする再分類は明確な退行。",
    ),
)


def _normalized_identity(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _validate_industry_overrides(
    entries: Sequence[GSheetIndustryOverride],
) -> Mapping[str, GSheetIndustryOverride]:
    """設定を import 時に fail-loud 検証し、変更不能な exact-ID map にする。"""
    validated: dict[str, GSheetIndustryOverride] = {}
    for entry in entries:
        match = _EXTERNAL_ID_RE.fullmatch(entry.external_id)
        if match is None:
            raise ValueError(f"invalid gsheets override external_id: {entry.external_id!r}")
        if (
            match.group("sheet") != _KNOWLEDGE_SHEET_ID
            or int(match.group("gid")) != _KNOWLEDGE_TAB_GID
            or int(match.group("row")) < 2
        ):
            raise ValueError(
                "gsheets industry override must target the audited knowledge form tab: "
                f"{entry.external_id!r}"
            )
        if entry.external_id in validated:
            raise ValueError(f"duplicate gsheets override external_id: {entry.external_id!r}")
        for field_name, value in (
            ("expected_client_name", entry.expected_client_name),
            ("cls_industry", entry.cls_industry),
            ("rationale", entry.rationale),
        ):
            if (
                not value
                or value != value.strip()
                or "\x00" in value
                or "\n" in value
                or "\r" in value
            ):
                raise ValueError(f"invalid {field_name} for gsheets override {entry.external_id!r}")
        if len(entry.cls_industry) > _MAX_CLASSIFICATION_VALUE_LENGTH:
            raise ValueError(f"cls_industry is too long for gsheets override {entry.external_id!r}")
        validated[entry.external_id] = entry
    return MappingProxyType(validated)


_INDUSTRY_OVERRIDES = _validate_industry_overrides(_RAW_INDUSTRY_OVERRIDES)


def apply_gsheet_industry_override(
    external_id: str,
    *,
    client_name: str | None,
    classification_metadata: Mapping[str, str],
) -> dict[str, str]:
    """監査済み行だけ業種を上書きし、それ以外の metadata を byte-for-byte 保つ。

    exact ID が一致しても人間入力から導出した取引先名が監査時と違えば、行削除・並べ替え
    などで identity が変わった可能性があるため fail-loud にする。誤った別行へ固定値を
    silently 適用するより、当該 Sheet の ingest を止めて再監査する方が安全である。
    """
    result = dict(classification_metadata)
    override = _INDUSTRY_OVERRIDES.get(external_id)
    if override is None:
        return result

    if _normalized_identity(client_name or "") != _normalized_identity(
        override.expected_client_name
    ):
        raise ValueError(
            "gsheets industry override identity mismatch: "
            f"external_id={external_id!r}, client_name={client_name!r}"
        )

    result["cls_industry"] = override.cls_industry
    # search.filter_industry が読む後方互換キー。DocClassification.as_metadata と同値に固定。
    result["industry"] = override.cls_industry
    return result
