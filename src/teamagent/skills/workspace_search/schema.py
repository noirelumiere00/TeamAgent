"""workspace_search Skill の I/O スキーマ（Pydantic v2）。

⚠️ 戻り値は DLP マスク済みのスニペットのみ（生本文・PII は入れない）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 対応サービス（query→結果が綺麗な2つ。Gmail は mail_constraints、Docs/Sheets/Slides は
# ID 指定の read-by-id なので別経路）。
WORKSPACE_SERVICES = ("calendar", "people")


class WorkspaceSearchInput(BaseModel):
    """本人の Workspace を横断検索する入力。"""

    service: str = Field(description="検索対象: 'calendar'（予定）| 'people'（連絡先）")
    query: str = Field(min_length=1, max_length=200, description="クライアント名・人名などの検索語")
    limit: int = Field(default=10, ge=1, le=50, description="返す最大件数")


class WorkspaceHit(BaseModel):
    """検索ヒット1件（DLP マスク済み）。"""

    kind: str = Field(description="'event' | 'contact'")
    title: str = Field(description="件名/氏名（マスク後）")
    detail: str = Field(description="日時・参加者・組織等（マスク後）")


class WorkspaceSearchOutput(BaseModel):
    """本人 Workspace 検索の結果。生データは含まない。"""

    service: str
    hits: list[WorkspaceHit] = Field(default_factory=list)
    count: int = Field(ge=0)
    owner_masked: str = Field(default="", description="参照した本人（マスク表示）。監査用")
