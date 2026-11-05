あなたは teamagent リポジトリの開発者。作業場所はこの worktree（branch chore/python315-survey-20261030）。日本語で報告する。
docs/python315_migration_plan_20261030.md（あなたが前回書いた計画）の「段 1」を実装する: CI で Python 3.15 を検証できる状態にし、3.15 の wheel が無い依存を、3.15 の wheel がある版へ上げる。
1. uv.lock の 2.1 節の 16 件（特に psycopg-binary 3.3.4）について、`uv lock --upgrade-package <name>` 等で 3.15（cp315 / abi3）の Linux arm64・x86_64 の wheel がある版へ上げる。PyPI からの取得はパッケージ管理の通常操作として可。上げられないものは理由を書く。3.14 での既存の挙動を壊さないこと（メジャー更新は避け、必要なら報告して止める）。
2. CI（.github/workflows/ci.yml）に Python 3.15 の pytest ジョブを「失敗しても全体を落とさない（continue-on-error）」形で 1 つ足す（既存の 3 分割の流儀に合わせる）。依存の入れ方は既存ジョブと同じ（--no-deps＋手動列挙の方式があればそれに従う）。
3. 手元で `uv run --python 3.14 --extra dev --extra mcp --extra media pytest -q -n auto` が通ること（3.15 が手元にあれば 3.15 でも回し、結果を報告）。ruff・mypy も。
4. media（Alpine の APK・Dockerfile・世代）にはまだ触れない。
禁止: git commit・push、AWS の実行、.env・secrets の読み取り。最後に上げた版の一覧・CI の変更・テスト結果・残る停止要因を短くまとめる。
