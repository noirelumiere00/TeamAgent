# Aico のツール選択 v2：設計案

調査日：2026-11-09。対象はこの作業ツリー。実装・配備はしない。

**推奨は A。既存の入口ルーターを育て、明確な読取依頼から直接実行する。** 1〜3日目に振り分けの正解表・ローカル評価、4〜5日目に合格 route だけを試験用1名へ enforce、2週目に合格 route を全員へ広げる。答えの件数・リンク・添付もコードで組み立てる。42 本の削減は別段階とし、必要なら B へ進む。C は OpenClaw の再読込動作を確かめてから判断する。

## 1. 現状：モデルが選ぶ経路と、コードで選ぶ経路が併存

**本番確認済み（11-09）：** 本レビューで共有された Claude の AWS 読取結果。本作業では再接続していない。

| 本番の設定 | 意味 |
|---|---|
| TD の `TEAMAGENT_INBOUND_ROUTER`、`TEAMAGENT_ROUTE_CLASSIFIER` は共に未設定 | 既定 **off／0**。入口ルーター・小モデル分類は本番では未稼働。 |
| `SEARCH_DIRECT_ANSWER=1`、`SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS=*` | 既存の検索直接回答は、本人確認済みの全員が対象。 |
| `SEARCH_DIRECT_ANSWER_KINDS=not_found,list_fast,comparison` | この3種類は検索の答えを OC が書き直さず直接出す。全種類の直接回答が ON という意味ではない。 |
| 外側ループは Haiku 4.5 | 設定の根拠：`infra/openclaw/openclaw.config.json5:50–56`。 |

既定値・対象判定の根拠：`infra/openclaw/caller-identity-plugin/dist/index.js:3086–3101,6609–6612`。**入口の先行検索は off、検索後の直接回答は3種類で全員 ON**。

| 確認した事実 | 根拠（リポジトリ相対パス：行） |
|---|---|
| SOUL が「意図からツールを選ぶ」と指示する。 | `infra/openclaw/SOUL.md:37–44` |
| 各 Skill の description と Pydantic 入力定義を ToolSpec にする。機能フラグで登録数が変わる。 | `src/teamagent/orchestrator/tools.py:31–51`、`src/teamagent/orchestrator/factory.py:127–190` |
| Gateway は起動時の specs を保持し、`tools/list` で全定義を返す。OpenClaw 側にも静的な公開許可リストがある。 | `src/teamagent/mcp_gateway/server.py:2274–2306`、`infra/openclaw/openclaw.config.json5:255–277` |
| 42 本・37,000 字は固定フラグでの回帰条件。上限は説明文だけでなく、name・入力スキーマを含む tools 全体の JSON 長。実稼働の数・文字数は今回測っていない。 | `tests/mcp_gateway/test_tool_definition_budget.py:34–65` |
| 選び分けの規則も文章にある。例：社内の実績は search、新規の競合動画は video_algorithm。Slack 全体検索と現在会話の要約も説明文で区別する。 | `src/teamagent/skills/search/skill.py:225–233`、`src/teamagent/skills/slack_search/skill.py:122–128` |
| 既存入口ルーターは原文を規則で分類する。`off / shadow / enforce` があり、限定した DM の社内検索をモデルより先に直接実行する。分類モデルは未実装で、フラグは 0 以外を拒否する。 | `infra/openclaw/caller-identity-plugin/dist/index.js:2962–2999,3086–3089,6588–6638` |

**署名原文の受入先は限定される。** `request_text` は Slack 検索・要約と、DM のカルテ・提案資料生成だけ。通常の `search` は対象外。根拠：`infra/openclaw/caller-identity-plugin/dist/index.js:3977–4015,6150–6194`、`src/teamagent/mcp_gateway/server.py:704–722`。

| コードで決めている部分 | 適用範囲・根拠 |
|---|---|
| Slack の一覧検索 | 原文・query・focus から一覧モードへ。`src/teamagent/skills/slack_search/skill.py:171–194`、`src/teamagent/skills/slack_search/listing.py:25–99` |
| 検索の回答形式・取得範囲 | fact/list/insight、件数、簡潔表示を **input.query** から決める。署名原文の判定とは異なる。`src/teamagent/skills/search/answer_mode.py:64–71`、`src/teamagent/skills/search/fanout.py:330–347` |

<div style="break-before: page;"></div>

## 2. 選択肢：効果は仮説、工数は設計見積り

前提数値は依頼文による。全 DM の「良い」率 24.8%、12 月目標 50%。失敗の型は「社内用語・長すぎる答え」31 件、誤情報18、検索不足15、会話混在14、聞き返し14、拒否13。資料作成0/17、Slack 検索1/11、案件2/13を優先する。**件数の重複・母数・原因は未検証。改善幅の予測には使わない。**

| 案 | 方式 | 「良い」率への効き方の仮説 | 実装の大きさの目安 |
|---|---|---|---|
| **A：入口の振り分け** | 原文→分野・必要な根拠・出力形式。明確な依頼は直接実行。将来は低確信時だけ小さい判定モデル。少数ツールの提示には別途 OpenClaw の対応が必要。 | 検索不足・社内用語の誤解・不要な拒否に直接効く。答えの固定組立ても併せ、件数・出典の欠落と長文を抑える。資料生成そのものの品質は別。 | **小→中**。1週で正解表・入口専用 gate・1名 enforce。全分野・小モデル・候補削減は後続。 |
| **B：8 前後の窓口** | 42 本を分野ごとの窓口へ束ねる。内部で既存 Skill を選ぶ。 | 外側の取り違え・聞き返し・拒否を減らす余地。中身も LLM に丸投げすると選択ミスが内部へ移るだけ。 | **中→大**。2〜4週以上の目安。子ツールの認可、入力、非同期 job 契約の維持が必要。 |
| **C：必要時の定義読込** | 初期は検索口だけ。必要な定義を発見・登録してから呼ぶ。 | 無関係な定義を減らせる。一方、必要な定義を発見できないと検索不足・拒否が増える。 | **大・上限未定**。まず再読込の成立試験。Gateway だけの変更では成立しない。 |
| **D：説明文を最適化** | 現行42本を保ち、語彙・相互排他・スキーマを圧縮。 | 既知の混同には効き得る。検索の強制、会話の分離、回答長の保証にはならない。 | **小**。数日。ただし追加ごとに再調整・回帰評価が続く。 |

工数は1名が既存基盤を使う場合の幅。配備待ち・レビューは別。どの案も、検索元の欠落や資料生成の故障を直すものではない。

| 案 | 主な壊れ方 | 戻し方（設計） | OpenClaw の便＝イメージの再配布 |
|---|---|---|---|
| A | 誤分類で必要な検索・操作を落とす。複合依頼を1本へ潰す。直接投稿が重複する。 | 既存 `TEAMAGENT_INBOUND_ROUTER=off`。対象外・未知は従来経路。実行開始後の失敗は二重実行せず明示する。 | 既存フラグのみなら設定変更・再起動。規則・plugin の変更には便が要る。候補提示の変更は上流対応も未確認。 |
| B | 子ツールのガード漏れ。広い汎用引数により捏造が増える。job の所有者・status が壊れる。 | 旧42本を保持し、窓口の公開をフラグで戻す。OpenClaw の許可リストも同じ世代へ戻す。 | **要る**。新しい窓口名の公開と plugin の署名対象の整理。MCP 側も配備。 |
| C | 一覧のキャッシュが古い。他会話の一覧が混ざる。定義を発見しても呼べない。 | 静的な全一覧へ戻し、クライアントの再接続・キャッシュ破棄まで確認。 | **要る見込み**。再発見と run ごとの候補更新を保証する必要。 |
| D | 文言の削除で別ツールへ誤流入。SOUL と矛盾。 | 説明文を旧版へ。入力契約は変えない。 | description だけなら原則 MCP 側。SOUL・公開名を変えるなら要る。再取得時点は未確認。 |

B の便の根拠：公開には別途 OpenClaw 設定が必要（`src/teamagent/orchestrator/factory.py:208–212`）。A の既存フラグは Terraform へ配線済み（`infra/terraform/fargate.tf:1170–1174`）。C の判断は次節。

<div style="break-before: page;"></div>

## 3. C は「MCP で可能」と「今の Aico で使える」を分ける

**結論：現行 `tools/list` の返却内容を変えるだけでは、質問別の遅延読込にはならない。MCP の仕様全体が禁止する、とは断定しない。** 今回はネットワークを使わず、仕様本文と上流クライアントの実行物を検証していない。

| 論点 | コードで確かめた事実 | 判断・未確認点 |
|---|---|---|
| 質問の伝達 | `_list()` は引数なし。specs は server 構築時に固定。`src/teamagent/mcp_gateway/server.py:2274–2306` | 原文や route を discovery に渡す経路がない。署名 `request_text` は call 時の情報で、list の情報ではない。 |
| セッション | 単一の production server を stateful Streamable HTTP で公開。`scripts/run_mcp_http_server.py:124–130` | stateful というだけでは、質問別一覧・変更通知の対応を証明しない。 |
| 接続・公開範囲 | 単一 server・共有 bearer・静的 toolFilter。`infra/openclaw/openclaw.config.json5:255–277` | 一覧をグローバルに切り替える案は避ける。並行会話が混線するおそれは設計上の推測。 |
| repo 内の通信契約 | canary は protocol `2025-03-26`、`tools/list` に `{}` を送り、review 済みの名前と完全一致を要求。`infra/openclaw/rollout-task-canary.mjs:140–170,95–114` | この実例は全一覧の discovery。動的一覧には canary の契約も整理する必要がある。protocol の版だけで検索機能の有無は決めない。 |
| SDK・上流 | Python MCP の依存範囲は `>=1.28.1,<2`。OpenClaw は外部の固定イメージ。`pyproject.toml:86–88`、`infra/docker/Dockerfile.openclaw:16–25` | `notifications/tools/list_changed` の送受信、再取得、キャッシュ破棄、同じ run の候補更新は repo 内の実装・試験で確認できない。対応済みとも非対応とも断定しない。 |
| prompt hook | 現在は `before_prompt_build` で直接検索し、結果を appendContext に入れる。`infra/openclaw/caller-identity-plugin/dist/index.js:7858–7878` | これは候補ツールの差し替えではない。上流関数の待機試験も候補更新を証明しない（`infra/openclaw/caller-identity-plugin/test/inbound-router.test.mjs:198–245`、実行物未指定なら skip）。 |

**C を進める前の小さな成立試験（後日の作業）：** 固定版クライアントと代役 MCP だけを使う。①初期1本、②定義追加と通知、③同じ run で再取得・登録、④追加ツールの call 成功、⑤別会話への非混入、を確認する。対応がなければ bridge の改修量を見積もり直す。

定義 JSON を検索結果として返すだけでは、呼べるツールへ登録されたことにはならない。検索窓口が内部実行まで行うなら **B**。さらにコードで振り分けるなら **A＋B** と整理する。これは設計上の区別である。

なお `src/teamagent/mcp_gateway/server.py:899–907` の「新規ツール呼び出し hook はない」という注記は、現在の直接検索実装と一致しない。C の不可根拠には採用しない。

<div style="break-before: page;"></div>

## 4. 推奨構成：A を先行、必要なら B で公開面を縮める

入口は、モデルが加工した query ではなく受信原文を使う。既存 `classifyInboundRoute` の「根拠が必要か・一覧か・生成か」を再利用し、分野ラベルを加える。**分野、出力形式、実行許可を別にする。** 分野が決まっても書込の許可にはしない。

```text
受信原文＋同じ会話の必要な文脈
  → 既存規則 → 分野 / 根拠の要否 / 出力形式 / unknown・複合
  → 明確な読取：既存の署名経路で直接実行
  → その他：従来の外側ループ
  → 後続：低確信だけ小モデル判定、または分野窓口
```

下表は **B を採る場合の仮の8窓口**。括弧内は42本の固定ベースラインの内訳。窓口内では操作を型付きで分ける。巨大な自由形式の引数辞書は設けない。

| 分野・仮の窓口 | 既存ツールの割当案 |
|---|---|
| 社内資料・実績（6） | search / workspace_search / recommend / media_case_search / knowledge_deliver / knowledge_search_url |
| 案件・カルテ（1） | clientkarte |
| Slack（2） | slack_search / slack_summary |
| 資料作成・レビュー（6） | proposal_draft / proposal_review / proposal_builder_submit・status / omiyage_report_submit・status |
| メール（5） | mail_summary / mail_followup / mail_to_internal_context / mail_reply / mail_draft |
| 予定・商談準備（4） | calendar_freebusy / calendar_event / schedule_propose / meeting_prep |
| 公開調査・動画（15） | web_research / tiktok_search / tiktok_acquire・status / tiktok_comment_mining / search_surface_check / video_analysis / video_algorithm・status / video_capture / ad_label_check / x_voice_search / x_needs_mining / x_buzz_measure・status |
| 連携・添付・設定（3） | oauth_connect / attachment_assist / digest_settings |

割当の元：`tests/mcp_gateway/tool_input_contract_mcp224.json:1`。一般会話・未知・複合は分野外の退避先とする。分野を越えるメール→社内資料等は内部で連携する。上の分類自体は提案であり、正解表で境界を検証する。

**初週に8窓口は作らない。** A の直接実行で選択ミスを減らしても、通常の42本の定義量は減らない。選択精度を確認した後、説明文の予算問題には B、または成立確認後の候補限定で対処する。窓口化で本数が減っても、全子スキーマを展開すれば文字数は減らないため、定義 JSON 長も測る。

小モデルは後続。規則で決まらない時だけ、固定ラベルと根拠要否を返す。timeout・不正 JSON・未知ラベルは従来経路へ戻す。モデルの自己申告 confidence を正解確率と扱わない。既存 `skills/router.py` は検索戦略用の補助で、42本の入口選択には流用しない（`src/teamagent/skills/router.py:1–16,115–125`）。

<div style="break-before: page;"></div>

## 5. 答えの組み立て：選んだ後も件数・出典を落とさない

**レビュー提供例（一般化）：** 金庫5・Slack17・案件3件を得たが、出典付き1件と聞き返しになった。`search_compact_fallback reason=missing_citations` の経路。本作業では再検証していない。

引用IDなしの要約行は除外される（`src/teamagent/skills/search/compact.py:443–478`）。compact が空ならカードか通常再要約へ（`src/teamagent/skills/search/fanout.py:725–779`）。通常合成には2→1件、節ごとの削除もある（`src/teamagent/skills/search/answer_sections.py:850–896`）。**取得成功・直接配信 ON・KINDS 拡張だけでは、表示件数は保証されない。**

### 直接回答の種類はどこで決まるか（コードで確認）

入口の `rule_id` と、取得結果から決まる `direct_answer_kind` は別物である。

| kind | 決定箇所・条件 |
|---|---|
| `list_fast` / `case_cards` | 一覧要求・非比較。一覧描画成功時は許可された case_cards を優先。`src/teamagent/skills/search/fanout.py:647–666,1002–1050` |
| `comparison` / `video_metrics` | 比較表の成立を優先。次に施策合計・動画の数字の節の成立。`src/teamagent/skills/search/skill.py:1634–1646` |
| `not_found` | 可視ヒットなし、非公開一致なし、必要な検索が完了した場合。`src/teamagent/skills/search/fanout.py:953–967` |
| `case_state` | 専用 gate＋状況質問から固定描画。本番有効範囲は未確認。`src/teamagent/skills/search/fanout.py:1193–1212` |

Gateway は kind を照合して直接配信し、成功後に添付する（`src/teamagent/mcp_gateway/server.py:1783–1822`）。OC の書き直しを止めても、MCP 内の要約欠落は残る。

### A の route と答えの組立ての対応（提案）

| route（既存 rule_id） | 組立てと直接回答の扱い |
|---|---|
| `case_list` | コードが選別・重複除去・要求数・題名・リンク・添付候補を固定。AI は各件の要点だけ。要点の失敗で一覧を落とさない。初週は list_fast、case_cards は対象者限定で後続。 |
| `internal_status` | 正式状態・根拠日・出典は固定描画。AI は補足だけ。case_state の専用 gate を再利用し、成立した範囲だけ enforce。 |
| `internal_metrics` | 集計行・単位・動画別数字・リンクはコード、AI は各施策の要点だけ。video_metrics を追加候補とする。 |
| `internal_comparison` | 対象・比較軸・根拠リンクは固定表、AI は各対象の要点・差の説明。comparison は許可済み。入口 enforce は後続。 |
| 用語・関与・社内一般・出典 | `internal_term / involvement / internal_topic / source`。根拠・リンクを固定、AI は短い説明。初週は従来経路。 |
| その他 | 生成・連携・新規調査・一般会話・否定・未知・複合・ファイル配送は既存 Skill／退避先へ。検索用 kind を流用しない。 |

再利用元：一覧 `src/teamagent/skills/search/case_list.py:101–216`、表示・添付参照 `src/teamagent/skills/search/fanout.py:1033–1050`、状態 `src/teamagent/skills/search/timeline_status.py:352–421`、数字 `src/teamagent/skills/search/campaign_files.py:104–126,452–461`。添付は表示URLに対応する候補から最大3件、専用フラグ・非公開の会話面を確認する（`src/teamagent/mcp_gateway/search_file_delivery.py:67–103`）。**取得数・重複除去後の候補数・表示数・添付数を分ける。**

**KINDS の拡張：** case_cards・video_metrics は両側に定義があるが、共通 KINDS への追加は全員に効く。初週は3種を維持し、新種類は対象者別 gate から。MCP の許可種類は `src/teamagent/mcp_gateway/direct_search_answer.py:22–40`。case_state は専用 gate（`src/teamagent/case_state.py:57–65`、`src/teamagent/mcp_gateway/server.py:1355–1366`）で扱う。OC の共通 KINDS は case_state を知らず、未知値があると許可集合を全消去するため単純追加しない（`infra/openclaw/caller-identity-plugin/dist/index.js:3090–3101`）。

答えの合否・便前の答え試験は [answer_eval_gate.md](answer_eval_gate.md) へ。別作業で作成中で、このツリーにはまだない。本節は組立ての責務を定め、答えの正解表・採点・閾値は作らない。

<div style="break-before: page;"></div>

## 6. 最初の1週間：正解表から、合格 route の1名 enforce へ

以下は将来の実装・配備計画。今回変更するのは本書だけ。12月50%を目指し、初週を shadow だけで終えず、合格した読取 route を実際の改善へつなげる。

**既存 allowlist はあるが共用である。** 入口も `SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS` を検査する（`infra/openclaw/caller-identity-plugin/dist/index.js:6588–6612`）。Terraform も入口 mode と共用 allowlist だけを持つ（`infra/terraform/variables_fargate.tf:685–720`、`infra/terraform/fargate.tf:1170–1174`）。これを1名に狭めると、既存の全員向け直接回答まで変わる。また route 別 gate はなく、enforce では kind 対象外でも data.answer を採るため、KINDS で入口 route を制限できない（`infra/openclaw/caller-identity-plugin/dist/index.js:6625–6633,6715–6733`）。

**初週に足す最小の gate（未実装）：** `TEAMAGENT_INBOUND_ROUTER_ALLOWED_EMAILS` と `TEAMAGENT_INBOUND_ROUTER_ALLOWED_RULES`。空は全拒否。検証済み本人と合格済み rule_id の両方が一致した時だけ先行実行する。既存 mode を使い、共用 `SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS=*` と既存3種は維持する。

| 日 | 作業・置き場所（将来案） | 到達点 |
|---|---|---|
| 1〜2 | `tests/routing/routing_v2_corpus.jsonl` に架空の120件。調整80・固定評価40件。資料作成・Slack・案件を厚くし、2名で期待値を確認。 | 分野・rule_id・根拠要否・出力形式・対象 tool・退避・文脈を正解にする。実 DM・氏名・メール・実URL・実IDは入れない。 |
| 3 | `scripts/routing_eval/` にローカル runner。既存分類と候補規則を比較し、API 代役の hook 試験で本人・route gate も確認。専用 gate の最小変更と配線を用意。 | 分類と入口の合格表を確定。小モデルは0のまま。答えの便前合否は別設計書の担当へ渡す。 |
| 4〜5 | 便前に振り分けと別設計書の答え試験が合格した route **だけ**、`TEAMAGENT_INBOUND_ROUTER=enforce` と入口専用1名 allowlist で試す。 | 候補は `case_list` / `internal_status` / `internal_metrics`。まず既存 list_fast で成立する case_list から。3種同時投入は求めない。不合格・対象外は従来経路。 |
| 2週目 | 試験を通過した route を、入口専用 allowlist `*` で全員へ。RULES は合格分だけを維持。 | 追加種類は対象者別 gate または専用 gate で段階公開。全員公開後に共通 KINDS を広げる。残る分野は個別に追加する。 |

専用 gate の配線先は plugin、`infra/terraform/variables_fargate.tf`・`fargate.tf`、`infra/docker/openclaw-entrypoint.mjs:98–105`、`infra/openclaw/harden-task-definition.jq:169–197`。新環境変数は既存にないので許可契約も同時に直す。plugin の変更には OpenClaw の便が要る。別設計書の答え試験が未完了なら、その route は配備せず従来経路に残す。

### 振り分けの正解表と測り方

架空例は「架空A社の事例を3件」「架空B案件の正式状態」「架空C施策の保存数」。否定・引用内の指示・同音の一般語・複合・原文欠落・添付・別会話も対照に入れる。言い換えを調整用と固定評価用へ跨がせない。

| 測定対象 | 初版の合格線（提案） |
|---|---|
| 純粋分類：分野正答率＝正解分野に一致／全件 | 固定評価90%以上。分野・rule_id 別の n/N・混同行列、根拠要否・出力形式の一致率も出す。 |
| hook 試験：正しい直接実行／直接実行判定 | **公開する route ごと**95%以上。n=0は未達。coverage＝直接実行判定／全件も出し、全退避で合格にしない。 |
| hook 試験：禁止操作、別会話、引数捏造、対象者・RULES漏れ、off/shadow差分 | 0件。重大回帰例100%合格。比較するのは入口の call・引数・配送回数。本文の良否は別設計書。 |
| 退避率、分類時間 p50/p95 | 基準との差を出す。規則のみの暫定予算 p95 20ms。 |

既存 `CorpusRow` は追加キーを捨てるため分野・rule_id の採点を runner に足す。直接実行行のみ既存 `expect / alt_ok / forbid / arg_rules` を再利用、退避は別採点（`scripts/routing_eval/scoring.py:57–89,180–221`）。hook は `fetchFn` 代役方式（`infra/openclaw/caller-identity-plugin/test/inbound-router.test.mjs:16–65,169–190`）。旧表は実発話を含むため転載しない（`tests/routing/README.md:58–59`）。結果は CI artifact に残す。小さい固定表の合格だけで、答えの品質改善を証明した扱いにはしない。

<div style="break-before: page;"></div>

## 7. 切り戻し・効果の追跡・未確認点

| 対象 | 切り戻し |
|---|---|
| 入口 enforce | `TEAMAGENT_INBOUND_ROUTER=off`。または新設 gate で対象者／RULESを外す。既存の全員向け直接回答3種は維持する。 |
| 追加した回答種類 | 追加分だけを対象者別 gate・KINDS から戻す。case_state は既存の専用 gate で戻す。入口の停止と配信の停止を分ける。 |
| 直接配信全体 | **MCP の配信を先に止め、最後に OC の回答抑止を止める。** 逆順は二重回答のおそれ。再開は OC が先、MCP が後。 |

mode は起動時読込なので変更には再起動が必要（`infra/openclaw/caller-identity-plugin/dist/index.js:3086–3101`）。配信の順序は `infra/terraform/variables_fargate.tf:667–670,705–708`。実行開始後の timeout・配送不明では、旧経路から同じ処理を再実行しない。既存 off/shadow の一致検査は `infra/openclaw/caller-identity-plugin/test/inbound-router.test.mjs:183–190`。

### 効果の追跡と、別設計書への引継ぎ

| 本書で追うもの | 扱い |
|---|---|
| 振り分けの精度・適用範囲 | 第6節の正解表、route別適合率、coverage、誤振分け、退避理由、分類時間。route_version と適用群を対応付け、本文を新しいログへ複製しない。 |
| 最終的な改善 | 週次 **全 DM の「良い」率**24.8%→12月50%と、資料作成・Slack・案件の n/N を同じ判定基準で追う。正解表の分類精度とは別の数字。1名の試験だけで改善率を確定しない。 |
| 答えの合否 | 便前の答え試験・採点規約・閾値は [answer_eval_gate.md](answer_eval_gate.md) へ。route、要求件数、取得結果、採用候補、表示参照、添付参照、kind、fallback理由を引き渡す設計とする。ここでは答え用 corpus や採点器を新設しない。 |
| 👍👎 | 補助指標。`search_feedback` の query・rating・note=tools は選択原因の正解ではない。👍率は全 DM の良い率と母数が違う。利用者・answer_id ごとの最新1票で数える。 |

評価記録の根拠：`src/teamagent/adapters/answer_feedback_store.py:21–24,38–54`、`src/teamagent/mcp_gateway/server.py:2103–2111`。最新1票の集計：`src/teamagent/dashboard/queries.py:583–597`。既存ログの hit_count・slack_count・case_count は取得側の件数で、表示数ではない（`src/teamagent/skills/search/fanout.py:1090–1106`）。引継ぎ項目の追加・集計は今後の実装であり、今回データ抽出はしない。

### リスクと未確認点

| 区分 | 内容・対処 |
|---|---|
| **確認済み** | 本番の入口はoff、既存直接回答は3種で全員ON（第1節のレビュー提供事実）。入口allowlistは共用で独立 gate は未実装。種類の拡張や enforce の一括ONで、試験範囲を全員へ広げない。 |
| **確認済みの認可制約** | claim は tool名・全引数・一度だけのnonceに束縛（`src/teamagent/mcp_gateway/caller_claim.py:437–453,481–490`）。B は子の DM限定・RLS・入力・job所有者の検証を維持する（`src/teamagent/mcp_gateway/server.py:1172–1227,1265–1367`）。 |
| **設計上の推測** | 選択ミスと要約時の欠落の両方を減らせる可能性がある。取得の欠落・誤った元資料・生成機能の故障は別。誤分類・会話混在・二重配送・キャッシュ混線も残るため、合格 route だけを広げる。 |
| **未確認** | 実稼働のツール数・定義量、OpenClaw の再発見、case_state・添付等の本番有効範囲、週次判定規約の詳細、選択と組立てが各失敗に占める割合。0/17等の少数例だけで改善見込みは確定できない。 |
| **今回の範囲** | 本書だけを修正。ローカルのコード・設定・テスト定義を読み、テスト・モデル推論は未実行。本番・AWS・DB・Slack・ネットワークには接続せず、実 DM・社員の氏名・メールを載せていない。実装・配備・commit はしない。 |
