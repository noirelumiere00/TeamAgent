# TeamAgent v3.2 実装計画書（ドラフト）

> 作成日：2026-05-22（Sprint 1 Day 2 完了時点）
> ベース：`docs/v3.1/teamagent_implementation_plan_v3.1.html`
> 訂正反映：`docs/v3.1/teamagent_design_corrections_2026-05-22.md` v0.1 / v0.2 / v0.3
> ステータス：**Draft（ゲート① 2026-06-07 で確定）**

本ドキュメントは v3.1 実装計画書の改訂版である。Sprint 1 Day 2 までに End-to-End 疎通が成功し、OpenClaw の前提が一部破綻したことを受け、Sprint 構成・ゲート判断・コスト枠を現状に合わせて再記述する。

---

## 1. 現状サマリー（2026-05-22 Day 2 完了時点）

### 1.1 PR #1〜#14 の達成事項

`feat/v3.1-monorepo` ブランチ上で 14 件の PR がマージ済み。すべて `main` 系列に統合され、ローカル開発環境・AWS インフラ・Slack 連携・Skill 基盤・E2E 疎通までが揃っている。

| PR | 種別 | 内容 |
|---|---|---|
| [#1](https://github.com/) feat | 設計 | monorepo 化 + OpenClaw フル採用方針 + セキュリティ運用ルール初版 |
| [#2](https://github.com/) feat | 設計 | 検索 Skill 設計 v1 + pgvector デモ + Claude Code ハンドオフ |
| [#3](https://github.com/) docs | 記録 | Day 0 夕方追加作業（Bedrock hello world + AWS Budgets） |
| [#4](https://github.com/) feat | infra | Terraform AWS インフラ apply 準備（ap-northeast-1 / bastion / S3 公開ブロック） |
| [#5](https://github.com/) fix | infra | RDS param group の `shared_preload_libraries` に `apply_method=pending-reboot` |
| [#6](https://github.com/) docs | ルール | CLAUDE.md に「AI エージェント実装ルール（メンテ性最優先）」追加（3 層分離 / Pydantic v2 / mypy --strict / prompt 分離） |
| [#7](https://github.com/) fix | infra | 踏み台 IAM Role に Secrets Manager 読み取り権限追加（最小権限） |
| [#8](https://github.com/) chore | infra | PostgreSQL エンジンバージョンを 16.4 → 16.14 に統一 |
| [#9](https://github.com/) feat | code | Skill 実装基盤 bootstrap（3 層分離 / Pydantic / 構造化ログ / tests 16 件） |
| [#10](https://github.com/) feat | code | Slack Bot ランタイム + Slack adapter（Socket Mode） |
| [#11](https://github.com/) docs | 記録 | Day 1 実績を CLAUDE.md に記録（Sprint 1 P0 全完了 + echo Bot 疎通） |
| [#12](https://github.com/) feat | code | mention/DM を SearchSkill にディスパッチ（実データ検索 + Claude 要約） |
| [#13](https://github.com/) docs | 訂正 | v3.1 設計訂正ノート v0.1 + Day 2 E2E 成功 + OpenClaw を再評価中に変更 |
| [#14](https://github.com/) docs | 訂正 | 訂正ノート v0.2 + v0.3（AWS 公式テンプレ発見、OpenClaw + Bedrock 実装最短パス） |

### 1.2 動いている技術スタック

| 層 | 実装 | 検証 |
|---|---|---|
| LLM | AWS Bedrock 経由 Claude Sonnet 4.6 / Haiku 4.5（推論プロファイル `us.` プレフィックス） | hello world + 実 E2E 通過 |
| Embedder | LocalE5Embedder（multilingual-e5-large、1024 次元） | PDF / md チャンクをローカル埋め込み |
| データ層 | RDS PostgreSQL 16.14（db.t4g.micro / ap-northeast-1）+ pgvector 0.8.2 | 踏み台 SSM 経由で `CREATE EXTENSION vector` 完了 |
| 検索 Skill | `src/teamagent/skills/search/`（SkillRegistry / SkillDispatcher） | 3 連投クエリ成功、cost $0.01-0.02 / latency 7-11 秒 / top sim 0.80-0.84 |
| Slack | Bolt Socket Mode + 17 OAuth スコープ + Secrets Manager 保管 | mention + DM 実機疎通 |
| 品質ゲート | pytest 24 件 PASS / mypy --strict 16 source files クリア | CI レベルで担保 |
| コスト枠 | AWS Budgets：Bedrock $50/月 / Server $267/月（50/80/100% アラート） | 通知メール 2 件設定済 |

### 1.3 残タスク（Sprint 1 の積み残し）

| 優先度 | 項目 | 担当軸 |
|---|---|---|
| 🔴 | **OpenClaw 採否判断**（B 案 or D 案）→ ゲート① | 子会社ヒアリング + PoC |
| 🟡 | Skill 拡張：メタデータ抽出パイプライン（P1）— JSONB スキーマ確定 | Skill 設計 |
| 🟡 | Skill 拡張：Contextual Retrieval（既存 chunk への前置詞付与・再 embedding） | retrieval 精度 |
| 🟡 | 本番 RDS へのデータ取り込み（proposal_chunks 本番スキーマ確定 + 移行スクリプト） | データ層 |
| 🟢 | Slack Bot Token / App Token ローテーション（チャットに露出済） | セキュリティ |
| 🟢 | Query Router（meta / content / conditional / compare）ルールベース版 | 検索精度 |

---

## 2. Sprint 1 残り（5/23〜5/29）— Day 3〜Day 7

Sprint 1 の主目的「ローカル → AWS の最小疎通」は Day 2 で達成済みのため、残期間は **ゲート①の準備期間**と位置付ける。

### 2.1 Day 3-4（5/23〜5/24, 週末）

- **Slack トークンローテーション**：Reinstall App → 新 Bot/App Token 取得 → Secrets Manager 上書き（`teamagent/dev/slack/bot_token`, `app_token`）→ slack_bot 再起動疎通
- **子会社エンジニアへ質問リスト送付**：`docs/v3.1/teamagent_subsidiary_questions_v2.md` をメール送付（OpenClaw 120 ユーザー運用の具体内容ヒアリング）
- **本番 RDS 接続情報の整理**：踏み台 SSM 経由で本番 RDS の DSN を `.env.prod.example` に追記、ローカルからは触らない方針を明文化

### 2.2 Day 5-6（5/25〜5/26）— データスキーマ確定

- **`proposal_chunks` 本番スキーマ確定**：
  - `id BIGSERIAL PRIMARY KEY`
  - `document_id UUID NOT NULL`
  - `chunk_index INT NOT NULL`
  - `content TEXT NOT NULL`
  - `embedding vector(1024) NOT NULL`
  - `metadata JSONB NOT NULL DEFAULT '{}'`（業界 / 予算 / ターゲット / 商材 / 担当者 / 部署 / 自社サービス / 提案日）
  - `source_uri TEXT`（S3 raw path）
  - `created_at TIMESTAMPTZ DEFAULT NOW()`
  - INDEX：HNSW on `embedding` + GIN on `metadata`
- **データ取り込みスクリプト雛形**：`scripts/ingest_proposals.py`（S3 → pdfplumber → chunk → embedding → INSERT）

### 2.3 Day 7（5/27〜5/29）— メタデータ抽出 P1 着手

- Claude Sonnet 4.6 で 1 PDF → 業界・予算・ターゲット・担当者を JSON 抽出する prompt を `src/prompts/metadata_extract/v1/system.md` に作成
- 既存 `data/proposals/` 配下 3 PDF で動作確認（temperature=0.1, prompt caching ON）
- 抽出 JSON を `metadata` 列に保存する pipeline 雛形（**完成は Sprint 4 まで持ち越し**）

Sprint 1 終了基準（5/29）：
1. Slack トークン更新済
2. proposal_chunks スキーマ確定 PR マージ
3. メタデータ抽出 prompt の単発実行成功
4. 子会社ヒアリング送付済

---

## 3. Sprint 2（5/30〜6/12）— ゲート①含む

Sprint 2 の核心は **OpenClaw 採否の最終判断**。判断材料を 2 週で揃え、6/12 ゲート①で B 案 / D 案を確定する。

### 3.1 W1（5/30〜6/5）— OpenClaw PoC

訂正ノート v0.3 で発見した AWS 公式テンプレートを最短パスとして使う。

- **CloudFormation apply**：`aws-samples/sample-OpenClaw-on-AWS-with-Bedrock` を **ap-northeast-1** で apply
  - EC2 t3.medium（4 GB） + IAM Role（Bedrock InvokeModel）+ VPC + SG
  - API キー不要（IMDSv2 経由で temporary credentials 取得）
- **Slack 接続**：EC2 の `~/.openclaw/.env` に Slack App Token / Bot Token を投入（既存 17 スコープと完全一致）
- **Bedrock 接続確認**：`openclaw.json` で `models.providers.amazon-bedrock` を有効化、`us.anthropic.claude-sonnet-4-6` で hello 応答
- **AWS_PROFILE の罠対応**：`AWS_PROFILE=default` を `~/.openclaw/.env` に書き込み、`systemctl --user restart openclaw-gateway.service`
- **ClawHub 無効化**：`openclaw.json` で `clawhub.disabled: true`（ClawHavoc 対策）

PoC 成功定義：Slack `@TeamAgent_v3 hello` → OpenClaw → Bedrock Claude Sonnet 4.6 → 応答が 30 秒以内に Slack へ戻る。

### 3.2 W2（6/6〜6/12）— ヒアリング + セキュリティレビュー

- 子会社 120 ユーザー運用の回答整理（運用開始時期 / 期間 / トラブル事例 / Skill ロールバック手順）
- 社内セキュリティ部レビュー：
  - サンドボックス制限（ファイル / ネット / 実行時間）
  - 最小権限の Skill 実行
  - 動的解析（ClawScan + 社内独自）
  - インシデント時のロールバック手順
- B 案実装コスト見積（FastAPI ラップ + HTTP 橋渡し）vs D 案維持コスト

### 3.3 Go/No-Go ゲート①（2026-06-12）

**判定軸**：

| 軸 | B 案採用条件 | D 案維持条件 |
|---|---|---|
| PoC 結果 | hello world 成功、レイテンシ < 30 秒 | PoC 不安定 / レイテンシ過大 |
| 子会社ヒアリング | 120 ユーザー運用が安定、Skill インシデントゼロ | 運用上の懸念多数 |
| セキュリティ | ClawHub 無効化 + サンドボックスで社内ポリシー合格 | ポリシー整合不可 |
| 営業価値 | 23 チャネル対応・Skill エコシステムが MVP に必要 | MVP では不要 |
| 工数 | +1.5 Sprint 追加で許容範囲 | +0 Sprint で完了 |

**事前推定**：D 案優位（Day 2 で E2E 疎通済 / 0 Sprint で MVP / ClawHavoc を 100% 回避）。ただし子会社ヒアリング次第で B 案に振れる余地を残す。

---

## 4. Sprint 3（6/13〜6/26）— 判断結果の実装

ゲート①の結果でルートが分岐する。

### 4.1 B 案（OpenClaw 採用）の場合

- `services/teamagent_skills_api/` 新設：`POST /skills/{name}/invoke`（FastAPI + uvicorn）
  - 入力：`SkillInput`（Pydantic v2）
  - 出力：`SkillOutput`（引用・コスト・レイテンシ含む）
- 認証：API キー（OpenClaw → FastAPI 間）+ mTLS（推奨）
- OpenClaw 側に `teamagent-search` SKILL.md 作成：YAML frontmatter + 自然言語指示 + `curl` で FastAPI 呼び出し
- Pact 契約テスト追加（OpenClaw ⇔ FastAPI の I/O 互換性保証）
- 既存 `runtime/slack_bot.py` は **Sprint 4 まで並走**（段階移行）

### 4.2 D 案（OpenClaw 不採用）の場合

- 自前 Skill Registry 強化：
  - **Idempotency-Key 機構**：Slack 投稿 / DB 書き込みでリトライ時の二重実行を防ぐ（Redis or RDS の `idempotency_keys` テーブル）
  - **Memory 層**：会話履歴を `conversation_messages` テーブルに保存、SkillContext に直近 N 件を渡す
  - **Skill バージョニング**：Registry に `version` フィールドを追加、A/B 切替可能に
- Skill 追加：メタデータ抽出 Skill（Sprint 1 P1 の延長）を本実装

両ルートとも **既存 `src/teamagent/` 実装は 85〜100% 流用可能**（訂正ノート v0.2 §3 参照）。

---

## 5. Sprint 4（6/27〜7/10）— Skill 拡張 + ベータ展開

### 5.1 Skill ② メタデータ抽出パイプライン本実装

- バッチ：S3 raw → Claude Sonnet 4.6 → JSON 抽出 → `metadata` JSONB に保存
- 増分対応：`document_id` 単位で再処理可能、`metadata_version` 列で世代管理
- 監査：抽出結果を `debug_snapshots/` に保存（KMS 暗号化、TTL 30 日）
- Query Router 連携：`metadata` フィルタ → vector search の WHERE 条件に変換

### 5.2 Slack ベータ展開（営業 3〜5 名）

- 対象：協力的な営業 3〜5 名（PJ リードが選定）
- フィードバック収集：
  - Slack 専用チャンネル `#teamagent-beta` で質問・不満を収集
  - 週次レビュー（30 分 × 1）でクエリログ + 検索精度を共有
- 計測指標：
  - top-3 retrieval hit rate
  - クエリあたりコスト
  - レイテンシ p50 / p95
  - ユーザー満足度（reaction 絵文字で簡易採集）

### 5.3 Skill ③ Query Router（Claude Haiku 版）

- ルールベース版（Sprint 1 残）を Claude Haiku 4.5 へ置き換え
- 4 種ルーティング：meta（SQL 集計）/ content（vector）/ conditional（フィルタ + vector）/ compare（複数フェッチ）
- Haiku の prompt cache を効かせ、クエリあたり追加コストを $0.001 未満に抑える

---

## 6. Sprint 5-6（7/11〜8/7）— 営業 16 名展開 + 検索精度

### 6.1 Sprint 5（7/11〜7/24）— 展開拡張

- ベータ 3〜5 名 → 営業 16 名へ拡張
- Slack ワークスペース内で「困ったら #teamagent」を周知
- 既存 PR 履歴・提案 PDF（直近 2 年分）を本番 RDS にバルク投入
  - 想定 1,500 PDF × 平均 30 chunk = 45,000 chunks
  - 埋め込み所要：LocalE5 ローカル並列で 6〜8 時間（一晩バッチ）
- オンボーディング資料：1 ページ「使い方カード」+ 3 分動画

### 6.2 Sprint 6（7/25〜8/7）— 検索精度チューニング

- **Contextual Retrieval** 適用：Claude Haiku で各 chunk に「この章は〇〇についての記述」前置詞を生成 → 再 embedding
  - Anthropic 公式手法に準拠（retrieval error 35% → 49% 削減の論文値）
- ハイブリッド検索の重み調整：vector 0.7 + JSONB filter 0.3（営業 8 軸の重要度を反映）
- 引用フォーマット改善：Slack 上で「📄 提案書名 / 該当 chunk へのリンク」を必ず付与
- 失敗クエリの再現テストスイート構築（営業から寄せられた NG クエリを pytest 化）

---

## 7. Sprint 7-12（8/8〜11/30）— Phase 4 Skill 拡張 + 動画分析

### 7.1 Sprint 7-8（8/8〜9/4）— Skill ⑤ 過去提案再利用支援

- 既存提案から「類似業種・類似予算」のテンプレを抽出
- Claude Sonnet 4.6 でドラフト生成 → 営業がレビュー
- 期待効果：提案作成時間 20h → 12-15h（中間計測）

### 7.2 Sprint 9-10（9/5〜10/2）— Skill ④ 動画分析（Gemini 2.5 Flash）

- 競合 PR 動画を Gemini 2.5 Flash に投入し、構成・テロップ・尺・引きを抽出
- Bedrock 経由ではなく Gemini API 直接（Google Vertex AI）。月次予算 $30 を別途確保（PoC）
- 出力スキーマ：`scenes[]`（時刻・要約・テロップ・カメラワーク・推定意図）
- 営業 8 軸との結合：競合分析を提案書に直接転記可能なフォーマットへ

### 7.3 Sprint 11-12（10/3〜11/30）— Phase 4 拡張 + ゲート②

- Skill ⑥ 営業活動ログ自動生成（Slack 会話 → CRM フォーマット）
- Skill ⑦ 提案レビュー Bot（コードレビュー的に提案 PDF を診断）
- 11 月末：**ゲート②**（後述 §9）で提案作成時間の実測値を判定

---

## 8. Sprint 13-14（12/1〜12/28）— QA + 負荷試験 + 本番リリース

### 8.1 Sprint 13（12/1〜12/14）— QA

- 全 Skill のリグレッションテスト（pytest フル実行 + 主要クエリ 50 種）
- セキュリティ監査：
  - Secrets Manager のローテーション動作確認
  - CloudWatch ログに PII / 機密が出ていないか grep
  - Bedrock prompt injection 耐性テスト
- 障害訓練：Bedrock リージョン障害 / RDS 障害 / Slack API ダウンの 3 シナリオで切替手順を確認

### 8.2 Sprint 14（12/15〜12/28）— 負荷 + 本番

- 負荷試験：営業 20 名 × 1 日 30 クエリ = 600 クエリ/日 を **同時 50 並列**で再現
  - 目標：p95 < 15 秒、エラー率 < 1%
- 本番リリース：
  - Blue/Green 切替（既存 EC2 → 新 EC2）
  - DNS 切替後 48 時間モニタリング
  - ロールバック手順を 1 ページにまとめ Runbook 化
- 引き継ぎ：運用ドキュメント / オンコール体制 / SLO（uptime 99.0% / p95 < 15 秒）確定

---

## 9. Go/No-Go ゲート②（2026-10 後半）

ゲート②は **「提案書作成時間 20h → 8〜12h」の実証**を判定する。

| 指標 | 目標 | 計測方法 |
|---|---|---|
| 提案書作成時間 | 20h → 8-12h | 営業 5 名 × 直近 3 提案で対照実験（Before/After） |
| 検索 top-3 hit rate | ≥ 70% | クエリログ 500 件をサンプル評価 |
| ユーザー満足度 | ポジティブ reaction ≥ 60% | Slack reaction 集計 |
| クエリあたりコスト | ≤ $0.03 | Bedrock usage log を request_id で突合 |
| レイテンシ p95 | ≤ 15 秒 | CloudWatch メトリクス |

**ゲート②で No-Go の場合**：Sprint 13-14 を機能追加凍結 + 精度改善に振り替え、本番リリースを 2027 Q1 に延期する判断を CTO 承認で行う。

---

## 10. コスト管理

### 10.1 既設定の予算枠

| 枠 | 月次上限 | 50/80/100% 通知 | 設定済 |
|---|---|---|---|
| `TeamAgent-Bedrock-Monthly` | **$50/月** | s-komata@vectorinc.co.jp, NewsTV_AWS_AIagentAdmin@vectorinc.co.jp | ✅ |
| `TeamAgent-Server-Monthly` | **$267/月**（≒¥40,000、RDS / EC2 / S3 / Lambda） | 同上 | ✅ |

### 10.2 過去実績との突合（Day 2 時点）

- **Bedrock**：実機 E2E 3 連投で $0.01-0.02/クエリ × 3 ≒ $0.05。営業 16 名 × 30 クエリ/日 × 22 営業日 = **$32〜$64/月**（予算 $50 の上限ギリギリ → prompt caching で 50% 削減が必須）
- **Server**：現状ローカル開発のみ。Sprint 2 で EC2 t3.medium（OpenClaw 採用なら $30/月）+ RDS db.t4g.micro（$15/月）+ S3 / Secrets Manager（$5/月）= **約 $50/月**。予算 $267 に十分収まる
- **データ移行スパイク**：Sprint 5 のバルク投入で Bedrock コスト一時的に +$20〜30 を見込む（metadata 抽出 45,000 chunks）

### 10.3 コスト最適化施策

1. **prompt caching ON 必須**（90% コスト削減）
2. **Claude Haiku 4.5 を Query Router に使う**（Sonnet の 1/10 コスト）
3. **embedding はローカル E5**（API 課金ゼロ）
4. **Bedrock リージョン固定**（us-east-1）でクロスリージョン費を削減
5. **Server 側は ap-northeast-1**（RDS / EC2）、跨ぎは Bedrock 呼び出しのみ
6. 月次でコスト実績レビュー（毎月 1 日に CloudWatch Cost Explorer + Bedrock usage ログを突合）

### 10.4 コスト超過時のエスカレーション

- 80% 通知到達 → PJ リードがレビュー、不要な Skill を一時停止
- 100% 通知到達 → 翌月予算引き上げ承認フロー（CTO 承認）/ もしくは機能制限モード（Sonnet → Haiku 強制切替）

---

## 11. リスクと前提

| # | リスク | 影響 | 対策 |
|---|---|---|---|
| 1 | OpenClaw 採否がゲート①で確定しない | Sprint 3 開始がずれる | Sprint 2 末で必ず決める、判断保留は不可 |
| 2 | 子会社ヒアリング回答が来ない | ゲート①判断材料が不足 | 5/23 送付、6/5 までに返信なき場合 D 案デフォルトで進む |
| 3 | Bedrock 予算 $50/月を Sprint 5 のバルク投入で突破 | コスト超過 | バルク投入は週末バッチ + Haiku でメタデータ抽出 + prompt caching |
| 4 | 営業 16 名がベータで離脱 | 価値証明に失敗 | Sprint 4 ベータ 3〜5 名で先に課題を潰す |
| 5 | ClawHub サプライチェーン事件の再発（B 案採用時） | セキュリティ重大 | `clawhub.disabled: true` 固定 + Skill ホワイトリスト + 週次 CVE 確認 |
| 6 | Slack Bot Token が漏洩済（チャットに露出） | 不正利用 | Sprint 1 Day 3 で必ずローテーション |
| 7 | Gemini 2.5 Flash の利用規約変更 | Skill ④ が動かない | Bedrock 経由 Claude Sonnet vision に fallback 設計 |

---

## 12. 参照ドキュメント

- 検索 Skill 設計：`docs/v3.1/teamagent_search_skill_design_v1.md`
- v3.1 実装計画（前版）：`docs/v3.1/teamagent_implementation_plan_v3.1.html`
- v3.1 設計訂正ノート：`docs/v3.1/teamagent_design_corrections_2026-05-22.md`
- 子会社質問リスト：`docs/v3.1/teamagent_subsidiary_questions_v2.md`
- MVA 全体仕様：`docs/v3.1/teamagent_mva_spec_v1.1.html`
- AI エージェント実装ルール：`CLAUDE.md` §6-bis
- AWS 公式テンプレ：`aws-samples/sample-OpenClaw-on-AWS-with-Bedrock`

---

## 更新履歴

| 日付 | バージョン | 更新内容 |
|---|---|---|
| 2026-05-22 | v3.2 draft v0.1 | Day 2 完了時点を起点に Sprint 1〜14 を再構成、ゲート①の判断軸とゲート②の実証指標を確定、コスト管理を予算枠 $50/$267 と突合 |
