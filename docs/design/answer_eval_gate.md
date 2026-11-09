# Aico 便の前の答え試験（リリースゲート）設計

2026-11-09／設計のみ。A4縦・6頁想定（本文9pt・表8pt・余白14mm・各節で改頁）。根拠は調査時点の作業木。リンクの表示はファイル名:行、リンク先はリポジトリ相対パス。

## 1. 結論と既存部品の棚卸し

**新イメージのスキル試験と、OpenClawが最終回答を作る試験を併用する。apply前に自動実行し、必須問の失敗・未完走・証跡不足で止める。** 初週は24問。社員の実DMを正解表へ移さず、一般化した質問と架空の固定データを使う。

背景の「良い」率24.8%、12月50%目標は依頼上の前提。今回、集計や本番データは未確認。答え試験の合格率は社員満足度とは別の指標とする。

| 既存部品 | 測れること／再利用 | 測れないこと・接続時の注意 | コードの根拠 |
|---|---|---|---|
| `scripts/routing_eval/` | 84発話。初手の道具、引数、禁止、拒否。採点・差分・JSONL記録を再利用 | MCPは代役。実行後の件数・出典・最終回答は対象外。実推論は現在手動専用 | [README.md:3–11,26–42](../../scripts/routing_eval/README.md#L3) |
| 同 `scoring.py` / `report.py` | 決定的な採点。必須障害例・禁止・引数違反・APIエラーで停止する集計 | 聞き返しは定型句による近似。内部名漏出は参考値。実行器だけでは採点不合格を止めない | [scoring.py:133–138,180–238,349–362](../../scripts/routing_eval/scoring.py#L133)、[report.py:189](../../scripts/routing_eval/report.py#L189) |
| `scripts/run_eval.py` | 検索top-1/top-5、MRR、偽の「無い」、費用・時間。検索側の診断に使う | 最終回答を採点しない。既定コンテキストがadmin。完走すると採点失敗でもexit 0。流用時に本人権限と終了判定を替える | [run_eval.py:128–198,576–586](../../scripts/run_eval.py#L128) |
| gold構造テスト | YAML、ID重複、質問の長さ・期待値の型を検査。形式検査の型を再利用 | 正解内容、匿名性、閲覧権限、答えの品質は保証しない | [test_gold_set_structure.py:13–66](../../tests/eval/test_gold_set_structure.py#L13) |
| `tests/eval8_checks.py` | 本文・添付名・引用・時間への決定的チェック。AI判定は除外 | 外部checker依存。環境変数未設定なら何もせず戻る。初週の必須依存にはしない | [eval8_checks.py:12–57](../../tests/eval8_checks.py#L12) |
| `scripts/eval_orchestration.py` | 必須／禁止の道具列、ターン上限、引用IDの捏造 | 別SDKのagentでありOpenClawではない。失敗でもexit 0。URL・最終件数は対象外 | [eval_orchestration.py:29–43,61–80,107–132](../../scripts/eval_orchestration.py#L29) |
| 検索の件数・短縮処理 | 件数解釈、重複排除、不足表示、引用なし時の退避を再利用 | 「5件要求でも引用付き短文を維持する」テストがあり、独立した件数検査が必要 | [answer_mode.py:64–84](../../src/teamagent/skills/search/answer_mode.py#L64)、[compact.py:443–488](../../src/teamagent/skills/search/compact.py#L443)、[test_requested_count.py:77–127,151–161](../../tests/skills/search/test_requested_count.py#L77) |

`scripts/user_eval` は作業木に存在しなかった。既存goldの確認待ち問は除外されるが、新ゲートの必須問では黙って除外しない（[run_eval.py:86–93](../../scripts/run_eval.py#L86)）。引用IDの既存検査は数字のchunk ID用で、無引用も正常扱い。URL必須検査の代用にはならない（[faithfulness.py:21–36,49–73](../../src/teamagent/orchestrator/faithfulness.py#L21)）。

<div style="break-after: page; page-break-after: always;"></div>

## 2. 正解表と実行場所

以下は新規設計。現行に答え試験の関門があるという意味ではない。

| 分野 | 100問時 | 初週24問：固定／実読取 | 架空・一般化した質問例 |
|---|---:|---:|---|
| 資料探し | 20 | 3／2 | 「飲料の提案資料を3件見せて」 |
| 事例 | 20 | 4／2 | 「飲料系の事例を5件出して」 |
| 案件 | 12 | 2／1 | 「飲料案件の進み具合を2件教えて」 |
| Slack | 14 | 1／3 | 「公開チャンネルから、展示会の振り返りを3件探して」 |
| 予定 | 10 | 2／0 | 「明日の予定を教えて」 |
| メール | 10 | 2／0 | 「自分の未読メールを2件要約して」 |
| 数値計算 | 8 | 1／0 | 「12万円を3回使い、残額4万円。総予算はいくら？」 |
| 資料作成 | 6 | 1／0 | 「架空の飲料向け提案を、見出し3つで本文だけ作って」 |
| **合計** | **100** | **16／8** | 実DM・実名・メールアドレスは登録しない |

**正解表はversion付きYAML。** 既存の`version/cases`とルーティングの`expect/forbid/arg_rules`の考え方を使う。約100問を初回に全部書かず、確認済みだけ昇格する。各問は`id/domain/utterance/data_mode/fixture_id/runner/expected_tools/forbid/required_lanes/requested_count/item_key/eligible_refs/checks/max_chars/time_limit_s/critical`を持つ。`item_key`は資料・事例・案件ごとの一意項目の定義。曖昧質問だけ`allow_ask=true`。計算問は`operands/operation/unit/rounding/expected`も持つ。実読取の正解参照は別のアクセス制限付き台帳に置き、リポジトリには別名だけ置く。

```yaml
version: answer-gate-v1
cases:
  - id: case-count-005
    domain: cases
    utterance: "飲料系の事例を5件出して"
    data_mode: fixture
    fixture_id: beverage-many-v1
    runner: [skill, openclaw]
    expected_tools: [search]
    forbid: [knowledge_deliver, mail_draft, calendar_event]
    required_lanes: [vault, slack, case]
    requested_count: 5
    item_key: case_ref
    eligible_refs: [ref-a, ref-b, ref-c, ref-d, ref-e]
    checks: [count, links, no_ask, no_internal_terms, length]
    max_chars: 2500
    time_limit_s: 120
    critical: true
```

固定データには「資料5・Slack17・案件3候補、重複排除後に関連する出典可能な5件」を用意する。数字を足して25件を正解にしない。引用脱落、0件、2件しかない場合、全角件数、検索先の失敗も変種にする。既存の架空データと退避検査を再利用できる（[test_requested_count.py:21–44,77–137](../../tests/skills/search/test_requested_count.py#L21)）。

| 候補 | 観測できる範囲 | 判断 |
|---|---|---|
| (a) 候補イメージのrun-taskでスキルを直接呼ぶ | 実際の検索・要約・コード整形。安価で原因を切り分けやすい | 初週24問。固定16問は取得adapterだけ架空化。検索・要約・整形は実コードで動かす |
| (b) OpenClaw外側ループから実ツールを呼ぶ | 道具選択→検索→最終可視回答。ツール結果を落とす失敗を含む | 初週は上の実読取8問も実行。さらに固定の5件脱落問を1回実行する。24問中の重複実行 |
| **(c) 両方** | (a)の出力と(b)の最終回答のどこで失ったか比較可能 | **採用。** MCP候補digestと現行OpenClaw・plugin・SOUL・設定を固定する |

(b)は既存の初手API呼出しへ結果を足すだけでは不足する。実OpenClawを完走させ、本人特定pluginも載せ、送信直前の本文を捕捉する。Slack APIへ接続せず受信形を組むdriverと配信callbackが土台になる。ただし現driverは架空ID・plugin除外の評価用で、本人経路・全量出力保存は追加が必要（[README.md:35–42](../../scripts/routing_eval/README.md#L35)、[slack_dm_driver.mjs:61–103](../../scripts/routing_eval/slack_dm_driver.mjs#L61)）。

<div style="break-after: page; page-break-after: always;"></div>

## 3. 決定的な合否と必要な記録

採点が決定的であっても、LLMの回答生成は揺れる。scorerは固定した正解表・観測記録だけを読む。AIの自己申告や「合格」という文は根拠にしない。

**判定用記録（新設）:** 問ID・run ID・request ID・ツール列／引数・各検索先の試行と成否・取得／適格／選択／表示参照・スキル出力・最終可視回答・送信要求・使用量・時間を対応付ける。実読取の本文は隔離メモリ内で採点し、通常の成果物には件数・チェック結果・HMAC化参照・回答ハッシュだけ残す。固定データの回答は再採点用JSONLに保存できる。

| 必須チェック | 決定的な規則 | 使う出力・ログ／限界 |
|---|---|---|
| 件数どおり | 要求N、正解台帳の適格な一意項目Aに対し、表示は`min(N,A)`件。A<Nなら実数と不足を明記。重複・無関係・架空出典は不算入 | スキルの`displayed_refs`と最終本文の項目・URL対応。URL総数だけは数えない。同一URLの別事例は`item_key`で区別。既存一覧形式でparseし、判別不能なら失敗 |
| リンクあり | 各項目に取得済みの許可HTTPS URLがある。URLと項目の参照IDが正解集合に一致する。署名付きURL・途切れURLは禁止 | 取得元URL対応表＋最終可視本文。構文確認と本人閲覧可否は別。単にリンクがあるだけで内容の支持までは保証しない |
| 見つかったら聞き返さない | 適格出典があり`allow_ask=false`なら、聞き返し・ブランド指定要求・不在表現の凍結辞書に一致しない | 最終本文。既存`response_kind`の規則を拡張。定型句検査の範囲に限る。意味が同じ全言い換えの検知は保証しない |
| 「見つからない」の前に広く探す | 負例は`required_lanes`全部が実行成功し、台帳上の適格項目が0件。無関係なraw hitは許す。指定の同義語展開も実行済み。timeout・未連携・skippedは成功と数えない | `search_fanout_lanes`、検索引数、展開の試行記録、`search_found_decision`。`complete=true`だけで合格にしない |
| 内部語を出さない | ツール名、`teamagent__`、内部引用マーカー、内部URI・ID裸出しが0 | 最終可視本文と添付ラベル。URL内の正当なIDは除外。許可URLを先に抽出し、残りを共通禁止辞書・ID形式で検査 |
| 長さ上限 | 本文・blocksから復元した可視文とラベル全体が問の上限以内。既定2500 Unicode code point | 配信直前出力。切詰め前だけ合格、切詰め後に件数・リンク欠落なら不合格。複数回答の合計も測る |
| 数値一致 | 固定入力をscorerのDecimalで独立計算。単位・桁・丸めを正解表で固定し、指定結果欄と一致する | 計算入力、計算ログ、最終本文。上例は400,000円。数字が元資料内にあるだけでは合格にしない |
| 道具・安全・完走 | 許可された実行列、必須道具、引数契約、最大ターン・時間、外部送信0。必須問のskip・ログ欠損0 | 全tool call、送信sink、task exit。未知の道具や出力形式は失敗。不在とAPI障害を区別する |
| 予定・メール・資料本文 | 予定は固定時計JST・期待日時／空き枠、メールは固定参照と件数、資料は指定見出し・空でない本文・禁止送信を検査 | 初週は架空adapter応答。PPTX/PDF生成の見栄えや実送付、メール下書き保存は範囲外 |

既存の根拠と再利用先:

| 既存観測・処理 | 使い方と不足 | 根拠 |
|---|---|---|
| `answer/hits/displayed_refs/found/sources` | 一覧ではhitsが空になる経路もある。表示参照と最終出力を併用する | [schema.py:340–372](../../src/teamagent/skills/search/schema.py#L340)、[fanout.py:1223–1266](../../src/teamagent/skills/search/fanout.py#L1223) |
| `search_found_decision` / `search_compact_fallback` | found・件数・理由／退避理由は使える。関連する候補の保持率は現ログだけで証明できない | [fanout.py:750–754,1089–1106](../../src/teamagent/skills/search/fanout.py#L750) |
| laneログとURL対応 | 必須先を別途照合。`complete`はskippedを排除しない。URL検査は疎通保証ではない | [fanout.py:938–945,1157](../../src/teamagent/skills/search/fanout.py#L938)、[compact.py:336–398](../../src/teamagent/skills/search/compact.py#L336) |
| 出力規則と数値検査 | 共通禁止語・上限を再利用。NumberGrounderは数の由来確認であり計算器ではない | [outbound_rules.json:64–91](../../infra/openclaw/caller-identity-plugin/outbound_rules.json#L64)、[grounding.py:193–234](../../src/teamagent/skills/_shared/grounding.py#L193) |

AI採点は初週に入れない。後から「質問との関連」「要約の意味の保持」「読みやすさ」だけを補助採点する。個人情報を渡さず固定データで実施する。ゲートの合否・原因確定・例外承認には使わない。

<div style="break-after: page; page-break-after: always;"></div>

## 4. 本人権限・無送信と、apply前の自動停止

| 安全条件（実装時に必要） | 既存の根拠／追加が必要な点 |
|---|---|
| 試験用本人1人に固定。member/RLSで実読取。管理者権限を使わない | gatewayは署名claimを検証し、LLM申告のemail/groups/roleを捨ててresolverで本人を決める。この経路を維持する。[server.py:760–803,1172–1191](../../src/teamagent/mcp_gateway/server.py#L760)。正解表に実識別子を置かず、実行設定で別名を解決 |
| データ面は読み取り専用。DBはSELECT専用資格情報。Google/Slackも本人の許可された読取操作のみ | カレンダーは既存read-only実装を再利用できる。[calendar_freebusy/skill.py:11–20](../../src/teamagent/skills/calendar_freebusy/skill.py#L11)。実IAM・OAuth scope・Slack可視範囲は未確認。DB専用ロールでも既存RLSが働くか導入前に確認 |
| 最終回答・直接投稿・進捗・添付・非同期通知を捕捉し、ネットワーク送信を禁止 | 通常の検索でも直接投稿する。[direct_search_answer.py:107–120](../../src/teamagent/mcp_gateway/direct_search_answer.py#L107)。OpenClawの送信callbackだけ止めても不足。全配信adapterをローカルsinkに替え、予定外の送信呼出しは不合格 |
| 業務DB書込、メール下書き、予定作成、ジョブ登録、S3業務出力、記憶・cache更新も禁止 | 進捗は`ENABLE_PROGRESS_NOTIFY=0`、usageは`USAGE_EVENTS_DISABLE=1`。それだけで全副作用を止めたとは扱わない。[progress_notify.py:64–66,98–104](../../src/teamagent/mcp_gateway/progress_notify.py#L64)、[server.py:184–185,1675–1696](../../src/teamagent/mcp_gateway/server.py#L184) |
| 認可台帳も本番へ書かない。隔離task内のreplay storeへ注入 | 通常のnonce消費はDynamoDB書込。既存注入口を使い署名・期限・一回性は維持する。[caller_claim.py:164–174,303–350,486–490](../../src/teamagent/mcp_gateway/caller_claim.py#L164)。試験主体だけを許す署名経路と送信sinkは未実装。claim検証の無効化は禁止 |
| 実読取の対象と保管を限定 | 社員DM履歴を入力・fixture・成果物に使わない。Slackは試験本人が見える公開ch、予定・メールは試験本人のみ。実本文をstdoutへ出さない。試験結果用の保管先だけ別途許可し、原文はtask終了で破棄 |

予定された配信は**同じ配信直前処理を通したローカルsink**に届ける。配信直前payloadを採点し、別証跡に`delivery_mode=capture, external_sent=false`を残す。成功後の分岐を模擬する場合も隔離したsimulation記録とし、本番のPOSTED／送信成功と混同しない。直接投稿後のフォールバックによる二重回答も検査する。読取APIは許可リスト、変更操作は拒否する。HTTPS全許可だけでは保証しない。固定16問は業務接続を全遮断。安全自己検査に失敗したら実読取を始めない。

便の流れは、**build・署名済み昇格 → 保存plan作成 → 自動答え試験run-task → 合格証跡 → guarded apply** とする。現`release_mcp.sh`は焼くまで（[release_mcp.sh:24,627–633](../../scripts/aws/release_mcp.sh#L24)）。applyは共有lockと既存receiptを必須とする入口を使い続ける（[infra/terraform/README.md:4–8](../../infra/terraform/README.md#L4)）。

| 関門の設計 | 条件 |
|---|---|
| 起動・待機 | 候補digest固定の一時TD/run-taskを使う既存型を再利用。現型はruntime校正用で答え試験ではない。停止・exit・実imageDigest検証を流用する。[terraform_runtime_guard.sh:8334–8366,8369–8622](../../infra/deploy/terraform_runtime_guard.sh#L8334)。既存の10分待機は新試験の時間予算に合わせ別設定が必要 |
| apply側の必須照合 | 試験本体はlock取得前。lock内の既存plan/intent照合後、saga開始前に短い証跡検証を追加する位置は同[13629–13674](../../infra/deploy/terraform_runtime_guard.sh#L13629)。実applyは同[13703–13711](../../infra/deploy/terraform_runtime_guard.sh#L13703)。未提出でもapply不可 |
| 証跡の結び付け | plan/context/intent/commit、変更consumerのdesired digest、候補・対照の実task digestと結果hash、正解表・scorer・runner・prompt・plugin・設定hash、model ID、本人別名hash、必要問数・全結果・費用・時刻を記録。対照の条件も一致させる。署名／検証可能な専用証跡にし、自己申告JSONだけで許可しない |
| 有効期限・既存統制 | 期限は試験開始から1時間、plan期限、intent期限の最小値。同じplan・設定のみ。既存plan receiptは最大1時間。同[10021–10030](../../infra/deploy/terraform_runtime_guard.sh#L10021)。別schemaのanswer-eval receiptを必須引数で渡す。既存receiptのキー完全一致検査へ勝手に項目追加しない。同[9908–9988](../../infra/deploy/terraform_runtime_guard.sh#L9908)。署名・lock・rollbackは維持 |
| 対象範囲 | MCP由来6consumerを含む対象はregistryとplanから確定し、全TDへ同一digestと決め打ちしない。全registryは8consumer。[image_deployment_consumers.json:5–121](../../infra/codebuild/image_deployment_consumers.json#L5)、[image_release_gate.tf:52–77,167–175](../../infra/terraform/image_release_gate.tf#L52) |

<div style="break-after: page; page-break-after: always;"></div>

## 5. 停止基準・費用と最初の1週間

| 判定 | applyへの扱い |
|---|---|
| 必須チェック失敗、禁止操作、権限逸脱、必須問のskip | **停止（品質不合格）**。初週は24問全部が必須。5件脱落問の外側ループ実行も必須 |
| task失敗、期限・費用超過、必要ログ欠損、読取先の障害・権限不足、正解台帳の前提崩れ | **停止（試験不成立）**。合格として扱わない。異常原因を直して全必須問を再実行 |
| 対照便より下がる | 同じ正解表・scorer・model/prompt条件で対照を採点。必須チェックのpass→failが1つでもあれば停止。既知の不合格を基準に相殺しない |
| 100問へ拡大後 | 必須問・安全条件は100%合格。追加の観測問も含む総合点は「全チェック合格の問数／固定母数」。95%未満、または対照より1問でも低下で停止。分野内の低下も停止。観測問も黙示除外しない |
| 再実行 | 失敗したLLM回答の引き直しで通さない。品質失敗は修正後に再実行。外部障害は回復確認後に再実行し、前回失敗も証跡へ残す。初週は1回ずつ、後に障害型を3回全通過へ |

対照は現行安定digestで同じ試験を行う。固定データは版を一致させる。実読取8問は、試験本人から見える既知の出典が必要件数あることを独立した読取確認で検証する。正解参照・版・可視権限が変わったら試験不成立。候補自身の取得件数から正解を作らない。関連性は登録時に人が確認する。新規問・実読取は対照も再実行し、保存済み固定データは再採点できる。

**費用・時間は計画用の仮定。実測でも現行AWS単価の確認でもない。** ネットワークを使わず、既存runnerの概算単価（百万tokenあたり新規入力$1.1、cache読取$0.11、出力$5.5）を使う（[run_routing_eval.py:44,149–156](../../scripts/routing_eval/run_routing_eval.py#L44)）。キャッシュ・レート・費用停止の型も再利用する（同[225–243,329–353](../../scripts/routing_eval/run_routing_eval.py#L225)）。

| 仮定／1回の便の試験 | 初週 | 100問段階の例 |
|---|---:|---:|
| 候補＋対照の実行数 | スキル48回＋外側18回（各9問） | スキル200回＋外側80回（各40問） |
| token仮定 | スキル1回6千入力＋1千出力。外側1問3推論、各10万入力＋1千出力 | 同左。実ツール内の要約をスキル分の平均へ含む |
| モデル費用 | 温cache約$2、冷cache約$7 | 温cache約$9、冷cache約$31 |
| その他／上限案 | task・rerank等は暫定$0.5枠、合計$8で停止 | 暫定$2枠、合計$35で停止 |
| 所要時間／停止上限 | 15〜30分／40分。ただしplan等の残存期限を優先 | 40〜70分の仮見積もり。現行plan期限を超え得るため、拡大前に分割設計が必要 |

モデル費用は`新規入力×1.1＋cache読取×0.11＋出力×5.5`（各百万token単位）。温cacheは入力の90%が読取と仮定。cache書込・失効も計上し、下位ツールを含め呼出し前に予算を予約する。実読取・モデル同時実行は2まで、毎分150万入力token以下。task起動込みの実測で単価・上限を確定する。100問化では固定問・対照の先行実行と、plan後の短い必須実読取への分割を別途設計し、既存期限を黙って延ばさない。実ファイル生成も別見積もり。

| 日 | 作る範囲と完了条件（今回は実装しない） |
|---|---|
| 1 | 24問とfixture、適格参照を確定。既存件数テストの0/2/6件を流用。実読取8問の出典・本人権限を確認できなければ勝手に代替合格にしない |
| 2 | 共通記録と決定的scorer。5件→1件、架空URL、重複、聞き返し、lane省略、計算違い、長さ超過を故意に与えて落ちることを確認 |
| 3 | 候補run-taskとスキル24問。本人claim/RLS、読取資格情報、全送信sink・副作用拒否を接続。安全自己検査が通るまで実データを読まない |
| 4 | OpenClawを9問で完走。実読取8問＋固定5件脱落問。スキル段と最終本文を別々に採点。直接投稿経路もsinkで捕捉 |
| 5 | 昇格→試験→証跡→apply入口の必須照合を接続。欠落・失敗・別digest・期限切れでapplyへ進まない検査を行い、24問全通過で初週完了 |

安全隔離や外側ループが間に合わなければ、初週の完成とは呼ばない。黙って(a)だけの合格に縮小せず日程を延ばす。2週目は40〜60問、次に100問へ増やす。予定・メールの本人実読取、資料作成の隔離一時ファイル検査、外側ループ対象の拡大を順に加える。既存routing84問は道具選択の別診断として保ち、最終回答の点と混ぜない。

<div style="break-after: page; page-break-after: always;"></div>

## 6. 毎朝の👎分類とリスク・未確認点

日次処理は別の読取ジョブにする。Slack由来の`search_session_id LIKE 'slack-%'`を対象に、前日JSTに更新された本人×回答IDを抽出する。そのキーの集計締切時点の最新値を履歴全体から選び、`target_type=answer AND rating=-1`だけ分類する。👍へ変わったら前回分類を取り消す。最新値選択は既存SQLを再利用する。

| 現行で確かめた事実 | 機械分類への影響・再利用 |
|---|---|
| `search_feedback`にquery/rating/note/対象ID/時刻、追加列にscore/session/answer IDがある。request IDと回答本文はない | queryには元発言が入り得る。匿名表とみなさない。[0015_search_feedback.sql:21–36](../../infra/migrations/0015_search_feedback.sql#L21)、[0022_search_feedback_score.sql:25–27](../../infra/migrations/0022_search_feedback_score.sql#L25)、[answer_feedback_store.py:7](../../src/teamagent/adapters/answer_feedback_store.py#L7) |
| Slack評価のscoreはNULL。noteは使用道具群、sessionは`slack-`＋answer ID | `score IS NOT NULL`の既存4段階集計ではSlackの👎が落ちる。ratingで拾う。[answer_feedback_store.py:38–45](../../src/teamagent/adapters/answer_feedback_store.py#L38)、[server.py:2103–2111](../../src/teamagent/mcp_gateway/server.py#L2103)、[answer_rating_summary.py:19–25](../../scripts/answer_rating_summary.py#L19) |
| 検索イベントはrequest ID中心、Slack評価はanswer ID中心 | 現行記録だけで確実な結合は保証できない。時刻＋queryの近似一致を確定分類にしない。[fanout.py:1089–1106](../../src/teamagent/skills/search/fanout.py#L1089)、[answer_feedback.py:235–240](../../src/teamagent/mcp_gateway/answer_feedback.py#L235) |
| 本人・回答IDの最新評価SQLとdashboard用SELECT権限がある | app権限を広げず読取専用側を使う。[dashboard/queries.py:587–597](../../src/teamagent/dashboard/queries.py#L587)、[0032_search_feedback_dashboard_read.sql:22](../../infra/migrations/0032_search_feedback_dashboard_read.sql#L22) |

**追加する最小記録案:** `answer_id ↔ request_id[]`、一般化した質問型、使用道具、検索先の状態、適格候補数、選択／表示参照のHMAC、最終回答hash。本文を新たに恒久保存しない。質問型・候補の関連性が未確定なら自動確定せず「要確認」を残す。

| 分類 | 機械で確定できる条件（必要記録が揃った後） | 現行だけで分かる範囲 |
|---|---|---|
| 道具の選び間違い | 人が確認済みの質問型の期待道具と実行列が不一致 | `note.tools`から候補抽出。自由文から正しい道具を決定論では決められない |
| 検索が見つけていない | 正しい道具を使い、必須先の検索が成功し、関連する出典可能候補が0 | `found=false`は手掛かり。「データ自体なし」と「検索の取りこぼし」は追加確認が必要 |
| 見つけたのに落とした | 適格候補Aに対し表示が`min(N,A)`未満、または不在・聞き返しへ変わる。選択→表示の対応で落ちた段を特定 | raw hit数だけでは確定不可。`search_compact_fallback`は退避の記録で、救済に成功していれば失敗ではない |
| 不明／検索未完了 | 相関不能、認可・timeout・lane欠損、質問型不明 | 3分類へ強制配分しない。件数と理由を別掲する |

分類は「道具→検索→表示」の順で最初に破れた段を主因にし、複数の兆候は副因へ残す。毎朝、担当者が不明例を確認し、実DMを転記せず「業種で5件要求→出典を1件に縮めた」のような失敗型へ一般化する。架空データで同型問を作り、修正前で落ち、修正後で通ることを確認して必須問へ追加する。毎朝の報告には分類・件数・問ID・修正先だけを出し、元発言・氏名・メールを載せない。

| リスク・未確認点 | 対応／設計上の限界 |
|---|---|
| 決定的検査の限界 | 件数・リンク・定型句は測れる。回答の意味や出典が主張を本当に支えるかは完全判定できない。補助AI・人の確認で見落としを補う |
| 実データの変化・権限差 | 本人1人の可視範囲しか証明しない。ライブ正解の前提が変われば試験不成立。別の権限層の試験は後続課題 |
| 試験経路と本番の差 | fixture、送信sink、隔離replay storeは本番配信・認可台帳の疎通を証明しない。本番と同じ整形とpluginの版を固定し、差を証跡へ残す |
| 副作用の取りこぼし | 読取ツール名だけの制限では不十分。書込・送信adapterの棚卸しと自己検査が初週の前提。隔離接続は現行未実装 |
| 初回の既存失敗 | 現仕様でも新しい件数基準に落ち得る。期待値を下げて通さず、先に挙動を修正するか、その便を止める |
| ゲートの強制範囲 | 通常のguarded applyを止める。管理者の直接AWS操作まで強制できる境界ではない。[infra/terraform/README.md:19–23](../../infra/terraform/README.md#L19) |
| 未確認 | liveのIAM/TD、DB・Slackの保存／結合率、ログ保持、実読取8問の出典、料金・所要時間。今回、本番・AWS・DB・Slack・ネットワークへ接続していない |

今回の成果物はこの設計書だけ。実装、試験の実行、デプロイ、commitは行わない。A4の頁数は改頁位置を指定した目安であり、印刷環境での実レンダリングは未確認。
