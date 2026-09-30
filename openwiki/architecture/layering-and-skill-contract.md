---
type: architecture
title: 3層分離と Skill の契約
description: src/teamagent の skills / adapters / runtime（と orchestrator・mcp_gateway・ingest）の依存方向を import-linter がどう強制しているか、BaseSkill・SkillRegistry・SkillContext・Pydantic I/O の契約、prompt ファイルの読み込み、structlog の JSON 出力。
tags: [architecture, skills, adapters, import-linter, pydantic, logging, prompts]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T07:51:05.076Z
sources:
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-49e9ec8046eee60f7a08c80f
    resource: repo://scripts/run_mcp_http_server.py
  - id: openwiki-source-5af7cbe3c2f8beda733ea57c
    resource: repo://src/teamagent/observability/logging_config.py
  - id: openwiki-source-9182f0f54a683a2ffa1f00c6
    resource: repo://src/teamagent/prompts/loader.py
  - id: openwiki-source-4303ac50b39830c520361ca8
    resource: repo://src/teamagent/skills/_shared/rollout.py
  - id: openwiki-source-65bed21ef984d567bf3bb17a
    resource: repo://src/teamagent/skills/base.py
  - id: openwiki-source-ad10fd63dc1fd8c554a725f0
    resource: repo://src/teamagent/skills/knowledge_deliver/skill.py
generated: { by: "claude-code", at: "2026-09-30T04:50:20.078Z" }
---

# 3層分離と Skill の契約

## 層と依存方向

`src/teamagent/` は機能を「業務ロジック（skills）」と「外部クライアント（adapters）」と「起動・配線（runtime ほか）」に分けている。

| 層 | 主なパッケージ | 役割 |
|---|---|---|
| 入口・配線 | `mcp_gateway/`、`orchestrator/`、`runtime/`、`connect_web/`、`dashboard/`、`workers/`、`scripts/` | MCP サーバ、ツール登録、ECS エントリポイント、Web UI |
| 業務ロジック | `skills/<name>/{schema,skill}.py`、`skills/_shared/`、`ingest/` | 1 ツール＝1 Skill。入出力は Pydantic |
| 外部クライアント | `adapters/` | Bedrock・Gemini・pgvector/PostgreSQL・Slack・Google・S3・DynamoDB・Apify など |

CI の `lint-imports`（`pyproject.toml` の `[tool.importlinter]`）が強制している契約は **2 本だけ**。

1. `teamagent.adapters` は `runtime` / `skills` / `orchestrator` / `mcp_gateway` / `ingest` を import しない（adapters が最下層）。
2. `teamagent.skills` は `teamagent.runtime` を import しない。

つまり「runtime → skills → adapters の一方向」のうち機械的に止められているのはこの 2 点で、skills から `orchestrator` や `mcp_gateway` を import することは契約上は禁止されていない。実際 `skills/knowledge_deliver/skill.py` は関数内で `orchestrator.factory._build_search_skill` を import している。新しいコードでは上位層への依存を増やさない方が安全。

skills が SDK（boto3・googleapiclient・psycopg）を直接叩かないという規約（CLAUDE.md §3）はコードレビュー上のルールで、import-linter では強制されていない。多くの skill のモジュール docstring が「adapters/ 経由」と明記している。

たとえるなら、adapters は「電源コンセント」、skills は「家電」、runtime は「配電盤」。家電はコンセントに挿すが、コンセントが家電を呼ぶことはない。

## Skill の契約（`skills/base.py`）

```python
@register
class MySkill(BaseSkill[MyInput, MyOutput]):
    name = "my_skill"
    description = "..."
    input_schema = MyInput
    output_schema = MyOutput

    def run(self, input: MyInput, ctx: SkillContext) -> MyOutput: ...
```

- `BaseSkill` のサブクラスは `name`・`description`・`input_schema`・`output_schema`（Pydantic v2 の BaseModel）を必ず持つ。`run` は同期メソッド。
- 任意メタ: `version`（既定 "1.0"）、`owner`、`required_scope`（現状は MCP 境界で照合しない足場）、`audit_tag`、`mcp_relay_fields`（MCP へ返す欄を絞る。None は全体）。
- `cleanup_output(output)` は配送・JSON 化の後に一時成果物を消す任意 hook。MCP gateway は `model_dump` の直後に必ず呼ぶ。
- `SkillRegistry.register` は `@register` デコレータから呼ばれ、**同名の重複登録で ValueError**。`get` は未登録名で KeyError。`_clear` はテスト専用。
- docstring 例では `@register("my_skill")` と書かれているが、実装の `register` はクラスだけを受け取る（名前は `name` クラス変数から取る）。例とシグネチャが食い違っている点に注意。

### SkillContext と metadata の印

`SkillContext` は `request_id`（既定 `req-<12hex>`）・`user_id`・`metadata` を持ち、`bind_logger(skill)` で request_id・skill・user_id を束縛した structlog ロガーを返す。metadata には次の印が立つことがある。

| キー | 立てる場所 | 意味 |
|---|---|---|
| `orchestrated_tool_call`（`ORCHESTRATED_METADATA_KEY`） | `orchestrator/sdk_runner` | L2 オーケストレーター内の中間ステップ。Slack へのファイル投下など取り返しのつかない副作用を止める判定に使う（`is_orchestrated_call`） |
| `async_job_poll`（`ASYNC_JOB_POLL_METADATA_KEY`） | `mcp_gateway/async_job_notify` の見張り | status 照会の経路。課金を伴う補完（Apify 等）を起こさない |

本人情報（`user_email` など）も MCP gateway が metadata に詰める（[MCP gateway](mcp-gateway.md)）。

## 段階公開の allowlist（`skills/_shared/rollout.py`）

`rollout_allowed(env_name, user_email)` はカンマ区切り email 一覧の判定で、**未設定・空なら全員許可**。一方、`mcp_gateway` の切り離し・直接投稿・本人メモの allowlist は **空なら誰にも適用しない**逆の意味を持つ。同じ「ALLOWED_EMAILS」でも意味が逆なので、env を設定するときはどちらの実装が読むかを確かめること。

## prompt のファイル化

prompt は `src/teamagent/prompts/<skill>/<version>/<name>.md` に置き、`prompts/loader.py` の `load_prompt(skill, version, name)` で `importlib.resources` 経由で読む。版はディレクトリで分かれており（例: `prompts/search/` に `v1`・`v2`・`v2c`・`v2d`・`v2e`）、どの版を使うかは各 skill のコードが決める。コード内に prompt の文字列リテラルを書かないのがルール（CLAUDE.md §3）。

## 構造化ログ

`observability/logging_config.py` の `configure_logging()` は、env `STRUCTLOG_FORMAT=json` のときだけ structlog を JSONRenderer にする（既定は人間可読の Console）。CloudWatch の metric filter は `{ $.cost_usd = * }` のような JSON セレクタ前提なので、本番で JSON にしないとコスト・エラーのアラームが永久に発火しない。MCP HTTP サーバ（`scripts/run_mcp_http_server.py`）と旧 Slack bot（`runtime/slack_bot.py`）が起動時に呼ぶ。INFO 未満は捨てる。

ログに生入力（メール本文・顧客名・会話・email）を入れない、`cost_usd` というキー名を新しく使うとコストアラームを二重計上させる、といった運用上の注意は [観測・利用記録・コスト管理](../operations/observability-and-cost.md) を参照。

## 補助: 旧 router（`skills/router.py`）

mention テキストから検索戦略（meta / conditional / compare / content）を rule-based で決め、低確信時だけ Haiku にフォールバックする。本番では OpenClaw の agent loop がツール選択を担うため比重は低い（legacy / 補助）。業界キーワードは `ingest/industry_taxonomy.py` を唯一の真実源として共有する。

## 新しい Skill を足すとき

1. `skills/<name>/schema.py` に入出力モデル、`skill.py` に `@register` 付きクラス。
2. prompt は `prompts/<name>/v1/*.md`。
3. 外部呼び出しは `adapters/` に置き、テストでフェイクに差し替えられるようにする。
4. MCP に出すには factory・OpenClaw toolFilter・本番 env の段が要る → [ツール登録と機能フラグ](tool-registry-and-feature-flags.md)。
5. `mypy --strict`・`ruff`・`lint-imports` を通す → [テストの走らせ方と CI](../testing/running-tests.md)。
