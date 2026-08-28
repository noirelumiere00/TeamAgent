# media worker のサイズ収束と SQS 可視性タイムアウトの判定 Runbook

作成: 2026-08-27。対象: 費用・性能棚卸しの C1（media worker が 16vCPU/32GB でドリフト）と
C2（SQS 可視性タイムアウト 1800 秒）。**この Runbook 自体は apply しない**。実際の反映は
`infra/terraform/README.md` の single guarded full saved-plan workflow に従う。

区分: **【実測】**=本 Runbook 作成時に AWS read-only で自分で取った値 /
**【推定】**=実測から導いた見込み。

---

## 0. 現在地【実測 2026-08-27】

### 0-1. task definition

| 項目 | live | repo 既定 | 出典 |
|---|---|---|---|
| `teamagent-dev-tiktok-acquire` | **rev25 / cpu 16384 / mem 32768 / ephemeral 40GiB** | `tiktok_task_cpu="2048"` / `tiktok_task_memory="4096"` / `tiktok_ephemeral_gib=40` | `describe-task-definition` / `infra/terraform/tiktok_acquire.tf:60-80` |
| `teamagent-dev-mcp` | rev92 / cpu 1024 / mem 4096 | `fargate_mcp_cpu=1024` / `fargate_mcp_memory=4096` | 同上 / `variables_fargate.tf:32-42` |
| `teamagent-dev-x-buzz-worker` | rev1 / cpu 512 / mem 1024 | — | 同上 |

mcp はドリフトなし。**ドリフトは media worker だけ**。

### 0-2. ドリフトがどこで入ったか

| rev | cpu / mem | registeredAt | registeredBy |
|---|---|---|---|
| 1〜6 | 2048 / 4096 | 2026-06-26 〜 07-06 | — |
| **7** | **4096 / 8192** | 2026-07-22 18:26 | `root` |
| **8** | **16384 / 32768** | 2026-07-22 19:00 | `root` |
| 9〜25 | 16384 / 32768 据置 | 〜 2026-08-05 17:23 | `root` → `user/AIIAdev` |

34 分のあいだに 2 段階で上がっている＝**その場の消火作業**。加えて
`tiktok_task_memory` には `condition = var.tiktok_task_memory == "4096"` の
validation があるので、**terraform では 32768 を作れない**。よってドリフトの出所は
手動 `register-task-definition` で確定（tfvars の書き換えではない）。

⇒ **修正対象はコードではなく tfvars / state 側**、という前提は正しい。ただし後述のとおり
「tfvars の既定値 2048/4096 へ戻す」は**実測と矛盾する**ので、そのままやってはいけない。

### 0-3. 実際にどれだけ使っているか

Container Insights（`ECS/ContainerInsights` / `ClusterName=teamagent-dev-tiktok` /
2026-08-01〜08-27 / 1 時間バケット）:

| メトリクス | 最大 | その時の平均 | 割当 | 使用率 |
|---|---|---|---|---|
| `CpuUtilized` | **2547.7 units（≒2.49 vCPU）** | 1001.9 | 16384 | **15.5%** |
| `MemoryUtilized` | **465 MB** | 216 MB | 32768 MB | **1.4%** |
| `EphemeralStorageUtilized` | **2.4 GB** | 1.1 GB | 40 GiB | 6% |

CPU 最大は 2026-08-05 16:00 JST のバケット。ジョブ台帳から同時刻の
**同時実行タスク数は 1** と算出済み ⇒ この 2547.7 は**単一タスクのピーク**。

⚠️ Container Insights は 1 分平均のサンプル。瞬間ピークはこれより高いことがある。

### 0-4. ジョブ台帳（`teamagent-dev-tiktok-acquire-jobs`・156 行）

所要 = `updated_at - dispatched_at`（Fargate のタスク起動・image pull を含む壁時計）:

| operation | 件数(done) | p50 | p90 | max |
|---|---|---|---|---|
| `acquire` | 23 | 70s | 79s | 84s |
| `frame` | 22 | 70s | 79s | 83s |
| `proxy` | 42 | 65s | 76s | 90s |
| `thumbnail` | 29 | 66s | 75s | 79s |
| `tiktok_acquire` | 13 | 83s | 93s | 96s |

- 最大同時実行 **6**（2026-08-07 11:31）
- `created_at` の範囲は **2026-08-03 18:54 〜 08-17 18:09**＝**08-17 以降 media ジョブ 0 件**
- 🔴 **`slides` / `pdf` / `proposal_pptx` の実行が 1 件も無い**

最後の点が C1 の肝。お土産資料の FMT レンダは
`skills/omiyage_report/fmt/build.py:86` → `MediaJobClient().slides_to_pptx()` で
**この media worker の chromium** を通る。つまり **chromium で 1920×1080 を
枚数ぶん焼く一番重いオペだけが未計測**のまま、上の CPU/メモリ実測が取られている。

---

## 1. C1: media worker のサイズをどう収束させるか

### 1-1. 結論（先に）

- **「tfvars の既定 2048/4096 へ収束」はそのままでは不可**。実測ピーク 2547 units は
  2048 を **24% 超える**。戻すと観測済みのピークで頭打ちになる。
- 妥当な着地は **cpu 4096 / mem 8192**（＝ rev7 の形）。live 比で CPU 1/4・メモリ 1/4 に
  下げつつ、実測ピークに 1.6 倍の余裕が残る。
- ただし Fargate の組合せ制約で **cpu 4096 はメモリ 8192 以上が必須**。今の
  `tiktok_task_memory` validation（`== "4096"`）のままでは宣言できない ⇒ **repo 側の
  validation を 8192 に移す変更が要る**（この validation を参照している契約・テストは
  リポジトリ内に存在しないことを確認済み＝影響は terraform 内で閉じる）。
- 🔴 **費用の話ではない**。実測のタスク稼働は 1 か月で約 3.5 task-hour（140 ジョブ×約 90 秒）。
  16vCPU/32GB → 4vCPU/8GB の削減額は**【推定】月 $2 未満**。C1 の価値は金額ではなく
  **「live を terraform が再現できない」状態の解消**（state drift の恒久化を止める）。

### 1-2. 縮小の前に必ず測る（slides オペの実 CPU）

**未計測のまま縮小してはいけない。** 手順:

1. 実行前スナップショット
   ```bash
   export AWS_PROFILE=aiiadev AWS_REGION=ap-northeast-1
   aws dynamodb scan --table-name teamagent-dev-tiktok-acquire-jobs \
     --select COUNT --output json
   ```
2. 本番の omiyage 便でお土産資料を 1 本作らせる（`omiyage_report_submit`）。
   FMT レンダが走ると `slides` オペのジョブ行が台帳に立つ。
3. ジョブ行から `dispatched_at` / `updated_at` / `dispatched_task_arn` を取る
   ```bash
   aws dynamodb scan --table-name teamagent-dev-tiktok-acquire-jobs \
     --output json | python3 -c '...'   # operation.kind == "slides" を抽出
   ```
4. **その時間帯だけ** 1 分粒度で Container Insights を引く（1 時間バケットだと
   60〜120 秒のジョブのピークが平均に薄まって消える）
   ```bash
   aws cloudwatch get-metric-statistics --namespace ECS/ContainerInsights \
     --metric-name CpuUtilized --dimensions Name=ClusterName,Value=teamagent-dev-tiktok \
     --start-time <dispatched_at-60s> --end-time <updated_at+60s> \
     --period 60 --statistics Maximum Average --output json
   ```
   `MemoryUtilized` / `EphemeralStorageUtilized` も同じ窓で取る。
5. 判定線
   - `CpuUtilized` max ≤ **3200 units** かつ `MemoryUtilized` max ≤ **6000 MB**
     → 4096/8192 で安全。1-3 へ進む
   - 超えたら **縮小しない**。実測値を記録して、必要サイズを改めて決める

⚠️ 測定中に他のジョブが走ると cluster 集計に混ざる。台帳から
**その窓の同時実行が 1 であること**を必ず確認してから読む（0-3 と同じやり方）。

### 1-3. tfvars 収束手順（apply は guarded workflow）

tfvars は git 管理外。実運用の正本は
`/Users/s-komata/dev/worktrees/teamagent-build/infra/terraform/terraform.tfvars` の 1 本だけ。

1. repo 側の validation を実測に合わせて動かす（PR が要る）
   ```hcl
   # infra/terraform/tiktok_acquire.tf
   variable "tiktok_task_memory" {
     default = "8192"
     validation {
       condition     = var.tiktok_task_memory == "8192"
       error_message = "tiktok_task_memory must remain 8192 MiB (cpu 4096 の Fargate 下限)。"
     }
   }
   ```
   `tiktok_task_cpu` の既定も `"4096"` へ。**契約テストを同じ PR で足す**
   （`tests/scripts/test_terraform_runtime_contracts.py` に「cpu/mem の組が Fargate の
   有効な組合せであること」を凍結する）。
2. tfvars に明示（既定に頼らない＝次に既定が動いても live が動かない）
   ```hcl
   tiktok_task_cpu    = "4096"
   tiktok_task_memory = "8192"
   ```
3. `terraform_runtime_guard.sh plan --var-file <tfvars>` で saved plan を作る。
   **plan の差分が task definition 1 本の置き換えだけ**であることを目視で確定させる
   （`-var-file` なしの apply は禁止＝本番全断の前歴）。
4. guarded apply → `describe-task-definition` で新 rev の cpu/mem を確認。

### 1-4. 検証とロールバック

- 検証: 縮小後に 1-2 と同じ測り方で slides ジョブを 1 本流し、
  `CpuUtilized` が新しい割当に**張り付いていない**こと（= throttle していない）を見る。
  張り付いていたら所要時間も伸びるので、台帳の `updated_at - dispatched_at` を
  0-4 の p50 と比べる。
- ロールバック: 旧 rev（現行 `teamagent-dev-tiktok-acquire:25`）は `skip_destroy` で
  残る。dispatcher が参照する family:revision を戻せば即復帰できる。

---

## 2. C2: SQS 可視性タイムアウトの判定

### 2-1. `x_jobs` の 1800 秒は **据置**（測れないから触らない）

| 事実 | 値 | 出典 |
|---|---|---|
| repo 宣言 | `visibility_timeout_seconds = 1800` | `infra/terraform/x_research.tf`（`aws_sqs_queue.x_jobs`） |
| live | **1800**（宣言と一致・ドリフトなし） | `get-queue-attributes` |
| ジョブ台帳 `teamagent-dev-x-buzz-jobs` | **0 件（Count=0・ScannedCount=0）** | `dynamodb scan --select COUNT` |
| `NumberOfMessagesSent`（2026-07-28〜08-27） | **Sum = 0** | CloudWatch |
| worker task | rev1 / cpu 512 / mem 1024 | `describe-task-definition` |

**x_buzz は本番で 1 度も走っていない。** 「62 日検索は最大約 2.5h」というコードコメントは
設計時の見積りであって実測ではなく、短縮の根拠も維持の根拠も**どちらも存在しない**。

- 可視性タイムアウトは**課金対象ではない**（短縮しても費用は 1 円も減らない）。
  効くのは「worker が落ちた時に再配信されるまでの待ち時間」だけ。
- ⇒ **今は触らない。** 最初の実ジョブが走った日に、`dispatched_at`/`updated_at` の実測
  p90 × 1.5 を新しい値の根拠にする。それまでは計測器（台帳）を用意しておくだけでよい。

### 2-2. 🔴 media キューに未報告のドリフトがある（repo 180 / live 1800）

| | 値 |
|---|---|
| repo 宣言 | `visibility_timeout_seconds = 180`（`infra/terraform/tiktok_acquire.tf:221`） |
| **live** | **1800** |
| `ignore_changes` | **無し**（`kms_key_id` のみ） |
| dispatcher Lambda | `teamagent-dev-tiktok-acquire-dispatch` timeout **30 秒** |
| event source mapping | batch=1 / Enabled / `maxReceiveCount = 5` |
| ジョブ deadline | 既定 180 秒（台帳の `deadline - created_at` = 179 秒で実測一致） |

AWS の要件は「キュー可視性 ≥ Lambda timeout × 6」＝ **180 が下限**。repo の 180 は
その下限ちょうどで、900 秒の不変 job deadline より前にリトライを終える意図が
コメントに明記されている。live の 1800 はその 10 倍。

**影響**: 次に `aws_sqs_queue.tiktok_jobs` を含む plan を作ると、この 1 行が
**意図しない差分として必ず出る**。C1 の apply（同じ `tiktok_acquire.tf`）で確実に踏む。

**判断**: repo の 180 が正（下限充足・deadline との整合が明文化されている）。live を
180 へ戻す差分は C1 の plan に**同乗させてよい**が、plan レビューで
「task definition 1 本 + キュー 1 本」の 2 差分になることを**事前に宣言してから**通すこと。
黙って通すと「plan に知らない差分が出た」で止まる。

---

## 3. C7: `dispatch_tool` のサーバ側 timeout — **実装しない**

### 3-1. 判断

**この便では実装しない。課題として記録する。**

### 3-2. 根拠（リポジトリ内に実測済みの前例がある）

`src/teamagent/mcp_gateway/server.py:780` は
`output = await asyncio.to_thread(skill.run, skill_input, ctx)`。
ここを `asyncio.wait_for(...)` で包んでも、**止まるのは await だけでスレッドは止まらない**。

`src/teamagent/skills/attachment_assist/skill.py:348-361` に、この論点の実測が
すでに残っている（要旨）:

- `asyncio.run(asyncio.wait_for(asyncio.to_thread(...)))` は、`asyncio.run` が終了時に
  既定 executor を join するため、**timeout しても抽出スレッドが終わるまで戻らない**。
  実測 2 秒の処理 × timeout 0.05 秒で **2.008 秒ブロック**。同条件の
  `ThreadPoolExecutor` は 0.055 秒。
- 採った形は二段構え: ①処理側が `deadline` を見て**協調的に**自分で降りる
  ②それでも返らない分は専用プールの `future.result(timeout=)` で見切り、
  プールを `shutdown(wait=False)` で捨てる。

つまり「スロットは空くが CPU は走り続ける」という当初の理解も**半分しか正しくない**。
`to_thread` は既定 executor へ投げるので、走り出した仕事は cancel できず
**executor のワーカー枠も解放されない**。mcp は cpu 1024（実測）なので、暴走スレッドは
CPU も枠も掴んだままになる。

### 3-3. さらに悪い副作用

`dispatch_tool` が先にエラーを返した後も skill 本体は生き続けるため、
**その後で Slack 投稿・DynamoDB 書込・S3 書込を行う**。利用者には「失敗しました」と
出したのに資料が届く／台帳が更新される、という不整合を作る。

### 3-4. 正しい形（別便の設計課題）

1. 重い skill 側に協調 deadline を通す（attachment_assist と同じ型）。`SkillContext` に
   deadline を載せ、長いループの各段で `checkpoint()` する。
2. gateway 側は「見切り」専用に `ThreadPoolExecutor` を持ち、`shutdown(wait=False)` で
   捨てられるようにする。既定 executor を使う限り見切りは成立しない。
3. 全 skill 一律の適用は不可（thread-local / structlog contextvars / DB プール親和性が
   実行コンテキストに依存する）。**対象 skill を明示列挙**して段階導入する。

見積り: 設計 0.5 日 + 実装 1〜2 日（対象 skill の数で変動）。本便のスコープ外。
