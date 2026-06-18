# 提案書 要素スキーマ + VSEO編集可レポートへの実装計画

> 出典: くら寿司Platinum提案デッキ（無添蔵新宿店 開業PR ショート動画施策）51枚を6セクションで要素抽出（55要素）。
> 現VSEOカバー: already=4 / partial=8 / missing=43。
> 目的: 編集可(slides)レポートを「提案書の要素テンプレ」に拡張し、各要素の情報を OCリサーチ→保管できるようにする。
> 生成: 2026-06-17 / Workflow proposal-element-extraction。

必要な情報は揃いました。`render_slides` の `builders` リストに `(kind, body)` を追加すれば章が増える純関数構造、`CrossSynthesis` の実フィールド名、本番反映は「MCP再ビルド+OpenClaw再デプロイ待ち」(メモリ通り)という事実を確認できました。

以下が成果物の1枚Markdownです。

---

# 提案書要素スキーマ ＆ VSEO編集可レポート実装計画

対象: くら寿司Platinum提案デッキ抽出41要素 / 現状 `video_algorithm/slides.py`(7要素・純関数 `render_slides`)
凡例: VSEOカバー = `already`(既存生成) / `partial`(一部データあり) / `missing`(未保有)

---

## 1. 提案書の要素体系（セクション→要素）

保管先 凡例: **DB**=提案書要素DB(構造化メタ) / **PG**=pgvector/connector_state(OC横断検索面) / **S3**=画像/サムネ / **HTML**=編集可HTMLのdata属性に直書き / **CL**=クライアント提供

| # | セクション | 要素 | 役割 | 必要情報(主) | リサーチ源 | 保管先 | VSEO |
|---|---|---|---|---|---|---|---|
| 01-1 | 01 与件整理 | 表紙(タイトル/日付/宛先) | 所有権・鮮度を1枚で確定 | client名/商材/日付/タイトル/ロゴ | CL | DB | already |
| 01-2 | 01 与件整理 | アジェンダ(目次) | 全体ストーリー握り | 章一覧+ページ番号 | 複合 | DB | missing |
| 01-3 | 01 与件整理 | 章扉 01 | セクション区切り | 章番号+タイトル | 複合 | DB | missing |
| 01-4 | 01 与件整理 | クライアント基礎/商材ファクト | 勝ち筋の根拠を置く | USP3点/コンセプト/看板メニュー/利用シーン | CL | DB(client_facts↔HTMLのclient_pitch) | partial |
| 01-5 | 01 与件整理 | 課題・ゴール・与件サマリ | スコープ宣言 | 目的/KGI/チャネル/予算/時期/ターゲット | CL | DB(brief) | missing |
| 02-1 | 02 市場変化 | セクション扉 | 論拠パート導入 | 章番号/サブテーマ | OC社内 | DB | missing |
| 02-2 | 02 市場変化 | 情報流通の主役交代(一次統計) | 地殻変動を立てる起点 | 統計値%/出典URL/時点/含意1行 | Web一次統計 | PG | missing |
| 02-3 | 02 市場変化 | 信頼源=他者体験(口コミ統計) | UGC獲得の必然性 | 参照率%/口コミ比率%/出典 | Web一次統計 | PG | missing |
| 02-4 | 02 市場変化 | ショート動画=購買起点(媒体統計) | 媒体選定正当化 | 転換率%/GMV/利用率/世代/出典 | Web一次統計 | PG | missing |
| 02-5 | 02 市場変化 | TVCM vs ショート動画(対比図) | 質的優位の対比 | 受容構造の対比軸/投稿画像複数 | 複合 | HTML+S3 | missing |
| 02-6 | 02 市場変化 | 若年層検索シフト(◯◯る化) | VSEO前提を立てる | 検索ランク1位/愛称/出典 | Web一次統計 | PG | partial |
| 02-7 | 02 市場変化 | 中高年層への拡大 | ターゲット汎用性 | 上位世代の視聴/活用率/出典 | Web一次統計 | PG | missing |
| 02-8 | 02 市場変化 | アルゴリズム機構解説 | 複数アカ打ち手の根拠 | 反復接触の機序/設計原則/実投稿URL | 複合 | HTML | missing |
| 02-9 | 02 市場変化 | 中核仮説/KPIロジック | 因果仮説の明文化 | 構成変数/期待アウトカム/指標定義 | OC社内 | DB | missing |
| 02-10 | 02 市場変化 | フリークエンシー閾値調査 | 再現性の追い風 | 接触回数調査/N・属性/出典 | Web一次統計 | PG | missing |
| 02-11 | 02 市場変化 | AISAS→SEESAS フレーム | 理論的バックボーン | 旧/新モデル定義/決定的差分/図解 | OC社内 | DB | missing |
| 03-1 | 03 箱推し文脈 | セクション扉 | 章ゴール宣言 | 章タイトル/キーフレーズ | OC社内 | DB | missing |
| 03-2 | 03 箱推し文脈 | 分析アプローチ宣言(USP×検索上位) | 方法論の約束 | USP一言/検索クエリ群/交差ロジック | 複合 | DB | partial |
| 03-3 | 03 箱推し文脈 | 商材USP・ファクトシート | 文脈翻訳の原資 | 立地/コンセプト/シグネチャー/シーン/階層 | CL | DB | missing |
| 03-4 | 03 箱推し文脈 | 検索KW仮説の提示 | 分析スコープ明示 | エリア名/ジャンル5KW/選定根拠 | 複合 | DB | partial |
| 03-5 | 03 箱推し文脈 | KW別 上位5動画ボード | 共通文脈の一次エビデンス | アカウント/views/F数/サムネ/計測日 | **TikTok実分析(VSEO)** | S3 | **already** |
| 03-6 | 03 箱推し文脈 | KW別 共通文脈3要素抽出 | 勝ち筋を言語化 | メイン文脈コピー/3点の型/心理機構 | **TikTok実分析(VSEO)** | HTML | **already** |
| 03-7 | 03 箱推し文脈 | マルチKW入賞動画 構造解剖 | 横断再現可能化 | アカウント/同時入賞KW+順位/構造3点/翻訳 | **TikTok実分析(VSEO)** | HTML | partial |
| 03-8 | 03 箱推し文脈 | 2KW入賞クラスタ俯瞰 | 再現性の量的裏取り | 動画群/入賞KW組合せ/構造3点 | **TikTok実分析(VSEO)** | S3 | partial |
| 03-9 | 03 箱推し文脈 | 切り口総ざらい(チップ群) | 採用候補ショートリスト | 勝ち切り口10語/方針文 | **TikTok実分析(VSEO)** | HTML | **already** |
| 03-10 | 03 箱推し文脈 | 勝ち筋文脈の結論+コピー仮案 | 結論宣言 | 文脈定義文/コピー案複数/仮注記 | 複合 | HTML | partial |
| 04-1 | 04 展開/キャスティング | セクション扉 | 分析→実行の切替 | 章番号/タイトル | OC社内 | DB | missing |
| 04-2 | 04 展開 | アルゴリズム成功事例 | 実証で説得力 | ブランド名/施策要約/成果数値/サムネ | 自社実績DB(KOL台帳) | DB | missing |
| 04-3 | 04 展開 | 施策方針(生成戦略の基本構造) | 中核ロジック言語化 | 生活者行動仮説/核→面→UGC導線 | OC社内 | DB | partial |
| 04-4 | 04 展開 | 全体スキーム図(階層ピラミッド) | 全体像を握らせる | 各層呼称/規模感/役割コピー/波及方向 | OC社内 | DB | missing |
| 04-5 | 04 展開 | 第三者調査の裏付け(PF公式) | 客観性補強 | 調査主体/出典/支持スタッツ/示唆3点 | Web一次統計 | PG | missing |
| 04-6 | 04 展開 | TOP/ミドルKOLキャスティング | 起用現実性・リーチ上限 | 自社TOPアカ+F数/外部KOL人数/各F実数 | 自社実績DB | DB | missing |
| 04-7 | 04 展開 | 起点アカウント実績証明 | 起用価値の裏付け | アカ名/受賞歴/横並び比較/計測日 | 自社実績DB | DB | missing |
| 04-8 | 04 展開 | 自社サブアカ群 追加起用 | 同一管理のフリークエンシー | 追加アカ名/各F数/同一運営注記 | 自社実績DB | DB | missing |
| 04-9 | 04 展開 | ミドル:自社SNSメディア起用 | 拡散の面を作る | メディア名/累計F/月間imp/得意領域/事例 | 自社実績DB | DB | missing |
| 04-10 | 04 展開 | ミドル:投稿イメージ | T&M具体化 | 参考投稿URL/サムネ複数 | 自社実績DB | S3 | missing |
| 04-11 | 04 展開 | マイクロ:大量招致ソリューション | 高速大量UGCの仕組み | 登録総数/招致フロー/差別化3点/運用体制 | 自社実績DB | DB | missing |
| 04-12 | 04 展開 | マイクロ:ジャンルカバレッジ | 適層到達の担保 | 保有ジャンル一覧/中心ジャンル指定 | 自社実績DB | DB | missing |
| 04-13 | 04 展開 | マイクロ:起用ボリューム設計 | スケール調整可能性 | 平均F/予算別人数/サンプルF/確約有無 | 自社実績DB | DB | missing |
| 04-14 | 04 展開 | トレンドメディア:媒体リスト | 話題化担保 | 累計再生/媒体数/媒体名+ハンドル/対応対象 | 自社実績DB | DB | missing |
| 04-15 | 04 展開 | トレンドメディア:投稿イメージ | 取材型T&M具体化 | 参考投稿URL/サムネ複数 | 自社実績DB | S3 | missing |
| 04-16 | 04 展開 | トレンドメディア:招致型イベント事例 | 同型再現性証明 | 事例名/招致人数/PF/成果/会場キャプチャ | 自社実績DB | DB | missing |
| 05-1 | 05 スケジュール | セクション扉 | 区切り | 章番号/タイトル/章順 | CL | DB | missing |
| 05-2 | 05 スケジュール | ガント時間軸(週/月ヘッダー) | 期間感俯瞰 | 総期間/刻み/起点日/配信ピーク | 複合 | DB | missing |
| 05-3 | 05 スケジュール | 施策スイムレーン | 並行進行の構造化 | 施策タイプ一覧/タスク粒度/依存関係 | 自社実績DB | DB | missing |
| 05-4 | 05 スケジュール | 撮影マイルストーン | 店舗準備ポイント明示 | 撮影日/ロケハン週/内覧会/オープン日 | CL | DB | missing |
| 05-5 | 05 スケジュール | 準備フェーズタスク | リードタイム訴求 | オリエン/構成FIX日数/可否取り/選定工数 | 自社実績DB | DB | missing |
| 05-6 | 05 スケジュール | 意思決定ゲート | 発注デッドライン認識 | 最終期限/遅延影響/決裁フロー | CL | DB | missing |
| 06-1 | 06 KPI/見積 | セクション扉 | 意思決定モードへ | 章番号/タイトル/対応ページ | CL | DB | missing |
| 06-2 | 06 KPI/見積 | プラン階層(松竹梅) | 「どれにするか」を選ばせる | プラン名/各総額/差分/推奨 | 複合 | DB(plans[]) | missing |
| 06-3 | 06 KPI/見積 | 費目①TOP KOL | 最大費目の妥当性 | 自社TOPアカ本数/外部人数/金額/投稿数/再生保証 | 自社実績DB | DB(line_item) | missing |
| 06-4 | 06 KPI/見積 | 費目②自社グループメディア | 保証母数の大半 | 媒体種別/媒体数/金額/投稿数/再生保証 | 自社実績DB | DB(line_item) | missing |
| 06-5 | 06 KPI/見積 | 費目③マイクロ招致 | CP納得感 | 人数/平均F/確約有無/金額/ジャンル | 自社実績DB | DB(line_item) | missing |
| 06-6 | 06 KPI/見積 | 総再生保証サマリー | 円/再生の判断軸 | プラン別合計保証/CPV/保証定義 | 複合 | DB(派生CPV) | missing |
| 06-7 | 06 KPI/見積 | 見積注記・取引条件 | 契約齟齬/ステマ規制潰し | 手数料率/超過扱い/AD内包/#PR表記 | OC社内 | DB(terms[]) | missing |

---

## 2. VSEO編集可レポートとのギャップ

### 集計(全41要素)
| カバー | 件数 | 割合 | 中身 |
|---|---|---|---|
| already | 4 | 10% | 表紙 / KW別上位5動画ボード / KW別共通文脈3要素 / 切り口チップ群 |
| partial | 8 | 20% | 商材ファクト / 分析アプローチ宣言 / 検索KW仮説 / マルチKW構造解剖 / 2KW俯瞰 / 結論コピー / 若年層検索シフト / 施策方針 |
| missing | 29 | 71% | 一次統計群・SEESAS・キャスティング(具体KOL/メディア)・スケジュール・KPI見積 ほぼ全て |

現状slides.pyの7要素は、本デッキの **03章(箱推し文脈)とその結論コピーにほぼ集中**。提案書の前半論拠(02)・後半実行(04-06)が丸ごと空白。

### missing で価値が高い要素 TOP5（=投資対効果が最も高い拡張）
| 順 | 要素 | なぜ高価値 | 取得難度 |
|---|---|---|---|
| 1 | **06-2〜06-6 KPI/見積(松竹梅+費目+再生保証)** | これが無いと「提案」でなく「分析」止まり。発注判断の核 | 中(自社実績DB+営業入力) |
| 2 | **04-4 全体スキーム図(階層ピラミッド)** | 施策の世界観を1枚で握らせる。03の分析と04の実行を接続する蝶番 | 低(テンプレ図) |
| 3 | **02-2〜02-4 市場一次統計(主役交代/口コミ/購買起点)** | 「なぜショート動画か」の客観的論拠。再利用性が全案件で最も高い | 中(Web一次統計・要出典) |
| 4 | **02-11 AISAS→SEESAS フレーム** | 提案全体の理論バックボーン。一度作れば全案件で使い回せる固定資産 | 低(テンプレ図・一度作れば固定) |
| 5 | **04-6〜04-8 TOP/サブKOLキャスティング** | 「誰が投稿するか」の現実性。VSEO分析を実行プランに変換する要 | 中(KOL台帳・要可否取り注記) |

補足: TOP5のうち 2/4 はテンプレ図で再利用資産化でき(スキーム図/SEESAS)、残りは自社実績DB or Web一次統計の整備で全案件横展開が効く。

---

## 3. OCリサーチ→保管の流れ（research_source 別）

| research_source | 何をどう取るか | 保管先 | 編集可HTMLのどの要素に差し込むか |
|---|---|---|---|
| **TikTok実動画分析(VSEO既存)** | Geminiが実上位動画を読解(フック/演出/保存導線)＋メタ(views/F/サムネ)。**既に slides.py が保有** | S3(サムネ240px) / HTML(文脈テキスト) | 03-5/03-6/03-9(already) と top5/synthesis スライド。ここは新規取得不要、再配置のみ |
| **Web一次統計(調査会社/政府/業界)** | WebSearch+WebFetchで統計値%・出典URL・調査時点・N数を取得→出典付きで構造化。`go2senkyo`/`commercepick`/`Ipsos×TikTok`/`トレンダーズ` 等の実数字を1枚1主張で | **PG(pgvector/connector_state)** に出典付きチャンク登録→再利用可能化 | 02-2〜02-4/02-6/02-7/02-10/04-5(一次統計スライド群)。**出典URLは data-source 属性に必ず保持** |
| **OC社内ナレッジ(Slack/Drive横断検索)** | TeamAgentの横断検索(pgvector)で過去提案の「SEESAS図/スキーム図/施策方針文/フリークエンシー仮説」を引く。定型文・図解は社内資産として再利用 | DB(章構成/フレームテンプレ) | 02-1/02-9/02-11/03-1/04-1/04-3/04-4(セクション扉・理論フレーム・施策方針)。固定文言はテンプレ化 |
| **自社実績DB(KOL台帳/過去事例)** | 保有アカウントのF数・受賞歴・サブアカ群・メディア規模(累計imp)・マイクロ招致網・過去案件成果を台帳から引く。**計測日を必ず併記**(F数は鮮度劣化する) | DB(line_item/casting) / S3(投稿サムネ) | 04-2/04-6〜04-16(キャスティング全般)/05-3・05-5(スイムレーン/可否取り)/06-3〜06-5(費目)。台帳→費目の自動マッピング |
| **クライアント提供(CL)** | オリエンシートをパース→objective/kgi/scope/budget/timeline/target に構造化。撮影日・オープン日・決裁フローもここ | DB(meta/brief) | 01-1/01-4/01-5/03-3(表紙・ファクト・与件)/05-4・05-6/06-1。**未提供は「要確認」で空けておく** |
| **複合** | 上記2源以上を掛け合わせ(例: USP×検索上位=勝ち筋文脈、予算×台帳=松竹梅) | 主にHTML(掛け算結果) | 01-2/02-5/02-8/03-2/03-4/03-10/05-2/06-2/06-6 |

流れの要点: **Web統計→PG(出典付き)・社内図解→DB(テンプレ)・KOL→DB(計測日付)・クライアント→DB(空欄許容)** の4経路を整備すれば、slides.py は「DB/PGから引いて contenteditable HTMLに流す」だけになる。

---

## 4. 編集可レポートを「提案書要素テンプレ」に拡張する最小実装案

### 設計原則(現 slides.py の規約を踏襲)
- `render_slides()` の `builders: list[tuple[kind, body]]` に **(kind, body) を追加するだけ**で章が増える純関数構造を維持。
- 各ブロックは contenteditable 付き素タグ＋意味クラス名(既存 `.kicker/.slide-title/.lead/.slide-points/.chips/.pitch` を流用)。
- **データ未取得は描画スキップしない**(従来 `if not body: return ""`)→ 代わりに **「要確認(リサーチ待ち)」プレースホルダ**を返し、営業が穴を視認できるようにする(提案書は「空欄を埋める」運用)。
- 各要素ブロックの最外 div に **`data-source` 属性**(research_source タグ)と **`data-element`** を付け、後段の自動充填/QAが要素を特定できるようにする。

### 追加するヘルパ(2つ)
```python
# slides.py に追記
PLACEHOLDER = "要確認（リサーチ待ち）"

def _editable(text: str | None, *, src: str, element: str, tag: str = "span") -> str:
    """値があれば表示、無ければプレースホルダ。research_source を data 属性に保持。
    src ∈ {client, web_stat, oc_internal, casting_db, tiktok_vseo, composite}"""
    val = _esc(text) if text else f'<span class="todo">{PLACEHOLDER}</span>'
    return (f'<{tag} class="el" data-element="{element}" data-source="{src}" '
            f'contenteditable>{val}</{tag}>')

def _section_divider(no: str, title: str) -> str:
    return (f'<div class="kicker">{_esc(no)}</div>'
            f'<h2 class="slide-title" contenteditable>{_esc(title)}</h2>')
```
CSS追加(`.todo{{color:#b91c1c;background:#fff1f2;border:1px dashed #fca5a5;padding:1px 8px;border-radius:6px}}`)で未取得を赤破線表示。

### 追加する要素ブロック例(一般形・くら寿司値は data 既定として併記)
```python
def _market_stat(out, *, label, src="web_stat") -> str:
    """02-2〜02-4: 一次統計1主張1枚。出典URLは data-source-url に必須保持。"""
    s = getattr(out.cross, "market_stats", None)   # 将来 schema 拡張
    stat = s.value if s else None        # 例:"公式発信は10%、90%が第三者ショート動画"
    url  = s.source_url if s else None   # 例:"go2senkyo"
    return (
        f'<div class="el" data-element="market_stat" data-source="{src}" '
        f'data-source-url="{_esc(url or "")}">'
        '<div class="kicker">市場・消費者行動の変化</div>'
        f'<h2 class="slide-title">{_esc(label)}</h2>'                       # 例:情報流通の主役交代
        f'<div class="lead">{_editable(stat, src=src, element="stat_value")}</div>'
        f'<div class="tline">出典: {_editable(url, src=src, element="source")}</div></div>')

def _casting_pyramid(out) -> str:
    """04-4: 階層ピラミッド。台帳が空でも層テンプレは常に描画。"""
    layers = ["TOP KOL（話題の核を創出）","グルメ特化（核を広げる）",
              "ミドル/EMME（接触頻度）","トレンドメディア（トレンド拡散）",
              "マイクロUGC（接触回数・大量）"]
    li = "".join(f'<li>{_editable(None, src="casting_db", element="layer")}'
                 f'<span class="mut">{l}</span></li>' for l in layers)
    return (_section_divider("04 展開イメージ","キャスティング設計（階層スキーム）")
            + f'<ul class="slide-points">{li}</ul>')

def _kpi_plans(out) -> str:
    """06-2〜06-6: 松竹梅3列。費目/再生保証は line_item から、無ければ要確認。"""
    plans = getattr(out, "plans", None) or [("梅",None),("竹",None),("松",None)]
    cells = "".join(
        f'<div class="gcell"><div class="rk">{_esc(n)}</div>'
        f'<div class="mt">総額 {_editable(p and p.total_price, src="casting_db", element="price")}</div>'
        f'<div class="mt">総再生保証 {_editable(p and p.total_view_guarantee, src="composite", element="view_guarantee")}</div>'
        f'</div>' for n, p in plans)
    return (_section_divider("06 KPI/お見積","松竹梅プランと再生保証")
            + f'<div class="grid" style="grid-template-columns:repeat(3,1fr)">{cells}</div>'
            + f'<div class="warn">※進行手数料/超過分/AD内包/全投稿#PR表記 → '
            + f'{_editable(None, src="oc_internal", element="terms")}</div>')
```

### builders への配線(`render_slides` 内)— 既存7要素の前後に挿入
```python
builders = [
    ("cover",        _cover(out, generated_at)),          # 01-1 already
    ("agenda",       _agenda(out)),                       # 01-2 NEW(章構成テンプレ自動採番)
    ("brief",        _brief(out)),                         # 01-5 NEW(CL・要確認許容)
    ("market_stat1", _market_stat(out, label="情報流通の主役交代")),  # 02-2 NEW
    ("market_stat2", _market_stat(out, label="信頼源は他者体験へ")),  # 02-3 NEW
    ("seesas",       _seesas(out)),                        # 02-11 NEW(OC社内テンプレ図)
    ("conclusion",   _conclusion(out)),                    # 03 already(勝ち筋)
    ("creative",     _creative(out)),                      # already
    ("top5",         _top5(out)),                          # 03-5 already
    ("thumbs",       _thumbs(out)),                        # already
    ("synthesis",    _synthesis(out)),                     # 03 already
    ("casting",      _casting_pyramid(out)),               # 04-4 NEW
    ("casting_top",  _casting_top(out)),                   # 04-6〜04-8 NEW(KOL台帳)
    ("schedule",     _schedule(out)),                      # 05 NEW(スイムレーン)
    ("kpi_plans",    _kpi_plans(out)),                     # 06 NEW(松竹梅)
    ("cta",          _cta(out)),                           # already
]
# 従来の filtered=[(k,b) for k,b if b] は維持しつつ、NEWブロックは
# 空でもプレースホルダHTMLを返すため常に描画される（=穴が見える提案書になる）
```

### schema 最小拡張(`CrossSynthesis`/`VideoAlgorithmOutput` に追加)
既存フィールド(`headline/strategy/creative_brief/posting_design/client_pitch/shared_funnel`)はそのまま。追加で `market_stats: list[MarketStat]`、`brief: ClientBrief|None`、`plans: list[Plan]`、`casting: Casting|None` を **全てデフォルト空**で持たせる(未取得=空=プレースホルダ描画)。既存の純関数性・「空ならスキップ」regression を壊さない。

### 本番デプロイ要否
**要(ただしB案件の既存ゲートに合流)** — slides.py/schema変更は MCP(video_algorithm) の再ビルド＋OpenClaw再デプロイで本番反映、現状メモリ通り「B本番反映=MCP再ビルド+OpenClawデプロイ待ち」の同一バッチに乗せれば追加デプロイ作業は不要。ローカル実走・PRレビューは `scripts/demo_video_algorithm_local.py` で完結し、本番AWS書込みを伴わないため auto-mode classifier のブロック対象外。

---

### 関連ファイル(絶対パス)
- 実装対象: `/Users/s-komata/Documents/teamagent-orchestrator-poc/src/teamagent/skills/video_algorithm/slides.py`
- データ契約: `/Users/s-komata/Documents/teamagent-orchestrator-poc/src/teamagent/skills/video_algorithm/schema.py`(`CrossSynthesis` L368〜・`VideoAlgorithmOutput` L445〜)
- PPTX変換(既存・要素単位スクショ): `/Users/s-komata/Documents/teamagent-orchestrator-poc/src/teamagent/skills/video_algorithm/pptx_export.py`(`.slide` locatorで章を1画像化するため、追加章は自動でPPTX側にも反映)
- ローカル実走: `/Users/s-komata/Documents/teamagent-orchestrator-poc/scripts/demo_video_algorithm_local.py`

---

## 付録: 抽出要素 全リスト（JSON）

```json
[
 {
  "section": "01 ご与件の整理（クライアント要件・課題・ゴール）",
  "element": "表紙：施策タイトル＋提案日＋宛先",
  "purpose": "誰の・何の・いつの提案かを1枚で確定し、ドキュメントの所有権と鮮度を示す",
  "required_info": [
   "クライアント正式名称（御中表記）",
   "対象事業・店舗・商材名",
   "施策の一言サマリ（例: 開業PRショート動画施策）",
   "提案日（YYYY.MM.DD）",
   "提案社名/ロゴ"
  ],
  "research_source": "クライアント提供",
  "storage": "提案書要素DB（メタ情報テーブル: client_name/product/date/title）",
  "deck_example": "「無添蔵新宿店 開業PRショート動画施策のご提案」/ くら寿司御中 / 2026.06.04",
  "vseo_coverage": "already"
 },
 {
  "section": "01 ご与件の整理（クライアント要件・課題・ゴール）",
  "element": "アジェンダ（目次：章構成＋ページ参照）",
  "purpose": "提案全体のストーリーライン（与件→市場変化→文脈設計→展開→スケジュール→KPI）を冒頭で握り、読み手の期待値を整える",
  "required_info": [
   "章タイトル一覧（6章程度の定型: 与件整理/市場アルゴリズム/勝ち筋文脈/展開イメージ/スケジュール/KPI見積）",
   "各章の開始ページ番号"
  ],
  "research_source": "複合",
  "storage": "提案書要素DB（章構成テンプレート＋自動採番）",
  "deck_example": "ご与件の整理 P.3 / アルゴリズムの変化 P.6 / 箱推し文脈の設計 P.17 / 展開イメージ P.30 / スケジュール P.48 / KPI・お見積 P.50",
  "vseo_coverage": "missing"
 },
 {
  "section": "01 ご与件の整理（クライアント要件・課題・ゴール）",
  "element": "章扉：01 ご与件の整理（セクションディバイダー）",
  "purpose": "「01 与件整理」セクションの開始を視覚的に区切り、章番号とタイトルを提示する",
  "required_info": [
   "章番号（01）",
   "章タイトル（ご与件の整理）"
  ],
  "research_source": "複合",
  "storage": "提案書要素DB（章扉テンプレート：番号＋タイトルのみ）",
  "deck_example": "01. ご与件の整理",
  "vseo_coverage": "missing"
 },
 {
  "section": "01 ご与件の整理（クライアント要件・課題・ゴール）",
  "element": "クライアント基礎情報＆商材ファクト（ブランド/店舗概要・USP・提供価値）",
  "purpose": "提案の前提となる商材の固有事実（立地・コンセプト・看板メニュー・ブランドポジション）を整理し、後段の勝ち筋文脈に接続する根拠を置く",
  "required_info": [
   "ブランド/店舗のポジショニング（例: ハイグレードブランド）",
   "USPファクト3点（立地・鮮度・空間など差別化要素）",
   "コンセプト/世界観の一言",
   "シグネチャー商品・キラーメニューのリスト",
   "ターゲット利用シーン（デート/お酒/女子会等）",
   "出店背景（◯店舗目・エリア戦略）"
  ],
  "research_source": "クライアント提供",
  "storage": "提案書要素DB（client_facts: usp[]/concept/signature_menu[]/scene[]）。VSEO横断シンセシスのclient_pitchと相互参照",
  "deck_example": "新宿駅直結／新幹鮮魚／大人の隠れ蔵、コンセプト『日常の中の“非日常”』、関東2店舗目のハイグレードブランド、ミルフィーユ鉄火・生本まぐろ中落ち等",
  "vseo_coverage": "partial"
 },
 {
  "section": "01 ご与件の整理（クライアント要件・課題・ゴール）",
  "element": "課題・ゴール・与件サマリ（オリエン要件の構造化）",
  "purpose": "クライアントが解きたい課題・達成したいゴール・制約条件（予算/時期/対象施策）を1枚に圧縮し、提案のスコープを宣言する",
  "required_info": [
   "施策の目的/解決したい課題（例: 新規開業の認知・来店獲得）",
   "達成ゴール/KGI（来店・指名検索・POS等の方向性）",
   "対象チャネル/施策タイプ（ショート動画PR）",
   "想定予算レンジ・時期/開業日などの制約",
   "ターゲット層の定義（年代/性別/利用シーン）"
  ],
  "research_source": "クライアント提供",
  "storage": "提案書要素DB（brief: objective/kgi/scope/budget_range/timeline/target）。オリエンシートをパース",
  "deck_example": "無添蔵新宿店の開業PRをショート動画で実施（梅900万〜松1500万円のレンジ、7/3撮影想定の開業タイミング）",
  "vseo_coverage": "missing"
 },
 {
  "section": "02. 市場・消費者行動の変化／アルゴリズム型情報流通（Slide 6-16）",
  "element": "セクション扉（市場・消費者行動の変化）",
  "purpose": "提案の論拠パートへ切り替える章扉。なぜショート動画なのかの論理導入を宣言する",
  "required_info": [
   "章番号・章タイトル文言",
   "サブテーマ（アルゴリズム型情報流通など提案の世界観を一言で）"
  ],
  "research_source": "OC社内ナレッジ(Slack/Drive)",
  "storage": "提案書要素DB",
  "deck_example": "「02. ご提案にあたって：市場・消費者行動の変化／アルゴリズム型情報流通時代の到来（再掲含む）」",
  "vseo_coverage": "missing"
 },
 {
  "section": "02. 市場・消費者行動の変化／アルゴリズム型情報流通（Slide 6-16）",
  "element": "市場トレンド主張：情報流通の主役交代（一次統計1枚）",
  "purpose": "公式発信より第三者・切り抜きの方が流通する地殻変動を、検証可能な一次統計で立てる起点スライド",
  "required_info": [
   "時事性ある一次統計値（例:第三者発信が占める比率%）",
   "出典URL（調査会社/メディア/政府/業界レポート）",
   "調査時点・対象",
   "1行の含意（だからショート動画が効く）"
  ],
  "research_source": "Web一次統計(調査会社/政府/業界)",
  "storage": "pgvector/connector_state",
  "deck_example": "参院選の動画視聴のうち政党/候補者の公式発信は10%、90%近くが切り抜き等の第三者ショート動画（go2senkyo出典）",
  "vseo_coverage": "missing"
 },
 {
  "section": "02. 市場・消費者行動の変化／アルゴリズム型情報流通（Slide 6-16）",
  "element": "購買インサイト：信頼源が公式→他者体験へ（口コミ統計）",
  "purpose": "購買意思決定で他者評価が公式情報を上回る事実を示し、UGC獲得の必然性を裏づける",
  "required_info": [
   "レビュー参照率%",
   "「最も参考にする情報＝口コミ」の比率%と比較対象（価格/公式情報）",
   "出典・調査主体",
   "商材カテゴリで体験評価が効く理由の1行"
  ],
  "research_source": "Web一次統計(調査会社/政府/業界)",
  "storage": "pgvector/connector_state",
  "deck_example": "レビュー記事参照者95%以上、購買時に最も参考＝口コミ31.8%で価格23.4%を上回りトップ",
  "vseo_coverage": "missing"
 },
 {
  "section": "02. 市場・消費者行動の変化／アルゴリズム型情報流通（Slide 6-16）",
  "element": "ショート動画＝第一接触＆購買起点（プラットフォーム統計）",
  "purpose": "短尺縦型動画が発見〜購買サイクルの新たな起点であることを定量で示し、媒体選定を正当化",
  "required_info": [
   "視聴→購買行動への転換率%",
   "コマース規模（TikTok Shop GMV等の金額）",
   "利用率推移・ユーザー平均年齢",
   "高購入率の世代セグメント",
   "出典URL"
  ],
  "research_source": "Web一次統計(調査会社/政府/業界)",
  "storage": "pgvector/connector_state",
  "deck_example": "短尺視聴者の4割が購買行動、TikTok Shop 12月GMV60億円、平均年齢39.2歳、32-35歳のライブ購入率33.3%（commercepick出典）",
  "vseo_coverage": "missing"
 },
 {
  "section": "02. 市場・消費者行動の変化／アルゴリズム型情報流通（Slide 6-16）",
  "element": "媒体比較：TV CM『広告』 vs ショート動画『コンテンツ』",
  "purpose": "ショート動画はタイムラインに溶け込み受容されやすいという質的優位を対比図で示す",
  "required_info": [
   "TV CMとショート動画の受容構造の対比軸（広告として現れる/コンテンツとして溶け込む）",
   "比較を体感させる投稿ビジュアル複数枚",
   "1行のテイクアウェイ"
  ],
  "research_source": "複合",
  "storage": "編集可HTMLのdata属性",
  "deck_example": "「TV CMはコンテンツの中の“広告”、ショート動画はタイムライン上の“コンテンツ”として溶け込む」対比（投稿画像12枚）",
  "vseo_coverage": "missing"
 },
 {
  "section": "02. 市場・消費者行動の変化／アルゴリズム型情報流通（Slide 6-16）",
  "element": "若年層の検索行動シフト（指名検索の前提：◯◯る化）",
  "purpose": "若年層の情報探索がショート動画プラットフォームに移行した事実を示し、VSEO（検索面攻略）の前提を立てる",
  "required_info": [
   "若年層の検索ツールランキング/1位媒体",
   "検索ツール化を示す呼称・愛称（◯◯る等）",
   "出典・調査主体",
   "“動画で検索する時代”の1行含意"
  ],
  "research_source": "Web一次統計(調査会社/政府/業界)",
  "storage": "pgvector/connector_state",
  "deck_example": "10〜20代の検索ツール第1位にTikTokが台頭、愛称“ティクる”",
  "vseo_coverage": "partial"
 },
 {
  "section": "02. 市場・消費者行動の変化／アルゴリズム型情報流通（Slide 6-16）",
  "element": "中高年層への接触拡大（ターゲット汎用性の補強）",
  "purpose": "若年層に閉じず上の世代にも届く＝商材ターゲットを問わず有効だと示し反論を先回りで潰す",
  "required_info": [
   "上位世代（40代以降等）のショート動画視聴/活用率",
   "“役立つ情報として活用”を示すデータ",
   "出典",
   "商材ターゲット世代との接続1行"
  ],
  "research_source": "Web一次統計(調査会社/政府/業界)",
  "storage": "pgvector/connector_state",
  "deck_example": "40代以降でもショート動画が視聴され『役立つ情報』として活用される時代に",
  "vseo_coverage": "missing"
 },
 {
  "section": "02. 市場・消費者行動の変化／アルゴリズム型情報流通（Slide 6-16）",
  "element": "アルゴリズム機構の解説（フリークエンシー獲得の仕組み）",
  "purpose": "一度の興味が反復接触に増幅される仕組みを説明し、複数アカ・複数投稿の打ち手の根拠にする",
  "required_info": [
   "レコメンドが反復接触を生む機序の説明",
   "施策設計原則（例:1テーマ×マルチアカウント×マルチ投稿）",
   "裏づけとなる実投稿/事例URL",
   "図解素材"
  ],
  "research_source": "複合",
  "storage": "編集可HTMLのdata属性",
  "deck_example": "“1テーマ・マルチアカウント・マルチ投稿”でプラットフォームによる効率的フリークエンシー獲得が可能（実TikTok動画URL添付）",
  "vseo_coverage": "missing"
 },
 {
  "section": "02. 市場・消費者行動の変化／アルゴリズム型情報流通（Slide 6-16）",
  "element": "施策の中核仮説／KPIロジック（複数接触→行動）",
  "purpose": "本提案が依拠する因果仮説を一文で明文化し、後段のKPI・投稿本数設計の土台にする",
  "required_info": [
   "仮説の構成変数（複数アカウント×複数発信×複数接触）",
   "期待アウトカム（POS増/検索/オンライン購買等の行動指標）",
   "仮説を支える指標名・定義"
  ],
  "research_source": "OC社内ナレッジ(Slack/Drive)",
  "storage": "提案書要素DB",
  "deck_example": "一定期間に複数アカウントからの複数回発信×複数回接触で、POS増加・検索・オンライン購買に繋がる",
  "vseo_coverage": "missing"
 },
 {
  "section": "02. 市場・消費者行動の変化／アルゴリズム型情報流通（Slide 6-16）",
  "element": "フリークエンシー閾値の裏づけ調査（再現性の追い風）",
  "purpose": "“何回接触で話題と感じるか”の調査で、設計可能なフリークエンシーに再現性があると示す",
  "required_info": [
   "“話題と感じる接触回数”の調査結果（設問文・分布）",
   "調査対象N・属性・期間・調査主体",
   "出典",
   "再現性が高いという1行の解釈"
  ],
  "research_source": "Web一次統計(調査会社/政府/業界)",
  "storage": "pgvector/connector_state",
  "deck_example": "「何回でこの商品話題かなと思う？」設問、15〜44歳女性3,612人・2023年1月・トレンダーズ調べ",
  "vseo_coverage": "missing"
 },
 {
  "section": "02. 市場・消費者行動の変化／アルゴリズム型情報流通（Slide 6-16）",
  "element": "購買・来店モデルの変化（AISAS→SEESAS フレーム）",
  "purpose": "従来モデルから新モデルへの転換を図解し、提案全体の理論的バックボーン（出会い方＝UGC）を据える",
  "required_info": [
   "旧モデルの頭文字フレーム（AISAS等）と導線説明",
   "新モデルの頭文字フレーム（SEESAS等：Surf/Encounter→Engage→Search→Action→Share）と各段の定義",
   "新旧を分ける決定的差分（出会いが企業広告でなくUGC）",
   "フレーム図解素材"
  ],
  "research_source": "OC社内ナレッジ(Slack/Drive)",
  "storage": "提案書要素DB",
  "deck_example": "AISAS（CM/雑誌で認知→検索→購買）から、おすすめ接触(Surf/Encounter)→複数接触で興味(Engage)→検索/評判確認→購買のSEESASへ。出会いがUGCである点が重要",
  "vseo_coverage": "missing"
 },
 {
  "section": "03. 箱推し文脈の設計（店舗USP/ファクト＋「エリア×グルメ」上位動画の文脈・構成分析→勝ち筋文脈）／Slide 17-29",
  "element": "セクション扉（章タイトル）",
  "purpose": "「箱推し文脈の設計」というこの章のゴール（何を分析してどんな結論に至るか）を一言で宣言する区切り",
  "required_info": [
   "章タイトル文言",
   "本施策固有のキーフレーズ（例: 箱推し文脈/勝ち筋文脈）",
   "章番号"
  ],
  "research_source": "OC社内ナレッジ(Slack/Drive)",
  "storage": "提案書要素DB",
  "deck_example": "Slide 17「03.『箱推し文脈』の設計について」",
  "vseo_coverage": "missing"
 },
 {
  "section": "03. 箱推し文脈の設計（店舗USP/ファクト＋「エリア×グルメ」上位動画の文脈・構成分析→勝ち筋文脈）／Slide 17-29",
  "element": "分析アプローチ宣言（USP × 検索上位の交差ロジック）",
  "purpose": "「商材固有USP」と「エリア×ジャンル検索上位動画」を重ねた交差部分＝勝ち筋文脈、という分析の方法論を読み手に約束する",
  "required_info": [
   "商材固有の切り口/USPの一言",
   "想定検索クエリ群（エリア×ジャンル）",
   "交差＝勝ち筋というロジック図解の説明文"
  ],
  "research_source": "複合",
  "storage": "提案書要素DB",
  "deck_example": "Slide 18「無添蔵新宿店ならではの切り口と『エリア×グルメ』検索時の上位動画を分析。切り口が重なる部分＝本施策の勝ち筋文脈を探る」",
  "vseo_coverage": "partial"
 },
 {
  "section": "03. 箱推し文脈の設計（店舗USP/ファクト＋「エリア×グルメ」上位動画の文脈・構成分析→勝ち筋文脈）／Slide 17-29",
  "element": "商材USP・ファクトシート（店舗/商品の強み棚卸し）",
  "purpose": "勝ち筋文脈の片側＝商材側の独自素材（立地/コンセプト/シグネチャー商品/ブランド階層/利用シーン）を網羅的に可視化し、後段の文脈翻訳の原資にする",
  "required_info": [
   "立地/アクセスファクト",
   "コンセプト/ブランドポジション",
   "シグネチャー商品リスト（固有名）",
   "想定利用シーン",
   "ブランド階層内での位置づけ",
   "価格/グレード感の一言"
  ],
  "research_source": "クライアント提供",
  "storage": "提案書要素DB",
  "deck_example": "Slide 19「新宿駅直結／新幹鮮魚／大人の隠れ蔵」「ミルフィーユ鉄火・生本まぐろ中落ちセット…」「くら寿司のハイグレードブランド」",
  "vseo_coverage": "missing"
 },
 {
  "section": "03. 箱推し文脈の設計（店舗USP/ファクト＋「エリア×グルメ」上位動画の文脈・構成分析→勝ち筋文脈）／Slide 17-29",
  "element": "検索キーワード仮説の提示（流入KW定義）",
  "purpose": "商材へ流入が見込めるエリア×ジャンルの検索クエリ群を仮置きし、以降の上位動画分析の対象スコープを明示する",
  "required_info": [
   "対象エリア名",
   "ジャンル軸の5KW程度（グルメ/寿司/ランチ/ディナー/おでかけ等）",
   "KW選定の根拠（流入期待の理由）"
  ],
  "research_source": "複合",
  "storage": "提案書要素DB",
  "deck_example": "Slide 20「無添蔵新宿店の動画へ流入が期待できるキーワードを以下と仮定し…勝ち筋文脈を探る」",
  "vseo_coverage": "partial"
 },
 {
  "section": "03. 箱推し文脈の設計（店舗USP/ファクト＋「エリア×グルメ」上位動画の文脈・構成分析→勝ち筋文脈）／Slide 17-29",
  "element": "KW別 上位5動画ボード（実績数値＋サムネ）",
  "purpose": "各検索KWの上位動画を実データ（アカウント名/再生数/フォロワー数/サムネ）で並べ、後段の共通文脈抽出の一次エビデンスにする",
  "required_info": [
   "KWごとの上位5本：アカウント名",
   "再生数(views)",
   "フォロワー数(F)",
   "サムネ画像",
   "計測日/検索条件"
  ],
  "research_source": "TikTok実動画分析(VSEO)",
  "storage": "S3",
  "deck_example": "Slide 21「新宿 グルメ」#3コネクト東京グルメ 794,900 views / F43,500",
  "vseo_coverage": "already"
 },
 {
  "section": "03. 箱推し文脈の設計（店舗USP/ファクト＋「エリア×グルメ」上位動画の文脈・構成分析→勝ち筋文脈）／Slide 17-29",
  "element": "KW別 共通文脈・構成パターン抽出（3つの勝ち筋要素）",
  "purpose": "各KWの上位動画から「なぜ上位を取れるか」の共通フック/演出/保存誘発を3点に言語化し、そのKWのメイン文脈を一言で定義する",
  "required_info": [
   "KWごとのメイン文脈の一言コピー",
   "共通要素3点（冒頭フック/視覚演出/保存誘発の型）と各説明",
   "各要素が効く心理メカニズムの記述"
  ],
  "research_source": "TikTok実動画分析(VSEO)",
  "storage": "編集可HTMLのdata属性",
  "deck_example": "Slide 22「新宿 寿司」=『価格訴求×インパクト盛り』／#1衝撃価格の初速フック『150円〜』『10円寿司』を冒頭3秒",
  "vseo_coverage": "already"
 },
 {
  "section": "03. 箱推し文脈の設計（店舗USP/ファクト＋「エリア×グルメ」上位動画の文脈・構成分析→勝ち筋文脈）／Slide 17-29",
  "element": "マルチKW入賞動画の構造解剖（1本=複数検索意図ケース）",
  "purpose": "複数の検索KWで同時上位を取る単一動画を分解し、その横断的に勝てる構造（網羅性/利用シーン多面化/保存率最大化）を抽出して再現可能化する",
  "required_info": [
   "対象アカウント名/再生数/フォロワー数",
   "同時入賞したKWと各順位＋検索意図段階（顕在/準顕在/潜在）",
   "構造分析3点",
   "商材への翻訳（どう同型再現するか）の一文"
  ],
  "research_source": "TikTok実動画分析(VSEO)",
  "storage": "編集可HTMLのdata属性",
  "deck_example": "Slide 26 @balilax_shinjuku F47人/47,500views が3KW同時入賞→無添蔵翻訳『ビル内POV導線×席種3バリエーション』",
  "vseo_coverage": "partial"
 },
 {
  "section": "03. 箱推し文脈の設計（店舗USP/ファクト＋「エリア×グルメ」上位動画の文脈・構成分析→勝ち筋文脈）／Slide 17-29",
  "element": "2KW入賞動画クラスタの俯瞰（再現性の量的裏取り）",
  "purpose": "複数KWで入賞する動画が単発でなく多数存在することを一覧で示し、勝ち筋構造（多軸掛け算/3選/多面体）の汎用性・再現性を担保する",
  "required_info": [
   "2KW入賞動画群のアカウント名/再生数/フォロワー数",
   "各動画の入賞KW組合せ",
   "共通する構造分析3点（掛け算構造/3選フォーマット/利用シーン多面体）"
  ],
  "research_source": "TikTok実動画分析(VSEO)",
  "storage": "S3",
  "deck_example": "Slide 27 むにぐるめ(サブアカ)1.5M views F31k『グルメ・ランチ2KW入賞』ほか8本＋『945円食べ放題×ウニ×出汁茶漬け』掛け算例",
  "vseo_coverage": "partial"
 },
 {
  "section": "03. 箱推し文脈の設計（店舗USP/ファクト＋「エリア×グルメ」上位動画の文脈・構成分析→勝ち筋文脈）／Slide 17-29",
  "element": "上位動画 切り口の総ざらい（採用候補の要素カタログ）",
  "purpose": "全KW分析から得た勝ちパターンを一望のチップ群に圧縮し、商材に転用する候補要素のショートリストとして提示する",
  "required_info": [
   "全KW横断で抽出した勝ち切り口の短語リスト（10個前後）",
   "商材に活かせるものを抽出する旨の方針文"
  ],
  "research_source": "TikTok実動画分析(VSEO)",
  "storage": "編集可HTMLのdata属性",
  "deck_example": "Slide 28「まだバレてない新宿」「画映えキラーメニュー」「日常移動導線スタート」「3選まとめ」「衝撃価格」「喧騒と静寂のギャップ」等",
  "vseo_coverage": "already"
 },
 {
  "section": "03. 箱推し文脈の設計（店舗USP/ファクト＋「エリア×グルメ」上位動画の文脈・構成分析→勝ち筋文脈）／Slide 17-29",
  "element": "勝ち筋文脈の結論（USP×検索面の掛け算＋訴求コピー仮案）",
  "purpose": "商材USPとVSEO上位文脈を掛け合わせた本施策の勝ち筋文脈を結論として宣言し、具体的な訴求コピー案（仮）に落とし込む",
  "required_info": [
   "勝ち筋文脈の定義文（USP×『エリア×○○』VSEO）",
   "訴求コピー案 複数（商材USPと勝ち文脈を融合した実フレーズ）",
   "コピーが仮である旨の注記"
  ],
  "research_source": "複合",
  "storage": "編集可HTMLのdata属性",
  "deck_example": "Slide 29『まだバレてない！新宿に誕生した関東2店舗目のプレミアムくら寿司』『無添蔵のまぐろミルフィーユが美味すぎた』※文脈は仮",
  "vseo_coverage": "partial"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "セクション扉（展開イメージ・キャスティング設計）",
  "purpose": "分析パート(勝ち筋文脈)から実行パート(誰がどう投稿するか)への切り替えを宣言する章区切り",
  "required_info": [
   "章番号と章タイトル",
   "本章で示す要素の予告（任意）"
  ],
  "research_source": "OC社内ナレッジ(Slack/Drive)",
  "storage": "提案書要素DB",
  "deck_example": "「04. くら寿司様展開イメージ」(Slide 30)",
  "vseo_coverage": "missing"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "アルゴリズム活用 成功事例（リファレンス）",
  "purpose": "提案する『1テーマ・複数アカウント・複数投稿』モデルが既に他社で機能した実証として説得力を担保する",
  "required_info": [
   "参照ブランド名",
   "施策内容の要約",
   "成果指標(再生/フォロワー増/POS/指名検索等の数値)",
   "なぜアルゴリズム的に効いたかの説明",
   "投稿サムネ/画面キャプチャ"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "提案書要素DB",
  "deck_example": "ユニクロのSNSアルゴリズム活用事例（おすすめ面での連続接触で同様コンテンツが配信され続ける構造）(Slide 31-32)",
  "vseo_coverage": "missing"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "施策方針（コンテンツ生成戦略の基本構造）",
  "purpose": "本提案の中核ロジック=『TOPクリエイター起点で話題の核を作り、UGCライク投稿を面的に展開して自発的UGCを誘発する』を1枚で言語化する",
  "required_info": [
   "商材カテゴリ特有の生活者行動仮説",
   "話題の核→面形成→自発UGCの導線説明",
   "起点となる投稿タイプと拡散投稿タイプの役割分担"
  ],
  "research_source": "OC社内ナレッジ(Slack/Drive)",
  "storage": "提案書要素DB",
  "deck_example": "『トップグルメクリエイターの投稿を起点に話題の核を形成、UGCライク投稿を複数展開して面的に投稿文脈を形成→自発的UGC拡散を促進』(Slide 33)",
  "vseo_coverage": "partial"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "全体スキーム図（キャスティング階層ピラミッド）",
  "purpose": "TOP/ミドル/マイクロ/メディアの各レイヤーが果たす役割と量的関係を1枚の図で俯瞰させ、施策の全体像を握らせる",
  "required_info": [
   "各レイヤーの呼称(TOPKOL/ミドル/マイクロ/トレンドメディア等)",
   "各レイヤーの本数・人数規模感",
   "各レイヤーの役割コピー(核creation/拡散/接触頻度/トレンド拡散)",
   "矢印で示す波及の方向性"
  ],
  "research_source": "OC社内ナレッジ(Slack/Drive)",
  "storage": "提案書要素DB",
  "deck_example": "トップKOL／グルメ特化クリエイター／ミドル・EMME／トレンドメディア／マイクロ模倣型UGC(大量投稿)の5層スキーム。各層に『話題の核を創出』『核を広げる』『トレンド拡散』『接触回数増加』の役割注記(Slide 34-35)",
  "vseo_coverage": "missing"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "第三者調査による方針の裏付け（プラットフォーム公式データ）",
  "purpose": "『複数コンテンツ・複数文脈』戦略がプラットフォーム自身/調査会社にも推奨されていることを示し、提案の客観性を補強する",
  "required_info": [
   "調査主体名(調査会社×プラットフォーム)",
   "出典URL",
   "戦略を支持する具体スタッツ(専用コンテンツ選好率/購買意向リフト/好感度リフト等)",
   "示唆の要約3点程度"
  ],
  "research_source": "Web一次統計(調査会社/政府/業界)",
  "storage": "pgvector/connector_state",
  "deck_example": "Ipsos×TikTok調査『Return on Creative』より、TikTok専用コンテンツ選好79%／TVCM流用→TikTokファーストで購買意向+37%・ブランド好感度+38%(Slide 36)",
  "vseo_coverage": "missing"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "TOP/ミドルKOLキャスティング（自社トップクリエイター＋外部KOL）",
  "purpose": "話題の核を担う具体的なトップ人選とそのフォロワー規模を提示し、起用の現実性とリーチ上限を見せる",
  "required_info": [
   "自社保有トップアカウント名とフォロワー数",
   "起用想定の外部KOL人数レンジ",
   "各候補のフォロワー実数",
   "裏どり前提・案件受注後可否確認などの注記"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "提案書要素DB",
  "deck_example": "TC保有グルメアカウント『コネクト東京』＋外部グルメKOL2~5名。フォロワー42万／32.4万／21.3万／8.1万人を提示(Slide 37)",
  "vseo_coverage": "missing"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "起点アカウントの実績証明（受賞・カテゴリ影響力）",
  "purpose": "中核に置くアカウントが業界で実績/権威を持つことを受賞歴や横並び比較で証明し、起用価値を裏付ける",
  "required_info": [
   "アカウント名とフォロワー数",
   "受賞名・選出歴・出典",
   "同カテゴリ上位インフルエンサーとの比較(名前+フォロワー数)",
   "フォロワー計測日"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "提案書要素DB",
  "deck_example": "『コネクト東京グルメ(42万)』が2025年最も活躍したグルメ関係者4人で最優秀賞を受賞。東京グルメ102万/東京大人グルメもと30万/tatsuya25万と並置(計測日2025/12/1)(Slide 38)",
  "vseo_coverage": "missing"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "自社運営サブアカウント群の追加起用",
  "purpose": "同一運営体が持つ中小アカウントを束ねて起用することで、同一管理下で複数アカウント露出（=フリークエンシー）を担保できることを示す",
  "required_info": [
   "追加起用アカウント名/ハンドル",
   "各フォロワー数",
   "同一運営体である旨の注記"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "提案書要素DB",
  "deck_example": "同じくTC運営の追加4アカウント、フォロワー3.2万/1.8万/1.2万/7997人を今回合わせて起用想定(Slide 39)",
  "vseo_coverage": "missing"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "ミドル施策：自社グループSNSメディア起用",
  "purpose": "TOPほどのフォロワーはないが話題を広げる中間層として、自社保有メディアの規模感と実績で拡散の面を作れることを示す",
  "required_info": [
   "メディア名/アカウント名",
   "累計フォロワー数・月間インプレッション",
   "起用想定メディア数レンジ",
   "コンテンツ得意領域(まとめ/アレンジ動画等)",
   "過去実施企画事例"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "提案書要素DB",
  "deck_example": "ライフスタイル系『まよ/うに/メルフィー』、累計25万フォロワー以上・3億imp(月間)、×5~6メディア。EMME編集部の爆買い企画を事例提示(Slide 40)",
  "vseo_coverage": "missing"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "ミドル施策：投稿イメージ（クリエイティブ参考）",
  "purpose": "中間層メディアが実際に作る投稿のトーン&マナーを動画サムネで見せ、完成イメージを具体化する",
  "required_info": [
   "参考投稿の動画URL/サムネ画像(複数)",
   "各投稿の訴求軸の簡易ラベル(任意)"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "S3",
  "deck_example": "EMME系メディアの投稿サムネ4点(動画URL差し込み枠)(Slide 41)",
  "vseo_coverage": "missing"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "マイクロ施策：大量招致ソリューション説明",
  "purpose": "接触回数を稼ぐUGC風の量的投稿を、独自の一括可否取りネットワークで高速・大量に実現できる仕組みを説明する",
  "required_info": [
   "ネットワーク登録インフルエンサー総数",
   "招致フロー(可否取りシート→配信までの時間)",
   "ソリューションの差別化メリット3点",
   "リレーション構築済/専門ディレクション等の運用体制"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "提案書要素DB",
  "deck_example": "『速攻!インフルエンサーあつめるくん』、専門性を持つ1,500名をLINE公式登録、依頼から24時間以内に最大1,500名へ一括可否取り(Slide 42)",
  "vseo_coverage": "missing"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "マイクロ施策：ジャンルカバレッジ",
  "purpose": "招致ネットワークが商材に必要なジャンルを網羅していることを示し、適切な層へ届く確度を担保する",
  "required_info": [
   "保有ジャンルのカテゴリ一覧",
   "本案件で中心的に招致するジャンルの指定",
   "各ジャンルの代表アカウント例(任意)"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "提案書要素DB",
  "deck_example": "美容/家事・生活/グルメ・ライフスタイル/ママ・育児系をカバー、今回はグルメ・ライフスタイル系を中心に招致(Slide 43)",
  "vseo_coverage": "missing"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "マイクロ施策：アカウントイメージと起用ボリューム設計",
  "purpose": "予算に応じた起用人数とフォロワー帯を提示し、量的投稿のスケール調整可能性を示す",
  "required_info": [
   "平均フォロワー数",
   "予算別の起用人数レンジ",
   "代表アカウントのフォロワー実数サンプル",
   "投稿確約有無"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "提案書要素DB",
  "deck_example": "グルメ系マイクロ(平均FW1.5万人)を予算感に応じ10~40名起用想定。サンプル5.9万/2万/1.2万/9230人(Slide 44)",
  "vseo_coverage": "missing"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "トレンドメディア施策：媒体ネットワークとリスト",
  "purpose": "新商品・発表会・POPUP等の話題化を担うトレンド系メディア群の規模とリーチ実績、具体媒体リストを提示する",
  "required_info": [
   "累計再生回数などのネットワーク規模指標",
   "起用想定媒体数レンジ",
   "媒体名とTikTok/Instagramハンドル(URL)一覧",
   "対応可能な取材対象(新商品/記者発表/POPUP等)"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "提案書要素DB",
  "deck_example": "累計再生5000万回以上のトレンド系メディア×20~30媒体。トレステ/イベントナビ/ぷちとれ/TrendNewsTV等のハンドルをリスト化(Slide 45)",
  "vseo_coverage": "missing"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "トレンドメディア施策：投稿イメージ（クリエイティブ参考）",
  "purpose": "トレンドメディアが作る取材型投稿のトーンを動画サムネで見せ、完成イメージを具体化する",
  "required_info": [
   "参考投稿の動画URL/サムネ画像(複数)"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "S3",
  "deck_example": "トレンドメディア投稿サムネ4点(動画URL差し込み枠)(Slide 46)",
  "vseo_coverage": "missing"
 },
 {
  "section": "04 展開イメージ・キャスティング設計（Slide 30-47）",
  "element": "トレンドメディア施策：招致型イベント事例（話題形成の実績）",
  "purpose": "記者発表会等にメディア/マイクロを大量招致して話題を作った過去案件で、同型施策の再現性を証明する",
  "required_info": [
   "事例ブランド/商品名",
   "招致した人数(メディア+マイクロ)",
   "投稿プラットフォーム",
   "得られた話題形成の成果",
   "当日の投稿/会場キャプチャ"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "提案書要素DB",
  "deck_example": "エギョン産業(LUNA)事例：記者発表会にトレンドメディア/マイクロ100名招致しTikTok/Instagram投稿、新商品発表時の大きな話題形成(Slide 47)",
  "vseo_coverage": "missing"
 },
 {
  "section": "05 スケジュール (Slide 48-49)",
  "element": "セクション扉（章タイトル）",
  "purpose": "提案書の章立てを示し「これからスケジュールを説明する」と読み手の頭を切り替える区切りページ",
  "required_info": [
   "章番号(例: 05)",
   "章タイトル文言(例: スケジュール)",
   "提案書の章構成順序(アジェンダとの整合)"
  ],
  "research_source": "クライアント提供",
  "storage": "提案書要素DB",
  "deck_example": "Slide48「05. スケジュール」",
  "vseo_coverage": "missing"
 },
 {
  "section": "05 スケジュール (Slide 48-49)",
  "element": "ガントチャート時間軸（週/月のヘッダー）",
  "purpose": "施策全体を時間軸（横軸）で俯瞰させ、提案〜投稿までの期間感とマイルストーンの前後関係を一目で伝える",
  "required_info": [
   "施策開始〜終了の総期間(週数or月数)",
   "週/月の刻み単位",
   "起点日(キックオフ or 提案日)",
   "投稿配信ピーク時期"
  ],
  "research_source": "複合",
  "storage": "提案書要素DB",
  "deck_example": "「※7/3撮影日の場合」を基準にした週次の進行レーン",
  "vseo_coverage": "missing"
 },
 {
  "section": "05 スケジュール (Slide 48-49)",
  "element": "施策スイムレーン（施策タイプ別の並行進行行）",
  "purpose": "TOP KOL／ベクトルメディア／マイクロ／トレンドメディア等の施策を行ごとに並べ、並行進行と役割分担を構造的に見せる",
  "required_info": [
   "展開する施策タイプ一覧(プラン要素と一致)",
   "各施策のタスク粒度",
   "施策間の依存関係(どれが先行するか)"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "提案書要素DB",
  "deck_example": "「インフルエンサーバンク投稿(IB投稿)」「EMME/トレンドメディア／インフルエンサー投稿」「マイクロ可否取り」のレーン",
  "vseo_coverage": "missing"
 },
 {
  "section": "05 スケジュール (Slide 48-49)",
  "element": "撮影・制作マイルストーン（ロケハン/撮影/内覧会）",
  "purpose": "コンテンツ制作のキー日程（撮影日・内覧会・ロケハン）を打点し、クライアントの店舗側準備が必要なポイントを明示する",
  "required_info": [
   "撮影候補日",
   "ロケハン候補週",
   "内覧会/記者発表の有無と日程",
   "店舗オープン日との関係"
  ],
  "research_source": "クライアント提供",
  "storage": "提案書要素DB",
  "deck_example": "「撮影」「内覧会」「※15日週辺りでロケハンのお伺いは可能でしょうか？」「※7/3撮影日の場合」",
  "vseo_coverage": "missing"
 },
 {
  "section": "05 スケジュール (Slide 48-49)",
  "element": "準備フェーズタスク（オリエン/構成案FIX・可否取り・声掛け）",
  "purpose": "投稿前の事前準備タスク（オリエンシートFIX・構成案FIX・インフルエンサー選定/可否取り）を並べ、リードタイムの長さと着手の早さの必要性を訴求する",
  "required_info": [
   "オリエン/構成案のFIX所要日数",
   "可否取り(キャスティング確認)の所要リードタイム",
   "インフルエンサー選定〜声掛けの工数",
   "各タスクの開始トリガー"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "提案書要素DB",
  "deck_example": "「オリエンシートFIX」「EMME構成案FIX」「マイクロ可否取り」「インフルエンサー選定／声掛け」",
  "vseo_coverage": "missing"
 },
 {
  "section": "05 スケジュール (Slide 48-49)",
  "element": "クライアント意思決定ゲート（発注/実施ご判断）",
  "purpose": "「ここまでに発注判断が必要」という意思決定ポイントを明示し、提案承認のデッドラインをクライアントに認識させる",
  "required_info": [
   "発注/実施判断が必要な最終期限",
   "判断が遅れた場合に後ろ倒しになるタスク",
   "クライアント側の社内決裁フロー想定"
  ],
  "research_source": "クライアント提供",
  "storage": "提案書要素DB",
  "deck_example": "「ご提案」→「実施ご判断」のゲート配置",
  "vseo_coverage": "missing"
 },
 {
  "section": "06 KPI/お見積（保証回数・配信・手数料・#PR）— Slide 50-51",
  "element": "セクション扉（章ナンバー＋章タイトル）",
  "purpose": "提案書の最終章「KPI/お見積」への章切り替えを明示し、読み手の頭を「費用対効果の意思決定」モードに切り替える",
  "required_info": [
   "章番号（例: 06）",
   "章タイトル文言",
   "全体アジェンダ上の対応ページ番号（目次と整合）"
  ],
  "research_source": "クライアント提供",
  "storage": "提案書要素DB（章メタ: section_no/title/agenda_page）",
  "deck_example": "「06. KPI/御見積」/ アジェンダ上 P.50",
  "vseo_coverage": "missing"
 },
 {
  "section": "06 KPI/お見積（保証回数・配信・手数料・#PR）— Slide 50-51",
  "element": "プラン階層フレーム（松竹梅の3グレード設計）",
  "purpose": "予算帯の異なる3案を横並びで提示し、クライアントに「やる/やらない」でなく「どれにするか」を選ばせる意思決定フレームを作る",
  "required_info": [
   "プラン本数とネーミング（例: 梅/竹/松）",
   "各プランの総額（円）",
   "各プランの一言コンセプト差分（起用規模/再生保証の差）",
   "推奨プラン（どれを本命に置くか）"
  ],
  "research_source": "複合",
  "storage": "提案書要素DB（plans[]: name/total_price/positioning）",
  "deck_example": "梅プラン900万円／竹プラン1200万円／松プラン1500万円",
  "vseo_coverage": "missing"
 },
 {
  "section": "06 KPI/お見積（保証回数・配信・手数料・#PR）— Slide 50-51",
  "element": "施策①費目: TOP KOL施策（起用構成×投稿数×再生保証×金額）",
  "purpose": "提案の中核である上位KOL/自社トップアカウント起用の中身と単価・保証を明示し、最も金額が大きい費目の妥当性を裏付ける",
  "required_info": [
   "自社トップアカウント名と起用本数",
   "外部KOL起用人数レンジ（例: 1〜2名／3〜5名）",
   "費目金額（円）",
   "投稿本数レンジ",
   "再生回数保証値（万回）",
   "プランごとの増減差分"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "提案書要素DB（line_item: top_kol{accounts/external_n/price/posts/view_guarantee} × plan）",
  "deck_example": "松: コネクト東京＋4メディア＋外部インフルエンサー3〜5名／500万円/8〜10投稿/200万回再生保証",
  "vseo_coverage": "missing"
 },
 {
  "section": "06 KPI/お見積（保証回数・配信・手数料・#PR）— Slide 50-51",
  "element": "施策②費目: 自社グループメディア施策（媒体数×投稿数×再生保証×金額）",
  "purpose": "ミドル層のボリューム供給源である自社保有メディア群の動員規模を数量で示し、再生保証の母数の大半をここで担保する根拠にする",
  "required_info": [
   "自社メディアの種別と動員アカウント/媒体数（例: トレンド20/EMME5）",
   "費目金額（円）",
   "投稿本数（保証値）",
   "再生回数保証値（万回）",
   "プランごとの媒体数・投稿数の増減"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "提案書要素DB（line_item: owned_media{accounts/posts/price/view_guarantee} × plan）",
  "deck_example": "竹: ベクトルメディアトレンド30アカウント＋EMME6アカウント／600万円/71投稿/270万回再生保証",
  "vseo_coverage": "missing"
 },
 {
  "section": "06 KPI/お見積（保証回数・配信・手数料・#PR）— Slide 50-51",
  "element": "施策③費目: マイクロインフルエンサー招致施策（起用人数×平均FW×投稿確約×金額）",
  "purpose": "UGC的な面の広がりを担う大量招致施策の規模と前提条件（平均フォロワー・投稿確約有無）を明示し、CPあたりの納得感を作る",
  "required_info": [
   "起用人数（プランごと）",
   "想定平均フォロワー数",
   "投稿確約の有無",
   "費目金額（円）",
   "対象ジャンル（グルメ/ライフスタイル等）"
  ],
  "research_source": "自社実績DB(過去事例/KOL台帳)",
  "storage": "提案書要素DB（line_item: micro{n/avg_fw/post_guaranteed/price} × plan）",
  "deck_example": "松: 40名起用／平均FW1.5万人想定／投稿確約付／400万円",
  "vseo_coverage": "missing"
 },
 {
  "section": "06 KPI/お見積（保証回数・配信・手数料・#PR）— Slide 50-51",
  "element": "総再生保証サマリー（プラン別の合計保証KPI）",
  "purpose": "各費目の再生保証を積み上げた「このプランで最低これだけ回る」という合計KPIを一行で提示し、費用対効果（円/再生）の判断軸を与える",
  "required_info": [
   "プランごとの総再生保証値（万回）",
   "（任意）総額÷総再生で算出するCPV",
   "保証の定義（どの面の再生を合算するか）"
  ],
  "research_source": "複合",
  "storage": "提案書要素DB（plan.total_view_guarantee / 派生CPV）",
  "deck_example": "梅330万再生／竹370万再生／松470万再生",
  "vseo_coverage": "missing"
 },
 {
  "section": "06 KPI/お見積（保証回数・配信・手数料・#PR）— Slide 50-51",
  "element": "見積注記・取引条件（手数料率/超過分の扱い/AD配信内包/#PR表記）",
  "purpose": "金額表に乗らない取引条件と法令順守事項を脚注で明示し、契約時の認識齟齬とステマ規制リスクを事前に潰す",
  "required_info": [
   "進行手数料率（%）と費用への内/外の別",
   "保証超過分の請求方針",
   "AD配信費の内包有無と配信額の調整主体",
   "全投稿への#PR表記（景表法/ステマ規制対応）",
   "（任意）保証未達時の補填条件"
  ],
  "research_source": "OC社内ナレッジ(Slack/Drive)",
  "storage": "提案書要素DB（terms[]: fee_rate/overage_policy/ad_included/pr_disclosure）",
  "deck_example": "別途進行手数料15％／保証超過分は請求なし／費用内にAD配信を含む／全投稿に「#PR」表記",
  "vseo_coverage": "missing"
 }
]
```

---

## 5. 冗長性（多層フォールバック）設計：04 キャスティング / 06 KPI・見積（2026-06-17 追記）

方針: 04/06 は「自社実績DB（KOL台帳）＋プラン/料金ナレッジ＋戦績」が引ければ埋まる。
ただし **F数は鮮度劣化・料金は機微・台帳は穴あり** なので、**ハードフェイルしない多層フォールバック**で組む。
データが欠けても「提案書の骨格は必ず立つ」＝営業が穴だけ直す運用。

### データモデル（ingest 対象＝Drive/Sheets を構造化）
- `casting_roster`（KOL/媒体台帳）: `name / platform / tier(top|mid|micro) / genre[] / follower_count / follower_measured_at / base_cost / availability / account_url`
- `campaign_record`（戦績）: `brand / date / kol_refs[] / views / engagement / result_note`（過去の実成果）
- `plan_template`（松竹梅の構造・常に存在）＋ `pricing_knowledge`（費目別 単価/保証回数/CPV・過去提案由来）

### 04 キャスティング — フォールバック4層（上から優先・冗長性）
1. **L1 実データ**: 台帳の具体KOL（名前＋現F＋戦績）。`follower_measured_at` で鮮度判定。
2. **L2 近似**: 該当KOLが古い/不在 → 同 genre×tier の代替KOL＋`campaign_record` を proxy（「同ジャンル〇〇で△万再生実績」）。
3. **L3 tier集約**: 具体が無い → tier平均で枠だけ立てる（「ミドル10名・平均F1.5万・想定リーチ◯◯」）＝プラン構造は崩さない。
4. **L4 要確認**: 何も無い → `要確認（台帳未整備）` プレースホルダ。
- **源の冗長化**: 台帳Sheets＋過去提案デッキ(ingested)＋実績レポートを **相互補完**（1源欠けても他で埋める）。

### 06 KPI・見積 — フォールバック4層（料金は機微なのでHTMLには確定値を出さない）
1. **L1 実料金**: `pricing_knowledge` の費目単価 → 確定見積。
2. **L2 過去近似**: 類似予算/スコープの過去見積から**レンジ**（「類似案件で梅900〜松1500万」）。
3. **L3 テンプレ構造**: 松竹梅＋費目カテゴリ＋保証定義は常に出す。数値だけ `要確認`。
4. **L4 要確認**: プレースホルダ。
- **機微情報の隔離**: 確定料金は DB 側に保持し、編集可HTMLには**レンジ or 要確認のみ**（確定値は人間が最終入力）。RULES の「機微情報を公開HTML/ログに出さない」と整合。

### 編集可HTMLでの表現（既存 `_editable` ヘルパを拡張）
- 各要素ブロックに `data-fallback-level`（L1〜L4）と `data-source`（casting_db/pricing/web/oc）を持たせ、**どの確度のデータか**を色/バッジで可視化（L1=実데이터=緑 / L2-3=近似=黄 / L4=要確認=赤）。
- 営業は「赤と黄だけ直す」運用。`run_agent`（L2オーケストレーター）が L1→L4 の順で各要素を自動充填。

## 6. 5KW（エリア×ジャンル検索KW）の選定について
- **現状＝人間が選定**（その通り）。`video_algorithm` は今 **1KW入力**で、Platinumデッキの「新宿グルメ/新宿寿司/新宿ランチ」のような **5KWマトリクスは戦略判断＝人間**。
- **半自動の upgrade 余地**: 商材USP＋エリア＋**ラッコキーワード（検索ボリューム）** から LLM が **5KW候補を提案 → 人間が確定**。ローカル `tiktok-vseo-proposal` は既にラッコ連携あり＝流用可能。
- 結論: **「AI提案→人間確定」の半自動**が落としどころ（完全自動にはしない＝勝ち筋KWは戦略の核なので人間ゲート）。
