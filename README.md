# TeamAgent v3.0

**Slack で動く社内 AI Agent 基盤**
営業16名 + マネージャー向けに、Claude Agent SDK と Skill Registry / Plugin アーキで構築する AI Agent プラットフォーム。

---

## 何ができるのか

`@TeamAgent` に話しかけると、メール・Slack・Drive・動画分析データを横断して、過去事例を引き出したり、提案書のたたきを生成したり、競合動画を分析したり、進捗をまとめたりします。Skill は **Skill Registry に無限に追加可能** な設計で、初期 Skill セット 5 本（動画ナレッジ分析 / Mail ワークフロー / 提案コンテンツ生成 / Slack 自動サジェスト / 営業進捗サマリー）から段階展開していきます。

## 戦略：MVA（Minimum Viable Agent）

「個別 Skill から作る」のではなく「**データ層を先に固めて、Skill を後から無限に乗せる**」ファウンデーション・ファーストで進めます。

| Phase | 期間 | 中身 |
|---|---|---|
| **MVA P1** | 〜2 ヶ月 | Gmail / Drive / Slack OAuth + pgvector への取り込み |
| **MVA P2** | 〜2.5 ヶ月 | Claude Agent SDK の基本ループ |
| **MVA P3** | 〜3 ヶ月 | 超軽量・検索 Skill（最初の登録 Skill） |
| **Phase 4** | 〜9 ヶ月 | 初期 Skill セット 5 本のフル展開 |
| **Phase 5** | 9 ヶ月〜 | 全社 AI 基盤「社内版 NoimosAI」へ |

## ドキュメント

実装に入る前に読んでおく順序：

1. **[MVA 仕様書 v1](docs/v3.0/mva_spec_v1.html)** ← 実装担当はまずこれ
2. **[構想・概要書 v3.0](docs/v3.0/overview_v3.0.html)** — 全体像
3. **[要件定義書 v3.0](docs/v3.0/requirements_v3.0.html)** — FR / NFR / DR
4. **[アーキテクチャ図書 v3.0](docs/v3.0/architecture_v3.0.html)** — C4 model / Skill Registry 設計
5. **[実装計画書 v3.0](docs/v3.0/implementation_plan_v3.0.html)** — 17 Sprint ロードマップ

完全な目次は [docs/README.md](docs/README.md) を参照。

## クイックスタート

### 1. ローカル開発環境

```bash
# 1) リポジトリを clone
git clone git@github.com:noirelumiere00/TeamAgent.git
cd TeamAgent

# 2) セットアップスクリプトを実行
bash scripts/setup_local.sh

# 3) .env を編集（API キー等）
vi .env

# 4) 動作確認
source .venv/bin/activate
pytest tests/
```

### 2. 必要なもの

- **Python 3.11+**
- **Docker Desktop**
- **API キー / 認証情報**：
  - Anthropic API or AWS Bedrock アクセス
  - Google OAuth Client（Gmail / Drive）
  - Slack App（Bot Token + App Token）
  - Gemini API（機能④ 動画分析用）
  - 詳細は `.env.example` 参照

### 3. 本番デプロイ

[infra/terraform/](infra/terraform/) に AWS Terraform 雛形あり。

```bash
cd infra/terraform
terraform init
terraform plan
terraform apply
```

## アーキテクチャ概要

```
┌─────────────────────────────────────────────────────────────┐
│ User（営業 16 名）                                          │
│   ↕                                                          │
│ Slack Bolt (Frontend)                                       │
│   ↕                                                          │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Claude Agent SDK (Orchestrator / Planner / Executor)    │ │
│ │   ├─ Skill Registry  ─→  検索 Skill / 動画 / Mail / ... │ │
│ │   ├─ Tools / MCP     ─→  pgvector / Web / Gemini / API  │ │
│ │   ├─ Memory / Session                                    │ │
│ │   └─ Permissions / Guardrails                            │ │
│ └─────────────────────────────────────────────────────────┘ │
│   ↕                                                          │
│ Data Layer (pgvector + S3)                                   │
│   ├─ Gmail / Drive / Slack ingest                            │
│   ├─ 動画分析データ                                          │
│   └─ Gemini DR / X                                           │
└─────────────────────────────────────────────────────────────┘
```

詳しくは [アーキテクチャ図書 v3.0](docs/v3.0/architecture_v3.0.html) を参照。

## 改訂履歴

- **v3.0** (2026-05-19)：設計思想の再構成。Skill Registry / Plugin パターン採用。OpenClaw 不採用。MVA 戦略採用。料理メタファ表現を技術書表現に統一。
- **v2.2** (2026-05-13)：整合性 QA 反映、応答 SLO 緩和、Tier 0-3 明記、データドリブン度 KPI 化
- **v2.1** (2026-05-08)：IBM レビュー反映、機能① をメンション起動に、17 Sprint 化
- **v2.0** (2026-05-07)：5 機能 / 6 データ層 / 4 共通基盤に再構成
- **v1.5** (2026-05-01)：ナレッジソース統合
- v1.0 〜 v1.4（2026-04-27 〜 04-30）：初版〜図解強化

過去版は [docs/archive/](docs/archive/) を参照。

## ライセンス

社内利用専用 / Proprietary

## 連絡先

Shogo Komata（TeamAgent 推進担当 / FDE）

---

最終更新：2026 年 5 月 19 日
