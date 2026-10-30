# Python 3.15 本番移行調査・計画（2026-10-30）

## 結論と対象

目標は **2026-11-20 に core / media の Python 3.15 を本番稼働、11-24 に ECR 例外撤去と稼働継続を確認**すること。現状の lock のまま移行可能とは判断できない。最優先の停止要因は、core 必須の `psycopg-binary==3.3.4` に CPython 3.15 用の互換 wheel も sdist も記録されていないこと。ほかにも C/Rust 等の拡張で対応 wheel が不足し、media は Alpine の `python3` / `pyc` パッケージ提供に依存する。

順序は **CI の 3.15 検証追加 → core の依存・ベース更新 → media の APK / Python 更新 → 契約・buildspec 世代再導出 → 世代公開・署名リリース便 → 本番反映**とする。core の準備を先行するが、通常の image-builder は core / media を同じ便で作るため、media の既存ビルドが壊れていても core だけ進められるとは考えない。

小さい代替案は「core の 3.15 化と ECR 例外解消を先に完了し、media は対応 APK が揃ってから別便」。これは全対象の 11-20 完了とは異なるため、期限に間に合わない場合のスコープ判断として扱う。今回は採用・実行していない。

今回行ったのはローカル調査と本書の追加だけ。コード・設定・lock は変更せず、commit / push / fetch・外部取得・AWS 実行・`.env` / secrets の読み取りは行っていない。以下のビルド、CI、取得、公開、本番確認の記述はすべて後続作業案である。

### 調査基準

- worktree: `/Users/s-komata/dev/worktrees/teamagent-py315`
- branch: `chore/python315-survey-20261030`
- HEAD: `9e07ddeae42c39468d61b8866cb857a48423dbc2`
- 手元の `origin/dev`: `69f09c498d7d4b89315875fc3344177c49066393`。merge-base は HEAD と同じ。調査開始時点で追跡 ref と HEAD は一致していない。ネットワークによる最新確認・取り込みはしていないため、実装着手時に差分を再確認する。
- 実行対象: 通常の GIL あり CPython 3.15、Linux arm64。core は glibc / manylinux 系、media は Alpine musl / musllinux 系。`cp315t` の free-threaded 化は対象外。
- 根拠はこの HEAD のソース・契約・テスト・`uv.lock`。AWS の稼働版や上流の現在の提供状況を表すものではない。OpenWiki は構成確認に必要なページだけ参照し、古い版数や例外件数は現行ソースで上書きして判断した。

## 1. Python の版・ABI・検証条件が決まる箇所

行番号は調査 HEAD の目安。リンクはリポジトリ内の正本に向けている。版番号だけでなく、digest / SHA / パス / wheel タグによる間接固定も含む。

### 1.1 プロジェクト・lock・開発環境

| 箇所 | 現在の固定・制約 | 移行時の扱い |
|---|---|---|
| [pyproject.toml](../pyproject.toml) L9 | `requires-python = ">=3.11"` | 3.15 は範囲内。本番更新だけを理由に最低対応版を 3.15 にしない。3.11 対応を落とす場合は別判断 |
| 同 L149–185 | Ruff `target-version = "py311"`、mypy `python_version = "3.11"` | 最低対応版向けの静的検査。Python 実行版の pin ではない。原則維持し、ツール自体の 3.15 実行を検証 |
| 同 dependencies / extras / dependency-groups | core、`mcp`、`embeddings`、`media`、`dev` と独立 `media-runtime`。Linux torch は CPU index | 必須の範囲だけ依存を更新。media extra と media-runtime の重複する厳密版を一致させる |
| [uv.lock](../uv.lock) L1–16 | revision 3、`requires-python >=3.11`。`>=3.15` の Linux / 非 Linux resolution 分岐が既にある | 分岐の存在はインストール成功の証拠ではない。wheel 調査は §2。更新後も既存 Python の解決を検証 |
| 同 torch 2 エントリ L4216 / L4250 | 非 Linux は PyPI `2.13.0`、Linux は CPU index `2.13.0+cpu` | バージョン・配布元・platform marker を混同しない |
| [.gitignore](../.gitignore) L53 | `.python-version` を ignore。worktree に同ファイルなし | 追跡される単一のローカル実行版 pin はない |
| [scripts/setup_local.sh](../scripts/setup_local.sh) L35 | fallback が `python3.11 -m venv` | ローカル最低版として列挙。3.15 検証時は明示的に 3.15 で venv を作る |
| [scripts/deploy_to_ec2.sh](../scripts/deploy_to_ec2.sh) L375 | `python3.11 -m venv --copies .venv` | ECS の core/media とは別経路。EC2 も対象にするなら別途更新・rollback 検証が必要 |
| [scripts/setup_worktree.sh](../scripts/setup_worktree.sh) | 既存 venv を再利用 | 新 Python に古い venv / `.pyc` を流用しない |

### 1.2 core Docker・runtime 契約

| 箇所 | 現在値・固定方法 | 到達すべき変更・確認 |
|---|---|---|
| [Dockerfile.teamagent-mcp](../infra/docker/Dockerfile.teamagent-mcp) L7–17 | builder `2c8414a1…`、runtime `f9552d99…` の arm64 digest、Python `3.14.8`、ELF SHA `775bf454…` | 3.15 の builder/runtime child digest、実測版・両段の Python 実体 SHA を揃える。値を推測しない |
| 同 L16–17 | torch `2.13.0+cpu`、`cp314-cp314-manylinux_2_28_aarch64` URL と SHA | lock にある cp315 URL / SHA に更新可能。ただし依存全体の成功は別途必要 |
| 同 L71–72 | `python --version` の `+` を除去して厳密比較、`/usr/bin/python3.14` SHA 検証 | 新実体の表記・パスへ追随。snapshot 表記の処理を不用意に落とさない |
| 同 L194 / L199 | `/app/.venv/lib/python3.14/site-packages` で不要実装・bundled Claude の除去と検査 | 3.15 パスに追随。古いパスのままでは禁止物の検査が空振りする |
| 同 L279 / L283 | JSON 形式 RUN が `/usr/bin/python3.14` を直接実行 | 2 箇所とも新実行パスへ追随 |
| 同 runtime labels / provenance | Python 版、ELF SHA、ベース digest、torch wheel SHA、runtime receipt | build args・ラベル・receipt・実体が一致すること |
| [teamagent_runtime_contract.json](../infra/codebuild/teamagent_runtime_contract.json) | `base.builder.arm64.digest`、`base.runtime.arm64.digest`、`binary.python.sha256`、`component.python.version`、`artifact.torch.arm64-wheel.sha256`、対応 `dockerfile_uses` | core の正本入力。Dockerfile・smoke・outer release 契約と同時更新 |
| [teamagent_core_media_release_contract.json](../infra/codebuild/teamagent_core_media_release_contract.json) L14–16 / L81–84 | inner runtime 契約の raw SHA、core Python probe `/usr/bin/python3.14` と SHA | inner 更新後に raw SHA を計算し直し、probe パス / SHA を一致させる |
| [teamagent_bundle_provenance.py](../infra/codebuild/teamagent_bundle_provenance.py) L85–90 / L1100 | `EXPECTED_PROBE_PATHS` と契約ペア検証が `/usr/bin/python3.14` を直接要求 | **JSON だけ変えても通らない**。2 箇所と対応テストを更新し、埋め込まれる buildspec も再導出 |
| [source_provenance.py](../infra/codebuild/source_provenance.py) | runtime 必須キー・値型・Dockerfile 使用文字列・receipt を検査 | 版の値自体は契約から読む。必須キーや使用文字列を変える場合のみ連動更新 |
| [smoke_core.py](../infra/docker/smoke_core.py) L20 / L125–126 | `3.14.8`、Python SHA、`/usr/bin/python3.14` | 更新後に import・E5 offline load・health・隔離条件を検証 |

core には旧土台向けの CA / Brotli 差し替えがある。[core-ca-bundle.lock.json](../infra/docker/core-ca-bundle.lock.json) の `base_version=20260909-r2` と [replace_core_ca_bundle.py](../infra/docker/replace_core_ca_bundle.py) の既存 DB 検証、Dockerfile の旧 `libbrotli*.so.1.2.0` / SBOM の無条件 unlink は、新ベースで既に修正済みなら失敗する。3.15 ベースの実インベントリを見て、不要な差し替えの撤去または厳密な再導出を判断する。旧パッケージを新ベースに機械的に戻さない。撤去する場合は runtime/release 契約、必須キー検証、vendor 参照、テストまで同じ変更で揃える。

### 1.3 media Docker・Alpine APK

| 箇所 | 現在値・固定方法 | 到達すべき変更・確認 |
|---|---|---|
| [Dockerfile.teamagent-media-worker](../infra/docker/Dockerfile.teamagent-media-worker) L5 / L62–64 | Chromium Alpine arm64 child `ee09ed19…`、edge と v3.24 の main/community | ベースと混在 repo の解決結果を実測。OS 一式の互換性が条件 |
| 同 L79–81 / L318–320 | `PYTHON_PACKAGE_VERSION=3.14.10-r1`、`PYTHON_VERSION=3.14.10`、ELF SHA `b776d2d6…` を 2 段で保持 | 3 値を両段で更新。`python3` と `pyc` は同じ APK 版を指定 |
| 同 L165–175 | `apk add pyc=… python3=…`、`python3 --version` と `/usr/bin/python3` SHA 検証 | Alpine が実際に配布する package/revision に従う。core の版や ELF SHA を転用しない |
| 同 L260 / L275 | Playwright の展開先と yt-dlp 処理先が `lib/python3.14/site-packages` | 3.15 へ追随。手動展開した Python Playwright と Node 差し替え経路も確認 |
| 同 L331 / L337–343 / L494 | `MEDIA_APK_LOCK_SHA256=7d31efa3…`、実インベントリとの `cmp`、ラベル | APK 段の実結果から lock を再生成し SHA を更新。Python の行だけ手修正しない |
| [media-apk.lock](../infra/docker/media-apk.lock) L200–203 | `pyc`、`python3`、`python3-pyc`、`python3-pycache-pyc0` が `3.14.10-r1` | 全 240 行の追加・削除・更新をレビュー。依存ライブラリの差も対象 |
| [teamagent_core_media_release_contract.json](../infra/codebuild/teamagent_core_media_release_contract.json) media subject | `binary.python.sha256` (`/usr/bin/python3`)、`component.python.version`、`package.python.version`、`artifact.apk-lock.sha256` と使用文字列 | Dockerfile・lock・probe・ラベルの値を一致させる |
| [smoke_media.py](../infra/docker/smoke_media.py) L35–36 / L131 | ELF SHA は現行値、Python 期待値は **3.14.5 のまま** | 現行 Dockerfile の 3.14.10 と不一致。3.15 検証の前に期待値を実体に揃える。yt-dlp も smoke は 2026.6.9、Dockerfile は 2026.8.19 で不一致 |
| [runtime-contract.json](../infra/docker/runtime-contract.json)、[RUNTIME_CONTRACT.md](../infra/docker/RUNTIME_CONTRACT.md) | 実行境界・禁止依存・成果物 hash。文章の media Python package は 3.14.5-r2 と古い | 新規証跡が実際に使う値を整合。文書や過去の値を測定値として転記しない |

**Alpine 制約:** media は `UV_PYTHON_DOWNLOADS=never` で、APK の `/usr/bin/python3` を使う。`PYTHON_VERSION=3.15.x` の文字列だけを変更しても interpreter は変わらない。採用する arm64 repo で Python 3.15 の `python3` と `pyc`、必要な関連 APK が入手でき、Chromium / ffmpeg / Node / musl の依存解決が成立することが前提。本調査では上流提供を確認していないため **要確認**。3.15 が無ければ、この構成のままの期限は保証できない。別配布 interpreter のコピーや独自 CPython ビルドは、新しい供給・ABI・証跡経路になるため今回の標準手順に含めない。

### 1.4 CI・CodeBuild・世代入力

| 箇所 | 現在の固定・連動 | 移行時の扱い |
|---|---|---|
| [.github/workflows/ci.yml](../.github/workflows/ci.yml) L43 | latest-dependency job: PR は 3.14、その他は 3.11 / 3.13 / 3.14、各 3 shard | まず 3.15 を追加し 3.14 を残す。移行後に PR の主検証を 3.15 へ |
| 同 L141 / L147 | coverage は 3.14 のみ、artifact 名に Python 版 | coverage の対象・出力・集計を一緒に変更 |
| 同 L161 / L187–207 / L260 | lockfile job は 3.14 固定。uv 0.11.29、`uv export --frozen --extra dev --extra mcp --extra media` → pip `--require-hashes --no-deps` | 3.15 の lock 経路も必須。latest の成功だけで本番互換としない。**embeddings は CI に含まれない** |
| 同 L277–295 | summary 実行版、`PYTHON_VERSIONS`、lock artifact glob、coverage glob と 3 shard 数 | 新 matrix と全件照合・coverage が一致すること。job 表示名だけの変更では不足 |
| [.github/workflows/openwiki-update.yml](../.github/workflows/openwiki-update.yml) | Python 版の明示 pin なし | 今回の版更新は不要 |
| [infra/terraform/codebuild.tf](../infra/terraform/codebuild.tf)、[mcp_approval.tf](../infra/terraform/mcp_approval.tf) | build host は `aws/codebuild/amazonlinux-aarch64-standard:3.0`、buildspec 内は `python3`。Python minor の `runtime-versions` 明示なし | アプリの 3.15 と build host の Python を分離。host の版を 3.15 に変える必然性はない。埋め込む helper が host でも動くこと |
| [buildspec.yml](../infra/codebuild/buildspec.yml) | core/media 双方をビルド、ECR scan、inner/outer 契約の pin と検証 | core/media の片方が失敗すれば便は未完了。Dockerfile の pin と契約変更を反映した inline body が必要 |
| [mcp-source-publisher-buildspec.yml](../infra/codebuild/mcp-source-publisher-buildspec.yml)、[image-attestor-buildspec.yml](../infra/codebuild/image-attestor-buildspec.yml)、[image-promoter-buildspec.yml](../infra/codebuild/image-promoter-buildspec.yml) | Terraform が契約 JSON / helper を base64 埋め込みし body SHA を算出 | raw ファイルの SHA と完成 buildspec の SHA は別。3 本を再導出 |
| [mcp_approval.tf](../infra/terraform/mcp_approval.tf) L149–152 / L497–513 | approval-publisher にも bundle/source helper を埋め込む。bootstrap body SHA の固定もある | 今回は bundle helper の Python probe パス変更が必要なので approval-publisher も再導出。bootstrap body / expected SHA への波及も照合 |
| [buildspec_generation_inputs.json](../infra/deploy/buildspec_generation_inputs.json) | baseline、18 入力の **git blob SHA**、4 project の `expected_generation_sha256`、KMS / Terraform 評価 context | 変更後の blob・完成 body SHA・baseline を更新。image-builder inline body は別管理。契約 raw SHA256 と git blob SHA を混同しない |
| [verify_generation_inputs.py](../infra/deploy/verify_generation_inputs.py)、[assert_published_generation.py](../infra/deploy/assert_published_generation.py) | ローカル freshness と公開済み参照の検査 | 前者だけ今回実行。後者は AWS の後続確認で使用 |
| [supply_chain_adopt.tf](../infra/terraform/supply_chain_adopt.tf)、[supply_chain_adoptions.json](../infra/deploy/supply_chain_adoptions.json) | 不変 S3 世代の台帳 / adopt mapping | adopt 済み世代は削除しない。新世代と import を現行モデルに従って扱う |
| [scripts/aws/release_mcp.sh](../scripts/aws/release_mcp.sh) L129–173 | repo の世代 freshness と CodeBuild ref を preflight。契約 SHA はファイルから算出 | 新世代を公開・pin 更新してから便へ。`--dry-run` も AWS 読み取りがあるため今回は未実行 |
| [build_teamagent_image.sh](../infra/deploy/build_teamagent_image.sh) | inner/outer SHA を読んで署名鎖へ渡す。remote 取得もある | 今回は未実行。固定文字列の一律置換は不要、実装時に読取 pin と IAM Condition を照合 |
| [activation_freeze.json](../infra/deploy/activation_freeze.json) / [activation_freeze_check.py](../infra/deploy/activation_freeze_check.py) | 現在の unlock scope は core_media 契約 1 path | これは既存別案件の宣言。将来の実装 PR は実変更する frozen paths に合う宣言・テストが必要。今回の調査は宣言を変更しない |

世代 manifest が列挙する 18 入力は次のとおり。Python 文字列を持たない helper も、変更すると世代が動く。

```text
infra/codebuild/mcp-source-publisher-buildspec.yml
infra/codebuild/image-attestor-buildspec.yml
infra/codebuild/image-promoter-buildspec.yml
infra/codebuild/source_provenance.py
infra/codebuild/release_evidence.py
infra/codebuild/teamagent_bundle_provenance.py
infra/codebuild/teamagent_schema_versions.py
infra/codebuild/teamagent_release_approval.py
infra/codebuild/actual_image_evidence.py
infra/codebuild/openclaw_provenance.py
infra/codebuild/tiktok_source_provenance.py
infra/codebuild/verify_actual_image.sh
infra/codebuild/teamagent_runtime_contract.json
infra/codebuild/teamagent_core_media_release_contract.json
infra/codebuild/tiktok_release_contract.json
infra/codebuild/openclaw_bundle_contract.json
infra/terraform/codebuild.tf
infra/terraform/mcp_approval.tf
```

### 1.5 OpenClaw・Hermes・その他の Python

| 箇所 | 現在値・意味 | 対象判断 |
|---|---|---|
| [Dockerfile.openclaw](../infra/docker/Dockerfile.openclaw)、[openclaw_bundle_contract.json](../infra/codebuild/openclaw_bundle_contract.json) | 最終 runtime は固定 Chainguard Node。Python の版 pin なし | Python runtime の 3.15 化は不要。ただし共有 attestor が更新されるので OpenClaw の契約・検証に回帰が無いことを確認 |
| [infra/openclaw/build-image.sh](../infra/openclaw/build-image.sh) L911 | 脆弱性検査の記述に `packageFamily: python3.11` | Python interpreter の選択ではない。Node runtime 更新と混同して置換しない |
| [Dockerfile.hermes](../infra/docker/Dockerfile.hermes) L10 / L31 / L48 / L60、[hermes_runtime/pyproject.toml](../hermes_runtime/pyproject.toml) | `python-3.13=3.13.15_git20260918-r2`、venv `/usr/bin/python3.13`。上流制約 `>=3.11,<3.14` | 独立制約のため維持。3.15 に一括更新すると宣言と衝突。依存は別の [requirements-hermes.lock](../hermes_runtime/requirements-hermes.lock) で固定し、`hermes_runtime/uv.lock` は存在しない |
| [Dockerfile.alpine-experiment](../infra/docker/Dockerfile.alpine-experiment) | Python APK `3.14.5-r2` | 実験用。現用 media の代わりに更新しない |
| [Dockerfile.wolfi-experiment](../infra/docker/Dockerfile.wolfi-experiment) | `python-3.14=3.14.6-r3`、ENTRYPOINT `/usr/bin/python3.14`、pip3.13 / pip3.14 の除去 | 実験用。現用 core とは別。残存 pin として記録 |
| [terraform_runtime_guard.sh](../infra/deploy/terraform_runtime_guard.sh) L8397 / L8437 / L8479 | main / tiktok / x_buzz preflight が `(3, 14)` を assert | どの profile が現用 consumer に到達するかを確かめ、新イメージを通す検証を更新。Lambda の runtime 判定とは分ける |
| [ingest_schedule.tf](../infra/terraform/ingest_schedule.tf) L374、[x_research.tf](../infra/terraform/x_research.tf) L444、[reminders.tf](../infra/terraform/reminders.tf) L245、[tiktok_acquire.tf](../infra/terraform/tiktok_acquire.tf) L667 / L960 | Lambda runtime `python3.12` | コンテナと別ランタイム。今回は維持。AWS での 3.15 対応も未確認 |
| 同 guard L6576 / L8255 | Lambda `python3.12` の許可条件 | 上記 Lambda と同じ理由で維持 |
| [lambda_iam.tf](../infra/terraform/lambda_iam.tf) L208 | `python3.11` はコメント中の例 | 実行版 pin ではない |
| [infra/bootstrap/provenance_iam_bootstrap.py](../infra/bootstrap/provenance_iam_bootstrap.py) | bootstrap host に Python 3.11 以上を要求 | 最低条件であり本番 Python pin ではない。維持 |
| [tools/first_visit/sales25.lock.json](../tools/first_visit/sales25.lock.json) | Python `>=3.10` | キットの最低版記述。3.15 実動作は media smoke / 資料生成で確認 |

core の consumer は [runtime-consumers.json](../infra/docker/runtime-consumers.json) の mcp / connect-web / canary / ingest / morning-digest / x-buzz-worker の 6 個。media は media-worker。いずれも `/app/.venv/bin/python` を使うため command の minor 版置換は不要だが、全 consumer の新 digest・起動・期待終了値を確認する。canary / ingest / x-buzz / media の合成検証は registry 上の非ゼロ終了もあり、機械的に「終了 0 だけ」を合格にしない。

### 1.6 テスト・fixture・文書の追随

| 箇所 | 固定・検証しているもの |
|---|---|
| [tests/infra/test_dockerfile_teamagent_mcp.py](../tests/infra/test_dockerfile_teamagent_mcp.py) | Python 3.14.8、torch cp314、digest / SHA / 除去・境界・health |
| [tests/infra/test_dockerfile_teamagent_media_worker.py](../tests/infra/test_dockerfile_teamagent_media_worker.py) | Python package 3.14.10-r1、版 3.14.10、ELF SHA、APK lock 全体 hash、各段 / 契約との一致 |
| [tests/codebuild/fixtures/teamagent_runtime_contract.ready.json](../tests/codebuild/fixtures/teamagent_runtime_contract.ready.json) | `/usr/bin/python3.14`、fixture の版は 3.14.6。現用値とは別の試験データ |
| [tests/codebuild/test_source_provenance.py](../tests/codebuild/test_source_provenance.py) L551、[test_release_evidence.py](../tests/codebuild/test_release_evidence.py) L532 | Python 3.14 probe / 使用文字列。fixture を新パスへ揃える場合に追随 |
| [tests/codebuild/test_teamagent_bundle_provenance.py](../tests/codebuild/test_teamagent_bundle_provenance.py)、[test_buildspec_contract.py](../tests/codebuild/test_buildspec_contract.py) | 契約ペア・probe・Dockerfile・埋め込み helper の整合。旧パス拒否と新パス受理の回帰確認 |
| [tests/infra/test_replace_core_ca_bundle.py](../tests/infra/test_replace_core_ca_bundle.py) | Python 3.14 package を含む DB fixture と、3.15 への予期しない DB 改変を拒否する負例 | 負例の 3.15 を「旧版だから」と消さない。差し替え方式を変える場合は検証意図を維持 |
| [tests/codebuild/test_ecr_scan_gate.py](../tests/codebuild/test_ecr_scan_gate.py) L159–169 | core Python 3.14 例外 1 件と 11-30 の期限を固定 | 例外撤去時に現行集合の期待値も空へ。stale / expired / 版違い拒否のテストは維持 |
| [tests/scripts/test_terraform_runtime_contracts.py](../tests/scripts/test_terraform_runtime_contracts.py) L317 / L1537 | `(3, 14)` preflight と別系統の Lambda 3.12 | コンテナ側だけ追随。Lambda 側は維持 |
| [tests/scripts/test_activation_freeze.py](../tests/scripts/test_activation_freeze.py)、[test_activation_freeze_policy.py](../tests/scripts/test_activation_freeze_policy.py) | 現行 scope と過去の版更新の記録 | 実装 PR の scope に必要な期待値だけ変更。履歴の版は書き換えない |
| [tests/scripts/test_release_mcp_generation_preflight.py](../tests/scripts/test_release_mcp_generation_preflight.py)、[test_supply_chain_adopt.py](../tests/scripts/test_supply_chain_adopt.py) | generation manifest、世代 pin、台帳 / adopt 条件 | 再導出後の一致・stale 検出を確認 |
| [tests/infra/test_runtime_contract.py](../tests/infra/test_runtime_contract.py)、[test_runtime_evidence_verifier.py](../tests/infra/test_runtime_evidence_verifier.py) | runtime 境界・smoke・証跡。版固定の主対象は smoke 側 | 新 interpreter で同じ隔離・証跡条件を通す |
| [tests/infra/test_ci_supply_chain.py](../tests/infra/test_ci_supply_chain.py)、[tests/pytest_shard.py](../tests/pytest_shard.py) | workflow の pin と shard 全件照合 | Python job / artifact 名変更後も集計が成立すること |
| [tests/infra/test_ecs_service_apply_saga.py](../tests/infra/test_ecs_service_apply_saga.py)、[tests/scripts/test_terraform_runtime_guard.py](../tests/scripts/test_terraform_runtime_guard.py) | Lambda 3.12 fixture / 3.13 を不正変更として使う負例 | コンテナ移行では維持 |

AWS CLI の表示例（`tests/scripts/test_media_cutover_evidence.py`、`test_runtime_evidence_guard_contract.py`、`test_terraform_runtime_guard.py`）にある Python 3.13 は、CLI host の fixture で本番 interpreter の pin ではない。`tests/observability/test_sentry.py` の `3.14` は数値データ、`tests/skills/video_algorithm/test_cover_reader.py` の 3.14 は JSON 挙動の説明であり、一律置換しない。

文書では `infra/docker/RUNTIME_CONTRACT.md` を新測定値に追随させる。README / `docs/dev_setup_windows_wsl.md` / `docs/runbooks/provenance_iam_bootstrap.md` の 3.11+ / OS 既定版は最低条件や host の説明。`hermes_runtime/README.md`、`docs/architecture/hermes_migration_design.md`、`hermes_implementation_plan.md` の 3.13 は独立制約。`docs/activation/ACTIVATION_STATE.md`、`docs/aila_loop/ARCHITECTURE.md`、`docs/v3.2/{system_reference.md,spec_vs_current_full_matrix_2026-06-15.md,teamagent_overview_v3.2_draft.md}`、`docs/v3.0/teamagent_architecture_v3.0.html`、`docs/archive/{v2.0,v2.1}/` の architecture HTML、`docs/reports/vault-campaign-videos/validation-failures.txt` の過去環境表記は runtime の正本ではない。履歴・生成 OpenWiki を一括書換えしない。

## 2. uv.lock から分かる 3.15 wheel の不足

### 判定方法と限界

lock を標準ライブラリ `tomllib` で解析した。**189 package エントリ / 188 名称**（torch が配布元別に 2 エントリ）、うちローカル editable の teamagent を除く 188 配布エントリが対象。

通常 CPython 3.15 の候補として `cp315-cp315` / `cp315-none`、`cp3N-abi3`（N ≤ 15）、Python 3 を含む `py3` 系 `none` wheel を区別した。さらに platform を分ける。`cp314-cp314` は互換候補にしない。`cp315t` は対象にしない。タグ上の候補があっても Requires-Python の上限、実行時 API、共有ライブラリ、CPU 要件の適合や公式サポートまでは証明しない。

lock は各 package の完全な Requires-Python / classifiers を保存していない。したがって **「wheel が lock に無い」ことは判定できるが、「上流が 3.15 非対応」とは断定しない**。sdist があってもビルド成功の証拠にはならない。外部メタデータ・新 wheel・ソースは取得していない。

### 2.1 互換候補 wheel が無い全エントリ（16 件）

以下は platform 条件の前に、通常 CPython 3.15 用の互換候補タグ自体が無い。既存 wheel の CPython タグは最大 cp314。Linux の依存選択で残るのはこのうち 14 件（本番 image の 13 件と dev 用 librt）。残りは Windows 用 pywin32 と非 Linux 用 PyPI torch。

| package / lock 行 | 版 | 主な到達経路 | sdist | 判定・次に確認すること |
|---|---|---|---|---|
| cffi / L496 | 2.0.0 | core: google-auth → cryptography、media: curl-cffi / WeasyPrint | あり | 両イメージの障害候補。対応 wheel のある最小更新、または C/libffi ビルド可否が要確認 |
| httptools / L1415 | 0.8.0 | core: uvicorn[standard] | あり | 対応 wheel / C 拡張の 3.15 ビルド可否が要確認 |
| jiter / L1589 | 0.15.0 | core: anthropic | あり | 対応 wheel / Rust 側の 3.15 ビルド可否が要確認 |
| librt / L1724 | 0.11.0 | dev: mypy（CPython） | あり | CI の 3.15 lint/typecheck 導入時に要確認。mypy 本体の pure wheel だけでは足りない |
| lxml / L1797 | 6.1.1 | core: python-docx、media: python-pptx | あり | 両イメージで必要。対応 wheel / C・libxml 系ビルド可否が要確認 |
| markupsafe / L1932 | 3.0.3 | core: alembic → Mako、embeddings: torch → Jinja2 | あり | 対応 wheel / source fallback が要確認 |
| numpy / L2265 | 2.4.6 | core: pgvector、および embeddings | あり | embeddings だけの問題ではない。対応 wheel を優先、数値計算系 source build を安易に追加しない |
| **psycopg-binary / L2755** | **3.3.4** | core: psycopg[binary]、CI lock 経路 | **なし** | **現 lock でインストールできない明確な停止要因**。対応 wheel を持つ版への更新が必要。binary extra の削除は libpq 導入方式まで変えるため第一案にしない |
| pydantic-core / L2869 | 2.46.4 | core / media: pydantic 2.13.4 | あり | 両イメージの障害候補。pydantic と整合する版の組で更新・検証 |
| pywin32 / L3182 | 311 | mcp、`sys_platform == 'win32'` | なし | Linux 便は非対象。Windows の 3.15 開発対応は要確認 |
| pyyaml / L3201 | 6.0.3 | core、embeddings、dev pre-commit | あり | pure 実装が含まれるか / build isolation / C extension fallback は lock だけでは不明。要確認 |
| regex / L3270 | 2026.5.9 | core embeddings: transformers | あり | 対応 wheel / C 拡張の 3.15 ビルド可否が要確認 |
| scikit-learn / L3613 | 1.9.0 | core embeddings: sentence-transformers | あり | 対応 wheel と numpy/scipy の整合を確認 |
| scipy / L3658 | 1.17.1 | core embeddings: sentence-transformers / scikit-learn | あり | 対応 wheel を優先。Fortran/BLAS 等の source build を新設するなら別見積り |
| torch（PyPI）/ L4216 | 2.13.0 | embeddings、`sys_platform != 'linux'` | なし | macOS / Windows の 3.15 は現 lock で不可。Linux CPU 配布と区別する |
| uvloop / L4440 | 0.22.1 | core: uvicorn[standard]、Linux CPython で選択 | あり | 対応 wheel / C 拡張の 3.15 ビルド可否が要確認 |

### 2.2 wheel があるもの・platform 固有の注意

| 分類 | lock 内の該当 package | 解釈 |
|---|---|---|
| cp315 の arm64 manylinux / musllinux がある | aiohttp 3.14.4、brotli 1.2.1、greenlet 3.5.1、pillow 13.0.0、rpds-py 2026.5.1、watchfiles 1.2.0、yarl 1.25.1 | 少なくともタグの不足はない。実動作確認は必要 |
| arm64 manylinux / musllinux の abi3 がある | ast-serialize 0.5.0、cryptography 50.0.0、curl-cffi 0.15.0、hf-xet 1.5.1、psutil 7.2.2、safetensors 0.8.0、tokenizers 0.22.2、zopfli 0.4.2 | cp315 の文字列が無くても通常 CPython 3.15 の候補。cryptography / curl-cffi 自体が候補でも推移依存 cffi は別の不足 |
| py3-none-any fallback がある（ネイティブ wheel も持つもの） | aiohttp、charset-normalizer、coverage、fonttools、frozenlist、multidict、mypy、propcache、protobuf、sqlalchemy、tomli、websockets、yarl | cp315 ネイティブ wheel 不在だけをもって不可にしない。性能・機能差や上限は要確認 |
| py3 platform wheel | ruff 0.15.15 | arm64 manylinux / musllinux 候補あり。Python ABI の cp315 タグは不要 |
| Playwright 1.60.0 | `py3-none-manylinux…aarch64` はあるが musllinux wheel / sdist はない | 通常の Alpine wheel 導入には使えない。現 Dockerfile は検証済み wheel の Python 部分を手動展開し、glibc Node を `/usr/bin/node` に置換する既存経路。3.15 でもこの経路の smoke が必要 |
| brotlicffi 1.2.1.0 | abi3 manylinux はあるが arm64 musllinux はない | `fonttools[woff]` の非 CPython marker の経路。通常 CPython 本番では選ばれず、今回の停止要因に数えない |
| torch CPU 2.13.0+cpu | cp315 の arm64 / x86_64 manylinux はある、musllinux はない | core は候補あり。media は embeddings を入れないため問題にしない |

上表以外の配布エントリにも Python 3 を含む `none-any` wheel がある。pure wheel について、個別の上流 3.15 対応声明は一切確認していない。アプリと依存全体のテストを通す必要がある。

core torch の更新候補は **既に lock L4284 に存在**する。外部取得はしていない。

```text
version: 2.13.0+cpu
wheel: torch-2.13.0%2Bcpu-cp315-cp315-manylinux_2_28_aarch64.whl
source URL: https://download-r2.pytorch.org/whl/cpu/torch-2.13.0%2Bcpu-cp315-cp315-manylinux_2_28_aarch64.whl
sha256: 8eb5002ca81af00ae69b57540f615b58b8ae922b6d4848176b366a52bd2196e6
```

### 2.3 ローカル再検査用（取得なし）

リポジトリ root で実行する標準ライブラリだけのチェック。これはタグ不足 16 件の再現用で、resolver / platform 全体 / 実インストールの代用ではない。

```python
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

lock = tomllib.loads(Path("uv.lock").read_text())

def compatible(filename):
    py, abi, platform = filename.removesuffix(".whl").rsplit("-", 3)[1:]
    py_tags = py.split(".")
    if abi == "none" and any(tag in {"py3", "py315"} for tag in py_tags):
        return True
    if "cp315" in py_tags and abi in {"cp315", "none"}:
        return True
    return abi == "abi3" and any(
        tag.startswith("cp3") and tag[3:].isdigit() and int(tag[3:]) <= 15
        for tag in py_tags
    )

assert compatible("x-1-cp39-abi3-manylinux_2_28_aarch64.whl")
assert compatible("x-1-py2.py3-none-any.whl")
assert not compatible("x-1-cp314-cp314-manylinux_2_28_aarch64.whl")
assert not compatible("x-1-cp315-cp315t-manylinux_2_28_aarch64.whl")
missing = []
for package in lock["package"]:
    if package["source"].get("editable") is not None:
        continue
    names = [
        unquote(urlsplit(wheel["url"]).path.rsplit("/", 1)[-1])
        for wheel in package.get("wheels", [])
    ]
    if not any(compatible(name) for name in names):
        missing.append((package["name"], package["version"], "sdist" in package))
for row in missing:
    print(*row)
assert len(lock["package"]) == 189  # この調査 HEAD の期待値
assert len(missing) == 16
```

## 3. 手順・確認・戻し方・見積り

工数は担当 1 名の実作業日で、実測ではない。上流 wheel / APK の待ち時間、レビュー、管理者の公開枠は別。後続で外部取得・AWS 作業が許可された段階で実施する。

| 段階・目安日 | 作業 | 合格条件 | 戻し方 | 実作業見積り |
|---|---|---|---|---|
| 0: 10-30〜11-05、実装前 | 更新された dev との差分確認。現在の release digest、task definition、契約・receipt・世代 pin・既存例外を保存する。対応 wheel と Alpine arm64 3.15 の提供を確認 | §2 の不足依存の解消方針、media の APK 提供、再導出・rollback 経路が具体化 | 本番未変更。調査を閉じ、停止要因を明記 | 0.5〜1 日、上流待ち別 |
| 1: 11-02〜11-06、CI | 3.14 を残し 3.15 の latest / lock 検証を追加。必要最小の依存更新と lock 再解決。summary / artifact / coverage も整合 | 3.15 の frozen install、全 shard の全件照合、ruff / mypy / 既存検査成功。3.11 / 3.13 / 3.14 の既存経路も維持 | CI 変更・依存更新の変更単位で revert。本番は旧 image のまま | 1〜2 日、依存対応に追加 1〜3 日 |
| 2: 11-06〜11-11、core | 3.15 builder/runtime arm64 digest と ELF SHA を測定。torch cp315 pin、直接パス、旧 CA/Brotli 対策、inner/outer 契約、helper・smoke・テストを揃える | arm64 の frozen build と実 image の import、E5 の revision あり/なし offline load、health、DB client、UID・read-only・禁止依存確認 | ビルド候補を採用しない。旧 release digest は保持。旧 interpreter に新 venv を載せない | 1〜2 日 |
| 3: 11-09〜11-12、media | Alpine APK 解決→実インベントリ / ELF SHA→lock→Dockerfile 両段→outer 契約の順で再導出。site-packages / smoke の既存不一致を修正 | APK lock の全行一致、frozen media-runtime install、Playwright Python/Node、curl-cffi、Chromium、ffmpeg、WeasyPrint/PPTX/PDF/フォント、yt-dlp sanitizer の実動作 | APK 数行だけを戻さず、Dockerfile・lock・契約・smoke の一組を旧版へ。本番未変更 | 1〜2 日、APK 提供待ち別 |
| 4: 11-12〜11-13、世代再導出 | 最終の契約と helper から source-publisher / attestor / promoter / approval-publisher、image-builder inline body、必要なら approval bootstrap body を再導出 | 完成 body の SHA、未展開 placeholder 無し、埋め込み契約 raw SHA・helper の一致、18 入力 freshness、契約ペア検証、世代テスト成功 | 公開前なら候補を破棄。既存世代を削除しない。失敗した候補 hash を期待値として残さない | 0.5〜1 日、導出手段の整備が必要なら追加 |
| 5: 11-16〜11-18、公開・検証便 | 管理者が不変 S3 body を append、CodeBuild の参照と読み取り権限 pin を更新。承認→source publisher→image builder→attestor→promoter を実行 | Terraform 評価 SHA = S3 body SHA = content-addressed key = CodeBuild ref。repo freshness / published preflight 成功。core/media 両方の ECR / actual-image 検査・署名・同一 digest の verified candidate | ECS はまだ変更しない。必要なら対応する旧世代の参照・IAM pin・契約の組へ戻す。公開済み object は保存 | 0.5〜1 日＋ビルド/スキャン待ち |
| 6: 11-18〜11-19、検証環境 | release ゲートで昇格した digest を使い、Fargate の read-only / user / メモリ / sandbox と全 consumer を実測。旧 digest への rollback をリハーサル | §4 の証跡と業務 smoke、rollback 経路が成立。OpenClaw→MCP の代表操作も成功 | 正規 guard / 保存 plan / 証拠の経路で旧 core/media digest と task definition へ戻す | 0.5〜1 日 |
| 7: **11-20、本番** | release digest を正規 deploy 経路で consumer 群へ反映。時限監視枠と担当を確保 | 実稼働 image / task definition の一致、3.15、health、MCP、検索、接続、定期処理、media ジョブの成功。失敗時の判定と復旧完了を記録 | 事前保存した旧 release digest / task definition / スケジュール参照を整合して復旧。image の再ビルドを復旧条件にしない | 0.5 日、監視 1〜2 時間を別確保 |
| 8: **11-24、撤去確認** | 実稼働 digest の scan とソース・便の証跡を再照合、11-20 以降のエラー率・ジョブ・資源使用を確認 | core 例外 0、対象 finding 消失、旧 Python package 消失、media/OpenClaw の例外追加無し、3.15 稼働継続 | 再発なら原因と復旧判断。例外を黙って再追加・延長しない | 0.25〜0.5 日 |

目安合計は **6〜11 実作業日＋依存対応 1〜3 日・上流待ち・レビュー／公開待ち**。不足 wheel を無理にソースビルドで埋める場合はこの見積り外。11-20 は依存提供の確認を前提とする目標であり、現ローカル情報だけで達成確約はできない。

### 日付ごとの判断期限

- **11-06:** `psycopg-binary`、`pydantic-core`、cffi、numpy、lxml、embedding 計算系、uvicorn 拡張と、Alpine 3.15 APK の見通しを確定。未提供なら全体の日程リスクを報告し、core 先行案を比較する。
- **11-13:** 両 image と契約・世代導出がローカル／CI で揃わなければ 11-20 全対象投入を再計画する。検証を削って日付だけ合わせない。
- **11-19:** 検証環境・署名鎖・rollback のどれかが不成立なら本番投入を見送る。
- **11-24:** 本番投入と例外撤去の確認日。例外の実期限 **11-30** までを無条件の作業猶予にしない。

### 世代公開に関する既存の不確実性

[supply_chain_adopt.tf](../infra/terraform/supply_chain_adopt.tf) のコメントにある `infra/deploy/derive_buildspec_generations.py` はこの tree に存在しない。利用可能な自動導出コマンドとして計画に引用しない。正本は `codebuild.tf` の replace/filebase64 チェーン、`mcp_approval.tf` の heredoc と評価結果。後続実装ではこれを決定論的に評価できる手段を確保し、既存期待 SHA の再現で校正してから新 body を採用する。

また [runbook](runbooks/supply_chain_adopt.md) §3 は通常の新世代 import-only が現 validator では使えないと明記しており、ソースでも `supply_chain_adopt_validate.py` は `old_address` と対応 forget 集合を要求する。新世代の publish / CodeBuild pin 更新と Terraform state への adopt は同一操作ではない。今回 state / AWS を見ていないため、適用が必要な場合の import-only 対応・運用経路を **11-06 までに要確認**とする。架空の old_address を作る、adopt 済み台帳を削除する、検証を迂回する案にはしない。

## 4. 各段の確認内容と ECR 例外撤去

### 静的・CI 検査

実装後の最小の関連回帰は `tests/infra/test_dockerfile_teamagent_{mcp,media_worker}.py`、`test_runtime_contract.py`、CA 対策を触る場合の `test_replace_core_ca_bundle.py`、`tests/codebuild/test_{source_provenance,teamagent_bundle_provenance,release_evidence,buildspec_contract,ecr_scan_gate}.py`、`tests/scripts/test_{release_mcp_generation_preflight,supply_chain_adopt,activation_freeze,terraform_runtime_contracts}.py`。その後は CI の全 shard を通す。テスト名・fixture の不一致を調べ、必要な隣接テスト（actual image / OpenClaw / Terraform guard 等）も変更範囲に従って通す。

latest CI は一部依存の手動導入で embeddings を省き、lock CI も embeddings extra を含めない。**CI 緑だけでは core 本番の E5 / torch / scipy を検証したことにならない**。新 arm64 image に対する frozen build と E5 の offline load を独立した必須条件とする。Ubuntu CI の結果は Alpine musl の代用にもならない。

### 実 image・業務確認

- core: `sys.version` / 実行パス / ELF SHA、frozen 導入結果、`anthropic` / `psycopg` / `pydantic` / `numpy` / `sentence_transformers` の import、E5 の固定 revision と revision 無指定の offline load、DB/RLS・MCP health / tool 呼び出し。
- 6 core consumer: mcp と connect-web の health、canary / ingest / morning-digest / x-buzz の既存合成検査と代表ジョブ。起動 command・メモリ上限・期待 exit は registry に従う。
- media: APK / Python inventory の一致、Playwright の手動展開と Node 置換、Chromium sandbox、ffmpeg 変換、curl-cffi import、PPTX/PDF と WOFF フォント、yt-dlp 除去集合と sanitized source tree SHA、TikTok 等の既存代表経路。実媒体へのアクセスは後続で許可されたテスト素材・範囲で行う。
- 共通: arm64、UID/GID、read-only root、`/tmp` の書込み、capability、core/media の禁止モジュール境界、Fargate sandbox、成果物の署名と digest、旧版に対する起動時間・RSS・応答時間・失敗率の差を記録。
- [build_local_runtime_evidence.sh](../infra/docker/build_local_runtime_evidence.sh) は取得・ビルド・scanner 実行を伴い clean な追跡 tree を要求する。今回未実行。後続で使う場合は frozen source と同じ入力で証跡を作り、スキャンや secret 検査の結果を保存する。証跡処理に任せて秘密の実値を閲覧・出力しない。

### 例外は 11-24 まで残すものではない

[ecr_scan_exceptions_core.json](../infra/codebuild/ecr_scan_exceptions_core.json) は次の **1 件**を持つ。media と OpenClaw の exceptions は空。

```text
CVE-2026-12345 / MEDIUM
package: python-3.14
version: 3.14.8_git20261008-r0
expires_on: 2026-11-30
stale_exception_policy: fail
```

[verify_ecr_scan.py](../infra/codebuild/verify_ecr_scan.py) は finding と例外の完全一致を要求する。新ベースでこの finding が消えると、古い例外を残した時点で stale になり **image-builder のゲートが落ちる**。したがって修正済み新 image を通す変更と同時に例外を削除し、`test_ecr_scan_gate.py` の現行集合期待値を空へ揃える。**11-20 のリリース便までに撤去し、11-24 は撤去後の確認日**とする。

後続の確認証跡は、対象 repository / arm64 child digest の COMPLETE scan（ページ欠落なし）、全重大度の finding と例外 0、旧 `python-3.14` inventory の消失、actual-image の Python probe / OCI labels / receipt 一致、昇格・本番 task の同一 digest。3.15 に変えたという理由だけで CVE 解消済みとは扱わない。新しい別 finding が出たら同じゲートで停止する。

actual-image Trivy は `verify_actual_image.sh` と release 契約の **Critical / High 0**。ローカル証跡の `verify_runtime_evidence.py` はさらに厳しい全重大度 / secret の検査を持つ。ECR の全重大度ゲート、actual-image、ローカル証跡の合格を同一視しない。

### rollback の条件

Python 更新だけで DB schema や永続データ形式は変更しない計画とする。既存 release repository の旧 digest と署名・receipt、旧 task definition、consumer / schedule 参照、旧契約・世代 pin の組を保持する。障害時は正規の release / Terraform guard 経路でそれらに復旧し、全 consumer の health と代表ジョブまで確認する。quarantine tag や可変 latest を復旧先にしない。

旧 Python の ECR 例外を新系統に残して rollback を容易にすることはできない（stale=fail）。旧 image の再利用・再承認が現在の release gate と期限で通るか、検証環境で事前に確認する。旧版の再ビルドは削除済み APK 等で失敗し得るので復旧手順の前提にしない。

forced rollback drill が必要なリリース条件では [runbook](runbooks/forced_rollback_drill.md) と現行契約を確認する。同文書は caller / evidence の未解決 NO-GO を記載しているため、「スクリプトがあるので戻せる」とは判断しない。11-19 の投入判断までに必要な復旧経路の成立を確認する。上表の通常復旧時間と、drill 固有の時間制約・承認は別扱いとする。

## 5. 今回の実施検証・残課題

実施済み:

- 作業開始時の `git status --short` は空。ローカル HEAD / origin/dev / merge-base を記録。
- runtime、Docker、CI、CodeBuild、契約・世代入力、apk lock、関連 tests / fixtures と呼出経路をローカルで照合。
- `uv.lock` 189 エントリを解析し、タグ不足 16 件と platform / marker による対象差を確認。§2.3 の再検査を実行し assert 成功。
- `python3 infra/deploy/verify_generation_inputs.py --manifest infra/deploy/buildspec_generation_inputs.json --repo-root .` は成功: `generation inputs fresh: 18 inputs unchanged since d33d9710c3a1`。これは公開済み一致を保証しない。
- 本書のローカルリンク、表の対象、diff の空白・変更対象を確認。

未実施・要確認:

- 3.15 本体、上流 image / wheel / APK / metadata の取得と提供確認、ソースビルド可否。ローカル解析に使った `python3` は **3.14.7**。
- `pytest` / アプリ実行 / CI / Docker build / smoke / scanner。ローカル `python3` には pytest がなく、今回の文書のみの変更のため依存導入はしていない。
- AWS の実稼働版、CodeBuild/S3 公開状況、Terraform state、最新 scan、ECR 例外の実運用状態、rollback/drill。いずれも今回の禁止事項に従い未照合。
- 対応 wheel と Alpine 3.15、世代再導出手段・公開/adopt 経路がクリティカルパス。これらの確認が済むまでは 11-20 全体投入を確定しない。

## 6. 段1の実装記録（2026-10-30）

本節は上記の取得なし調査に続く実装結果。`uv.lock`、CI、CI の回帰テスト、本節を変更した。アプリのコード、`pyproject.toml` の最低対応 Python、media の APK / Dockerfile / 世代・契約は変更していない。commit / push、AWS 実行、`.env` / secrets の読み取りは行っていない。

### 依存更新

PyPI の JSON メタデータと更新後の lock の実ファイル名を照合した。以下の「両 Linux」は通常 CPython 3.15 に適合する `cp315-cp315` または `cp3N-abi3`（N ≤ 15）の **manylinux aarch64 / x86_64 両方**を意味する。Alpine / musllinux の実動作確認ではない。

| §2.1 の対象 | 更新前 → 更新後 | 確認結果・例外 |
|---|---|---|
| cffi | 2.0.0 → 2.1.1 | 両 Linux |
| httptools | 0.8.0 → 0.9.0 | 両 Linux |
| jiter | 0.15.0 → 0.18.0 | 両 Linux |
| librt | 0.11.0 → 0.16.0 | 両 Linux |
| lxml | 6.1.1 → 6.1.3 | 両 Linux |
| markupsafe | 3.0.3 → 3.0.4 | 両 Linux |
| numpy | 2.4.6 → 2.5.4（Python ≥3.15 のみ） | 両 Linux。3.14 以下は 2.4.6 を維持 |
| psycopg-binary | 3.3.4 → 3.3.6 | 両 Linux。親の psycopg も 3.3.6 に揃えた |
| pydantic-core | 2.46.4 → 2.50.2 | 両 Linux。親の pydantic も 2.13.4 → 2.14.2 |
| pywin32 | build 311 → 312 | Windows 専用。cp315 の win32 / win_amd64 / win_arm64 wheel を確認。Linux wheel は対象外 |
| pyyaml | **6.0.3 を維持** | [PyPI](https://pypi.org/pypi/PyYAML/json) の最新安定版も 6.0.3。cp315 / abi3 / py3-none の互換 wheel がなく、版更新で解消できない。sdist はあるが 3.15 でのビルドは未検証 |
| regex | 2026.5.9 → 2026.9.29 | 両 Linux |
| scikit-learn | 1.9.0 → 1.9.1 | 両 Linux |
| scipy | 1.17.1 → 1.18.1（Python ≥3.15 のみ） | 両 Linux。3.14 以下は 1.17.1 を維持 |
| torch（PyPI） | 2.13.0 → 2.15.0（非 Linux / Python ≥3.15 のみ） | 非 Linux 用の cp315 macOS arm64 / Windows x86_64 wheel を確認。Linux は既存の CPU index 2.13.0+cpu を維持し、その cp315 両 Linux wheel を確認 |
| uvloop | 0.22.1 → 0.23.0 | 両 Linux |

必要な推移依存も `typing-extensions 4.15.0 → 4.16.0`、`typing-inspection 0.4.2 → 0.4.4` に更新した。メジャー更新はない。pywin32 の数字は build 番号、regex は日付版。

NumPy 2.5 の型定義は Python 3.12 以降の構文を使い、lock 内の mypy 2.1.0 と既存の `python_version = "3.11"` の組では解析エラーになる。このため NumPy / SciPy は 3.15 の解決だけ新版へ分岐し、最低対応版の静的検査と 3.14 以下の数値計算依存を維持した。別の隔離環境では最新 mypy 2.4.1 + NumPy 2.5.4 の最小 import 型検査が同じ Python 3.11 設定で成功し、既存 latest-dependency CI に追加の NumPy 制約は加えていない。torch も既存の 3.14 以下と Linux CPU 版を維持し、本番 Dockerfile が参照する wheel URL / SHA が lock から消えないことを確認した。media 専用の yt-dlp / curl-cffi / Playwright / python-pptx / Pillow / WeasyPrint の版と wheel / sdist は更新前と一致する。

再解決には `uv lock --upgrade-package '<name><次のメジャー>'` を対象名ごとに指定し、親の psycopg / pydantic も指定した。その後、既存経路を維持する分岐を以下で解決した（どちらも lock を生成する一時制約で、project の恒久的な版制約は追加していない）。

```sh
uv lock \
  --upgrade-package "numpy==2.4.6; python_version < '3.15'" \
  --upgrade-package "numpy==2.5.4; python_version >= '3.15'" \
  --upgrade-package "scipy==1.17.1; python_version < '3.15'" \
  --upgrade-package "scipy==1.18.1; python_version >= '3.15'"
uv lock \
  --upgrade-package "torch==2.15.0; sys_platform != 'linux' and python_version >= '3.15'" \
  --upgrade-package "torch==2.13.0; sys_platform == 'linux' or python_version < '3.15'"
```

### CI と検証

`.github/workflows/ci.yml` に `python315-pytest` を追加した。既存 lockfile ジョブと同じ PostgreSQL / Terraform / Node / ffmpeg 準備、`uv export --frozen --extra dev --extra mcp --extra media`、`pip --no-deps --require-hashes` と editable 導入を使い、既存の duration-balanced 3 shard を実行する。ジョブ全体を `continue-on-error: true` とし、summary は完了を待って 3.15 の manifest 全件照合だけを別の `continue-on-error` step で行う。既存の 3.14 coverage と 3.11 / 3.13 / 3.14 の必須ゲートは維持した。追加した回帰テストは、この非必須性、frozen 導入、3 shard、artifact / summary の接続を確認する。

- 手元: CPython 3.14.7、uv 0.11.14。3.15 interpreter は未インストールのため、3.15 の pytest / mypy は未実行。GitHub Actions の実行も commit / push 禁止のため未実施。
- `uv lock --check` と `uv export --frozen --no-emit-project --extra dev --extra mcp --extra media` は成功。
- lock 内の対象 wheel をスクリプトで照合し、PyYAML 以外の Linux 対象 13 件は両アーキテクチャの互換 wheel を確認。pywin32 は Windows、PyPI torch は非 Linux として別判定した。
- export した依存に対し `uv pip install --python-version 3.15 --python-platform <aarch64|x86_64>-manylinux_2_28 --only-binary :all: --no-deps --dry-run` を実行。両方とも **PyYAML 6.0.3 の wheel 不足で失敗**。PyYAML だけを除いた同じ dry-run は両方成功した。これはインストール / import / アプリ実行の成功ではない。
- 3.14 の `ruff check src/ tests/ scripts/`、`ruff format --check src/ tests/ scripts/` は成功。`mypy src/teamagent` は **507 source files、問題なし**。
- CI 設定と既存 core / media Dockerfile 契約の関連 pytest は **38 passed**。
- 最初の全件完走は **121 failed / 16,199 passed / 439 skipped**。119 件は、検証用に加えた `AWS_CONFIG_FILE=/dev/null` / `AWS_SHARED_CREDENTIALS_FILE=/dev/null` が既存 guard の環境変数拒否仕様に衝突したことによる手順上の失敗。残る 2 件は rollback 試験の 30 秒 timeout。完全な出力は `/private/tmp/teamagent-py315-survey/pytest314-final.log` に保存した。
- ローカルで継承された `C.UTF-8` は bash に大量の locale 警告を出したため、再検査では `LC_ALL=C LANG=C` とした。上記 AWS 設定ファイル変数は渡さず、資格情報の環境変数も引き継がない。AWS 操作の試験は既存の fake command を使用する。locale 修正後も並列実行の timeout は残り、locale だけで解決したとはしていない。
- この再検査は指定の `uv run --python 3.14 --extra dev --extra mcp --extra media pytest -q -n auto` に、配分粒度と診断出力だけの `PYTEST_ADDOPTS='--maxschedchunk=1 --durations=20'` を加えて実行した。**8 failed / 8,133 passed / 352 skipped、41 分45秒、約50%で中断**。全失敗は未変更の `test_forced_rollback_drill.py`（3 件、30 秒）と `test_terraform_runtime_guard.py`（5 件、120 秒）の `subprocess.TimeoutExpired`。除外や timeout 緩和はしていない。完全な出力は `/private/tmp/teamagent-py315-survey/pytest314-verified.log` に保存した。
- timeout した 8 件を、同じ更新済み依存・正常な locale で単独再検査し、**8 passed in 485.49s**。完全な出力は `/private/tmp/teamagent-py315-survey/pytest-timeout-check.log` に保存した。最長ケースは 116.84 秒で 120 秒制限に近く、並列負荷による実行時間の影響が考えられるが、依存更新との因果関係までは断定していない。**3.14 の全体成功条件は未達**であり、「3.14 の既存挙動をすべて検証済み」とはしない。単独成功と並列全体成功は区別する。全体を通せる実行環境で、通常配分の指定コマンドを再検証する必要がある。

残る停止要因は、3.14 全体 pytest の timeout と全件成功未確認、PyYAML の 3.15 wheel 不足（更新先なし）、実際の 3.15 frozen install / pytest 未確認、本番 arm64 core と embeddings の実 image 検証、および未着手の media APK / Dockerfile / 世代更新。3.15 の lint / typecheck まで広げる場合は、NumPy 2.5 の型定義と mypy の最低版設定の整合も別途必要。今回の変更だけでは段1の検証完了や本番移行完了とはしない。
