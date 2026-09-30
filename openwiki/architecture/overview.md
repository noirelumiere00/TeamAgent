---
type: architecture
title: 全体構成
description: Aico（TeamAgent）の実行時構成。Slack → OpenClaw（受け口・Haiku）→ MCP gateway（Skill 実体・秘密を持つ側）→ skills → adapters → AWS / Google / Slack の流れと、ECS サービス・スケジュールタスク・使い捨て Fargate・Lambda・Hermes・停止中の旧 EC2 worker の責任範囲。
tags: [architecture, overview, ecs, openclaw, mcp, aws]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T07:51:05.076Z
sources:
  - id: openwiki-source-c70c8eeab3912252bf404e66
    resource: repo://build_tiktok_image.sh
  - id: openwiki-source-bc8a30c6713f1d80f06dcb70
    resource: repo://infra/terraform/canary_schedule.tf
  - id: openwiki-source-037ca235c39b9535c1b5b718
    resource: repo://infra/terraform/connect_web.tf
  - id: openwiki-source-ef54f8e63a41477899e459da
    resource: repo://infra/terraform/fargate.tf
  - id: openwiki-source-12ebfa30d204595170297d8f
    resource: repo://infra/terraform/ingest_schedule.tf
  - id: openwiki-source-e0b106f8a839c4d9c63c200a
    resource: repo://infra/terraform/morning_digest_schedule.tf
  - id: openwiki-source-28586a4f91418f64bc0ae9e5
    resource: repo://infra/terraform/openclaw_state.tf
  - id: openwiki-source-0797f11611524f999bdf6d0c
    resource: repo://infra/terraform/tiktok_acquire.tf
  - id: openwiki-source-a724d1b5718b86090fdc57f9
    resource: repo://infra/terraform/worker.tf
  - id: openwiki-source-c7fe456d30b27763073e95ae
    resource: repo://infra/terraform/x_research.tf
  - id: openwiki-source-ab4c69f811ca2c613d22078a
    resource: repo://src/teamagent/runtime/slack_bot.py
generated: { by: "claude-code", at: "2026-09-29T07:51:05.076Z" }
---

# 全体構成

## 一言でいうと

Aico は社内営業向けの Slack AI 秘書。利用者が Slack で話しかけると、**OpenClaw** が内容を理解してツールを選び、**MCP gateway** がそのツール（Skill）を本人の権限で実行し、結果を OpenClaw が文章に整えて返す。定時処理（朝ダイジェスト・資料取り込み・監視）は別の ECS タスクが EventBridge で起動する。

たとえるなら、OpenClaw は「受付と通訳」、MCP gateway は「鍵を持った事務局」、skills は「各担当者」、adapters は「電話回線」。

## 実行時の構成

```text
Slack（DM・メンション・ボタン）
  │ Socket Mode
  ▼
OpenClaw（ECS サービス・Node）             … Haiku 4.5 でツール選択と返事の文面
  │ caller-identity plugin が署名 claim を付与
  │ streamable-http + bearer（Cloud Map 内部名 :8787/mcp）
  ▼
MCP gateway（ECS サービス・Python / teamagent-mcp イメージ）
  │ claim 検証 → Slack で本人解決 → RLS メタ → Skill 実行
  ├─ skills/*（業務ロジック）
  │    └─ adapters/*（Bedrock・Gemini・PostgreSQL/pgvector・Slack・Google・S3・DynamoDB・Apify）
  ├─ SQS → Lambda dispatcher → 使い捨て Fargate（media worker / x-buzz worker）
  └─（本人メモ・既定 OFF）Hermes 学習係（TLS）

EventBridge（cron）→ ECS scheduled task（teamagent-mcp イメージを流用）
  ├─ morning_digest（平日朝＋planner モード）
  ├─ ingest（週次・Lambda ingest_dispatch 経由）
  └─ canary（毎時の健全性確認）

connect-web（ECS サービス・teamagent-mcp イメージ）… Google 連携の OAuth 画面・資料検索 Web UI
RDS PostgreSQL 16 + pgvector（RLS）   踏み台 EC2（SSM のみ）
```

## 各コンポーネントの責任

| コンポーネント | 定義 | 責任 | 触れないもの |
|---|---|---|---|
| OpenClaw | `infra/terraform/fargate.tf`（`aws_ecs_service.openclaw`）、`infra/openclaw/` | Slack の受信・返信、ツール選択、SOUL.md に沿った文面づくり、ボタン押下の直接実行 | RDS・Secrets の営業データ・Google |
| MCP gateway | `fargate.tf`（`aws_ecs_service.mcp`）、`src/teamagent/mcp_gateway/` | ツール一覧の公開、本人確認、Skill 実行、長時間ジョブの切り離し、usage 記録 | Slack の会話の受け口 |
| skills | `src/teamagent/skills/` | 検索・メール・カレンダー・提案書・動画分析などの業務ロジック | SDK の直接呼び出し（adapters 経由） |
| adapters | `src/teamagent/adapters/` | 外部サービスのクライアント、再試行、コスト記録 | 上位層の import |
| media worker | `infra/terraform/tiktok_acquire.tf`（名前は旧称）、`infra/docker/Dockerfile.teamagent-media-worker` | ブラウザ・動画取得・フレーム抽出・PPTX 描画など重い処理。task role 無しの使い捨て Fargate で、S3 の署名付き URL だけを受け取る | AWS 資格情報・DynamoDB |
| x-buzz worker | `infra/terraform/x_research.tf` | X の効果測定の非同期ジョブ（`teamagent.workers.x_buzz_job`） | |
| scheduled tasks | `morning_digest_schedule.tf`・`ingest_schedule.tf`・`canary_schedule.tf` | 朝ダイジェスト、資料取り込み、監視 | |
| connect-web | `connect_web.tf`、`src/teamagent/connect_web/` | 本人の Google 認可（token 保存）、資料検索 Web UI | |
| Lambda | `infra/terraform/lambda/` | SQS からの RunTask（ingest / tiktok / x）、media の後始末、リマインダー通知 | |
| Hermes | `hermes_runtime/` | 本人メモの学習係（返事は作らない）。本リポジトリの terraform には未配線 | MCP・RDS・Slack |
| RDS | `rds.tf`、`infra/migrations/` | documents/chunks・OAuth token・usage・digest 状態など | |

MCP の task role には RunTask / PassRole を持たせず、使い捨て Fargate の起動権限は Lambda dispatcher に集約している（`x_research.tf` と `tiktok_acquire.tf` の冒頭）。OpenClaw の状態（workspace）は EFS に永続する（`openclaw_state.tf`）。

## 代表的な流れ

<!-- openwiki: broken internal link [/openwiki/architecture/mcp-gateway.md] link "/openwiki/architecture/mcp-gateway.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/openwiki/workflows/knowledge-search.md] link "/openwiki/workflows/knowledge-search.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
1. **会話**: DM で「◯◯の過去提案ある？」→ OpenClaw が `search` を選ぶ → plugin が claim を付ける → MCP が本人を解決して RLS 付きで pgvector 検索 → 結果 JSON を OpenClaw が文章化。→ [MCP gateway](/openwiki/architecture/mcp-gateway.md)、[社内資料検索](/openwiki/workflows/knowledge-search.md)
<!-- openwiki: broken internal link [/openwiki/workflows/morning-digest.md] link "/openwiki/workflows/morning-digest.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/openwiki/workflows/digest-buttons.md] link "/openwiki/workflows/digest-buttons.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
2. **朝ダイジェスト**: EventBridge が平日朝に起動 → 利用者ごとに Gmail / Calendar を本人の OAuth で読む → DM に投稿（ボタン付き）→ ボタン押下は OpenClaw の plugin が直接 MCP ツールを呼ぶ。→ [朝ダイジェスト](/openwiki/workflows/morning-digest.md)、[ボタン処理](/openwiki/workflows/digest-buttons.md)
<!-- openwiki: broken internal link [/openwiki/architecture/detached-jobs-and-async-notify.md] link "/openwiki/architecture/detached-jobs-and-async-notify.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
3. **重い処理**: 動画分析・提案書・TikTok 取得は MCP 内の thread か SQS→Fargate で走り、完了を MCP が Slack に直接届ける。→ [長時間ジョブ](/openwiki/architecture/detached-jobs-and-async-notify.md)
<!-- openwiki: broken internal link [/openwiki/workflows/ingest-pipeline.md] link "/openwiki/workflows/ingest-pipeline.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
4. **取り込み**: 週次の ingest タスクが Drive などから資料を抽出・分類・埋め込みして保存。→ [資料取り込み](/openwiki/workflows/ingest-pipeline.md)

## イメージ

| イメージ | 使う場所 |
|---|---|
| `teamagent-mcp`（`infra/docker/Dockerfile.teamagent-mcp`） | MCP・connect-web・morning_digest・ingest・canary・x-buzz worker |
| OpenClaw（`Dockerfile.openclaw`） | OpenClaw サービス |
| `teamagent-media-worker`（`Dockerfile.teamagent-media-worker`） | 使い捨て media worker |
| Hermes（`Dockerfile.hermes`） | 本人メモ学習係（未配線） |

<!-- openwiki: broken internal link [/openwiki/operations/container-images-and-build.md] link "/openwiki/operations/container-images-and-build.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
TikTok 取得専用のイメージは別リポジトリ（tiktok-data-service）の担当で、このリポジトリの `build_tiktok_image.sh` は意図的に失敗するだけのスクリプトになっている。ビルドとゲートは [コンテナイメージとビルド](/openwiki/operations/container-images-and-build.md)。

## 旧 EC2 worker（停止中・退役決定）

`infra/terraform/worker.tf` は、かつて Slack Socket Mode Bot（`src/teamagent/runtime/slack_bot.py`、Slack Bolt 製）と動画処理を 24 時間常駐させていた EC2 の定義。現在の Slack 受け口は OpenClaw で、CLAUDE.md の記述ではこの EC2 は停止中・退役決定済み（terraform 上の EC2・IAM・SG は destroy 保留で残っている）。`slack_bot.py` のコードは残っており、ボタン処理の一部の文面（取り消し導線など）は OpenClaw plugin がこの経路と揃えている。新規機能をこちらに足さないこと。

## 変えない前提（抜粋）

- Claude は Bedrock 経由で呼ぶ（東京の推論プロファイル `jp.anthropic.*`）。
- OpenClaw は営業データに触れず、本人ごとの認可は MCP 境界で行う。
<!-- openwiki: broken internal link [/openwiki/architecture/tool-registry-and-feature-flags.md] link "/openwiki/architecture/tool-registry-and-feature-flags.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- ツールを本番で使えるようにするには factory・OpenClaw toolFilter・本番 env の段をすべて通す（[ツール登録と機能フラグ](/openwiki/architecture/tool-registry-and-feature-flags.md)）。
<!-- openwiki: broken internal link [/openwiki/architecture/layering-and-skill-contract.md] link "/openwiki/architecture/layering-and-skill-contract.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/openwiki/operations/terraform-layout.md] link "/openwiki/operations/terraform-layout.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- コードの依存方向は [3層分離と Skill の契約](/openwiki/architecture/layering-and-skill-contract.md)、AWS 側の詳細は [Terraform 構成](/openwiki/operations/terraform-layout.md)。
