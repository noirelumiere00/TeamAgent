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

import structlog

from .tools import ToolSpec

logger = structlog.get_logger(__name__)


def _envflag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


def _envint(name: str, default: int) -> int:
    """env を int として読む（空・不正値は default にフォールバック）。"""
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _envfloat(name: str, default: float) -> float:
    """env を float として読む（空・不正値は default にフォールバック）。"""
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def resolve_search_skill_config() -> dict[str, Any]:
    """env → SearchSkill コンストラクタ引数を 1 か所で解決する（**唯一の真実源**）.

    factory（MCP/OpenClaw 経路）と runtime/slack_bot.py（Socket Mode 経路）の双方が
    本関数を使い、4 ノブ（rerank_pool_size / min_relevance_fallback / use_client_boost /
    use_knowledge_filters）を含む全ノブを **同じ env から同じ既定で** 解決する。
    過去は slack_bot 側が 4 ノブを渡さずコンストラクタ既定（30/0.0/False/False）に落ち、
    本番 env を入れても黙って無効化される構築ドリフトがあった（QW-2 で解消）。

    戻り値は embedder / query_planner を**含まない**純粋な kwargs（int/float/bool/str のみ）。
    そのまま起動ログにも出せる（観測可能化）。重い依存は呼び出し側で注入する。
    """
    return {
        "use_contextual": _envflag("USE_CONTEXTUAL"),
        "use_new_schema": _envflag("USE_NEW_SCHEMA"),
        "use_fb_drive_match": _envflag("USE_FB_DRIVE_MATCH"),
        "use_cohere_rerank": _envflag("USE_COHERE_RERANK"),
        # Rerank 候補プール（dense retrieval を何件 rerank に渡すか）。既定 30＝従来挙動。
        "rerank_pool_size": _envint("SEARCH_RERANK_POOL_SIZE", 30),
        # QW-4: rerank が返す件数（救済プール幅）。min_relevance の母数を top_k から切り離す。
        # SEARCH_MIN_RELEVANCE=0.0（既定）では最終 [:top_k] が効き従来挙動と完全等価。
        "rerank_return_size": _envint("SEARCH_RERANK_RETURN_SIZE", 100),
        "min_relevance": _envfloat("SEARCH_MIN_RELEVANCE", 0.0),
        # 2段階しきい値の fallback（既定 0.0 = 無効＝従来挙動）。
        "min_relevance_fallback": _envfloat("SEARCH_MIN_RELEVANCE_FALLBACK", 0.0),
        # client-boost は A/B で +4pp 実証済み・固有名詞のみ発火で副作用なし・DB障害時は
        # fail-open（語彙取得失敗→ブースト無効）。よって既定 ON を採用
        # （USE_CLIENT_BOOST=false で明示無効化は可能）。両経路で同一既定にする。
        "use_client_boost": _envflag("USE_CLIENT_BOOST", "true"),
        "use_aggregation_mode": _envflag("USE_AGGREGATION_MODE"),
        # ナレッジ Q&A: 「○○業界の提案事例」等の資料種別語を cls_doc_type で絞る
        # （0 件なら通常検索にフォールバック＝副作用なし）。USE_KNOWLEDGE_FILTERS で有効化。
        "use_knowledge_filters": _envflag("USE_KNOWLEDGE_FILTERS"),
        "prompt_version": os.environ.get("PROMPT_VERSION", "v2d"),
        "summary_max_tokens": _envint("SEARCH_MAX_TOKENS", 800),
    }


def build_search_skill_from_env() -> Any:
    """実 SearchSkill を env から構築する（factory / slack_bot の**共通**ビルダー）.

    env→引数解決は resolve_search_skill_config() に集約済み。embedder（LocalE5Embedder）と
    query_planner（USE_QUERY_PLANNER=1 のときだけ非 None）はここで遅延生成して注入する。
    起動ログに全ノブを出し、どの env でどう構築されたかを観測可能にする（QW-2）。
    """
    from teamagent.adapters.embeddings_client import build_embedder_from_env
    from teamagent.skills.search.query_planner import build_query_planner_from_env
    from teamagent.skills.search.skill import SearchSkill

    config = resolve_search_skill_config()
    logger.info("search_skill_config_resolved", source="factory", **config)
    return SearchSkill(
        # EMBEDDER_BACKEND（既定 local）で local-e5 / Bedrock Cohere を切替。
        # EMBEDDING_COLUMN とのペア整合は build_embedder_from_env 内で fail-loud 検証する。
        embedder=build_embedder_from_env(),
        # P3 エージェント検索（USE_QUERY_PLANNER=1 のときだけ非 None・既定は単一クエリ）。
        query_planner=build_query_planner_from_env(),
        **config,
    )


# 後方互換エイリアス: connect_web / knowledge_deliver が `_build_search_skill` を import 済。
_build_search_skill = build_search_skill_from_env


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
    # カタログ成果物の永続化(Part1・外部脳化)。USE_RESEARCH_PERSIST=1 のときだけ有効化し、常駐
    # embedder/pgvector を再利用（二重ロード回避）。None のとき各 skill は完全 no-op（後方互換）。
    _research_persister = None
    if _envflag("USE_RESEARCH_PERSIST"):
        from teamagent.skills._shared.research_persist import ResearchPersister

        _research_persister = ResearchPersister(pgvector=search.pgvector, embedder=search.embedder)
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

    # ナレッジ配信: 検索 → 該当資料の実ファイルを依頼者 DM に添付して届ける。
    # 既定 OFF（USE_KNOWLEDGE_DELIVER=1 で opt-in）。共有 search を注入（埋め込み二重ロード回避）。
    # slack/gdrive は run() で遅延生成。Drive 書込はせず読取 DL のみ（drive.readonly）。
    if _envflag("USE_KNOWLEDGE_DELIVER"):
        from teamagent.skills.knowledge_deliver.skill import KnowledgeDeliverSkill

        specs.append(
            ToolSpec(
                KnowledgeDeliverSkill.name,
                KnowledgeDeliverSkill.description,
                KnowledgeDeliverSkill,
                factory=lambda: KnowledgeDeliverSkill(search=search),
            )
        )

    # recommend: 新規案件概要 → 類似の過去 提案書/議事録/営業FB をベクトル近傍で3カテゴリ提示。
    # **既定 OFF**（USE_RECOMMEND_SKILL=1 で opt-in）。SearchSkill.retrieve_hits を再利用するため
    # 共有 search を注入（埋め込み二重ロード回避）。Bedrock 要約はせず近傍提示のみ＝DB/Bedrock
    # 追加依存なし。OC 露出は openclaw.config.json5 の toolFilter.include 追加が別途必要。
    if _envflag("USE_RECOMMEND_SKILL"):
        from teamagent.skills.recommend.skill import RecommendSkill

        specs.append(
            ToolSpec(
                RecommendSkill.name,
                RecommendSkill.description,
                RecommendSkill,
                factory=lambda: RecommendSkill(search=search),
            )
        )

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

    # ③動画チェック: 自社編集者の納品動画をオリエンと照合し合否/誤植/尺の一次FB。**既定 OFF**
    # （USE_VIDEO_APPROVAL=1）。OC 露出は openclaw.config.json5 の toolFilter.include 追加が前提。
    # Gemini/Drive 依存は run() で遅延生成。description で video_analysis(外部競合)と棲み分け済。
    if _envflag("USE_VIDEO_APPROVAL"):
        from teamagent.skills.video_approval.skill import VideoApprovalSkill

        specs.append(
            ToolSpec(
                VideoApprovalSkill.name,
                VideoApprovalSkill.description,
                VideoApprovalSkill,
            )
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

    # proposal_builder: submit は job row 作成後に MCP 内 daemon thread で生成を継続し、
    # status が DynamoDB（未設定時だけprocess-local memory）の状態と安全な結果要約を返す。
    # Composer/Bedrock は権限を持つ MCP task 内に残し、roleless media workerへ委譲しない。
    # **既定 OFF**（USE_PROPOSAL_BUILDER_TOOLS=1）。
    if _envflag("USE_PROPOSAL_BUILDER_TOOLS"):
        from teamagent.adapters.proposal_job_store import ProposalJobStore
        from teamagent.skills.proposal_builder.skill import (
            ProposalBuilderSkill,
            ProposalBuilderStatusSkill,
            ProposalBuilderSubmitSkill,
        )

        _proposal_store = ProposalJobStore()
        specs.append(
            ToolSpec(
                ProposalBuilderSubmitSkill.name,
                ProposalBuilderSubmitSkill.description,
                ProposalBuilderSubmitSkill,
                factory=lambda: ProposalBuilderSubmitSkill(
                    builder_factory=lambda: ProposalBuilderSkill(search=search),
                    store=_proposal_store,
                ),
            )
        )
        specs.append(
            ToolSpec(
                ProposalBuilderStatusSkill.name,
                ProposalBuilderStatusSkill.description,
                ProposalBuilderStatusSkill,
                factory=lambda: ProposalBuilderStatusSkill(store=_proposal_store),
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
    # ここはBedrockオーケストレータ用の並行配線（dark）に過ぎない。token_store を必ず渡す。
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
        reply_slack = _build_slack_context_provider()
        specs.append(
            ToolSpec(
                MailReplySkill.name,
                MailReplySkill.description,
                MailReplySkill,
                factory=lambda: MailReplySkill(token_store=reply_store, deal_provider=reply_slack),
            )
        )

    # 朝ダイジェストの「🗓 日程候補を提案」ボタン押下を処理するツール（OpenClaw 経由）。
    # 空き枠計算→候補入り返信下書き＋透明仮予定（送信/招待なし）。**既定 OFF**。
    if _envflag("USE_SCHEDULE_PROPOSE_TOOL"):
        from teamagent.skills.schedule_propose.skill import ScheduleProposeSkill

        sched_store = _build_token_store()
        specs.append(
            ToolSpec(
                ScheduleProposeSkill.name,
                ScheduleProposeSkill.description,
                ScheduleProposeSkill,
                factory=lambda: ScheduleProposeSkill(token_store=sched_store),
            )
        )

    # 朝ダイジェストの「📅 カレンダーに登録」ボタン押下を処理するツール（OpenClaw 経由）。
    # 本人カレンダーへ登録のみ（招待送信なし・削除/変更は adapter 物理封鎖）。**既定 OFF**。
    if _envflag("USE_CALENDAR_EVENT_TOOL"):
        from teamagent.skills.calendar_event.skill import CalendarEventSkill

        cal_store = _build_token_store()
        specs.append(
            ToolSpec(
                CalendarEventSkill.name,
                CalendarEventSkill.description,
                CalendarEventSkill,
                factory=lambda: CalendarEventSkill(token_store=cal_store),
            )
        )

    # 自由文の空き時間照会ツール（「空いてる？」「◯分どこに入る？」）。read-only＝
    # freebusy 読み取りのみで書込 API は一切呼ばない。**既定 OFF**。
    if _envflag("USE_CALENDAR_FREEBUSY_TOOL"):
        from teamagent.skills.calendar_freebusy.skill import CalendarFreeBusySkill

        freebusy_store = _build_token_store()
        specs.append(
            ToolSpec(
                CalendarFreeBusySkill.name,
                CalendarFreeBusySkill.description,
                CalendarFreeBusySkill,
                factory=lambda: CalendarFreeBusySkill(token_store=freebusy_store),
            )
        )

    # 公開Webの市場リサーチツール（Gemini の Google 検索グラウンディング・read-only）。
    # 検索も本文取得も Google 側で完結＝自 VPC からの直 fetch は無い。出典は
    # groundingMetadata からサーバが機械付与し、LLM 出力の URL は採用しない。
    # **既定 OFF**（USE_WEB_RESEARCH_TOOL=1）。段階公開は WEB_RESEARCH_ALLOWED_EMAILS。
    # 前提: GEMINI_USE_VERTEX/GEMINI_VERTEX_PROJECT（または GEMINI_API_KEY）が env にあること。
    if _envflag("USE_WEB_RESEARCH_TOOL"):
        from teamagent.skills.web_research.skill import WebResearchSkill

        specs.append(
            ToolSpec(
                WebResearchSkill.name,
                WebResearchSkill.description,
                WebResearchSkill,
            )
        )

    # 朝ダイジェストの「✏️ 下書きを作成」ボタン押下を処理するツール（OpenClaw 経由）。
    # 押下 → OpenClaw(socket) が system event でエージェントへ転送 → SOUL 指示で本ツールを呼ぶ。
    # その案件へ Reply-All 下書きを作成（送信しない）。**既定 OFF**（USE_MAIL_DRAFT_TOOL=1）。
    if _envflag("USE_MAIL_DRAFT_TOOL"):
        from teamagent.skills.mail_draft.skill import MailDraftSkill

        draft_store = _build_token_store()
        specs.append(
            ToolSpec(
                MailDraftSkill.name,
                MailDraftSkill.description,
                MailDraftSkill,
                factory=lambda: MailDraftSkill(token_store=draft_store),
            )
        )

    # §U-Part3 Step C: 朝ダイジェスト Skill。EventBridge Scheduled Task（平日 9:30 JST）が
    # scripts/run_morning_digest_fargate.py 経由で各 user_email ごとに呼ぶ。mention 経由では
    # ないが統一的に ToolSpec 登録（ローカル検証用）。**既定 OFF**（USE_MORNING_DIGEST_TOOL=1）。
    if _envflag("USE_MORNING_DIGEST_TOOL"):
        from teamagent.skills.morning_digest.skill import MorningDigestSkill

        morning_store = _build_token_store()
        morning_slack = _build_slack_context_provider()
        specs.append(
            ToolSpec(
                MorningDigestSkill.name,
                MorningDigestSkill.description,
                MorningDigestSkill,
                factory=lambda: MorningDigestSkill(
                    token_store=morning_store, deal_provider=morning_slack
                ),
            )
        )

    # §U: oauth_connect — 本人専用の Google 連携 URL を発行（@NewsTV AI「連携」で個別 URL）。
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

    # knowledge_search_url — @NewsTV AI「検索ページ教えて」で資料検索 Web UI（connect-web の
    # /search・/search/graph）の URL を返す（oauth_connect と同じく URL 生成のみ＝依存なし）。
    # CONNECT_BASE_URL 未設定なら壊れたリンクを出さず「未公開」と返す（fail-safe）。
    # **既定 OFF**（USE_KNOWLEDGE_SEARCH_URL_TOOL=1）。OC 露出は openclaw.config.json5 の
    # toolFilter.include に追加してから（=人間ゲート）。
    if _envflag("USE_KNOWLEDGE_SEARCH_URL_TOOL"):
        from teamagent.skills.knowledge_search_url.skill import KnowledgeSearchUrlSkill

        specs.append(
            ToolSpec(
                KnowledgeSearchUrlSkill.name,
                KnowledgeSearchUrlSkill.description,
                KnowledgeSearchUrlSkill,
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

    # カタログ①②④: X(Twitter)リサーチ群。**既定 OFF**（USE_X_RESEARCH_TOOLS=1 で opt-in）。
    # Apify actor は Apify インフラで実行（MCP からは REST のみ）。コストは cost_guard の
    # DynamoDB 月次台帳が check/record（COST_GUARD_TABLE / COST_APIFY_MONTHLY_USD）。
    # ④は tiktok_acquire と同じ A′トポロジ（SQS投函のみ・RunTask/PassRole 非保有）。
    # env: APIFY_API_TOKEN(secret) / X_TASK_QUEUE / X_JOBS_TABLE / X_S3_BUCKET /
    #      X_ANALYSIS_MODEL_ID(分析はSonnet明示注入・未設定は既定Haiku) /
    #      X_RESEARCH_ALLOWED_EMAILS(段階公開・空=全員)。
    # OC 露出は openclaw.config.json5 の toolFilter.include 追加が別途必要（=人間ゲート）。
    if _envflag("USE_X_RESEARCH_TOOLS"):
        from teamagent.adapters.x_task_store import XTaskStore
        from teamagent.skills.x_research.skill import (
            XBuzzMeasureSkill,
            XBuzzMeasureStatusSkill,
            XNeedsMiningSkill,
            XVoiceSearchSkill,
        )

        _x_store = XTaskStore()
        _persist = _research_persister  # lambda キャプチャ用（None なら skill は no-op）
        # 永続化(Part1)は v1 では voice のみ注入（needs/buzz は任意テーマで取引先に不適・未記録）。
        specs.append(
            ToolSpec(
                XVoiceSearchSkill.name,
                XVoiceSearchSkill.description,
                XVoiceSearchSkill,
                factory=lambda: XVoiceSearchSkill(persister=_persist),
            )
        )
        specs.append(
            ToolSpec(XNeedsMiningSkill.name, XNeedsMiningSkill.description, XNeedsMiningSkill)
        )
        specs.append(
            ToolSpec(
                XBuzzMeasureSkill.name,
                XBuzzMeasureSkill.description,
                XBuzzMeasureSkill,
                factory=lambda: XBuzzMeasureSkill(store=_x_store),
            )
        )
        specs.append(
            ToolSpec(
                XBuzzMeasureStatusSkill.name,
                XBuzzMeasureStatusSkill.description,
                XBuzzMeasureStatusSkill,
                factory=lambda: XBuzzMeasureStatusSkill(store=_x_store),
            )
        )

    # カタログ③: 検索面チェック（TikTok×IG媒体比較）。**既定 OFF**（USE_SEARCH_SURFACE_TOOL=1）。
    # TikTok面は tiktok_acquire 成果物のS3読込が正（3KW以上は必須・descriptionで誘導）、
    # IG面は Apify（APIFY_API_TOKEN）。IG_SURFACE_DEFAULT=search|hashtag で既定面を切替
    # （日本語KWカバレッジの検証ゲート用）。
    if _envflag("USE_SEARCH_SURFACE_TOOL"):
        from teamagent.skills.search_surface_check.skill import SearchSurfaceCheckSkill

        specs.append(
            ToolSpec(
                SearchSurfaceCheckSkill.name,
                SearchSurfaceCheckSkill.description,
                SearchSurfaceCheckSkill,
            )
        )

    # カタログ⑤: コメント欄マイニング。**既定 OFF**（USE_TIKTOK_COMMENT_TOOLS=1）。
    # 取得は既存 chromium 一次（fatイメージ前提）→ Apify clockworks 縮退。分析は Bedrock。
    if _envflag("USE_TIKTOK_COMMENT_TOOLS"):
        from teamagent.skills.tiktok_comment_mining.skill import TikTokCommentMiningSkill

        _persist_c = _research_persister  # lambda キャプチャ用（None なら no-op）
        specs.append(
            ToolSpec(
                TikTokCommentMiningSkill.name,
                TikTokCommentMiningSkill.description,
                TikTokCommentMiningSkill,
                factory=lambda: TikTokCommentMiningSkill(persister=_persist_c),
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


def _build_slack_store() -> Any:
    """本人 Slack(xoxp) TokenStore を構築（OAUTH_KMS_KEY_ID + RDS。無ければ空ストア）。"""
    key_id = os.environ.get("OAUTH_KMS_KEY_ID")
    if not key_id:
        from teamagent.adapters.oauth_token_store import InMemoryTokenStore

        return InMemoryTokenStore()  # 空＝全員未連携（provider は fail-open で素通り）。
    from teamagent.adapters.oauth_token_store import KmsCipher, SlackTokenStore
    from teamagent.adapters.pgvector_client import PgVectorClient

    return SlackTokenStore(PgVectorClient.from_env(), KmsCipher(key_id))


def _build_slack_context_provider() -> Any:
    """USE_SLACK_CONTEXT 有効時のみ、本人Slack文脈プロバイダを構築（それ以外は None）。"""
    from teamagent.skills._shared.mail_compose import env_bool

    if not env_bool("USE_SLACK_CONTEXT", False):
        return None
    from teamagent.skills._shared.slack_context import SlackContextProvider

    return SlackContextProvider(slack_store=_build_slack_store())


__all__ = ["build_production_tools"]
