# マルチSkill適応オーケストレーター — 進捗 & 次フェーズ計画

> 2026-06-03。worktree `teamagent-orchestrator-poc`（branch `poc/multiskill-orchestrator`）。
> 基盤＝**Claude Agent SDK on Bedrock**（評議で採用。OpenClaw不採用／自前ループはフォールバック温存）。
> 本書は「現状TODOに対する到達点」と「Agent協議で固めた最適な次フェーズ計画」をまとめる。

---

## 0. 現在地（ひと言）
**Phase 0→1→2 ライブ達成。2026-06-03 に「実コーパスからの grounded 出力」を実証（commit `9d415d6`）。** 実RDS(9420 chunks)から chunk_id/Drive URL/見積数値つきの根拠付き提案を、実Bedrockで生成できることを確認（hit 10/5/5・top_score 0.49-0.86・is_error=False・$0.106・ハルシ無し）。**さらに Phase 6（Mail/Drive横断）の 6a設計・6b実装・6d配線まで完了（commit `f69bcbd`、`mail_constraints` スキル＝『MailのNG→別案差替』の中核、課金0・pytest 10 passed・既定OFF）**。**Phase 4 のオフライン採点土台も完了（commit `0b7bd2b`、gold set 10本＋決定的採点・pytest 26緑）**。残: Phase 4 実Bedrock eval 実行（課金~$1・承認待ち）、Phase 6c（実受信箱接続=人間ゲート後）、Phase 3（runtime統合=ゲート①）。

> ⚠️ **過去記録の訂正（重要）**: 旧版は Phase 1/2 を「DBは実質空・データギャップ・全クエリ0件」と記載していたが**誤り**。DB には実ショート動画/PR提案データ（9420 chunks）が存在し、RLS(email)で正常にアクセス可能。0件の真因は **3つのバグ**だった: ① SearchSkill要約モデルが実行リージョンと不一致（`jp.*` プロファイルを us-east-1 で呼び ValidationException）② `SEARCH_MIN_RELEVANCE=0.4` が borderline クエリの Rerank スコア(0.3)を足切り ③ SDKが cwd の CLAUDE.md を自動文脈化し古い記述でハルシ。①②③すべて対処済。

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
- [x] **ライブ検証 達成（2026-06-03）**: 実 pgvector(RDS/SSMトンネル) 接続成功・LLMが適応的に複数キーワードで search 実行・6-bisログ・SDK実コスト$0.20・`is_error=False/final`。Phase 0 が実DB障害(OperationalError, トンネル未起動時)を catch→構造化エラー→優雅終了 まで実証。
  - ※「BtoB SaaS採用」が0件だったのは**データギャップではなくバグ**（上記①②③）。同DBに**ショート動画/PR提案の実データが豊富**にあり、`s-komata@vectorinc.co.jp` の RLS で 30 raw hits（cosine 0.91）→ Rerank → grounded 出力まで到達済。
- DoD: 実 search で実データを引き、**chunk_id/URL/数値つきで grounded 回答**できる ✅（commit `9d415d6`）。残: orchestrator gold set 10本（Phase 4）で品質を数値化。

### ✅ Phase 2 — 複数“既存”Skillで適応（**ライブ達成 2026-06-03**・commit `a5aa5e6`）
- [x] `clientkarte / proposal_draft / proposal_review` を `factory.build_production_tools()` に追加。**`proposal_*` に同一 `SearchSkill` を共有注入**（embedder二重ロード回避）
- [x] **ライブ達成**: 「森ビル向け次施策を提案＋レビュー」で proposal_draft＋**proposal_review（勝ち筋照合・リスク診断）まで実行**し、4施策＋フェーズ計画＋セルフレビュー＋ネクストアクションの提案を実Bedrockで生成。多段が成立
  - ※当時 0件だったのはバグ（上記①②③）。修正後は proposal_* も含め実データで grounded 出力に到達
- DoD: 多段適応が実Claudeで成立 ✅。グラウンディングも実データで成立 ✅（commit `9d415d6`）。**残るは Phase 4 gold set での定量評価**（hit率・期待ツール列・cost分布）

### Phase 3 — runtime統合（opt-in・単発フロー併存）⚠️ゲート判断含む
- [ ] **既存 intent→単発dispatch には触らず**、新規の明示トリガ（例: 新Slashコマンド／`@TeamAgent 深く考えて`）で起動。`USE_ORCHESTRATOR` 既定OFF→特定ユーザー→全体の段階ロールアウト
- [ ] 全クエリをorchestrator化しない（$0.10/run・レイテンシ増）。**起動ゲート**＝多段・横断・データ依存分岐を要する要求のみ。1ユーザー/日上限＋月次予算アラート
- ⚠️ **ゲート判断**：本番ランタイムに **同梱Node CLI を持ち込む**＝findings Step2の「本番非持込ライン」を越える決定。ゲート①で明示再確認すること

### Phase 4 — オーケストレーション eval（gold set）｜**オフライン土台 完了**（commit `0b7bd2b`）
- [x] **gold set 10本＋決定的採点ハーネス（課金0）**：`src/teamagent/orchestrator/eval.py`（`GoldCase`/`score_case`純関数/`summarize`/`GOLD_CASES`）。goal→期待ツール列の部分集合（`expect_all`/`expect_any`）・禁止（`forbid`）・反復上限（`max_turns`）・`needs_flags`。`sdk_runner` に `tool_calls`（呼び出し列）を追加（後方互換）。`tests/orchestrator/test_orchestration_eval.py` で採点の全分岐＋ゴールドセット健全性を検証（pytest 26緑）。
- [ ] **実Bedrock eval 実行（課金 ~$1・手動ゲート）**：`scripts/eval_orchestration.py`（実装済・未実行）。非mail 8ケースを実行→「期待ツール列を踏む率」「cost/turn分布」を数値化。mail系2本は 6c ゲート後に `USE_MAIL_TOOLS=1` で評価。複数seed分散測定は次段。
- DoD: 採点ロジックはCI決定的（課金0）✅。実数値化は手動eval実行で取得（コスト承認待ち）。

### Phase 5 — コスト/レイテンシ最適化
- [ ] system_prompt/ツール定義を**安定化**して prompt cache（2回目以降 cache_read で1/10）。`max_turns` を実測p95に締める。**モデル分離**（ツール選択=Haiku 4.5／生成=Sonnet）を `model` 引数で検討

### Phase 6 — Mail/Drive 横断（**独立ゲート**・新Skill）｜📐**詳細設計済み → `docs/poc/phase6_mail_drive_design.md`**
- [x] **6a 設計（完了 2026-06-03）**：スコープ確定＝**`mail_constraints` が本丸**（Gmailはpgvector外＝本人受信箱ライブ）。**Drive裏付けは既存 `search` で大半カバー**（`search` schema に `source_uri='gdrive://FILE_ID'` 完備、取込済みならRLS付きgroundedで引ける）→ `drive_cases`(ライブ)は必要時のみ後回し。adapter（`GmailClient.from_env(readonly=True)`/`GDriveClient`）は既存再利用、**Skill層のみ新設**。DLPは `observability/sentry.py:scrub_value()` 流用。
- [x] **6b オフライン実装＋テスト（完了 2026-06-03・commit `f69bcbd`）**：`skills/mail_constraints/`（schema.py/skill.py/__init__.py）。**fake GmailClient/Bedrock** で run() 単体テスト＝DLPマスク(G3)・fail-closed(G1本人受信箱/G2同意)・構造化戻り値・**注入耐性(G6)**(悪意メール本文の指示に従わない=システム規則+`<<<MAIL>>>`区切り)・クエリ限定(G5)・parse堅牢性。**pytest 10 passed**・ruff/format/mypy strict 緑。既存adapter(extract_plain_text/scrub_value/BedrockClient.converse)再利用、google系は遅延importでci.yml変更不要。
- [x] **6d orchestrator配線（完了 2026-06-03・同 commit）**：`factory.build_production_tools()` に `USE_MAIL_TOOLS`(**既定OFF**)でToolSpec追加。`run_orchestrator_prod.py` はフラグON時のみ「NG→差替」フローをsystem_promptに付与。ON/OFF検証済(OFF=従来4ツール/ON=+mail_constraints)・orchestrator回帰17 passed。
- [ ] 6c ライブ(本人1名オプトイン)【⚠️人間ゲート後】 → 6e 評価(NG検知→差替 gold set)
- ⚠️ **死守ライン7条**（設計書§4）：G1本人受信箱限定(impersonate=requester固定/LLM選択不可/fail-closed)・G2本人同意オプトイン・G3生本文をLLM/ログ/戻り値に入れない(scrub前進配置)・G4 readonly最小スコープ(書込ツール非公開)・G5クエリ限定(無差別走査禁止)・G6**プロンプトインジェクション対策**(メール=データであり指示でない/読取専用)・G7監査ログ(本文なし)
- ⚠️ **6c以降の人間ゲート**（設計書§9）：本人同意フロー方式・DWD運用確認・CASA(gmail.readonly Tier3)・監査ポリシー。**6bまでは安全に課金0で着手可**

---

## 3. 重大ゲート / 意思決定事項（先に握るべき）
1. **Node CLI を本番ランタイムに入れるか**：SDKは同梱Node CLIをsubprocess spawn。findings で「本番非持込＝死守ライン」と書いた一線を越える判断。ECS/Lambda に Node 24 を同梱する運用変更が伴う → **ゲート①で再確認**。
2. **Mail/Drive 横断のデータガバナンス**：PII/DLP/同意/最小権限を満たす独立ゲート（Phase 6）。「自分のMail分析」は本人受信箱限定が大前提。
3. ✅ **CLAUDE.md の矛盾を訂正済み**（poc/multiskill-orchestrator ブランチ・2026-06-03）：L13/L37/L420 の「OpenClaw フル採用で確定／不採用にしない」断定を「**再評価中・ゲート①(6/07)で最終確定。orchestrator PoC は SDK on Bedrock 採用・OpenClaw不採用を推奨**」へ修正（L168-178 の status-change 注記と整合）。プラットフォーム全体の採否はゲート①の領分として温存（断定回避）。※本ブランチのみの修正なので **main へは merge 時に反映**（または cherry-pick）。本番ブランチ feat/video-approval-sheet-writeback には非干渉。
4. **ゲート①の日付（不一致の解消）**：**6/07＝ゲート①判断会議日**（CLAUDE.md/overview/findings で一致する支配的日付）、**6/12＝Sprint 2 W2 ウィンドウ末日**（2026-06-06〜06-12）。両者は別物で矛盾ではない（会議は週内の6/07）。最終確定日は会議主催者に要確認。※CLAUDE.md本体への日付補足追記は auto-mode が自己改変として拒否したため、本書に記載（CLAUDE.md L178 は既に「2026-06-07」で正）。

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
