"""VideoAlgorithm Skill の入出力スキーマ。

Gemini に**構造化JSON**で吐かせる per-video 分析（`VideoVSEOAnalysis`）と、検索メタ
（`VideoMeta`）、5本横断の読み解き（`CrossAnalysis`）。HTML タイムライン描画のため、
テロップ/ブランド/シーン/CTA は**秒(timecode)を必須級**で持つ。

Gemini 出力は欠落しうるので、全フィールドに default を与え防御的にパースできるようにする
（video_approval と同じ思想: 不明は空/Noneでfail-safe）。
"""

from __future__ import annotations

import math
import os
import re
import unicodedata
from typing import Annotated, Any, Literal

import structlog
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

logger = structlog.get_logger(__name__)

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


# 場面の役割（検索上位チェックの 2 段目の構成表）。順番はレポートの凡例の順。
SCENE_ROLES: tuple[str, ...] = ("hook", "problem", "steps", "result", "proof", "cta", "other")
# 任意の欄（場面ごとの詳しい構成）。無ければ出力（model_dump）にも出さない＝既存の出力と同じ。
SCENE_DETAIL_FIELDS: tuple[str, ...] = ("role", "telop", "speech", "intent")
_SCENE_TEXT_MAX = 120


class Scene(BaseModel):
    """シーン（ショット）1 区間。実視聴の担保（時刻参照）。

    role / telop / speech / intent は任意（検索上位チェックの 2 段目が、場面ごとの構成表のために
    プロンプトの追記で頼むときだけ埋まる）。video_algorithm の既定のプロンプト（v1/v2）では出ず、
    None のときは model_dump にも出さないので、既存の出力と結果キャッシュの形は変わらない。
    role は SCENE_ROLES 以外を None にする（コードが推定し直す）。
    """

    start_sec: float = 0.0
    end_sec: float = 0.0
    desc: str = ""
    role: str | None = None
    telop: str | None = None
    speech: str | None = None
    intent: str | None = None

    @field_validator("role", mode="before")
    @classmethod
    def _role_in_vocabulary(cls, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        role = value.strip().lower()
        return role if role in SCENE_ROLES else None

    @field_validator("telop", "speech", "intent", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        text = " ".join(value.split())
        return text[:_SCENE_TEXT_MAX] if text else None

    @model_serializer(mode="wrap")
    def _omit_missing_detail(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict):
            for key in SCENE_DETAIL_FIELDS:
                if data.get(key) is None:
                    data.pop(key, None)
        return data


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


# サムネの色の区分のしきい値からこの幅以内は「境界」と出す。
THUMB_BORDER = 0.03
# サムネの出どころ: cover＝表紙の画像・frame＝表紙を取れずコマで代用・""＝不明（旧キャッシュ）。
CoverSource = Literal["", "cover", "frame"]


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

    def borderline(self) -> list[str]:
        """区分のしきい値から ±0.03 以内の軸（「境界」と出す。断定しない）。"""
        near: list[str] = []
        if any(abs(self.warmth - t) <= THUMB_BORDER for t in (0.12, -0.12)):
            near.append("暖寒")
        if any(abs(self.brightness01 - t) <= THUMB_BORDER for t in (0.6, 0.35)):
            near.append("明度")
        return near


# ── サムネ（一覧の表紙）の読み取り ────────────────────────────────────────────
# 表紙の画像を Gemini が 1 回だけ見て、タップの要因になりうる要素を JSON で返す（cover_read.py）。
# 本数・段階・差・指示の土台はコード（cover_facts.py）が決める。AI の値は壊れていることがあるので、
# どの欄も例外にせず「unknown（分からない）」へ倒す。unknown の欄は、その欄の母数から外す
# （「無い」と数えない）。rank・group・status・reason・version・via・img_w・img_h は
# コードだけが書く。

COVER_STATUSES: tuple[str, ...] = (
    "ok",  # 読めた
    "no_cover",  # 表紙の URL が無い（acquire_job_id の経路など）
    "skipped",  # 画像投稿（尺 0）
    "fetch_failed",  # 取得できない（署名 URL の失効 403 など）
    "read_failed",  # AI が読めない（JSON 崩れ・例外・必須の欄の欠け）
    "timeout",  # 締め切りまでに読めない
)
CoverStatus = Literal["ok", "no_cover", "skipped", "fetch_failed", "read_failed", "timeout"]
# 写っている要素（1 つに決めさせず、写っているものを全部挙げさせる）。
COVER_ELEMENTS: tuple[str, ...] = (
    "person",  # 人
    "product",  # 商品・パッケージ
    "result",  # 完成品・仕上がり
    "process",  # 工程・使っている途中
    "before_after",  # 使用前後・比較
    "text_main",  # 文字が主（画より文字が目立つ）
    "scene",  # 場所・景色
)
COVER_SIZZLE: tuple[str, ...] = (
    "steam",  # 湯気
    "gloss",  # 照り・つや
    "cross_section",  # 断面
    "pour",  # 注ぐ・垂れる・とろみ
    "foam",  # 泡
    "skin",  # 肌の質感
    "hair",  # 髪の質感
    "texture",  # そのほかの質感（布・素材）
)
COVER_APPEALS: tuple[str, ...] = (
    "benefit",  # ベネフィット（得られること）
    "how_to",  # やり方
    "target",  # 誰向けか
    "time_saving",  # 時短
    "ranking",  # ランキング・何選
    "reaction",  # 驚き・感想
)
COVER_TEXT_STYLES: tuple[str, ...] = ("outline", "box", "shadow", "plain")
COVER_FACE_KINDS: tuple[str, ...] = ("real", "illustration", "in_media", "none")
COVER_EXPRESSIONS: tuple[str, ...] = ("smile", "surprise", "serious", "other", "none")
COVER_GAZES: tuple[str, ...] = ("camera", "subject", "away", "none")
COVER_ACTIONS: tuple[str, ...] = ("eating", "using", "showing", "pointing", "none")
COVER_TEXT_MAX = 60
COVER_NOTE_MAX = 30
COVER_BRAND_MAX = 30
COVER_TEXT_BLOCKS = 4
COVER_TEXT_LINES = 6
_COVER_CTRL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


def _enum_token(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFKC", value).strip().lower().replace("-", "_").replace(" ", "_")


def _enum_or_unknown(allowed: tuple[str, ...]) -> Any:
    def read(value: Any) -> str:
        token = _enum_token(value)
        return token if token in allowed else "unknown"

    return BeforeValidator(read)


def _enum_list_or_none(allowed: tuple[str, ...]) -> Any:
    """知らない要素だけ捨てる。リストでないもの（欄の欠け・壊れ）は None＝分からない。"""

    def read(value: Any) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = [x for x in re.split(r"[,、/|\s]+", value) if x]
        if not isinstance(value, (list, tuple)):
            return None
        out: list[str] = []
        for item in value:
            token = _enum_token(item)
            if token in allowed and token not in out:
                out.append(token)
        return out

    return BeforeValidator(read)


def _tri_bool(value: Any) -> bool | None:
    """真偽の 3 値（True／False／None＝分からない）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    token = _enum_token(value)
    if token in ("true", "yes", "あり"):
        return True
    if token in ("false", "no", "なし"):
        return False
    return None


def _cover_line_text(value: Any) -> str:
    """表紙の文字（AI の読み取り）。改行（行の区切り）は残し、制御文字と空行を除く。"""
    if not isinstance(value, str):
        return ""
    text = _COVER_CTRL_RE.sub(" ", value.replace("\\n", "\n").replace("\r", "\n"))
    lines = [" ".join(line.split()) for line in text.split("\n")]
    joined = "\n".join(line for line in lines if line)[: COVER_TEXT_MAX * 2]
    kept: list[str] = []
    budget = COVER_TEXT_MAX
    for line in joined.split("\n")[:COVER_TEXT_LINES]:
        if budget <= 0:
            break
        kept.append(line[:budget])
        budget -= len(line)
    return "\n".join(kept)


def _short_text(limit: int) -> Any:
    def read(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(_COVER_CTRL_RE.sub(" ", value).split())[:limit]

    return BeforeValidator(read)


def _box_2d(value: Any) -> tuple[int, int, int, int] | None:
    """[ymin, xmin, ymax, xmax]（0〜1000 に正規化した座標・Gemini の box_2d の形）。

    壊れていれば None。
    """
    if isinstance(value, dict):
        value = [value.get(k) for k in ("ymin", "xmin", "ymax", "xmax")]
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    nums: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        try:
            num = float(item)  # 桁の大きな整数は OverflowError（壊れた枠＝None・例外にしない）
        except (OverflowError, ValueError):
            return None
        if not math.isfinite(num):
            return None
        nums.append(min(1000.0, max(0.0, num)))
    y0, x0, y1, x1 = nums
    if y1 <= y0 or x1 <= x0:
        return None
    return (round(y0), round(x0), round(y1), round(x1))


_Box = Annotated[tuple[int, int, int, int] | None, BeforeValidator(_box_2d)]


class CoverText(BaseModel):
    """表紙の文字の 1 ブロック（AI の読み取り）。

    大きさ・位置・行数は box と改行からコードが計算する。
    """

    text: Annotated[str, BeforeValidator(_cover_line_text)] = ""
    box: _Box = Field(default=None, validation_alias=AliasChoices("box_2d", "box"))
    style: Annotated[list[str] | None, _enum_list_or_none(COVER_TEXT_STYLES)] = None
    # 縦書きか（None＝分からない）。縦書きの枠の高さは列の長さなので、字の大きさは枠の幅÷列の数
    # で測る（09-29 本番 #5「市販の／カレールーは／卒業！」は縦書きで、高さ÷行で 3 倍に出た）。
    # 分からないときは字の大きさを測らない（母数から外す）。
    vertical: Annotated[bool | None, BeforeValidator(_tri_bool)] = None


class CoverFace(BaseModel):
    """顔。実写の人（real）だけを「顔あり」に数える（イラスト・画面やパッケージの中の顔は別）。"""

    kind: Annotated[str, _enum_or_unknown(COVER_FACE_KINDS)] = "unknown"
    expression: Annotated[str, _enum_or_unknown(COVER_EXPRESSIONS)] = "unknown"
    gaze: Annotated[str, _enum_or_unknown(COVER_GAZES)] = "unknown"
    box: _Box = Field(default=None, validation_alias=AliasChoices("box_2d", "box"))

    @model_validator(mode="before")
    @classmethod
    def _present_to_kind(cls, data: Any) -> Any:
        if isinstance(data, dict) and "kind" not in data and "present" in data:
            present = _tri_bool(data.get("present"))
            data = {**data, "kind": "real" if present else "none" if present is False else ""}
        return data


def _cover_texts(value: Any) -> list[Any] | None:
    if value is None:
        return None
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        return None
    return [{"text": v} if isinstance(v, str) else v for v in value if isinstance(v, (str, dict))]


def _cover_face(value: Any) -> Any:
    if isinstance(value, (dict, BaseModel)):
        return value
    flag = _tri_bool(value)
    if flag is False or _enum_token(value) == "none":
        return {"kind": "none", "expression": "none", "gaze": "none"}
    return None


def _brand_texts(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = " ".join(_COVER_CTRL_RE.sub(" ", item).split())[:COVER_BRAND_MAX]
            if text and text not in out:
                out.append(text)
    return out[:3]


# コードだけが書く欄（AI の JSON に書かれていても捨てる）。
COVER_CODE_ONLY: tuple[str, ...] = (
    "rank",
    "group",
    "status",
    "reason",
    "version",
    "via",
    "img_w",
    "img_h",
)
# 読み取りが成り立つのに要る欄（無ければ read_failed。空の JSON を「全部無い」と数えない）。
COVER_REQUIRED_KEYS: tuple[str, ...] = ("elements", "texts", "face")


class CoverRead(BaseModel):
    """サムネ（一覧の表紙）1 枚の読み取り。None の欄は「分からない」（その欄の母数から外す）。"""

    rank: int = 0
    group: Literal["top", "rest"] = "top"  # top＝表示順の上位 n 本・rest＝6〜30 位など
    status: CoverStatus = "read_failed"  # 既定は ok にしない（コードが読めたときだけ ok にする）
    reason: str = ""
    version: str = ""
    via: str = ""  # 取得の経路（media／local／injected）
    img_w: int = 0  # 読み取りに渡した画像の幅・高さ（読めなければ 0）
    img_h: int = 0
    elements: Annotated[list[str] | None, _enum_list_or_none(COVER_ELEMENTS)] = None
    subject_note: Annotated[str, _short_text(COVER_NOTE_MAX)] = ""
    texts: Annotated[list[CoverText] | None, BeforeValidator(_cover_texts)] = None
    unreadable_text: Annotated[bool | None, BeforeValidator(_tri_bool)] = None
    face: Annotated[CoverFace | None, BeforeValidator(_cover_face)] = None
    action: Annotated[str, _enum_or_unknown(COVER_ACTIONS)] = "unknown"
    closeup: Annotated[bool | None, BeforeValidator(_tri_bool)] = None
    sizzle: Annotated[list[str] | None, _enum_list_or_none(COVER_SIZZLE)] = None
    product: Annotated[str, _enum_or_unknown(("hero", "visible", "none"))] = "unknown"
    brand_text: Annotated[list[str], BeforeValidator(_brand_texts)] = Field(default_factory=list)
    clutter: Annotated[str, _enum_or_unknown(("simple", "moderate", "busy"))] = "unknown"
    legibility: Annotated[str, _enum_or_unknown(("good", "ok", "poor", "none"))] = "unknown"
    appeals: Annotated[list[str] | None, _enum_list_or_none(COVER_APPEALS)] = None

    @model_validator(mode="after")
    def _tidy(self) -> CoverRead:
        if self.texts is not None:
            self.texts = [t for t in self.texts if t.text][:COVER_TEXT_BLOCKS]
            if not self.texts and self.legibility not in ("none", "unknown"):
                self.legibility = "none"
        return self

    @property
    def ok(self) -> bool:
        return self.status == "ok"


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
    follower_count: int = 0  # 投稿者フォロワー数（上位ボード掲載用・tiktok_search 由来）
    desc: str = ""  # キャプション本文
    play_count: int = 0
    digg_count: int = 0  # いいね
    comment_count: int = 0
    share_count: int = 0
    collect_count: int = 0  # 保存
    engagement_rate: float = 0.0  # 百分率ポイント（2.9% は 2.9）
    cover_url: str | None = None
    # 尺（秒）。カルーセル/画像投稿は TikTok 側に video オブジェクトが無く 0 になる＝
    # 「DL して Gemini に渡せない投稿」の判別に使う（深掘り対象から除外し、
    # 次の候補で必ず max_videos 本を埋めるため）。
    duration_sec: float = 0.0
    # 取得済みで以前は捨てていた欄（tiktok_search / tiktok_acquire 由来）。
    # create_time は投稿日時の UNIX 秒（0 = 不明。facts が動画 ID から換算する）。
    # hashtags は「#」を付けない名前の並び（search.mjs の textExtra 由来）。
    create_time: int = 0
    hashtags: list[str] = Field(default_factory=list)
    music_title: str = ""
    # サムネ（一覧の表紙）の読み取り。置き場所は上位ボード（out.board）の各行だけ（唯一の正）。
    # videos[].meta には載せない（skill が写しから外す）。None は「読んでいない」で、
    # 出力にも出さない。
    cover_read: CoverRead | None = None

    @model_serializer(mode="wrap")
    def _omit_missing_cover(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict) and data.get("cover_read") is None:
            data.pop("cover_read", None)
        return data

    @field_validator("engagement_rate")
    @classmethod
    def _engagement_rate_is_percent(cls, value: float) -> float:
        """百分率として不正な値を正規化し、100超は観測値のまま警告する。

        分子はいいね・コメント・共有・保存の合計なので、正常値でも100%を
        超え得る。上限へ丸めると実績を過小評価するため、有限な正値は保持する。
        """
        if not math.isfinite(value):
            logger.warning("video_meta_engagement_rate_clamped", value=str(value), clamped=0.0)
            return 0.0
        if value < 0.0:
            logger.warning("video_meta_engagement_rate_clamped", value=value, clamped=0.0)
            return 0.0
        if value > 100.0:
            logger.warning("video_meta_engagement_rate_above_100", value=value)
        return value

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
    cover_source: CoverSource = ""  # 表紙の画像か、コマで代用したか（"" は不明＝旧キャッシュ）
    thumb: ThumbColor | None = None  # サムネ色（ffmpeg+stdlib 算出）
    error: str | None = None
    cost_usd: float = 0.0
    model_id: str | None = None
    # 動画実体の取得経路。"" = 既定経路（media worker / ブラウザ）、"apify" = 二段構えで補完
    # （USE_TIKTOK_APIFY_FALLBACK=1）。出所を資料の注記に出せるようにする（捏造ゼロ原則）。
    acquired_via: str = ""


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


class KwTermLayer(BaseModel):
    """検索語 1 つ × 層 1 つの一致（コードが照合した本数）。

    exact は語そのもの、synonym は言い換え（テロップは秒±2で実在を照合済み・キャプションは
    本文に実在）。発話（speech）は動画分析 AI の聞き取りで、照合していない（verified=False）。
    board_hits / board_size は上位ボード全体（メタで測れる層＝キャプション・ハッシュタグだけ）。
    """

    term: str = ""
    layer: Literal["telop", "caption", "hashtag", "speech"] = "telop"
    exact_ranks: list[int] = Field(default_factory=list)
    synonym_ranks: list[int] = Field(default_factory=list)
    n: int = 0
    verified: bool = True
    board_hits: int | None = None
    board_size: int | None = None


class KwCoverage(BaseModel):
    """4 層一致の定量化。"""

    avg_score_0_100: float = 0.0
    avg_layers_0_4: float = 0.0
    layer_fill: list[tuple[str, str]] = Field(default_factory=list)  # [("テロップ","4/5"),...]
    per_video: list[str] = Field(default_factory=list)  # ["#1 4/4(100)",...]
    # 語ごと×層ごと（完全一致と言い換えを分ける）。layer_fill は語を問わない合計。
    per_term: list[KwTermLayer] = Field(default_factory=list)


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
    """廃止（旧キャッシュの読み込み互換のためだけに残す）。

    上位帯（n=5 なら上位 2 本）の最小〜最大を「n=5」として出していた（FC-04）。分布は
    StatsAnalysis.distributions（全 n 本の最小・中央値・最大）を使う。
    """

    label: str = ""
    text: str = ""


class StatsAnalysis(BaseModel):
    """AIにしかできない統計上乗せ（決定的・stdlibのみ・有意性なし）。"""

    sample_size: int = 0
    correlations: list[CorrItem] = Field(default_factory=list)
    distributions: list[DistItem] = Field(default_factory=list)
    kw_coverage: KwCoverage = Field(default_factory=KwCoverage)
    hook_counts: list[tuple[str, int]] = Field(default_factory=list)  # [("problem",3),...]降順
    strong_hook_ratio: str = ""  # 「4/5」
    # 廃止（常に空）。旧キャッシュに値が残っていても読み込みで捨て、LLM にも画面にも出さない。
    win_ranges: list[WinRange] = Field(default_factory=list)
    feature_matrix: list[FeatureRowOut] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)

    @field_validator("win_ranges", mode="before")
    @classmethod
    def _drop_win_ranges(cls, value: Any) -> list[WinRange]:
        return []


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


# ── 横断シンセシス v3 の欄（仕様 v3 §3-2）──────────────────────────────────────
# LLM が書く欄と、コードだけが書く欄（CODE_ONLY_*。parse で LLM の値を捨て、synthesis_checks が
# 決め直す）を同じモデルに持つ。どれも任意で既定は空＝v2 の出力はそのまま読める。
# LLM の出力は壊れていることがあるので、型が合わない値は例外にせず既定値へ倒す（1 項目の
# 誤りで synthesis 全体を捨てないため）。

# 「表紙」は cover_directives だけの種類（directives に書かれたら検査で空にする）。
DIRECTIVE_KINDS: tuple[str, ...] = (
    "フック",
    "構成",
    "テロップ",
    "撮影",
    "音",
    "商品",
    "投稿",
    "表紙",
)
COVER_KIND = "表紙"
_TIMECODE_TEXT_RE = re.compile(r"^\s*(\d{1,2}):([0-5]\d(?:\.\d+)?)\s*$")
_RANK_TEXT_RE = re.compile(r"^\s*(?:#|＃|rank\s*)?(\d{1,3})\s*(?:位)?\s*$", re.IGNORECASE)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _as_rank(value: Any) -> int:
    """順位（「#4」「4位」「rank4」も読む）。読めなければ 0（実在しない順位として捨てる）。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        m = _RANK_TEXT_RE.match(unicodedata.normalize("NFKC", value))
        if m:
            return int(m.group(1))
    return 0


def _as_sec(value: Any) -> float | None:
    """秒（「0:25」も読む）。読めない・負・無限は None（キャプションの引用と同じ扱い）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        sec = float(value)
    elif isinstance(value, str):
        text = unicodedata.normalize("NFKC", value).strip().removesuffix("秒").strip()
        m = _TIMECODE_TEXT_RE.match(text)
        try:
            sec = int(m.group(1)) * 60 + float(m.group(2)) if m else float(text)
        except ValueError:
            return None
    else:
        return None
    return sec if math.isfinite(sec) and sec >= 0 else None


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [t for t in (_as_text(v) for v in value) if t]


def _rank_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return [r for r in (_as_rank(v) for v in value) if r > 0]


def _object_or_none(value: Any) -> Any:
    return value if isinstance(value, (dict, BaseModel)) else None


def _dict_items(value: Any) -> list[Any]:
    """dict の項目だけ残す（LLM が文字列や null を混ぜても他の項目は読む）。"""
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, (dict, BaseModel))]


# LLM の値を読む型（壊れた値は既定へ倒す）。
_Rank = Annotated[int, BeforeValidator(_as_rank)]
_Sec = Annotated[float | None, BeforeValidator(_as_sec)]
_Text = Annotated[str, BeforeValidator(_as_text)]
_TextList = Annotated[list[str], BeforeValidator(_text_list)]
_RankList = Annotated[list[int], BeforeValidator(_rank_list)]


def _kind(value: Any) -> str:
    text = _as_text(value)
    return text if text in DIRECTIVE_KINDS else ""


class SynthRef(BaseModel):
    """根拠「#n の何秒の何」。sec=None はキャプションの引用。source/found_sec はコードだけ。"""

    rank: _Rank = 0
    sec: _Sec = None
    quote: _Text = ""
    # 引用の場所を明示する欄。"cover" はサムネ（一覧の表紙）の文字か説明（AI の読み取り）。
    # 空は従来どおり（sec が None ならキャプション）。空のときは出力に出さない。
    on: _Text = ""
    # コードだけ: telop/scene/hook/brand/caption/cover_text/cover_note（照合に合格した場所）
    source: _Text = ""
    found_sec: _Sec = None  # コードだけ: 見つかった秒（キャプションは None）

    @model_serializer(mode="wrap")
    def _omit_empty_on(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict) and not data.get("on"):
            data.pop("on", None)
        return data


_Refs = Annotated[list[SynthRef], BeforeValidator(_dict_items)]


class SummaryLines(BaseModel):
    """結論の行。type_line は多数派以上の feature_ids だけで作る（コードが段階を照合する）。"""

    type_line: _Text = ""
    feature_ids: _TextList = Field(default_factory=list)
    best_reason: _Text = ""  # 最も見られ保存された 1 本の理由（その 1 本の個票だけで照合）
    client_move: _Text = ""
    type_line_by_code: bool = False  # コードだけ: 見出しをコードの代わりの文にした
    best_rank: _Rank = 0  # コードだけ: 最も見られ保存された 1 本（再生→保存率→シェア）


class PerVideoNote(BaseModel):
    """1 本ずつの勝ち方（その動画の個票の数字と引用だけで書く）。"""

    rank: _Rank = 0
    win_line: _Text = ""
    why_fact: _Text = ""
    why_guess: _Text = ""  # 「推測:」で始める（コードが付け直す）
    steal: _TextList = Field(default_factory=list)
    not_to_copy: _Text = ""


class Directive(BaseModel):
    """クリエイティブ指示 1 つ。段階・順位はコードが照合済みの refs から決める。"""

    text: _Text = ""
    kind: Annotated[str, BeforeValidator(_kind)] = ""  # DIRECTIVE_KINDS 以外は空
    refs: _Refs = Field(default_factory=list)
    # コードだけ: code はコードが事実から作った指示
    origin: Annotated[
        Literal["llm", "code"], BeforeValidator(lambda v: "code" if v == "code" else "llm")
    ] = "llm"
    tier: _Text = ""  # コードだけ: 必須条件／多数派／事例
    ranks: _RankList = Field(default_factory=list)  # コードだけ: 照合に合格した順位
    # cover_directives だけ: 根拠にした表紙の特徴の表の id（例 cover:sizzle）。段階・本数・順位は
    # この特徴からコードが取る（引用した本数では決めない）。空は「未集計」。空なら出力に出さない。
    feature: _Text = ""

    @model_serializer(mode="wrap")
    def _omit_empty_feature(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict) and not data.get("feature"):
            data.pop("feature", None)
        return data


class AvoidItem(BaseModel):
    """やらないこと 1 つ。moved は、実績が伴わない 1 本だけの指示をコードが移したもの。"""

    text: _Text = ""
    refs: _Refs = Field(default_factory=list)
    origin: Annotated[
        Literal["llm", "moved"], BeforeValidator(lambda v: "moved" if v == "moved" else "llm")
    ] = "llm"  # コードだけ
    reason: _Text = ""  # コードだけ: 移した理由
    ranks: _RankList = Field(default_factory=list)  # コードだけ


class StoryboardCut(BaseModel):
    """絵コンテのカット。秒（start/end/stage）はコードがカット番号から決める。"""

    cut: _Rank = 0
    show: _Text = ""
    telop: _Text = ""
    aim: _Text = ""
    refs: _Refs = Field(default_factory=list)
    start_sec: _Sec = None  # コードだけ
    end_sec: _Sec = None  # コードだけ
    stage: _Text = ""  # コードだけ: 0〜3秒 などの段


class Storyboard(BaseModel):
    """絵コンテ案。出どころの順位・目安の尺・但し書きはコードが決める。"""

    name: _Text = ""
    basis_ranks: _RankList = Field(default_factory=list)  # コードが照合済みの refs から作り直す
    cuts: Annotated[list[StoryboardCut], BeforeValidator(_dict_items)] = Field(default_factory=list)
    target_sec: _Sec = None  # コードだけ: 目安の尺（全 n 本の尺の中央値）
    basis_note: _Text = ""  # コードだけ: 「事例1本（#k）にもとづく案」など


class BoardAngle(BaseModel):
    """上位ボードの切り口（LLM は語だけ・本数と順位はコードがキャプションから数える）。"""

    label: _Text = ""
    match_terms: _TextList = Field(default_factory=list)
    ranks: _RankList = Field(default_factory=list)  # コードだけ


class HypothesisV3(BaseModel):
    """仮説（A/B で確かめるもの）。該当する動画はコードが match_terms で数え直す。"""

    text: _Text = ""
    match_terms: _TextList = Field(default_factory=list)
    test: _Text = ""  # A/B の組み方
    stat_feature: _Text = ""  # n≥8 で相関を渡したときだけ使う（特徴のキー名）
    ranks: _RankList = Field(default_factory=list)  # コードだけ
    tier: _Text = ""  # コードだけ
    stat_tag: _Text = ""  # コードだけ: 〔テロップ枚数×順位 ρ=…〕（n≥8・文に特徴名があるとき）
    # コードだけ: 効果（保存・再生・シェア）を言う仮説の、該当と非該当の中央値（逆向きなら注記）
    metric_note: _Text = ""


class PostingPlan(BaseModel):
    caption_plan: _Text = ""
    ab_plan: _Text = ""


# 出力（model_dump）に空のまま出さない v3 の欄（v2 の出力・結果キャッシュの形を変えない）。
SYNTHESIS_V3_FIELDS: tuple[str, ...] = (
    "version",
    "summary_lines",
    "per_video",
    "directives",
    "avoid",
    "storyboards",
    "board_angles",
    "hypotheses",
    "posting",
    "cover_directives",
)
# コードだけが書く欄（LLM の JSON に書かれていても parse で捨てる）。キー: 欄のパス。
CODE_ONLY_TOP: tuple[str, ...] = ("version", "grounding_mode", "grounding_dropped")
CODE_ONLY_ITEM: dict[str, tuple[str, ...]] = {
    "summary_lines": ("type_line_by_code", "best_rank"),
    "directives": ("origin", "tier", "ranks"),
    "cover_directives": ("origin", "tier", "ranks"),
    "avoid": ("origin", "reason", "ranks"),
    "storyboards": ("target_sec", "basis_note"),
    "cuts": ("start_sec", "end_sec", "stage"),
    "refs": ("source", "found_sec"),
    "board_angles": ("ranks",),
    "hypotheses": ("ranks", "tier", "stat_tag", "metric_note"),
}


class CrossSynthesis(BaseModel):
    """横断シンセシス（120点の中核・Gemini 2nd pass）。事実層(stats)と別の解釈層。

    ショート動画PRプランナー/ディレクター目線の「戦略レポート」を生成する。
    v3（仕様 §3-2）の欄は任意。v3 では v2 の欄（headline など）を synthesis_checks が
    v3 の欄とコードの事実から作り直す（今の描画がそのまま読めるように）。
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
    # --- 数字の照合（_shared/grounding.py）。コードだけが書く（LLM の値は parse で捨てる）---
    # exclude=True: model_dump（MCP の返却 JSON・結果キャッシュ）に載せない。Aico が「N 件照合
    # できず」と言い換えないよう、件数はログ grounding_dropped でだけ測る。enforce のときだけ
    # 書く（shadow では照合前と同じ出力に保つため書かない）。
    grounding_mode: str = Field(default="", exclude=True)  # enforce のときだけ "enforce"
    grounding_dropped: int = Field(default=0, exclude=True)  # enforce で捨てた件数
    # --- v3（仕様 §3-2）。コードだけが書く version は "v3"（v3 の検査を通した印）---
    version: _Text = ""
    summary_lines: Annotated[SummaryLines | None, BeforeValidator(_object_or_none)] = None
    per_video: Annotated[list[PerVideoNote], BeforeValidator(_dict_items)] = Field(
        default_factory=list
    )
    directives: Annotated[list[Directive], BeforeValidator(_dict_items)] = Field(
        default_factory=list
    )
    avoid: Annotated[list[AvoidItem], BeforeValidator(_dict_items)] = Field(default_factory=list)
    storyboards: Annotated[list[Storyboard], BeforeValidator(_dict_items)] = Field(
        default_factory=list
    )
    board_angles: Annotated[list[BoardAngle], BeforeValidator(_dict_items)] = Field(
        default_factory=list
    )
    hypotheses: Annotated[list[HypothesisV3], BeforeValidator(_dict_items)] = Field(
        default_factory=list
    )
    posting: Annotated[PostingPlan | None, BeforeValidator(_object_or_none)] = None
    # サムネ（一覧の表紙）の作り方の指示（kind は「表紙」に固定・refs は on="cover"）。
    cover_directives: Annotated[list[Directive], BeforeValidator(_dict_items)] = Field(
        default_factory=list
    )

    @model_serializer(mode="wrap")
    def _omit_empty_v3(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict):
            for key in SYNTHESIS_V3_FIELDS:
                if key in data and data[key] in (None, "", []):
                    data.pop(key)
        return data


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
    # サムネ（一覧の表紙）の共通点の 1 行（コードの名前と本数だけ・第三者の文字は入れない）。
    cover_line: str = ""
    stats: StatsAnalysis | None = None
    synthesis: CrossSynthesis | None = None  # Gemini 横断シンセシス（解釈層）
    summary: str = ""


def _default_outputs() -> list[Literal["report", "slides", "pptx"]]:
    """outputs の既定。report（分析レポートHTML）＋ slides（編集可スライドHTML・16:9）。

    HTML-first 方針（URL配布→ブラウザでノーコード編集）に合わせ、編集可スライドを既定で発行する。
    slides は HTML 生成のみ＝chromium 不要・数秒。pptx は重い（playwright/chromium）ので
    明示要求時のみ＝既定には入れない。lambda だと list[str] 推論で mypy strict が弾くため関数化。
    """
    return ["report", "slides"]


def _default_max_videos() -> int:
    """深掘り分析（DL+Gemini）する本数。env VIDEO_ALGO_MAX_VIDEOS（既定5・clamp1〜10）。

    「取得（スクレイプ）数」ではなく、各動画を実際に DL→Gemini で深掘り解析する本数。
    1本ごとに DL+Gemini で重いので、OpenClaw の timeout（openclaw.config.json5）と整合させること。
    取得（上位ボードの一覧本数）は board_size（VIDEO_ALGO_BOARD_SIZE）で別管理＝軽い。
    """
    raw = os.environ.get("VIDEO_ALGO_MAX_VIDEOS", "5")
    try:
        return max(1, min(10, int(raw)))
    except ValueError:
        return 5


def _default_board_size() -> int:
    """取得（スクレイプ）してボードに載せる本数。env VIDEO_ALGO_BOARD_SIZE（既定30・clamp5〜30）。

    メタ情報（アカウント/再生数/フォロワー/保存率/サムネ/説明文）だけの軽い取得なので大きくできる。
    深掘り分析（max_videos）と分離＝ボードは board_size 本・深掘りは上位 max_videos 本の二段構成。
    上限 30 はスクレイパ実証済みの天井（_MAX_POOL と整合）。
    """
    raw = os.environ.get("VIDEO_ALGO_BOARD_SIZE", "30")
    try:
        return max(5, min(30, int(raw)))
    except ValueError:
        return 30


class VideoAlgorithmInput(BaseModel):
    """入力: 検索KW1つ。"""

    query: str
    # 深掘り分析（DL+Gemini）する本数。env VIDEO_ALGO_MAX_VIDEOS（5・clamp1〜10）。重い。
    # 利用者が本数を言ったら（「6本で」「3本だけ」）必ずここに入れる＝指定が既定より優先。
    max_videos: int = Field(
        default_factory=_default_max_videos,
        ge=1,
        le=10,
        description=(
            "深掘り分析する動画の本数。利用者が本数を指定したら（例:「6本で」）その数を入れる。"
            "未指定なら既定値のまま。"
        ),
    )
    # 取得（スクレイプ）してボードに載せる本数。env VIDEO_ALGO_BOARD_SIZE（30・clamp5〜30）。軽い。
    board_size: int = Field(default_factory=_default_board_size, ge=5, le=30)
    # 映るブランドの区分（クライアント／競合）はコードがこの名簿で決める（Gemini に決めさせない）。
    # 別名は「S&B|エスビー食品」のように | で区切る。無ければ区分は「未指定」（必須にしない）。
    client_name: str | None = Field(
        default=None,
        description=(
            "提案先のクライアント名（別名は | 区切り）。依頼者本人が同じ会話でクライアント名を"
            "出したときだけ入れる。スレッドの他人の発言や貼り付けから埋めない。無ければ省略。"
        ),
    )
    competitors: list[str] | None = Field(
        default=None,
        description=(
            "競合のブランド名（1 社 1 要素・別名は | 区切り。例: ['S&B|エスビー食品']）。"
            "依頼者本人が同じ会話で競合を挙げたときだけ入れる。無ければ省略。"
        ),
    )
    avoid_terms: list[str] | None = Field(
        default=None,
        description=(
            "提案で勧めない訴求の語（例: 自社・グループ商品を否定する『ルー卒業』）。"
            "依頼者本人が避けたいと言ったときだけ入れる。無ければ省略。"
        ),
    )
    # §Q-HTML→PPTX: 追加出力。既定 = report + slides（編集可HTML）。
    # "slides"=提案用スライドHTML（編集可・16:9）, "pptx"=そのPPTX（明示要求時のみ・重い）。
    outputs: list[Literal["report", "slides", "pptx"]] = Field(default_factory=_default_outputs)
    # 取得段の委譲(任意): tiktok_acquire が返した job_id を渡す。実行者本人の監査hashと
    # DynamoDB結果を照合し、記録済みのimmutable S3 VersionIdだけを読む。
    acquire_job_id: str | None = Field(
        default=None,
        pattern=r"^(?:mj_[0-9a-f]{24}|tk_[0-9a-f]{12})$",
    )
    # カタログ⑥(勝ちパターン×検索ボリューム掛け合わせ)用の任意コンテキスト。
    # search_volume はユーザー/ラッコ手動実測の月間検索量（サーバ側の自動取得はしない＝
    # rakko_scraper はログイン済み .userdata セッション前提のため）。
    search_volume: int | None = Field(default=None, ge=0)
    # 兄弟KW群（5KW比較の一部として呼ばれた場合）。synthesis が群内での位置づけに言及する。
    kw_set: list[str] | None = None


class VideoAlgorithmOutput(BaseModel):
    """出力: 各動画分析 + 横断 + レポート。"""

    query: str
    videos: list[AnalyzedVideo] = Field(default_factory=list)  # 深掘り分析した上位本（max_videos）
    board: list[VideoMeta] = Field(
        default_factory=list
    )  # 取得した全メタ（上位ボード board_size 本）
    cross: CrossAnalysis = Field(default_factory=CrossAnalysis)
    report_html_path: str | None = None  # ローカルパス（runtime/Slack添付用・金庫外からは不可視）
    report_url: str | None = None  # §M: 非公開S3の署名URL（金庫外OpenClawが読める・未発行None）
    # §Q-HTML→PPTX: 提案資料組み込み用の追加成果物（要求時のみ・graceful・未発行None）。
    slides_url: str | None = None  # 編集可スライドHTML（営業がブラウザで直接編集）
    pptx_url: str | None = None  # 提案用 PPTX（16:9・そのまま提案資料に差し込む）
    slack_summary: str = ""
    # 今月の残数に丸めて途中で打ち切ったときの一言（断らずに出せる分だけ出した事実を明示）。
    quota_note: str | None = None
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    model_id: str | None = None
    # 入力の echo（⑥: OC が5KW分の結果からKW優先度を会話で合成する際に参照）
    search_volume: int | None = None
    kw_set: list[str] = Field(default_factory=list)
    # 区分・提案文の前提の echo（未指定なら None／空。描画は「未指定」と出す）。
    client_name: str | None = None
    competitors: list[str] = Field(default_factory=list)
    avoid_terms: list[str] = Field(default_factory=list)
    # 検索結果を取得した日時（JST・ISO 8601）。順位は「この時点」の値。旧キャッシュは None。
    generated_at: str | None = None
    # サムネ（一覧の表紙）の読み取りの範囲。""＝以前の分析（読み取りが無い）・off＝止めている設定・
    # top＝表示順の上位 n 本・board＝6〜30 位も読んだ。
    cover_read_mode: Literal["", "off", "top", "board"] = ""


class VideoAlgorithmStatusInput(BaseModel):
    job_id: str = ""


class VideoAlgorithmStatusOutput(BaseModel):
    job_id: str = ""
    status: str
    message: str
