# 基盤選定 評議結果・採否結論（OpenClaw vs Agent SDK vs 自前ループ）

> 2026-06-02。擁護3名（OpenClaw / Agent SDK / 自前）＋ Red-team の評議を、一次資料・実機・公式docsで裏取りして統合。
> 判断対象＝「マルチSkill横断＋適応型の自律エージェント」（履歴→認知滑り検知→CVへ方針転換→案→Mail NG→差替→Drive裏付け→統合）。

## 🔄 UPDATE（2026-06-02 夜・追加検証で結論を更新）

評議直後、②の唯一の急所だった **「6-bis＝Bedrock呼び出し“毎”のcost/tokenログ」が満たせるか** を SDK 実体で検証 →
**`AssistantMessage.usage`（per-turn）で取れることを確認**（Red-teamの「セッション集計のみ」は不正確）。
これにより②の採用根拠が立ち、**意思決定者の判断で ② Claude Agent SDK on Bedrock を採用**に更新。
実装は `src/teamagent/orchestrator/sdk_runner.py`（Skillを@toolでツール化＋per-callコストログ）。
6-bis抽出は `tests/orchestrator/test_sdk_cost_logging.py` で**SDK実型に対し緑**（ruff/mypy strict/7 tests green）。
自前ループ（③）は **比較・フォールバック資産**として温存（`LLMDecider` 差し替え口）。ライブ往復は Bedrock 資格情報待ち。

## 結論（採否）— 更新後

| 候補 | 判定 |
|---|---|
| **② Claude Agent SDK on Bedrock** | ✅ **採用**（適応ループが題そのもの＋6-bisコストログ実証済）。ライブ検証が残（資格情報待ち） |
| **③ 自前ループ（D案）** | 🟡 比較・フォールバックとして温存（PoC緑・差し替え口あり） |
| **① OpenClaw 採用（B案）** | ❌ 不採用（言語境界＋供給網リスク＋ゲート①に間に合わず） |

> 以下は評議時点（更新前）の分析記録。判断材料として残す。

## 結論（採否）【評議時点・参考】

| 候補 | 判定 |
|---|---|
| **③ 自前ループ（D案／現状継続）** | ✅ **今これを採る**（最小形で前進） |
| **① OpenClaw 採用（B案）** | ❌ 今回不採用（ゲート①でも D 案を推奨）。再浮上は「多チャネル展開が事業要件化」＋「供給網サンドボックスを回せる体制」が揃った時のみ |
| **② Claude Agent SDK on Bedrock** | 🟡 本番は今は非採用。将来 multi-skill が10+規模／並列・ストリーミング要件／増員のいずれかで再評価 |

## 評価軸まとめ（4軸＋運用）

| 軸 | ③ 自前ループ | ② Agent SDK | ① OpenClaw |
|---|---|---|---|
| **メリット** | cost/log/request_id を**コードで完全制御**（PoC実証）。依存最小。273行で全把握。3層分離維持 | 自律ループ/tool-use/サブエージェント/context が**既製**。Anthropic純正で挙動追従 | 多チャネルが設定化。Slackスコープ完全一致。AWS公式CFN東京対応。IAM RoleでAPIキー排除 |
| **デメリット** | tool-use adapter・ループ・再試行を**自作保守**（車輪の再発明） | **同梱Node CLIをsubprocess起動**（純Pythonでない）。コストは**セッション集計**で6-bis細粒度は要フック自作・未検証。制御フローがCLI実装にロックイン | TS/Node言語境界。**ClawHavoc供給網リスク**（341悪性skill）。2コンテナ。+1.5 Sprint |
| **拡張性** | LLMDecider差し替えで mock→Bedrock→SDK へ載せ替え可（袋小路でない）。並列/streamingは未対応 | サブエージェントでC案へ自然拡張 | チャネル方向は最強。ただしClawHub無効化前提でSkillエコシステム恩恵は実質享受せず |
| **この案件での適正** | **高**（規約厳格・本番稼働中・1-2名・既存7Skill・適応分岐は1ループで足りる） | 中（題そのものに直撃するが6-bis適合が未検証） | 低〜中（MVPには過剰。横展開ロードマップ確定が前提） |
| **メンテ性** | **高**（小コード・Python単一・型/lint/test緑） | 中（ループ保守をAnthropicに委譲できるが、Node CLI同梱・版ドリフト・規約適合検証が負担） | 低（2言語2コンテナ・CVE週次追跡＝CLAUDE.md「メンテ性最優先」と衝突） |
| セキュリティ | 現構成の自然延長＝爆発半径最小 | Node CLI＋外部ループのブラックボックス | ClawHub＋Node依存ツリーが新攻撃面（恒久） |

## 一次確認した決定的事実
1. **Agent SDK(Python)は同梱 Node 製 Claude Code CLI を spawn して動く**（公式docs明記）。「②なら言語境界を避けられる」は不正確（OpenClawより軽いが純Pythonではない）。
2. **SDKのコストは `ResultMessage.total_cost_usd`＝セッション集計1値**。6-bis（Bedrock呼び出し毎に input/output/cache_read/cache_creation/cost を request_id付きで）はフック自作前提＝未検証。
3. 現 `bedrock_client.py` は **converse（テキスト）のみで tool_use 非対応**（③も②も、実LLMで回すには tool-use 配線が必要）。
4. 本日の自前PoC（`src/teamagent/orchestrator/`）は **ruff/mypy strict/4 tests 緑**だが、適応判断は **ScenarioDecider（mock）**＝**実LLMでの自律判断はまだ未配線**。

## なぜ③か（決め手）
- **6-bis（呼び出し毎の cost/log/request_id）を“標準で”満たすのは boto3直叩きの③だけ**。①②は後付け／フック前提で未検証。本番稼働中システムの生命線。
- **言語境界は①②の両方が抱える**（②も同梱Node CLI）。③だけが Python 単一ランタイム＝1-2名チームの保守継続性で決定的。
- **本番影響リスク**：①は2コンテナ化、②はNode CLI同梱という運用の地殻変動。③は現構成の自然延長で爆発半径が最小。
- **ゲート①(2026-06-07＝5日後)**：5日で②の6-bis適合PoCも①の橋渡しPoCも完成しない。「OpenClaw不採用・適応ループは自前で薄く」をゲート①決定にするのが時間制約に最も誠実。

## ⚠️ ③採用の前提（Red-team K1 直視）
**今のPoC緑は mock 証明であって実LLM証明ではない。** ③を「実証済み」と呼ぶ前に **Step 0** を必須とする：
- `bedrock_client.converse_with_tools()` を実装し、**実Bedrockで①〜⑥トレースを1本緑**にする（mockでなく実Claude）。
- ここで「自前ループで6-bis準拠の適応が解ける」を実証 → ゲート①で確定。SDK掃除（pyproject:14）の判断材料も同時に出る。

## 段階戦略
- **Step 0（〜6/7）**: converse_with_tools 実装＋実Bedrockで①〜⑥緑（要 AWS資格情報/トンネル）。
- **Step 1**: 真にデータ依存な分岐は④NG差替の1箇所に限定。他は決定的workflowで固定（過剰なagent裁量を与えない＝コスト/暴走/監査性で有利）。
- **Step 2（保険）**: SDKは本番ランタイムに入れない（Node CLIを本番に持ち込まない一線死守）。開発時の探索ツールとしての隔離利用のみ可。10+規模化で再評価。
- **①OpenClaw**: 評価終了・不採用。子会社知見は「Slack運用Tips」としてのみ吸収。

## 未確認・要注意
- ゲート①日付が資料間で不一致：CLAUDE.md/overview=**2026-06-07**、実装計画書=**2026-06-12**。判断会議の確定日を要確認。
- Step 0 を5日で緑にできるかは中確度（converse_with_tools自体は標準だが、実Bedrock疎通・トンネル/権限・parse堅牢化に不確実性）。
