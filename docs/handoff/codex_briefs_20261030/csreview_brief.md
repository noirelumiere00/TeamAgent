# 依頼: 人が確認した案件の状態（official）を、1 件目から本番で付けられるようにする

作業場所: ~/dev/worktrees/teamagent-csreview（branch feat/case-state-first-review-20261105・origin/dev 起点）

## 背景（事実）
- 正式な状態は `data/case_state_reviews.yaml`（今は `reviews: []`）にだけ入る（src/teamagent/ingest/case_state_evidence.py の `attach`）。
- 本番の ingest タスクの env: `INGEST_OWNER_EMAIL` は取込用の共通アカウント 1 つ（全資料の owner_email が同じ）、`CASE_STATE_EVIDENCE_ALLOWED_EMAILS` は利用者 1 人だけ。
  → `attach` / `repository.refresh_case_state_evidence` / pipeline の `enabled(owner_email)` 判定で全資料が弾かれ、本番に根拠束は 1 件も付いていない。
- 一方 `configured()` は真なので、`_should_skip_unchanged_gdrive_file`（pipeline.py:847 付近）の Drive MD5 skip は毎晩止まっている。
- 最初に入れたい記録: Drive ファイル `1hzRoEBotqN8-lxQMfkapOfeLtOM9zvYN`（JTB 企業広報の提案書・2026-09-11）を state=lost（見送り）として official 記録する。確認は 2026-11-05。

## 直すこと（小さく）
1. **確認表に載った資料は owner の許可に関係なく official 記録を付ける。** manifest の reviews に (source_type, external_id) が一致する資料だけ、`enabled(owner)` が偽でも attach と refresh_case_state_evidence が official 記録を合成する。営業報告（reported）の記録・それ以外の資料の挙動は今のまま（owner 許可が要る）。検索時の利用者ゲート（pgvector_client 側）は変えない。
2. **確認表と資料の版が食い違っても ingest 全体を止めない。** `attach` の "case state review source revision/position mismatch" などの ValueError が、各 attach 呼び出し元（pipeline.py の全箇所・refresh 含む）で run を止めるか確認する。止まるなら、その資料は確認記録なしで従来どおり取り込み、`case_state_review_mismatch`（external_id_ref だけ・原文なし）を warning で出す。manifest 自体の形式不正は今のまま例外でよい。
3. **source_ref を取る手段。** 1 つの Drive ファイル ID を受け取り、通常の Drive 取込と同じ処理（テキスト抽出・チャンク分割・metadata 組み立て。分類 cls_* と embedding・DB 書込は不要）で DocumentUpsert と ChunkUpsert を作り、`source_ref(doc, chunks)` を JSON で出す CLI を足す。出力は source_type・external_id・locator・url・content_sha256 と、各チャンク/ページの位置キーと先頭 120 文字（excerpt を選ぶため）。既存の関数を使い、取込処理を二重実装しない。ECS の ingest タスクで `python -m …` として動く形にする（Drive の認証は ingest と同じ env を使う）。
   - 注意: digest は取込時の metadata（cls_ 除く）を含む。CLI と本番取込で metadata が一致しないと hash がずれる。一致することをテストで示す（同じ入力で pipeline 経路の attach が使う source_ref と CLI の出力が等しい）。
4. MD5 skip の停止を、確認表に載った資料だけに絞れるなら絞る（`configured()` 全体で止めない）。難しければ現状維持で、理由を報告。

## やらないこと
- manifest への JTB の実記録の追加（Claude が CLI の結果を見てから足す）。
- ツールの description、DB スキーマ・マイグレーション、terraform、検索時の表示の変更。
- git add・commit・push、AWS、.env・secrets の読み取り。

## 合格条件
- テストを足す: (a) owner 非許可でも manifest 一致の資料には official 記録が付き、非一致の資料には何も付かない (b) 版の食い違いで run が止まらず warning が出る (c) CLI の source_ref が pipeline 経路と同じ hash になる (d) refresh_case_state_evidence も (a) と同じ判定。
- 次が全部緑:
  uv run --extra dev --extra mcp pytest -q -n 4 tests/ingest tests/skills/search -k "case_state or evidence or unchanged or skip"
  uv run --extra dev --extra mcp ruff check src tests scripts
  uv run --extra dev --extra mcp ruff format --check src tests scripts
  uv run --extra dev --extra mcp mypy src/teamagent/ingest src/teamagent/case_state.py
- pytest の事前確認（ps）は Claude が済ませた。手元で走る pytest はこの 1 本だけ。

最後に: 2 の確認結果（止まっていたか・ファイル:行）、4 の判断、CLI の起動コマンド例、テスト結果を短く。
