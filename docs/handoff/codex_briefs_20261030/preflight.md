あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
背景: 本番便が「撃ってから」上流の変化で止まる事故が 11/02 だけで 4 回あった: (1) Alpine の ffmpeg 8.1.2-r3 が消えた（media の apk pin）(2) core の土台の py3-pip-wheel に新しい HIGH (3) core の python-3.14 に新しい CRITICAL（ECR の門）(4) OpenClaw の Slack plugin の follow-redirects に新しい HIGH。さらに CI の trivy fs で pyasn1/pypdf の HIGH。どれも便の段 3・段 4・CI の門で初めて分かり、直し→再導出→撃ち直しで 1〜2 時間ずつ失った。
やりたいこと: 便を撃つ前（と毎朝）に、手元で同じ門を先回りして見る「発射前の脆弱性チェック」を 1 本のスクリプトにする。既存の部品を使う: ~/Documents/Claude/Artifacts/aico-inputs-20260911/apkwatch/（prefire_apk_check.sh・watch_media_apk_drift.sh・check_media_apk_pins.py）、infra/codebuild/ecr_scan_exceptions_*.json（例外と stale=fail）、infra/docker/ の各 Dockerfile と lock・vendor、Trivy（手元にある・DB は ghcr.io/aquasecurity/trivy-db:2 を使い、社内の TLS は SSL_CERT_FILE=$HOME/.hermes/ca_bundle.pem）。
やること:
1. scripts/release_preflight_vuln.py（または .sh）を作る。見るもの: (a) core の土台 image（Dockerfile.teamagent-mcp の digest）に、差し替え済みの APK（core-*-lock.json・vendor）を反映した上での OS パッケージの MEDIUM 以上（ECR の門と同じ基準: 例外にあるものは許可・例外にあって見つからないもの＝stale も失敗）(b) uv.lock の HIGH/CRITICAL（CI の trivy fs と同じ）(c) OpenClaw の Dockerfile.openclaw で差し替える npm パッケージ（npm pack 済みの版）と plugin の lock の HIGH/CRITICAL (d) media の apk pin が上流にあるか（既存の prefire_apk_check を呼ぶ）。
2. 出力は「便が止まる見込みの項目」と「直し方の候補（修正版の版）」を短い表で。終了コードで止まる/止まらないを返す。ネットワークは公開レジストリ・Alpine/Wolfi の公開索引・Trivy DB だけ。AWS は読み取りの ECR 例外ファイル（リポジトリ内）だけで、AWS API は呼ばない。
3. テスト: 例外・stale・修正版の判定ロジックを、固定の Trivy JSON（フィクスチャ）で確かめる。`uv run --extra dev --extra mcp pytest -q tests/scripts` と ruff・mypy。
4. 使い方を docs/release_preflight_vuln.md に（便の前に 1 回・毎朝の見張りに組み込む方法）。
禁止: git commit・push、AWS への書き込み、.env・secrets・~/.aws の読み取り。最後に変更点・手元で実行した結果（いまの dev で止まる項目があるか）・テスト結果を短く。
