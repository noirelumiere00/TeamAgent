---
type: architecture
title: オーケストレータ（bounded tool loop）
description: anthropic の AsyncAnthropicBedrock で既存 Skill をツールとして回す自前の上限付き tool loop（sdk_runner.run_sdk_agent）と、それを 1 ツールとして公開する run_agent（USE_AGENT_ORCHESTRATOR）、ToolSpec・decider/loop・忠実性チェック・評価の役割と現在の公開状態。
tags: [orchestrator, bedrock, tool-loop, run-agent, evaluation, faithfulness]
sources:
  - id: openwiki-source-11b6c3c162105aea7c4bba90
    resource: repo://infra/openclaw/openclaw.config.json5
  - id: openwiki-source-1fdde611c13aba4b68a5ff42
    resource: repo://src/teamagent/mcp_gateway/server.py
  - id: openwiki-source-7b325f7000716fa4a36cbee8
    resource: repo://src/teamagent/orchestrator/agent_config.py
  - id: openwiki-source-e9a2873609f3d33cdc2a95cd
    resource: repo://src/teamagent/orchestrator/eval.py
  - id: openwiki-source-53aadf0d1d14b46a258659a3
    resource: repo://src/teamagent/orchestrator/faithfulness.py
  - id: openwiki-source-85ae2d6f3c075b3c05193554
    resource: repo://src/teamagent/orchestrator/sdk_runner.py
  - id: openwiki-source-72a907c7c8ecff6406fc4b3d
    resource: repo://src/teamagent/orchestrator/tools.py
generated: { by: "claude-code", at: "2026-09-30T04:50:20.078Z" }
verified:
  - by: openwiki/0.6.1
    at: 2026-09-30T04:50:20.078Z
---

# オーケストレータ（bounded tool loop）

## 2 つのオーケストレーション層

Aico には「どのツールを呼ぶか決める層」が 2 つある。

| 層 | どこで動くか | 状態 |
|---|---|---|
| L0/L1: OpenClaw の agent loop | OpenClaw（Haiku）が MCP ツールを 1 本ずつ選んで呼ぶ | **本番の主経路** |
| L2: `run_agent` | MCP gateway 内で `run_sdk_agent` が複数ツールを自律的に回す | `USE_AGENT_ORCHESTRATOR` で MCP に出るが、OpenClaw の `toolFilter.include` に**意図的に入れていない**＝Aico からは呼べない（dark） |

L2 を Aico に出していないのは、OpenClaw 自身がオーケストレーションの外殻なので二重オーケストレーションを避けるため（`infra/openclaw/openclaw.config.json5` の注記）。L2 は `scripts/run_orchestrator_prod.py`・評価スクリプト・テストから使われる。

なお Claude Agent SDK はかつて使っていたが置き換え済みで、core イメージでは禁止依存（`pyproject.toml` の依存コメント: Bun/JS runtime を同梱するため core runtime に入れない）。

## ToolSpec（`orchestrator/tools.py`）

`ToolSpec` は「LLM に見せる 1 ツール = 1 Skill」の薄い包み。`name`・`description`・`skill_cls`・任意の `factory`（本番 Skill は依存注入が要るため）を持ち、`json_schema()` は Skill の Pydantic 入力スキーマをそのまま返す。MCP gateway の `list_tools` も L2 も同じ `ToolSpec` 群（`factory.build_production_tools()`）を使う。

## run_sdk_agent の仕組み（`orchestrator/sdk_runner.py`）

```text
goal → messages=[user: goal]
loop turn in range(max_turns):
   累計コスト ≥ cost_cap_usd → stopped_reason=error_max_budget_usd
   client.messages.create(model, system, messages, tools)   ← AsyncAnthropicBedrock
   usage → CostRecord をログ
   tool_use ブロックごとに handler(args) → tool_result
   tool_result があれば次のターンへ
   なければ stop_reason が end_turn/stop_sequence → final、他は error_<reason>
ループを使い切ったら error_max_turns
```

呼び出し境界でのガード:

- `max_turns` は 1〜32、`cost_cap_usd` は 0 超〜100 以外なら `OrchestratorError`。
- `require_rls=True` で `ctx_metadata["user_email"]` が無ければ fail-closed。
- API 例外は `stopped_reason="error_api"` にして HTTP ステータスを残し、ループを止める（raise しない）。

各ツール handler（`_make_handler`）のガード:

- **同一ツール×同一入力の繰り返し**は `max_same_call`（既定 2）を超えたら構造化エラーを返し、別の手段を促す（無限ループ殺し）。
- 入力は Skill の Pydantic で検証し、失敗は構造化エラー。
- 同期 `skill.run` を `run_in_executor` + `tool_timeout_s` で実行（イベントループを塞がない）。タイムアウト・例外も構造化エラーとして LLM に返す。
- `SkillContext.metadata` に `orchestrated_tool_call` の印を立てる。配信系 Skill はこれを見て、「調べるだけ」の中間ステップで Slack にファイルを投下しない（[3層分離と Skill の契約](layering-and-skill-contract.md)）。
- Skill が返した `total_cost_usd` は `skill_cost` としてログ。ツール結果 JSON 中の `chunk_id` を集め、最終回答の引用検証に使う。

コストは `usage_to_record` が input / output / cache_read / cache_creation のトークンから概算する（`Price` の既定単価は Sonnet 相当の PoC 値で、コメントも「概算」と明記）。Bedrock 呼び出し全般のコスト記録は [Bedrock / Gemini 呼び出し](../integrations/bedrock-gemini-and-retry.md)。

## run_agent（MCP ツールとしての L2）

`mcp_gateway/server.py` の `dispatch_run_agent` は、`dispatch_tool` と同じ caller claim 検証と `_resolve_metadata` を通したうえで、`goal`（必須の非空文字列）を `run_sdk_agent` に渡す。既定は `max_turns=8`・`cost_cap_usd=0.5`・`tool_timeout_s=90`。L1 ツール一式（`run_agent` 自身は含まない＝再帰しない）をそのまま渡し、「連携」依頼の振り替えは意図的に適用しない。返却は `answer`・`stopped_reason`・`is_error`・`num_turns`・`tool_calls`・`session_total_cost_usd` など。

model と system prompt は `orchestrator/agent_config.py` が単一の真実源で、model は `TEAMAGENT_BEDROCK_MODEL` → `BEDROCK_MODEL_ID` → 既定の東京 Haiku 4.5 推論プロファイルの順に決まる。`USE_MAIL_TOOLS` が ON のときだけ `mail_constraints` による NG 施策の差し替え指示を足す。

## 補助モジュール

| モジュール | 役割 |
|---|---|
| `decider.py` / `loop.py` | 「計画→実行→観測→再計画」の抽象ループ。`MockDecider` でスクリプト化してオフラインに適応分岐を検証する用途（方式 A/B の比較は `docs/poc/agent_orchestrator_poc_design.md`） |
| `faithfulness.py` | 回答中の `chunk_id` 引用が実際に取得した hits に含まれるかを純関数で判定（捏造引用・無引用の検出）。LLM judge 不要で CI で回せる |
| `eval.py` | ゴールドセット（期待ツール・いずれか 1 つ・禁止ツール）に対する決定的な採点 `score_case`。実 Bedrock で tool_calls を取るのは `scripts/eval_orchestration.py`（課金あり・手動） |
| `factory.py` | 本番ツール群の組み立て。[ツール登録と機能フラグ](tool-registry-and-feature-flags.md) |

## コードと規約の食い違い

- CLAUDE.md §2 は「temperature=0.1」「prompt caching を必ず使う」としているが、`run_sdk_agent` の `messages.create` は `temperature` も `cache_control` も指定していない（`max_tokens=4096` のみ）。L2 は dark 経路なので本番影響は限られるが、公開するなら要確認。
- CLAUDE.md §3 は prompt をコードに書かないルールだが、`agent_config.py` の `ORCHESTRATOR_SYSTEM_PROMPT` はコード内の文字列定数。

## テスト

`tests/orchestrator/`:

- `test_phase0_hardening.py`（RLS 伝播・繰り返し拒否・timeout・例外の構造化）
- `test_sdk_cost_logging.py`（usage → CostRecord）
- `test_adaptive_trace.py`（適応ループ）
- `test_faithfulness.py`、`test_orchestration_eval.py`
- `test_factory_smoke.py`、`test_search_skill_config_parity.py`

MCP 経由の `run_agent` は `tests/test_mcp_gateway_run_agent.py`。評価の回し方は [ルーティング検証と評価](../testing/routing-and-eval.md)。
