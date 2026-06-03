# VSEO 動画アルゴリズム読み解き分析 — 設計

作成: 2026-06-02 / 対象: TeamAgent 新 Skill `video_algorithm`（VSEO 概念の拡張）

営業が検索KWを1つ送ると、その**検索結果の上位5本**を取得して各動画を**マルチモーダルで深掘り分析**し、
「**なぜこの5本が検索上位なのか（＝アルゴリズムの読み解き）**」を VSEO 観点で言語化する機能。

## 0. 正直な前提（最重要・提案にも明記する）

- TikTok 公式が数値で公開しているのは For You の説明のみで、**「検索ランキングの重み」は非公開**。本機能が測るのは
  **上位5本に共通して観測できる表層特徴**であり、TikTok 内部の実シグナル（完了率実測・初速・視聴者属性一致・
  アカウント権威・鮮度）は非観測。
- **n=5 の小標本**：相関は「仮説生成」専用。有意性検定（p値）は出さない。**相関≠因果**、生存者バイアス（沈んだ動画は
  見えない）あり。出力は「相関する勝ち筋（必要条件の候補）」として提示し、検証はテスト投稿に委ねる。
- この誠実さを保つことが、AIくささ排除（[ui_design_principles_anti_ai.md] と同じ思想）＝信頼される提案の条件。

## 1. 入力と全体フロー

```
営業がKWを1つ送信（Slack: 「@TeamAgent VSEO分析 新宿 ランチ」）
  → search_tiktok(query, "keyword", max_videos=5)        # 上位5本＋エンゲージメント指標
  → 各動画: download_video → video_proxy(ensure_under_limit) → gemini.analyze_video_bytes（構造化JSON）
  → 5本分を横断スコアリング/比較（§4）
  → 営業向けサマリ（§5）＋ 機械可読JSON
```

## 2. 分析の土台：観測可能な VSEO ランキング要因（grounded）

TikTok 公式の3カテゴリ（①User interactions ②Video information=**captions/sounds/hashtags** ③Device settings）と、
信頼できる二次情報のコンセンサスから、**画面で観測できる**順位要因に絞る。

**A. キーワード適合（検索の肝）** — TikTok検索は動画を3層で読むとされる:
- キャプション本文（特に冒頭1行目にKW）／**画面内テロップ（OCR対象）**／**発話の音声書き起こし**（冒頭3秒でKW発話）／
  ニッチHT（3〜5個、`#fyp`等の汎用は無効）。

**B. エンゲージメント（重い順）** — 公式「強いシグナルほど重い」と整合:
- **完了率/視聴維持 ＞ 再視聴 ＞ シェア・保存（>いいね） ＞ コメント（質）**。検索文脈では**保存**が特に重要。
- 完了率は非観測 → **冒頭2秒フック・尺・ループ構造**で代理評価。

**C. 鮮度・投稿者**: フォロワー数・過去実績は**公式に「直接要因ではない」**＝フォロワー少でも上位なら A+B で勝っている＝
  再現可能な勝ち筋。トピック一貫性・投稿頻度は間接的に効く。

**D. 意図充足**: 検索は「クエリに直接答える明確な構成」を優遇（ハウツー61%/レビュー45%/体験談41% ※孫引き要確認）。

→ 詳細チェックリストは付録Bに（各上位動画を○×採点）。

## 3. 1本ごとの抽出（7観点 ＋ ★ブランド/物体認識）

各動画を Gemini に**構造化JSON**で吐かせる。原則: **時刻参照必須**（主張は秒数とセット＝実視聴の担保）、
**観測（事実）と解釈（順位への効き仮説）を別フィールド**に、**不明は `null`/`unknown`**（推測埋め禁止）。

| # | 観点 | 主な抽出内容 | VSEO的な効き |
|---|---|---|---|
| 1 | **フック(0-3秒)** | 型(問い/数字/衝撃/POV…)、冒頭テロップ有無、最初のフレーム（=サムネ） | 完了率の起点・検索一覧でのCTR |
| 2 | **テロップ** | 量/位置/スタイル、**焼き込み全文(秒付き)**、KW一致(秒・完全/部分) | OCR経由のKW適合＋保存率 |
| 3 | **★コンテンツ/ブランド認識** | 物体/場所/人物属性、**ブランド/ロゴ/看板検出**（例: 看板にユニクロ） | §3詳細↓ |
| 4 | **構成/編集** | 尺・カット数・テンポ・B-roll・展開 | 完了率/維持率の代理 |
| 5 | **訴求/CTA** | メインメッセージ、価値訴求型、利用シーン、**保存/フォロー等CTA** | 保存誘発＝検索最重要シグナル |
| 6 | **音源/音声** | レイヤー、トレンド音、ナレ、**発話内KW一致(秒)** | 音声書き起こし経由のKW適合 |
| 7 | **被写体/撮影様式** | UGC風〜作り込み、画角、カメラワーク、照明 | 検索面での好まれ方/転用コスト |

### ★ ブランド/物体認識（ユーザー強調点）= `BrandDetection`（1検出=1レコード）
「**何が・いつ(秒)・どの程度目立つか**」を構造化:
- `brand_name`（不明ロゴは `unidentified_logo`） / `detection_source`（看板/パッケージ/衣服ロゴ/店頭/画面UI/メニュー）
- `appear_sec[]`・`total_screen_time_sec`・`prominence`（hero/prominent/incidental/background）
- `is_intentional`（タイアップ濃厚/自然言及/偶発/不明）← **競合の「上位は実は案件か」を見抜く**
- `brand_relation`（client/competitor/neutral/unknown）← `client_name` 注入で「競合がどの面に露出か」の戦略マップ

### KW一致は4経路で多層追跡（`KeywordMatch`）
テロップ / 発話 / ハッシュタグ / 映像ラベル の各レイヤーで同一KWを追う → 「字幕にKWあり・発話なし」等の差が
検索シグナルの効きどころの生データになる。

（完全な Pydantic スキーマは付録A）

## 4. 5本横断の「アルゴリズム読み解き」手法

**STEP1｜1本SEOスコア（5軸×20点=100）** ※観測可能な代理変数のみ:
①KW適合（テロップ/キャプション命中）②エンゲージ強度（5本内で偏差値化＝相対）③フック強度④テロップ最適化
⑤完了率示唆（短尺/ループ/CTA）。→ `seo_score` 降順と実rankの一致を見る。**乖離＝観測外要因（権威/鮮度/初速）が
効いているサイン**として注記。

**STEP2｜5本横断比較**:
- 共通パターン: 各フラグの「5本中の立ち数」。**5/5=必須条件候補, 4/5=強い勝ち筋, 3/5=傾向**。数値は上位収束レンジ（例: 尺15-22秒）。
- **rank1-2 vs rank4-5 差分**: 上位帯にあって下位帯に無い因子＝**順位ドライバ候補**。
- 順位×特徴量: Spearman順位相関（**点推定＋単調性○/5のみ、有意性は出さない**）。散布小表と必ず併記。

**STEP3｜勝ち筋の言語化（昇格条件つき）**: 「共通4/5以上」「順位ドライバ」「単調4/5以上」の3条件のうち**2つ以上**を満たした
因子のみ仮説に昇格。各仮説に**確信度（高/中/低）＋「5本中n本で観測」根拠**を必ず添付。

## 5. 出力（営業向けサマリ構成・KWごと1枚）

1. 検索面サマリ（1行）: 「KW『○○』上位5本＝平均ENG○%/保存率○%/尺中央値○秒。勝者の型は〔結論先出し×KWテロップ×短尺〕」
2. この面の勝ち筋 TOP3（各: 仮説1行＋「5本中n本で観測」＋確信度バッジ）
3. rank1 解剖（1位がなぜ1位か: 5軸スコアと突出因子）
4. 下位帯との差（rank4-5に欠けていた因子＝提案で外せない条件）
5. VSEO提案アクション（自社が投稿するなら: テロップ冒頭にKW/フックは○型/尺○秒/保存導線）
6. 限界注記（定型1行: 「上位5本の観測に基づく仮説。n=5・相関≠因果。入賞率はテスト投稿で検証推奨」）
+ 機械可読JSON（各動画の7観点 + ブランド検出 + スコア + 横断結果）

## 6. 実装：新 Skill ＋ 再利用資産 ＋ 配線

新規 `src/teamagent/skills/video_algorithm/`（schema.py + skill.py）＋ `prompts/video_algorithm/v1/{system,synthesis}.md`。

**再利用（既存・実装済み）**:
- `adapters/tiktok_scraper.search_tiktok(query, "keyword", max_videos=5)` → 上位5本＋指標（再生/いいね/コメント/シェア/保存/eng_rate/cover_url/url/desc）
- `adapters/video_download.download_video(url, max_filesize_mb, request_id)` → (bytes, mime)（TikTokはURL直渡し不可、bytes必須）
- `adapters/video_proxy.ensure_under_limit(...)` → Gemini inline 上限超は ffmpeg 圧縮（動画審査で実証済み）
- `adapters/gemini_client.analyze_video_bytes(...)` / `generate_text(...)`（横断合成）
- `skills/base`（BaseSkill/SkillContext/@register）、`prompts/loader.load_prompt`
- VSEO 既存資産: `skills/vseo/dataprep.compute_stats`（avg/median/breakout/花王仮説）に `seo_score`/`rank_diff`/`spearman` を足す形で接続可能。ローカル `tiktok-vseo-proposal` の `gemini_unified_prompt.md`（ショット描写・時刻参照強制）を system prompt の土台に流用。

**コスト/性能**: 5本 × (DL + 圧縮 + Gemini ~$0.0014) + 横断1回 ≈ **$0.007〜0.01 / 実行**。動画は**並行**処理（既存 video_batch のように Semaphore でDL/Gemini並列）で数分。

## 7. 段階実装案

- **Phase 1（核）**: KW→上位5本→各動画 7観点JSON抽出（ブランド認識込み）→横断サマリ（共通パターン＋rank差分＋勝ち筋TOP3）。Slack返信＋JSON。
- **Phase 2**: SEOスコア5軸＋Spearman（`compute_stats`拡張）、サムネDL、複数KW横断の「普遍勝ち筋」。
- **Phase 3**: 既存VSEO提案書（PPTX/`build_proposal.js`）へスライド供給。

## 付録A: Pydantic スキーマ（抜粋・実装の核）

```python
class Prominence(str, Enum):
    hero="hero"; prominent="prominent"; incidental="incidental"; background="background"

class KeywordMatch(BaseModel):
    keyword: str; matched: bool
    match_type: Literal["exact","partial","synonym","none"]
    appear_sec: list[float] = []
    surface_text: str | None = None
    layer: Literal["caption","narration","dialogue","hashtag","object_label"]

class BrandDetection(BaseModel):                       # ★ユーザー強調の中核
    brand_name: str                                    # 不明ロゴは "unidentified_logo"
    detection_source: Literal["signboard","product_package","logo_on_clothing",
                              "storefront","screen_ui","menu","other"]
    appear_sec: list[float]; total_screen_time_sec: float
    prominence: Prominence
    is_intentional: Literal["likely_sponsored","organic_mention","incidental","unknown"]
    co_occurring_caption: str | None = None
    brand_relation: Literal["client","competitor","neutral_third_party","unknown"] = "unknown"

# 7観点クラス: HookAnalysis / OnScreenTextAnalysis / VisualContentRecognition(+brand_detections) /
#             StructureEditing / MessagingCTA / AudioAnalysis / SubjectCinematography
class VideoVSEOAnalysis(BaseModel):
    video_url: str; rank: int | None; target_keywords: list[str]; client_name: str | None = None
    hook: HookAnalysis
    on_screen_text: OnScreenTextAnalysis
    visual_content: VisualContentRecognition            # main_objects / location_cues / brand_detections[]
    structure: StructureEditing
    messaging: MessagingCTA
    audio: AudioAnalysis
    cinematography: SubjectCinematography
    # 横断サマリ用
    top_vseo_win_factors: list[str]                     # 勝因Top（順位仮説）
    save_share_motivation: str
    client_adaptability: Literal["◎","○","△","×"]; client_adaptability_reason: str
```

## 付録B: VSEOランキング要因チェックリスト（観測可能・上位動画を○×採点）

- **A.KW適合**: クエリ語がキャプション冒頭/テロップ(OCR)/冒頭3秒の発話に出るか・ニッチHT3〜5個か
- **B.エンゲージ**: 冒頭2秒フック強/保存がいいねに対し相対多/シェア多/コメント質・いいね偏重でないか
- **C.投稿者**: 同一ニッチ一貫投稿か（フォロワー数は直接要因でない＝少数でも上位は勝ち筋）
- **D.形式/意図**: 焼き込み字幕オン/尺が検索向き or 短尺×強ループ/検索意図に直接答える構成か
