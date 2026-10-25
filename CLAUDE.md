# TeamAgent / Aico — Claude Code 運用ルール

守るべき規則だけを置く。**コードの仕組み（構成・各機能・データ・運用・テスト）は [`openwiki/quickstart.md`](openwiki/quickstart.md)**、多段の手順は `.claude/skills/` と `docs/`。
簡素化前の原本は [`docs/handoff/CLAUDE_md_before_simplify_2026-10-19.md`](docs/handoff/CLAUDE_md_before_simplify_2026-10-19.md)。

## 0. 概要

- 社内営業 **16 名**向け Slack AI Agent。本番 Bot 名は **Aico**（旧称 NewsTV AI / AiLa）。多人数ツールなので §5 を厳守。
- AWS アカウント `718959508629` / リージョン `ap-northeast-1`。構成は [`openwiki/architecture/overview.md`](openwiki/architecture/overview.md)。

## 1. 作業の前に

- 作業は **`origin/dev` から切った worktree（`~/dev/worktrees/` 配下）**で行い、PR は **`dev` 宛て**（`main` は大きく遅れている）。`~/dev/teamagent` と `~/Documents/teamagent-orchestrator-poc` は checkout が古い既存クローン。作業前に `git rev-parse --show-toplevel && git branch --show-current` で現在地を確認。
- 古い feature 枝の上に積まない。「何が本番か」は `infra/deploy_log.md` と本番 ECS task の env（`aws ecs describe-task-definition --task-definition teamagent-dev-mcp`）で確認してから判断する。
- Bedrock のモデル ID は東京の推論プロファイル **`jp.anthropic.*`**。`BEDROCK_MODEL_ID` 未設定時のコード既定は **Haiku 4.5**（暗黙の Sonnet 課金防止）。高品質が要る呼び出しは env で明示。新しい ID は `aws bedrock list-inference-profiles --region ap-northeast-1` で実在確認してから使う。

## 2. 変えない技術的判断

1. Claude は **AWS Bedrock 経由**で呼ぶ（Anthropic API 直叩き禁止）。
2. **pgvector 0.8.0 以上**（古いとフィルタで結果ゼロ）。本番は 0.8.2。
3. **temperature=0.1 + 引用必須**（ハルシネーション抑制）。
4. **prompt caching を使う**（system prompt + 頻出 context）。低トラフィックでは `cache_read=0` になりうる。
5. データ層は **pgvector / RLS**（§5）。検索は SearchSkill を再利用し、embed/SQL を新規発明しない。

§2-3/4 と現行オーケストレータの差は wiki（[`openwiki/architecture/orchestrator.md`](openwiki/architecture/orchestrator.md)）に記録。新規・改修時はこの方針に合わせる。

## 3. AI エージェント実装ルール

**3層分離と import 方向は CI（import-linter）で強制**。詳細は [`openwiki/architecture/layering-and-skill-contract.md`](openwiki/architecture/layering-and-skill-contract.md)。

### Do
1. **3層分離** `skills/`（ビジネスロジック）/ `adapters/`（Bedrock・pgvector・Slack・Google）/ `runtime/`（ECS・local エントリポイント）。依存は **runtime → skills → adapters の一方向**。
2. **Pydantic v2 で I/O 固定**。Skill の input/output は `BaseModel`。dict をそのまま返さない。
3. **型ヒント + `mypy --strict`**。
4. **構造化ログ（JSON）+ request_id 伝播**。`{"request_id","skill","event","token_usage","latency_ms","cost_usd"}` を全層で同じ request_id で出す。
5. **prompt はファイル化 + Git 管理**（`src/teamagent/prompts/<skill>/<version>/<name>.md`）。コード内リテラル禁止。
6. **Bedrock 呼び出しごとに usage/cost をログ**（`input/output/cache_read/cache_creation tokens`・`model_id`・`latency_ms`・`cost_usd`）。
7. **テスト**は Skill 単位で pytest（adapter は fake/moto）。最低 happy path + 1 エッジ。
8. **Slack 投稿 / DB 書き込みは Idempotency-Key 付き**（リトライで二重処理しない）。

### Don't
- ❌ **エラーログに生入力（提案 PDF 全文・顧客名・会話履歴・メール本文・email）**。`request_id` のみログし本体は KMS 暗号化 S3 へ。`stderr` の print は `_mask_email` を通す。
- ❌ **「AI の confidence」をログ**。Claude に confidence はない。`retrieval_similarity / token_usage / latency` を出す。
- ❌ **Skill 内で boto3 / 各種 SDK を直叩き**。必ず `adapters/` 経由。
- ❌ **個人 email / 個人前提のハードコード**（§5 C1）。

## 4. デプロイ / 運用の地雷 ★踏むと本番事故

> デプロイ経路は [`openwiki/operations/release-gates-and-deploy.md`](openwiki/operations/release-gates-and-deploy.md) と `infra/terraform/README.md`。AWS 書込み（apply/build/push）は人間が実行。

- **B1 tfvars の ON スイッチ必須**：`enable_morning_digest` / `enable_ingest_schedule` / `enable_connect_web` を `terraform.tfvars` に明記しないと、plan が稼働中の EventBridge Rule・ECS TaskDef・IAM を**削除判定**する。apply 前に plan の destroy 行を確認。
- **B2 Fargate の command は `["python", ...]`**（`uv run` 禁止）。非 root の `uv run` が `/app/.venv` を再生成しようとして Permission denied → exit 2 で即死。
- **B3 ECR は immutable tag**：同一 tag の再 push 不可。ビルド毎に新 tag を採番し `--build-arg GIT_COMMIT=$(git rev-parse HEAD)` / `GIT_BRANCH` を image に刻む。デプロイ毎に [`infra/deploy_log.md`](infra/deploy_log.md) へ digest↔commit↔branch↔概要を1行追記。
- **B4 taskdef の env は terraform apply しないと live に効かない**（taskdef revision 登録 → EventBridge target の ARN 更新が必要）。
- **B5 Slack secret の命名**：`teamagent/dev/openclaw/slack-bot-token` / `teamagent/dev/openclaw/slack-app-token`。**`slack/*` 名だと OpenClaw が token を解決できず bot 起動失敗**。`deploy_to_ec2.sh` 系は旧 `slack/*` に戻しうるので流さない。
- **B6 `dmPolicy:"open"` でも `allowFrom:["*"]` が無いと全 DM を reject**。CI の `scripts/check_openclaw_config.py` が検出（§6 D4）。
- **B7 morning_digest は `CONNECT_GOOGLE_CLIENT_ID` と `CONNECT_GOOGLE_CLIENT_SECRET`（web 型）の両方必須**。欠けると `build_user_credentials()` が ValueError → mail=0 / calendar=0 で全件ゼロ。
- **B8 runtime を分離**：EventBridge 専用 morning-digest task は `DRAFT_ON_DEMAND_ONLY="false"`（高重要×To本人のみ・最大 `MORNING_DIGEST_MAX_DRAFTS` 件を作り置き）。Slack mention から呼ぶ通常 MCP task は `DRAFT_ON_DEMAND_ONLY="true"` 必須（自動 draft 禁止・ボタンのオンデマンド生成のみ）。`MORNING_DIGEST_MAX_DRAFTS` を上げるなら Budgets 閾値と突き合わせる。
- **B9 `reingest.sh` / direct task-definition 登録は禁止**。MCP image は guarded build → receipt → saved plan で Terraform-owned ingest family に反映してから `scripts/aws/run_ingest_task.sh`。receipt/plan の再利用不可。手順は `infra/terraform/README.md`。
- **B10 Slack は 1 App で Socket Mode + HTTP interactivity を併用できない**。

## 5. 多人数・セキュリティ原則 ★16名で使う前提

- **C1 個人 email / 個人前提のハードコード禁止**。「自分宛」は**処理中のユーザー本人の email が To にあるか**を動的判定（`skills/morning_digest/skill.py` の `_is_addressed_to`、`requester = ctx.metadata["user_email"]`）。`MORNING_DIGEST_USERS` は処理対象を絞るテスト用で、宛先固定ではない。
- **C2 RLS は `app_role="teamagent_app"` 必須**。master user（table owner）は FORCE RLS でも実質 bypass。`PgVectorClient.connection(app_role="teamagent_app", user_email, user_groups, user_role)` で session 注入。`user_email=None` は会社共有モデルで意図的にのみ。
- **C3 Fargate からの Drive アクセス（knowledge_deliver / ingest）は `GOOGLE_FORCE_OAUTH=1`**（Vertex SA は共有ドライブを開けない）。morning_digest は per-user token refresh の別経路なので使わない。
- **C4 Slack identity は fail-closed**。`adapters/slack_client.py` の `resolve_identity` が社外/ゲスト/bot を拒否。dmPolicy:open でもここが最終ガード。
- **C5 `SLACK_TEAM_ID` を本番 mcp env に必ず設定**（tfvars `slack_team_id = "T…"`）。`resolve_identity` は未設定・形式不正でも **fail-closed で全員拒否**（`missing_expected_team`）＝設定漏れで bot が全員に無応答になる。
- **既知 caveat**：`connect_web/app.py` の `_DEFAULT_SEARCH_EMAILS` は `CONNECT_SEARCH_ALLOWED_EMAILS` 未設定時に個人固定へフォールバックする。本番は必ず `CONNECT_SEARCH_ALLOWED_EMAILS` を明示。

## 6. CI で落ちる罠（`.github/workflows/ci.yml`）

- **D1 依存は手動列挙**：CI は `pip install --no-deps -e .` + ci.yml の依存リスト。**新しい import を足したら ci.yml にも追記**（ローカルは通って CI だけ落ちる）。
- **D2 `ruff format --check` 必須**（auto-fix 無し）。push 前に `ruff format`。
- **D3 import-linter**：adapters → 上位層、skills → runtime の import は CI fail。
- **D4 `scripts/check_openclaw_config.py`** が OpenClaw config の不変条件（B6）をゲート。
- **D5 trivy**：`trivy fs`（uv.lock の CVE）＋ `trivy config`（infra/ の IaC）が CRITICAL/HIGH で fail。依存 CVE は ignore に足さず**依存バンプで直す**（無関係 PR が突然赤くなったらバンプ PR を先行 merge → rebase）。misconfig の例外は `.trivyignore.yaml` に **paths スコープ＋statement＋expired_at** 付きでのみ（ID グローバル ignore 禁止）。
- **branch protection は未設定＝赤でも merge ボタンは押せる**。「赤なら merge しない」は運用規律。

## 7. Aico にツール/機能を足すとき（旧 §10）

- ツールは **4段（skill 実装 → factory の `USE_<X>` gate・既定 OFF → `openclaw.config.json5` の `toolFilter.include` → 本番 env ON + apply + run-task）**が全部通って初めて稼働。「実装済」と「稼働」は別。どの段で止まっているかを常に言う。
- description は OC がツールを選ぶ唯一の材料。被るツール（動画/TikTok 系）はトリガー語で棲み分け、「対象外（→他tool）」の相互排他注記を書く。
- 手順・チェックリスト・ルーティング検証は [`.claude/skills/aico-add-tool/SKILL.md`](.claude/skills/aico-add-tool/SKILL.md)。仕組みは [`openwiki/architecture/tool-registry-and-feature-flags.md`](openwiki/architecture/tool-registry-and-feature-flags.md)。

## 8. ローカル開発

```bash
cd ~/dev/worktrees/<作業名>                 # §1
cd infra/docker && docker compose up -d     # pgvector + adminer + minio
uv sync --extra dev --extra mcp             # CI と同じ extras。media 系は --extra media も
npm ci --prefix tools/tiktok_scraper        # media worker の契約テストが node で走る
pytest -q && mypy --strict src && ruff check . && ruff format --check src/ tests/ scripts/
```

- 詳細と CI で skip されるテストは [`openwiki/testing/running-tests.md`](openwiki/testing/running-tests.md)。
- 本番 RDS は SSM port-forward + `.env.local` 経由（`localhost:15432`、手順は `docs/v3.2/ops/local_dev_with_tunnel.md`）。RLS を効かせるには `app_role="teamagent_app"`（C2）。踏み台直 `psql` は hang するので psycopg / port-forward を使う。

## 9. リンク集（旧 §7・§9）

| 目的 | ドキュメント |
|---|---|
| コードの仕組み全般 | [`openwiki/quickstart.md`](openwiki/quickstart.md) |
| 一括デプロイ（dev→live, MCP 再ビルド） | `docs/v3.2/bundled_deploy_2026-06-16.md` |
| 観測/セキュリティ基盤 apply | `docs/v3.2/ops/observability_and_security.md` |
| Secrets ローテーション | `docs/v3.2/ops/secrets_rotation_policy.md` |
| CloudWatch Logs Insights クエリ集 | `docs/v3.2/ops/cloudwatch_queries.md` |
| SLO 閾値 | `docs/v3.2/slo_v1.md` |
| デプロイ履歴（digest↔commit↔branch） | `infra/deploy_log.md` |
| 構築当時の経緯 | `docs/handoff/teamagent_handoff_day0-4_2026-05.md` |

EC2 worker は撤去済（`docs/v3.2/ec2_cutover_runbook.md` は歴史文書・実行しない）。

<!-- OPENWIKI:START -->

## OpenWiki

@AGENTS.md

<!-- OPENWIKI:END -->
