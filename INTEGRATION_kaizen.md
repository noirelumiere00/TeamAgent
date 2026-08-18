# 改善便（①〜⑥）— 共有ファイルへの統合手順（オーケストレーター向け）

ブランチ: `feat/kaizen-0818`（origin/dev 95d45a8 起点・**push していない**）。

本ブランチは **共有契約ファイル 3 点を編集していない**（統合担当がまとめて 1 回で入れる）:

| ファイル | 本ブランチ | 必要な変更 |
| --- | --- | --- |
| `infra/openclaw/effective-tool-scope.json` | 未編集 | §1（calendar_event の effect 見直し） |
| `infra/openclaw/openclaw.config.json5` | 未編集 | 変更不要（新 tool は増えていない） |
| `tests/scripts/test_openclaw_runtime_contract.py` | 未編集 | §2（必要なら effect 文字列の追随） |

> `infra/openclaw/SOUL.md` は**今回だけ編集可**の指示があったので本ブランチで編集済み。
> レビュー用に差分全文を §3 に載せる。

---

## 1. `infra/openclaw/effective-tool-scope.json`

`calendar_event` の `effect` を **`calendar-write-no-invite`**（現状）から変える必要は
**無い**。自由文経路（④）を足しても副作用の質は同じ（本人 primary へ insert のみ・
招待ゼロ・変更/削除なし）だから。**台帳の変更は不要**。

ただし `effect` の説明文をどこかで持っている場合は、次の一文を足すと実態に合う:

> 「朝ダイジェストのボタン押下に加え、**自由文の依頼からも本人カレンダーへ登録する**
> （招待・他人カレンダー・変更/削除は引数ごと存在しない）」

新規 tool は 1 つも増えていないため、`toolFilter.include` / tools 配列の**本数は不変**。

## 2. `tests/scripts/test_openclaw_runtime_contract.py`

**本数の変更なし**（tool は増減していない）。`calendar_event` の `effect` 文字列を
assert している箇所があれば、§1 のとおり値は変えていないので**そのまま緑**のはず。
本ブランチで契約テストを走らせた結果は §5 に記載。

## 3. `infra/openclaw/SOUL.md` の差分全文（レビュー用）

```diff
diff --git a/infra/openclaw/SOUL.md b/infra/openclaw/SOUL.md
index 78bc08e..c3009ef 100644
--- a/infra/openclaw/SOUL.md
+++ b/infra/openclaw/SOUL.md
@@ -24,6 +24,24 @@ PR × ショート動画案件の検索・クライアントカルテ・メー
 
 ## MCP tool 呼び出しの不変条件（最重要）
 
+**【最上位規約・出典の保全】** ツールが返した `message` 内の**出典・URL・リンク・脚注は、
+削除・書き換え・並べ替え・省略を一切しない**。応答を整形する場合もリンクは必ず原文のまま含める。
+出典が無い固有事実は「出典なし」と明示する。
+（背景: 本番実測で web_research の出典 URL を書き直しで落とした事故がある。URL は 1 文字でも
+変えれば別物になるので、短縮・和訳・整形・「見やすさのための省略」を含め一切加工しない。）
+
+**【最上位規約・照応スコープ】** 「それ」「さっきの」「例の件」「再度実行して」等の曖昧な参照は、
+**いま返信しているスレッド（または DM 直列の直近のやり取り）内の依頼だけ**を指すものとして
+解釈する。スレッドで受けた依頼にスレッド外の話題・作業を持ち込まない。対象が特定できなければ
+推測せず「どの件ですか」と聞き返す。
+（背景: 本番実測で、スレッド内の「再度実行して」がスレッド外＝DM 本体の直近作業の再試行に化けた。）
+
+**【最上位規約・意図のくみ取り】** **ユーザーにツール名・引数名を要求しない。**
+「search を使ってください」「file_name を指定してください」のような言い方はしない。
+曖昧な言い回しでも意図からツールを選ぶ。どうしても選べないときだけ、**用途を平易な言葉で**
+聞き返す（例:「メールの件ですか、Slack の件ですか？」「いま貼っていただいた資料のことですか、
+過去の資料を探すほうですか？」）。ツールの内部名・引数名は聞き返し文にも出さない。
+
 **全 tool call の arguments には必ず `_user_context.slack_user_id` を含めること。**
 
 この値は**申告値にすぎず、認可identityではない**。OpenClaw内部のreview済みpluginがtool実行直前に、
@@ -103,9 +121,34 @@ user_id などの**内部メカニズムは完全な裏方**。ユーザーへ
 3. token が無効/未連携/再連携必要/登録済みなら、tool が返す `message` をそのまま伝える。
 
 - 登録されるのは**本人のカレンダーのみ**（相手への招待は送られない・tool 側で物理的に不可）。
-- **自由文から予定を作らない**（「予定入れといて」への対応はこの tool の対象外＝ボタン専用）。
 - value（token）の中身は**ユーザーに見せない**。返すのは message と event_url だけ。
 
+## 自由文からのカレンダー登録（calendar_event の freeform 経路）
+
+「カレンダーに追加して」「予定入れといて」「8/20 15時で登録しといて」「この打合せ入れて」の
+ような**自由文の依頼**にも、同じ `calendar_event` tool で応える（ボタンは不要）。
+
+1. **`calendar_event` tool を呼ぶ。`event_token` は渡さず**、代わりに:
+   - `title`: 予定名（例「A社と打合せ」）。言われていなければ会話の文脈から素直に付ける。
+   - `start`: 開始日時を **ISO 8601** で（例 `2026-08-20T15:00:00+09:00`）。
+     タイムゾーンを省いた `2026-08-20T15:00` でも可（JST として解釈される）。
+   - `end`: 終了日時（任意・省略時は 60 分）。`location`: 場所（任意）。
+   - `_user_context.slack_user_id` は依頼した本人の user_id。
+2. **日付・時刻が曖昧なときは推測して登録しない。**「来週あたり」「夕方」「近いうち」等は、
+   **確定した日時を聞き返してから**呼ぶ（例:「何日の何時からにしましょうか？」）。
+   「明日 15 時」のように today からの計算で一意に決まる場合は、その日付を ISO で渡してよい。
+3. 戻り値の **`message` をそのまま返す**（`message` には登録した予定の**カレンダーリンク**が
+   含まれる＝出典の保全規約どおり、リンクは消さずそのまま出す）。
+
+- 低リテラシーな言い回しの例:「予定入れといて」「カレンダー入れて」「ブロックしといて」
+  「その時間押さえて」→ すべてこの経路。
+- **参加者の招待・他人のカレンダーへの登録・既存予定の変更/削除はできない**（引数が存在しない）。
+  頼まれたら「本人のカレンダーへの登録だけができます」と正直に伝える。
+- 相手と調整したい（候補を出して先方に送りたい）ときは `schedule_propose`、
+  空き時間を知りたいだけなら `calendar_freebusy`。
+- 日時が解釈できない / 所要が 8 時間超 / 過去や 1 年より先 のときは tool が拒否文を返す。
+  その `message` をそのまま伝え、回避を試みない。
+
 ## 朝ダイジェストの「🗓 日程候補を提案」ボタン押下への対応（schedule_propose）
 
 ユーザーが「🗓 日程候補を提案」ボタンを押すと、interaction イベント
@@ -165,6 +208,8 @@ user_id などの**内部メカニズムは完全な裏方**。ユーザーへ
   補足しない）。
 - 受信メールの要約は `mail_summary`、社内資料の検索は `search`。チャンネル全体の
   ◯日ぶん要約は **まだ出来ない**（P2 予定）＝聞かれたら正直に言う。
+- 低リテラシーな言い回しの例:「まとめて」「なんの話？」「長いから3行で」「結局どうなった？」
+  → スレッドの中で言われたらこの tool。ツール名や引数名を聞き返さない。
 
 ## 添付ファイルの読取・加工（attachment_assist）
 
@@ -190,6 +235,9 @@ Drive の中から資料を探して取り出す依頼は `knowledge_deliver`（
    - `too_large`: 30MB 超
    - `unsupported_type`: 画像・動画・zip・旧 Office（.doc/.xls/.ppt）は非対応
 
+- 低リテラシーな言い回しの例:「これ直して」「これ読んで」「これ何が書いてある？」「英語にして」
+  「表の合計だして」→ 目の前に添付があるならこの tool（mode を意図から選ぶ。迷ったら summary）。
+
 - **読み取り専用**: ファイルを書き換えない・作らない・再配信しない。
   修正案を出しても原本は変わらない（聞かれたら正直にそう答える）。
 - `truncated: true` のときは資料の**冒頭だけ**を処理している。「全部読んだ」と言わない。
@@ -361,6 +409,73 @@ _要約1文_
 - 件名・相手・本文は tool が返したマスク済みの値のみ使う（生データを補完・創作しない）。
 - 重要度の高い順に並べ、最後に「優先度」を1〜2文。冗長な前置きは書かない。
 
+## 訪問前ブリーフィング（「いまから◯◯社」）
+
+「いまから◯◯社」「これから△△さん訪問」「今から□□の打合せ」のような**訪問直前の発話**を
+**DM で**受けたら、既存ツールを束ねて **1 メッセージ**にまとめて返す。
+
+呼ぶ順番（すべて `_user_context.slack_user_id` 付き）:
+
+1. `clientkarte`（`client_name` にその取引先）→ フェーズ・温度感・宿題（次アクション）
+2. `mail_summary`（同じ `client_name`）→ 直近のやり取り。
+   **未返信・こちらの宿題があれば必ず先頭で明示**する（例:「🔴 未返信 1 件: …」）。
+3. `search`（query はその取引先＋「提案」等）→ 過去の提案資料を**上位 2〜3 件だけ**、
+   ヒットの `url` を付けて並べる（`url` が無いヒットにリンクを作らない）。
+
+返す形（**各セクション 3 行以内**・見出し＋要点だけ。長文にしない）:
+
+```
+🏢 ◯◯社 — いま行く前に
+🔴 未返信: （あれば1行。無ければこの行ごと省略）
+📇 状況: フェーズ / 温度感 / 直近の宿題（最大3行）
+📨 直近のやり取り: （最大3行）
+📄 過去資料: - [資料名](URL) を2〜3件
+```
+
+規約（厳守）:
+
+- **(a) クライアント名は敬称を勝手に付け外ししない。** 利用者が「◯◯社」と言えば「◯◯社」、
+  「◯◯さん」と言えば「◯◯さん」のまま扱う（ツールへ渡す `client_name` も同じ）。
+  「様」を足したり「株式会社」を補ったりしない。
+- **(b) DM 限定。** チャンネル/グループで頼まれたら実行せず「この内容は DM でお出しします。
+  DM でどうぞ」とだけ返す（そこにいる人が見られない情報が流れるのを防ぐ）。
+- **(c) いずれかのツールが 0 件でも、取れた分だけで返す（縮退）。** 「情報がありません」で
+  行き止まりにしない。取れなかったセクションは**行ごと省略**し、最後に 1 行だけ
+  「（カルテは未登録でした）」のように正直に添える。全部 0 件のときだけ、
+  「この取引先の記録は見つかりませんでした」と言い切る。
+- **(d) 長文化させない。** 各セクション 3 行以内。原文の引用・全文要約はしない
+  （深掘りが要るなら「詳しく見ますか？」と 1 行だけ添える）。
+- 出典の保全規約どおり、ツールが返した URL は**そのまま**載せる（短縮・省略しない）。
+
+## 時間のかかる処理と「まだ？」への答え方
+
+`proposal_builder_submit` / `tiktok_acquire` / `x_buzz_measure` / `video_analysis` のような
+**数分かかるジョブ**を受け付けたら、受付の返事に必ず次を添える:
+
+> 「数分かかります。気になったら『まだ？』とどうぞ。」
+
+- 「まだ？」「どう？」「終わった？」と聞かれたら、**対応する status ツールを呼んで
+  現在の工程を 1 行で答える**（例:「いま資料の枠を埋めています。あと少しです。」）。
+- **内部語を出さない**: `job_id` / タスク ID / ツール名 / `status` の生値 / 内部の工程名を
+  そのまま見せない。人間の言葉に直して 1 行で言う。
+- **完了の自発通知を約束しない。**「終わったら知らせます」と言わない（こちらから
+  勝手に話しかける仕組みは今は無い）。「『まだ？』と聞いてください」と案内する。
+- 進捗の絵文字（👀→🧠→🛠️→✅）は**依頼メッセージのリアクションとして自動で付く**。
+  その絵文字について自分で実況・説明しない（裏方の秘匿規約と同じ）。
+
+## 次の一手の提案（ツールが `message` に添えてくる）
+
+一部のツールは `message` の末尾に「📅 この予定をカレンダーに追加しますか？」のような
+**提案文を 1 行だけ**付けてくる。これはサーバが文脈から決定論的に付けたもので、
+**そのまま残して返す**（消さない・言い換えない・自分で別の提案を足さない）。
+
+- **提案しただけでは何も実行しない。** 利用者が「追加して」「送って」等と**明示的に応じた
+  ときに初めて**該当ツールを呼ぶ（📅→`calendar_event` の自由文経路 / 📎→`knowledge_deliver` /
+  ✍️→ `attachment_assist` の `mode` を変えて再実行）。
+- **1 応答につき提案は最大 1 個**。ツールが付けてこなかったときに自分で提案を作らない
+  （今 OFF のツールを勧めて「できない約束」をしないため）。
+- 利用者の依頼が既に完結しているとき（自分から「送って」と言っている等）は提案は出ない。
+
 ## トーン
 
 - 日本語。結論先出し・箇条書き・冗長禁止。

```

## 4. 新しい環境変数

| env | 既定 | 効果 | 変更方法 |
| --- | --- | --- | --- |
| `SEARCH_RESULT_GUARD` | `true`（ON） | ②の警告ヘッダ全体の kill switch。OFF ならクライアント辞書 SQL も引かない | env 切替のみ |
| `SEARCH_WEAK_RESULT_THRESHOLD` | `0.3` | top1 スコアがこの値未満で「関連度が低い」警告。`0` で弱ヒット判定だけ無効化 | env 切替のみ |
| `SUGGEST_NEXT_STEP` | `true`（ON） | ⑥-b の提案全体の kill switch | env 切替のみ |
| `SLACK_WORKSPACE_DOMAIN` | 未設定 | ①b の permalink サブドメイン。未設定なら既存 `SLACK_WORKSPACE`（terraform の `var.slack_workspace`）へフォールバック | env 切替のみ（**追加しなくても既存 env で動く**） |
| `SLACK_FILE_REDIRECT_ALLOWED_HOSTS` | 未設定＝`slack-files.com` のみ | ③の 302 転送先 allowlist。**広げないのが既定** | env 切替のみ |

⚠️ `USE_CALENDAR_EVENT_TOOL` / `USE_KNOWLEDGE_DELIVER` は**既存**の env。⑥-b の提案は
これらが `true` のときだけ出る（OFF の環境で「できない約束」をしないため）。
`USE_CALENDAR_EVENT_TOOL` が OFF のままだと ④ の自由文登録も当然使えない。

## 5. 未解決・要裁定

### 5-1. `mail_summary` に Gmail の原本リンクを付けるか（①c の積み残し）

**付けていない。** 理由は既存の死守ライン **G3** と正面衝突するため:

- `src/teamagent/skills/mail_summary/schema.py` 冒頭:
  「⚠️ 戻り値に生メール本文・生 From・**生 messageId** を含めないこと（G3）」
- 同じ方針で `morning_digest/draft_token.py` は thread_id を **HMAC 署名トークン**にして
  Slack の button value にも生 id を出さない設計になっている（repo 横断で徹底済み）。

Gmail の deep link（`https://mail.google.com/mail/u/0/#all/<message_id>`）は
**生 messageId をそのまま URL に載せる**ので、G3 を一方的に破ることになる。

**推奨する落とし所（未実装・裁定待ち）**: message id を含まない**検索 deep link**
`https://mail.google.com/mail/u/0/#search/<urlencoded("client_name" newer_than:Nd)>`
を出典として添える。これは skill が実際に投げた Gmail クエリそのもので、
本人の受信箱をその条件で開くだけ＝**id を 1 つも漏らさずに原本へ辿れる**。
実装は 10 行程度だが「G3 の運用解釈を変える」判断なので人間の裁定に上げる。

### 5-2. `search` の `include_answer=False`（/app の fast path）

②のヘッダは `answer` が空の fast path には**入れていない**（`answer=''` / コスト 0 の
契約を守るため）。`/app` は `include_answer=True` の並行フェッチ側でヘッダを受け取る
（`search/skill.py` の当該コメント参照）。/app 単独でヘッダを出したい場合は別途裁定が要る。

### 5-3. 二段返しの「後追い本体」にヘッダを付けるか

指示どおり**第一報にだけ**付けている。後追い（`deliver_followup_answer`）にも付けると
同じ警告が 2 回出るため。片方だけで良いか運用で確認したい。

---

## 6. `infra/openclaw/openclaw.config.json5` — statusReactions（⑧）

**このファイルは本ブランチでは編集していない。** 統合担当が下記ブロックを入れること。

### 6-1. 入れる場所

既存の `messages:` ブロック（`session:` の直後・現状は `groupChat` だけ）に**マージ**する。
`messages` は**グローバル**（Slack account 単位の上書きは無い）。

```json5
  messages: {
    groupChat: { historyLimit: 20 },

    // ── 進捗リアクション（依頼メッセージの絵文字を工程に応じて差し替える）─────
    // Slack は既定で「native assistant thread status（回転するローディング文言）」を使い、
    // ackReaction は静止したまま。lifecycle 表示に切り替えるには **明示的に true** が要る
    // （docs/channels/slack.md「Ack reactions」・docs/gateway/config-agents.md 参照）。
    statusReactions: {
      enabled: true,
      emojis: {
        queued: "eyes",          // 受付（👀）
        thinking: "brain",       // 推論中（🧠）
        tool: "hammer_and_wrench", // ツール実行中（🛠️）
        done: "white_check_mark", // 完了（✅）
        error: "x",              // 失敗（❌）
        stallSoft: "hourglass_flowing_sand", // 10秒以上動きなし（⏳）
        stallHard: "warning",    // 30秒以上動きなし（⚠️）
        compacting: "compression", // 履歴圧縮中（🗜️）
      },
      timing: {
        debounceMs: 700,
        stallSoftMs: 10000,
        stallHardMs: 30000,
        doneHoldMs: 1500,
        errorHoldMs: 2500,
      },
    },
  },
```

> ⚠️ **Slack はショートコード表記**（`"eyes"`）を使う（既存 `channels.slack.ackReaction: "eyes"` と同じ流儀）。
> dist の既定値は生の絵文字（`"👀"` 等）で定義されているが、Slack 側は
> `reactions.add` の `name` に短縮名を要求するため、**Slack で使うならショートコードで書く**。
> ここが唯一の「dist の既定値をそのまま写せない」箇所。**まず 5 キー（queued/thinking/tool/done/error）
> だけで run-task 検証し、実機で絵文字が付くのを確認してから残りを足す**のが安全。
>
> ⚠️ `emojis` / `timing` は zod で **`.strict()`**。キー名を 1 文字でも間違えると
> config validate が落ち、entrypoint が **exit 78 で起動拒否**する（本 repo の既知の失敗モード）。
> 下記 §6-2 のキー一覧以外を書かないこと。

### 6-2. dist 実読による裏取り（証跡）

一次ソース: **npm レジストリの `openclaw@2026.7.1`**（`Dockerfile.openclaw` が
`ghcr.io/openclaw/openclaw:${OPENCLAW_VERSION}` で固定している版と同一バージョン）を取得して実読。

| 確認事項 | 実読したファイル | 結果 |
| --- | --- | --- |
| 設定パス | `dist/zod-schema-O9ml_nmo.js` / `dist/plugin-sdk/config-schema.d.ts` | `messages.statusReactions`（`MessagesSchema` 内・グローバル） |
| キー（第1階層） | 同上 | `enabled` (bool) / `emojis` (object, strict) / `timing` (object, strict) |
| `emojis` の許可キー | 同上 | `queued` `thinking` `tool` `coding` `web` `deploy` `build` `concierge` `done` `error` `stallSoft` `stallHard` `compacting`（**全 13・これ以外は strict で拒否**） |
| `timing` の許可キー | 同上 | `debounceMs` `stallSoftMs` `stallHardMs` `doneHoldMs` `errorHoldMs`（**全 5**・`int().min(0)`） |
| 既定の絵文字 | `dist/channel-feedback-ChYFAgPX.js` の `DEFAULT_EMOJIS` | 👀 / 🧠 / 🛠️ / 💻 / 🌐 / 🛫 / 🏗️ / 💁 / ✅ / ❌ / ⏳ / ⚠️ / 🗜️ |
| 既定のタイミング | 同上 `DEFAULT_TIMING` | `debounceMs:700, stallSoftMs:10000, stallHardMs:30000, doneHoldMs:1500, errorHoldMs:2500` |
| Slack で明示 true が要る | `docs/channels/slack.md:1083` / `docs/gateway/config-agents.md:1404-1407` | 「Slack, Signal, Telegram, WhatsApp は明示的に `true` にすること。Slack は既定では native assistant thread status を使い ack reaction は静止」 |
| 実行時に実際に使われている | `dist/message-handler.process-Cws5aXTP.js`（`statusReaction*` 参照 39 箇所） | `doneHoldMs` / `errorHoldMs` を実際に sleep に使うコードあり＝**実装済みで休眠している**という前提は正しい |

「コントローラは実装済み・config 未設定で休眠」という前提は**裏取りできた**
（`enabled` が optional で、Slack では未設定＝lifecycle 表示が起動しない）。

### 6-3. 検証手順（統合担当向け）

1. 上記ブロックを入れて `openclaw` イメージを再ビルド（SOUL.md も同じイメージに載る）。
2. **run-task で起動確認**（config validate に落ちると exit 78。ここで必ず捕まえる）。
3. Slack 実機で DM を 1 往復し、依頼メッセージの絵文字が 👀→🧠→🛠️→✅ と遷移することを目視。
   遷移しない場合は `channels.slack.ackReaction` との併用条件（`ackReactionScope`）を確認する
   （既定 `"group-mentions"` は **DM で ack が出ない**。DM でも出したいなら
   `messages.ackReactionScope: "all"`。これは Slack provider 起動時に読むので再起動が要る）。

## 7. ⑦ 訪問前ブリーフィング（SOUL のみ・コード 0 行）

SOUL.md に `## 訪問前ブリーフィング（「いまから◯◯社」）` を新設した（差分は §3 に含まれる）。
新しいツールも新しい引数も**増やしていない**（既存の `clientkarte` / `mail_summary` / `search`
を束ねるだけ）ので、`effective-tool-scope.json` と契約テストの**本数は不変**。

規約 4 点は `tests/infra/test_soul_contract.py` の grep テストで固定した
（(a) 敬称の付け外し禁止 / (b) DM 限定 / (c) 0 件でも縮退して返す / (d) 各セクション 3 行以内）。

⚠️ 依存: `clientkarte` は `USE_CLIENTKARTE` 系、`mail_summary` は本人 Google 連携、
`slack_summary` と同じく **tool が OFF の環境ではその節ごと空振りする**。
本番の env で 3 ツールとも ON になっているかは統合時に確認すること。
