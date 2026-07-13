# TeamAgent Next — 要件定義書（再構築）

> 現状 v3.x の「機能がごちゃついて構成がやりづらい」を構造から治し、**ごちゃつき再発防止を仕組みに組み込んだ**営業支援エージェントへ作り直すための要件定義。
> 作成: 2026-06-05 ／ 対象: PR×ショート動画の社内営業支援（営業16名・同時AI利用4名）／ 現状リポジトリ: `~/Documents/TeamAgent`(main `0cbd570`)
> 根拠: 現状コードの実査（8エージェント診断）＋ OpenClaw 一次ソース検証。**本書は意思決定用ドラフト**（§9 の未決事項を確認のうえ確定）。

---

## 0. エグゼクティブサマリ

**一言で:** スキル追加が「**1ディレクトリ・1 manifest の追加だけ**」で済む宣言レジストリを唯一の真実源にし、**単一プロセスのまま god-file を解体**して、日常は決定的な**規約ルータ(L1)**・横断や曖昧時だけ既存の**Agent SDK オーケストレータ(L2)**に委ねる。専任ほぼ1名／16名利用／同時4で**現実に保守できる**構成にする。

**「OpenClaw を主体に」への回答（2026-06-09 改訂・決定事項＝不変）:** **採用**。OpenClaw を「自律オーケストレーションの**外殻**(L0=ingress / cron 自発起動 / 会話記憶 / Claude#1)」として前段に置く。**既存資産(12スキル/RLS/per-user OAuth/テスト)は作り直さず**、OpenClaw からは **MCP境界(streamable-http)越しにしか触らせない**。OpenClaw の重大リスク（§A 一次調査で 2026-05-28 の GHSA 30+件=Critical2/High多数を自分で確認）は **Docker隔離＋専用最小IAM＋営業データ非接触＋WS-C anti-spoof で構造的に封じ込める**（＝本書 設計原則#5「fail-closed をランタイム強制・外部供給は審査ゲート」の実装そのもの）。境界の内側は **Claude Agent SDK on Bedrock(L2)** を継続。詳細は §7（改訂版）。

**「ごちゃつき」の正体（最重要）:** 良い抽象(`BaseSkill`/`SkillRegistry`/`@register`)が**存在するのに本番経路で使われていない**こと。本番Slack経路はそれをバイパスして、スキル名を `intent.py`(366行正規表現)/`dispatch_auto`(if連鎖)/`_ACK_BY_SKILL`/各 `skill.name` の**4箇所に手書き重複**し、`slack_bot.py`(2,186行=god-file)に配線が集中、env knob が66個に散乱している。**スキル1個の追加に5〜6ファイルの同期編集**を強いる水平スライス構造が、ユーザーの不満の主因。Next は「抽象を作る」のではなく「**既にある抽象を唯一の登録点に昇格させる**」ことで治す。

---

## 1. なぜ作り直すか — 現状の構造的負債（診断）

> 8エージェントが現状コードを実査して特定。すべて file 根拠あり。重大度順。

| # | 領域 | 問題（やりづらさ） | 根本原因 | 重大度 |
|---|---|---|---|:--:|
| 1 | **抽象の二重構造** | `BaseSkill`/`SkillRegistry`/`@register` が整備され全スキルが登録済みなのに、本番Slack経路は使わず `SkillDispatcher` が `get_X_skill()`+`run_X()` 手書き対を12個並べ、`dispatch_auto` が intent文字列 `==` の if連鎖(9分岐)で呼ぶ。registry は CLI/orchestrator だけが使い live では死蔵。env解決ロジックが `get_search_skill` と `factory._build_search_skill` に丸ごと重複。 | registry の `get(name)()` 一律生成に各スキルの `__init__` 引数(embedder注入/prompt_version)が乗らず、手書き生成に逃げた。抽象を捨てず迂回したため二重化。 | **高** |
| 2 | **スキル追加の作業点過多** | 1スキル追加に最低5〜6箇所を手で同期: ①intent.py のトリガ+抽出正規表現+分岐、②`SkillIntent` dataclass のスキル固有フィールド(全部入りで肥大)、③`get_X_skill()`+`run_X()`、④`dispatch_auto` の if分岐+ヘッダ整形、⑤`_ACK_BY_SKILL`、⑥slashハンドラ。スキル名文字列が**4箇所に手書き重複**、中央Enum不在。 | スキルのメタ情報(トリガ/受付文言/書式/生成法/パラメータ)が1箇所に集約されず横断ファイルへ散る水平スライス構造。 | **高** |
| 3 | **命名・粒度の不統一** | dir/クラス`name`/intent文字列の**3軸がズレ**。`video/`=`video_analysis`、`video_algorithm/`、`video_approval/`、`proposal/`=`proposal_draft`。VSEO が**2箇所に割れ**(`vseo/` は `skill.py` を持たない野良ヘルパ群、実体は `video_algorithm/`)。 | 命名規約を決めずその場の語感でdirを切り、VSEOの前処理と分析を別タイミングで作り統合しなかった。 | **高** |
| 4 | **god-file 肥大** | `slack_bot.py` が**2,186行**。Skill実行+表示整形(`format_search_response`等)+Slackイベント配線+5本のslashハンドラ+記録+並行制御が同居。`detect_skill` が1メッセージで**4回再評価**(ack/dispatch/mention記録/message)。user_email→user_groups の同一ブロックが**4回コピペ**(RLS事故の温床)。 | runtime層を更に分割せず1ファイルに積層。横断的関心(RLS解決/ヘッダ整形/ack)を共通化せずインライン展開。 | **高** |
| 5 | **正規表現ルータの肥大** | `intent.py` が純正規表現の巨大ヒューリスティック。優先順位依存の10段if連鎖、各分岐に「○○より先に判定」コメントが散在。スキルごとにトリガ用と抽出用で正規表現が2本ずつ。 | スキル増のたび衝突を正規表現の順序と除外条件で手当てし、グローバルな順序依存が蓄積。意図分類とパラメータ抽出が同居。 | **高** |
| 6 | **到達不能スキル / 経路で集合がズレる** | `@register` 済みでも `mail_constraints`/`workspace_search`/`proposal_deck` は `intent.py` に分岐ゼロで**Slack自然文から起動不能**。orchestrator(registry)とSlack(if連鎖)で対応スキル集合が食い違う。 | スキル登録の真実源がorchestrator経路とSkillDispatcher経路に分裂。 | **高** |
| 7 | **二重ルータ** | スキル選択の `intent.detect_skill` と検索戦略の `router.SkillRouter`(QueryType) が別物として直列。後者は「Sprint2でHaiku化」コメントのまま約1年塩漬け、`compare`/`meta` は判定だけで実装は `content` と同じ死にパス。 | 段階移行前提で2レイヤを切り、LLM移行未完のままルールベースを増築。 | 中 |
| 8 | **env knob 乱立** | env由来フラグが**66個**(distinct)。`slack_bot.py` 単体で `os.environ.get` 37回。検索生成だけで8〜10個を個別parse、`*_PROMPT_VERSION` が9種。同じフラグ解決が2箇所に二重実装(既定値ドリフト源)。A/B実験ノブ(v1/v2c/v2d)が本番に恒久ifとして残留。 | 全切替をenvフラグで行い、決着後も恒久化せずノブを残した。Settings集約層が無い。 | 中 |

**一言診断:** 「**抽象はあるが使われていない**」典型的な構造的負債。良い契約(`BaseSkill`)を唯一の登録点に昇格させ、配線を「manifest を読むだけ」に畳めば、上記の大半は構造的に消える。

---

## 2. 設計原則（Next の不変条件）

1. **宣言レジストリを唯一の真実源に（Convention over Configuration）** — `BaseSkill` を1段拡張した `CapabilityManifest`(triggers/ack_template/cost_class/permission_scope/handler_factory/eval_fixtures を宣言)を**唯一の登録点**に。ルータ・ディスパッチャ・ack生成・SDKの@tool化の4経路すべてが「manifest を読むだけ」になる。`intent.py` の手書き分岐を**ゼロ**にすることが、ごちゃつき再発防止の構造的担保。
2. **ルーティングを2層に分離（決定的 L1 / 自律 L2）** — 日常の単発リクエストは manifest の triggers を読む**決定的・低コスト・低レイテンシ**な規約ルータ(L1)。横断・曖昧・明示マルチステップのみ Agent SDK オーケストレータ(L2)へ委譲。スキル増の編集コストを **O(スキル数)→O(1)** にする。
3. **プロセス内モジュラリティを堅牢性の手段とする（単一プロセス維持・god-file解体）** — マイクロサービス化はしない(分散の堅牢性低下を避ける)。`runtime/skills/adapters` の3層境界を **import-linter でCI強制**し、依存の向きを常に内向き(技術→ドメイン)に固定。堅牢性は「プロセス数を増やす」ではなく「**1プロセス内の結合度を下げ障害波及を断つ**」で達成。
4. **境界で型を固定し、内側は純粋関数に（Pydantic I/O契約 + 副作用の外出し）** — スキルの `run()` は検証済み入力 + `SkillContext`(request_id伝播) だけを受け、外部I/Oは adapter 注入。表示整形は Slackアダプタ側の `ResponsePresenter` へ退避し、スキルは構造化出力のみ返す。
5. **信頼境界を fail-closed で強制し、外部Skill供給は自前審査ゲートを通す** — データアクセス系は**既定OFF・明示opt-in**。RLS行権限・per-user OAuth(本人トークン・KMS暗号化)を**ランタイムが強制**(実装の善意に依存しない)。外部由来ツール/MCPは**版pin＋権限宣言レビュー＋署名検証**を通さねば registry 登録不可（ClawHavoc型インシデント耐性）。
6. **反ハルシネーションを出力契約に組み込む** — 検索系は出典(`file_name`+`page_num`)を構造化フィールドで**必須化**、根拠不在時は断定せず「該当なし」を返す **fail-honest**。`faithfulness` 忠実性評価を生成系の受け入れ基準に。架空の confidence をログに出さず実測値(retrieval_similarity等)のみ記録。

---

## 3. ケイパビリティ・ドメイン（7）

> 散らばった12スキルを、意味のある **7ドメイン** に束ね直す。1ドメイン=1パッケージ=1 manifest。

| ドメイン | 目的 | 現行スキルのマップ |
|---|---|---|
| **1. Knowledge & Memory（会社の脳）** | 散在資料を単一の記憶層にし、自然文の問いに権限制御つきで答える基盤。全ドメインが参照。 | `search`、`router.py`(QueryType戦略=このドメイン内部サブルータへ降格し二重ルータ解消) |
| **2. Deal Intelligence（案件・顧客）** | 顧客/案件単位の状態(温度感/履歴/次アクション/BANT)を合成し「今どうか／次に何を」を提示。 | `clientkarte`、`operation_log`（将来 `deal_radar`/`morning_brief`） |
| **3. Proposal Studio（提案）** | 勝ち筋を再利用して提案を**作り→磨き→PPTX成果物化**する連続フロー。 | `proposal_draft`、`proposal_review`、`proposal_deck`、research を **4 Action に統一** |
| **4. Video Intelligence（動画）** | 競合分析・VSEO読み解き・納品審査を**一つのマルチモーダル動画能力**に束ねる（現状最大の重複源）。 | `video_analysis`/`video_algorithm`/`video_approval` を統一、野良 `vseo/` をドメイン内へ吸収 |
| **5. Trend & Market Discovery（外部リサーチ）** | 社外のショート動画市場を課金SaaSなしでローカル収集。社内RAGとは別レイヤの外部探索。 | `tiktok_search`（将来 Reel/Shorts 拡張・定点モニタ） |
| **6. Personal Workspace Bridge（PII境界）** | 営業個人のGoogle(メール/カレンダー/連絡先)を本人OAuthで安全取り込み。PII死守ラインを一手に担う境界。 | `mail_constraints`、`workspace_search`（到達不能だったのを正規経路へ・既定OFF維持） |
| **7. Conversation & Orchestration（対話・統括）** | 自然な対話の入口。意図判定→各ドメインへ振り分け、雑談は検索せず応答。肥大した `detect_skill` を L1規約ルータ＋L2委譲へ置換。 | `chitchat`、`intent.py`、`orchestrator/`(Agent SDK POC) |

**統合で消える冗長:** VSEO の2箇所割れ（`vseo/` 野良ヘルパ → Video へ吸収）、proposal の粒度ズレ（dir=`proposal`/name=`proposal_draft` → 4 Action）、二重ルータ（検索戦略を Knowledge 内部へ降格）。

---

## 4. 機能要件（FR・ドメイン別）

**1. Knowledge & Memory**
- 自然文の横断RAG検索（pgvector dense + Cohere Rerank + RLS行権限 + v2d教示）
- クエリ種別の内部サブルータ（meta集計／conditional絞込／compare比較／content意味検索）をドメイン内に閉じ込め、二重ルータ解消
- 業界・顧客・予算等のメタフィルタと集計（何件／一覧）への回答
- 出典(file_name+page_num)を構造化フィールドで**必須化**、根拠不在は「該当なし」の fail-honest
- （将来）会社の脳の鮮度・取込状況の可視化、勝ちパターン／失注理由の横断ナレッジ化

**2. Deal Intelligence**
- クライアントカルテ合成（提案履歴・温度感推移・推奨ネクストアクション）
- Slack商談会話のCRM構造化（フェーズ/アクション/次ステップ/BANT）
- RLS担当者スコープの一貫適用
- （将来）停滞案件アラート `deal_radar`、朝ブリーフィング `morning_brief`、定点モニタ

**3. Proposal Studio**
- 新規ブリーフからの提案ドラフト生成（類似過去提案を Knowledge.retrieve 共有注入で再利用）
- 提案の診断（勝ちパターン／失注理由と照合した"コードレビュー的"FB）
- 提案書FMT v2(95項目)を埋めた**PPTX自動生成**（`proposal_deck`）
- 研究素材(過去事例/Slack/Mail/Web)取込・self-repair による契約充足
- draft/review/deck/research を **1ドメイン4 Action** に統一、`faithfulness` を受け入れ基準に

**4. Video Intelligence**
- 競合PR動画の構造分析（構成/フック/テロップ/尺/CTA、YouTube/Shorts/TikTok/IG）
- VSEOアルゴリズム読み解き（上位N本の時刻付き構造分析→横断で勝ち筋→HTMLタイムラインレポート）
- 納品動画の一次FB審査（オリエン必須要素/NG/テロップ/尺と照合し合否+指摘、管理番号AND判定）
- 複数動画の横断シンセシス（共通する勝ちパターン仮説）
- `video_analysis`/`vseo_reports`/`video_approval` を統一、野良 `vseo/` を吸収（2箇所割れ一掃）
- 重処理ガード（1リクエスト最大20件）

**5. Trend & Market Discovery**
- TikTok のKW/ハッシュタグ検索（Puppeteer実ブラウザ+内部API傍受を一次経路とする。Apify はクラウドIP遮断時のフォールバック、および X(Twitter)/Instagram 収集（カタログ組み込み 2026-07 決定）の主経路として採用＝『Apify不要』方針はこの範囲で改訂）
- 上位動画メタ取得＋Gemini横断分析、伸びている勝ちパターン/フックの抽出
- trigger=TikTok名AND検索動詞 or ハッシュタグ を宣言、cost_class=high

**6. Personal Workspace Bridge（PII境界）**
- 本人受信箱からの制約抽出（NG手法/予算/期限/関係性、**生本文を返さず**構造化制約のみ）
- 本人OAuthでのWorkspace検索（カレンダー/連絡先、未連携は fail-closed）
- DLPマスク・監査ログ・最小スコープの一貫適用、施策のNG差し替え判断への供給
- 到達不能だった `mail_constraints`/`workspace_search` を triggers宣言で正規経路へ（既定OFF維持）

**7. Conversation & Orchestration**
- 挨拶/お礼/雑談/能力質問への軽量会話応答（検索しない・0コスト即応）
- task-first の意図判定と L1ルーティング、受付ack（話題復唱）
- L1低confidence時の L2(Agent SDK)委譲、マルチステップ横断（VSEO→提案→PPTX 等）の解放
- 全リクエストへの request_id 伝播・構造化JSONログ・Bedrock usage/cost 記録

---

## 5. 非機能要件（NFR・測定可能）

| カテゴリ | 要件 | 目標値 |
|---|---|---|
| **保守性** | 新スキル追加が1ディレクトリ内で自己完結し、ルータ/ディスパッチャ本体を編集しない | 新スキル追加diffの**共有ファイル0行変更**。手書きルーティング正規表現 366行→**0行**。1スキル実装ファイル ≤800行（god-file禁止） |
| **保守性** | 全スキルの I/O契約を Pydantic v2 で固定し純ロジックで単体テスト可能 | BaseSkill準拠率100%、run()は外部I/Oゼロ。**カバレッジ line≥80% を CIゲート** |
| **拡張性** | ルーティングが能力宣言ベースで、スキル数増に対し編集コスト一定 | 12→25スキルでもルーティング判定の**行数増0**。意図衝突は宣言triggersの重複検査でCI検出、「〜より先に判定」ハック**新規ゼロ** |
| **堅牢性/可用性** | 同時4のピークを単一プロセスで安定処理、1スキル障害が他に波及しない | 同時4で **p95応答≤15秒・エラー率<1%**。1スキルException時もプロセス継続・隣接成功率に影響なし。DB障害時はDB依存スキルが fail-honest、非依存(chitchat等)は継続 |
| **堅牢性/データ層** | RDS弱点の復旧目標を明示 | **RDS Multi-AZ化で RTO≤120秒・RPO≤5分**。DB断は3回リトライ後 fail-honest。client-boost等の副次機能は fail-open |
| **セキュリティ** | アクセスをランタイムで fail-closed 強制、外部供給を審査ゲートで遮断 | データ系は既定OFF・明示opt-in。RLS/per-user OAuth を100%クエリで適用、**本人外漏洩0件**。外部Skillは版pin+権限レビュー必須、無審査マーケット直結**0件**。secrets平文**0件**(gitleaks+bandit) |
| **性能/コスト** | 1クエリのコスト/レイテンシを実測範囲に維持し暴走を検知 | 平均 **$0.01–0.02／p50 7–11秒** を維持改善。日次コスト$5・p95 15秒・5分3エラーでアラート。重処理は1req最大20件 |
| **可観測性** | 全層 request_id 伝播・構造化JSON・Bedrock usage/cost 記録 | request_id 欠落0件。Bedrock呼び出し毎に in/out/cache tokens・model_id・latency・cost を100%記録。架空scoreログ0、本番エラーは5分以内検知 |
| **CI/開発規律** | main常時グリーン、整形/型/セキュリティを機械強制 | 全PRで ruff+mypy+pytest+bandit 必須。新importは ci.yml 列挙へ追記漏れ0（httpx欠落型の再発防止）。基盤依存は厳密pin。main CI成功率≥95% |

---

## 6. アーキテクチャ構成

**採用: 宣言レジストリを唯一の真実源とする「単一プロセス・モジュラモノリス + 2層オーケストレーション」**（3設計案の最良点を接合 — §付録A）。

### 6.1 レイヤー（物理3層・import方向を内向き固定／import-linter でCI強制）
- **adapters層** — Slack受信/ack(`SlackInboundAdapter`)・Bedrock・pgvector・Google・Puppeteer を技術詳細として封じる。**表示整形**(`format_search_response`/`build_*_blocks` 等)を `ResponsePresenter` として全量退避。スキルは Pydantic 構造体のみ返す。
- **skills層** — 各スキルは検証済み入力 + `SkillContext` だけ受ける純ロジック。外部I/Oは adapter 注入。共有ポートは乱立させず「複数スキルが共有する境界だけ」= **`RetrievalPort` と `GenerationPort` の2本に限定**（単独利用は adapter 直結を許容＝1名運用での過剰間接化を回避）。
- **runtime層** — 薄い殻。`Slackイベント→ルータ→dispatch→Presenter` だけ。`get_X_skill()`/`run_X()` 手書き対12個 + `dispatch_auto` if連鎖 + `_ACK_BY_SKILL` を、**manifest を読む単一 `dispatch(intent, ctx)`** に畳む。`slack_bot.py` を200行台へ。

### 6.2 ケイパビリティ（7ドメイン=7パッケージ・各1 manifest）
- `CapabilityManifest`（`BaseSkill` の1段拡張）を**唯一の登録点**に昇格。1ドメイン=1ディレクトリ=1 `capability_manifest.py`（triggers/ack_template/cost_class/permission_scope/handler_factory/eval_fixtures を宣言）+ 複数 Action サブスキル。
- **OpenClaw採用で分類軸を追加（重要）**：`access:read|write`／`data_class:sales|none`／`rls_required`／`identity_verified_required`(OAuth系)／`hitl_required`(重操作 propose→confirm)／`mcp_exposed`。これで **manifest 1枚から自動派生**できる：①L1 triggers ②L2 @tool ③MCP gateway 公開tool(`build_production_tools`) ④**OpenClaw `openclaw.json` の `toolFilter.include/exclude`**（現状手書き→生成で二重定義解消） ⑤HITL対象 ⑥RLS/identity_verified の enforcement。＝WS-G(manifest単一真実源)が OpenClaw 露出にも効く。
- **中央 `CapabilityId`(StrEnum)** を新設し、命名3軸ズレ(dir/class.name/intent文字列)と文字列4箇所手書きを**型で一元化**（未定義IDはimport時にmypy/registryで弾く）。

### 6.3 オーケストレーション（3層：L0 外殻 / L1 決定的 / L2 自律）※OpenClaw採用で2層→3層に改訂
- **L0=OpenClaw 外殻（ingress・自発起動・Claude#1）** — Slack受け・cron/heartbeat の**プロアクティブ起動**・会話記憶・ルーティング推論を担う隔離コンテナ。外側モデルは Haiku＋prompt caching で軽量化。**触れるのは MCP境界の読取系tool のみ**（`openclaw.json` の `toolFilter` は manifest から自動派生＝§6.2）。営業データ非接触。
- **L1=規約ルータ（境界内・決定的・低コスト）** — OpenClaw が**個別 MCP tool を直叩き**する経路。モデルの内側ループを増やさず、manifest の triggers/cost が示す単発(検索/カルテ/審査)を低レイテンシで確定。⚠️ 純粋な「LLM非介在・0コスト即応(chitchat)」は OpenClaw 前面化で薄まるため、どこまで高速路を守るかは §9 でP1実測決定。triggers DSL は keyword/url_signal/named_entity/priority に**意図的に限定**（regex肥大の再発防止）。
- **L2=Agent SDK オーケストレータ（境界内・委譲）** — L1低confidence or 明示マルチステップ（「VSEOで分析して提案ドラフトとPPTXまで」）のみ。OpenClaw が `run_agent` MCP tool を1回呼ぶと、境界内で Claude#2 が registry 全 manifest を `ToolSpec` @tool 化し自律連鎖。**新規実装ではなく** `orchestrator/`(6-bis合格・85MB実deck生成済)を本配線。本番ガード(`require_rls` fail-closed/`max_same_call=2`/`cost_cap_usd`/コスト計上/忠実性照合)は `sdk_runner.py` 内在を実機確認済。

### 6.4 ごちゃつき再発防止の機構（構造的担保）
1. **中央 `CapabilityId`(StrEnum)** を真実源化（4箇所手書き→1値、命名3軸→1軸）
2. **registry-as-single-source** — ルータ/ディスパッチャ/ack/SDK@tool化の4経路すべて「manifest を読むだけ」。本番経路がregistryをバイパスする現状を**構造的に不可能化**
3. **1スキル追加=新manifest1枚** の規約（共有ファイル0行変更）
4. **CI で manifest 網羅・契約チェック** — 全@registerが triggers宣言を持つ（到達不能スキル再発防止）・cost_class/permission_scope必須・triggers優先度衝突検査・import-linterで依存方向強制
5. **triggers宣言DSLで暗黙優先順位を明示化**（「○○より先に判定」コメント→ priority整数）
6. **env 66個→`Settings`(pydantic-settings)1モデル + manifest.config へ集約**、直 `os.environ.get` を lint禁止、実験ノブ(v1/v2c/v2d)を恒久ifから外す
7. **表示整形の境界分離**（`ResponsePresenter` へ強制移動、引用必須・fail-honest検証を出力直前に）
8. **外部Skill供給ゲート**（版pin+権限宣言レビュー+署名検証+fail-closed既定OFF＝ClawHavoc型耐性）

---

## 7. 基盤判断：OpenClaw を外殻に採用（2026-06-09 改訂・決定事項＝不変）

**結論（改訂）: OpenClaw を自律外殻(L0)として採用。ドメインは Claude Agent SDK on Bedrock(L2)のまま MCP境界の内側に温存し、OpenClaw からは MCP tool 越しにのみ触らせる。** 6/5版の「不採用」は *OpenClaw を裸でアプリに飲ませる前提* の判断であり、**MCP境界＋隔離で封じ込める前提なら採用が成立**する（懸念は §7.2 の通り構造で解消）。

### 7.1 採用アーキ（役割分担）
- **L0 = OpenClaw 外殻**：Slack ingress・cron/heartbeat による**自発起動**・会話記憶(SQLite)・ルーティング推論(Claude#1=Haiku4.5)。**Docker隔離・専用最小IAM(bedrock:InvokeModel系のみ)・営業データ非接触**。版pin `v2026.6.1`(digest)。
- **MCP境界(信頼境界)**：`mcp_gateway`（WS-A 実装済）。RLS行権限・per-user OAuth・fail-closed・反ハルシ・**本人解決(WS-C)** を Python側で死守。OpenClaw 申告の email/groups/role は採らない(STRICT)。
- **L2 = Claude Agent SDK(Claude#2)**：境界内のマルチステップ。`require_rls`/`max_turns`/`cost_cap`/faithfulness は `sdk_runner.py` 内在（6-bis合格・85MB実deck生成済・`claude-agent-sdk==0.2.87` 厳密pin）。

### 7.2 6/5版「不採用3根拠」への回答（封じ込めで解消）
1. **用途ミスマッチ** → OpenClaw は「アプリを飲む製品」だが、**前段の殻としてだけ使い、ドメインは飲ませない**。OpenClaw は MCP tool 越しにしかドメインに届かず、粒度の衝突を境界で回避。Bedrock(IAM)・MCP(streamable-http) 一次対応はそのまま接続に活用。
2. **資産毀損** → **作り直しゼロ**。12スキル/RLS/OAuth/テストはそのまま、`build_production_tools()` を MCP 公開するだけ(WS-A)。`openclaw.json`/`SOUL.md` は外殻の薄い設定のみ（Gitレビュー必須・秘密値非コミット）。
3. **セキュリティ** → §A で一次再確認（2026-05-28 GHSA一括: RCE/exec/MCP権限漏れ/Critical2、min-safe≥2026.5.26）。**隔離コンテナ(read-only/cap-drop ALL/digest pin)＋最小IAM(Secrets/KMS/RDS 明示Deny)＋営業データ非接触＋ `tools.exec.mode:deny` で exec/shell系CVEを構造無効化＋ WS-C で per-user 越権を封鎖**。これは本書 原則#5・#8(審査ゲート)の実装。CVE追従(月次パッチ/版pin)は運用の前提コストとして受容（専任1名の最大慢性負荷・P1で工数実測）。

### 7.3 OpenClaw 採用が突きつける新論点（§9 で確定）
- OpenClaw は **単一信頼オペレータ設計**（敵対的マルチテナント境界でない・`sessionKey`は認可でない・`dmScope`既定は会話漏れ）→ **認可はMCP内側で死守**（本書#5と一致）＋ `dmScope:"per-channel-peer"` 必須。
- **水平スケール非対応・会話メモリはローカル** → ~40名は単一GW(垂直)＋速度チューニング(Haiku外側＋prompt caching)で可、**同時実行上限は P1 実測**。
- MCP は **streamable-http**（stdio は OpenClaw が MCP を子同居させ creds/network 隔離を壊すため本番不可）。

---

## 8. 移行段階（P0–P5・ストラングラー / フラグ戻しで即ロールバック）

| Phase | 内容 | ゴール |
|---|---|---|
| **P0 土台（破壊なし）** | 中央 `CapabilityId`(StrEnum) と `Settings`(pydantic-settings) 新設。name/intent文字列/`_ACK` を Enum参照へ（4→1箇所）、env66個と二重parseを Settings集約。`CapabilityManifest` を各スキルに1枚追加し triggers を機械転記（未使用）。registry に import時検査+CI manifest網羅/import-linter(warning)。**slack_bot無改変・テストgreen維持** | 真実源の器を作る |
| **P1 ルータ統合（shadow）** | manifest triggers を読むL1規約ルータを新設し、本番は `detect_skill` 継続で **shadow並走**・一致率をログ。gold set50で検証。`router.py` の QueryType を Knowledge内部へ降格（二重ルータ1段化） | ルータを安全に並走検証 |
| **P2 ディスパッチャ置換 + god-file解体着手** | `get_/run_` 手書き対12個と `dispatch_auto` if連鎖を `manifest.handler_factory` を呼ぶ**単一 `dispatch(intent,ctx)`** に。`_ACK_BY_SKILL`→`manifest.ack_template`。`detect_skill` 4回→1回。**intent.py 366行正規表現を削除**。`ResponsePresenter`/`SlackInboundAdapter` 切り出し開始 | 配線をmanifestへ畳む |
| **P3 adapter整理・god-file完了** | 表示整形を `ResponsePresenter` へ全量退避、user_email→user_groups 4回コピペを `PolicyEngine`(middleware) へ集約、共有ポート(Retrieval/Generation)2本だけ切り出し。**slack_bot.py を200行台へ**。直 `os.environ.get` lint禁止、カバレッジ/型/bandit/gitleaks を CIゲート化 | god-file解体完了 |
| **P4 到達不能スキル復活 + ドメイン再編** | `mail_constraints`/`workspace_search`/`proposal_deck` を triggers宣言で正規経路へ（fail-closed既定OFF維持）。Video命名崩れ一掃(`vseo/` 吸収・name統一)、Proposal 4 Action束ね。`factory._build_search_skill` と `get_search_skill` を **registry単一factory** へ統合 | 重複・到達不能の一掃 |
| **P5 L2本配線（ゲート①完遂）** | registry 全 manifest を ToolSpec 自動@tool化し、既存 `orchestrator/`(6-bis合格済)を `USE_AGENT_ORCHESTRATOR`/低confidence経路に**本配線**（runtime→orchestrator参照ゼロの解消）。マルチステップ(VSEO→提案→PPTX)を解放。自前ゲート通過分の MCP のみ registry登録可とする保険レイヤ | 自律オーケストレーションの解放 |

各Phase末で **Slack E2E疎通 + 全テストgreen** を維持。

**OpenClaw 外殻トラック（本書のドメイン内リファクタと並行・統合ロードマップ）:**
- **済(2026-06-09)**: WS-A `mcp_gateway`（=P5前半 ToolSpec公開の先取り）／WS-B OpenClaw隔離デプロイ雛形(版pin/最小IAM/egress/Haiku+cache+dmScope)／WS-C 本人解決の単一真実源（=P3 PolicyEngine の種・admin/なりすまし封鎖・900テストgreen）。
- **次にやること**: ①Next **P0–P2**(CapabilityId/Settings集約/**god-file解体**/intent.py 366行削除)＝OpenClaw前面化の前提整備（内側がごちゃつくと境界も汚れる）。②**manifest 6軸 → `toolFilter` 自動生成**(§6.2)。③**P0隔離PoC**(実DBで RLS-through-MCP漏洩0 / インジェクション越権0 / Bedrock(IAM)疎通)。④**P1パイロット**(OpenClaw単一GW前面・専用ch少数・読取のみ・同時実行/レイテンシ/コスト実測)→ゲート通過で **P2本番**(プロアクティブ/HITL解放)。
- 旧 §8 P5「runtime→orchestrator 本配線」は **L2 を MCP境界内に置き OpenClaw(L0) から `run_agent` で叩く形** に更新。`USE_OPENCLAW_FRONTEND` flag で段階導入＋現行Bot へ1分ロールバック（Socket Mode 二重接続不可のため OC は専用Slackアプリ/ch）。

---

## 9. 未決事項（確認のうえ確定したい）

1. **ゲート①の承認** — SDK が Node を spawn する前提（Node CLI 本番持込＝EC2運用に1依存追加）を、専任ほぼ1名運用で負える前提でよいか。
2. **L2 の本番デフォルト ON/OFF** — 当面 `USE_AGENT_ORCHESTRATOR` フラグで明示マルチステップ時のみ起動し、単発は L1決定的ルータに留める方針でよいか（全reqをLLMループに通す純化案は不採用想定）。
3. **ドメイン境界の確定** — 7ドメインで進めるか、`chitchat` を Application層FastPathへ降格して6ドメインに寄せるか。Proposal 4 Action束ねと Video の vseo統合は移行コスト中規模なので着手順の合意がほしい。
4. **RDS Multi-AZ化の実施時期** — NFR(RTO≤120秒/RPO≤5分)の補強を本リファクタのどのPhaseで（または並行で）。コスト増を伴うため別途承認。
5. **カバレッジゲートの基準** — line≥80% を最初から強制するか、Phase進行で段階的に引き上げるか（1名運用での締め付け度合い）。
6. **ブランチ/着手前提** — 現在 `feat/proposal-deck-skill` で proposal_deck の作業中（別セッション）。本リファクタ着手前に main統合状況とブランチ戦略の整理が必要。
7. **【OpenClaw採用で追加】分離方式(~40名)** — 単一GW(垂直・速度は Haiku+cache で近似)で進め、**同時実行上限/SPOF は P1 実測**で確認。本格分離(利用者別GW)は P2 前に判断。`dmScope:"per-channel-peer"` 必須。配置は **ECS Fargate**(per-task IAM で外殻/境界の権限分離)推奨・**RDS Multi-AZ**。
8. **【OpenClaw採用で追加】L1決定ルートの去就** — OpenClaw 前面化で「LLM非介在の0コスト即応(chitchat/単発)」が薄まる。現行Bot 高速路を一部フォールバック保持するか、OpenClaw側 Haiku+cache で近似に寄せるか(§6.3 L1)を **P1 レイテンシ/コスト実測**で確定。
9. **【OpenClaw採用で追加】CVE運用** — OpenClaw の月次パッチ/版pin/赤チームが専任1名の最大慢性負荷。P1 で「月あたり運用工数」を実測し P2 Go の判断材料に（可逆性=flag1つで現行Bot復帰 を常時担保）。

---

## 付録A：設計レンズ3案（接合元）
- **A. ケイパビリティ・プラグイン型** — 1ドメイン=1プラグイン=1 `CapabilityManifest`。ルーティングを規約化し intent.py/dispatch/ack を「manifest を読むだけ」に。→ **採用構造の骨格**。
- **B. ツール中心・自律エージェント** — 全能力を薄いツールとして公開し SDK のループに委ねる。ルールベース全廃。→ **L2 と「既存POCを唯一のライブ経路に昇格」の論拠**。
- **C. ヘキサゴナル/ケイパビリティ・サービス** — ポート&アダプタ厳格化、intent/設定/権限/可観測性を横断プラットフォーム層へ収束、依存を内向き固定。→ **import-linter 強制・共有ポート2本・PolicyEngine の論拠**。

3案とも「単一プロセス維持」「registry を唯一の真実源に」「god-file解体」で一致。差は L2委譲の比重とポートの数。**過剰間接化を避けるため C のポートは2本に限定**、**B の自律ルーティングは L2(委譲時)に限定**して接合。

## 付録B：OpenClaw 検証の一次ソース
**2026-06-09 一次再確認（§A・WS-B-B1・自分で `gh`/公式docs 直確認）**: `openclaw/openclaw`(377k★・**MIT**・最新stable **v2026.6.1**・TS/Node 22.19+)、GitHub Security Advisories(**2026-05-28 GHSA 30+件**: Critical2/High多数・RCE/exec/MCP権限漏れ・stdio env注入 GHSA-mj59-h3q9-ghfh fixed 2026.4.20・min-safe≥2026.5.26)、`docs.openclaw.ai`(channels/slack=Socket Mode単一接続 vs HTTP Events、concepts/session=**`dmScope`既定`main`は会話漏れ**、gateway/multiple-gateways=**水平スケール非対応**、providers/bedrock=auth `aws-sdk`、concepts/agent-loop=**tool無→1ターン**、reference/prompt-caching=`cacheRetention`、gateway/security=**単一信頼オペレータ**)。旧版の個別CVE番号(CVE-2026-25253 等)は本一次確認(GHSA一括)で置換。

（旧版の二次情報）GitHub(`openclaw/openclaw`・releases・AGENTS.md)、`docs.openclaw.ai`(providers/bedrock・cli/mcp)、Wikipedia、Microsoft Security Blog(2026/02 identity-isolation-runtime-risk)、Cisco Blog(personal-ai-agents security nightmare)、The Hacker News(2026/03 agent flaws)、PointGuard AI(CVE-2026-35650)、Infosecurity Magazine(six new flaws)、Eye Security(log poisoning)。
