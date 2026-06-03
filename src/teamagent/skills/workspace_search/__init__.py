"""workspace_search Skill: 本人の Google Workspace を本人 OAuth で横断検索する。

per-user OAuth（TokenStore に保存された本人 refresh token）で、本人のカレンダー予定・
連絡先を取得する。本人未連携なら fail-closed。生データは DLP マスクして返す（G3）。
設計: docs/poc/workspace_integration_design.md §6。
"""

from teamagent.skills.workspace_search.schema import (
    WorkspaceHit,
    WorkspaceSearchInput,
    WorkspaceSearchOutput,
)
from teamagent.skills.workspace_search.skill import WorkspaceSearchSkill

__all__ = [
    "WorkspaceHit",
    "WorkspaceSearchInput",
    "WorkspaceSearchOutput",
    "WorkspaceSearchSkill",
]
