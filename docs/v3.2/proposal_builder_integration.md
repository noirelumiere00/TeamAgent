# proposal-builder → AiLa(MCP) 統合設計

## 1. 結論

既存の `proposal_deck`（FMT v2、95数値枠、coverage/self-repair、PPTX renderer）は
作り直さない。同期 `proposal_builder.run()` はPython互換用に残し、MCP面は
`proposal_builder_submit` / `proposal_builder_status` の2ツールにする。

```text
Slack / OpenClaw
  ├─ proposal_builder_submit(Gemini v3 JSON + 投稿開始日 D)
  │    ├─ DynamoDBへqueued rowを作成（local/testはprocess内dict）
  │    └─ MCP process内daemon threadを起動してjob_idを即返す
  │         ├─ strict検証、RAG/account選定、AiLa LLM、数値・引用検証
  │         ├─ 既存media workerでPPTX rendererだけを実行
  │         └─ readyだけ依頼元Slack threadへ添付（失敗時DM）→ done/failed
  └─ proposal_builder_status(job_id) → queued | running | done | failed
```

Composer/Bedrockを含むrun本体は、権限を持たないroleless media workerへ移さずMCP task内に残す。
running中はheartbeatで `updated_at` を更新し、thread起動前またはMCP再起動後に更新が止まったqueued/running rowはstatus照会時に
`failed / MCP_RESTARTED` へ条件付き更新する。

PowerPoint COMはランタイムから除く。前段14枚、検証2枚、2週間モデル、
D相対ガントの固定レイアウトを、案件作成前に1つの83枚統合FMTへ焼き込む。
Linux上でOOXML packageを毎回mergeする案も可能だが、slide relationship、master、
theme、media、埋め込みオブジェクトを安全に移植する実装が別途必要になり、
今回の「既存95枠へ差分追加」という範囲を超える。第一段は、Windows/PowerPointを
利用できる資産管理工程で一度だけ統合FMTを作り、ランタイムでは入力値の置換だけを行う。

統合FMT実体はrepoへ置かない。versioningとSSE-KMSを有効にした非公開S3へ置き、
S3 VersionId、byte size、SHA-256、KMS key ARNを固定する。起動時にtask-local `/tmp`
へ取得し、PowerPointが参照するslide relationship順・text body単位で、83枚、95 ID、
補助枠、版マーカー、D-56〜D+21の日付マーカーを検証する。

## 2. 調査対象と実測

外部配布物のrootを以下では `PB_ROOT` と表記する。

```text
/private/tmp/claude-163649916/-Users-s-komata-Claude-----/
1cd943b8-c96f-441f-89a1-e1d8bcd6a611/scratchpad/pb3/proposal-builder
```

- `PB_ROOT/SKILL.md`、全script、Gemini v3 prompt、Claude v2 promptを全文確認した。
- 外部FMTは146,602,093 bytes、65 slides、数値枠 `{1}`〜`{93}` と複合
  `{47,...,55}`。外部資料内には「1〜93」と「1〜103」の表記揺れがある
  （`PB_ROOT/scripts/flow_fill.py:3-7`、`PB_ROOT/prompts/②_Claudeプロジェクト用システムプロンプト_v2.md:13-17`）。
- AiLa側の正は、実装済みの `{1}`〜`{103}` から48〜55を除く95 IDである
  （`src/teamagent/skills/proposal_deck/contract.py:20-23`）。統合FMTもこの契約へ合わせる。
- 外部アカウントDBは145件、60,423 bytes。内容は読み取らず、JSON構造だけを確認した。
- 外部の完成枚数は83枚で、COMの追加処理はschedule差替、gantt 1枚、
  検証2枚、前段14枚だけである（`PB_ROOT/scripts/assemble.ps1:23-34`）。

## 3. 機能対応表

判定は「既存で足りる / 移植が要る / 作り直しが要る / 不要」の4種。
「作り直し」は周辺のWindowsローカル資産搬送や静的DB方式を置き換える意味であり、
`proposal_deck` 本体を作り直す意味ではない。

| 機能 | 判定 | 根拠と統合方針 |
|---|---|---|
| Gemini v3 JSON＋D入力 | 移植が要る | 外部入力はJSONとDの2つ（`PB_ROOT/SKILL.md:27-30`）、wire schemaは `PB_ROOT/prompts/プロンプト改訂_Gemini_v3.md:40-65`。既存deck入力は商材・目的・自然文素材（`src/teamagent/skills/proposal_deck/schema.py:34-58`）。新規strict A-H schemaとMCP入力は `src/teamagent/skills/proposal_builder/schema.py:180-344`。A〜Eは最低1件、Fだけ空を許し、F空なら{41}/{42}を強制skipする（`proposal_builder/skill.py:442-467`）。 |
| JSON構文修復 | 移植が要る | 外部は内容を創作せず構文だけ修復（`PB_ROOT/SKILL.md:39-41`）。新規実装はcode fence、構造上の空値、末尾commaだけを扱い、欠落fieldはschema errorにする（`src/teamagent/skills/proposal_builder/research.py:20-140`）。 |
| 95枠本文のLLM生成 | 既存で足りる | 既存がBedrock呼出し、Pydantic検証、最大N回self-repairを持つ（`src/teamagent/skills/proposal_deck/skill.py:311-444`）。builderは用途別model IDとv2 promptで同じSkillを再利用する（`src/teamagent/skills/proposal_builder/skill.py:261-287`）。 |
| 青木版の執筆・規制・守秘ルール | 移植が要る | 外部ルールは `PB_ROOT/prompts/②_Claudeプロジェクト用システムプロンプト_v2.md:13-39`。AiLaの95-ID契約・信頼境界・citation JSONに翻訳したpromptは `src/teamagent/prompts/proposal_deck/v2/system.md:1-78`。 |
| 95 ID / `{47}`複合枠 / coverage | 既存で足りる | 95 ID定義は `src/teamagent/skills/proposal_deck/contract.py:20-23`、filled/skipの完全被覆とcoverageは同:174-200。外部の47〜55結合は `PB_ROOT/scripts/flow_fill.py:52-60`。AiLaでは既存契約どおり `{47}` に9案をまとめる。 |
| 段落・group・tableを含むスロット充填 | 既存で足りる | 既存rendererがtext-frame連結、group/table再帰、段落跨ぎ置換を実装済み（`src/teamagent/skills/proposal_deck/renderer.py:92-109,138-176`）。 |
| 未検証数値の抑止 | 移植が要る | 外部最重要規則は `PB_ROOT/SKILL.md:45-47`。新規は回・本・日間・秒・代・会場・漢数字等も含むunit付き数量を検知（`proposal_deck/provenance.py:27-49,111-141`）、同一JSON objectにURLがない値を置換（`proposal_builder/research.py:247-302`）、残したmetricとobject URLを対応付け（同:305-337）、Composer出力の「入力中の同じmetric・同じsource URL・同じplaceholder ID」を検査する（`proposal_deck/provenance.py:144-213`）。違反はself-repair、未解決はdraft。 |
| 守秘商材名の非表示 | 移植が要る | 外部規則は `PB_ROOT/SKILL.md:50-51`。builderはNFKC/case-insensitiveでnested research、案件与件、カテゴリ語、case/account補助枠を伏せ、brand入りURLを証拠集合と返却値から外す（`proposal_builder/skill.py:57-117,338-447,548-568`）。Composer本文/citation/skip/evidenceも同じ正規化規則でrepair対象にする（`proposal_deck/skill.py:369-420`）。 |
| アカウント上位5件の選定 | 移植が要る | 外部式はカテゴリ一致×2＋説明keyword一致×1（`PB_ROOT/scripts/アカウントセレクタ.py:31-49`）。同じ式とstable tie-breakを `proposal_builder/selectors.py:138-177` に実装した。死活/直近投稿は外部でも将来課題（外部同:90）なのでready warningを残す。 |
| アカウントDBの保管・配布 | 作り直しが要る | 外部はWindowsローカルpath（外部同:16-17）、DBは改変禁止（`PB_ROOT/SKILL.md:17`）。新規は固定version S3→0600一時file、size/hash/checksum/SSE-KMS/key一致を検証する（`src/teamagent/adapters/proposal_assets.py:70-105,183-239,315-404`）。repo/Secretsへ入れない。 |
| 静的事例DBとtag score | 作り直しが要る | 外部scoreと売上/指名検索加点は `PB_ROOT/scripts/事例セレクタ.py:41-104`。正本更新元はDrive `03_レポート` とSlack `#general_news-tv`（`PB_ROOT/SKILL.md:84-102`）。新規は共有SearchSkillで両系統を検索し、売上/指名検索を優先、source/channel metadataをpost-filter、low-confidence・分類不明を除外しURL dedupeする（`proposal_builder/selectors.py:244-357`）。規制案件は検索語へ「薬機・景表規制」「検証型」を足す（`proposal_builder/skill.py:165-191`）。 |
| 事例の社名・実数マスク | 移植が要る | 外部更新規則は社名→業種、実数→range/達成率/倍率（`PB_ROOT/SKILL.md:96-101`）。ingest分類がfail-openでもraw chunkを外部資料へ流さないよう、第一段はindustryとclient identityが構造化済みのhitだけを採用し、本文は既知ラベルの存在から作るmetadata-only投影にする（`proposal_builder/selectors.py:218-302`）。高品質なsource-backed range化は第二段。 |
| 事例・アカウントslide | 移植が要る | 外部はA-1と事例slideを生成（`PB_ROOT/scripts/flow_fill.py:62-88,190-230`）。統合FMTへ `{{PB-ACCOUNTS}}` / `{{PB-CASES}}` を事前配置し、LLMを介さず注入する（`proposal_builder/skill.py:335-355`、`proposal_deck/renderer.py:247-288`）。 |
| D起点schedule/gantt | 作り直しが要る | 外部12工程はD-56〜D+21（`PB_ROOT/scripts/schedule_gen.py:17-36,45-46,70-99`）。図形は統合FMTへ固定し、週次軸の日付だけ `{{PB-DATE:offset:format}}` で置換する。rendererは必要offsetを検査する（`proposal_deck/renderer.py:33-42,270-288,427-443`）。 |
| PowerPoint COM組立 | 不要 | COM処理は静的slide挿入だけ（`PB_ROOT/scripts/assemble.ps1:23-34`）。83枚統合FMTへ事前焼込みし、起動時はpresentation relationship順・text-body単位、描画時はpython-pptxのtext-frame単位で同じ版/inventoryを検査する（`proposal_assets.py:334-477`、`proposal_deck/renderer.py:404-443`）。ランタイムでCOM/PowerPointは呼ばない。 |
| URL screenshot | 不要 | 外部はWindows Chrome path固定（`PB_ROOT/scripts/screenshot_pipeline.py:10-20`）。第一段では本文・事例・account・日付を価値範囲とし、SNS captureは人手または既存media系の後段に分離する。既存rendererには取得済み画像注入がある（`proposal_deck/renderer.py:289-381`）。 |
| 残留token・FMT監査 | 既存で足りる | 既存auditを再利用し、builder profileだけ83枚、95 ID exact、補助枠、版marker、全週次date offsetへ強化した（`proposal_deck/renderer.py:384-464,474-503`）。外部の非致命checker（`PB_ROOT/scripts/build_proposal.py:140-165`）よりfail-closed。 |
| 143MB超PPTXのmedia搬送 | 作り直しが要る | 一般mediaの128MiB境界は維持し、`proposal_pptx` のtemplate/outputだけ256MiBへ拡張した（`src/teamagent/media/contracts.py:29-35,340-368`、`media/tool_contracts.py:455-462`、`infra/terraform/lambda/tiktok_dispatch/handler.py:58-60,341-345,1068-1073,1943-1961`）。 |
| Slack tool公開と添付 | 移植が要る | factoryは `proposal_builder_submit/status` を同じjob storeへ接続し、OpenClawはjob_idをpollする。同期runtime attestationは廃止し、重いrunはMCP内threadで継続する。Slack clientはbuilderだけ明示timeoutを持ち、readyはverified callerのthread→本人DM、両方失敗かつURLなしは成功扱いにしない。 |
| 生成後の会話微調整 | 不要 | 外部では主用途後半（`PB_ROOT/SKILL.md:79-80`）だが、今回の第一段はSlack一回生成が受入範囲。再生成は新しいGemini/与件/制約で別versionを作る。差分編集UIは後続段階。 |

## 4. 統合FMT契約とCOM解消

### 4.1 統合FMTを作る工程

資産管理者がrepo外の作業領域で、既存AiLa FMT v2を基準に以下を一度だけ行う。

1. 外部の前段14枚、検証2枚、2週間モデル、D相対ganttのレイアウトを挿入する。
2. 事例slideとaccount slideを固定し、本文を
   `{{PB-CASES}}` / `{{PB-ACCOUNTS}}` にする。
3. schedule/ganttの週ラベルを最低でも
   `-56,-49,-42,-35,-28,-21,-14,-7,0,7,14,21` の
   `{{PB-DATE:offset:%m/%d}}` にする。barの相対位置と工程名は固定する。
4. 非表示の版marker `{{PB-TEMPLATE:proposal-builder-v1}}` を1個置く。
5. 95数値IDがexact、総枚数83であることを確認する。
6. versioning/SSE-KMS有効の非公開S3へ、full-object SHA-256 checksum付きで配置する。
7. VersionId、size、SHA-256、KMS key ARNをデプロイ設定へ渡す。

起動時validatorは `presentation.xml` のslide relationshipを解決して、PowerPointが実際に
開く83枚だけを順に検査する。placeholderはshapeをまたいで誤結合せずtext bodyごとに集計し、
local/media rendererのtext-frame境界と揃える。したがって、
外部の93枠FMT、前段未挿入FMT、日付marker不足、別版FMTはtool公開前または描画前に失敗する。

### 4.2 他案

| 案 | 評価 |
|---|---|
| 統合FMTを事前作成 | 推奨。実行時にCOM不要、slide順・themeを人が確認でき、ランタイムは置換だけ。 |
| LinuxでOOXML slide merge | 将来候補。自動化はできるがrelationship/master/theme/mediaの移植と破損検査が必要。第一段には過大。 |
| LibreOffice UNOで実行時merge | 非推奨。Officeとの差分、font/layout drift、巨大fileの起動時間を新たな本番依存にする。 |
| Windows sidecar/remote COM | 非推奨。Fargateの単純な境界を崩し、Windows worker、queue、認証、監査が必要。 |

## 5. 数値を断定させない実装

promptだけでは担保しない。次のfail-closed層を通す。

1. Gemini v3 schemaのA〜Eは最低1件を要求し、evidence URLは具体的なHTTP(S) URLだけを
   受理する。Fだけは空を許すが、{41}/{42}を強制skipして必ずdraftにする。
2. 入力JSONのunit付き数量（%、金額、件数に加えて回・本・期間・秒・年代・会場・
   漢数字等）は、最も近い同一objectにURLがなければ
   `要確認（出典URL未取得）` へ置換し、issueを残す。
3. 残したmetricの表記と、そのobjectのURLをmapにする。
4. LLMには「入力の表記を変えない」「同じIDへcitationを付ける」と指示する。
5. 出力citationが入力中のURLと完全一致することを確認する。
6. metricが入力mapに存在し、同じplaceholder IDのcitationがそのmetricのobject URLと
   交差することを確認する。別objectのURLによる根拠ロンダリングも失敗にする。
7. ユーザー指定のターゲット/運用制約に完全一致する計画数量だけは別集合にし、同じID内に
   「計画・予定・制作・期間」等の前向き文脈がある場合だけcitation不要とする。
   実績値への転用や、入力にない計画値の創作は失敗にする。
8. 失敗は既存self-repairへ戻す。修復不能/skip/入力issueがあればstatusはdraft。
9. LLMを通らないaccount説明はsourceなしmetricを伏せる。RAG caseはraw chunkを
   出力せず、分類済みmetadataと安全ラベルの存在だけから外部向け文面を作る。
10. 守秘語はNFKC/case-insensitive照合に加え、URLのpercent decode（最大2回）と
    IDNA host decode後も監査する。該当URLはcitation/補助枠/MCP返却から除く。
11. draftは既定でSlackへ添付しない。明示的な社内draft配信を有効にした場合だけ
   `DRAFT_裏取り前_` と外部提出禁止commentを付ける。

この検査は「URLがそのmetricを意味的に本当に裏付けるか」まではネットワーク検証しない。
Gemini v3がURL実在/内容確認済みであるという入力契約と、RAG正本の品質を前提にする。
第二段でsource本文とclaimの照合精度を評価する。

## 6. 事例DB: 静的JSONか既存RAGか

推奨は既存RAGである。

| 観点 | 静的 `事例DB.json` | 既存RAG |
|---|---|---|
| 鮮度 | 更新scriptを人が都度実行 | ingest済み正本を検索時に参照 |
| 二重管理 | Drive/SlackとJSONの二重化 | 正本を一箇所に保つ |
| source trace | `source` label中心 | Drive URL / Slack source URIまで追跡可能 |
| 選定 | tag/reachの決定論score | semantic retrieval＋metadata filter |
| 外部向けmask | curated済みで強い | raw chunkなので投影/mask層が必須 |
| 障害時 | localなら利用可能 | Search/RAG障害の影響を受ける |

第一段はRAGを使い、低信頼hit、source不明hit、industry/client identity未分類hitを落とし、
raw本文をそのまま出さない。売上/指名検索の記載を決定論的にrerankし、規制案件では
薬機・景表/検証型の語を検索へ追加する。
Driveは現行 `SearchInput` にfolder ID filterがないため `filter_doc_type="報告書"`、
Slackはchannel IDが設定されればそのIDだけ、未設定時だけ`channel_name`一致でpost-filterする。
この制約があるため、RAG失敗/0件は架空事例へfallbackせずdraftにする。

静的JSONは本経路の正本にしない。必要なら障害時rollback用の「生成日時・source cursor・hashを
持つcurated snapshot」として非公開S3へ置く余地はあるが、repoには置かず、
通常時の検索経路とは分ける。

## 7. アカウントDBの置き場所

versioning＋SSE-KMS有効の非公開S3を推奨する。Secrets Managerには置かない。

- 実測60,423 bytesでSecrets Managerの64KiB上限に近く、更新で超過しやすい。
- secretというより版管理・選定・監査を要する保護データセットである。
- S3はVersionId、full-object SHA-256、size、checksumでimmutable pinを作れる。
- task roleは指定2 objectの `s3:GetObjectVersion` と指定KMS keyの
  `kms:Decrypt` だけに絞る（`infra/terraform/fargate.tf:356-383`）。
- 起動時にSSE-KMS key ARNまで完全一致を確認し、0600のtask-local fileにする。
- raw recordをログ、tool output、errorへ含めない。tool outputは選定名だけに限定する。
- 更新は元DBのownerが行い、新VersionId/hash/sizeを人間review後に設定する。

## 8. モジュール配置と既存再利用

| 配置 | 責務 |
|---|---|
| `skills/proposal_builder/schema.py` | Gemini v3 strict schema、MCP I/O |
| `skills/proposal_builder/research.py` | 構文限定修復、URL registry、入力metric sanitize |
| `skills/proposal_builder/selectors.py` | account exact score、RAG case検索/安全投影/dedupe |
| `skills/proposal_builder/skill.py` | 全体orchestration、ready/draft、Slack配送 |
| `adapters/proposal_job_store.py` | DynamoDB job row、process内fallback、状態遷移CAS |
| `prompts/proposal_deck/v2/system.md` | 青木版ルールをAiLa 95-ID/citation契約へ翻訳 |
| `skills/proposal_deck/provenance.py` | Composer出力のmetric/citation検査 |
| `skills/proposal_deck/renderer.py` | 既存数値枠＋補助枠＋D相対日付＋統合FMT監査 |
| `adapters/proposal_assets.py` | S3固定version assetの起動時provision |
| `media/*` | proposal PPTXだけ256MiB、隔離rendererへ委譲 |
| `orchestrator/factory.py` / `infra/openclaw/*` | opt-in tool公開とSlack routing |

再利用するものは `SearchSkill`、`ProposalDeckSkill`、`ComposerOutput`、
95枠renderer/coverage、Bedrock adapter、media job、Slack adapter、MCP caller metadata。
新しいslot filler、独立PowerPoint generator、別RAG clientは作らない。

## 9. 段階計画と受け入れ条件

### 第一段: Slack非同期job MVP（COM解消を含む）

実施:

- repo外で83枚統合FMTを作り、S3 fixed versionにする。
- account DBを同じ保護方式でS3へ置く。
- `proposal_builder` gateを有効化する前にasset pin、channel metadata、Terraformによる専用DynamoDB tableの作成とtaskへの注入を確認する。
- submitでjob_idを即返し、statusでqueued/running/done/failedを照会する。
- background threadでGemini v3＋D→RAG/account→95枠→PPTX→Slackを一回で通す。
- screenshotは対象外、draftは外部配送しない。
- queued/runningの更新がstale閾値を超えた場合は `MCP_RESTARTED` でfail-closeする。job rowはTTLで7日後に削除する。

受け入れ条件:

- Linux taskにPowerPoint/COM/Windows commandがなくても生成できる。
- wrong version、82/84枚、95 ID不足/余剰、補助枠不足、必要date offset不足のFMTは起動/描画を拒否する。
- 正常FMTから83枚のPPTXが生成され、数値95 IDと全 `PB-*` tokenが残らない。
- sourceなしmetric、入力にないmetric、別object URL、同ID citationなしはreadyにならない。
- confidential=trueで元brandが本文/citation/補助枠へ残らない。
- case/accountが選定され、raw account DBとraw RAG chunkはtool output/logへ出ない。
- job `status=done` かつ `proposal_status=ready` の既定経路はverified callerのSlack threadまたは本人DMへ添付される。
  両方失敗し代替URLもなければtool successにしない。
- submit自体はMCP timeout内にjob row作成とthread起動だけを終え、生成時間に依存しない。
- stale判定とdone更新が競合しても、failed rowがdoneへ復活しない。
- draftは既定で添付されず、理由が `verification_issues` に出る。
- PPTX、account DB、静的事例DBはrepo/imageへ含まれない。

### 第二段: 事例品質・source精度

実施:

- `SearchInput` にDrive folder ID / Slack channel IDのserver-side exact filterを追加する。
- RAG正本からindustry、施策、masked result、win pattern、priority metricを
  source URL付きの構造へ投影する。
- 売上/指名検索を優先する外部selectorの意図を、RAG rerank featureとして評価する。
- accountの直近投稿/死活を保護された別sourceで検査し、raw DBは改変しない。

受け入れ条件:

- 採用caseは必ず `03_レポート` または設定済みchannel IDへtraceできる。
- 社名、実数、内部Slack URIを外部向けslideへ出さない。
- masked resultの各metricがsource object URLへ結び付く。
- RAGとcurated静的snapshotの固定評価集合で、採用上位3件の妥当性を人間評価する。

### 第三段: 画像と運用性能の継続改善

実施:

- 許可URLのcaptureを既存media workerへ委譲し、login wall/PDF失敗は非致命にする。
- 既存campaign feederのthumbnail/evidence imageを必要枠へ接続する。
- 143MB超PPTXのrender/upload時間、memory、Slack上限、MCP timeoutを継続計測する。

受け入れ条件:

- core MCP taskからbrowser/network captureを行わない。
- 画像失敗でも本文PPTXは失わず、warningに対象URLを安全に記録する。
- 代表サイズのPPTXが設定timeout/memory内で生成・添付できる。

### 第四段: 会話微調整と版管理

実施:

- 元Gemini JSON、D、制約、選定source、Composer versionを版metadataとして保存する。
- Slackの修正指示を構造化patchへ変換し、新versionとして再生成する。

受け入れ条件:

- 元PPTXを破壊的上書きせず、新旧versionと変更理由を追跡できる。
- 修正後も第一段のcoverage/provenance/confidentiality gateを全て再実行する。

## 10. 未実測・仮定

- 83枚統合FMTの実ファイルは未作成。外部143MB資産をrepoへcopyしない制約と、
  slide資産の最終組合せをownerが目視承認する必要があるため。
- Drive ingest metadataに外部folder IDが保持されているかは未実測。現行schemaにfolder exact
  filterが見当たらないため、第一段はdoc type filterとした。
- `#general_news-tv` のIDは外部文書に記載があるが、現行ingest `source_uri` の値と
  実データ照合していないため、設定値として注入し、未設定時だけchannel nameへfallbackする。
- 146MB超の最終PPTXをSlackへ添付できる上限、upload時間、Slack SDK timeout、
  media worker memoryは未実測。生成全体は非同期化済みだが、各下流I/O固有のtimeoutは引き続き計測する。
- MCP task再起動でprocess内threadは失われる。DynamoDB上のqueued/running rowはheartbeat停止後、
  status照会が `MCP_RESTARTED` へfail-closeし、自動再実行はしない。
- Gemini v3 URLが実在し、内容がclaimを意味的に裏付けることは入力側の契約。
  本実装はネットワークでURL本文を再取得していない。
- 統合FMTのfull-object S3 checksum、VersionId、KMS key、sizeは未設定。
  AWS操作・配置・deployは本作業では行っていない。
- 95枠を16,000 output tokensで安定生成できるmodel/latencyは実データ未実走。
  `PROPOSAL_BUILDER_MODEL_ID` は暗黙選択せず、受入環境で明示する。
- 作業treeの共通Git metadataがworkspace外のread-only領域にあり、指定された
  `feat/proposal-builder-integration` branchはこの実装セッションでは作成できていない。
  差分は`dev`上の未commit変更として保持している。
