あなたは teamagent リポジトリの開発者。作業場所はこの worktree（branch feat/vendor-media-apks-20261028）。日本語で報告する。

目的: media イメージの APK を S3 に自前保管してオフライン導入する実装（直前のコミット 2bceec4e と report-vendor-apk.md を読むこと）を、最新の origin/dev に追いつかせ、初回取り込みの直前まで準備する。

やること:
1. `git merge origin/dev` で取り込む（dev は 48 コミット先。#736 で nodejs が 24.22.1-r1 に、#738 で sales25 キット pin が変わり、世代の再導出が入っている）。衝突は dev 側の最新 pin・世代を正として解く。infra/docker/media-apk.lock は dev の内容を正とする。
2. 実装側（vendor_media_apks.py・media-apk-sha256.lock・media-apk-source.json・Dockerfile・契約・テスト）を、最新の media-apk.lock に合わせて整える。
3. `python infra/docker/vendor_media_apks.py import ...`（使い方はスクリプトの --help で確認）をこの端末で実行し、最新 lock 全件の APK を Alpine の公開 CDN から取って、署名と SHA を確かめた bundle と未公開の source JSON をローカルに作る。Docker が使えない場合はその段だけ不足として報告する。
4. 関連テストを `uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/infra tests/codebuild tests/scripts tests/skills/first_visit` で通す。ruff と mypy（変更ファイル）も通す。
5. report-vendor-apk.md を更新し、末尾に「小俣さんが実行するコマンド」として publish（S3 保存）と terraform apply（必ず -var-file 付き）の正確な手順を書く。

禁止: AWS への書き込み（publish・apply・put-object 等）は一切しない。git commit・push はしない（Claude が行う）。.env・secrets・~/.aws・~/.ssh は読まない。
最後に、変更ファイル一覧・テスト結果・import の結果（件数・bundle の SHA）・残る不足を短くまとめる。
