# 依頼: python 3.15 core 移行（B1）を今の dev に追いつかせる（衝突の解消と世代の再導出）

作業場所: ~/dev/worktrees/teamagent-py315b（branch chore/python315-core-20261105）。
いま origin/dev（c6b73b33 以降）に、10/30 の作業 chore/python315-core-20261030（コミット 15e23e90）を `git merge` した途中で、次の 13 ファイルが衝突している（UU）:
infra/codebuild/teamagent_core_media_release_contract.json・teamagent_runtime_contract.json・infra/deploy/activation_freeze.json・buildspec_generation_inputs.json・supply_chain_adoptions.json・infra/docker/Dockerfile.teamagent-mcp・Dockerfile.teamagent-mcp.dockerignore・core-pip-wheel.lock.json・smoke_core.py・infra/terraform/supply_chain_adopt.tf・tests/infra/test_dockerfile_teamagent_mcp.py・tests/infra/test_replace_core_ca_bundle.py・tests/scripts/test_activation_freeze.py

## 背景
- 10/30 の作業内容と残課題: docs/python315_migration_plan_20261030.md（408 行目付近の記録）。core を 3.15.1 に、CA／Brotli／pip を追随、greenlet 更新で segfault 解消。
- 10/30 以降に dev へ入った core 側の変更: #776（python-3.14 を 3.14.10 へ・CVE）・#805（core の libexpat1 を Wolfi 公式 2.10.0-r0 へ）・#804（media の apk 固定）。**これらのセキュリティ修正を 3.15 側でも保つこと**（libexpat 2.10.0-r0 以上・CA bundle 20261020-r0 以上）。

## やること
1. 13 ファイルの衝突を解消する（衝突記号を残さない）。dev の新しい修正（libexpat・3.14.10 の CVE 対応で入ったもの）と、3.15 への移行の両方を満たす形に。
2. 世代の入力を再導出し、fresh にする:
   `LC_ALL=C PYTHONPATH=src uv run --no-sync python infra/deploy/verify_generation_inputs.py --manifest infra/deploy/buildspec_generation_inputs.json --repo-root .` が fresh を返すまで（stale の時は再導出の手順が docs/ にある）。
3. 次が緑:
   uv run --extra dev --extra mcp pytest -q -n 4 tests/infra tests/codebuild tests/scripts tests/runtime
   uv run --extra dev --extra mcp ruff check infra tests src
   uv run --extra dev --extra mcp ruff format --check infra tests src

## やらないこと
- git commit・push（`git add` もしない。Claude がやる）、AWS 呼び出し、.env・secrets の読み取り、docker build。
- 3.15 と関係ない変更。

最後に: 解消の方針（ファイルごとに 1 行）・fresh かどうか・テスト結果・残る課題を短く。
