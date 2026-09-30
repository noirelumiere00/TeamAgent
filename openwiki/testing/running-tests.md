---
type: testing
title: テストの走らせ方と CI
description: GitHub Actions の CI（lint-and-test・uv.lock 固定版 pytest・activation freeze・gitleaks・trivy・terraform validate）の中身と、ローカルで CI と同じ extras（dev/mcp/media）・Node 依存・使い捨て PostgreSQL（TEAMAGENT_TEST_DB_DSN と teamagent_app ロール）でテストを走らせる方法、tests/ の配置。
tags: [testing, ci, pytest, github-actions, postgres, uv, lint]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T07:51:05.076Z
sources:
  - id: openwiki-source-164e2da859b5277df81c7d94
    resource: repo://.github/workflows/ci.yml
  - id: openwiki-source-12ddcdede0a5fc289016673e
    resource: repo://infra/deploy/activation_freeze_check.py
  - id: openwiki-source-edd2519005c1fcea63bf18bc
    resource: repo://scripts/setup_worktree.sh
  - id: openwiki-source-1fdde611c13aba4b68a5ff42
    resource: repo://src/teamagent/mcp_gateway/server.py
  - id: openwiki-source-5101c5c354e748eb58f867f7
    resource: repo://tests/adapters/test_oauth_tokens_rls.py
  - id: openwiki-source-cbc888d15cb6cd7c61ce3453
    resource: repo://tests/adapters/test_pgvector_schema.py
  - id: openwiki-source-f0a6e7dc03522b2682f88655
    resource: repo://tests/conftest.py
  - id: openwiki-source-70ca3fb43546ebab594e3e84
    resource: repo://tests/infra/test_dockerfile_teamagent_media_worker.py
  - id: openwiki-source-1535e213a8279c40163d01d2
    resource: repo://tests/ingest/test_ingest_differential_postgres.py
  - id: openwiki-source-341b964ce489dab98ec97e18
    resource: repo://tests/ingest/test_ingest_source_health_postgres.py
  - id: openwiki-source-5e891471c3a889284f914c50
    resource: repo://tests/personal_memory/conftest.py
  - id: openwiki-source-728d0ecd130a24f548e9a0af
    resource: repo://tools/tiktok_scraper/dns_pinned_proxy.mjs
generated: { by: "claude-code", at: "2026-09-29T07:51:05.076Z" }
---

# テストの走らせ方と CI

## 要点

- テストと静的検査の CI は `.github/workflows/ci.yml` の 1 本。`main` と `dev` への push と、両ブランチ宛ての PR で動き、同じ ref で新しく push されると古いランは取り消される。
- pytest は 2 通りの依存で走る。**最新版の依存**（`lint-and-test`・Python 3.11/3.13/3.14）と、**本番と同じ `uv.lock` の固定版**（`lockfile-pytest`・3.14）。
- ローカルでは最低 `--extra dev --extra mcp`、media 系を触るなら `--extra media` も入れ、TikTok 系のために `npm ci --prefix tools/tiktok_scraper` を実行する。extras が足りないと、CI では出ない収集エラーになる。
- DB テストは 2 つの env で出し分けている。CI が設定するのは `TEAMAGENT_TEST_DB_DSN` だけなので、`TEAMAGENT_DB_DSN` で守られたテストは **CI では常に skip** される。

## CI のジョブ構成

| ジョブ | 何を検査するか | 落ちる条件 |
|---|---|---|
| `lint-and-test` | ruff（lint と format チェック）・`lint-imports`・OpenClaw 設定の不変条件・`mypy src/teamagent`（strict）・pytest（カバレッジ付き）・bandit | どれか 1 つでも失敗。3 つの Python は `fail-fast: false` で別々に結果が出る |
| `lockfile-pytest` | `uv.lock` から書き出した依存をハッシュ検証つきで入れて pytest | 固定版でだけ壊れるテスト |
| `activation-freeze` | PR の差分が generation freeze の対象パス（frozen surface）を触っていないか | 同じ PR に unlock 宣言がないまま対象パスを変更した |
| `gitleaks` | 作業ツリーの秘密情報スキャン（公式 CLI イメージ・GitHub API 不使用） | 秘密情報らしきものを検出 |
| `trivy` | 依存の脆弱性（fs）と `infra/` の IaC 設定ミス（config） | CRITICAL/HIGH。修正版が無い CVE は無視、例外は `.trivyignore.yaml` |
| `terraform` | provider lock の再生成が差分ゼロか、オフラインのミラーから `init -backend=false` して `terraform validate` | lock ファイルのずれ・validate 失敗 |
| `smoke-test` | `main` への push 後だけ動く枠。現状は echo するだけ | ― |

### lint-and-test の手順と意味

1. `pip install --no-deps -e .` のあと、テストと lint に必要なパッケージを**手で列挙して**入れる（版は固定しないので最新が入る）。重い依存（torch・playwright・weasyprint・yt-dlp など）はこの列挙に入っていない。
2. 使い捨ての PostgreSQL に `teamagent_app` ロールを用意する（後述）。
3. `ruff check` と `ruff format --check`（対象は `src/ tests/ scripts/`）。
<!-- openwiki: broken internal link [/openwiki/architecture/layering-and-skill-contract.md] link "/openwiki/architecture/layering-and-skill-contract.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
4. `lint-imports`: `pyproject.toml` の `[tool.importlinter]` にある 2 本の契約（adapters は上位層を import しない・skills は runtime を import しない）。中身は [層分離と Skill 契約](/openwiki/architecture/layering-and-skill-contract.md)。
5. `python scripts/check_openclaw_config.py`: `infra/openclaw/openclaw.config.json5` の `channels.slack` の `dmPolicy` / `groupPolicy` / `allowFrom` の矛盾（例: `dmPolicy:"open"` なのに `allowFrom` に `"*"` が無い）を弾く。標準ライブラリだけで動く。
6. `mypy src/teamagent`（`[tool.mypy] strict = true`）。CI に入れない依存は `ignore_missing_imports` の override に列挙してある。
7. `npm ci --prefix tools/tiktok_scraper`、続けて `pytest tests/ -q --cov=teamagent`。
8. `bandit -r src/teamagent -ll`。`-ll` は MEDIUM 以上だけを報告する指定で、行末コメントの「low+」とは一致しない。

Terraform CLI もこのジョブで入れている。Terraform の `jsonencode` の結果を照合するテストなどが CLI を使い、無い環境では skip になる。

### lockfile-pytest（uv.lock 固定版）

`lint-and-test` は依存の最新版を検証するので、本番イメージが使う `uv.lock` の版とずれることがある（anyio の版が lock と違っていた件の再発防止としてこのジョブが加わった）。このジョブは `uv export --frozen --no-emit-project --extra dev --extra mcp --extra media` で requirements を書き出し、`pip install --no-deps --require-hashes` で入れてから `pip install --no-deps -e .` する。`pip freeze` を出力に残すので、どの版で走ったかはログで確かめられる。カバレッジと lint はこのジョブでは取らない。embeddings extra（torch）はどちらのジョブにも入らない。

### activation-freeze

<!-- openwiki: broken internal link [/openwiki/operations/release-gates-and-deploy.md] link "/openwiki/operations/release-gates-and-deploy.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
`infra/deploy/activation_freeze_check.py assert-frozen-surface` が PR の merge-base から HEAD までの変更パスを、`infra/deploy/buildspec_generation_inputs.json` の `inputs` と freeze 宣言の `additional_publisher_paths` を合わせた集合と突き合わせる。freeze の state が `pending_v2` か `active` のときは、同じ PR の `infra/deploy/activation_freeze.json` で unlock を active にし、`scope_paths`・`reason`・`gate` を書かない限り失敗する。`scope_paths` は実際の変更と過不足なく一致しなければならない（範囲外の変更も、変更していないパスを unlock に含めるのも失敗）。checker は AWS に一切アクセスしない。背景は [リリースゲートとデプロイ](/openwiki/operations/release-gates-and-deploy.md)。

## ローカルで CI と同じ依存で走らせる

```bash
# 依存（uv.lock に従う。media 系を触らないなら --extra media は省ける）
uv sync --extra dev --extra mcp --extra media
# TikTok スクレイパの Node 依存（CI と同じ）
npm ci --prefix tools/tiktok_scraper --no-audit --no-fund
# 全体、または一部
uv run --extra dev --extra mcp --extra media pytest tests/ -q
uv run --extra dev --extra mcp pytest tests/hermes_runtime -q
```

注意点:

- **mcp extra は必須に近い**。`src/teamagent/mcp_gateway/server.py` が `mcp` をトップレベルで import しており、これを読むテスト（例: `tests/test_mcp_gateway_server.py`）が多数ある。`scripts/setup_worktree.sh` は `uv pip install -e ".[dev]"` しか入れず（`uv.lock` も使わない）、`scripts/setup_local.sh` も同じなので、これらで作った環境のままでは mcp 系が収集エラーになる。
- **npm ci が要る理由**: `tools/tiktok_scraper/dns_pinned_proxy.mjs` が `ipaddr.js` を import しており、`tests/infra/test_dockerfile_teamagent_media_worker.py` がこのファイルを `node` で実際に動かす。`package.json` の engines は `node >=24 <25`。CI には `setup-node` の手順が無く、runner に最初から入っている Node を使う。
- **lint ツールの版**: CI は `ruff==0.15.15` に固定しているが、dev extra は `ruff>=0.7.0` の下限だけ、`.pre-commit-config.yaml` の ruff は v0.8.0 なので、フォーマット結果が CI とずれることがある。`import-linter` と `bandit` は dev extra に入っていないので、ローカルで `lint-imports` や `bandit` を走らせるには別途入れる。
- **実描画のテスト**: `tests/skills/video_algorithm/chromium.py` は playwright と chromium が無ければ skip する。CI の `lint-and-test` には playwright が無く、`lockfile-pytest` もブラウザ本体は入れないので、実描画の確認は merge 前に手元で行う前提になっている（`CHROMIUM_PATH` で実行ファイルを指定できる）。
<!-- openwiki: broken internal link [/openwiki/operations/container-images-and-build.md] link "/openwiki/operations/container-images-and-build.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- ほかに、`jq`・`openssl`・`node`・Terraform の archive provider が無い環境や、Linux の procfs が無い環境（macOS）で skip するテストがある。`OPENCLAW_RUNTIME_TEST_IMAGE` を設定したときだけ動く、ビルド済みイメージの契約テストもある（[コンテナイメージとビルド](/openwiki/operations/container-images-and-build.md)）。

## PostgreSQL を使うテスト

| env | 設定される場所 | 期待する DB | 使うテスト |
|---|---|---|---|
| `TEAMAGENT_TEST_DB_DSN` | CI の 2 つの pytest ジョブ（service コンテナ `postgres:16-alpine`・DB `teamagent_test`） | 使い捨て。テストが schema・ロール・DATABASE を作って消す | `tests/ingest/*_postgres.py`・`tests/personal_memory/`（`conftest.py` の `pm_db` fixture） |
| `TEAMAGENT_DB_DSN` | CI では設定しない | migration 済みの実 DB（go-live ゲート用） | `tests/adapters/test_pgvector_schema.py`・`test_oauth_tokens_rls.py`・`test_usage_metrics_migrations.py` の動的検証 |

`TEAMAGENT_DB_DSN` 側の 3 ファイルは、静的検証（SQL ファイルの中身の契約）だけが CI で走り、実 DB で RLS やスキーマを確かめる部分は常に skip される。RLS や migration を変えたときは、ローカルかトンネル越しの DB でこの env を与えて手で走らせる必要がある。

### teamagent_app ロール

<!-- openwiki: broken internal link [/openwiki/data/rls-and-app-role.md] link "/openwiki/data/rls-and-app-role.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
CI の `Prepare disposable PostgreSQL test role` は、`teamagent_app` を `NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOLOGIN INHERIT` で作り、`GRANT teamagent_app TO postgres` する。ingest の DB テストは専用 schema を作ったあと、このロールが存在し、接続ユーザーがそのメンバー（`SET ROLE` できる）であることを確かめ、満たさなければ skip する。RLS を bypass しないロールに切り替えてポリシーを試すための準備で、これが無いと skip になり RLS の検証が抜ける（`-ra` の skip 理由に出る）。仕組みは [RLS と実行ロール](/openwiki/data/rls-and-app-role.md)。

`tests/personal_memory/conftest.py` はさらに踏み込み、superuser で migration を流すと SECURITY DEFINER と RLS が偽の緑になるとして、本番の master に似せた「CREATEROLE・BYPASSRLS・非 superuser」の migrator ロールと使い捨て DATABASE をモジュールごとに作り、そこで `0029_personal_memory.sql` を流す。終わると `DROP DATABASE ... WITH (FORCE)` とロール削除で片付ける。

### pgvector の有無

CI の `postgres:16-alpine` には pgvector が無い。`tests/ingest/test_ingest_differential_postgres.py` の end-to-end（`chunks.embedding` が `vector(1024)`）は `CREATE EXTENSION vector` に失敗すると skip し、同じロジックはフェイクを使う `test_ingest_differential.py` が全環境で検証する。`test_ingest_source_health_postgres.py` は `vector` を配列の DOMAIN で代用して拡張なしで走る。

### ローカルで DB テストを走らせる

1. `docker compose -f infra/docker/docker-compose.yml up -d`（`pgvector/pgvector:pg16`。`scripts/setup_local.sh` もこれを起動する）。
2. CI の `Prepare disposable PostgreSQL test role` と同じ SQL を流し、`GRANT teamagent_app TO <DSN のユーザー>` する。
3. `TEAMAGENT_TEST_DB_DSN=postgresql://<user>:<pass>@localhost:5432/<db> uv run --extra dev --extra mcp pytest tests/ingest tests/personal_memory -q`

<!-- openwiki: broken internal link [/openwiki/data/postgres-schema-and-migrations.md] link "/openwiki/data/postgres-schema-and-migrations.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
`TEAMAGENT_TEST_DB_DSN` は**使い捨ての DB 以外に向けない**。テストはロールと DATABASE を作成・強制削除し、ロール作成権限も要求する。スキーマ全体は [PostgreSQL スキーマとマイグレーション](/openwiki/data/postgres-schema-and-migrations.md)。

## tests/ の配置

`pyproject.toml` の `[tool.pytest.ini_options]` は `testpaths = ["tests"]`・`asyncio_mode = "auto"`・`--strict-markers --strict-config`。ディレクトリは概ね `src/teamagent` の構成に沿う。

| 場所 | 中身 |
|---|---|
| `tests/skills/` | skill ごとのサブディレクトリと、intent・router・ツール description の契約テスト（最大の塊） |
| `tests/adapters/` | 外部 SDK ラッパ（Gemini・TikTok スクレイパ・pgvector スキーマ・oauth_tokens の RLS など） |
| `tests/mcp_gateway/` とトップの `tests/test_mcp_gateway_*.py`・`test_openclaw_*.py`・`test_hmac_*.py` | ゲートウェイ・caller claim・ボタン束縛・HMAC 鍵束 |
| `tests/ingest/`・`tests/personal_memory/`・`tests/orchestrator/`・`tests/runtime/`・`tests/media/` | 各サブシステム。`*_postgres.py` が実 DB テスト |
| `tests/scripts/`・`tests/infra/`・`tests/codebuild/` | スクリプト・Dockerfile・Terraform・CodeBuild の供給網契約（文字列やハッシュの固定） |
| `tests/hermes_runtime/` | wheel に入らない `hermes_runtime/` を `sys.path` に足して読む |
<!-- openwiki: broken internal link [/openwiki/testing/routing-and-eval.md] link "/openwiki/testing/routing-and-eval.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
| `tests/routing/`・`tests/eval/` | ルーティングコーパスと gold set（[ルーティングと評価](/openwiki/testing/routing-and-eval.md)） |

`tests/conftest.py` の autouse fixture は、受信箱スキャンのキャッシュ・下書きの日次上限カウンタ・HMAC ローテーションの時計状態・お土産資料の同時実行枠を各テストの前後でリセットする。これらは「本番は呼び出しごとに Skill を作り直す」ためにプロセス内に置いた状態で、テストは 1 プロセスなので消さないと次のテストへ漏れる。同じ種類のプロセス内状態を足したら、ここにリセットを足す。

## 変更するときの確認

- 依存を足したら、`lint-and-test` の手動列挙にも足す（`--no-deps` なので自動では入らない）。stub が無ければ `[tool.mypy]` の override にも足す。
- RLS・migration を変えたら、`TEAMAGENT_DB_DSN` のテストを手元で走らせる（CI では確かめられない）。
- `infra/deploy/` の generation inputs を触る PR は、`activation-freeze` の unlock 宣言が要るかを先に確かめる。
