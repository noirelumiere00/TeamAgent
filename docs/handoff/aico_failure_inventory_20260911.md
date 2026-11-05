# Aico（TeamAgent）利用者に見えた失敗の全棚卸しと根治策

- 作成: 2026-09-11
- 対象期間: **2026-09-04 00:00 UTC 以降**（`unwrap` 修正の着地日。9/3 以前は「解決済み」として §5 に別枠）
- 一次ソース: CloudWatch `/teamagent/dev/openclaw`・`/teamagent/dev/teamagent-mcp`、EFS 上の
  OpenClaw セッション記録（読み取り専用 Fargate プローブ `probe_failures.mjs` で tool call の
  引数の実物まで取得）、上流 `openclaw@2026.7.1` の実物、`aiiadev-mfa` での ECS/TD 読み取り
- 実装: PR **#405**（draft・`fix/caller-claim-missing-identity`・`03cb24c`・**CI 全緑**）
- 本書では Slack user id を伏せない（社内向け・利用者の特定が対処に要るため）。プラグインのログ側は G7 どおり値を出していない。

---

## 0. 30 秒サマリ

| 層 | 何が起きていたか | 件数 | 状態 |
|---|---|---|---|
| OC plugin | `_user_context` をモデルが付けてこず tool が block（`CONNECT-P06`） | 5 | **#405 で根治** |
| mcp gateway | unwrap した呼び出しの署名結合が必ず壊れる（`CONNECT-I01a`） | 6 | **#405 で根治** |
| 文面 | ブロック文をモデルが読み解けず「Google 連携をリセット」と作文 | 2 セッション | **#405 で根治**（plugin + SOUL） |
| ツール面 | `mail_draft` が本番で無効なのに SOUL は呼べと書いている | 13 | **未着手**（設定 1 行／§4 C-1） |
| ツール面 | 存在しないツール名をモデルが呼ぶ（`read` / `diagram_maker` / `taskflow`） | 23 | **未着手**（§4 C-2/C-3） |
| ジョブ | `tiktok_acquire` の job_id 消失（mcp 再デプロイでジョブが死ぬ） | 1 セッション | **未着手**（既知・§4 C-4） |
| 仕様 | 動画分析の月額上限・区間指定不可など「できません」応答 | 4 セッション | 一部は仕様。§4 C-5/C-6 |

**通底する構造**: どの失敗も「**Aico が自分で吸収できたはずのものを、利用者に押し付けていた**」。
`_user_context` はモデルが書かなくても ingress から組める。`mail_draft` は tool が無ければ
`mail_reply` へ回せる。存在しないツール名は 16 回もリトライさせる前に 1 回で気づかせられる。

---

## 1. 実測の全件表（2026-09-04 以降・利用者に見えたもの）

### 1-1. OC plugin の `before_tool_call blocked`

CloudWatch `/teamagent/dev/openclaw` で `before_tool_call blocked` は **10 行 = 5 ブロック**
（logger と console の二重出力）。**すべて `CONNECT-P06`・すべて同一 `id_shape`**。
`bind_agent_run rejected` は **0 行**、`discarded declared user_context fields` も **0 行**。

| 時刻(JST) | ツール | 利用者 | 引数の実物（EFS） | `_user_context` |
|---|---|---|---|---|
| 09-10 12:40:26 | `slack_summary` | U08EM8C0R1A | `{"arguments":{}}`（1 段包み・中身が空） | **無い** |
| 09-11 10:27:30 | `knowledge_deliver` ×2 | U08F7TK43L0 | `{query, top_k, filter_doc_type}`・包みなし | **無い** |
| 09-11 10:27:33 | `search` ×2 | U08F7TK43L0 | `{query, top_k, filter_doc_type}`・包みなし | **無い** |

> **`id_shape=sender:absent,team:absent` は異常ではない。**
> 上流 2026.7.1 の `before_tool_call` の ctx は `buildToolContext`
> （`dist/agent-tools.before-tool-call-84fX7TrL.js:1604-1614`）が組み、`senderId` / `teamId` を
> **そもそも含まない**（`toolName / agentId / sessionKey / sessionId / runId / trace / toolCallId / channelId` だけ）。
> 全 P コードで常に `absent` になる。`docs/design/connect_third_layer_defense.md` §11-5 の弱点 1 と同じ。
> 「送信者とワークスペースが消えた」という読みは**誤り**。落ちていたのは params 側だけ。

### 1-1b. 引いたが 0 件だった経路（網羅の裏取り）

| ログ | 期間内の件数 | 判定 |
|---|---|---|
| `bind_agent_run rejected` | **0** | run 束縛は健全 |
| `discarded declared user_context fields` | **0** | 会話面の申告不一致は発生していない |
| `connect guarantee … outcome=post_failed` | **0**（`delivered` 9・`already_delivered_by_guarantee` 6） | 保証経路は健全 |
| `connect suppression skipped reason=no_run_binding` | **0**（`not_connect_request` 85・`rule_disallows` 1） | 抑止の未束縛は発生していない |
| mcp `identity_spoof_rejected` | **0** | なりすましは発生していない |
| `unwrapped tool arguments` | 7（`arguments` 4・`name_arguments` 3） | うち 6 が §2-2 で拒否・1 が P06 |

### 1-2. mcp gateway の拒否

| 時刻(JST) | event | tool | reason |
|---|---|---|---|
| 09-09 15:12:36 | `caller_claim_rejected` | `oauth_connect` | caller claim request binding does not match |
| 09-09 15:12:51 | `caller_claim_rejected` | `oauth_connect` | 同上 |
| 09-10 12:40:32 / :34 / :39 / :41 | `caller_claim_rejected` | `slack_summary` ×4 | 同上 |

`identity_spoof_rejected` は期間内 **0 行**。

### 1-3. ツール結果のエラー（EFS・`isError=true`）

| 期間(JST) | ツール名 | 件数 | 利用者 | エラー |
|---|---|---|---|---|
| 09-06 15:07〜15:10 | `teamagent__mail_draft` | 13 | U08EK71DMUJ | `Tool teamagent__mail_draft not found` |
| 09-07 03:36〜03:37 | `teamagent__diagram_maker` / `teamagent__diagram-maker` | 6 | U08EK70M00J | `Tool … not found` |
| 09-10 03:40 | `read`（母艦ネイティブ） | 16 | U08EM8C0R1A | `Tool read not found` |
| 09-10 03:40 | `teamagent__taskflow` | 1 | U08EM8C0R1A | `Tool … not found` |

### 1-4. Aico が「申し訳ありません／できません」で始めた応答（16 件）

| 日時(JST) | 利用者 | 冒頭 | 原因コード |
|---|---|---|---|
| 09-04 10:46 ×2 | U09CX1CCBLN | `tiktok_acquire` の job_id が見つからない／連続エラー | ジョブ消失（§4 C-4） |
| 09-06 15:08 ×4 | U08EK71DMUJ | 「別の方法で対象メールを特定します」「同じ下書きが出ています」 | `mail_draft` 不在 + スレッド取り違え |
| 09-07 16:57 | U08EBTEE0TG | 「0 秒から 2 秒までという範囲指定はできません」 | 仕様（§4 C-6） |
| 09-08 16:53 | U093KEFU1NF | 「Drive の外部共有リンクなので詳細は取得できません」 | 仕様（§4 C-5） |
| 09-08 22:28 ×2 | U08EK71N7D0 | 「エラーが出ました」「動画分析の月額上限に到達」 | `VIDEO_QUOTA_EXCEEDED`（仕様どおり） |
| 09-09 15:12 ×2 | U08EM8C0R1A | 「連携の処理でエラー」`診断: CONNECT-I01a` | **I01a**（§2-2） |
| 09-09 18:04 | U08EK70M00J | 「催促メールの抽出には特定のお客様名が必要です」 | 仕様（§4 C-7） |
| 09-10 12:40 | U08EM8C0R1A | 「MCP 側の認可エラー」`診断: CONNECT-I01a` | **I01a**（§2-2） |
| 09-11 10:27 ×2 | U08F7TK43L0 | 「連携に一時的／継続的な問題」`診断: CONNECT-P06` + **Google 連携リセットの誤誘導** | **P06**（§2-1・§2-3） |

---

## 2. 根因（file:line つき）

### 2-1. P06 = モデルが `_user_context` を付けてこない

| # | 箇所 | 何が起きるか |
|---|---|---|
| a | `infra/openclaw/caller-identity-plugin/dist/index.js:2688`（修正前） `assertPlainObject(params[USER_CONTEXT_KEY], USER_CONTEXT_KEY)` | 欠落（`undefined`）・`null`・文字列・配列で throw → **P06** |
| b | 同 `:2488`（修正前） `declaredContext.slack_user_id !== trusted.senderId` | `_user_context` が `{}` や部分申告だと `undefined !== "U…"` で **P05** |
| c | 同 `unwrapToolArguments` の上限 2 段 | 3 段以上は剥がさず、(a) を経由して **P06** |

実物 dist を駆動した判定マトリクス（修正前）:

| 入力 | 修正前 | 修正後 |
|---|---|---|
| `{query, _user_context:{slack_user_id}}` | pass | pass |
| `{query}`（uc 欠落） | **P06** | pass |
| `{query, _user_context:{}}` | **P05** | pass |
| `{query, _user_context:null / 文字列 / 配列}` | **P06** | pass（捨てて観測） |
| `{query, _user_context:{channel_id のみ}}` | **P05** | pass（捨てて観測） |
| `{arguments:{…}}` 1 段 | pass（ただし §2-2 で I01a） | pass |
| `{arguments:{arguments:{arguments:{…}}}}` 3 段 | P06 | **P06 のまま**（明示 block） |
| `params` が `null` | P06 | **P06 のまま** |
| `{_user_context:{slack_user_id: 別人}}` | **P05** | **P05 のまま** |
| `{_user_context:{caller_claim: …}}` | **P05** | **P05 のまま** |

**なぜ根治してよいか**: 申告値は `mintCallerClaim` が `authoritativeContext` で**丸ごと置き換えてから**
署名する（同ファイル `mintCallerClaim`）。モデルが何を書いても（書かなくても）mcp へ渡る値は変わらない。
つまり「モデルが正しい形で渡すこと」への依存は**セキュリティを 1 ビットも稼いでいない純粋な失敗モード**だった。

### 2-2. I01a = unwrap 救済が本番で一度も成立していなかった

上流は hook の返り値を**置換せず浅くマージ**する。

```
openclaw@2026.7.1 dist/agent-tools.before-tool-call-84fX7TrL.js:1735
  if (hookResult?.params) finalParams = mergeParamsWithApprovalOverrides(finalParams, hookResult.params);
同 :938-947
  mergeParamsWithApprovalOverrides = (o, a) => ({ ...o, ...a })
```

`{"arguments":{…}}` を剥がして中身に署名して返すと、**元の `arguments` キーが実行引数に残る**。
mcp へ届くのは `{arguments:{…}, …剥がした中身}` で、署名した `arguments_sha256`（剥がした中身だけ）と
一致しない → `src/teamagent/mcp_gateway/caller_claim.py:446`
`caller claim request binding does not match` → 利用者には `診断: CONNECT-I01a`。

**同一秒で 1:1 一致（6/6）**:

| unwrap 成功（OC ログ） | caller_claim_rejected（mcp ログ） |
|---|---|
| 09-09 15:12:36 | 09-09 15:12:36 `oauth_connect` |
| 09-09 15:12:51 | 09-09 15:12:51 `oauth_connect` |
| 09-10 12:40:32 | 09-10 12:40:32 `slack_summary` |
| 09-10 12:40:34 | 09-10 12:40:34 `slack_summary` |
| 09-10 12:40:39 | 09-10 12:40:39 `slack_summary` |
| 09-10 12:40:41 | 09-10 12:40:41 `slack_summary` |

09-04 以降の unwrap 成功は 6 件（7 件目 09-10 12:40:26 は中身が空で P06）。**6/6 が拒否**。

> これは P06 の根治と**不可分**。`_user_context` の欠落を通すようにすると、09-10 の
> `{"arguments":{}}` は unwrap を通って署名され、そのまま I01a に化けるだけだった。

### 2-3. 文面 = モデルの作文を招く形になっていた

修正前の block 文 1 行目:

```
teamagent-caller-identity: teamagent-caller-identity: _user_context must be a plain object
```

接頭辞が 2 回重複した英語の技術理由（`fail()` が `PLUGIN_ID:` を付け、`formatBlockReason` がもう一度付けていた）。
モデルはこれを読み解けず、09-11 10:27 に自分で原因を作文した。

SOUL の既存規約 `infra/openclaw/SOUL.md`「🔴 連携の失敗は『診断:』行をそのまま出す（推測しない）」は
適用範囲を「`oauth_connect` の message/error、`CALLER_IDENTITY_REJECTED`」に限定していたため、
plugin の `before_tool_call` block は**どの規則にも当たらなかった**。診断行はそのまま提示された一方、
その後ろに自作の推測が付いた。

---

## 3. #405 で入れた根治

| # | 直したもの | 内容 |
|---|---|---|
| 1 | `_user_context` 欠落 | `{}` とみなす。非オブジェクトは捨てて観測（`discarded non-object declared user_context shape=…`）して続行 |
| 2 | `slack_user_id` 照合 | 「キーがあって値が違う」ときだけ拒否（`Object.hasOwn` を追加）。「申告が無い」は矛盾ではない |
| 3 | 3 段以上の包み | `unwrapToolArguments` に `stillWrapped` を追加し、**明示的に** P06 で block（入力面を広げない） |
| 4 | 署名と実行の一致 | `reconcileReturnedParams` — 署名集合に無い元のトップレベルキーを `undefined` で返す。JSON-RPC 化で落ちるので mcp が受け取るのは署名集合そのもの。包み無し（`depth===0`）は返り値を一切変えない |
| 5 | 利用者への文面 | `formatBlockReason` を 5 行（日本語の案内 → モデル宛の禁止 → 転送定型文 → 診断行 → 技術理由）。接頭辞の重複を除去 |
| 6 | SOUL | 節を「`診断:` 行を含む**どのツールの結果でも**」へ拡張。「拒否理由に書いていない別の操作を勧めない」「ツールのブロックを連携切れと解釈してやり直しを促さない」「`teamagent-caller-identity:` 行は管理者向け」を明文化 |

**信頼境界は 1 ビットも動かしていない**（変異テストで証明・§6）:
他人の ingress を掴まない／署名対象は不変／なりすまし申告（別人の `slack_user_id`・`caller_claim` 持ち込み）は
P05 のまま／`before_tool_call` の他の門（P01〜P05・replay・`mail_draft` のボタン権威・TTL）はすべて無変更。

---

## 4. 対策案（棚卸し全件・優先度つき）

方針は一貫して「**できないことを利用者にやらせない。可能な限り Aico が実行する**」。

| # | 事象 | 根因（file:line） | 現行で救われるか | 根治策 | 規模 | 利用者から見た改善 | 優先 |
|---|---|---|---|---|---|---|---|
| **A-1** | `CONNECT-P06`（5 件） | `dist/index.js:2688` / `:2488` | ❌ 全断 | #405（§3 の 1〜3） | 中 | 「連携に問題が発生しています」が出なくなる | **P0・実装済** |
| **A-2** | `CONNECT-I01a`（6 件） | 上流 `:1735` / `:938-947` × plugin の unwrap | ❌ 100% 拒否 | #405（§3 の 4） | 中 | 二重包みの回でもツールが通る | **P0・実装済** |
| **A-3** | 「Google 連携をリセット」の誤誘導 | `formatBlockReason` / SOUL 129-135 | ❌ | #405（§3 の 5, 6） | 小 | 無意味な操作へ誘導されない | **P0・実装済** |
| **C-1** | `mail_draft not found` 13 件 | 本番 mcp TD:102 の env **`USE_MAIL_DRAFT_TOOL=false`** なのに `SOUL.md` の「ボタン共通プロトコル」は `mail_draft` を必ず呼べと規定。`infra/openclaw/effective-tool-scope.json:163` にも載っている | ❌ 押すたびに失敗 | ①`USE_MAIL_DRAFT_TOOL=true` にする（tfvars 1 行・**要裁定**）か、②tool が無いときは `mail_reply` へ自動フォールバックする（SOUL + 実装）。**どちらにせよ「宣言している tool が本番で無効」という状態を CI で検出**（`effective-tool-scope.json` × TD env の突合テスト） | 設定＋小 | 朝ダイジェストの「下書きを作成」が動く／少なくとも「押しても何も起きない」が消える | **P0** |
| **C-2** | `read not found` 16 連発 | `effective-tool-scope.json` の `nativeTools.deny` に `read` があり**設計どおり拒否**。しかしモデルは 16 回リトライした（上流のループ検知が `Tool not found` を拾っていない） | ❌ ループ | ①SOUL に「`Tool … not found` が返ったら同じ名前で再試行しない・`search`/`knowledge_deliver` へ切り替える」を追加。②plugin 側で「同一 run で同一の未知ツール名が N 回」を観測ログに出す（`unknown tool retried name=… count=…`） | 小 | 無駄な待ち時間が消え、代替手段に切り替わる | **P1** |
| **C-3** | `diagram_maker` / `taskflow` 等の幻覚ツール 7 件 | 存在しないツール名。カタログに無い | ❌ | C-2 と同じ対処＋「Aico が持つツール一覧」を SOUL の一次情報として持たせるか、`Tool not found` の戻り値に**実在する近い名前の候補**を載せる（上流が返す文面は変えられないので plugin 側で補強） | 小〜中 | 「できません」ではなく代替提案が返る | **P1** |
| **C-4** | `tiktok_acquire` の job_id 消失 | mcp 再デプロイで実行中ジョブが死ぬ（既知・`project_omiyage_e2e_findings_20260903`） | ❌ 利用者が最初からやり直し | ①ジョブ状態を ECS タスクのメモリでなく永続層へ（中〜大）。②最低限、`job_not_found` のときに**Aico が自動で再取得を開始**し、その旨だけ伝える（小） | 小（暫定）／大（恒久） | 「しばらく経ってから」と言われて放置されない | **P1**（暫定は小） |
| **C-5** | Drive 外部共有リンクの詳細が取れない | `search` が返す URL が外部共有リンクで、`gdrive_client` の権限外 | ❌ | ①`knowledge_deliver` へ回して実ファイルを配信する経路が既にある（`project_knowledge_deliver_sheet_row_pr298`）。SOUL に「search の URL が Drive 外部共有なら `knowledge_deliver` を呼ぶ」を追記 | 小 | 「取得できません」でなくファイルが届く | **P2** |
| **C-6** | 動画の区間指定（0〜2 秒）不可 | 単一時刻のフレーム切り出しのみ対応 | ❌（仕様） | 区間 → 等間隔サンプリングへ自動変換して実行する（例: 0〜2 秒 → 0.0 / 0.5 / 1.0 / 1.5 / 2.0 の 5 枚・上限 12 枚の枠内） | 中 | 「できません」が「5 枚で出しました」に変わる | **P2** |
| **C-7** | 「催促メール抽出には顧客名が必要」 | `mail_followup` が顧客名必須 | ❌ | 顧客名なしでも「放置日数 N 日以上」で全件から抽出できるようにする（既に `inbox_triage` 経路がある）。SOUL 側で先に顧客名を要求しない | 小〜中 | 1 往復減る | **P2** |
| **C-8** | `VIDEO_QUOTA_EXCEEDED` | 月額上限（仕様どおり・`VIDEO_QUOTA_ENABLED=true`） | ✅ 文面は正しい | 恒久対処は不要。ただし**上限到達の 3 本前に管理者へ通知**すると、月末の駆け込みで止まらない | 小 | 予告があるので計画が立つ | **P3** |
| **C-9** | ブロック文が logger と console に二重で出る | `emitPluginLog` が両方へ書く（設計どおり・§11-5 弱点 2） | ✅ 実害なし | 本番でどちらも届くことが確認できたので、片方（console）へ寄せて CloudWatch の行数を半減できる | 小 | なし（管理者の可読性のみ） | **P3** |
| **C-10** | 本番 OC に `TEAMAGENT_CALLER_IDENTITY_TRACE=1` が入ったまま | TD:47（09-07 11:46 登録）の env | ✅ 実害なし | 切り分けが済んだら OFF に戻す運用（§11-5 弱点 6） | 設定 | なし（ログ量のみ） | **P3** |

### C-1 の裏取り（実測値）

```
teamagent-dev-mcp:102（2026-09-11 11:47 JST 登録・現行）の env
  USE_MAIL_DRAFT_TOOL  = false   ← mail_draft は登録されない
  USE_MAIL_REPLY_TOOL  = true
  USE_MAIL_SUMMARY_TOOL= true
  DRAFT_ON_DEMAND_ONLY = true
teamagent-dev-openclaw:47（2026-09-07 11:46 JST 登録・現行）
  TEAMAGENT_CALLER_IDENTITY_TRACE = 1
```

`USE_MAIL_DRAFT_TOOL` は **mcp TD:95〜102 のすべてで `false`**（09-02 20:10 〜 09-11 11:47）。
09-06 15:07 の 13 連発も当時の現行 TD が `false` だったため。一時的な事故ではなく**恒常状態**。

`infra/openclaw/effective-tool-scope.json` は `mail_draft` を 38 tools の 1 つとして宣言しており、
`SOUL.md` の「ボタン共通プロトコル」も `actionId と同名の tool を必ず呼ぶ` と規定している。
**宣言と本番設定が食い違っている**のが C-1 の本体。デプロイ判断は管理者（本セッションではデプロイしない）。

---

## 5. 9/3 以前の既知事象（別枠・解決済み）

| 事象 | 件数 | 状態 |
|---|---|---|
| `_user_context must be a plain object`（**二重包み** `{"arguments":{…}}`） | 72 | 2026-09-04 の unwrap で block は解消。ただし**その先で I01a に化けていた**ことが今回判明（§2-2）。#405 で完全解消 |
| `trusted Slack run identity is missing or stale` | 9 | `bind_agent_run` の C1 修正で解消（期間内 0 件） |
| `declared channel_id does not match the bound ingress` | 2 | 2026-09-04 の「破棄して続行」で解消（期間内 0 件） |
| plugin の warn が CloudWatch に 14 日間 0 行 | — | `blockAndLog` + console 二重書きで解消（今回この 5 件が 1 行で拾えた） |

---

## 6. 検証

### テスト（PR #405）

追加 5 本（`tests/scripts/test_openclaw_runtime_contract.py`）+ SOUL 契約 5 pin
（`tests/infra/test_soul_contract.py`）。

| テスト | 固定するもの |
|---|---|
| `test_tool_calls_without_a_declared_user_context_are_signed_not_blocked` | 本番実測 5 件の形（欠落 / 空 / 部分 / null / 文字列 / 配列 / 包み空）が block されず、署名済み uc が authoritative 値になる |
| `test_missing_user_context_does_not_move_the_trust_boundary` | 別人の `slack_user_id`・`caller_claim` 持ち込みは P05 のまま。params 非オブジェクト・3 段包みは P06 のまま |
| `test_signed_arguments_match_what_upstream_actually_executes` | 上流の浅いマージ + JSON-RPC 化を再現した実行引数が署名結合と一致し、包みキーが残らない |
| `test_unwrapped_calls_are_reconciled_with_one_observable_line` | 包みを剥がした呼び出しだけ reconcile され 1 行残る。包み無しは触らない。G7 |
| `test_block_reasons_carry_a_forwardable_diagnostic_line`（改訂） | 5 行構成・1 行目は日本語で `_user_context` を含まない・2 行目に「推測」「リセット」の禁止・接頭辞の重複なし |

### 変異（緑の実質性）— 6/6 が赤

| 変異 | 内容 | 結果 |
|---|---|---|
| M30 | `_user_context` 欠落を再び block に戻す | RED |
| M31 | `reconcileReturnedParams` を素通しにする | RED |
| M32 | `slack_user_id` の照合を `Object.hasOwn` 無しに戻す | RED |
| M33 | **信頼境界**: なりすまし申告も捨てて続行 | RED |
| M34 | **信頼境界**: 3 段以上の包みを通す | RED |
| M35 | **信頼境界**: `caller_claim` の持ち込みを許す | RED |

復元は `cmp` 一致・`shasum -c` 4 ファイル OK・変異残置 0。

---

## 7. 未解決・次の一手

1. **C-1（`USE_MAIL_DRAFT_TOOL=false`）は裁定が要る。** 有効化するのか、`mail_reply` フォールバックにするのか。
   どちらにせよ「宣言と本番設定の食い違い」を CI で検出する仕組み（`effective-tool-scope.json` × TD env の突合）は
   単独で入れる価値がある。
2. **C-2/C-3（存在しないツールへのリトライ）**は plugin 側の観測行を入れれば件数が見えるようになる。
   16 連発が常態なのか 1 回きりなのかは、今のログでは判らない。
3. **#405 の本番確認**は OC 便で載せたあとに:
   - `before_tool_call blocked diagnostic=CONNECT-P06` が出ないこと（出るなら params 非オブジェクトか 3 段包み）
   - `unwrapped tool arguments (…)` の直後に `reconciled unwrapped tool arguments removed=[…]` が並ぶこと
   - mcp の `caller_claim_rejected reason="caller claim request binding does not match"` が 0 になること
4. **`reconcileReturnedParams` は上流のマージ規則に依存する。** 上流が置換へ変わっても壊れはしない
   （墓標キーが無害な余剰になるだけ）が、probe 側に焼き込んだ 3 行は上流更新時に一次ソースで引き直すこと。
5. `tests/infra/test_dockerfile_teamagent_media_worker.py::test_dns_pinned_proxy_rebinding_answers_fail_closed_without_second_lookup`
   が **base f45e76e でも赤**（#405 と無関係）。別途対処が要る。
