# TeamAgent v3.2 マスター ToDo（Sprint 1 残 〜 本番運用 2026-12-28）

**作成日**: 2026-05-22 Day 2 完了時点
**ターゲット本番運用**: 2026-12-28（Sprint 14 末）

> ⚠️ **本ドキュメントの位置づけ**
> - Sprint 1 Day 2 完了時点の **本番運用までの全タスク**を 1 ファイルに集約
> - 「どこまで動いている / どこから人手が必要か」を明示
> - 個別 Sprint 詳細は `docs/v3.2/teamagent_implementation_plan_v3.2_draft.md` 参照

---

## 0. 凡例

| アイコン | 意味 |
|---|---|
| 🤖 | Claude（私）が実装可能 — コード生成、テスト、デプロイ、ドキュメント |
| 👤 | ユーザー（小俣さん）の手動操作が必要 — 管理画面、判断、承認、レビュー |
| 🏢 | IT / 経営 / 外部承認が必要 — プロキシ許可、IT 申請、コンプラ確認 |
| 📊 | ベータユーザー（営業）の協力が必要 — ドッグフード、FB 提供 |
| ✅ / 🟡 / 🔴 | 完了 / 進行中 / 未着手 |

---

## 1. ✅ 完了済（Day 2 = 2026-05-22）— PR #1〜#22

### インフラ・運用基盤
| | タスク |
|---|---|
| ✅ | AWS Bedrock 接続（us-east-1, Sonnet 4.6 + Haiku 4.5、Anthropic Use Case フォーム承認） |
| ✅ | Terraform apply（東京、23 リソース、RDS PG 16.14 + pgvector 0.8.2） |
| ✅ | 踏み台 EC2 SSM 接続確立（i-04fd1f367b454f641） |
| ✅ | Secrets Manager にトークン保管（db / slack 各種） |
| ✅ | AWS Budgets 設定（Bedrock $50, Server $267） |
| ✅ | tfstate S3 + DynamoDB バックエンド |

### コード基盤（pytest 33 件 / mypy --strict 18 source files）
| | タスク |
|---|---|
| ✅ | 3層分離パッケージ（adapters / skills / runtime / prompts） |
| ✅ | BedrockClient / PgVectorClient / SlackClient / LocalE5Embedder / GeminiClient（雛形） |
| ✅ | SearchSkill + SearchInput/Output Pydantic スキーマ |
| ✅ | SkillRouter（ルールベース、meta/conditional/compare/content 判定） |
| ✅ | SkillDispatcher（Slack mention → SearchSkill） |
| ✅ | Block Kit 整形（参考資料 + Drive リンクボタン） |
| ✅ | pre-commit hook 設定（.pre-commit-config.yaml） |

### 検索 Skill
| | タスク |
|---|---|
| ✅ | LocalE5Embedder（multilingual-e5-large、1024 次元） |
| ✅ | Contextual Retrieval 実装（INPEX +3.69 score 改善） |
| ✅ | メタデータ抽出（industry / client / target / pitch_axis 等 15 軸） |
| ✅ | filter_industry 実動作（INPEX → エネルギー / 森ビル → 不動産） |
| ✅ | PDF 取り込みパイプライン自動化（ingest_pdfs.py） |
| ✅ | 本番 RDS（東京）に 98 chunks 移行 |

### Slack
| | タスク |
|---|---|
| ✅ | Slack App 作成（TeamAgent Ver.2, App ID A0B51FGQ8JK） |
| ✅ | 17 OAuth scopes 取得 + Event Subscriptions（app_mention, message.im） |
| ✅ | Socket Mode 起動、xapp- トークンローテーション済 |
| ✅ | Slack 実機 E2E 疎通成功（mention → 引用付き回答、$0.01-0.02 / クエリ） |

### Drive リンク Phase 1
| | タスク |
|---|---|
| ✅ | proposals_chunks_contextual.drive_url 列追加 |
| ✅ | proposal_drive_map.json + update_drive_urls.py |
| ✅ | Slack 返信に「📎 Drive で開く」ボタン Block Kit 表示 |
| 🔴 | **実 Drive URL の取得・差し替えはまだ**（プレースホルダのまま） |

### ドキュメント
| | タスク |
|---|---|
| ✅ | v3.1 訂正ノート v0.3（OpenClaw 実証データ、AWS 公式テンプレ発見） |
| ✅ | v3.2 設計ドラフト 3 ファイル（overview / migration / implementation） |
| ✅ | CLAUDE.md 6-bis（AI エージェント実装ルール） |
| ✅ | Memory 整備（プロジェクト / AWS / リポジトリ / Agent 確認ルール） |

---

## 2. 🔴 Sprint 1 残 〜 Sprint 2 末（5/23 〜 6/12）— 53 タスク

> **🎯 マイルストーン**: Sprint 2 末（**2026-06-07**）の **Go/No-Go ゲート①**：OpenClaw 採用 vs 自前構成継続を確定

### 2.1 Drive リンク Phase 1 完成（**最優先、今すぐ動けるもの**）

| | タスク | 担当 | 工数 |
|---|---|---|---|
| 🔴 | Drive 3 PDF をベクトル社共有ドライブにアップロード（既にあればスキップ） | 👤 | 10分 |
| 🔴 | 各 PDF の webViewLink を取得（共有 → リンクを取得） | 👤 | 5分 |
| 🔴 | data/proposal_drive_map.json の PLACEHOLDER を実 URL に差し替え | 🤖 | 5分 |
| 🔴 | update_drive_urls.py を本番 RDS に対して実行（SSM tunnel 経由） | 🤖 | 10分 |
| 🔴 | Slack 実機で「📎 Drive で開く」ボタン遷移確認 | 👤 | 5分 |

### 2.2 ~~Slack トークン完全ローテーション~~（削除）

> このセクションは v1.1 で削除されました。
> xoxb- 露出はチャット内のみ（外部漏洩リスクなし）と判断、ローテーションは Sprint 14 の
> 定期 180 日サイクル（`docs/v3.2/ops/secrets_rotation_policy.md`）で実施。

### 2.3 本番 RDS への運用切り替え

| | タスク | 担当 | 工数 |
|---|---|---|---|
| ✅ | .env.production 雛形作成 + Secrets Manager 化（Day 3: `.env.production.template` + `scripts/load_secrets.sh`） | 🤖 | 20分 |
| 🔴 | Bot を本番 RDS 接続に切り替え（DATABASE_URL 差し替え）+ E2E 再疎通 | 🤖+👤 | 30分 |
| ✅ | Bedrock invocation logging を S3 + KMS で有効化（Day 3: `security.tf`、apply 待ち） | 🤖 | 20分 |

### 2.4 OpenClaw PoC + Go/No-Go ゲート①

| | タスク | 担当 | 工数 |
|---|---|---|---|
| 🔴 | OpenClaw 子会社ヒアリング回答受領（5/22 メール送信済） | 🏢 | 待ち |
| 🔴 | ヒアリング結果を docs に反映 | 🤖 | 30分 |
| 🔴 | OpenClaw PoC：aws-samples CFN を ap-northeast-1 で deploy | 🤖 | 45分 |
| 🔴 | OpenClaw Hello World Skill 動作確認 | 🤖 | 30分 |
| 🔴 | OpenClaw + Bedrock 経由 Skill サンプル動作 | 🤖 | 60分 |
| 🔴 | PoC 結果サマリ（性能/運用/セキュリティ比較表） | 🤖 | 30分 |
| 🔴 | **Go/No-Go ゲート①判定**：B 案 vs D 案 | 👤+🤖 | 30分 |
| 🔴 | 判定結果を v3.2 ドラフトに最終確定 | 🤖 | 20分 |

### 2.5 IT / 経営申請（リードタイム長）

| | タスク | 担当 | 工数 |
|---|---|---|---|
| 🔴 | 会社 Mac の sudo 権限 or 別端末調達 | 👤+🏢 | 1-3日 |
| 🔴 | Bedrock / Drive API のプロキシ許可リスト追加 | 👤+🏢 | 1-3日 |
| 🔴 | Slack chat:write.public 利用ポリシー社内確認 | 👤+🏢 | 1-3日 |
| 🔴 | 本番 RDS 接続元 IP / SSO 連携要否確認 | 👤+🏢 | 1-3日 |

### 2.6 観測・運用基盤

| | タスク | 担当 | 工数 |
|---|---|---|---|
| ✅ | CloudWatch Logs Insights 用クエリ集を docs/v3.2/ops/ に保存（PR #28） | 🤖 | 20分 |
| ✅ | CloudWatch メトリクスフィルタ（cost_usd / latency_ms / error_count）（Day 3: `cloudwatch.tf`） | 🤖 | 30分 |
| ✅ | CloudWatch アラーム（日次コスト > $5、p95 latency > 15s、5xx 連続 3 件）（Day 3: `cloudwatch.tf`） | 🤖 | 30分 |
| 🔴 | Sentry プロジェクト作成 + DSN を Secrets Manager に保管（Bot 側受け入れ実装は Day 3 完了、👤 はプロジェクト作成 + DSN 投入のみ） | 👤 | 20分 |
| ✅ | runtime/slack_bot.py に Sentry SDK 組込（PII scrubber 有効化）（Day 3: `observability/sentry.py` + `@app.error` + `loop.set_exception_handler`） | 🤖 | 30分 |
| 🔴 | AWS Budgets に Slack 通知（Chatbot 経由）追加（SNS Topic は Day 3 で作成済、Chatbot 連携待ち） | 🤖 | 30分 |
| ✅ | GitHub Actions CI：pytest + mypy + ruff + bandit 整備（PR #24） | 🤖 | 60分 |

### 2.7 セキュリティ

| | タスク | 担当 | 工数 |
|---|---|---|---|
| ✅ | Secrets Manager rotation marker secret + 期日タグ（Day 3: `security.tf`、自動化 Lambda は Sprint 14） | 🤖 | 20分 |
| ✅ | RDS 強制 SSL + IAM auth on 確認（Day 3: `rds.tf` parameter group に `rds.force_ssl=1` + `iam_database_authentication_enabled=true`） | 🤖 | 15分 |
| ✅ | S3 raw bucket Public Access Block + 暗号化確認（既存 `lambda_iam.tf` で AES256 + PAB 済、CloudTrail/Bedrock logs は KMS） | 🤖 | 15分 |
| ✅ | IAM Access Analyzer 有効化（Day 3: `security.tf` `aws_accessanalyzer_analyzer.account`） | 🤖 | 20分 |
| ✅ | CloudTrail multi-region + log file validation 有効化（Day 3: `security.tf` `aws_cloudtrail.main`） | 🤖 | 15分 |
| ✅ | ログから PII 漏洩スキャン（Day 3: `scripts/pii_log_scan.py`、xoxb-/sk-ant-/AKIA/メール/長文/顧客名検出） | 🤖 | 30分 |
| ✅ | pre-commit hook：gitleaks 追加（PR #24） | 🤖 | 30分 |

### 2.8 機能改善（並行）

| | タスク | 担当 | 工数 |
|---|---|---|---|
| ✅ | Query Router を Haiku ベース判定に置き換え（PR #27 / USE_LLM_ROUTER） | 🤖 | 90分 |
| 🔴 | filter_industry を Slack スラッシュコマンドで受け取る | 🤖 | 30分 |
| ✅ | 引用フォーマット強化（出典 + ページ + 類似度）（Day 3: `SearchHitOut` + Block Kit `📄 *file* (p.N)`） | 🤖 | 30分 |
| ✅ | prompt caching を system prompt に適用（PR #26） | 🤖 | 45分 |

### 2.9 営業ベータ準備（Sprint 2 末）

| | タスク | 担当 | 工数 |
|---|---|---|---|
| 🔴 | 営業 2 名ベータ用チャネル #teamagent-beta 作成 + Bot invite | 👤 | 10分 |
| 🔴 | ベータテスト依頼文 + 評価フォーム（5 件クエリ + 満足度） | 🤖+👤 | 30分 |
| 🔴 | ベータテスト実施 + 結果ログ集計 | 👤+🤖+📊 | 60分 |
| 🔴 | FB を docs/v3.2/feedback_sprint2.md に整理 | 🤖 | 45分 |

**Sprint 1 残〜Sprint 2 末 工数小計**: 🤖 約 12h / 👤 約 3h / 🏢 1-3日待ち / 📊 1h

---

## 3. 🔴 Sprint 3〜6（6/13 〜 8/7）MVA 完成 — 50 タスク

> **🎯 マイルストーン**: M3 末（**2026-08-07**）MVA 完成、営業 16 名展開準備完了

### Sprint 3（6/13 〜 6/26）pgvector 完成 & Gmail/Drive コネクタ

| # | タスク | 担当 | 工数 |
|---|---|---|---|
| S3-01 | GCP プロジェクト作成・OAuth 同意画面設定（Internal） | 👤 | 2h | 🔴 |
| S3-02 | Drive OAuth クライアント申請（**drive.file + drive.metadata.readonly = CASA 不要**、v1.5 で方針更新） | 👤+🏢 | 3h | 🔴 |
| S3-03 | ~~Drive API 用 IT 承認取得（DWD）~~ → **個人 OAuth 推奨で DWD 不要に**（v1.5） | 🏢 | -- | 🟢 不要化 |
| S3-04 | ベクトル社 営業 Drive フォルダ構造調査 | 👤 | 3h | 🔴 |
| S3-05 | adapters/gdrive_client.py **雛形**（PR #36 完了、本実装は credentials 取得後） | 🤖 | 6h | ✅ 雛形 |
| S3-06 | Drive 差分監視（changes.list、EventBridge cron は Sprint 4） | 🤖 | 5h | 🟡 interface 完了 |
| S3-07 | ingest_pdfs.py 拡張：Drive 取り込み統合（PR-6 で実施） | 🤖 | 4h | 🔴 |
| S3-08 | Drive メタデータ → pgvector の source_uri / owner / modified_at 反映 | 🤖 | 3h | 🟡 スキーマ準備済 |
| S3-09 | Gmail OAuth クライアント申請（**gmail.modify 1 本で読み + 下書き、CASA 不要**、v1.5 確定） | 👤+🏢 | 3h | 🔴 |
| S3-10 | ~~Gmail DWD 要否判断~~ → **個人 OAuth で十分**（v1.5） | 👤 | -- | 🟢 不要化 |
| S3-11 | adapters/gmail_client.py **雛形**（PR #37 完了、隠しラベル管理含む） | 🤖 | 7h | ✅ 雛形 |
| S3-12 | Gmail 取り込みパイプライン | 🤖 | 5h | 🔴 |
| S3-13 | Slack チャネル取り込みコネクタ **雛形**（PR #39 完了） | 🤖 | 5h | ✅ 雛形 |
| S3-14 | pgvector スキーマ最終化（**source_type ENUM + ACL + RLS、本番 RDS 適用済**、PR #35 + #38） | 🤖 | 3h | ✅ 完了 |
| S3-15 | **追加：gsheets_client.py 雛形**（ユーザー貴重情報源対応、PR #40） | 🤖 | 3h | ✅ 雛形 |
| S3-16 | **追加：data/ingest_sources.yaml + ingest dispatcher (PR-6)** | 🤖 | 4h | 🔴 次回 |

### Sprint 4（6/27 〜 7/10）検索 Skill 本番化 & 全 PDF 投入

| # | タスク | 担当 | 工数 |
|---|---|---|---|
| S4-01 | 社内提案 PDF 全件棚卸し（件数確定、機密区分仕分け） | 👤 | 4h |
| S4-02 | 全 PDF 一括投入（バッチ実行、想定 200〜500 ファイル） | 🤖 | 6h |
| S4-03 | メタデータ抽出パイプライン定期実行化（EventBridge 日次） | 🤖 | 3h |
| S4-04 | Bedrock Titan Embed v2 PoC（LocalE5 と精度比較） | 🤖 | 6h |
| S4-05 | Embedder 切り替え判断 | 👤 | 1h |
| S4-06 | EC2 本番環境構築（t3.medium、Docker、systemd） | 🤖+👤 | 5h |
| S4-07 | Slack Bot 本番デプロイ（Socket Mode → HTTP 検討） | 🤖 | 4h |
| S4-08 | 本番 RDS 接続切替 + Secrets Manager 連携 | 🤖+👤 | 3h |
| S4-09 | CloudWatch Logs / メトリクス整備 | 🤖 | 3h |
| S4-10 | Slack DM ベータユーザー allowlist 設定 | 🤖+👤 | 2h |
| S4-11 | App Home タブ実装（おすすめクエリ、検索履歴） | 🤖 | 6h |
| S4-12 | SkillRouter の Gmail/Drive 拡張（source 指定構文） | 🤖 | 4h |

### Sprint 5（7/11 〜 7/24）営業 5 名ベータ

| # | タスク | 担当 | 工数 |
|---|---|---|---|
| S5-01 | ベータユーザー 5 名選定 + 招待 DM | 👤 | 2h |
| S5-02 | ベータ向けクイックスタート資料（Slack canvas、3 分動画） | 🤖+👤 | 4h |
| S5-03 | #teamagent-beta 開設 | 👤 | 1h |
| S5-04 | Google Form FB アンケート（週次、5 項目） | 👤 | 2h |
| S5-05 | ベータ運用（実利用、質問対応、デイリーログ確認） | 📊+🤖 | 20h |
| S5-06 | 評価データセット作成（営業実クエリ 30 件 + 正解 chunk） | 👤+🤖 | 5h |
| S5-07 | 検索精度計測（top-1 hit rate, MRR@5, 満足度） | 🤖 | 4h |
| S5-08 | Contextual Retrieval プロンプトチューニング | 🤖 | 5h |
| S5-09 | SkillRouter ルール改善（industry 推定漏れ対応） | 🤖 | 4h |
| S5-10 | バグ修正（FB 起因の即時対応枠） | 🤖 | 8h |
| S5-11 | S5 末振り返り会 + Go/No-Go 判断 | 👤+📊 | 2h |

### Sprint 6（7/25 〜 8/7）16 名展開準備 & MVA 完成

| # | タスク | 担当 | 工数 |
|---|---|---|---|
| S6-01 | 営業 16 名向けオンボーディング資料（Notion + 動画） | 🤖+👤 | 6h |
| S6-02 | 全社展開お知らせ Slack 草稿 + 経営承認 | 👤 | 2h |
| S6-03 | 運用 Runbook（OAuth token 失効、RDS 接続断、Slack rate limit） | 🤖+👤 | 5h |
| S6-04 | インシデント対応フロー（Slack alert で代替） | 🤖 | 3h |
| S6-05 | SLO 定義（応答 p95 < 5s, 可用性 99%, 精度 70%） | 👤+🤖 | 2h |
| S6-06 | SLA ドキュメント（営業時間内対応） | 👤 | 2h |
| S6-07 | セキュリティレビュー（情シス向け資料、データフロー図） | 🤖+🏢 | 6h |
| S6-08 | 社内コンプライアンス確認（個人情報、商談機密） | 🏢+👤 | 4h |
| S6-09 | バックアップ / DR 手順（RDS snapshot 日次、RTO 4h） | 🤖+👤 | 3h |
| S6-10 | コスト試算最終版（Bedrock + EC2 + RDS、16 名想定） | 🤖 | 2h |
| S6-11 | 16 名分 Slack allowlist 拡張 | 🤖 | 1h |
| S6-12 | MVA Phase 2-3 完成判定会（M3 8/7、KPI レビュー） | 👤+📊 | 2h |

**Sprint 3-6 工数小計**: 🤖 約 140h / 👤 約 50h / 🏢 約 15h / 📊 約 22h

---

## 4. 🔴 Sprint 7〜14（8/8 〜 12/28）Phase 4-5 — 35 タスク

> **🎯 マイルストーン**:
> - Sprint 10 末（**2026-10/中旬**）**Go/No-Go ゲート②**：提案書 20h → 8-12h 実証
> - Sprint 14 末（**2026-12-28**）**本格運用開始**

### Sprint 7（8/8 〜 8/21）Phase 4-a: Slack 自動サジェスト Skill

| # | タスク | 担当 | 工数 |
|---|---|---|---|
| 7.1 | Events API message.channels サブスクライブ | 🤖 | 4h |
| 7.2 | suggest_skill.py 実装（過去案件ベクトル検索 + ephemeral） | 🤖 | 16h |
| 7.3 | confidence < 0.7 確認ダイアログ Block Kit | 🤖 | 8h |
| 7.4 | サジェスト ON/OFF ユーザー設定 DynamoDB | 🤖 | 4h |
| 7.5 | Slack スコープ追加申請（channels:history） | 👤 | 1h |
| 7.6 | 営業 3 名でドッグフード（1 週間） | 📊 | 6h |

### Sprint 8（8/22 〜 9/4）Phase 4-b: Mail ワークフロー Skill

| # | タスク | 担当 | 工数 |
|---|---|---|---|
| 8.1 | Gmail OAuth スコープ拡張（gmail.readonly） | 👤 | 2h |
| 8.2 | 朝 8:30 EventBridge cron + 当日メール要約 Lambda | 🤖 | 12h |
| 8.3 | Slack DND status 確認 → スキップ分岐 | 🤖 | 4h |
| 8.4 | メール × Slack 統合分析（thread_ts 紐付け） | 🤖 | 10h |
| 8.5 | 個別メール選択 UI（Block Kit overflow menu） | 🤖 | 6h |

### Sprint 9-10（9/5 〜 10/2）Phase 4-c: 提案コンテンツ生成 Skill

| # | タスク | 担当 | 工数 |
|---|---|---|---|
| 9.1 | 5 フェーズ生成パイプライン（要件→構成→本文→図表→校閲） | 🤖 | 24h |
| 9.2 | Playwright + WeasyPrint ローカル動作確認 | 👤 | 3h |
| 9.3 | HTML → PDF/PPTX 変換 Lambda Layer | 🤖 | 12h |
| 9.4 | ChromeOS / Edge Runtime での HTML 互換性検証 | 🤖 | 6h |
| 9.5 | Google Drive Service Account 権限委譲 | 👤 | 2h |
| 9.6 | Drive 保存 → Slack DM リンク返却フロー | 🤖 | 8h |
| 10.1 | **Go/No-Go ゲート②**：営業 5 名 × 提案書 3 件で工数測定 | 📊 | 30h |
| 10.2 | 20h → 8-12h 実証レポート + 経営判断 | 👤 | 4h |

### Sprint 11（10/3 〜 10/16）Phase 4-d: 動画ナレッジ分析 Skill

| # | タスク | 担当 | 工数 |
|---|---|---|---|
| 11.1 | Google AI Studio で Gemini API キー取得 | 👤 | 1h |
| 11.2 | GEMINI_API_KEY を AWS Secrets Manager に登録 | 👤 | 1h |
| 11.3 | adapters/gemini_client.py 本実装（2.5 Flash, 動画 inline） | 🤖 | 16h |
| 11.4 | yt-dlp ローカル動作確認 + Lambda Layer 化（FFmpeg 同梱） | 👤+🤖 | 8h |
| 11.5 | YouTube/TikTok/Instagram URL パーサ + 構造分析 prompt | 🤖 | 12h |
| 11.6 | 著作権ガード（DL せず stream URL 経由） + 規約レビュー | 🏢 | 4h |

### Sprint 12（10/17 〜 11/13）Phase 4-e: 営業進捗サマリー Skill

| # | タスク | 担当 | 工数 |
|---|---|---|---|
| 12.1 | Slack User Group @managers 作成 + IAM 権限マッピング | 👤 | 2h |
| 12.2 | 月曜 9:00 週間サマリー生成 Lambda（Salesforce 連携） | 🤖 | 16h |
| 12.3 | マネージャー権限チェック middleware | 🤖 | 6h |
| 12.4 | サマリー Block Kit テンプレート（KPI / リスク案件） | 🤖 | 8h |

### Sprint 13（11/14 〜 11/27）QA + 負荷試験

| # | タスク | 担当 | 工数 |
|---|---|---|---|
| 13.1 | 統合テスト（5 Skill × 正常/異常系 各 10 シナリオ） | 🤖 | 16h |
| 13.2 | 負荷試験（16 名同時 × 30 req/min × 1h、Locust） | 🤖 | 8h |
| 13.3 | 朝 8:30 スパイク試験（16 並列 DM） | 🤖 | 4h |
| 13.4 | Bedrock コスト試算（実利用予測） | 🤖 | 4h |
| 13.5 | E2E リグレッション（GitHub Actions matrix） | 🤖 | 8h |

### Sprint 14（11/28 〜 12/28）本番化 + 監視

| # | タスク | 担当 | 工数 |
|---|---|---|---|
| 14.1 | セキュリティ監査: PII 検出（Macie）/ ログマスキング | 🏢 | 8h |
| 14.2 | セキュリティ監査: token rotation（Slack/Gmail/Gemini） | 👤 | 4h |
| 14.3 | セキュリティ監査: CVE スキャン（Snyk + Dependabot） | 🤖 | 4h |
| 14.4 | Sentry エラートラッキング統合 | 🤖 | 4h |
| 14.5 | DataDog APM + CloudWatch カスタムメトリクス | 🤖 | 8h |
| 14.6 | AWS Budgets 80% 通知整備 | 👤 | 2h |
| 14.7 | RDS 自動スナップショット 7 日保持 + S3 クロスリージョン | 🤖 | 4h |
| 14.8 | DR 計画（東京 → us-east-1 RTO 4h / RPO 1h） | 🤖+🏢 | 8h |
| 14.9 | Runbook 作成（インシデント / SLO 違反 / cost spike） | 🤖 | 8h |
| 14.10 | インシデント対応訓練（模擬障害 2 シナリオ） | 👤+📊 | 4h |
| 14.11 | **本番リリース**（営業 16 名展開）+ 経営報告 | 👤 | 4h |
| 14.12 | 継続改善ルーチン（週次 KPI、月次 retro） | 👤 | 2h |

**Sprint 7-14 工数小計**: 🤖 約 268h / 👤 約 32h / 🏢 約 12h / 📊 約 40h

---

## 5. 📊 全体工数サマリ

| 担当 | Sprint 1-2 | Sprint 3-6 | Sprint 7-14 | **合計** |
|---|---|---|---|---|
| 🤖 Claude 実装 | 12h | 140h | 268h | **420h** |
| 👤 ユーザー手動 | 3h | 50h | 32h | **85h** |
| 🏢 IT / 経営 / 外部 | 1-3日 × 4件 | 15h | 12h | **30h相当** |
| 📊 ベータユーザー | 1h | 22h | 40h | **63h** |

> 🤖 の工数は私（Claude）が並列で進められれば物理時間は半分以下。
> 👤 + 🏢 がボトルネックになるので、**承認 / IT 申請を Sprint 2 までに片付ける**のが鍵。

---

## 6. 🚨 今すぐブロックされている主要タスク

| ブロッカー | 待ち項目 | 期限目安 |
|---|---|---|
| 子会社からの返信 | OpenClaw 運用実績ヒアリング | Sprint 2 末（6/7） |
| Drive 実 URL | 3 PDF の webViewLink | 今日中 〜 Sprint 2 |
| GCP プロジェクト | Drive API OAuth 同意画面 | Sprint 3 開始時 |
| 営業 PDF 棚卸し | 何件 / どこに保管 / 機密区分 | Sprint 3 末 |
| IT 申請 4 件 | sudo / プロキシ / Slack ポリシー / RDS IP | Sprint 2 末 |
| Salesforce 連携可否 | 進捗サマリー Skill のため | Sprint 12 開始時 |

---

## 7. ✅ どこまで「動いている」のか

### 今すぐ動くもの
- Slack で `@TeamAgent_Dev_Ver.2 INPEX案件は？` → Contextual + filter + 引用付き回答（**$0.01-0.02 / クエリ**）
- 「📎 Drive で開く」ボタン表示（**ただしリンク先は PLACEHOLDER**）
- メタデータフィルタ（industry='エネルギー' → INPEX のみ）
- mention / DM 両方

### 動いていない（次の TODO）
- ❌ Drive リンクは実 URL じゃない（プレースホルダ）
- ❌ Drive 自動取り込み（手動マッピング）
- ❌ Gmail 連携（未着手）
- ❌ Slack チャネル履歴取り込み（未着手）
- ❌ Skill ②〜⑤（自動サジェスト / Mail ワークフロー / 提案生成 / 動画分析 / 進捗サマリー）
- ❌ 本番 EC2 デプロイ（ローカル Mac で動作中）
- ❌ 本番 RDS 接続切替（Bot はローカル DB 参照中）

---

## 8. 📅 次に踏み出す具体的な一歩

優先度順：

1. **🟡 5/29 までに**：IT 申請 4 件を投げる
2. **🟡 Sprint 3 で実施**：3 PDF の Drive URL は Google Drive API 連携の中で自動取得
3. **🟡 5/30 〜**：OpenClaw PoC で Sprint 2 末ゲート①の判定材料準備
4. **🔵 子会社返信待ち**：ヒアリング結果が来たら docs に反映
5. **🟡 Sprint 14 で実施**：Slack トークン定期ローテーション（180 日サイクル）

---

## 更新履歴

| 日付 | バージョン | 更新内容 |
|---|---|---|
| 2026-05-22 | v1.0 | 初版（Day 2 完了時点で 3 Agent 並列調査の結果を統合） |
| 2026-05-22 夜 | v1.1 | PR #15〜#28 マージ後の追記。Sprint 1 残タスクから完了分を削除。 |
| 2026-05-22 深夜 | v1.2 | Slack xoxb- ローテ TODO を削除（外部漏洩リスクなしと判断、Sprint 14 の定期サイクルで実施）。Drive URL 手動取得タスクも Sprint 3 自動連携に統合。 |
| 2026-05-23 | v1.3 | Day 3 着手分を反映。2.3（.env.production 雛形 + load_secrets.sh）/ 2.6（CloudWatch メトリクスフィルタ + アラーム）/ 2.7（CloudTrail + IAM Access Analyzer + Bedrock invocation logging + RDS force_ssl + PII スキャン）/ 2.8（引用フォーマット強化）を完了。Terraform 8 リソース追加、テスト 40 → 43、運用ドキュメント 1 本追加。 |
| 2026-05-23 夕方 | v1.4 | Sentry SDK 組込完了。`src/teamagent/observability/sentry.py`（DSN 空で no-op / before_send で xoxb-/sk-ant-/AKIA/メール/長文を再帰スクラブ / request_id を tag 昇格）+ `@app.error` ハンドラ + `loop.set_exception_handler` で Bolt AsyncApp の例外を二重キャッチ。Sentry Python SDK 2.60.0 採用、テスト 43 → 69（observability 25 件 + slack_bot 1 件追加）。残るは👤による Sentry プロジェクト作成 + DSN 投入のみ。 |
| 2026-05-26 | v1.5 | **Day 4 大進捗：1 日で 7 PR / +5,500 行**。Sentry 本番動作確認（vectorinc.sentry.io）→ 本番 RDS E2E 動作確認（INPEX クエリ実機成功、score=0.89）→ PR #34 テンプレ修正 → PR #35 統合 documents/chunks スキーマ + RLS → PR #36/#37 gdrive/gmail 雛形 → PR #38 RLS hotfix（teamagent_app role 分離、本番 RLS 13/13 検証 PASS）→ PR #39 Slack ingest + 取り込みソース宣言（ユーザー貴重情報源 2 セット） → PR #40 gsheets adapter。本番 RDS に migration 0001 + 0002 適用済。pytest 53→147、mypy strict 18→24 source files。Sprint 3 着手の足場完成。次は PR-6 (ingest dispatcher) と GCP OAuth 取得。 |

---

# 📌 v1.1 追加分（2026-05-22 夜、PR #15〜#28）

## 追加で完了したもの

### 検索 Skill / 機能拡張
- ✅ **PR #16** Contextual Retrieval 実装（INPEX クエリ +3.69 改善）
- ✅ **PR #17** USE_CONTEXTUAL 環境変数で切替対応
- ✅ **PR #18** Drive リンク Phase 1（Block Kit ボタン、proposal_drive_map.json）
- ✅ **PR #19** メタデータ抽出 + filter_industry 実動作
- ✅ **PR #21** 5 機能一括（PDF パイプライン自動化 / pre-commit / Router 雛形 / Gemini 雛形 / 文脈設計）
- ✅ **PR #22** 本番 RDS（東京）migration 完了（98 chunks）
- ✅ **PR #26** Prompt Caching 適用（コスト 27% 削減見込み）
- ✅ **PR #27** SkillRouter Haiku 4.5 ハイブリッド対応（USE_LLM_ROUTER）

### CI / セキュリティ
- ✅ **PR #24** GitHub Actions CI（ruff + format + mypy + pytest + bandit）+ gitleaks
- ✅ **PR #25** CI mypy type-stub overrides 修正で緑化
- ✅ **PR #28** Dependabot（pip / github-actions、週次月曜 09:00 JST）

### ドキュメント / 運用基盤
- ✅ **PR #15** v3.2 設計ドラフト 3 ファイル（overview / migration / implementation）
- ✅ **PR #20** Day 2 総括追記
- ✅ **PR #23** マスター ToDo v1.0（138 タスク統合）
- ✅ **PR #24** Secrets ローテーションポリシー文書化（Sprint 14 で Lambda 自動化予定）
- ✅ **PR #28** CloudWatch Logs Insights クエリ集（10 個、Sprint 4 で運用開始）

## 数値の更新

| 指標 | v1.0（朝） | v1.1（夜） |
|---|---|---|
| マージ済 PR | #1〜#22 | **#1〜#28** |
| pytest | 24 → 27 | **40** |
| mypy strict source files | 16 → 17 | **18** |
| 静的解析 | mypy + pytest のみ | **+ ruff + ruff format + bandit + gitleaks** |
| 検索精度（INPEX） | top-1 0.85 | **0.89（Contextual）** |
| 想定月額コスト | $159 | **$116（caching 込）** |
| AWS リソース | 23 | **23 + 本番 RDS 98 chunks** |

## v1.1 時点でブロック中

| ブロッカー | 状態 |
|---|---|
| 子会社からの返信 | メール送信済（5/22）、Sprint 2 末まで待ち |
| 3 PDF の実 Drive URL | **未取得**（プレースホルダのまま、5 分作業） |
| GCP プロジェクト作成 | Sprint 3 開始時 |
| IT 申請 4 件 | Sprint 2 末まで投げる必要 |
| Salesforce 連携可否 | Sprint 12 開始時 |

## v1.1 時点で「今すぐ動く」

| クエリ例 | 動作 |
|---|---|
| `@TeamAgent INPEX案件の提案内容を教えて` | Contextual + industry=エネルギー auto-filter + 引用 + Drive ボタン |
| `@TeamAgent 飲食事例を3つ` | 業界キーワード検出 + filter_industry='飲食' |
| `@TeamAgent ベクトル社のサービスについて` | LLM Router fallback（USE_LLM_ROUTER=true 時） |

## 次に踏み出す具体的な一歩（v1.1 更新版）

優先度順：

1. **🟢 今すぐ（5 分）**：3 PDF の Drive URL を取得 → `data/proposal_drive_map.json` 更新
2. **🟢 今日中（10 分）**：Slack xoxb- 完全ローテーション
3. **🟡 明日**：本番 RDS 接続切替テスト（DATABASE_URL を SSM tunnel 経由に向ける）
4. **🟡 5/29 までに**：IT 申請 4 件
5. **🟡 5/30 〜**：OpenClaw PoC（aws-samples/sample-OpenClaw-on-AWS-with-Bedrock）
6. **🔵 子会社返信待ち** → 受領後 docs/v3.2/ に反映 + Sprint 2 末ゲート①判定

---

# 📌 v1.6 追加分（2026-05-26 = Day 5 完了）

## Day 5 完了タスク（全部 🤖 単独完結）

### Sprint 3 着手 PR-6 + 補完（5 PR merged）
- ✅ **PR #43** Sprint 3 PR-6: ingest dispatcher（loader + repository + pipeline + scripts/ingest_sources.py + 17 件 test）
- ✅ **PR #44** chore: ingest_sources.yaml の channel_id を実値に更新（C091ZSVTKF1, C0A1207GYHZ）
- ✅ **PR #45** feat: Slack channel メンバーを ACL に自動写像 + fail-safe skip
- ✅ **PR #46** fix(rls): chunks の INSERT/UPDATE/DELETE policy 追加 + documents の UPDATE policy（migration 0003）

### 本番 RDS データ投入完了 🎯
**2026-05-26 15:26 JST に Slack 197 documents + 197 chunks 投入成功**

| channel | documents | acl_emails/doc |
|---|---|---|
| #proj-ナレッジ共有 (C091ZSVTKF1) | 97 | 53 emails |
| #proj-ショート動画_営業フィードバック情報 (C0A1207GYHZ) | 100 | 54 emails |

## 数値の更新

| 指標 | v1.5（Day 4 末） | v1.6（Day 5 末） |
|---|---|---|
| マージ済 PR | #1〜#42 | **#1〜#46** |
| pytest | 150 | **169** |
| mypy strict source files | 24 | **28** |
| migration | 0001, 0002 | **0001, 0002, 0003** |
| 本番 RDS 投入 docs | 98 (旧 proposals_chunks) | **+197 (新 documents/chunks)** |
| Slack ingest 対象 channel | 0 | **2 (ナレッジ + 営業FB)** |
| Slack OAuth scopes | 17 | **18 (+users:read.email)** |

## 動作確認済（2026-05-26）

| 経路 | 状態 |
|---|---|
| `aws ssm start-session ... portForward` → 本番 RDS | ✅ 安定動作 |
| `python scripts/ingest_sources.py --sources slack`（dry-run）| ✅ resolved_emails 53/54 |
| `python scripts/ingest_sources.py --commit --sources slack` | ✅ 197 docs / 197 chunks INSERT、エラー 0 |
| RLS app_role=teamagent_app + SET LOCAL app.user_email | ✅ chunks INSERT policy 通過 |
| Sentry: PII スクラブ + 例外捕捉 | ✅（Day 3 から動作中）|

## v1.6 時点でブロック中（user 作業）

| ブロッカー | 担当 | 影響 |
|---|---|---|
| **GCP プロジェクト + OAuth クライアント** | 👤 | Drive folder ingest が動かない（PDF 取り込み未開始）|
| **Terraform apply（cloudwatch.tf / security.tf）** | 👤 | CloudWatch メトリクスフィルタ + アラーム未稼働、CloudTrail multi-region 未起動 |
| **AWS Chatbot Slack 通知連携** | 👤 | SNS Topic → Slack 通知の手動コンソール作業 |
| **IT 申請 4 件**（sudo / プロキシ / Slack ポリシー / RDS IP） | 👤+🏢 | 本番運用安定化 |
| **OpenClaw 子会社ヒアリング返信** | 🏢 | Sprint 2 末ゲート①判定材料 |

## 次に踏み出す具体的な一歩（v1.6）

優先度順（🤖 単独）:

1. **🟢 最優先**: SearchSkill を documents/chunks に切替（`USE_NEW_SCHEMA=true` オプション追加）
   - Slack 投入済 197 件が **検索可能になる** = ナレッジ AI の本質的価値
   - 既存 proposals_chunks_contextual 経路は USE_NEW_SCHEMA=false で温存
   - 想定: 2-3h, 1 PR
2. **🟡 続き**: migration 0004 で `gsheets` ENUM 追加 + GSheets ingest 本実装
   - Service Account のみで動く（OAuth 不要、共有設定だけ）
3. **🟡 続き**: SearchSkill response に「どの Slack thread / channel から来た」を Block Kit で表示
4. **🔵 待ち**: GCP OAuth 取得後 → Drive folder ingest
   - Vision API でスライドページ画像化 → 説明文生成 → embedding（Sprint 4 採用候補）

## モデル構成（2026-05-26 確定）

| 役割 | 採用 | 備考 |
|---|---|---|
| LLM メイン | Bedrock Sonnet 4.6 (`jp.anthropic.claude-sonnet-4-6`) | 東京推論プロファイル |
| LLM ルーター | Bedrock Haiku 4.5 (`jp.anthropic.claude-haiku-4-5-20251001-v1:0`) | USE_LLM_ROUTER=true |
| Embedder | LocalE5 (`intfloat/multilingual-e5-large`, 1024 次元) | ローカル sentence-transformers |
| DB | RDS PostgreSQL 16 + pgvector 0.8.2 | HNSW cosine + RLS |
| Framework | 自前 SkillRegistry + 3層分離 (CLAUDE.md 6-bis) | LlamaIndex/LangChain 不採用 |
| 観測 | Sentry SDK 2.60 + CloudWatch + structlog | PII スクラブ + AsyncioIntegration |

| 日付 | バージョン | 更新内容 |
|---|---|---|
| 2026-05-26 | v1.6 | Day 5 完了。Sprint 3 PR-6 完成 + 本番 RDS に Slack 197 docs 投入。migration 0003 で chunks RLS 完備。次は SearchSkill を新スキーマに切替。 |
