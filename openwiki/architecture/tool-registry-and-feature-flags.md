---
type: architecture
title: ツール登録と機能フラグ
description: MCP のツール群を決める factory.build_production_tools の USE_* env フラグ（既定 OFF）、検索ノブの一元解決、OpenClaw toolFilter と effective-tool-scope、terraform の MCP task env への配線までの「4 段ゲート」と、フラグを増やす・変えるときの注意。
tags: [feature-flags, factory, toolfilter, terraform, mcp, configuration]
sources:
  - id: openwiki-source-58cab42b7659adb32893d79a
    resource: repo://infra/openclaw/effective-tool-scope.json
  - id: openwiki-source-ef54f8e63a41477899e459da
    resource: repo://infra/terraform/fargate.tf
  - id: openwiki-source-852620980912ad94a789a6b7
    resource: repo://src/teamagent/orchestrator/factory.py
  - id: openwiki-source-f97ff4a39ce3d3e6ac6dda9f
    resource: repo://tests/scripts/test_openclaw_runtime_contract.py
generated: { by: "claude-code", at: "2026-09-30T04:50:20.078Z" }
verified:
  - by: openwiki/0.6.1
    at: 2026-09-30T04:50:20.078Z
---

# ツール登録と機能フラグ

## 4 段ゲート

Skill を書いただけでは Aico から使えない。本番で呼べるまでに 4 段ある（CLAUDE.md §10 E1 と同じ整理）。

| 段 | 場所 | 止まったときの症状 |
|---|---|---|
| ① Skill 実装 | `src/teamagent/skills/<name>/` + `@register` | 「実装済」だが誰からも呼ばれない |
| ② factory 登録（env gate） | `src/teamagent/orchestrator/factory.py` の `build_production_tools()` | MCP の `tools/list` に出ない |
| ③ OpenClaw 露出 | `infra/openclaw/openclaw.config.json5` の `mcp.servers.teamagent.toolFilter.include` + `infra/openclaw/effective-tool-scope.json` | MCP にあっても Aico から見えない（一番の見落としポイント） |
| ④ 本番 ON | `infra/terraform/fargate.tf` の `aws_ecs_task_definition.mcp` の env → terraform apply | include にあっても MCP 側で未登録＝呼んでも失敗 |

`effective-tool-scope.json` の `effectiveRule` は「OpenClaw の include に載り、**かつ** MCP backend がデプロイ済みタスクで登録したツールだけが呼べる」と定める。たとえるなら、店の棚（factory）に並べ、メニュー（include）に載せ、その日の仕入れ（本番 env）がある料理だけが注文できる。

## factory のフラグ判定

`_envflag(name, default="false")` は env の値が `1` / `true` / `yes`（小文字化）のときだけ True。新しいツールは**必ず既定 OFF** で足す（後方互換）。

常に登録されるのは `search`・`clientkarte`・`proposal_draft`・`proposal_review` の 4 本（`SearchSkill` は 1 インスタンスを共有し、proposal 系にも注入する＝埋め込みモデルの二重ロード回避）。それ以外はフラグで追加される。

| フラグ | 追加されるツール |
|---|---|
| `USE_KNOWLEDGE_DELIVER` | `knowledge_deliver` |
| `USE_RECOMMEND_SKILL` | `recommend` |
| `USE_MAIL_TOOLS` | `mail_constraints` |
| `USE_WORKSPACE_TOOLS` | `workspace_search` |
| `USE_VIDEO_TOOLS` | `video_analysis`、`video_algorithm` |
| `USE_TIKTOK_TOOLS` | `tiktok_search` |
| `USE_VIDEO_APPROVAL` | `video_approval` |
| `USE_OPERATION_LOG_TOOLS` | `operation_log` |
| `USE_PROPOSAL_DECK_TOOLS` | `proposal_deck` |
| `USE_PROPOSAL_BUILDER_TOOLS` | `proposal_builder_submit`、`proposal_builder_status`（同期版 `proposal_builder` は MCP に出さない） |
| `USE_OMIYAGE_REPORT_TOOLS` | `omiyage_report_submit`、`omiyage_report_status` |
| `USE_PROPOSAL_CAMPAIGN_TOOLS` | `proposal_campaign` |
| `USE_MAIL_LINK_TOOL` / `USE_FOLLOWUP_TOOL` / `USE_MAIL_SUMMARY_TOOL` / `USE_MAIL_REPLY_TOOL` | `mail_to_internal_context` / `mail_followup` / `mail_summary` / `mail_reply` |
| `USE_SCHEDULE_PROPOSE_TOOL` / `USE_CALENDAR_EVENT_TOOL` / `USE_DIGEST_ACK_TOOL` / `USE_CALENDAR_FREEBUSY_TOOL` | `schedule_propose` / `calendar_event` / `digest_ack` / `calendar_freebusy` |
| `USE_SLACK_SUMMARY_TOOL` / `USE_ATTACHMENT_TOOLS` / `USE_VIDEO_CAPTURE_TOOL` / `USE_WEB_RESEARCH_TOOL` | `slack_summary` / `attachment_assist` / `video_capture` / `web_research` |
| `USE_MAIL_DRAFT_TOOL` / `USE_MORNING_DIGEST_TOOL` / `USE_OAUTH_CONNECT_TOOL` / `USE_KNOWLEDGE_SEARCH_URL_TOOL` | `mail_draft` / `morning_digest` / `oauth_connect` / `knowledge_search_url` |
| `USE_TIKTOK_ACQUIRE` | `tiktok_acquire`、`tiktok_acquire_status` |
| `USE_X_RESEARCH_TOOLS` | `x_voice_search`、`x_needs_mining`、`x_buzz_measure`、`x_buzz_measure_status` |
| `USE_SEARCH_SURFACE_TOOL` / `USE_TIKTOK_COMMENT_TOOLS` | `search_surface_check` / `tiktok_comment_mining` |
| `USE_RESEARCH_PERSIST` | ツールは増えない。X の声集めなどの成果物を pgvector に永続化する persister を注入 |

MCP gateway 側には factory 以外のフラグもある: `USE_AGENT_ORCHESTRATOR`（`run_agent`）、`USE_PERSONAL_MEMORY`（本人メモ）、`USE_VIDEO_ALGORITHM_DETACH`・`USE_SURFACE_VIDEO_FOLLOWUP`・`USE_ASYNC_JOB_NOTIFY`・`ENABLE_PROGRESS_NOTIFY`・`USE_DIRECT_SUMMARY_POST`・`USE_PAYLOAD_OFFLOAD`（[長時間ジョブの切り離し](detached-jobs-and-async-notify.md)）。

### 検索ノブの一元解決

`resolve_search_skill_config()` は SearchSkill の全ノブを env から解決する**唯一の真実源**で、MCP 経路と旧 Slack bot 経路の両方がこれを使う（過去に片方だけ既定値に落ちて本番 env が黙って無効化された構築ドリフトの対策）。主なノブ: `USE_CONTEXTUAL`、`USE_NEW_SCHEMA`、`USE_FB_DRIVE_MATCH`、`USE_COHERE_RERANK`、`SEARCH_RERANK_POOL_SIZE`（既定 30）、`SEARCH_RERANK_RETURN_SIZE`（100）、`SEARCH_DRIVE_POOL_FLOOR`（15）、`SEARCH_MIN_RELEVANCE`（0.0）、`SEARCH_MIN_RELEVANCE_FALLBACK`（0.0）、`USE_CLIENT_BOOST`（**既定 ON**）、`USE_AGGREGATION_MODE`、`USE_KNOWLEDGE_FILTERS`、`PROMPT_VERSION`（既定 `v2d`）、`SEARCH_MAX_TOKENS`（800）。構築時に `search_skill_config_resolved` ログで全値を出す。中身は [社内資料検索](../workflows/knowledge-search.md)。

## terraform での配線

MCP の task env（`fargate.tf` の `aws_ecs_task_definition.mcp`）は 3 通りの書き方が混在している。

1. **固定 ON**: `USE_OAUTH_CONNECT_TOOL`・`USE_MAIL_SUMMARY_TOOL`・`USE_FOLLOWUP_TOOL`・`USE_MAIL_LINK_TOOL`・`USE_MAIL_REPLY_TOOL`・`USE_MORNING_DIGEST_TOOL`・`USE_MAIL_DRAFT_TOOL`・`USE_NEW_SCHEMA`・`USE_KNOWLEDGE_FILTERS`・`USE_KNOWLEDGE_DELIVER`・`USE_COHERE_RERANK` などは値が直書き。
2. **変数で切替**: `USE_CALENDAR_EVENT_TOOL`・`USE_SCHEDULE_PROPOSE_TOOL`・`USE_DIGEST_ACK_TOOL`・`USE_CALENDAR_FREEBUSY_TOOL`・`USE_SLACK_SUMMARY_TOOL`・`USE_ATTACHMENT_TOOLS`・`USE_VIDEO_CAPTURE_TOOL`・`USE_WEB_RESEARCH_TOOL`・`USE_PAYLOAD_OFFLOAD`・`USE_VIDEO_ALGORITHM_DETACH`・`USE_SURFACE_VIDEO_FOLLOWUP`・`USE_DIRECT_SUMMARY_POST` などは `var.use_*` から。
3. **機能ブロックごと**: `var.enable_proposal_builder`・`var.enable_x_research`・`var.enable_scrape_tools`・media worker などが真のときだけ、関連する env（`USE_PROPOSAL_BUILDER_TOOLS`、`USE_X_RESEARCH_TOOLS`＋`USE_SEARCH_SURFACE_TOOL`＋`USE_TIKTOK_COMMENT_TOOLS`、`USE_VIDEO_TOOLS`＋`USE_TIKTOK_TOOLS` など）とキュー・テーブル名を `concat` でまとめて足す。`enable_scrape_tools`・`enable_proposal_builder`・`enable_omiyage_report` は media worker が有効でないと precondition で plan が失敗する。

同じ task env には `DRAFT_ON_DEMAND_ONLY="true"`（mention 経由の MCP では自動下書きを作らない。CLAUDE.md §4 B8）、`STRUCTLOG_FORMAT="json"`、`BEDROCK_MODEL_ID`（`var.mcp_model_id`）、`SLACK_TEAM_ID`、`TEAMAGENT_SHARED_COMPANY_DOMAINS`、段階公開 allowlist（`X_RESEARCH_ALLOWED_EMAILS` など）も載る。

env を変えても **terraform apply で task definition の新 revision を登録し、サービスや EventBridge target がそれを指すまで本番には効かない**（CLAUDE.md §4 B4）。デプロイ手順は [リリースゲートとデプロイ](../operations/release-gates-and-deploy.md)。

## OpenClaw 側の露出

- `toolFilter.include` に明示列挙されたツールだけが見える。未レビューの重操作は `exclude` で明示的に外す。
- `effective-tool-scope.json` は各ツールの副作用分類（`effect`）、`terraformGate`、`enabledBy`（`always` / env / `never`）を管理し、runtime contract テストが include・factory・terraform との整合を突き合わせる。
- `enabledBy=never` のツール（`video_approval`・`operation_log`・`knowledge_search_url`）は MCP 側のフラグだけでは解禁されない。「tf の task env・scope の enabledBy・contract テスト・OpenClaw イメージ再ビルド」の 4 点を同じ変更で揃える。
- ツールの description は OpenClaw がツールを選ぶ唯一の材料。似たツール（特に動画系: `tiktok_search` / `tiktok_acquire` / `video_algorithm` / `video_analysis` / `video_approval`）はトリガー語と「対象外（→別ツール）」の注記で棲み分ける。検証方法は [ルーティング検証と評価](../testing/routing-and-eval.md)。

## 段階公開の allowlist

ツール単位の段階公開には env の email allowlist を使う。**空の意味が 2 種類ある**ので注意。

| 実装 | 空・未設定のとき |
|---|---|
| `skills/_shared/rollout.py` の `rollout_allowed`（X リサーチ・検索面チェック・コメント分析など） | 全員許可 |
| `mcp_gateway` の切り離し・直接投稿・2 段目・本人メモ | 誰にも適用しない |

## 食い違い・注意

- `fargate.tf` のコメントには「OpenClaw timeout(300s)」とあるが、現在の OpenClaw config の MCP timeout は 600 秒。
- CLAUDE.md §1 が指摘するとおり、Bedrock モデル ID の既定が `variables.tf`（Sonnet・サフィックス無し）と `variables_fargate.tf`（Haiku・版付き）で書式不一致。MCP は `mcp_model_id` を使う。新しくモデルを指すときは実在する推論プロファイル ID を確認する。

## テスト

- `tests/orchestrator/test_factory_smoke.py`、`tests/orchestrator/test_search_skill_config_parity.py`（ノブ解決の一致）
- `tests/scripts/test_openclaw_runtime_contract.py`（include・scope・束縛の整合）
- 各 skill の単体テスト（登録と description の固定）: `tests/skills/`
