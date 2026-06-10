"""TeamAgent を spec-MCP サーバとして公開する薄いラッパ（自律外殻 ⟷ ドメイン能力の境界）。

OpenClaw 等の MCP クライアント（＝自律オーケストレーションの外殻）が、TeamAgent のドメイン能力を
tool として叩くための境界。RLS 行権限・per-user OAuth・fail-closed・反ハルシは本サーバ
（＝境界の内側 Python）で死守し、外殻はここを越えて RDS/Secrets/Google に直接触れない。

セキュリティ不変条件（WS-C 強化版）:
- **STRICT モード（resolver 注入＝本番）**：外殻は ``_user_context.slack_user_id`` のみ渡す。
  email/groups/role はサーバ側で Slack から解決し、**外殻申告の email/groups/role は一切採らない**。
  slack_user_id 欠落・解決不能・resolver 例外は require_rls 下で fail-closed（ダウングレード不可）。
- ``user_role`` は常にサーバ導出の ``"member"``＝MCP 越しの admin 昇格は構造的に不可能。
- **LEGACY モード（resolver 未注入）**：単体テスト/PoC 専用。本番エントリポイントは resolver 必須で
  起動（``build_slack_identity_resolver`` が None なら起動拒否）。legacy でも role は member 強制。
- 重操作（シート書込/メール下書き/PPTX 確定）の HITL propose→confirm 化は WS-D で別 tool 化する。

3層分離: 本モジュールは runtime 寄りの境界層。adapter は直叩きせず、既存 ToolSpec/Skill 経由で呼ぶ。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import structlog
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from teamagent.identity import (
    IdentityResolver,
    build_rls_metadata,
    company_member_metadata,
    no_access_metadata,
)
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import SkillContext

logger = structlog.get_logger(__name__)

# 呼び出し元（外殻）が RLS 用コンテキストを渡す予約キー。skill 入力とは分離する。
USER_CONTEXT_KEY = "_user_context"


def _augment_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """入力スキーマに RLS 用の ``_user_context`` を足す（外殻が身元を渡す口）。"""
    out = dict(schema)
    props = dict(out.get("properties") or {})
    props[USER_CONTEXT_KEY] = {
        "type": "object",
        "description": (
            "RLS 用の呼び出し元コンテキスト。本番(STRICT)では slack_user_id のみ有効＝サーバ側で"
            "身元解決され権威となる（user_email/user_groups/user_role の外殻申告は無視）。"
        ),
        "properties": {
            "slack_user_id": {
                "type": "string",
                "description": "Slack user_id。本番はこれだけでサーバが email/groups を解決。",
            },
            # 後方互換（LEGACY=テスト/PoC のみ有効。STRICT では破棄される）。
            "user_email": {"type": "string"},
            "user_groups": {"type": "array", "items": {"type": "string"}},
            "user_role": {"type": "string"},
        },
    }
    out["properties"] = props
    return out


def list_tool_defs(specs: list[ToolSpec]) -> list[Tool]:
    """ToolSpec 群を MCP の Tool 定義へ（入力スキーマに _user_context を付与）。"""
    return [
        Tool(
            name=s.name,
            description=s.description,
            inputSchema=_augment_schema(s.json_schema()),
        )
        for s in specs
    ]


def _domain_of(email: str | None) -> str | None:
    """email のドメイン部（監査ログ用・平文 email は出さない）。"""
    if email and "@" in email:
        return email.split("@", 1)[1]
    return None


def _err(message: str, **extra: Any) -> list[TextContent]:
    """構造化エラーを TextContent で返す（サーバ/外殻ループを落とさない）。"""
    payload: dict[str, Any] = {"error": message, **extra}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def _resolve_metadata(
    raw: dict[str, Any],
    *,
    require_rls: bool,
    identity_resolver: IdentityResolver | None,
    allowed_domains: frozenset[str] | None,
    company_shared_groups: frozenset[str] | None,
    tool: str,
) -> tuple[dict[str, Any], list[TextContent] | None]:
    """RLS メタを決める。返り値 ``(metadata, fail_response)``。fail_response 非 None なら即返す。

    COMPANY_SHARED（会社共有・§G）：全員が会社ナレッジを読む。本人IDは認可に使わず監査のみ。
    STRICT（resolver 有）：slack_user_id をサーバ側解決し、外殻申告は破棄。
    LEGACY（resolver 無）：テスト/PoC 専用。user_email を使うが role は member 強制。
    """
    slack_user_id = raw.get("slack_user_id")

    if company_shared_groups is not None:
        # 会社共有モード: 全員が同じ会社ナレッジを見る。OC 申告の email/groups/role は破棄、
        # slack_user_id は「誰が聞いたか」の監査用途のみ（認可には一切使わない）。
        if raw.get("user_email") or raw.get("user_groups") or raw.get("user_role"):
            logger.warning("identity_spoof_rejected", tool=tool, reason="oc_fields_dropped")
        audit_uid = slack_user_id if isinstance(slack_user_id, str) and slack_user_id else None
        logger.info("identity_company_shared", tool=tool, slack_user_id_audit=audit_uid)
        return company_member_metadata(company_shared_groups), None

    if identity_resolver is not None:
        # 外殻が email/groups/role を申告してきたら破棄して警告（攻撃 or バグの早期検知）。
        if raw.get("user_email") or raw.get("user_groups") or raw.get("user_role"):
            logger.warning("identity_spoof_rejected", tool=tool, reason="oc_fields_dropped")
        if not slack_user_id or not isinstance(slack_user_id, str):
            if require_rls:
                logger.warning("identity_spoof_rejected", tool=tool, reason="missing_slack_user_id")
                return {}, _err("RLS required: slack_user_id is missing (fail-closed)")
            return no_access_metadata(), None
        try:
            identity = await identity_resolver(slack_user_id)
        except Exception:
            logger.warning("identity_spoof_rejected", tool=tool, reason="resolver_error")
            identity = None
        meta = build_rls_metadata(identity, allowed_domains=allowed_domains) if identity else None
        if meta is None:
            if require_rls:
                logger.warning("identity_spoof_rejected", tool=tool, reason="resolve_none")
                return {}, _err("RLS required: identity could not be resolved (fail-closed)")
            return no_access_metadata(), None
        logger.info(
            "identity_resolved", tool=tool, source="resolver", domain=_domain_of(meta["user_email"])
        )
        return meta, None

    # LEGACY モード（resolver 未注入＝テスト/PoC 専用）。本番エントリポイントは resolver 必須。
    email = raw.get("user_email")
    if require_rls and not email:
        logger.warning("mcp_rls_fail_closed", tool=tool)
        return {}, _err("RLS required: _user_context.user_email is missing (fail-closed)")
    meta = {
        "user_email": email,
        "user_groups": list(raw.get("user_groups") or []),
        "user_role": "member",  # OC 申告 role は採らない（admin 昇格は legacy でも不可）。
        "identity_verified": False,
    }
    return meta, None


async def dispatch_tool(
    by_name: dict[str, ToolSpec],
    name: str,
    arguments: dict[str, Any],
    *,
    require_rls: bool = True,
    identity_resolver: IdentityResolver | None = None,
    allowed_domains: frozenset[str] | None = None,
    company_shared_groups: frozenset[str] | None = None,
) -> list[TextContent]:
    """1 tool 呼び出しを実行する（身元解決 → 入力検証 → 同期 skill を thread 実行）。

    例外は握って構造化エラーで返す（MCP サーバも外殻のループも落とさない）。
    """
    spec = by_name.get(name)
    if spec is None:
        return _err(f"unknown tool: {name}")

    raw = arguments.get(USER_CONTEXT_KEY) or {}
    metadata, fail = await _resolve_metadata(
        raw,
        require_rls=require_rls,
        identity_resolver=identity_resolver,
        allowed_domains=allowed_domains,
        company_shared_groups=company_shared_groups,
        tool=name,
    )
    if fail is not None:
        return fail

    skill_args = {k: v for k, v in arguments.items() if k != USER_CONTEXT_KEY}
    try:
        skill_input = spec.input_schema(**skill_args)
    except Exception as e:  # 入力検証エラーは構造化で返す
        return _err(f"invalid input: {type(e).__name__}: {e}")

    ctx = SkillContext(user_id=metadata.get("user_email"), metadata=metadata)
    try:
        # 同期 skill.run（DB I/O 等でブロックする）を thread に逃がしイベントループを塞がない。
        output = await asyncio.to_thread(spec.instantiate().run, skill_input, ctx)
    except Exception as e:
        logger.warning(
            "mcp_tool_error", tool=name, error=type(e).__name__, request_id=ctx.request_id
        )
        return _err(f"{type(e).__name__}: {e}", request_id=ctx.request_id)

    data = output.model_dump() if hasattr(output, "model_dump") else {"result": str(output)}
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, default=str))]


def allowed_domains_from_env() -> frozenset[str] | None:
    """``TEAMAGENT_ALLOWED_EMAIL_DOMAINS``（カンマ区切り）の許可ドメイン集合。無指定は None。"""
    raw = os.environ.get("TEAMAGENT_ALLOWED_EMAIL_DOMAINS")
    if not raw:
        return None
    domains = frozenset(d.strip().lower() for d in raw.split(",") if d.strip())
    return domains or None


def build_slack_identity_resolver() -> IdentityResolver | None:
    """``SLACK_BOT_TOKEN`` があれば ``SlackClient.resolve_identity`` を resolver として返す。

    本番エントリポイントはこれが None なら起動拒否（後方互換 LEGACY パスを本番から到達不能化）。
    """
    if not os.environ.get("SLACK_BOT_TOKEN"):
        return None
    from teamagent.adapters.slack_client import SlackClient
    from teamagent.identity import ResolvedIdentity

    client = SlackClient.from_env()

    async def _resolver(slack_user_id: str) -> ResolvedIdentity | None:
        return await client.resolve_identity(slack_user_id)

    return _resolver


def company_shared_groups_from_env() -> frozenset[str] | None:
    """``TEAMAGENT_SHARED_COMPANY_DOMAINS``（カンマ区切り）。設定時は会社共有モード（§G）。"""
    raw = os.environ.get("TEAMAGENT_SHARED_COMPANY_DOMAINS")
    if not raw:
        return None
    groups = frozenset(d.strip().lower() for d in raw.split(",") if d.strip())
    return groups or None


def build_server(
    specs: list[ToolSpec] | None = None,
    *,
    require_rls: bool = True,
    identity_resolver: IdentityResolver | None = None,
    allowed_domains: frozenset[str] | None = None,
    company_shared_groups: frozenset[str] | None = None,
) -> Server:
    """TeamAgent MCP サーバを構築する（specs 省略時は本番ツールを遅延構築）。"""
    if specs is None:
        from teamagent.orchestrator.factory import build_production_tools

        specs = build_production_tools()
    by_name = {s.name: s for s in specs}
    server: Server = Server("teamagent")

    @server.list_tools()
    async def _list() -> list[Tool]:
        return list_tool_defs(specs)

    @server.call_tool()
    async def _call(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        return await dispatch_tool(
            by_name,
            name,
            arguments,
            require_rls=require_rls,
            identity_resolver=identity_resolver,
            allowed_domains=allowed_domains,
            company_shared_groups=company_shared_groups,
        )

    return server


def build_production_server() -> Server:
    """本番用に構築する。会社共有(§G)優先＝`TEAMAGENT_SHARED_COMPANY_DOMAINS` があればそれ、

    無ければ per-user resolver 必須（`SLACK_BOT_TOKEN` 未設定なら fail-closed で起動拒否）。
    """
    company = company_shared_groups_from_env()
    if company is not None:
        return build_server(company_shared_groups=company)
    resolver = build_slack_identity_resolver()
    if resolver is None:
        raise RuntimeError(
            "TEAMAGENT_SHARED_COMPANY_DOMAINS も SLACK_BOT_TOKEN も未設定です。"
            "会社共有モードか本人解決 resolver のいずれかが必須です（fail-closed）"
        )
    return build_server(identity_resolver=resolver, allowed_domains=allowed_domains_from_env())


async def _amain() -> None:
    server = build_production_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """stdio で MCP サーバを起動する CLI エントリポイント（resolver 必須）。"""
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
