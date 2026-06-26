# TeamAgent / AiLa Skill 棚卸し & 統廃合提案 — 2026-06-18

> 他セッション・他作業者向けの**読みもの**。コード変更はまだ実施していない（提案フェーズ）。
> 一次調査: 3 Explore エージェント並列・読み取り専用・branch `dev`（HEAD `6f22a7d`）。

---

## TL;DR（30秒で読む）

- 全 **22 Skill ディレクトリ** を棚卸し。**大きな機能被り（重複）は存在しない**（思ったより整理されてた）。
- 4 つあった「同名疑惑グループ」(`proposal*` / `video*` / `mail*` / `*search*`) は、ほぼ全て**段階や入出力が違う別物で並列維持が正解**。
- **整理対象は 3 つだけ**:
  1. **`vseo/`** — Skill クラス無し。`video_algorithm` の補助ユーティリティ。「Skill 化保留」状態。要決定。
  2. **`mail_constraints`** — Skill としては完全実装だが、本番 OpenClaw・Slack ホットパス両方から外れている＝**「実装したが運用されていない」**。要決定（独立プロダクト化 or 削除）。
  3. **`runtime/slack_bot.py`** — EC2 worker 専用で**本番では動いていない**（本番は OpenClaw Node）。OC合流が確定したら丸ごと削除候補。
- 命名の混乱が 1 件: `dir = proposal/` だが `Skill 登録名 = proposal_draft`、`dir = video/` だが `name = video_analysis`。これが「リポにそのディレクトリが無い」と誤読する原因。**dir 名を name と揃えるリネーム**を別途検討（差分大きいので別 PR・優先度低）。

---

## 1. 全 Skill 一覧（dir → name → 状態）

| dir | Skill 登録名 | 本番(OC) include | factory(env) | Slack Bot intent | テスト | 直近 commit | 判定 |
|---|---|---|---|---|---|---|---|
| `chitchat/` | `chitchat` | （特殊・常時ON） | 常時 | あり | ✓ | — | ✅ 本流 |
| `clientkarte/` | `clientkarte` | ✅ include | 常時 | あり | ✓ | — | ✅ 本流 |
| `search/` | `search` | ✅ include | 常時 | あり | ✓ | — | ✅ 本流 |
| `workspace_search/` | `workspace_search` | ❌ exclude | あり | あり | ✓ | — | 🟡 本番外（EC2のみ） |
| `operation_log/` | `operation_log` | ✅ include (env-gate) | `USE_OPERATION_LOG_TOOLS` | あり | ✓ | — | ✅ 本流 |
| `tiktok_search/` | `tiktok_search` | ✅ include | `USE_TIKTOK_TOOLS` | あり | 6 | `bf23ecb` | ✅ 本流 |
| `video/` | **`video_analysis`** | ✅ include | `USE_VIDEO_TOOLS` | あり | 9 | `959fea5` | ✅ 本流 |
| `video_algorithm/` | `video_algorithm` | ✅ include | `USE_VIDEO_TOOLS` | あり | 31 | `af94d82` | ✅ 心臓部 |
| `video_approval/` | `video_approval` | ❌ 未登録 | ❌ factory無し | 直接呼び | 8 | `788941a` | 🟡 Slack Bot 直配線（OC外） |
| `vseo/` | **（クラス無し）** | — | — | — | 11 | `e0ea32d` | ⚠️ Skill 化保留・要決定 |
| `proposal/` | **`proposal_draft`** | ✅ include | 常時 | 自動トリガー | 8 | `f42e972` | ✅ 本流 |
| `proposal_review/` | `proposal_review` | ✅ include | 常時 | 自動トリガー | 1 | `1d4a7fa` | ✅ 本流（テスト薄め） |
| `proposal_campaign/` | `proposal_campaign` | ❌ 未登録 | `USE_PROPOSAL_CAMPAIGN_TOOLS` | なし | 2 | `e72cea5` | 🟡 人間ゲート待ち |
| `proposal_deck/` | `proposal_deck` | ❌ 明示 exclude | `USE_PROPOSAL_DECK_TOOLS` | なし | 4 | `e72cea5` | 🟡 人間ゲート待ち |
| `mail_followup/` | `mail_followup` | ❌ exclude(mail_*) | `USE_FOLLOWUP_TOOL` | あり | 1 | `de55d71` | ✅ 営業4機能・EC2経路 |
| `mail_summary/` | `mail_summary` | ❌ exclude | `USE_MAIL_SUMMARY_TOOL` | あり | 1 | `de55d71` | ✅ 営業4機能・EC2経路 |
| `mail_reply/` | `mail_reply` | ❌ exclude | `USE_MAIL_REPLY_TOOL` | あり | 1 | `de55d71` | ✅ 営業4機能・EC2経路 |
| `mail_to_internal_context/` | `mail_to_internal_context` | ❌ exclude | `USE_MAIL_LINK_TOOL` | あり | 1 | `9fd9055` | ✅ 営業4機能・EC2経路 |
| `mail_constraints/` | `mail_constraints` | ❌ exclude | `USE_MAIL_TOOLS` | **なし** | 1 | `b5ac0a5` | ⚠️ 実装済だが運用外・要決定 |

凡例: ✅ 本流稼働 / 🟡 一部経路のみ稼働 or ゲート待ち / ⚠️ 要決定

---

## 2. 「被り疑惑」グループの真相

### グループ A：提案書系 4 個 → **全部別役割・統廃合不要**
1段階を担う「直列パイプライン」だった。

```
[Step 1] proposal_draft     新規案件 → 過去提案検索 → 骨子テキスト
   ↓ 自動トリガー "レビュー/添削/診断"
[Step 1.5] proposal_review  骨子 → 過去勝ち/失注照合 → 改善案
   ↓
[Step 2a] proposal_campaign  KW群 → TikTok 1位サムネ並列取得 → evidence_images (画像群)
   ↓
[Step 2b] proposal_deck     商材+研究素材+画像 → FMT v2 95項目 → PPTX 生成
```
- **入力/出力が完全に違う**（テキスト / 画像 / PPTX）
- proposal_draft と proposal_review は**自動トリガーで本番稼働中**
- proposal_campaign / proposal_deck は**人間ゲート（OC配線+本番反映）待ち**

### グループ B：動画/VSEO/TikTok 系 5 個 → **4 個は本流・1 個だけ要決定**
- `tiktok_search`（KW検索の取得） / `video_analysis`（単体URL分析） / `video_algorithm`（VSEO 30本検索→5本深掘り）／`video_approval`（編集者納品QA） — **全部役割が違う**。
- ⚠️ `vseo/` だけは **Skill クラス無し**（`dataprep.py` / `prepare.py` / `covers.py` のユーティリティ集）。memory に「video_algorithm が VSEO 本体」と書いてある一方で別 `vseo/` も存在＝**役割が中途半端**。
  - `video_algorithm` から呼ばれていない（grep で確認）。
  - 外部の Claude Skill（`~/.claude/skills/tiktok-vseo-proposal/`）からの呼び出しを想定した形跡あり。
  - 直近 commit `e0ea32d feat(vseo): VSEO提案書データ準備を自動化`（PR #100）以降進展なし。

### グループ C：メール系 5 個 → **4 本流 + 1 運用外**
- 4 機能（`mail_followup` / `mail_summary` / `mail_reply` / `mail_to_internal_context`）は PR#119 で本番投入済み・**営業向け正式機能**。EC2 worker 経路で稼働。
- ⚠️ `mail_constraints` は Skill としては完全実装だが：
  - **本番 OC** から exclude
  - **Slack Bot intent.py に未登録**（呼び出されない）
  - **factory env-gate も既定 OFF**（USE_MAIL_TOOLS）
  - = **実装したが運用されていない状態**
- memory `project_teamagent_mail_release.md` には「4機能」と書いてあるので、mail_constraints は実は「別フェーズの実験実装」。

### グループ D：検索系 3 個 → **役割が完全に違う**
- `search`（社内ナレッジ・本番） / `workspace_search`（Drive直接検索・本番外）/ `tiktok_search`（外部TikTok）—**全部別データソース**。被りなし。

---

## 3. 整理推奨（優先順）

### 🟢 即決できる軽い整理（低リスク）

#### A. `vseo/` ディレクトリの扱いを決める
**現状**: Skill クラス無し・`video_algorithm` から呼ばれない・外部 Claude Skill 用の置き場のような状態。

**選択肢**:
1. **`video_algorithm` に統合**（dataprep/covers を `video_algorithm/data/` 等のサブモジュールへ移動）
2. **正式 Skill 化**（`VSEODataPrepSkill` を作って独立運用）
3. **削除**（外部スキルが本当に使っているか確認後に）

**推奨**: **1（統合）**。理由：`video_algorithm` がVSEO本体である memory の文脈と一致するし、データ準備は video_algorithm の内部関心事。

#### B. `mail_constraints` の扱いを決める
**現状**: 完全実装だが本番外（OC exclude + intent.py 未登録）。

**選択肢**:
1. **独立プロダクト化**（intent.py に regex 追加＋同意ゲートを整備して営業に提供）
2. **削除**（4機能で十分という判断なら）
3. **保留・実験ステータスを明記**（factory.py のコメントに "experimental" 注記）

**推奨**: **3（保留＋注記）**。理由：実装に労力かかっているし、6c フェーズ（施策ゲート）の先行投資。今すぐ削除すると将来再実装になる。

### 🟡 中期で対応（影響範囲広め）

#### C. `runtime/slack_bot.py` の処遇
**現状**: EC2 worker 専用で本番では動いていない（本番は OpenClaw Node が Slack 投稿）。私が PR#126 で改修したが、それも本番には効かない。

**選択肢**:
1. **OpenClaw 移行が完了したら削除**（あなたの要望）
2. **EC2 worker をフォールバック経路として温存**

**推奨**: **1**。条件は「OC で全 Skill 経路が live と検証できたら」。それまでは消さない。

#### D. dir 名 ↔ Skill 登録名のリネーム（混乱の元）
**現状**:
- `dir = proposal/` だが `name = proposal_draft`
- `dir = video/` だが `name = video_analysis`

**影響**: import 変更が多い・PR は大きくなる。**優先度低**（混乱はするが動作には影響しない）。やるなら別 PR で。

### 🔴 やってはいけない

- ❌ `proposal_*` 4 個を統合（段階が違うのに混ぜると逆に複雑化）
- ❌ `video_*` を統合（用途が完全に分かれている）
- ❌ `mail_*` 4 個を統合（PR#119 で本番運用中、影響大）

---

## 4. 命名混乱を解消するための簡易マッピング（即効性あり）

これだけ docstring か README に書いておけば、他セッションが混乱しなくなる：

```
ディレクトリ → 実際の Skill 登録名:
  src/teamagent/skills/proposal/           → "proposal_draft"
  src/teamagent/skills/video/              → "video_analysis"
  src/teamagent/skills/<dir>/              → "<name>" (上記以外は dir 名と一致)

OpenClaw config に書いてある名前は「Skill 登録名」(右側)。
```

---

## 5. 次のアクション提案（優先度順）

| # | アクション | 影響 | 担当 | 工数 |
|---|---|---|---|---|
| 1 | この文書を共有して他セッションと合意形成 | なし | 全員 | 即 |
| 2 | `vseo/` の扱いを決める → 統合 or 独立 Skill 化 | 小 | 開発者 | 0.5d |
| 3 | `mail_constraints` の experimental ステータスを factory.py に明記 | なし | AI 代行可 | 5分 |
| 4 | `runtime/slack_bot.py` の OC 合流後削除を Issue 化 | なし | AI 代行可 | 5分 |
| 5 | dir リネーム（`proposal/` → `proposal_draft/`、`video/` → `video_analysis/`）の検討 | 大 | 別 PR | 1d |
| 6 | PR#126（Slack 段階表示）の扱いを決める — 本番では効かないので close するか保留するか | なし | あなた | 即 |

---

## 6. 補足：本番アーキテクチャの現状（混乱回避用）

```
Slack ──→ OpenClaw (Fargate Node, teamagent-dev-openclaw)
              │
              │ MCP プロトコル over HTTP
              ▼
         TeamAgent MCP (Fargate Python, teamagent-dev-mcp)
              │
              │ factory.py → Skill 登録 → MCP ツール公開
              ▼
         [Skill 群] search, clientkarte, proposal_draft, proposal_review,
                    tiktok_search, video_analysis, video_algorithm,
                    operation_log   ← OC include に列挙されたものだけ公開

EC2 worker (teamagent-dev-worker):
   ├ slack_bot.py（本番では非アクティブ・OC 経路に移行済）
   ├ ingest_drive 等のバッチ
   └ メール系 Skill が動く想定経路（per-user OAuth が OC 外なので EC2 で動かす設計）
```

**重要**: 「Skill のコードを直しても本番に届くとは限らない」 — OC include に入っていない、または EC2 経路のものは、別途デプロイ判断が必要。

---

*この文書は読み取り専用調査の結果です。コード変更は未実施。次回セッションは「§5 次のアクション」から実装に進むことを推奨。*
