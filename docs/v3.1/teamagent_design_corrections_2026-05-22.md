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
