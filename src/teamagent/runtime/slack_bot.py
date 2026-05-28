"""Slack Bot ランタイム（Socket Mode）。

Sprint 1 末：mention テキストを SearchSkill にディスパッチして結果を返す。
DM では Sprint 1 時点では echo（次の Sprint で SearchSkill 接続）。

Usage:
    SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... \\
    python -m teamagent.runtime.slack_bot

CLAUDE.md 6-bis：
- 3層分離：本ファイルは Runtime 層。Slack API / Bedrock / pgvector は adapters 経由
- 構造化ログ：request_id を毎イベント生成して伝播
- prompt のファイル化：SearchSkill 経由で prompts/search/v1/system.md を読む
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from typing import Any

import structlog
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from teamagent.adapters.slack_client import SlackClient
from teamagent.observability.sentry import (
    capture_event_exception,
    capture_skill_exception,
)
from teamagent.skills.base import SkillContext
from teamagent.skills.router import SkillRouter
from teamagent.skills.search.schema import SearchInput, SearchOutput

logger = structlog.get_logger(__name__)


# Slack の @mention は <@U12345> 形式で来るので、テキストから剥がすための正規表現
_MENTION_PATTERN = re.compile(r"<@[A-Z0-9]+>\s*")


def strip_mention(text: str) -> str:
    """app_mention イベントのテキストから先頭の `<@BOT_ID>` を取り除く。

    例:
        "<@U082ABC> A社の前回提案は？" → "A社の前回提案は？"
    """
    return _MENTION_PATTERN.sub("", text, count=1).strip()


def _slack_thread_permalink(source_uri: str) -> str | None:
    """slack://CHANNEL_ID/THREAD_TS を Slack permalink URL に変換する。

    変換例: slack://C091ZSVTKF1/1748244936.050099
          → https://vectorinc.slack.com/archives/C091ZSVTKF1/p1748244936050099

    SLACK_WORKSPACE 環境変数が未設定の場合は None を返す。
    """
    # SLACK_WORKSPACE はワークスペース名のみ（例: "vectorinc"）
    workspace = os.environ.get("SLACK_WORKSPACE")
    if not workspace:
        return None
    if not source_uri.startswith("slack://"):
        return None
    rest = source_uri[len("slack://") :]
    parts = rest.split("/", 1)
    if len(parts) != 2:
        return None
    channel_id, thread_ts = parts[0], parts[1]
    # "1748244936.050099" → "1748244936050099" (小数点を除去)
    ts_digits = thread_ts.replace(".", "")
    return f"https://{workspace}.slack.com/archives/{channel_id}/p{ts_digits}"


# /teamagent_search で受け付けるオプションのホワイトリスト
# 未知キーは query にそのまま残す（=有効な値が誤って options に吸われない fail-safe）
_SLASH_COMMAND_ALLOWED_KEYS: frozenset[str] = frozenset({"industry", "top_k"})

_KV_PATTERN = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<val>\"[^\"]*\"|\S+)")


def parse_command_text(text: str) -> tuple[str, dict[str, str]]:
    """Slack スラッシュコマンドの text を「自然文クエリ + key=value オプション」に分解する。

    例:
        "案件 industry=飲食"             → ("案件", {"industry": "飲食"})
        "industry=飲食 top_k=10 PR事例"  → ("PR事例", {"industry": "飲食", "top_k": "10"})
        'industry="飲食 業界" 案件'       → ("案件", {"industry": "飲食 業界"})

    未知のキー（ホワイトリスト外）は options に取らず、query 側にそのまま残す。
    "Escape channels, users, and links" は Slack App 設定で OFF にする前提。
    """
    options: dict[str, str] = {}

    def _take(m: re.Match[str]) -> str:
        key = m.group("key")
        if key not in _SLASH_COMMAND_ALLOWED_KEYS:
            return m.group(0)  # 未知キーは query に残す
        val = m.group("val").strip('"')
        options[key] = val
        return ""

    residual = _KV_PATTERN.sub(_take, text)
    query = " ".join(residual.split()).strip()
    return query, options


def _format_hit_source_label(hit: Any) -> str:
    """SearchHitOut から「出典 + ページ」の表示ラベルを組み立てる。

    優先順位：
      1. source_type='slack' → 💬 channel_name（新スキーマ）
      2. file_name + page_num（構造化、Sprint 2 で追加）
      3. source 文字列（後方互換）
      4. chunk #N（最終フォールバック）
    """
    source_type = getattr(hit, "source_type", None)
    if source_type == "slack":
        channel = getattr(hit, "channel_name", None) or "Slack"
        return f"💬 *{channel}*"
    file_name = getattr(hit, "file_name", None)
    page_num = getattr(hit, "page_num", None)
    if file_name:
        if page_num is not None:
            return f"📄 *{file_name}* (p.{page_num})"
        return f"📄 *{file_name}*"
    if hit.source:
        return f"📄 {hit.source}"
    return f"chunk #{hit.chunk_id}"


def format_search_response(output: SearchOutput) -> str:
    """SearchOutput を Slack に表示する文字列（フォールバック / 通知用）に整形する。

    Block Kit を使う場合も text フィールドにこれを入れて、通知やインデックス用に保持する。
    引用フォーマット：📄 file_name (p.N) — score=0.91 → Drive で開く
    """
    lines = [output.answer, ""]
    if output.hits:
        lines.append("*参考資料:*")
        for hit in output.hits[:5]:
            label = _format_hit_source_label(hit)
            link = f" → <{hit.drive_url}|Drive で開く>" if hit.drive_url else ""
            lines.append(f"• {label}  _score={hit.score:.2f}_{link}")
    lines.append("")
    lines.append(f"_推算コスト: ${output.total_cost_usd:.4f}_")
    return "\n".join(lines)


def build_search_blocks(output: SearchOutput) -> list[dict[str, Any]]:
    """SearchOutput を Slack Block Kit に整形する。

    Drive URL があれば各 hit を「Drive で開く」ボタン付きで表示する。
    Block Kit が無効な環境（通知中心）でも text フィールドで読める形を保つ。
    """
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": output.answer},
        }
    ]
    if output.hits:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "*参考資料*"}],
            }
        )
        for hit in output.hits[:5]:
            label = _format_hit_source_label(hit)
            # 出典 + score を1行で見やすく（score は item context として末尾に）
            line = f"• {label}  _score={hit.score:.2f}_"
            section: dict[str, Any] = {
                "type": "section",
                "text": {"type": "mrkdwn", "text": line},
            }
            # ボタン優先順位: Slack thread > Drive（source_type で判定）
            source_type = getattr(hit, "source_type", None)
            source_uri = getattr(hit, "source_uri", None)
            if source_type == "slack" and source_uri:
                permalink = _slack_thread_permalink(source_uri)
                if permalink:
                    section["accessory"] = {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "💬 Slack で開く"},
                        "url": permalink,
                        "action_id": f"open_slack_{hit.chunk_id}",
                    }
            elif hit.drive_url:
                section["accessory"] = {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📎 Drive で開く"},
                    "url": hit.drive_url,
                    "action_id": f"open_drive_{hit.chunk_id}",
                }
            blocks.append(section)

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"_推算コスト: ${output.total_cost_usd:.4f}_",
                }
            ],
        }
    )
    return blocks


class SkillDispatcher:
    """mention テキストを Skill に振り分けて結果を返す。

    Sprint 1 末：常に "search" Skill にディスパッチ。
    Sprint 2+ でルールベース or Claude Haiku ベースのルーターを実装。
    """

    def __init__(self, router: SkillRouter | None = None) -> None:
        self._skill_cache: dict[str, Any] = {}
        # Slack user_id → email cache（RLS GUC 注入用、users.info の呼び出しを削減）
        self._user_email_cache: dict[str, str | None] = {}
        if router is not None:
            self._router = router
        else:
            # USE_LLM_ROUTER=true で Haiku 4.5 ベースの自然文判定を有効化
            use_llm_router = os.environ.get("USE_LLM_ROUTER", "false").lower() in (
                "1",
                "true",
                "yes",
            )
            if use_llm_router:
                from teamagent.adapters.bedrock_client import BedrockClient

                haiku_model_id = os.environ.get(
                    "BEDROCK_HAIKU_MODEL_ID",
                    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                )
                haiku = BedrockClient(
                    region=os.environ.get("AWS_REGION", "us-east-1"),
                    model_id=haiku_model_id,
                )
                self._router = SkillRouter(bedrock=haiku)
                logger.info("router_initialized", llm_fallback=True)
            else:
                self._router = SkillRouter()
                logger.info("router_initialized", llm_fallback=False)

    def get_search_skill(self) -> Any:
        """SearchSkill インスタンスをキャッシュして返す（embedder ロードが重い）。

        環境変数 USE_CONTEXTUAL=true で Contextual Retrieval 版に切替。
        proposals_chunks_contextual テーブルを参照、contextualized_text 列を検索。

        Skill ごとに __init__ 引数が異なるため、ここでは search 専用の生成ロジックを持つ。
        Sprint 2 で Router を導入したら抽象化する。
        """
        if "search" in self._skill_cache:
            return self._skill_cache["search"]
        # 動的 instance 生成。Skill 固有の init 引数を扱うため Any 経由
        from teamagent.adapters.embeddings_client import LocalE5Embedder
        from teamagent.skills.search.skill import SearchSkill

        use_contextual = os.environ.get("USE_CONTEXTUAL", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        use_new_schema = os.environ.get("USE_NEW_SCHEMA", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        # Day 8 (2026-05-28) Phase 2: Slack 営業 FB の client_name で Drive 資料を裏で検索して
        # 「関連資料」として attach する機能。USE_FB_DRIVE_MATCH=true で有効化。
        use_fb_drive_match = os.environ.get("USE_FB_DRIVE_MATCH", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        # Day 8 (2026-05-28) Sprint 4-A: Cohere Rerank v3.5 (Bedrock 東京)。
        # USE_COHERE_RERANK=true で有効化、top_k=30 retrieve → Rerank → top-5。
        # Anthropic ベンチで失敗率 -67%、$2/1000 queries (10560 query/月で $21 想定)。
        use_cohere_rerank = os.environ.get("USE_COHERE_RERANK", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        # Day 8 (2026-05-28) Sprint 4-B: prompt v2 (insight + actionable thinking)。
        # PROMPT_VERSION=v1 / v2 / v2c で切替。デフォルト v1 (既存挙動互換)。
        # v2c は v2 の compact 版 (Sprint 4-D, latency 短縮目的)。
        prompt_version = os.environ.get("PROMPT_VERSION", "v1")
        # Day 8 (2026-05-28) Sprint 4-D: max_tokens 制限で latency 短縮。
        # 既定 4096、v2c と組合せて 1200 程度で運用想定 (46s → 20s 以下狙い)。
        try:
            summary_max_tokens = int(os.environ.get("SEARCH_MAX_TOKENS", "4096"))
        except ValueError:
            summary_max_tokens = 4096
        instance = SearchSkill(
            embedder=LocalE5Embedder(),
            use_contextual=use_contextual,
            use_new_schema=use_new_schema,
            use_fb_drive_match=use_fb_drive_match,
            use_cohere_rerank=use_cohere_rerank,
            prompt_version=prompt_version,
            summary_max_tokens=summary_max_tokens,
        )
        logger.info(
            "search_skill_initialized",
            use_contextual=use_contextual,
            use_new_schema=use_new_schema,
            use_fb_drive_match=use_fb_drive_match,
            use_cohere_rerank=use_cohere_rerank,
            prompt_version=prompt_version,
            summary_max_tokens=summary_max_tokens,
        )
        self._skill_cache["search"] = instance
        return instance

    async def _resolve_user_email(self, user_id: str | None) -> str | None:
        """Slack user_id → email を解決する（RLS 評価用、users.info キャッシュ）。

        SLACK_BOT_TOKEN に users:read.email スコープが必要。
        失敗時は None を返す（RLS 経由で fail-safe に何も見えなくなる）。
        """
        if not user_id or user_id == "unknown":
            return None
        if user_id in self._user_email_cache:
            return self._user_email_cache[user_id]
        try:
            from teamagent.adapters.slack_client import SlackClient

            slack = SlackClient(bot_token=os.environ.get("SLACK_BOT_TOKEN", ""))
            # slack_sdk の users_info を直接叩く（adapter に専用 method 未実装のため暫定）
            resp = await slack._client.users_info(user=user_id)
            profile: dict[str, Any] = (resp.get("user") or {}).get("profile", {}) or {}
            email = profile.get("email")
            self._user_email_cache[user_id] = email
            logger.info(
                "slack_user_email_resolved",
                user_id=user_id,
                resolved=bool(email),
            )
            return email
        except Exception:
            logger.exception("slack_user_email_resolve_failed", user_id=user_id)
            return None

    async def run_search(
        self,
        query: str,
        request_id: str,
        user_id: str | None,
        *,
        filter_industry_override: str | None = None,
        top_k: int = 5,
    ) -> SearchOutput:
        """SearchSkill を別スレッドで実行（同期 I/O が含まれるため）。

        SkillRouter で クエリを判定し、industry キーワードが含まれていれば
        SearchInput.filter_industry に自動付与する。

        filter_industry_override / top_k はスラッシュコマンドから明示指定する用途。
        - filter_industry_override が非 None なら Router の auto-detection より優先
        - top_k は 1〜20 の範囲にサニタイズ（呼び出し側で済んでいる前提だが二重防御）

        Slack user_id → email を解決して ctx.metadata['user_email'] に注入。
        SearchSkill 側で PgVectorClient.connection(app_role='teamagent_app',
        user_email=...) として RLS policy が評価される（documents/chunks 切替後に有効）。
        """
        decision = self._router.route(query, request_id=request_id)
        logger.info(
            "skill_router_decision",
            request_id=request_id,
            query_type=decision.query_type.value,
            confidence=decision.confidence,
            filter=decision.extracted_filter,
            reason=decision.reason,
            filter_industry_override=filter_industry_override,
            top_k=top_k,
        )

        # 明示指定が最優先、無ければ router の自動検出
        filter_industry = filter_industry_override or decision.extracted_filter.get("industry")
        # strict_industry: スラッシュコマンドで明示指定された場合のみ True。
        # Router 自動付与は soft (industry IS NULL も許容) で Slack docs を巻き込み除外しない。
        strict_industry = filter_industry_override is not None
        # 注：meta / compare は今は通常検索で代用（Sprint 2 で本格実装）
        # query_type=COMPARE/META はログ出すだけで content と同じ動作にする

        # top_k を 1〜20 にサニタイズ（API 経由の異常値防御）
        top_k_safe = max(1, min(top_k, 20))

        # RLS 評価用に Slack user_id → email を解決
        user_email = await self._resolve_user_email(user_id)

        # 会社思想 (Day 7, 2026-05-27): 「資料は全て共有物」
        # user_email の domain を user_groups に自動注入する。
        # → documents.acl_groups に 'vectorinc.co.jp' / 'domain名' が入っていれば
        #   RLS の acl_groups intersect で workspace 全員に見せられる。
        # 将来 Slack User Group → group email[] 解決時に追加で merge する。
        user_groups: list[str] = []
        if user_email and "@" in user_email:
            user_groups.append(user_email.split("@", 1)[1])  # 'vectorinc.co.jp'

        skill = self.get_search_skill()
        ctx = SkillContext(
            request_id=request_id,
            user_id=user_id,
            metadata={
                # PgVectorClient.connection() に渡される RLS GUC
                "user_email": user_email,
                "user_groups": user_groups,
                "user_role": "member",
            },
        )
        input_obj = SearchInput(
            query=query,
            top_k=top_k_safe,
            filter_industry=filter_industry,
            strict_industry=strict_industry,
        )
        loop = asyncio.get_running_loop()
        output: SearchOutput = await loop.run_in_executor(
            None,
            skill.run,
            input_obj,
            ctx,
        )
        return output


def build_app(dispatcher: SkillDispatcher | None = None) -> AsyncApp:
    """Bolt AsyncApp を構築する。

    SLACK_BOT_TOKEN は必須。
    Socket Mode で動かすには SLACK_APP_TOKEN も必要（main() でチェック）。
    """
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("SLACK_BOT_TOKEN が未設定です")

    app = AsyncApp(token=bot_token)
    slack = SlackClient(bot_token=bot_token)
    disp = dispatcher or SkillDispatcher()

    @app.event("app_mention")
    async def handle_app_mention(event: dict[str, Any]) -> None:
        request_id = f"req-{uuid.uuid4().hex[:12]}"
        user_id = event.get("user", "unknown")
        raw_text = event.get("text", "")
        query = strip_mention(raw_text)
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts")

        logger.info(
            "slack_app_mention_dispatch",
            request_id=request_id,
            user_id=user_id,
            channel=channel,
            raw_len=len(raw_text),
            query_len=len(query),
        )

        if not query:
            await slack.post_message(
                channel=channel,
                text="何か質問してください。例: `@TeamAgent A社の前回提案は？`",
                request_id=request_id,
                thread_ts=thread_ts,
            )
            return

        try:
            output = await disp.run_search(query, request_id, user_id)
        except Exception as e:
            logger.exception("search_skill_failed", request_id=request_id)
            # Sentry へ送信（DSN 未設定なら no-op）。スクラブは before_send で実施
            capture_skill_exception(
                e,
                request_id=request_id,
                skill="search",
                user_id=user_id,
                extra={"channel": channel, "query_len": len(query)},
            )
            await slack.post_message(
                channel=channel,
                text=f"検索中にエラーが発生しました。`request_id={request_id}`",
                request_id=request_id,
                thread_ts=thread_ts,
            )
            return

        await slack.post_message(
            channel=channel,
            text=format_search_response(output),
            request_id=request_id,
            thread_ts=thread_ts,
            blocks=build_search_blocks(output),
        )

    @app.event("message")
    async def handle_message(event: dict[str, Any]) -> None:
        # bot 自身のメッセージは無視
        if event.get("bot_id"):
            return
        if event.get("channel_type") != "im":
            return  # DM のみ反応

        request_id = f"req-{uuid.uuid4().hex[:12]}"
        user_id = event.get("user", "unknown")
        channel = event.get("channel", "")
        text = event.get("text", "")

        logger.info(
            "slack_dm",
            request_id=request_id,
            user_id=user_id,
            channel=channel,
            text_len=len(text),
        )

        if not text:
            return

        try:
            output = await disp.run_search(text, request_id, user_id)
        except Exception as e:
            logger.exception("search_skill_failed", request_id=request_id)
            capture_skill_exception(
                e,
                request_id=request_id,
                skill="search",
                user_id=user_id,
                extra={"channel": channel, "text_len": len(text), "via": "dm"},
            )
            await slack.post_message(
                channel=channel,
                text=f"検索中にエラーが発生しました。`request_id={request_id}`",
                request_id=request_id,
            )
            return

        await slack.post_message(
            channel=channel,
            text=format_search_response(output),
            request_id=request_id,
            blocks=build_search_blocks(output),
        )

    @app.command("/teamagent_search")
    async def handle_teamagent_search(
        ack: Any,
        respond: Any,
        command: dict[str, Any],
    ) -> None:
        """Slack スラッシュコマンド `/teamagent_search <query> [industry=...] [top_k=N]`

        3 秒制約: ack() を先頭で即時に呼ぶ。検索本体は response_url 経由で `respond()` する。
        コマンドが何も渡されなかったら使い方を ephemeral で返す。
        エラーは ephemeral で本人にだけ通知（チャネルを汚さない）。
        """
        await ack()

        request_id = f"req-{uuid.uuid4().hex[:12]}"
        user_id = command.get("user_id")
        channel_id = command.get("channel_id", "")
        raw_text = (command.get("text") or "").strip()

        logger.info(
            "slack_slash_command",
            request_id=request_id,
            user_id=user_id,
            channel=channel_id,
            text_len=len(raw_text),
        )

        if not raw_text:
            await respond(
                response_type="ephemeral",
                text=(
                    "使い方: `/teamagent_search <自然文クエリ> [industry=飲食] [top_k=10]`\n"
                    "例: `/teamagent_search 飲食店PR事例 industry=飲食 top_k=5`"
                ),
            )
            return

        query, options = parse_command_text(raw_text)
        if not query:
            await respond(
                response_type="ephemeral",
                text=(
                    "クエリ本文が空です。`/teamagent_search 飲食店事例` のように指定してください。"
                ),
            )
            return

        filter_industry = options.get("industry")
        top_k_raw = options.get("top_k", "5")
        try:
            top_k_val = int(top_k_raw)
        except ValueError:
            top_k_val = 5

        try:
            output = await disp.run_search(
                query,
                request_id,
                user_id,
                filter_industry_override=filter_industry,
                top_k=top_k_val,
            )
        except Exception as e:
            logger.exception("slash_command_search_failed", request_id=request_id)
            capture_skill_exception(
                e,
                request_id=request_id,
                skill="search",
                user_id=user_id,
                extra={"channel": channel_id, "via": "slash"},
            )
            await respond(
                response_type="ephemeral",
                text=f"検索中にエラーが発生しました。`request_id={request_id}`",
            )
            return

        # 結果は in_channel で公開（営業同士で共有価値があるため）。
        # コマンド本人のみに見せたいなら response_type="ephemeral" に変える。
        await respond(
            response_type="in_channel",
            text=format_search_response(output),
            blocks=build_search_blocks(output),
        )

    # Bolt のグローバルエラーハンドラ — ハンドラ外で起きた例外を Sentry に飛ばす
    @app.error
    async def handle_bolt_error(error: BaseException, body: dict[str, Any]) -> None:
        event_type = (body.get("event") or {}).get("type") or body.get("type") or "unknown"
        logger.exception(
            "bolt_global_error",
            event_type=event_type,
        )
        capture_event_exception(
            error,
            event_type=f"bolt:{event_type}",
            extra={
                "team_id": body.get("team_id"),
                "api_app_id": body.get("api_app_id"),
            },
        )

    return app


def _asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """asyncio Task で握りつぶされた例外を Sentry / structlog に拾う。

    Bolt / Socket Mode の long-running loop では `fire_and_forget` 的に
    タスクが落ちると default handler は warning だけ出して終わる。
    ここで明示的に Sentry に飛ばす。
    """
    exc = context.get("exception")
    message = context.get("message", "asyncio_unhandled")
    logger.error(
        "asyncio_unhandled_exception",
        message=message,
        exc_type=type(exc).__name__ if exc else None,
    )
    if isinstance(exc, BaseException):
        capture_event_exception(exc, event_type="asyncio:unhandled", extra={"message": message})


async def _run() -> None:
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        raise RuntimeError("SLACK_APP_TOKEN が未設定です（xapp- で始まる Socket Mode 用トークン）")

    # Sentry init は async 文脈内で実施
    # （AsyncioIntegration が起動済み event loop を取りこぼさないため）
    from teamagent.observability.sentry import init_sentry

    sentry_enabled = init_sentry()

    # asyncio Task 内の握りつぶされた例外を Sentry に拾う
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_asyncio_exception_handler)

    app = build_app()
    handler = AsyncSocketModeHandler(app, app_token)
    logger.info("slack_bot_start", mode="socket", sentry_enabled=sentry_enabled)
    await handler.start_async()  # type: ignore[no-untyped-call]


def main() -> None:
    """CLI エントリポイント。"""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
