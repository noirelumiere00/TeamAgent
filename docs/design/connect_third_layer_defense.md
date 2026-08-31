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

