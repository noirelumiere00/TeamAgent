# 連携根治・第3層防御 設計書（draft・実装未着手）

- 対象事故: Aico URL 捏造（2026-08-31 本番実測）
- 塞ぐ穴: 「LLM がツールを 1 つも呼ばないターンは MCP 境界の決定論分岐に到達しない」
- 前提: #352（SOUL 文言防御・今夜 OC 便）/ #349（スキル剥ぎ・供給網便）の裁定は既知
- 上流: OpenClaw 2026.7.1（`npm pack openclaw@2026.7.1` の実物を実測。以下の行番号はその dist）

---

## 0. 結論（先に）

**`agent_end` では不可能。しかし `before_agent_finalize` で可能であり、しかも「定型文への置換」より上等な
「ツールを実際に呼ばせる」形で塞げる。**

既存 docstring（`server.py:651-662`）の 2 つの主張を、上流実物で検証した結果:

| docstring の主張 | 判定 | 根拠 |
|---|---|---|
| 「5 hook のうち戻り値で挙動を変えられるのは `before_tool_call` だけ」 | **その 5 hook の範囲では正しい** | `agent_end` / `message_received` は型が `Promise<void> \| void`（`hook-types-DQ9eTy2x.d.ts:1099,1108`）＝戻り値を受け取らない |
| 「tool 呼び出しを新規に発生させる hook は無い」 | **不正確（要訂正）** | 上流 hook は 5 個ではなく **39 個**（`hook-types:349`）。うち `before_agent_finalize` は `{action:"revise", retry:{instruction}}` を返せ、ハーネスが**追加のモデルパスを実行する**。そのパスは通常のツール一式を持つため、結果として oauth_connect が呼ばれる |

つまり穴は「塞げないので SOUL に託すしかない」ものではなく、**当時 5 hook しか見ていなかったための見落とし**である。

---

## 1. 【裁定1】API 実力の一次検証（最重要）

### 1-1. 不可と確定したもの

- `agent_end: (event, ctx) => Promise<void> | void`（`hook-types:1099`）
  → 応答差し戻し・置換・ツール起動の**いずれも不可能**。貴セッションの当初案（agent_end での差し戻し）は**成立しない**。
- `message_received: => Promise<void> | void`（`hook-types:1108`）
  → ショートサーキット不可。代替案として挙がっていた「message_received 短絡」も**不成立**。

### 1-2. 可能と確定したもの（採用）

`before_agent_finalize`（`hook-types:1098`）:

```
event: { runId?, sessionId, turnId?, stopHookActive, lastAssistantMessage?, messages?, ... }   // :505-517
result: { action?: "continue"|"revise"|"finalize", reason?, retry?: {instruction, idempotencyKey?, maxAttempts?} }  // :518-531
```

実装まで追って確認した経路:

1. `lifecycle-hook-helpers-BIb1q90h.js:111-166` `normalizeBeforeAgentFinalizeResult`
   - `action:"revise"` かつ `retry.instruction` があると `{action:"revise", reason: reason + "\n\n" + instruction}` を返す
   - **冪等予算**: runId × retryKey で回数を数え、`maxAttempts`（既定 1）超過は `continue` へ落ちる＝無限ループ不可
   - hook が例外を投げたら `{action:"continue"}`＝**fail-open**
2. `embedded-agent-CLJk10ON.js:4460-4469`
   - `shouldHonorBeforeAgentFinalizeRevision = !aborted && !promptError && !timedOut && !attempt.clientToolCalls && !attempt.yieldDetected && !emptyAssistantReplyIsSilent`
   - 成立時 `nextAttemptPromptOverride = buildBeforeAgentFinalizeRetryPrompt(reason)` → **エージェントループの次アテンプトへ再入**
   - ハーネス側上限 `MAX_BEFORE_AGENT_FINALIZE_REVISIONS = 3`（`:1774`）
3. `embedded-agent:1773` 再パスの固定前置き:
   > "Before accepting the previous final answer, apply this revision request and produce the revised final answer. **Do not repeat completed work or rerun tools unless the request explicitly requires it.**"

**注目**: `!attempt.clientToolCalls` = 「ツールを呼んでいないアテンプトでのみ revise が honor される」。
本件の発火条件（0 tool call）と**同方向**である。

ただし上流の `clientToolCalls` / `hasCompletedClientToolCall` が MCP（`teamagent__*`）の tool call を
含意するかは**未確認**である。含む場合も含まない場合も本設計の判定は正しい——
権威は §3 の条件 (a)（plugin 自前のカウンタ）であり、上流ゲートはその手前で走る追加の絞りに過ぎない。
ループ不在も上流挙動に頼らず (a) で担保する（§7-1 ケース 8）。

### 1-3. 権限まわり

`before_agent_finalize` は CONVERSATION_HOOK（`command-registration-tKF3dsKu.js:170-178`）。
非 bundled プラグインは `hooks.allowConversationAccess: true` が必須（`registry-D1_pYg_a.js:4225-4235`）。
→ `openclaw.config.json5:74` で **既に true**。**config 変更は不要**。

### 1-4. hook が呼ばれる条件（実測・弱点1の解消）

`selection-8ixiqbew.js:13318-13326` が呼び出し側。`before_agent_finalize` は次を**すべて満たすときだけ**発火する:

- `!willRetry && !isError && !incompleteTerminalAssistant && hasAssistantVisibleText`
- `lastAssistantMessage` が 3 段フォールバック（visible text → raw text → `assistantTexts.join`）で**非空に解決できる**
- `!aborted && !promptError && !timedOut && !hasCompletedClientToolCall && !yieldDetected && !silentFinalReply`

ここから 2 つ確定する:

1. **`lastAssistantMessage` が未定義のまま hook が呼ばれることは無い**（解決できなければ `return` で hook 自体が走らない）。
   当初の弱点「optional なので渡ってこない経路がある」は**消える**。hook が走った時点で本文は必ず手元にある。
2. 呼ばれない経路（error / abort / timeout / 可視テキスト無し）では、**そもそも利用者に本文が届かない**。
   すなわち取りこぼしても偽リンクは出ない＝**良性の穴**である。

さらに `hasCompletedClientToolCall` が真だと hook 自体が走らない。これは本設計の発火条件 (a)（0 tool call）と
同方向であり、上流がすでに「ツールを使い終えたターンは触らない」設計を持っていることを意味する。
§3 の (a) はこれと二重の守りになる。

### 1-5. 却下した代替案

| 案 | 却下理由 |
|---|---|
| `agent_end` で差し戻し | 型が void。不可能 |
| `message_received` 短絡 | 型が void。不可能 |
| `before_model_resolve` でシステム注入 | 可能だが**毎ターン**プロンプトを膨らませる。0 tool call ターンだけを狙えず、SOUL（第2層）と役割が重複 |
| plugin が claim を鋳造して MCP の oauth_connect を直接叩く | **明確に却下**。①plugin が署名 claim を発行できる立場を新設するのは権限昇格そのもの ②使い切り URL の無駄撃ち（利用者が開かないリンクを発行）③LLM の応答と plugin の応答で**リンク二重発行**が起きる。得るものに対しリスクが不釣り合い |
| `message_sending` / `reply_payload_sending` で本文置換 | 単独では非推奨（定型文しか出せず「リンクが欲しい」の再往復を生む）。ただし**最終ネット**としては採用（§4-2） |

**推奨: `before_agent_finalize` の revise 一本。** ツールを実際に呼ばせるので、利用者には正規のリンクが 1 往復で届く。

---

## 2. 【裁定2】発火判定 — 設計の転換

依頼では「残差法を TS 移植するか、SOUL の発火語リストへ寄せるか」の二択が示されたが、**どちらも採らない**ことを推奨する。

### 2-1. なぜ二択とも不適か

`connect_intent.py` の残差法は「**LLM が凝縮した tool 引数**」に較正されている（同 docstring の「残差一致だけでは足りない」節）。
plugin が見るのは利用者の**生 Slack 本文**で、語尾・長さの手掛かりが残っている＝前提が逆。移植すれば較正がずれる。
発火語リストへ寄せる案は単純部分一致に退行し、残差法が明示的に禁じた誤爆（「〇〇社との連携について提案書を」）を復活させる。

さらに致命的なのは、依頼書自身が指摘するとおり**誤爆コストが応答置換では一段厳しくなる**点である。
intent 判定を主軸に置く限り、この非対称性からは逃れられない。

### 2-2. 推奨: intent 判定ではなく「出力検証」を主判定にする

第3層が守るべきものを言い直すと、「連携依頼を取りこぼすな」ではなく
**「捏造した連携 URL を利用者に届けるな」**である。ならば入力の意図ではなく**出力の中身**を見ればよい。

`before_agent_finalize` は `event.lastAssistantMessage`（`hook-types:515`）で
**まだ送っていない応答本文**を渡してくる。ここで次の不変量を検査する:

> **この run の `teamagent__*` tool call が 0 件なら、応答本文に連携 URL が含まれていてはならない。**

0 tool call なら `oauth_connect` は発行していない。よってそこに現れる連携 URL は**定義上すべて捏造**である。
判定に推測が入らない。

これにより誤爆コスト非対称の問題がほぼ消える:

| ケース | 0 tool call | 連携URLを含む | 介入 |
|---|---|---|---|
| 「〇〇社との連携について提案書を」→ 資料検索 | ✗（search 実行） | — | しない |
| 雑談・能力説明 | ○ | ✗ | しない |
| **今回の事故** | ○ | ○ | **する** |

正当な応答が「0 tool call かつ連携 URL 入り」になる経路は存在しない。

### 2-3. URL 検出パターン（保守的に）

`lastAssistantMessage` から URL を抽出し、次のいずれかに当たるものだけを連携 URL とみなす:

- ホストが `openclaw.ai` またはそのサブドメイン（本家ドメイン＝自社では絶対に使わない）
- パスまたはクエリに `oauth` / `authorize` / `/connect` を含む
- ホストが自社 connect-web（`connect.newstv.co.jp`）**かつ** 上記パス条件

素の `https://example.com/help` のような一般 URL は拾わない。
「知識から出典 URL を書いた正当な応答」を巻き込む懸念は、0 tool call 条件が大半を落とす
（`web_research` が走った run は tool call ≠ 0）。残余は §6 の弱点として明記する。

### 2-4. Python / TS の契約同期

出力検証方式では残差法の移植が不要なため、**同期すべき判定ロジックは存在しない**。
同期対象は「連携 URL とみなすパターン」だけになる。これを

`tests/fixtures/connect_url_patterns.json`（新設・単一正本）

に置き、Python 側（将来 `oauth_connect` の出力検査に使う場合）と TS 側 probe の**両方がこの 1 本を読む**。
fixture が変わればどちらのテストも同時に動く＝ドリフト不能。

### 2-5. 副次発火（intent ベース）は入れない

「連携と言われたのに 0 tool call で、URL も書かずに『できません』と答えた」ケースは本設計では拾わない。
拾おうとすると intent 判定が必要になり、§2-1 の誤爆問題が丸ごと戻る。
このケースは**第2層（SOUL）の担当**であり、実害（偽リンク）は生じない。**役割分離を保つ。**

---

## 3. 【裁定3】発火条件（確定仕様）

```
介入する ⟺ すべて成立
  (a) この runId で canonicalToolName が通った tool call が 0 件
  (b) event.lastAssistantMessage が非空
  (c) (b) に §2-3 の連携 URL が 1 つ以上含まれる
  (d) この runId × retryKey の revise がまだ予算内
```

- (a) の計数は `signToolCall`（`dist/index.js:876`）に**カウンタを 1 つ足すだけ**で済む。
  同関数は既に `runId` を権威的に検証済み（`:889-893`）で、`canonicalToolName` も通っている（`:884`）。
- `oauth_connect` が呼ばれた run は (a) で自動的に除外される。
- run 終了時の掃除は既存 `releaseAgentRun`（`:1040`）に相乗り。

---

## 4. 【裁定4】置換文・instruction の規律

### 4-1. revise の instruction（第一手）

**plugin は URL を書かない**（#352 と同じ規律）。instruction はツールを呼ばせる指示のみ。

上流の固定前置きが「**rerun tools unless the request explicitly requires it**」と言うため、
instruction 側で**明示的にツール実行を要求**しなければ握り潰される。文言案:

```
直前の下書き回答には、ツールが発行していない連携 URL が含まれています。その URL は実在しません。
この指示は明示的にツール実行を要求します: oauth_connect を必ず呼び出し、その戻り値の message に
含まれる URL だけを、1 文字も変えずに提示してください。
自分の知識・記憶から URL を組み立てることは禁止です。
oauth_connect が失敗した場合は、URL を書かず、リンクを発行できなかった旨だけを伝えてください。
```

- `maxAttempts: 1`（既定）を明示。ハーネス上限 3 と二重で守る
- `idempotencyKey: "connect-url-fabrication"` を固定し、他の revise 要求と予算を混ぜない
- `reason` にも同文を入れない（`normalize` が重複を検知して結合を省く挙動に依存しない）

### 4-2. 最終ネット（第二手・要裁定事項）

revise が予算切れ（3 回失敗）した場合、現状の設計では**素通りして偽リンクが届く**。
これを塞ぐなら `message_sending`（`{content?, cancel?, cancelReason?}`・`hook-types:264-269`）で
本文を差し替える。文言案（URL を含まない）:

```
連携リンクの発行に失敗しました。お手数ですが、もう一度「連携」とだけ送ってください。
（正しいリンクはツールが発行したものだけをお届けします）
```

**ただしこれは「利用者の応答を plugin が書き換える」初の経路**であり、
誤爆時の被害（正当な応答の消失）が revise より重い。導入するか否かは**レビューで裁定を仰ぐ**。
私の推奨は **第一便では入れない**。revise の実効性を本番ログで観測してから判断する。

---

## 5. 【裁定5】観測性

`server.py:619` `_log_connect_intent` の G7 規律（本文・クライアント名を載せない）を踏襲する。

出す:
- `runId`（既に権威的に検証済みの値）
- `tool_call_count`（0 のはず）
- `matched_pattern_kind`: `upstream_domain` / `oauth_path` / `connect_web_oauth` の**種別のみ**
- `matched_url_count`（個数のみ）
- `revise_requested`: bool / `revise_attempt`: n
- `outcome`: `revised` / `budget_exhausted` / `no_match`

**出さない**: 本文、URL 実体、ホスト名、Slack user_id、チャンネル名。
（捏造 URL には user_id が埋まっていた実績があるため、URL 実体のログ出力は G7 違反そのものになる）

イベント名: `connect_url_fabrication_blocked`。第1層の `connect_intent` ログと**別名**にして混線を防ぐ。

---

## 6. 【裁定6】凍結面と便

- `infra/openclaw/` は `activation_freeze.json` の `frozen_change_surface` **非該当**（#349 で一次確認済み）
- ただし plugin 実体は `dist/index.js` としてイメージへ焼かれるため **OC イメージ再ビルドが必須**

**推奨: #349（supply-chain 便）への同乗ではなく、次の OC 便に単独で乗せる。**

根拠:
1. #349 は unlock 宣言・世代 publish・contract SHA 再取得という**手順の正しさ**を証明する便である。
   そこに Aico の**挙動変更**を混ぜると、本番で異常が出たとき「契約手順の問題か挙動変更の問題か」の
   切り分けが不能になる
2. 第3層は #352（SOUL）が本番で効いているかを観測してから調整したい。#352 より先または同時に出すと、
   どちらが効いたのか判別できない
3. 凍結面非該当なので、供給網便を待つ理由（unlock 宣言）が無い

---

## 7. 【裁定7】テスト計画

### 7-1. 常に走る形（probe 拡張）

`tests/scripts/openclaw_caller_identity_probe.mjs` の流儀（上流実測値を焼き込み・上流非依存）に倣い、
同ファイルへケースを追加する。新規ファイルにしないのは、既存 probe が
「CI から常に走る」配線を既に持っているため（新設すると配線漏れで緑のまま壊れる前例＝probe 冒頭コメントの教訓を踏む）。

ケース:
1. 0 tool call ＋ 捏造 URL 入り `lastAssistantMessage` → `{action:"revise"}` を返し、instruction にツール実行要求が含まれる
2. 0 tool call ＋ URL 無し（雑談） → `undefined`（不介入）
3. tool call 1 件（search）＋ 本文に一般 URL → 不介入
4. **oauth_connect を呼んだ run** ＋ 本文に正規 URL → 不介入（(a) で除外されること）
5. 予算超過（同 runId で 2 回目） → 不介入
6. `lastAssistantMessage` 未定義 → 不介入（fail-open）
7. ログに URL 実体・本文が出ない（G7）

### 7-1b. 台帳の寿命（レビュー指摘 2026-08-31）

`toolCallsByRun` / `connectRevisionsByRun` の掃除を `agent_end`（`releaseAgentRun`）だけに
任せると、agent_end が発火しない run（abort / crash / timeout）が残留し、長寿命プロセスで
無制限に育つ。他の Map と同じ TTL 掃除（`INBOUND_CONTEXT_TTL_MS`）に加え、
上限（`MAX_CONNECT_GUARD_RUNS`）超過時は最古から捨てる。

- 掃除は `pruneConnectGuardState` に切り出し、`pruneState` と guard の**両方**から呼ぶ
  （finalize だけが走る経路でも育たないようにする）
- guard から `pruneState` 全体は呼ばない。容量超過の `fail` を握り潰して fail-open させないため
- `MAX_TRACKED_CONTEXTS` の `fail` には**相乗りさせない**。この防御の溢れで署名経路を
  落とすのは不釣り合いで、脱落しても上流の revise 予算がループを止める
- ケース 9（上限）とケース 10（TTL）を probe に追加。**両方が独立に必要**——
  上限退避だけでは「少数 run が長期残留」が緑のまま通る（実測で確認）
- 退避順: JS の Map は既存キーへの再 set で挿入順が更新されない（実測）。
  そのため記録の更新側で `delete` → `set` して、挿入順を更新順に一致させる
- 両台帳とも `delete` → `set` で統一する。`connectRevisionsByRun` は
  `MAX_CONNECT_FABRICATION_REVISIONS` が 1 である限り再 set が起きないため
  現状は退避順が自明に正しいが、その値を 2 以上へ上げた瞬間に壊れる依存を残さない
- なお `toolCallsByRun` が自前の上限に達する状況は実際には到達不能である。
  tool call を伴う run は `ingressByRun` / `consumedInvocations` も同時に増やすため、
  1000 run に達する前に既存の `MAX_TRACKED_CONTEXTS` の `fail` が先に飛ぶ。
  この経路の専用テストは書いていない（到達不能な経路に、落ちないテストを
  足しても緑の実質性が下がるだけのため）

### 7-2. 変異テスト（緑の実質性）

- (a) の tool-call カウンタを常に 0 にする改変 → ケース 3・4 が赤
- URL パターンから `openclaw.ai` を外す → ケース 1 が赤
- instruction から「明示的にツール実行を要求します」を削る → 専用アサーションが赤
- ログから種別のみ出力を「URL 実体」に変える → ケース 7 が赤

各改変で**赤くなることを実行して確認**してから提出する（#349 / #352 と同じ流儀）。

### 7-3. 上流 API 契約の固定

本設計は上流の `before_agent_finalize` 実装に依存する。上流更新で静かに壊れないよう、
probe に**上流バージョン表明**を焼き込む（`openclaw@2026.7.1` の型と `MAX_BEFORE_AGENT_FINALIZE_REVISIONS=3`）。
バージョンが動いたら再実測する運用を README に 1 行残す。

---

## 8. セルフFB（反対尋問）— 残る弱点

1 周した結果、潰せなかった点を明記する。

1. ~~`lastAssistantMessage` が optional~~ → **§1-4 で解消**。hook が走る時点で本文は必ず非空。
   呼ばれない経路は利用者にも本文が届かないため良性。**弱点ではない**ことを実測で確定済み。
2. **予算切れで素通りする**（§4-2）。最終ネットを入れない推奨なので、3 回失敗＝偽リンクが届く。
   確率は低いが 0 ではない。
3. **URL パターンはヒューリスティック**。「0 tool call かつ oauth 系 URL を含む正当な応答」が
   将来生まれれば誤爆する（例: LLM が知識から Google の OAuth 説明ページを引用）。
   現時点で該当する正当ユースケースは思い当たらないが、**存在しない証明はできていない**。
4. **dark 経路（`USE_AGENT_ORCHESTRATOR=1`）は未検証**。`server.py:677-681` のとおり第1層は
   意図的に非適用。第3層が embedded agent 前提の hook である以上、dark 経路での挙動は別途実測が要る。
5. **`dist/index.js` は TS ソース無しの手書き ESM 1089 行**。変更はレビュー負荷が高く、
   型による保護が無い。probe の網羅性が唯一の安全網になる。
6. **上流 API 依存**（§7-3）。2026.7.1 に固有の挙動であり、上流更新で静かに死ぬ可能性がある。

---

## 9. 実装フェーズの見積り（分担裁定の材料）

| 作業 | 規模 | 適性 |
|---|---|---|
| `dist/index.js` へのカウンタ＋finalize hook 追加 | 約 80-120 行 | 手書き ESM・既存流儀の踏襲が要る。**Codex 委任可**（file:line 先渡し前提） |
| URL パターン fixture | 小 | Codex 委任可 |
| probe 拡張 7 ケース | 約 150 行 | Codex 委任可 |
| 変異テスト実行・緑の実質性証明 | — | **こちら（検証は委任しない）** |
| 上流再実測（弱点 1・4） | — | **こちら**（一次検証の性質上） |


---

## 10.【追記 2026-09-03】決定論の最前段（層1）と 0 tool call 対策（層2・層3）

（依頼上は「§7 として追記」だが §7 はテスト計画で使用済みのため §10 に置く）

### 10-0. 事故の再定義

2026-09-03 実測: 利用者 u-imai が DM で「連携」とだけ送っても、Aico がツールを一度も呼ばず
「未登録／管理者に問い合わせ」と**自作回答**する事故が同一 DM で 5 回以上。mcp 側の
`mcp_connect_intent` はゼロ＝MCP 境界の決定論分岐（`server.py:_maybe_redirect_to_connect`）は
tool 呼び出しが無いので到達せず、§2 の出力検証（URL 検出）も**応答に URL が無い**ので効かない。
09-02 の一斉オンボでも複数 DM で「0 tool call の返信」を観測。

裁定（ユーザー指示）: 「③ Aico がツールを呼ばない」を**必ず起きないように**する。revise だけでは
LLM が従わない残余が消えないため 3 層にする。優先順位は 層1 > 層2 > 層3。

### 10-1. 層1 の手段選定 — 上流 2026.7.1 の一次検証（推測禁止）

候補 (a)(b)(c) を、`npm pack openclaw@2026.7.1` の実物（`dist/` の各ファイル）と
`@openclaw/slack@2026.7.1`（sha256 `d6ae87…ef46`＝`infra/openclaw/plugins-lock.json` と一致）で検証した。

| 候補 | 判定 | 根拠（file:line） |
|---|---|---|
| (a-1) `message_received` で「処理済み・返信はこれ」を返す | **不可** | 型が `Promise<void> \| void`（`hook-types-DQ9eTy2x.d.ts:1108`）。`runVoidHook` は戻り値を捨てる（`hook-runner-global-Cucx8m-W.js:458-477`）。しかも `fireAndForgetHook` で呼ばれる（`dispatch-V82RCNJs.js:1438`） |
| (a-2) `inbound_claim` で `{handled, reply}` を返す | **本件では不可** | 戻り値型は `PluginHookInboundClaimResult = {handled, reply?}`（`hook-types:551-554`）だが、呼ばれるのは **plugin-owned conversation binding がある会話だけ**（`dispatch:1490-1512` の `pluginOwnedBinding` 分岐・`runInboundClaimForPluginOutcome`）。Slack DM は plugin 所有ではないので発火しない |
| **(a-3) `before_agent_reply` で `{handled:true, reply}` を返す** | **採用** | 型 `PluginHookBeforeAgentReplyResult = {handled, reply?: ReplyPayload, reason?}`（`hook-types:419-423`）。通常応答経路 `get-reply-CknL88Yv.js:5596-5623` で**モデル起動より前**に走り、`handled` なら `return hookResult.reply ?? {text:"NO_REPLY"}`＝**モデルを起動しない**。first-claim wins（`hook-runner-global:519-544`）。CONVERSATION hook（`command-registration-tKF3dsKu.js:170-178`）だが `openclaw.config.json5:74` で `allowConversationAccess: true` 済み。既定タイムアウト無し（`hook-runner-global:435`・`modifyingHookTimeoutMsByHook` に未登録）→ plugin 側で `AbortSignal.timeout(20s)` を持つ |
| (b) `before_model_resolve` / `before_prompt_build` で「oauth_connect を今すぐ呼べ」を強制 | **不可（強制にならない）** | `before_model_resolve` の戻り値は `{modelOverride?, providerOverride?}` のみ（`hook-types:44-46`）。`before_prompt_build` は system/prepend/append の文面注入のみ（`hook-types:52-62`）。`tool_choice` / forced tool 相当のフィールドは `hook-types` 全体に存在しない（`grep -n "toolChoice\|tool_choice\|forcedTool"` で 0 件）。文面注入は SOUL（第2層）と同じ「お願い」であり、今回の事故はまさにその「お願い」が無視された事例 |
| (c-1) plugin API から MCP tool を直接起動する API | **無い** | `OpenClawPluginApi`（`types-DaHgOqFX.d.ts:12078-12147`）に `registerTool` / `registerHook` / `registerCommand` 等はあるが、既登録ツール（MCP 経由の `teamagent__*`）を plugin から呼ぶ `callTool` 相当は無い |
| **(c-2) plugin が既存の署名 claim で mcp `/mcp` へ `tools/call oauth_connect` を直接 POST** | **採用（層1 の実体）** | 手順は `infra/openclaw/rollout-task-canary.mjs:47-110,140-160`（同イメージ内で `fetch` により `initialize → notifications/initialized → tools/list` 済み＝経路と bearer の実績）と同一。claim は `signToolCall` と同じ鋳造関数 `mintCallerClaim` を使い、mcp 側は `caller_claim.py` の既存検証をそのまま通す＝**新しい信頼境界を作らない** |

**(c-2) の前提条件と実測**:

- 利用者の生本文: `message_received` の `event.content` は `BodyForCommands ?? RawBody ?? Body`
  （`message-hook-mappers-BK8VuspZ.js:23`）。Slack はこれを `commandBody ?? rawBody`＝**封筒無しの本文**として渡す
  （`kernel-DIE2bgVW.js:283-285` / `@openclaw/slack pipeline.runtime-rpVpay59.js:3505-3512`）。
  一方 `before_model_resolve` の `event.prompt` と `before_agent_reply` の `cleanedBody` は封筒付き（`formatInboundEnvelope`・
  `get-reply:1555-1577`）なので判定には使わない。**本文は保持せず、判定の真偽だけを ingress に載せる（G7）。**
- `before_agent_reply` の ctx には `runId` が無い（`get-reply:5599-5617`）。会話照合は `sessionKey × senderId × channel`
  で行い、`bindAgentRun` と同じ `matchesConversation` を使う。`message_received` が `runId` を伴うと ingress は
  既に run へ束縛され pending から消える（`rememberInbound → bindRun → removePending`）ので、**pending と束縛済みの両方**を見る。
- mcp の claim 検証は `channel` に `^[CDG][A-Z0-9]{8,}$` を要求する（`caller_claim.py:_SLACK_CHANNEL_RE`）。DM の inbound は
  `user:U…` → 内部別名 `DM:U…` にしかならない。`before_agent_reply` の ctx は channel fields の**後に** identity fields を展開する
  （`get-reply:5605-5616`）ため `ctx.chatId` が `NativeChannelId ?? ChatId`＝Slack の `conversation.id = message.channel`
  （`pipeline.runtime:3487-3489` / `kernel:298`）＝DM では `D…` になる。層1 はこれを**この送信者の DM に限って**正準 id として採る
  （`bindAgentRun` の `dmAlias` 昇格と同じ規律）。`D…` が取れなければ層1 は鋳造せず層2 へ落とす（`reason=no_canonical_channel`）。
- 環境: `TEAMAGENT_MCP_BEARER` / `TEAMAGENT_CALLER_CLAIM_SECRET` は gateway タスクの env に注入済み（`infra/terraform/fargate.tf:842,844`）。
  URL は canary と同じ `http://teamagent-mcp.teamagent.internal:8787/mcp`（`TEAMAGENT_MCP_URL` で上書き可）。

### 10-2. 3 層の確定仕様

| 層 | hook | 発火条件 | 動作 | 失敗時 |
|---|---|---|---|---|
| 1 | `before_agent_reply` | messageProvider=slack・trigger=user・会話に一意の ingress・`connectRequest`・未試行・正準 channel・bearer あり | `mintCallerClaim(tool=oauth_connect)` → `tools/call` → 戻り値 `message` を `{handled:true, reply:{text}}` で返す | **次の層へ**（`undefined` を返す。理由コードを warn） |
| 2 | `before_agent_finalize` | (a) 0 tool call ∧ (b) 本文あり ∧ ((c) URL 捏造 ∨ `ingressByRun[runId].connectRequest`) ∧ (d) 予算内 | `revise`。URL 側は #353 の instruction、zero-tool 側は固定文（§10-3）。予算は両ルール共有・1 run 1 回 | 予算切れ → 層3 を武装 |
| 3 | `reply_payload_sending` | `event.runId === ctx.runId` ∧ 武装済み ∧ payload.text あり | 1 通目を定型文へ置換、2 通目以降は `cancel` | runId 不一致は触らない |

「短い連携依頼」: 前後の空白・句読点・絵文字・Slack マークアップ・敬語末尾を落として NFKC 正規化した本文が
**12 文字以下** かつ `^(再)?(Google|グーグル|Slack|スラック)?(再)?(連携|接続|connect)(助詞)?$`。
単一正本は `tests/fixtures/connect_request_phrases.json`（must_match 26 / must_not_match 12）。

### 10-3. 固定文（変更しない）

- 層2 instruction: 「利用者は Google/Slack 連携を依頼しています。`oauth_connect` ツールを必ず呼び、その戻り値の message とリンクを一字も変えずに提示してください。自分で原因を推測したり、管理者への問い合わせを案内したりしてはいけません。」（`maxAttempts: 1`・`idempotencyKey: connect-zero-tool`）
- 層3 本文: 「連携リンクの発行に失敗しました。もう一度『連携』と送ってください。解決しない場合は次の 1 行を管理者（小俣）へ送ってください: 診断: CONNECT-Z01 <YYYY-MM-DD HH:MM JST> <Slack user id>」

### 10-4. 観測性（§5 の G7 規律を踏襲）

- 層1: `connect deterministic path invocation=<connect-l1-…> outcome=answered|fallthrough reason=<code>`
- 層2: `connect zero-tool revise runId=… tool_calls=0 reason=short_connect_request outcome=revised|budget_exhausted`、
  武装時 `reason=model_did_not_call_tool outcome=fallback_armed diagnostic=CONNECT-Z01`
- 層3: `connect zero-tool fallback runId=… outcome=replaced diagnostic=CONNECT-Z01`
- **出さない**: 本文・URL・Slack user_id・bearer。依頼書は「sender id」をログに含める案だったが、§5 と plugin 既存の
  「caller identity はログに出さない」規律に合わせて**載せない**（縮小点として明記）。診断行の user id は利用者→管理者へ
  転記される経路で届き、ログ側は runId＋時刻で突合する。

### 10-5. 却下・縮小・残る弱点

1. `inbound_claim` 短絡は plugin-owned binding 前提なので採れない（10-1）。会話を plugin 所有にする案は Slack DM 全体の
   配線を変えるため見送り。
2. 層1 が `D…` を取れない ctx 形状（`chatId` が `U…` のまま）では層1 は発火せず層2/3 のみになる。コード上は `ChatId=message.channel`
   を確認したが、**本番ログでの実測は OC 便の後**（`reason=no_canonical_channel` の有無で判る）。
3. `before_agent_reply` は `useFastTestBootstrap` 時（テスト用）と `before_agent_reply` を持たない経路では走らない。
   通常の Slack 応答経路（`get-reply`）では走る。
4. 層1 で `oauth_connect` が業務エラー（例: `no_user_email` の fail-closed）を返すと層2 へ落ち、モデルが同じ tool を呼んで
   同じエラーを受ける。利用者には「発行できなかった」旨が届く（定型文ではなくモデル文）。
5. 層3 は「利用者の応答を plugin が書き換える」初の経路（§4-2）。誤爆面は「短い連携依頼 ∧ revise 後も 0 tool call」に限定。
6. URL 捏造ルール（#353）単独で予算切れした run は従来どおり素通り（層3 は zero-tool 側のみ武装）。
7. `before_agent_reply` の hook 例外はハーネスが握って次の handler/通常処理へ進む（`hook-runner-global:537-543`）＝
   plugin 内で catch 漏れがあっても fail-to-next-layer は保たれる。

### 10-6. 敵対的レビュー（2026-09-03）で直した点

1. **層1 成功時に受信を消費する**: `handled` で返すとモデルが起動せず `before_model_resolve` が走らないため、
   受信を `pendingByMessage` に残すと同じ DM の次の受信で `bindAgentRun` が `candidates=2` で run を拒否し、
   以後 10 分間すべてのツールが「trusted Slack run identity is missing or stale」でブロックされる（レビュー実証）。
   成功分岐で `removePending(ingress)`＋束縛済み `ingressByRun` の同一オブジェクトを削除。fallthrough 分岐では残す。
2. **候補 2 件以上は無言にしない**: `reason=ambiguous_ingress` で warn して層2 へ渡す。
3. **別送信者の受信を使わない**ことをテストで固定（スレッドで B の「連携」が pending でも A には不発）。
   `matchesConversation` の senderId 照合を外す変異で赤になる。
4. 語彙: 「する／させて／してくれる／できる（疑問）」を末尾語に追加。「連携解除／連携できない／連携済み／連携やめて」は偽のまま fixture で固定。
5. 層1 の 3 POST は 15s の全体予算を 1 本の `AbortSignal` で共有（claim TTL 60s と同長にしない）。
6. 既知事項（記録のみ）: `message.trim()` は末尾改行を落とす。mcp 成功後に応答の読取だけ失敗すると層2 でモデルが再発行し
   リンクが 2 本（1 本目は未使用）になりうる。

---

## 11.【追記 2026-09-03】ツール引数の二重包みと、拒否の観測性

### 11-0. 実測（OpenClaw の EFS 上のセッション記録・読み取り専用 Fargate プローブで集計）

| 観測 | 値 |
|---|---|
| セッション記録 | 166 ファイル / tool call 363 件 |
| `before_tool_call` で block | **83 件（23%）**（toolResult `details.status="blocked"` / `deniedReason="plugin-before-tool-call"`） |
| 理由の内訳 | `_user_context must be a plain object` **72** / `trusted Slack run identity is missing or stale` 9 / `declared channel_id does not match the bound ingress` 2 |
| 引数の包み形 | `{"arguments":{"_user_context":{…}}}` **74 件** / `{"name":"teamagent__oauth_connect","arguments":{…}}` 2 件 |
| 全滅セッション | 7 本以上（14/14, 9/9, 8/8, 7/7, 5/5, 5/5, 4/4）。9/2 の一斉オンボ時に集中し 9/3 も継続 |
| 発生ツール | oauth_connect 105 / search 95 / calendar_event 19 / tiktok_acquire 16 / tiktok_search 15 / mail_summary 14 / slack_summary 14 … ＝**ツールを問わない** |
| モデル | Bedrock `jp.anthropic.claude-haiku-4-5-20251001-v1:0` |

ブロックされた toolResult を見たモデルが「技術的な問題」「管理者へお問い合わせ」と自作回答するため、
利用者には「連携できない」としか見えていなかった。セッションを作り直しても再発するので、
履歴汚染ではなく**モデル側が引数をもう一段包む癖**と見る。

### 11-1. 決定論の unwrap（`unwrapToolArguments`）

`signToolCall` の**引数検査より前**に 1 度だけ通す。

- (a) トップのキーが `arguments` 1 つだけで、その値がプレーンオブジェクト → その値を採用
- (b) キー集合が `{name, arguments}` で `name` が呼び出し中のツール名（`teamagent__<tool>` / `<tool>`）と一致し、
  `arguments` がプレーンオブジェクト → `arguments` を採用
- (c) 最大 **2 段**まで再帰。2 段剥がしてもまだ包みなら剥がさず元のまま返す（＝3 段以上は従来どおり block・診断 P06）
- (d) それ以外は無変更（同一参照をそのまま返す＝バイト同一）

剥がしたら warn 1 行 `unwrapped tool arguments (shape=arguments|name_arguments, depth=n)`。
識別子・本文・URL は載せない（§5 の G7 規律）。

**信頼境界は動かない**: unwrap 後も `_user_context` は `mintCallerClaim` が authoritative な署名済み値で
上書きするため、利用者・モデル由来の `_user_context` は元々すべて破棄される。ここで変わるのは
「どの階層を検査するか」だけで、検査は 1 つも緩めない。包みの中で別人を騙った場合に、包まない場合と
**同じコード（P05）で拒否される**ことをテストで固定した（`test_unwrap_does_not_move_the_trust_boundary`）。

### 11-2. logger 到達性の一次検証（`openclaw@2026.7.1` の実物 dist）

**結論: 条件つき YES。既定構成なら `api.logger.warn` はコンテナのログに到達するが、行き先は
stdout ではなく `console.warn`（＝stderr）。ECS の awslogs ドライバは stderr も CloudWatch へ送る。
ただし logger が完全な no-op に差し替わる登録経路が実在するため、console にも直接書く。**

展開物: `npm pack openclaw@2026.7.1` を scratchpad に展開（`occore/package`）。以下は同 package 起点の相対パス。

| # | 事実 | file:line |
|---|---|---|
| 1 | `buildPluginApi` が `logger: params.logger` をそのままプラグイン API に載せる | `dist/api-builder-CX43eAAh.js:109,123` |
| 2 | 実運用の登録経路 `createApi()` が `logger: normalizeLogger(registryParams.logger)` を渡す | `dist/registry-D1_pYg_a.js:4393,4404` |
| 3 | `normalizeLogger` は `{info,warn,error,debug}` を抜き出すだけ（実装はクロージャ参照なので `this` 喪失なし） | `dist/registry-D1_pYg_a.js:4271-4276` |
| 4 | 既定 logger は `createSubsystemLogger("plugins")`＝**サブシステム名は `plugins`** | `dist/loader-svIpMF0d.js:646,1497,1570-1571` |
| 5 | `warn()` → `emitLog("warn", …)` → `writeConsoleLine(...)` | `dist/subsystem-C3fiUGN1.js:205-211,231-233` |
| 6 | `writeConsoleLine` は warn を `console.warn` に出す（`process.stdout.write` でもファイル直書きでもない）。Node の `console.warn` は **stderr** | `dist/subsystem-C3fiUGN1.js:161-164` |
| 7 | console level の既定は `"info"`。`warn(4) >= info(3)` なので**既定で通る** | `dist/console-DDSYsaep.js:16,40` / `dist/subsystem-C3fiUGN1.js:16-20` / `dist/logger-DPps3u8A.js:32-42` |
| 8 | 環境変数は `OPENCLAW_LOG_LEVEL` のみ。未設定ならオーバーライドなし（本 repo に設定箇所なし＝grep 0 件） | `dist/logger-DPps3u8A.js:15-23,46-61` |
| 9 | subsystem フィルタの既定は `null`＝全通過。設定するのは CLI `run` と TUI だけで gateway 経路では呼ばれない | `dist/state-CZ7QadD1.js:13` / `dist/console-DDSYsaep.js:76-82` / `dist/run-CPf2XxVd.js:1151` |
| 10 | `forceConsoleToStderr` の既定は `false` | `dist/state-CZ7QadD1.js:11` |
| 11 | 本番 gateway 経路では `routeLogsToStderr()` が走らない（`ownsProtocolStdout` 既定 false・`--json` なし） | `dist/command-path-policy-NzlS0DJF.js:250-252,720` / `dist/command-startup-policy-Bq9-nxRO.js:19` |
| 12 | **bundled capability runtime 経由の登録では logger が完全な no-op**（`{info(){},warn(){},error(){},debug(){}}`） | `dist/bundled-capability-runtime-DNfN9uhv.js:85,92-97` |

出ない条件（実物根拠あり）: `OPENCLAW_LOG_LEVEL` を `silent`/`error`/`fatal` に設定 /
config の `logging.consoleLevel` を同様に設定（現 `openclaw.config.json5` に `logging` ブロックなし）/
`setConsoleSubsystemFilter` で `plugins` が外れる（gateway では非該当）/ **#12 の no-op logger 経路** /
`--json` 付き CLI・`acp`・`mcp serve`・`tools stdio` / `VITEST=true`。

**判断**: #7〜#11 だけを見れば logger で足りる。しかし実測では **14 日間 1 行も出ていない**。
主原因は本 repo 側にあり、`register` が `before_tool_call` にだけ `api.logger` を渡しておらず、
`signToolCall` の block 経路は 1 行も書いていなかった（本 PR で修正）。加えて #12 の no-op logger 経路が
実在する以上、拒否の観測を上流の logging 構成に依存させない。したがって
`emitPluginLog` は **logger と console の両方**へ同じ 1 行を書く。
本番では logger 側の `[plugins] teamagent-caller-identity: …` と console 側の
`teamagent-caller-identity: …` の 2 行になるが、**拒否が必ず観測できること**を優先する。

**縮小点（明記）**: 依頼書は「stdout に 1 行」だったが、実装は `console.warn`＝**stderr** に出す。
上流 logger と同じ経路に揃えるため。ECS の awslogs は stdout / stderr を同じロググループへ送るので
CloudWatch 上の可観測性は同じ（`harden-task-definition.jq:199-205` が `logDriver == "awslogs"` を要求）。

### 11-3. 診断コード（P 系統）

`block(reason)` が返す文の末尾に、利用者がそのまま転送できる 2 行を付ける。

```
teamagent-caller-identity: <従来どおりの理由>
解決しない場合は、次の 1 行をそのまま管理者（小俣）へ送ってください:
診断: CONNECT-P03 2026-09-03 16:35 JST
```

コード体系は `src/teamagent/connect_diagnostics.py`（`ConnectDiag` / `DIAG_SPECS`）の流儀に合わせた。
**P 系統の正本は plugin 側の `BLOCK_DIAG`**（Python から発行しないため）。意味・ログの引き方・対処は
`docs/runbooks/connect_diagnostics.md` の P コード節。user id は載せない（G7）。時刻は `formatJstMinute`
（第3層の `CONNECT-Z01` と同じ関数）。

### 11-4b.【追記・レビュー指摘 2026-09-03】会話 id / team id の実値をログから外す

本 PR で `emitPluginLog` が console へ**必ず**二重書きするようになったため、
上流のログレベル抑制が効かなくなった。そこで実値を出していた 2 箇所を形に置換した。

| 箇所 | 旧 | 新 |
|---|---|---|
| `bindAgentRun` の会話 id 不一致 | `runChannelId=C0B0PQD83N2 pendingChannelIds=[DM:U09CX1CCBLN]` | `runChannelShape=C pendingChannelShapes=[U] pendingChannelDistinct=1` |
| `rememberInbound` の他ワークスペース | `foreignTeam=<T…> expected=<T…>` | `foreign_team=true` ＋ `id_shape` の `team:mismatch` |

**なぜ旧実装が誤りだったか**: 「会話 id は Slack のチャンネル/DM 識別子であって caller identity ではない」
というコメント付きで実値を出していたが、**DM では成り立たない**。`resolveSlackChannel`（`dist/index.js:285-286`）は
DM を `DM:<senderId>` に解決するため、その実値は **Slack user id そのもの**になる。
実証ログ: `runChannelId=C0B0PQD83N2 pendingChannelIds=[DM:U09CX1CCBLN]`。

診断能力は落としていない: `matchChannelId=0` と両側の形の不一致で切り分けられる。
実値が要る調査は Slack 側で行う。

### 11-4. `id_shape`（値を出さずに形だけ出す）

拒否ログには `id_shape=sender:U,channel:D,message:ts,session:thread,team:match` のように
**先頭 1 文字と構造の有無だけ**を載せる。Enterprise Grid の `W…` user id や、想定外のチャンネル形
（`user:U…` / `c…:thread:<ts>` / `slack`）で拒否が出ていないかを、本番ログの生値を見ずに切り分けるため。
実値（Slack user id / channel id / ts）が含まれないことをテストで固定した。

### 11-6.【追記】層1 の脱出経路をすべて観測可能にする（2026-09-03 の事故）

**事故**: OC TD:43（dev `8a1560b`・#381 の層1 入り）が 2026-09-03 16:37 JST に着地した直後、
DM で「連携」を送ったところ**層1 が発火せずモデル経路になった**（OC ログに
`[agents/tool-policy] tool policy removed 26 tool(s)` ＝モデル起動。層1 が `handled` を返していれば
モデルは起動しない）。plugin のログは CloudWatch に 1 行も無く、どの条件で落ちたか判別できなかった。

**原因（コード上）**: `answerShortConnectRequest` は `fallthrough()` を通る 3 経路以外、
すべて無言の `return undefined` だった。前提条件で落ちた場合と、層1 に入って失敗した場合が
区別できず、`before_agent_reply` が呼ばれたのかどうかすら判らない。

**対処**: 全脱出経路に理由を付け、`outcome` を 3 語に分けた。

| outcome | 意味 | 既定で出るか |
|---|---|---|
| `layer1 entered provider=… trigger=…` | **hook が呼ばれた事実そのもの**。これが無ければ `before_agent_reply` が呼ばれていないと確定できる | trace のみ |
| `outcome=skipped reason=…` | 前提条件で層1 に入らなかった | trace のみ |
| `outcome=fallthrough reason=…` | 層1 に入ったが実行できずモデル経路へ渡した | **常時** |
| `outcome=answered tool_calls=1` | 層1 が handled で応答した | **常時** |

`skipped` の reason は 6 種。`not_slack_provider` / `trigger_not_user`（実 trigger 値を併記）/
`missing_session_or_sender_or_channel`（どれが欠けたか＋`id_shape`）/
`no_candidate_ingress`（`pending=` と `bound=` の件数）/ `not_connect_request`（正規化後の**文字数だけ**）/
`already_attempted`。

`rememberInbound` にも `inbound recorded connect_request=… normalized_len=… pending=… bound=… id_shape=…`
（trace のみ）と `inbound rejected reason=…`（常時）を入れた。層1 の `no_candidate_ingress` が
「受信を記録できていない」のか「照合が外れた」のかは、この 2 行の有無で切り分ける。

**トレードオフと env**: `not_connect_request` は通常の会話 1 通ごとに出るためノイズになる。
そこで詳細は **`TEAMAGENT_CALLER_IDENTITY_TRACE=1`** のときだけ出し、既定は
block / fallthrough / answered / rejected のみ。env は OC のタスク定義で注入する。
**挙動は env に依存しない**（`handled` の可否は同じ）ことをテストで固定した
（`test_trace_is_off_by_default_and_does_not_change_behaviour`）。

### 11-5. 残る弱点

1. `before_tool_call` の ctx には `senderId` が無い（本番実測形状）ため、そこでの `id_shape` は
   `sender:absent` になる。`W…` の切り分けは `bindAgentRun` 側の 1 行で行う。
2. logger 側と console 側で本番ログが 2 行になる。片方に寄せる判断は、実際に CloudWatch へ出た形を
   見てから（デプロイ後に `diagnostic=CONNECT-P` で引ける）。
3. unwrap は「2 段まで」の固定上限。3 段以上を送るモデルが現れたら P06 の件数として観測できる。
4. 本 PR は plugin の挙動のみ。モデルが包む癖そのもの（プロンプト側）には触れていない
   → 後続 PR（`fix/skill-descriptions-and-start-diagnostics`）で description / SOUL を修正済み。
5. 2026-09-03 の層1 不発の**真因はまだ確定していない**。本 PR は「次に起きたら 1 行で判る」ところまで。
   確定には OC 再ビルド後に `TEAMAGENT_CALLER_IDENTITY_TRACE=1` を入れて再現させる必要がある。
6. trace ON にすると通常会話 1 通あたり 2〜3 行増える。切り分けが済んだら OFF に戻す運用が前提。
7. `arguments` という入力フィールドを持つ skill が将来増えると、unwrap 規則 (a) が誤発火して
   静かに P06 で落ちる。本 PR 時点では 45 skill すべてに存在せず、増えたら
   `test_no_skill_declares_an_input_field_named_arguments` が赤になる。
8. ~~14 スキルの description が「arguments に `_user_context` を含める」と書いている（記録のみ）。~~
   → **解消済み**（後続 PR）。実際は 15 スキル。文言を
   `teamagent.skills._shared.user_context.USER_CONTEXT_RULE`
   （「他の引数と同じ**トップレベル**に並べて渡す・入れ子のオブジェクトで包み直さない」）へ統一し、
   `tests/skills/test_user_context_description_contract.py` が全スキル横断で
   「`arguments に` が 1 本も無い」ことを固定する。SOUL.md の同趣旨の 1 行も同時に直した。
   なお block 最多の oauth_connect(105) / search(95) にはこの文言が無いので、
   **これが唯一の主因とは言えない**（プロンプト側の対策として実施）。

---

## 12.【追記 2026-09-04】ログ不達の真因・保証経路（層0）・残ブロックの根治

前節（§11）の弱点 5 に「2026-09-03 の層1 不発の真因はまだ確定していない」と書いた。
本節でそれを確定させ、あわせて **「誰が言っても『連携』が必ず届く」** ための経路を追加する。

一次検証はすべて上流 `openclaw@2026.7.1` の**実物**に対して行った。
`npm pack openclaw@2026.7.1` を展開したうえで、読むだけでなく**実際に gateway を起動して観測**している。
以下の `file:line` はその展開物（`<pkg>/dist/...`）に対応する。

### 12-1.【真因】プラグインのログは壊れていない。トレース env が捨てられていた

PR #383 では「plugin のログが CloudWatch に 1 行も出ない」を前提に、
`api.logger` に加えて `console` へも二重書きする対策を入れた。**この前提は誤診だった。**

実機検証（gateway を起動し、プラグインの register 内から 4 種類の出力を出して stdout/stderr を分離捕捉）:

| 出力 | 行き先 | 実測 |
|---|---|---|
| `console.warn` | stderr | 出る |
| `console.info` | stdout | 出る |
| `api.logger.warn` | stderr（`[plugins] …`） | 出る |
| `api.logger.info` | stdout（`[plugins] …`） | 出る |

つまり **gateway プロセスでプラグインのログは stdout/stderr に確かに届く＝CloudWatch に届く**。
根拠となる上流の経路:

- `console.*` は起動時に `enableConsoleCapture()` が差し替えるが、差し替え先は
  ファイルロガーへ転送した**うえで元の console へも書く**
  （`dist/console-DDSYsaep.js:111-186`、抑制されるのは `SUPPRESSED_CONSOLE_PREFIXES` の
  5 文言だけ＝`:83-94`。本プラグインの文言は該当しない）。
- subsystem logger は `writeConsoleLine` を通り、`loggingState.rawConsole ?? console` へ書く
  （`dist/subsystem-C3fiUGN1.js:157-165`）。console 既定レベルは `info`
  （`dist/console-DDSYsaep.js:13-16`）で、抑制されていない。
- `bundled-capability-runtime-DNfN9uhv.js:92-96` の **no-op logger は本プラグインの経路ではない**。
  あれは `createCapturedPluginRegistration`（capability の**発見**パス）専用で、
  実行時のフックはここを通らない。

では何が起きていたのか。**トレース env がプラグインに届いていなかった。**

`infra/docker/openclaw-entrypoint.mjs` は `process.env` を継承しない。
`buildChildEnvironment()` が allowlist だけで `childEnv` を組み立て、`run()` が
`process.execve(process.execPath, [...], childEnv)` でプロセスを置換する
（本 PR 前の `:156-186` と `:369-375`）。
`TEAMAGENT_CALLER_IDENTITY_TRACE` は `REQUIRED_SECRETS` にも `PASSTHROUGH_ENV` にも無かったため、
**ECS のタスク定義へ注入しても黙って捨てられていた**。
プラグイン側の `traceEnabled` は常に `false`、`emitTrace()` は全て no-op。
だから `layer1 entered` も `inbound recorded` も、TRACE=1 を入れたつもりでも 1 行も出なかった。

**修正**: `openclaw-entrypoint.mjs` に `DIAGNOSTIC_ENV`（非秘密の診断 env 専用の allowlist）を新設し、
`TEAMAGENT_CALLER_IDENTITY_TRACE` と `CONNECT_ADMIN_NAME` を通す。
`PASSTHROUGH_ENV`（資格情報・trust store・proxy）とは意図が違うので混ぜない。
契約は `test_entrypoint_is_readonly_secret_safe_and_environment_allowlisted` が固定する
（両 allowlist の完全一致・互いに素・秘密を含まないこと）。

> **教訓**: 「ログが出ない」を見たら、出力経路を疑う前に **その行が実行される条件**を疑う。
> env の allowlist は「設定したのに効かない」を完全に無音で作る。

### 12-2.【層1 の発火可否】上流の制約ではない。ただし「発火した」証拠も無かった

`before_agent_reply` の呼び出し側は通常応答経路にあり、条件は
`!useFastTestBootstrap && hookRunner.hasHooks("before_agent_reply")` だけで、
DM・socket mode・trigger による除外は無い（`dist/get-reply-CknL88Yv.js:5588-5624`）。
`handled` を返せばモデルは起動しない（`:5620-5623`）。

登録側にはもう 1 つ門がある。`before_agent_reply` は **conversation hook**
（`dist/command-registration-tKF3dsKu.js:170-180`）で、**非 bundled プラグインは
`plugins.entries.<id>.hooks.allowConversationAccess=true` が無いと登録が捨てられる**
（`dist/registry-D1_pYg_a.js:4224-4235`）。しかも捨てたことは `registry.diagnostics` に
積まれるだけで**ログには一切出ない**（`pushDiagnostic` は `dist/registry-D1_pYg_a.js:2503-2505`）。

本番相当の設定（`infra/openclaw/openclaw.config.json5:70-75`）でローカルに実機再現したところ、
`openclaw plugins inspect teamagent-caller-identity --runtime --json` は
**`hookCount: 8` / `diagnostics: []`**（8 フックすべて登録成功）を返した。
つまり **層1 が上流の制約で発火しない、という事実は確認できなかった。**

一方で「発火した」証拠も無い。層1 の脱出経路のうち `skipped(...)` はすべて `emitTrace` 依存で、
12-1 のとおりその TRACE が届いていなかったからである。
本番ログの `[agents/tool-policy] tool policy removed 26 tool(s)`（＝モデル起動）は、
**層1 が呼ばれなかった**場合とも、**層1 が呼ばれて `skipped` で静かに降りた**場合とも整合する。
両者を区別する情報は、当時のログには存在しなかった。

したがって本 PR は「(B) 層1 が発火しない」を**確定した不具合として修正しない**。
そもそも前提が実証されていない。代わりに次の 2 つを行う。

1. **判別可能にする**（12-3）。次に起きたら 1 行で判る。
2. **層1 の発火可否に依存しない保証経路を作る**（12-4）。これが本題。

> ⚠️ 明記: **(B) は「上流の制約で不可能」ではない。** 層1 は実機で登録・呼び出しの両方が可能である。
> 未確定なのは「本番の特定の会話でなぜ答えなかったか」であり、それは 12-3 の観測で次回に確定する。
> ただし後述のとおり、**確定を待たずに保証は成立させる**。

### 12-3. どのフックが本番で呼ばれるかを、ログだけで列挙できるようにする

TRACE と無関係に、必ず次の 2 種類を出す。

- `register` 時に 1 行: `registered hooks=[...] trace=on|off mcp_bearer=… slack_bot_token=…`
- 各フックが**初めて呼ばれたとき**に 1 行: `hook first_fired name=<hook>`

初回だけにするのは、通常会話 1 通ごとに数行増えるのを避けるため。知りたいのは回数ではなく可否である。
**バナー（登録を要求した）と first_fired（実際に呼ばれた）の差分が、そのまま
「登録はしたが本番では呼ばれないフック」の一覧**になる。
12-2 のとおり、上流は登録拒否を無音で行うので、これが唯一の一次証拠になる。
契約は `test_every_registered_hook_reports_its_first_invocation` が固定する。

バナーは **実際に `api.on` した名前**から組み、期待値 `REGISTERED_HOOKS` と食い違ったら
起動時に `fail()` する（2026-09-04 レビュー指摘 中2）。定数を直接出していると、
`observe()` を 1 つ消してもバナーは出続けテストも緑のままで、
「バナー vs first_fired の差分＝唯一の一次証拠」という**前提そのものが黙って壊れる**。
`test_registered_hooks_banner_is_derived_from_actual_registrations` と、
`observe()` を 1 つ削除する変異が赤くなることで固定する。

保証経路が語彙不一致で不発になる場合は既定では黙るが、受信の `content` が
**文字列ですらない**ときだけは TRACE と無関係に**フックごとに 1 回**警告する
（同 中3）。上流の `event.content` が別フィールドへ移ると、(A) と同型の
「設定したのに無音」が再発するため。毎回出すと `content` を持たない受信で騒音になるので初回だけ。

あわせて `emitPluginLog` の console 側を **常に stderr**（`console.warn`）に統一した。
このプラグインは `node -e` で読み込まれ、同じプロセスの stdout が
`JSON.stringify(...)` の**データ面**であることがある
（`tests/test_mcp_gateway_caller_claim.py` の 3 本のハーネス）。
バナーを `console.info`（= stdout）で出した瞬間にその 3 本が壊れたのが実測である。
**診断は stderr、データは stdout** ——この分離は破らない。
CloudWatch は stdout/stderr を同じロググループへ入れるので到達性は変わらず、
level の意味は logger 側（`logger.info` / `logger.warn`）で保つ。
契約は `test_plugin_diagnostics_never_touch_stdout` が固定する。

### 12-4.【保証経路 = 層0】`message_received` から Slack へ直接届ける

**ゴール**: 新規・既存・過去のテストユーザーを問わず、「連携して」と言ったら
**漏れなく**リンク（または次の一手が分かる診断つき案内）が届くこと。

#### なぜ `message_received` なのか（フック選定の一次比較）

| フック | 種別 | 非 bundled での登録 | 本番で呼ばれる実証 | 保証の土台に使えるか |
|---|---|---|---|---|
| `message_received` | 非 conversation | 設定に依存せず必ず登録 | あり（下記） | **使える** |
| `inbound_claim` | 非 conversation | 同上 | あり | 使える（同経路） |
| `before_agent_reply` | **conversation** | `allowConversationAccess` 必須・欠けると無音で消える | 無し（12-2） | 使えない |
| `before_model_resolve` / `before_agent_finalize` / `agent_end` | **conversation** | 同上 | — | 使えない |

`CONVERSATION_HOOK_NAMES` は `dist/command-registration-tKF3dsKu.js:170-178`、
門は `dist/registry-D1_pYg_a.js:4224-4235`。`message_received` はこの集合に**入っていない**ので、
`allowConversationAccess` の設定ミス・上流の仕様変更・config の取り違えのいずれでも落ちない。

「本番で呼ばれる実証」の根拠と、その**限界**: mcp が署名済み claim を受理してツールが
動いている以上、`before_tool_call`→`signToolCall`→`mintCallerClaim` は確実に実行されている。
`mintCallerClaim` は `ingressByRun` に載った ingress を要求し、その ingress を作るのは
`rememberInbound` だけである。

ただし `rememberInbound` は `message_received` **と** `inbound_claim` の両方に繋がっている。
したがってこの実績が示すのは「**どちらか**が動いている」ことだけで、
`message_received` **単独**の実証にはならない（2026-09-04 レビュー指摘 重大2）。
本番で `inbound_claim` だけが発火していた場合、片方にしか保証を載せていなければ
保証経路は一度も動かず、層1 の二の舞になる。

→ **保証経路は両方の受信フックに掛ける。** 一回性は下記の台帳が守るので二重投稿しない。
`test_connect_guarantee_holds_whichever_inbound_hook_fires` が
「message_received のみ / inbound_claim のみ / 両方」の 3 通りとも投稿 1 で固定する。

#### 実行モデル — 保証経路は hook の await から切り離す

上流実物で両フックの呼ばれ方を確認した（2026-09-04 レビュー指摘の必須確認）。

| フック | 実行 | タイムアウト | 実物 |
|---|---|---|---|
| `message_received` | 呼び出し側が fire-and-forget | **無し** | 呼び出し `dist/dispatch-V82RCNJs.js:1438`／`runVoidHook` は `dist/hook-runner-global-Cucx8m-W.js:458-477`／既定表 `:248-253` は `agent_end` `channel_pairing_requested` `before_compaction` `after_compaction` の 4 つだけ |
| `inbound_claim` | **claiming hook。ハンドラを逐次 `await`** | **無し** | `runClaimingHook` `:689-690` → `runClaimingHooksList`（`for` ループ内で `await`、first-claim wins）／`getClaimingHookTimeoutMs` が引く `DEFAULT_MODIFYING_HOOK_TIMEOUT_MS_BY_HOOK`（`:254-260`）に `inbound_claim` は無い |

`voidHookTimeoutMsByHook` / `modifyingHookTimeoutMsByHook` の既定表（`DEFAULT_VOID_HOOK_TIMEOUT_MS_BY_HOOK` / `DEFAULT_MODIFYING_HOOK_TIMEOUT_MS_BY_HOOK`）を上書きする呼び出し元は
上流 dist に存在しない（唯一の `createHookRunner` 呼び出しは `:1112-1124` で
`logger` / `catchErrors` / `failurePolicyByHook` しか渡さない）。
よって **どちらのフックにもタイムアウトは無い**＝保証経路が途中で切られることはない。

しかし `inbound_claim` は**逐次 await される**ので、そこで保証経路
（最大 MCP 15s + Slack 10s×2）を待つと **受信パイプライン全体をその間止めてしまう**。
そこで `startConnectGuarantee` で **両方とも hook の await から切り離す**（fire-and-forget）。
`message_received` 側は待っても実害が無いが、上流が将来タイムアウトを足したら
配信が切られるため、形を揃えておくほうが安全である。

受信の記録（`rememberInbound`）と一回性の確保は
`deliverConnectGuarantee` の**最初の `await` より前**に同期で終わるので、切り離しても取りこぼさない。

#### 配信手段の選定（なぜ Slack Web API を直接叩くのか）

上流にも送信面はある（`api.runtime.channel` の `outbound.loadAdapter` / `reply.dispatch*` /
`inbound.dispatchReply`、型は `dist/types-DaHgOqFX.d.ts:8228-8352`）。しかしこれらは
gateway の request context と account 解決に依存し、フックから単独で正しく駆動する契約が
公開されていない（多くが `@deprecated` か、channel plugin 内部からの利用を前提にしている）。

対して `SLACK_BOT_TOKEN` は `REQUIRED_SECRETS` として**確実に子プロセスへ渡っている**
（`openclaw-entrypoint.mjs:15-21` と `buildChildEnvironment`）。
本プラグインは既に mcp へ生 `fetch` している（層1 の `callMcpTool`）ので、同じ流儀で済む。
**「確実に届く」ことを最優先し、依存の少ない方を選ぶ。**

使う API は 2 つだけ:

- `conversations.open` — DM の正準 `D…` を得る。mcp の caller claim は
  `^[CDG][A-Z0-9]{8,}$` を要求する（`src/teamagent/mcp_gateway/caller_claim.py:39,385`）が、
  受信側は DM を内部別名 `DM:U…` にしか解決できないため。チャンネルは既に正準なので呼ばない。
- `chat.postMessage` — 本文の投稿。スレッド受信ならそのスレッドへ返す。

Slack は失敗を HTTP 200 + `{"ok": false, "error": …}` で返すので必ず `ok` を見る。

#### 一回性と二重投稿

一回性は **`connectAnsweredByMessage`（`pendingKey` = `[sessionKey, messageId]` 基準の台帳）**
が持つ。層1 と保証経路が同じ台帳を見るので、どちらかが答えたらもう一方は降りる。

> **⚠️ 当初の実装は壊れていた**（2026-09-04 レビュー指摘 重大1）。
> 旗を ingress **オブジェクトのフィールド**（`connectDeterministicAttempted`）に載せていたが、
> `bindRun` 成功時に `removePending` で pending から ingress が消えるため、
> 次の通知は新しいオブジェクトを作り、**旗が毎回リセットされていた**。
> 実測（実物 dist を駆動）: `message_received` ×2（runId 付き）で**投稿 2・tools/call 2**、
> `message_received` → `before_model_resolve` → `message_received` でも**投稿 2**。
> tools/call が 2 回走るということは **state token が 2 個発行される**ということで、
> 単なる「うるさい」では済まない。
> 224 組マトリクスがこれを捕らえなかったのは、各行が受信を 1 回しか通知しない
> 新規プラグインで測っていたため。以後**マトリクスの全行を 2 回通知で測る**。
> 旗は受信の同一性（`pendingKey`）に紐づけ、**オブジェクトの寿命から切り離す**のが正解。

`message_received` は `before_agent_reply` より先に走るので、通常は保証経路が台帳を取り、
層1 は `already_attempted` で降りる。

**台帳は投稿の前に押さえるが、投稿に失敗したら必ず解放する**（2026-09-04 レビュー指摘）。
前に押さえるのは、同時に走る再通知で二重投稿しないため。しかし失敗のまま抜けると
層1 まで `already_attempted` で降り、**利用者に何も届かない**。
実測: `slackMode=post_fails` で posts 0 / 層1 stand down / fallthrough 0 ＝ 完全な無音だった。
層1 はハーネスの reply 経路で返す＝bot token も Slack Web API も使わない
**別の故障ドメイン**なので、ここで降りるのは救済機会の放棄になる。
解放しても二重投稿にはならない（投稿は 0 通で終わっている）。
`test_slack_delivery_failure_hands_the_inbound_back_to_layer1` が
「投稿失敗 → 層1 が同じ受信に答える」を、
`test_connect_guarantee_posts_once_and_layer1_stands_down` が
「投稿成功 → 層1 は降り、tools/call は 1 回のまま」（過剰解放していないこと）を固定する。

**旗は「実際に配信を試みる」と決めた後にだけ立てる。** 手前で立てると、保証経路が使えない環境
（bot token 無し＝ローカル/テスト）で層1 まで降りてしまい **誰も答えない穴**ができる。
これは実装中に実際に踏んだ（mutation `M8` がこの退行を固定する）。

モデル経路そのものは止めない。裁定どおり **「抑制のために保証を犠牲にしない」**。
同じ受信に対して保証経路が 2 通投稿しないことだけを守る。

⚠️ ただし正確に言うと、モデル経路は止まらないので **`oauth_connect` がもう一度呼ばれうる**。
これは「返事が 2 通に見える」だけでなく **state token がもう 1 個発行される**ということである
（token 自体は本人専用・使い捨てなので危険ではないが、無害でもない）。
層1 は台帳で降りるが、モデルが自分でツールを呼ぶ経路までは塞いでいない。
塞ぐなら層2/3 を「保証済みの run では畳む」改修が要る（本 PR の範囲外・§12-8 の 2）。

なお、同じ受信が 2 度通知される経路（`inbound_claim` と `message_received` の両方）があるため、
`rememberInbound` は `sameIngress` で同一と確認できた再通知について**旗を引き継ぐ**。
引き継がないと旗が毎回リセットされ、同じ受信に何通も投稿する（mutation `M7`）。

### 12-5.【(E)】利用者の状態差は mcp の単一正本に委ねる

mcp は**失敗も成功と同じ `TextContent` の JSON** で返す
（`src/teamagent/mcp_gateway/server.py:442-445`、例外は `:797,819` で `{"error": …}` に畳まれる）。
`isError` も JSON-RPC error も使わない。しかもその `error` 文面は**既に利用者向けに整形済み**で、
「何をすればよいか」＋転送用の `診断: CONNECT-Ixx <時刻> <識別子>` 行まで含む
（`src/teamagent/connect_diagnostics.py:260-277`）。

代表例が新規ユーザーの **CONNECT-I02**（Slack プロフィールに会社メールが無い／会社ドメイン外）:
`src/teamagent/skills/oauth_connect/skill.py:228-236` が
「Slack プロフィールのメールアドレスが会社メールになっているか確認し、管理者へご連絡ください。」
を含む文面を返す。

旧実装（`extractConnectMessage`）はこれを一律 `mcp_tool_error` に潰して捨てていた。
その結果、新規ユーザーには**無言**か、ブロックされた toolResult を見たモデルの自作回答
（「技術的な問題」「管理者へお問い合わせ」）しか届かなかった。
**これが「誰でも連携できる」を破っていた中心的な穴である。**

`extractConnectOutcome` に置き換え、`{kind:"user_error"|"message", text}` を返す。
保証経路も層1 も、この `text` を**一字も変えずに**利用者へ届ける。
既に連携済み／片方だけ連携済みは元々成功側の `message` に入って返る
（`skill.py:460-492`）ので、追加の分岐は要らない＝**状態差の判断は mcp 側の単一正本に集約**される。

mcp へ到達すらできなかった場合（fetch 失敗・5xx・壊れた戻り値）だけ、プラグイン側の最終文面
`CONNECT-Z02` を出す。「もう一度『連携』と送ってください」＋管理者へ転送する 1 行で、
**無言終了を作らない**。

### 12-6.【(C)】残ブロック 11 件の根治

#### C1: `trusted Slack run identity is missing or stale`（9 件）

`bindAgentRun` が `candidates !== 1` で run を拒否し、`rejectedRuns` に 10 分間登録していた。
以後その run の**すべてのツール**が block される。連続してメッセージを送る、あるいは
並行 run が走るだけで候補は 2 件以上になるので、正常な使い方で再現する。

**修正**: 同じ会話の候補が複数あるときは**最新の受信**を選ぶ。

安全側は崩れない。`matchesConversation` は `sessionKey` / `senderId` / channel（DM 別名込み）/ TTL を
**すべて**満たしたものだけを残す。つまり候補は全員「同じ人の同じ会話の受信」であり、
曖昧なのは「どのメッセージか」だけで「**誰か**」ではない。他人・別会話は候補に入る前に落ちている。
`test_run_binding_prefers_the_newest_inbound_in_the_same_conversation` の③が、
**より新しい別送信者の受信があっても掴まない**ことを固定する。

> 当初この③は守れていなかった（2026-09-04 レビュー指摘 中1）。別送信者を
> チャンネル（`CHANNEL_SESSION_KEY` / `channel:C…`）で作る一方、run ctx は DM
> （`DM_SESSION_KEY` / `user:U…`）で、**sessionKey も channel も違っていた**ため、
> `senderId` 照合が無くても落ちていた。実測: `matchesConversation` から
> `ingress.senderId === senderId` を消す変異でこのテストは**緑のまま**だった
> （赤くなったのは層1 のテスト 1 本だけ）。実装自体は安全だったが、テストが守っていなかった。
> **同一 channel・同一 sessionKey・別 senderId・別送信者のほうが新しい** に直し、
> 同じ変異で赤くなることを確認した。
曖昧化したときは `bind_agent_run disambiguated candidates=N rule=newest_in_conversation` を出す（黙って選ばない）。

#### C2: `declared channel_id does not match the bound ingress`（2 件）

`validateDeclaredContext` がモデルの申告違いで block していた。
しかしこの拒否は**セキュリティ上 1 ビットも稼いでいない**: `mintCallerClaim` は
`_user_context` を authoritative 値で丸ごと置き換えてから署名するため、申告値は元々 100% 捨てられる。
拒否は「捨てる前に落とす」だけの純粋な失敗モードだった。

**修正**: 会話面 3 フィールド（`slack_team_id` / `channel_id` / `thread_ts`）は
**破棄して続行**し、`discarded declared user_context fields fields=[…]` を 1 行出す。
`caller_claim`（持ち込み署名＝replay）と `slack_user_id`（唯一の明示的ななりすまし申告）は
引き続き block する＝fail-closed は維持。

### 12-7. G7 の維持

追加した行が出すのはフック名・理由コード・件数・`id_shape` だけ。
Slack 識別子・本文・URL・claim・bearer は載せない。
利用者へ**届く**文面（mcp の `error` や `CONNECT-Z02`）には従来どおり本人の識別子が入るが、
それは利用者本人と管理者の突合用であり、ログには出さない。
`test_deterministic_path_logs_keep_the_g7_discipline` / `test_block_logs_carry_id_shape_but_no_identifiers`
が引き続き固定する。

### 12-8. セルフFB（反対尋問）— 残る弱点

1. **層1 不発の真因は依然として未確定**（12-2）。本 PR は判別可能にしただけ。
   ただし保証経路（層0）が入ったので、真因が何であれ利用者への到達は守られる。
2. **モデルの自発的な `oauth_connect` 呼び出しは止まらない**（§12-10）。
   配信成功時はモデルの最終応答を `cancel` し、層2 の revise も掛けないので
   **二重返信は解消**し token の重複も大幅に減るが、モデルが自分の判断で
   `oauth_connect` を呼べば token はもう 1 個出る。完全に止めるにはツール門で
   拒否する必要があり、正当な再依頼まで塞ぐため採っていない。
3. **Slack Web API が落ち続けても、層1 という別経路が残る**。429 / 5xx / 一時的な
   ネットワーク失敗は最大 2 回まで再試行し（`Retry-After` は 5 秒で刈る）、
   `thread_ts` 付きが弾かれたらスレッド無しで 1 回投げ直す。
   それでも投稿できなければ台帳を解放し、層1（ハーネスの reply 経路＝bot token も
   Slack Web API も使わない別の故障ドメイン）が同じ受信に答える。
   両方が同時に落ちている場合だけ届かず、その場合も `outcome=post_failed` が残る。
4. **`conversations.open` の追加コール**が DM の初回「連携」ごとに 1 回入る
   （送信者単位でキャッシュ。キャッシュは `MAX_TRACKED_CONTEXTS` で上限を持つ）。
   Slack のレート制限に当たるほどの頻度ではないが、監視対象ではある。
5. **保証経路は「短い連携依頼」語彙に依存する**。fixture（`tests/fixtures/connect_request_phrases.json`）
   の外の言い回しは拾えない。誤爆を避けるため厳格側に倒しており、
   `must_not_match`（「連携解除」「連携できない」等）で誤爆しないことを固定している。
6. **本番実機での確認が未了**。本 PR はローカル実機（上流 gateway 起動）と probe までで、
   OC 再ビルド後の run-task 検証と Slack 実機確認までは「完了」ではない。
7. `CONNECT_ADMIN_NAME` は今回初めて実際に届くようになった（従来は allowlist で捨てられ、
   常に既定値 `小俣` にフォールバックしていた）。未設定なら従来と同じ挙動。

### 12-9.【本番実測 2026-09-04・OC TD:45】(A) は効いた／Slack の送信通知で「連携」が落ちる

OC 便3（TD:45・dev `a89f93c`）着地後、**(A) の修正が効いていることが本番で確認された**。
14 日間ゼロだったプラグインのログが CloudWatch に出るようになり、
`registered hooks=[…] trace=on mcp_bearer=yes slack_bot_token=yes` と
**8 フックすべての `first_fired`** が観測できた。
12-2 で未確定だった「層1 は登録されているのか」も、これで**登録・発火の両方が確認**された。

同時に**新しい不具合**が実測で出た。利用者が Slack 連携機能経由で「連携」（2 文字）と
送ったのに、プラグインには次の形で届いていた:

```
inbound recorded connect_request=false normalized_len=16 content_len=16 …
connect deterministic path … outcome=skipped reason=not_connect_request normalized_len=16 content_len=16
```

`CONNECT_REQUEST_MAX_LENGTH`（12）を超えたため `not_connect_request` で落ちている。

**`normalized_len == content_len == 16`（正規化で 1 文字も減っていない）**という事実は重要で、
その 16 文字には Slack マークアップ（`<@U…>` 等）も絵文字も端の約物も**無い**ことを意味する
（いずれも `normalizeConnectRequest` が落とすため）。つまり**素のテキスト**が混ざっている。

#### まず「形」を出せるようにした（レビュー指摘 1）

本文はログに出せない（G7）。そこで `connectRequestShape()` を足し、
**本文を 1 文字も出さずに内訳が判る指標**を `inbound recorded` と
`not_connect_request` の両方に載せた:

```
connect_shape=lines:2,content_lines:1,whole:no,stripped:yes,leading_line:yes,
              word:yes,head_word:yes,boiler:[decorated_notice],stripped_len:2
```

- `lines` / `content_lines` … 何行で届いたか／注記を落として中身が何行残るか
- `whole` / `stripped` / `leading_line` … どの規則なら通る（通らない）のか
- `word` / `head_word` … 連携語を含むか／先頭にあるか
- `boiler` … 落とせた注記の種類（語彙は固定・本文は出さない）

これで**次の実機テストのログ 1 行で 16 文字の内訳が確定する**。

> ✅ **決着（2026-09-04 追試）**: この `content_len=16` は
> **レビュアーのテスト経路（Slack 連携機能経由・定型の付加文が混ざる）に固有**と確定した。
> 実利用者が Slack クライアントから直接打った場合は `content_len=2 / normalized_len=2` で
> 正しく `connect_request=true` になっている（実測ログ）。
> したがって本節の付加文対応は「実利用者の不具合の修正」ではなく、
> **①テスト経路でも実機検証が通るようにするため ②未知の定型に対する頑健性**
> という位置づけである。優先度は下げてよいが、fixture と判定は入れたまま維持する。

#### 判定を文字数上限だけに頼らない形へ（レビュー指摘 2）

次のいずれかを満たせば連携依頼とみなす。誤爆側は従来どおり通さない。

| 規則 | 内容 | 何を救うか |
|---|---|---|
| (a) | 正規化後の**本文全体**が連携語＋助詞・敬語末尾だけ（従来・維持） | 素の「連携」 |
| (b) | **送信通知を除去**したあとの全体が (a) を満たす | `_…を使用して送信されました_` 等の既知の定型 |
| (c) | **最初の中身のある行**が (a) を満たし、**後続行に連携語が無い** | **語彙に無い未知の定型** |

(b) の除去は保守的に行う: **送信通知の語彙を含み、かつ連携語を含まない**部分だけを落とす。
連携語を含む行・装飾は絶対に落とさない（本文を消してしまわないため）。

(c) を「行のどれかが一致」にしないのが要点である。実際の並びは
「利用者の本文 → クライアントの定型」なので、**先頭行だけ**を救えば十分で、
それ以上緩めると誤爆する。実際 `test_slack_boilerplate_does_not_open_a_false_positive` が
「今日の予定を教えて\n連携」「〇〇社との連携について提案書を\n連携」「連携\n連携」を
不通過で固定し、変異 `M21`（(c) を「どれかが一致」へ緩める）が赤になる。

> **設計上の自己反省**: (c) は当初「除去後に中身のある行が 1 本だけ」という規則で書いたが、
> 実測したところ**どの fixture でも (b) だけで通ってしまい、(c) は一度も効いていなかった**
> ＝ テストに守られない死んだ分岐だった。語彙に依存しない受け皿という当初の狙いも
> 果たせていない（未知の定型行は正規化しても空にならないため、常に 2 行と数えられて落ちる）。
> そこで (c) を「先頭行 + 後続に連携語なし」へ作り直し、**語彙に無い定型**
> （fixture の `連携\n-- Acme Slack Bridge --`）でのみ通ることを確認したうえで、
> 変異 `M20`（(c) を外す）が赤になることまで固定した。

#### fixture

`tests/fixtures/connect_request_phrases.json` に 2 カテゴリを新設した。
`must_match_with_slack_boilerplate`（8 件・装飾つき／装飾なし／英語／空行つき／
サービス名つき／敬語末尾つき／**未知の定型**）と
`must_not_match_with_slack_boilerplate`（6 件）である。
保証マトリクスは 7 状態 × （素の 32 表現 + 付加文つき 8 表現）= **273 組**へ拡張し、
全組み合わせで 1 通届くことを再固定した。

### 12-10.【本番実測 2026-09-04・OC TD:45】二重返信の抑止

#### 実測: 同じ内容が 2 通届いていた

実利用者が Slack クライアントから「連携」と打った際の実測ログ:

```
05:54:24 inbound recorded connect_request=true normalized_len=2 content_len=2 …
05:54:24 layer1 entered provider=slack trigger=user
05:54:24 connect deterministic path outcome=skipped reason=already_attempted   ← 層1 は保証側に譲って降りた（設計どおり）
05:54:25 connect guarantee outcome=delivered result=message                    ← 保証経路が配信
05:54:26 connect zero-tool revise tool_calls=0 reason=short_connect_request outcome=revised revise_attempt=1  ← 層2 が revise
```

**保証経路の 1 通＋モデル経路の 1 通＝合計 2 通**が利用者に届いていた。
保証（無言をゼロにする）は達成できているが、体験としては壊れている。

#### 上流実物の確認: 最終応答は「落とせる」

`reply_payload_sending` の結果型は `{ payload?, cancel?: boolean, reason? }`
（`dist/hook-types-DQ9eTy2x.d.ts:698-702`）。ランタイムは:

1. `dist/deliver-DGDN_7sT.js:36` — `if (result?.cancel) return null;`
2. `:852-856` — `null` なら `{ cancelled: true }`
3. `:1329-1334` — `cancelled` なら
   `suppressedPayloadOutcome({reason:"cancelled_by_reply_payload_sending_hook"})` して `continue`

＝**その payload の配信を丸ごと飛ばす**。空文字への置換や短い定型への差し替えに頼る必要はない。
本プラグインは既に層3 で同じ `cancel` を使っている（分割 payload の 2 通目の取り消し）。

#### 実装

配信成功した受信だけを記録する台帳 `connectDeliveredByMessage`（`pendingKey` 基準）を新設し、
`reply_payload_sending` でその run に束縛された ingress が載っていれば `{cancel:true}` を返す。

**⚠️ 抑止の根拠は `connectDeliveredByMessage`（配信成功）だけに置く。**
一回性の台帳 `connectAnsweredByMessage` は投稿失敗時に解放される（§12-4）ので、
それを根拠にすると「届いていないのにモデルも黙る」＝**完全な無音**を作りうる。
2 つの台帳を分けているのはこのためである:

| 台帳 | 意味 | 立つ時点 | 解放 |
|---|---|---|---|
| `connectAnsweredByMessage` | 一回性（同じ受信に 2 回投稿しない） | 配信を試みると決めた時点 | 投稿失敗時 |
| `connectDeliveredByMessage` | 抑止（既に届いたので重複を落としてよい） | **投稿成功後** | TTL のみ |

この違いが意味を持つのは**配信中（成否未確定）にモデルの応答が来る競合**だけである。
配信失敗のシナリオでは一回性の台帳も解放済みで両者は一致してしまうため、
`test_suppression_never_wins_over_an_unfinished_delivery` がその競合を直接固定し、
変異 `M23`（根拠を answered へ取り違える）はこのテストでのみ赤くなる。

#### state token の重複について

あわせて層2 の revise も、保証経路が配信済みなら掛けない。
revise は「`oauth_connect` を必ず呼べ」とモデルへ要求するもので、
実測ログのとおりこれが **state token をもう 1 個発行させる**直接の原因だった。

> ただし **token の重複が完全に無くなるわけではない**。抑止が止めるのは
> ①モデルの最終応答の配信 ②こちらから再要求すること の 2 つであって、
> モデルが自発的に `oauth_connect` を呼ぶこと自体は止めていない。
> 呼ばれれば token はもう 1 個出る（本人専用・使い捨てなので危険ではないが、無害でもない）。
> 完全に止めるにはツール門（`signToolCall`）で配信済み run の `oauth_connect` を
> 拒否する必要があり、それは正当な再依頼まで塞ぐ副作用があるため本 PR では採らない。

#### 保証との関係（絶対条件）

抑止は**保証経路が配信に成功したと確認できた場合だけ**効く。
配信失敗（`post_failed`）・未発火（連携依頼でない）・配信中のいずれでも、
モデル経路は従来どおり素通しである。§12-4 の「投稿失敗時に台帳を解放する」規律とも矛盾しない
（解放されるのは `answered` だけで、`delivered` はそもそも立っていない）。
