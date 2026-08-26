# 資料検索 高速化 3点セット — 統合手順（v2e / 二段返し / レイテンシ計測）

対象ブランチ: `feat/search-speed-0817`（base `origin/dev`）
作成: 2026-08-17

このファイルは **共有ファイルを編集せずに済ませるための引き継ぎ書**である。
`infra/openclaw/SOUL.md` / `openclaw.config.json5` / `effective-tool-scope.json` /
`tests/.../test_openclaw_runtime_contract.py` は本ブランチでは一切触っていない。
それらに入れるべき文面・手順をここに書き置く。

---

## 0. 何を入れたか（3点）

| # | 変更 | 既定 | 効き先 |
|---|------|------|--------|
| ① | `prompts/search/v2e/system.md`（350字・3セクション） | **未使用**（コード既定は v2d のまま） | Bedrock 出力トークン＝search の 68〜76% |
| ② | 二段返し `USE_SEARCH_TWO_STAGE` | **OFF** | ツール応答の待ち時間（第一報を約2.6秒に） |
| ③ | レイテンシ計測（`mcp_tool_usage.gateway_ms/total_ms`・`search_latency_breakdown`） | 常時ON（ログのみ） | 「Slack 19秒 vs mcp 8.3秒」の差の可視化 |

①と②は**排他ではない**が、同時に ON にすると効果の切り分けができない。
**先に③を載せて実測 → ① → 効果測定 → ②** の順を推奨する。

---

## 1. 順序契約（最重要・破ると検索が全断する）

**イメージ配備が先、env 切替が後。逆順は必ず事故る。**

- `prompts/loader.py:24` は `path.read_text()` で、存在しないバージョンに対する
  **フォールバックが無い**（`skill.py` の `_summarize` にも try/except が無い）。
- したがって **v2e を含まないイメージのまま `PROMPT_VERSION=v2e` を入れると**、
  `include_answer=True` の検索が毎回 `FileNotFoundError` で落ちる＝Slack 検索が全断する。
- 同じ理由で **「env を v2e のまま、イメージだけロールバック」も全断**する。
  ロールバックは必ず **env を先に v2d へ戻してから** イメージを戻す。
- この非対称を避けるため、**コード既定の `PROMPT_VERSION` は v2d のまま**にしてある
  （`orchestrator/factory.py:79`）。切替は env 1 本で行い、CI 側は
  `tests/skills/test_search_skill.py::test_prompt_version_default_stays_v2d` が既定 v2d を固定する。

```
 [OK]  新イメージ(v2e入り)を配備 → 動作確認 → env PROMPT_VERSION=v2e → 検証 → （戻す時）env を v2d → イメージ戻し
 [NG]  env を先に入れる / env を残したままイメージだけ戻す  → どちらも検索全断
```

---

## 2. env 切替手順（mcp のみ・実コマンド）

`teamagent-dev-mcp` の taskdef は CLI 直登録運用（terraform に env が無い）。
`docs/v3.2/bundled_deploy_2026-06-16.md` の手順を踏襲する。

```bash
# ① 現行 taskdef を land に取得
aws ecs describe-task-definition --task-definition teamagent-dev-mcp --region ap-northeast-1 \
  --query 'taskDefinition' --output json > /tmp/mcp-td.json

# ② image は据え置き（v2e 入りイメージが既に載っていること！）、env だけ足す
jq '{family,taskRoleArn,executionRoleArn,networkMode,containerDefinitions,volumes,
     placementConstraints,requiresCompatibilities,cpu,memory,runtimePlatform,ephemeralStorage}
    | with_entries(select(.value != null))
    | .containerDefinitions[0].environment |= (
        (map(select(.name!="PROMPT_VERSION"))) + [{"name":"PROMPT_VERSION","value":"v2e"}]
      )' /tmp/mcp-td.json > /tmp/mcp-td-v2e.json

# ③ v2e がイメージに入っていることを先に確認（入っていなければここで止める）
aws ecs execute-command ... # もしくは run-task で `python -c "from teamagent.prompts.loader import load_prompt; load_prompt('search','v2e','system')"`

aws ecs register-task-definition --cli-input-json file:///tmp/mcp-td-v2e.json --region ap-northeast-1 \
  --query 'taskDefinition.{family:family,revision:revision}'

aws ecs update-service --cluster teamagent-dev --service teamagent-dev-mcp \
  --task-definition teamagent-dev-mcp:<NEW_REV> --region ap-northeast-1 \
  --query 'service.deployments[].{status:status,td:taskDefinition,rollout:rolloutState}'
```

二段返しを ON にする時は同じ手順で `USE_SEARCH_TWO_STAGE=1` を足す（v2e とは別便で入れる）。

### 切替後に必ず見る（1リクエストで足りる）

```
fields @timestamp, output_tokens, stop_reason, latency_ms
| filter event="bedrock_converse"
| sort @timestamp desc
```
- `stop_reason=max_tokens` が出たら **途中切れ**＝即ロールバック（v2e が長文を書こうとしている）。
- `output_tokens` 中央値が 498 → 300 前後に落ちていれば狙いどおり。

---

## 3. connect-web(/app) との PROMPT_VERSION 整合（要注意）

`PROMPT_VERSION` は `resolve_search_skill_config()`（**唯一の真実源**）経由で
**3面** に効く。mcp の taskdef にだけ入れること。

| 面 | 経路 | v2e を入れると |
|----|------|----------------|
| Slack（mcp） | `mcp_gateway` → SearchSkill | 狙いどおり短くなる |
| /app（connect-web） | `connect_web/app.py` が `_build_search_skill()` を直呼び | **同じ env を connect-web の taskdef にも入れると**回答が短くなる。入れなければ v2d のまま |
| `knowledge_deliver` | 内部で SearchSkill を再利用（本番 `USE_KNOWLEDGE_DELIVER=true`） | 添付の `initial_comment` が短くなる |

- `infra/terraform/connect_web.tf` に `PROMPT_VERSION` は無い＝ connect-web は既定 v2d のまま。
  **Slack と /app で回答仕様が食い違う**状態になるので、A/B が終わったら
  どちらに揃えるかを決めて明示すること（食い違ったまま放置しない）。
- 逆に「/app だけ長い回答が欲しい」なら現状のまま（mcp のみ v2e）で目的は達する。

---

## 4. 二段返し（USE_SEARCH_TWO_STAGE）の仕組みと SOUL 文面案

### 4.1 仕組み

1. `mcp_gateway.server.dispatch_tool` は **search tool の呼び出しにだけ**
   `ctx.metadata["search_two_stage_allowed"]=True` を立てる
   （`knowledge_deliver` の内部 search・connect-web 直呼び・`slack_bot` 直呼び・
   L2 `run_agent`（`sdk_runner` が skill を直呼び）には付かない
   ＝二重投稿と /app 破壊を構造的に防ぐ）。
2. SearchSkill は **env ON かつ 印あり かつ ヒット≥1 かつ 宛先が決まる** ときだけ、
   要約を待たずに「ヒット一覧＋定型文」を返す（`total_cost_usd=0`）。
3. 回答文は daemon thread で生成し、**発信元スレッド**（`channel_id`＋`thread_ts`）へ
   bot token で後追い投稿する。channel が無ければ依頼者本人の DM。
   どちらも無ければ **後追いしない**（従来どおり同期要約に落ちる）。
4. 後追いの失敗は fail-open（第一報は既に届いている）。`search_followup_failed` を warning。

第一報に載る定型文（`skills/search/two_stage.py`）:

```
🔎 該当資料を先にお出しします。詳細な考察を続けてこのスレッドに投稿します。
```

### 4.2 SOUL.md へ入れる文面案（このブランチでは編集していない）

`## ナレッジ検索（過去資料・提案事例）への誘導` セクションの末尾に追記する想定:

```markdown
**二段返しが有効なときの規約（厳守）**: `search` の `answer` が
「詳細な考察を続けて…投稿します」という**続報予告**だった場合、
その回は**ヒットの一覧と `answer` の文面だけ**を返す。
- 自分で考察・要約・パターン抽出を**書き足さない**（後から本体が同じスレッドに届くため、
  書くと同じ内容が二重に出る）。
- ヒットは案件(project)/業界(industry)/種別(doc_type) と `url` を添えて簡潔に並べる
  （`url` が無いヒットに URL を作らないのは従来どおり）。
- 「少々お待ちください」等の言い換えをせず、`answer` の文面をそのまま含める。
- 続報が届かなくても再実行しない（ユーザーから催促があった時だけ再検索する）。
```

補足: `_user_context` の `channel_id` / `thread_ts` は署名 claim と一致必須
（`caller_claim.py:440-442`）なので、**成功している本番呼び出しは必ず channel_id を持つ**。
後追い投稿の宛先が取れないケースは実質 LEGACY/テスト経路だけ。

### 4.3 二段返しの検証手順（ON にした直後）

1. Aico 宛 DM で「〇〇の提案資料あったっけ？」→ **第一報が数秒で来る**こと。
2. 続けて同じスレッド（DM ならそのまま）に**考察本文が後追いで届く**こと。
3. `search_two_stage_deferred` → `search_followup_done posted=True` がログに並ぶこと。
4. OpenClaw が第一報で自前の考察を書いていない（＝ SOUL 反映済み）こと。
   ここが未反映だと「OC の要約」と「後追い本文」が二重に出る＝ SOUL 反映まで ON にしない。

---

## 5. 新しく出るログ（Insights クエリ）

```
# ゲート内訳（受信→skill開始→skill完了）
fields @timestamp, tool, gateway_ms, latency_ms, total_ms
| filter event="mcp_tool_usage" and tool="search"
| stats avg(gateway_ms), pct(latency_ms,50), pct(total_ms,50) by bin(1d)

# search 内部の区間別（embed / 検索 / rerank / URL解決 / 要約）
fields @timestamp, embed_ms, retrieve_ms, rerank_ms, resolve_urls_ms, converse_ms, total_ms, hit_count, deferred
| filter event="search_latency_breakdown"
| stats pct(retrieve_ms,50), pct(rerank_ms,50), pct(converse_ms,50), pct(total_ms,50)

# 二段返しの後追い（要約時間・投稿成否・コスト）
fields @timestamp, converse_ms, total_ms, posted, cost_usd
| filter event="search_followup_done" or event="search_followup_failed"
```

- `latency_ms`（skill 実行窓）の**定義は変えていない**＝既存の台帳・SLI と連続。
- `retrieve_ms` は `rerank_ms` を内包する（差が pgvector 側の実測時間）。
- クエリ原文・チャンク本文は一切載せていない（G8）。CI で固定済み
  （`tests/skills/search/test_latency_breakdown.py::test_breakdown_never_logs_query_or_content`）。
- 既存ダッシュボード `cloudwatch_fargate.tf:272` が参照していた `search_skill_done.latency_ms`
  は今回初めて実際に出るようになった（今まで空カラムだった）。

---

## 6. ロールバック

| 対象 | 手順 |
|------|------|
| v2e | `PROMPT_VERSION` を **v2d に戻す**（env のみ・イメージは触らない）→ update-service |
| 二段返し | `USE_SEARCH_TWO_STAGE` を **削除 or 0** → update-service。コード側は既定 OFF で従来挙動とバイト等価 |
| 計測 | ロールバック不要（ログのキー追加のみ・メトリックフィルタに引っかからない） |

---

## 7. 残リスク（引き継ぐ側が知っておくこと）

1. **v2e の削減幅は未実測**。現状は「550字指示 → 出力 p50 498tok」＝約0.91 tok/字なので、
   350字指示が同じ遵守率で効けば **約 315tok（-183tok）**。実測 slope 6.8〜10.5 ms/出力tok を
   当てると `bedrock_converse` 5.67s → **3.8〜4.5s（-1.2〜1.9秒）**、上振れで -2.1 秒。
   指示遵守率は字数比ほど素直に落ちないのが通例（550指示で400字なら削減は半減）＝下振れ余地あり。
   ON 後に `output_tokens` の中央値で必ず答え合わせすること。
2. **v2e は ⚠️避けたい論点 を持たない**。営業が評価している 💡刺さったパターン は残したが、
   ネガ論点の打ち返しは 💡 の中に 1 行畳みになる＝情報量は確実に減る。**製品判断**。
3. **二段返し時の会計が 0 になる**。`total_cost_usd=0` → `mcp_tool_usage.tool_cost_usd=0`・
   `usage_events` も 0。実費は `bedrock_converse` と `search_followup_done.cost_usd` に出る。
   コスト台帳を tool 単位で見ている場合は search が過小計上になる。
4. **thread_ts が None の回はチャンネル直下に後追いが出る**。署名 claim の `thread_ts` は
   「スレッド内発言なら親 ts」で、チャンネル直下の発言では None。OpenClaw はそれをスレッド返信に
   するので、第一報（スレッド内）と後追い（チャンネル直下）が別の場所に出ることがある。
   claim の `message_id` を ts として使えば揃うが、`message_id` は ts 形式の検証が無い
   opaque 文字列なので今回は採らなかった（改善余地）。
5. **後追いは bot 直投稿**なので OpenClaw の markdown→mrkdwn 変換を通らない。
   `[label](url)` は `to_slack_mrkdwn()` で変換済みだが、その他の記法（見出し等）は素通し。
   また `SEARCH_ANSWER_SOURCE_LINKS=1` の 📎資料リンクは **後追い側の投稿に付く**
   （第一報には付かない）。第一報の資料導線は hits の `url` を OpenClaw が出す形になる。
   デプロイ中に MCP タスクが入れ替わると、走行中の後追い thread は daemon なので投稿されずに消える
   （第一報は届いている＝fail-open。切替は業務時間外を推奨）。
6. **SOUL 未反映のまま ON にすると二重回答**になる（4.3 の検証 4）。
7. 二段返しは `include_answer=False`（Web の fast path）とは独立。/app の 2 フェッチ挙動は不変。
