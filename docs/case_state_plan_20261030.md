# 案件状態・根拠日の調査と実装計画（2026-10-30）

## 結論

推奨は **案 A：既存 metadata に根拠付きの状態を持たせ、回答をコードで組み立てる**。まず「提案資料」「営業報告」「投稿の存在」「正式な受注・完了」を別の事実として扱い、正本がない状態は未確認にする。その後、案件を横断した現況・履歴管理が必要になった時点で案 B の専用台帳へ進む。

既に `timeline_status.py` があるため、状態判定をもう一つ追加する必要はない。ただし、現実装は検索された資料から状態を導く仕組みであり、確定した案件状態の正本ではない。LLM 分類、案件決定という分類、投稿集計から受注・実施済みを導ける。回答本文も原則として LLM が生成し、コードの状態行は前置きにとどまる。

計画名の「数字の正本（PR2）→ 正本から描く（PR3）」に合わせ、PR2 で数字と同じ根拠束に状態・日付・出典を定義し、PR3 でその値だけを描画する。**プロンプトへの注意書きだけでは完了としない。** この PR 番号は本計画の呼称であり、リポジトリ内の別計画の同名番号とは区別する。

## 調査条件と確認範囲

- 対象：この worktree、ブランチ `chore/case-state-plan-20261030`、調査時 HEAD `9e07ddea`。ローカルの `origin/dev` が HEAD の祖先であることを確認した。fetch はしていない。
- ユーザー提示の「全 DM 判定で 35 件」は背景として採用した。35 件の原文・採点データは今回読み取っておらず、各件の原因を特定したという意味ではない。
- 検索、取り込み、DB migration、Vault 書き出し・表示、カルテ、回答生成・配信、主要な関連テストを静的に追った。顧客名・実案件の識別子は本書に載せない。
- AWS・DB・Slack・Drive 等への実接続、`.env`・secrets の読み取り、実 Vault の読み取り、commit・push は行っていない。アプリケーションのコード・設定・テストは変更せず、本書だけを追加した。
- 本番のフラグ値、投入済みデータの状態付与率、実 DB の schema 適用状況、外側のモデルが実際に回答を再編集したかは未確認。以下はコードから分かる経路とリスクであり、本番発生頻度の断定ではない。

## 1. 状態と日付の経路

```text
Slack のフォーム／スレッド ─ 生の商談フェーズ、親投稿 ts ─┐
Sheets のフォーム行 ───── 生の項目、回答タイムスタンプ ─┤
Drive の資料 ───────── 本文、ファイル modifiedTime ──┤
事例集 Sheets／PPTX ───── 企業・商材・効果・ページ ────┤
動画集計 Sheets ──────── 投稿数・URL・投稿期間 ──────┤
                           任意の LLM 分類 cls_* ─────┤
                                                   ▼
                  documents.metadata + modified_at / chunks
                     │                             │
                     ▼                             ▼
          pgvector の検索・カルテ・事例射影      export_vault.py
                     │                       clients/・docs/ のミラー
                     ▼                             ▼
       通常検索／fanout／一覧／compact       build_app_html.py → /app
                     │
          timeline_status の検索時判定（flag 有効時）
                     │
            LLM 要約＋コードの節・前置き
                     ▼
           MCP／DM／Web／後追い投稿／派生 skill
```

金庫は DB の読み取りミラーであり、別の正式案件台帳ではない（`scripts/export_vault.py:246`）。検索の `drive_first` が選ぶ正本 URL や、重複排除の「正本」も、状態を確定する業務上の正本とは別概念である。

### 出どころの対応表

| 入力 | 状態の材料 | 日付の材料 | 現時点で確定できる範囲 |
|---|---|---|---|
| Slack 営業 FB | `商談フェーズ` → `deal_phase`、任意の `cls_phase` | 親投稿／thread の ts → `modified_at` | その投稿にその報告があること。正式受注や最新現況の保証はない |
| Sheets 営業 FB | 同じラベル写像で `deal_phase` | フォームのタイムスタンプ、なければ処理日時 | 回答・処理の記録日。商談発生日と同じとは限らない |
| 一般 Drive 資料 | 本文・題名から LLM が `cls_phase` を分類 | Drive の `modifiedTime`、タイトルから別途抽出した年月日 | 資料の種類・記載内容・ファイル更新。契約状態の確定ではない |
| 事例シート | 企業・商材・効果・対外利用可否等 | 対応する timestamp がなければ日付なし | 事例台帳への掲載。状態・実施年月の専用項目は現在の写像にない |
| 事例集 PPTX | 結果ハイライトで事例分割し `case_effect` 等を保存 | 各事例とも deck の `modifiedTime` | 事例の掲載・ページ。案件ごとの実施日・正式状態は保存しない |
| 動画集計 | `campaign_aggregate`、`video_count` | URL で結合した投稿日の最初／最後 | 集計行・投稿の存在。契約受注・案件全体の完了ではない |
| Vault カルテ | 取引先の最新 FB の `deal_phase` | DB の `modified_at` を日付化 | 取引先単位の最新報告。複数案件の代表状態にはできない |

## 2. 状態なし・推測・取り違えが起きうる箇所

優先度 P0 は状態・根拠を直接取り違える経路、P1 は情報欠落・表示差・迂回経路を表す。既存の防止策は残す。表の指摘は「35 件それぞれの発生原因を実証した」という意味ではない。

### 取り込み・保存

| ID | 優先度 | ファイル:行 | 確認内容と影響 |
|---|---|---|---|
| I1 | P0 | `src/teamagent/ingest/classify.py:198`、`src/teamagent/ingest/classify.py:291`、`src/teamagent/ingest/classify.py:503` | LLM が資料本文から `phase` を選び `cls_phase` に保存する。根拠箇所・状態の発生日・正式性をセットで保存しない。分類値を受注の正本にすると推定が業務事実になる |
| I2 | P0 | `src/teamagent/ingest/classify.py:328`、`src/teamagent/ingest/classify.py:508` | フェーズにも部分一致の正規化を使う。例えば分類出力が `未受注` なら許可語の `受注` を含むため受注へ寄るコードであり、否定・修飾を厳密に検証しない。原文からその出力になる頻度は未確認 |
| I3 | P1 | `src/teamagent/ingest/slack_fb_parser.py:51`、`src/teamagent/ingest/slack_fb_parser.py:131` | 人の `商談フェーズ` を非空の文字列として写す。値の来歴、認定者、状態発生日、厳密な状態 enum はない。未知語はそのまま残る。BANT は別キーであり、状態に転用してはいけない |
| I4 | P0 | `src/teamagent/ingest/pipeline.py:1794`、`src/teamagent/ingest/pipeline.py:1809`、`src/teamagent/ingest/pipeline.py:1891` | 親と返信をまとめた本文を FB parser に渡し、日時は親 ts にする。parser は重複ラベルを後勝ちにする（`src/teamagent/ingest/slack_fb_parser.py:173`）。返信が同じフォーム形式なら新しいフェーズと古い親投稿日を組み合わせうる |
| I5 | P0 | `src/teamagent/ingest/pipeline.py:191`、`src/teamagent/ingest/pipeline.py:4558` | Sheets は回答 timestamp、なければ処理日時を `modified_at` に入れる。両者を区別する日付種別が保存されない。処理日を商談日・提案日と呼ぶと誤りになる |
| I6 | P1 | `src/teamagent/ingest/pipeline.py:3465`、`src/teamagent/ingest/pipeline.py:3531`、`src/teamagent/ingest/pipeline.py:4660`、`src/teamagent/ingest/pipeline.py:4702` | Drive は分類値＋ファイル更新日、Sheets は人入力＋分類値を保存する。再分類による状態の揺れを止める契約はない。既存の監査 override は業種だけ（`src/teamagent/ingest/gsheet_classification_overrides.py:1`） |
| I7 | P0 | `src/teamagent/ingest/form_mappings.py:270`、`src/teamagent/ingest/pipeline.py:5060`、`src/teamagent/ingest/pipeline.py:5094` | 事例シートの列写像と deck metadata に状態・案件別実施年月がない。deck 全体の更新日が事例それぞれの日付として流れる。`case_external_use` は社外利用可否であり、実施状態ではない |
| I8 | P0 | `src/teamagent/ingest/campaign_aggregate.py:256`、`src/teamagent/ingest/campaign_aggregate.py:467`、`src/teamagent/ingest/pipeline.py:401` | URL を持つ行を動画として集計し、投稿数・投稿期間を保存する。正式受注・全作業完了の列はない。投稿日は別入力との URL 結合で、欠落時は空。集計印や効果の数字だけで案件完了を確定できない |
| I9 | P0 | `infra/migrations/0001_unified_documents.sql:39`、`infra/migrations/0001_unified_documents.sql:81` | documents は資料単位、chunks は断片単位。状態・状態時点・根拠・案件 ID の必須契約／状態履歴テーブルは、確認した migration 群にはない。`client_code` や外部 ID は存在するが、全入力で共有する案件識別子という契約ではない |
| I10 | P1 | `src/teamagent/ingest/repository.py:1838`、`src/teamagent/ingest/content_hash.py:72`、`src/teamagent/ingest/pipeline.py:1824` | 再 upsert は metadata を丸ごと置換する。DB に手で状態だけ追記する対処は再取り込みで消える。差分 skip もあるため、新しい正本入力・判定版を hash に含めない変更は過去資料に反映されない |
| I11 | P1 | `src/teamagent/ingest/docdedup.py:5`、`src/teamagent/ingest/docdedup.py:20` | 重複資料の「正本」は本文量最大で選ぶ。業務状態の新しさや正式性で選ぶのではない。将来、可視の状態根拠を検索候補・重複代表だけから引くと、修正版の状態を落としうる |
| I12 | P0 | `data/ingest_sources.yaml:57`、`data/ingest_sources.yaml:185`、`data/ingest_sources.yaml:254`、`data/ingest_sources.yaml:289`、`src/teamagent/ingest/pipeline.py:1813` | `topic=案件決定`、事例母集団、動画集計の印は取り込み元ごとの設定に由来する。例えば Slack の topic は対象ソースの投稿へ一律にコピーされる。個々の投稿で受注が確認された印ではなく、S4 の受注変換に使うと分類と状態を取り違える |

### 検索・状態解決・日付射影

| ID | 優先度 | ファイル:行 | 確認内容と影響 |
|---|---|---|---|
| S1 | P0 | `src/teamagent/skills/search/timeline_status.py:20`、`src/teamagent/adapters/pgvector_client.py:64` | 状態判定と metadata 全体の追加射影は `SEARCH_TIMELINE_STATUS` が有効な場合だけ。既定は無効。本番値は未確認。有効化だけでも以下の推測は残る |
| S2 | P0 | `src/teamagent/skills/search/timeline_status.py:83`、`src/teamagent/skills/search/timeline_status.py:107` | 同一性は取引先名＋`campaign`／`case_product`／`proposed_menu` の文字列。施策不明は資料別に分ける既存保護がある一方、同じ取引先・同じメニューの別年度案件は合流しうる。逆に表記差で同一案件が分離する |
| S3 | P0 | `src/teamagent/skills/search/timeline_status.py:113`、`src/teamagent/skills/search/timeline_status.py:149` | 最新資料を日付文字列で選ぶが、投稿数がある集計が一つでもあればその投稿実績を優先する。同日で状態が異なる場合は入力順に依存する。古い実績と新しい再提案・中止等の併存を、一つの状態に潰しうる |
| S4 | P0 | `src/teamagent/skills/search/timeline_status.py:155`、`src/teamagent/skills/search/timeline_status.py:158`、`src/teamagent/skills/search/timeline_status.py:175` | 投稿数 > 0 → `実施済み`、`deal_phase` がなければ `cls_phase`、それもなく `topic=案件決定` なら `受注（案件決定）` とする。コードの決定論であっても根拠の正式性は上がらない |
| S5 | P0 | `src/teamagent/skills/search/timeline_status.py:43`、`src/teamagent/skills/search/dates.py:98`、`src/teamagent/skills/search/answer_sections.py:405` | timeline は投稿日→記録日→更新日→作成日→資料名日付。公開ヒットの `date_basis` と一覧は資料名日付を優先。同じ資料の状態行と一覧が異なる日付を示し、年月だけのタイトルは timeline では日付不明になる |
| S6 | P1 | `src/teamagent/adapters/pgvector_client.py:737`、`src/teamagent/adapters/pgvector_client.py:832`、`src/teamagent/adapters/pgvector_client.py:1111`、`src/teamagent/adapters/pgvector_client.py:1344` | 検索・事例・カルテがそれぞれ別 SQL で射影する。`modified_at` を `updated_at`／`occurred_at` に変え、日単位に切る。状態に紐づいた共通の根拠束はなく、元の時刻や日付種別も失いやすい |
| S7 | P1 | `src/teamagent/skills/search/knowledge_query.py:49`、`src/teamagent/skills/search/skill.py:441`、`src/teamagent/skills/search/skill.py:1656` | 「受注」「成約」等を自動分類の `cls_phase` フィルタにする。未分類の許容・0 件時のフィルタ解除もあるため、検索一致は正式受注の検証ではない。候補選びと状態の検証を分ける必要がある |
| S8 | P1 | `src/teamagent/skills/search/schema.py:203`、`src/teamagent/skills/search/schema.py:256`、`src/teamagent/skills/search/fanout.py:1136` | 公開出力は生の `deal_phase` と資料の日付を持つが、共通の状態 enum・根拠日種別・確認区分・状態出典を持たない。通常系と fanout の出力整形も別実装である |
| S9 | P1 | `src/teamagent/skills/search/fanout_sources.py:293`、`src/teamagent/skills/search/fanout_sources.py:623` | 「施策実績」「事例」「案件」は metadata による探し先・資料の分類。`CaseRow` は効果・更新日等を保持するが状態契約はない。カテゴリ名を受注・実施・完了の代わりに扱えない |
| S10 | P1 | `src/teamagent/skills/search/drive_first.py:207`、`src/teamagent/skills/search/skill.py:3051` | ライブ Slack 投稿を SearchHit に変える際に timestamp・状態を渡さない。状態解決も通常は hits／カルテを対象とし、別配列のライブ投稿は含めない。新しい Slack の記載とコードの状態行に差が出うる |
| S11 | P1 | `src/teamagent/adapters/pgvector_client.py:403`、`src/teamagent/skills/search/skill.py:2251` | 旧 schema の検索も残り、任意の metadata と追加列から SearchHit を作る。新しい状態契約はない。移行済み資料だけを直して旧経路が状態推測へ戻ることを避け、根拠束のない旧ヒットは未確認にする必要がある |

### 金庫・回答・派生経路

| ID | 優先度 | ファイル:行 | 確認内容と影響 |
|---|---|---|---|
| O1 | P0 | `scripts/export_vault.py:272`、`scripts/export_vault.py:308`、`scripts/export_vault.py:1144`、`scripts/build_app_html.py:2484` | Vault は取引先の最新 FB のフェーズを frontmatter に採用し、Web はそのまま代表 phase として表示する。案件 ID がないので、同じ取引先の別案件の状態を代表させうる。formal／reported の区別や根拠日も phase に束ねない |
| O2 | P1 | `scripts/export_vault.py:380`、`scripts/export_vault.py:1186`、`scripts/build_app_html.py:1755` | 資料 note は主に資料種別・更新日等を表示し、状態の根拠束は書き出さない。Web のフェーズ語彙には口頭内示を含む成約区分があるが、timeline の既知語には同じ区分がない。語彙と意味が層で異なる |
| O3 | P0 | `src/teamagent/skills/search/skill.py:2941`、`src/teamagent/skills/search/skill.py:3051`、`src/teamagent/skills/search/skill.py:3071` | コードの状態はプロンプトへ渡すが、本文は LLM が自由生成する。`src/teamagent/prompts/search/timeline/system.md:3` の規律と、`src/teamagent/prompts/search/v3/system.md:56` の「正式な受注正本は読めない」規律が併存する。受注ラベルをコードで渡すこと自体は正式性の証明にならない |
| O4 | P0 | `src/teamagent/skills/search/timeline_status.py:210`、`src/teamagent/skills/search/skill.py:1244`、`src/teamagent/skills/search/fanout.py:1072` | コードの状態行は現況語を含む問いに限定して前置きする。矛盾した LLM 本文を置換・拒否する処理ではない。「実績を教えて」「受注した事例」等では同じ強制表示にならない |
| O5 | P0 | `src/teamagent/skills/search/compact.py:542`、`src/teamagent/skills/search/case_list.py:150`、`src/teamagent/skills/search/answer_sections.py:679` | compact は一部の状態文に引用元の日付を後付けするが、状態そのものを正本照合しない。一覧には「案件は実績ではない」という保護がある一方、説明は本文・効果の抜粋で、全カードに検証済み状態・日付を必須化していない |
| O6 | P0 | `src/teamagent/skills/search/skill.py:2811`、`src/teamagent/skills/search/skill.py:2829`、`src/teamagent/skills/search/fanout.py:1187` | 二段返しの後追いは要約＋数字等を投稿し、同期出口の状態前置き処理を通らない。grounding の一部は数字の shadow ログで、状態違反を止める出口にはなっていない |
| O7 | P0 | `src/teamagent/mcp_gateway/direct_search_answer.py:22`、`src/teamagent/mcp_gateway/direct_search_answer.py:61`、`src/teamagent/mcp_gateway/server.py:1715` | 直接配信は許可された回答種別・利用者・フラグに限定され、状態専用の種別はない。通常の tool 結果として返す経路もあり、コードで作った状態を外側で再作文させない配信契約まで確認する必要がある。本番の外側での実挙動は未確認 |
| O8 | P0 | `src/teamagent/skills/clientkarte/skill.py:844`、`src/teamagent/skills/clientkarte/skill.py:863`、`src/teamagent/skills/meeting_prep/sources.py:60`、`src/teamagent/skills/proposal/skill.py:105` | カルテは生フェーズと本文から LLM 合成する。会議準備は search の本文を Material にし、提案は SearchHit の抜粋から再生成する。search の answer だけを直しても、構造を捨てる呼び出し側で再び状態を推測できる |

### 残すべき既存の保護

- BANT C を失注に近似する処理は既に除去されている（`src/teamagent/skills/search/aggregation.py:13`）。これは維持する。
- 施策不明の資料は別案件へ合流させず、不明日付に取り込み日時を使わない保護がある（`src/teamagent/skills/search/timeline_status.py:59`、`src/teamagent/skills/search/timeline_status.py:87`）。
- 「投稿確認」を全作業完了と区別する説明はプロンプトに既にある（`src/teamagent/prompts/search/timeline/system.md:5`）。区別をコードと型にも移す。
- Vault の共有 group 制約と per-user RLS は維持する（`scripts/export_vault.py:1253`、`infra/migrations/0001_unified_documents.sql:118`）。新しい状態根拠を理由に可視範囲を広げない。

## 3. 両案に共通する契約

### 保存する情報と表示の意味

| 項目（設計名） | 意味・制約 |
|---|---|
| `subject_ref` | 状態の対象となる案件・施策。資料 ID、取引先名、商材名とは別。明示的な対応がない限り資料をまたいで同一案件とみなさない |
| `document_kind` | 提案資料・報告資料・事例掲載等。案件状態とは独立。提案書があるだけでは「今も提案中」にならない |
| `state` | `unknown / proposed / won / in_progress / completed / lost / cancelled`。曖昧な「決定」「報告」「終了」は、意味が定義された入力以外では未確認にする |
| `authority` | `official / reported / unverified`。正式台帳・確認済み根拠、営業報告、未検証候補を区別する。LLM 分類は未検証で、正式状態へ昇格させない |
| `resolution` | `resolved / unknown / conflict`。矛盾は状態の進み具合や入力順で解消せず、競合として保持する |
| `evidence_date` と `evidence_date_kind` | 状態を述べている根拠記録の日付と種別。`message_posted / form_submitted / document_issued / reviewed` 等。処理日時・ファイル保存日を状態の発生日へ読み替えない |
| `state_as_of` と精度 | 明示された状態時点。根拠記録日と同じと保証できなければ空。日／月を区別し、年月だけを架空の 1 日で表示しない |
| `source_ref` | 元 document、外部の安定識別子、元投稿／行／ページ、表示 URL、根拠部分、元データの版・hash。状態と出典を別の資料から継ぎ合わせない |
| `delivery_observation` | 投稿確認等の観測事実。投稿数と投稿期間を持つが、`completed` や `won` に変換しない |

`source_modified_at` と `ingested_at` はこれらと別に保持する。既存の `updated_at / title_date / date_basis` は資料の日付として互換性を保ち、状態時点の代用品にはしない。タイトルの年月は「資料名の日付」と明示する。日付を確定できない場合は「根拠日不明」と出す。

### 根拠の採用規則

1. 正式台帳の明示項目、または担当者が原資料・案件対応を検証した記録だけを正式状態とする。現時点で読める正式台帳があるとは仮定しない。
2. 営業 FB の生フェーズは「その記録での報告」として保存する。たとえ `受注` と書いてあっても「営業 FB に受注との記載。正式受注は未確認」と表示し、正式受注の一覧・件数には入れない。
3. `cls_phase`、資料種別、チャンネル名、`topic`、BANT、金額、動画数、ファイル更新日は正式状態の認定材料にしない。LLM は候補抽出に使えても採用判断をしない。
4. 投稿 URL・数・日付は「投稿確認」の根拠とする。案件の完了を表示するには、対象案件と完了を明示した検証済み根拠が別に必要。
5. 同一案件の認定は安定 ID または人が確認した対応だけ。年度・施策が違えば別案件。同一主体への対応が曖昧、同時点で食い違う、日付区間が重なる、根拠が失効した場合は自動で統合・昇格しない。
6. 最新の「資料」と最新の「状態イベント」を分ける。後日の資料保存で状態時点を動かさない。過去の受注を現在の受注と表現せず、必ず時点・根拠を添える。
7. 本人に見える根拠だけで解決する。見えない正式根拠を要約だけで漏らさない。アクセス不能は「未確認／検索未完了」であり「失注」「案件なし」ではない。

### 回答で値だけを使うための境界

案件の状態・日付・実績認定を含む事実ブロックは、検証済みの構造体から定数テンプレートで作る。LLM には根拠 ID の選択・並べ替えまでを許し、採用する ID はサーバ側の候補集合に制限する。モデルが生成した状態文・日付文をそのまま貼らない。

自由文を併記してから矛盾する語を正規表現で削る方法では、意味上の言い換えを取り逃がす。案 A では、案件事実を含む回答を構造化したカード中心に縮め、自由作文が必要な混合質問でも状態に関する部分は固定ブロックに限定する。未検証の本文抜粋から「当社の実績」と認定することも禁止する。これを分離できない出力面は回答全体を定型表示へ落とす。

例（すべて匿名の表示仕様）：

- 「案件 A：正式な受注状況は未確認。2026-10-01 の営業 FB に『提案』との記載。［根拠］」
- 「施策 B：投稿 6 本を確認（最終投稿日 2026-08-15）。受注・案件全体の完了は未確認。［根拠］」
- 「資料 C：資料名の日付 2026-01、ファイル更新日 2026-10-01。実施年月・正式状態は未確認。［資料］」
- 「案件 D：記録が競合しているため状態未確認。各記録の状態・時点・出典を併記。」

## 4. 案 A：既存 JSONB と既存検索を補強する小さい案（推奨）

### 変更範囲

専用テーブル・新しい CRM 接続・依存ライブラリを増やさない。`documents.metadata` に版付きの `case_state_evidence` を持たせ、共通の型・検証・描画は既存 `timeline_status` 周辺へ集約する。旧 metadata は候補情報として維持し、根拠束がないものは `unknown` として扱う。

| 工程 | 対象と作業 |
|---|---|
| PR2：正本の型 | `skills/search/schema.py` と共有の型に根拠束を定義。`timeline_status.py` の推測による受注・実施済み認定を置き換える。数字の正本と同じ対象 ID・出典を参照し、投稿観測を別欄にする |
| PR2：入力 | `ingest/slack_fb_parser.py`、`form_mappings.py`、`pipeline.py`、`campaign_aggregate.py`、事例 deck 経路。生報告と正式認定を区別し、返信の根拠は返信自体の日付・位置へ結び付ける。通常の分類・本文の保存は維持する |
| PR2：正本がない時の補完 | 既存の ingest 設定ロード方式に合わせ、source identity＋根拠位置＋内容 hash をキーにした小さな人手確認 manifest を追加する。値は state・日付種別・時点・確認記録。顧客名で照合せず、ID・hash が一致しない記録は適用を止める |
| PR2：永続化 | manifest を ingest 入力に合成し、入力 hash／判定版にも含める。`repository.py` による再取り込みで消えない形にする。DB だけへの手作業追記は採用しない。適用対象の限定再取り込み手順と乾式差分出力を用意する |
| PR2：検索への伝搬 | `adapters/pgvector_client.py` の意味検索・メタ検索・カルテ・事例・名指し資料検索で同じ束を射影する。`fanout_sources.py` の CaseRow、ライブ Slack 変換、公開 SearchHitOut も束を落とさない。status は検索スコアに左右されない |
| PR3：コード描画 | `skill.py`、`fanout.py`、`compact.py`、`answer_sections.py`、`case_list.py`、`visit_cases.py`、二段返しで同じ renderer を使う。flag 無効・失敗・古い行でも「未確認」を既定にし、LLM の状態推測へ戻さない |
| PR3：金庫 | `export_vault.py` と `build_app_html.py` で同じ束・語彙を扱う。案件不明の取引先ページに単一の案件状態を付けず「最新 FB 報告」と表示。資料の保存日と状態の根拠日を別ラベルで出す |
| PR3：配信・呼び出し側 | `mcp_gateway/direct_search_answer.py`／`server.py` の既存配信方式へ案件事実の種別を追加。`runtime/slack_bot.py`、Web、カルテ、会議準備、提案の参照事実へ同じ描画を使い、raw hit から状態を再作文する迂回路を塞ぐ。配信失敗時も同じ定型結果を再提示できる契約にする |

`__init__`／export、型を通す中間 dataclass、mock・fixture、フラグの設定契約も変更の到達範囲に含む。新しい runtime option を増やすことは目的にせず、既存の段階公開方式を使う。本番設定値を今ここで変更する計画ではない。

### データの出どころ・競合・限界

- 既存の人入力 FB は reported、集計は投稿観測。事例掲載や提案資料の存在だけなら、正式状態は unknown。
- manifest で official にできるのは、人が案件対応・状態の記載・原資料の版を確認し、その確認根拠も辿れるものだけ。推測を手入力して正本と呼ぶ運用にはしない。正本を確認できなければ、35 件であっても unknown のまま残す。
- 案件 ID が元にない資料は source 単位で分ける。人が確認した明示対応だけを manifest に持たせる。同じブランド・メニューの過去実績から新提案を上書きしない。
- 検索で見えた根拠の範囲を表示し、完全な案件履歴とは称さない。資料横断の一意な最新状態・履歴照会はこの案の限界。複数の正式記録が矛盾する場合は conflict にする。
- 全社資料の自動確定・全件人手棚卸しは含めない。まず 35 件の失敗類型と、同じ入力形式から作る匿名回帰 fixture を優先する。既存値を削除しないため、分類検索との互換性を保てる。

### テスト方法

共通の受け入れ試験（第 6 節）に加え、JSONB の欠落・不正型・旧版を unknown として扱うこと、再 ingest で確認済み入力が消えないこと、hash 不一致の manifest を別案件へ適用しないこと、同一入力で回答が同一になることを確認する。単体は fake repository／mock client、射影は mock cursor で行う。

## 5. 案 B：案件・状態イベント・根拠を専用台帳に分ける本格案

### 変更範囲

| 工程 | 対象と作業 |
|---|---|
| PR2：DB | 新規 migration で `cases`（安定案件 ID）、`case_document_links`（資料・施策の明示対応）、`case_state_events`（状態・時点・確認区分・訂正関係）、イベントの根拠対応を追加。既存 documents／chunks は原資料として使う |
| PR2：履歴 | イベントは追記・訂正履歴を残し、source identity＋元版＋根拠位置で冪等化。日付精度と実際の時点を保持する。現在状態の view は更新日時順ではなく、承認済みの時点・訂正関係からコード／SQL で解決し、競合は未解決として返す |
| PR2：権限 | 各イベントと案件対応にも RLS を設ける。可視資料への単純な FK だけで十分と考えない。根拠群・案件自体の権限を評価し、隠れた記録があることやその状態を返さない。company-shared export は全必要根拠が共有可能なものだけ |
| PR2：取り込み | `ingest/repository.py` と入力別 adapter を拡張。原資料更新・ACL 変更・削除／stale・訂正時は根拠を失効・再評価し、確認履歴は残す。正式 CRM が将来接続できれば、安定 ID と明示状態をここに流す |
| PR2：既存データ | source 単位の候補から始め、人が明示対応を承認して案件 ID に結ぶ。文字列名寄せで全件自動合流しない。読み取りの比較結果を見てから移行する |
| PR3：検索・回答 | 案 A と同じ全回答面・呼び出し側の修正に加え、検索後に対象案件の状態を本人の権限で台帳から一括取得する。top-k や本文量による dedup に依存せず、状態・時点・出典を取得できる |
| PR3：金庫 | 案件別の出力を持ち、取引先ページは複数案件を集約する。静的成果物へ台帳の版を載せ、出力済み状態と根拠を照合可能にする |

### データの出どころ・正本がない場合

専用テーブルを作っただけでは正本は生まれない。初期は案 A と同じ FB・資料・投稿観測・人の確認からイベントを作り、正式根拠がないものは reported／unknown にとどめる。CRM 接続はこの調査で存在・権限を確認していないため、前提にも工数にも入れない。

正式状態の管理責任者と訂正手順を決め、案件の分割・統合・再提案は明示イベントで扱う。過去受注と新規提案が併存する時も別案件／別期間として保持できる。状態の順序を単純な「提案 < 受注 < 完了」の大小比較にしない。

### テスト方法と限界

第 6 節に加え、冪等 upsert、同時更新、訂正・失効、月精度の期間重複、履歴の時点照会、RLS の可視性の組合せ、ACL 縮小後の状態・静的 export、migration の旧データ互換性を検証する。将来の実装時に disposable なローカル PostgreSQL で統合試験する。今回 DB は起動・接続していない。

負担は schema・移行・運用に及ぶ。安定した案件 ID を作る運用と正式データの整備が必要であり、DB を増やしても根拠不在を解決できない。将来、全件台帳・複数資料横断の現況・履歴を業務要件にするならこの案が適する。

## 6. テスト・受け入れ条件

### 最低限の回帰ケース

| ケース | 期待結果 |
|---|---|
| 提案書だけ／提案したという営業報告だけ | 受注・当社実績・実施済みとしない。資料種別または報告内容として表示 |
| `cls_phase=受注`、`topic=案件決定`、BANT A／C | 正式受注／失注を作らない。候補・報告と正式状態を区別 |
| 否定・修飾・未知フェーズ、口頭内示の成約区分 | 部分一致で受注に昇格しない。意味を定義していなければ未確認 |
| 過去投稿＋同じ取引先・商材の今年の提案 | 投稿観測と別案件の提案を分離。投稿数で今年の受注・完了を上書きしない |
| 同じ日付の受注と失注、入力順・ランキング順の入れ替え | 結果は一定。明示的な訂正がなければ競合 |
| 親投稿が古く、返信に新しい状態記載 | 返信の出典・日付を使う。親の日付と混ぜない |
| 提案年月、ファイル更新日、投稿年月、フォーム処理日の不一致 | それぞれの意味で表示。更新日・処理日を実施日や状態時点にしない |
| 年月だけ、不正日付、JST 日付境界、未来予定日 | 精度と timezone を守り、日付を補完しない。未来の予定から実施済みを作らない |
| 根拠なし、旧 metadata、検索 timeout、ACL 不可視 | 未確認／検索未完了。状態の推測や情報漏えいをしない |
| 複数根拠・複数案件・同じ事例集の複数ページ | 状態・数字・根拠日・リンクが同じ対象を指す。資料リンクだけの一致で合流しない |
| モデルが未確認案件を実績とする文や架空日付を返す | 自由文を採用せず、コードの値だけから表示。定型化できなければ未確認表示へ落とす |
| フラグ各設定、通常／fanout／compact／一覧／比較／訪問用／二段返し／金庫／カルテ／派生 skill | 同じ根拠束から同じ意味の状態・日付を返す。直接配信失敗時も未検証の再作文へ戻らない |

### 既存テストを拡張する場所

- 取り込み：`tests/ingest/test_slack_fb_parser.py`、`test_form_mappings.py`、`test_pipeline.py`、`test_pipeline_v2.py`、`test_ingest_differential.py`、`test_case_corpus_ingest.py`、`test_case_deck.py`、`test_campaign_aggregate.py`、`test_classify.py`。
- 検索・日付：`tests/skills/search/test_timeline_status.py`、`test_eval13_status.py`、`test_search_dates_exposure.py`、`test_dates.py`、`test_fanout.py`、`test_two_stage.py`、`test_case_list_drive_first_v2.py`、`test_visit_cases.py`、`test_compact_surface_guards.py`。既存の「投稿があれば実施済み」という期待は、投稿観測を維持しつつ正式完了と分ける期待へ更新する。
- 射影・型：`tests/adapters/test_pgvector_case_studies.py`、`test_pgvector_case_scope_metadata.py`、`tests/skills/test_tool_schema_contract.py`、既存 SQL mock テスト。
- 金庫・配信・利用側：`tests/scripts/test_export_vault.py`、`test_build_app_html.py`、`tests/mcp_gateway/test_direct_search_answer.py`、`test_direct_search_case_links_v2.py`、`tests/skills/clientkarte/test_clientkarte_skill.py` と会議準備・提案の対応テスト。

既存 fixture の state だけを追加して実装をなぞるテストではなく、上表の誤認が出口に出ないことを確認する。テストには架空の対象名・固定日付を使い、実 DM の顧客名や機密本文をコミットしない。

### 完了の判定

1. 35 件を匿名のケース ID で整理し、各件の期待する状態・根拠日・出典・正式性を人が確定する。根拠が足りなければ正解は unknown とする。
2. 匿名 fixture への置換後、各回答面で「提案→受注／実績」「投稿確認→全体完了」「資料更新日→実施日」の誤変換が 0 件であることを機械検査する。unknown の率は別に記録し、推測で埋めて改善扱いしない。
3. 状態を表す全表示に根拠束または未確認理由があり、出典 URL・対象・日付種別が一致することを確認する。複数記録の競合を人がレビューする。
4. 新仕様の旧行・エラー時の安全側フォールバックを確認してから、既存の段階公開手順で比較する。PR2 の型がない行でも PR3 は落ちず unknown を描画する。PR3 を戻す必要がある場合も、未検証の状態推測を復活させず、状態回答を定型の未確認へ戻す。

## 7. 工数・推奨・着手順

工数はコード調査に基づく概算、人日（1 人日＝開発者 1 名の実働 1 日）。設計、実装、レビュー対応、匿名 fixture とローカル検証を含む。AWS／DB の本番作業、CRM 接続、全件棚卸し、大規模 UI 改修、外部部署の回答待ちは含まない。

| 作業 | 案 A：小さい案 | 案 B：本格案 |
|---|---:|---:|
| PR2：型・入力・保存・射影／台帳と移行 | 3〜4 人日 | 10〜15 人日 |
| PR3：全回答経路・配信・金庫・派生利用側 | 4〜6 人日 | 6〜9 人日 |
| 回帰・権限・異常系・成果物の確認 | 2〜3 人日 | 5〜7 人日 |
| 開発計 | **9〜13 人日** | **21〜31 人日** |
| 別途、初期データ確認・35 件の根拠整理 | 営業・データ担当を含め 2〜4 人日 | 案件対応・移行確認を含め 4〜8 人日 |
| 初期合計の目安 | **11〜17 人日** | **25〜39 人日** |

案 A を推奨する理由は、正式な正本がない状況で台帳だけを増やすより、まず誤断定を止める効果が高いため。既存の状態判定・JSONB・直接配信・金庫生成を使い、必要な入力の来歴と全出口をそろえる。文章の自由度と状態回答の網羅性は下がりうるが、確認できない案件を未確認にすることを仕様とする。

着手順は、(1) 第 3 節の用語・正式性と匿名 35 件の期待値を決める、(2) PR2 で根拠束を投入・旧行互換・射影まで通す、(3) PR3 で全出口の描画と再作文防止をそろえる、(4) 第 6 節の回帰で公開範囲を広げる、の順。案 A の manifest に案件対応が大量に増える、完全な履歴照会が必要になる、正式台帳を接続できるようになる、のいずれかを案 B への移行判断とする。

未確定の業務事項は、正式受注・実施完了の根拠を誰が認定するか、口頭内示をどこまで正式状態とするか、事例の実施年月をどの資料で確認するかの 3 点。今回の結論はこれらが未決でも unknown で成立し、確認前に断定を増やさない。

## 8. 今回実施した検証と未実施事項

ソースの静的追跡に加え、標準ライブラリだけの一時的な Python 実行で `timeline_status.py` の必要な純関数・dataclass を AST から取り出し、匿名の入力で次の 6 シナリオを確認した。リポジトリのアプリケーションモジュールや接続 factory は import していない。

| 確認シナリオ | 現行挙動 |
|---|---|
| `cls_phase=受注` と日付だけ | 受注を含む状態文になる |
| `topic=案件決定` と日付だけ | 受注（案件決定）を含む状態文になる |
| 古い投稿集計＋新しい失注報告 | 投稿由来の実施済みが選ばれる |
| 同日の提案と失注の入力順反転 | 状態文が変わる |
| 資料名の年月と新しいファイル更新日 | timeline は更新日、`date_basis` は資料名日付を選ぶ |
| 資料名に年月のみ・ほかに日付なし | timeline の根拠日は不明になる |

6 シナリオは全て現行挙動の確認に成功した。将来案の正しさや、本番 DM でこの経路が有効だったことを証明するテストではない。既存テストの内容も読み、過去 FB の時点表現・別施策の分離・JST 境界等の保護があることを確認した（`tests/skills/search/test_timeline_status.py:53`、`tests/skills/search/test_timeline_status.py:76`、`tests/skills/search/test_timeline_status.py:97`）。

今回は調査・計画のみのため、pytest 全体、実サービス試験、本番設定・実データの確認は未実施。実装・migration・再取り込み・公開は行っていない。
