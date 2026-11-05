あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点の feat/vendor-media-apks-20261030）。日本語で報告する。

状況: media イメージの APK を S3 に自前保管してオフライン導入する実装（未コミットの変更 25 ファイル・report-vendor-apk.md は /private/tmp/vendor-apk-refresh と前の worktree ~/dev/worktrees/teamagent-vendor-apk にある）を、最新 dev の上に載せた。前回の import では lock 240 件のうち 23 件（alpine-release・apk-tools・libapk・chromium 系 4・mesa 系 5・ca-certificates-bundle・libpng・sqlite-libs・xz-libs・cups-libs・ffmpeg-libavformat・libopenmpt・llvm22-libs・pipewire-libs・libseccomp・bubblewrap）が Alpine の CDN から消えていて取れなかった。取得済みの 217 件は /private/tmp/vendor-apk-refresh/cdn-apks にある。

考え方: media のビルドは digest 固定の Chromium ベースイメージの上で apk add する。ベースに既に入っている版の APK はビルド中に取得されないので、自前保管する必要がない。保管すべきは「lock にあり、かつベースイメージに同じ版で入っていないもの」だけ。

やること:
1. ベースイメージ（Dockerfile の digest 固定のもの）に入っているパッケージと版を、ネットワークを切ったコンテナで `apk info -v` 等から取り（Docker が使えなければ、既存の証跡・テストフィクスチャ・lock の生成手順から確実に分かる方法を探す）、lock 240 件を「ベース由来」と「追加導入」に分ける。
2. vendor_media_apks.py・media-apk-sha256.lock・media-apk-source.json・Dockerfile・契約・テストを、追加導入の分だけを保管・照合する形に直す。ベース由来の分は、導入後の実 inventory と lock の突き合わせ（従来の cmp）で引き続き固定されることを確認する。
3. 消えた 23 件がすべてベース由来か確かめる。ベース由来でないものが残った場合は、その名前と、Alpine に今ある版を報告し、lock を上げるべきかの判断材料を書く（lock 自体は上げない）。
4. `--apk-dir /private/tmp/vendor-apk-refresh/cdn-apks` を使って import をやり直し、署名と SHA を確かめた bundle と未公開の source JSON をローカルに作る。bundle の件数・SHA を報告する。
5. 関連テストを `uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/infra tests/codebuild tests/scripts tests/skills/first_visit` で通す。commit 済みソースを要求する guard で落ちるものは、そうだと分かる形で区別して報告する。ruff と mypy（変更ファイル）も通す。
6. report-vendor-apk.md を新しい形に合わせて書き直す。末尾の「小俣さんが実行するコマンド」（publish と、必ず -var-file 付きの terraform apply）も合わせる。

禁止: AWS への書き込み（publish・apply・put-object 等）、git commit・push、.env・secrets・~/.aws・~/.ssh の読み取り。
最後に、変更ファイル一覧・テスト結果・import 結果・残る不足を短くまとめる。
