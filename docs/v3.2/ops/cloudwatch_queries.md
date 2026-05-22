# CloudWatch Logs Insights クエリ集

**作成日**: 2026-05-22 Day 2
**対象ログ**: `/teamagent/dev`（Terraform で作成済）
**前提**: 構造化ログ JSON が CloudWatch に流れている（Sprint 4 で本番 EC2 デプロイ後に有効化）

> 本ドキュメントは「本番 EC2 で Bot が動き始めたら、即座にこのクエリ集を CloudWatch Logs Insights に貼って運用開始する」ための備え。

---

## 1. クイックリンク

| 用途 | Insights クエリ番号 |
|---|---|
| エラー検出 | Q1 / Q2 |
| コスト追跡 | Q3 / Q4 |
| 遅延分析 | Q5 |
| ユーザー利用パターン | Q6 / Q7 |
| Skill 別利用統計 | Q8 |

---

## Q1. エラー全件抽出（直近 1 時間）

```
fields @timestamp, request_id, event, error, level
| filter level = "error" or level = "warning"
| sort @timestamp desc
| limit 100
```

エラーが request_id 単位で見える。同じ request_id で複数イベントが出てたら相関調査。

---

## Q2. search_skill_failed の例外パターン分布

```
fields request_id, error
| filter event = "search_skill_failed"
| stats count(*) as occurrences by error
| sort occurrences desc
```

「Bedrock タイムアウト」「pgvector 接続断」「embedder ロード失敗」などの頻度がわかる。

---

## Q3. Bedrock 累積コスト（日別）

```
fields @timestamp, cost_usd, model_id
| filter event = "bedrock_converse"
| stats sum(cost_usd) as daily_cost, count(*) as calls by bin(1d), model_id
| sort @timestamp desc
```

AWS Budgets と二重で確認用。Sonnet 4.6 / Haiku 4.5 別に積み上げる。

---

## Q4. 1 クエリあたりの平均コスト + トークン

```
fields cost_usd, input_tokens, output_tokens, cache_read_input_tokens
| filter event = "bedrock_converse"
| stats
    avg(cost_usd) as avg_cost,
    avg(input_tokens) as avg_in,
    avg(output_tokens) as avg_out,
    avg(cache_read_input_tokens) as avg_cache,
    count(*) as n
  by bin(1h)
```

Prompt caching の効果が `avg_cache` で見える（Sprint 1 末で `cache_system=True` 追加済）。

---

## Q5. レイテンシ p50/p95/p99（Bedrock + Slack 投稿）

```
fields latency_ms, event
| filter event in ["bedrock_converse", "slack_post_message", "pgvector_search", "embedder_embed"]
| stats
    pct(latency_ms, 50) as p50,
    pct(latency_ms, 95) as p95,
    pct(latency_ms, 99) as p99,
    max(latency_ms) as max
  by event
```

SLO（p95 < 5s）の遵守状況がここで見える。

---

## Q6. ユーザー別利用回数 + 平均コスト（直近 7 日）

```
fields user_id, cost_usd
| filter event = "search_skill_done"
| stats count(*) as queries, sum(cost_usd) as cost by user_id
| sort cost desc
| limit 20
```

営業 16 名のうち、誰がヘビーユーザーかが見える。コスト按分の判断材料。

---

## Q7. Skill 別利用統計

```
fields skill, event
| filter event = "search_skill_done"
| stats count(*) as calls by skill, bin(1d)
| sort @timestamp desc
```

Sprint 7 以降の Skill 拡張時、どの Skill が使われているか測定。

---

## Q8. Slack mention → 完了までの全フロー追跡

```
fields @timestamp, event, request_id, skill, latency_ms, cost_usd
| filter request_id like /^req-/
| sort @timestamp asc
| limit 200
```

特定 request_id で絞れば、`slack_app_mention_dispatch` → `skill_router_decision` → `embedder_embed` → `pgvector_search` → `bedrock_converse` → `slack_post_message` の連鎖が時系列で見える。

---

## Q9. Router 判定の偏り

```
fields query_type, confidence
| filter event = "skill_router_decision"
| stats count(*) as n by query_type
```

`content`（デフォルト）が極端に多ければ rule-based のキーワード辞書を増やす必要あり。

---

## Q10. Contextual Retrieval の効果計測（cache hit 率）

```
fields cache_read_input_tokens, input_tokens
| filter event = "bedrock_converse"
| stats
    sum(cache_read_input_tokens) as cache_read,
    sum(input_tokens) as total,
    (sum(cache_read_input_tokens) / sum(input_tokens)) * 100 as cache_hit_pct
  by bin(1d)
```

PR #26 で導入した prompt caching の効果。理想は cache_hit_pct ≥ 80%。

---

## 設定方法

CloudWatch メトリクスフィルタへ自動化（Sprint 4 で実装予定）：

```bash
# 例: エラー検出のメトリクスフィルタ
aws logs put-metric-filter \
  --log-group-name /teamagent/dev \
  --filter-name TeamAgentErrors \
  --filter-pattern '{$.level = "error" || $.level = "warning"}' \
  --metric-transformations \
    metricName=TeamAgentErrors,metricNamespace=TeamAgent/Dev,metricValue=1 \
  --region ap-northeast-1
```

CloudWatch アラーム：

```bash
# 1分あたり 3 件以上エラーが出たら通知
aws cloudwatch put-metric-alarm \
  --alarm-name TeamAgent-Dev-ErrorRate \
  --metric-name TeamAgentErrors \
  --namespace TeamAgent/Dev \
  --statistic Sum \
  --period 60 --threshold 3 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --evaluation-periods 1 \
  --alarm-actions arn:aws:sns:ap-northeast-1:718959508629:teamagent-alerts \
  --region ap-northeast-1
```

---

## 関連ドキュメント
- 構造化ログ仕様: `CLAUDE.md` 6-bis セクション
- Skill 設計: `docs/v3.1/teamagent_search_skill_design_v1.md`
- マスター ToDo: `docs/v3.2/teamagent_master_todo_v1.md`
