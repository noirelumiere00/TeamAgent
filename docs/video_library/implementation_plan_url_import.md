# 「URL を貼って動画を取り込む」MVP 実装計画

改訂履歴：2026-11-02 v2 — 小俣さんの追加決定により、(c) Web→RunTask、全社員利用、日次上限、短縮URL、一般投稿の棚、案件の新規追加・後追い統合へ改訂。

> **v3 改訂（2026-11-02・Claude、レビュー反映）— 以下が本文より優先する**
> 1. **数値は別テーブル `vl_public_metrics`**（PK `(platform, video_id, captured_on)`、`captured_at`・`metric_observation` 付き）。`vl_metrics` の主キー・列は変えない。0037 は追加だけ。理由: migration 適用から新 image の便までの間に、稼働中の平日 18 時シート同期（`_upsert` の ON CONFLICT 3 列）が壊れるため。表示側（ビュー／`_METRICS_JOIN`）で取得元を選ぶ
> 2. **起動は connect-web の中の worker（(c) RunTask はやめる）**。受付を DB に commit → 同じプロセス内の 1 本のバックグラウンドスレッドが `FOR UPDATE SKIP LOCKED` で行を取り、queue が空になるまで処理。複数タスク（デプロイ中の 2 本）でも DB の取り合いで二重処理しない。再起動で残った processing 行は一定時間（例 10 分）経過で回収。IAM は既存のインライン `VideoLibraryThumbnailsBootstrap` に thumbs/ への PutObject を足すだけ（RunTask・PassRole・terraform 不要）。connect-web（512 CPU／3 GiB、1 タスク、公開 IP あり＝外向き通信可）で 100 ページ×約 430KB は軽い。日次上限・1 秒間隔・429 停止・連続失敗停止は worker 側で
> 3. **シート同期の書き込みは MVP では変えない**。URL 側は専用の保守的な書き込み（無ければ挿入／あれば `page_*`・公開数値・空のサムネだけ更新、案件・caption・is_pr などシート由来の値は触らない）。シートが後から来たらシートの `_upsert` がそのまま上書き＝シート優先が自然に成り立つ。既知の穴: シートの caption が NULL だと URL 由来の caption を消す（MVP では許容・記録）
> 4. **PR 分割**: PR1＝0037（受付・行・日次枠・公開数値・分析待ち・`vl_videos` の page 列・案件の作成者）＋URL 正規化／短縮展開＋1 本取り込み関数（thumbs のページ取得・停止条件を共通化）＋worker（スレッドと単独 CLI）＋A01〜A08 のテスト（フェイクは 1.4KB 空ページ・429・JSON 欠損・ID 不一致を再現）。画面なし。PR2＝取り込み画面（全員）・広告主／案件の選択と新規追加・「一般投稿」・「広告配信あり」・純オーガニック絞り込み・公開数値の表示。PR3＝IAM（PutObject）とスレッド起動の env、本番 10 本→100 本
> 5. 日次上限の初期値は 300（設定で変更可）


作業対象：`/Users/s-komata/dev/worktrees/teamagent-vl-import`、branch `feat/video-library-url-import`。確認したソースのHEADは `d4c91a5785805932bba6b50abf38f76067e526ae`。改訂時のローカルorigin/devは `75333d919dac8bc0528425b87cc41c2986304fff`、merge-baseは前者。fetch・rebaseは行っていない。実装着手時に最新devへ追随し、0037の番号競合と下記の差分を再確認する。

**本書は計画だけを変更する。アプリコード・SQL・Terraformの実装、外部取得、DB/AWSの変更は行わない。** 以下では確認した現状と、新設する設計案を分ける。旧版・要件追加書と衝突する箇所は、今回の11/02追加決定を優先する。

## 1. 今回の決定と範囲

| 項目 | 改訂後の仕様 |
| --- | --- |
| 起動 | **(c) connect-web→ecs:RunTask**。「開始」で検査済み受付をDBへ確定・commitしてからworker起動を要求。取得はingest taskdefのworker-only commandで実行。WebはTikTok通信・長い取得・完了待ちをしない |
| 利用者 | 社内ドメインの認証済み閲覧者全員が、受付・開始・数値更新・失敗再実行・起動の再開を利用可。依頼者メールはセッションから記録。管理者への昇格は不要 |
| 上限 | 1回は非空100行まで。全利用者・新規取込・数値再取得を合算するJST日次上限を設定で持つ。超過分も受付を残し、翌日以降の処理待ちを表示。運用上限は10本→100本の実測後に決定 |
| URL | TikTok完全動画URLに加え、vt.tiktok.com・vm.tiktok.com・www.tiktok.com/t/を対象にする。短縮展開はworkerだけで実行、手動リダイレクト最大5段、各段を検査、展開時の本文は読まない |
| 公開値 | total_plays/likes/comments/shares/saves。sheet/public_pageを分離。初回＋利用者による手動更新。Apify・自動的な定期数値更新は含めない |
| 純オーガニック | **canonical is_pr=false AND 最新正常ページ観測のpage_is_ad=false AND 同観測に補助の広告表記なし**。媒体ラベル確認は条件から外す。isAd=trueはorganic区分のまま「広告配信あり」 |
| 一般投稿 | 新規originはexternal。外部・案件未紐付け・PRなしはカードで「一般投稿」。事例棚・事例集計から除き、動画一覧・検索・トップの独立した一般投稿棚で見せる |
| PR投稿 | #PR付きは案件動画の候補として保存。受付時に既存の広告主・案件を任意選択でき、新しい組をその場で追加可能。追加は全員可・作成者記録。未選択なら「案件未設定」。後から管理者が紐付ける |
| 既存動画 | 同じ(platform,video_id)は同じ行。受付時点ですでに存在すれば「登録済み（統合済み）」で、canonical情報・案件は変えず数値と取得観測だけ更新。後からシートへ載った場合は同じ行に統合、シートの有効値優先・ownへ昇格、URL経路を保持 |
| 分析 | 社内の参考資料として保存・分析対象にできる。ただし**P3b（gracemode等の自動取得）とP6の実行は今回の範囲外**。URL1本部品はP3bから再利用可能にし、structure/v1分析承認待ちの登録は残す |

今回追加しないもの：他媒体、動画ファイルの恒久保存、P3bの列挙・定期取得、P6の試行実行・分析モデル、履歴グラフ、汎用ジョブ基盤。分析候補の選定・実行判断も後日のP6で行う。

## 2. ソースで確認した現状

本書の列名・テーブル・API・envの新設案は、現状の機能ではない。根拠リンクは今回のworktreeを指す。

| ID | 根拠 | 確認内容と計画への影響 |
| --- | --- | --- |
| R1 | [要件追加書](/Users/s-komata/Documents/projects/video-structure-search/requirements_addendum_url_import.md) §2〜7・末尾11/02実測 | A01〜A14、値の優先順位、AWSで約430KBの正常ページ／Macで約1.4KBの空ページ。実測は提供記録であり、今回の再取得結果ではない |
| C1 | [CLAUDE.md](/Users/s-komata/dev/worktrees/teamagent-vl-import/CLAUDE.md) §3〜6 | runtime→skills→adapters、Pydantic v2、strict型、冪等、構造化ログ、生入力非出力、app_roleとGUC、forward-only、guard経由の反映、CI依存の手動列挙 |
| C2 | [0035](/Users/s-komata/dev/worktrees/teamagent-vl-import/infra/migrations/0035_video_library.sql)・[0036](/Users/s-komata/dev/worktrees/teamagent-vl-import/infra/migrations/0036_video_library_settings.sql) | vl_videosのPKは媒体＋ID、vl_metricsのPKは媒体＋ID＋日。vl_campaignsは広告主＋案件の複合PKで広告主単独テーブル・作成者列なし。現在のキャンペーンINSERTはadminのみ。動画は共有SELECT/admin書込、settingsは別テーブル |
| C3 | [動画モデル・解析](/Users/s-komata/dev/worktrees/teamagent-vl-import/src/teamagent/ingest/video_library.py:66)・[同期](/Users/s-komata/dev/worktrees/teamagent-vl-import/src/teamagent/ingest/video_library_sync.py:50) | origin既定own、is_pr既定false。video_identityは短縮展開なし・入力port制限なし。シートのcaptured_onは同期時のJST日。build_batchがcaptionからタグとPRを作る |
| C4 | [repository](/Users/s-komata/dev/worktrees/teamagent-vl-import/src/teamagent/ingest/video_library_repository.py:16) | _upsertはNULLも含め非キー列を更新。saveからcampaign/account/video/metricsへ使用。現行VideoモデルにS3キー・first_ingested_atは入らないが、caption・案件・投稿情報・台帳値・指標の欠損上書き対策が必要 |
| C5 | [thumbs](/Users/s-komata/dev/worktrees/teamagent-vl-import/src/teamagent/ingest/video_library_thumbs.py:24)・[storage](/Users/s-komata/dev/worktrees/teamagent-vl-import/src/teamagent/adapters/video_library_storage.py) | Chrome UA、hydration JSON、zoomCover[480]→cover、CDN許可、JPEG/5MiB、20秒timeout、1秒間隔、429即停止、連続5失敗停止。停止制御は関数ローカル。サムネは既存raw_files/video-library/thumbs |
| C6 | [ingest入口](/Users/s-komata/dev/worktrees/teamagent-vl-import/scripts/run_ingest_fargate.py:128) | 現在は主ingest成功→sheet同期→thumbs。後段失敗で主ingest成功のexitを壊さない。URL workerはまだない。**この既存18時処理へURL queueを付け足す案は撤回する** |
| C7 | [library](/Users/s-komata/dev/worktrees/teamagent-vl-import/src/teamagent/connect_web/library.py:127) | _METRICS_JOINは日付最新1行。動画一覧・詳細・API・関連・集計・featuredが同じJOINを利用。search_campaignsは広告主/案件が両方ある行を集計。featured_videosは未割当も対象になり得るので一般投稿分離が必要 |
| C8 | [認証・接続](/Users/s-komata/dev/worktrees/teamagent-vl-import/src/teamagent/connect_web/library.py:890)・[フォーム](/Users/s-komata/dev/worktrees/teamagent-vl-import/src/teamagent/connect_web/library.py:1274) | identity/admin_identity、teamagent_app＋user GUC、非公開のWeb条件、Origin完全一致。既存フォームは8KB・12field上限。全員利用の取り込みはadmin_identity配下へ置かない |
| C9 | [分析表示](/Users/s-komata/dev/worktrees/teamagent-vl-import/src/teamagent/connect_web/library_analysis.py)・[広告補助表記](/Users/s-komata/dev/worktrees/teamagent-vl-import/src/teamagent/skills/ad_label_check/skill.py:52) | structure/v1表示契約・タグ検索あり。結果テーブルはqueueではない。find_disclosuresは補助抽出に利用可、Apifyを呼ぶAdLabelCheckSkill.runは流用しない |
| C10 | [connect IAM](/Users/s-komata/dev/worktrees/teamagent-vl-import/infra/terraform/connect_web.tf:257)・[ingest定義/IAM](/Users/s-komata/dev/worktrees/teamagent-vl-import/infra/terraform/ingest_schedule.tf:193)・[dispatcher IAM](/Users/s-komata/dev/worktrees/teamagent-vl-import/infra/terraform/ingest_schedule.tf:325) | connect task roleへのECS起動権限は新設。ingest taskdef、execution/taskの2ロール、subnet/SGは既存。dispatcherにfamily限定RunTask・cluster条件・2ロールPassRoleの実例あり |
| C11 | [既存dispatcher](/Users/s-komata/dev/worktrees/teamagent-vl-import/infra/terraform/lambda/ingest_dispatch/handler.py)・[ロック作法](/Users/s-komata/dev/worktrees/teamagent-vl-import/src/teamagent/ingest/media_cases.py:28) | dispatcherは通常ingest familyの先行taskを確認するがURL queue管理はない。専用DB接続のsession advisory lockの作法あり。URL workerのstartedByと世代を別管理し、通常ingestと同一視しない |
| C12 | [migration runner](/Users/s-komata/dev/worktrees/teamagent-vl-import/scripts/migrate.py:83)・[tunnel helper](/Users/s-komata/dev/worktrees/teamagent-vl-import/migrate_tunneled.sh) | checksum・forward-only・1ファイル1transaction、SQL内BEGIN/COMMIT禁止。SSM＋SecretからのDSN＋runner。helperは未適用の全migrationを実行 |
| C13 | [Terraform README](/Users/s-komata/dev/worktrees/teamagent-vl-import/infra/terraform/README.md:430)・[config便](/Users/s-komata/dev/worktrees/teamagent-vl-import/docs/runbooks/config_migration.md) | 署名image/receipt/full saved plan、--var-file、clean最新origin/dev。config便はimage不変、rule/dispatcher変更に制限。feature worktreeからapplyしない |
| C14 | [既存手動launcher](/Users/s-komata/dev/worktrees/teamagent-vl-import/scripts/aws/run_ingest_task.sh)・[CI](/Users/s-komata/dev/worktrees/teamagent-vl-import/.github/workflows/ci.yml:24)・[import契約](/Users/s-komata/dev/worktrees/teamagent-vl-import/pyproject.toml:273) | launcherに任意command overrideの公開引数はなく、旧ECS直ターゲット前提のnetwork解決が残る。今回Web起動へ置き換えるため、その修正を必須PRに含めない。CIは使い捨てPostgresと非BYPASSRLSロールを用意。adapter→ingestのimportも禁止 |

実測フィールドは、itemStruct.statsのplayCount/diggCount/commentCount/shareCount/collectCount、createTime、author.uniqueId、desc、isAd、adLabelVersion。statsV2の数値は文字列で、stats.collectCountも文字列の場合がある。表示名の実キー、adLabelVersionの型・意味、isAdとSpark Ads等の正確な対応は未確認。媒体ラベルの確認方法は今回の純オーガニック条件の前提ではない。

## 3. Web受付→RunTaskとqueueの処理

### 3.1 起動方法の比較と採用

| 方法 | 待ち時間・変更 | 判定 |
| --- | --- | --- |
| (a) 平日18時ingest後段 | 追加IAMは小さいが、週末は約72時間＋通常ingest時間待ち。全社員の随時利用に合わない | **不採用**。URL workerを後段へ追加しない |
| (b) 短間隔EventBridge | 間隔＋task起動時間待ち。新schedule/target/IAM・空queue起動対策が必要 | 今回の主起動方式には採用しない |
| (c) connect-web→RunTask | 開始操作後に起動要求。Fargate起動時間は必要。connect roleの限定IAM、DB起動排他、起動失敗からの再開が必要 | **11/02決定により採用** |

Webは「入力検査→開始でbatch/行をqueuedとしてDBへ保存・commit→ensure_workerで短いECS API処理→受付IDを返す」。外部ページ取得・短縮展開・画像取得・worker完了待ちはWebで行わない。BackgroundTasksやWeb常駐スレッドにqueueの実行を任せない。ECS API失敗でも受付transactionを巻き戻さず、queuedと理由、「再開」を残す。

RunTaskは既存ingest taskdefの固定revision ARNを使い、container名ingest、count=1、Fargate/awsvpc、既存subnet/ingest SG、公衆通信設定をTerraformで渡す。commandだけを `python -m teamagent.runtime.video_library_url_import` へ固定overrideする案。通常ingest・Google/Slack取得は起動しない。URL、任意command、role/network/ARNを利用者入力からoverrideできるAPIは作らない。起動世代IDだけをworkerへ渡し、仕事本体はDBから読む。

同じfamilyを使うため、**既存ingest_dispatchの先行task判定にも対応が必要**。現状はfamilyの全taskを対象にskip/滞留停止するので、URL workerを通常ingestと誤認し得る。既存dispatcherではDescribeTasks後にURL専用startedByのtaskを判定・停止対象から除き、通常ingest同士の二重起動防止と滞留処理は維持する。URL workerの探索は逆に専用startedByだけを見る。通常18時処理とURL取得はHTTPの共通ロックで協調させる。このdispatcher変更も③と反映便の対象に含める。

### 3.2 多重起動防止・起動不明・終了競合

`startedBy=vl-url-import`のような環境内固定マーカーで通常ingestと区別し、**DBの起動世代記録を併用する**。ListTasksの「今0件」だけでは起動許可にしない。ECS APIにはeventual consistencyがあり、起動直後のtaskがまだ見えない場合があるため。[AWS RunTask API](https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_RunTask.html)

1. **受付の確定**：開始transactionで依頼者・選択案件・行・idempotency_keyを確定し、queue更新番号を増やす。idempotency_keyは依頼者と操作に束縛し、同一キー・同一入力は同じbatch、別入力なら409。依頼者はformのメールを採用せず認証セッションから得る。
2. **短い起動排他**：DBのcontrol行をFOR UPDATE/CASで取得し、launching/running/stoppingと世代・task ARN・clientTokenを確認する。起動待ちが既にあれば新しい世代を作らない。既知task ARNとstartedBy付きtaskをECSで確認し、PROVISIONING/PENDING/RUNNING等の生存taskがあるならRunTaskしない。全pageを確認し、DescribeTasksの不完全応答や確認失敗では新起動せずqueuedを保持する。
3. **冪等起動**：起動可能なら世代UUID、clientToken、固定したRunTask引数の識別情報をlaunchingとしてcommitしてからRunTaskする。Web replica間の同時開始も同じ起動記録を見る。clientTokenはHTTPリクエストごとに変えず、同じ未確定起動の再送は同じ引数・同じtoken。HTTP200でもfailures/task ARNを検査する。workerが先にrunningを記録した場合、遅いWeb応答保存でlaunchingへ戻さない。
4. **失敗と不明を分離**：明確な起動失敗はqueuedのまま失敗コードを表示。timeout等で起動の成否が不明ならlaunch_unknownを保持し、ECSの再照会／同一tokenでの結果確認から再開する。確認せず新tokenを発行しない。tokenの有効期間・taskdef変更で再利用不能な場合は既存taskの停止確認を先に行う。全社員が押せる「再開」も同じensure_workerを使う。
5. **worker側の最終防御**：起動世代を照合し、専用session advisory lockでURL workerの実処理を1本にする。万一2taskが作られても後着はqueueや日次使用量を変更せず終了する。ロック接続断のまま処理を続けない。heartbeatだけが古いことを根拠に生存taskを二重起動しない。
6. **queueを処理**：FIFOでその時点で実行可能な行を順次処理し、開始元のbatchに限定しない。100本は1受付の上限で、worker1便の上限ではない。後続batchが入れば同じworkerが続ける。全体日次上限・共通取得停止・個別失敗を守る。
7. **終了直前の再確認**：起動調整と同じcontrol行の排他下で、実行可能queueと更新番号をもう一度見る。新着があれば継続、空ならstoppingを記録して終了する。確認前後の同時enqueueを試験する。stopping後の受付はqueuedのまま「worker終了待ち」とし、生存taskがいる間は新起動しない。画面は停止確認後に認証・Origin付きの再開POSTを試せる。画面を閉じても受付を消さず、次の開始／再開で拾う。GETを起動副作用にしない。

workerが処理できるqueueが空になれば終了する。日次上限により未来へ延期した行は「今は実行不能」であり、queue全体が未完でも終了してよい。翌日までworkerをsleepさせない。起動失敗・終了競合・プロセス停止で残った行は、DBで状態と理由を示し、社員の再開から回復する。再開APIは作業の起動要求だけで、他人のbatch内容や依頼者メールを返さない。

ListTasksはclusterを指定し、startedBy以外の検索フィルタ（family/desiredStatus等）を重ねない。返却taskのfamily・lastStatus等はDescribeTasksで照合する。`desiredStatus=PENDING`検索では起動待ちtaskを検出できないので使わない。[AWS ListTasks API](https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_ListTasks.html)

### 3.3 日次上限と翌日待ち

設定案は `VIDEO_LIBRARY_IMPORT_DAILY_LIMIT`（整数）。全員合計の値であり、利用者・Web replica・worker・batchごとの値ではない。機能ON時の未設定・負数・不正値はfail closed、0は取得停止。暫定の実測設定は人が10を明示し、100本試験時に100へ変更する。これは運用上限を10/100に決定した意味ではなく、運用値は実測後に決める。

上限の単位案は「ネットワーク取得を開始する代表行の取込／再取得試行1回＝1本枠」。新規・既存数値更新・失敗行の手動再実行を同じ枠で数え、失敗も消費する。入力時に除外できる不正・完全URL重複は0枠。短縮展開は外部通信なので、最初の通信前に1枠を確保し、展開後に重複と判明してもその枠は消費する。表示上も成功本数と枠消費本数を分ける。自動retryは同じ1枠内で追加最大2回、実際のHTTP回数は別に計測する。この数え方をA17と画面の説明に固定し、成功だけ数えて失敗で無制限取得できる設計にしない。

JST日付でDBの日次使用量行をtransaction内でロックし、残枠確認・加算・処理行へのquota日付記録を同時に確定する。HTTPより先に消費確定し、取得後にworkerが落ちても払い戻して二重消費／上限超過を起こさない。保存済みの同じ試行を復旧する場合は再加算しないが、外部通信をやり直す別試行には新しい枠を使う。日付はDB時刻を固定UTC+9へ変換して求め、Web端末の時計を使わない。

上限に達した時点で残行を`deferred`、`eligible_at=翌日0時JST以降`とし、受付は保存する。UIは「本日の全体上限に達したため翌日以降の処理待ち」と日付・残件数を出す。翌日も先行queueが多ければ再延期されるため、全件が翌日完了するとは表示しない。延期をfailedにしない。

**翌日の起動契機についての最小案**：主起動は(c)だけとし、翌日のいずれか社員の「開始／再開」で実行する。上限による待ちでは「YYYY-MM-DD 0:00以降に再開できます」と明示する。日付をまたいだだけでは終了済みworkerは起動しない。翌日自動再開が必要かは確認中で、自動化を選ぶ場合には起動通知のTerraform/IAM/受入条件を追加してから確定する。0時自動実行を実装済みのように案内しない。

既存のthumbs補完も同じTikTok停止・間隔制御を使う。日次の外部アクセス全体を抑えるため、独立したサムネ補完が投稿ページを新規取得する場合も同じ本数枠に含める案とし、既存便の枠消費を実測・表示する。URL取込中に同じitemからカバーを保存する処理は追加の本数枠を消費しない。シートの読み取り自体は公開ページ取込ではないので枠対象外。

### 3.4 費用と待ち時間

開始後の待ちはRunTask要求＋Fargate起動＋先行queue＋直列取得。18時便や通常ingestの完了を待たない。ただし共有TikTok取得ロック・日次上限・ブロック中は待つ。起動p50/p95と100本の所要時間は未確認、10本→100本で測る。1秒間隔、20秒HTTP timeoutは成功率・総時間の保証ではない。

Fargate Linuxはimage取得から終了まで秒課金・最低1分で、割当CPU/メモリ、S3、ログ、転送、公開IPv4等が費用要素となる。[AWS Fargate料金](https://aws.amazon.com/fargate/pricing/)（初版で2026-11-02確認）。(c)は空queue・上限待ちでは起動せず、実処理時だけ起動する。liveのCPU/メモリ、東京ARM単価、image pull時間、月次受付数は未確認なので金額を捏造しない。基盤増分＝実行割当×起動・処理時間＋S3/DB/ログ/通信等で見積もり、有料分析の費用とは分ける。Apify/LLM/動画分析は0回だが、基盤費を0円とはしない。

## 4. DB：0037での変更

新migration案は `infra/migrations/0037_video_library_url_import.sql`。0035/0036は編集しない。既存データ・列・テーブルを削除しないforward展開。runner所有のtransaction内で処理し、SQL内BEGIN/COMMITなし。実装前に最新devで番号競合を確認する。

### 4.1 受付・日次枠・起動状態・分析待ち

| テーブル案 | 主キー・主要列 | 目的 |
| --- | --- | --- |
| vl_url_import_batches | batch_id UUID PK、依頼者＋idempotency_keyのUNIQUE、requested_by、operation(import/refresh)、source(url_manual/p3b)、選択advertiser/campaign、status、各日時、parent_batch_id | 全員の受付。状態はdraft/queued/running/completed/partial_failed/deferred/paused/interrupted。検査結果draftはworker対象外、開始でqueuedを確定 |
| vl_url_import_rows | PK(batch_id,line_no)、input_url、canonical_url、platform/video_id（未展開はNULL）、duplicate_of、status、registration_result、attempt_count、attempts/observation JSONB、error_code/retryable、eligible_at、quota_date、各日時、親行参照 | 全非空入力行の結果。短縮URLはqueuedで保持しworker解決後にIDを設定。statusはpending/resolving/processing/succeeded/partial/failed/unprocessed/deferred/invalid/unsupported/duplicate/classification_pending等。成功動画へのnullable FKは、不正行・未解決行を拒まない別参照列で持つ |
| vl_public_fetch_control | platform PK、paused/pause_reason/last_request_at、launch_state、generation/client_token/task_arn、起動確認時刻・heartbeat・queue更新番号・再開要求者/日時 | TikTok共通停止とWeb起動調整。起動結果不明と失敗、処理中と終了待ちを区別する。RunTask引数の固定情報を束縛し、世代の応答競合をCASで防ぐ |
| vl_public_fetch_daily_usage | PK(platform,quota_on)、used_count、updated_at | 全利用者・新規・refresh・対象thumbsの同日枠を原子的に消費。quota_onはJST日、上限値そのものは設定で持つ |
| vl_video_analysis_requests | PK(platform,video_id,kind,version)、動画FK、status=awaiting_approval、要求日時/要求batch | structure/v1の実結果がなければ冪等登録。実分析結果があれば登録しない。同版の待機は1行でP6の自動実行なし |

rowのattemptsは最大3回の短いコード・時刻・HTTP状態を保持し、本文・Cookie・例外本文を入れない。再実行は元結果を消さず親batch/行を参照した子受付を作る。日次延期は同じ行のeligible_atで表し、翌日分を別行に複製して二重処理しない。実行可能queue向けの`(status,eligible_at,batch_id,line_no)`、batch受付順、動画別履歴の索引を追加する。履歴の自動TTL削除は今回入れず、保持日数は運用で決める。

### 4.2 動画と案件への追加

vl_videosへの追加案は、ingest_sources配列、page_is_ad（nullable bool）、page_ad_label_version（原値JSONB）、page_disclosure（観測caption・PR/補助表記・抽出版等）、page_aux_ad_present（nullable bool）、page_observed_at、page_fetch_status、page_last_attempted_at/page_last_success_at/page_fetch_error_code、canonical値との差異のreview表示フラグ。既存経路はsheet、新規はurl_manual、将来P3bはp3bとし、経路は集合へ追加する。originはown/externalのままで取得方法を入れない。

正常なページ観測でisAdが欠ければ今回のpage_is_adはNULL、投稿文不明なら補助表記状態はNULLにする。昔のfalseを最新観測として流用しない。ページ全体の失敗時は最終正常観測・指標を残し、取得試行日時・状態だけ更新する。isAd=trueはビューからhas_ad_deliveryを算出して表示し、二重保存のboolを別管理しない。adLabelVersionの値はラベル有無へ変換しない。

vl_campaignsへ`created_by TEXT NULL`・`created_at TIMESTAMPTZ NULL`・`created_via`相当を追加する。既存行の作成者・過去時刻は未確認としてNULLにし、推測で埋めない。Web新規追加では認証利用者・現在時刻・url_importを設定、シート新規追加ではsheetを記録する。既存行をシートが再UPSERTしても、作成者をNULL化しない。広告主の単独テーブルは新設せず、既存のadvertiser/campaignの組を使う。広告主名だけが新しくても、案件名と組にして追加する。

全社員には案件のINSERTだけを新たに許可し、既存名の変更・削除、案件設定（種別/業種/目的/非公開）の編集、既存動画の後日紐付けは管理者に限定する。新規追加はtrim・長さ上限・空欄禁止・HTML escape・パラメータSQLで検証する。NFKC/大小文字/空白等で比較した既存候補を注意表示するが、既存名を一括改名・自動統合しない。完全同名の同時作成は複合PK＋ON CONFLICT DO NOTHINGで1組にし、最初の作成者を保持する。

### 4.3 vl_metricsの取得元・主キー・スナップショット

`metric_source TEXT NOT NULL DEFAULT 'sheet'`（sheet/public_page）、`captured_at TIMESTAMPTZ NULL`、`metric_observation JSONB`を追加。既存行はsheet、実測時刻は未確認なのでcaptured_at=NULL。新しいシート同期時刻は保存しても「元指標の観測時刻」とは呼ばない。公開ページは実取得時刻とそのJST日付を保持する。

新PKは **(platform,video_id,captured_on,metric_source)**。同日シートと公開ページを別行にし、公開値はtotal_*の5列だけへ保存する。organic_*やad_*へ複写・合算しない。同日同sourceは最新正常スナップショット、翌日は別行。完全失敗では指標行を作らない。

1回の5指標をまとめて扱う。新日／初回のpartialはNULLと理由を含むその回の行を保存。同日に既存の有効指標が今回欠けた場合は旧行全体を保持し、新たなpartial観測はrow履歴へ残す。別時刻の値を項目ごとにCOALESCEして混ぜない。保持した数値の時刻と最新試行時刻を別表示する。

**「追加のみ」の注意**：旧3列PKを残すと同日2sourceを保存できず、4列UNIQUE追加だけでは解決しない。既存データを消さない拡張として旧PK制約を外し、新PKを作る設計である。これはDDLがADDだけという意味の「追加のみ」ではない。この制約置換は実装前レビューに明示し、厳密に制約DROPも禁止なら、別テーブル案への要件変更が必要。黙って旧PKのまま実装しない。

### 4.4 シート優先・人の紐付け・既存動画の更新範囲

現在の_upsert呼び出しを調べ、VideoLibraryRepository.saveのvideo/account/metricsとcampaign作成者保存をsource別の共通DB部品へ切り替える。URLから既存SyncBatchを作り、NULLや既定falseを含め丸ごとsaveする実装はしない。adapterからingestモデルはimportせず、依存を逆転させない。[C3,C4,C14]

| 状況 | 保存規則 |
| --- | --- |
| URL新規・PRなし | external、未割当、一般投稿。URLから投稿情報・PR根拠・指標・サムネを保存 |
| URL新規・#PRあり・案件選択あり | 選択した実在の広告主/案件を採用、externalのまま案件動画として保存。ownへ推定昇格しない |
| URL新規・#PRあり・案件未選択 | external、案件未設定。動画一覧・管理者の未設定一覧へ。架空の案件行を作らない |
| 投稿文不明の新規 | rowに分類保留として観測を保存し、is_pr=falseの既定で通常一覧へ公開しない。案件適用・分析待機も保留 |
| URL受付時／保存競合時に既存 | canonical caption/タグ/is_pr/アカウント/投稿日/origin/案件・人の設定を変更しない。「登録済み（統合済み）」として公開数値と今回観測・取得状態を更新。案件指定があっても既存動画を移動せず、その理由と管理者紐付け導線を表示。既存サムネ・分析を維持 |
| URL→後日シート | 同じPK行へ統合。有効なシート投稿情報を優先、ownへ昇格、経路sheet＋url_manualを残す。シートの広告内訳・指標はsheet行へ。人が確定した案件紐付けは保護し、競合は管理者へ表示 |
| シート→URL | 既存情報は維持、public_page指標を追加。ownや運営名をexternal/NULLへ戻さない |
| シートの欠損 | NULL・空配列・モデル既定falseで有効なcaption・hashtags・is_pr・案件・運営を消さない。caption/タグ/PRは有効な同一入力の組として更新し、0や明示falseは欠損扱いしない |

案件は投稿情報より人の判断を優先する。Webで選択／後日紐付けした案件を識別するため、`vl_videos.campaign_assignment_source`（sheet/user、既存はsheet）と`campaign_assigned_by/at`を追加する案。シートに有効案件があり現在未設定／URLの自動補完値なら採用するが、人が選択した案件が違う場合は勝手に付け替えない。この優先順位は要件§2.4の「人の確定値＞シート＞URL補完」を維持する。シートの有効値優先は人の確定値を破壊する許可とは解釈しない。

取り込み時の選択案件は、workerでPRが確認できた**新規**動画だけに適用する。PRなしの動画は一般投稿として保存し「選択案件はPR付き新規投稿に適用」と行別に説明する。新規登録競合でも既存行へ案件を上書きしない。origin、初回日時、S3キー、既存分析、fetch_enabled、settingsは保護する。NULL防止を全列COALESCEだけで済ませず、指標はスナップショット単位、captionとタグは組単位で扱う。

### 4.5 ビュー・指標選択・表示分類

vl_videos_effectiveはsecurity_invokerを維持し、既存のis_prによるorganic自動区分を変更しない。既存列の順序・型・名称を明示列指定で保ち、新列は末尾へ追加する。v.*のまま再作成してcomputed列の位置をずらす、DROP VIEW CASCADEで回避する、といった変更はしない。

「純オーガニックのみ」の式は **NOT canonical.is_pr AND page_is_ad IS FALSE AND page_aux_ad_present IS FALSE**。補助表記は最新正常ページの投稿文をNFKC等で検査し、#ad・#提供・【PR】等があればtrue、投稿文不明ならNULL。#PR自体はcanonical is_prと根拠表示の担当と分ける。媒体labelがunknownでもこの3条件を満たせば含む。isAd不明／補助表記不明は条件を満たさない。canonicalとの不一致フラグや現在の取得不可を、指定外の追加除外条件にはしない。取得失敗で最終正常観測を使う場合は、その日時と現在状態を明示する。

表示上は次の判定を共通化する。事業区分・origin・案件紐付けは別軸として残す。

| 条件 | カード・棚・集計 |
| --- | --- |
| 広告主・案件が両方ある | 案件動画。事例棚・事例集計へ。既存の#PRなしシート案件も従来どおり扱う |
| #PRあり・案件なし | **「案件未設定」**を優先表示する。一般投稿棚とは分け、動画一覧・未設定一覧へ。案件別集計には未参加 |
| origin=external・案件なし・#PRなし | **「一般投稿」**。動画一覧・検索・トップ独立棚へ。既存CASE/PICK UP/NEW/事例カテゴリ棚と事例集計には入れない |
| own・案件なし | 既存の扱いを維持し、勝手に一般投稿へ分類しない |

search_campaigns、案件詳細・集計・代表動画、featured_videos、archive_home、カード、一覧/APIで同じ判定を使う。トップの一般投稿棚は条件を限定した別クエリ／結果で描画する。既存の事例系TRENDINGへ一般投稿を混ぜない。空の新規案件はDBにあっても、動画0件の成功事例カードとして並べない。

_METRICS_JOINはsourceを先に選び、その中で日付・時刻の最新1行を読む。既定はsheet経路あり→sheet、URLのみ→public_page。公開ページ比較を選ぶと全行でpublic_pageに固定し、値がない動画をsheetで埋めない。画面・API・率・sort・案件集計・関連・一般投稿棚の参照元をそろえる。後追いsheet統合で既定表示がsheetへ変わる場合は取得元表示で分かるようにし、公開ページ値も別sourceとして残す。[C7]

### 4.6 全員利用に合わせたRLS

新テーブルはENABLE＋FORCE RLS、PUBLIC権限剥奪、app_role=teamagent_app。一般利用者を一括admin GUCに昇格させない。Web接続は実利用者のメール・会社groups・user_roleを注入、workerだけがsystem/adminとして永続データを書き込む。

| 対象 | 一般の認証済み社員 | 管理者・worker |
| --- | --- | --- |
| batch/row | 自分の受付の作成・検査・開始・閲覧・再実行。requested_by=app.user_emailをRLSで検査。rowは親batch所有者と照合。他人のURL入力・メール・観測履歴は返さない | 管理者は全履歴閲覧。workerだけが処理結果・試行・quotaを更新 |
| vl_campaigns | 参照可能な既存案件の選択、新規INSERT。作成者は自分、既存名変更/削除不可。非公開案件は選択候補からWebで除く | sheet新規INSERT可、既存作成者保持。設定編集は従来のadminのみ |
| vl_videos/metrics/analysis | 既存の共有読み取りを維持。新APIでもWebのis_private検査を適用。一般利用者に直接UPDATE権限を広げない | worker/admin書き込み、非公開の扱いは既存方針 |
| 起動調整・全体使用量 | 個人情報を含まない残枠・起動状態だけを取得。再開は自分の権限内でglobal workerを起動要求する限定操作 | worker/adminが状態と使用量を更新。共通ブロック解除・上限変更は管理者 |

global起動調整のための限定DB操作は、固定引数・許可状態遷移だけを扱う関数／DB APIにし、他人のbatch・上限値・成功状態を書き換える汎用UPDATE口を作らない。SECURITY DEFINERを使う場合はschema修飾・固定search_path・PUBLIC EXECUTE剥奪・最小owner権限・呼出元GUC確認を0037の実DB試験対象にする。日次枠の予約はworkerだけ。再開ボタンはRunTask再要求を全員へ開くが、429等の共通pauseを解除したり日次上限を増やす権限ではない。

既存0035/0036の共有SELECTはis_privateをDB単独で隠す仕組みではない。一般投稿・案件候補・API・thumb・分析待機表示の全経路で非公開条件を守る。UUIDを知っていることは認可にしない。[C2,C8]

## 5. URL1本の取得部品と短縮URL

### 5.1 配置・呼び出し元

| 部品案 | 責務と既存呼び出し先への変更 |
| --- | --- |
| adapters/tiktok_public_page.py（新規） | 厳格URL検査・短縮展開・HTTP・hydration抽出・ID検証・カバー。既存thumbsのUA/parser/CDN検査を共通化。adapterからingest/skills/runtimeをimportしない |
| adapters/video_library_import_store.py（新規） | 受付・日次枠・起動調整・RLS・source別保存・分析待機・排他。既存sheet repositoryの対象upsertも利用 |
| adapters/video_library_worker_launcher.py（新規） | boto3 ECS List/Describe/RunTaskだけを担当。既存dispatcherのrequest構成と冪等の作法を参考に、Webから固定workerを起動 |
| skills/video_library_import/schema.py・service.py（新規） | Pydantic v2 I/O、100行検査、案件適用、1本取込、PR/補助表記、枠・retry・保存判断。SDK直呼びなし |
| runtime/video_library_url_import.py（新規） | env検証・依存生成・queue drain・世代・request_id・終了コード・finally close。Webからは固定commandとして呼ぶ |
| ingest/video_library_repository.py・video_library.py・video_library_sync.py | 優先順位・4列指標キー・captured_at・作成者保護・sheet後追い統合。既存_upsertの全callerと関連fixtureを追随 |
| ingest/video_library_thumbs.py | 共通HTTP/停止/間隔・枠へ接続。既存CLIと保存検査は維持 |
| connect_web/library.py・library_assets.py等 | 全員の取り込み導線・受付/再開・案件追加、一般投稿棚、source JOIN、表示と管理者紐付け。app.pyの配線・戻り先許可も新ルートで検証 |
| scripts/run_ingest_fargate.py | URL queue呼び出しは追加しない。主ingest→sheet→thumbsの既存順序・dry-run・exit互換を回帰検査する |
| infra/terraform/lambda/ingest_dispatch/handler.py | 同family内のURL専用startedByを通常ingestの先行/滞留判定から除く。通常ingestの保護は維持し、tests/infra/test_ingest_dispatch.pyで確認 |

新規外部依存は予定しない。httpx・boto3・Pydantic・psycopg・標準ライブラリで構成する。Aicoツールを追加しないためfactory/OpenClaw露出変更は不要。P3b入口は実装せず、source=p3bの入力を将来同じ1本部品へ渡せる形を保つ。

### 5.2 入力・短縮展開

Webでは1行1URLのtrim・空行除外・100行上限・構文検査だけを行う。101行は重複除去前に全体拒否、先頭100行だけを黙って採用しない。完全URLはHTTPSへ正規化、query/fragment除去、固定www.tiktok.comへ再構成。userinfo、異常port、偽ホスト、内部IP、制御文字、異常エンコード、photo/live/profile/search、IG/YTは行別不正／対象外。スキームなし・httpは構文検査後にhttpsへ変更し、HTTPで外部アクセスしない。

短縮URLはWebで「workerで展開予定」と表示し、video_id・登録済み・重複の最終判定はworkerまで保留する。最初から同一短縮文字列の重複は分かるが、異なる短縮URLが同じ動画かは検査画面で断定しない。

workerの展開手順は次のとおり。

1. 許可ホストを**完全一致**でvt.tiktok.com、vm.tiktok.com、www.tiktok.com、tiktok.comに固定する。短縮パスはvt/vmの所定パスまたはwww.tiktok.com/t/、最終は/@handle/video/数字ID。汎用の*.tiktok.com許可やCDNリストを短縮展開へ流用しない。
2. 入力をHTTPSへ正規化後、**HTTPS・認証情報なし・portなし/443**だけで通信する。各Locationはurljoinで相対URLを解決してから、同じhost/scheme/port・パス・制御文字検査を通す。途中でhttpへdowngrade・外部ホスト・内部IPへ向けばその先には通信しない。
3. httpxの自動redirectはOFF。`stream(GET, ...)`でヘッダだけを受け、本文を読み出さずcloseする方式を第一案とする（HEADでもよいが、HEAD非対応時の余分なfallbackを必須にしない）。20秒timeout、Location必須、3xxだけを手で1段追う。最大5リダイレクト、循環・上限超過・Location不正は行別失敗。展開中の429/403/challengeは共通停止へ伝える。
4. 完全動画URLに着いた時点で正規video_idを確定し、そのURLの通常ページ取得へ進む。上限5段のLocation先が完全URLなら受け付け、さらに6段目が必要なら拒否する。短縮先200だが完全動画パスにならない場合は、本文のJS/meta refreshを実行して展開しない。挙動は実測で確認する。
5. 同じbatchの解決済みIDと照合し、短縮＋完全URL／別短縮の同じIDは代表1件だけページ取得する。代表の選択・参照をDBへ保存し、他行にも結果・動画リンクを残す。既存DB照合と動画保存競合もここで再確認する。

本文を読まないのは**短縮展開の段階**であり、最終動画ページはメタデータ解析のため上限付きで本文を読む。全通信をHTTPTransport fakeで検査する。短縮サービスの実redirect先ホストは今回未取得のため未確認。上記許可外へ出る場合は止め、実測根拠なしに許可リストを広げない。

### 5.3 1本取り込みの順序

業務関数案は `import_one_url(ImportOneInput, ...) -> ImportOneResult`。認証主体・batch/row・source・選択案件・request_idを持ち、任意AWS引数や人の設定変更を受け付けない。

1. 正規化・日次枠確保・必要なら短縮展開・重複確定。
2. 最終ページ取得。完全Chrome UA、redirect OFF、timeout20秒、HTML上限案4MiB。HTTP200だけで成功にしない。
3. hydration JSON→default scope→webapp.video-detail→itemInfo.itemStructを検査。JSON/item欠損・ID不一致は動画/指標を保存しない。約1.4KBの空ページはpage_data_missing、明確なchallengeはblockedとして停止。ID一致なしの1件補完はしない。
4. 同じ応答から数値・投稿情報・isAd・caption根拠・coverを得る。stats優先、なければ同じitemのstatsV2を選ぶ。指標ごとのコンテナ混合はしない。playCount→plays、diggCount→likes、commentCount→comments、shareCount→shares、collectCount→saves。数値/数字文字列は非負有限整数にし、0は0、欠損・bool・負値・小数・NaN・桁あふれはNULL＋理由。statsだけ欠損なら投稿はpartial。createTimeのUNIX秒とauthor.uniqueId、descの存在・型を検証する。
5. captionはNFKC/casefoldしたタグ完全一致で#PR。#PRADAは除外。find_disclosuresで#PR以外の補助表記も記録し、isAdはbool/NULLで保持する。空のdescとdesc不明を区別する。事業区分はisAdで変更しない。
6. 1動画ごとに保存・行結果・分析待機をtransactionで確定。初回だけcanonical投稿情報と任意案件を保存し、既存は第4.4節どおり数値・観測だけ。1件の失敗で他の成功を戻さない。DB失敗を成功表示しない。
7. 新規動画のサムネは同じitemのzoomCover[480]→coverから取得する。既存の許可CDN・HTTPS・443・JPEG magic・最大5MiB・stream・redirect OFFを維持。既存動画は数値更新に限定しサムネを置き換えない。サムネ失敗は投稿情報/指標を消さずpartialとして残す。保存成功直後のクラッシュ復旧で動画を重複作成しない。

映像・音声のダウンロード、モデル実行、P3b列挙は呼ばない。構成分析待機と実結果を分け、実結果のない動画を分析済み件数・タグ検索へ入れない。

### 5.4 間隔・停止・retry・ログ

URL worker実行排他とは別に、TikTokのHTTP区間をthumbsと共有する専用session advisory lock/共通controlで直列化する。通常ingestが同じfamilyでも、URL workerを別workerとして起動できる一方、TikTokアクセスは重ならないようにする。ロックのため長い行transactionをHTTP待ち中に開かず、専用接続・finally unlockの既存作法を利用する。[C5,C11]

ページ・短縮各段の開始間隔は最低1秒。retryは一時通信失敗・5xxに追加最大2回（2秒→4秒案）、同じ回の回数上限は短縮展開を含めて管理する。429は即停止・同便内retryなし。403・明確なchallengeも即停止。連続5失敗で停止し、正常取得で連続数をリセットする。画像429も共通停止を伝える。thumbsの次入口・別batchが直後に続けないようpausedを永続化する。

paused中の行は未処理として残す。全員の「再開」は起動失敗/中断からのensure_workerであり、共通ブロック解除や上限回避にはしない。共通pause解除は管理者の明示操作。workerクラッシュ時は既知task停止確認後、新世代workerが処理中行をinterruptedへ整合し、保存済みの成功・枠消費・分析待機を重複させず復旧する。

ログはrequest_id、世代、batch_id/row_no、event、状態別件数、token_usage=0、API費、latency_ms、ページ/redirect/画像のHTTP回数・bytes。URL・caption・社員メール・例外本文をログへ出さない。基盤費は別実測見積とし、モデル呼び出し0を全体無料と混同しない。

## 6. 画面・API

### 6.1 全社員の取り込み

新しい取り込みルート案は `/library/imports`。既存identity＋社内閲覧ドメイン判定を使い、全endpointで再認証・権限検査。管理者専用 `/library/admin/...` へ取り込み機能を置かない。社内ドメイン外・未認証は実行不可。社員向け新規ルートをloginの戻り先許可でも検証する。

| 操作 | 振る舞い |
| --- | --- |
| 入力・任意案件 | 1行1URL、完全/短縮100行まで。既存広告主→案件の選択と「新しい広告主・案件を追加」。依頼者はログイン情報から記録 |
| 案件追加 | 社員全員がPOSTでvl_campaignsに追加。既存・表記揺れ候補を注意表示、同じ組なら既存を選べる。空欄・長すぎる値は拒否。既存を編集できるフォームとは分ける |
| 検査 | サーバー側の構文・件数・完全URL重複だけを確認し、外部通信0回。短縮は「展開予定」。100件対象、既存（確定できる範囲）、重複、不正/対象外、案件の適用条件を示す |
| 開始 | POST /importsで検査済み内容をDBへ確定→RunTask。確認対象・依頼者・選択案件をサーバー側で再検証。idempotency_keyで二重クリックを吸収。draft運用なら開始時にqueuedへ遷移 |
| 進捗 | batch結果と状態APIは本人＋管理者。短いGETポーリング、画面を閉じても復元。開始時に完了を待たない。新規/登録済み（統合済み）/重複/一部/保留/失敗/未処理/翌日待ちを区別 |
| 再開 | RunTask失敗・起動不明・worker終了待ちでは、**管理者以外も使える「再開」**。固定ensure_workerへPOSTし、実行中なら追加起動せず既存workerが拾うと表示。queueなし・上限前倒しでは起動しない |
| 失敗の再実行 | 本人/管理者が対象行を子batchへ。成功分は含めない。不正は修正が必要。共通pauseは解除しない。日次枠を消費する説明をする |
| 数値更新 | 動画一覧の最大100件選択・詳細から全社員がrefresh受付。通常GETやカード表示では再取得しない。非公開動画をID直指定して更新できないよう閲覧権限を先に検査 |

100URLは既存8KBフォーム上限を超え得るので、この取り込み用だけUTF-8本文上限案1MiB・1URL2,048文字・100行・許可field・重複field拒否を設ける。全管理画面の上限を一括で緩めない。Origin完全一致・Content-Type・stream段階のサイズ上限、CSRF、escape、nonce/CSPは維持する。formからrequested_byやworker roleを指定させない。

案件が取得時点で非公開になった場合、一般利用者の選択を再検査して取り込み状態に理由を残す。既存動画が既に別案件にあれば勝手に移動しない。一般利用者向け案件候補で、非公開案件の名前・存在を漏らすエラーを返さない。

### 6.2 動画一覧・詳細・トップ

カードに「一般投稿」「案件未設定」「広告配信あり」「#PR」の適切なラベルを表示する。isAd=trueでもorganic区分を維持。「一般投稿」棚は社内参考資料の棚として分離し、表示・検索・分析待機の対象にできる。事例の成果集計には足さない。後からシート/人の操作で案件が確定したら、同じ動画リンクのまま該当棚へ移り、二重に件数を増やさない。

一覧・検索には一般投稿/案件/案件未設定、表示指標source、PR根拠・広告状態、純オーガニックの条件を追加する。本人のbatchでの絞り込みも権限検査し、他人の操作履歴は公開しない。トップの一般投稿棚は一覧の一般投稿条件へ遷移する。

詳細には「公開ページの数値」、5指標、指標取得日時・取得元、最新試行と欠損理由、広告観測日時・#PRと補助表記の根拠、最終正常値と現在取得状態、運営名（台帳優先・なければ運営未確認）、分析承認待ちを出す。媒体ラベルは取れなければ未確認と出せるが、純オーガニック抽出の条件にはしない。

保存率＝保存÷再生、ENG率＝(いいね＋保存＋コメント＋シェア)÷再生。再生0・必須項目欠損は算出不可、5,000未満は参考値。derived_rates、SQL sort、API、各棚で同じsource/snapshotを使う。埋め込み・元投稿リンク・認証付きS3サムネを再利用。取得不可でも過去値を残し、今再生できると保証する表示にしない。

後日の紐付けは既存管理画面に「#PR付き・案件未設定」動画一覧と案件選択POSTを追加する。管理者だけが実行でき、対象動画/案件の存在確認・Origin保護・assignment provenanceを保存。案件設定そのものの編集と紐付けを分け、既存のis_private等を消さない。

## 7. テスト・受け入れ条件

すべて実装時に行う計画。外部通信はhttpx.MockTransport、ECS/S3はfake、時計・sleepを注入し、実サービスには通信しない。実測形状の合成JSON/HTMLをfixtureにし、社員メール・実投稿本文を持ち込まない。実DBは既存の隔離schema＋rollback＋teamagent_app/GUC方式を拡張する。

### 7.1 A01〜A13の改訂対応

| ID | 主なテスト先（新設案を含む） | 合格条件 |
| --- | --- | --- |
| A01 | tests/skills/video_library_import/test_input.py、tests/connect_web/test_library_import.py | 0/1/100/101行・空行・100件8KB超、101行は重複除去前に拒否。URL入力検査ではTikTok通信0回 |
| A02 | input、tests/ingest/test_video_library_url_import.py | 完全query違い・短縮/完全・別短縮が同一ID。全行に結果、ID確定後ページ取得は代表1回。既存は「登録済み（統合済み）」で数値更新 |
| A03 | input、tests/adapters/test_tiktok_public_page.py | 偽host/userinfo/port/内部IP/異常encoding、profile/photo/LIVE/他媒体拒否。短縮許可の各段host/scheme/portを検査し禁止先には通信しない |
| A04 | HTTP adapter、worker | stats/文字列/statsV2、0/NULL/負値/bool/NaN等。**HTTP200の約1.4KB空ページ、JSON欠損・破損、ID不一致**を再現し成功扱いしない。個別指標欠損はpartial |
| A05 | tests/skills/video_library_import/test_classification.py | #PR/#pr/全角/#PRADA/#ad/【PR】、desc空/不明、isAd true/false/欠損。純オーガニックは改訂後の3条件だけ。label unknownでも3条件を満たせば含む |
| A06 | tests/ingest/test_video_library_url_import_postgres.py、既存test_video_library_postgres.py | URL→sheet／sheet→URL、NULL/false/配列、同時書込、own昇格、経路保持、シート値優先・人の紐付け保護、S3/分析/初回日時/運営/内訳不変 |
| A07 | 同Postgres、既存test_video_library_settings_postgres.py・test_library_catalog_postgres.py | 同日source2行、同日更新/翌日、同日部分欠損は旧snapshot丸ごと保持、失敗で正常値保持。JST境界・captured_at・source JOIN/sort/API一致 |
| A08 | worker、tests/adapters/test_video_library_worker_launcher.py、既存thumbs/ingest script試験 | 1件失敗後続成功、ページ/短縮/画像429、403、連続5停止、1秒・20秒・retry上限。Web二重送信・workerクラッシュ・画面再表示・再開で受付を失わない |
| A09 | Web TestClient＋実DB RLS | **社員全員**が受付/開始/更新/再開/案件追加可。社外/未認証不可。他人batch内容・非公開動画不可。管理者専用の紐付け/設定/共通pause解除は一般社員不可。DB ownerでRLSを代用しない |
| A10 | library/catalog/JS/browser fixture試験 | 一般投稿/案件未設定から詳細到達、5指標/source/日時/根拠/運営/取得状態、棚分離・sort・API一致、escape、社員情報/S3キー非露出 |
| A11 | 既存test_video_library.py、catalog Postgres | 再生0/4,999/5,000、保存/ENGの欠損、Python率とSQL sort、publicとsheet/adを合算しない |
| A12 | worker＋実Postgres | structure/v1待機unique、既存結果なら待機なし。import/refresh/retryでApify・モデル・動画取得・P3b列挙fakeの呼出0回。分析済み表示なし |
| A13 | 既存tests/connect_web/test_library_catalog.py・test_library_catalog_postgres.py | 合成structure/v1結果の区間・tags・standards表示、同タグ検索、結果なし/不正の見本との区別。P6は実行しない |

### 7.2 追加受け入れ条件

A14は既存どおり100本の承認済み本番実測であり、fake試験だけで取得率を保証しない。追加分はA15〜A21として管理する。

| ID | 検査 | 合格条件 |
| --- | --- | --- |
| A15 短縮URL | vt/vm/www.tiktok.com/t/、相対Location、5段/6段、loop、Locationなし、外部host/http降格/異常port、200非動画 | 5段以内だけ手動追跡、各段検査、展開レスポンス本文をreadしない、禁止先通信0。ID確定後に既存/重複を更新。展開中429も全体停止 |
| A16 全員の権限 | 異なる社員2人＋管理者＋社外＋未ログイン | 2社員とも同じ取込/refresh/resume/案件INSERT機能を利用。依頼者偽装不可、他人履歴不可、全体再開で他人内容が漏れない。既存管理機能はadmin限定 |
| A17 全体日次上限 | 2社員・2Web・複数batch、通常/refresh/手動retry/短縮/対象thumbs、JST日付変更 | 原子的に上限を超えない。失敗も枠消費、入力不正/事前重複は0、短縮後重複は説明した枠消費。超過を受付保存・deferred/eligible_at表示。上限前の再開で起動せず翌日対象化。設定不正fail closed |
| A18 RunTask排他 | 同時開始、ECS未反映、launching中再開、timeout後再送、HTTP200 failures、worker2task、終了前後enqueue | 既存生存worker時は新RunTaskなし。同token/同引数で再照会、新世代の無断増殖なし。実処理1本。全行は処理/明示待ちのどちらかで消えない。画面を閉じた後も再開可能 |
| A19 新規案件 | 新広告主/新案件、既存組、表記揺れ、同時同名追加、非公開候補、XSS | 全員INSERT可、作成者は実セッション、1組unique、既存作成者を消さない。注意表示後に選択/追加、既存rename/delete不可。新規PRへ任意適用、未選択PRは案件未設定・後日admin紐付け |
| A20 シート後追い統合 | URL先行→sheet、sheet先行→URL、両者同時、既存へ別案件指定 | 動画1行、URL経路保持、sheet有効値/own反映・人の確定案件保護、source別指標共存。既存のURL取込はcanonical/案件不変で「登録済み（統合済み）」 |
| A21 一般投稿 | external未割当PRなし、PR未割当、own未割当、案件あり、後日紐付け | 一般投稿カード/動画検索/専用トップ棚に出る。案件棚・事例集計から除外。PR未設定は専用ラベル、架空案件なし。統合/紐付け後は同じIDで正しい棚・件数へ移動 |

Postgres fixtureは0035→0036→0037へ追随し、既存列・値・件数・PK・RLS・ビュー旧列順を検証。キャンペーン一般INSERTとadmin以外の設定更新拒否、起動限定DB操作の権限を実DBで試す。移行runnerのchecksum/既適用skip/失敗rollbackも確認する。既存fixtureの0035のみ前提を残したままrepositoryだけ更新しない。

対象pytest→実DB→mypy --strict src→ruff check/format→import-linter→Terraform fmt/契約試験の順で必要範囲を確認。CIのDB skipを合格と扱わない。新importがあればCIの手動依存リストも確認し、失敗全文を保持する。起動APIのバリデーション、通常18時ingestの既存順序/dry-run/exit、thumbs回帰を含める。

手元は最新AGENTS.mdに従い、変更ファイルに対応する**個別のテストファイルだけ**を実行する。開始前に `ps -Ao comm=,args= | awk '$1 ~ /[Pp]ython/ && /pytest/' | wc -l` で本数を確認し、2本以上なら30秒おきに再確認、1本以下で開始。並列は-n 4まで、-n autoなし。ディレクトリ丸ごと・全件はCIに任せる。test_terraform_runtime_guard.py等の重い試験は、③で関連guardを変更した範囲に限る。計画書だけを改訂する今回はpytestを実行しない。

## 8. PR分割と本番反映

### 8.1 3本のPR・マージ順

①→②→③で契約を固定し、必要な全PRとmigration適用前には本番機能を有効にしない。マージは本番実行の承認ではない。

| PR | 範囲 | 完了条件 |
| --- | --- | --- |
| ① DB・取得・worker | 0037、作成者/RLS、枠/queue/起動状態、source保存、短縮展開、共通HTTP停止、worker drain/終了確認、sheet統合、thumbs、分析待機、fixture | A01〜A08/A11〜A13の部品試験、A15/A17/A20、実DB・保存順逆転・クラッシュ・3条件の純オーガニックを満たす。PK制約置換と旧writer非互換をレビュー。通常ingest後段へURLを追加しない |
| ② 全員のWebとRunTask接続 | /library/imports、本人結果/再開/refresh、案件選択/INSERT、ECS adapter/排他連携、一般投稿/案件未設定棚、source JOIN、admin後日紐付け、JS/認証 | A09/A10/A16/A18/A19/A21。ECS fakeで同時開始・起動不明・終了競合を再現。WebからTikTok通信0、admin以外も再開可、他人履歴/非公開保護。fixtureブラウザ確認 |
| ③ Terraform/IAM・運用 | connect task roleのRunTask/List/Describe/2ロールPassRole、固定taskdef/network env、機能OFF gate・日次上限設定、既存ingest dispatcherのstartedBy除外、guard manifest/契約、deploy runbook | family/cluster/roleの限定と通常ingest非干渉を契約試験。dispatcher差分を許す限定的なguard反映経路もレビュー。意図しないdestroy/権限拡大0、-var-file付きplan手順。image→dispatcher→IAM/envで起動成立。apply・本番実行はしない |

既存手動launcherを汎用化する変更や、短間隔schedule、P3b/P6の実装はこの3PRへ混ぜない。元launcherの不整合は本書に残すが、Web→固定RunTask経路の完成に不要な修正は別件とする。

### 8.2 migrationと展開前の確認

1. 人の本番反映GOを得た後、cleanな最新origin/devのデプロイ用checkout・署名commitを固定。live ingest/connect taskdef/digest、ingest execution/task role、cluster/network/SG、DB migration、会社ドメインと管理者設定を読み取り確認する。古い資料のARN/CPU/ポートを現在値と断定しない。
2. 既存`apply_0035_0036`と同じSSM＋Secret DSN＋checksum runner方式で0037を適用する。その名前のhelper本体・番号限定の詳細は今回未確認なので元helperを照合する。確認済みの代替経路は `bash migrate_tunneled.sh dry` → 人が出力レビュー → `bash migrate_tunneled.sh apply`。未適用全件が対象のため、0037だけがAPPLY、0035/0036までchecksum一致SKIPであることを確認し、他の未適用・checksum不一致があれば止める。--rerunで通さない。[C12]
3. PK置換前〜新ingest image反映完了まで、通常シートwriter・手動実行・対象workerを停止する窓を確保。旧writerのON CONFLICT(媒体,ID,日付)は新4列PKで動かないため、0035/0036の無停止手順を流用しない。必要な通常schedule停止/復旧は既存guardの認可経路で人が行う。
4. 人が0037を適用後、履歴checksum、既存データ件数/値、4列PK/source、作成者の既存NULL、新RLS、ビュー旧列順・security_invokerを確認。transaction失敗ならrollbackを確認して停止。URL取込はまだOFF、queued実行もなし。

### 8.3 env・IAM・Terraform便

機能設定案は `VIDEO_LIBRARY_URL_IMPORT_ENABLED`（既定OFF）、`VIDEO_LIBRARY_IMPORT_DAILY_LIMIT`、connect側の固定`VIDEO_LIBRARY_IMPORT_CLUSTER_ARN/TASKDEF_ARN/SUBNETS/SECURITY_GROUPS`相当。名前は実装時に固定する。taskdef ARNはingest taskdefのTerraform出力へ追随させ、familyの別imageを勝手に選ばせない。workerは既存DATABASE_URL・VIDEO_LIBRARY_S3_BUCKET・STRUCTLOG_FORMATを使い、新Secret不要。ユーザーが日次上限envをリクエストで変更することはできない。

connect-webの**task role**に追加する権限は次だけ。execution roleへWeb APIの起動権限を置かない。

| 権限 | 制限 |
| --- | --- |
| ecs:RunTask | ingest task-definition familyのrevisionだけ。ecs:clusterを既存clusterへArnEquals。count=1、worker固定command/networkはサーバー設定。別family/clusterを起動する入力はなし |
| iam:PassRole | ingestのexecution roleとtask roleの**2 ARNだけ**。iam:PassedToService=ecs-tasks.amazonaws.com。ロール名wildcardなし |
| ecs:ListTasks | API上必要なResource=*はcluster条件で限定。startedByでURL workerだけを検索し、全page確認 |
| ecs:DescribeTasks | 当該clusterのtask ARN範囲のみ。既知task状態・起動待ちを確認。StopTask権限は今回追加しない |

ingestのS3 Put/GetとconnectのS3 Getは既存thumb prefixのまま。新bucket、EventBridge主起動schedule、Lambda dispatcher、connectからの任意role/network変更は足さない。IAM追加は既存ingest dispatcherのstatement構造を参考にする。[C10]

反映は次の3便を基本にする。**Claude/Codexはapply・build/push・本番RunTask・migrationを行わず、以下は人の承認後に実行する手順**。

| 便 | 内容 |
| --- | --- |
| image便 | ③までのコードを署名releaseの新immutable tag/digestへ。fresh receipt/consumer manifestでingest/connect更新、機能OFF。新writerが0037対応であることを確認して通常シート同期を復旧可能にする |
| dispatcher便 | URL専用startedByを通常ingest判定から外す既存Lambdaコードだけを、review済みの専用migrationで反映。既存config便はdispatcher差分を許さないため、③で対象Lambda・旧新code hash・変更addressを固定した最小のguard契約拡張を準備し、通過試験を行う。既存のままで使える実行コマンドは未確認。AWSへの直更新で迂回せず、反映経路が完成するまでWeb機能はOFF |
| IAM/env便 | 同じdigestでconnect roleの限定IAMとtaskdef/envを反映。日次limitを実測用10で明示してON。config migrationのallowed_resource_changesにconnect policy・ingest/connect taskdef/serviceの必要address、allowed_env_changesに今回envを限定列挙。image/rule/dispatcherをconfig便で変更しない |

全plan系は完全なtfvarsを **--var-file（Terraformへの-var-file）必須**で渡す。enable_ingest_schedule/enable_connect_web/enable_morning_digest等の既存スイッチ・ruleの意図した状態を保つ。生terraform apply、-target、直接taskdef登録、古いreceipt/plan再利用はしない。guard対象変更は最新devにマージし、許可されないenv/IAM差分は契約を先にレビューする。

config便の形式は既存runbookに合わせる。ID/パス/live/期限が確定するまでは下記はテンプレートであり実行不可。image便にはimage releaseのreceiptを使い、config便のreceiptで代用しない。

```bash
bash infra/deploy/terraform_runtime_guard.sh preflight \
  --migration REVIEWED_URL_IMPORT_CONFIG_ID --out /secure/url-import-preflight.json
bash infra/deploy/terraform_runtime_guard.sh review-plan \
  --var-file /secure/teamagent.tfvars --out /secure/url-import-reviewed.json \
  --runtime-migration REVIEWED_URL_IMPORT_CONFIG_ID \
  --preflight-receipt /secure/url-import-preflight.json
# reviewed_planとenabledの承認commitをmerge後、新しいfinal planを作成
bash infra/deploy/terraform_runtime_guard.sh plan \
  --var-file /secure/teamagent.tfvars --out /secure/url-import.tfplan \
  --runtime-migration REVIEWED_URL_IMPORT_CONFIG_ID \
  --preflight-receipt /secure/url-import-preflight.json
bash infra/deploy/terraform_runtime_guard.sh verify --plan /secure/url-import.tfplan
# 人が承認後に実行。var-fileはsaved planに束縛済み
bash infra/deploy/terraform_runtime_guard.sh apply \
  --plan /secure/url-import.tfplan --out /secure/url-import-apply.json
```

planで意図しないdestroy・別family/cluster/roleへの権限・他サービス差分が0であることを確認。connectの新revision/service、ingest revision、通常dispatcherが指すrevision、Webの固定worker ARNが整合することも確認する。infra/deploy_log.mdへdigest↔commit↔branch↔env/IAM↔上限値↔実施者・確認結果を残す。

### 8.4 本番10本→100本と停止

1. 一般社員2人・管理者・社外/未ログインで、本番導線と受付/refresh/resume/案件追加・他人履歴・非公開・Origin・管理機能の権限を確認。WebへRunTask権限が入っても任意command/role/URLを起動できないことを確認する。
2. 人の取得GO後、全体limit=10で短縮/完全、既存/新規、#PRと案件有/無、isAd状態を含む10本を受付。「開始」からDB確定→RunTask→workerを確認。通常18時便を待たない。実測環境はAWSで、Macの空ページを本番成功率に置き換えない。
3. 起動時間・行結果・使用枠・HTTP回数・ID統合・指標source/日時・広告/PR・一般投稿棚/案件未設定・サムネ・作成者・分析待機・分析等0回を検証。2人同時開始でworkerが増えないこと、起動失敗のqueuedと一般社員再開も確認。ブロック時は停止して100本へ進まない。
4. 問題修正後、人が承認した設定便でlimit=100へ。100本のA14を実測し、母数・日時・taskdef/image・新規/既存・短縮内訳・成功/一部/失敗/未処理/延期・時間・起動数・HTTP/bytes・費用見積を報告する。既存thumbs等が同日枠を使った場合はその分も分けて報告する。成功率が低ければ運用可否は人が判断。
5. その後の運用上限を人が決め設定に保存。日付境界・翌日待ち/再開、シート後追い統合・人の案件保護、トップ棚と事例集計の不混入を確認。P3b/gracemode・P6の試行/分析は始めない。

停止は共通pauseまたは機能OFFで受付/起動/取得を止め、最終正常値・queued・履歴を残す。稼働workerは行境界で設定/DB停止状態を確認して終了する。env OFFが既に走っているtaskのenvを変えると誤認しない。PK変更後に旧ingest imageへ単純rollbackは不可で、新PK対応の修正版imageへforward fixする。戻しもfresh receipt＋新full planを人が実行し、DBをDROP/downで戻さない。

## 9. リスク・未確認事項

| 項目 | 対応・未確認点 |
| --- | --- |
| isAd | #PRなしでもtrueという実測を採用し、決定どおり「広告配信あり」と別表示。ただし実配信履歴/Spark Ads等の正確な意味は未確認。媒体ラベルを条件にしないので旧版の「label未確認のため純オーガニック0件」という前提は撤回 |
| TikTok/短縮URL | AWS3本成功は100本・将来の保証ではない。redirectの実host、HEAD/GETの挙動、JSON変更/200空/429/地域制限は未確認。各段allowlistと共通停止で扱い、CAPTCHA回避・Apify代替をしない |
| 起動・終了競合 | ListTasksだけではeventual consistency・Web同時起動・終了競合を防げない。DB世代/同token/worker排他/終了前再確認を試験。起動不明を新規起動で埋めずqueued/再開で回復。同familyを使う既存dispatcherの除外対応と、そのguard反映経路の追加が必要 |
| 翌日待ちの起動 | 終了済みworkerは日付変更だけでは起動しない。最小案は社員の開始/再開。0時自動再開の要否は確認中で、自動起動を保証する記載はしない |
| 日次上限/費用 | 実測用10→100と運用上限は別。最終値・基盤予算・東京単価/live割当は未確認。短縮/失敗/refresh/既存thumbsを含む消費の説明と実測が必要 |
| PK/展開 | データ非破壊でも旧PK制約置換が必要。「追加のみ」の厳密解釈はレビュー事項。旧writer停止窓・新imageの順序とビュー列順を実DBで検証 |
| 全員の案件追加 | 表記揺れと重複、非公開候補漏洩に注意。作成者記録・類似候補注意・PK冪等・INSERTだけ公開。既存名/設定/後日紐付けは管理者のまま |
| migration/guard | apply_0035_0036の実helper詳細とlive情報は未確認。実装前のdev追随、0037番号、guardが受理する今回env/IAM差分を確認。未確認コマンドを存在するものとして実行しない |
| 履歴・権利 | 履歴保持日数・削除依頼窓口・適用規約/許諾の評価は未確認。必要公開情報だけ、社員メールは本人/管理者履歴、社内利用のみ。社外公開・コメント本文収集なし |
| P3b/P6 | 再利用境界・分析待機だけを残す。gracemode等の列挙・自動取得、実動画の分析用取得、試行候補選定/分析実行は後日別工程 |

## 10. 本改訂で行った確認

旧計画、要件の11/02実測、今回の追加決定、CLAUDE.md、現HEADのschema/保存/取得/画面/IAM/guard/テストを確認して改訂した。AWS公式API資料でstartedBy検索・起動状態とeventual consistencyを確認した。新規成果物はこの計画書だけで、コードは変更していない。

未実施：アプリ/DBテスト、DB/AWSのlive照会、TikTok/短縮URLの実取得、migration、Terraform plan/apply、build/push/deploy。起動の実時間・運用日次上限・費用は10本→100本の実測後に確定する。

## 追記（11/02 夜・Claude）: 0037 適用時に片付けること
- 11/02 18:35〜18:50 JST に、アカウントからの直近投稿 556 本を一回きりの補完で `vl_videos`／`vl_metrics` に直接入れた（`aico-inputs/vl_backfill_latest.sh`・Apify）。0037 適用で `ingest_sources` は既定 `{sheet}` になり、公開数値は `vl_metrics` 側にある
- **0037 適用直後に**: 対象（`first_ingested_at` が 2026-11-02 09:35〜09:50 UTC の行）を `ingest_sources='{p3b}'` に更新し、その 11/02 の `total_*` 行を `vl_public_metrics` に写す（`vl_metrics` の行は残してよい）。PR2 の取得元選択はこの前提で書く
- gracemode の 15 アカウントは `vl_accounts` に手で登録（operator_label=gracemode）。シート同期の管理外
