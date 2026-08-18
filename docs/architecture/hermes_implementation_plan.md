# Hermes 導入 Implementation Plan（PR 分割・実装単位・テスト戦略）

- 親文書: [hermes_migration_design.md](hermes_migration_design.md)（ADR。設計判断・Security 不変条件はそちらが正）
- 前提: 全変更は Feature Flag 既定 OFF・既存挙動の無フラグ変更なし・OpenClaw toolFilter.include に入れない（dark）・Big Bang 禁止

## PR 分割と順序

```
PR1 Docs → PR2 Hermes dark runtime → PR3 run_hermes_agent + delegated security
        → PR-R capacity control（必須 Gate）→ PR4 Proposal pilot
        → PR5 Profile → PR6 Memory → PR7 Multi source → PR8 Router
```

| PR | 内容 | 変更範囲 | リスク | 承認 |
|---|---|---|---|---|
| PR1 | docs only（ADR/実装計画/README/索引/Archive） | *.md のみ | ゼロ | 本 PR |
| PR2 | Hermes dark runtime: コンテナ + ECS service（**desired_count=0**）+ 最小 IAM + healthz | infra/docker, infra/terraform（新規リソースのみ） | 低 | PR1 後に個別承認 |
| PR3 | run_hermes_agent + delegated session claim + callback boundary + Security Tests | mcp_gateway/, hermes/, tests/ | 中（flag OFF で不活性） | **着手前に delegated claim 設計の再レビューを実施**（裁定済み） |
| **PR-R** | **容量制御（PR4 前の必須 Gate）**: MCP admission control / in-flight metrics / heavy-tool semaphore / 明示 overload 応答 | mcp_gateway/, runtime/, fargate.tf env | 中 | 個別承認 |
| PR4 | Proposal Specialist pilot（allowlist + A/B eval） | hermes profile 定義, eval | 低 | 個別承認 |
| PR5 | Personal Profile（hermes_profiles migration + store） | migrations, hermes/ | 低（additive） | 個別承認 |
| PR6 | Personal Memory（propose→approve・監査） | hermes/, usage_recorder | 中 | 個別承認 |
| PR7 | Multi source 解禁拡大（policy version 更新を含む） | policy 定義, （必要なら）skill G1 強化 | 中 | 個別承認 |
| PR8 | AI General Router（SOUL 改訂 + OC 4点セット） | infra/openclaw/ | 中 | 個別承認 |

## PR2 詳細（Hermes dark runtime）

**受け入れ形態（ADR §20 裁定）**: `desired_count=0`。受け入れ試験は ECS RunTask で 1 タスク起動 → `startup → /healthz → Bedrock client 初期化 → CloudWatch logs` を確認して終了。常駐ゼロ＝idle コストゼロ・外部 routing 0・MCP exposure 0 が自明。

| Priority | File | Change | Test / 証明 | Rollback |
|---|---|---|---|---|
| P0 | `infra/docker/Dockerfile.hermes` | hermes-agent を digest 固定 pin・非 root・readonly rootfs | Trivy C0/H0（リリース契約と同基準） | イメージ未使用なら無影響 |
| P0 | `infra/terraform/hermes.tf` | ECS service `teamagent-hermes`（desired_count=0）・Cloud Map `teamagent-hermes.teamagent.internal`・SG は mcp→hermes:8790 / hermes→mcp:8787 のみ。**RDS SG / vpce SG に hermes SG を足さない** | SG 契約テスト・tf diff レビュー | 新規リソースのみ＝除去容易 |
| P0 | hermes IAM role | Allow = bedrock:InvokeModel（限定 profile）+ logs のみ。OpenClaw 同様の**明示 Deny**（secretsmanager:\* / kms:\* / rds\* / dynamodb:\* / s3:\*） | IAM policy 契約テスト（既存 test_\*_contract 同型） | 同上 |
| P0 | healthz | GET /healthz = process alive + config loaded + Bedrock client init + MCP callback 設定 presence（外部 tool call は含めない） | RunTask 受け入れ試験 | — |
| P0 | 構造化ログ | service/version/request_id/model/latency/error（dark 中は startup/health のみ） | CloudWatch 実ログ確認 | — |

**PR2 受け入れ条件**: OpenClaw 挙動完全不変 / MCP tool list 完全不変 / existing tests green / RunTask 試験緑 / RDS・OAuth token へのアクセス権なし（IAM 実証）/ rollback = リソース削除 or desired_count=0 のまま放置。

## PR3 詳細（run_hermes_agent + delegated claim）

設計は ADR §7 が正。実装単位:

| Priority | File | Change |
|---|---|---|
| P0 | `src/teamagent/mcp_gateway/hermes_claim.py`（新規） | `DelegatedSessionClaim` + `DelegatedClaimVerifier`（**既存 CallerClaimVerifier とは別クラス**・16field 型の exact-set 検査/重複キー拒否/サイズ上限/署名先行検証は caller_claim.py の実装を共通化して踏襲）。K_session = HKDF(master, session_id‖nonce‖sub) 導出・per-call MAC 検証 |
| P0 | `src/teamagent/mcp_gateway/hermes_session_store.py`（新規） | DynamoDB `hermes-session-state`: session 正本（sub/allowed_tools/policy_version）・absolute_deadline・remaining_calls（実行前 conditional UpdateItem）・consumed call_nonce（conditional PutItem）・budget。**全障害 fail-closed** |
| P0 | `src/teamagent/mcp_gateway/server.py` | `RUN_HERMES_TOOL_NAME="run_hermes_agent"`・`_envflag("USE_HERMES_ORCHESTRATOR")` で list/call（run_agent と同型）。dispatch は既存 `_verify_caller`→`_resolve_metadata` 通過後に claim mint → Hermes へ HTTP POST |
| P0 | `scripts/run_mcp_http_server.py` | BearerAuthMiddleware を route×token 対応表型へ拡張し `/hermes/callback` を追加（既存 route の挙動不変）。callback route は縮小 tool マップ＋`_user_context.caller_claim` 不受理 |
| P0 | `src/teamagent/hermes/`（新規 pkg） | gateway_client（Gateway→Hermes・bearer・timeout・構造化エラー）・policy.py（`hermes_tool_policy_version=1` の allowlist/denylist 定義＝サーバ側真実源） |
| P0 | `src/teamagent/hmac_keyring.py` | `HMAC_PURPOSE_HERMES_DELEGATION` 追加（purpose 重複拒否・rotation 継承）。相互排他チェック 5 値化 |
| P0 | `tests/scripts/test_openclaw_runtime_contract.py` | run_hermes_agent が OC include に**無い**ことの断言（dark 宣言） |
| P0 | env | `USE_HERMES_ORCHESTRATOR`(0) / `HERMES_SERVICE_URL` / `TEAMAGENT_HERMES_INGRESS_BEARER` / `TEAMAGENT_HERMES_CALLBACK_BEARER` / `HERMES_SESSION_STATE_TABLE` / `HERMES_COST_CAP_USD`(0.5) / `HERMES_MAX_CALLS`(8) / `HERMES_SESSION_TTL_S`(≤300) / `HERMES_ABSOLUTE_DEADLINE_S`(900) / `HERMES_ALLOWED_EMAILS`(空=拒否) |

### PR3 Security Tests（指示 §19 の A〜K 対応 + マージブロッカー）

| 指示 | テスト | 既存の複製元 |
|---|---|---|
| A/B/C forged role/groups/email → ignored | Hermes 申告値の全破棄 + fuzz で `user_role` 常に member | `test_mcp_gateway_identity.py:102,203` |
| D expired → reject | exp / absolute_deadline 超過 | `test_mcp_gateway_caller_claim.py:899-943` |
| E replay → reject | per-call nonce one-use + verifier インスタンス跨ぎ | 同 `:946-1012` |
| F tool outside allowed_tools → reject | intersection + **denylist が allowlist に勝つ** | 同 `:673-711` 型 |
| G max_calls exceeded → reject | 並行 16→8 race（conditional UpdateItem 実証） | `hmac_durable_state.py:704` 型 |
| H User A claim → User B → reject | cross-user session race・K_session 混線なし | `test_mcp_gateway_caller_claim.py:714-896`（最重要） |
| I profile isolation | PR5 で実装（v1 は not yet implemented を明示） | — |
| J recursive → impossible | meta-tool 恒久 deny（claim に入れても拒否） | — |
| K missing identity → fail-closed | resolver 再実行でゲスト/退職/stranger 拒否・予算ストア障害時 skill 不実行 | `test_mcp_gateway_caller_claim.py:1015-1080` |
| 起動時契約 | 鍵未設定/同値/台帳未設定で起動拒否 | `test_mcp_gateway_caller_claim.py:1091-1115` |
| route×token | 既存 bearer で callback 不達・callback bearer で /mcp 不達 | 新規 |

全て**変異テスト**で実質性を証明（ガードを壊して赤を確認）。

## PR-R 詳細（容量制御・PR4 前の必須 Gate）

| 項目 | 実装 |
|---|---|
| MCP admission control | `dispatch_tool` 直前に既存 `RequestGate`（runtime/request_gate.py）を module-level 配線。`REQUEST_GATE_*` env を fargate.tf mcp taskdef へ |
| 明示 overload 応答 | QueueFullError/GateTimeoutError → `_err(...)` 構造化エラー → OpenClaw が「混雑しています」を返す |
| in-flight metrics | MetricsSnapshotter を MCP プロセスへ配線（gate/pool → runtime_metrics） |
| heavy-tool semaphore | video_algorithm / proposal_builder 等の別枠（connect_web の SEARCH_CONCURRENCY 同型） |

## PR4〜PR8（概要）

- **PR4**: Proposal Hermes（allowlist= search/clientkarte/proposal_*・`HERMES_ALLOWED_EMAILS` で段階公開）。eval は `orchestrator/eval.py`・`faithfulness.py`（chunk_id 忠実性照合）を流用し、既存フロー vs Hermes の同一 goal shadow 比較（quality/latency/cost/tool count/failure rate/citation）
- **PR5**: `hermes_profiles` migration（RLS 本人行のみ・oauth_tokens_self 同型）+ profile_id 決定論導出。Hermes からの profile 指定は不可
- **PR6**: memory_items（propose→pending→approve）。company source ingest 禁止は「永続化 API を渡さない」構造で担保。隔離テスト（A≠B・改ざん不可・Hermes 自身による profile 変更不可）
- **PR7**: policy version 更新による段階解禁（前提: mail_*/calendar_* G1 の identity_verified 強化）。GWS/Slack/RAG は既存 adapter/MCP を再利用（再実装禁止）。Salesforce は adapter+skill 新設後に allowlist へ
- **PR8**: Router（SOUL 改訂）。OC 露出は 4 点セット（tf env / scope 台帳 / 契約テスト / OC イメージ再ビルド）

## 各 PR で報告するもの（運用契約）

開始前: Scope / Files / Security impact / Runtime impact / Rollback。
終了後: Changed files / Tests / Security tests / Terraform plan shape / Runtime behavior / Feature flags / Known risks / Rollback command。
