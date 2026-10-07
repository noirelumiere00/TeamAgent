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
import functools
import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

import structlog
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from teamagent.connect_diagnostics import format_user_message, identity_reject_code, now_jst
from teamagent.identity import (
    IdentityResolver,
    build_rls_metadata,
    company_member_metadata,
    no_access_metadata,
    shared_company_domains_from_env,
)
from teamagent.mcp_gateway import (
    answer_feedback,
    detached_jobs,
    direct_summary,
    surface_video_followup,
)
from teamagent.mcp_gateway.caller_claim import (
    CallerClaimError,
    CallerClaimVerifier,
    VerifiedCallerClaim,
)
from teamagent.mcp_gateway.personal_memory import PERSONAL_MEMORY_TOOL_NAMES
from teamagent.mcp_gateway.usage_sources import source_usage_metadata
from teamagent.orchestrator.tools import ToolSpec
from teamagent.runtime.usage_recorder import UsageEvent, UsageRecorder
from teamagent.skills._shared.connect_intent import (
    ConnectIntent,
    detect_connect_intent_in_args,
)
from teamagent.skills._shared.private_surface import is_private_surface
from teamagent.skills.base import ASYNC_JOB_POLL_METADATA_KEY, SkillContext

# 二段返しの契約定数だけを持つ軽量モジュール（boto3/psycopg を引かない）。
from teamagent.skills.search.two_stage import TWO_STAGE_CTX_KEY

if TYPE_CHECKING:
    from teamagent.adapters.answer_feedback_store import AnswerFeedbackStore

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
_ASYNC_JOB_TOOLS = frozenset({"tiktok_acquire", "proposal_builder_submit", "omiyage_report_submit"})

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
    metadata: dict[str, Any] | None = None,
) -> None:
    """MCP 利用を非同期記録へ渡す。入力本文は非空 ``query`` だけを採る。

    ``metadata`` は本文を含まない付帯情報だけ（``usage_sources.source_usage_metadata``）。
    """
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
            metadata=dict(metadata) if metadata else None,
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


def _strip_schema_titles(node: Any, *, names: bool = False) -> Any:
    """JSON Schema から ``title``（pydantic が付ける表示名）だけを再帰的に落とす。

    ``names=True`` は「キーが引数名の辞書」（properties・$defs）を表し、キーはそのまま残す
    （``title`` という名前の引数＝calendar_event など を消さないため）。
    """
    if isinstance(node, list):
        return [_strip_schema_titles(v) for v in node]
    if not isinstance(node, dict):
        return node
    if names:
        return {k: _strip_schema_titles(v) for k, v in node.items()}
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k == "title" and isinstance(v, str):
            continue
        out[k] = _strip_schema_titles(v, names=k in ("properties", "$defs", "definitions"))
    return out


def _augment_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """入力スキーマを tools/list 用に整える（``_user_context`` の口を足し、飾りを落とす）。

    2026-09-30: OpenClaw は毎回すべてのツール定義をモデルへ送る。固定部分 127k トークンの
    約 8 割がツール定義で、特に日本語はツール定義の中だと system prompt の約 5 倍の
    トークンになる（Bedrock CountTokens で実測）。
    - ``_user_context`` は宣言を ``{"type": "object"}`` だけにする。値は caller-identity
      plugin が before_tool_call で丸ごと正しいものに置き換え、欠落も ``{}`` とみなす
      （caller-identity-plugin/dist/index.js の rawDeclared 分岐）。モデルに中身の説明を
      見せる意味は無く、34 本で約 2.7 万トークンを使っていた。
    - pydantic が付ける ``title`` と、最上位の ``description``（入力モデルの docstring＝
      開発メモ）はモデルの判断材料にならないので落とす（約 0.7 万トークン）。
      ツールの説明は ToolSpec.description、引数の説明は各 property の description に残る。
    """
    out: dict[str, Any] = _strip_schema_titles(dict(schema))
    out.pop("description", None)
    props = dict(out.get("properties") or {})
    props[USER_CONTEXT_KEY] = {"type": "object"}
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
    ]
    if failed:
        from teamagent.skills._shared.long_jobs import failure_reason

        return "❌ TikTok取得に失敗しました。" + failure_reason(output.error_code)
    if output.counts:
        counts = "、".join(
            f"{str(key)[:32]}={str(value)[:64]}" for key, value in list(output.counts.items())[:10]
        )
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
    ]
    if failed:
        from teamagent.skills._shared.long_jobs import failure_reason

        return "❌ 提案書生成に失敗しました。" + failure_reason(output.error_code)
    result_message = output.result_message or output.message
    if result_message:
        lines.append(result_message[:1000])
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
    # 見張り経路の印を立てる: status skill 側はこの印を見て、課金を伴う補完（Apify）を
    # 発火させない（LLM 照会との同時発火＝同じ URL の並列 run を作らない）。
    poll_ctx = SkillContext(
        request_id=ctx.request_id,
        user_id=ctx.user_id,
        metadata={**ctx.metadata, ASYNC_JOB_POLL_METADATA_KEY: True},
    )
    status_skill: Any = None

    def _poll() -> tuple[str, str]:
        nonlocal status_skill
        if tool == "video_algorithm":
            from teamagent.skills.video_algorithm.schema import VideoAlgorithmStatusInput
            from teamagent.skills.video_algorithm.skill import VideoAlgorithmStatusSkill

            output = VideoAlgorithmStatusSkill().run(
                VideoAlgorithmStatusInput(job_id=job_id), poll_ctx
            )
            return output.status, output.message

        if tool == "tiktok_acquire":
            from teamagent.skills.tiktok_acquire.schema import TikTokAcquireStatusInput
            from teamagent.skills.tiktok_acquire.skill import TikTokAcquireStatusSkill

            status_skill = status_skill or TikTokAcquireStatusSkill()
            output = status_skill.run(TikTokAcquireStatusInput(job_id=job_id), poll_ctx)
            state = output.status
            if state == "done" and not output.posts_json_url:
                return "unknown", "取得結果の共有リンクを確認できません。"
            return state, _format_tiktok_completion(output)

        if tool == "omiyage_report_submit":
            from teamagent.skills.omiyage_report.schema import OmiyageReportStatusInput
            from teamagent.skills.omiyage_report.skill import OmiyageReportStatusSkill

            status_skill = status_skill or OmiyageReportStatusSkill()
            output = status_skill.run(OmiyageReportStatusInput(job_id=job_id), poll_ctx)
            if output.status == "failed":
                from teamagent.skills._shared.long_jobs import failure_reason

                reason = {
                    "OMIYAGE_SEARCH_FAILED": "TikTok検索がすべて失敗しました。",
                    "OMIYAGE_BUILD_FAILED": "資料の組み立てで止まりました。",
                }.get(output.error_code, failure_reason(output.error_code))
                text = "お土産資料は作成に失敗しました。" + reason
            else:
                from teamagent.skills._shared.long_jobs import origin

                target = origin(poll_ctx)
                if (
                    output.status == "done"
                    and not output.slack_delivered
                    and not (target is not None and target.pending)
                ):
                    return "failed", "資料は生成・保存できましたが、結果の配信が中断されました。"
                text = (
                    "\n".join([output.result_message, *output.summary_lines, output.next_step])
                    if output.status == "done"
                    else "処理中です。"
                )
            return output.status, text

        from teamagent.skills.proposal_builder.schema import ProposalBuilderStatusInput
        from teamagent.skills.proposal_builder.skill import ProposalBuilderStatusSkill

        status_skill = status_skill or ProposalBuilderStatusSkill()
        output = status_skill.run(ProposalBuilderStatusInput(job_id=job_id), poll_ctx)
        from teamagent.skills._shared.long_jobs import origin

        target = origin(poll_ctx)
        if (
            output.status == "done"
            and output.proposal_status == "ready"
            and not output.slack_delivered
            and not output.pptx_url
            and not (target is not None and target.pending)
        ):
            return "failed", "資料は生成・保存できましたが、結果の配信が中断されました。"
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
    # tiktok_acquire は1回の実行時間に収まらない要求を複数ジョブへ分けて job_ids で返す。
    # 先頭の job_id だけ見張ると残りの完了が届かないので、全ジョブに見張りを付ける。
    job_ids: list[str] = []
    extra = data.get("job_ids")
    for candidate in [data.get("job_id"), *(extra if isinstance(extra, list) else [])]:
        if isinstance(candidate, str) and candidate and candidate not in job_ids:
            job_ids.append(candidate)
    from teamagent.mcp_gateway.async_job_notify import schedule_completion_notice
    from teamagent.skills._shared.long_jobs import origin, remember_latest

    target = origin(ctx)
    if target is None or not job_ids or data.get("status") not in {"queued", "running", "done"}:
        return
    try:
        remember_latest(ctx, tool, "|".join(job_ids))
        if data.get("deduplicated"):
            return
        polls = [_build_async_job_poll(tool, job_id, ctx) for job_id in job_ids]

        def poll_all() -> tuple[str, str]:
            results = [poll() for poll in polls]
            if any(status not in {"queued", "running", "done", "failed"} for status, _ in results):
                return "unknown", "作業の状態を確認できません。"
            if any(status in {"queued", "running"} for status, _ in results):
                state = "running" if any(status == "running" for status, _ in results) else "queued"
                return state, "処理中です。"
            state = "failed" if any(status == "failed" for status, _ in results) else "done"
            return state, "\n\n".join(text for _, text in results)

        schedule_completion_notice(
            tool=tool,
            job_id="|".join(job_ids),
            origin=target,
            request_id=ctx.request_id,
            poll=poll_all,
            ctx=ctx,
        )
    except Exception as exc:
        logger.warning(
            "async_job_notify_dispatch_failed",
            tool=tool,
            request_id=ctx.request_id,
            error=type(exc).__name__,
        )
        data["message"] = "作業は受け付けましたが、自動配信の準備を確認できませんでした。"


# 例外文としてモデルへ返す上限（字）。2026-09-29 本番: proposal_builder_submit の ValidationError の
# 全文（推定 約 24k tokens・大量のエラー行）が S3 退避の対象外のままモデルへ返り、Aico の DM が
# 上限 200k を超えて毎回失敗した。OpenClaw 側の toolResultMaxChars は保存時と回復時にしか効かず、
# 受け取った直後の同じターンの呼び出しは守らないので、ここで短くする。
_ERROR_TEXT_MAX_CHARS = 4000
# pydantic の ValidationError から返す個別エラーの件数（残りは件数だけ）。
_VALIDATION_ERROR_MAX_ITEMS = 10


def _exception_text(e: BaseException) -> str:
    """例外を ``"<型名>: <内容>"`` の 1 文字列にし、モデルへ返してよい長さに切る。

    pydantic の ValidationError は input_value と URL を落とし、先頭 10 件の「場所: 理由」と
    総件数だけにする（どの項目を直せばよいかは残す）。それ以外も ``_ERROR_TEXT_MAX_CHARS`` で切る。
    """
    from pydantic import ValidationError

    name = type(e).__name__
    if isinstance(e, ValidationError):
        items = e.errors(include_url=False, include_input=False, include_context=False)
        lines = [f"{e.error_count()} validation error(s) for {e.title}"]
        for item in items[:_VALIDATION_ERROR_MAX_ITEMS]:
            loc = ".".join(str(part) for part in item.get("loc", ())) or "(root)"
            lines.append(f"{loc}: {item.get('msg', '')}")
        rest = len(items) - _VALIDATION_ERROR_MAX_ITEMS
        if rest > 0:
            lines.append(f"（ほか {rest} 件）")
        body = "\n".join(lines)
    else:
        body = str(e)
    text = f"{name}: {body}"
    if len(text) > _ERROR_TEXT_MAX_CHARS:
        omitted = len(text) - _ERROR_TEXT_MAX_CHARS
        text = f"{text[:_ERROR_TEXT_MAX_CHARS]}…（以下 {omitted} 字を省略）"
    return text


def _err(message: str, **extra: Any) -> list[TextContent]:
    """構造化エラーを TextContent で返す（サーバ/外殻ループを落とさない）。"""
    payload: dict[str, Any] = {"error": message, **extra}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


def _identity_rejected(reason: str, *, slack_user_id: str | None) -> list[TextContent]:
    """本人特定失敗（``CALLER_IDENTITY_REJECTED``）を、診断行つきの利用者向け文で返す。

    code は従来どおり ``CALLER_IDENTITY_REJECTED``（attack_mcp / 外殻の判定は code を見る）。
    message 末尾に ``診断: CONNECT-I01a|b|c <時刻 JST> <slack_user_id>`` を付け、利用者が
    そのまま管理者へ転送できるようにする（docs/runbooks/connect_diagnostics.md）。
    """
    diag = format_user_message(
        identity_reject_code(reason),
        when=now_jst(),
        extra=slack_user_id or None,
    )
    return _err(f"Caller authorization failed. {diag}", code="CALLER_IDENTITY_REJECTED")


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
            return {}, _identity_rejected("missing_verified_caller", slack_user_id=slack_user_id)
        try:
            identity = await identity_resolver(slack_user_id)
        except Exception:
            logger.warning("identity_spoof_rejected", tool=tool, reason="resolver_error")
            return {}, _identity_rejected("resolver_error", slack_user_id=slack_user_id)
        resolved = (
            build_rls_metadata(identity, allowed_domains=allowed_domains) if identity else None
        )
        if not resolved or not resolved.get("user_email"):
            logger.warning("identity_spoof_rejected", tool=tool, reason="resolve_none")
            return {}, _identity_rejected("resolve_none", slack_user_id=slack_user_id)
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
                return {}, _identity_rejected(
                    "missing_verified_caller", slack_user_id=slack_user_id
                )
            return no_access_metadata(), None
        reject_reason = "resolve_none"
        try:
            identity = await identity_resolver(slack_user_id)
        except Exception:
            logger.warning("identity_spoof_rejected", tool=tool, reason="resolver_error")
            identity = None
            reject_reason = "resolver_error"  # 診断コードは I01b（ログ event は従来どおり）
        strict_meta = (
            build_rls_metadata(identity, allowed_domains=allowed_domains) if identity else None
        )
        if strict_meta is None:
            if require_rls:
                logger.warning("identity_spoof_rejected", tool=tool, reason="resolve_none")
                return {}, _identity_rejected(reject_reason, slack_user_id=slack_user_id)
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
        # 署名済み claim が無い/不正＝検証済み caller が無い。本番で利用者が最初に踏む
        # 拒否はここ（_resolve_metadata の missing_verified_caller は claim 検証を通った後の
        # 同義の防壁）なので、同じ I01a を付けて利用者が転送できるようにする。
        return None, _identity_rejected("missing_verified_caller", slack_user_id=None)


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


def _detach_response(query: str, text: str, *, status: str = "running") -> list[TextContent]:
    """切り離し中・二重依頼・順番待ち・混雑・中断の返答。内部語（job_id・コード名）は載せない。

    status は running（受付・順番待ち・二重依頼）/ busy（混み合っていて始めていない）/
    interrupted（システム更新で中断）。
    """
    payload = {"status": status, "query": query, "message": text, "slack_summary": text}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def _wait_or_detach(job: detached_jobs.DetachedJob, timeout_s: float) -> str:
    """ジョブの完了を最大 ``timeout_s`` 秒待つ。

    返り値は ``DETACH_DONE``（完了＝同期で返す）/ ``DETACH_DETACHED``（切り離した）/
    ``DETACH_INTERRUPTED``（終了処理中に切り離した＝受付文の代わりに中断文を返す）。
    待っている間に OpenClaw が打ち切っても（CancelledError）ジョブには伝えない（shield 相当）。
    切り離して完了時に依頼元の会話へ届ける。既に完了していた場合も別 thread で届ける。
    """
    loop = asyncio.get_running_loop()
    waiter: asyncio.Future[None] = loop.create_future()

    def _set() -> None:
        if not waiter.done():
            waiter.set_result(None)

    def _wake() -> None:
        try:
            loop.call_soon_threadsafe(_set)
        except RuntimeError:  # loop が閉じている（打ち切り後）＝待ち手は居ない
            pass

    job.add_waker(_wake)
    try:
        await asyncio.wait_for(waiter, timeout=timeout_s)
        return detached_jobs.DETACH_DONE
    except TimeoutError:
        return job.detach()
    except asyncio.CancelledError:
        state = job.detach()
        if state == detached_jobs.DETACH_DONE:
            job.deliver_in_background()
        elif state == detached_jobs.DETACH_INTERRUPTED:
            # 終了処理中で、返す相手（OpenClaw の実行）も居ない＝中断をその場で投稿する。
            job.post_interrupted_in_background()
        logger.info("video_algorithm_detach_caller_cancelled", request_id=job.request_id)
        raise


def _complete_detached(
    result: Any,
    error: BaseException | None,
    interrupted: bool,
    *,
    loop: asyncio.AbstractEventLoop,
    skill: Any,
    tool: str,
    query: str,
    destination: detached_jobs.Destination,
    request_id: str,
    started: float,
    gateway_ms: int,
    user_email: str | None,
    usage_user_id: str | None,
    skill_args: dict[str, Any],
    fallback_user_id: str | None = None,
    job_origin: Any = None,
    job_id: str = "",
) -> None:
    """切り離したジョブの完了処理（ジョブの thread で走る）: 投稿 → cleanup_output → usage 記録。"""
    if isinstance(error, detached_jobs.DetachInterruptedError):
        # 順番待ちのまま終了処理に入った＝skill.run は走っていない（quota も Gemini も未使用）。
        # 後始末も usage 記録も無い。中断通知がまだなら（通知の印より先に終わった）ここで送る。
        logger.warning(
            "video_algorithm_detach_queued_job_interrupted",
            request_id=request_id,
            notified=interrupted,
        )
        if not interrupted:
            detached_jobs.post_to_origin(
                detached_jobs.interrupted_text(query), destination, request_id=request_id
            )
        return
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    delivered = False
    try:
        if interrupted:
            # 再デプロイの中断通知を送った後に完了した分は、矛盾する 2 通目を出さない
            # （結果は complete としてキャッシュ済みなので、再依頼は課金なしで即返る）。
            logger.warning("video_algorithm_detach_done_after_interrupt", request_id=request_id)
        else:
            text = (
                detached_jobs.completion_text(result, query)
                if error is None
                else detached_jobs.error_text(query, error)
            )
            # 完了は Block Kit で出す（描けなければ None＝今の文字だけの投稿）。
            rich = (
                detached_jobs.completion_message(result, request_id=request_id)
                if error is None
                else None
            )
            if job_origin is not None:
                from teamagent.mcp_gateway.async_job_notify import publish_notice

                delivered = publish_notice(
                    text,
                    origin=job_origin,
                    request_id=request_id,
                    job_id=job_id,
                    rich=rich,
                    completed=error is None,
                )
            else:
                delivered = detached_jobs.post_to_origin(
                    text,
                    destination,
                    request_id=request_id,
                    fallback_user_id=fallback_user_id,
                    rich=rich,
                )

    finally:
        if result is not None:
            try:
                skill.cleanup_output(result)
            except Exception as exc:
                logger.warning(
                    "video_algorithm_detach_cleanup_failed",
                    request_id=request_id,
                    error=type(exc).__name__,
                )
    tool_cost_usd = (
        float(getattr(result, "total_cost_usd", 0.0) or 0.0) if result is not None else 0.0
    )
    logger.info(
        "mcp_tool_usage",
        tool=tool,
        request_id=request_id,
        latency_ms=elapsed_ms,
        gateway_ms=gateway_ms,
        total_ms=gateway_ms + elapsed_ms,
        tool_cost_usd=tool_cost_usd,
        detached=True,
        delivered=delivered,
        status="ok" if error is None else "error",
    )
    # usage_events の記録は本体の event loop へ渡す（recorder は loop に紐づく接続を使う）。
    record = functools.partial(
        _record_usage,
        request_id=request_id,
        skill=tool,
        user_email=user_email,
        user_id=usage_user_id,
        cost_usd=tool_cost_usd,
        latency_ms=elapsed_ms,
        skill_args=skill_args,
        status="ok" if error is None else "error",
        error_code=None if error is None else type(error).__name__,
    )
    try:
        loop.call_soon_threadsafe(record)
    except RuntimeError:
        logger.warning("usage_event_schedule_failed", request_id=request_id, error="LoopClosed")


#: 本人の受信メール・下書き・ダイジェストを読む道具（本人 DM でだけ動かす）。
#: 足すときはここに名前を追加する（tests/mcp_gateway/test_dm_only_tools.py が一覧を固定）。
DM_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "mail_summary",
        "mail_followup",
        "mail_reply",
        "mail_to_internal_context",
        "morning_digest",
        "meeting_prep",
    }
)
DM_ONLY_MESSAGE = (
    "メールの中身は、ほかの人の目に触れないよう Aico との DM でだけお出ししています。"
    "DM でもう一度お声がけください。"
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

    # ── 出力面ガード（10-01 監査候補②・deny-by-default）: 本人のメールを読む道具は、
    # 署名済み claim の会話が本人 DM（D 始まり）で、身元が検証済みのときだけ動かす。
    # チャンネルに第三者が置いた指示文でモデルが呼ばされても、受信メールの要約・件名・
    # 相手がスレッドへ出ない（スレッドの記録にも残らない＝候補③も塞ぐ）。Gmail には触らない。
    if name in DM_ONLY_TOOLS and not is_private_surface(
        verified_caller.channel_id if verified_caller is not None else None,
        metadata.get("identity_verified") is True,
    ):
        logger.info("dm_only_tool_rejected", tool=name)
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"error": "dm_only", "message": DM_ONLY_MESSAGE}, ensure_ascii=False
                ),
            )
        ]

    try:
        skill_input = spec.input_schema(**skill_args)
    except Exception as e:  # 入力検証エラーは構造化で返す
        return _err(f"invalid input: {_exception_text(e)}")

    # 二段返し（USE_SEARCH_TWO_STAGE・既定 OFF）を許可してよい面の印。**この境界を通った
    # search tool だけ**が対象で、connect-web(/app)・runtime/slack_bot.py の直呼び・
    # knowledge_deliver が内部で回す search には印が付かない（env を入れても影響しない）。
    # 実際に後追いするかは skill 側が env と宛先の有無で決める。
    if name == SEARCH_TOOL_NAME:
        metadata[TWO_STAGE_CTX_KEY] = True

    from teamagent.skills._shared.long_jobs import _OWNER_KEY, ORIGIN_KEY, Origin, enabled

    # 生の申告値を必ず消し、署名検証結果だけから共通ジョブの身元・宛先を作る。
    metadata.pop(ORIGIN_KEY, None)
    metadata.pop(_OWNER_KEY, None)
    if verified_caller is not None:
        metadata[_OWNER_KEY] = (
            verified_caller.slack_team_id + "\x1f" + verified_caller.slack_user_id
        )
        destination = detached_jobs.destination_from_claim(verified_caller)
        if enabled() and destination is not None:
            metadata[ORIGIN_KEY] = Origin(
                destination.channel_id,
                destination.thread_ts,
                verified_caller.slack_user_id,
                deferred=name in _ASYNC_JOB_TOOLS,
            )

    ctx = SkillContext(user_id=metadata.get("user_email"), metadata=metadata)

    # ── video_algorithm の切り離し（USE_VIDEO_ALGORITHM_DETACH 既定OFF＝以下は素通り）─────
    # 宛先・対象者・二重依頼の判定は、署名検証済み claim と resolver が解決した email だけで行う。
    detach_policy: detached_jobs.DetachPolicy | None = None
    detach_destination: detached_jobs.Destination | None = None
    detach_key: str | None = None
    detach_query = str(getattr(skill_input, "query", "") or "")
    if name in detached_jobs.DETACHABLE_TOOLS:
        detach_policy = detached_jobs.load_policy()
        if detach_policy.enabled and verified_caller is not None:
            detach_key = detached_jobs.inflight_key(verified_caller.slack_user_id, detach_query)
            running = detached_jobs.REGISTRY.get(detach_key)
            # 終了処理中は「お届けします」を約束しない（start が中断文を返す）。
            if running is not None and not detached_jobs.REGISTRY.closing:
                # 同じ人・同じ KW の分析が走っている（本数など引数違いも含む）。
                # quota も Gemini も使わずに返す。
                logger.info(
                    "video_algorithm_detach_duplicate", request_id=ctx.request_id, stage="precheck"
                )
                return _detach_response(
                    detach_query,
                    detached_jobs.in_progress_text(
                        detach_query,
                        same_conversation=running.destination.channel_id
                        == verified_caller.channel_id,
                    ),
                )
            detach_destination, detach_reason = detached_jobs.decide(
                detach_policy,
                tool=name,
                verified_caller=verified_caller,
                metadata=metadata,
            )
            logger.info(
                "video_algorithm_detach_decision",
                request_id=ctx.request_id,
                reason=detach_reason,
            )

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
    detached_job: detached_jobs.DetachedJob | None = None
    if (
        detach_policy is not None
        and detach_destination is not None
        and detach_key is not None
        and verified_caller is not None
    ):
        from teamagent.adapters.proposal_job_store import ProposalJobStore
        from teamagent.skills._shared.long_jobs import origin, owner_key, remember_latest

        video_job_id = f"va_{ctx.request_id}"
        video_store = ProposalJobStore()
        video_origin = origin(ctx)
        if video_origin is not None:
            video_store.create_job(
                video_job_id,
                {"kind": "video_algorithm", "owner": owner_key(ctx, "video_algorithm")},
            )

        def run_video() -> Any:
            if video_origin is None:
                return skill.run(skill_input, ctx)
            if not video_store.mark_running(video_job_id):
                raise RuntimeError("VIDEO_ALGORITHM_STATE_WRITE_FAILED")
            heartbeat_stop = threading.Event()

            def heartbeat() -> None:
                while not heartbeat_stop.wait(30):
                    try:
                        if not video_store.heartbeat(video_job_id):
                            return
                    except Exception as exc:
                        logger.warning("video_job_heartbeat_failed", error=type(exc).__name__)

            heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
            heartbeat_thread.start()
            try:
                result = skill.run(skill_input, ctx)
                text = detached_jobs.completion_text(result, detach_query)
                if not video_store.mark_done(video_job_id, json.dumps({"message": text})):
                    raise RuntimeError("VIDEO_ALGORITHM_STATE_WRITE_FAILED")
                return result
            except Exception as exc:
                video_store.mark_failed(
                    video_job_id,
                    "VIDEO_ALGORITHM_FAILED",
                    error_summary=detached_jobs.user_message_for_error(exc),
                )
                raise
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=1)

        detached_job, start_state = detached_jobs.REGISTRY.start(
            key=detach_key,
            max_background=detach_policy.max_background,
            max_queued=detach_policy.max_queued,
            tool=name,
            query=detach_query,
            request_id=ctx.request_id,
            destination=detach_destination,
            target=run_video,
            on_detached_done=functools.partial(
                _complete_detached,
                loop=asyncio.get_running_loop(),
                skill=skill,
                tool=name,
                query=detach_query,
                destination=detach_destination,
                request_id=ctx.request_id,
                started=_started,
                gateway_ms=_gateway_ms,
                user_email=metadata.get("user_email"),
                usage_user_id=usage_user_id,
                skill_args=skill_args,
                fallback_user_id=verified_caller.slack_user_id,
                job_origin=video_origin,
                job_id=video_job_id,
            ),
        )
        if start_state == "duplicate" and detached_job is not None:
            # precheck と start の間に同じキーが登録された（同時に 2 通届いた）。
            logger.info(
                "video_algorithm_detach_duplicate", request_id=ctx.request_id, stage="start"
            )
            same = detached_job.destination.channel_id == verified_caller.channel_id
            await clear_progress(_progress, request_id=ctx.request_id)
            return _detach_response(
                detach_query,
                detached_jobs.in_progress_text(detach_query, same_conversation=same),
            )
        if detached_job is None:
            # 同期には戻さない（戻すと 360 秒の打ち切りで結果が消え、quota だけ減る）。
            # skill.run の前なので quota は使っていない。
            await clear_progress(_progress, request_id=ctx.request_id)
            if start_state == "closing":
                logger.warning("video_algorithm_detach_closing", request_id=ctx.request_id)
                return _detach_response(
                    detach_query,
                    detached_jobs.interrupted_text(detach_query),
                    status="interrupted",
                )
            logger.warning(
                "video_algorithm_detach_queue_full",
                request_id=ctx.request_id,
                max_background=detach_policy.max_background,
                max_queued=detach_policy.max_queued,
            )
            return _detach_response(
                detach_query, detached_jobs.busy_text(detach_query), status="busy"
            )
        if video_origin is not None and start_state not in {"duplicate", "closing", "full"}:
            remember_latest(ctx, "video_algorithm", video_job_id)

    try:
        if detached_job is not None and detach_policy is not None:
            wait_state = await _wait_or_detach(detached_job, detach_policy.detach_after_s)
            if wait_state == detached_jobs.DETACH_INTERRUPTED:
                # 終了処理中＝届けられない約束はしない。完了しても投稿はせず、後始末と記録だけ行う。
                logger.warning(
                    "video_algorithm_detach_interrupted_at_detach", request_id=ctx.request_id
                )
                return _detach_response(
                    detach_query,
                    detached_jobs.interrupted_text(detach_query),
                    status="interrupted",
                )
            if wait_state == detached_jobs.DETACH_DETACHED:
                if video_origin is not None:
                    from teamagent.mcp_gateway.async_job_notify import schedule_completion_notice
                    from teamagent.skills.video_algorithm.schema import VideoAlgorithmStatusInput
                    from teamagent.skills.video_algorithm.skill import VideoAlgorithmStatusSkill

                    def video_poll() -> tuple[str, str]:
                        status = VideoAlgorithmStatusSkill().run(
                            VideoAlgorithmStatusInput(job_id=video_job_id), ctx
                        )
                        return status.status, status.message

                    schedule_completion_notice(
                        tool=name,
                        job_id=video_job_id,
                        origin=video_origin,
                        request_id=ctx.request_id,
                        poll=video_poll,
                        ctx=ctx,
                    )
                queued = detached_job.queued
                logger.info(
                    "video_algorithm_detached",
                    request_id=ctx.request_id,
                    after_ms=int((time.perf_counter() - _started) * 1000),
                    dm=detached_job.destination.is_dm,
                    queued=queued,
                )
                # 完了時の投稿・cleanup_output・usage 記録はジョブの thread が 1 回だけ行う。
                return _detach_response(
                    detach_query,
                    detached_jobs.queued_receipt_text(detach_query)
                    if queued
                    else detached_jobs.receipt_text(detach_query),
                )
            # 待ち時間内に終わった＝今までどおり同期で返す（失敗なら skill の例外がここで出る）。
            output = detached_job.outcome()
        else:
            # 同期 skill.run（DB I/O 等でブロックする）を thread に逃がしイベントループを塞がない。
            output = await asyncio.to_thread(skill.run, skill_input, ctx)
        _elapsed_ms = int((time.perf_counter() - _started) * 1000)
    except detached_jobs.DetachInterruptedError:
        # 順番待ちのまま終了処理に入った（skill.run は走っていない＝usage も quota も無し）。
        return _detach_response(
            detach_query, detached_jobs.interrupted_text(detach_query), status="interrupted"
        )
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
        if detached_job is not None and detached_jobs.is_in_progress_error(e):
            # 同じ引数の処理中リース（別プロセス・同期経路の実行など）。
            # コード名を出さずに言い換える。
            return _detach_response(detach_query, detached_jobs.error_text(detach_query, e))
        return _err(_exception_text(e), request_id=ctx.request_id)
    finally:
        if _progress is not None:
            await clear_progress(_progress, request_id=ctx.request_id)

    # ── 検索上位チェックの 2 段目（USE_SURFACE_VIDEO_FOLLOWUP 既定OFF＝素通り）──────────
    # 対象なら上位の動画の中身の分析を裏で登録し、slack_summary に予告を 1 行足す（fail-open）。
    # 対象のときは月間上限の残りを DB から読むので、event loop を塞がないよう thread で呼ぶ。
    # 取得の前の確認（status=needs_input）は 2 段目も直接投稿もしない（確認文を Aico が返す）。
    needs_input = getattr(output, "status", None) == "needs_input"
    if name == surface_video_followup.TOOL and not needs_input:
        await asyncio.to_thread(
            surface_video_followup.maybe_schedule,
            skill=skill,
            output=output,
            skill_input=skill_input,
            ctx=ctx,
            verified_caller=verified_caller,
            metadata=metadata,
            usage_user_id=usage_user_id,
            record_usage=_record_usage,
            loop=asyncio.get_running_loop(),
        )
    # 出典 ID（上位 5 件）と回答の文字数（search 系だけ・本文/URL は残さない・例外を出さない）。
    # source_uri は model_dump から外れる内部項目なので、dump 前の出力オブジェクトから取る。
    usage_metadata = source_usage_metadata(name, output)
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
        metadata=usage_metadata,
    )
    # ── 直接投稿（USE_DIRECT_SUMMARY_POST 既定OFF＝素通り・mcp_gateway/direct_summary.py）──
    # 対象なら slack_summary を mcp が依頼元の DM へ直接出し、Aico には「投稿済み」だけを返す
    # （Aico に文面を組み直させない・URL を落とさせない）。届かなければ今までどおり返す。
    # usage 記録の後に置く（費用の記録は投稿の成否に関係なく残す）。
    if name in direct_summary.DIRECT_TOOLS and isinstance(data, dict) and not needs_input:
        direct_destination, direct_reason = direct_summary.decide(
            direct_summary.load_policy(),
            tool=name,
            verified_caller=verified_caller,
            metadata=metadata,
        )
        logger.info(
            "direct_summary_decision",
            tool=name,
            request_id=ctx.request_id,
            reason=direct_reason,
        )
        if direct_destination is not None:
            direct_status = await asyncio.to_thread(
                direct_summary.deliver,
                data,
                direct_destination,
                request_id=ctx.request_id,
                skill_input=skill_input,
            )
            if direct_status != direct_summary.FAILED:
                return direct_summary.posted_response(
                    direct_status, deferred=bool(data.get("deferred"))
                )
    # ── 返却前ミドルウェア（順序契約・v0.3 監査 Step4-(a)）────────────────────
    # (0.5) 返す欄の絞り込み（skill の mcp_relay_fields・None＝全体）: usage 記録（total_cost_usd
    #     を読む）より後、退避・注入より前。Aico に生データを渡さず文面をそのまま返させる。
    # (1) 長文退避（Task8・USE_PAYLOAD_OFFLOAD 既定OFF）: 切り詰めは注入キーに触れない
    #     よう **リンク注入より先** に行う（逆順だと注入したURLごと切り詰め対象になる）。
    # (2) リンク注入（Task6）: search 応答にだけ Web UI/Aico Vault リンクを差し込む。
    # usage DB/ログ記録は (0)。将来 quota を強制する場合もこの順序契約を保つ。
    if isinstance(data, dict):
        data = _relay_fields(spec, data)
    if isinstance(data, dict):
        from teamagent.mcp_gateway.payload_offload import maybe_offload

        data = maybe_offload(name, data, request_id=ctx.request_id)
    if name == SEARCH_TOOL_NAME and isinstance(data, dict):
        _inject_search_web_links(data)
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, default=str))]


def _relay_fields(spec: ToolSpec, data: dict[str, Any]) -> dict[str, Any]:
    """skill が宣言した欄（``mcp_relay_fields``）だけを残す。宣言が無ければそのまま返す。"""
    fields = getattr(spec.skill_cls, "mcp_relay_fields", None)
    if fields is None:
        return data
    return {key: data[key] for key in fields if key in data}


def _pm_err(code: str) -> list[TextContent]:
    """本人メモの拒否・失敗。固定コードだけを返す。

    発話・例外文・pydantic の input_value は返さない。
    """
    payload = {"error": "personal_memory_rejected", "code": code}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def dispatch_personal_memory_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    identity_resolver: IdentityResolver | None,
    allowed_domains: frozenset[str] | None,
    company_shared_groups: frozenset[str] | None,
    caller_claim_verifier: CallerClaimVerifier | None,
) -> list[TextContent]:
    """本人メモ（personal_memory_*）専用の経路。dispatch_tool を通さない。

    判定の順（docs/architecture/hermes_migration_design.md §10b.3）:
    署名済み claim 必須 → DM（claim の channel を D… で fullmatch）→ 予約 tool_call_id
    （aico-pm-(obs|ctx|cmd)-<32hex> かつ run_id == tool_call_id）→ スレッド不可 → resolver →
    allowlist（空なら全員拒否）→ 入力検証。「連携」振り替え・usage 記録・進捗投稿・長文退避・
    非同期通知はどれも走らせない。例外は外へ出さない（SDK が str(e) を応答に入れるため）。
    """
    received = time.perf_counter()
    outcome = "internal"
    gateway_ms = 0
    try:
        from teamagent.mcp_gateway.personal_memory import gate

        def reject(code: str) -> list[TextContent]:
            nonlocal outcome
            outcome = code
            return _pm_err(code)

        # ① LEGACY（resolver/verifier 無し）では verified_caller が None のまま通るので拒否する
        if identity_resolver is None or caller_claim_verifier is None:
            return reject("PM_UNAVAILABLE")
        raw = arguments.get(USER_CONTEXT_KEY)
        if not isinstance(raw, dict):
            return reject("PM_INVALID_INPUT")
        verified, caller_fail = await _verify_caller(
            arguments,
            tool=name,
            identity_resolver=identity_resolver,
            company_shared_groups=company_shared_groups,
            caller_claim_verifier=caller_claim_verifier,
        )
        if caller_fail is not None:
            return reject("PM_CALLER_REJECTED")
        if verified is None:
            return reject("PM_CALLER_REQUIRED")
        if not gate.is_dm_channel(verified.channel_id):
            return reject("PM_NOT_DM")
        if not gate.reserved_invocation_ok(name, verified.tool_call_id, verified.run_id):
            return reject("PM_INVOCATION_REJECTED")
        if verified.thread_ts is not None:
            return reject("PM_THREAD_REJECTED")
        metadata, fail = await _resolve_metadata(
            raw,
            verified_caller=verified,
            require_rls=True,
            identity_resolver=identity_resolver,
            allowed_domains=allowed_domains,
            company_shared_groups=company_shared_groups,
            tool=name,
        )
        if (
            fail is not None
            or metadata.get("identity_verified") is not True
            or metadata.get("verified_slack_user_id") != verified.slack_user_id
            or metadata.get("verified_slack_team_id") != verified.slack_team_id
        ):
            return reject("PM_IDENTITY_REJECTED")
        email = metadata.get("user_email")
        # 照合するのは resolver が解決した email（_user_context の申告値は使わない）
        if not gate.is_allowed(email):
            return reject("PM_NOT_ALLOWED")

        from pydantic import ValidationError

        from teamagent.adapters.personal_memory_store import (
            PersonalMemoryStoreError,
            Principal,
        )
        from teamagent.mcp_gateway.personal_memory import handle_personal_memory
        from teamagent.mcp_gateway.personal_memory.schemas import INPUT_MODELS

        business = {k: v for k, v in arguments.items() if k != USER_CONTEXT_KEY}
        try:
            payload = INPUT_MODELS[name].model_validate(business)
        except ValidationError:
            return reject("PM_INVALID_INPUT")
        try:
            principal = Principal(
                team_id=verified.slack_team_id,
                slack_user_id=verified.slack_user_id,
                user_email=str(email),
            )
        except PersonalMemoryStoreError:
            return reject("PM_IDENTITY_REJECTED")
        gateway_ms = int((time.perf_counter() - received) * 1000)
        result = await handle_personal_memory(name, principal, verified.message_id, payload)
        outcome = "ok"
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    except Exception as exc:
        outcome = "PM_INTERNAL"
        logger.warning("personal_memory_internal_error", tool=name, error=type(exc).__name__)
        return _pm_err("PM_INTERNAL")
    finally:
        logger.info(
            "personal_memory_call",
            tool=name,
            outcome=outcome,
            gateway_ms=gateway_ms,
            total_ms=int((time.perf_counter() - received) * 1000),
        )


def _afb_err(code: str) -> list[TextContent]:
    """回答評価の拒否・失敗。固定コードだけを返す（トークン・検索語・例外文は返さない）。"""
    payload = {"error": "answer_feedback_rejected", "code": code}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


_answer_feedback_store_singleton: AnswerFeedbackStore | None = None


def _default_answer_feedback_store() -> AnswerFeedbackStore:
    """本番の保存先（RDS・teamagent_app）を遅延生成する。"""
    global _answer_feedback_store_singleton

    if _answer_feedback_store_singleton is None:
        from teamagent.adapters.answer_feedback_store import PgAnswerFeedbackStore

        _answer_feedback_store_singleton = PgAnswerFeedbackStore.from_env()
    return _answer_feedback_store_singleton


async def dispatch_answer_feedback_tool(
    arguments: dict[str, Any],
    *,
    identity_resolver: IdentityResolver | None,
    allowed_domains: frozenset[str] | None,
    company_shared_groups: frozenset[str] | None,
    caller_claim_verifier: CallerClaimVerifier | None,
    store: AnswerFeedbackStore | None = None,
) -> list[TextContent]:
    """回答評価（answer_feedback_record）専用の経路。dispatch_tool を通さない。

    判定の順: 署名済み claim 必須（nonce を消費）→ 予約 tool_call_id（aico-fb-<32hex> かつ
    run_id == tool_call_id＝plugin の直接呼び出し）→ 入力検証 → 評価トークン（署名・期限・
    押した人＝質問した人・team）→ resolver で本人 email → search_feedback へ INSERT。
    「連携」振り替え・usage 記録・進捗投稿は走らせない。例外は外へ出さない。
    ログは outcome・rating だけ（email・検索語・トークンは出さない）。
    """
    received = time.perf_counter()
    outcome = "internal"
    rating: int | None = None
    tool = answer_feedback.ANSWER_FEEDBACK_TOOL_NAME
    try:

        def reject(code: str) -> list[TextContent]:
            nonlocal outcome
            outcome = code
            return _afb_err(code)

        if identity_resolver is None or caller_claim_verifier is None:
            return reject("AFB_UNAVAILABLE")
        raw = arguments.get(USER_CONTEXT_KEY)
        if not isinstance(raw, dict):
            return reject("AFB_INVALID_INPUT")
        verified, caller_fail = await _verify_caller(
            arguments,
            tool=tool,
            identity_resolver=identity_resolver,
            company_shared_groups=company_shared_groups,
            caller_claim_verifier=caller_claim_verifier,
        )
        if caller_fail is not None:
            return reject("AFB_CALLER_REJECTED")
        if verified is None:
            return reject("AFB_CALLER_REQUIRED")
        if not answer_feedback.reserved_invocation_ok(verified.tool_call_id, verified.run_id):
            return reject("AFB_INVOCATION_REJECTED")

        from pydantic import ValidationError

        business = {k: v for k, v in arguments.items() if k != USER_CONTEXT_KEY}
        try:
            payload = answer_feedback.AnswerFeedbackInput.model_validate(business)
        except ValidationError:
            return reject("AFB_INVALID_INPUT")
        try:
            claim = answer_feedback.verify_feedback_token(
                payload.feedback_token,
                key=caller_claim_verifier.derive_purpose_key(answer_feedback.KEY_LABEL),
                now=caller_claim_verifier.now(),
                presser_user_id=verified.slack_user_id,
                team_id=verified.slack_team_id,
            )
        except answer_feedback.AnswerFeedbackTokenError as error:
            return reject(error.code)
        metadata, fail = await _resolve_metadata(
            raw,
            verified_caller=verified,
            require_rls=True,
            identity_resolver=identity_resolver,
            allowed_domains=allowed_domains,
            company_shared_groups=company_shared_groups,
            tool=tool,
        )
        email = metadata.get("user_email")
        if (
            fail is not None
            or metadata.get("identity_verified") is not True
            or metadata.get("verified_slack_user_id") != verified.slack_user_id
            or not isinstance(email, str)
            or not email.strip()
        ):
            return reject("AFB_IDENTITY_REJECTED")

        from teamagent.adapters.answer_feedback_store import (
            AnswerFeedbackRow,
            AnswerFeedbackStoreError,
        )

        try:
            row = AnswerFeedbackRow(
                user_email=email.strip().lower(),
                query=claim.query,
                rating=payload.rating,
                answer_id=claim.answer_id,
                search_session_id=answer_feedback.slack_search_session_id(claim.answer_id),
                note=json.dumps({"tools": claim.tools}, separators=(",", ":"))
                if claim.tools
                else None,
            )
            target = store if store is not None else _default_answer_feedback_store()
            await asyncio.to_thread(target.insert, row)
        except AnswerFeedbackStoreError as error:
            logger.warning("answer_feedback_store_failed", code=error.code)
            return reject("AFB_STORE_FAILED")
        except Exception as error:
            logger.warning("answer_feedback_store_failed", code=type(error).__name__)
            return reject("AFB_STORE_FAILED")
        outcome = "ok"
        rating = payload.rating
        result = {"ok": True, "rating": payload.rating}
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    except Exception as exc:
        outcome = "AFB_INTERNAL"
        logger.warning("answer_feedback_internal_error", error=type(exc).__name__)
        return _afb_err("AFB_INTERNAL")
    finally:
        logger.info(
            "answer_feedback_call",
            outcome=outcome,
            rating=rating,
            total_ms=int((time.perf_counter() - received) * 1000),
        )


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
        return _err(_exception_text(e), request_id=request_id)

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
    answer_feedback_store: AnswerFeedbackStore | None = None,
) -> Server:
    """TeamAgent MCP サーバを構築する（specs 省略時は本番ツールを遅延構築）。

    ``answer_feedback_store`` はテスト用の注入口（未指定なら本番は RDS へ遅延生成）。
    """
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
    # 本人メモのツールを ToolSpec にすると list_tools と L2 のツール面に出てしまうので禁じる
    if any(n.startswith("personal_memory") for n in by_name):
        raise RuntimeError("personal_memory_* must not be registered as a ToolSpec")
    # 回答評価も同じ（モデルのツール面に出さない・plugin の直接呼び出しだけ）
    if answer_feedback.ANSWER_FEEDBACK_TOOL_NAME in by_name:
        raise RuntimeError("answer_feedback_record must not be registered as a ToolSpec")
    enable_orchestrator = _envflag("USE_AGENT_ORCHESTRATOR")
    enable_answer_feedback = _envflag(answer_feedback.ANSWER_FEEDBACK_FLAG_ENV)
    enable_personal_memory = _envflag("USE_PERSONAL_MEMORY")
    if enable_personal_memory:
        from teamagent.mcp_gateway.personal_memory.gate import install_sdk_warning_filter

        install_sdk_warning_filter()
    server: Server = Server("teamagent")
    # M7: mcp 全体の同時実行の上限（既定 OFF＝None＝素通り）。本人メモは上限の外（軽い経路）。
    from teamagent.mcp_gateway import capacity

    request_gate = capacity.gate_from_env()

    @server.list_tools()
    async def _list() -> list[Tool]:
        return list_all_tool_defs(specs, enable_orchestrator=enable_orchestrator)

    @server.call_tool()
    async def _call(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        # 本人メモ（list_tools に出さない）。フラグ off なら未登録ツールと同じ応答にし、
        # claim の検証（nonce の消費）にも DB にも触れない。dispatch_tool より前に振り分ける。
        if name in PERSONAL_MEMORY_TOOL_NAMES:
            if not enable_personal_memory:
                return _err(f"unknown tool: {name}")
            return await dispatch_personal_memory_tool(
                name,
                arguments,
                identity_resolver=identity_resolver,
                allowed_domains=allowed_domains,
                company_shared_groups=company_shared_groups,
                caller_claim_verifier=caller_claim_verifier,
            )
        # 回答評価（list_tools に出さない）。フラグ off なら未登録ツールと同じ応答にし、
        # claim の検証（nonce の消費）にも DB にも触れない。
        if name == answer_feedback.ANSWER_FEEDBACK_TOOL_NAME:
            if not enable_answer_feedback:
                return _err(f"unknown tool: {name}")
            return await dispatch_answer_feedback_tool(
                arguments,
                identity_resolver=identity_resolver,
                allowed_domains=allowed_domains,
                company_shared_groups=company_shared_groups,
                caller_claim_verifier=caller_claim_verifier,
                store=answer_feedback_store,
            )
        if request_gate is not None:
            from teamagent.runtime.request_gate import GateTimeoutError, QueueFullError

            try:
                return await request_gate.submit(_dispatch, name, arguments)
            except QueueFullError:
                return capacity.overloaded(name, "queue_full", request_gate)
            except GateTimeoutError:
                return capacity.overloaded(name, "timeout", request_gate)
        return await _dispatch(name, arguments)

    async def _dispatch(name: str, arguments: dict[str, Any]) -> list[TextContent]:
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
    from teamagent.mcp_gateway.async_job_notify import start_recovery

    start_recovery()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """stdio で MCP サーバを起動する CLI エントリポイント（resolver 必須）。"""
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
