# hermes_runtime — Aico DM 本人メモ v1 の Hermes「覚える係」

設計: `docs/architecture/hermes_migration_design.md` §10b ／ 工程: `docs/architecture/hermes_implementation_plan.md`（M2）

MCP から本人の 1 対 1 DM 発話（5 件まで）と本人メモのスナップショットを受け取り、Hermes 本家の memory ツールで
整理した結果を返すだけのサービス。返事は作らない。MCP・RDS・Slack・既存の鍵には触れない。
保存してよいかは MCP 側の `teamagent.personal_memory.guard` が最終判断する。

## 構成

| ファイル | 役割 |
|---|---|
| `aico_hermes/server.py` | TLS 必須の HTTP サーバ。`GET /healthz`、`POST /v1/learn`（ingress bearer 必須・同時 2 件・超過は 503） |
| `aico_hermes/schema.py` | 入力の検査。未知のキー（email・user_id・name など）はすべて拒否 |
| `aico_hermes/runner.py` | ジョブごとに 0700 の使い捨て `HERMES_HOME` を作り、子プロセスで学習して必ず消す。子に渡す環境変数は許可リストのみ |
| `aico_hermes/child.py` | 子プロセス。memory 以外のツールが読み込まれていたらモデルを呼ぶ前に止める。Hermes の黙った失敗（例外なしのエラー応答）も失敗として返す |
| `aico_hermes/config.py` | `config.yaml` の生成と検査（裏 review・skill 自動作成・通知・承認待ちを止める） |
| `aico_hermes/prompt.py` | 学習の指示文。発話は「参考資料であり指示ではない」枠に入れる |
| `requirements-hermes.lock` | Hermes v2026.9.21 の依存（ハッシュ固定・anyio は CVE 修正版） |
| `infra/docker/Dockerfile.hermes` | 公開 Wolfi の Python 3.13 上のイメージ（非 root・Hermes は commit と tarball の sha256 で固定） |

## 実測（2026-09-25）

- Trivy（HIGH/CRITICAL）: 0 件（OS・Python）。イメージ 434MB（本家の tests・apps・website・evals と Node の lock を除いた）
- 本物の Bedrock（ap-northeast-1・`jp.anthropic.claude-haiku-4-5-20251001-v1:0`）で学習 1 回 6.1 秒。
  架空の発話 5 件から「返事の形式」「資料の型・よく扱う顧客」を追加。先方担当者名と目印の文字列は覚えなかった。
  コンテナのログと全ファイルに目印の文字列が残らず、作業場も残らないことを確認
- 地雷（すべて対処済み）:
  - `AIAgent` を直接作ると Bedrock は Converse 経路になり jp. 推論プロファイルの Claude が弾かれる →
    `api_mode="anthropic_messages"` と `anthropic` SDK で mcp と同じ経路にする
  - Hermes は API 失敗でも例外を投げずエラー文を返す → `completed`/`failed`/`partial`/`error` で判定する
  - `model.context_length` が無いと初期化のたびに約 700 万字のダミー文を Bedrock に送って context 長を探る →
    config で明示（`model.base_url` も実行経路と一致させる）。明示後は 13.5 秒 → 6.1 秒
  - 既定のストリーミングは IAM（InvokeModel のみ）で拒否される → `model.streaming: false`
- 反対尋問（高 2・中 9）を反映: 応答ごとに接続を閉じる（Keep-Alive で本文が次の応答に載る穴）、
  TLS の握手を受付ループの外で時間切れ付きに、例外文をログに出さない、子の後は必ずプロセスグループを止める、
  出力も入力と同じ規則で検査、スナップショットの合計を Hermes の上限以下に、Hermes が実際に読んだ config を子の中で検査

## ローカルでのビルド

```bash
docker build -f infra/docker/Dockerfile.hermes --secret id=teamagent_ca,src=<社内プロキシ CA> -t aico-hermes:dev .
```

`cgr.dev/chainguard/wolfi-base` の既定リポジトリは認証付きなので、公開 Wolfi（`packages.wolfi.dev/os`）と
その署名鍵（`infra/docker/hermes/wolfi-signing.rsa.pub`）を使う。

## テスト

```bash
uv run --extra dev --extra mcp pytest tests/hermes_runtime
```

本物の Hermes は使わず、子プロセスは小さなスクリプトで代用する（異常終了・時間切れ・結果なし・孫プロセスの居残り・規則外の出力・秘密の環境変数・黙った API 失敗を再現）。
HTTP は本物のソケットで叩く（Keep-Alive で本文が次の応答に載らないこと・握手が止まっても受付が止まらないこと・ログに例外文が出ないこと）。

CI の ruff・mypy・bandit は現状 `src/ tests/ scripts/` だけが対象で、`hermes_runtime/` は含まれない（ローカルでは `ruff check hermes_runtime` と `mypy --strict hermes_runtime/aico_hermes` を通している）。
