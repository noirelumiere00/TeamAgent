# sales25 入力・制作・検索契約（PR1・PR2・PR3）

`first_visit.sales25_contract` と `first_visit.sales25` は、通常ツールから登録・呼び出しされない独立入口。取得済み JSON と案件内画像だけを使う。旧 `first_visit.contract` の API、manifest、旧キット、お土産の生成・配送経路は変更しない。

## 内部100件検索（PR3）

`MediaJobClient.search_tiktok_sales25(query, *, owner_id, request_fingerprint, search_type="keyword", max_videos=100, timeout_s=780)` は `raw / acquisition_status / artifacts / media_job_id` を返す。本人確認済みの呼出側が渡す `owner_id` を、検証済みのworker rawへ結び付ける。`artifacts` の不変参照・SHAはworkerが保存した元のバイト列に対応し、ownerを結び付けた案件rawはその後のsales25 manifestで封印する。

内部要求の `edition="sales25"` は1語・最大100件・`metadata_only`・動画0・`sort="display"` に限定する。公開 `tiktok_acquire` と従来の内部検索は上限30。edition未指定の要求JSONには新しいeditionキーを出さない。親ジョブのrollout、同時実行制御、通常お土産経路への接続はPR6で扱う。

workerは次の3成果物を必須で保存し、欠落やupload失敗はジョブを失敗にする。サムネイルや動画の取得は行わない。

|成果物|内容|
|---|---|
|`sales25-raw.json`|JSの縮約前出力。nullableな5指標、missingFields、広告・写真・投稿URL、取得日時、単一セッションの表示順、拒否診断を保持|
|`acquisition-status.json`|`accepted / status / target / actual / shortfall / stop_reason / diagnostics / raw_ref`。raw_refは同じジョブのraw成果物名|
|`posts.json`|既存の投稿一覧形式。拒否時は空。分析の根拠には上のrawを使う|

100件未達は `acquisition_status.status="partial"` と実数で返す。正常なrawのstatusは既存の入力契約に合わせて `ok`、正常0件は `observed_zero` + `TIKTOK_TRULY_EMPTY`。CAPTCHA・CDN拒否・異常API応答は、途中の投稿をrawに保持しても `accepted=false / status="failed"` として採用しない。別セッション・hashtagからkeywordへのfallback・失敗後のworker自動再検索で穴埋めしない。取得日時がない失敗には日時を補完しない。

sales25 の検索実行・受付見積もりは720秒、呼出側の既定期限は780秒（`OMIYAGE_SALES25_SEARCH_SECONDS` は780〜900秒）。edition未指定は30件以下120秒、31〜100件240秒のまま。50/100件取得の実時間は未実測。DNS-pinned proxyとredirect制御を維持し、正本キットのengineは収録しない。mediaのJS固定SHAはDockerfile・release契約・連動テストを同時更新する。PR3/4/5統合後の世代再導出まで、変更済みrelease契約によるgeneration inputの不一致を解消しない。

## 穏やかな取得（sales25 のみ）

検索はジョブ内で常に直列（旧 `OMIYAGE_SALES25_SEARCH_CONCURRENCY=2` も直列へ制限）。最初の検索API応答を処理してから初回スクロールまで20秒、以後10〜14秒待ち、1回につき1画面だけ進む。前の応答処理が終わるまで次へ進まない。下端以外の新規0件は終了判定に数えず、下端で5回新規0件・最大40スクロール・開始から600秒・最大18応答のいずれかで終了する。時間上限は処理境界で確認し、応答処理自体の停止はworkerの720秒期限が担う。

ブラウザ取得終了から次のブラウザ検索まで最低30秒。拒否（captcha / cdn_denied / api_error / no_api_response / refused）を一度でも観測したら、同じ親ジョブ実行の残りはブラウザを起動せず既存Apify予備へ送る。切替状態は実行中のメモリだけに置き、他ジョブと共有・永続化しない。予備が無効・失敗なら未完了として保持する。直接予備へ送った語は `browser_skipped` を記録し、worker子ジョブとしては扱わない。

`diagnostics.scrollCount` と `pageTimings`（検索処理開始からの `elapsedSeconds`・追加件数・成功可否）、`diagnostics.stop_reason` を保存する。上限終了時のrawの `stop_reason` は既存契約の `capped`、具体的な上限は診断に残す。Cookie・トークンは診断へ追加しない。

親期限の既定は10,800→14,400秒。受付・継続通知の目安は35〜45分→45〜60分（ブラウザが断られた後は Apify に切り替わるため AWS では従来並み）、混雑・長尺時は90分以上（暫定）。6語×780秒＋語間150秒でも約81分で、後続分析・QAの余裕を確保する。既存の親期限上書きは引き続き有効。

### Apify の途中終了（2026-10-29）

Actor が `SUCCEEDED` でも目標未達なら `accepted=true / status=partial`、`actual=取得行数 / shortfall=target-actual` を保持する。raw の `requested=target / actual=取得行数` は整数で整合している必要があり、正の実数が目標以下なら入力契約で受け入れる。URL、表示順位、欠測、owner、全IDの関連判定などの検証は維持する。Actor 完了を表す既存の `stop_reason=exhausted / stop_reason_basis=apify_actor_completed` は、検索結果を全件取得できた証明ではない。

目標未達かつ Apify 予算・親期限内に時間が残る場合は、同じ語を1回だけ再取得する。既存の `tiktok_search` が追加run分も費用予約・精算する。2回の行は結合せず、多く取れた単一runの表示順を採用し、両runの根拠を `apify_attempts` に保持する。再取得が失敗・予算不足・期限切れでも先の有効なpartialを破棄しない。試行マーカーと保存済み結果により再開時の追加課金を防ぐ。最終配送は既存の「一部の数字が足りません」を使う。

r81相当の偽物（6語中5語50件、3語目は221秒・`SUCCEEDED / Scraped 0/1 search queries / 13件`）で関連判定・PPTX生成・偽DM配送を検証する。再取得50件では検索3のcheckpointは `complete`。固定キットの100件基準は維持するため、資料全体のQAでは引き続きpartialになり得る。

調査上の限界: 修正前のsrcでも `_evidence → _source → judge_query → prepare_inputs` は13/50の偽物を受け入れた。件数不足だけから本番の `ValueError` は再現できず、原因は未特定。元raw・モデル応答または例外メッセージが必要。prefetch警告とstageの未完了化には、例外型に加えてURL・トークン等を伏せたメッセージ先頭200字を記録する。

### 検索入力と未完了通知の確認（2026-10-30）

`ApifyClient.tiktok_search` は `clockworks~tiktok-scraper` に `searchQueries=[query] / searchSection="" / resultsPerPage=target` とダウンロード無効の3項目を送る。`max_items` はREST入力の `maxItems` ではなく、dataset取得の `limit` と費用予約に使う。50件指定も偽HTTPで固定したが、リポジトリ内に検索用Actorの公式schema・READMEの写しは見つからない。**要確認**: 件数・ページ送りの正式項目、空の `searchSection` の意味、50件取得に必要な条件。根拠のない入力変更はしていない。

再試行前の待機はなく、同一入力を最大1回繰り返す。初回・再試行・証拠保存は同じ `min(親の残り期限, TIKTOK_APIFY_FALLBACK_DEADLINE_S)` を共有する。240秒中221秒で初回13件なら再試行は19秒。RUNNINGのまま期限に達するとabortし、50×$0.004=$0.2を保守的に台帳計上する（実請求額の証明ではない）。この経路を偽時計で再現した。再試行0件・時間切れでも初回の検証済み13件は保持する。本番の0件の理由・実請求はActorのrunログ等が必要。

完成時は `Parent.status → prepare_delivery → Origin.defer` で添付を予約し、共通の `async_job_notify.publish_notice` が送信・記録する。未完了時は `not_ready / none` のため添付を予約しないが、従来から共通監視の本文投稿対象ではある。したがって `slack_delivered=false` だけでは未通知を断定できない。未完了の終端本文は、同じ通知済みロックと既存DM退避経路で検証済み本人へ送る。成功時は `status=incomplete` を維持し、`slack_delivered=true / delivery_target=dm / delivery_status=delivered` と記録する。「添付済み」とは表示しない。確定した送信失敗は通知ロックを解放し、タイムアウト時は重複を避けて自動再送しない。

## 固定取得物

`tools/first_visit/sales25.lock.json` と `sales25/` を組にする。正本 `tiktok-sales-research-kit` の commit `f8c98b4a5555479e28fee71a4195eaf38feeec09`、version `1.2.4`、format `sales25` から、4資産と5スクリプトを同梱した。`check_fmt.py` の投稿別媒体不足への対応はローカル修正であり、lock の `local_patches` に正本の SHA と理由を記録する。

- 資産: `build-info.json / template.pptx / fields.json / template-plan.json`。
- CLI: `fill_template.py / check_fmt.py / check_top5_analysis.py`。
- 直接依存: `kit_contract.py / metrics.py`。

配布 ZIP と計画表の8つの SHA を正本で照合して取り込んだ。lock は全9ファイルの SHA、正本の依存ファイル2点の SHA（出所情報。インストールはしない）、adapter 契約版を記録する。lock 自身の SHA は adapter に固定する。追加・削除・改変・symlink を拒否する。キットの自動取得・展開・更新・依存インストールはない。Docker 収録やリリース契約への追加は後続 PR。

期待長が整数値の場合は丸めを考慮して実測との差を1秒、小数値は0.5秒まで許容する。実測・操作の180秒上限は維持する。既知の180秒超は操作を作らず「動画は確認対象外（長尺）」、取得失敗は「媒体不足」として投稿別に記録する。分析の `media_missing` は投稿ID・元URL・理由を保持し、その投稿の観察事実や画像を作らない。取得済み投稿の同一性・全区間検証は従来どおり必須。

TOP5の不足画像は理由を表示し、計画の `media_missing` と分析記録の一致を検証する。FMT検査は明示した不足分だけ画像数から差し引く。ランキング・取得済みの数値は保持し、PR事例や提案の未確認部分も明示した25ページを partial で配送する。スナップショットのサイズ超過や同一性不一致など、投稿別の取得失敗以外は従来どおり停止する。

## 入力と API

```python
convert_inputs(case, raw_by_query_id, decisions_by_query_id, *, owner_id) -> dict
write_inputs(root, inputs)                    # 新しいディレクトリのみ
validate_inputs(root, *, owner_id)             # 不整合は例外で停止
seal_inputs(root, *, owner_id) -> dict         # sales25-manifest.json を排他作成
verify_manifest(root, *, owner_id)             # ファイル集合・全SHA・size・owner・資産を再検証
generate(root, output_dir, *, owner_id) -> dict # sealed root 外の新規ディレクトリのみ
check_deck(root, pptx, qa_dir, *, owner_id) -> dict
```

`case` は adapter 専用の `edition: sales25 / owner_id / category / queries` を持つ。queries は自社、競合2社、中立3語の順で6件。各行は `query_id / role / group / query`。role は `self / competitor / market_keyword`、query_id は `[a-z][a-z0-9_]{0,39}`。ID・実 query・group はそれぞれ6件で一意。検索語をブランド名から再作成しない。これは旧 case の上書き変換ではない。

query ごとに以下を保存する。

|パス|内容|
|---|---|
|`raw/<query_id>.json`|入力の raw 全体。重複行と元の欠測表現も保持|
|`selection/decisions_<query_id>.json`|全ユニーク投稿の `id / eligible / reason`|
|`selection/source_<query_id>.json`|raw から導出した選定用母集団。初出 ID の表示順位と全投稿フィールドを保持|

raw は `owner_id / query / fetched_at / status / sessions: 1 / order_basis: search_display_order / unfiltered: true / missingness_preserved: true / stop_reason / diagnostics / videos` が必要。日時不明や複数セッションを補完しない。status は投稿がある `ok`、または `error_code: TIKTOK_TRULY_EMPTY` を持つ空の `observed_zero`。取得失敗・不明を実測0へ変換しない。stop_reason は `exhausted / no_new / capped`。CAPTCHA 検出は拒否する。

各投稿は、文字列の投稿 ID、同じ ID の HTTPS TikTok video/photo URL、連続した `rank_display`、5指標の stats、明示的な `missingFields` を持つ。数値は非負整数または欠測 null。欠測リストに載る指標は source で null に戻す。載らない0は実測0として保持する。元の欠測リストがない legacy データは受け付けない。広告・写真・本文等の追加フィールドも raw/source から落とさない。

同じ query の重複 ID は source だけ初出で一意化する。別 query の同じ ID は独立した母集団・順位・判定を保持する。同一 ID の URL 不一致は拒否する。封印時に raw と decisions から source を再導出し、JSON 全体を照合する。source だけ数字や URL を直して通すことはできない。

案件作成後、作者が以下を追加してから封印する。

- `deck-plan.json`: sales25 の25頁 role 順、各頁の実 query/group/投稿順、3ブランドの `analysis_selection`（`top_n: 10 / target_count_per_query: 100 / queries`）。各 query は `source_path / rankings`。再生数・ENG率・保存数を raw 由来 source から再計算し、同点は表示順位。ENG率は `(いいね+コメント+共有+保存)/再生*100`、必要値欠測と再生0は対象外。TOP5 は各 TOP10 の先頭5件。
- `top5-analysis.json`: キット checker の review 形式。TOP10 の ID 和集合すべての媒体、SHA、opened_files、時刻/写真番号付き evidence、visual/text/audio 記録、各 TOP 頁の observation/interpretation/proposal/refs が必要。6〜10位の根拠も同一 query/指標内で使用できる。実視聴・聴取の真偽を機械検査で証明するものではない。
- `deck-input.json`: 固定 fields に対応する `texts / images / links / notes`。画像・媒体・証拠はすべて案件内の相対パス。TOP5 画像の順序・URL は掲載 ID、画像パスは該当 review の opened_files に一致させる。
- `deck-plan.pr_examples`: P21〜23 の `page / query_id / post_id / pr_basis / observation_key / observation / proposal_key / proposal`。raw の isAd=true または PR タグと、同じ投稿の review、3つの別画像と URL、正本の右2枠の原稿を照合する。広告フラグを PR タグから書き換えない。

manifest は `schema_version / owner_id / kit_lock_sha256 / files` を持つ。files は manifest 自身を除く全ファイルの SHA-256 と size。追加ファイル、同じサイズの書き換え、別 owner、案件外パス、symlink を拒否する。owner_id は呼出側が信頼して渡す識別子であり、このオフライン API 自体に本人認証や署名機能はない。

## 生成と機械 QA

`generate()` は指定 Python（既定は実行中 Python）で、固定した3本の CLI を `-B`、shell 不使用、各120秒上限で起動する。任意コマンド指定や旧経路への fallback はない。`analysis_selection` を封印・再開時に必須化し、checker の `legacy_top5` 合格も成功にしない。

```text
output_dir/
  deck.pptx
  build-status.json              # filler 自身の記録
  filler-qa/fill_template.{stdout,stderr,json}
  qa/check_fmt.{stdout,stderr,json}
  qa/check_top5_analysis.{stdout,stderr,json}
  qa/result.json                # input manifest と最終PPTXのSHAを記録
```

checker は片方が invalid でも両方を実行して記録する。stdout/stderr と終了コードを保持し、analysis の exit 2 は `incomplete` として保存する。filler の失敗はログを残して停止する。新規出力だけを許し、既存の成果物を上書きしない。入力 manifest は実行前後に再検証する。

result.json の `acquisition` には6 queryすべての `query_id / target / actual / shortfall` を保存する。実数は重複除外後の source の件数。ブランド3社のcheckerが合格しても、中立queryを含むいずれかの100件未達は全体の `incomplete` として残す。

機械検査の `passed` は納品完了ではない。`delivery_status: incomplete / visual_review: not_verified` は常に維持する。全25頁の描画、字体、見切れ、音声確認の実証、全編分析の品質、本人限定公開は後続 PR の責任。この段階で本番経路は接続しない。

## Pillow 13 と再現テスト

上流 requirements の `Pillow<13` は変更していない。Aico の予定環境13.0.0の互換性を、同梱した実 filler を呼ぶテストで別に確認する。縦 PNG の原寸、横 JPEG の比率と中央透明余白、透過 PNG のアルファを、25頁 PPTX に実際に埋め込まれた画像から照合する。12系へ戻す処理はない。

```bash
LC_ALL=C LANG=C PYTHONPATH=src perl -e 'alarm 300; exec @ARGV' \
  ~/dev/teamagent/.venv/bin/python -m pytest -q \
  tests/skills/first_visit tests/skills/omiyage_report
```

CI が13.0.0ならそのまま動作する。ローカルの指定 venv が別版の場合だけ、既存13.0.0をコピーした隔離ディレクトリを `SALES25_PILLOW13_PATH` に指定できる。互換テストの子プロセスだけその PIL を先頭に置く。13以外ならスキップせず失敗する。fixture はブランドA/B/Cと合成写真・合成 ID のみで、取得・外部書き込みは行わない。
