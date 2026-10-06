# OpenWiki 方針（teamagent / Aico）

この wiki の読者は、このリポジトリで作業するコーディングエージェントと開発者。
Aico（社内 Slack の AI 秘書）の仕組みを、コードを全部読まずに正しく把握できることを目的にする。

## 書き方

- 本文は日本語。コード・識別子・パスは英語のまま。
- 根拠はコードとテスト。`docs/` 配下の文書は古いことがあるので、コードと食い違ったらコードを正とし、食い違いがあることを書く。
- 秘密情報の値、AWS アカウント ID、クライアント名、個人名は書かない（鍵や設定は「どの仕組みで読むか」まで）。

## 優先して書くこと

1. 全体構成: Slack → OpenClaw / Hermes ランタイム → mcp_gateway → skills → adapters → AWS の流れと、それぞれの責任範囲。
2. リリースとデプロイ: コンテナイメージの種類（mcp / openclaw / tiktok / media）、ビルドとゲート（ECR スキャン・Trivy・世代 publish）、ECS タスク定義の差し替え。
3. 機能フラグと設定: どこで定義され、どこで読まれ、既定値が何か。
4. データ: PostgreSQL / pgvector のテーブルとマイグレーション、RLS と実行ロール。
5. 外部連携: Google / Slack の OAuth、トークン保管、Gemini / Bedrock の呼び出しとリトライ。
6. 主要な業務フロー: 朝ダイジェストとボタン処理、ingest（取り込み）、提案書・動画分析などの長時間ジョブ。
7. テストの走らせ方: CI と同じ依存（`--extra dev --extra mcp`、media 系は `--extra media`）と、よく使うテストの場所。

## 書かないこと

- ファイル一覧だけのページ。
- 過去の作業経緯（`infra/deploy_log.md` や activation 文書の時系列）の転記。
