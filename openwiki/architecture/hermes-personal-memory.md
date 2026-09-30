---
type: architecture
title: 本人メモ（Hermes 覚える係）
description: 1 対 1 DM の発話から「本人に合う返事のためのメモ」を学習する仕組み。MCP 側の personal_memory（gate・buffer・learner・service・guard）、専用ロールと RLS の 3 表、TLS 必須の Hermes 学習サービス（hermes_runtime）の役割と安全策。
tags: [personal-memory, hermes, mcp-gateway, rls, security, privacy]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T07:51:05.076Z
sources:
  - id: openwiki-source-c43516a29725cad2c0495750
    resource: repo://hermes_runtime/README.md
  - id: openwiki-source-644f9f97a358f08cab41cb0f
    resource: repo://infra/migrations/0029_personal_memory.sql
  - id: openwiki-source-e0f39073bee95c9fd3564c02
    resource: repo://src/teamagent/adapters/hermes_learn_client.py
  - id: openwiki-source-09f5a21cd8887f41f025dc31
    resource: repo://src/teamagent/mcp_gateway/personal_memory/buffer.py
  - id: openwiki-source-c2352df6e6f680ba0f2bb8ae
    resource: repo://src/teamagent/mcp_gateway/personal_memory/gate.py
  - id: openwiki-source-f98ad5704ccb42610cff9dc3
    resource: repo://src/teamagent/mcp_gateway/personal_memory/learner.py
  - id: openwiki-source-fbdfc45e0977863209a3f697
    resource: repo://src/teamagent/mcp_gateway/personal_memory/service.py
  - id: openwiki-source-1fdde611c13aba4b68a5ff42
    resource: repo://src/teamagent/mcp_gateway/server.py
  - id: openwiki-source-b4ef73e5364917d29ae9a222
    resource: repo://src/teamagent/personal_memory/guard.py
generated: { by: "claude-code", at: "2026-09-29T07:51:05.076Z" }
---

# 本人メモ（Hermes 覚える係）

## 何をする機能か

Aico と利用者の **1 対 1 DM だけ**を対象に、返事の長さや言い回しの好み・よく扱う資料の型などを 200 字以内の短いメモとして覚え、次の返事の前に読み込む機能。返事そのものは今までどおり OpenClaw（Haiku）が作り、Hermes は「覚える内容を整理する係」でしかない。

コードはすべて **既定 OFF**（`USE_PERSONAL_MEMORY`）で、対象者も `PERSONAL_MEMORY_ALLOWED_EMAILS` の allowlist（**空・未設定なら全員拒否**）で絞る。利用者向けの運用ガイドは `docs/architecture/personal_memory_v1.md`、段階計画は `docs/architecture/hermes_implementation_plan.md`。

たとえるなら、秘書が会話を録音するのではなく、「この人は短い返事が好き」と付箋を 1 枚ずつ書き、本人がいつでも剥がせる仕組み。

## 構成

| 層 | 場所 | 責任 |
|---|---|---|
| 入口の門 | `src/teamagent/mcp_gateway/server.py` `dispatch_personal_memory_tool` + `mcp_gateway/personal_memory/gate.py` | 3 ツール（`personal_memory_observe` / `_context` / `_command`）の受付可否 |
| 処理 | `mcp_gateway/personal_memory/service.py` | observe（発話を溜める）・context（返信前に読む）・command（本人操作） |
| 揮発状態 | `mcp_gateway/personal_memory/buffer.py` | 発話バッファ・1 日の回数枠・同時実行枠・掃除スレッド |
| 学習ジョブ | `mcp_gateway/personal_memory/learner.py` | Hermes 呼び出しと差分の反映 |
| 安全検査 | `src/teamagent/personal_memory/guard.py` | 保存前・読み出し時の純粋な検査（IO なし） |
| 保存 | `src/teamagent/adapters/personal_memory_store.py` + `infra/migrations/0029_personal_memory.sql` | 専用ロールでの読み書き |
| 学習係 | `hermes_runtime/aico_hermes/` + `src/teamagent/adapters/hermes_learn_client.py` | TLS の `POST /v1/learn` |

## 入口の門（fail-closed の順序）

本人メモの 3 ツールは **`list_tools` に出さない**。`build_server` は `personal_memory*` が ToolSpec として登録されていたら起動時に `RuntimeError` で止まる。フラグ OFF なら未登録ツールと同じ応答を返し、claim の nonce も DB も触らない。通常の `dispatch_tool` は通らず、「連携」振り替え・usage 記録・進捗投稿・長文退避・非同期通知も走らない。

ON のときの判定順:

1. resolver と caller claim 検証器が揃っている（無ければ `PM_UNAVAILABLE`）。
<!-- openwiki: broken internal link [/openwiki/architecture/caller-identity-and-button-bindings.md] link "/openwiki/architecture/caller-identity-and-button-bindings.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
2. 署名済み caller claim の検証（[呼び出し元の証明](/openwiki/architecture/caller-identity-and-button-bindings.md)）。
3. claim の `channel_id` が `D[A-Z0-9]{8,}` に fullmatch（申告値は見ない。strip もしない）。
4. `tool_call_id` が `aico-pm-(obs|ctx|cmd)-<32hex>` に一致し、kind がツール名と合い、`run_id == tool_call_id`（plugin の直接呼び出しの印。モデル経由の呼び出しを通さない）。
5. スレッド内の発話は拒否。
6. Slack identity 解決（RLS 必須）と allowlist、入力検証。

例外は外へ出さない（MCP SDK が `str(e)` を応答に入れるため）。

## 学習の流れ

1. **observe**: 発話を `guard.check_utterance` で検査し、告知済み・active の人だけ MCP プロセスのメモリ上のバッファに入れる。発話は永続化もログ出力もしない。
2. 1 人 5 発話、または最初の発話から 480 秒で掃除スレッドが取り出し、学習ジョブを起動する。1 人 1 日 6 ジョブまで。起動できなければその場で捨てる。
3. **learner**: 本人メモを読み、保存済み項目を guard で再検査して合格分だけを Hermes へ送る → 結果（全量）と送った snapshot の完全一致差分を取る → 削除が 1 ジョブ 5 件（`MAX_REMOVALS`）を超えたらジョブごと捨てる（注入による一括削除の防止）→ 追加候補を `check_entry` で検査 → 件数・合計字数の上限に収める → `apply_learned(expected_version=読んだ版)`。版が進んでいたら（凍結・削除・忘れて）捨てる。
4. **context**: 返信前に読む。1.0 秒で諦めて空を返す（メモなしで現行どおり返事をする）。DB と Hermes は専用の小さいスレッドプールで動かし、重いツールで MCP 既定の executor が詰まっても 1.0 秒を守る。
5. **command**: 一覧・「○番を忘れて」・止めて（凍結）・再開・全部消して（10 分以内の確認が必要）。

プロセス内状態がそのまま系全体の状態になるのは、mcp が uvicorn の単一プロセス・ECS `desired_count=1` であることが前提（`buffer.py` の注記）。タスクを複数に増やす変更をするときはこの前提が崩れる。

## guard（保存してよいか）

`guard.py` は入力を保存せず外部 IO も持たない純関数群。メモ 1 項目は 200 字以内、学習元の発話は 800 字以内。拒否する主なもの: 不可視文字、prompt injection のパターン、秘密情報のパターン、メールアドレス、電話番号、URL、8 桁以上の数字、発話からの 25 字以上の逐語、敬称・役職で判定した人名（社内の在籍者名簿 `member_names` にある同僚だけは許す）、引用・転送・コードブロック・添付。

## 保存（0029 migration）

`personal_memory_profiles` / `personal_memory_entries` / `personal_memory_audit` の 3 表。既存の RLS 表と違い、次の構造で守る。

- **admin 例外を入れない**（他の表にある `OR app.user_role='admin'` を写さない）。
- 表を読み書きできるのは専用の `personal_memory_app` ロールだけ。migration 実行者（master）には `INHERIT FALSE` で付与し、master 直結・`teamagent_app`・ダッシュボード用ロールからは表の権限そのものが無い。master が BYPASSRLS を持っていても読めない。
- 表と関数の所有者は `NOLOGIN NOBYPASSRLS` の `personal_memory_definer`。FORCE RLS が所有者にも効く。他人の行に届くのは SECURITY DEFINER 関数 2 本（管理者閲覧・退職削除）だけで、閲覧は「監査 INSERT → 行を返す」を 1 関数で行う（監査に失敗したら 1 行も返らない）。
- `ON CONFLICT` も `RETURNING` も使わない（最小権限ロールで失敗する既知の地雷）。
- 既存ロールが危険な属性（superuser・login・bypassrls 等）を持っていたら migration ごと止める。

<!-- openwiki: broken internal link [/openwiki/data/rls-and-app-role.md] link "/openwiki/data/rls-and-app-role.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
以後この 3 表を変える migration は、冒頭で definer を一時付与して `SET ROLE` し、最後に戻す手順が必要（migration 冒頭のコメント参照）。一般的な RLS の話は [RLS と実行ロール](/openwiki/data/rls-and-app-role.md)。

## Hermes 学習係（hermes_runtime）

`hermes_runtime/` は TeamAgent の wheel に入らない独立パッケージ。

- `server.py`: TLS 必須の HTTP サーバ（ポート 8790）。`GET /healthz`、`POST /v1/learn`（ingress bearer 必須・同時 2 件・超過は 503）。応答ごとに接続を閉じる。
- `schema.py`: 入力検査。email・user_id・name など未知のキーはすべて拒否。
- `runner.py` / `child.py`: ジョブごとに 0700 の使い捨て `HERMES_HOME` を作り、子プロセスで学習して必ず消す。子に渡す env は許可リストのみ。memory 以外のツールが読み込まれていたらモデル呼び出し前に止める。Hermes は API 失敗でも例外を投げないので、結果の状態で失敗を判定する。
- Bedrock は mcp と同じ `anthropic_messages` 経路・非ストリーミング（IAM が InvokeModel のみのため）。

MCP 側クライアント（`hermes_learn_client.py`）は `HERMES_SERVICE_URL`・`TEAMAGENT_HERMES_INGRESS_BEARER`・`HERMES_TLS_CA_PEM` を env から読み、自己署名証明書をトラストアンカーにしてホスト名まで検証する。プロキシ設定は読まない（`trust_env=False`）。再試行は送信前の接続失敗に 1 回だけ、503 はそのジョブを捨てる。入出力の上限値は `hermes_runtime/aico_hermes/schema.py` と複製しており、一致は契約テストで固定する。

## 現在の状態と確認すべき点

- mcp 側のコードとテストは揃っているが、本 worktree の `infra/terraform/` には Hermes サービスの定義が見当たらず、`infra/openclaw/caller-identity-plugin/dist/index.js` にも `aico-pm-` 形式の呼び出しは見当たらない。したがってこのリポジトリの現状では、本人メモは**未配線（dark）**と読むのが妥当。点灯の手順と前提は `docs/architecture/hermes_implementation_plan.md` の M 系列を参照。
- CI の ruff・mypy・bandit は `hermes_runtime/` を対象にしていない（`hermes_runtime/README.md` の注記）。

## テスト

- `tests/mcp_gateway/test_personal_memory_*.py`（buffer・commands・constants・context・gate・learner・log_contract・sdk_path）
- `tests/personal_memory/`（guard）
- `tests/hermes_runtime/`（本物の Hermes は使わず子プロセスを小さなスクリプトで代用し、HTTP は本物のソケットで叩く）: `uv run --extra dev --extra mcp pytest tests/hermes_runtime`
