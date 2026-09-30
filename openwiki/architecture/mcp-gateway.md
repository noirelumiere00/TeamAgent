---
type: architecture
title: MCP gateway（ツール実行サーバ）
description: OpenClaw から streamable-http で呼ばれる TeamAgent MCP サーバの起動条件と、1 回のツール呼び出しを caller claim 検証・Slack 本人解決・RLS メタデータ組み立て・入力検証・skill 実行・usage 記録・返却前処理へ流す dispatch_tool の流れ。
tags: [mcp-gateway, mcp, rls, identity, fail-closed, streamable-http]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T07:51:05.076Z
sources:
  - id: openwiki-source-49e9ec8046eee60f7a08c80f
    resource: repo://scripts/run_mcp_http_server.py
  - id: openwiki-source-02168c912f267e4d2df9fc4d
    resource: repo://scripts/run_mcp_vertex_entrypoint.py
  - id: openwiki-source-844698543a25f403fd0c9bae
    resource: repo://src/teamagent/identity.py
  - id: openwiki-source-1fdde611c13aba4b68a5ff42
    resource: repo://src/teamagent/mcp_gateway/server.py
generated: { by: "claude-code", at: "2026-09-29T07:51:05.076Z" }
---

# MCP gateway（ツール実行サーバ）

## 位置づけ

<!-- openwiki: broken internal link [/openwiki/architecture/overview.md] link "/openwiki/architecture/overview.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
MCP gateway は「秘密と権限を持つ側」のプロセスで、Skill の実体・Bedrock・pgvector・Google/Slack の token にアクセスする。Slack の受け口である OpenClaw は RDS・Secrets・Google に直接触れず、私設ネットワーク越しにこのサーバを呼ぶだけ（[全体構成](/openwiki/architecture/overview.md)）。

stdio ではなく HTTP にしているのは、stdio MCP だとサーバが OpenClaw のコンテナ内の子プロセスになり、OpenClaw の IAM ロールとネットワークを共有してしまうため（`scripts/run_mcp_http_server.py` 冒頭）。stdio 版 `scripts/run_mcp_server.py` はローカル / PoC 専用。

## 起動（fail-closed）

本番コンテナは `scripts/run_mcp_vertex_entrypoint.py` から始まる。core イメージにシェルが無いため、この Python が env の `VERTEX_SA_JSON` をタスク専用 `/tmp` に 0600 で書き出して `GOOGLE_APPLICATION_CREDENTIALS` に差し替え、`USE_PROPOSAL_BUILDER_TOOLS` が ON なら提案書資産を用意してから `run_mcp_http_server.py` を exec する。

`run_mcp_http_server.py` の `main()` は次の順で確認し、欠けていれば終了コード 2 で起動を拒否する。

1. `configure_logging()`（`STRUCTLOG_FORMAT=json` で JSON）。
<!-- openwiki: broken internal link [/openwiki/operations/hmac-keyring-and-rotation.md] link "/openwiki/operations/hmac-keyring-and-rotation.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
2. `require_runtime_startup(...)`: mail_action / report_link の HMAC 鍵束が使える状態か（[HMAC 鍵束](/openwiki/operations/hmac-keyring-and-rotation.md)）。
3. `TEAMAGENT_MCP_BEARER` が設定されている（無認証公開の禁止）。
4. `CallerClaimVerifier.from_env()` が成功する（`TEAMAGENT_CALLER_CLAIM_SECRET`・`TEAMAGENT_CALLER_CLAIM_REPLAY_TABLE`・`SLACK_TEAM_ID`）。

その後 `build_production_server()` が `SLACK_BOT_TOKEN` から Slack identity resolver を作る（無ければ `RuntimeError`）。`TEAMAGENT_SHARED_COMPANY_DOMAINS` があれば会社共有モードで、無ければ本人ごとの STRICT モードで `build_server` を組む。

HTTP 面は Starlette:

| パス | 内容 |
|---|---|
| `/healthz` | bearer 不要のヘルスチェック（ECS / Dockerfile の HEALTHCHECK が叩く） |
| `TEAMAGENT_MCP_PATH`（既定 `/mcp`） | `StreamableHTTPSessionManager`。`BearerAuthMiddleware` が定数時間比較で bearer を検査し、違えば 401 |

<!-- openwiki: broken internal link [/openwiki/architecture/detached-jobs-and-async-notify.md] link "/openwiki/architecture/detached-jobs-and-async-notify.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
既定の bind は `127.0.0.1:8787`。OpenClaw 側の接続先は `infra/openclaw/openclaw.config.json5` の `mcp.servers.teamagent.url`（Cloud Map の内部名・ポート 8787・`/mcp`）。SIGTERM 時の中断通知は [長時間ジョブの切り離し](/openwiki/architecture/detached-jobs-and-async-notify.md)。

## ツール一覧（list_tools）

<!-- openwiki: broken internal link [/openwiki/architecture/tool-registry-and-feature-flags.md] link "/openwiki/architecture/tool-registry-and-feature-flags.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
`list_tool_defs` は factory が作った `ToolSpec` 群（[ツール登録と機能フラグ](/openwiki/architecture/tool-registry-and-feature-flags.md)）を MCP の Tool 定義に変換し、入力スキーマの properties に `_user_context`（`slack_user_id`・`slack_team_id`・`caller_claim`・配信先ヒントの `channel_id` / `thread_ts` など）を足す。

`_user_context` を **required に入れてはいけない**。OpenClaw のクライアント側引数検証は caller-identity plugin の注入より前に走るため、required にするとモデルが省略した時点で全ツールが死ぬ（過去の本番全ツール障害）。

<!-- openwiki: broken internal link [/openwiki/architecture/orchestrator.md] link "/openwiki/architecture/orchestrator.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/openwiki/architecture/hermes-personal-memory.md] link "/openwiki/architecture/hermes-personal-memory.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
`USE_AGENT_ORCHESTRATOR` が ON のときだけ L2 の `run_agent` が追加される（[オーケストレータ](/openwiki/architecture/orchestrator.md)）。本人メモのツールは一覧に出さず別経路で受ける（[本人メモ](/openwiki/architecture/hermes-personal-memory.md)）。

## dispatch_tool の流れ

```text
tools/call(name, arguments)
 ├─ 未登録ツール → "unknown tool"
 ├─ _verify_caller: 署名 claim 検証（失敗 → 本人確認拒否）
 ├─ _resolve_metadata: 本人解決 → RLS メタ（失敗 → 本人確認拒否）
 ├─ _maybe_redirect_to_connect: 引数に「連携」依頼があれば oauth_connect へ寄せる
 ├─ spec.input_schema(**args): Pydantic 入力検証（失敗 → "invalid input"）
 ├─ SkillContext(user_id=email, metadata=RLS メタ)
 ├─ （video_algorithm の切り離し判定・進捗表示）
 ├─ asyncio.to_thread(skill.run, input, ctx)   ← 同期 skill を thread で実行
 │    └─ 例外 → mcp_tool_error ログ・usage(status=error)・構造化エラー
 └─ 返却前ミドルウェア（2 段目登録・完了通知・usage 記録・直接投稿・relay・退避・リンク注入）
```

例外はすべて握り、構造化エラー（`TextContent` の JSON）で返す。MCP サーバも OpenClaw のループも落とさない。

### 本人解決と RLS メタデータ

`_resolve_metadata` は 3 つのモードを持つ。

| モード | 条件 | 挙動 |
|---|---|---|
| 会社共有（COMPANY_SHARED） | `TEAMAGENT_SHARED_COMPANY_DOMAINS` 設定時 | 署名済み caller と resolver の成功が必須。会社ドメイン群を `user_groups` に足し、本人 email も載せる（search などは全社可視、mail 系は本人 token を引ける） |
| STRICT | resolver あり | 署名済み event user をサーバ側で解決。外殻が申告した `user_email` / `user_groups` / `user_role` は破棄し `identity_spoof_rejected` を警告 |
| LEGACY | resolver なし | テスト / PoC 専用。本番エントリポイントからは到達不能 |

<!-- openwiki: broken internal link [/openwiki/integrations/slack-identity-and-oauth.md] link "/openwiki/integrations/slack-identity-and-oauth.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/openwiki/data/rls-and-app-role.md] link "/openwiki/data/rls-and-app-role.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
どのモードでも RLS メタへの変換は `src/teamagent/identity.py` の `build_rls_metadata` が唯一の変換点で、`user_role` は常に `"member"`（MCP 越しの admin 昇格は構造的に不可）、email は strip+lower+形式検証、非メンバ・許可外ドメイン（`TEAMAGENT_ALLOWED_EMAIL_DOMAINS`）は None ＝ fail-closed。`channel_id` / `thread_ts` は配信先のヒントとしてだけ載り、認可には使わない。resolver の判定内容は [Slack の本人確認と連携](/openwiki/integrations/slack-identity-and-oauth.md)、RLS 側は [RLS と実行ロール](/openwiki/data/rls-and-app-role.md)。

### 「連携」依頼の決定論分岐

<!-- openwiki: broken internal link [/openwiki/architecture/caller-identity-and-button-bindings.md] link "/openwiki/architecture/caller-identity-and-button-bindings.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
`_maybe_redirect_to_connect` は、引数に連携依頼が検出されたら、モデルがどのツールを選んだかに関係なく `oauth_connect` へ差し替える（元の引数は捨てる）。署名 claim は元のツール名で検証済みで、`oauth_connect` は呼んだ本人向け URL を返すだけなので権限は広がらない。モデルがツールを 1 つも呼ばないターンはここに来ないので、plugin の層1〜3 と SOUL.md が補う（[呼び出し元の証明](/openwiki/architecture/caller-identity-and-button-bindings.md)）。`run_agent` 経路には意図的に適用しない。

### usage 記録

<!-- openwiki: broken internal link [/openwiki/operations/observability-and-cost.md] link "/openwiki/operations/observability-and-cost.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
成功時は `mcp_tool_usage` ログ（`latency_ms` = skill 実行時間、`gateway_ms` = 受信→skill 開始、`total_ms`、`tool_cost_usd`）と `usage_events` への best-effort 記録を行う。ログのキーを `cost_usd` にしないのは、CloudWatch のコスト metric filter `{ $.cost_usd = * }` が adapter 層のログと二重計上になるため。`usage_events.user_id` には署名検証済み claim 由来の Slack ID だけを使う。詳しくは [観測・利用記録・コスト管理](/openwiki/operations/observability-and-cost.md)。

## 変更するときの注意

- 新しい返却前処理を足すときは順序契約（usage 記録 → 直接投稿 → relay → 退避 → リンク注入）を崩さない。
- 本人確認の拒否文には診断コード（例 `missing_verified_caller`）が付き、利用者が管理者へ転送できるようになっている。拒否理由を増やすときも生入力や email を応答・ログに入れない。
- ローカルで HTTP サーバを試すには bearer・caller claim 秘密・replay table・`SLACK_TEAM_ID`・`SLACK_BOT_TOKEN` が要る。テストでは `build_server` にフェイクを注入する。

## テスト

- `tests/test_mcp_gateway_server.py`: dispatch・エラー隔離・スキーマ拡張。
- `tests/test_mcp_gateway_identity.py` / `tests/test_mcp_gateway_caller_claim.py`: モード別の本人解決と claim 検証。
- `tests/test_mcp_gateway_diagnostics.py`、`tests/test_mcp_gateway_run_agent.py`、`tests/mcp_gateway/test_connect_intent_routing.py`、`tests/mcp_gateway/test_tool_usage_breakdown.py`。
