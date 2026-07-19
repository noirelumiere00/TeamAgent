# P0 敵対ハーネス Runbook（RLS越権0・なりすまし封鎖の実証）— §J / A

会社共有モデル(§G)の安全核を確認する手順。MCP bearer は OpenClaw workload の認証にしか
使わず、LLM tool argument の `_user_context.slack_user_id` は認可identityにしない。
OpenClaw内部pluginがSlack event由来のuser/team/channelを、nonce・issued-at・expiry・audience・
message/session・tool・全引数hashへ束縛したone-use HMAC claimとして渡す。MCPは専用DynamoDB
tableへの条件付き書込みでrolling taskを跨いだreplayを拒否し、claim検証後に
Slack `users.info` resolverで同一workspaceの非guest/非stranger memberを解決できた場合だけ
会社共有groupを付与する。欠落・未知・障害は全てfail closed。

> 静的には既に緑（再検証不要）: `tests/test_mcp_gateway_identity.py`（admin破棄・ダウングレード封鎖・
> OC申告破棄・外部/ゲスト拒否）／`tests/test_slack_client_identity.py`／`tests/test_rls_email_ci_migration.py`
> （0010 両側 lower()）／`tests/test_mcp_gateway_caller_claim.py`（Node signer→Python verifierの
> 交差言語E2E、caller mismatch/guest/stranger/resolver failure/replay/expired/wrong audience/
> tamper）／`tests/test_attack_mcp.py`（bearer-only negative harnessの純ロジック）。
> **本 runbook が足すのは「実 pgvector＋live MCP での実走証明」だけ**（SSMトンネル/承認後）。

## 前提（gated）
- `docs/openclaw/deploy_runbook.md` の「7. Post-apply functional gates」に従い、
  exact task revision上のMCP backendが起動済みでhealthz green。またはローカルPoCとして
  次の全値を別々に設定して起動する（claim secretはbearerと共用しない）。
  `TEAMAGENT_MCP_BEARER=... TEAMAGENT_CALLER_CLAIM_SECRET=<32-byte以上> TEAMAGENT_CALLER_CLAIM_REPLAY_TABLE=<専用table> SLACK_TEAM_ID=T... SLACK_BOT_TOKEN=xoxb-... TEAMAGENT_SHARED_COMPANY_DOMAINS=vectorinc.co.jp uv run python scripts/run_mcp_http_server.py`。
- 実RDSへのmigrationは`docs/v3.2/data_model_v1.md`の「migration の運用」と
  forward-only runner `scripts/migrate.py`に従う。`schema_migrations`で
  `0010_rls_email_case_insensitive.sql`と`0011_backfill_company_acl_groups.sql`の
  checksum付き適用を確認済みであること。`DATABASE_URL` は承認済みSSMトンネル経由。

## 手順1: fixture 投入（会社doc×2＋会社外doc×1）
```sh
DATABASE_URL=... TEAMAGENT_SHARED_COMPANY_DOMAINS=vectorinc.co.jp \
  uv run python scripts/ingest_test_data.py --commit
```
- `company1/company2`（`acl_groups=[会社ドメイン]`）＝会社メンバーに可視。
- `outsider`（`outsider@evil.com`・`acl_groups=[]`）＝**会社メンバーに不可視であるべき**。本文に
  `OUTSIDER_ONLY_TOKEN` を含む。全 doc に共通語 `P0HARNESS`（search 候補に載せRLSで会社外が落ちるのを観る）。

## 手順2: smoke（protocol/認証/露出のみ）
```sh
TEAMAGENT_MCP_BEARER=... TEAMAGENT_SHARED_COMPANY_DOMAINS=vectorinc.co.jp \
  uv run python scripts/smoke_mcp.py --base-url http://127.0.0.1:8787
```
期待: healthz=200 / bearer無=401 / `tools/list` 200。direct MCPの`search`は署名claimを
生成できないため、ここでは成功条件にしない。`tools/list` は
`infra/openclaw/effective-tool-scope.json` のうち現在の Terraform gate が有効な集合と一致し、
余分・不足がないこと（「会社ナレッジ4のみ」という旧前提は廃止）。OpenClaw rollout の
one-off canary も同じ exact tools/list を検査するが、本ハーネスの RLS 攻撃検証を代替しない。

## 手順3: bearer-only 敵対ハーネス（全件早期拒否）
```sh
TEAMAGENT_MCP_BEARER=... TEAMAGENT_SHARED_COMPANY_DOMAINS=vectorinc.co.jp \
  uv run python scripts/attack_mcp.py --base-url http://127.0.0.1:8787 \
  --query P0HARNESS
```
投げる詐称vector: `admin_role` / `evil_email` / `evil_groups` / `verified_flag` /
`bad_slack_uid` / `kitchen_sink`。bearerだけではcaller claimを偽造できないため、期待値は全件
`CALLER_IDENTITY_REJECTED`であり、resolver/RLS/searchへ到達しない。harnessへ
`TEAMAGENT_CALLER_CLAIM_SECRET`を渡して「成功claim」を自作してはならない。

## 手順4: 署名済みpositive E2Eと監査ログ
まずローカルで次を全passさせる。
```sh
uv run pytest -q tests/test_mcp_gateway_caller_claim.py \
  tests/test_mcp_gateway_identity.py tests/test_slack_client_identity.py
```
live positive testはclaimを手作りせず、許可済み実Slack memberがOpenClawへmentionし、
その返信で会社docが得られ会社外docの`OUTSIDER_ONLY_TOKEN`が出ないことを確認する。
CloudWatch Logsでは成功時に`identity_resolved`、手順3では`caller_claim_rejected`を確認する。
guest/stranger/別workspace/期限切れ/replay/改ざんの自動E2Eが1件でも失敗した場合は公開停止。

## 手順5: 後片付け
```sh
DATABASE_URL=... uv run python scripts/ingest_test_data.py --cleanup
```

## 合否（go/no-go の安全ゲート）
- 手順2/3/4 が **全 PASS**＝bearer-only偽造不可、署名済みmemberだけがRLSへ到達、
  会社外doc漏洩0を実走で実証。
- 1つでも FAIL（特に `no_outsider_leak`）＝**公開停止**。gateway の会社共有分岐／`0010`/`0011` 適用／
  ingest の `acl_groups` を点検（多くは migration 未適用 or company domain env 不一致）。
