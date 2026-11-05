# 動画ライブラリ P3（2026-10-30）

## URL と動作

ホストは `https://connect.newstv.co.jp`。公開・デプロイはこの作業では行っていない。

| URL | 内容 |
| --- | --- |
| `/library` | PICK UP／NEW／TRENDING／CATEGORY／ARCHIVEのトップ |
| `/library/cases` | 事例一覧、検索・絞り込み・並べ替え、12件ずつのページ送り |
| `/library?all=1` | `/library/cases`へ303リダイレクト（他の条件は保持） |
| `/library/c?advertiser=…&campaign=…` | 事例ヒーロー、正確な合計数値、事例まとめ／URLコピー、全動画 |
| `/library/videos` | 50件ずつの動画一覧、検索・絞り込み・並べ替え、カーソル再生、選択URLコピー |
| `/library/v/{platform}/{video_id}` | 詳細、オーガニック／総計／広告の分離、構成の受け皿 |
| `/library/admin/campaigns` | 管理者向け案件設定、PRあり・案件なしの件数 |
| `/library/admin/accounts` | 管理者向けTikTokアカウント登録・一覧（取得はP3b） |
| `/library/api/videos` | 一覧と同じ検索、最新スナップショット、P1の率計算のJSON |
| `/library/thumb/{platform}/{video_id}` | 認証付きS3画像配信、privateキャッシュとETag |

動画の有効値は `vl_videos_effective` から読み、設定変更が次のリクエストで効く。一般利用者は非公開動画を一覧・詳細・サムネで閲覧できず、管理者は確認できる。再生・保存率・ENG率の並べ替えとAPIの `rates` は**総計**を使う。カードの再生数は万／億単位で小数1桁まで切り捨て、正確な値とオーガニック／総計／広告の内訳は詳細に表示する。最新の指標が欠損なら算出不可のまま。5,000再生未満の率は参考値とする。

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

## アーカイブ型UI 第3版（2026-11-02）

この節と冒頭のURL表が現行仕様。前節までの検証記録は各実装時点の履歴。

トップをメディア型の棚に変更した。PICK UPは合計再生順からサムネ有りを優先した5事例の横スクロール、NEWは最終投稿日の新しい8事例、TRENDINGは既存の案件／アカウントばらしを使う12動画。CATEGORYはメディア→TTO→切り抜き→オーガニック→その他→種別未設定の順に最大4事例、0件は非表示。ARCHIVEから全55事例をページ送りで閲覧できる。集計結果を再利用し、トップは集計1回＋動画候補1回の計2クエリ。事例一覧は集計1回の結果を12件ずつ表示する。

ヘッダーはロゴ／検索／種別ナビ、フッターはロゴ／種別ナビ／更新時刻／社内限定注記。トップの大きいフォームを外し、一覧だけに検索・並び替えのバーとnative detailsの詳しい条件を置いた。カード全体を内部詳細への単一リンクにし、動画選択チェックボックスはリンクの外側へ配置。事例は4:5、動画は9:16、濃い青をアクセントに白地・控えめな角丸・枠無しカードを使う。動画詳細の構成／分類タグ／見本は維持し、同じ事例の他動画を最大6本、非公開判定付きの1クエリで追加した。事例コピーはヒーロー内の主ボタン。

変更ファイル:

- `src/teamagent/connect_web/library.py`: HTML、導線、丸め表記、事例一覧ルート、関連動画
- `src/teamagent/connect_web/library_assets.py`: 共通トーン、棚・ヒーロー・レスポンシブ、ヘッダー検索の開閉
- `tests/connect_web/test_library_archive.py`: 第3版の新規回帰テスト
- `tests/connect_web/test_library_catalog_review.py`: 55事例／2,800動画fixtureと既存レビュー項目
- `tests/connect_web/test_library.py`、`test_library_catalog.py`、`test_library_js.py`: 既存機能・セキュリティ・JSの期待値更新
- `docs/v3.2/ops/video_library.md`: 本記録

app.py、library_analysis.py、migration、JSON APIの形式、認証／管理者判定／CSRF／CSP／サムネキャッシュは変更していない。追加依存なし。テストからログ設定を変更していない。

### テストと壊して確かめた箇所

関連pytest 6ファイルは **145 passed / 3 skipped**。実DB3件は `TEAMAGENT_TEST_DB_DSN` 未設定でスキップ。`mypy --strict src` は525ファイル成功。`uvx ruff@0.15.15 check src/ tests/ scripts/` と同版 `format --check` は成功（1,464ファイル）。ネットワーク制限のためuvxは既存キャッシュのコピーを指定し `--offline` で実行。import-linterは2契約成功。

| 対象 | テスト名 | 一時的に壊して失敗を確認した内容 |
| --- | --- | --- |
| 万／億の丸め・境界 | `test_compact_plays_boundaries` | 単位の境界を変更 |
| PICK UPのサムネ優先 | `test_home_shelves_thumbnail_priority_recency_and_category_order` | 優先処理を除去 |
| 新着順 | 同上 | 新着の順序を逆転 |
| 0件の種別を非表示 | 同上 | 空の種別も表示 |
| 未設定は最後 | 同上 | 未設定を先頭へ移動 |
| 旧URLと条件保持 | `test_legacy_catalog_redirect_preserves_filters` | リダイレクトのステータスを200へ変更 |
| カード全体リンク・モバイル導線 | `test_whole_card_links_selection_and_mobile_navigation` | カードのhrefを除去 |
| 種別ナビの現在地 | `test_category_navigation_current_and_header_search` | 現在地の一致判定を無効化 |
| 関連動画の上限 | `test_related_video_query_is_scoped_private_safe_and_bounded` | LIMIT 6を60へ変更 |
| 横スクロール棚の幅 | `test_whole_card_links_selection_and_mobile_navigation` | 横棚に通常グリッドの6列を再設定 |

10件とも赤を確認して復元。関連動画上限の初期テストは部分一致のため60を検出できず、SQL末尾の `LIMIT 6` 一致に強めて再検証した。大量データと2クエリ制限は `test_large_home_two_queries_and_limited_shelves`、ページ送りは `test_case_listing_is_paginated_with_large_catalog`、既存のコピー／タグ／iframe破棄／認証／CSP／CSRF／エスケープも関連テストで継続検証。

検証ログ: `/tmp/library-v3-{pytest,mypy,ruff,format,imports}-final.log`。破壊確認一覧: `/tmp/library-v3-mutations.json`、失敗全文: `/tmp/library-v3-mutation-*.log`（関連動画の再検証は `/tmp/library-v3-related-mutation.log`）。初期失敗の出力も一時ファイルに保持。

### 画面確認

実ルートをTestClientとfake DBで描画し、ローカルHTMLをChromeで撮影。`LargeCatalogPg` は55事例・2,800動画、サムネ無し280本（10%）、未設定30事例・オーガニック18・メディア3・TTO2・切り抜き1・その他1。長いアカウント名、投稿日／投稿文欠損も含む。画像は撮影専用のSAMPLEイラスト、分析は明記された見本で、本番DBに書き込んでいない。

| ページURL | CSS 1440px | CSS 375px |
| --- | --- | --- |
| `/library` | `/tmp/library-v3-screenshots/home-1440.png` | `/tmp/library-v3-screenshots/home-375.png` |
| `/library/cases` | `/tmp/library-v3-screenshots/cases-1440.png` | `/tmp/library-v3-screenshots/cases-375.png` |
| `/library/c?advertiser=…&campaign=…` | `/tmp/library-v3-screenshots/case-1440.png` | `/tmp/library-v3-screenshots/case-375.png` |
| `/library/videos` | `/tmp/library-v3-screenshots/videos-1440.png` | `/tmp/library-v3-screenshots/videos-375.png` |
| `/library/v/tiktok/10000` | `/tmp/library-v3-screenshots/detail-1440.png` | `/tmp/library-v3-screenshots/detail-375.png` |

DPR 2。トップ・事例一覧・動画詳細は全ページ、事例詳細と動画一覧は上部ビューポート（1440は高さ1,000 CSS px、375は高さ1,600 CSS px）。検索と詳細条件を開いた画像は `search-open-375.png`／`filters-open-375.png`。目視で横棚の先頭6枚が潰れるCSS競合と新着サムネの伸びを修正し、修正後を再撮影した。タイトル／メタの省略、仮表示と再生ボタンの分離、見本タイムラインと分類タグ、コピー主ボタンも確認。撮影用ウィンドウは閉じた（既存の別ウィンドウは維持）。

未実施: 実DB3テスト、実TikTok再生・本番S3との疎通、本番データ確認・デプロイ。768／1024pxはCSS分岐のみで実画面確認未完了。push・PRは実施していない。

コミット未実施: 最後の `git add` が `/Users/s-komata/dev/teamagent/.git/worktrees/teamagent-vl-ui/index.lock` の作成を `Operation not permitted` で拒否した。依頼の許容条件に従い、8ファイルの変更をこのworktreeに保持している。

## 第3版・実データレビュー後の修正（2026-11-02）

対象URLは `/library`（種別・PICK UP）、`/library/c?advertiser=…&campaign=…`（動画タイトル）。カードの未設定ラベル非表示と複数種別の区切りは `/library/cases`／`/library/videos`／動画詳細の関連カードにも共通で適用する。詳細の区分表示と未設定セクションは残す。

事例集計SQLのCTEで広告主×案件×有効種別の本数をウィンドウ集計し、外側の既存GROUP BYで最多種別を `primary_category` として返す。同数ならメディア→TTO→切り抜き→その他→オーガニック→未設定。可視範囲・検索条件を適用した動画を数える。トップは引き続き2クエリ、事例集計は1クエリ。種別セクションでは主な種別だけを使い、PICK UP／NEWとの重複は許容する。4件未満はNEWのリスト表示を再利用。PICK UPの負の余白を打ち消して左右をコンテナに揃え、フォーカスリングを内側に置いた。

変更ファイルは `library.py`、`library_assets.py`、`test_library.py`、`test_library_catalog.py`、`test_library_catalog_review.py`、`test_library_archive.py`、`test_library_catalog_postgres.py`、本書。認証・CSP・CSRF・コピー・分析・既存JSON APIの動画形式は変更なし。

| 修正 | 回帰テスト（test_library_archive.py） | 壊して赤を確認した内容 |
| --- | --- | --- |
| 未設定ラベル非表示・複数種別の区切り | `test_card_labels_hide_unset_and_separate_known_categories` | UNCLASSIFIEDを復活／区切りを除去（2件） |
| 種別ごとの英語見出し | `test_category_shelves_use_primary_category_once_and_named_headings` | CATEGORYへ戻す |
| 主な種別だけに分類 | 同上、`test_primary_category_is_counted_in_one_parameterized_aggregate` | 全種別に分類／本数の昇降順を逆転／同数時優先順を逆転（3件） |
| 4件未満はリスト型 | `test_sparse_category_uses_new_list_layout` | 閾値4を1へ変更 |
| 事例内は投稿文の1行目・欠損時アカウント名 | `test_case_video_titles_use_caption_first_line_or_account` | 事例内用タイトル処理を呼ばない |
| PICK UP左端揃え | `test_pickup_shelf_aligns_with_container_and_keeps_focus_inside` | 負のmarginを復活 |

計9件の破壊確認がすべて失敗を検出し、復元済み。結果は `/tmp/library-v3-review-mutations.json`、失敗全文は `/tmp/library-v3-review-mutation-*.log`。初回はPythonの同秒・同サイズのbytecodeキャッシュが1件の変更を拾わなかったため、対象の生成キャッシュを消して9件すべて再検証した。

関連pytest6ファイルは **157 passed / 9 skipped**。mypy strictは525ファイル、ruff 0.15.15 check／formatは1,464ファイル、import-linterは2契約成功。ログは `/tmp/library-v3-review-{pytest,mypy,ruff,format,imports}.log`。実SQLの多数決・全6種別の同数時優先順は `test_primary_category_majority_and_tie_priority` を追加したが、実DB接続設定がなく6パターンともスキップ（既存3件も同様）。実行済みのSQL契約テストはクエリ構造・優先順・パラメータ化・1クエリを検証するもので、実DBの返却値の検証とは区別する。

fake 55事例・2,800動画（サムネ無し280本）に種別混在と異なる投稿文を加え、1440／375pxで再撮影・目視した。トップは `/tmp/library-v3-review-screenshots/home-1440.png`、`home-375.png`。事例は `case-1440.png`（上部1,600 CSS px）、`case-375-full.png`（ブラウザの撮影上限により上部8,192 CSS px）。すべてDPR 2。375pxのビューポート撮影はブラウザ側で拡大・切り抜きになるため全体撮影に切り替え、動画タイトルを含む画像を確認した。棚と先頭カードの左端一致は撮影用のレイアウト検査でも確認。撮影用ウィンドウは終了後に閉じた。

未実施: 実DB9テスト、本番データでの再確認、実TikTok／本番S3疎通、768／1024px再撮影、デプロイ。依頼どおりコミット・push・PRはせず、変更を作業ツリーに残す。

## 一覧オートロード（2026-11-02）

ブランチ `feat/video-library-autoload`、開始時のHEADは `origin/dev` と同じ `6376b01b`。対象は `/library/videos`、`/library/cases`、`/library/c?advertiser=…&campaign=…`。動画一覧は既存50件、事例一覧は既存12件の単位を維持し、事例内動画の描画も50件単位にした。SQLとJSON APIの形式は変更していない。事例コピーに全動画が必要なため、事例ページのDB読出しとコピー本文は全件のまま、カードだけをページ分割する。

各一覧はページ番号と条件を保持した `rel="next"`／`rel="prev"` を返す。JavaScriptなしでも通常のページ遷移ができる。JavaScriptがある場合は次リンクを隠し、下部の400px手前で同じHTMLページを同一オリジン・cookie付きでfetchし、DOMParserからカードと次リンクだけを取り出す。次ページのスクリプトやヘッダー、コピー本文は追加しない。直接2ページ目以降を開いた時の前リンクは残す。新しいAPIやpartialモードは追加していない。

二重取得を防ぎ、失敗後は自動再試行を止めて「再試行」と次ページリンクを表示する。「さらに読み込む」はObserver非対応時にも操作可能。表示件数と終端をaria-live politeで通知する。URLや履歴は書き換えず、独自のスクロール位置復元は行わない。ホバーはmouseover／mouseoutの委譲、画像エラーはcaptureで処理し、追加カードにも1枚だけのiframe制御が効く。コピーは操作時に現在のチェック済み要素を読む。

変更ファイル:

- `src/teamagent/connect_web/library.py`: 共通のページ付きグリッド、条件保持リンク、事例内50件表示
- `src/teamagent/connect_web/library_assets.py`: オートロード、件数・再試行表示、ホバー／画像イベントの委譲
- `tests/connect_web/test_library_autoload.py`: HTML・ページ境界・コピー・ページ上限と150本事例fake
- `tests/connect_web/test_library_autoload_browser.py`: opt-inのヘッドレス実行手順（外部ネットワーク不要）
- `tests/connect_web/test_library_js.py`: 配信JSをNodeで実行する回帰テスト
- `tests/connect_web/test_library.py`: mouseleaveから委譲mouseoutへの期待値更新
- `tests/connect_web/test_library_catalog_review.py`: fakeのLIMIT／OFFSET対応
- `docs/v3.2/ops/video_library.md`: 本記録

### 検証

| テスト名 | 確認内容／一時的に壊して赤を確認した内容 |
| --- | --- |
| `test_next_links_preserve_all_filters_and_end` | 条件・種別・並び順保持、最終ページ、JSなしのリンク／rel=nextの除去・条件の脱落 |
| `test_case_grid_pages_fifty_but_copy_includes_every_video` | 150本を50本×3ページ、コピーは全150本／表示上限の除去・コピーを50本に切断 |
| `test_autoload_page_limit_stays_validated` | 3種類の一覧で0・負数・100万超・非数値を拒否 |
| `test_autoload_serial_fetch_retry_completion_and_button_fallback` | 二重取得、再試行、終端、Observerなし、途中ページの前リンク、ログインHTML・異常な次リンク／busy制御の除去・自動再試行抑止の除去・ボタン無効化・cookie省略・完了表示の破壊 |
| `test_single_player_disposal_and_checked_urls_clipboard` | 追加カードの委譲ホバー、iframe1枚・破棄、追加チェックのコピー、追加画像のエラー／委譲イベント名の破壊・未選択URLの混入 |

計11件で失敗検出後に復元した。結果は `/tmp/library-autoload-mutations.json`、失敗全文は `/tmp/library-autoload-mutation-*.log`。関係するpytestは8ファイルに限定。実DB9件はDSN未設定、ブラウザ1件は通常実行ではopt-inのためスキップする。最終結果と静的検査ログは `/tmp/library-autoload-{pytest,mypy,ruff,format,imports}-final.log`。ログ設定はテストから変更していない。

### ブラウザ確認の制限と再実行

ヘッドレス検証は `TEAMAGENT_LIBRARY_BROWSER=1` を指定して実際に実行したが、Chromium起動時の `MachPortRendezvousServer: Permission denied (1100)` で失敗した。画面表示前のOS権限制約であり、スクロール／追加カード操作／1440pxスクリーンショットは今回未確認・未取得。起動したプロセスは終了し、Playwrightの一時プロファイルcleanupまでログで確認した。失敗全文は `/tmp/library-autoload-browser.log`。既存の利用者ブラウザは操作していない。

実行環境の制約がない場所では以下で再現できる（既存dev/media依存のPlaywright・Chromiumを使用）。ブラウザはfinallyで閉じる。

```bash
TEAMAGENT_LIBRARY_BROWSER=1 PYTHONPATH=.:src .venv/bin/pytest -q \
  tests/connect_web/test_library_autoload_browser.py
```

このテストは実ルートのHTML・認証cookie・CSPをTestClientで返し、2,800動画の一覧を50→100→150→200件、55事例を終端まで、150本事例を50→100→150件にロードする。追加されたカードで選択コピーとiframe生成・破棄、JSなしの次リンクも検証する設計。TikTokと画像はfake応答で、本番DB・外部サービスには接続しない。成功時の画像保存先は `/tmp/library-autoload-screenshots/{videos-200,cases-55,case-150}-1440.png`（今回は未生成）。

未実施: 上記のブラウザ実機確認と撮影、実DB9テスト、本番データ・実TikTok／S3疎通・デプロイ。push・PRは行わない。

最終検証結果: 関連pytest **177 passed / 10 skipped**、mypy strict **526ファイル成功**、ruff **0.15.15** check／format（1,468ファイル）成功、import-linter **2契約成功**。opt-inブラウザを明示実行した別runは起動エラーで1 failedであり、ブラウザ確認まで完了したという意味ではない。

コミット未実施: `git add` が `/Users/s-komata/dev/teamagent/.git/worktrees/teamagent-vl-ui/index.lock` 作成を `Operation not permitted` で拒否した。依頼の許容条件どおり、8ファイルを作業ツリーに保持。push・PRは未実施。

## 営業向け検索（2026-11-02）

ヘッダー検索は `/library/videos?q=…` に遷移する。1ページ目だけ、一致した事例を合計再生順で最大4件、その下に一致した動画の9:16グリッドを表示する。事例候補は既存の集計1クエリを追加するだけで、追加ロードでは再集計しない。動画のオートロード・ホバー再生・選択コピー・遅延サムネ読み込みは既存の仕組みを利用する。

`parse_search` を動画一覧・事例一覧・動画API・検索解釈表示で共用する。検索語はNFKC正規化→空白で分割し、既存の連結フィールドに対して各語をANDでILIKE検索する。利用者の値はSQLパラメータだけに渡す。上限は10語、正規化前後とも500字。空白で区切られた `#PR`（全角・大文字小文字も同等）は検索語から除き、`is_pr = true` にする。明示的な `pr=no` より `#PR` を優先し、フォームの選択と解釈表示もPRありに揃える。`PR広告` は通常の語で、その他の先頭ハッシュは取り除く。検索解釈・件数・複数語ANDの説明と、0件時の語を減らす／PR指定を解除する案内を表示する。

今回の変更: `library.py`、`test_library_search.py`（新規）、`test_library.py`・`test_library_archive.py`（既存期待値更新）、本記録。前回のオートロード変更は保持し、コミット・push・PRは行わない。

| テスト名（`test_library_search.py`） | 確認内容／一時的に壊して失敗を確認 |
| --- | --- |
| `test_search_words_execute_as_and_and_pr` | 生成したSQL条件をSQLiteに方言変換して実行し対象IDを検証／NFKC無効化、AND→OR、PR判定無効化、先頭ハッシュ除去無効化 |
| `test_search_limits_and_pr_override` | 10語・500字・正規化で長くなる入力・PR優先／語数上限と文字数上限の緩和 |
| `test_all_search_routes_share_parameters_and_validation` | 動画・事例・APIの共通条件、パラメータ、400、認証／検索パラメータの破壊 |
| `test_combined_results_keep_video_interactions_and_next_query` | 事例最大4件→動画、PR説明、サムネ・ホバー・選択コピーのHTML、条件保持、初回3クエリ・追加2クエリ、CSP／ヘッダー遷移先と事例上限の破壊 |
| `test_search_interpretation_escaping_and_empty_guidance` | AND・PR解釈、エスケープ、0件案内／PR説明とエスケープの破壊 |
| `test_empty_search_page_guidance` | 両一覧の0件案内、候補なしは事例枠なし／PR解除案内の削除 |

計12件の変異で失敗検出後に復元。結果 `/tmp/library-search-mutations.json`、失敗全文 `/tmp/library-search-mutation-*.log`。検索判定テストのSQLiteはWHEREの論理検証用で、実PostgreSQL接続の代替ではない。

前回のオートロード実ブラウザテストは利用者から `test_library_autoload_browser.py: 1 passed` の確認済み連絡あり。今回はブラウザ再実行・スクリーンショット追加は行わず、関連pytestと静的検査で検索変更を検証する。実DBテストは `TEAMAGENT_TEST_DB_DSN` 未設定時にスキップする。今回の最終ログは `/tmp/library-search-{pytest,mypy,ruff,format,imports}-final.log`。

最終検証結果: 関連pytest **196 passed / 10 skipped**（実DB9件・opt-inブラウザ1件）、`mypy --strict src` **526ファイル成功**、`uvx ruff@0.15.15 check src/ tests/ scripts/` 成功、同 `format --check` **1,469ファイル成功**、import-linter **2契約成功**、`git diff --check` 成功。ruffは既存のローカルキャッシュから指定版をオフライン実行。今回の変更に実DB・実ブラウザでの追加確認は含まない。コミットは利用者が行うため未実施。

## URL 取り込み PR1（2026-11-02）

画面を含まない受付・取得・保存・worker。0037 は runner 所有 transaction で適用する追加 migration で、`vl_metrics` と既存シート同期の書き込みは変更しない。`vl_public_metrics` は公開ページの5指標を別の日次スナップショットに保存する。既存ビューは再作成せず列契約を保つ。`vl_videos` に page 観測と経路、`vl_campaigns` に nullable の作成者情報を追加する。シートが URL 動画を own に更新すると、trigger が sheet 経路を追加する。

入口は `teamagent.ingest.video_library_url_import.submit_import(text, repository, requested_by=..., idempotency_key=..., wake=...)`。認証済みの社内依頼者を呼び出し元で渡す。非空100行までを通信前に検査し、依頼者とキーで冪等保存後に wake する。画面を閉じても行結果は DB に残る。P3b 用の `import_one_url(..., source="p3b", requested_by=...)` は同じ取得・保存・制限を使い、列挙や分析は実行しない。

| 設定 | 既定 | 用途 |
| --- | --- | --- |
| `VIDEO_LIBRARY_IMPORT_WORKER_ENABLED` | OFF | `1` / `true` で connect-web lifespan の1スレッドを起動 |
| `VIDEO_LIBRARY_IMPORT_DAILY_LIMIT` | 300 | 固定 UTC+9 の日次枠。0は取得停止、負数・非整数は起動拒否 |
| `VIDEO_LIBRARY_IMPORT_STALE_SECONDS` | 600 | processing 回収までの秒数。回収時は lease を更新し旧 worker の保存を拒否 |
| `VIDEO_LIBRARY_S3_BUCKET` | 既存設定 | 共通 CDN/JPEG 検査後のサムネ保存先 |

単独実行は `python -m teamagent.ingest.video_library_url_worker`。同じ queue を空になるまで処理する。スレッドは空 queue を1秒ごとに再確認するため、翌日の deferred 行もプロセス稼働中は自動的に候補へ戻る。CLI は未来の deferred 行を待たず終了する。

複数 replica / CLI の HTTP は session advisory lock で直列化し、行は `FOR UPDATE SKIP LOCKED` で確保する。ページ・短縮各段・再試行は通信前に全員共通の日次枠を消費し、開始間隔を1秒以上にする。画像保存は同じ投稿観測の一部で追加の投稿枠を使わない。通信失敗/5xxの追加試行は最大2回（2秒・4秒）。429/403/明確なチャレンジ・連続5失敗は DB の `vl_public_fetch_control` に停止状態を残す。次のバッチやプロセス再起動で停止は解除しない。共通停止解除は管理者による明示的な DB 操作に限定し、この PR には解除画面を設けない。従来の独立した `fill_thumbnails()` の選択・停止・枠の挙動は維持する。

新規動画は external と入力経路を保存する。既存動画は page 観測・公開指標・未保存サムネ・経路だけ更新し、caption/is_pr/案件/origin/台帳/既存分析へ書かない。desc 不明の新規動画は classification_pending の行観測に残して公開・分析待機登録を保留する。同日部分取得で有効な指標が欠けた場合は旧スナップショット全体を保持し、異なる回の値を混ぜない。失敗では最終正常値を残して試行状態だけ更新する。シートが後から caption=NULL を上書きする既存動作は MVP の既知の制限として残す。

新規テストは `test_video_library_url_import.py`（A01-A05/A08、HTTP・worker）と `test_video_library_url_import_postgres.py`（A02/A05-A08、RLS・並行claim・回収・日次枠・両順序のシート同期）。実 DB は `TEAMAGENT_TEST_DB_DSN` がある場合のみランダムな一時 schema で実行し、public の既存データには触れない。新規の外部依存はない（CI latest の既存依存一覧で充足）。

PR1 の変更ファイル（12件）:

- `infra/migrations/0037_video_library_url_import.sql`
- `src/teamagent/adapters/video_library_page.py`
- `src/teamagent/ingest/video_library_url_models.py`
- `src/teamagent/ingest/video_library_url_repository.py`
- `src/teamagent/ingest/video_library_url_import.py`
- `src/teamagent/ingest/video_library_url_worker.py`
- `src/teamagent/ingest/video_library_thumbs.py`
- `src/teamagent/connect_web/app.py`
- `tests/ingest/test_video_library_url_import.py`
- `tests/ingest/test_video_library_url_import_postgres.py`
- `tests/ingest/test_video_library_thumbs.py`
- `docs/v3.2/ops/video_library.md`

主要テスト名:

| テスト | 確認すること |
| --- | --- |
| `test_a01_limits_count_before_dedup_and_preserve_line_numbers` | 100/101行、空行、全入力への結果対応 |
| `test_a02_queries_and_handles_deduplicate_by_video_id` | クエリ・handle違いの同一IDを1回に集約 |
| `test_short_redirect_body_never_read` / `test_redirect_refuses_untrusted_destination_before_request` / `test_redirect_maximum_five` | 許可された短縮3形式、本文非読込、各段検査、5段上限 |
| `test_a04_empty_missing_broken_and_mismatched_pages` / `test_a04_string_metrics_statsv2_and_zero` | 1.4KB空ページ・JSON・ID検査、文字列、statsV2、0/NULL |
| `test_a05_pr_rule_does_not_use_is_ad` | #PR規則と広告配信観測の分離 |
| `test_a08_http_blocks_pause_without_retries` / `test_a08_retries_count_every_attempt_and_backoff` / `test_a08_one_failed_row_does_not_discard_later_success` | 制限停止・試行総数・個別失敗後の継続 |
| `test_a06_sheet_always_wins_and_page_public_settings_analysis_survive` | 両順序のシート共存（実DB・未実行） |
| `test_a07_snapshot_unit_partial_failure_and_jst_date` | スナップショット・部分欠損・JST境界（実DB・未実行） |
| `test_a08_claim_skips_locked_row_in_another_session` / `test_a08_two_workers_claim_distinct_rows` | 実ロック下の非重複claim（実DB・未実行） |
| `test_a08_processing_recovery_fences_old_worker` / `test_a08_daily_budget_counts_failed_retry_and_defers` / `test_a08_pause_is_durable_and_leaves_remaining_queue` | 回収・旧lease拒否・全体枠・永続停止（実DB・未実行） |

最終の関連 pytest は8ファイルに限定し **374 passed / 23 skipped**（新設実DB14件、既存実DB9件）。`mypy --strict src` は533ファイル成功、import-linterは2契約成功、bandit `-ll` は中・高の指摘0。Ruff **0.15.15** の `check src/ tests/ scripts/` と `format --check src/ tests/ scripts/` は成功（1,487ファイル）。`uvx ruff@0.15.15` はキャッシュ解決／ネットワーク制約で起動できず、既存の `/private/tmp/teamagent-pgx2-venv/bin/ruff`（同じ0.15.15）で実行した。pytest本数の事前確認は `ps` が OS に拒否され計測不能だった。pytestは並列化せず、1プロセスずつ実行した。ログ設定は変更していない。

一時的な変異8件（101行受付、ID重複排除無効、redirect HTTP許可、ID一致検査無効、0を欠損化、PR部分一致、429停止無効、retry上限増加）はすべて対応テストで赤を確認後に復元。結果は `/private/tmp/vl-url-pr1-mutations.json`、失敗全文は `/private/tmp/vl-url-pr1-mutation-*.log`。最終検査ログは `/private/tmp/vl-url-pr1-final-{pytest,mypy,ruff,format,imports,bandit}.log`。

未検証: DSN未設定による実DB23件、実TikTok/S3疎通、本番migration適用・worker起動。Docker接続は権限で拒否され、利用可能なローカルPostgreSQLも確認できず、実DB確認の完了条件は残っている。画面・IAM・本番env反映はPR2/PR3の範囲。`git add` は共有管理領域 `/Users/s-komata/dev/teamagent/.git/worktrees/teamagent-vl-import/index.lock` 作成を `Operation not permitted` で拒否されたため、コミットせず全変更を作業ツリーに保持。push・PR作成は未実施。

### キャプションからの広告主・案件補完（0038）

0038 はシートとは別の `vl_video_campaign_links` に AI / human の判断を保存する。
`vl_videos_effective` は既存列の名前・型・順番を維持し、末尾に `case_inferred` を追加する。
有効値はシート（別名の読み替え後） > human > applied の AI。広告主が異なる案件は混ぜず、人の却下も AI の再適用を防ぐ。
0038 の `vl_case_aliases` は元の広告主×案件の組を完全一致で読み替える。`to_campaign=NULL` は元の案件名を保つ。
候補一覧は読み替え後の組を重複除去する。評価の正解はシートで案件が埋まった投稿の読み替え後の値だけを使い、
AI・human で補完したラベルは正解にしない。過去の判断行も表示時に読み替える。
設定は読み替え先を優先し、元の設定にフォールバックする。元が非公開なら読み替え先が公開でも非公開を維持する。
別名は社内共有read、adminのみinsert/update。シートと事例マスターの生データは書き換えない。

11/04 決定の初期2件は migration には入れず、0038 適用後に一回だけ登録する。

```bash
python scripts/register_video_case_aliases.py --decided-by '<実行者の識別子>'
```

`TikTok / 20260717 → ラフォーレ / セール`、`トップバリュ / 昭和レトロ → AEON / 昭和レトロ` を登録する。
後者の `to_campaign` は NULL。既存の別名は再実行でも上書きせず、決定者・日時も維持する。
案件が空の PR 投稿と、PR なし・広告主ありの投稿が対象。PR 判定は変更しない。
新規案件は candidate として保存するだけで、事例マスターは増やさない。

日次実行は既定 OFF。`VIDEO_LIBRARY_CASE_INFERENCE_ENABLED=1` の場合だけ同期・サムネ処理後に実行し、
推定失敗は ingest の終了コードを変えない。管理画面の修正 UI は次の PR。

最初に正解ラベルを伏せて評価し、設定ファイルを作る（実行には DB と Bedrock の権限が必要）。

```bash
VIDEO_LIBRARY_CASE_MAX_COST_USD=10 python scripts/evaluate_video_campaigns.py \
  --report /path/to/case-evaluation.json --policy-output /path/to/case-policy.json
VIDEO_LIBRARY_CASE_POLICY=/path/to/case-policy.json \
  python -m teamagent.ingest.video_library_case_inference --dry-run
```

評価は案件・アカウント・月・文の長さで層別に150本を抽出し、重複投稿を除く。
50本で閾値を調整、100本で固定評価。広告主も伏せる投稿／残す投稿を半分ずつにし、
一部は正解案件を候補から除く。全体と案件別の広告主一致率、完全一致率、既知広告主での案件一致率、
適合率、提案率、不明率、不正出力件数を JSON に保存する。
既存一致の自動反映には固定評価で30件以上、適合率97%以上、候補除外時の誤った既存一致が0件必要。
完全一致率と不正出力件数は指標として残すが、ゲートの合否には使わない。不正出力自体は反映しない。
同じ広告主に複数案件がある場合、キャプション・ハッシュタグ全体に選んだ案件名があり、他の案件名が一切無い時だけ案件まで反映する。
案件名の照合は NFKC・空白無視・大小無視で行う。引用に一方だけあっても、本文やハッシュタグに別の案件名があれば広告主だけにする。
それ以外の既存一致は広告主だけに落とす。1案件の広告主は従来どおり。
適合率は、案件まで反映する時は広告主×案件の一致、広告主だけの時は広告主一致で計算する。
既存一致から広告主だけに落とした結果も既存一致ゲートで評価・反映する。
モデルが初めから広告主だけ返した結果は、別に30件以上・97%以上を評価する。
未合格の種類は適用不可。少数の評価結果は将来の精度を保証しない。
設定はモデル・プロンプト本文・候補一覧のハッシュと反映ルールの版 (`application_version=v3`) に結び付ける。
旧ルールの設定は読み込まず、別名登録後に実データで再評価して生成し直す。
本文・顧客名・引用をログに出さず、評価レポートの案件別キーもハッシュにする。
レポートの `incorrect_applications` は正解・元の推定（確信度と引用含む）・反映値・本文の先頭80字を保存する。
閾値設定の `status`、調整用／最終評価の `split` も記録し、ゲート未合格時の誤った反映候補も診断用に含める。
この一覧はローカルのレポートだけに出し、本番ログには出さない。

| 環境変数 | 既定・意味 |
|---|---|
| `VIDEO_LIBRARY_CASE_MODEL` | 既存 Bedrock 価格表の東京 Haiku 4.5。価格が不明なモデルは拒否 |
| `VIDEO_LIBRARY_CASE_POLICY` | 未設定。未設定・未合格では applied にしない。新規候補は candidate、それ以外は unknown |
| `VIDEO_LIBRARY_CASE_LIMIT` | 100件 / 実行（既推定で入力が同じ投稿は数えない） |
| `VIDEO_LIBRARY_CASE_MAX_COST_USD` | 推定 $1 / 実行、評価 $10 / 実行 |

費用は UTF-8 バイト数による入力 token 上限・cache 書込料金・最大出力を呼出し前に予約する。
タイムアウトも予約を消費し、SDK 内部リトライは無効。実費より保守的に停止する。
`--dry-run` は推定呼出しを行うが DB は更新しない。
同一入力・候補・設定・モデル・プロンプトはスキップし、変更された AI 判断は更新前に無効化する。
失敗は次回再試行する。human 行は推定で更新しない。

確認対象: `test_video_library_case_inference.py`, `test_video_library_case_postgres.py`,
`test_run_ingest_fargate.py`, `test_library_catalog.py`。
実 DB テストには隔離・ロールバック用の `TEAMAGENT_TEST_DB_DSN` が必要。

## 自社・外部の区分と他社事例（0041、2026-11-05）

0041 を適用してから新しい connect-web / URL worker / 事例推定を起動する。
既存 migration は変更しない。`vl_videos_effective` の既存列は名前・順序・型を維持し、
`external_advertiser` と `is_external_case` だけ末尾に追加する。`origin` は有効な区分になる。

- `sheet` 経路、または台帳の自社運営アカウント（NewsTV／ベクトル（VCL）／ベクトル九州／イニシャル／gracemode）は必ず自社。ハンドルの大文字小文字・先頭 @ は同一視する。
- それ以外は直近の人の選択。選択がなければ外部。取り込みの既定は「他社・世の中の投稿」で、自社の依頼した KOL も「自社の案件」を選ぶ。自社選択時だけ既存の案件選択を表示する。
- URL 取り込みの自社選択は既存動画にも適用する。既存動画の案件は維持する。既定の外部選択で、既存の自社区分は取り消さない。区分を戻す操作は動画詳細で行う。
- `vl_video_origin_decisions` は社内全員が追記可能な履歴。選択した区分・本人・時刻を残す。UPDATE/DELETE は付与しない。詳細の切り替えは Origin 照合と受付 UUID による冪等化を行う。シート・自社アカウントの優先順位は切り替え後も維持する。
- 外部で #PR または広告配信あり（公開ページの isAd、または配信指標が正）の動画を「他社事例」、それ以外を「一般投稿」と表示する。動画一覧の `external_case=yes` で絞り込める。外部は案件が付いていても、自社の事例一覧・詳細・コピー・事例別集計・事例評価から除く。
- 外部の #PR は既存の定期推定で Haiku にキャプションだけを渡す。自社事例リストは渡さない。既存と同じ予算・温度・キャッシュ・引用検証を使い、広告主名も原文にない場合は不採用。`vl_video_campaign_links` に source=ai、campaign=NULL、prompt_version=external-v1 として保存する。自社と外部の推定結果は区分を跨いで反映しない。
- 外部広告主は詳細で社内全員が修正できる。source=human に本人・時刻を保存し、空欄への修正も AI の再推定を抑止する。Origin 照合・動画の公開範囲・RLS を適用する。
- トップの「最近取り込んだ動画」は全員の URL 取り込みの成功・一部取得分を完了日時順に、動画ごとの直近12本。非公開は既存方針で除外し、0本なら棚を表示しない。「もっと見る」は `/library/videos?url_import=yes`。
- `/library/api/videos` は既存の返却列を保ち、`origin`（有効値）、`is_external_case`、`external_advertiser` で Aico からも区別できる。

検証の入口は `test_library_external.py`、`test_library_js.py`、
`test_video_library_case_inference.py`、`test_video_library_case_aliases.py`、
`test_video_library_external_postgres.py`、`test_0041_preserves_all_existing_view_columns`。
実DB検証は `TEAMAGENT_TEST_DB_DSN` を指定した時だけ、既存の使い捨てスキーマで実行する。

### この変更の確認結果（2026-11-05）

- 関連 pytest: 475 passed / 51 skipped。スキップは実DB用 DSN 未設定によるもの。全件 pytest は実行していない。sandbox が `ps` を拒否したため他セッションの pytest 本数は確認できず、こちらは常に1プロセスで実行した。
- `mypy --strict src/teamagent`: 543 source files、エラーなし。
- `uvx --offline ruff@0.15.15 check src/ tests/ scripts/` および `format --check src/ tests/ scripts/`: 成功（1518 files）。既存のキャッシュを `/tmp` から参照し、追加ダウンロードなし。
- `lint-imports --no-cache`: 2 kept / 0 broken。`bandit -r src/teamagent -ll`: Medium/High なし。
- 実DBでの0041適用・RLS・優先順位・ビュー列互換と、実ブラウザ・本番 Bedrock は未実施。配信JSは Node のDOMハーネス、Bedrockは fake で確認。実DB用テストは追加済み。

主なテスト名:

- `test_intake_default_external_and_own_choice_survives_retry`
- `test_external_intake_ignores_campaign_and_own_reveals_it`
- `test_external_badges_and_filter`
- `test_own_case_queries_exclude_external_even_with_campaign`
- `test_recent_imports_shared_order_limit_and_empty_shelf`
- `test_employee_switch_and_advertiser_edit_require_origin_and_record_actor`
- `test_api_adds_origin_external_case_and_advertiser`
- `test_external_advertiser_uses_caption_without_own_catalog`
- `test_external_advertiser_rejects_invented_name_quote_or_campaign`
- `test_external_advertiser_skips_non_pr_or_missing_caption`
- `test_external_unknown_does_not_apply_an_advertiser`
- `test_inference_routes_external_pr_even_with_campaign_but_skips_non_pr_and_human`
- `test_campaign_creation_preserves_unsent_urls_and_selection`
- 実DB用（未実行）: `test_0041_three_ownership_conditions_and_priority`（5運営ラベル）、`test_0041_external_with_campaign_excluded_from_own_cases_and_totals`、`test_0041_external_pr_ad_and_general_tags`、`test_0041_external_advertiser_ai_human_and_origin_race`、`test_0041_switch_is_idempotent_audited_and_employee_rls`、`test_0041_own_intake_and_recent_shared_12_newest`、`test_0041_preserves_all_existing_view_columns`。

引用検証・外部への自社推定の誤適用・外部の事例集計混入・他社タグ・Originガード・棚12本上限・API追加列・自社選択のJS・受付の区分保存の9箇所をそれぞれ一時的に壊し、対象テストが exit 1 になることを確認して復元した。結果は `/tmp/vl-external-mutations.json`、失敗全文は `/tmp/vl-external-mutation-*.log`。最終 pytest 全文は `/tmp/vl-external-pytest-final.log`。

変更ファイル（すべてこのリポジトリ内）:

- DB: `infra/migrations/0041_video_library_external_cases.sql`
- UI/API: `src/teamagent/connect_web/library.py`、`library_import.py`、`library_assets.py`
- 取り込み・推定: `src/teamagent/ingest/video_library_url_import.py`、`video_library_url_repository.py`、`video_library_case_inference.py`、`video_library_case_repository.py`
- Prompt: `src/teamagent/prompts/external_advertiser/v1/system.md`
- Webテスト: `tests/connect_web/test_library_external.py`、`test_library_import.py`、`test_library_import_postgres.py`、`test_library_import_browser.py`、`test_library_js.py`、`test_library_archive.py`、`test_library_catalog.py`
- Ingestテスト: `tests/ingest/test_video_library_external_postgres.py`、`test_video_library_case_inference.py`、`test_video_library_case_aliases.py`、`test_video_library_case_postgres.py`、`test_video_library_url_import_postgres.py`
- 運用・確認記録: 本書 `docs/v3.2/ops/video_library.md`
