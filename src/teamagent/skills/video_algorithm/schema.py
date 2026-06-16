"""VideoAlgorithm Skill の入出力スキーマ。

Gemini に**構造化JSON**で吐かせる per-video 分析（`VideoVSEOAnalysis`）と、検索メタ
（`VideoMeta`）、5本横断の読み解き（`CrossAnalysis`）。HTML タイムライン描画のため、
テロップ/ブランド/シーン/CTA は**秒(timecode)を必須級**で持つ。

Gemini 出力は欠落しうるので、全フィールドに default を与え防御的にパースできるようにする
（video_approval と同じ思想: 不明は空/Noneでfail-safe）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Position = Literal["top", "center", "bottom", "full", "unknown"]
Prominence = Literal["hero", "prominent", "incidental", "background"]
MatchLayer = Literal["caption", "telop", "narration", "dialogue", "hashtag", "object_label"]


class KeywordMatch(BaseModel):
    """検索KWが動画のどのレイヤーに何秒で出るか。"""

    keyword: str = ""
    matched: bool = False
    match_type: Literal["exact", "partial", "synonym", "none"] = "none"
    layer: MatchLayer = "caption"
    appear_sec: list[float] = Field(default_factory=list)
    surface_text: str | None = None


class TelopItem(BaseModel):
    """画面内テロップ（焼き込み字幕）1 つ。タイムライン描画の主役。"""

    sec: float = 0.0
    text: str = ""
    position: Position = "unknown"
    kw_match: bool = False  # 検索KWに一致するテロップか（OCR適合の核）


class BrandDetection(BaseModel):
    """動画内のブランド/ロゴ/看板/物体の検出。ユーザー強調の中核。"""

    brand_name: str = ""  # 不明ロゴは "unidentified_logo"
    detection_source: Literal[
        "signboard",
        "product_package",
        "logo_on_clothing",
        "storefront",
        "screen_ui",
        "menu",
        "other",
    ] = "other"
    appear_sec: list[float] = Field(default_factory=list)
    total_screen_time_sec: float = 0.0
    prominence: Prominence = "incidental"
    is_intentional: Literal["likely_sponsored", "organic_mention", "incidental", "unknown"] = (
        "unknown"
    )
    co_occurring_caption: str | None = None
    brand_relation: Literal["client", "competitor", "neutral_third_party", "unknown"] = "unknown"


class Scene(BaseModel):
    """シーン（ショット）1 区間。実視聴の担保（時刻参照）。"""

    start_sec: float = 0.0
    end_sec: float = 0.0
    desc: str = ""


ColorRole = Literal["dominant", "accent", "background"]
Tone = Literal["warm", "neutral", "cool"]


class ColorSwatch(BaseModel):
    """主要色 1 つ（hex は Gemini の近似報告）。"""

    hex: str = ""  # "#RRGGBB"（不正値は描画側でサニタイズ）
    role: ColorRole = "dominant"
    ratio: float = Field(default=0.0, ge=0.0, le=1.0)  # 画面占有率 0.0-1.0
    tone: Tone = "neutral"


class ColorAnalysis(BaseModel):
    """配色・明度・トーンの観点（VSEO: 検索一覧でのCTR/ブランド整合の代理）。"""

    palette: list[ColorSwatch] = Field(default_factory=list)  # 3-5色
    brightness: Literal["dark", "dim", "medium", "bright", "very_bright"] = "medium"
    temperature: Literal["warm", "neutral", "cool", "mixed"] = "neutral"
    saturation: Literal["muted", "moderate", "vivid"] = "moderate"
    contrast: Literal["low", "medium", "high"] = "medium"
    thumbnail_focus: str = ""  # サムネ(0-1秒)の主役色/被写体1文
    text_legibility: Literal["poor", "ok", "good"] = "ok"  # テロップが背景から分離してるか

    def is_bright(self) -> bool:
        return self.brightness in ("bright", "very_bright")


class FrameShot(BaseModel):
    """レポート埋め込み用の実フレーム1枚（base64 data URI）。"""

    sec: float = 0.0
    caption: str = ""
    data_uri: str = ""  # "data:image/jpeg;base64,..."


class LayerMessages(BaseModel):
    """テロップ/キャプション/映像中身が「それぞれ何を言っているか」を1フレーズで。"""

    telop: str = ""  # テロップが語る要旨（≤20字）
    caption: str = ""  # キャプションが語る要旨
    visual: str = ""  # 映像（被写体/シーン）が語る要旨


class ThumbColor(BaseModel):
    """サムネ画像（検索一覧のタイル）から算出した色（ffmpeg+stdlib・動画内色とは別）。"""

    swatches: list[str] = Field(default_factory=list)  # 主要3色 hex（占有降順）
    brightness01: float = Field(default=0.5, ge=0.0, le=1.0)  # 0.0(暗)-1.0(明) 知覚輝度
    warmth: float = Field(default=0.0, ge=-1.0, le=1.0)  # -1.0(寒)〜+1.0(暖) （R-B 由来）
    focus: str = ""  # サムネ主役の被写体/色 1文（任意）

    def tone_jp(self) -> str:
        if self.warmth > 0.12:
            return "暖色"
        if self.warmth < -0.12:
            return "寒色"
        return "中性"

    def bright_jp(self) -> str:
        if self.brightness01 >= 0.6:
            return "高明度"
        if self.brightness01 < 0.35:
            return "低明度"
        return "中明度"


class VideoVSEOAnalysis(BaseModel):
    """1 動画の VSEO 観点マルチモーダル分析（Gemini 構造化出力）。"""

    duration_sec: float = 0.0
    # フック(0-3秒)
    hook_type: str = "other"  # question/number/shock/visual/pov/dialogue/problem/other
    hook_summary: str = ""
    hook_has_caption: bool = False
    # テロップ
    telop_density: Literal["none", "light", "medium", "heavy"] = "none"
    telops: list[TelopItem] = Field(default_factory=list)
    # コンテンツ/ブランド認識
    main_objects: list[str] = Field(default_factory=list)
    setting: str = "unknown"  # indoor/outdoor/mixed/studio/unknown
    brand_detections: list[BrandDetection] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)
    # 構成/編集
    cut_count: int | None = None
    pacing: Literal["slow", "moderate", "fast", "very_fast", "unknown"] = "unknown"
    # 訴求/CTA
    main_message: str = ""
    value_propositions: list[str] = Field(default_factory=list)
    cta_type: list[str] = Field(default_factory=list)  # save/follow/visit/buy/...
    cta_text: str | None = None
    cta_sec: float | None = None
    # 音源/音声
    has_narration: bool = False
    is_trending_sound: Literal["yes", "no", "unknown"] = "unknown"
    spoken_keywords: list[KeywordMatch] = Field(default_factory=list)
    # KW適合 / キャプション関連性
    keyword_matches: list[KeywordMatch] = Field(default_factory=list)
    caption_relevance: str = ""  # キャプション本文と動画内容/KWの関連性の評価（判断要素）
    # メッセージ一貫性（テロップ↔キャプション↔映像中身が同じことを言っているか）
    message_coherence: int | None = Field(default=None, ge=0, le=100)  # 0-100
    layer_messages: LayerMessages | None = None  # 3者がそれぞれ言っている要旨
    divergence_note: str | None = None  # ズレ（乖離）の名指し（一致時は None）
    reinforcement_note: str | None = None  # どう補強し合っているか1文
    # 色味（動画内色は廃止＝サムネ色を使う。後方互換でフィールドは残すが既定空）
    color: ColorAnalysis = Field(default_factory=ColorAnalysis)
    # VSEO 総括
    win_factors: list[str] = Field(default_factory=list)
    save_share_motivation: str = ""

    def coherence_band(self) -> str:
        """message_coherence を営業向け4段階に。None は『—』。"""
        c = self.message_coherence
        if c is None:
            return "—"
        if c >= 80:
            return "一貫"
        if c >= 60:
            return "概ね一貫"
        if c >= 40:
            return "部分的"
        return "乖離"

    def kw_in_telop(self) -> bool:
        return any(t.kw_match for t in self.telops)

    def kw_in_thumbnail(self) -> bool:
        """サムネ(0-1秒)テロップに検索KWが乗っているか（検索一覧での効き）。"""
        return any(t.kw_match and t.sec <= 1.0 for t in self.telops)

    def has_cta(self) -> bool:
        return bool(self.cta_type) or bool(self.cta_text)

    def has_brand(self) -> bool:
        return bool(self.brand_detections)


class VideoMeta(BaseModel):
    """検索結果のメタ＋エンゲージメント指標（tiktok_search 由来、Gemini外）。"""

    rank: int = 0
    url: str = ""
    author: str = ""
    desc: str = ""  # キャプション本文
    play_count: int = 0
    digg_count: int = 0  # いいね
    comment_count: int = 0
    share_count: int = 0
    collect_count: int = 0  # 保存
    engagement_rate: float = 0.0
    cover_url: str | None = None

    def save_rate(self) -> float:
        return (self.collect_count / self.play_count * 100) if self.play_count else 0.0

    def share_rate(self) -> float:
        return (self.share_count / self.play_count * 100) if self.play_count else 0.0


class AnalyzedVideo(BaseModel):
    """1 動画分（メタ + 分析）。analysis=None は取得/分析失敗。"""

    meta: VideoMeta
    analysis: VideoVSEOAnalysis | None = None
    frames: list[FrameShot] = Field(default_factory=list)  # 実フレーム画像（埋込用）
    video_data_uri: str = ""  # 軽量Webプレビュー動画 base64（タイムライン<video>再生用）
    cover_data_uri: str = ""  # サムネ画像 base64（検索一覧タイル・埋込用）
    thumb: ThumbColor | None = None  # サムネ色（ffmpeg+stdlib 算出）
    error: str | None = None
    cost_usd: float = 0.0
    model_id: str | None = None


class WinFactor(BaseModel):
    """5本横断で抽出した勝ち筋仮説（根拠＋確信度つき）。"""

    factor: str
    observed_in: int = 0  # 5本中n本
    total: int = 0
    confidence: Literal["高", "中", "低"] = "中"
    evidence: str = ""


class CorrItem(BaseModel):
    """Spearman 1 ペア（p値は持たない＝設計の正直さ）。"""

    feature: str = ""
    target: Literal["rank", "save_rate"] = "rank"
    rho: float | None = None  # None=有効n<3
    n_pairs: int = 0
    direction_label: str = ""  # 「高いほど上位」等（rank時）
    monotonic_hits: int = 0
    monotonic_total: int = 0


class DistItem(BaseModel):
    """分布サマリ（中央値中心）と外れ値。"""

    feature: str = ""
    median: float = 0.0
    min: float = 0.0
    max: float = 0.0
    outlier_rank: int | None = None
    outlier_value: float | None = None
    outlier_note: str = ""


class KwCoverage(BaseModel):
    """4 層一致の定量化。"""

    avg_score_0_100: float = 0.0
    avg_layers_0_4: float = 0.0
    layer_fill: list[tuple[str, str]] = Field(default_factory=list)  # [("テロップ","4/5"),...]
    per_video: list[str] = Field(default_factory=list)  # ["#1 4/4(100)",...]


class FeatureRowOut(BaseModel):
    """特徴量マトリクス 1 行（HTML 描画用）。"""

    rank: int = 0
    save_rate: float = 0.0
    duration_sec: float = 0.0
    telop_count: int = 0
    telop_density: str = ""
    hook_type: str = ""
    kw_layers: str = ""  # 「4/4」
    has_cta: bool = False
    has_brand: bool = False


class WinRange(BaseModel):
    """勝ち筋の定量レンジ。"""

    label: str = ""  # 「尺」「保存率」「テロップ」
    text: str = ""  # 「11-18秒」「3.0% 以上」


class StatsAnalysis(BaseModel):
    """AIにしかできない統計上乗せ（決定的・stdlibのみ・有意性なし）。"""

    sample_size: int = 0
    correlations: list[CorrItem] = Field(default_factory=list)
    distributions: list[DistItem] = Field(default_factory=list)
    kw_coverage: KwCoverage = Field(default_factory=KwCoverage)
    hook_counts: list[tuple[str, int]] = Field(default_factory=list)  # [("problem",3),...]降順
    strong_hook_ratio: str = ""  # 「4/5」
    win_ranges: list[WinRange] = Field(default_factory=list)
    feature_matrix: list[FeatureRowOut] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class ConceptItem(BaseModel):
    """Top N を貫く『概念』（Gemini 横断シンセシス）。"""

    concept: str = ""  # ≤12字「安さ×ボリューム」等
    gist: str = ""  # その概念の中身 ≤1文
    videos: list[int] = Field(default_factory=list)  # 該当順位（実在rankのみ）
    prevalence: str = ""  # 「4/5」


class AngleCluster(BaseModel):
    """訴求『角度』のクラスタ（concept より行動寄り）。"""

    angle: str = ""  # price_volume/aesthetic/convenience/authority/empathy/novelty 等
    label_jp: str = ""  # 営業向け和名「安さ実感」等
    videos: list[int] = Field(default_factory=list)
    why_works: str = ""  # この角度が検索面で効く観測上の理由 ≤1文


class SharedFunnel(BaseModel):
    """共通の導線（保存/シェア/来店設計）。"""

    pattern: str = ""  # 「保存を促し→週末の来店に接続」等 ≤1文
    cta_consensus: list[str] = Field(default_factory=list)  # 多数派CTA
    save_logic: str = ""  # なぜ保存されるか ≤1文


class Differentiator(BaseModel):
    """上位内での差別化点（同質化の中で何で抜けたか）。"""

    rank: int = 0
    edge: str = ""  # ≤1文


class WinHypothesis(BaseModel):
    """勝ちパターン仮説（提案書の核）。n小ゆえ確信度の天井は『中』。"""

    hypothesis: str = ""  # ≤1文
    supported_by: list[int] = Field(default_factory=list)  # 根拠動画の順位
    confidence: Literal["高", "中", "低"] = "中"
    counter_example: str | None = None  # 反例（誠実さ）
    so_what: str = ""  # 営業の次アクション ≤1文


class CrossSynthesis(BaseModel):
    """横断シンセシス（120点の中核・Gemini 2nd pass）。事実層(stats)と別の解釈層。

    ショート動画PRプランナー/ディレクター目線の「戦略レポート」を生成する。
    """

    # --- プランナー/ディレクターの戦略サマリ（レポートの主役） ---
    headline: str = ""  # この検索面の攻略方針を1文で（ディレクターの読み）
    strategy: str = ""  # どう攻めるかの戦略ナラティブ 2-3文
    creative_brief: list[str] = Field(default_factory=list)  # 撮影/編集/テロップ/尺の具体指示
    posting_design: str = ""  # 投稿設計（CTA/保存導線/頻度）1文
    client_pitch: str = ""  # クライアントにそのまま言える提案の一言
    # --- 根拠の解釈層 ---
    common_concepts: list[ConceptItem] = Field(default_factory=list)
    angle_clusters: list[AngleCluster] = Field(default_factory=list)
    shared_funnel: SharedFunnel | None = None
    differentiators: list[Differentiator] = Field(default_factory=list)
    win_hypotheses: list[WinHypothesis] = Field(default_factory=list)
    caveat: str = ""  # n小・相関≠因果の定型


class CrossAnalysis(BaseModel):
    """5本横断の読み解き結果。"""

    keyword: str = ""
    video_count: int = 0
    avg_engagement_rate: float = 0.0
    avg_save_rate: float = 0.0
    median_duration_sec: float = 0.0
    common_patterns: list[str] = Field(default_factory=list)
    rank_diff_drivers: list[str] = Field(default_factory=list)
    win_factors: list[WinFactor] = Field(default_factory=list)
    common_palette: list[ColorSwatch] = Field(default_factory=list)  # 上位に頻出の色
    dominant_temperature: Literal["warm", "neutral", "cool", "mixed"] = "neutral"
    dominant_brightness: Literal["dark", "dim", "medium", "bright", "very_bright"] = "medium"
    thumb_consensus: str = ""  # サムネ色の横断1文（検索一覧での目立ち方）
    thumb_agree: bool = False  # サムネ色が過半数一致しているか（提案に使えるか）
    stats: StatsAnalysis | None = None
    synthesis: CrossSynthesis | None = None  # Gemini 横断シンセシス（解釈層）
    summary: str = ""


def _default_outputs() -> list[Literal["report", "slides", "pptx"]]:
    """outputs の既定（report のみ）。lambda だと list[str] 推論で mypy strict が弾くため関数化。"""
    return ["report"]


class VideoAlgorithmInput(BaseModel):
    """入力: 検索KW1つ。"""

    query: str
    max_videos: int = Field(default=5, ge=1, le=10)
    client_name: str | None = None  # brand_relation 判定用（任意）
    # §Q-HTML→PPTX: 追加出力。既定は report のみ＝既存挙動/契約を壊さない。
    # "slides"=提案用スライドHTML（編集可・16:9）, "pptx"=そのPPTX（提案資料に組み込む）。
    outputs: list[Literal["report", "slides", "pptx"]] = Field(default_factory=_default_outputs)


class VideoAlgorithmOutput(BaseModel):
    """出力: 各動画分析 + 横断 + レポート。"""

    query: str
    videos: list[AnalyzedVideo] = Field(default_factory=list)
    cross: CrossAnalysis = Field(default_factory=CrossAnalysis)
    report_html_path: str | None = None  # ローカルパス（runtime/Slack添付用・金庫外からは不可視）
    report_url: str | None = None  # §M: 非公開S3の署名URL（金庫外OpenClawが読める・未発行None）
    # §Q-HTML→PPTX: 提案資料組み込み用の追加成果物（要求時のみ・graceful・未発行None）。
    slides_url: str | None = None  # 編集可スライドHTML（営業がブラウザで直接編集）
    pptx_url: str | None = None  # 提案用 PPTX（16:9・そのまま提案資料に差し込む）
    slack_summary: str = ""
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    model_id: str | None = None
