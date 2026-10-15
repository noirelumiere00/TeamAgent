# 初訪取得保存（移植・単位②）

`FIRST_VISIT_CAPTURE=1`（`true` / `yes` も可）で、お土産の取得情報を型への縮約・関連性除外より前に保存する。未設定・空・`0`・`false` はOFF。OFFでは検索引数、media要求JSON、既存の計測・監査JSON、結果スキーマ、S3書き込みを変更しない。

取得は既存のお土産のまま実行し、初訪資料を自動生成しない。保存例外はログに記録してお土産を続行する。labels・anatomy・transcripts・synthesis等を補完したことにはしない。

## 単位②と④の切り分け

単位②はPython側の保存だけで出す。`tools/tiktok_scraper/search.mjs` は `origin/dev` とバイト一致に戻し、JSの変更は [first_visit_capture_scraper.patch](first_visit_capture_scraper.patch) にgit diff形式で退避した。**単位④で、専用コンテナのリリース契約と一緒に入れる。** ②では `Dockerfile.teamagent-media-worker` の `TIKTOK_SCRAPER_SHA256`、`teamagent_core_media_release_contract.json`、`buildspec_generation_inputs.json` を変更せず、世代の公開は行わない。

JSの `firstVisitCapture` がない既存出力では、Pythonが受け取れる正規化済み投稿、順位、query、shortfalls、ジョブ・成果物参照を保存する。元の正規化済み値は `raw_response` に残す。欠測かどうか判別できない数値は `unknownFields`（例: `stats.playCount`）へ記録し、`missingness_preserved: false` とする。正規化済みの0を実測0とも欠測とも断定しない。①へ渡すrawでは該当数値をnullとし、`unknownFields` を残して `missingFields` には含めない。

④の後に `firstVisitCapture` がある出力を受け取った場合は、そちらの元投稿と `missingFields` を優先し、実測0と欠測を区別する。②でも両方の入力形式を検証する。

既存media-workerイメージのCOPY・dockerignore許可一覧には `teamagent.media.tiktok_capture` が含まれないため、②はworker側の保存用sidecarに依存しない。sidecarがない・空・不正な場合も、core側で既存の `posts.json` とジョブ参照から保存する。workerへの当該モジュール収録は④でコンテナ契約と一緒に行う。

## 保存境界と状態

- ②は既存の `search.mjs` の出力をPythonで保存する。退避した④用パッチは、ONでのみ `--first-visit-capture` を受け、`normalizeVideo()` の前に元投稿とページごとの応答投稿を記録する。これを適用するまでは、キー不在・null・実測0、広告フラグや写真種別等の正規化前の区別を復元できるとは扱わない。
- 同期アダプタはPythonの数値補完と空結果例外より前にcallbackへ渡す。media経路は保存用成果物 `first-visit-capture.json` から、子ジョブID、結果メタデータ、成果物の不変S3参照を取り出す。
- `CaptureSession` はcallback内で、PostRecord化より前にS3へ保存する。軸IDは `axis_01` 等。再試行は `attempts[]` に追加し、失敗した試行を消さない。同じquery・同じ投稿IDでも別軸の取得を混ぜない。
- `TIKTOK_TRULY_EMPTY` / 対応するmedia shortfallの証跡だけを `observed_zero` とする。取得失敗は `failed`、保存情報がない旧応答は未確定として扱う。日時不明を実行日で補わない。
- 取得元の終了理由を `source_stop_reason` に残し、契約用 `stop_reason` は `exhausted` / `no_new` / `capped` / `unknown` へ対応付ける。複数セッションの頻度順は検索表示順と扱わず、①のゲートで停止する。
- 関連性判定は別の `relevance.json` に軸ID・除外ID・理由・判定実施有無を保存する。未除外の取得記録を書き換えない。

## S3と単位⑤への受け渡し

バケットは既存 `OMIYAGE_DECK_PLAN_BUCKET`、接頭辞は `OMIYAGE_DECK_PLAN_PREFIX`（既定 `omiyage/deck-plans/`）。未設定なら保存失敗を記録し、お土産は続行する。公開ACL・署名付きURL・共有リンクは作らない。

```text
<prefix>/<UTC日付>/<request_id>/first-visit/<capture_id>/
  capture-<SHA256>.json
  0001/capture-manifest.json
  0002/capture-manifest.json
  ...
  relevance-01/relevance.json
  relevance-02/relevance.json
  ...
```

各保存は `IfNoneMatch="*"` の新規作成。captureは内容ハッシュ付きのキーで、manifestも試行ごとに別revisionとなる。manifestの `capture` がその時点の全軸・全試行を含む保存物を指す。後続revisionには先行試行も含む。関連性の保存物も別revisionとして残す。

`first_visit_capture_saved` ログの `capture_ref` と `capture_id`、`revision`、`omiyage_job_id` を⑤の入力に使える。ジョブ結果やSlack出力には追加しない。保存本体とmanifestには `request_id` / `omiyage_job_id` / `owner_user_id` を保持する。⑤でジョブの所有者と照合してから読み込むこと。ここでは非同期の受付・配達・権限ルートを新設しない。

参照は `{bucket, key, version_id, sha256, size, content_type}`。保存バケットが未version化ならcapture参照の `version_id` はnullで、内容ハッシュ・サイズを読み取り時に検証する。media成果物の参照はworkerの実際のVersionId・SHA-256・サイズ・Content-Typeを保つ。架空のVersionIdを作らない。

media成果物は既存の保持期限に従う。⑤が動画・カバー・フレーム等を使う場合は期限内に案件側へ固定する必要がある。Pythonが受け取った応答はcaptureへ複製するが、②では正規化前の検索元応答があるとは限らない。②は動画解析の追加取得・カバー取得・音声処理を発火しない。

## 保存から案件を作るAPI

```python
from teamagent.skills.first_visit.capture import materialize_capture

result = materialize_capture(
    capture_ref,
    new_case_root,
    reader=read_exact_s3_object,  # ref -> bytes。テストでは辞書の偽物で差し替える
    kit_path=Path("/path/to/tiktok-skills-kit"),
    case_details=confirmed_case_details,
    selection=selected_posts,
)
```

readerは指定のbucket/key/VersionIdを読む。読み込んだbytesのSHA-256・サイズを確認し、①の `convert_inputs()` / `write_inputs()` で新しい案件に `case.json`・`raw/axis_NN.json`・`selection.json`・`capture-manifest.json` を作る。既存案件は上書きしない。

rawには最後の取得試行を使い、軸ID・試行番号・実行query・取得元参照とmediaジョブ・成果物参照を記録する。全試行と元応答は保存元captureから追跡できる。写真のURLを動画URLへ作り直さず、ID/URLの一致を①で検証する。

戻り値は `capture_manifest`、`missing_inputs`、`manifest`。①の `missing_inputs()` が不足を返す間は `manifest=None`、封印済み `manifest.json` は作らない。不足が解消した場合だけ①の `seal_inputs()` を使う。未完の取得manifestと、生成許可を表す封印manifestは別ファイルである。

## 検証

検証結果・テスト名・変異は作業完了報告に記載する。再実行はネットワークなしで行う。

```bash
FIRST_VISIT_KIT_PATH=/Users/s-komata/dev/tiktok-skills-kit \
.venv/bin/python -m pytest -q -p no:warnings \
  tests/skills tests/media tests/adapters tests/infra tests/mcp_gateway
.venv/bin/ruff check src/ tests/ scripts/ infra/terraform/lambda/tiktok_dispatch/handler.py
MYPYPATH=src .venv/bin/mypy src/ infra/terraform/lambda/tiktok_dispatch/handler.py
```

`tests/infra/test_dockerfile_teamagent_media_worker.py` の切り分け後の結果は15 passed、1 failed（0.99秒）。content-addressed検査は合格し、失敗は手元の `ipaddr.js` 未導入による `test_dns_pinned_proxy_rebinding_answers_fail_closed_without_second_lookup` のみ。テスト・DNS proxy・package/lockのbytesが `origin/dev` と一致することを確認し、`git show origin/dev:...` で `/tmp` に抽出したコードと同じ手元node_modulesでも同じ `ERR_MODULE_NOT_FOUND: Cannot find package 'ipaddr.js'` を再現した。依存のインストールやネットワーク通信は行っていない。完全出力は `/tmp/fv-unit2-split-dockerfile.log` と `/tmp/fv-unit2-split-origin-dev-ipaddr.log`。

### 切り分け後の検証（2026-10-15）

- 上記の指定5ディレクトリを `FIRST_VISIT_KIT_PATH=/Users/s-komata/dev/tiktok-skills-kit` 付きで実行: **8516 passed / 30 skipped / 1 failed**（150.75秒）。唯一の失敗は上記の手元依存不足。完全出力: `/tmp/fv-unit2-split-full-suite.log`。
- Dockerfile検査は当該1件を除いて **15 passed / 1 deselected**。`test_media_sources_and_js_lock_are_content_addressed` も合格。
- JS元データ有無、worker保存、sidecarなし・空・不正時のcore保存を検証。変更したPythonのruffとmypy（5ソース）も合格。
- `origin/dev` と単位②前（`ea693c42^`）の実装を別モジュールとして読み、未設定・空・`0`・`false` の4状態で比較した。結果JSON（1700 bytes）、既存audit（1337 bytes）・deck plan（6246 bytes）のキーとbytes、3軸の検索引数が現行と完全一致。保存用の追加書き込み・ネットワーク呼び出しは0件。完全記録: `/tmp/fv-unit2-split-off-baseline.log`。
- 変異6件すべて検出: unknownを欠測へ変更、callback未発火時の結果破棄、OFF無視、unknown注記削除、元データ優先無効化、media fallback投稿破棄。各失敗の完全出力を `/tmp/teamagent-first-visit-mutations/` と `/private/tmp/first-visit-capture-mutations-vyrzw4__/` に保存し、変異後のソース復元も確認。
- `search.mjs` のbytesと `origin/dev` が一致し、対象diffは空。退避パッチの適用後は `ea693c42` のJSとbytes一致する往復検証も合格。commit・push・依存インストール・ネットワークアクセスは行っていない。

## 切り分け前の作業報告（2026-10-15、ea693c42）

以下はJS変更を含んでいた手元コミットの履歴。`search.mjs` の変更・検証結果・git statusは当時の記録であり、今回の切り分け後の状態を示すものではない。

### 変更箇所

| file:line | 変更 |
|---|---|
| `tools/tiktok_scraper/search.mjs:226` | normalize前の投稿・原応答、順位・欠測・広告・写真・実検索種別の保存 |
| `src/teamagent/media/tiktok_capture.py:9` | media最小依存環境でも使う記録変換。取得日時不明と実行日時を分離 |
| `src/teamagent/adapters/tiktok_scraper.py:203` | parse・空結果例外前のcallback、保存エラーの隔離 |
| `src/teamagent/adapters/media_job.py:911` | media結果・不変S3参照の保持、任意保存ファイルのdownload失敗を隔離 |
| `src/teamagent/adapters/media_job.py:1019` | ON専用fingerprint、実media metadata / shortfalls / job IDをcallbackに付加 |
| `src/teamagent/media/contracts.py:187` | 既定falseの要求フラグ。false時は既存JSONから追加フィールドを省略 |
| `src/teamagent/media/operations.py:443` | 元応答をエラー分類前に取得 |
| `src/teamagent/media/operations.py:722` | ONのみ保存用成果物を作り、保存の失敗で既存投稿を失わない |
| `src/teamagent/media/worker.py:1210` | 保存用成果物だけupload失敗を隔離 |
| `src/teamagent/media/tool_worker.py:626` | roleless workerでも同じ失敗隔離 |
| `src/teamagent/media/tool_contracts.py:424` | ON時のみ固定キー・最大128MiBの出力capability |
| `infra/terraform/lambda/tiktok_dispatch/handler.py:223` | 厳密boolの任意フラグ受理と保存用出力の検証 |
| `src/teamagent/skills/omiyage_report/skill.py:1072` | PostRecord化前の保存。再試行と全軸失敗も記録 |
| `src/teamagent/skills/omiyage_report/skill.py:1264` | OFF既定の接続、親omyジョブとの対応、関連性除外を別記録 |
| `src/teamagent/skills/first_visit/recording.py:17` | フラグ、既存非公開S3バケットへの新規作成、軸別attempts・revision manifest |
| `src/teamagent/skills/first_visit/contract.py:210` | 軸IDごとのraw/shortfallを受け、同じqueryの別取得を混ぜない |
| `src/teamagent/skills/first_visit/capture.py:22` | 偽readerで差し替え可能な保存読込・変換・不足返却・条件付き封印 |
| `docs/first_visit_contract.md:41` | ②の軸ID別入力と実装ドキュメントへの参照 |

### 主なテスト名

| テストファイル | テスト名と検証内容 |
|---|---|
| `tests/skills/omiyage_report/test_first_visit_capture.py` | `test_capture_before_postrecord_and_relevance_retains_unfiltered_evidence`：縮約前に永続化、順位7/9、欠測/0、広告、複数軸の同一投稿、関連性で消えた投稿の保持 |
| 同上 | `test_flag_off_preserves_output_and_existing_s3_bytes`：未設定と各OFF表記の出力JSON・S3 bytes・引数の一致 |
| 同上 | `test_capture_storage_failure_does_not_fail_omiyage`：AccessDeniedでも結果と既存保存bytesが同一 |
| 同上 | `test_all_failed_still_saves_each_axis_without_claiming_zero` / `test_retry_preserves_failed_attempt_and_parent_job_id`：全失敗・再試行・親ジョブID |
| `tests/skills/first_visit/test_capture.py` | `test_saved_rows_keep_original_order_rank_missingness_ads_and_each_axis_occurrence`：順位・ID/URL・欠測・広告・軸別重複 |
| 同上 | `test_identical_queries_use_distinct_axis_snapshots_and_actual_executed_query` / `test_same_query_shortfall_does_not_poison_another_axis`：同queryの別取得・不足を分離 |
| 同上 | `test_last_attempt_is_selected_without_erasing_failed_attempt_or_artifact_pointer`：最後の成功を使い、失敗とmedia参照も残す |
| 同上 | `test_observed_empty_failed_and_not_attempted_remain_distinct` / `test_invalid_saved_capture_fails_before_creating_output`：実測0・失敗・未実施と参照改変 |
| 同上 | `test_uses_real_missing_inputs_to_report_human_evidence_and_never_seals`：labels・anatomy・transcripts・synthesis等で停止 |
| `tests/skills/first_visit/test_capture_pipeline.py` | `test_adapter_saved_capture_has_no_acquisition_gate_shortfalls`：実adapter→保存→変換→実ゲート。終了理由3種 |
| `tests/adapters/test_tiktok_first_visit_capture.py` | `test_capture_runs_before_lossy_parse_and_preserves_source` / `test_capture_distinguishes_observed_zero_from_failure` / `test_capture_records_executed_fallback_type_before_legacy_empty_error` |
| `tests/media/test_first_visit_capture.py` | `test_raw_javascript_capture_keeps_missing_zero_ad_and_photo`：Nodeで実ソース関数を実行 |
| 同上 | `test_capture_sidecar_upload_failure_keeps_successful_job` / `test_capture_sidecar_download_failure_keeps_legacy_artifact` / `test_worker_capture_write_failure_keeps_legacy_posts`：各S3/ファイル失敗を隔離 |
| 同上 | `test_media_client_capture_keeps_job_and_immutable_object_refs` / `test_worker_capture_works_without_core_adapter_dependencies`：VersionId/SHA/size/mime・media実行時依存 |
| `tests/infra/test_media_dispatcher.py` | `test_first_visit_capture_on_reaches_dispatcher_launch_with_bounded_output_capability`：実dispatcher launchと署名capability、既定出力不変・不正bool拒否・任意成果物検証 |
| `tests/media/test_tool_worker.py` | `test_first_visit_capture_upload_failure_does_not_fail_roleless_tiktok_job`：roleless経路の障害継続 |

### 赤確認と変異

保存口未接続の7件の完全な失敗出力は `/tmp/fv-unit2-root-red.log` に保存。縮約前callback等9件、同query混入、dispatcher拒否、保存用write/upload/download障害、worker依存不足等も赤で確認してから修正した。実装途中の広範囲テスト出力は `/tmp/fv-unit2-baseline.log` に保存。

| コード変異 | 最終結果 |
|---|---|
| raw投稿を削除 | 検出 |
| OFFフラグを無視して保存 | 検出 |
| 保存例外をお土産へ伝播 | 検出 |
| 同じqueryの別軸を連結 | 検出 |
| 最後の成功より先頭の失敗を採用 | 最初はすり抜け。raw状態・内容のassertを追加し、再実行で検出 |
| SHA-256検証を無効化 | 検出 |
| 元の終了理由を契約へ変換しない | 検出 |
| 欠測を0へ補完 | 検出 |
| 広告フラグを削除 | 検出 |
| 写真URLを動画URLへ変更 | 検出 |

最終10種類すべて検出。構文エラーによる赤ではなく、期待する振る舞いのassert等で失敗。すべて元のソースbytesへ復元し、復元後の対象186件が合格。各失敗の完全出力と再試行すり抜け時の出力は `/tmp/fv-unit2-mutation-*.log`、一覧は `/tmp/fv-unit2-mutations*.json`。

OFFはさらにHEAD版の旧 `skill.py` と現worktreeを同じ偽取得・時刻・生成・配信条件で直接比較した。結果JSON、計測/監査S3 bytes、配信ファイル/文面、検索引数がすべて一致。ログは `/tmp/fv-unit2-off-differential.log`。

### 最終検証結果

| コマンド・対象 | 結果 |
|---|---|
| `FIRST_VISIT_KIT_PATH=/Users/s-komata/dev/tiktok-skills-kit .venv/bin/python -m pytest -q -p no:warnings tests/skills tests/media tests/mcp_gateway` | **6741 passed, 20 skipped / 179.88秒** |
| `.venv/bin/python -m pytest -q -p no:warnings tests/adapters/test_tiktok_scraper.py tests/adapters/test_tiktok_first_visit_capture.py tests/infra/test_media_dispatcher.py` | **74 passed / 2.82秒** |
| 復元後の初訪・お土産保存・adapter/media保存の対象テスト | **186 passed / 26.58秒** |
| `.venv/bin/ruff check src/ tests/ scripts/ infra/terraform/lambda/tiktok_dispatch/handler.py` | **All checks passed** |
| `MYPYPATH=src .venv/bin/mypy src/ infra/terraform/lambda/tiktok_dispatch/handler.py` | **468 source files、エラー0**（未使用設定のnoteのみ） |
| `node --check tools/tiktok_scraper/search.mjs` / `git diff --check` | 合格 |

pytestはAWS metadata無効・HTTP(S)/ALL proxyを `127.0.0.1:9` に固定し、外部通信なしで実行。20 skipの内訳はSlackClient未注入8件、環境でChromium起動不可12件。①の実キットゲートは `FIRST_VISIT_KIT_PATH` を指定して検証し、外部キットのgit statusは空。

全出力: `/tmp/fv-unit2-final-pytest.log`、`/tmp/fv-unit2-final-extra-pytest.log`、`/tmp/fv-unit2-restored-focused.log`、`/tmp/fv-unit2-final-ruff.log`、`/tmp/fv-unit2-final-mypy.log`。

追加の試験的mypyで既存テストhelperの型エラーも確認した（お土産integration helperに3件、dispatcherテストに15件。後者はHEAD版でも同じ15件を再現）。無関係のテスト型注釈は修正していない。最終の実装全体＋dispatcherのmypyは上記のとおり合格。

### 未実施・残りのリスク

- ネットワーク、実TikTok取得、実AWS/S3/IAM、デプロイ、実動画・実描画の品質確認は行っていない。commit/pushなし。キットは読み取りのみ、`.env` / `secrets/` / `*.pem` は読んでいない。
- 既定OFFのまま配備し、有効化前にdispatcher・worker・coreの対応版を揃える必要がある。旧dispatcherは新しいフラグを拒否する。
- S3書き込み権限/バケット未設定等ではお土産を継続するが、初訪用の保存は残らない。⑤は保存成功の参照がある場合だけ受け渡す。
- media成果物は保持期限までに固定する。所有者とジョブの照合、非同期再開・配達は単位⑤で実装する。
- 指定の設計書 `codex-kit-port/step1_final.md` は作業場所と周辺の候補パスで見つからず、①の契約・docsと今回の明示要件に沿って実装した。

### git status --short

ブランチ `feat/first-visit-save`。commit・pushは行っていない。

```text
 M docs/first_visit_contract.md
 M infra/terraform/lambda/tiktok_dispatch/handler.py
 M src/teamagent/adapters/media_job.py
 M src/teamagent/adapters/tiktok_scraper.py
 M src/teamagent/media/contracts.py
 M src/teamagent/media/operations.py
 M src/teamagent/media/tool_contracts.py
 M src/teamagent/media/tool_worker.py
 M src/teamagent/media/worker.py
 M src/teamagent/skills/first_visit/contract.py
 M src/teamagent/skills/omiyage_report/skill.py
 M tests/infra/test_media_dispatcher.py
 M tests/media/test_tool_worker.py
 M tools/tiktok_scraper/search.mjs
?? docs/first_visit_capture.md
?? src/teamagent/media/tiktok_capture.py
?? src/teamagent/skills/first_visit/capture.py
?? src/teamagent/skills/first_visit/recording.py
?? tests/adapters/test_tiktok_first_visit_capture.py
?? tests/media/test_first_visit_capture.py
?? tests/skills/first_visit/test_capture.py
?? tests/skills/first_visit/test_capture_pipeline.py
?? tests/skills/omiyage_report/test_first_visit_capture.py
```
