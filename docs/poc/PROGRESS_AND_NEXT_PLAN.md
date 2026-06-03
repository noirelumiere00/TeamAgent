# マルチSkill適応オーケストレーター — 進捗 & 次フェーズ計画

> 2026-06-03。worktree `teamagent-orchestrator-poc`（branch `poc/multiskill-orchestrator`）。
> 基盤＝**Claude Agent SDK on Bedrock**（評議で採用。OpenClaw不採用／自前ループはフォールバック温存）。
> 本書は「現状TODOに対する到達点」と「Agent協議で固めた最適な次フェーズ計画」をまとめる。

---

## 0. 現在地（ひと言）
**PoC（実Bedrockで適応提案を完遂）＋ Phase 0 堅牢化まで完了・コミット済（`ba0d3ff`→`2bd10d0`）。** 動作は **fixtureスキル**段階で、**実Skill接続・本番runtime統合・Mail/Drive横断は未着手**。次は **Phase 1（実 `search` を隔離環境で接続）**。

---

## 1. 完了（DONE）✅ — commit `ba0d3ff`

- [x] 基盤選定をAgent評議（OpenClaw / 自前 / Agent SDK）→ **Agent SDK on Bedrock 採用**（`docs/poc/agent_orchestrator_poc_findings.md`）
- [x] オーケストレーター中核 `src/teamagent/orchestrator/`
  - [x] `sdk_runner.py`：既存Skillを `@tool` 化→SDKループ（方式B）
  - [x] `loop.py`（自前ループ＝比較/フォールバック）/ `decider.py`（`LLMDecider` 抽象・差し替え口）/ `tools.py`（`ToolSpec.factory` = 実Skill差し替えの唯一のseam）
- [x] **6-bis per-call コストログ**：`AssistantMessage.usage` から呼び出し毎 cost/token を `request_id` 付き出力（last-wins dedup、最終回答は `ResultMessage.result`、コストは SDK実コストを正）
- [x] ガードレール：SDKネイティブ `max_turns` + `max_budget_usd`、preflight（`aws sts` で資格情報実検証）
- [x] **実Bedrockで適応シナリオ完遂**（履歴→認知滑り検知→CV転換→施策→Mail NG→差替→Drive裏付け→**文章で最終提案**）。1run ≈ $0.10
- [x] 品質ゲート：**pytest 7（offline）/ ruff / mypy strict 緑**、`docs/poc/`（設計・採否findings・本書）

> 補足：この完遂は **fixtureスキル＋プロンプト誘導** での実証。「ループ機構・6-bis・SDK on Bedrock の実在性」は固いが、**実データでの提案品質はまだ未測定**（Phase 4 の gold set が要る）。

---

## 2. 未完（TODO）= 次フェーズ計画（Agent協議の統合版）

### ✅ Phase 0 — 本番前“堅牢化”（**完了**・commit `2bd10d0`）
Red-team指摘の致命リスクを実装で対処。`tests/orchestrator/test_phase0_hardening.py` で検証（pytest 15 / ruff / ruff format / mypy strict 緑）。
- [x] **エラー/予算/拒否の可観測性**：`ResultMessage` の is_error/subtype/permission_denials を読み `stopped_reason` に反映（`classify_result`）。打ち切りは劣化回答でなく明示
- [x] **RLSコンテキスト注入**：`_make_handler` が `SkillContext` に user_id/metadata(user_email等) を伝播。`run_sdk_agent(require_rls=True)` で fail-closed
- [x] **try/except + per-tool timeout**：失敗を構造化エラー(is_error)で返す（ループを落とさない）
- [x] **同期Skillを `run_in_executor` + timeout**（Slackイベントループ非阻害）
- [x] **同一ツール×同一入力の連続呼び出しを機械的に拒否**（無限ループ殺し）
- [x] **コスト較正**：`ResultMessage.model_usage` を保持（SDK実コストが正、`Price` は概算）
- [x] **SDK `==0.2.87` 厳密pin**（pyproject）＋ ci.yml 依存列挙（[[feedback_ci_no_deps_manual_enumeration]]）
- 残（任意・小）: `session_id` 記録の追加、`Price` の env/config 化

### 🟡 Phase 1 — 実Skill 1個（`search`）を**隔離環境**でE2E（コード実装済・commit `741a0be`／ライブ検証 待ち）
- [x] 本番ToolSpec工場 `orchestrator/factory.py`（`build_production_tools()`）、`search` を `factory` で注入。env フラグは `slack_bot.py:get_search_skill` と一致。重い依存は遅延 import
- [x] ライブ実行スクリプト `scripts/run_orchestrator_prod.py`（preflight + RLS注入 + timeout 90s）。本番 slack_bot には非干渉
- [x] 軽量スモークテスト `test_factory_smoke.py`（ruff/format/mypy 6files/pytest 17 緑）
- [ ] **ライブ検証（要 full env + SSMトンネル + Bedrock）**: `search` が呼ばれ 6-bisログ＋最終回答が出る（← 次に人間がやる）
- DoD: 実 search で適応提案が出る。orchestrator gold set 10本（Phase 4）で緑

### Phase 2 — 複数“既存”Skillで適応（新Skill不要）
- [ ] `clientkarte → proposal_draft → proposal_review`（必要なら `search`）を工場に追加。**`proposal_*` には同一 `SearchSkill` インスタンスを共有注入**（embedder二重ロード回避）
- DoD: 「クライアントXに次施策を提案して」で review 指摘→draft やり直しの適応分岐が実Claudeトレースで1本

### Phase 3 — runtime統合（opt-in・単発フロー併存）⚠️ゲート判断含む
- [ ] **既存 intent→単発dispatch には触らず**、新規の明示トリガ（例: 新Slashコマンド／`@TeamAgent 深く考えて`）で起動。`USE_ORCHESTRATOR` 既定OFF→特定ユーザー→全体の段階ロールアウト
- [ ] 全クエリをorchestrator化しない（$0.10/run・レイテンシ増）。**起動ゲート**＝多段・横断・データ依存分岐を要する要求のみ。1ユーザー/日上限＋月次予算アラート
- ⚠️ **ゲート判断**：本番ランタイムに **同梱Node CLI を持ち込む**＝findings Step2の「本番非持込ライン」を越える決定。ゲート①で明示再確認すること

### Phase 4 — オーケストレーション eval（gold set）
- [ ] 既存 gold set（検索hit rate中心・50ケース）とは**別軸**の orchestration gold set（goal→期待ツール列の部分集合／禁止手法回避／上限内）最低10本。temperature=0×複数seedで分散測定
- DoD: 「期待ツール列を踏む率」「cost/turn分布」を数値化。CIは決定的mockのみ（課金ゼロ）、実Bedrock evalは手動/nightly

### Phase 5 — コスト/レイテンシ最適化
- [ ] system_prompt/ツール定義を**安定化**して prompt cache（2回目以降 cache_read で1/10）。`max_turns` を実測p95に締める。**モデル分離**（ツール選択=Haiku 4.5／生成=Sonnet）を `model` 引数で検討

### Phase 6 — Mail/Drive 横断（**独立ゲート**・新Skill）
- [ ] `skills/mail_constraints/`・（`search` で代替不可なら）`skills/drive_cases/` を新設（adapter `gmail_client`/`gdrive_client` は既存、Skill層のみ）
- ⚠️ 本番前ゲート：**本人受信箱限定**（DWD impersonate先=リクエスト発行者に固定、LLMに受信箱選択権を渡さない）／**Mail本文はLLMに渡す前にDLPマスク・要約**（生本文をプロンプト/ログに入れない、6-bis）／readonly最小スコープ／**本人同意フロー**

---

## 3. 重大ゲート / 意思決定事項（先に握るべき）
1. **Node CLI を本番ランタイムに入れるか**：SDKは同梱Node CLIをsubprocess spawn。findings で「本番非持込＝死守ライン」と書いた一線を越える判断。ECS/Lambda に Node 24 を同梱する運用変更が伴う → **ゲート①で再確認**。
2. **Mail/Drive 横断のデータガバナンス**：PII/DLP/同意/最小権限を満たす独立ゲート（Phase 6）。「自分のMail分析」は本人受信箱限定が大前提。
3. **CLAUDE.md の更新**：先頭が今も「OpenClaw フル採用」のままで**今回の決定（SDK採用）と矛盾**。Claude Code が毎回読むので早急に訂正（OpenClaw→SDK、Step2の本番ラインも明記）。
4. **ゲート①の日付**：資料間で **6/7 と 6/12** 不一致。確定要。

---

## 4. リスク要約（Red-team killer-risk）
| # | リスク | 対策フェーズ |
|---|---|---|
| RLSコンテキスト喪失（越権/PII） | `_make_handler` が裸の `SkillContext` | Phase 0 |
| 打ち切り沈黙（予算/エラーで劣化回答を正常返却） | `stopped_reason` 固定・ResultMessageのerror未読 | Phase 0 |
| Slackイベントループ阻害 | async内で同期Skill直呼び＋Node subprocess | Phase 0 |
| 無限ループ/スタック（実LLMで再発） | プロンプト抑制は確率的 | Phase 0（機械的拒否） |
| コスト/レイテンシ暴走 | 起動ゲート無し・全件orchestrator化 | Phase 3/5 |
| Mail PII/越権 | DWDで全社員受信箱なりすまし可 | Phase 6（独立ゲート） |
| 改善/退行を測れない | orchestrator用gold set不在 | Phase 4 |
| Node版ドリフト | SDK同梱CLI・pin下限のみ | Phase 0（厳密pin） |

---

## 5. master TODO との関係
本件は `docs/v3.2/teamagent_master_todo_v1.md` の **「Sprint 7+ / C案＝multi-skill orchestrator」** に相当する先行PoC。当初ペンディングだった「OpenClaw採否／Agent SDK採否」を**実機で決着（SDK採用）**させた。本番統合（Phase 3）はゲート①の意思決定事項として上程する。
