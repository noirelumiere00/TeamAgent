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
from collections.abc import Callable
from typing import Any, cast

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
from teamagent.runtime.usage_recorder import UsageEvent, UsageRecorder
from teamagent.skills._shared.connect_intent import (
    ConnectIntent,
    detect_connect_intent_in_args,
)
from teamagent.skills.base import SkillContext

# 二段返しの契約定数だけを持つ軽量モジュール（boto3/psycopg を引かない）。
from teamagent.skills.search.two_stage import TWO_STAGE_CTX_KEY

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

# 「連携」依頼の決定論分岐で寄せ先にする tool 名。露出していない環境（USE_OAUTH_CONNECT_TOOL
# 未設定）では by_name に居ないので、その場合は寄せずに通常ディスパッチへ落とす。
OAUTH_CONNECT_TOOL_NAME = "oauth_connect"

# submit 応答を返した後も MCP process 内で完了を待つ対象。SQS/DynamoDB/worker の契約は
# 変えず、それぞれの status skill を通常どおり呼んで利用者向けサマリへ整形する。
_ASYNC_JOB_TOOLS = frozenset({"tiktok_acquire", "proposal_builder_submit"})

# usage_events 記録器は本番 MCP プロセス内で 1 つだけ遅延生成する。初期化失敗時の None も
# キャッシュし、env 不足等を各 tool 呼び出しで繰り返さない（利用者処理は常に fail-open）。
_USAGE_RECORDER_UNSET = object()
_usage_recorder_singleton: UsageRecorder | object | None = _USAGE_RECORDER_UNSET

# fire-and-forget task は完了まで強参照を保持する。done callback で例外も回収するため、
# recorder の失敗が MCP 応答や event loop の未回収例外へ波及しない。
_usage_record_tasks: set[asyncio.Future[Any]] = set()


def _envflag(name: str, default: str = "false") -> bool:
    """ENV を bool に変換（"1"/"true"/"yes" を True とみなす・factory._envflag と同流儀）。

    末尾/先頭の空白は ``.strip()`` で除去する。task-def の env に紛れた末尾改行や
    スペース付き ``"1 "`` でも意図どおり ON 判定されるようにする（フラグの取りこぼし防止）。
    """
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def _usage_recorder() -> UsageRecorder | None:
    """usage_events 記録器を遅延生成する。初期化失敗も None としてキャッシュする。"""
    global _usage_recorder_singleton

    if _usage_recorder_singleton is _USAGE_RECORDER_UNSET:
        try:
            from teamagent.adapters.pgvector_client import PgVectorClient

            _usage_recorder_singleton = UsageRecorder(
                PgVectorClient.from_env(), app_role="teamagent_app"
            )
        except Exception as exc:
            _usage_recorder_singleton = None
            logger.warning("usage_recorder_init_failed", error=type(exc).__name__)
    return cast(UsageRecorder | None, _usage_recorder_singleton)


def _usage_record_done(task: asyncio.Future[Any]) -> None:
    """完了 task の参照と例外を回収する（dispatch へは伝播させない）。"""
    _usage_record_tasks.discard(task)
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    except Exception:
        return
    if error is not None:
        logger.warning("usage_event_record_failed", error=type(error).__name__)


async def _record_usage_event(event: UsageEvent) -> None:
    """遅延初期化を含む DB 記録を fire-and-forget task の内側で行う。"""
    recorder = _usage_recorder()
    if recorder is not None:
        await recorder.record(event)


def _record_usage(
    *,
    request_id: str,
    skill: str,
    user_email: str | None,
    user_id: str | None,
    cost_usd: float,
    latency_ms: int,
    skill_args: dict[str, Any],
    status: str = "ok",
    error_code: str | None = None,
) -> None:
    """MCP 利用を非同期記録へ渡す。入力本文は非空 ``query`` だけを採る。"""
    if _envflag("USAGE_EVENTS_DISABLE"):
        return

    try:
        # query 以外の引数（メール本文等）は usage_events へ絶対に持ち込まない。
        query = skill_args.get("query")
        query_text = query if isinstance(query, str) and query else None

        event = UsageEvent(
            request_id=request_id,
            skill=skill,
            status=status,
            user_email=user_email,
            user_id=user_id,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            error_code=error_code,
            query_chars=len(query_text) if query_text is not None else None,
            query_text=query_text,
            via="mcp",
        )
        # singleton 初期化と DB 書込を task 内へ送り、dispatch は完了を await しない。
        loop = asyncio.get_running_loop()
        task = loop.create_task(_record_usage_event(event))
        _usage_record_tasks.add(task)
        task.add_done_callback(_usage_record_done)
    except Exception as exc:
        # recorder double の同期例外や task 生成失敗も利用者応答には影響させない。
        logger.warning(
            "usage_event_schedule_failed",
            request_id=request_id,
            error=type(exc).__name__,
        )


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
    # ⚠️ ``_user_context`` を **required に入れてはならない**（2026-08-26 本番全ツール障害）。
    #
    # かつてここで required へ注入していたが、OpenClaw のクライアント側引数検証
    # （validateToolArguments）は caller-identity plugin の注入（execute 内側の
    # before_tool_call）**より前**に走る。つまり ``_user_context`` は plugin が後から
    # 足す設計なのに、モデルが省略した時点で required 違反となり、tools/call が
    # ワイヤに出る前に全ツールが死ぬ（OC 実物リプレイで旧 40/40 PASS vs
    # required 注入後 0/44 PASS を実測）。properties への注入は宣言として無害かつ
    # 有益なので残すが、required 化は同じ轍を踏まないこと。
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
    Aico Vault（/app）へのディープリンクも注入する:
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


def _format_tiktok_completion(output: Any) -> str:
    failed = output.status == "failed"
    lines = [
        "❌ TikTok取得に失敗しました。" if failed else "✅ TikTok取得が完了しました。",
        f"job_id: `{output.job_id}`",
    ]
    if output.message:
        lines.append(output.message)
    if output.error_code:
        lines.append(f"error_code: `{output.error_code}`")
    if output.counts:
        counts = "、".join(f"{key}={value}" for key, value in output.counts.items())
        lines.append(f"件数: {counts}")
    if output.videos:
        downloaded = sum(1 for video in output.videos if video.get("downloaded"))
        lines.append(f"動画: {downloaded}/{len(output.videos)}本取得")
    if output.posts_json_url:
        lines.append(f"投稿データ: {output.posts_json_url}")
    return "\n".join(lines)


def _format_proposal_completion(output: Any) -> str:
    failed = output.status == "failed"
    lines = [
        "❌ 提案書生成に失敗しました。" if failed else "✅ 提案書生成が完了しました。",
        f"job_id: `{output.job_id}`",
    ]
    result_message = output.result_message or output.message
    if result_message:
        lines.append(result_message)
    if output.error_code:
        lines.append(f"error_code: `{output.error_code}`")
    if output.proposal_status:
        lines.append(f"結果: {output.proposal_status}")
    if output.filled_count is not None and output.skipped_count is not None:
        lines.append(f"反映: {output.filled_count}件 / スキップ: {output.skipped_count}件")
    if output.pptx_url:
        lines.append(f"提案資料: {output.pptx_url}")
    return "\n".join(lines)


def _build_async_job_poll(
    tool: str,
    job_id: str,
    ctx: SkillContext,
) -> Callable[[], tuple[str, str]]:
    """対象 job の status skill を呼ぶ poll closure を作る（初期化も通知 thread 内）。"""
    poll_ctx = SkillContext(
        request_id=ctx.request_id,
        user_id=ctx.user_id,
        metadata=dict(ctx.metadata),
    )
    status_skill: Any = None

    def _poll() -> tuple[str, str]:
        nonlocal status_skill
        if tool == "tiktok_acquire":
            from teamagent.skills.tiktok_acquire.schema import TikTokAcquireStatusInput
            from teamagent.skills.tiktok_acquire.skill import TikTokAcquireStatusSkill

            status_skill = status_skill or TikTokAcquireStatusSkill()
            output = status_skill.run(TikTokAcquireStatusInput(job_id=job_id), poll_ctx)
            return output.status, _format_tiktok_completion(output)

        from teamagent.skills.proposal_builder.schema import ProposalBuilderStatusInput
        from teamagent.skills.proposal_builder.skill import ProposalBuilderStatusSkill

        status_skill = status_skill or ProposalBuilderStatusSkill()
        output = status_skill.run(ProposalBuilderStatusInput(job_id=job_id), poll_ctx)
        return output.status, _format_proposal_completion(output)

    return _poll


def _schedule_async_job_notice(
    tool: str,
    data: dict[str, Any],
    raw: dict[str, Any],
    ctx: SkillContext,
) -> None:
    if tool not in _ASYNC_JOB_TOOLS:
        return
    job_id = data.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return
    try:
        from teamagent.mcp_gateway.async_job_notify import enabled, schedule_completion_notice

        if not enabled():
            return
        schedule_completion_notice(
            tool=tool,
            job_id=job_id,
            user_context=raw,
            request_id=ctx.request_id,
            poll=_build_async_job_poll(tool, job_id, ctx),
        )
    except Exception as exc:
        logger.warning(
            "async_job_notify_dispatch_failed",
            tool=tool,
            job_id=job_id,
            request_id=ctx.request_id,
            error=type(exc).__name__,
        )


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
            "verified_slack_user_id": slack_user_id,
            "verified_slack_team_id": verified_caller.slack_team_id,
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
        return {
            **strict_meta,
            "verified_slack_user_id": slack_user_id,
            "verified_slack_team_id": verified_caller.slack_team_id,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
        }, None

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


def _log_connect_intent(
    intent: ConnectIntent,
    *,
    requested: str,
    dispatched: str,
    slack_user_id: str | None,
    connect_tool_available: bool,
) -> None:
    """連携依頼の検出結果を構造化ログへ出す（観測性・柱2）。

    ⚠️ **本文・クライアント名は絶対に載せない**（G7 規律）。載せるのは
    「誰が（署名検証済み slack_user_id）」「連携語を検出したか」「どの tool へ流れたか」
    「判定理由コード」「一致した引数名」だけ。これで「連携と言ったのに何も起きなかった」
    を、Slack のログを見ずに CloudWatch 側だけで後追いできる。
    """
    logger.info(
        "mcp_connect_intent",
        tool_requested=requested,
        tool_dispatched=dispatched,
        connect_keyword=intent.matched,
        connect_reason=intent.reason,
        connect_field=intent.field,
        redirected=requested != dispatched,
        connect_tool_available=connect_tool_available,
        slack_user_id=slack_user_id,
    )


def _maybe_redirect_to_connect(
    by_name: dict[str, ToolSpec],
    *,
    name: str,
    spec: ToolSpec,
    skill_args: dict[str, Any],
    slack_user_id: str | None,
) -> tuple[str, ToolSpec, dict[str, Any]]:
    """「連携」依頼を、LLM の tool 選択に関係なく ``oauth_connect`` へ寄せる決定論分岐。

    ## なぜここ（MCP 境界）なのか

    OpenClaw 側で「本文が連携語なら必ずこの tool を呼ぶ」を書ける層は**存在しない**:

    * ``infra/openclaw/openclaw.config.json5`` にルーティング DSL は無い
      （あるのは ``tools.profile`` / ``mcp.servers.teamagent.toolFilter`` の許可リストだけ）。
    * ``infra/openclaw/caller-identity-plugin`` が握れる hook は
      ``inbound_claim`` / ``message_received`` / ``before_model_resolve`` /
      ``before_tool_call`` / ``agent_end`` の 5 つで、戻り値で挙動を変えられるのは
      ``before_tool_call``（``{block, blockReason}`` か ``{params}`` を返す）だけ。
      **tool 呼び出しを新規に発生させる hook は無い**。

    したがって「LLM が何かしらの tool を呼んだ後」に効かせられるのは MCP 境界だけで、
    ここが実際に配線できる最下流の決定論点になる。

    ## 安全性

    * 寄せ替えは ``_verify_caller`` / ``_resolve_metadata`` の **後**に行う。署名 claim は
      LLM が申告した元の tool 名に対して検証済みで、その束縛は一切緩めない。
    * 寄せ先の ``oauth_connect`` は「呼んだ本人向けの認可 URL を組み立てて返すだけ」で、
      元の tool より広い権限を要求しない（＝権限昇格にならない）。
    * ``oauth_connect`` が露出していない環境では寄せずに通常ディスパッチへ落とす。

    ## 残る限界（正直に書く）

    LLM が **1 つも tool を呼ばなかった**ターン（本番実測の 1・2 ターン目）はここへ来ない。
    そこは SOUL.md の専用節（連携語は一語でも ``oauth_connect`` を呼ぶ）が担う。

    また :func:`dispatch_run_agent`（``USE_AGENT_ORCHESTRATOR=1`` の dark 経路）には
    **意図的に適用していない**。あちらは L1 tool 一式（``oauth_connect`` を含む）を
    そのまま SDK へ渡す委譲口なので、境界で ``goal`` を横取りすると
    「エージェントに任せる」という当の契約を壊す。連携語は SDK 側の tool 選択で拾う。
    """
    intent = detect_connect_intent_in_args(skill_args)
    connect_spec = by_name.get(OAUTH_CONNECT_TOOL_NAME)
    redirect = intent.matched and name != OAUTH_CONNECT_TOOL_NAME and connect_spec is not None
    if intent.matched or name == OAUTH_CONNECT_TOOL_NAME:
        _log_connect_intent(
            intent,
            requested=name,
            dispatched=OAUTH_CONNECT_TOOL_NAME if redirect else name,
            slack_user_id=slack_user_id,
            connect_tool_available=connect_spec is not None,
        )
    if redirect and connect_spec is not None:
        # oauth_connect は入力を持たない（対象は常に呼び出した本人）。元の引数は捨てる。
        return OAUTH_CONNECT_TOOL_NAME, connect_spec, {}
    return name, spec, skill_args


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
    # ゲート受信時刻。skill 実行前（身元検証・resolver 往復・入力検証・進捗投稿）に
    # どれだけ溶けているかを mcp_tool_usage.gateway_ms として可視化する（挙動は不変）。
    _received = time.perf_counter()
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
    # usage_events.user_id には署名検証済み claim 由来の Slack ID だけを採る。
    # LEGACY の raw["slack_user_id"] は未検証なので不採用。metadata 契約は変えない。
    usage_user_id = verified_caller.slack_user_id if verified_caller is not None else None

    skill_args = {k: v for k, v in arguments.items() if k != USER_CONTEXT_KEY}

    # ── 決定論分岐: 「連携」依頼は LLM の tool 選択を待たず oauth_connect へ寄せる ──
    # 身元検証・metadata 解決の後・入力検証の前に置く（claim は元の tool 名で検証済み）。
    name, spec, skill_args = _maybe_redirect_to_connect(
        by_name,
        name=name,
        spec=spec,
        skill_args=skill_args,
        slack_user_id=usage_user_id,
    )

    try:
        skill_input = spec.input_schema(**skill_args)
    except Exception as e:  # 入力検証エラーは構造化で返す
        return _err(f"invalid input: {type(e).__name__}: {e}")

    # 二段返し（USE_SEARCH_TWO_STAGE・既定 OFF）を許可してよい面の印。**この境界を通った
    # search tool だけ**が対象で、connect-web(/app)・runtime/slack_bot.py の直呼び・
    # knowledge_deliver が内部で回す search には印が付かない（env を入れても影響しない）。
    # 実際に後追いするかは skill 側が env と宛先の有無で決める。
    if name == SEARCH_TOOL_NAME:
        metadata[TWO_STAGE_CTX_KEY] = True

    ctx = SkillContext(user_id=metadata.get("user_email"), metadata=metadata)
    # ── 進捗表示（v0.3.1 Task7・ENABLE_PROGRESS_NOTIFY 既定OFF・fail-open）───────────
    # 重いツールの実行前に「📂 資料を検索しています…」等を Slack へ投稿し、成功/失敗
    # どちらも返却前に削除する。宛先は raw の channel_id → 無ければ slack_user_id DM。
    # ⚠️ send/clear は latency 計測窓の外に置く（_started はツール実行の直前で取る）＝
    # mcp_tool_usage.latency_ms を Slack 往復で水増ししない（Task10 台帳の検証データを歪めない）。
    from teamagent.mcp_gateway.progress_notify import clear_progress, send_progress

    _progress = await send_progress(name, raw, request_id=ctx.request_id)
    _started = time.perf_counter()
    # 受信 → skill 開始 の内訳（身元検証・resolver・入力検証・進捗投稿の合計）。
    _gateway_ms = int((_started - _received) * 1000)
    skill = spec.instantiate()
    try:
        # 同期 skill.run（DB I/O 等でブロックする）を thread に逃がしイベントループを塞がない。
        output = await asyncio.to_thread(skill.run, skill_input, ctx)
        _elapsed_ms = int((time.perf_counter() - _started) * 1000)
    except Exception as e:
        _elapsed_ms = int((time.perf_counter() - _started) * 1000)
        logger.warning(
            "mcp_tool_error",
            tool=name,
            error=type(e).__name__,
            request_id=ctx.request_id,
            gateway_ms=_gateway_ms,
            latency_ms=_elapsed_ms,
        )
        # error 応答でも進捗削除を先に終え、usage task を schedule した後には await しない。
        # これにより lazy 初期化/DB I/O が応答の critical path に入らない。
        await clear_progress(_progress, request_id=ctx.request_id)
        _progress = None
        _record_usage(
            request_id=ctx.request_id,
            skill=name,
            user_email=metadata.get("user_email"),
            user_id=usage_user_id,
            cost_usd=0.0,
            latency_ms=_elapsed_ms,
            skill_args=skill_args,
            status="error",
            error_code=type(e).__name__,
        )
        return _err(f"{type(e).__name__}: {e}", request_id=ctx.request_id)
    finally:
        if _progress is not None:
            await clear_progress(_progress, request_id=ctx.request_id)

    try:
        data = output.model_dump() if hasattr(output, "model_dump") else {"result": str(output)}
    finally:
        skill.cleanup_output(output)
    if isinstance(data, dict):
        _schedule_async_job_notice(name, data, raw, ctx)
    # ── ミドルウェア(0): usage 計測（既定ON・best-effort DB 記録）─────────────────
    # 本番主経路（Aico→MCP）の tool 使用量を構造化ログと usage_events の両方へ
    # 記録する。本文/PII は原則保存せず、裁定済みの非空 query_text だけを例外とする
    # （長さ上限は UsageRecorder が適用）。
    tool_cost_usd = float(data.get("total_cost_usd") or 0.0) if isinstance(data, dict) else 0.0
    logger.info(
        "mcp_tool_usage",
        tool=name,
        request_id=ctx.request_id,
        latency_ms=_elapsed_ms,
        # 内訳（Slack 体感と mcp 実測の差を詰めるための計器）:
        # gateway_ms = 受信→skill 開始（身元検証・Slack resolver・入力検証・進捗投稿）
        # latency_ms = skill 開始→完了（既存キー・定義不変。台帳の連続性を壊さない）
        # total_ms   = 受信→skill 完了（返却前ミドルウェアは含まない）
        gateway_ms=_gateway_ms,
        total_ms=_gateway_ms + _elapsed_ms,
        # ⚠️ キー名は cost_usd に**しない**こと: cloudwatch_fargate.tf のメトリックフィルタ
        # { $.cost_usd = * } が adapter/skill 層の既存ログと合算して日次コストアラームを
        # 二重〜三重計上に汚染する（レビュー F-1）。usage 集計は専用 Insights クエリで行う。
        tool_cost_usd=tool_cost_usd,
    )
    _record_usage(
        request_id=ctx.request_id,
        skill=name,
        user_email=metadata.get("user_email"),
        user_id=usage_user_id,
        cost_usd=tool_cost_usd,
        latency_ms=_elapsed_ms,
        skill_args=skill_args,
    )
    # ── 返却前ミドルウェア（順序契約・v0.3 監査 Step4-(a)）────────────────────
    # (1) 長文退避（Task8・USE_PAYLOAD_OFFLOAD 既定OFF）: 切り詰めは注入キーに触れない
    #     よう **リンク注入より先** に行う（逆順だと注入したURLごと切り詰め対象になる）。
    # (2) リンク注入（Task6）: search 応答にだけ Web UI/Aico Vault リンクを差し込む。
    # usage DB/ログ記録は (0)。将来 quota を強制する場合もこの順序契約を保つ。
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
    Bedrock を要するライブ実行。例外は構造化エラーで返す（外殻ループを落とさない）。
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
        # 遅延 import: Bedrock orchestration は本番ライブ専用。MCP モジュール import を
        # 軽く保ち、依存初期化エラーも構造化して外殻ループを落とさない。
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
