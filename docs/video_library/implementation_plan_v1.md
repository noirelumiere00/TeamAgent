# 構成×動画 相互検索 — 実装計画 v1（2026-10-29）

要件: https://claude.ai/artifact/M9FBZdu7bhq8t8MXmumWFp（v0.3）
実装: Codex（gpt-6.1-sol）、レビュー: Claude。リポジトリは TeamAgent（`origin/dev` から worktree `~/dev/worktrees/teamagent-video-library`）。

## 方針
- 新しいサービスを作らず、TeamAgent の部品を流用する（gsheets_client / video_download / thumbnails.fetch_cover / dashboard.auth / ingest_schedule / RDS / S3）
- 3層分離（adapters / skills / runtime）、Pydantic I/O、mypy --strict、構造化ログ（CLAUDE.md §3）
- 入力の正本はスプレッドシートのまま。DB は毎日同期
- 本番 DB の migration・terraform apply・ECR push は内容を見せてから小俣さんの GO で実行

## 10/30 の改定（小俣さん判断・アドバイザー合意）
- 入力の分担：動画と数値はシートのまま（Apify と運用担当の作業は変えない）。人が決める値（案件の種別・業種・目的・非公開、アカウントの登録）は Web の設定テーブルに置き、同期はそこに触れない。有効値は「Web の設定 ＞ 台帳シート ＞ 未設定」
- 事業区分：#PR あり＝施策の動画で、区分は案件の種別（メディア／TTO／切り抜き／その他）。#PR なし＝オーガニック。運営は区分の判定に使わない
- アカウント登録：Web から登録し、登録したアカウントは直近の投稿を差分で自動取得する（累積で全部は取らない）。シートに無い gracemode などのアカウントもこれで集める
- 案件マスタのタブは作らない（P1 のタブ読みは P3 で Web 設定に置き換えて削る）

## 段階（順番: P1 → P2a → P3 → P3b → P2b → P4 → P5 → P6）
### P1 DB と同期（AI なし）
- migration `0035_video_library.sql`（追加のみ・既存テーブルに触れない）
  - `vl_videos`（媒体＋動画ID・URL・アカウント・投稿日時・尺・BGM・投稿文・ハッシュタグ配列・事業区分・出どころ・PR印・非公開印・広告主・案件・サムネ S3 キー・動画 S3 キー）
  - `vl_metrics`（動画×取得日のスナップショット。オーガニック／広告を列で分離。欠損は NULL＝算出不可）
  - `vl_campaigns`（広告主＋案件名・事業区分・業種・目的・期間・非公開・レポートの Drive ID）
  - `vl_accounts`、`vl_business_categories`（区分の一覧・増やせる）、`vl_ingest_runs`
  - P6 用のテーブル（タグ辞書・事実・判定・点数）は読む側ができる P6 で作る
- 同期ジョブ `ingest/video_library_sync.py`: データベースタブ（広告指標は L〜AI 列に動画ごとに入っている）＋全体Raw（尺・BGM・投稿日時）を動画URL／動画IDで結合。結合・欠損判定は既存 `campaign_aggregate.py`（`is_missing` / `to_number` / 全体Raw の URL 結合）を流用し、Aico の既存回答と数字をそろえる
  - 広告Raw は P1 では結合しない（きれいな結合キーが無い。形式名の抽出は P6 で扱う）
  - 事業区分: 広告主名なし（#N/A）×アカウント台帳の運営が VCL ⇒「メディア」を規則で付ける。案件に紐づく分（約54案件）だけ、シートに足す「案件マスタ」タブに人が書く（推測で付けない）
  - PR 印 ＝「広告主名あり」または「投稿文に #PR」。広告配信の有無は PR 印に使わず vl_metrics に残す
  - 既存の env（`SHORT_VIDEO_DB_SHEET_ID` / `_GID`）を使い、新しい env を増やさない
  - 動画IDを主キーに upsert（データベースタブ約3,400行に対し重複削除後は約2,800。重複・空行・審査落ちが混ざる前提）
  - 指標の定義は要件 v0.3 のとおり（保存率・ENG率、欠損＝算出不可、再生5,000未満は参考値）
- 冪等（同じ入力で2回流しても結果が同じ）。取込の件数・失敗を `vl_ingest_runs` に記録

### P2a サムネイルを S3 へ（P3 の前）
- カバー画像（yt-dlp のメタデータ or 既存スクレイパの cover_url）を取得して S3 に保存。既存 `thumbnails.fetch_cover` を流用
- S3 は新バケットを作らず `media_jobs` バケットの prefix で始められるか確認
- 署名付き CDN URL は期限切れになるので、取得したらすぐ保存。失敗は記録して次回再試行

### P3b アカウント登録と自動取得
- `/library` の管理画面で、アカウントを登録すると直近の投稿（例：最新30本）を定期的に取りに行き、まだ無い動画だけを追加する
- 取得は既存の仕組み（tiktok_scraper か Apify）を流用。取得元・取得日を記録し、外部の一般投稿も同じ経路で入れる

### P2b 動画ファイルを S3 へ（P3 の後・バックグラウンド）
- 既存 `video_download.download_video_chained` で新しい動画から順に保存

### P3 出口① 社内一覧（箱）
- connect_web（connect.newstv.co.jp・API Gateway→内部 ALB→Fargate）の FastAPI に `/library` ルートを足す。既存の `/search/login`（Google id_token＋許可リスト）を流用するので、新しいタスク・ALB ルール・DNS は不要（ALB は internal なのでホストルールだけでは外から届かない）
- 許可リスト `CONNECT_SEARCH_ALLOWED_EMAILS` は本番で明示が必須（未設定だと個人固定にフォールバックする既知の caveat）
- 管理ページ2枚（編集は管理者＝小俣さんのみ）：①案件（種別・業種・目的・非公開）②アカウント（一覧・検索・登録）。設定テーブルには更新者・更新日時を残す
- 検索（フリーワード・事業区分・業種・アカウント・期間・PR印）、並び替え（再生・保存率・ENG率）、選んだ動画の URL 一覧をコピー

### P4 出口③ Aico
- `video_library_search` skill を追加（4段ゲート: skill → factory `USE_VIDEO_LIBRARY` 既定OFF → toolFilter.include → 本番 env）
- 既存の事例回答（campaign_aggregate）との棲み分けを description に書き、ルーティング・シミュで確認

### P5 出口② メディア風（公開）
- 内藤さんの見た目の判断を待って着手
- DB から公開してよい項目だけを書き出した静的サイトを毎日生成し、S3＋CloudFront で配信（サーバー無し＝攻撃面が小さい）
- 再生は TikTok 埋め込みプレイヤー（カーソル時のみ読み込み）

### P6 構成分析と点数（保留中の構想）
- 判定の基準・重み・項目の出し入れは DB の設定（版付き・変更履歴つき）として持ち、箱（出口①）の管理者画面から変えられるようにする。基準は「数値の範囲・あり／なし・選択肢」の型だけ（自由な式は書けない）。変更したら保存済みの事実から付け直し、旧版との点数差を反映前に見られる
- 楢原さんの制作基準10項目は合否ではなく加点要素。著名人の声・素材の権利は点数とは別に注意の印
- テーブルだけ P1 で用意。分析の実行は小俣さんの GO 待ち

## 検証
- 単体テスト（シートの fake は本番の失敗モード＝#N/A・空セル・カンマ付き数値・重複URL を再現）、変異テスト
- 本番シートを読み取り専用で流し、件数をシートと突き合わせる
- CI と同じ依存（`--extra dev --extra mcp --extra media`）、ruff format・mypy --strict・import-linter

## 小俣さんの GO が要る操作
- 本番 RDS への migration 適用（追加のみ）
- terraform apply（同期スケジュール・connect_web の env・S3 権限）、ECR push
- シートへの「案件マスタ」タブ追加（共有シートの編集）
- DNS は P5（公開サイト）で newstv.co.jp のサブドメインを追加するときだけ

## 1本目（Codex への最初の指示）の完了条件
- P1 だけ。本番シートを読み取り専用で流し、件数をタブと突き合わせた結果を PR に貼る
- アドバイザー確認済み（2026-10-29）：配置・広告Raw・事業区分の規則・PR印の定義・順番を反映
