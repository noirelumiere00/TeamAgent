# sales25 入力・制作・検索契約（PR1・PR2・PR3）

`first_visit.sales25_contract` と `first_visit.sales25` は、通常ツールから登録・呼び出しされない独立入口。取得済み JSON と案件内画像だけを使う。旧 `first_visit.contract` の API、manifest、旧キット、お土産の生成・配送経路は変更しない。

## 内部100件検索（PR3）

`MediaJobClient.search_tiktok_sales25(query, *, owner_id, request_fingerprint, search_type="keyword", max_videos=100, timeout_s=300)` は `raw / acquisition_status / artifacts / media_job_id` を返す。本人確認済みの呼出側が渡す `owner_id` を、検証済みのworker rawへ結び付ける。`artifacts` の不変参照・SHAはworkerが保存した元のバイト列に対応し、ownerを結び付けた案件rawはその後のsales25 manifestで封印する。

内部要求の `edition="sales25"` は1語・最大100件・`metadata_only`・動画0・`sort="display"` に限定する。公開 `tiktok_acquire` と従来の内部検索は上限30。edition未指定の要求JSONには新しいeditionキーを出さない。親ジョブのrollout、同時実行制御、通常お土産経路への接続はPR6で扱う。

workerは次の3成果物を必須で保存し、欠落やupload失敗はジョブを失敗にする。サムネイルや動画の取得は行わない。

|成果物|内容|
|---|---|
|`sales25-raw.json`|JSの縮約前出力。nullableな5指標、missingFields、広告・写真・投稿URL、取得日時、単一セッションの表示順、拒否診断を保持|
|`acquisition-status.json`|`accepted / status / target / actual / shortfall / stop_reason / diagnostics / raw_ref`。raw_refは同じジョブのraw成果物名|
|`posts.json`|既存の投稿一覧形式。拒否時は空。分析の根拠には上のrawを使う|

100件未達は `acquisition_status.status="partial"` と実数で返す。正常なrawのstatusは既存の入力契約に合わせて `ok`、正常0件は `observed_zero` + `TIKTOK_TRULY_EMPTY`。CAPTCHA・CDN拒否・異常API応答は、途中の投稿をrawに保持しても `accepted=false / status="failed"` として採用しない。別セッション・hashtagからkeywordへのfallback・失敗後のworker自動再検索で穴埋めしない。取得日時がない失敗には日時を補完しない。

検索見積もりは30件以下120秒、31〜100件240秒をcore・worker・Lambdaで揃える。100件取得の実時間は未実測。DNS-pinned proxyとredirect制御を維持し、正本キットのengineは収録しない。mediaのJS固定SHAはDockerfile・release契約・連動テストを同時更新する。PR3/4/5統合後の世代再導出まで、変更済みrelease契約によるgeneration inputの不一致を解消しない。

## 固定取得物

`tools/first_visit/sales25.lock.json` と `sales25/` を組にする。正本 `tiktok-sales-research-kit` の commit `f8c98b4a5555479e28fee71a4195eaf38feeec09`、version `1.2.4`、format `sales25` から、4資産と5スクリプトをバイト変更せず同梱した。

- 資産: `build-info.json / template.pptx / fields.json / template-plan.json`。
- CLI: `fill_template.py / check_fmt.py / check_top5_analysis.py`。
- 直接依存: `kit_contract.py / metrics.py`。

配布 ZIP と計画表の8つの SHA を正本で照合して取り込んだ。lock は全9ファイルの SHA、正本の依存ファイル2点の SHA（出所情報。インストールはしない）、adapter 契約版を記録する。lock 自身の SHA は adapter に固定する。追加・削除・改変・symlink を拒否する。キットの自動取得・展開・更新・依存インストールはない。Docker 収録やリリース契約への追加は後続 PR。

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
