# ============================================================
# 変数 — ECS Fargate（P1: OpenClaw外殻 + TeamAgent-MCP境界）§H / M2
# ============================================================
# 値は terraform.tfvars / 環境で上書き。秘密「値」はここに書かない（secret 名のみ）。

variable "openclaw_image" {
  description = "Guarded saved-plan flowでのみ変更できるOpenClaw release repositoryのdigest URI。空値によるmanaged service/task削除は禁止"
  type        = string

  validation {
    condition = can(regex(
      "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-openclaw@sha256:[0-9a-f]{64}$",
      var.openclaw_image,
    ))
    error_message = "openclaw_image must be a nonempty fixed release-repository digest; decommissioning requires a separately reviewed destructive workflow."
  }
}

variable "mcp_image" {
  description = "TeamAgent-MCP release repositoryの必須digest URI。空値によるmanaged service/task削除は禁止"
  type        = string

  validation {
    condition = can(regex(
      "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-mcp@sha256:[0-9a-f]{64}$",
      var.mcp_image,
    ))
    error_message = "mcp_image must be a nonempty fixed release-repository digest; decommissioning requires a separately reviewed destructive workflow."
  }
}

variable "fargate_mcp_cpu" {
  description = "MCPタスク CPU units（1024=1vCPU）"
  type        = number
  default     = 1024
}

variable "fargate_mcp_memory" {
  description = "MCPタスク メモリ MB（embedder/torch を載せるため余裕）"
  type        = number
  default     = 4096
}

variable "fargate_openclaw_cpu" {
  description = "OpenClawタスク CPU units"
  type        = number
  default     = 512
}

variable "fargate_openclaw_memory" {
  description = "OpenClawタスク メモリ MB"
  type        = number
  default     = 1024
}

variable "shared_company_domains" {
  description = "会社共有モデル(§G)の会社ドメイン（カンマ区切り）。MCPへ TEAMAGENT_SHARED_COMPANY_DOMAINS で渡す。例: vectorinc.co.jp"
  type        = string
  default     = ""
}

variable "mcp_model_id" {
  description = "mcp（スキル/オーケストレーター）の Bedrock モデル ID。コスト方針(2026-06-29)により Haiku 4.5 既定・live rev40 と同値。var.bedrock_model_id を流用しない（あちらは Lambda 用で tfvars が Sonnet を指定しており、共有すると apply で mcp が Sonnet 化する＝2026-07-11 反対尋問レビューで検出）。"
  type        = string
  default     = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"

  validation {
    condition     = var.mcp_model_id == "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
    error_message = "MCPは監査済みJP Claude Haiku 4.5 inference profileだけを使用できます。"
  }
}

variable "enable_proposal_builder" {
  description = "Gemini v3→RAG→統合FMT→Slack添付の proposal_builder をMCPで有効化する。S3固定version資産、KMS権限、generic media workerが全て必須。"
  type        = bool
  default     = false
}

variable "enable_omiyage_report" {
  description = "お土産資料 便1（omiyage_report_submit/status）をMCPで有効化する。点灯条件: generic media worker（TikTok検索委譲＋動画DL/フレーム抽出）が必須（precondition で強制）・Bedrock視覚推論の課金が発生・ジョブ行は proposal_builder_jobs DynamoDB table へ相乗り（enable_proposal_builder が OFF のときは本フラグが PROPOSAL_JOBS_TABLE を注入）。既定 false＝env未注入でツール未登録のまま。"
  type        = bool
  default     = false
}

variable "proposal_builder_publish_ready" {
  description = "ready提案書のS3公開(presigned URL発行)を許可する。既定OFF＝Slack添付のみ。"
  type        = bool
  default     = false
}

variable "proposal_builder_deliver_internal_drafts" {
  description = "draft(裏取り前)提案書の社内Slack添付を許可する。既定OFF＝ready以外は添付しない。"
  type        = bool
  default     = false
}

variable "proposal_builder_template_s3_bucket" {
  description = "統合FMTのversioning/SSE-KMS有効S3 bucket（enable_proposal_builder時必須）"
  type        = string
  default     = ""
}

variable "proposal_builder_template_s3_key" {
  description = "統合FMT object key（enable_proposal_builder時必須）"
  type        = string
  default     = ""
}

variable "proposal_builder_template_s3_version_id" {
  description = "統合FMTのimmutable S3 VersionId。null/latestは禁止"
  type        = string
  default     = ""
}

variable "proposal_builder_template_s3_sha256" {
  description = "統合FMT全体のSHA-256 hex"
  type        = string
  default     = ""
}

variable "proposal_builder_template_s3_size" {
  description = "統合FMTの実byte数（1〜256MiB）"
  type        = number
  default     = 0
}

variable "proposal_builder_account_s3_bucket" {
  description = "改変禁止アカウントDBのversioning/SSE-KMS有効S3 bucket（enable時必須）"
  type        = string
  default     = ""
}

variable "proposal_builder_account_s3_key" {
  description = "改変禁止アカウントDB object key（enable時必須）"
  type        = string
  default     = ""
}

variable "proposal_builder_account_s3_version_id" {
  description = "アカウントDBのimmutable S3 VersionId。null/latestは禁止"
  type        = string
  default     = ""
}

variable "proposal_builder_account_s3_sha256" {
  description = "アカウントDB全体のSHA-256 hex"
  type        = string
  default     = ""
}

variable "proposal_builder_account_s3_size" {
  description = "アカウントDBの実byte数（1〜5MiB）"
  type        = number
  default     = 0
}

variable "proposal_builder_assets_kms_key_arn" {
  description = "統合FMTとアカウントDBを暗号化する単一KMS key ARN（enable時必須）"
  type        = string
  default     = ""
}

variable "proposal_builder_news_channel_id" {
  description = "ingestで実測した general_news-tv のSlack channel ID。未設定時はchannel_nameだけで絞る"
  type        = string
  default     = ""
}

variable "use_calendar_event_tool" {
  description = "calendar_event tool（📅カレンダー登録ボタンの押下処理・v0.3 Task3）を mcp で有効化。既定 false。ON 後に morning_digest_calendar_button=true にする（順序を守らないと無反応ボタン）。"
  type        = bool
  default     = false
}

variable "use_digest_ack_tool" {
  description = "digest_ack tool（☑️確認済みボタンの押下処理）を mcp で有効化。既定 false。ON 後に morning_digest_ack_button=true にする（順序を守らないと無反応ボタン）。触るのは本人の digest_ack 行のみで、Google API・Slack API・外部送信は一切呼ばない。"
  type        = bool
  default     = false
}

variable "use_schedule_propose_tool" {
  description = "schedule_propose tool（🗓日程候補提案ボタンの押下処理・v0.3 Task4）を mcp で有効化。既定 false。ON 後に morning_digest_schedule_button=true にする（順序を守らないと無反応ボタン）。"
  type        = bool
  default     = false
}

variable "use_calendar_freebusy_tool" {
  description = "calendar_freebusy tool（空き時間の自由文照会）を mcp で有効化。read-only＝freebusy 読み取りのみで書込 API は一切呼ばない。既定 false。"
  type        = bool
  default     = false
}

variable "use_slack_summary_tool" {
  description = "slack_summary tool（Slack スレッド要約）を mcp で有効化。読取は依頼者本人の xoxp のみ（bot token 不使用）・Slack への書込は一切なし。出力面ガードで発信元チャンネル以外のスレッド要約は拒否。既定 false。"
  type        = bool
  default     = false
}

variable "use_web_research_tool" {
  description = "web_research tool（公開Webの市場リサーチ＝Gemini の Google 検索グラウンディング）を mcp で有効化。read-only＝Web への直 fetch も書込 API も無い（取得は Google 側で完結）。既定 false。⚠️ON の前提: mcp に Gemini の認証 env（GEMINI_USE_VERTEX/GEMINI_VERTEX_PROJECT/VERTEX_SA_JSON）が入っていること＝現状は enable_scrape_tools=true が必要（task definition の precondition で強制）。⚠️ 到達確認の限界: 「全員開放」の裁定のうち **allowlist 側だけが git で読める**（fargate.tf が WEB_RESEARCH_ALLOWED_EMAILS へ \"\" を直接焼く）。本フラグの ON/OFF は依然 git 管理外の tfvars が真実源なので、実際に全員が使えているかは tfvars 実物か本番 taskdef の env でしか確認できない。段階公開用だった web_research_allowed_emails は退役済み。"
  type        = bool
  default     = false
}

variable "use_attachment_tools" {
  description = "attachment_assist tool（会話に添付されたファイルの読取・加工＝要約/修正案/議事録FMT/集計/英訳）を mcp で有効化。read-only＝Slack へのファイル生成・再配信はしない（P2 は別フラグ）。読取対象は署名済み caller claim 由来の会話に添付されたファイルのみ（file_id/URL/channel は入力に持たない）。既定 false。"
  type        = bool
  default     = false
}

variable "use_video_capture_tool" {
  description = "video_capture tool（動画の指定時刻を JPEG 切出しして依頼スレッド/本人 DM に添付）を mcp で有効化。既定 false。前提: enable_media_worker=true（acquire/frame の media job 基盤）。対象は外部公開URL（TikTok/Instagram）と自WSの添付動画のみで社内データは読まない。⚠️ YouTube は取得元の bot 判定でブロックされる実測があり、スキル側で既定 OFF（env VIDEO_CAPTURE_ALLOW_YOUTUBE で解禁可能だが cookie/PO_TOKEN 対応が入るまで通らない）。"
  type        = bool
  default     = false
}

variable "web_research_allowed_emails" {
  description = "【退役・2026-08-25 裁定で全員開放】web_research の段階公開 allowlist だったもの。stage1=小俣のみ→数名→空 を経て空（全員）で確定したため、fargate.tf は本変数を参照せず WEB_RESEARCH_ALLOWED_EMAILS へ \"\" を直接焼く。宣言だけ残すのは、git 管理外の実 tfvars に本キーが残っていても `-var-file` が undeclared variable で警告/失敗しないようにするため。再び絞る場合は変数を復活させるのではなく、fargate.tf の値を明示的に変更する（誰に開いているかが git で読めることを優先する）。"
  type        = string
  default     = ""
}

variable "enable_progress_notify" {
  description = "ツール実行中の進捗表示（v0.3.1 Task7）。ON で mcp が重いツール実行前に『📂 資料を検索しています…』等を Slack へ投稿し完了後に削除する。既定 false。fail-open（送信失敗はツール実行を阻害しない）。bot の chat:write/im:write scope 前提。"
  type        = bool
  default     = false
}

variable "use_entity_tags" {
  description = "名寄せタグ（cls_entities・v0.3.1）。ON で ingest 分類時に Haiku で取引先/代理店/ブランド/コラボ名を抽出し cls_entities に保持（親クライアント検索で子コラボが出る）。既定 false。既存分は scripts/backfill_entities.py で別途 backfill。"
  type        = bool
  default     = false
}

variable "use_ailavault_deeplinks" {
  description = "AiLaVault ディープリンク（v0.3 Task6）。ON で mcp が検索結果に /app#client:<名前> リンクを注入し、app.html の applyHashTarget が該当ノートを自動展開する。既定 false。⚠️ONの前提: connect-web が実 app.html を配信中であること（healthz app_html_source=s3。プレースホルダ配信中にONにすると飛べないリンクになる）。"
  type        = bool
  default     = false
}

variable "html_reports_tools" {
  description = <<-EOT
    検索系ツールの結果を HTML レポート化して /r で配布する対象（env USE_HTML_REPORTS）。
    空文字＝OFF（従来どおり構造化結果のみ）。"tiktok_search" のようにツール名をカンマ区切りで
    列挙するとそのツールだけ、"1"/"true" なら全ツール。段階的に開けるため既定は空。
    ON の前提: enable_report_shorturl=true（/r 実機 200 確認済み）であること。
  EOT
  type        = string
  default     = ""
}

variable "enable_html_report_thumbs" {
  description = "HTML レポートにサムネイルを載せるか（env USE_HTML_REPORT_THUMBS）。TikTok CDN の署名URLは数日で失効するため、取得して自社S3へ再ホストしてから貼る。既定 false。"
  type        = bool
  default     = false
}

variable "enable_html_report_headline" {
  description = "HTML レポート冒頭の一行見出しを Bedrock で生成するか（env USE_HTML_REPORT_HEADLINE）。入力1800字・max_tokens 80・40字上限で、規約違反の出力は採用しない。既定 false。"
  type        = bool
  default     = false
}

variable "enable_html_report_pptx" {
  description = "HTML レポートの PPTX 出力を許すか（env USE_HTML_REPORT_PPTX）。media worker の slides 変換を同期実行するため数十秒かかる。skill の outputs に \"pptx\" が明示された時だけ発火する。既定 false。"
  type        = bool
  default     = false
}

variable "enable_html_report_frames" {
  description = "レポートに該当秒の実フレームを載せるか（env USE_HTML_REPORT_FRAMES）。video_algorithm を後段実行するため数十秒〜数分・Gemini 課金あり。skill の outputs に \"frames\" が明示された時だけ発火する。既定 false。"
  type        = bool
  default     = false
}

variable "enable_report_shorturl" {
  description = "レポート短縮リンク(/r)を発行するか（Part2 段階ゲート＝env USE_REPORT_SHORTURL）。既定 false＝従来 presigned。ON の前提: connect-web が同一新イメージ(/r ルート)＋vseo-s3-read(bootstrap_vseo_s3_iam.sh)を持ち、実機で /r→302 を確認済みであること。揃う前に true にすると受信者側で 404/403 に劣化する。"
  type        = bool
  default     = false
}

variable "video_quota_enabled" {
  description = "動画分析の月間クォータ（v0.3 Task10）。既定 false。有効化前に migration 0017 の本番適用が必須（未適用だと fail-open で素通り・WARN のみ）。"
  type        = bool
  default     = false
}

variable "video_monthly_quota" {
  description = "ユーザーごとの月間動画分析上限（本・JST月次リセット・Step0裁定の初期値20）。"
  type        = number
  default     = 20
}

variable "use_analysis_cache" {
  description = "Gemini 動画分析結果の S3 キャッシュ（v0.3 Task10）。既定 false。同一動画×同一プロンプトの再分析を回避（Gemini=GCP課金の数少ないガード）。"
  type        = bool
  default     = false
}

variable "enable_kaiwai_classify" {
  description = "X投稿者の界隈マルチラベル分類（Part4・env USE_KAIWAI_CLASSIFY）。既定 false＝bio を LLM へ送らず・界隈を尋ねず・カードにチップも出さない（完全 no-op・後方互換）。分類は LLM 推定でありカードには「推定界隈」と明示表示する。⚠️前提1: enable_x_research=true（false 時は USE_KAIWAI_CLASSIFY env 自体が taskdef に出力されず、本フラグ true でも黙って no-op）。⚠️前提2: 第一候補 actor(apidojo) の本番 bio 取得率と分類品質を実データで確認済みであること。"
  type        = bool
  default     = false
}

variable "enable_research_persist" {
  description = "施策研究(x_voice/コメント分析/検索上位チェック)を pgvector→AiLaVault へ永続記録するか（Part1・env USE_RESEARCH_PERSIST。検索上位チェックはクライアント名つきの実行だけを記録）。既定 false＝完全 no-op。ON の前提: ローカルRDS(SSMトンネル)で admin INSERT した doc を owner/別社員/社外の3者の**通常検索(member接続)**で引き、ACL(会社横断可視・社外不可)が正しいことを実証済みであること（export_vault は admin 接続なので dry-run では RLS 検証にならない）。"
  type        = bool
  default     = false
}

variable "use_payload_offload" {
  description = "MCP 長文ペイロードの S3 退避（v0.3 Task8）。既定 false。対象は会社共有ナレッジ系 tool のみ（allowlist・per-user PII 系は対象外）。"
  type        = bool
  default     = false
}

variable "karte_attach_docs" {
  description = "clientkarte(カルテ)の「関連資料」機能まるごとの kill switch。既定 true（「カルテを見て資料をクリックする」工数をなくす要求そのもの）。true のとき資料名+Drive リンクの一覧と実ファイルを、まとめて依頼者本人の DM へ出す（2026-08-20 裁定 A）。チャンネル/スラッシュでカルテを呼ばれた場合、チャンネルへ出るのは件数だけの 1 行通知で、資料名も Drive リンクも実ファイルも出さない。DM で呼ばれた場合だけその場に一覧を出す。false にすると一覧も添付も出ず、本機能追加前の出力（カルテ本文+出典 URL のみ）へ戻る。⚠️ 止め方: mcp task definition の env は terraform_runtime_guard の validate_plan(mode=sync) が live と完全一致を要求し env の追加・変更・削除を die するため、素の apply では倒せない。mode=migration / kind=runtime の manifest allowlist 経路で適用すること（「apply するだけで即止まる」ではない）。"
  type        = bool
  default     = true
}

variable "karte_attach_docs_max" {
  description = "カルテ添付の最大件数。0 は「一覧は出すが実ファイルは 1 件も送らない」の明示指定。skill 側で 5 件にハードクランプされる。"
  type        = number
  default     = 3

  validation {
    # 0 は skill 側で「添付しない」の明示指定として尊重される（黙って既定 3 件に戻らない）。
    # 負値・小数・上限超過は運用ミスなので plan で落とす（skill 側の clamp 頼みにしない）。
    condition     = var.karte_attach_docs_max >= 0 && var.karte_attach_docs_max <= 5 && floor(var.karte_attach_docs_max) == var.karte_attach_docs_max
    error_message = "karte_attach_docs_max は 0〜5 の整数（0 = 実ファイルを送らない）。"
  }
}

variable "karte_attach_docs_max_bytes" {
  description = "カルテ添付の 1 ファイルあたり取得上限（バイト）。既定 50MiB。gdrive_client の既定 256MB をそのまま常時経路に許さないためのつまみ。"
  type        = number
  default     = 52428800

  validation {
    # 0 以下は「上限なし」と紛らわしいので plan で落とす（skill 側は不正値を既定へ戻す）。
    condition     = var.karte_attach_docs_max_bytes > 0 && var.karte_attach_docs_max_bytes <= 268435456 && floor(var.karte_attach_docs_max_bytes) == var.karte_attach_docs_max_bytes
    error_message = "karte_attach_docs_max_bytes は 1〜268435456(256MiB) の整数。"
  }
}

variable "karte_attach_docs_dedup_ttl_s" {
  description = "同じ資料を同じ相手へ重ね送りしない TTL 秒。既定 600。0 で重複防止を無効化（OpenClaw のタイムアウト再試行で同じ資料が 2 度 DM に届く事故の唯一のつまみ）。"
  type        = number
  default     = 600

  validation {
    condition     = var.karte_attach_docs_dedup_ttl_s >= 0 && var.karte_attach_docs_dedup_ttl_s <= 86400 && floor(var.karte_attach_docs_dedup_ttl_s) == var.karte_attach_docs_dedup_ttl_s
    error_message = "karte_attach_docs_dedup_ttl_s は 0〜86400 の整数（0 = 重複防止を無効）。"
  }
}

variable "slack_team_id" {
  description = "本番必須の自社Slack workspace team_id（T + 8文字以上の英大文字/数字）。OpenClaw署名claimとMCP resolverが同じexact IDを検証する。既定の空文字は未設定sentinelでplanをfail-closedにする。"
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^T[A-Z0-9]{8,}$", var.slack_team_id))
    error_message = "slack_team_id is required and must be a canonical Slack T ID."
  }
}

variable "slack_workspace" {
  # 本番の実値は "vector-workspcae"（Slack が返す permalink の実測値。綴りは
  # ワークスペース側がそうなっている。"vectorinc" ではない）。誤った値を入れると
  # 開けない URL を出すことになるため、変更時は必ず実 permalink と突き合わせること。
  description = "Slack出典の内部識別子を開けるpermalinkへ変換するためのworkspace名。既定の空文字では変換せず、URLを出さない。"
  type        = string
  default     = ""
}

variable "slack_dm_allowlist" {
  description = "本番で必須のSlack DM契約。\"*\"はdmPolicy=open+allowFrom=[\"*\"]、それ以外は1〜100件の重複しないSlack U IDを空白なしのカンマ区切りで指定しdmPolicy=allowlistにする。既定の空文字は安全な未設定sentinelであり、明示値なしのplanをfail-closedにする。"
  type        = string
  default     = ""

  validation {
    condition = (
      var.slack_dm_allowlist == "*" ||
      (
        length(var.slack_dm_allowlist) <= 2048 &&
        can(regex("^U[A-Z0-9]{8,}(,U[A-Z0-9]{8,}){0,99}$", var.slack_dm_allowlist)) &&
        length(distinct(split(",", var.slack_dm_allowlist))) == length(split(",", var.slack_dm_allowlist))
      )
    )
    error_message = "slack_dm_allowlist is required: use \"*\" or 1-100 unique comma-separated Slack U IDs with no spaces."
  }
}

variable "openclaw_model_id" {
  # §J: 既定を openclaw.config.json5（焼込・権威）と一致させる。本 var は参照/将来の env 注入用。
  # §P: list-inference-profiles 実測値で確定（2026-06-11・account 718959508629/東京）。
  description = "OpenClaw外側モデル（Haiku4.5・東京推論プロファイル実ID）。権威=openclaw.config.json5"
  type        = string
  default     = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"

  validation {
    condition     = var.openclaw_model_id == "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
    error_message = "OpenClawは監査済みJP Claude Haiku 4.5 inference profileだけを使用できます。"
  }
}

# ---------- 秘密値の secret 名（本人が Secrets Manager に作成。値は注入のみ） ----------
variable "mcp_bearer_secret_name" {
  description = "OpenClaw⟷MCP 共有 bearer の Secrets Manager 名"
  type        = string
  default     = "teamagent/dev/mcp/bearer"
}

variable "database_url_secret_name" {
  description = "MCP が使う DATABASE_URL（pgvector）の Secrets Manager 名"
  type        = string
  default     = "teamagent/dev/database-url"
}

variable "slack_bot_token_secret_name" {
  description = "OpenClaw用 Slack Bot Token（xoxb）の Secrets Manager 名"
  type        = string
  default     = "teamagent/dev/openclaw/slack-bot-token"
}

variable "slack_app_token_secret_name" {
  description = "OpenClaw用 Slack App Token（xapp・Socket Mode）の Secrets Manager 名"
  type        = string
  default     = "teamagent/dev/openclaw/slack-app-token"
}

variable "openclaw_gateway_token_secret_name" {
  description = "OpenClaw gateway 管理トークン（full operator scope相当）の Secrets Manager 名"
  type        = string
  default     = "teamagent/dev/openclaw/gateway-token"
}

variable "openclaw_caller_claim_secret_name" {
  description = "OpenClaw→MCPのone-use caller identity claim専用HMAC鍵（bearerとは別、32-byte以上）のSecrets Manager名"
  type        = string
  default     = "teamagent/dev/openclaw/caller-claim-hmac"
}

# §M: スクレイプ/動画ツール（USE_VIDEO_TOOLS/USE_TIKTOK_TOOLS）を有効化する“拡張版”の配線。
# true で Gemini secret 注入・S3(vseo-reports)権限・VSEO_REPORT_BUCKET を MCP タスクへ足す。既定 false＝P1薄殻は不要。
variable "enable_scrape_tools" {
  description = "スクレイプ/動画ツールを有効化（Gemini secret/S3権限/VSEO_REPORT_BUCKETを配線）。Dockerは WITH_SCRAPE_TOOLS=true でビルド。"
  type        = bool
  default     = false
}

variable "video_algo_max_videos" {
  description = "video_algorithm の既定『深掘り分析』本数（5〜10）。同期処理なので OpenClaw timeout(300s)と整合させること。値変更は taskdef env 差し替えのみで反映可。"
  type        = string
  default     = "5"
}

variable "video_algo_board_size" {
  description = "video_algorithm の『取得（上位ボード）』本数（5〜30・既定30）。メタのみ取得＝軽く、深掘り分析(video_algo_max_videos)とは独立。値変更は taskdef env 差し替えのみで反映可。"
  type        = string
  default     = "30"
}

# §M改(VSEO有効化): Gemini 認証は本番EC2と同方式の Vertex SA を採用（API キー方式は廃止）。
variable "vertex_sa_secret_name" {
  description = "Vertex AI SA JSON の secret 名（本番EC2 load_secrets.sh と同一ソース・enable_scrape_tools=true 時に VERTEX_SA_JSON として注入）"
  type        = string
  default     = "teamagent/dev/vertex_sa"
}

variable "gemini_vertex_project" {
  description = "Vertex AI の GCP プロジェクトID。2026-09-17 に個人課金の ntv-ai から会社プロジェクト newstv-508906（組織 vectorinc.co.jp）へ移行。Secrets Manager の vertex_sa も同プロジェクトの SA 鍵に差し替え済み"
  type        = string
  default     = "newstv-508906"
}

variable "gemini_vertex_location" {
  description = "Vertex AI ロケーション。Gemini 3 系（既定 gemini-3.5-flash・2026-09-24 裁定）は Vertex では global でのみ応答する。2.5 系（2026-10-16 廃止）へ戻すときは us-central1"
  type        = string
  default     = "global"
}

variable "enable_vpc_endpoints" {
  description = "VPC interface endpoint（bedrock/secrets/kms/ecr/logs）を作成して egress を private 化する"
  type        = bool
  default     = true
}

# ============================================================
# 2026-09-15: live（CLI 直登録の mcp TD）にだけ存在し tf に描画元が無かった env 8 key の取り込み
# ============================================================
# 2026-07-11 監査で見つかった「CLI 直登録 taskdef にだけ存在する env が apply で剥がれる」の再発防止。
# 既定値は 2026-09-15 の live（mcp TD:104）の実値に合わせる（tf 描画の TD が live と等価になる）。
# ⚠️ SLACK_WORKSPACE_DOMAIN は var.slack_workspace から描画する（live の値は同一・アプリ側は
#   SLACK_WORKSPACE へ fallback する。skills/_shared/source_url.py）。
variable "use_llm_router" {
  description = "検索の語彙統合ルーター（USE_LLM_ROUTER）。2026-08-31 便で live に投入済み。"
  type        = string
  default     = "true"
}

variable "use_oauth_start_links" {
  description = "連携リンクを path 形式（/oauth2/start）で出す（USE_OAUTH_START_LINKS・#376）。live は 1。"
  type        = string
  default     = "1"
}

variable "omiyage_search_depth" {
  description = "お土産資料の TikTok 検索深度（OMIYAGE_SEARCH_DEPTH）。dispatcher の n_per_kw 上限 30 と同値（#377）。"
  type        = string
  default     = "30"
}

variable "search_min_relevance_fallback" {
  description = "検索の足切り fallback（SEARCH_MIN_RELEVANCE_FALLBACK）。live は 0.05。"
  type        = string
  default     = "0.05"
}

variable "use_tiktok_apify_fallback" {
  description = "TikTok 取得失敗分の Apify 補完（USE_TIKTOK_APIFY_FALLBACK・#375/#379）。live は 1。"
  type        = string
  default     = "1"
}

variable "video_algorithm_apify_wallclock_s" {
  description = "動画分析の Apify 経路の壁時計上限秒（VIDEO_ALGORITHM_APIFY_WALLCLOCK_S）。live は 150。"
  type        = string
  default     = "150"
}

variable "video_algorithm_result_cache_enabled" {
  description = "動画分析の結果キャッシュ（VIDEO_ALGORITHM_RESULT_CACHE_ENABLED）。live は true。"
  type        = string
  default     = "true"
}

# ============================================================
# 2026-09-28: 動画分析（video_algorithm）の切り離し（detach）。既定はすべて「今と同じ」。
# ============================================================
# mcp が 30 秒で受付を返し、完了時に依頼元の会話へ直接届ける（OpenClaw の約 6 分の打ち切り対策）。
# 段階公開は TD の env 差し替えで進める（第 1 段階＝小俣さん本人の DM だけ）。
# ⚠️ TD 差し替えで ON/変更したら、activation 版 tfvars（正本・
#    ~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars）へ同じ値を必ず追記する。
#    terraform_runtime_guard.sh の live→tfvars 導出はこの 6 変数を列挙していないので、guard 経由の
#    apply（次の mcp便など）で既定（OFF・allowlist 空・LEASE 1800）に黙って戻る。
#    有効化の手順は docs/runbooks/video_algorithm_detach.md。
variable "use_video_algorithm_detach" {
  description = "動画分析の切り離し（USE_VIDEO_ALGORITHM_DETACH）。既定 0＝同期のまま（今と同じ）。⚠️ TD で ON/変更したら activation 版 tfvars（正本・~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars）へ同じ値を必ず追記。guard 経由の apply で既定に戻るため（手順: docs/runbooks/video_algorithm_detach.md）。"
  type        = string
  default     = "0"
}

variable "video_algorithm_detach_after_s" {
  description = "受付を返すまでの待ち秒（VIDEO_ALGORITHM_DETACH_AFTER_S）。アプリ側で 5〜240 に丸める。⚠️ TD で ON/変更したら activation 版 tfvars（正本・~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars）へ同じ値を必ず追記。guard 経由の apply で既定に戻るため（手順: docs/runbooks/video_algorithm_detach.md）。"
  type        = string
  default     = "30"
}

variable "video_algorithm_detach_allowed_emails" {
  description = "切り離しを使う人（VIDEO_ALGORITHM_DETACH_ALLOWED_EMAILS・カンマ区切り）。空なら誰にも適用しない。⚠️ TD で ON/変更したら activation 版 tfvars（正本・~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars）へ同じ値を必ず追記。guard 経由の apply で既定に戻るため（手順: docs/runbooks/video_algorithm_detach.md）。"
  type        = string
  default     = ""
}

variable "video_algorithm_detach_dm_only" {
  description = "切り離しを 1 対 1 DM だけに限る（VIDEO_ALGORITHM_DETACH_DM_ONLY）。既定 1。⚠️ TD で ON/変更したら activation 版 tfvars（正本・~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars）へ同じ値を必ず追記。guard 経由の apply で既定に戻るため（手順: docs/runbooks/video_algorithm_detach.md）。"
  type        = string
  default     = "1"
}

variable "video_algorithm_max_background" {
  description = "切り離した動画分析の同時実行上限（VIDEO_ALGORITHM_MAX_BACKGROUND）。超えた分は順番待ち（最大 10 件・満杯なら「混み合っています」）。0 なら切り離さない。⚠️ TD で ON/変更したら activation 版 tfvars（正本・~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars）へ同じ値を必ず追記。guard 経由の apply で既定に戻るため（手順: docs/runbooks/video_algorithm_detach.md）。"
  type        = string
  default     = "2"
}

variable "video_algorithm_cache_lease_seconds" {
  description = "動画分析の処理中リース秒（VIDEO_ALGORITHM_CACHE_LEASE_SECONDS）。既定 1800＝コード既定と同じ。切り離しの第 1 段階で 600 に下げる想定。⚠️ TD で ON/変更したら activation 版 tfvars（正本・~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars）へ同じ値を必ず追記。guard 経由の apply で既定に戻るため（手順: docs/runbooks/video_algorithm_detach.md）。"
  type        = string
  default     = "1800"
}

# ============================================================
# 2026-09-28: 検索上位チェックの 2 段目（上位の動画の中身を裏で分析して会話へ追記）。既定 OFF。
# ============================================================
# mcp_gateway/surface_video_followup.py。1 段目（約 2 分）は今どおり返し、同じ上位 N 本を
# video_algorithm の分析エンジンで裏で分析して 5〜9 分後に同じ DM へ追記する。
# 同時実行の上限と順番待ちは動画分析の切り離し（VIDEO_ALGORITHM_MAX_BACKGROUND）と共有。
# 1 回で動画分析の月間上限（1 人月 50 本）を最大 MAX_VIDEOS 本使う・+$0.05〜0.3/回。
# ⚠️ TD 差し替えで ON/変更したら、activation 版 tfvars（正本）へ同じ値を必ず追記する
#    （terraform_runtime_guard.sh の live→tfvars 導出はこの 3 変数を列挙していない）。
variable "use_surface_video_followup" {
  description = "検索上位チェックの 2 段目（USE_SURFACE_VIDEO_FOLLOWUP）。既定 0＝今と同じ（2 段目なし）。⚠️ TD で ON/変更したら activation 版 tfvars（正本・~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars）へ同じ値を必ず追記。guard 経由の apply で既定に戻るため。"
  type        = string
  default     = "0"
}

variable "surface_video_followup_allowed_emails" {
  description = "2 段目を使う人（SURFACE_VIDEO_FOLLOWUP_ALLOWED_EMAILS・カンマ区切り）。空なら誰にも適用しない。DM・署名検証済みの依頼だけが対象。⚠️ TD で ON/変更したら activation 版 tfvars（正本・~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars）へ同じ値を必ず追記。guard 経由の apply で既定に戻るため。"
  type        = string
  default     = ""
}

variable "surface_video_followup_max_videos" {
  description = "2 段目で分析する上位の動画の本数（SURFACE_VIDEO_FOLLOWUP_MAX_VIDEOS）。既定 5・アプリ側で 1〜10 に丸める。⚠️ TD で ON/変更したら activation 版 tfvars（正本・~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars）へ同じ値を必ず追記。guard 経由の apply で既定に戻るため。"
  type        = string
  default     = "5"
}

# ============================================================
# 2026-09-28: 検索上位チェックの結果を mcp が会話へ直接投稿する。既定 OFF。
# ============================================================
# mcp_gateway/direct_summary.py。SOUL の「そのまま返す」と MCP 返却の絞り込み（#462）の後も、
# Aico（Haiku）が文面を組み直して詳細レポートの URL を落としたため、slack_summary は mcp が
# 依頼元の 1 対 1 DM へ直接出し、Aico には「投稿済み」だけを返す。届かなければ今までどおり返す。
# ⚠️ TD 差し替えで ON/変更したら、activation 版 tfvars（正本）へ同じ値を必ず追記する
#    （terraform_runtime_guard.sh の live→tfvars 導出はこの 2 変数を列挙していない）。
variable "use_direct_summary_post" {
  description = "検索上位チェックの結果の直接投稿（USE_DIRECT_SUMMARY_POST）。既定 0＝今と同じ（Aico が返す）。⚠️ TD で ON/変更したら activation 版 tfvars（正本・~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars）へ同じ値を必ず追記。guard 経由の apply で既定に戻るため。"
  type        = string
  default     = "0"
}

variable "direct_summary_post_allowed_emails" {
  description = "直接投稿を使う人（DIRECT_SUMMARY_POST_ALLOWED_EMAILS・カンマ区切り）。空なら誰にも適用しない。1 対 1 DM・署名検証済みの依頼だけが対象。⚠️ TD で ON/変更したら activation 版 tfvars（正本・~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars）へ同じ値を必ず追記。guard 経由の apply で既定に戻るため。"
  type        = string
  default     = ""
}

# ============================================================
# 2026-09-29: F0 連携切れの見える化（PR-0b 連携の生存確認）。既定はすべて「今と同じ」。
# ============================================================
# mcp: oauth_connect が「連携済み」と答える前に保存済みトークンで refresh を 1 回試す
#   （skills/oauth_connect/skill.py・adapters/google_liveness.py・5 秒で打ち切り）。
# connect-web: Google が実際に許可した範囲（granted_scopes）を保存し、一部だけ許可されたときは
#   完了画面で足りない機能を伝える（adapters/google_oauth_flow.py・connect_web/app.py）。
# ⚠️ TD 差し替えで ON/変更したら、activation 版 tfvars（正本・
#    ~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars）へ同じ値を必ず追記する
#    （terraform_runtime_guard.sh の live→tfvars 導出はこの 2 変数を列挙していないので、guard 経由の
#    apply＝次の mcp便など で既定の OFF に黙って戻る）。
variable "oauth_connect_liveness_probe" {
  description = "「連携」で連携済みと答える前にトークンの生存を確かめる（OAUTH_CONNECT_LIVENESS_PROBE・mcp）。既定 0＝確かめない（今と同じ）。⚠️ TD で ON/変更したら activation 版 tfvars（正本・~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars）へ同じ値を必ず追記。guard 経由の apply で既定に戻るため。"
  type        = string
  default     = "0"
}

variable "connect_store_granted_scopes" {
  description = "Google が実際に許可した範囲を保存し、一部許可なら完了画面で伝える（CONNECT_STORE_GRANTED_SCOPES・connect-web）。既定 0＝要求した範囲を保存（今と同じ）。⚠️ TD で ON/変更したら activation 版 tfvars（正本・~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars）へ同じ値を必ず追記。guard 経由の apply で既定に戻るため。"
  type        = string
  default     = "0"
}
