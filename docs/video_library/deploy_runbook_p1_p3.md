# 本番反映手順（P1＋P3 まとめて1回）

対象: PR #742（P1）と PR #753（P3）。どちらも dev にマージ済み（dev HEAD `b7332b37`・2026-10-30）。
すべて人（小俣さん）の承認が必要な操作（repo の Human Gate ①②④）。各段の「確認する出力」が想定と違えばそこで止める。

## 0. 事前確認（読み取りのみ・Claude が実施可）
- dev の HEAD `b7332b37` に #742・#753 が入っていること、CI が緑であること（確認済み）
- サムネの保存先は常設の `teamagent-dev-raw-files`（実在・SSE-S3・公開ブロック・`video-library/` は期限削除の対象外を確認済み）。当初案の `media_jobs` バケットは本番に存在しないため使わない
- 本番 connect-web（taskdef rev 148・10/30 確認済み）: 閲覧は `CONNECT_SEARCH_ALLOWED_HD` で会社ドメイン全体に開放（営業全員が `/library` を見られる）。`CONNECT_ADMIN_EMAILS` は未設定のため管理者は既定の小俣さんだけ（決定どおり）。管理者を増やすときは taskdef に `CONNECT_ADMIN_EMAILS` を明示する
- 本番 RDS の `schema_migrations` の最新が 0034 であること

## 1. 本番 RDS に migration 0035・0036（小俣さんが実行）
```
cd ~/dev/teamagent && git fetch origin dev && git checkout --detach origin/dev
bash migrate_tunneled.sh dry      # 確認: [APPLY] は 0035_video_library.sql と 0036_video_library_settings.sql の2つだけ（他が出たら止める）
bash migrate_tunneled.sh apply    # 確認: 2つとも applied、エラーなし
```
- 0035/0036 は `teamagent_app` ロールへの GRANT と RLS ポリシーを作る。このロールは 0002 で作成済みで、本番の ingest・connect-web は毎日このロールで動いているので、存在は運用上確認できている。もし `role "teamagent_app" does not exist` 等で失敗しても、1ファイル1トランザクションなので何も残らずに止まる
- 0035/0036 は追加のみ（既存テーブルに触れない）。ingest を止める必要はない
- 戻すとき: forward-only のため、不要になったら DROP する migration を新しく足す

## 2. MCP image の署名リリース（小俣さんが実行・MFA 1回）
```
cd ~/dev/teamagent && bash scripts/aws/release_mcp.sh --dry-run          # MFA 不要の事前検証
bash scripts/aws/release_mcp.sh "APPROVED: video library P1+P3 (#742 #753)"
```
- 確認: 「5 段完走」と release repository の digest が表示される（この digest を次の段で使う）

## 3. タスク定義の差し替えと IAM（小俣さんが実行）
- **【小俣さんに確認したいこと】普段 mcp image の差し替えに使っているコマンド**。repo の `infra/deploy/` では直接登録系（`deploy_connectweb_unified.sh`・`register_ingest_td.sh`）が退役済みで、guard 経由（`terraform_runtime_guard.sh` の review-plan → plan → verify → apply、または `run_image_deployment_gate.sh`）になっている。どれを使っているかで、この段のコマンドを書き下す
- いずれの経路でも、image を段2の digest に更新し、`-var-file` 付きの plan を見てから apply
- plan の差分として想定するもの（これ以外が出たら止める）:
  - ingest と connect-web の task definition: image digest、env `VIDEO_LIBRARY_ACCOUNTS_SHEET_ID`・`VIDEO_LIBRARY_S3_BUCKET` の追加
  - ingest と connect-web の task role policy: `media_jobs` バケット `video-library/thumbs/*` への S3 権限の追加（ingest は Put/Get、connect-web は Get）
  - destroy 行が 0
- connect-web の service が新しい taskdef に入れ替わったことを確認
- apply 直後に `infra/deploy_log.md` の表の先頭へ追記する行（下書き）:
  `| 2026-MM-DD HH:MM | mcp(connect-web・ingest) | <tag> | <digest先頭8> | dev b7332b37（#742 #753） | 動画ライブラリ P1+P3（/library・同期・サムネ） | 小俣 |`

## 4. 初回の同期とサムネ（小俣さんの GO で実行）
```
# 同期の単独実行（dry-run で件数と warning を先に確認）
scripts/aws/run_ingest_task.sh で command を python -m teamagent.ingest.video_library_sync --dry-run に上書き
# 確認: unique_videos が約2,800、warnings に accounts_ledger_unavailable が無い（あれば台帳シートを ingest の Google 主体に共有）
# 本実行
同上で --dry-run を外す
# サムネの初回埋め（1回300本・約2,800本なので約10回。1回あたり5〜10分）
command を python -m teamagent.ingest.video_library_thumbs --limit 300 に上書きして繰り返す
```
- 以降は平日 18:00 の ingest の最後に、同期とサムネ（新しい動画だけ）が自動で走る

## 5. 本番での実機確認（Claude が実施・結果を報告。Chrome 拡張が繋がらなければ小俣さんに画面を見てもらう。社内プロキシ下のカーソル再生はここが唯一の確認機会）
- `https://connect.newstv.co.jp/library` に Google ログインして開く
- 一覧が約2,800本、サムネ、カーソル再生、詳細の「近日公開」、URL コピー
- 管理画面で案件の種別を1件保存し、一覧の区分がすぐ変わること
- 管理者以外のアカウントで管理画面が 403 になること

## 戻し方
- 画面だけ止める: connect-web を前の taskdef に戻す（`/library` が消えるだけで既存機能は無変更）
- 同期を止める: ingest を前の taskdef に戻す（同期・サムネの呼び出しが消える）
- DB のテーブルは残しても既存機能に影響しない
