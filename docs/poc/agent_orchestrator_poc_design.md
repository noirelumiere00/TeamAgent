# マルチSkill 自律オーケストレーター PoC — 設計メモ

> branch `poc/multiskill-orchestrator`（main 基点・worktree 隔離）。本番 `~/Documents/TeamAgent`
> の `feat/video-approval-sheet-writeback`（別セッション稼働中）には一切触れない。
> 位置づけ: v3.2 「C案 = multi-skill orchestrator」を**本実装する“前まで”の地ならし**。
> ゴールは「Agent SDK 採否＋自律ループ実現性」の判断材料を、動く薄い縦スライスで出すこと。

## 1. 何を作るか（タイプB＝適応型エージェント）

「調べた結果を見て、次の手と結論を自分で変える」ループ。代表シナリオ（ユーザー要望そのまま）:

```
入力: 「クライアントXに次の施策を提案して」
 ① client_history を確認  → 「過去“認知”施策で実行→KPI未達(滑った)」を発見
 ② 【適応判断】認知が滑った → 今回は“CV型”を提案すべき、と方針転換
 ③ CV型 施策候補を生成
 ④ mail_constraints を確認 → 「この手法はNG」を発見 → 【候補を差し替え】
 ⑤ supporting_case を Drive から取得（裏付け）
 ⑥ 統合 → 「失敗を踏まえCV型・NG回避・成功事例で裏付け」した提案を出力
```

②④⑤の分岐は**入力時に確定できない＝データ依存の適応**。ここがワークフローでなく自律ループを要する所以。

## 2. 既存資産を“ツール”として使う（新規構築を最小化）

`SkillRegistry`/`BaseSkill[In,Out]`/`SkillContext(request_id)` が既にあり、**各 Skill をそのままツール化**できる。

| エージェントのツール | 既存 Skill / Adapter | シナリオ対応 |
|---|---|---|
| `get_client_history` | `operation_log`(CRMログ) + `search`(過去提案RAG) | ① 過去の認知施策と結果 |
| `search_past_cases` | `search`(pgvector/Drive) | ⑤ 裏付け成功事例 |
| `draft_measure` | `proposal`(/`proposal_review`) | ③ 施策候補生成 |
| `check_mail_constraints` | `gmail_client` adapter（**新 Skill 薄く追加**） | ④ MailのNG規則 |

→ オーケストレーターは **runtime 層の新コンポーネント**。ツール（Skill）を呼ぶだけで **adapter は直叩きしない**（3層分離維持）。

## 3. アーキテクチャ（3層を壊さない）

```
runtime/orchestrator.py  ← 新規。エージェントループ本体
   │  SkillRegistry からツール仕様(JSON schema=各Skillのinput_schema)を生成
   │  LLM(Bedrock)に「次にどのツールを呼ぶ/もう答える」を判断させる
   ▼
skills/*  (既存 + check_mail_constraints を新設)  ← ツール実体
   ▼
adapters/* (gmail/gdrive/pgvector/bedrock …)      ← I/O
```

ループ擬似コード:
```
state = []                       # 観測の履歴
for step in range(MAX_STEPS):    # 暴走防止の上限
    decision = llm.decide(goal, tools, state)        # tool_use or final
    if decision.is_final: return decision.answer
    out = SkillRegistry.get(decision.tool).run(decision.input, ctx)  # request_id 伝播
    state.append((decision.tool, out))
    # cost/token/latency を job_step 相当で必ずログ
```

## 4. 2方式を薄く比較（これが採否の判断材料）

| | 方式A: 自前ループ | 方式B: Agent SDK on Bedrock |
|---|---|---|
| ループ機構 | 自前（boto3 Converse の tool_use を回す） | SDK が提供（`CLAUDE_CODE_USE_BEDROCK=1`） |
| 規約適合(cost/log/request_id) | 完全に自分で握れる | **SDKフックで差し込めるか要検証**（本PoCの主眼） |
| 実装量 | 多め（ループ自作） | 少なめ（ツール定義中心） |
| 留意 | — | Bedrockは**Invoke API**・モデルは**inference profile ID**・prompt cacheはリージョン依存 |

評価軸: (a) 規約通りの観測性/コスト記録ができるか (b) 実装量 (c) レイテンシ。

## 5. ガードレール / ガバナンス（必須）

- **コスト**: ループに `max_steps` と `cost_cap_usd`(例 $0.50/PoC) のハードカット。Bedrock呼び出し毎に usage/cost ログ（CLAUDE.md 6-bis）。
- **request_id**: 1リクエスト＝1 `SkillContext.request_id` を全ツール呼び出しに伝播。
- **Mail/PII**: PoC は **fixtures（架空のMail/履歴/Drive事例）で完結**。実Gmailは繋がない。ログに生本文を出さない（マスク）。「自分のMail横断」を本番でやる場合の **DLP/同意/対象受信箱** 方針は別途ゲート。
- **冪等性**: PoC段階では副作用なし（読み取り＋生成のみ、Slack投稿やDB書込はしない）。

## 6. PoC スコープ（“前まで”の線引き）

**やる**:
- `runtime/orchestrator.py`（方式B=SDK を主、方式A=自前を比較用に最小）
- ツール4種のうち `check_mail_constraints` Skill を新設、他3つは既存Skillを薄くラップ
- 代表シナリオの **fixtures**（認知施策で滑った履歴 / MailのNG規則 / Drive成功事例）
- **オフラインで決定トレース①〜⑥が出る**こと（ループ機構の検証。LLMはmock/可能なら実Bedrock）
- 比較結果メモ（採否の結論）

**やらない（＝本実装フェーズ）**:
- 全Skillのツール化 / 本番 slack_bot 配線 / 実Gmail・実Drive 接続 / 本番デプロイ

## 7. SDK 採否の判断基準（PoC後にこれで決める）

- ✅ 規約(cost/log/request_id)を SDK フックで満たせ、実装量が自前より十分小さい → **SDK採用**
- ❌ 規約適合に無理がある or ループが単純で自前で十分 → **自前ループ採用**（SDKはpyprojectから掃除）

## 8. 想定ファイル（worktree内）

```
src/teamagent/runtime/orchestrator.py         # ループ本体（A/B切替）
src/teamagent/orchestrator/                    # ツール束ね・状態・LLM判断アダプタ
src/teamagent/skills/mail_constraints/         # 新Skill（gmail_client ラップ）
tests/orchestrator/fixtures/                   # 架空シナリオ
tests/orchestrator/test_adaptive_trace.py      # ①〜⑥が出る回帰テスト
docs/poc/agent_orchestrator_poc_findings.md    # 比較結果・採否結論（PoC後）
```
