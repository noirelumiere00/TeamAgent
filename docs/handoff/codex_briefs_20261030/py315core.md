あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
docs/python315_migration_plan_20261030.md の次の段（core の Python 3.15 化）を進める。段 1（#757: 依存の更新と CI の 3.15 試験ジョブ）はマージ済み。
1. CI の 3.15 試験ジョブ（continue-on-error）の失敗内容を、手元で再現できる範囲で調べる（3.15 が手元に無ければ uv python install 3.15 を試す）。PyYAML など 3.15 の wheel が無い依存の扱いを決める（ソースビルドで通るか・代替があるか）。
2. core の Dockerfile（infra/docker/Dockerfile.teamagent-mcp）の土台を 3.15 の Chainguard python に替える案を作る。土台の digest は公開レジストリ cgr.dev/chainguard/python から確認（取得はネットワーク可）。CA 証明書・brotli・pip-wheel の差し替え（replace_core_ca_bundle.py）を 3.15 の土台で通るように追随。
3. 世代の再導出が要るので、~/Documents/Claude/Artifacts/aico-inputs-20260911/pipwheel-20261030-run（公開済み・live）を OLD にして py315core-20261030-run を作り、同じ方法で再導出する。fresh を確認。
4. `uv run --python 3.15 --extra dev --extra mcp pytest -q -n auto` が通る範囲を報告（通らないものは理由）。3.14 の既存テストも通ること。ruff・mypy。
禁止: AWS への書き込み・CodeBuild 起動・S3 公開、git commit・push、.env・secrets・~/.aws の読み取り。media（Alpine）はまだ触らない。最後に変更点・世代の新値・3.15 での結果・残る停止要因を短く。
