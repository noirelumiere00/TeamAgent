# 発射前の脆弱性チェック

便を承認・発射する前に、対象 worktree で次を1回実行する。Python 3.11 以上の標準ライブラリ、Trivy（検証版 0.72.0）、起動済み Docker と buildx が必要。AWS CLI や npm は使わない。

```bash
cd /Users/s-komata/dev/worktrees/teamagent-preflight
SSL_CERT_FILE="$HOME/.hermes/ca_bundle.pem" python3 scripts/release_preflight_vuln.py
```

省略時も `~/.hermes/ca_bundle.pem` が存在すれば使用する。未コミットの Dockerfile・lock・vendor の変更も対象になる。自動修正・依存更新・git fetch はしない。

| 終了コード | 意味 | 発射前の扱い |
| --- | --- | --- |
| 0 | 指定した4系統に停止見込みなし | 通常の便の門へ進む |
| 1 | 脆弱性、stale 例外、APK 消失を検出 | 表の項目を直して再実行 |
| 2 | DB・ネットワーク・Docker・入力などが確認不能 | 検査を復旧して再実行 |

最後に「便が止まる見込みの項目」と「直し方の候補」の表を出す。`FixedVersion` は Trivy が示す修正版候補をそのまま表示する。複数の系列がある場合は全候補を表示し、未公開なら「修正版未公開」とする。異なる APK・Python・npm の版を共通の数値比較で選ばない。候補への更新後は lock/vendor と必要な契約値を再導出し、このチェックと本来の門を再実行する。

証跡（Trivy JSON、ビルド・実行ログ、検査に使った入力）は毎回新しい `/tmp/release-preflight-*` に残る。保存先は `--output-dir /path/to/new-directory`、外部 APK チェックの場所は `--apkwatch /path/to/apkwatch`、対象は `--repo /path/to/worktree` で変更できる。ログは失敗時も省略せず保存する。DB キャッシュは対象リポジトリの `.cache/release-preflight/trivy` に再利用する。DB は毎回 Trivy の更新判定を通し、取得先を `ghcr.io/aquasecurity/trivy-db:2` に限定する。更新失敗時は古い DB で続行しない。

DB 更新・Docker ビルド・Trivy・media チェックの各子プロセスは5分で打ち切り、確認不能にする。

## 検査対象

| 対象 | 入力と判定 |
| --- | --- |
| core OS | `Dockerfile.teamagent-mcp` の digest 固定 Wolfi runtime/builder から、同じ Dockerfile の APK 差し替えブロックだけを実行。`core-*.lock.json`・vendor とハッシュ照合・既存の `replace_core_ca_bundle.py`・Brotli のオフライン APK 適用を再利用し、差し替え後 runtime rootfs を Trivy で検査。MEDIUM/HIGH/CRITICAL を停止対象にする。 |
| core 例外 | `infra/codebuild/ecr_scan_exceptions_core.json` を既存 ECR 門の厳密なローダーで読む。CVE・severity・package・installed version の完全一致のみ許可。期限切れ・重複・不正形式は確認不能、検出にない例外は stale として停止する。LOW の例外も stale 照合には含める。 |
| uv.lock | lock だけを隔離して Trivy fs。CI と同じ HIGH/CRITICAL・修正版あり（`ignore-unfixed` 相当）・`.trivyignore.yaml` を適用する。開発用を含む lock 全体を対象にする。 |
| OpenClaw | Dockerfile の `npm pack name@exact-version` を読み、その公開 npm tarball の integrity と package identity を確認。指定版を npm lock inventory にして検査する。`plugins-lock.json` の各公開 tarball と shrinkwrap の SHA-256 も確認し、Dockerfile の実際の展開先ごとに shrinkwrap を差し替えて検査する。親ディレクトリの差し替えで消える旧 private dependencies も除去する。HIGH/CRITICAL は修正版未公開でも停止。 |
| media APK | 外部 `apkwatch/prefire_apk_check.sh` の一時コピーを呼び、同梱 `check_media_apk_pins.py` と base inventory で公開 Alpine 索引を照合。HEAD ではなく worktree を読むようにし、ECR を使う shadow build を無効化する。消えた版と上流の候補版を表に出す。外部スクリプトの構造が変わった場合は確認不能にする。 |

OpenClaw の npm inventory は全 runtime イメージの検査ではなく、今回指定された差し替え npm と plugin lock の検査。ECR 例外は core OS にだけ適用する。media/OpenClaw の OS やアプリ全体は対象外なので、それらの ECR 例外を部分検査に当てて stale と判定しない。

Trivy と ECR は脆弱性データ源・severity が異なるため、これは停止の予測であり ECR 結果の同一性は保証しない。既存 ECR 門は LOW/INFORMATIONAL 等も対象にしているが、この事前チェックの core 停止対象は依頼範囲の MEDIUM 以上。media の新しい依存や依存解決の変化は索引だけでは確定できず、WARN として `media.log` に残る。通常の便のビルドと ECR 門は引き続き必要。

通信先は公開コンテナレジストリ（Docker Hub の Dockerfile frontend、`cgr.dev`）、公開 npm レジストリ、公開 Alpine 索引、GHCR の Trivy DB。Trivy の追加依存照会は offline scan で抑止する。コンテナビルドのコンテキストには APK 差し替えの明示入力だけをコピーする。子プロセスは一時 HOME と認証情報なしの Docker 設定で実行し、AWS 認証・npm auth・Trivy の利用者設定を継承しない。`.env`・secrets・`~/.aws` は読まず、AWS API は呼ばない。

## 毎朝の見張り

既存の朝の監視ジョブから同じコマンドを呼び、終了コード 1 と 2 の両方を失敗として扱う。毎回新しい証跡ディレクトリを渡す。Docker が使える実行ホストで設定する。cron の例（毎朝 08:00、PATH はホストに合わせる）:

```cron
0 8 * * * PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin; export PATH; cd /Users/s-komata/dev/worktrees/teamagent-preflight && SSL_CERT_FILE="$HOME/.hermes/ca_bundle.pem" python3 scripts/release_preflight_vuln.py >> /tmp/release-preflight-morning.log 2>&1
```

既存ジョブに接続する場合は、後続の `echo` 等で終了コードを上書きしない。

```bash
python3 scripts/release_preflight_vuln.py --repo "$PAD"
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "発射前チェック失敗（exit $rc）: 表と証跡を確認"
  exit "$rc"
fi
```

cron 自体の登録や通知送信はこのスクリプトでは行わない。監視する worktree は既存の更新手順で最新に保つ。

## 開発時の検証

```bash
uv run --extra dev --extra mcp pytest -q tests/scripts
uv run --extra dev --extra mcp ruff check scripts/release_preflight_vuln.py tests/scripts/test_release_preflight_vuln.py
uv run --extra dev --extra mcp ruff format --check scripts/release_preflight_vuln.py tests/scripts/test_release_preflight_vuln.py
uv run --extra dev --extra mcp mypy --follow-imports=silent --explicit-package-bases scripts/release_preflight_vuln.py tests/scripts/test_release_preflight_vuln.py
```

固定 Trivy JSON で、閾値、例外の完全一致・stale・期限切れ・重複、修正版あり/なし/複数候補を検証する。加えて APK ブロックの抽出、plugin の展開先と private dependency の差し替え、公開 URL 制限、media の公開索引アダプター、終了コードを検証する。

既存のメディア関連テストの収集で `No module named 'PIL'` になる環境では、検証用仮想環境に `uv pip install pillow` で補完してから再実行する。本スクリプトは Python の追加依存を使わず、`pyproject.toml`・`uv.lock` は変更しない。

既存の `tests/scripts/test_terraform_runtime_guard.py` は `infra/`・`scripts/` がクリーンな HEAD であることを要求する。未コミットの本スクリプトがある開発中は、そのモジュールのセットアップが失敗する。そのモジュールだけは同じ HEAD のクリーンな一時複製で検証し、本チェックの新規テストは変更を含む worktree で実行する。
