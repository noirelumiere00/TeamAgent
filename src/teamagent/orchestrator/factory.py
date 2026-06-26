"""本番 Skill を SDK オーケストレーターのツールへ束ねる工場（Phase 1）.

fixture → 実 Skill の差し替えは `ToolSpec.factory` に本物の Skill インスタンスを渡すだけ。
重い依存（LocalE5Embedder / boto3 / psycopg）は **関数内 遅延 import** にしてあるので、
本モジュールの import 自体は軽量（heavy deps が無い環境でも import できる）。
実構築は `build_production_tools()` を呼んだ時に初めて起こる。

⚠️ 実行要件: 実 Skill の依存（pgvector(RDS)+SSMトンネル / Bedrock / LocalE5Embedder）が必要。
   env フラグ解決は runtime/slack_bot.py:get_search_skill と一致させている（将来は共通化したい）。
"""

from __future__ import annotations

import os
from typing import Any

from .tools import ToolSpec


def _envflag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


def _build_search_skill() -> Any:
    """実 SearchSkill を本番 runtime と同じ env フラグで構築（依存は内部で遅延生成）.

    参照: runtime/slack_bot.py:get_search_skill（同じフラグ・既定値に揃える）。
    """
    from teamagent.adapters.embeddings_client import LocalE5Embedder
    from teamagent.skills.search.skill import SearchSkill

    try:
        summary_max_tokens = int(os.environ.get("SEARCH_MAX_TOKENS", "800"))
    except ValueError:
        summary_max_tokens = 800
    try:
        min_relevance = float(os.environ.get("SEARCH_MIN_RELEVANCE", "0.0"))
    except ValueError:
        min_relevance = 0.0
    try:
        # 2段階しきい値の fallback（既定 0.0 = 無効＝従来挙動）。
        min_relevance_fallback = float(os.environ.get("SEARCH_MIN_RELEVANCE_FALLBACK", "0.0"))
    except ValueError:
        min_relevance_fallback = 0.0
    try:
        # Rerank 候補プール（dense retrieval を何件 rerank に渡すか）。既定 30＝従来挙動。
        # 固有名詞クエリのリコール改善を試すための可変ノブ（SEARCH_RERANK_POOL_SIZE）。
        rerank_pool_size = int(os.environ.get("SEARCH_RERANK_POOL_SIZE", "30"))
    except ValueError:
        rerank_pool_size = 30

    return SearchSkill(
        embedder=LocalE5Embedder(),
        use_contextual=_envflag("USE_CONTEXTUAL"),
        use_new_schema=_envflag("USE_NEW_SCHEMA"),
        use_fb_drive_match=_envflag("USE_FB_DRIVE_MATCH"),
        use_cohere_rerank=_envflag("USE_COHERE_RERANK"),
        rerank_pool_size=rerank_pool_size,
        min_relevance=min_relevance,
        min_relevance_fallback=min_relevance_fallback,
        # client-boost は A/B で +4pp 実証済み・固有名詞のみ発火で副作用なし・DB障害時は
        # fail-open（語彙取得失敗→ブースト無効）。よって orchestrator では既定 ON を採用
        # （USE_CLIENT_BOOST=false で明示無効化は可能）。
        use_client_boost=_envflag("USE_CLIENT_BOOST", "true"),
        use_aggregation_mode=_envflag("USE_AGGREGATION_MODE"),
        prompt_version=os.environ.get("PROMPT_VERSION", "v2d"),
        summary_max_tokens=summary_max_tokens,
    )


def build_production_tools() -> list[ToolSpec]:
    """本番 Skill を ToolSpec 群へ束ねる（Phase 1-2: search + clientkarte + proposal_draft/review）.

    SearchSkill は 1 インスタンスを共有（embedder 二重ロード回避）。proposal_draft / proposal_review
    は内部で SearchSkill.retrieve_hits を再利用するため **同じ search を注入**する
    （runtime/slack_bot.py と同じ共有方針）。clientkarte は pgvector 直で search 非依存。
    """
    from teamagent.skills.clientkarte.skill import ClientKarteSkill
    from teamagent.skills.proposal.skill import ProposalDraftSkill
    from teamagent.skills.proposal_review.skill import ProposalReviewSkill
    from teamagent.skills.search.skill import SearchSkill

    search = _build_search_skill()  # 共有インスタンス
    specs = [
        ToolSpec(SearchSkill.name, SearchSkill.description, SearchSkill, factory=lambda: search),
        ToolSpec(ClientKarteSkill.name, ClientKarteSkill.description, ClientKarteSkill),
        ToolSpec(
            ProposalDraftSkill.name,
            ProposalDraftSkill.description,
            ProposalDraftSkill,
            factory=lambda: ProposalDraftSkill(search=search),
        ),
        ToolSpec(
            ProposalReviewSkill.name,
            ProposalReviewSkill.description,
            ProposalReviewSkill,
            factory=lambda: ProposalReviewSkill(search=search),
        ),
    ]

    # Phase 6 (6d): Mail 制約ツール。**既定 OFF**（USE_MAIL_TOOLS=1 で opt-in）。
    # 実行時に run() が G1 本人受信箱限定 / G2 本人同意（MAIL_CONSENT_EMAILS）を
    # fail-closed で強制。実受信箱接続（6c）の人間ゲート（同意/DWD/CASA）承認後に有効化。
    if _envflag("USE_MAIL_TOOLS"):
        from teamagent.skills.mail_constraints.skill import MailConstraintsSkill

        specs.append(
            ToolSpec(
                MailConstraintsSkill.name,
                MailConstraintsSkill.description,
                MailConstraintsSkill,
                factory=lambda: MailConstraintsSkill(),
            )
        )

    # Workspace 横断ツール（カレンダー予定・連絡先）。**既定 OFF**（USE_WORKSPACE_TOOLS=1）。
    # 本人 OAuth（TokenStore）で本人の Workspace のみ参照。未連携は run() が fail-closed。
    # 実トークン解決は OAUTH_KMS_KEY_ID + RDS（RdsTokenStore）。無ければ InMemory（空=未連携）。
    if _envflag("USE_WORKSPACE_TOOLS"):
        from teamagent.skills.workspace_search.skill import WorkspaceSearchSkill

        token_store = _build_token_store()
        specs.append(
            ToolSpec(
                WorkspaceSearchSkill.name,
                WorkspaceSearchSkill.description,
                WorkspaceSearchSkill,
                factory=lambda: WorkspaceSearchSkill(token_store=token_store),
            )
        )

    # §L Phase1: スクレイプ/動画ツール。既定OFF（USE_VIDEO_TOOLS/USE_TIKTOK_TOOLS=1 で opt-in）。
    # 会社共有・読取専用（per-user非依存）。OpenClaw は native exec/browser 不使用＝MCP ツール越しに
    # 取得（実スクレイプ Puppeteer/yt-dlp/ffmpeg は金庫内で実行）。依存は run() で遅延生成。
    if _envflag("USE_VIDEO_TOOLS"):
        from teamagent.skills.video.skill import VideoAnalysisSkill
        from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill

        specs.append(
            ToolSpec(VideoAnalysisSkill.name, VideoAnalysisSkill.description, VideoAnalysisSkill)
        )
        specs.append(
            ToolSpec(VideoAlgorithmSkill.name, VideoAlgorithmSkill.description, VideoAlgorithmSkill)
        )

    if _envflag("USE_TIKTOK_TOOLS"):
        from teamagent.skills.tiktok_search.skill import TikTokSearchSkill

        specs.append(
            ToolSpec(TikTokSearchSkill.name, TikTokSearchSkill.description, TikTokSearchSkill)
        )

    # operation_log: Slackスレッド営業会話 → CRM 転記用の構造化ログ（フェーズ/アクション/BANT）。
    # 既定 OFF（USE_OPERATION_LOG_TOOLS=1 で opt-in）。Bedrock/Slack 依存は run() で遅延生成。
    # 既存のSlack配線（slack_bot.py）と独立して MCP 経由でも呼べる（factory 登録だけが必要）。
    if _envflag("USE_OPERATION_LOG_TOOLS"):
        from teamagent.skills.operation_log.skill import OperationLogSkill

        specs.append(
            ToolSpec(
                OperationLogSkill.name,
                OperationLogSkill.description,
                OperationLogSkill,
            )
        )

    # proposal_deck: 商材情報+研究素材 → FMT v2 95項目 → .pptx 生成。**既定 OFF**
    # （USE_PROPOSAL_DECK_TOOLS=1 で opt-in）。run() は TEAMAGENT_FMT_TEMPLATE（FMT v2 .pptx の
    # 実パス）が無いと ValueError/FileNotFoundError になるため、テンプレを provision してから
    # 有効化する（他の任意スキルと同じ gate パターンに統一）。Agent は search/proposal_draft/
    # clientkarte で素材を集めてから research_material に渡して呼ぶ。
    if _envflag("USE_PROPOSAL_DECK_TOOLS"):
        from teamagent.skills.proposal_deck.skill import ProposalDeckSkill

        specs.append(
            ToolSpec(
                ProposalDeckSkill.name,
                ProposalDeckSkill.description,
                ProposalDeckSkill,
            )
        )

    # proposal_campaign: KW群 → 並列で TikTok 1位の実物サムネ → {58-92}枠の evidence_images。
    # **既定 OFF**（USE_PROPOSAL_CAMPAIGN_TOOLS=1 で opt-in）。video_algorithm と同列の取得系で、
    # 並列検索/サムネ取得/正規化は skill 内 ThreadPool に閉じる（OC は 1 回呼ぶだけ）。OC 露出は
    # openclaw.config.json5 の toolFilter.include に追加してから（=人間ゲート）。
    if _envflag("USE_PROPOSAL_CAMPAIGN_TOOLS"):
        from teamagent.skills.proposal_campaign.skill import ProposalCampaignSkill

        specs.append(
            ToolSpec(
                ProposalCampaignSkill.name,
                ProposalCampaignSkill.description,
                ProposalCampaignSkill,
            )
        )

    # メール×社内ナレッジ横断ツール（read-only・per-user OAuth）。**既定 OFF**
    # （USE_MAIL_LINK_TOOL=1）。本番 Slack Bot へは intent.py + slack_bot.py 経由で届くため、
    # ここはオーケストレータ（Agent SDK）用の並行配線（dark）に過ぎない。token_store を必ず渡す。
    if _envflag("USE_MAIL_LINK_TOOL"):
        from teamagent.skills.mail_to_internal_context.skill import MailToInternalContextSkill

        mail_link_store = _build_token_store()
        specs.append(
            ToolSpec(
                MailToInternalContextSkill.name,
                MailToInternalContextSkill.description,
                MailToInternalContextSkill,
                factory=lambda: MailToInternalContextSkill(token_store=mail_link_store),
            )
        )

    # 要返信トリアージツール（read-only・メタデータのみ・LLM不使用）。**既定 OFF**
    # （USE_FOLLOWUP_TOOL=1）。mail_constraints のトークンレス factory は踏襲しない
    # （per-user の本人トークンを渡す）。
    if _envflag("USE_FOLLOWUP_TOOL"):
        from teamagent.skills.mail_followup.skill import MailFollowupSkill

        followup_store = _build_token_store()
        specs.append(
            ToolSpec(
                MailFollowupSkill.name,
                MailFollowupSkill.description,
                MailFollowupSkill,
                factory=lambda: MailFollowupSkill(token_store=followup_store),
            )
        )

    # メール要約ツール（read-only）。**既定 OFF**（USE_MAIL_SUMMARY_TOOL=1）。
    if _envflag("USE_MAIL_SUMMARY_TOOL"):
        from teamagent.skills.mail_summary.skill import MailSummarySkill

        summary_store = _build_token_store()
        specs.append(
            ToolSpec(
                MailSummarySkill.name,
                MailSummarySkill.description,
                MailSummarySkill,
                factory=lambda: MailSummarySkill(token_store=summary_store),
            )
        )

    # 返信ドラフト生成ツール（gmail.modify・下書き作成のみ・送信は人間）。**既定 OFF**
    # （USE_MAIL_REPLY_TOOL=1）。利用には各自が gmail.modify を含む connect 再認可が必要。
    if _envflag("USE_MAIL_REPLY_TOOL"):
        from teamagent.skills.mail_reply.skill import MailReplySkill

        reply_store = _build_token_store()
        specs.append(
            ToolSpec(
                MailReplySkill.name,
                MailReplySkill.description,
                MailReplySkill,
                factory=lambda: MailReplySkill(token_store=reply_store),
            )
        )

    # §U-Part3 Step C: 朝ダイジェスト Skill。EventBridge Scheduled Task（平日 9:30 JST）が
    # scripts/run_morning_digest_fargate.py 経由で各 user_email ごとに呼ぶ。mention 経由では
    # ないが統一的に ToolSpec 登録（ローカル検証用）。**既定 OFF**（USE_MORNING_DIGEST_TOOL=1）。
    if _envflag("USE_MORNING_DIGEST_TOOL"):
        from teamagent.skills.morning_digest.skill import MorningDigestSkill

        morning_store = _build_token_store()
        specs.append(
            ToolSpec(
                MorningDigestSkill.name,
                MorningDigestSkill.description,
                MorningDigestSkill,
                factory=lambda: MorningDigestSkill(token_store=morning_store),
            )
        )

    # §U: oauth_connect — 本人専用の Google 連携 URL を発行（@AiLa「連携」で個別 URL）。
    # OpenClaw に connect 経路が無い問題への対応。URL 生成のみ＝token_store 依存なし。
    # 実行時に run() が本人 user_email を metadata から必須取得し fail-closed。
    # **既定 OFF**（USE_OAUTH_CONNECT_TOOL=1）。OAUTH_REDIRECT_URI/OAUTH_STATE_SECRET/
    # CONNECT_GOOGLE_CLIENT_ID/SECRET が env/secret に要る（fargate.tf で配線）。
    if _envflag("USE_OAUTH_CONNECT_TOOL"):
        from teamagent.skills.oauth_connect.skill import OAuthConnectSkill

        specs.append(
            ToolSpec(
                OAuthConnectSkill.name,
                OAuthConnectSkill.description,
                OAuthConnectSkill,
            )
        )

    # TikTok取得ツール（30本/KW・上位N本は動画本体DL→S3）。**既定 OFF**（USE_TIKTOK_ACQUIRE=1）。
    # video_algorithm/tiktok_search が bot プロセス内でスクレイプするのと違い、submit は SQS 投函
    # のみ（RunTask/PassRole 非保有）で、実取得は使い捨て Fargate に隔離（A′トポロジ）。
    # env: TIKTOK_TASK_QUEUE / TIKTOK_JOBS_TABLE / TIKTOK_S3_BUCKET（tiktok_acquire.tf）。
    # OC 露出は openclaw.config.json5 の toolFilter.include に追加してから（=人間ゲート）。
    # 本番ONの前提: ToS/stealth の法務承認（O1・承認済）＋ infra apply 済み。
    if _envflag("USE_TIKTOK_ACQUIRE"):
        from teamagent.adapters.tiktok_task_store import TikTokTaskStore
        from teamagent.skills.tiktok_acquire.skill import (
            TikTokAcquireSkill,
            TikTokAcquireStatusSkill,
        )

        _tk_store = TikTokTaskStore()
        specs.append(
            ToolSpec(
                TikTokAcquireSkill.name,
                TikTokAcquireSkill.description,
                TikTokAcquireSkill,
                factory=lambda: TikTokAcquireSkill(store=_tk_store),
            )
        )
        specs.append(
            ToolSpec(
                TikTokAcquireStatusSkill.name,
                TikTokAcquireStatusSkill.description,
                TikTokAcquireStatusSkill,
                factory=lambda: TikTokAcquireStatusSkill(store=_tk_store),
            )
        )

    return specs


def _build_token_store() -> Any:
    """per-user TokenStore を構築（OAUTH_KMS_KEY_ID + RDS。無ければ dev 用 InMemory）。"""
    key_id = os.environ.get("OAUTH_KMS_KEY_ID")
    if not key_id:
        from teamagent.adapters.oauth_token_store import InMemoryTokenStore

        return InMemoryTokenStore()  # プロセス内のみ（空＝全員未連携）。実運用は RDS+KMS。
    from teamagent.adapters.oauth_token_store import KmsCipher, RdsTokenStore
    from teamagent.adapters.pgvector_client import PgVectorClient

    return RdsTokenStore(PgVectorClient.from_env(), KmsCipher(key_id))


__all__ = ["build_production_tools"]
