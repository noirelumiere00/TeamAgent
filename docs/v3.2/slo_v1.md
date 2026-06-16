# TeamAgent SLO v1（パイロット土台・Wave2-⑥）

> ベクトル社・営業 16 名向け Slack マルチスキル AI エージェントの**運用品質契約（SLO）**。
> パイロット（P1: 専用ch 2-3 名・読取のみ・1 週間 → P2: 営業 16 名・write 有効）の合否ゲートと、
> 本運用（2026/12〜）での月次レビュー指標を一元化する。

最終更新: 2026-06-15 (Wave2-⑥) ／ 主担当: 小俣 ／ 月次レビュー: 月初の運用定例

---

## 1. SLO 全体像

### スコープ
- **応答品質**: Slack メンションへの応答（検索・クライアントカルテ・提案ドラフト・動画分析・operation_log 等 9 Skill）
- **取り込み品質**: 週次 ingest（Slack / Drive / Sheets → pgvector）の鮮度・成功率
- **可用性**: Bot サービス（ECS Fargate teamagent-dev-mcp / -openclaw / -aiia-mcp）の稼働
- **コスト**: Bedrock / Gemini / Cohere / OpenAI などの月次・1リクエスト平均

### 測定基盤（既存）
| シグナル | 出所 | 保存先 |
|---|---|---|
| `latency_ms` (1リクエスト end-to-end) | `slack_bot.handle_app_mention` の `time.perf_counter()` 差分 | RDS `usage_events` (列 `latency_ms`) |
| Bedrock 単独 latency | `bedrock_client.converse` | structlog（CloudWatch Logs） |
| `cost_usd` | usage と単価テーブル（Sonnet/Haiku/Cohere）から算出 | RDS `usage_events` |
| `status` | Skill 戻り値 | RDS `usage_events` |
| Bot/MCP/OpenClaw ECS タスク稼働 | ECS service metric (`RunningTaskCount`) | CloudWatch |
| 検索精度 (gold set top-1 hit rate) | `tests/eval/` の手動実行 | 手動レポート（自動化未着手） |

---

## 2. レイテンシ SLO（区分別）

「重い処理ほど時間がかかる」を許容しつつ、**各区分の p95** を明示的にゲートする。
基準値は現在の実測（cache 効果反映前の中央値）と負荷テスト結果（`docs/v3.2/load_test_results.md`）に基づく**暫定値**。
P1 パイロット実測で確定する想定。

| 区分 | 代表 Skill / 操作 | 目標 p95 | 暫定 p99 | 根拠 |
|---|---|---|---|---|
| **軽量** | clientkarte（既知顧客の時系列）、operation_log の構造化 | ≤ 3 s | ≤ 6 s | Bedrock Haiku + 軽量プロンプト |
| **中量** | search（L2 合成あり）、proposal_review、proposal_draft | **≤ 15 s** | ≤ 25 s | 実機 12-18s 実績（v2d / SEARCH_MAX_TOKENS=800 / cache 有効）|
| **重量** | VSEO 分析、video_analysis（>5分動画）、video_approval | ≤ 5 min | ≤ 8 min | Gemini 動画 + ffmpeg + Drive 連携の合計 |

**注記**
- 「中量 ≤ 15s」は Wave1-① の合否基準として確定（コード変更不要・既存設定で達成見込み）。
- 「重量」は動画長・スクレイプ成功率に大きく依存。p95 は「成功した実行のみ」を母集団とする。
- 計測は `usage_events.latency_ms` の 24h 移動窓 p95（Skill ごと）。

---

## 3. 可用性 / エラー率 SLO

| 指標 | 目標 | 計測 |
|---|---|---|
| **MCP サービス可用性**（Slack メンションが応答を返す月次率） | **≥ 99.0%** | CloudWatch ECS `RunningTaskCount`（desired との比率を 1 分粒度で集計） |
| **エラー率**（5xx + ハンドラ例外 / 全 mention） | ≤ 1% / 24h 移動窓 | `usage_events.status` で `error` / `failed` を集計 |
| **Bedrock 失敗率**（throttle/timeout）| ≤ 0.5% / 24h | structlog の `bedrock_converse_failed` 件数 / 全 converse |
| **ingest 連続失敗** | 2 週連続失敗で **P0** | `teamagent-ingest.service` の status + #ops Slack alert（Wave1-③）|

**エラー予算**: 月次 99.0% → 月内 7.3h までのダウンタイム許容。
超過時は次月の新規機能リリース凍結・運用安定化を優先する（バーンレート2倍超で**緊急レビュー**）。

---

## 4. 品質 SLO

| 指標 | 目標 | 計測 / 備考 |
|---|---|---|
| **検索 gold set top-1 hit rate** | ≥ 60% | `tests/eval/` の月次手動実行（v2d prompt + Cohere Rerank 後の実測 64% を SLO 基準に固定） |
| **1検索あたりコスト** | ≤ $0.02 | `usage_events.cost_usd` の 24h 移動窓 p50（prompt caching の cache_read 反映後） |
| **ingest 鮮度**（週次 ingest の last_success） | last_success ≤ 8 日 | `usage_events.created_at where skill='ingest'` または `journalctl -u teamagent-ingest` |
| **operation_log の BANT 抽出成功率** | ≥ 80% | Skill 戻り値の `parse_ok` フラグ集計（Wave1-② 配線済） |

---

## 5. SLI 実装状況

| SLI | 既実装 | 未実装 | 補足 |
|---|---|---|---|
| Skill 別 p95 latency | ✅ (`usage_events.latency_ms`) | CloudWatch Insights クエリの cron 化 | `.env.production.template` にクエリ例を Wave1-① で追記済 |
| エラー率 | ✅ (`usage_events.status`) | アラート閾値の正式化 | `infra/terraform/variables_fargate.tf` で 15000ms 既定 |
| Bedrock cache_read 計測 | ⚠️ structlog のみ | `usage_events` への列追加（`cache_read_input_tokens`）| 将来 migration で対応 |
| gold set top-1 自動化 | ❌（手動） | pytest + nightly CI で `tests/eval/` 実行 | Wave3 候補 |
| 月次可用性集計 | ❌（手動） | CloudWatch メトリクス → 月次レポート自動生成 | Wave3 候補 |

---

## 6. アラート & エスカレーション

| 重大度 | 条件 | 対応 SLA | チャネル |
|---|---|---|---|
| **P0** | MCP サービス全タスク停止 / ingest 2 週連続失敗 / RDS 接続不可 | 5 分以内一次対応 | PagerDuty なし → 当面は Slack `#ops` メンション + 小俣個別連絡 |
| **P1** | p95 latency 連続 30 分超過 / エラー率 ≥ 5% / Bedrock throttle 多発 | 1 時間以内確認 | Slack `#ops` |
| **P2** | gold set hit rate 月次低下 / コスト超過予兆 | 翌営業日 | 月次レビュー定例で取り扱い |

**Wave1-③ で追加した ingest #ops 通知** が P1 / P0 の初期検知経路。
webhook を Secrets Manager (`teamagent/prod/ops-slack-webhook`) に投入することで有効化。
未投入なら ingest 失敗時は `journalctl -u teamagent-ingest.service` で人手検知。

---

## 7. 本番運用ゲート

| フェーズ | 期間 | 条件 |
|---|---|---|
| **P1 パイロット** | 1 週間 | 専用 ch・2-3 名・**読取のみ**。p95 ≤ 15s / エラー率 < 1% を実測で確認 |
| **P2 拡大** | 1 ヶ月 | 営業 16 名全員・write（operation_log / proposal_draft）有効化。SLO ダッシュボード月次 review |
| **本運用** | 2026/12〜 | 全 SLO 緑（P1/P2 で実測したベースラインを SLO に確定）+ 露出トークン rotation 完了（Wave2-⑦） |

各 gate の合否は `docs/openclaw/golive_checklist.md` のチェックリストと本ドキュメントを併用して判定する。

---

## 8. 所管 / 参考資料

- **サービスオーナー**: 小俣翔碁
- **月次レビュー**: 運用定例（月初）で SLO ダッシュボード（CloudWatch + RDS クエリ）を読み合わせ
- **関連 docs**
  - [`system_reference.md`](./system_reference.md) — Skill カタログ・技術スタック
  - [`load_test_results.md`](./load_test_results.md) — 並行 20 件・DL 失敗 50% 嵐などの堅牢性検証
  - [`ops/observability_and_security.md`](./ops/observability_and_security.md) — CloudWatch metric filter / Sentry / CloudTrail
  - [`ops/secrets_rotation_policy.md`](./ops/secrets_rotation_policy.md) — トークン rotation（Wave2-⑦）
  - [`openclaw/golive_checklist.md`](../openclaw/golive_checklist.md) — go-live 前チェック

---

## 9. 未確定事項（決定が必要）

1. **可用性目標 99.0% vs 99.5%**: 上位を目指すなら冗長化（multi-AZ ECS service + RDS replica）の追加投資が必要。当面 99.0%。
2. **エラー予算超過時の機能凍結ルール**: バーンレート2倍で **24h レビュー**、3倍で **次回リリース凍結** で運用する案を採用。
3. **オンコール体制**: 現状は小俣個別連絡のみ。営業 16 名展開後は週次ローテーション化を検討（Wave3 以降）。
