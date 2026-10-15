# 初訪入力契約（移植・単位①）

`teamagent.skills.first_visit.contract` は、お土産の取得データを初訪キットの入力へ変換する独立モジュール。既存のお土産経路への接続、取得・分析・資料生成・配達は行わない。キットは取り込まず、`kit_path` にキットのリポジトリルートを明示する。

## API

```python
convert_inputs(request, axes, normalized, *, acquisition, selection=(), case_details=None)
write_inputs(root, inputs)
missing_inputs(root, *, kit_path, kit_python=None) -> list[str]
seal_inputs(root, *, kit_path, kit_python=None) -> dict
verify_manifest(root, *, kit_path, kit_python=None) -> list[str]
```

- `convert_inputs()` は相対JSONパスからJSON内容への辞書を返す。主な出力は `case.json`、`raw/<axis>.json` の `videos[]`、`selection.json`。軸ごとに異なるファイルを使う。
- `write_inputs()` はその辞書を案件ルートへ書き出す。
- `missing_inputs()` は不足・不整合を返す。空リストになるまで生成に進めない。キットの入力ゲートによる検査も含む。
- `seal_inputs()` は不足がない案件を固定し、ファイルごとのSHA-256・サイズ・契約版とキット版を記録したmanifestを作る。
- `verify_manifest()` は記録済みファイルとキットの一致を確認し、不一致を返す。

`selection`、カバー、動画、フレーム、書き起こし、anatomy、原稿などの証拠は、すべて同じ案件ルートに置く。別案件や案件外のファイルを参照しない。

`kit_python` はPillowを持つPythonのパス。省略時は実行中のPythonを使う。ゲートは `-B` を付けた別プロセスで実行し、キットへpycacheを書かない。git検証も `--no-optional-locks` でindexを更新しない。不足時は `seal_inputs()` が `IncompleteInputsError.problems` を返して停止する。既存manifestは上書きしない。単位③の生成入口は `verify_manifest()` が空リストの場合だけ進む必要がある。

## 変換と取得証跡

| Aico | 初訪入力 |
|---|---|
| `general` / `brand` / `competitor` | `category` / `own` / `competitor` |
| `provider_id` / `PostRecord.video_id` | `id`（文字列。内部`pid`は使わない） |
| `title` / `caption` | `desc` |
| `account_id` / `author` | `author.uniqueId` |
| `account_name` / `nickname` | `author.nickname` |
| `followers` | `author.followerCount` |
| `plays` / `likes` / `comments` / `shares` / `saves` | `stats.playCount` / `diggCount` / `commentCount` / `shareCount` / `collectCount` |
| `cover_url` / `create_time` / `duration` | `coverUrl` / `createTime` / `duration` |
| `url` / `hashtags` | 同名 |

率は件数からキットで再計算する。元の配列順、元順位、投稿ID・URL、実行済みの`query`を保持する。短い社名から検索語を作り直さない。同じ投稿が複数軸に現れたら各軸に残し、軸ごとの順位・判定を保つ。`selection`のID・URLはrawの投稿と一致させる。

`AxisData.label` は実際の表示文と依頼名を照合して対応付ける。rawのファイル名は入力軸順の `raw/axis_01.json` 等。変換自体は順位の抜けや元順序をそのまま残し、欠落した表示順位があれば不足検査で停止する。同じqueryの軸は同一取得スナップショットを共有する契約であり、別取得の結果を同じ `kw` で連結して渡してはいけない。②では軸ID・取得job参照で別スナップショットを識別する。

`acquisition` は `"<role>:<label>"` をキーにした軸別メタデータ。次の情報を明示する。

- `status`: `ok` / `observed_zero` / `failed` / `unknown`。投稿が0件という理由だけで`observed_zero`にしない。
- `acquired_on`、`order_basis`、`sessions`、`stop_reason`: 実際の取得日時・順序根拠・セッション数・終了理由。
- `unfiltered`: 関連性除外前の一覧であること。
- `missingness_preserved`: 元応答の欠測と実測0を区別して保持できていること。

`observed_zero` は `error_code: "TIKTOK_TRULY_EMPTY"`、またはmediaの対応するtrue-empty shortfallを必要とする。取得日時の原文は `aico.source.acquired_on` に保持し、キット用の `fetched_on` と主一般軸の `case.acquired_on` はその日付を使う。日時不明を実行日で補わない。`missingFields` はキットのフィールド名（例 `stats.playCount`）で渡せる。該当数値はnullへ戻し、出力にも欠測フィールド一覧を付ける。

欠測は欠測のまま残し、取得失敗とは分ける。`isAd`の真・偽・未取得も区別し、`#PR`から広告フラグを推測しない。既存の`PostRecord`や`posts.normalized.json`で欠測から変換された0は復元不能。証跡のない0を実測0として扱わない。

## 担当者が確定する情報

`case_details` は不足する案件情報を渡す。

- `brand_details`: ブランド名から `{short, official_status, official}` への対応。公式未確認と公式無しを区別する。
- `vocab`、`focus_products`、`client_short`。
- `project`: `project_title`、`recipient`。
- `research`: `review_collect`を明示。trueなら案件内 `reviews/` に取得ログ・原文JSONL・分類JSONLを用意する。初訪は既定3企画・台本不要。

カテゴリ、口コミの収集有無、公式確認、語彙、商品などは未回答を既定値で確定しない。加えてカバー実体、投稿×軸の`labels.json`、再計算した`first_visit.json`と入力指紋、`selection.json`、選定投稿の実動画・全尺フレーム・書き起こし・`anatomy`・`anatomy_page`、`authored/synthesis.json`等が必要。`missing_inputs()`で具体的な不足を確認する。初訪の台本は要求しない。

## 固定するキット

単位①の対象は `VERSION = 2026.10.14.1`、git HEAD `5544938b7f1695a7f13a646ea4b7fd8cadf37629`。manifestには版とgitの指紋を記録する。別版や未固定の変更を暗黙に受け入れない。キット側のソースを変更せず、設定された`kit_path`の入力ゲートを使う。

## 単位②で追加する保存

1. `tools/tiktok_scraper/search.mjs` の `normalizeVideo()` より前に、元応答のキー存在・null・実測0・`isAd`・写真種別・言語を保存する。現状はここですでに`|| 0`への変換と項目削除がある。
2. 検索条件、実検索語、取得時刻、順序根拠、セッション数、終了理由、診断を保存する。複数セッションの頻度順を表示順と偽らない。
3. mediaの`shortfalls`と結果メタデータ、子ジョブID、不変成果物参照（VersionId・SHA-256・サイズ・Content-Type）を親案件に結び付ける。現行の同期互換アダプタは投稿配列だけを返し、空結果と失敗の区別を失う。
4. `PostRecord`への縮約・順位再採番と関連性除外より前の投稿一覧を保存する。除外ID・理由は別に保存し、未除外rawを上書きしない。
5. カバー・動画・時刻付き全尺フレーム・音声処理結果を、案件の入力manifestから追える形で保存する。未処理を発話なしとして記録しない。集計済みの`deck_plan.json`・`audit.json`だけでは代用できない。

単位①の合成fixtureは実在取引先を使わず、この変換と不足検出・改変検出を確認する。実案件の取得、永続保存、生成workerの接続は単位②以降で行う。

## オフライン検証

```bash
FIRST_VISIT_KIT_PATH=/path/to/tiktok-skills-kit \
FIRST_VISIT_KIT_PYTHON=/path/to/python-with-pillow \
.venv/bin/python -m pytest -q -p no:warnings tests/skills/first_visit
.venv/bin/ruff check src/ tests/ scripts/
MYPYPATH=src .venv/bin/mypy src/teamagent/skills/first_visit tests/skills/first_visit
```

外部キットのパス未設定時はゲート統合テストだけskipする。変換テストはキット不要。合成fixtureの画像は開けるJPEG、動画は明記した非再生ダミー。入力ゲートの契約確認用であり、実動画の妥当性・人やAIの観測・描画品質を証明しない。

2026-10-15の確認結果:

- 新規129件が合格。実キットの初訪ゲートを呼び、台本無し・3企画で合格。不足証拠の削除、ID/URL・指標・順位・広告・フレーム時刻・観測記録・指紋・口コミ0件/失敗・版の変異を拒否。
- 既存 `tests/skills/omiyage_report` と `tests/media/test_tiktok_operations.py` は259件合格、python-pptx不足の2件skip。既存の追跡ファイルは変更なし。
- コード変異9件をすべて検出: provider_id→pid、欠測→0、順位再採番、広告未取得→false、キットゲートのエラー無視、manifestのframes除外、不足時の封印許可、キット版検証無視、検査中の入力変更許可。変異後は原本を復元して再検証。
- ruffは `src/ tests/ scripts/` 全体、mypyは新規モジュール・テスト6ファイルが合格。全体mypyは既存 `media/deck_qa.py` と `skills/proposal_deck/renderer.py` のPillow不足に伴う3エラーで不合格。依存追加・既存コードの変更で回避していない。

全pytest・実媒体取得・実描画は対象外。キットのバイトコード生成、ネットワーク利用、commit/push、既存お土産経路への接続は行わない。
