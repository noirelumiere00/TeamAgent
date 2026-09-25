# Hermes 導入 Implementation Plan（DM 本人メモ v1）

- 親文書: [hermes_migration_design.md](hermes_migration_design.md)（Security 不変条件と §10b を正とする）
- 決裁日: 2026-09-25
- 目的: Hermes を「覚える係」に限定し、1 対 1 DM の本人メモを機械検査後に自動反映する
- 共通条件: feature flag 既定 OFF、Hermes に MCP/RDS/Slack/既存 bearer を渡さない、返事は現行 OpenClaw/Haiku が作る
- 番号: v1 の工程は **M0〜M11**。旧番号（PR1〜PR8・PR2-A0/A1/B・PR-R）は v2（汎用 Hermes）で、末尾の付録に残す

## PR 分割と順序

```text
M0 docs
  → M1 guard（未配線）
  → M2 学習ランナー/image（dark）
  → M3 独立した署名 release pipeline
  → M4 ECS dark runtime（0 台）
  → M5 MCP 保存・学習・コマンド（off）
  → M6 管理者閲覧・退職削除
  → M7 capacity gate
  → M8 OpenClaw plugin（off）
  → M9 小俣さん 1 名
  → M10 2〜3 名 → 16 名
  → M11 便δ後の state/guard 整合
```

M3 の Hermes 便に限り、ACTIVATION の「adopt+Pin 完了後に A1」という順序と、正名化に関する 2 つの禁止事項（「正名化をしないまま次の generation release へ進まない」「Hermes A1 が generation publisher を必要とするなら、その前に正名化を片付ける」）を適用しない。M9 は「production user traffic 0」の学習係限定例外であり、小俣さん 1 名から始める。いずれも 2026-09-25 決裁者・小俣承認で、一般則は変更しない。

## PR 一覧と出口条件

| PR | 実装範囲 | 本番変更 / 人の関門 | 出口条件 |
|---|---|---|---|
| **M0** | 本 ADR、実装計画、ACTIVATION 裁定、利用者/運用者向け 1 枚 | 本番変更なし。Human Gate ⑦（不変条件の改訂承認） | docs のみ。§6/7/9/10/20/21/23 と §10b が矛盾せず、M0〜M11、告知、残存先、例外裁定が記録される |
| **M1** | `src/teamagent/personal_memory/{__init__,guard,_threat_patterns}.py` と tests（PR #446）。注入パターンは自作（英語・日本語・ロールタグ）。`member_names`（社内の同僚名）は許可側。どこからも import しない | 本番変更なし / Gate 不要 | 良例/悪例、24/25 字、NFKC、非保持、ablation、純粋性、3 変異が期待どおり。`check_entry` は保存前/読出し、`check_utterance` は学習前に使える純粋関数 |
| **M2** | `hermes_runtime/`（Python 3.13、commit 固定、独立 lock）、`:8790` TLS server、`/healthz`、`/v1/learn`、子 process + 使い捨て `HERMES_HOME`、本家 memory tool、config lint、`Dockerfile.hermes` | 本番変更なし / C/H 0 の基盤が無ければ再裁定 | A/B job の HOME 非共有、foreground replace/remove、config load fail-closed、余分 tool/background review 拒否、crash 時 HOME 削除、identity field schema 拒否、Bedrock 以外 egress 無し、Trivy C0/H0。jp. Haiku inference profile の受付を実測。**書き込み可能な領域全体（HOME の外の /tmp・キャッシュを含む）で目印入り発話が残らない** |
| **M3** | Hermes 独立 pipeline。ECR 3 本/lifecycle 2 本、CodeBuild、builder/launcher role、KMS、buildspec/provenance/contract/例外空 JSON、promoter 3 層 allowlist、attestor、release evidence、approval、StartBuild 条件。generation 18 入力を更新し unlock scope を変更 | ECR/CodeBuild/KMS/IAM と初回便。IAM apply、generation publish、発射、署名承認は小俣さん | release ECR に digest 1 本、receipt/attestation、未登録 pipeline/subject FATAL、release repo に lifecycle 無し、各 IAM policy <6,144 字、Hermes 承認必須。既存便を止めない |
| **M4** | `hermes.tf` / variables。ECS service（Terraform で常駐 0 台と宣言）、release digest、task role は限定 Bedrock+logs、execution role は secret 2 本だけ、SG/Cloud Map/log group、public IP 無し | 0 台 service/SG/IAM/Cloud Map/secret/SG rule。**MCP の execution role に Hermes ingress bearer の読み取りを追加し、Hermes の TLS 証明書を検証する手段（CA かピン留め）を MCP に渡す**。secret 作成、IAM 拡大、apply、初回 RunTask に Gate ②④⑥⑪ | healthz → Bedrock init → 目印入り合成 1 job → CloudWatch に目印無し → 終了。Deny 存在、egress に 8787/5432/`0.0.0.0/0` 無し、0 台、public IP 無し。**vpce SG は inline ingress の concat に `var.enable_hermes ? [hermes SG] : []` を足す形で配線する**（別リソースの rule を混ぜると次の通常 apply で消える。`infra/terraform/vpc_endpoints.tf:14-38,48`）。concat に Hermes が入っていることを契約テストで固定。Bedrock の IAM は jp. 推論プロファイルに加えて裏側のモデル ARN（大阪・東京）への Allow も要る（OpenClaw と同型、`infra/terraform/fargate.tf:321-335`） |
| **M5** | migration `0029_personal_memory.sql`、profiles/entries/audit、RLS、store/buffer/learner/context/commands、MCP plugin 専用 tools、Sentry scrub。5 発話/10 分、1 人 1 日 6 job、全体 1〜2 job、context 3,600 字、全削除の確認 10 分 | mcp 便へ同乗、flag off。便の署名承認と DB migration に Gate ①。migration 番号は `0029` を仮置き（draft #420 の `0028` が先に入る前提。入らなければ次の空き番号） | 実 DB で A/B RLS。**`app.user_role='admin'` の接続でも本人メモの表は 0 行**（管理者閲覧は SECURITY DEFINER 関数だけ）。**サーバ側の不変条件**: list_tools に `personal_memory_*` が無い／署名済み claim の channel が `^D` fullmatch・予約 tool_call_id・flag と allowlist がそろわないと拒否／引数に `query` を使わない（usage query_text=None）。各項目を変異テストで固定。在籍者名簿（`users.list` キャッシュ）を `member_names` として guard に渡す。profile キーは `team_id:U…`。書込/読出し guard、version/凍結/削除 race、1.2 秒 fallback、本文がログ/Sentry（ローカル変数送信も停止）に無いこと。ON CONFLICT 不使用 |
| **M6** | connect_web `/admin/memory` と access-log、表示前 audit INSERT、本人への閲覧回数、`retire.py` と morning digest の `users.info` 掃除 | connect_web TD と morning digest env。名簿決定/TD 差替えに Gate | 非管理者 403、監査失敗は 503 かつ無表示、`deleted=true` は削除（DELETE 専用関数・保存した `U…` で `users.info`）、API 例外は削除しない、guest は凍結のみ。**本人メモ専用の `PERSONAL_MEMORY_ADMIN_EMAILS`**（利用状況画面の `CONNECT_ADMIN_EMAILS` と共用しない）が「小俣さんちょうど 1 名」であることを契約テストで固定 |
| **M7** | 既存 `runtime/request_gate.py` を MCP dispatch 直前へ接続、構造化 overload、in-flight metrics、重い tool semaphore。observe/context は軽い lane | mcp 便/TD env。便の承認に Gate | 上限超過は構造化 error、上限内は素通り、配線を外す変異が赤。context は 1.2 秒超過で空を返し返信を止めない |
| **M8** | caller-identity plugin に `message_received` observe、`before_prompt_build` context、`before_agent_reply` commands、LLM 経路の署名拒否、SOUL/config/env。hook 8→9。flag off | OC 便。security 再レビュー、発射/承認に Gate | C/G/mpim/Slack Connect/スレッドは observe/context 無し。DM 判定は「ingress の channelId が `DM:<sender>`」「ctx.chatId が `^D` fullmatch」「chat 種別 direct」の 3 条件で、`conversations.open` の fallback を使わない。LLM 経路 `personal_memory_*` 拒否、timeout でも返信、本人メモは system 側にだけ差し込み **EFS の会話記録に目印が残らない**、凍結・削除でキャッシュ即時破棄、hook banner と登録一致、effective scope/include に memory tool 無し。コマンドは「記憶を再開して」など全文一致 |
| **M9** | DB migration、mcp/OC flags と allowlist、Hermes 1 台を順に点灯。Hermes の desired_count 0→1 は tfvars の変更と `-var-file` 付き apply で行う（ECS を手で操作しない） | 実利用者 DM、小俣さん 1 名。Gate ⑩・Gate ②（apply）と TD 差替え。告知の送信状態（`notified_at`）を profile に記録してから学習を始める | 初回告知、1 通目は学習しない、5 発話後に学習、次 turn 反映、5 コマンド、C/G/mpim 非適用、管理閲覧監査、先方名/URL 拒否、凍結/全削除、CloudWatch/Sentry に本文無しを実機確認 |
| **M10** | 法務/総務確認後、allowlist を 2〜3 名へ広げ 5 営業日観察し、GO 後に 16 名へ | 対象拡大と告知。決裁者の明示 GO 必須 | 学習件数、拒否理由、p95、Bedrock 費用を観察し許容。告知と社内公表が完了 |
| **M11** | guard 外で作った ECR/CodeBuild/IAM/ECS/SG/secret を import。runtime guard に Hermes consumer image、`GUARD_VERSION` 更新、台帳 close | state 取込のみ。Gate ②③ | import 後の plan 差分 0、guard 契約緑、台帳 close。runtime 挙動不変 |

## v1 の runtime / network 契約

| 項目 | 契約 |
|---|---|
| Hermes ingress | MCP SG → Hermes SG の TCP 8790、TLS + ingress bearer |
| Hermes egress | vpce SG の 443 と S3 prefix list だけ。MCP 8787、RDS 5432、internet へ出さない |
| secret | ingress bearer と TLS 鍵の 2 本だけ |
| IAM Allow | jp. Haiku inference profile の `bedrock:InvokeModel` と logs |
| IAM Deny | secretsmanager / kms / rds / dynamodb / s3 を明示 Deny |
| callback/delegation | v1 は無し。callback、delegated claim、`HMAC_PURPOSE_HERMES_DELEGATION` は M4 以降の後続設計へ送る |
| 保存 | Hermes は一時 HOME のみ。MCP が `personal_memory.guard` 後に RDS へ書く |
| S3 経路 | task role で s3 を Deny しても、S3 gateway endpoint に endpoint policy が無い（`infra/terraform/vpc_endpoints.tf:89-97`）ため、署名なし・presigned の外部 bucket へは届きうる。M4 で S3 prefix list を egress から外すか endpoint policy を付ける |

### vpce SG の訂正（旧 plan:68）

旧記述の「RDS SG / vpce SG に Hermes SG を足さない」は誤りである。**RDS SG には足さず、vpce SG の ingress には Hermes SG を足す**。private DNS の interface endpoint を使う task は vpce SG の 443 許可が必要で、既存 Terraform 自身が「ここに入れ忘れると…443 が落ち provisioning ループになる」と警告している（`infra/terraform/vpc_endpoints.tf:19-31`、特に `:21`）。M4 の契約テストでこの配線を必須化する。

## 保存・監査・本人操作の実装契約

| 領域 | 決定 |
|---|---|
| 対象 | `^D[A-Z0-9]{8,}$` の fullmatch だけ。C/G/mpim は学習も適用もしない |
| 保存可 | 返事の長さ/言い回し、顧客名、商材名、資料の型、仕事の進め方、社内同僚名 |
| 保存不可 | 会話本文、25 字以上の逐語、先方担当者名、URL、連絡先、秘密、prompt injection。人名は敬称ベースで迷ったら落とす |
| 管理者 | connect_web だけ。「監査 INSERT → 行を返す」の SECURITY DEFINER 関数だけで読む。失敗は 503/無表示。本人メモ専用の `PERSONAL_MEMORY_ADMIN_EMAILS`（v1 は小俣さん 1 名） |
| G7 例外 | MCP 揮発メモリだけに最大 10 分/5 発話。DB/log/CloudWatch/Sentry へ出さない |
| 凍結 | 学習も適用も停止、内容は残す。明示「再開して」まで再開しない |
| 全削除 | 10 分以内の確認後に物理削除し frozen。自動 backup は最長 7 日、Bedrock 呼出しログは最低 60 日残る（ADR §10b.5 の残存表） |
| 退職 | `users.info deleted=true` の直接確認時だけ自動削除。resolver `None` は API 失敗でも返るため使わない（`src/teamagent/adapters/slack_client.py:392-408`） |

### M5 の実装メモ（2026-09-25）

| 項目 | 内容 |
|---|---|
| DB ロール | 本人の読み書きは専用の `personal_memory_app`（NOLOGIN NOBYPASSRLS）で行う。master には INHERIT FALSE・SET TRUE で付け、master 直結（BYPASSRLS の有無にかかわらず）・`teamagent_app`・`teamagent_dashboard` からは表が読めない。表と関数の所有者は `personal_memory_definer`。migration の最後で「master が本人メモのロールを継承していない」ことを検査し、満たさなければ中止する |
| 環境変数（mcp） | `USE_PERSONAL_MEMORY`（既定 off）／`PERSONAL_MEMORY_ALLOWED_EMAILS`（空＝全員拒否）／`PERSONAL_MEMORY_MAX_JOBS`（1〜2・既定 1）／`HERMES_SERVICE_URL`（https・8790）／`TEAMAGENT_HERMES_INGRESS_BEARER`／`HERMES_TLS_CA_PEM`（自己署名 CA をトラストアンカーにしてホスト名も検証）／`PERSONAL_MEMORY_NOTICE_RETENTION`・`PERSONAL_MEMORY_NOTICE_CONTACT`（告知文の「〇〇」。**未設定なら告知を出さず、告知済みにもしない＝学習が始まらない**）。在籍者名簿は既存の `SLACK_TEAM_ID`・`SLACK_BOT_TOKEN` で `users.list` を読む |
| 点灯前の確認（M9） | bot token に `users:read` があること（`users.list` を読み取りで 1 回）／Hermes 証明書の SAN が `HERMES_SERVICE_URL` のホスト名と一致すること／告知の 2 値の確定／mcp では Sentry が未初期化なので「Sentry に無い」確認は実質的な確認にならないこと |
| guard の訂正 | 「〜を担当する」（目的語が仕事）を人名扱いしていた誤検知を直した。「田中が担当」「花王担当」「山田を担当に推す」は引き続き落とす |

## 人の作業と目安

| 時期 | 担当 / 作業 |
|---|---|
| 着手時 | 決裁者: ADR 3 点（自動反映、記録つき管理者閲覧、学習係の実流量例外）と v1 境界を承認（Gate ⑦） |
| M3 | 小俣さん: IAM targeted saved plan apply、generation publish、初回 Hermes 便、署名承認 |
| M4 | 小俣さん: secret 2 本、guard 外 apply、RunTask 受入れ |
| M5〜8 | 小俣さん: mcp/OC 便、security review、migration、connect_web/mcp/OC TD 差替え |
| M9 | 小俣さん: 本人 1 名の点灯と DM 実機確認（Gate ⑩） |
| M10 | 決裁者・法務/総務: 告知、2〜3 名、16 名への各 GO |
| M11 | 小俣さん: terraform import/apply（Gate ②③） |

点灯前に月次予算を再確認する。`monthly_budget_usd` の既定は USD 250（`infra/terraform/budgets.tf:11-15`）。今回の増分 USD 55〜80/月は仮定であり、usage_events による再検証は**未確認**。

## リリース停止条件

- Python 3.13 基盤で Trivy Critical/High 0 を満たせない
- jp. Haiku inference profile を Hermes provider が受け付けない
- `before_prompt_build` の本番発火を確認できない
- CloudWatch、Sentry、アプリログ、OpenClaw の EFS 会話記録のいずれかに合成本文や本人メモが残る
- Bedrock 呼出しログ（既定で有効・本文を含む・最低 60 日）の保持を告知文に書いていない
- 管理者画面が監査 INSERT より先に表示できる
- M7 の capacity gate が未配線
- 告知、管理者名簿、法務/総務確認、決裁者 GO のいずれかが未完了

## 各 PR で報告するもの（運用契約）

開始前: Scope / Files / Security impact / Runtime impact / Rollback / Human Gate。

終了後: Changed files / Tests / Security tests / Terraform plan shape / Runtime behavior / Feature flags / Known risks / Rollback command。

---

## 付録: v2（汎用 Hermes）の旧計画（2026-08-18 版・参照用）

ADR §25・`docs/README.md`・ACTIVATION の Next action が参照する PR2-A0 / PR2-A1 / PR2-B / PR3 のマージブロッカー / PR-R の詳細は以下に残す。v1 の M0〜M11 には適用しない。

## Hermes 導入 Implementation Plan（PR 分割・実装単位・テスト戦略）

- 親文書: [hermes_migration_design.md](hermes_migration_design.md)（ADR。設計判断・Security 不変条件はそちらが正）
- 前提: 全変更は Feature Flag 既定 OFF・既存挙動の無フラグ変更なし・OpenClaw toolFilter.include に入れない（dark）・Big Bang 禁止

### PR 分割と順序

```
PR1 Docs → PR2-A0 Supply-Chain Adopt → PR2-A1 Hermes supply-chain onboarding
        → PR2-B Hermes dark runtime → PR3 run_hermes_agent + delegated security
        → PR-R capacity control（必須 Gate）→ PR4 Proposal pilot
        → PR5 Profile → PR6 Memory → PR7 Multi source → PR8 Router
```

| PR | 内容 | 変更範囲 | リスク | 承認 |
|---|---|---|---|---|
| PR1 | docs only（ADR/実装計画/README/索引/Archive） | *.md のみ | ゼロ | 完了（PR #302 merged） |
| **PR2-A0** | **Supply-Chain Adopt（本 PR）**: content-addressed buildspec を hash-keyed append-only generation model へ移行し、実態から取り残された Terraform state を adopt。**Hermes は一切登場しない** | infra/terraform（既存 buildspec object の世代化）, infra/deploy, docs/runbooks | 中（供給網の要・実体は不変） | PR1 後に個別承認 |
| **PR2-A1** | **Hermes Supply-Chain Onboarding**: upstream image を digest 固定 → 薄い derived image → 署名リリース鎖へ載せる（ECR 3 本・promoter 3 層 allowlist・receipt subject・contract・テスト）。**ECS は作らない** | infra/docker, infra/codebuild, infra/terraform（ECR/IAM/promoter）, tests | 中（既存鎖の改修を含む＝最低 14 ファイル） | PR2-A0 後に個別承認 |
| **PR2-B** | **Hermes dark runtime**: PR2-A1 の release digest で ECS service（**常駐タスク 0 — Terraform で desired_count を 0 と宣言**）+ 最小 IAM + healthz | infra/terraform（新規リソースのみ） | 低 | PR2-A1 後に個別承認 |
| PR3 | run_hermes_agent + delegated session claim + callback boundary + Security Tests | mcp_gateway/, hermes/, tests/ | 中（flag OFF で不活性） | **着手前に delegated claim 設計の再レビューを実施**（裁定済み） |
| **PR-R** | **容量制御（PR4 前の必須 Gate）**: MCP admission control / in-flight metrics / heavy-tool semaphore / 明示 overload 応答 | mcp_gateway/, runtime/, fargate.tf env | 中 | 個別承認 |
| PR4 | Proposal Specialist pilot（allowlist + A/B eval） | hermes profile 定義, eval | 低 | 個別承認 |
| PR5 | Personal Profile（hermes_profiles migration + store） | migrations, hermes/ | 低（additive） | 個別承認 |
| PR6 | Personal Memory（propose→approve・監査） | hermes/, usage_recorder | 中 | 個別承認 |
| PR7 | Multi source 解禁拡大（policy version 更新を含む） | policy 定義, （必要なら）skill G1 強化 | 中 | 個別承認 |
| PR8 | AI General Router（SOUL 改訂 + OC 4点セット） | infra/openclaw/ | 中 | 個別承認 |

### PR2-A0 詳細（Supply-Chain Adopt・本 PR）

**なぜ独立した PR なのか（ADR §20.1）**: buildspec は evidence バケット上に content-addressed key（`codebuild-buildspecs/<project>/<body の sha256>.yml`）で置かれ、Object Lock GOVERNANCE と bucket policy の Delete Deny で不変化されている。body の入力が変わるたびに key が変わり replacement 判定になるが、`prevent_destroy` が plan 段階で停止させるため、**dev HEAD は `aws_s3_object` 4 本で apply 不能**だった。AWS 実体は正しく、取り残されていたのは tfstate だけである。**Hermes はこの PR に一切登場しない。**

**モデル**: 世代（generation）を body の sha256 をキーにした **append-only 台帳**として持つ。新世代の取り込みは「実体を publish → 台帳へ 1 エントリ追記 → adopt（import）」で、既存エントリは削除しない。

| Priority | File | Change | Test / 証明 | Rollback |
|---|---|---|---|---|
| P0 | `infra/terraform/supply_chain_adopt.tf` | 世代台帳 + `for_each` の世代リソース（`prevent_destroy` 維持）・旧アドレスの `removed`（`destroy = false`）・publish 済み実体の `import` | read-only plan で replacement 0 件・実体の VersionId 不変 | 台帳/世代リソースを戻すだけ（実体は不変） |
| P0 | `infra/terraform/codebuild.tf` / `mcp_approval.tf` | 単一アドレスの content-addressed `aws_s3_object` 定義を撤去し、世代アドレスへ移す（key 導出 local は body 検査用に残す） | `terraform validate` + plan 差分レビュー | 同上 |
| P0 | Terraform `check` ブロック | 現行 body の sha256 が台帳に登録済みであることを要求（実体の無い key を CodeBuild へ指す事故を停止） | 未登録 sha に変異させて check が赤くなることを確認 | — |
| P0 | `infra/deploy/supply_chain_adoptions.json` | adopt の exact mapping（old_address / new_address / key / import_id / expected_content_sha256）。列挙外は通さない | key の basename と new_address の index が sha256 に一致することの検査 | 台帳エントリを戻すだけ |
| P0 | `infra/deploy/supply_chain_adopt_validate.py` | plan validator（**fail-closed**）。許可するのは `no-op` / `forget`（mapping の old_address 一致）/ `update` かつ import 付き（mapping の new_address・import_id 一致）の 3 種のみ。create・delete・replace・mapping 外アドレスは 1 件でも拒否。mapping 全件が過不足なく plan に現れることも要求 | 実 plan JSON に対する実行 + 変異（create/delete/アドレス改変）で赤になることの確認 | validator を通さない apply はしない |
| P1 | 手順書（新世代の publish → 台帳追記 → adopt） | 運用手順を runbook 化 | — | — |

**PR2-A0 受け入れ条件**: dev HEAD の `terraform plan` が prevent_destroy 停止なしに通る / replacement・destroy 0 件 / 新しい buildspec 世代を出せる承認済み apply 経路が実在する / **既存の `prevent_destroy` / Object Lock（GOVERNANCE）/ bucket policy の Delete Deny を一切弱めていない** / 既存リリース鎖の挙動不変。

### PR2-A1 詳細（Hermes Supply-Chain Onboarding）

**スコープ**: Hermes upstream image（Docker Hub `nousresearch/hermes-agent` の release tag を **digest 固定**）から薄い derived image を作り、TeamAgent の署名リリース鎖（**quarantine → SBOM/Trivy/attestation → verified-candidates → promoter → release ECR**）を通せる状態にする。**ECS は作らない**（成果物は検証済みの release digest 1 つ）。既存改修だけで最低 14 ファイルに及ぶため PR2-B と分離する。

| Priority | File | Change | Test / 証明 | Rollback |
|---|---|---|---|---|
| P0 | `infra/docker/Dockerfile.hermes` | upstream を **digest 固定** pin した薄い derived image・非 root・readonly rootfs | Trivy C0/H0（リリース契約と同基準） | イメージ未使用なら無影響 |
| P0 | ECR 3 本（`*-hermes-quarantine` / `*-hermes-verified-candidates` / `*-hermes`） | 既存 openclaw/mcp と同型で追加（immutable tag・lifecycle） | ECR 契約テスト・tf diff レビュー | 新規リソースのみ＝除去容易 |
| P0 | image promoter の 3 層 allowlist | pipeline / receipt subject name / receipt repository mapping の各 allowlist へ hermes を追加（**既存 3 層構造を緩めない**） | 未登録 subject / 未登録 mapping が FATAL で落ちることを実測 | allowlist から除去 |
| P0 | receipt subject / contract | hermes の subject 名と repository mapping を契約側へ登録 | 既存 receipt 契約テストと同型 | 同上 |
| P0 | attestation / SBOM 経路 | attestor に hermes pipeline を追加（証跡は既存 evidence バケット・KMS を流用） | 実走で receipt / attestation が生成されること | — |

**PR2-A1 受け入れ条件**: release ECR に Hermes の digest が 1 本入る / receipt・attestation が既存 OpenClaw・MCP と同基準で揃う / promoter の 3 層 allowlist が未登録値を FATAL で拒否する（変異テストで実証）/ **ECS リソースを一切作っていない** / 既存 pipeline の挙動不変。

### PR2-B 詳細（Hermes dark runtime）

**入力**: PR2-A1 が生成した **release digest**（タグではなく digest で参照）。

**受け入れ形態（ADR §20.4 裁定）**: 常駐タスク 0（Terraform で desired_count を 0 と宣言）。受け入れ試験は ECS RunTask で 1 タスク起動 → `startup → /healthz → Bedrock client 初期化 → CloudWatch logs` を確認して終了。常駐ゼロ＝idle コストゼロ・外部 routing 0・MCP exposure 0 が自明。

| Priority | File | Change | Test / 証明 | Rollback |
|---|---|---|---|---|
| P0 | `infra/terraform/hermes.tf` | ECS service `teamagent-hermes`（desired_count を 0 で宣言・常駐なし）・image は PR2-A1 の release digest・Cloud Map `teamagent-hermes.teamagent.internal`・SG は mcp→hermes:8790 / hermes→mcp:8787 のみ。**RDS SG / vpce SG に hermes SG を足さない** | SG 契約テスト・tf diff レビュー | 新規リソースのみ＝除去容易 |
| P0 | hermes IAM role | Allow = bedrock:InvokeModel（限定 profile）+ logs のみ。OpenClaw 同様の**明示 Deny**（secretsmanager:\* / kms:\* / rds\* / dynamodb:\* / s3:\*） | IAM policy 契約テスト（既存 test_\*_contract 同型） | 同上 |
| P0 | healthz | GET /healthz = process alive + config loaded + Bedrock client init + MCP callback 設定 presence（外部 tool call は含めない） | RunTask 受け入れ試験 | — |
| P0 | 構造化ログ | service/version/request_id/model/latency/error（dark 中は startup/health のみ） | CloudWatch 実ログ確認 | — |

**PR2-B 受け入れ条件**: OpenClaw 挙動完全不変 / MCP tool list 完全不変 / existing tests green / RunTask 試験緑 / RDS・OAuth token へのアクセス権なし（IAM 実証）/ rollback = リソース削除 or 常駐タスク 0（Terraform 宣言値）のまま放置。

### PR3 詳細（run_hermes_agent + delegated claim）

設計は ADR §7 が正。実装単位:

| Priority | File | Change |
|---|---|---|
| P0 | `src/teamagent/mcp_gateway/hermes_claim.py`（新規） | `DelegatedSessionClaim` + `DelegatedClaimVerifier`（**既存 CallerClaimVerifier とは別クラス**・16field 型の exact-set 検査/重複キー拒否/サイズ上限/署名先行検証は caller_claim.py の実装を共通化して踏襲）。K_session = HKDF(master, session_id‖nonce‖sub) 導出・per-call MAC 検証 |
| P0 | `src/teamagent/mcp_gateway/hermes_session_store.py`（新規） | DynamoDB `hermes-session-state`: session 正本（sub=principal/allowed_tools/policy_version）・deadline・remaining_calls・consumed call_nonce・budget。「nonce 未使用 ∧ calls<max ∧ deadline ∧ budget」判定と consume+increment は**単一 `TransactWriteItems` で原子化**（分割 write 禁止＝replay が budget だけ削る DoS 防止・ADR §7.4）。**全障害 fail-closed** |
| P0 | `src/teamagent/mcp_gateway/server.py` | `RUN_HERMES_TOOL_NAME="run_hermes_agent"`・`_envflag("USE_HERMES_ORCHESTRATOR")` で list/call（run_agent と同型）。dispatch は既存 `_verify_caller`→`_resolve_metadata` 通過後に claim mint → Hermes へ HTTP POST |
| P0 | `scripts/run_mcp_http_server.py` | BearerAuthMiddleware を route×token 対応表型へ拡張し `/hermes/callback` を追加（既存 route の挙動不変）。callback route は縮小 tool マップ＋`_user_context.caller_claim` 不受理 |
| P0 | `src/teamagent/hermes/`（新規 pkg） | gateway_client（Gateway→Hermes・bearer・timeout・構造化エラー）・policy.py（`hermes_tool_policy_version=1` の allowlist/denylist 定義＝サーバ側真実源） |
| P0 | `src/teamagent/hmac_keyring.py` | `HMAC_PURPOSE_HERMES_DELEGATION` 追加（purpose 重複拒否・rotation 継承）。相互排他チェック 5 値化 |
| P0 | `tests/scripts/test_openclaw_runtime_contract.py` | run_hermes_agent が OC include に**無い**ことの断言（dark 宣言） |
| P0 | env | `USE_HERMES_ORCHESTRATOR`(0) / `HERMES_SERVICE_URL` / `TEAMAGENT_HERMES_INGRESS_BEARER` / `TEAMAGENT_HERMES_CALLBACK_BEARER` / `HERMES_SESSION_STATE_TABLE` / `HERMES_COST_CAP_USD`(0.5) / `HERMES_MAX_CALLS`(8) / `HERMES_SESSION_TTL_S`(≤300・absolute_deadline は v1 で TTL と同値) / `HERMES_ALLOWED_EMAILS`(空=拒否) |

#### PR3 Security Tests（指示 §19 の A〜K 対応 + マージブロッカー）

| 指示 | テスト | 既存の複製元 |
|---|---|---|
| A/B/C forged role/groups/email → ignored | Hermes 申告値の全破棄 + fuzz で `user_role` 常に member | `test_mcp_gateway_identity.py:102,203` |
| D expired → reject | exp / absolute_deadline 超過 | `test_mcp_gateway_caller_claim.py:899-943` |
| E replay → reject | per-call nonce one-use + verifier インスタンス跨ぎ | 同 `:946-1012` |
| F tool outside allowed_tools → reject | intersection + **denylist が allowlist に勝つ** | 同 `:673-711` 型 |
| G max_calls exceeded → reject | 並行 16→8 race（**単一 TransactWriteItems** の原子性実証・replay で budget だけ削れない） | 条件付き write は `hmac_durable_state.py:704` 型 |
| H User A claim → User B → reject | cross-user session race・K_session 混線なし | `test_mcp_gateway_caller_claim.py:714-896`（最重要） |
| I profile isolation | PR5 で実装（v1 は not yet implemented を明示） | — |
| J recursive → impossible | meta-tool 恒久 deny（claim に入れても拒否） | — |
| K missing identity → fail-closed | resolver 再実行でゲスト/退職/stranger 拒否・予算ストア障害時 skill 不実行 | `test_mcp_gateway_caller_claim.py:1015-1080` |
| 起動時契約 | 鍵未設定/同値/台帳未設定で起動拒否 | `test_mcp_gateway_caller_claim.py:1091-1115` |
| route×token | 既存 bearer で callback 不達・callback bearer で /mcp 不達 | 新規 |

全て**変異テスト**で実質性を証明（ガードを壊して赤を確認）。

### PR-R 詳細（容量制御・PR4 前の必須 Gate）

| 項目 | 実装 |
|---|---|
| MCP admission control | `dispatch_tool` 直前に既存 `RequestGate`（runtime/request_gate.py）を module-level 配線。`REQUEST_GATE_*` env を fargate.tf mcp taskdef へ |
| 明示 overload 応答 | QueueFullError/GateTimeoutError → `_err(...)` 構造化エラー → OpenClaw が「混雑しています」を返す |
| in-flight metrics | MetricsSnapshotter を MCP プロセスへ配線（gate/pool → runtime_metrics） |
| heavy-tool semaphore | video_algorithm / proposal_builder 等の別枠（connect_web の SEARCH_CONCURRENCY 同型） |

### PR4〜PR8（概要）

- **PR4**: Proposal Hermes（allowlist= search/clientkarte/proposal_*・`HERMES_ALLOWED_EMAILS` で段階公開）。eval は `orchestrator/eval.py`・`faithfulness.py`（chunk_id 忠実性照合）を流用し、既存フロー vs Hermes の同一 goal shadow 比較（quality/latency/cost/tool count/failure rate/citation）
- **PR5**: `hermes_profiles` migration（RLS 本人行のみ・oauth_tokens_self 同型）+ `profile_id = HMAC(salt, principal_id=team_id:slack_user_id)` の決定論導出（**email 非依存** — email は resolver 由来の属性列。ADR §8）。Hermes からの profile 指定は不可
- **PR6**: memory_items（propose→pending→approve）。company source ingest 禁止は「永続化 API を渡さない」構造で担保。隔離テスト（A≠B・改ざん不可・Hermes 自身による profile 変更不可）
- **PR7**: policy version 更新による段階解禁（前提: mail_*/calendar_* G1 の identity_verified 強化）。GWS/Slack/RAG は既存 adapter/MCP を再利用（再実装禁止）。Salesforce は adapter+skill 新設後に allowlist へ
- **PR8**: Router（SOUL 改訂）。OC 露出は 4 点セット（tf env / scope 台帳 / 契約テスト / OC イメージ再ビルド）

### 各 PR で報告するもの（運用契約）

開始前: Scope / Files / Security impact / Runtime impact / Rollback。
終了後: Changed files / Tests / Security tests / Terraform plan shape / Runtime behavior / Feature flags / Known risks / Rollback command。
