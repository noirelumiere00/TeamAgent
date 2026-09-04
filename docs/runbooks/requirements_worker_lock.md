# `requirements-worker.lock` 再生成 Runbook

## 役割

`requirements-worker.lock` は EC2 常駐 bot のデプロイ専用の、hash 固定済み pip
requirements である。`scripts/deploy_to_ec2.sh` は Python 3.11 venv で
`pip install --require-hashes --only-binary=:all:` を使ってこのファイルを導入する。

Fargate の media worker はこのファイルを使わず、Docker build 時に `uv.lock` を使う別経路で
ある。両者を混同して片方だけを更新しないこと。

## 再生成

`uv.lock` を更新してレビューした後、リポジトリルートで実行する。

```bash
uv export --frozen --no-emit-project --no-annotate \
  --extra mcp --extra embeddings --extra media > requirements-worker.lock
```

これは project の core 依存に、EC2 bot が利用する `mcp`、ローカル埋め込み用の
`embeddings`、media 機能用の `media` を加えた集合を出力する。`dev` extra は本番 bot に
不要なので含めない。`--frozen` は export 中に `uv.lock` を解決し直さないため、先に lock の
変更を確定させる必要がある。生成ファイルは手編集しない。

対象は Python 3.11 の EC2 bot（t4g.medium / arm64）である。export には
`--python-platform` を付けず、universal な出力として platform marker を保持する。従って
arm64/Linux に必要な分岐を削らない。

## torch とバイナリ導入の注意

Linux では `torch==2.13.0+cpu` を `pytorch-cpu` index から選ぶ。`+cpu` は local version
なので、通常の PyPI の `torch==2.13.0` と同一視しない（非 Linux 用の marker 付き行も
lock には残る）。EC2 では source build を許可しないため、必ず lock の hash を保ったまま
`--require-hashes --only-binary=:all:` で導入する。新しい wheel が必要な場合は、index と
`uv.lock` を更新して export し直す。hash を手で足したり `--only-binary` を外したりしない。

## `claude-agent-sdk` を含めない理由

core runtime は Bun/JS runtime を同梱する `claude-agent-sdk` を採用していない。
`src/teamagent/orchestrator/sdk_runner.py` は Python の `anthropic.AsyncAnthropicBedrock` を
使う bounded tool loop であり、EC2 の `teamagent.runtime.slack_bot` 起動経路にも
`claude_agent_sdk` import はない。そのためこの lock に SDK を戻さない。

## 依存更新時の手順

1. `pyproject.toml` を変更し、通常の依存更新手順で `uv.lock` を更新・レビューする。
2. 上記コマンドで `requirements-worker.lock` を再生成する。
3. `git diff -- requirements-worker.lock` を確認し、必要なら hash 形式 gate を実行する。
4. EC2 リリースでは通常どおり新しい Terraform saved plan を作成してレビューする。

`infra/terraform/hmac_worker_deploy.tf` は `runtime_lock` を `filesha256` で計画へ取り込み、
`scripts/hmac_rollout_gate.py` は saved plan の値と実ファイルを照合する。ロック digest を
別途ハードコードして更新する箇所はない。ただし、ファイル更新後に以前の saved plan を再利用
してはならない。
