# HEARTBEAT — プロアクティブ・チェックリスト（P2/WS-D で解禁）

> ⚠️ **P1 では heartbeat 無効**（openclaw.json: `agents.defaults.heartbeat.every: "0m"`）。
> このファイルは P2 の自発起動を設計するためのテンプレ。有効化は HITL・冪等・コストガード整備後。
>
> heartbeat の契約: 対応不要なら **`HEARTBEAT_OK`** とだけ返す（それ以外は配信される）。
> 過去チャットのタスクを推測・蒸し返さない。下の `tasks:` のうち「実行期限が来たもの」だけ処理する。

```yaml
tasks:
  - name: morning_brief
    interval: 1d
    prompt: >
      担当案件の今日の要点を MCP の読取 tool で集め、3〜5行で要約。
      新規アラートが無ければ HEARTBEAT_OK。
  - name: deal_radar
    interval: 4h
    prompt: >
      停滞案件（一定期間 動きのないカルテ）を検出し、該当があればスレッドに1行で通知。
      無ければ HEARTBEAT_OK。
  - name: competitor_trend
    interval: 1d
    prompt: >
      競合/市場トレンドの新着があれば見出しだけ共有。無ければ HEARTBEAT_OK。
```

## 冪等メモ（P2 実装時）

- 通知の重複防止は `runtime/video_approval_poller.py` の ProcessedStore + claim-before-await +
  初回 baseline パターンを踏襲（連続2回・再起動後も重複0 を E2E で確認）。
- 重操作はここから直接行わず、必ず HITL の propose→confirm に乗せる。
