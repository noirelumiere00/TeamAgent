# 案 A / PR2: 状態の根拠束

`documents.metadata.case_state_evidence` に version=1 の根拠束を保存する。
既存 JSONB を使うため migration は不要。既存の分類・本文・日付キーは維持する。

## 公開フラグ

`CASE_STATE_EVIDENCE_ALLOWED_EMAILS` が空なら OFF、カンマ区切りのメールなら対象者のみ、
`*` ならメールが解決できた全員。既存 `email_allowed` と同じ照合を使う。
取り込みでは `owner_email`、検索では解決済み `ctx.metadata.user_email` に適用する。
検索の SQL 射影は許可リスト設定時だけ追加し、公開フィールドは対象者にだけ返す。
OFF の場合は manifest を読まず、既存の upsert metadata・出力を変えない。

## 保存する事実

- `subject_ref`: 既定は source type・external ID・根拠位置の組。取引先名や施策名で統合しない。
- `document_kind`: 資料の分類。提案書・事例掲載は正式受注の根拠にしない。
- `records`: 営業報告と人手確認を区別する。生フェーズは `raw_value` に保存し、
  `cls_phase`、チャンネル、BANT、投稿数から正式状態を作らない。
- `source_ref`: 同じ対象の ID・行/ページ/投稿位置・表示 URL・原文・版 hash。
- `evidence_date` と種別: Slack は各投稿自身の JST 投稿日、フォームは提出日時のみ。
  返信を親の日付と組み合わせず、処理日時・資料更新日を状態時点にしない。
- `state_as_of` と精度: 明示された日/月だけ。報告日で補わない。
- `delivery_observation`: 投稿数・投稿期間・既存集計の数値文字列。
  数字と状態は同じ source_ref に結び付けるが、投稿確認を受注・全体完了に変換しない。

保存時の正式状態は unknown。既存 `timeline_status.resolve_statuses(..., case_state=True)` が
公式の確認記録だけを使って解決する。旧行・不正型・旧版・出典不一致は unknown（不明）。
同時点または重なる月の食い違いは conflict。最新資料の更新日・検索順位では解消しない。
同一案件への統合は manifest の明示対応だけ。競合時は全ての可視の根拠束を保持する。

## 人手確認と限定再取り込み

正本が未確認のため、`data/case_state_reviews.yaml` の初期値は空。
既定のパスは作業ディレクトリ基準（既存コンテナでは `/app/data/`）。
必要なら `CASE_STATE_REVIEW_MANIFEST` で確認済み manifest のローカルパスを指定する。
形式は `version: 1` と `reviews` 配列。各項目は次の二つだけを持つ。

- `subject_ref`: 人が対応を確認した安定 ID。
- `record`: `StateRecord` の official 記録。`state`、根拠日と種別、状態時点と日/月精度、
  `reviewed_by`（確認者 ID）、`reviewed_on`、`review_ref`（確認記録 ID）、
  `source_ref` を必須とする。根拠部分が空の確認は受け付けない。

`source_ref` は取得済み `DocumentUpsert` と本文の `ChunkUpsert` から
`teamagent.ingest.case_state_evidence.source_ref(doc, chunks)` で得る。
必要に応じて locator を `chunk:<chunk_idx>` / `page:<page_num>`、excerpt を実際の根拠部分へ
絞る。指定位置にその原文が存在し、同じ資料の版 hash と URL に結び付くことを検証する。
元資料・版・根拠部分・対象対応を人が確認してから記録する。
対象の source type/external ID は完全一致、位置・内容・元の更新日時が変わった記録は
例外として適用を止める。同名の別対象には適用しない。

取得済みの入力だけで、接続なしの乾式差分を確認できる。

```sh
CASE_STATE_EVIDENCE_ALLOWED_EMAILS='reviewer@example.test' \
  uv run python -m teamagent.ingest.case_state_evidence /tmp/upsert-input.json
```

入力は `[{"document": {...}, "chunks": [{"chunk_idx": 0, "content": "...", "page_num": 1}]}]`。
document は既存 `DocumentUpsert` のフィールド（owner_email を含む）。embedding は不要。
出力は hash・変更有無・状態・日付・投稿数だけで、原文や対象名は出さない。
確認済み manifest の変更と判定版は差分 ingest の入力 hash に含む。
Drive の MD5 skip は段階公開中だけ停止し、同じ入力を再取り込みしても確認を再合成する。

実再取り込み時は既存 `scripts/ingest_sources.py --yaml <限定した設定> --sources <対象種別>`
で範囲を限定し、乾式差分確認後に既存の投入手順を使う。本 PR の作業中は接続・投入しない。

## PR3 への引き継ぎ

通常検索・fanout・カルテ・事例・名指し資料・ライブ Slack に束を通した。
公開 `SearchHitOut` / `SlackHitOut` も対象者だけ同じ型を返す。
PR3 は複数ヒットを `timeline_status` にまとめて渡し、`evidence_sources` と解決結果から
全回答面・一覧・二段返し・Vault・派生 skill を固定描画する。
自由文の状態推測と外側の再作文防止、直接配信失敗時の同じ結果の再提示は PR3 の範囲。
現段階では既存の自由文回答経路と `SEARCH_TIMELINE_STATUS` の互換動作を維持している。
このフラグだけで全回答の誤断定が防止されるとは扱わず、PR3 完了後に利用者向け公開を検証する。
匿名 35 件の正本・期待値は未提供であり、今回の人工回帰入力と区別する。
