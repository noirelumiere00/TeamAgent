# 動画ライブラリ P3（2026-10-30）

## URL と動作

ホストは `https://connect.newstv.co.jp`。公開・デプロイはこの作業では行っていない。

| URL | 内容 |
| --- | --- |
| `/library` | 50件ずつの一覧、検索・絞り込み・並べ替え、カーソル再生、選択URLコピー |
| `/library/v/{platform}/{video_id}` | 詳細、オーガニック／総計／広告の分離、構成の受け皿 |
| `/library/admin/campaigns` | 管理者向け案件設定、PRあり・案件なしの件数 |
| `/library/admin/accounts` | 管理者向けTikTokアカウント登録・一覧（取得はP3b） |
| `/library/api/videos` | 一覧と同じ検索、最新スナップショット、P1の率計算のJSON |
| `/library/thumb/{platform}/{video_id}` | 認証付きS3画像配信、privateキャッシュとETag |

動画の有効値は `vl_videos_effective` から読み、設定変更が次のリクエストで効く。一般利用者は非公開動画を一覧・詳細・サムネで閲覧できず、管理者は確認できる。再生・保存率・ENG率の一覧／並べ替えは**オーガニック**を使い、総計で補完しない。最新の指標が欠損なら算出不可のまま。5,000再生未満の率は参考値とする。

検索は投稿文・ハッシュタグ・アカウント名／ハンドル・広告主・案件名。期間は固定UTC+9の暦日で、終了日を含む。管理POSTは既存 `/admin/analyze` と同じ完全一致のOrigin検証を行う。HTMLはエスケープし、nonce付きの自前JS/CSSを使う。TikTokのiframe許可はlibraryのHTMLレスポンスだけに加える。

## 変更ファイル

- `infra/migrations/0036_video_library_settings.sql`: 設定2テーブル、分析テーブル、ビュー、RLS（0035は変更なし）
- `src/teamagent/adapters/video_library_storage.py`: 固定prefix内のS3読書き
- `src/teamagent/ingest/video_library_thumbs.py`: 埋め込みJSONのカバー取得、上限・間隔・停止・集計ログ、単独CLI
- `src/teamagent/connect_web/library.py`: 検索SQL、画面、API、管理POST、認証ゲート、サムネ配信
- `src/teamagent/connect_web/app.py`: 既存認証・管理者・DB・HTML応答の受け渡しと戻り先検証
- `scripts/run_ingest_fargate.py`: ingest成功後に同期→サムネ、個別の失敗を終了コードに波及させない（dry-run時サムネなし）
- `infra/terraform/ingest_schedule.tf`: 既存media_jobs内の対象prefixへのPutObject/GetObjectとバケットenv
- `infra/terraform/connect_web.tf`: 同prefixへのGetObjectとバケットenv
- `.github/workflows/ci.yml`: 新規直接importのbotocoreを依存リストに明示
- `tests/connect_web/test_library.py`: 認証・管理者・戻り先・CSP・エスケープ・検索・キャッシュ・管理編集
- `tests/connect_web/test_library_js.py`: 配信JSのiframe破棄・1枚制限・チェック済みURLコピー
- `tests/ingest/test_video_library_thumbs.py`: 実際の埋め込みJSON構造、HTTP・S3・上限・停止
- `tests/ingest/test_video_library_settings_postgres.py`: 実DBのビュー／RLS／即時反映／最新指標
- `tests/scripts/test_run_ingest_fargate.py`: 呼出順、dry-run、同期／サムネ失敗時の終了コード
- `tests/scripts/test_terraform_runtime_contracts.py`: タスクのバケットenv契約
- `tests/infra/test_video_library_terraform.py`: 最小権限とライフサイクル対象外の契約
- `docs/v3.2/ops/video_library.md`: 本書

## デプロイ時の手順

1. `CONNECT_ADMIN_EMAILS` と `CONNECT_SEARCH_ALLOWED_EMAILS` が本番で明示され、利用する全社員と管理者の設定が正しいことを確認する。既存の個人fallbackに頼らない。
2. 通常のmigration手順（`scripts/migrate.py`）で0035→0036を適用する。0036は追加のみ。同期は設定テーブルを上書きしない。
3. 既存のguarded image／Terraformの手順に従う（`infra/terraform/README.md`）。新しいAWSリソースは作らない。`media_jobs` が有効な構成で、ingestとconnect_web双方の `VIDEO_LIBRARY_S3_BUCKET` とprefix権限が入ることをplanで確認する。ガード対象ファイルはコミット済みである必要がある。
4. 既存の `scripts/aws/run_ingest_task.sh` 経路で同期とサムネを動かす。taskdefの直接登録やapplyはしない。この変更は既存ingestのスケジュール式を変更しないため、運用上必要な同期頻度も確認する。
5. 初回埋めの単独実行口は、同じ環境変数・DB権限・バケット権限の環境で `python -m teamagent.ingest.video_library_thumbs --limit 300`。既定上限は `VIDEO_LIBRARY_THUMB_LIMIT=300`、1件間隔は1秒、429は即終了、連続失敗5件で終了。NULLのTikTokだけを対象にし、欠損投稿が後続を塞がないよう順序はランダム。未成功分は次回へ残る。初回全件を1回で取得しない。
6. 実ヘッダ付きのconnect_webで、一般利用者／管理者の一覧・期間検索・カーソル再生→離脱・URLコピー・詳細・案件編集→即反映・非公開・アカウント登録・サムネキャッシュを確認する。TikTokからの本当の再生とS3への取得を含む。

サムネはTikTokの埋め込みJSONのzoomCover[480]、なければcover。JPEGのマジックバイトと最大5MiBを確認し、許可されたTikTok CDNのHTTPS URLだけを取得する。HTTPリダイレクトは追わない。外部URL・投稿文・例外本文を集計ログへ出さない。保存先は `video-library/thumbs/{platform}/{video_id}.jpg`。`media-jobs/` の期限付き成果物とは別で、既存の自動削除対象に入らない。

## 検証結果

- 依存環境: 指定の `uv sync --extra dev --extra mcp --extra media` 相当を既存キャッシュの作業用コピーでオフライン同期。ネットワーク経由の同期はPyPIのDNS解決失敗。`import-linter` は同じキャッシュから追加した（CIの既存依存）。
- `mypy --strict src`: 516ファイル成功。
- `ruff check src/ tests/ scripts/`、`ruff format --check src/ tests/ scripts/`: 成功。
- import-linter: 2契約とも成功。
- Terraform: 変更2ファイルの `terraform fmt -check` 成功。apply／本番planは未実施。
- 関連pytest: **1,515 passed / 23 skipped**（connect_web全体、ingest全体、該当scripts・infra契約、no-IANA）。全skipは実DB未設定によるもの。
- 追加の `tests/scripts/test_ops_shell_scripts.py`: クリーンな検証用コピーで **29 passed**。元worktreeでの実行は2件が、既存guardの「Terraformソースに未コミット差分がある」という検査で停止した。ガードを弱めず、変更を含む一時Gitスナップショットで29件を実行して成功を確認した。元worktreeは未コミットのままなので、この2件をそこで走らせると同じ停止になる。
- pytest・型・import境界の完全な出力は `/tmp/video-library-final-pytest.log`、`/tmp/video-library-clean-ops.log`、`/tmp/video-library-final-mypy.log`、`/tmp/video-library-final-imports.log` に保存。広げた検査での元worktreeの失敗全文は `/tmp/video-library-infra.log`。破壊確認の結果は `/tmp/video-library-mutations.json`、各失敗全文は `/tmp/video-library-mutation-*.log`。

最終pytestの範囲:

```bash
.venv/bin/pytest -q tests/connect_web/ tests/ingest/ \
  tests/scripts/test_run_ingest_fargate.py \
  tests/scripts/test_terraform_runtime_contracts.py \
  tests/scripts/test_runtime_minimal_reads.py \
  tests/scripts/test_slack_team_id_terraform.py \
  tests/infra/test_hmac_rollout_terraform.py \
  tests/infra/test_media_terraform_hardening.py \
  tests/infra/test_report_shortlink_iam_terraform.py \
  tests/infra/test_runtime_contract.py \
  tests/infra/test_video_library_terraform.py \
  tests/runtime/test_no_iana_timezone.py
```

元worktreeではコミット・push・PR作成を行っていない。一時Gitスナップショットはローカル検証専用。

主なテスト名:

- `test_view_matches_resolver_and_settings_change_without_sync`（PR有無×設定、非公開、即時反映、P1との一致）
- `test_settings_rls_and_security_invoker`、`test_search_uses_latest_snapshot_and_settings_immediately`
- `test_all_routes_require_cookie`、`test_admin_gate`、`test_next_rejects_redirect_tricks`、`test_login_returns_to_library_with_query`
- `test_listing_csp_escaping_copy_and_single_player`、`test_detail_metrics_are_separate_and_caption_escaped`
- `test_search_api_matches_listing_and_hides_s3_keys`、`test_thumbnail_cache_and_authenticated_304`
- `test_admin_forms_require_origin_and_save_as_admin`
- `test_single_player_disposal_and_checked_urls_clipboard`
- `test_cover_from_real_hydration_shape`、`test_http_failure_stops`、`test_limit_interval_and_s3_key`、`test_no_json_skips_and_is_retried_next_run`、`test_cover_429_also_stops`
- `test_ingest_success_runs_sync_after_ingest_with_resolved_yaml`、`test_sync_failure_keeps_successful_ingest_exit_code`、`test_thumbnail_failure_preserves_ingest_status`
- `test_library_thumbnail_iam`、`test_video_library_bucket_env_uses_existing_media_bucket`、`test_library_thumbnails_do_not_expire_with_job_artifacts`
- `test_runtime_code_does_not_use_iana_time_zones`、`test_every_module_imports_without_time_zone_data`

破壊確認（いずれも一時改変後に対応テストの失敗を確認し、原本へ復元）:

| 破壊した規則 | 失敗したテスト |
| --- | --- |
| zoomCover 480を960へ変更 | `test_cover_from_real_hydration_shape` |
| HTTP 429のbreakを除去 | `test_http_failure_stops` |
| HTMLエスケープを除去 | `test_listing_csp_escaping_copy_and_single_player` |
| nextの検証を「/で始まるだけ」に弱体化（///library等も拒否するテストあり） | `test_next_rejects_redirect_tricks` |
| cookieなしを匿名利用として通す | `test_all_routes_require_cookie` |
| 次のiframe作成前の破棄を除去 | `test_single_player_disposal_and_checked_urls_clipboard` |
| コピー対象から:checkedを除去 | 同上 |
| サムネmax-ageを60秒へ変更 | `test_thumbnail_cache_and_authenticated_304` |
| ingestのPutObjectを除去 | `test_library_thumbnail_iam` |

未実施: `TEAMAGENT_TEST_DB_DSN` は未設定で実DBテストはスキップ。DB接続を伴う検証とビュー規則の実DB破壊確認はレビュー側で実施する。ブラウザ実画面は、サンドボックスがlocalhostのbindとChromiumの起動を拒否したため未確認。TestClientの実レスポンスヘッダとNodeの配信JS検証は実施したが、実ブラウザ・本番TikTok・S3疎通の代替とはしていない。本番データ2,811本の件数確認・migration適用・取得ジョブ実行・デプロイ・push・PR作成は実施していない。
