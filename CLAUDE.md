# TeamAgent / AiLa — Claude Code 運用マニュアル

このファイルは Claude Code 起動時に自動で読み込まれます。
**現状アーキ・変えないルール・運用地雷・多人数原則・CIゲート・runbookリンク**を集約した運用マニュアルです。

> 📦 2026-05 の構築日記（Day 0〜4 の時系列・Sprint 1 タスク等）は
> [`docs/handoff/teamagent_handoff_day0-4_2026-05.md`](docs/handoff/teamagent_handoff_day0-4_2026-05.md) に退避済（履歴は失っていません）。
> 経緯を追うときだけそちらを参照。

最終更新：2026-06-25（全面再構成）

---

## 0. プロジェクト概要

- **TeamAgent v3.1** — 社内営業 **16 名**向け Slack ベース AI Agent プラットフォーム。本番 Bot 名は **AiLa**。
- **多人数ツール**（→ §5 の原則を厳守。特定個人の前提・ハードコード禁止）。
- スケジュール：14 Sprint × 2 週（2026/5〜12）。**Sprint 14（2026-12-28）本番運用ターゲット**。Go/No-Go ゲート ①(Sprint 2末) / ②(Sprint 10末)。
- コスト枠：Dev ¥80K 一時 / Ops ¥100K〜¥1M/月（規模次第）。
- AWS アカウント `718959508629` / リージョン `ap-northeast-1`（東京）。

---

## 1. 現状アーキ（実態 — ここが最重要・旧 EC2 中心の記述は廃止）

```
Slack ─▶ OpenClaw (TypeScript/Node, ECS Fargate)         ← Slack 受け口 + MCP 外殻 + @AiLa
            └─▶ MCP gateway (Python, ECS Fargate)        ← Skill 実体・Bedrock/pgvector
EventBridge(cron) ─▶ ECS Fargate scheduled tasks ×4:
            morning_digest / ingest / connect-web / canary
EC2 踏み台 (SSM のみ・22閉鎖)                              ← DB 接続/運用専用。アプリは載らない
RDS PostgreSQL 16 + pgvector 0.8.2 (東京)                 ← データ層 (RLS あり)
AWS Bedrock (東京)                                        ← Claude Sonnet/Haiku
```

- **リポジトリは `~/Documents/teamagent-orchestrator-poc`**。
  - ⚠️ `~/Documents/TeamAgent` は**別の旧リポ**。worktree が複数あり「気づいたら別ブランチ/別ディレクトリ」になる事故が頻発 → 作業前に必ず `git -C <repo> rev-parse --show-toplevel && git branch --show-current` で現在地確認。
- **オーケストレータ/実行基盤は確定済**：OpenClaw を Slack 受け口＋MCP 外殻として本番採用（2026-06-12 go-live）、skill オーケストレーションは **Claude Agent SDK on Bedrock**。OpenClaw は TypeScript/Node、Skill 実体は Python（MCP gateway）。
- **Bedrock モデル ID は東京の推論プロファイル `jp.anthropic.*`**。env `BEDROCK_MODEL_ID`（既定 `jp.anthropic.claude-sonnet-4-6`）。`adapters/bedrock_client.py` の PRICE_TABLE は `jp.` / `us.` 両対応（region-aware）。
  - ⚠️ tf の既定が `variables.tf`(sonnet, サフィックス無し) と `variables_fargate.tf`(haiku, `-20251001-v1:0` 付き) で**書式不一致**。新規にモデルを指す時は `aws bedrock list-inference-profiles --region ap-northeast-1` で実在 ID を確認してから設定。
- 3層分離 `src/teamagent/{adapters,skills,runtime}` は §3 のルールに従う（CI で強制）。

---

## 2. 変えない技術的判断

1. **AWS Bedrock 経由で Claude を呼ぶ**（Anthropic API 直叩き禁止）。理由：2026/4 のサブスク制限事件以降、政策変動を Bedrock で遮断。実装は region-aware（§1 の `jp./us.`）。
2. **pgvector 0.8.0 以上**を必ず使う（古いとフィルタで結果ゼロのバグ）。本番は 0.8.2。
3. **temperature=0.1 + 引用必須化**（ハルシネーション抑制の鍵）。
4. **prompt caching を必ず使う**（system prompt + 頻出 context で大幅コスト削減）。ただし**低トラフィックでは cache がヒットせず `cache_read=0` になる**ことがある（観測時の留意）。
5. **データ層は pgvector / RLS**（§5）。検索は SearchSkill を再利用（embed/SQL を新規発明しない）。

---

## 3. AI エージェント実装ルール（メンテ性最優先）

新しい Skill / タスク / バッチを書くときは以下を必ず守る。**3層分離・import 方向は CI(import-linter) で強制**。

### Do
1. **3層分離**：`skills/`（ビジネスロジック）/ `adapters/`（Bedrock・pgvector・Slack・Google クライアント）/ `runtime/`（ECS・local エントリポイント）。依存方向は **runtime → skills → adapters の一方向**（adapters から runtime/skills を import 禁止＝`pyproject.toml [tool.importlinter]` で CI fail）。
2. **Pydantic v2 で I/O 固定**：Skill の input/output は必ず `pydantic.BaseModel`。dict をそのまま返さない。
3. **型ヒント + `mypy --strict`**：CI で型エラーは fail。
4. **構造化ログ（JSON）+ request_id 伝播**：`{"request_id","skill","event","token_usage","latency_ms","cost_usd"}` を全層で同じ request_id で出す（CloudWatch Insights が JSON 前提＝§7）。
5. **prompt はファイル化 + Git 管理**：`src/prompts/<skill>/v1/system.md`。コード内文字列リテラル禁止。
6. **Bedrock 呼び出しごとに usage/cost を必ずログ**：`input_tokens / output_tokens / cache_read_input_tokens / cache_creation_input_tokens / model_id / latency_ms / cost_usd`。
7. **テスト**：Skill 単位で pytest（adapter は fake/moto でモック）。最低 happy path + 1 エッジ。
8. **Slack 投稿 / DB 書き込みは Idempotency-Key 付き**（リトライで二重処理しない）。

### Don't
- ❌ **エラーログに生入力（提案 PDF 全文・顧客名・会話履歴・メール本文・email）を入れる** — PII/機密漏洩。`request_id` のみログし本体は KMS 暗号化 S3 へ。`stderr` の print も email 素出しに注意（`_mask_email` を通す）。
- ❌ **「AI の推論スコア/ confidence」を曖昧にログ** — Claude に confidence はない。`retrieval_similarity / token_usage / latency` を出す。
- ❌ **Skill ファイル内で boto3 / 各種 SDK を直叩き** — 必ず `adapters/` 経由（テストでモック差し替え可能に）。
- ❌ **prompt をコードに hard-code** — `src/prompts/` の `.md` を読む形に統一。
- ❌ **個人 email / 個人前提のハードコード**（§5）。

### コードレビュー定型チェック
- [ ] Pydantic で入出力定義 / [ ] `mypy --strict` 通過 / [ ] 構造化ログに request_id・token_usage・cost_usd / [ ] 機密が CloudWatch に出ていないか grep / [ ] prompt が `src/prompts/` 配下 / [ ] pytest happy path / [ ] import 方向（import-linter）OK

---

## 4. デプロイ / 運用の地雷 ★踏むと本番事故（メモリ非依存で明文化）

> デプロイは **CodeBuild → ECR → tfvars 反映 → terraform apply → run-task 検証** が基本フロー。turnkey 手順は §7 の `bundled_deploy_*` / `deploy_log.md`。

- **B1 tfvars の ON スイッチ必須**：`terraform.tfvars` に `enable_morning_digest = true` / `enable_ingest_schedule = true` / `enable_connect_web = true` を**明記**しないと、plan が稼働中の EventBridge Rule・ECS TaskDef・IAM を**「削除」判定**する（過去の CLI `-var` 渡しの名残）。apply 前に必ず plan の destroy 行を確認。
- **B2 Fargate の command は `["python", ...]`**（`uv run` 禁止）。root が build した `/app/.venv` を非 root が `uv run` で再生成 → **symlink Permission denied → exit 2 で即死**。
- **B3 ECR は immutable tag**：同一 tag の再 push 不可。ビルド毎に**新 tag を採番**し、`--build-arg GIT_COMMIT=$(git rev-parse HEAD)` / `GIT_BRANCH` を渡して image に刻む。デプロイ毎に [`infra/deploy_log.md`](infra/deploy_log.md) へ digest↔commit↔branch↔概要を1行追記。
- **B4 terraform apply しないと taskdef の env が live に効かない**：scheduled task の env 変更は ECS で taskdef revision を register → EventBridge target の task-definition ARN を更新、が必要。「apply したら env が勝手に sync」は**誤り**。
- **B5 ⚡Slack secret の命名規約**：`teamagent/dev/openclaw/slack-bot-token` / `teamagent/dev/openclaw/slack-app-token`（`variables_fargate.tf` の `slack_bot/app_token_secret_name`）。**`slack/*` 名で保存すると OpenClaw が token 解決に失敗して bot 起動失敗**（2026-06-25 に誤 `.env.production` でこの事故が発生）。`deploy_to_ec2.sh` 系を流すと旧 `slack/*` に戻りうるので注意。
- **B6 `dmPolicy:"open"` でも `allowFrom:["*"]` が無いと全 DM を reject**（管理者限定 allowlist 扱い）。2026-06-22 の無音 DM drop 事故の真因。CI `scripts/check_openclaw_config.py` が不変条件として検出（§6）。
- **B7 morning_digest は Google OAuth env 両方が必須**：per-user token refresh に **`CONNECT_GOOGLE_CLIENT_ID` と `CONNECT_GOOGLE_CLIENT_SECRET`（web 型）両方**を env 注入。欠けると `build_user_credentials()` が ValueError → **mail=0 / calendar=0 / errors=2 で全件ゼロ**（2026-06-25 回帰）。
- **B8 `DRAFT_ON_DEMAND_ONLY="false"`（朝の作り置き）が現行の正**（2026-07-10 裁定・#151 で方針変更を反映）：朝ダイジェスト時に**高重要×To本人のみ・最大 `MORNING_DIGEST_MAX_DRAFTS`（tf 実値 5・コード上限 10）件**を自動で作り置きし、未作成分だけボタンでオンデマンド生成する。件数キャップがあるため旧記述の「全ユーザー分生成で爆発」は起きない。地雷は2つ: (a) **`MORNING_DIGEST_MAX_DRAFTS` を安易に上げるとユーザー数×件数で Bedrock コストが線形増**（上げるときは #154 の Budgets 閾値と突き合わせる）。(b) `true` に戻すと作り置きが止まりボタンオンデマンドのみになる（旧方針・UI は壊れないが朝の下書きが消える挙動変化）。※本項の旧記述（true 必須）は 3505c06 時点の方針で、#151 以降の実態と逆だった。
- **B9 `reingest.sh` の `NEW_IMAGE` digest は手動更新**：新 MCP image を push したら実行前に最新 digest へ。古いと image not found。
- **B10 Slack は 1 App で Socket Mode + HTTP interactivity を併用できない**（アーキ制約）。

---

## 5. 多人数・セキュリティ原則 ★16名で使う前提

- **C1 個人 email/個人前提のハードコード禁止**。「自分宛」= **処理中のそのユーザー本人の email が To にあるか**を**動的判定**（`skills/morning_digest/skill.py` の `_is_addressed_to(headers, requester)`、`requester = ctx.metadata["user_email"]` をユーザーごとに切替）。
  - 起動引数 `MORNING_DIGEST_USERS=<email,...>` は「**処理対象ユーザーを絞る**」テスト用であって**宛先固定ではない**。
- **C2 RLS は `app_role="teamagent_app"` 必須**：master user（table owner）で接続すると FORCE RLS でも実質 bypass する。アプリは `PgVectorClient.connection(app_role="teamagent_app", user_email, user_groups, user_role)` で session 注入。`user_email=None` は会社共有モデルでは許容だが意図的にのみ。
- **C3 Fargate からの Drive ファイルアクセス（knowledge_deliver / ingest）は `GOOGLE_FORCE_OAUTH=1`**：Vertex SA だと共有ドライブを開けず失敗 → 個人 OAuth に強制。（morning_digest は別経路＝per-user token refresh なので GOOGLE_FORCE_OAUTH は使わない）。
- **C4 Slack identity は fail-closed**：`adapters/slack_client.py` の identity 解決が社外/ゲスト/bot を拒否（データ非到達で安全）。dmPolicy:open でも resolve_identity が最終ガード。
- **既知 caveat（未修正・このまま運用）**：`connect_web/app.py` の `_DEFAULT_SEARCH_EMAILS = "s-komata@..."` は env `CONNECT_SEARCH_ALLOWED_EMAILS` 未設定時に**個人固定にフォールバック**する。多人数で使うなら**本番は必ず `CONNECT_SEARCH_ALLOWED_EMAILS` を明示**すること。
- **未検証メモ（要確認・着手前に裏取り）**：① `resolve_identity` が `SLACK_TEAM_ID` 未設定時に team 検証を skip（fail-open）の疑い。② `connect_google_client_id` 変数が `variables*.tf` に未定義で `enable_connect_web=false` 時に terraform error の疑い。

---

## 6. CI で落ちる罠（`.github/workflows/ci.yml`）

- **D1 依存は手動列挙**：CI は `pip install --no-deps -e .` + ci.yml にハードコードした依存リスト。**新しい import を足したら ci.yml のリストにも追記**しないと、ローカルは通って CI だけ落ちる（依存リーク）。
- **D2 `ruff format --check` 必須**（auto-fix step は無い）。push 前にローカルで `ruff format` を実行。
- **D3 import-linter で 3層分離を強制**（§3）。adapters→runtime/skills は CI fail。
- **D4 `scripts/check_openclaw_config.py`** が OpenClaw config 不変条件（B6 の `dmPolicy:open ⇒ allowFrom:["*"]` 等）を CI ゲート。
- **D5 trivy ゲート（2026-07-10 追加）**：`trivy fs`（uv.lock の依存 CVE）＋ `trivy config`（infra/ の IaC misconfig）が CRITICAL/HIGH で fail。依存 CVE は ignore に足さず**依存バンプで直す**（`--ignore-unfixed` 運用＝修正版が出た時点で赤くなる。無関係 PR が突然赤くなったらバンプ PR を先行 merge → rebase）。misconfig の例外は `.trivyignore.yaml` に **paths スコープ＋statement＋expired_at** 付きでのみ追加可（ID グローバル ignore は将来の新規検出まで素通りさせるため禁止・expired_at 超過で自動的に再検出＝赤）。image スキャンは CI 対象外（E5 2.2GB 焼き込みで PR 毎 build 不可）＝ECR scan_on_push＋別チケット。なお branch protection はプラン制約で未設定＝**赤でも merge ボタンは押せる**ので「赤なら merge しない」は運用規律（D1-D4 も同条件）。
- **D6 CI トリガーは `branches: [main, dev]`**（2026-07-10 修正）：かつて `[main]` のみで**dev ベース PR では CI が一切発火していなかった**（branches フィルタは PR の base branch 基準）。dev に赤が蓄積する構造だったため、dev PR も全ゲートを通る。

---

## 7. runbook / docs リンク集（いずれも実在・現役）

| 目的 | ドキュメント |
|---|---|
| 一括デプロイ（dev→live, MCP 再ビルド, JSON ログ） | `docs/v3.2/bundled_deploy_2026-06-16.md` |
| EC2 へ Bot を移す（Socket Mode 二重起動回避手順） | `docs/v3.2/ec2_cutover_runbook.md` |
| 観測/セキュリティ基盤 apply（SNS/CloudWatch/KMS/CloudTrail/Sentry） | `docs/v3.2/ops/observability_and_security.md` |
| Secrets ローテーション（9 secrets・周期） | `docs/v3.2/ops/secrets_rotation_policy.md` |
| CloudWatch Logs Insights クエリ集（8本・JSON ログ前提） | `docs/v3.2/ops/cloudwatch_queries.md` |
| 本番 RDS にローカルから繋ぐ（SSM tunnel + .env.local + RLS テスト） | `docs/v3.2/ops/local_dev_with_tunnel.md` |
| SLO 閾値（latency p95 / 可用性 / エラーバジェット） | `docs/v3.2/slo_v1.md` |
| デプロイ履歴（image digest↔commit↔branch） | `infra/deploy_log.md` |
| 検索 Skill 設計 | `docs/v3.1/teamagent_search_skill_design_v1.md` |
| Sprint 14 までの全タスク | `docs/v3.2/teamagent_master_todo_v1.md` |
| オーケストレータ PoC 判断根拠 | `docs/poc/agent_orchestrator_poc_findings.md` |

---

## 8. ローカル開発

```bash
# リポジトリ（現行）
cd ~/Documents/teamagent-orchestrator-poc

# ローカル pgvector + adminer + minio
cd infra/docker && docker compose up -d && docker ps   # 3 コンテナ

# Python（uv 管理）
cd ~/Documents/teamagent-orchestrator-poc
# テスト/型/整形（push 前に全部緑にする）
pytest -q
mypy --strict src
ruff check . && ruff format --check src/ tests/ scripts/
```

- ローカル DB：`localhost:5432`（teamagent/teamagent）/ Adminer `http://localhost:8080` / MinIO `http://localhost:9001`。
- **本番 RDS 接続は SSM port-forward + `.env.local` 経由**（踏み台 EC2 → `localhost:15432`）。手順とトンネル詳細は `docs/v3.2/ops/local_dev_with_tunnel.md`。**RLS を効かせるには `app_role="teamagent_app"`**（§5-C2）。踏み台直 `psql` は hang するので psycopg / port-forward を使う。

---

## 9. 困ったとき早見表

| やりたいこと | 見る場所 |
|---|---|
| いま何が本番で動いてるか | §1 アーキ + `infra/deploy_log.md` |
| デプロイの地雷を踏みたくない | §4 |
| 多人数で安全に作る | §5 |
| CI が赤い | §6 |
| 構築当時の経緯を追う | `docs/handoff/teamagent_handoff_day0-4_2026-05.md` |
| 検索 Skill を実装 | `docs/v3.1/teamagent_search_skill_design_v1.md` |
| AWS リソース ID / モデル ID | §0 + `aws bedrock list-inference-profiles` |
| AiLa にツール/機能を足したい | §10（4段ゲート＋description＋チェックリスト） |

---

## 10. AiLa にツール/機能を足すとき ★忘れてはいけない手順（2026-06-25 追記）

新しい能力は「skill を書いたら使える」ではない。**OC(AiLa) に届くには段が4つ**あり、どこかで止まると「実装したのに本番で呼べない」になる（実際 ③`video_approval` は skill 完成済みなのに OC 未露出で長く埋もれていた）。

### E1 ツールが本番で AiLa に届くまでの4段（全部通って初めて稼働）
1. **skill 実装**：`skills/<name>/{schema,skill}.py` ＋ `@register`（§3）。←ここまでで「実装済」。
2. **factory 登録（env-gate）**：`orchestrator/factory.py` の `build_production_tools()` に `if _envflag("USE_<X>"): specs.append(ToolSpec(...))`。新規は**必ず既定 OFF**（後方互換）。
3. **OC 露出（第2ゲート）**：`infra/openclaw/openclaw.config.json5` の `mcp.servers.teamagent.toolFilter.include` に **tool 名を追加**。← factory に在っても include に無ければ **OpenClaw からは見えない**（一番の見落としポイント）。
4. **本番 ON（デプロイ＝人間ゲート）**：mcp の ECS task env に `USE_<X>=1`（tfvars/CodeBuild）→ apply → run-task 検証（§4）。AWS 書込み（apply/build/push）は **classifier がブロック＝人間が実行**。

> **「実装済」と「稼働」は別。** どの段で止まっているかを常に言う。`factory` だけ＝dark（コードは在るが見えない）／`include` だけ＝MCP 側の `USE_<X>` が無いと呼んでも失敗。

### E2 description は「AI の振り分け判断材料」★曖昧だと誤選択で事故る
- OC は **name + description だけ**でどの tool を呼ぶか決める。被る tool（特に **動画/TikTok 系**: tiktok_search / tiktok_acquire / video_algorithm / video_analysis / video_approval / proposal_campaign）は、**トリガー語で棲み分け**＋**「これは対象外（→他tool）」の相互排他注記**を description に書く。
  - 例：`video_approval`=「自社編集者の**納品**動画チェック（誤植/尺/NG）」 vs `video_analysis`=「**外部URLの競合**動画分析・納品物は対象外（→video_approval）」。
- **新規/変更時はルーティング・シミュで検証**：現実的な依頼を多数流し、正しい tool に行くか・混同ペアが無いかを出荷前に確認（実績：32発話で正答率97%、最大リスクは動画チェック↔競合分析の取り違え＝納品事故）。

### E3 ツール追加チェックリスト
- [ ] `@register` で skill 登録 / [ ] factory に `USE_<X>` gate（既定 OFF）/ [ ] openclaw.config.json5 `toolFilter.include` に追加 / [ ] description にトリガー語＋相互排他注記（被る tool があるなら）/ [ ] 単体テスト（登録＋description 固定）/ [ ] §4 デプロイ（env ON）は人間ゲート / [ ] 「今どの段か」を PR / `deploy_log.md` に明記

### E4 ブランチ衛生 ★「実装済」が散らばり本番との対応が不明になりがち
- 機能が**未マージ PR / 未 push ローカルコミット**に散在しやすい（本番イメージが dev より進む／dev にコードが無いのに本番 env に flag がある、等の不整合が起きる）。
- **新規作業は必ず `origin/dev` 基点で branch を切る**（古い feature 枝の上に積まない＝デプロイ系統からズレる）。「何が本番か」は §1＋`infra/deploy_log.md`＋**本番 ECS task の env を実機で**確認してから判断（`aws ecs describe-task-definition --task-definition teamagent-dev-mcp`）。

---

最終更新：2026-06-25（5月の日記を docs/handoff へ退避し全面再構成。§10「ツール追加の4段ゲート・description 棲み分け・ブランチ衛生」を追記）
