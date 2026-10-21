"""直前に表示した資料を、同じ本人の会話で再認可して返す。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

import structlog

from teamagent.adapters.document_reference_store import DocumentReferenceStore
from teamagent.skills._shared.drive_slack_delivery import extract_drive_file_id
from teamagent.skills._shared.private_surface import is_private_surface
from teamagent.skills._shared.source_url import hit_doc_url
from teamagent.skills.base import SkillContext

if TYPE_CHECKING:
    from teamagent.mcp_gateway.caller_claim import VerifiedCallerClaim
    from teamagent.skills.search.schema import DocumentReference, SearchOutput

logger = structlog.get_logger(__name__)
_FOLLOWUP = re.compile(
    r"^(?:その|さっきの|直前の)(?:事例の)?(?:資料|ファイル)(?:の(?:URL|リンク))?"
    r"(?:も|を|も一緒に)?(?:出して|見せて|送って|教えて|共有して|ください|下さい)"
    r"(?:ください|下さい)?[。！!？?\s]*$",
    re.IGNORECASE,
)
_MISSING = "直前に表示した資料の閲覧権限を確認できませんでした。資料名を指定してください。"


def is_followup(query: str) -> bool:
    return bool(_FOLLOWUP.fullmatch(query.strip()))


def _key(ctx: SkillContext, claim: VerifiedCallerClaim | None) -> str | None:
    from teamagent.skills.search.drive_first import enabled

    if claim is None or not enabled(ctx.metadata) or not is_private_surface(claim.channel_id, True):
        return None
    identity = (
        claim.slack_team_id,
        claim.slack_user_id,
        str(ctx.metadata["user_email"]).lower(),
        claim.channel_id,
        claim.thread_ts or "",
    )
    return hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()


def remember(
    skill: Any, output: Any, ctx: SkillContext, claim: VerifiedCallerClaim | None, *, query: str
) -> bool | None:
    key = _key(ctx, claim)
    if key is not None:
        refs = getattr(output, "displayed_refs", None) or []
        if not is_followup(query):
            try:
                DocumentReferenceStore(_search_skill(skill)._pgvector).save(
                    ctx.metadata["user_email"],
                    key,
                    [
                        ref.model_dump() if hasattr(ref, "model_dump") else dict(ref)
                        for ref in refs[:5]
                    ],
                )
            except Exception as exc:
                logger.warning(
                    "document_followup_save_failed",
                    request_id=ctx.request_id,
                    error=type(exc).__name__,
                )
                return False
        return bool(refs)
    return None


def _search_skill(skill: Any) -> Any:
    if hasattr(skill, "_pgvector"):
        return skill
    return skill._search or skill._build_search()


def _slack_authorized(skill: Any, ref: DocumentReference, ctx: SkillContext) -> bool:
    from teamagent.adapters.slack_user_reader import SlackUserReader
    from teamagent.skills.attachment_assist.permalink import parse_permalink

    target, _ = parse_permalink(hit_doc_url({"source_uri": ref.source_uri}) or ref.url or "")
    store = getattr(skill, "_slack_store", None)
    if target is None or store is None:
        return False
    token = store.get(ctx.metadata["user_email"])
    if token is None or not token.access_token:
        return False
    factory = getattr(skill, "_slack_reader_factory", None) or SlackUserReader.from_user_token
    result = factory(token.access_token).read_message_checked(
        target.channel_id, target.ts, ctx.request_id, thread_ts=target.thread_ts
    )
    if result.error or not result.messages:
        return False
    file_ids = set(ref.slack_file_ids)
    if ref.slack_file_id:
        file_ids.add(ref.slack_file_id)
    current_ids = {str(f.get("id") or "") for message in result.messages for f in message.files}
    if ref.kind == "slack_attachment" and not file_ids:
        return False
    return not file_ids or file_ids <= current_ids


def reauthorize(
    skill: Any, refs: list[DocumentReference], ctx: SkillContext
) -> list[DocumentReference]:
    from teamagent.skills.search.fanout_sources import rls_connection

    search = _search_skill(skill)
    db_refs = [
        ref
        for ref in refs
        if ref.kind not in {"slack_attachment", "slack_conversation"}
        and getattr(ref, "file_record_number", None) is None
    ]
    rows: list[dict[str, Any]] = []
    if db_refs:
        with rls_connection(
            search._pgvector, ctx.metadata, app_role=getattr(search, "_app_role", "teamagent_app")
        )() as conn:
            rows = search._pgvector.list_authorized_document_references(
                conn,
                document_ids=[ref.document_id for ref in db_refs if ref.document_id],
                drive_file_ids=[ref.drive_file_id for ref in db_refs if ref.drive_file_id],
                source_uris=[ref.source_uri for ref in db_refs if ref.source_uri],
                request_id=ctx.request_id,
            )
    allowed: list[DocumentReference] = []
    for ref in refs:
        if ref.kind in {"slack_attachment", "slack_conversation"}:
            if _slack_authorized(search, ref, ctx):
                allowed.append(ref)
        elif ref.kind == "drive":
            if getattr(ref, "file_record_number", None) is not None:
                from teamagent.skills.search.saved_file_refs import reauthorize_saved_reference

                if reauthorize_saved_reference(search, ref, ctx):
                    allowed.append(ref)
                continue
            if ref.drive_file_id and any(
                (
                    row.get("source_type") == "gdrive"
                    and ref.drive_file_id
                    in {
                        extract_drive_file_id(str(row.get("source_uri") or "")),
                        row.get("external_id"),
                    }
                )
                or (
                    bool(ref.document_id)
                    and str(row.get("document_id") or row.get("id")) == ref.document_id
                    and row.get("source_uri") == ref.source_uri
                    and extract_drive_file_id(ref.source_uri) == ref.drive_file_id
                )
                for row in rows
            ):
                allowed.append(ref)
        elif ref.document_id and any(
            str(row.get("document_id") or row.get("id")) == ref.document_id for row in rows
        ):
            allowed.append(ref)
    return allowed


def restore(
    skill: Any, query: str, ctx: SkillContext, claim: VerifiedCallerClaim | None
) -> SearchOutput | None:
    key = _key(ctx, claim)
    if key is None or not is_followup(query):
        return None
    from teamagent.skills.search.answer_sections import _safe_url
    from teamagent.skills.search.drive_first import label
    from teamagent.skills.search.schema import DocumentReference, SearchOutput

    saved = []
    try:
        saved = DocumentReferenceStore(_search_skill(skill)._pgvector).get(
            ctx.metadata["user_email"], key
        )
        refs = (
            reauthorize(skill, [DocumentReference.model_validate(ref) for ref in saved], ctx)
            if saved
            else []
        )
    except Exception as exc:
        logger.warning("document_followup_reauthorization_failed", error=type(exc).__name__)
        refs = []
    lines = []
    for ref in refs:
        title = re.sub(r"[\[\]()<>@\r\n]", " ", ref.title or "資料").strip()
        # URL は検索時に決定した同じ正本だけを返し、別資料を選び直さない。
        url = _safe_url(ref.url)
        lines.append(f"- [{title}]({url})（{label(ref)}）" if url else f"- {title}（{label(ref)}）")
    answer = "\n".join(lines) if lines else _MISSING
    if refs and len(refs) != len(saved):
        answer += "\n閲覧権限を確認できた資料だけを示しています。"
    return SearchOutput(
        answer=answer,
        total_cost_usd=0,
        found=bool(refs),
        displayed_refs=refs,
        answer_format="list",
        direct_answer=answer,
        direct_answer_kind="list_fast",
    )
