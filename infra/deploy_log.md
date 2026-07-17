# deploy_log — 本番デプロイ履歴（image ↔ commit/branch ↔ 概要）

このファイルが**「何が本番で動いているか」の唯一の正**。CLAUDE.md §4(B3)/§10 の運用ルール：
**デプロイするたびに 1 行追記**（image tag / digest 先頭 / 出所branch・commit / 対象service / 概要 / 実行者）。

> 初版は 2026-06-25 に **実機（ECS task-definition + ECR タグ）を直読みして再構成**したスナップショット
> （それ以前の履歴記録は存在しなかった）。値は「読み取った時点」のもの。

---

## 2026-07-16 🚑 /app の Drive 共有範囲を本番へ再同期
- source=`dev`@`ad39f501`（#222）＋ローカル未マージの Unicode パス別名保護。RDS の会社共有 ACL を正として Vault を全量再生成し、旧管理 note 1,902件を prune。生成物は `clients=518 / docs=659 / sha256=772c7e1609ad...`。
- 反映前に検出した「`acl_groups` に `vectorinc.co.jp` が無い Drive 文書」の `/app` 混入は116件。反映候補の抽出可能な Drive file ID 302/302件を会社共有と照合し、未許可0件を確認。S3 配信物を再取得して候補と byte-for-byte 一致も確認。
- S3 `codebuild/connect-web-app.html` VersionId=`R2Q2X4WgaIwccMEmg2SUorr3swNClh9U`、connect-web=`:48` を force deploy。`/healthz` は source=`s3` / sha=`772c7e1609ad`、rollout COMPLETED・1/1 healthy、反映後 ERROR/Exception/Traceback 0。未ログイン `/app` は `/search/login?next=%2Fapp` へ303。
- rollback=S3 VersionId=`aW0hB4FZP7VL.G7aHlLsT4mThMOOD8Xl` を復元後、connect-webを force deploy。実行者=Codex（s-komata AWSアカウント）。

---

## 2026-07-17 🚀 `/app` クライアント所属・監査済み属性の補正を本番反映
- source=`dev`@`5bfdab620844fc84b0876701cd9f41f34f1f5f87`（#245/#246、CI全緑）。DB由来のクライアント所属を優先し、タイトルだけ一致した別案件を活動件数・時系列・最終接点へ混ぜない境界判定と、監査済み28社の業種マスターを反映。SBI証券/SBI生命保険を分離し、検証済みのi-ne/泉屋表記だけを統合。
- 現行Vaultから生成したHTMLは sha256=`03f8e8cc0adbc397cc636e30fcc8baaffeb1c53502cf74baf1031399cceb391c`、manifest=`aa451e744d26e9dc13c170b019307b0eb10d3645267960fbff41c4038e9b909e`、build inputs=`6697acf311f0c9a96b41426e81ae05ad221482a6e6f69799281ad3532c2e78bf`。clients=516 / docs=662 / timeline=447（全件日付あり）/ payload FB=677 / activity doc pair=575 / FBありかつtimelineなし=0 / source ownership mismatch=0 / document・FB日付欠落=0 / internal source exposure=0。artifact QAは violations={}。
- 画面QAで、ポート株式会社=人材・資料2件・最終接点2025-10-02、SBI2社の分離、花王の担当者と日付時系列、グラフ「まとめる（配置）」を確認し、ブラウザログエラー0。S3 `codebuild/connect-web-app.html` VersionId=`FTXbcN70D0DCN90TI_hRK1IdQK_HhLee`、connect-web=`:50` を force deploy。`/healthz` は source=`s3` / sha=`03f8e8cc0adb`、rollout COMPLETED・1/1 healthy・pending=0・直近30分の ERROR/Exception/Traceback 0。未ログイン `/app` はログイン画面へ303。
- rollback=S3 VersionId=`OLMDBO1l.ibKH8700v.ckAEk.5klK7h3`（sha=`5b1026c60d07...`）を復元後、connect-webを force deploy。実行者=Codex（s-komata AWSアカウント）。

---

## 2026-07-17 🚀 `/app` 営業FB時系列の欠落防止を本番反映
- source=`dev`@`da4f8facae541dcb205890713f52c9db894e9e1f`（#242、CI全緑）。日付見出しへ移行した営業FBを正しく時系列へ取り込み、FBがあるのに時系列が空になる生成物をQAで拒否。グラフの「その他」表示と担当者名の切れた接尾辞も修正。
- 現行Vaultから生成したHTMLは sha256=`5b1026c60d07e12ae7d2d11afa135b8e275524a2c10f60adda590b4859484a2e`、manifest=`f78c318279540bec0ef19236b20d775e9d56e677a2aaee9836fb8a2afaddadb1`、build inputs=`f3eba1edeadb6be7127b7fea81e369a40a9e40d9ae34aba8c078224a89c10e92`。clients=518 / docs=662 / timeline=448（全件日付あり）/ payload FB=679 / FBありかつtimelineなし=0 / schema error=0 / internal source exposure=0。全3,057テスト＋環境依存skip 10、生成・QAの集中テスト162件、画面確認を通過。
- S3 `codebuild/connect-web-app.html` VersionId=`OLMDBO1l.ibKH8700v.ckAEk.5klK7h3`、connect-web=`:50` を force deploy。`/healthz` は source=`s3` / sha=`5b1026c60d07`、rollout COMPLETED・1/1 healthy・pending=0・直近30分の ERROR/Exception/Traceback 0。未ログイン `/app` は `/search/login?next=%2Fapp` へ303、ログイン画面とsecurity headersを再確認。
- rollback=S3 VersionId=`I1qOb7Kwl.pMg71wqFxbHnbbTqMWjQcY`（sha=`46f0079783cd...`）を復元後、connect-webを force deploy。実行者=Codex（s-komata AWSアカウント）。

---

## 2026-07-17 🚀 connect-web 最新 dev 統合／`/app` QA署名不一致を解消
- connect-web: source=`dev`@`e4daa71986f544d66e0563879b7a4808b4e7b674`（#240まで、post-merge CI全緑）/ image tag=`connect-unified-20260717-141242` / digest=`sha256:0f23860dc382e29d2051f3e6e415a427c853182d90ef05cce0935c3c7cecc144` / task definition=`teamagent-dev-connect-web:50`。固定S3 `codebuild/source.zip` VersionId=`ln59hKGu176f1SfoRYUHao0W7wPtbKqd`の全tracked fileがclean worktree `e4daa719...`とbyte-for-byte一致、`:49`→`:50`はtask definitionがimage以外完全一致。rollout COMPLETED・1/1 healthy・failed=0・直近logエラー0。ただし旧CodeBuild経路のためOCI revisionは`unknown`、ECR scanは従来同値（Critical 4 / High 8 / Medium 3）。署名付きdigest gateへの移行完了まで追加image deployは停止。
- `/app`: 旧配信sha=`ec1b5917474b...`が現行Vault manifestとは一致する一方、最新の除外／名寄せsidecarと`build_inputs_sha256`が不一致でread-only QA gate不合格だった。`e4daa719...`＋現行Vaultから再生成し、153件の生成／QAテストと実artifact QAを全緑で通過。S3 VersionId=`I1qOb7Kwl.pMg71wqFxbHnbbTqMWjQcY`、sha256=`46f0079783cde24b066c7823b7d6672bad12b33debf933a4d7a7ff04b7a3b067`、manifest=`15663a838b1bd648443949244c02e66ccfd6cb7b684390baeb1a86efcdd6d4a2`、build inputs=`1ca6f0213155d8d4dbef4220f641dbb38310fe79473f6c013ef4e54dfa6a87e2`。clients=518 / docs=659 / internal source exposure=0 / duplicate ID・重複fingerprint・空title・空excerpt=0。#215メタタグと#217「まとめる軸」も再検証済み。`/healthz`=source=s3 / sha=`46f0079783cd`、ログイン画面のGoogleボタン表示・CSP/HSTS他security headersをブラウザ再確認。rollbackは直前VersionId=`yMIrK11unxaEJZHhQ8Qk4Ucb1ZW14yhi`（sha=`ec1b5917474b...`）。
- gsheets手動ingest: task=`483d74dfc6f448bdb72738b51bf77cf6` / td=`teamagent-dev-ingest:42` は627文書（ナレッジ239＋営業FB388）をerror=0 / exit=0で完了。DB実測は全3,994文書でexternal ID重複0・孤立chunk 0・chunk欠落0・RLS ENABLE+FORCE。社外identityのgsheets可視0 / `vectorinc.co.jp`グループは627。#240監査対象3行の業種も期待値と一致。EventBridgeは morning-digest=ENABLED / ingest週次=DISABLED / canary=DISABLEDを維持。
- 実行者=Codex（s-komata AWSアカウント）。

## 2026-07-16 🚀 dev 全8本（#213〜#221）を本番で有効化＋/app更新
- source=`dev`@`7ecf725bd19750001ae878877220b11bf1bb7a66` / image tag=`dev-7ecf725` / digest=`sha256:fb44f7cdb19c7f683768fe074aa85ba3a99fdefe7b6c9e49422e46055bb458b5`。mcp=`:55`、connect-web=`:48`、ingest=`:41`、morning-digest=`:44`、canary=`:13` を同digestへ統一。openclaw=`:25` は変更なし。
- #213/#220/#221 の短縮URL・研究成果配信、#214/#215 のRLS/共有ACL、#216界隈分類、#217グラフまとめ軸、#218 Markdown injection、#219ルールブック修正を反映。`/r` 302→S3 200、RLSは社員から可・社外0、主要サービスhealthy・重大ログ0を確認。
- rollback=mcp`:54` / connect-web`:47` / ingest`:40` / morning`:43` / canary`:12`、appは当時の直前S3 VersionId=`ejA5axqKaKNCU7hf61Gis6vPxOLv6oms`。実行者=Codex（s-komata AWSアカウント）。

---

## ⚠️ いま本番で起きている重要な不整合（初版で判明）
1. **Python サービス群が 3 つの別 image ビルドで動いている**（下表）。`teamagent-mcp` image を connect-web/ingest/canary も共用するが、**サービスごとに別バージョン**になっている。
2. **mcp は dev でなく feature 枝から焼かれている**（タグが `search-*` / `mailrich-*` / `nlk-*`＝feature 名）。`dev-*` タグの mcp は存在しない＝**dev は本番の正ではない**。
3. **dev に無いコードが本番に居る**（例：`knowledge_deliver` は dev 未マージ＝PR #145 系だが、本番 mcp `nlk-*` はそれを含む）。
4. openclaw だけ dev 基点（`dev-20260622-resilience`）だが **06-22 と古い**（dev 最新や未マージ PR は未反映）。

→ 是正方針：**次回から mcp も dev から焼く**（`dev-*` タグに統一）。本番依存の feature 枝（#145 等）を dev へマージして「dev＝本番」に寄せる。

## 出所タグの読み方
- `dev-YYYYMMDD-<label>` … dev 基点ビルド（正しい形）。
- `search-*` / `mailrich-*` / `nlk-*` … **feature 枝ビルド**（暫定。dev へ寄せるべき）。
  - `search-*`=feat/search-filters-phase1、`nlk-*`=nl-knowledge（#145系・自然言語ナレッジ）、`mailrich-*`=mail 下書き濃度向上。

---

## 現状スナップショット（2026-06-25 ~17:4x JST・実機直読み）

| service | taskdef rev | image | digest(先頭) | 出所(推定) | 概要 |
|---|---|---|---|---|---|
| **mcp**（ツール実体） | 32 | `teamagent-mcp:nlk-20260625-173320` | `5c46b5fe` | nl-knowledge（#145系） | 当日17:33 再デプロイ。knowledge_deliver/検索フィルタ込み |
| **openclaw**（頭脳） | 21 | `teamagent-openclaw:dev-20260622-resilience` | `50e8b536` | dev(06-22) | 再発防止/dmopen/SOUL。dev最新・未マージPR未反映 |
| **connect-web** | 16 | `teamagent-mcp:search-1782295456` | `05be4a78` | feat/search-filters | OAuth公開。mcp image を旧版で共用 |
| **ingest** | 27 | `teamagent-mcp:search-1782295456` | `05be4a78` | feat/search-filters | 週次/一括取込。旧版共用 |
| **morning-digest** | 28 | `teamagent-mcp:mailrich-1782372476` | `f7ab51ed` | mail-draft-richer | 朝ダイジェスト。mail濃度向上ビルド |
| **canary** | 7 | `teamagent-mcp:search-1782295456` | `05be4a78` | feat/search-filters | 合成カナリア。旧版共用 |

### 本番 mcp で ON のツール flag（rev32・参考）
`USE_TIKTOK_TOOLS` `USE_VIDEO_TOOLS` `USE_KNOWLEDGE_DELIVER` `USE_KNOWLEDGE_FILTERS`
`USE_MAIL_LINK_TOOL` `USE_MAIL_REPLY_TOOL` `USE_MAIL_SUMMARY_TOOL` `USE_FOLLOWUP_TOOL`
`USE_MORNING_DIGEST_TOOL` `USE_OAUTH_CONNECT_TOOL` `USE_COHERE_RERANK` `USE_NEW_SCHEMA`
（= search / clientkarte / proposal_draft / proposal_review に加え上記が露出。`video_approval`/`tiktok_acquire` は未登録＝未稼働）

---

## デプロイ履歴（新しい順・1デプロイ1行で追記）

| 日時(JST) | service | image tag | digest先頭 | 出所branch/commit | 概要 | 実行者 |
|---|---|---|---|---|---|---|
| 2026-06-25 17:33 | mcp | nlk-20260625-173320 | 5c46b5fe | nl-knowledge(#145系) | 自然言語ナレッジ/knowledge_deliver | 不明(実機再構成) |
| 2026-06-25 16:31 | morning-digest | mailrich-1782372476 | f7ab51ed | mail-draft-richer | メール下書き濃度向上 | 不明(実機再構成) |
| 2026-06-24 19:07 | mcp(→現 connect-web/ingest/canary) | search-1782295456 | 05be4a78 | feat/search-filters-phase1 | 検索フィルタ/ナレッジ | 不明(実機再構成) |
| 2026-06-23 08:08 | openclaw | dev-20260622-resilience | 50e8b536 | dev(resilience build) | 再発防止/dmopen/SOUL | 不明(実機再構成) |

> 以降のデプロイは**この表の先頭に1行追記**すること（CLAUDE.md §10 E1 の4段ゲートを通したら必ず記録）。
