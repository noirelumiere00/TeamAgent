あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点・branch fix/core-base-bump-20261102）。日本語で報告する。
事態: 本番便 r88 の段 3（image-builder）の ECR 脆弱性の門が core イメージを拒否した:
  - CVE-2026-115557 CRITICAL python-3.14 3.14.8_git20261008-r0
  - CVE-2026-114583 MEDIUM python-3.14 3.14.8_git20261008-r0
  - CVE-2026-111943 MEDIUM py3-pip-wheel 26.2.2-r1
core の土台は infra/docker/Dockerfile.teamagent-mcp の PYTHON_RUNTIME_ARM64_DIGEST（cgr.dev/chainguard/python:latest@sha256:f9552d99…）と PYTHON_BUILDER_ARM64_DIGEST（:latest-dev）。手元の Trivy で、現在の cgr.dev/chainguard/python:latest（linux/arm64 の digest sha256:60e3c0d33ab77e50ace0850ccdf6eaec825ce89dad9b1ec2b0300a2070892b05）は MEDIUM 以上 0 件だった。
やること:
1. 先例のベースバンプ（~/Documents/Claude/Artifacts/aico-inputs-20260911/apkwatch/apply_mcp_base_bump.py と、過去の base bump の commit を git log で探す）に従い、runtime と builder の両方の digest を最新（同じ Python 3.14 系・arm64）へ上げる。builder 側の latest-dev の arm64 digest は公開レジストリから取得（ネットワーク可）。
2. 新しい土台に既に修正版の ca-certificates-bundle・brotli・py3-pip-wheel が入っているなら、infra/docker/replace_core_ca_bundle.py の差し替え（base_version を固定して fail closed）が新土台で失敗しないように整理する（不要になった差し替えは外す・必要なものは新しい base_version に合わせる）。vendor した APK・lock・テストも追随。
3. 世代の再導出: ~/Documents/Claude/Artifacts/aico-inputs-20260911/pipwheel-20261030-run を写して basebump1102-run を作り、OLD は live（= pipwheel-20261030-run の aico-base-bump-new.env の NEW_* を OLD_* として）にして同じ方法で再導出。`LC_ALL=C PYTHONPATH=src uv run --no-sync python infra/deploy/verify_generation_inputs.py --manifest infra/deploy/buildspec_generation_inputs.json --repo-root .` が fresh になること。
4. `uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/infra tests/codebuild tests/scripts` と ruff。新土台の Python の版（3.14.x）が変わるなら、関連の固定値（smoke_core.py・RUNTIME_CONTRACT など）も追随。
禁止: AWS への書き込み・CodeBuild 起動・S3 公開、git commit・push、.env・secrets・~/.aws の読み取り。最後に変更ファイル・新 digest・Python の版・外した差し替え・世代の新値・テスト結果を短く。
