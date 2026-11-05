# P3 社内アーカイブ `/library` 設計案（2026-10-30・アドバイザー確認用）

最初のリリース。完了条件は計画 v2 の「v3：進め方の決定」に書いた4つ（営業・案件進行が絞り込み→カーソル確認→URL コピー／全約2,811本が出て毎日同期／管理者が案件の種別を Web で設定し即反映／目的の動画まで1分以内）。

## 置き場所
- `src/teamagent/connect_web/library.py` に APIRouter（または `register_library_routes(app, deps)`）を新設。app.py は認証・管理者判定・DB 接続を渡して登録する数行だけ
- 認証は既存の検索 cookie（`_search_email`、`/search/login` で Google ログイン、許可は `CONNECT_SEARCH_ALLOWED_EMAILS`）をそのまま使う。未ログインは `/search/login` へ（ログイン後に `/library` に戻す）
- 管理者は既存の `_admin_emails()`（env `CONNECT_ADMIN_EMAILS`）
- `/library` 配下のページだけ CSP の `frame-src` に `https://www.tiktok.com` を足す（既存のページ別 CSP の前例に合わせる）。他のページの CSP は変えない

## DB（migration 0036・追加のみ）
- `vl_campaign_settings(advertiser, campaign, business_category → vl_business_categories, industry, purpose, is_private, updated_by, updated_at)`：人が Web で決める案件の値
- `vl_account_settings(platform, handle, display_name, note, fetch_enabled, registered_by, registered_at, updated_at)`：Web で登録したアカウント（P3 は登録と一覧だけ、取得は P3b）
- `vl_video_analysis(platform, video_id, kind, version, payload JSONB, analyzed_at)`：構成の受け皿。P3 では空
- ビュー `vl_videos_effective`（`security_invoker = true`）：有効値を読み出し時に決める
  - 事業区分 ＝ `#PR` なしなら `organic`、`#PR` ありなら `vl_campaign_settings.business_category`（無ければ NULL＝未設定）
  - 非公開 ＝ `vl_campaign_settings.is_private`（無ければ false）
  - 業種・目的も同じく設定テーブルから
  - `/library`・Aico・将来の出口はすべてこのビューを読む（同期時のコピーに頼らない＝設定変更が即時に効く）
- 同期（P1）は「案件マスタ」タブ読みを削除。`vl_videos.business_category`／`is_private` は同期が付ける既定値（organic/NULL・false）のままにし、有効値はビューで決める
- RLS：設定テーブルは社内の読み取り可、書き込みは `app.user_role='admin'` のみ（0035 と同じ型）

## 画面
1. 一覧 `/library`：カード（サムネ・アカウント・投稿日・再生・保存率・区分・PR 印）。絞り込み（フリーワード＝投稿文・ハッシュタグ・アカウント・広告主・案件名、区分、PR 有無、期間、アカウント）、並び替え（新しい順・再生・保存率・ENG率）、50件ずつ。カードにカーソルを乗せると TikTok プレイヤー（音なし自動再生）、クリックで TikTok を新しいタブで開く。チェックした動画の URL をまとめてコピー
2. 詳細 `/library/v/{platform}/{video_id}`：数値（オーガニック／総計／広告を分けて表示、保存率・ENG率は再生 5,000 未満なら「参考値」）、投稿文、案件・区分、TikTok へのリンク、「構成」枠（`vl_video_analysis` に行が無ければ「近日公開」）
3. 管理（管理者のみ）`/library/admin/campaigns`：約55案件の一覧で種別・業種・目的・非公開を編集。`/library/admin/accounts`：アカウント登録・一覧
4. JSON API `/library/api/videos`（一覧と Aico が同じ検索を使う）

## サムネイル（P2a を含める）
- 同期の後段で、`thumbnail_s3_key` が無い動画だけ yt-dlp のメタデータからカバーを取得して S3 へ（実測 10/10 成功・平均3.2秒）。1回の上限（例：300本）を設けて ingest の所要を抑え、初回の約2,800本は run-task の単独実行で埋める
- 保存先は既存の `media_jobs` バケットの prefix（`video-library/thumbs/`）を第一候補。ingest タスクの書き込み権限と connect_web の読み取り権限が要る（terraform）
- 配信は `/library/thumb/{platform}/{video_id}` が S3 から読んで返す（ブラウザに S3 の URL を出さない・CSP は `img-src 'self'` で足りる）。無ければ灰色の枠

## 検査
- 単体：ビューの有効値（#PR×設定あり／なし、非公開、設定変更が即時に効く）、認証必須、管理画面は管理者のみ、CSP が `/library` だけ TikTok を許す、投稿文の HTML エスケープ、URL コピー対象の生成
- 実 DB：使い捨て PostgreSQL に 0035＋0036、本番データ 2,811本を入れて API を叩く
- 実画面：connect_web をローカル起動し、ブラウザで一覧・絞り込み・カーソル再生・詳細・管理画面の設定→即反映を確認
- デプロイは P1 とまとめて1回（migration 0035/0036 → image → terraform → run-task で同期とサムネ取得 → 本番で実画面確認）

## アドバイザー確認後の決定（2026-10-30）
- P1 のうちに 0035 から人の判断値の列（`vl_videos.business_category/is_private`、`vl_campaigns` の種別・業種・目的・非公開・レポート）を削除し、案件マスタのタブ読みも削除（同期が人の入力を上書きする構造をなくす）。有効値は P3 のビューだけで決める
- サムネ: MCP image は yt-dlp・ffmpeg を意図的に入れていない（Dockerfile で禁止を検査）ため、yt-dlp は使わない。TikTok の動画ページの埋め込み JSON（`__UNIVERSAL_DATA_FOR_REHYDRATION__` → `itemStruct.video.zoomCover["480"]`、無ければ `cover`）から取得。実測 9/10 成功・約1秒、480px で約36KB（原寸は 200KB〜800KB）。失敗は次回再試行
- 保存先: `media_jobs` バケットの `video-library/thumbs/` prefix（ライフサイクルの自動削除は `media-jobs/` prefix だけなので影響なし）。ingest タスクに書き込み、connect_web に読み取りの権限を terraform で追加
- `/library/thumb/*` は `Cache-Control: private, max-age=86400` と ETag を返す（全体の `no-store` の例外）
- ログイン後の戻り先は同一オリジンの `/library` 配下だけ許す（open redirect を作らない）
- カーソル再生の iframe は同時に1枚だけ作り、カーソルが離れたら破棄。実画面確認は connect_web の実ヘッダ付きで行う
- `#PR` ありで案件の無い動画（実データ 389本）の件数を管理画面に表示（P3b のアカウント設定で埋める前提）
- デプロイ手順に `CONNECT_ADMIN_EMAILS`・`CONNECT_SEARCH_ALLOWED_EMAILS` の本番明示の確認を入れる
- 初回のサムネ埋めは 1回300本の上限・1件ずつ間隔を置く・429 が出たら止めて次回へ
