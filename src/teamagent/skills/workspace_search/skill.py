"""workspace_search Skill 本体。

本人の OAuth トークン（TokenStore）で本人の Google Workspace（カレンダー予定・連絡先）を
取得する。死守ライン：G1 本人限定（ctx.metadata.user_email→TokenStore）・G2 未連携は
fail-closed・G3 DLP マスク・G4 readonly（アダプタが readonly スコープ）。

3 層分離: 本ファイルは Skill 層。google 依存は adapters/ 経由（遅延 import）。
"""

from __future__ import annotations

from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.oauth_token_store import TokenStore
from teamagent.observability import scrub_value
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.workspace_search.schema import (
    WORKSPACE_SERVICES,
    WorkspaceHit,
    WorkspaceSearchInput,
    WorkspaceSearchOutput,
)

logger = structlog.get_logger(__name__)


def _mask(text: str) -> str:
    """DLP: PII/シークレットをマスク（G3）。"""
    return str(scrub_value(text or ""))


def _mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


@register
class WorkspaceSearchSkill(BaseSkill[WorkspaceSearchInput, WorkspaceSearchOutput]):
    """本人の Workspace（予定・連絡先）を本人 OAuth で検索する Skill（per-user・DLP）。"""

    name: ClassVar[str] = "workspace_search"
    description: ClassVar[str] = (
        "本人の Google Workspace（カレンダー予定・連絡先）を本人 OAuth で検索する。"
        "本人が /teamagent connect で連携済みの時のみ使える（未連携は不可）。生データは返さない。"
    )
    input_schema: ClassVar[type[BaseModel]] = WorkspaceSearchInput
    output_schema: ClassVar[type[BaseModel]] = WorkspaceSearchOutput

    def __init__(self, token_store: TokenStore | None = None) -> None:
        self._token_store = token_store

    def run(self, input: WorkspaceSearchInput, ctx: SkillContext) -> WorkspaceSearchOutput:
        log = ctx.bind_logger(self.name)
        log.info("workspace_search_start", service=input.service)

        # G1: 本人限定（fail-closed）。
        requester = ctx.metadata.get("user_email")
        if not requester or not isinstance(requester, str):
            raise PermissionError("workspace_search は本人 user_email が必須です（fail-closed）")
        requester = requester.strip()

        # G2: 本人が未連携なら fail-closed。
        if self._token_store is None:
            raise PermissionError("TokenStore が未設定です（workspace_search は連携前提）")
        token = self._token_store.get(requester)
        if token is None:
            raise PermissionError(
                "Workspace 未連携です（/teamagent connect で自分の Google を認可してください）"
            )

        if input.service == "calendar":
            from teamagent.adapters.gcalendar_client import GCalendarClient

            events = GCalendarClient.from_user_token(token).list_events(
                ctx.request_id, query=input.query, max_results=input.limit
            )
            hits = [
                WorkspaceHit(
                    kind="event",
                    title=_mask(e.summary),
                    detail=_mask(f"{e.start}〜{e.end} 参加:{','.join(e.attendees)}"),
                )
                for e in events
            ]
        elif input.service == "people":
            from teamagent.adapters.gpeople_client import GPeopleClient

            contacts = GPeopleClient.from_user_token(token).search_contacts(
                input.query, ctx.request_id, page_size=input.limit
            )
            hits = [
                WorkspaceHit(
                    kind="contact",
                    title=_mask(c.display_name),
                    detail=_mask(f"{c.organization} {','.join(c.emails)}"),
                )
                for c in contacts
            ]
        else:
            raise ValueError(
                f"未対応のサービスです: {input.service!r}（対応: {WORKSPACE_SERVICES}）"
            )

        log.info("workspace_search_done", service=input.service, count=len(hits))
        return WorkspaceSearchOutput(
            service=input.service,
            hits=hits,
            count=len(hits),
            owner_masked=_mask_email(requester),
        )
