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

動画の有効値は `vl_videos_effective` から読み、設定変更が次のリクエストで効く。一般利用者は非公開動画を一覧・詳細・サムネで閲覧できず、管理者は確認できる。再生・保存率・ENG率の一覧／並べ替えとAPIの `rates` は**総計**を使う。オーガニック再生は値がある場合に併記する。最新の指標が欠損なら算出不可のまま。5,000再生未満の率は参考値とする。

検索は投稿文・ハッシュタグ・アカウント名／ハンドル・広告主・案件名。期間は固定UTC+9の暦日で、終了日を含む。管理POSTは既存 `/admin/analyze` と同じ完全一致のOrigin検証を行う。HTMLはエスケープし、nonce付きの自前JS/CSSを使う。TikTokのiframe許可はlibraryのHTMLレスポンスだけに加える。

## 変更ファイル

- `infra/migrations/0036_video_library_settings.sql`: 設定2テーブル、分析テーブル、ビュー、RLS（0035は変更なし）
- `src/teamagent/adapters/video_library_storage.py`: 固定prefix内のS3読書き
- `src/teamagent/ingest/video_library_thumbs.py`: 埋め込みJSONのカバー取得、上限・間隔・停止・集計ログ、単独CLI
- `src/teamagent/connect_web/library.py`: 検索SQL、画面、API、管理POST、認証ゲート、サムネ配信
- `src/teamagent/connect_web/app.py`: 既存認証・管理者・DB・HTML応答の受け渡しと戻り先検証
- `scripts/run_ingest_fargate.py`: ingest成功後に同期→サムネ、個別の失敗を終了コードに波及させない（dry-run時サムネなし）
- `infra/terraform/ingest_schedule.tf`: 常設の raw_files バケットの `video-library/thumbs/` prefix への PutObject/GetObject とバケット env（media_jobs は media 機能 OFF の本番に存在しないため使わない）
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
3. 既存のguarded image／Terraformの手順に従う（`infra/terraform/README.md`）。新しいAWSリソースは作らない。ingestとconnect_web双方の `VIDEO_LIBRARY_S3_BUCKET` とprefix権限が入ることをplanで確認する。ガード対象ファイルはコミット済みである必要がある。
4. 既存の `scripts/aws/run_ingest_task.sh` 経路で同期とサムネを動かす。taskdefの直接登録やapplyはしない。この変更は既存ingestのスケジュール式を変更しないため、運用上必要な同期頻度も確認する。
5. 初回埋めの単独実行口は、同じ環境変数・DB権限・バケット権限の環境で `python -m teamagent.ingest.video_library_thumbs --limit 300`。既定上限は `VIDEO_LIBRARY_THUMB_LIMIT=300`、1件間隔は1秒、429は即終了、連続失敗5件で終了。NULLのTikTokだけを対象にし、欠損投稿が後続を塞がないよう順序はランダム。未成功分は次回へ残る。初回全件を1回で取得しない。
6. 実ヘッダ付きのconnect_webで、一般利用者／管理者の一覧・期間検索・カーソル再生→離脱・URLコピー・詳細・案件編集→即反映・非公開・アカウント登録・サムネキャッシュを確認する。TikTokからの本当の再生とS3への取得を含む。

サムネはTikTokの埋め込みJSONのzoomCover[480]、なければcover。JPEGのマジックバイトと最大5MiBを確認し、許可されたTikTok CDNのHTTPS URLだけを取得する。HTTPリダイレクトは追わない。外部URL・投稿文・例外本文を集計ログへ出さない。保存先は `video-library/thumbs/{platform}/{video_id}.jpg`。`media-jobs/` の期限付き成果物とは別で、既存の自動削除対象に入らない。

## 初回P3の検証結果

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


## 実データレビュー後の修正

- `/library` とその配下だけ `Referrer-Policy: same-origin`。通常HTMLフォームのOrigin検証を維持し、`Origin: null` は403。他ページは `no-referrer` のまま。
- 同期時に負の指標をNoneへ変換し、セルごとに `negative_metric` を数える。広告の率・CTRは1超もNoneにして `invalid_rate` を数える。元のシート列名は変更しない。既にDBにある負の値は**次回同期が必要**で、過去のスナップショットを一括書換えする処理は加えていない。
- 件数は整数・3桁区切り、広告の率は小数1桁の%、コストは円、平均視聴時間は小数2桁の秒。広告の反応数ラベルを有料いいね／有料シェア／有料コメント／有料保存に変更。
- 一覧は総計再生を主表示し、オーガニックは値がある場合のみ併記。保存率・並べ替え・APIも総計に統一。
- サムネキーがない動画はimg自体を出さない。画像の読み込み失敗時はJSでimgを除去し灰色の枠だけを残す。CSPに反するinline onerrorは使わない。
- 案件の種別からorganicを除外し、POSTでも拒否する。PRなし動画の自動判定は維持。
- 案件設定の上部には、案件一覧が空でも `#PR ありで案件の無い動画: N本` を表示する。

レビュー側のUA修正コミット `e3aa64b2` と整形コミット `d263062e` は保持。サムネ取得モジュール・そのUAテスト・Terraform・migrationは今回変更していない。

回帰テスト:

| 修正 | 主なテスト |
| --- | --- |
| Referrer PolicyとOrigin拒否 | `test_library_referrer_policy_is_same_origin_only` / `test_admin_forms_require_origin_and_save_as_admin` |
| 負の値・率の範囲・件数集計 | `test_negative_metrics_become_missing_and_warn` / `test_rates_above_one_become_missing_and_warn` / `test_rate_boundaries_are_preserved` / `test_negative_metric_warning_counts_cells_without_mixing_families` |
| 単位・有料指標ラベル | `test_metric_display_units` / `test_detail_uses_formatted_metrics_and_paid_labels` |
| 総計を主表示・率も総計 | `test_card_uses_total_plays_and_save_rate` / `test_search_api_matches_listing_and_hides_s3_keys` |
| サムネ欠損・失敗 | `test_missing_thumbnail_has_only_gray_frame` / `test_single_player_disposal_and_checked_urls_clipboard`（配信JSで画像失敗も検証） |
| organic拒否 | `test_admin_pages_escape_and_count_orphan_pr` / `test_campaign_rejects_organic_even_if_category_exists` |
| 案件なしPR件数 | `test_orphan_pr_count_is_visible_without_campaign_rows` |

再検証: 関連pytest **1,556 passed / 23 skipped**（connect_web全体、ingest全体、該当scripts／infra契約、no-IANA）。`mypy --strict src` は516ファイル成功、ruff check／formatとimport-linterの2契約も成功。実DBは `TEAMAGENT_TEST_DB_DSN` 未設定のため未実施。Chromiumでの再確認・実データ再同期・本番反映は実施していない。

破壊確認は次の12件を一時的に改変し、対応テストの失敗を確認して復元した。

| 壊した箇所 | 検出したテスト |
| --- | --- |
| libraryのReferrer Policyをno-referrerへ戻す | `test_library_referrer_policy_is_same_origin_only` |
| 負の値を許す | `test_negative_metrics_become_missing_and_warn` |
| 広告率の上限を2まで許す | `test_rates_above_one_become_missing_and_warn` |
| 件数をfloatのまま表示 | `test_metric_display_units` |
| 保存率をオーガニックに戻す | `test_card_uses_total_plays_and_save_rate` |
| 主表示の再生をオーガニックに戻す | 同上 |
| サムネキーがなくてもimgを出す | `test_missing_thumbnail_has_only_gray_frame` |
| 画像失敗のイベントでimgを除去しない | `test_single_player_disposal_and_checked_urls_clipboard` |
| 案件種別にorganicを表示 | `test_admin_pages_escape_and_count_orphan_pr` |
| organicのPOSTを許す | `test_campaign_rejects_organic_even_if_category_exists` |
| 案件なしPRの件数表示を除去 | `test_orphan_pr_count_is_visible_without_campaign_rows` |
| Origin: nullを許す | `test_admin_forms_require_origin_and_save_as_admin` |

完全な検証出力は `/tmp/video-library-review-pytest.log`、`/tmp/video-library-review-restored.log`、`/tmp/video-library-review-mypy.log`、`/tmp/video-library-review-imports.log`。破壊確認の結果は `/tmp/video-library-review-mutations.json`、各失敗全文は `/tmp/video-library-review-mutation-*.log`。push・PR作成はしていない。

## 事例カタログ UI（2026-10）

上記の旧一覧・灰色の仮表示を更新し、入口を事例カタログに変更した。API `/library/api/videos` の返却形式は維持する。

| ページ | URL | 主な操作 |
| --- | --- | --- |
| 事例カタログ | `/library` | 検索、区分・PR・業種・目的、合計再生／新着／本数順、ばらした再生上位動画 |
| 事例詳細 | `/library/c?advertiser=…&campaign=…` | 全動画、成果、事例まとめ／URLだけコピー |
| 動画一覧 | `/library/videos` | 絞り込み、再生／保存率順、選択URLコピー |
| 動画詳細 | `/library/v/{platform}/{video_id}` | クリック再生、数値、投稿文、事例リンク、構成とタグ |
| 案件管理 | `/library/admin/campaigns` | 既存の設定・権限・Origin検証を維持 |
| アカウント管理 | `/library/admin/accounts` | 既存の登録と権限を維持 |

集計は `vl_videos_effective` と各動画の最新 `vl_metrics` を GROUP BY する1クエリ。代表画像はサムネありを優先し、その中で最大再生を選ぶ。合計に必要な値が欠損した場合は「算出不可」。カタログの平均保存率は有効な各動画の保存率の単純平均、事例詳細の保存率は合計保存÷合計再生。上位動画は案件・アカウント別順位で候補を確保してから再生順にばらす。異なる隣接動画を確保できない場合は12本未満になる。

構成の読み出し契約は `vl_video_analysis.kind='structure'`、`version='v1'`、最新 `analyzed_at`。payload は次の形を `library_analysis.AnalysisPayload` で検証する。

```json
{
  "segments": [
    {"start": 0.0, "end": 1.8, "role": "hook", "telop": "知ってた？", "speech": "…", "visual": "商品を見せる"}
  ],
  "tags": {"hook_type": ["question"], "presentation": ["demo"], "topic": ["problem"], "format": ["interview"], "pain": ["準備に時間がかかる"], "cta": ["save"]},
  "standards": {"cuts_first_3s": true, "loop": true, "telop_safe": true}
}
```

区間は昇順・重複なし・有限の秒数で、終了が開始より後。表示名・説明は `TAG_DESCRIPTIONS` に集約。区間の幅は秒数に比例し、クリックまたはフォーカスでテロップ・発話・映像を開く。分析タグは分類別に表示し、対応する動画一覧へリンクする。分析なし・不正payloadでは明確に「見本」「近日公開」を表示し、見本のタグリンクは無効。見本は表示専用で本番DBには保存しない。区分・業種・目的のタグも動画一覧へリンクする。

変更ファイルは `library.py`、新規 `library_assets.py`／`library_analysis.py`、`test_library.py`／`test_library_js.py`、新規 `test_library_catalog.py`／`test_library_catalog_postgres.py` と本書。app.py、migration、認証方式は変更していない。

検証はメモリ消費を抑えて library 関連4ファイルに限定し、118 passed / 3 skipped。主要な追加テスト:

- `test_catalog_case_links_copy_and_all_videos`
- `test_catalog_navigation_and_security`
- `test_catalog_industry_chips_and_card_tag_labels`
- `test_diversify_never_repeats_campaign_or_account_and_does_not_mutate`
- `test_analysis_contract_grouped_tags_timeline_and_escaping`
- `test_analysis_invalid_or_absent_shows_disabled_example`
- `test_tag_search_is_parameterized_and_latest_version_only`
- `test_detail_has_clickable_player_and_only_supported_analysis`
- `test_campaign_group_by_latest_totals_average_and_representative`（実DB・未実施）
- `test_campaign_filters_privacy_and_sort`（実DB・未実施）
- `test_analysis_tag_filter_executes_on_latest_supported_payload`（実DB・未実施）

mypy strict、CI固定版ruff 0.15.15のcheck／format、import-linterの2契約も検証。ローカルvenvに不足する依存を補うため、mypyは既存の依存導入済みPythonを `--python-executable /tmp/ci-fast-repro-venv/bin/python` で参照した。ログは `/tmp/library-v2-final-pytest.log`、`/tmp/library-v2-mypy-final.log`、`/tmp/library-v2-ruff-final.log`、`/tmp/library-v2-format-final.log`、`/tmp/library-v2-import-linter.log`。

次の9箇所を一時的に壊し、対象テストが失敗することを確認して復元した: 案件／アカウントのばらし、コピー文の再生数、サムネ準備中の表示、HTMLエスケープ、iframeの先行破棄、選択済みURLだけのコピー、見本タグの無効化、タイムラインの秒数比、非公開除外。結果は `/tmp/library-v2-mutations.json`、失敗全文は `/tmp/library-v2-mutation-*.log`。

スクリーンショットは fake DB で実ルートのレスポンスを生成し、ローカルHTMLをChromeで表示。画像は撮影専用の見本。`/tmp/library-v2-screenshots/` に `catalog`／`case`／`videos`／`detail`／`analysis`／`admin`／`accounts` のそれぞれ `-1440.png` と `-375.png` を保存した（CSS幅、DPR 2）。画面を目視し、広告指標の詰まり、CTAタグの配色、縦型サムネの比率、アカウント登録の入力欄配置を修正した。分析見本は区間をクリックして内容が開くことも確認。撮影用ブラウザウインドウは終了後に閉じた。

未実施: 実DB3テストは `TEAMAGENT_TEST_DB_DSN` 未設定でスキップ。使い捨てPostgreSQLの起動もサンドボックスの共有メモリ制限で失敗（`/tmp/library-v2-initdb.log`）。実TikTok再生・本番S3との疎通、本番データ・デプロイは未検証。768／1024pxはCSS分岐の確認のみで実画面撮影は未実施。push・PRは実施していない。

## 55事例・大量投稿のレビュー修正（2026-10-30）

既定の `/library` は事例12件と「すべての事例を見る（件数）」を表示する。`/library?all=1`、検索・絞り込み時は該当事例を全件表示。並び替えだけの場合は12件を維持する。事例サムネは4:5、動画サムネは9:16。375pxでも事例は2列に収める。

サムネ無しは控えめな頭文字とアカウント名を表示し、動画詳細では上部に配置して中央の再生ボタンと重ねない。アカウント名は1行省略と全文のtitle属性、投稿日欠損は非表示、空の投稿文は見出しごと省略する。768px未満では検索と区分だけを常時表示し、その他の条件を「詳しく絞り込む」にまとめる。デスクトップでは条件を展開する。

変更ファイル: `src/teamagent/connect_web/library.py`、`src/teamagent/connect_web/library_assets.py`、`tests/connect_web/test_library_catalog_review.py`、`tests/connect_web/test_library_js.py`、本書。ページURLは前節のまま、全件表示の `/library?all=1` のみ追加。

`LargeCatalogPg` は55事例・2,800動画、サムネ無し2,040本（約73%）、長いアカウント名、日付・投稿文欠損を再現する。関係するpytest 5ファイルは **126 passed / 3 skipped**。mypy strictは524ファイル、CI版ruff 0.15.15 check／formatは成功、import-linterは2契約とも成功。テストからログ設定は変更していない。

| 修正 | 回帰テスト | 一時的に壊して失敗を確認した箇所 |
| --- | --- | --- |
| 既定12件・全件表示 | `test_catalog_limits_default_but_all_and_filters_show_every_match` | 12件の制限を除去 |
| 事例の4:5比率 | `test_case_cover_is_compact_but_video_stays_portrait` | 事例を9:16へ戻す |
| 控えめな仮表示・再生ボタンとの分離 | `test_placeholder_is_quiet_and_clear_of_detail_play_button` | 頭文字を64pxに戻す／詳細の仮表示を中央へ戻す（2件） |
| アカウント名の省略・title | `test_account_names_are_single_line_with_escaped_full_title` | 折り返しを有効化 |
| 空の日付・投稿文を非表示 | `test_absent_date_and_caption_do_not_render_empty_metadata` | 日付／投稿文の条件を常時表示へ変更（2件） |
| モバイルの条件開閉 | `test_mobile_filters_keep_search_category_outside_disclosure` / `test_single_player_disposal_and_checked_urls_clipboard` | 幅に関係なく条件を開く |

8件すべて失敗検出後に復元。結果は `/tmp/library-review-mutations.json`、失敗全文は `/tmp/library-review-mutation-*.log`。最終検証ログは `/tmp/library-review-pytest-final.log`、`/tmp/library-review-mypy.log`、`/tmp/library-review-ruff-check-final.log`、`/tmp/library-review-ruff-format.log`、`/tmp/library-review-imports.log`。

今回のスクリーンショットは未取得。実ルートをfake DBで生成した `/tmp/library-review-preview/{catalog,all,case,videos,detail}.html` で撮影を準備したが、Chromeの共有操作が別作業の `/tmp/nsp-visual/after-400.html` と競合し、1440／375pxの撮影は完了していない。Safariは操作許可なし、独立ブラウザ接続も利用不可。撮影用Chromeはいったん終了し、その後に開いたゲストウインドウは別作業に切り替わったため終了操作を控えた。

未実施: 上記の画面撮影、実DB3テスト（DSN未設定）、実TikTok再生・本番S3との疎通、本番データ確認・デプロイ。コミットも未完了: `git add` が `/Users/s-komata/dev/teamagent/.git/worktrees/teamagent-vl-ui/index.lock` の書き込みを `Operation not permitted` で拒否された。変更は作業ツリーに保持。push・PRは実施していない。
