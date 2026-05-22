# TeamAgent v3.1 設計訂正ノート（2026-05-22）

> ⚠️ **このドキュメントは v3.1 の設計判断を訂正するための公式記録**です。
> 関連する v3.1 系 HTML ドキュメントは本訂正に**従って読み替え**てください。
> 修正版（v3.2）の発行は **Sprint 2 末ゲート①（2026-06-07 頃）** を予定しています。

---

## 📌 概要

Sprint 1 Day 2 の Web 調査と E2E 疎通検証の結果、v3.1 ドキュメントに以下 3 件の **事実と異なる前提**が判明したため記録します。

| # | 訂正項目 | 影響度 |
|---|---|---|
| 1 | OpenClaw は Anthropic Agent SDK 互換**ではない** | 🔴 高（採用根拠の柱が崩れる） |
| 2 | OpenClaw は TypeScript / Node.js 製（言語境界課題） | 🟡 中（統合方針の見直し） |
| 3 | ClawHub のセキュリティ評価が甘い（サプライチェーン事例あり） | 🟡 中（運用ルール強化） |

これに伴い、**OpenClaw 採用ステータスを「フル採用（v3.1 確定）」から「再評価中（2026-05-22〜）」に変更**します。

---

## 訂正 ① OpenClaw は Anthropic Agent SDK 互換ではない

### v3.1 ドキュメントでの記述（誤り）

- `docs/v3.1/teamagent_overview_v3.1.html:1640`
  > （OpenClaw を AI Agent Runtime として正式採用 / **Skill 内部ロジックは Claude Agent SDK**）

- `docs/v3.1/teamagent_overview_v3.1.html:1785-1788`
  > v3.1 のアーキは **OpenClaw Runtime + Claude Agent SDK Skill の二層構成**として記述する。
  > Skill 内部の Agent ロジック（Planner / Executor / Tool 呼び出し）は **Claude Agent SDK** で実装し、LLM は **Bedrock 経由 Claude Sonnet 4.6** を呼ぶ

- `docs/v3.1/teamagent_implementation_plan_v3.1.html:1613`
  > OpenClaw が **Agent SDK 互換のオープンソース実装**として既に存在し、Skill Hub（ClawHub）も整っていること

### 事実

- OpenClaw 公式 GitHub Issue [#10149](https://github.com/openclaw/openclaw/issues/10149) で `@anthropic-ai/claude-agent-sdk` の採用が **"Closed as not planned"** で却下済み（2026-02）
- OpenClaw は独自ランタイム + 直 Anthropic API 呼び出し構成

### 訂正後の方針

- v3.1 の「二層構成」（OpenClaw が外側 + Claude Agent SDK が内側）の前提は**破綻**
- 「OpenClaw を採用するなら、Skill は OpenClaw 独自の `SKILL.md` 仕様で書く」必要がある
- **取りうる選択肢**：
  - **(a)** OpenClaw 非採用：現在の `boto3 + slack-bolt + 自前 Skill Registry` を続け、内部で公式 `claude-agent-sdk` を使う（推奨：Sprint 1 で動作実証済み）
  - **(b)** OpenClaw 採用：Python Skill を `SKILL.md` ラッパで OpenClaw に登録、TypeScript ランタイムから呼び出し（複雑性高）

---

## 訂正 ② OpenClaw は TypeScript/Node.js 製（言語境界課題）

### v3.1 ドキュメントでの記述（言及なし）

- `docs/v3.1/teamagent_overview_v3.1.html:1772`
  > OpenClaw — AI Agent Runtime の OSS（GitHub 24.7 万 stars）
  > （**実装言語の言及なし**）

### 事実

- OpenClaw 本体は **TypeScript / Node.js** 実装
- TeamAgent の Skill は **Python**（pydantic v2 + boto3 + psycopg）
- 言語境界をまたぐ統合が必要：
  - シェル呼び出し（OpenClaw → Python CLI Skill）
  - HTTP API 経由（OpenClaw → Python FastAPI Skill）
  - 双方向 IPC（複雑）

### 訂正後の方針

- v3.1 が想定していた「Skill を Python で書いて OpenClaw に置く」は単純には不可
- もし採用するなら以下のどれかを選択：
  - **(a)** Skill を Node.js / TypeScript で書き直す（既存資産を捨てる）
  - **(b)** Python Skill を独立 HTTP サーバ化し、OpenClaw から呼び出す（HTTPS + 認証必要）
  - **(c)** OpenClaw 非採用（推奨）

---

## 訂正 ③ ClawHub のサプライチェーンリスク評価が甘い

### v3.1 ドキュメントでの記述（評価不足）

- `docs/v3.1/teamagent_overview_v3.1.html:2692`
  > ClawHub は OpenClaw コミュニティが公開する Skill 共有レジストリだが、本案件では **ホワイトリスト運用**（社内審査済みの Skill のみ）を採用する。

- `docs/v3.1/teamagent_overview_v3.1.html:2697`
  > Custom Skills（社内実装）は GitHub PR レビュー + コードオーナー承認を必須化

- `docs/v3.1/teamagent_overview_v3.1.html:2703`
  > **週次**で OpenClaw 本体の GitHub Releases / CVE データベース / セキュリティアドバイザリを情シスが確認

### 事実

- ClawHub に過去 **341 件の悪意ある Skill** が混入し、**9,000+ インストール** が侵害された事例あり（ClawHavoc インシデント、Cisco 研究者報告）
- HKCERT / Straiker / CertiK が「**Skill scanning は security boundary にならない**」と公式警告
- 中国規制当局は ClawHub へのアクセスを制限

### 訂正後の方針

- 「ホワイトリスト運用 + 週次 CVE 確認」だけでは不十分
- 追加必須項目：
  - **(a)** Skill ごとのサンドボックス制限（ファイルアクセス・ネットワーク・実行時間）
  - **(b)** Skill 実行時の権限分離（最小権限の原則）
  - **(c)** Skill 投入前の動的解析（ClawScan ＋ 社内独自スキャン二重化）
  - **(d)** Skill インシデント時のロールバック手順

---

## 📝 ライセンスの誤記

| | v3.1 ドキュメント | 実態 |
|---|---|---|
| OpenClaw ライセンス | Apache-2.0 と記載 | **MIT License** |

- `docs/v3.1/teamagent_implementation_plan_v3.1.html:1613` の「Apache-2.0 で fork 可能」は誤り
- 実際は MIT。fork 可能性に影響なし（むしろ MIT は更に緩い）

---

## 🔢 数値の誤差（軽微）

| | v3.1 ドキュメント | 2026-05-22 時点 Web 確認値 |
|---|---|---|
| OpenClaw GitHub stars | 24.7 万 | 約 37.4 万 |

数値は時間経過で変動するもので、判断には影響しない。

---

## 🔄 OpenClaw 採用ステータス変更

| 時点 | ステータス |
|---|---|
| v3.0 まで | 不採用 |
| v3.1（2026-05-20） | **フル採用** |
| **v3.1 訂正版（2026-05-22）** | **🟡 再評価中** |
| Sprint 2 末ゲート①（2026-06-07 予定） | (a) 採用継続 / (b) 不採用に転換 / (c) 部分採用 のいずれかを決定 |

---

## 🎯 Sprint 2 末ゲート①の判断材料

子会社の運用ヒアリング（質問リスト送信済み）と以下の追加検証で最終判断する：

1. **OpenClaw を localhost 起動できるか**（docker compose）
2. **Slack mention → OpenClaw → Bedrock の Hello World が動くか**
3. **Python Skill を OpenClaw から呼べるか**（言語境界の解決方法）
4. **ClawHub の Skill インシデント対応プロセスが社内ポリシーと整合するか**
5. **子会社 120 ユーザー運用の具体内容**（運用開始時期、運用期間、トラブル事例）

---

## 📊 当面の影響範囲

| 項目 | 影響 |
|---|---|
| 現状の `src/teamagent/` 実装 | **影響なし** — Skill Registry / adapters は OpenClaw 採否によらず有効 |
| `runtime/slack_bot.py` | **影響なし** — Bolt Socket Mode は OpenClaw 採否によらず有効 |
| `adapters/bedrock_client.py` | **影響なし** — Bedrock 経由の Claude 呼び出しは方針通り |
| pgvector / RDS | **影響なし** — データ層は OpenClaw 採否によらず継続 |
| Sprint 1 / Day 2 までの実装 | **影響なし** — 全 PR がそのまま有効 |

つまり、**現在動いている TeamAgent コードはそのまま継続できます**。OpenClaw 採否の判断は別軸として進めます。

---

## 📚 引用元（Web 調査）

- OpenClaw GitHub: https://github.com/openclaw/openclaw
- Anthropic Agent SDK 採用却下 Issue: https://github.com/openclaw/openclaw/issues/10149
- OpenClaw ドキュメント: https://docs.openclaw.ai
- ClawHub: https://github.com/openclaw/clawhub
- ClawHavoc インシデント解説（HKCERT）: https://www.hkcert.org/blog/openclaw-s-rapid-adoption-exposes-skills-supply-chain-and-fake-installer-risks-in-a-high-privilege-ai-agent-platform
- Skill scanning is not a security boundary（CertiK）: https://www.certik.com/blog/skill-scanning-is-not-a-security-boundary
- Anthropic Agent SDK 互換の混乱解説: https://thenewstack.io/anthropic-agent-sdk-confusion/

---

## 更新履歴

| 日付 | バージョン | 更新内容 |
|---|---|---|
| 2026-05-22 | v0.1 | 初版（3 訂正項目を記録、OpenClaw 採用を「再評価中」に変更） |
| 2026-05-22 | v0.2 | Anthropic 5/13 「Agent SDK Credits」発表反映、OpenClaw + Bedrock 連携の実装可能性を実証データで更新、移行プラン 4 案を追加 |

---

# 📌 v0.2 追加内容（2026-05-22 夕方）

## 1. Anthropic 「Agent SDK Credits」発表（2026-05-13）

**前提が変わったので訂正ノート v0.1 を補完**。

### 発表内容
- 発表日：**2026年5月13日 20:10 PT**
- 適用開始：**2026年6月15日**
- 内容：第三者 Agent（OpenClaw 等）の利用が **Agent SDK Credits** として正式に有料サブスクリプションから分離・許諾される

### クレジット枠
| プラン | 月次 Agent SDK Credit |
|---|---|
| Pro | $20 |
| Max 5x | $100 |
| Max 20x | $200 |

クレジットは月次失効、繰越なし。Interactive Claude Code / Cowork / chat はサブスク枠のまま。

### 経緯
- 2026/4：Anthropic が第三者 Agent の Claude サブスク利用を制限（GPU インフラ逼迫）
- 2026/5/13：「Agent SDK Credits」として復活
- 6/15 から正式適用

### TeamAgent への影響
**TeamAgent は Bedrock 経由で Claude を呼ぶ設計**のため、**Agent SDK Credits は使わない**。
Anthropic サブスクリプション課金体系の変動から AWS Bedrock 経由で遮断される。

### 出典
- [VentureBeat: Anthropic reinstates OpenClaw and third-party agent usage on Claude subscriptions](https://venturebeat.com/technology/anthropic-reinstates-openclaw-and-third-party-agent-usage-on-claude-subscriptions-with-a-catch)
- [The New Stack: Anthropic Agent SDK credits](https://thenewstack.io/anthropic-agent-sdk-credits/)
- [Gigazine: Anthropic Claude Agent SDK credits](https://gigazine.net/gsc_news/en/20260514-anthropic-claude-agent-sdk-credits/)

---

## 2. OpenClaw 実証データ（gh / docs 直接確認）

私自身で `gh api` および公式 docs で確認した事実：

### 2.1 リポジトリ事実
| 項目 | 値 | 出典 |
|---|---|---|
| URL | https://github.com/openclaw/openclaw | gh api |
| Description | "Your own personal AI assistant. Any OS. Any Platform. The lobster way. 🦞" | gh api |
| Stars | **373,824** | gh api（2026-05-22 時点）|
| Forks | 77,665 | gh api |
| Open Issues | 7,425 | gh api |
| Language | TypeScript（114MB / Python 0.08MB） | gh api |
| License | **MIT** | gh api |
| 最新リリース | **v2026.5.20**（2026-05-21） | gh release list |
| Homepage | https://openclaw.ai | gh api |

### 2.2 Issue #10149（Agent SDK 採用）の正確な状態
- Title: "[Feature]: Adopt @anthropic-ai/claude-agent-sdk for enhanced agent capabilities"
- State: **CLOSED**
- stateReason: **NOT_PLANNED**
- Created: 2026-02-06
- Closed: 2026-04-24
- Author: Emanuel Ciuca (eciuca)

**意義**：OpenClaw が **内部実装として** Claude Agent SDK を採用するのは却下。  
ただし **ユーザーが外部から Agent SDK Credits 経由で連携する**運用は、5/13 発表により可能（TeamAgent は Bedrock 経由なので無関係）。

### 2.3 Bedrock provider が公式サポート
出典: https://raw.githubusercontent.com/openclaw/openclaw/main/docs/providers/bedrock.md

```json5
{
  models: {
    providers: {
      "amazon-bedrock": {
        baseUrl: "https://bedrock-runtime.us-east-1.amazonaws.com",
        api: "bedrock-converse-stream",
        auth: "aws-sdk",
        models: [
          { id: "us.anthropic.claude-opus-4-6-v1:0", ... }
        ],
      },
    },
  },
}
```

- 必要 env: `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION`
- IAM: `bedrock:InvokeModel` + `InvokeModelWithResponseStream` + `ListFoundationModels` + `ListInferenceProfiles`
- 推論プロファイル prefix（`us.` / `eu.` / `ap.`）対応
- ⚠️ Opus 4.7 は `temperature` パラメータ拒否

### 2.4 Slack チャネル設定（公式）
出典: https://raw.githubusercontent.com/openclaw/openclaw/main/docs/channels/slack.md

```json5
{
  channels: {
    slack: {
      enabled: true,
      mode: "socket",  // 私たちが既に使ってる方式と一致
      appToken: { source: "env", id: "SLACK_APP_TOKEN" },
      botToken: { source: "env", id: "SLACK_BOT_TOKEN" },
    },
  },
}
```

必要スコープ：**TeamAgent で取得済みの 17 個と完全一致**。

### 2.5 SKILL.md 形式（公式）
- YAML frontmatter + Markdown 本文
- 本文は LLM への自然言語指示
- **同ディレクトリの `scripts/` から Python script を呼べる**
- 実例: 公式 `skill-creator` Skill が Python スクリプトを使用
- HTTP リクエストも可（Skill 本文に `curl` 指示を書く）

### 2.6 インストール方法（公式）
出典: https://docs.openclaw.ai/install

```bash
# 個人 / 開発（macOS, Linux, WSL2）
npm install -g openclaw@latest
openclaw onboard --install-daemon
openclaw gateway --port 18789 --verbose

# 本番（Linux サーバ）
docker compose up -d openclaw-gateway
# image: ghcr.io/openclaw/openclaw:latest
```

- 必須: Node 24 推奨 / Node 22.19+ LTS
- デフォルトポート: 18789
- 設定: `~/.openclaw/openclaw.json`（JSON5）

---

## 3. 統合方針 4 案の比較

OpenClaw を採用するか否か、採用するならどう統合するかの 4 案：

| 案 | 説明 | 工数 | 既存コード流用 | デプロイ複雑度 | 推奨度 |
|---|---|---|---|---|---|
| **A. 完全 TS 移行** | Python Skill を TypeScript で書き直し | 6 Sprint | 0% | 低（1 コンテナ） | ❌ |
| **B. HTTP 橋渡し** | Python Skill を FastAPI ラップ、OpenClaw が HTTP で呼ぶ | 1.5 Sprint | **85%** | 中（2 コンテナ） | ⭐⭐⭐ |
| **C. subprocess 橋渡し** | OpenClaw Skill が Python CLI を毎回起動 | 1 Sprint | 80% | 低 | ❌（embedder ロード毎回 3-5 秒の致命傷） |
| **D. OpenClaw 不採用** | 現在の boto3 + slack-bolt 構成を継続 | 0 Sprint | 100% | 低（1 コンテナ） | ⭐⭐⭐ |

### B 案（HTTP 橋渡し）の構成図

```
Slack ─ socketmode ─▶ OpenClaw Gateway (TS, Node) ─▶ HTTP ─▶ teamagent-skills (FastAPI, Python)
                          │                                     │
                          └─ Bedrock (TS SDK)                    ├─ Bedrock (boto3)  ← 現行
                                                                 ├─ pgvector
                                                                 └─ LocalE5Embedder
```

Bedrock 呼び出しは Python 側に一本化（既存 `adapters/bedrock_client.py` を使う、TS 再実装しない）。

### D 案（不採用）の根拠
- Day 2 で既に End-to-End 疎通成功（mention → 検索 → 引用付き回答、$0.01-0.02/クエリ）
- pytest 24 件 + mypy --strict 通過済み
- OpenClaw 採用の主要メリット（23 チャネル対応・ClawHub Skill エコシステム）が **TeamAgent の MVP に必須ではない**
- ClawHavoc サプライチェーンリスクを 100% 回避できる

### 最終判断は Sprint 2 末ゲート①（2026-06-07）
判断材料は：
- 子会社運用ヒアリング結果（質問リスト送付済み）
- B 案 PoC（OpenClaw + FastAPI 往復テスト）
- ClawHub セキュリティ運用ルールの社内ポリシー整合

---

## 4. v0.2 時点の TeamAgent 採用方針

| 項目 | 方針 |
|---|---|
| Bedrock 経由で Claude を呼ぶ | ✅ 継続（5/13 Agent SDK Credits は使わない） |
| Slack 連携 | ✅ Socket Mode + 17 スコープで継続 |
| OpenClaw 採否 | 🟡 Sprint 2 末ゲート①で確定（B 案 or D 案） |
| 既存 src/teamagent/ | ✅ そのまま継続（採否に関わらず 85〜100% 流用） |

**現在の実装はどちらのシナリオでも無駄にならない。**

