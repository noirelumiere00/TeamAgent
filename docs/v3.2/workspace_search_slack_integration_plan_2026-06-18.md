# Slack DM から workspace_search（カレンダー/連絡先）を呼べるようにする設計プラン — 2026-06-18

> 他セッション・他作業者向けの**設計書**。実装はまだ未着手。
> 起点: ユーザー要望「Slack で AiLa にカレンダー予定を頼みたい（mail のサマリーは以前動いていた、今は認証問題で停止中）」

---

## TL;DR

- **メール機能(mail_*)が live で動いていた経路 = EC2 worker (`teamagent-bot.service`) の `slack_bot.py`**。OC ではなく EC2 worker が per-user 機能を提供している。
- **同じ経路に workspace_search(カレンダー+連絡先) を乗せるだけで**、Slack DM から「来週の予定まとめて」「○○社の連絡先教えて」が動く。
- **改修は3ファイル・小さい**（intent.py に regex 追加 / slack_bot.py に dispatch 分岐追加 / テスト）。
- **前提**: PR#127（今朝の本番 OAuth インシデント恒久対策）が完了して connect が復活していること。それまではメール経路ごと止まる。

---

## 1. 現状把握（確認済み事実）

### 1.1 経路の二重構造
```
@AiLa の Slack メンション/DM
   │
   ├──→ OpenClaw (Fargate, teamagent-dev-openclaw)
   │      └ Slack Socket Mode で接続
   │      └ MCP gateway 経由で TeamAgent MCP の include 列挙ツールを呼ぶ
   │      └ include: search, clientkarte, proposal_draft, proposal_review,
   │                  tiktok_search, video_analysis, video_algorithm, operation_log
   │      └ exclude: mail_*, workspace_search, proposal_deck, *reply*, *confirm*, *write*
   │
   └──→ EC2 worker (teamagent-bot.service / slack_bot.py)
          └ Slack Socket Mode で並走接続（両方が同じイベントを受ける）
          └ intent.py の regex で判定 → 担当 Skill を直接呼ぶ
          └ mail_followup / mail_summary / mail_reply / mail_to_internal_context
          └ connect (Google OAuth リンク発行)
          └ proposal_draft / proposal_review も regex でホット dispatch
```

### 1.2 per-user OAuth が EC2 経路でしか動かない理由（再確認）
- **OC 公式 v2026.6.5 にユーザー身元伝搬機能は無い**（docs リファレンス確認済み）。
- OC は「単一信頼オペレータモデル」（per-user 認可は MCP 境界でやれという設計思想）。
- `mail_*` / `workspace_search` は `ctx.metadata["user_email"]` 必須＝身元不明では fail-closed。
- EC2 worker は `slack_bot.py` 内で `SlackClient.resolve_identity(user_id)` を呼んで email を解決し、SkillContext に注入する経路を持つ。

### 1.3 メール機能が「以前動いていた」の正体
- 営業が自分の Slack DM で「○○社のメール要約して」と頼む
- OC と EC2 worker 両方がイベントを受ける
- OC は intent.py 相当の判定機構を持たない＋mail_* exclude なので何もしない（or 生応答）
- EC2 worker の `intent.py` の `_MAIL_SUMMARY_RE` がマッチ → `mail_summary` Skill 実行 → 本人 DM に ephemeral 返答
- これが 2026-06-08（PR#119）以降の正常稼働経路

### 1.4 今朝（2026-06-18）止まっている理由
- `connect-web` (`connect.newstv.co.jp`) が `OAUTH_STATE_SECRET` 未設定+SG ingress 抜けで 500 → per-user OAuth 連携が壊れた
- 既に連携済の本人 token は RDS に残っているので mail_summary 自体は動く**はずだが**、新規連携や再認証は不可
- **PR#127 で恒久対策中**（CONFLICTING/DIRTY・main宛・別セッション対応中）

---

## 2. やること（workspace_search を Slack DM 経路へ乗せる）

### 2.1 全体像
**既存のメール機能の設計パターンを完コピ**するだけ。新規概念ゼロ。

| 項目 | mail_summary（既存） | workspace_search（追加） |
|---|---|---|
| intent regex | `_MAIL_SUMMARY_RE` | **新規 `_WORKSPACE_CALENDAR_RE` + `_WORKSPACE_CONTACTS_RE`** |
| dispatch | `run_mail_summary()` | **新規 `run_workspace_search()`** |
| 受付メッセージ | `_ACK_BY_SKILL["mail_summary"]` | **新規 `_ACK_BY_SKILL["workspace_search"]`** |
| 配信方式 | ephemeral（本人のみ） | **ephemeral（本人のみ・同じ扱い）** |
| _PRIVATE_SKILLS | 登録済み | **追加** |
| TokenStore | per-user token | **per-user token（既に factory.py 配線済み）** |

### 2.2 トリガー設計
**カレンダー**:
- `_WORKSPACE_CALENDAR_RE`: 「予定」「カレンダー」「スケジュール」「ミーティング」「会議」
- 例: 「来週の予定まとめて」「明日のスケジュール教えて」「今日のミーティング何時から？」
- → `WorkspaceSearchInput(service="calendar", query=<メッセージ全体>, limit=20)`

**連絡先**:
- `_WORKSPACE_CONTACTS_RE`: 「連絡先」「メアド」「電話番号」「メールアドレス」
- 例: 「○○社の連絡先教えて」「田中さんのメアド」
- → `WorkspaceSearchInput(service="people", query=<メッセージ全体>, limit=20)`

**両方マッチ or どちらでもない場合**:
- カレンダー優先（営業の頻度的に多い想定）
- 「カレンダーと連絡先を見比べたい」は限定ケースなので一旦切る（将来 multi-service 対応）

### 2.3 改修するファイル
1. **`src/teamagent/skills/intent.py`**
   - `_WORKSPACE_CALENDAR_RE` / `_WORKSPACE_CONTACTS_RE` 定数を追加
   - `detect_skill()` 内に workspace_search 分岐追加（既存 `_MAIL_SUMMARY_RE` の隣・同パターン）
   - 戻り値の `SkillIntent` に `service` フィールド追加（calendar / people 判定済み値）
2. **`src/teamagent/runtime/slack_bot.py`**
   - `_ACK_BY_SKILL["workspace_search"]` 追加（例: 「📅 予定/連絡先を検索しています…」）
   - `_PRIVATE_SKILLS` に `"workspace_search"` 追加（ephemeral 配信＝メール系と同じ）
   - `SkillDispatcher.dispatch_auto()` に `elif intent.skill == "workspace_search":` 分岐追加
   - 新メソッド `run_workspace_search(query, request_id, user_id, service)` を追加
     - `WorkspaceSearchInput(service=service, query=query, limit=20)` を組む
     - `WorkspaceSearchSkill().run(input, ctx)` を呼ぶ
     - 結果を Slack 整形（hits を箇条書きに）
3. **`tests/`**
   - `tests/skills/test_intent.py` に workspace_search トリガーテスト2-3本
   - `tests/runtime/test_slack_bot_*.py` に dispatch_auto テスト1本

### 2.4 やらないこと（スコープ外）
- OC の include に workspace_search を入れる（→ user identity 伝搬問題があるので別案件）
- workspace_search の機能拡張（drive/docs/sheets/gmail への対応）
- 新しい service の追加（カレンダー+連絡先で十分）

---

## 3. 検証

### 3.1 ローカル
- `uv run pytest tests/skills/test_intent.py tests/runtime/ tests/skills/workspace_search/ -q` → 緑
- `uv run pytest tests/ -q` フルスイート緑（既存テスト無傷）
- `ruff check` / `ruff format --check` / `mypy src/teamagent` 緑

### 3.2 本番（前提あり）
1. **PR#127 のマージ＆デプロイ完了** で connect-web が復活していること
2. このPRをデプロイ（EC2 worker）
3. `teamagent-bot.service` を restart
4. Slack DM で `@AiLa 来週の予定まとめて` → カレンダー予定が ephemeral で返ってくる
5. Slack DM で `@AiLa ○○社の連絡先教えて` → 連絡先 hit が ephemeral で返ってくる
6. **失敗ケースの確認**:
   - 未連携ユーザー（s-komata 以外）→ 「まず connect してください」と graceful
   - クエリが不適切 → fail-closed（生データを返さない・既存 G1〜G3 維持）

### 3.3 セキュリティチェック
- ephemeral 配信のみ（チャンネルにブロードキャストしない）→ G3 維持
- DLP マスク既存（`scrub_value`）→ G2 維持
- per-user OAuth fail-closed → G1 維持
- これらは workspace_search Skill 内部で実装済みなので**新規セキュリティリスクは無い**

---

## 4. リスクと縮退

| リスク | 対策 |
|---|---|
| PR#127 が片付かないと per-user OAuth が壊れたまま→このPRも無意味 | 順序を守る（PR#127 → workspace_search PR） |
| OC が「カレンダー予定を…」に対して別の返答を出す（二重応答） | OC の応答内容は変えられない（公式pluginなので）。EC2 経路の返答が本物として、OC の生返答はノイズとして許容 or DM では OC を止める設定変更を検討 |
| Google Calendar API のレート制限 | workspace_search Skill 内で既に retry/timeout 実装済み |
| カレンダーの取得期間が広すぎて重い | `limit=20` で上限を切る。「来週」のような期間語は Google Calendar の query パラメータで解決される |

縮退の成立：
- workspace_search 失敗 → ephemeral で「取得に失敗しました」が返る（既存パターン）
- PR#127 完了前にこれをデプロイしても、connect が壊れているので **他人 token は無いが s-komata 本人 token は残っている**ので本人 DM では試せる（リハーサル可能）

---

## 5. 工数見積もり

| 項目 | 見積もり |
|---|---|
| intent.py 改修 + テスト | 30分 |
| slack_bot.py 改修 + テスト | 60分 |
| ローカル CI 緑化 | 30分 |
| PR作成 + レビュー対応 | 30分 |
| **合計** | **2.5h** |

デプロイ（人間ゲート）は PR#127 完了後・別作業。

---

## 6. 開いた論点（あなたの判断が要る）

- **Q1. カレンダー連携のメッセージ表現どうする？**
  - 例：「📅 予定を確認しています…（5〜15秒）」 vs 「🔎 カレンダーを検索中…」 vs 営業向けの口語
- **Q2. 検索期間のデフォルトは？**
  - 「来週」「明日」のような期間語が無いとき、Google API の既定（今日以降全て）でいいか、それとも今後7日に絞るか
- **Q3. multi-service（カレンダー+連絡先）対応は？**
  - 例：「○○社のメアドと、来週の○○社訪問予定」みたいな複合クエリ
  - 一旦切る（calendar優先）でOKか

---

## 7. 関連ドキュメント

- `docs/v3.2/skill_inventory_2026-06-18.md` — 全 Skill 棚卸し（mail/workspace_search の現状）
- `docs/openclaw/deploy_runbook.md` — OC デプロイ手順
- `docs/poc/workspace_integration_design.md` — per-user OAuth 設計
- 直近の関連 PR: #119（メール4機能・merged）/ #122（connect harden・open）/ #127（本番インシデント・CONFLICTING）

---

*この文書は調査結果の設計提案です。コード変更未実施。次回セッションで「§2.3 改修するファイル」から実装に進めます。*
