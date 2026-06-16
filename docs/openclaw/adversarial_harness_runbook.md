# P0 敵対ハーネス Runbook（RLS越権0・なりすまし封鎖の実証）— §J / A

会社共有モデル(§G)の安全核を、実DB＋live MCP に対して**実際に攻撃して**確認する手順。
gateway は OC 申告の `user_email`/`user_groups`/`user_role`/`identity_verified` を破棄し、固定の
「会社メンバー」identity で実行する（`slack_user_id` は監査のみ）。本ハーネスは「詐称が結果に
一切効かない／会社外 doc はどの詐称でも漏れない」を end-to-end で示す。

> 静的には既に緑（再検証不要）: `tests/test_mcp_gateway_identity.py`（admin破棄・ダウングレード封鎖・
> OC申告破棄・外部/ゲスト拒否）／`tests/test_slack_client_identity.py`／`tests/test_rls_email_ci_migration.py`
> （0010 両側 lower()）／`tests/test_attack_mcp.py`（本ハーネスの純ロジック）。
> **本 runbook が足すのは「実 pgvector＋live MCP での実走証明」だけ**（SSMトンネル/承認後）。

## 前提（gated）
- §I deploy_runbook の手順で MCP backend が起動済（healthz green）。または PoC として
  `TEAMAGENT_MCP_BEARER=... TEAMAGENT_SHARED_COMPANY_DOMAINS=vectorinc.co.jp uv run python scripts/run_mcp_http_server.py`。
- 実RDS に `0010`/`0011` 適用済（§I 手順5）。`DATABASE_URL` は SSMトンネル経由（要承認）。

## 手順1: fixture 投入（会社doc×2＋会社外doc×1）
```sh
DATABASE_URL=... TEAMAGENT_SHARED_COMPANY_DOMAINS=vectorinc.co.jp \
  uv run python scripts/ingest_test_data.py --commit
```
- `company1/company2`（`acl_groups=[会社ドメイン]`）＝会社メンバーに可視。
- `outsider`（`outsider@evil.com`・`acl_groups=[]`）＝**会社メンバーに不可視であるべき**。本文に
  `OUTSIDER_ONLY_TOKEN` を含む。全 doc に共通語 `P0HARNESS`（search 候補に載せRLSで会社外が落ちるのを観る）。

## 手順2: smoke（protocol/認証/露出）
```sh
TEAMAGENT_MCP_BEARER=... TEAMAGENT_SHARED_COMPANY_DOMAINS=vectorinc.co.jp \
  uv run python scripts/smoke_mcp.py --base-url http://127.0.0.1:8787 --full
```
期待: healthz=200 / bearer無=401 / tools=会社ナレッジ4のみ / search 200。

## 手順3: 敵対ハーネス（詐称→無効化）
```sh
TEAMAGENT_MCP_BEARER=... TEAMAGENT_SHARED_COMPANY_DOMAINS=vectorinc.co.jp \
  uv run python scripts/attack_mcp.py --base-url http://127.0.0.1:8787 \
  --query P0HARNESS --outsider-needle OUTSIDER_ONLY_TOKEN
```
投げる詐称 vector（`_user_context` に詰める）: `admin_role` / `evil_email` / `evil_groups` /
`verified_flag` / `bad_slack_uid` / `kitchen_sink`。**期待（全て PASS）**:
- `<vector>:identical_to_baseline` — どの詐称も baseline（無害な会社identity）と**同一結果**＝詐称は無効。
- `<vector>:no_outsider_leak` — `OUTSIDER_ONLY_TOKEN` がどの vector の結果にも**現れない**（admin 詐称でも会社外0）。
- `baseline:no_outsider_leak` — 無害identityでも会社外 doc は出ない（RLS が効いている）。

## 手順4: 監査ログの確認（任意）
MCP の CloudWatch Logs（`/teamagent/<env>/teamagent-mcp`）で、各攻撃時に
`identity_company_shared{slack_user_id_audit}` が記録され、OC申告フィールドを詰めた vector で
`identity_spoof_rejected{reason="oc_fields_dropped"}` が出ることを確認（詐称破棄の証跡）。

## 手順5: 後片付け
```sh
DATABASE_URL=... uv run python scripts/ingest_test_data.py --cleanup
```

## 合否（go/no-go の安全ゲート）
- 手順2/3 が **全 PASS**＝「RLS越権0・なりすまし封鎖」を実走で実証。
- 1つでも FAIL（特に `no_outsider_leak`）＝**公開停止**。gateway の会社共有分岐／`0010`/`0011` 適用／
  ingest の `acl_groups` を点検（多くは migration 未適用 or company domain env 不一致）。
