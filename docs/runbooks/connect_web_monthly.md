# connect-web 月次更新 Runbook（ingest → export → build → publish）

作成: 2026-07-10（入れ込み v2）。対象: Drive/Slack の営業ナレッジを pgvector に取り込み、
No-AI 静的 HTML（/app）として 16 名へ配信する月次サイクルの全手順＋フォルダ再編の移行手順。

> **公開範囲の契約**: 静的 `/app` は per-user ACL 表示ではなく、
> `--shared-group` で指定した会社共有集合のミラー。`owner_email` だけ、
> `acl_emails` だけ、`acl_groups` 空の資料は配信しない。raw ACL や安定 source key は
> HTML へ表示しない。ユーザーごとの RLS 検索は `/search` 側の責務。

## 前提・役割分担

| 主体 | やること | 理由 |
|---|---|---|
| ユーザー | AWS 書込を伴うコマンド（run-task / s3 cp / update-service） | Claude の hook が本番 AWS 書込をブロックするため |
| Claude Code | ローカル工程（export_vault → サイドカーレビュー → build → QA diff） | 分類・品質整備はサブスク内で安く回す方針 |

定数: region=`ap-northeast-1` / cluster=`teamagent-dev` / bucket=`teamagent-dev-raw-files` /
service=`teamagent-dev-connect-web` / ingest td family=`teamagent-dev-ingest`。

設計原則（v2 で確定・変更不可）:
- 配信時 No-AI 維持。/app のコンテンツ更新は「S3 cp + force-new-deployment（約3分）」。
  CodeBuild bake（10-20分）は**コード変更時専用**（`infra/deploy/deploy_connectweb_unified.sh`）
- ingest の yaml は S3 オーバーライド（`INGEST_SOURCES_S3_URI`）。取得失敗・sha256 不一致は
  即 exit 1（同梱 yaml への silent fallback 禁止）
- stale は soft-delete（`metadata.stale`）＋量的ブレーキ。物理 DELETE はしない
- export は RLS を bypass できる admin DSN を使うため、全 SELECT で単一の会社共有
  group を `documents.acl_groups` に持つ行だけに明示絞り込む
- すべて fail-loud。「黙って劣化」を検知したら止まる

---

## 月次サイクル（毎月1回・所要 約1.5〜2h うち人手 約15分）

### ① ユーザー: ingest 実行（AWS 書込）

```bash
cd ~/dev/teamagent   # merge 済みの dev
bash scripts/aws/run_ingest_task.sh --mark-stale
```

- git 管理 `data/ingest_sources.yaml` を sha256 付きで S3 へ配置 → Fargate ingest を起動 →
  完了待ち → exitCode 検証 → ログ要約（documents/chunks・yaml sha 一致・skipped_folder）まで自動
- `--mark-stale`: run 中に観測されなかった gdrive documents へ `metadata.stale` を付与
  （Drive 削除・監視外移動の資料を /app から落とすため月次では常用）
- 共有ドライブ crawl も回す場合は `--sources all`（時間がかかる。crawl OFF 判断後は不要）
- 失敗（exit 1）したら下のトラブルシュートへ。**失敗のまま次工程に進まない**

### ② Claude Code: export → サイドカーレビュー → build → QA

Claude Code セッションで依頼する内容（そのまま貼れる指示例）:

> connect-web 月次更新の②をやって。SSM トンネル → export_vault の prune dry-run →
> export_vault --commit --prune →
> サイドカーレビュー → build_app_html.py → QA diff まで。Runbook は
> docs/runbooks/connect_web_monthly.md。

手順の実体:

1. **SSM ポートフォワード**（`migrate_tunneled.sh` と同方式・bastion 経由）:
   ```bash
   aws ssm start-session --target <bastion instance id> \
     --document-name AWS-StartPortForwardingSessionToRemoteHost \
     --parameters "host=<RDSエンドポイント>,portNumber=5432,localPortNumber=15432" &
   ```
2. **Vault エクスポート**（DB は SELECT のみ、company-shared ACL 必須、
   stale は既定で除外される）:
   ```bash
   python scripts/export_vault.py \
     --dsn postgresql://...@localhost:15432/... \
     --shared-group vectorinc.co.jp \
     --prune
   # 上の一覧で生成・削除予定と件数を確認してから確定
   python scripts/export_vault.py \
     --dsn postgresql://...@localhost:15432/... \
     --shared-group vectorinc.co.jp \
     --commit --prune
   # 移行検証等で stale も見たい時だけ --include-stale
   ```
   - `--shared-group` は単一 DNS ドメインのみ。省略時は
     `TEAMAGENT_SHARED_COMPANY_DOMAINS` が単一値の場合だけ使う。未設定、空、
     カンマ区切り、不正値は DB 接続前に exit 2。
   - この絞り込みは admin DSN で RLS が bypass されることを前提にした必須防御。
     ドライランでも per-user RLS 検証の代わりにはならない。
   - 月次の完全 export は `--prune` を常用する。これにより、stale 化・共有解除・名称変更で
     現行計画から外れた「この exporter の生成物」だけを削除する。初回は旧形式 note も
     exporter 固有の構造で認識する。人が編集した note、別フォルダ、非 Markdown は削除しない。
   - `--prune` は空または前回 manifest の半分未満に減る計画を拒否し、`--client` / `--limit`
     との併用も拒否する。通常の月次運用ではこのブレーキを解除しない。
   - **ACL 初回移行で半分未満になる場合だけ**、まず `--prune` なしの dry-run で現行の
     `planned` 件数を確認し、通常の `--prune` が表示する前回件数との差が ACL 共有解除分として
     妥当かレビューする。承認後、上の dry-run と commit の両方へ
     `--allow-prune-shrink` を追加する。これは 50% ブレーキだけを解除し、空 plan は拒否する。
     続く HTML build も前回比20%超減なら止まるため、QA後に限り `--allow-shrink` を使う。
3. **サイドカーレビュー**: `data/connect_web_filters/` の 5 点
   （exclude_stems.json / exclude_source_keys.json / dedup_drop_map.json /
   weird_rename_high.json / inter-var.b64）を
   新規取込資料に対して見直す。新たな重複・変名・ジャンクがあれば JSON を更新
   （タイトル stem・安定 source key・表示名のみ。本文/BANT/温度感を入れない）
4. **HTML 生成**（サイドカー欠落・Vault 不在・0件・前回比20%超減は exit 1）:
   ```bash
   python scripts/build_app_html.py
   # 意図的な縮小（大量 stale 掃除後など）のときだけ --allow-shrink
   ```
5. **QA diff**: 生成 HTML のフッタ統計（`更新: YYYY-MM-DD JST・取引先N・資料M`）と
   `<out>.stats.json` の前回比を確認。新規取込がフッタ数字に反映されているか、
   意図しない激減が無いかを確認して報告

### ③ ユーザー: publish（AWS 書込・約3分）

```bash
bash infra/deploy/publish_app_html.sh
```

- S3 配置 → force-new-deployment → services-stable → `/healthz` の
  `app_html_sha256`（ローカル sha 先頭12hexと一致）と `app_html_source=="s3"` を自動検証
- 成功表示のあと、ブラウザで https://connect.newstv.co.jp/app のフッタ更新日を目視確認して完了

---

## 移行手順（フォルダ再編・ルールブック適用）

### 前日（コード・インフラ準備。/app は現行のまま無傷）

1. 入れ込み v2 の PR を dev へ merge
2. `bash infra/deploy/bootstrap_apphtml_s3_iam.sh`（冪等・task role 2本へ S3 読取付与）
3. 新コードの image を bake: `bash infra/deploy/deploy_connectweb_unified.sh`
   （**最後の bake**。以後コンテンツ更新は publish script）
4. ingest td にも新 image を配布: `bash infra/deploy/register_ingest_td.sh --image-tag <手順3のタグ>`
   （タグは手順3の完了表示 `image tag:` 行に出る。そのままコピーする）
5. connect-web の td に `CONNECT_APP_HTML_S3_URI=s3://teamagent-dev-raw-files/codebuild/connect-web-app.html`
   の env が入っていることを確認（unified script が宣言的に注入する。手順3を実行済みなら入っている。
   あわせて No-AI フラグ `USE_QUERY_PLANNER=false` / `USE_COHERE_RERANK=false` も同 script が固定する）:
   ```bash
   aws ecs describe-task-definition --region ap-northeast-1 \
     --task-definition teamagent-dev-connect-web \
     --query "taskDefinition.containerDefinitions[0].environment[?name=='CONNECT_APP_HTML_S3_URI']"
   ```
6. 確認: `curl -s https://connect.newstv.co.jp/healthz` に `app_html_sha256` / `app_html_source`
   が出ること（この時点では source は s3 か baked のどちらでも良い。フィールドの存在が確認点）

### 当日（Drive 再編 → 取り込み → 配信）

1. Drive に ナレッジ/ ルートと `01_提案事例`〜`06_価格・契約`・`99_一次倉庫` を作成し、資料を移動
   （同一性キーは Drive fileId なので**移動/リネームで重複もリンク切れも起きない**）
2. `data/ingest_sources.yaml` の `REPLACE_WITH_KNOWLEDGE_01`〜`06` と
   `REPLACE_WITH_KNOWLEDGE_ROOT` に実 folder_id を貼付 → commit & push → dev へ
   （ROOT を貼るとルート検査が有効化: yaml に無い NN_ フォルダがあると exit 1 で silent 未取込を防ぐ）
3. crawl 抜きで取り込み: `bash scripts/aws/run_ingest_task.sh --sources gdrive`
   （初回は `--mark-stale` を付けない。移行直後の大量差分でブレーキを踏まないため）
4. 上の「月次サイクル②」を実行（export → build → QA）
5. `bash infra/deploy/publish_app_html.sh` で配信。/app のフッタと件数を確認

### 週+1（後始末・恒久運用への切替判断）

1. **カバレッジ監査**: 01〜06 経由で取り込まれた documents 件数と、crawl 経由でしか
   取れていない資料を比較（Claude Code に依頼: DB を SELECT して重複マップを出す）
2. **crawl OFF 判断**: ルールブック 6 フォルダで実用上カバーできていれば
   `data/ingest_sources.yaml` の `shared_drives_crawl.enabled: false` に変更して commit
3. **stale 掃除**: `bash scripts/aws/run_ingest_task.sh --mark-stale`
   （監視外になった旧配置の資料へ stale を付与 → 次回 build から /app に出なくなる）
   - stale 候補が 50% 超で止まったら件数を確認し、意図どおりなら `--allow-mass-stale` で再実行

---

## ロールバック

| 対象 | 手順 |
|---|---|
| /app コンテンツ | S3 の旧バージョンへ戻す（publish script 末尾に表示されるコマンド）→ `update-service --force-new-deployment`。versioning 無効なら手元の前回 HTML を `--src` で再 publish |
| connect-web コード | `aws ecs update-service --cluster teamagent-dev --service teamagent-dev-connect-web --task-definition <旧 revision ARN>`（unified script が控えを表示している） |
| ingest コード | 旧 revision を `run-task --task-definition` で明示指定（register script が控えを表示） |
| yaml | git revert → `run_ingest_task.sh` 再実行（S3 上の yaml は毎回上書き配置される） |
| stale 誤付与 | 対象を戻して ingest 再実行（観測された documents は stale が自動解除される） |

## トラブルシュート

| 症状 | 意味 / 対処 |
|---|---|
| healthz `app_html_source=baked` | タスクが S3 取得に失敗しイメージ同梱版を配信中（**古いコンテンツの可能性**）。bootstrap_apphtml_s3_iam.sh 済みか・S3 オブジェクト有無・td の URI を確認 → 再 publish |
| healthz `app_html_source=missing` | S3 も baked も無い異常。直近のコードデプロイ（bake）を確認 |
| healthz に `app_html_*` フィールドが無い | 旧コードの td が動いている。移行前日の手順 3-4 をやり直す |
| ingest が即 exit 1（yaml） | S3 取得失敗 or sha256 不一致（fallback 禁止仕様）。run_ingest_task.sh を使ったか・IAM を確認 |
| ingest exit 1（stale ブレーキ） | stale 候補が既存 gdrive documents の 50% 超。ログの件数を確認し、意図どおり（大規模再編直後など）なら `--allow-mass-stale` |
| ingest exit 1（ルート検査） | ナレッジ/ 直下に yaml 未登載の NN_ フォルダがある（ログに不足一覧）。yaml に追記するか、暫定なら `run_ingest_task.sh --root-check-warn-only` で降格（env のローカル export はタスクへ届かないので不可） |
| run_ingest_task.sh がネットワーク設定で exit 1 | EventBridge ルール `teamagent-dev-ingest-weekly` のターゲット不在。env `SUBNETS=... SECURITY_GROUPS=...` で明示指定 |
| build_app_html.py exit 1（20%減） | 取引先/資料/バイトが前回比20%超減。export 失敗や Vault 破損を疑う。意図的なら `--allow-shrink` |
| 99_一次倉庫 の資料が /app に出る | フォルダ名 regex 除外（`gdrive_exclude_folder_name_re`）の設定と skipped_folder ログを確認。旧取込分は `--mark-stale` で落ちる |

## 注意（既知の地雷）

- EventBridge 週次スケジュールは現在 DISABLED（手動 run_ingest_task.sh が正）。再開する場合、
  ターゲットは特定 td revision 固定なので register_ingest_td.sh 後にターゲット更新が必要
- app.html は機密（BANT/営業FB 埋込）につき **git コミット禁止**。S3 と手元にのみ置く
- terraform apply は -var-file 必須。本 Runbook の経路はすべて ECS 直更新で tf を触らない
- **/healthz の sha 露出（意図的トレードオフ）**: /healthz は無認証でインターネットから
  到達可能であり、`app_html_sha256`（先頭12hex・内容復元不可）と `app_html_source` を
  publish 検証のため意図的に露出している。第三者は更新タイミングの観測・流出コピーとの
  ハッシュ照合・劣化状態（baked/missing）の観測が可能だが、内容漏洩はないため許容する。
  加えて S3（codebuild/connect-web-app.html）への書込権限者は配信 HTML を差し替え可能
  （既存の CodeBuild 注入経路と同等の信頼境界であり、権限拡大ではない）
