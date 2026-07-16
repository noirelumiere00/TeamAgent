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
