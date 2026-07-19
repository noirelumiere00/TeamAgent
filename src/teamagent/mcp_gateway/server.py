"""TeamAgent を spec-MCP サーバとして公開する薄いラッパ（自律外殻 ⟷ ドメイン能力の境界）。

OpenClaw 等の MCP クライアント（＝自律オーケストレーションの外殻）が、TeamAgent のドメイン能力を
tool として叩くための境界。RLS 行権限・per-user OAuth・fail-closed・反ハルシは本サーバ
（＝境界の内側 Python）で死守し、外殻はここを越えて RDS/Secrets/Google に直接触れない。

セキュリティ不変条件（WS-C 強化版）:
- **STRICT モード（resolver 注入＝本番）**：OpenClaw の ingress plugin が Slack event の
  user/team/channel と tool/全引数を one-use HMAC claim に束縛する。LLM が申告した
  ``_user_context.slack_user_id`` 単体は認可 identity として一切採らない。
- MCP は署名・audience・iat/exp・request hash・nonce replay・申告ID一致を検証後にだけ
  Slack resolver を呼ぶ。email/groups/role はサーバ側で解決し、外殻申告は全破棄する。
- claim 欠落/改ざん/replay、Slack ID 不一致、team 不一致、resolver 障害/未知/guest/stranger は
  会社共有を含む全本番モードで fail-closed（ダウングレード不可）。
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
import time
import uuid
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
    shared_company_domains_from_env,
)
from teamagent.mcp_gateway.caller_claim import (
    CALLER_CLAIM_FIELD,
    CallerClaimError,
    CallerClaimVerifier,
    VerifiedCallerClaim,
)
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import SkillContext

logger = structlog.get_logger(__name__)

# 呼び出し元（外殻）が RLS 用コンテキストを渡す予約キー。skill 入力とは分離する。
USER_CONTEXT_KEY = "_user_context"

# L2 適応オーケストレーター（run_sdk_agent）を 1 つの MCP tool として露出する時の名前。
# `USE_AGENT_ORCHESTRATOR=1` の時だけ list/call に出す（既定 OFF・dark）。
RUN_AGENT_TOOL_NAME = "run_agent"

# search ツールの応答に「ブラウザ/グラフで開く」Web UI リンクを差し込む対象の tool 名。
# 注入は本ゲート層でのみ行い、SearchSkill / skills/search/schema.py は不変に保つ
# （並行編集との衝突回避）。CONNECT_BASE_URL 未設定なら一切載せない（壊れたリンクを出さない）。
SEARCH_TOOL_NAME = "search"


def _envflag(name: str, default: str = "false") -> bool:
    """ENV を bool に変換（"1"/"true"/"yes" を True とみなす・factory._envflag と同流儀）。

    末尾/先頭の空白は ``.strip()`` で除去する。task-def の env に紛れた末尾改行や
    スペース付き ``"1 "`` でも意図どおり ON 判定されるようにする（フラグの取りこぼし防止）。
    """
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def _augment_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """入力スキーマに RLS 用の ``_user_context`` を足す（外殻が身元を渡す口）。"""
    out = dict(schema)
    props = dict(out.get("properties") or {})
    props[USER_CONTEXT_KEY] = {
        "type": "object",
        "description": (
            "RLS 用の呼び出し元コンテキスト。本番では trusted OpenClaw ingress plugin が"
            "Slack event由来値とone-use署名claimを注入する。LLM申告値単体は認可に使わない。"
        ),
        "properties": {
            "slack_user_id": {
                "type": "string",
                "description": "Slack user_id申告値。署名claimのevent userと一致した時だけ有効。",
            },
            "slack_team_id": {
                "type": "string",
                "description": "trusted pluginが注入するSlack workspace team_id。",
            },
            CALLER_CLAIM_FIELD: {
                "type": "string",
                "description": (
                    "trusted pluginがtool実行直前に注入するone-use署名claim。"
                    "モデルや利用者が作成してはならない。"
                ),
            },
            # 後方互換（LEGACY=テスト/PoC のみ有効。STRICT では破棄される）。
            "user_email": {"type": "string"},
            "user_groups": {"type": "array", "items": {"type": "string"}},
            "user_role": {"type": "string"},
            # 配信先ルーティング hint（identity ではない＝RLS/認可には一切使わない）。
            # チャンネル/スレッド発の依頼で、skill が「そのスレッドに添付」するために使う。
            "channel_id": {
                "type": "string",
                "description": "依頼が発せられた Slack channel_id（配信ルーティング用・任意）。",
            },
            "thread_ts": {
                "type": "string",
                "description": "親メッセージの ts（スレッド配信用・任意）。",
            },
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


def _run_agent_tool_def() -> Tool:
    """L2 オーケストレーター（run_agent）の MCP Tool 定義。"""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "エージェントに与える調査/提案ゴール（自然文）。",
            },
        },
        "required": ["goal"],
    }
    return Tool(
        name=RUN_AGENT_TOOL_NAME,
        description=(
            "L2 適応オーケストレーター。goal を受け取り、search/clientkarte/proposal_* 等の "
            "L1 ツールを自律的に複数ステップ呼び出して最終提案をまとめる"
            "（USE_AGENT_ORCHESTRATOR=1 の時だけ露出）。"
        ),
        inputSchema=_augment_schema(schema),
    )


def list_all_tool_defs(specs: list[ToolSpec], *, enable_orchestrator: bool) -> list[Tool]:
    """L1 ツール定義 + （有効時のみ）L2 run_agent 定義を返す。"""
    defs = list_tool_defs(specs)
    if enable_orchestrator:
        defs.append(_run_agent_tool_def())
    return defs


def _domain_of(email: str | None) -> str | None:
    """email のドメイン部（監査ログ用・平文 email は出さない）。"""
    if email and "@" in email:
        return email.split("@", 1)[1]
    return None


def _inject_search_web_links(data: dict[str, Any]) -> None:
    """search 応答に Web UI リンク（web_url/graph_url）を *この場で* 差し込む（破壊的・in-place）。

    URL 組み立ては knowledge_search_url skill と同一の真実源（build_search_web_links）に委譲。
    CONNECT_BASE_URL 未設定なら空 dict が返り、キーを一切足さない＝壊れた相対リンクは出さない。
    SearchSkill / skills/search/schema.py は不変（注入はこのゲート層だけで完結）。

    v0.3 Task6: ``USE_AILAVAULT_DEEPLINKS=1``（既定 OFF・§10 E1-2）のとき、追加で
    AiLaVault（/app）へのディープリンクも注入する:
      - トップレベル ``app_url``: /app そのもの
      - 各 hit の ``app_client_url``: hit に client_name があるときだけ ``/app#client:<名前>``
    フラグ既定 OFF の理由: リンク先の app.html 側ハッシュ展開 JS（別デプロイ・repo 外生成器）
    が先に本番へ出ていないと、リンクは開くが該当ノートが自動展開されない（実害は無いが
    中途半端な UX になる）ため、両方が揃った時点で人間が ON にする（§10 E1-4）。
    """
    from teamagent.skills.knowledge_search_url.skill import (
        build_app_client_link,
        build_app_url,
        build_search_web_links,
    )

    data.update(build_search_web_links())
    if not _envflag("USE_AILAVAULT_DEEPLINKS"):
        return
    app_url = build_app_url()
    if not app_url:
        return  # CONNECT_BASE_URL 未設定＝壊れたリンクを出さない
    data["app_url"] = app_url
    hits = data.get("hits")
    if not isinstance(hits, list):
        return
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        link = build_app_client_link(str(hit.get("client_name") or ""))
        if link:
            hit["app_client_url"] = link


def _err(message: str, **extra: Any) -> list[TextContent]:
    """構造化エラーを TextContent で返す（サーバ/外殻ループを落とさない）。"""
    payload: dict[str, Any] = {"error": message, **extra}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def _resolve_metadata(
    raw: dict[str, Any],
    *,
    verified_caller: VerifiedCallerClaim | None,
    require_rls: bool,
    identity_resolver: IdentityResolver | None,
    allowed_domains: frozenset[str] | None,
    company_shared_groups: frozenset[str] | None,
    tool: str,
) -> tuple[dict[str, Any], list[TextContent] | None]:
    """RLS メタを決める。返り値 ``(metadata, fail_response)``。fail_response 非 None なら即返す。

    COMPANY_SHARED（会社共有・§G）：署名済み本人を解決できた会社memberだけが共有群を使う。
    STRICT（resolver 有）：署名済みevent userをサーバ側解決し、外殻申告は破棄。
    LEGACY（resolver 無）：テスト/PoC 専用。user_email を使うが role は member 強制。
    """
    slack_user_id = verified_caller.slack_user_id if verified_caller else None
    # 配信先ルーティング hint（identity ではない＝認可/RLS には一切使わない）。
    # knowledge_deliver が「聞かれたチャンネル/スレッドに添付」するのに使う。無ければ DM 配信。
    channel_id = verified_caller.channel_id if verified_caller else raw.get("channel_id")
    thread_ts = verified_caller.thread_ts if verified_caller else raw.get("thread_ts")

    if company_shared_groups is not None:
        # 会社共有モードも「Slack event署名 + exact team + member resolver成功」が必須。
        # 共有groupは会社memberであることを検証した後だけ付与する。
        if raw.get("user_email") or raw.get("user_groups") or raw.get("user_role"):
            logger.warning("identity_spoof_rejected", tool=tool, reason="oc_fields_dropped")
        if verified_caller is None or identity_resolver is None or not slack_user_id:
            logger.warning("identity_spoof_rejected", tool=tool, reason="missing_verified_caller")
            return {}, _err(
                "Caller authorization failed.",
                code="CALLER_IDENTITY_REJECTED",
            )
        try:
            identity = await identity_resolver(slack_user_id)
        except Exception:
            logger.warning("identity_spoof_rejected", tool=tool, reason="resolver_error")
            return {}, _err(
                "Caller authorization failed.",
                code="CALLER_IDENTITY_REJECTED",
            )
        resolved = (
            build_rls_metadata(identity, allowed_domains=allowed_domains) if identity else None
        )
        if not resolved or not resolved.get("user_email"):
            logger.warning("identity_spoof_rejected", tool=tool, reason="resolve_none")
            return {}, _err(
                "Caller authorization failed.",
                code="CALLER_IDENTITY_REJECTED",
            )
        company_meta = company_member_metadata(company_shared_groups)
        meta = {
            **company_meta,
            "user_email": resolved["user_email"],
            "user_groups": sorted(set(company_meta["user_groups"]) | set(resolved["user_groups"])),
            "identity_verified": True,
        }
        logger.info(
            "identity_resolved",
            tool=tool,
            source="company_shared+signed_claim+resolver",
            domain=_domain_of(resolved["user_email"]),
        )
        return {**meta, "channel_id": channel_id, "thread_ts": thread_ts}, None

    if identity_resolver is not None:
        # 外殻が email/groups/role を申告してきたら破棄して警告（攻撃 or バグの早期検知）。
        if raw.get("user_email") or raw.get("user_groups") or raw.get("user_role"):
            logger.warning("identity_spoof_rejected", tool=tool, reason="oc_fields_dropped")
        if verified_caller is None or not slack_user_id:
            if require_rls:
                logger.warning(
                    "identity_spoof_rejected",
                    tool=tool,
                    reason="missing_verified_caller",
                )
                return {}, _err(
                    "Caller authorization failed.",
                    code="CALLER_IDENTITY_REJECTED",
                )
            return no_access_metadata(), None
        try:
            identity = await identity_resolver(slack_user_id)
        except Exception:
            logger.warning("identity_spoof_rejected", tool=tool, reason="resolver_error")
            identity = None
        strict_meta = (
            build_rls_metadata(identity, allowed_domains=allowed_domains) if identity else None
        )
        if strict_meta is None:
            if require_rls:
                logger.warning("identity_spoof_rejected", tool=tool, reason="resolve_none")
                return {}, _err(
                    "Caller authorization failed.",
                    code="CALLER_IDENTITY_REJECTED",
                )
            return no_access_metadata(), None
        logger.info(
            "identity_resolved",
            tool=tool,
            source="resolver",
            domain=_domain_of(strict_meta["user_email"]),
        )
        return {**strict_meta, "channel_id": channel_id, "thread_ts": thread_ts}, None

    # LEGACY モード（resolver 未注入＝テスト/PoC 専用）。本番エントリポイントは resolver 必須。
    email = raw.get("user_email")
    if require_rls and not email:
        logger.warning("mcp_rls_fail_closed", tool=tool)
        return {}, _err(
            "RLS required: _user_context.user_email is missing. "
            "Caller MUST retry with arguments including "
            '_user_context: {"user_email": "<the requester\'s email>"} (LEGACY mode).'
        )
    meta = {
        "user_email": email,
        "user_groups": list(raw.get("user_groups") or []),
        "user_role": "member",  # OC 申告 role は採らない（admin 昇格は legacy でも不可）。
        "identity_verified": False,
        "channel_id": channel_id,
        "thread_ts": thread_ts,
    }
    return meta, None


async def _verify_caller(
    arguments: dict[str, Any],
    *,
    tool: str,
    identity_resolver: IdentityResolver | None,
    company_shared_groups: frozenset[str] | None,
    caller_claim_verifier: CallerClaimVerifier | None,
) -> tuple[VerifiedCallerClaim | None, list[TextContent] | None]:
    """Verify the signed ingress identity before any resolver or company access."""

    protected = identity_resolver is not None or company_shared_groups is not None
    if not protected:
        return None, None
    if caller_claim_verifier is None:
        logger.error("caller_claim_verifier_missing", tool=tool)
        return None, _err(
            "Caller authorization is unavailable.",
            code="CALLER_IDENTITY_CONFIGURATION_ERROR",
        )
    try:
        return await caller_claim_verifier.verify(tool=tool, arguments=arguments), None
    except CallerClaimError as error:
        logger.warning(
            "caller_claim_rejected",
            tool=tool,
            reason=str(error),
        )
        return None, _err(
            "Caller authorization failed.",
            code="CALLER_IDENTITY_REJECTED",
        )


async def dispatch_tool(
    by_name: dict[str, ToolSpec],
    name: str,
    arguments: dict[str, Any],
    *,
    require_rls: bool = True,
    identity_resolver: IdentityResolver | None = None,
    allowed_domains: frozenset[str] | None = None,
    company_shared_groups: frozenset[str] | None = None,
    caller_claim_verifier: CallerClaimVerifier | None = None,
) -> list[TextContent]:
    """1 tool 呼び出しを実行する（身元解決 → 入力検証 → 同期 skill を thread 実行）。

    例外は握って構造化エラーで返す（MCP サーバも外殻のループも落とさない）。
    """
    spec = by_name.get(name)
    if spec is None:
        return _err(f"unknown tool: {name}")

    raw_value = arguments.get(USER_CONTEXT_KEY)
    raw = {} if raw_value is None else raw_value
    if not isinstance(raw, dict):
        return _err("invalid input: _user_context must be an object")
    verified_caller, caller_fail = await _verify_caller(
        arguments,
        tool=name,
        identity_resolver=identity_resolver,
        company_shared_groups=company_shared_groups,
        caller_claim_verifier=caller_claim_verifier,
    )
    if caller_fail is not None:
        return caller_fail
    metadata, fail = await _resolve_metadata(
        raw,
        verified_caller=verified_caller,
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
    # ── 進捗表示（v0.3.1 Task7・ENABLE_PROGRESS_NOTIFY 既定OFF・fail-open）───────────
    # 重いツールの実行前に「📂 資料を検索しています…」等を Slack へ投稿し、完了後（成功/
    # 失敗どちらも finally）に削除する。宛先は raw の channel_id → 無ければ slack_user_id DM。
    # ⚠️ send/clear は latency 計測窓の外に置く（_started はツール実行の直前で取る）＝
    # mcp_tool_usage.latency_ms を Slack 往復で水増ししない（Task10 台帳の検証データを歪めない）。
    from teamagent.mcp_gateway.progress_notify import clear_progress, send_progress

    _progress = await send_progress(name, raw, request_id=ctx.request_id)
    _started = time.perf_counter()
    try:
        # 同期 skill.run（DB I/O 等でブロックする）を thread に逃がしイベントループを塞がない。
        output = await asyncio.to_thread(spec.instantiate().run, skill_input, ctx)
        _elapsed_ms = int((time.perf_counter() - _started) * 1000)
    except Exception as e:
        logger.warning(
            "mcp_tool_error", tool=name, error=type(e).__name__, request_id=ctx.request_id
        )
        return _err(f"{type(e).__name__}: {e}", request_id=ctx.request_id)
    finally:
        await clear_progress(_progress, request_id=ctx.request_id)

    data = output.model_dump() if hasattr(output, "model_dump") else {"result": str(output)}
    # ── ミドルウェア(0): usage 計測（v0.3 Task10・常時ON・PII 無し）────────────────
    # 本番主経路（AiLa→MCP）の tool 使用量がどこにも記録されていなかった穴（監査指摘）を
    # まず構造化ログで塞ぐ（CloudWatch Insights で user 単位/tool 単位に集計可能）。
    # DB 計上（クォータ台帳）は migration 0017 とセットで次段（このログが検証データになる）。
    logger.info(
        "mcp_tool_usage",
        tool=name,
        request_id=ctx.request_id,
        latency_ms=_elapsed_ms,
        # ⚠️ キー名は cost_usd に**しない**こと: cloudwatch_fargate.tf のメトリックフィルタ
        # { $.cost_usd = * } が adapter/skill 層の既存ログと合算して日次コストアラームを
        # 二重〜三重計上に汚染する（レビュー F-1）。usage 集計は専用 Insights クエリで行う。
        tool_cost_usd=float(data.get("total_cost_usd") or 0.0) if isinstance(data, dict) else 0.0,
    )
    # ── 返却前ミドルウェア（順序契約・v0.3 監査 Step4-(a)）────────────────────
    # (1) 長文退避（Task8・USE_PAYLOAD_OFFLOAD 既定OFF）: 切り詰めは注入キーに触れない
    #     よう **リンク注入より先** に行う（逆順だと注入したURLごと切り詰め対象になる）。
    # (2) リンク注入（Task6）: search 応答にだけ Web UI/AiLaVault リンクを差し込む。
    # 将来の usage 計測/quota（Task10）は (0) として tool 実行の前後に入る想定。
    if isinstance(data, dict):
        from teamagent.mcp_gateway.payload_offload import maybe_offload

        data = maybe_offload(name, data, request_id=ctx.request_id)
    if name == SEARCH_TOOL_NAME and isinstance(data, dict):
        _inject_search_web_links(data)
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, default=str))]


async def dispatch_run_agent(
    specs: list[ToolSpec],
    arguments: dict[str, Any],
    *,
    require_rls: bool = True,
    identity_resolver: IdentityResolver | None = None,
    allowed_domains: frozenset[str] | None = None,
    company_shared_groups: frozenset[str] | None = None,
    caller_claim_verifier: CallerClaimVerifier | None = None,
    max_turns: int = 8,
    cost_cap_usd: float = 0.5,
    tool_timeout_s: float = 90.0,
) -> list[TextContent]:
    """L2 オーケストレーター（run_sdk_agent）を 1 回の MCP 呼び出しとして実行する。

    身元解決は dispatch_tool と同じ境界（_resolve_metadata）を通す。L1 tool（specs）を
    そのまま SDK に渡す（run_agent 自身は specs に含まれないので再帰しない）。
    Bedrock/Node CLI を要するライブ実行。例外は構造化エラーで返す（外殻ループを落とさない）。
    """
    raw_value = arguments.get(USER_CONTEXT_KEY)
    raw = {} if raw_value is None else raw_value
    if not isinstance(raw, dict):
        return _err("invalid input: _user_context must be an object")
    verified_caller, caller_fail = await _verify_caller(
        arguments,
        tool=RUN_AGENT_TOOL_NAME,
        identity_resolver=identity_resolver,
        company_shared_groups=company_shared_groups,
        caller_claim_verifier=caller_claim_verifier,
    )
    if caller_fail is not None:
        return caller_fail
    metadata, fail = await _resolve_metadata(
        raw,
        verified_caller=verified_caller,
        require_rls=require_rls,
        identity_resolver=identity_resolver,
        allowed_domains=allowed_domains,
        company_shared_groups=company_shared_groups,
        tool=RUN_AGENT_TOOL_NAME,
    )
    if fail is not None:
        return fail

    goal = arguments.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return _err("invalid input: 'goal' (non-empty string) is required")

    user_email = metadata.get("user_email")
    request_id = f"run-agent-{uuid.uuid4().hex[:12]}"
    try:
        # 遅延 import: claude_agent_sdk(Node CLI) は重く本番ライブ専用。MCP モジュール import を
        # 軽く保つため呼び出し時に import する。SDK 未導入環境の ImportError も握って構造化エラー化
        # する（dispatch の「例外で外殻ループを落とさない」契約を import 失敗でも守る）。
        from teamagent.orchestrator.agent_config import (
            build_orchestrator_system_prompt,
            orchestrator_model_from_env,
        )
        from teamagent.orchestrator.sdk_runner import run_sdk_agent

        result = await run_sdk_agent(
            goal=goal,
            request_id=request_id,
            specs=specs,
            model=orchestrator_model_from_env(),
            system_prompt=build_orchestrator_system_prompt(),
            user_id=user_email,
            ctx_metadata=metadata,
            # 本番のSTRICT/会社共有は署名済みSlack memberをresolverでemailへ解決済み。
            # LEGACYテストだけがemail無しになり得る。
            require_rls=bool(user_email),
            max_turns=max_turns,
            cost_cap_usd=cost_cap_usd,
            tool_timeout_s=tool_timeout_s,
        )
    except Exception as e:
        logger.warning("run_agent_error", error=type(e).__name__, request_id=request_id)
        return _err(f"{type(e).__name__}: {e}", request_id=request_id)

    payload: dict[str, Any] = {
        "answer": result.answer,
        "stopped_reason": result.stopped_reason,
        "is_error": result.is_error,
        "num_turns": result.num_turns,
        "tool_calls": result.tool_calls,
        "session_total_cost_usd": result.session_total_cost_usd,
        "request_id": request_id,
    }
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, default=str))]


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
    """会社共有モード（§G）のドメイン集合。identity の単一真実源に委譲（ingest と同値を保証）。"""
    return shared_company_domains_from_env()


def build_server(
    specs: list[ToolSpec] | None = None,
    *,
    require_rls: bool = True,
    identity_resolver: IdentityResolver | None = None,
    allowed_domains: frozenset[str] | None = None,
    company_shared_groups: frozenset[str] | None = None,
    caller_claim_verifier: CallerClaimVerifier | None = None,
) -> Server:
    """TeamAgent MCP サーバを構築する（specs 省略時は本番ツールを遅延構築）。"""
    if specs is None:
        from teamagent.orchestrator.factory import build_production_tools

        specs = build_production_tools()
    if company_shared_groups is not None and identity_resolver is None:
        raise RuntimeError("company-shared mode requires the Slack identity resolver")
    if (
        identity_resolver is not None or company_shared_groups is not None
    ) and caller_claim_verifier is None:
        raise RuntimeError("signed caller claim verifier is required for Slack identity")
    by_name = {s.name: s for s in specs}
    enable_orchestrator = _envflag("USE_AGENT_ORCHESTRATOR")
    server: Server = Server("teamagent")

    @server.list_tools()
    async def _list() -> list[Tool]:
        return list_all_tool_defs(specs, enable_orchestrator=enable_orchestrator)

    @server.call_tool()
    async def _call(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        # L2: run_agent は specs に無い特別 tool。有効時のみ専用ディスパッチへ。
        if enable_orchestrator and name == RUN_AGENT_TOOL_NAME:
            return await dispatch_run_agent(
                specs,
                arguments,
                require_rls=require_rls,
                identity_resolver=identity_resolver,
                allowed_domains=allowed_domains,
                company_shared_groups=company_shared_groups,
                caller_claim_verifier=caller_claim_verifier,
            )
        return await dispatch_tool(
            by_name,
            name,
            arguments,
            require_rls=require_rls,
            identity_resolver=identity_resolver,
            allowed_domains=allowed_domains,
            company_shared_groups=company_shared_groups,
            caller_claim_verifier=caller_claim_verifier,
        )

    return server


def build_production_server() -> Server:
    """本番用に構築する。会社共有(§G)優先＝`TEAMAGENT_SHARED_COMPANY_DOMAINS` があればそれ、

    無ければ per-user resolver 必須（`SLACK_BOT_TOKEN` 未設定なら fail-closed で起動拒否）。

    §U ハイブリッド: 会社共有モードでも `SLACK_BOT_TOKEN` があれば resolver を併せて渡す。
    search 等は会社共有グループで全社可視のまま、mail_*/morning_digest は resolver が解決した
    本人 user_email で per-user OAuth token を引ける（_resolve_metadata の company_shared 参照）。
    """
    caller_claim_verifier = CallerClaimVerifier.from_env()
    resolver = build_slack_identity_resolver()
    if resolver is None:
        raise RuntimeError("SLACK_BOT_TOKEN is required for caller identity resolution")
    company = company_shared_groups_from_env()
    if company is not None:
        return build_server(
            company_shared_groups=company,
            identity_resolver=resolver,
            allowed_domains=allowed_domains_from_env(),
            caller_claim_verifier=caller_claim_verifier,
        )
    return build_server(
        identity_resolver=resolver,
        allowed_domains=allowed_domains_from_env(),
        caller_claim_verifier=caller_claim_verifier,
    )


async def _amain() -> None:
    server = build_production_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """stdio で MCP サーバを起動する CLI エントリポイント（resolver 必須）。"""
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
