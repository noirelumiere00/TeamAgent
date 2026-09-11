"""切り抜き提案の解析パイプライン（文字起こし → 訴求軸5 → 界隈5 / インサイト5 → 10 clips）。

計画 §2-2「実装方式」の段 4〜6 に対応する。**新しい media operation は作らない**:
動画の取得とフレーム切り出しは omiyage_report の video_analysis と同じ media client
（``acquire_video`` / ``extract_frames``）を使い、推論は既存 ``GeminiClient`` の
``analyze_video_bytes`` / ``generate_text`` を使う。

捏造を通さないための構造（計画 §2-2 安全装置「捏造しない」）:
- Gemini を分割し、**文字起こし専用コール（コール1）の確定 transcript とだけ**
  ``quote_evidence`` を照合する。1 コールで transcript と clips を同時生成させると、
  自己整合的な捏造（存在しない発言を transcript ごと作る）が素通りする。
- コール2 が transcript を返しても **捨てる**（``parse_clips_payload`` は読まない）。
- 界隈言語は実在の観測語のみ。web/X 実測が無い語は ``verified=False`` として
  「（未検証）」付きで描く。素で出すのは実測が取れた語だけ。

untrusted 扱い（同 安全装置「出力の untrusted 扱い」）:
- 動画の音声・テロップは第三者が用意した内容。採用テキストは ``is_safe_output_text``
  で検査し、制御文字・URL・``@mention``・``<!channel>``・Slack markup・連続改行を
  含む clip は **不採用**（資料にもコメントにも出さない）。
- 検査対象は clip 直下のコピーだけではない。``Community.scale_note`` /
  ``Community.description`` / ``CommunityTerm.term`` / ``Insight.evidence_hint`` も
  ``render_detail_lines()`` 経由で **資料本文の段落へ入る**ため、同じ検査を通す。
  1 つでも外れたらその界隈 / インサイトを採らず、それを載せる予定だったセルごと
  落とす（``reason="unsafe_source_text"``・どの語で止まったかを marker に残す）。
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from teamagent.skills.clip_proposal.limits import CostLedger, configured_cost_cap_usd

#: 提案スライドの 1 枚に載るセル数（界隈 5 ＋ インサイト 5）。テンプレ実物の枠数。
CELL_COUNT = 10
COMMUNITY_CELLS = 5
INSIGHT_CELLS = 5
#: 本編から抽出する訴求軸の本数。
AXIS_COUNT = 5

#: 冒頭フックの既定尺（秒）。テンプレ本文『9:16で2秒間をトリミング＝一言フック』に由来。
HOOK_SECONDS_DEFAULT = 2.0
#: フック区間の許容幅（記入例は 2.5〜5.0 秒の幅で使われている）。
HOOK_SECONDS_MIN = 1.5
HOOK_SECONDS_MAX = 6.0
#: 切り抜き 1 本の秒数の許容幅（記入例実測: 15〜21 秒）。
CLIP_SECONDS_MIN = 5.0
CLIP_SECONDS_MAX = 60.0

#: 解析用 proxy の段階的劣化（長辺 px）。**必ず解析まで到達させる**ためのはしご。
LONG_EDGE_LADDER: tuple[int, ...] = (1280, 720, 480, 360, 240)

#: Gemini の inline 上限に収める解析用 proxy のバイト上限。
PROXY_LIMIT_BYTES = 18 * 1024 * 1024

#: 1 依頼で打ってよい有料推論コールの本数（``CLIP_MAX_GEMINI_CALLS`` 既定 5）。
MAX_GEMINI_CALLS_DEFAULT = 5

#: 課金前ゲートの入力トークン見積り閾値。超えたら **1 コールも打たない**。
INPUT_TOKEN_GATE_DEFAULT = 1_100_000
#: 動画 1 秒あたりの概算入力トークン（Gemini の video tokenization 実測に基づく概算）。
TOKENS_PER_VIDEO_SECOND = 300
#: proxy 1KiB あたりの概算入力トークン（``estimate_input_tokens`` の導出を参照）。
TOKENS_PER_PROXY_KIB = 4

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_RE = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
_MENTION_RE = re.compile(r"(?:^|[\s(])@[A-Za-z0-9._-]+")
_SLACK_BROADCAST_RE = re.compile(r"<!(?:channel|here|everyone)\b", re.IGNORECASE)
_SLACK_MARKUP_RE = re.compile(r"<[@#!][A-Za-z0-9]")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


class ClipAnalysisError(RuntimeError):
    """解析の失敗。``code`` は利用者へ出してよい安全なマーカーのみ。

    ``spent_usd`` / ``calls`` は **失敗するまでに実際に課金された分**。失敗経路でも
    日次の費用カウンタへ計上させるために載せる（載せないと「失敗し続ける日は費用 cap が
    一度も発火しない」経路になる）。
    """

    def __init__(
        self, code: str, detail: str = "", *, spent_usd: float = 0.0, calls: int = 0
    ) -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.spent_usd = float(spent_usd)
        self.calls = int(calls)


class ClipCostGateError(ClipAnalysisError):
    """課金前ゲート / cap による停止。**1 コールも打っていない**ことを含意する場合がある。"""


def annotate_spend(exc: BaseException, *, spent_usd: float, calls: int) -> BaseException:
    """例外へ実測課金額を貼る（貼れない例外型でも落とさない）。

    注入された caller（本番は Vertex クライアント）が上げる任意の例外にも貼るので、
    ``setattr`` が通らない型（``__slots__`` 付き等）では黙って諦める。
    """

    try:
        exc.spent_usd = max(float(getattr(exc, "spent_usd", 0.0) or 0.0), float(spent_usd))  # type: ignore[attr-defined]
        exc.calls = max(int(getattr(exc, "calls", 0) or 0), int(calls))  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - 貼れない例外型の保険
        pass
    return exc


def spend_of(exc: BaseException) -> float:
    """例外に貼られた実測課金額（無ければ 0.0）。"""

    try:
        return max(0.0, float(getattr(exc, "spent_usd", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _envint(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = default if not raw else int(raw)
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def configured_max_gemini_calls() -> int:
    return _envint("CLIP_MAX_GEMINI_CALLS", MAX_GEMINI_CALLS_DEFAULT, minimum=1, maximum=12)


def configured_input_token_gate() -> int:
    return _envint(
        "CLIP_INPUT_TOKEN_GATE",
        INPUT_TOKEN_GATE_DEFAULT,
        minimum=10_000,
        maximum=20_000_000,
    )


# ---------------------------------------------------------------------------
# untrusted テキストの検査
# ---------------------------------------------------------------------------


def is_safe_output_text(text: str) -> bool:
    """資料 / Slack コメントへ載せてよいテキストか。

    動画の音声・テロップは第三者が用意した内容なので、許可文字集合で検査する。
    制御文字・URL・``@mention``・``<!channel>``・Slack markup・3 連以上の改行を
    含むものは False（その clip ごと不採用にする）。
    """

    if not text or not text.strip():
        return False
    if _CONTROL_RE.search(text):
        return False
    if _URL_RE.search(text):
        return False
    if _MENTION_RE.search(text):
        return False
    if _SLACK_BROADCAST_RE.search(text):
        return False
    if _SLACK_MARKUP_RE.search(text):
        return False
    if _MULTI_NEWLINE_RE.search(text):
        return False
    return True


def normalize_for_match(text: str) -> str:
    """引用照合用の正規化（NFKC・空白と約物の揺れを潰す）。

    音声の書き起こしは読点・中黒・全半角が揺れる。素の substring 照合だと
    「実在する発言なのに落ちる」が多発し、結局照合を緩める圧力になるため、
    **照合前に潰す**（照合そのものは緩めない）。
    """

    folded = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"[\s、。，．・「」『』　\"'!?！？…ー–—-]+", "", folded).lower()


# ---------------------------------------------------------------------------
# transcript（コール1 の確定値）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptSegment:
    start_sec: float
    end_sec: float
    text: str


@dataclass(frozen=True)
class Transcript:
    """コール1 が返した確定 transcript。**ジョブ行には保存しない**（覗き見防止）。"""

    segments: tuple[TranscriptSegment, ...] = ()
    duration_sec: float = 0.0

    @property
    def full_text(self) -> str:
        return "\n".join(segment.text for segment in self.segments)

    @property
    def normalized(self) -> str:
        return normalize_for_match(self.full_text)

    def contains(self, quote: str) -> bool:
        """``quote`` が確定 transcript に実在するか（正規化した部分一致）。"""

        needle = normalize_for_match(quote)
        if len(needle) < 4:  # 短すぎる断片は「実在」の証明にならない
            return False
        return needle in self.normalized

    def as_prompt_text(self) -> str:
        """コール2 へ **テキストで** 渡す確定 transcript（動画は渡し直さない）。"""

        return "\n".join(
            f"[{segment.start_sec:.1f}-{segment.end_sec:.1f}] {segment.text}"
            for segment in self.segments
        )


def parse_transcript_payload(raw: str, *, duration_sec: float = 0.0) -> Transcript:
    """コール1 の JSON を Transcript へ。壊れていれば ``CLIP_TRANSCRIPT_INVALID``。"""

    payload = _load_json_object(raw, code="CLIP_TRANSCRIPT_INVALID")
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ClipAnalysisError("CLIP_TRANSCRIPT_EMPTY")
    segments: list[TranscriptSegment] = []
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        start = _as_float(item.get("start_sec"))
        end = _as_float(item.get("end_sec"))
        if end < start:
            start, end = end, start
        segments.append(TranscriptSegment(start_sec=start, end_sec=end, text=text))
    if not segments:
        raise ClipAnalysisError("CLIP_TRANSCRIPT_EMPTY")
    detected = _as_float(payload.get("duration_sec"))
    return Transcript(
        segments=tuple(segments),
        duration_sec=duration_sec or detected or segments[-1].end_sec,
    )


# ---------------------------------------------------------------------------
# 訴求軸 / 界隈 / インサイト / clip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppealAxis:
    """本編での訴求軸（＝強み）。**本編で実際に語られた事実に限る**。"""

    index: int
    text: str
    quote_evidence: str = ""


@dataclass(frozen=True)
class CommunityTerm:
    """界隈言語 1 語。``verified=False`` は web/X 実測が取れていない語。"""

    term: str
    verified: bool = False

    def render(self) -> str:
        return self.term if self.verified else f"{self.term}（未検証）"


@dataclass(frozen=True)
class Community:
    """界隈（3 テスト＋質的 3 条件を通ったもの）。規模は常に仮定値として描く。"""

    name: str
    terms: tuple[CommunityTerm, ...] = ()
    scale_note: str = ""
    description: str = ""
    is_primary: bool = False

    def render_name(self) -> str:
        return f"{self.name}（主戦場）" if self.is_primary else self.name

    def render_detail_lines(self) -> tuple[str, str, str]:
        """界隈詳細 3 行（・界隈言語 / ・規模 / ・説明）。テンプレの段落構成に合わせる。"""

        terms = "／".join(term.render() for term in self.terms) or "（実在語の確認が取れず）"
        scale = self.scale_note.strip() or "規模は未算出"
        return (
            f"・界隈言語：{terms}",
            f"・規模：{scale}（仮定値）",
            f"・{self.description.strip()}",
        )


@dataclass(frozen=True)
class Insight:
    """インサイト特化文脈の 1 セル（属性 × 本音）。"""

    target: str
    insight: str
    evidence_hint: str = ""

    def render_detail_lines(self) -> tuple[str, str]:
        return (
            f"・インサイト：{self.insight.strip()}",
            f"・判断材料：{self.evidence_hint.strip() or '本編で語られた事実'}",
        )


@dataclass(frozen=True)
class ClipWindow:
    """本編での秒区間。``hook_*`` は冒頭フックに使う区間（clip の内側）。"""

    start_sec: float
    end_sec: float
    hook_start_sec: float
    hook_end_sec: float

    @property
    def seconds(self) -> float:
        return round(self.end_sec - self.start_sec, 1)

    @property
    def hook_seconds(self) -> float:
        return round(self.hook_end_sec - self.hook_start_sec, 1)

    def render_range(self) -> str:
        return f"{_mmss(self.start_sec)}〜{_mmss(self.end_sec)}（{self.seconds:g}秒）"

    def render_hook(self) -> str:
        return (
            f"冒頭{self.hook_seconds:g}秒＝フック映像"
            f"（{_mmss(self.hook_start_sec)}〜{_mmss(self.hook_end_sec)}）"
        )


@dataclass(frozen=True)
class ClipPlan:
    """1 セル分の切り抜き案。``kind`` で上段（界隈）／下段（インサイト）を分ける。"""

    cell_index: int
    kind: Literal["community", "insight"]
    label: str
    detail_lines: tuple[str, ...]
    window: ClipWindow
    hook_copy: str
    band_top: str
    band_bottom: str
    search_word: str
    quote_evidence: str
    takeaway: str = ""

    def render_note_lines(self) -> tuple[str, ...]:
        """切り抜きメモ枠の段落（記入例実測: 6 段落）。"""

        return (
            "切り抜き箇所",
            f"・{self.window.render_range()}",
            f"・{self.window.render_hook()}",
            "",
            "伝わること",
            f"・{self.takeaway.strip() or '本編で語られた事実が短時間で伝わる'}",
        )


@dataclass(frozen=True)
class DroppedClip:
    """不採用になった clip と理由（**どの語で止まったかを必ず出す**）。"""

    cell_index: int
    reason: str
    marker: str = ""


@dataclass(frozen=True)
class ClipProposalAnalysis:
    """解析 1 回分の結果。transcript 本文は持たない（採用後の値のみ）。"""

    client_name: str
    axes: tuple[AppealAxis, ...] = ()
    clips: tuple[ClipPlan, ...] = ()
    dropped: tuple[DroppedClip, ...] = ()
    quality_note: str = ""
    long_edge_used: int = 0
    cost_usd: float = 0.0
    gemini_calls: int = 0
    model_id: str = ""

    @property
    def clip_count(self) -> int:
        return len(self.clips)


# ---------------------------------------------------------------------------
# 秒区間の決定（決定論・LLM の値を必ず clamp する）
# ---------------------------------------------------------------------------


def clamp_window(
    start_sec: float,
    end_sec: float,
    *,
    duration_sec: float,
    hook_start_sec: float | None = None,
    hook_seconds: float = HOOK_SECONDS_DEFAULT,
) -> ClipWindow:
    """LLM が申告した秒区間を本編の尺の内側へ必ず収める。

    冒頭フック区間は **clip の内側** に置く（clip の外を指すフックは本編に存在しない
    映像を提案することになる）。``duration_sec`` が 0 以下なら ``CLIP_WINDOW_INVALID``。
    """

    if duration_sec <= 0:
        raise ClipAnalysisError("CLIP_WINDOW_INVALID", "duration")
    start = max(0.0, min(float(start_sec), duration_sec))
    end = max(0.0, min(float(end_sec), duration_sec))
    if end < start:
        start, end = end, start
    length = end - start
    if length < CLIP_SECONDS_MIN:
        end = min(duration_sec, start + CLIP_SECONDS_MIN)
        start = max(0.0, end - CLIP_SECONDS_MIN)
    if end - start > CLIP_SECONDS_MAX:
        end = start + CLIP_SECONDS_MAX
    if end - start < CLIP_SECONDS_MIN:
        raise ClipAnalysisError("CLIP_WINDOW_TOO_SHORT")

    hook_len = min(max(float(hook_seconds), HOOK_SECONDS_MIN), HOOK_SECONDS_MAX)
    hook_len = min(hook_len, end - start)
    hook_start = start if hook_start_sec is None else float(hook_start_sec)
    hook_start = max(start, min(hook_start, end - hook_len))
    return ClipWindow(
        start_sec=round(start, 1),
        end_sec=round(end, 1),
        hook_start_sec=round(hook_start, 1),
        hook_end_sec=round(hook_start + hook_len, 1),
    )


def _mmss(seconds: float) -> str:
    total = max(0, round(seconds))
    return f"{total // 60}:{total % 60:02d}"


# ---------------------------------------------------------------------------
# コール2 のパース（transcript は読まない）
# ---------------------------------------------------------------------------


def _load_json_object(raw: str, *, code: str) -> dict[str, Any]:
    match = _JSON_BLOCK_RE.search(raw or "")
    if match is None:
        raise ClipAnalysisError(code, "no_json")
    try:
        payload = json.loads(match.group(0))
    except ValueError as exc:
        raise ClipAnalysisError(code, "bad_json") from exc
    if not isinstance(payload, dict):
        raise ClipAnalysisError(code, "not_object")
    return payload


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_axes(payload: dict[str, Any], transcript: Transcript) -> tuple[AppealAxis, ...]:
    """訴求軸 5 つ。**確定 transcript に実在する引用**を持つものだけ採る。"""

    axes: list[AppealAxis] = []
    for item in payload.get("axes") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        quote = str(item.get("quote_evidence", "")).strip()
        if not text or not is_safe_output_text(text):
            continue
        if not transcript.contains(quote):
            continue
        axes.append(AppealAxis(index=len(axes) + 1, text=text, quote_evidence=quote))
        if len(axes) >= AXIS_COUNT:
            break
    return tuple(axes)


def _first_unsafe(*values: str) -> str:
    """資料本文へ入る文字列のうち、最初に検査を外れたもの（無ければ空文字）。"""

    return next((value for value in values if value and not is_safe_output_text(value)), "")


def _parse_communities(
    payload: dict[str, Any],
) -> tuple[list[Community | None], dict[int, str]]:
    """界隈を index 保存で採る。**採れなかった枠は ``None`` で場所を残す**。

    落とした分を詰めると、界隈 2 の内容が界隈 1 のセルへ繰り上がって
    「別の界隈の説明が別のセルへ入る」取り違えになる。検査対象は ``name`` だけでなく
    ``render_detail_lines()`` が資料へ流す ``terms`` / ``scale_note`` / ``description``
    を全部含む（1 つでも外れたらその界隈ごと不採用）。
    """

    communities: list[Community | None] = []
    rejected: dict[int, str] = {}
    for item in payload.get("communities") or []:
        if not isinstance(item, dict):
            continue
        index = len(communities)
        name = str(item.get("name", "")).strip()
        scale_note = str(item.get("scale_note", "")).strip()
        description = str(item.get("description", "")).strip()
        raw_terms: list[tuple[str, bool]] = []
        for term_item in item.get("terms") or []:
            if isinstance(term_item, dict):
                term = str(term_item.get("term", "")).strip()
                verified = bool(term_item.get("observed_on_web"))
            else:
                term, verified = str(term_item).strip(), False
            if term:
                raw_terms.append((term, verified))

        unsafe = _first_unsafe(name, scale_note, description, *(term for term, _ in raw_terms))
        if not name or unsafe:
            communities.append(None)
            if unsafe:
                rejected[index] = unsafe[:24]
            continue
        communities.append(
            Community(
                name=name,
                terms=tuple(
                    CommunityTerm(term=term, verified=verified) for term, verified in raw_terms
                ),
                scale_note=scale_note,
                description=description,
                is_primary=bool(item.get("is_primary")),
            )
        )
    return communities, rejected


def _parse_insights(payload: dict[str, Any]) -> tuple[list[Insight | None], dict[int, str]]:
    """インサイトを index 保存で採る（``evidence_hint`` も資料本文なので検査対象）。"""

    insights: list[Insight | None] = []
    rejected: dict[int, str] = {}
    for item in payload.get("insights") or []:
        if not isinstance(item, dict):
            continue
        index = len(insights)
        target = str(item.get("target", "")).strip()
        insight = str(item.get("insight", "")).strip()
        evidence_hint = str(item.get("evidence_hint", "")).strip()
        unsafe = _first_unsafe(target, insight, evidence_hint)
        if not target or not insight or unsafe:
            insights.append(None)
            if unsafe:
                rejected[index] = unsafe[:24]
            continue
        insights.append(Insight(target=target, insight=insight, evidence_hint=evidence_hint))
    return insights, rejected


def parse_clips_payload(
    raw: str,
    transcript: Transcript,
    *,
    client_name: str = "",
    search_word: str = "",
) -> tuple[tuple[AppealAxis, ...], tuple[ClipPlan, ...], tuple[DroppedClip, ...]]:
    """コール2 の JSON を 訴求軸 / 10 clips へ。

    **``payload['transcript']` は読まない**（コール2 が返しても捨てる）。採用の条件は
    確定 transcript（コール1）との照合だけ。落ちた clip は理由つきで返す。
    """

    payload = _load_json_object(raw, code="CLIP_PLAN_INVALID")
    axes = parse_axes(payload, transcript)
    communities, rejected_communities = _parse_communities(payload)
    insights, rejected_insights = _parse_insights(payload)

    clips: list[ClipPlan] = []
    dropped: list[DroppedClip] = []
    fallback_search = (search_word or client_name).strip()

    for index, item in enumerate(payload.get("clips") or []):
        if index >= CELL_COUNT:
            break
        if not isinstance(item, dict):
            dropped.append(DroppedClip(cell_index=index, reason="malformed"))
            continue
        kind: Literal["community", "insight"] = (
            "community" if index < COMMUNITY_CELLS else "insight"
        )
        quote = str(item.get("quote_evidence", "")).strip()
        if not transcript.contains(quote):
            dropped.append(DroppedClip(cell_index=index, reason="quote_not_in_transcript"))
            continue

        band_top = str(item.get("band_top", "")).strip()
        band_bottom = str(item.get("band_bottom", "")).strip()
        hook_copy = str(item.get("hook_copy", "")).strip()
        takeaway = str(item.get("takeaway", "")).strip()
        search = str(item.get("search_word", "")).strip() or fallback_search
        unsafe = next(
            (
                value
                for value in (band_top, band_bottom, hook_copy, takeaway, search)
                if value and not is_safe_output_text(value)
            ),
            "",
        )
        if unsafe:
            dropped.append(DroppedClip(cell_index=index, reason="unsafe_text", marker=unsafe[:24]))
            continue
        if not (band_top and band_bottom and hook_copy and search):
            dropped.append(DroppedClip(cell_index=index, reason="incomplete"))
            continue

        try:
            window = clamp_window(
                _as_float(item.get("start_sec")),
                _as_float(item.get("end_sec")),
                duration_sec=transcript.duration_sec,
                hook_start_sec=(
                    _as_float(item["hook_start_sec"]) if "hook_start_sec" in item else None
                ),
                hook_seconds=_as_float(item.get("hook_seconds")) or HOOK_SECONDS_DEFAULT,
            )
        except ClipAnalysisError as exc:
            dropped.append(DroppedClip(cell_index=index, reason=exc.code))
            continue

        # 界隈 / インサイト側が untrusted 検査で落ちていたら、そのセルごと採らない
        # （フォールバック文言で描くと「何の界隈でもないセル」が資料へ残る）。
        slot = index if kind == "community" else index - COMMUNITY_CELLS
        rejected = rejected_communities if kind == "community" else rejected_insights
        if slot in rejected:
            dropped.append(
                DroppedClip(cell_index=index, reason="unsafe_source_text", marker=rejected[slot])
            )
            continue

        detail: tuple[str, ...]
        if kind == "community":
            source = communities[slot] if slot < len(communities) else None
            label = source.render_name() if source else "界隈（要確認）"
            detail = (
                source.render_detail_lines()
                if source
                else ("・界隈言語：（未検証）", "・規模：未算出（仮定値）", "・要確認")
            )
        else:
            insight_source = insights[slot] if slot < len(insights) else None
            label = insight_source.target if insight_source else "ターゲット（要確認）"
            detail = (
                insight_source.render_detail_lines()
                if insight_source
                else ("・インサイト：要確認", "・判断材料：本編で語られた事実")
            )

        clips.append(
            ClipPlan(
                cell_index=index,
                kind=kind,
                label=label,
                detail_lines=tuple(detail),
                window=window,
                hook_copy=hook_copy,
                band_top=band_top,
                band_bottom=band_bottom,
                search_word=f"🔍{search}" if not search.startswith("🔍") else search,
                quote_evidence=quote,
                takeaway=takeaway,
            )
        )
    return axes, tuple(clips), tuple(dropped)


# ---------------------------------------------------------------------------
# proxy の段階的劣化
# ---------------------------------------------------------------------------


def plan_long_edge_ladder(
    *,
    size_bytes: int,
    limit_bytes: int = PROXY_LIMIT_BYTES,
    source_long_edge: int = LONG_EDGE_LADDER[0],
) -> tuple[int, ...]:
    """解析用 proxy の長辺候補。**最初から入りそうにない段は飛ばす**。

    「1280 で入らなければ 720→480→360→240 へ落ちて **必ず解析まで到達させる**」
    （計画 §2-2「できないことを手作業へ突き返さない」）。入らないと分かっている段を
    律儀に再エンコードすると、上限 18MB に対して 100MB の素材で 2〜3 回ぶんの
    無駄な変換時間を使う。バイト数は解像度の面積にほぼ比例するので、
    ``size_bytes × (rung / source_long_edge)²`` が上限を下回る最初の段から始める。

    尺が分からない・サイズが取れない（``size_bytes <= 0``）回は削らず全段返す。
    どの段でも入らない見積りでも **最下段だけは必ず返す**（手作業へ突き返さない）。
    """

    if size_bytes <= 0 or size_bytes <= limit_bytes:
        return LONG_EDGE_LADDER
    base = max(1, int(source_long_edge))
    for position, rung in enumerate(LONG_EDGE_LADDER):
        if size_bytes * (rung / base) ** 2 <= limit_bytes:
            return LONG_EDGE_LADDER[position:]
    return LONG_EDGE_LADDER[-1:]


def quality_note_for(long_edge: int) -> str:
    """品質が落ちた回の但し書き。最上段で通ったときは空文字。"""

    if long_edge >= LONG_EDGE_LADDER[0]:
        return ""
    return (
        f"動画が長いため解析用の画質を落としています（長辺 {long_edge}px）。"
        "テロップが読み取れなかったため音声中心で分析しました。"
    )


def estimate_input_tokens(*, duration_sec: float, size_bytes: int) -> int:
    """課金前ゲート用の入力トークン見積り（proxy の尺とバイト数から）。

    バイト項は「尺が取れない回の粗い下限」でしかない。長辺 1280 の proxy は
    概ね 1.2Mbps 前後＝ 1 秒あたり約 150KB なので、1 秒 300 tokens から逆算すると
    1KiB ≒ 2 tokens。実測ばらつきに対して安全側（過大評価側）へ 2 倍を取り
    ``TOKENS_PER_PROXY_KIB = 4`` とする。旧実装の 1KiB = 1 token は 18MB の proxy で
    18,432 tokens ＝ ゲート既定の約 1/60 で、バイト側が事実上無効だった。
    """

    by_duration = int(max(0.0, duration_sec) * TOKENS_PER_VIDEO_SECOND)
    by_bytes = int(max(0, size_bytes) / 1024 * TOKENS_PER_PROXY_KIB)
    return max(by_duration, by_bytes)


# ---------------------------------------------------------------------------
# 解析実行体
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeminiCall:
    """1 コールの結果（費用は **実測 usage**・リトライ分も含む）。"""

    text: str
    cost_usd: float = 0.0
    model_id: str = ""
    finish_reason: str = ""


#: コール1（動画 → transcript）。テストでは fake を注入する。
TranscriptCaller = Callable[[bytes, str, str], GeminiCall]
#: コール2/3（テキスト → JSON）。動画は渡さない。
TextCaller = Callable[[str, str], GeminiCall]


def _default_transcript_caller(data: bytes, mime_type: str, prompt: str) -> GeminiCall:
    """本番: Vertex Gemini へ inline 動画 1 コール（API キー経路へは倒さない）。"""

    from teamagent.adapters.gemini_client import GeminiClient

    client = GeminiClient.from_env()
    response = client.analyze_video_bytes(data, mime_type, prompt, "clip-proposal-transcript")
    return GeminiCall(
        text=response.text, cost_usd=float(response.cost_usd), model_id=response.model_id
    )


def _default_text_caller(prompt: str, request_id: str) -> GeminiCall:
    from teamagent.adapters.gemini_client import GeminiClient

    client = GeminiClient.from_env()
    response = client.generate_text(prompt, request_id)
    return GeminiCall(
        text=response.text, cost_usd=float(response.cost_usd), model_id=response.model_id
    )


TRANSCRIPT_PROMPT = (
    "この動画の音声を、話者と時刻つきで文字起こししてください。"
    "画面のテロップは音声と区別し、読めた範囲だけ text に含めてください。"
    "推測で補完してはいけません。聞き取れない箇所は省略してください。\n"
    'JSON のみで応答: {"duration_sec": <尺の秒数>, "segments": '
    '[{"start_sec": <開始秒>, "end_sec": <終了秒>, "text": "<発話>"}]}'
)


def build_clips_prompt(transcript: Transcript, *, client_name: str) -> str:
    """コール2 のプロンプト。**確定 transcript をテキストで渡す**（動画は渡さない）。"""

    who = client_name.strip() or "（クライアント名 未確定）"
    return (
        f"次は「{who}」のビデオリリース本編の確定文字起こしです。"
        "この文字起こしに実在する発言だけを根拠に、切り抜き提案を組んでください。\n\n"
        f"{transcript.as_prompt_text()}\n\n"
        "手順:\n"
        f"① 訴求軸を {AXIS_COUNT} つ抽出する（本編で実際に語られた事実に限る）。\n"
        "② 界隈を 5 つ選ぶ。3 テスト（当事者が自称する／SNS で語が流通している／"
        "ゆるく相互作用している）を満たすものだけ。主戦場 1 ＋波及先 4。\n"
        "③ 界隈言語は実在の観測語のみ。web で流通を確認できた語だけ "
        '"observed_on_web": true にする。確認できない語は false にする（捏造しない）。\n'
        "④ インサイト特化の属性を 5 つ（誰の・どんな本音か）。\n"
        f"⑤ 切り抜きを {CELL_COUNT} 本（界隈 5 → インサイト 5 の順）。各本に、本編での"
        "秒区間・冒頭フックの秒区間・上帯コピー・下帯コピー・指名検索ワードを付ける。\n"
        "各要素の quote_evidence には、上の文字起こしから**そのまま抜いた一文**を入れる。\n"
        'JSON のみで応答: {"axes": [{"text": "", "quote_evidence": ""}], '
        '"communities": [{"name": "", "is_primary": false, "terms": '
        '[{"term": "", "observed_on_web": false}], "scale_note": "", "description": ""}], '
        '"insights": [{"target": "", "insight": "", "evidence_hint": ""}], '
        '"clips": [{"start_sec": 0, "end_sec": 0, "hook_start_sec": 0, "hook_seconds": 2, '
        '"hook_copy": "", "band_top": "", "band_bottom": "", "search_word": "", '
        '"takeaway": "", "quote_evidence": ""}]}'
    )


@dataclass
class ClipProposalAnalyzer:
    """解析パイプラインの実行体（推論コールは注入可能）。

    ``cost_cap_usd`` の分母は **実測 usage の累計 USD**。リトライ分も足す
    （``GEMINI_RETRY_MAX_ATTEMPTS`` × ``CLIP_MAX_GEMINI_CALLS`` で最大 15 回の
    有料推論になりうるため、コール数ではなく金額で止める）。
    """

    request_id: str
    transcript_caller: TranscriptCaller = _default_transcript_caller
    text_caller: TextCaller = _default_text_caller
    cost_cap_usd: float = field(default_factory=configured_cost_cap_usd)
    max_calls: int = field(default_factory=configured_max_gemini_calls)
    input_token_gate: int = field(default_factory=configured_input_token_gate)

    def preflight_cost_gate(self, *, duration_sec: float, size_bytes: int) -> None:
        """課金前ゲート。閾値超なら **1 コールも打たずに** 失敗させる。

        **尺が取れない回は素通りさせない**（``CLIP_DURATION_UNKNOWN``）。ゲートは実質
        尺で効いていて、バイト項は 18MB の proxy でも閾値の 1/15 にしかならない。
        ``duration_sec=0.0`` を許すと、100MB の素材でもゲートが 1 度も発火しないまま
        有料コールへ入る。media client の配線が入るまで本番で現実に起きうる失敗モード。
        """

        if duration_sec <= 0:
            raise ClipCostGateError("CLIP_DURATION_UNKNOWN")
        estimated = estimate_input_tokens(duration_sec=duration_sec, size_bytes=size_bytes)
        if estimated > self.input_token_gate:
            raise ClipCostGateError("CLIP_INPUT_TOO_LARGE", f"tokens~{estimated}")

    def run(
        self,
        *,
        video_bytes: bytes,
        mime_type: str,
        duration_sec: float,
        client_name: str,
        long_edge: int = LONG_EDGE_LADDER[0],
        search_word: str = "",
    ) -> ClipProposalAnalysis:
        """文字起こし → 訴求軸 / 界隈 / インサイト / clips まで 1 依頼ぶん走らせる。"""

        self.preflight_cost_gate(duration_sec=duration_sec, size_bytes=len(video_bytes))
        ledger = CostLedger(cap_usd=self.cost_cap_usd)

        try:
            call1 = self._call(
                ledger, lambda: self.transcript_caller(video_bytes, mime_type, TRANSCRIPT_PROMPT)
            )
            ledger = ledger.add(call1.cost_usd)
            self._raise_if_capped(ledger)
            transcript = parse_transcript_payload(call1.text, duration_sec=duration_sec)

            prompt = build_clips_prompt(transcript, client_name=client_name)
            call2 = self._call(
                ledger, lambda: self.text_caller(prompt, f"{self.request_id}:clip-plan")
            )
            ledger = ledger.add(call2.cost_usd)
            self._raise_if_capped(ledger)

            axes, clips, dropped = parse_clips_payload(
                call2.text,
                transcript,
                client_name=client_name,
                search_word=search_word,
            )
        except Exception as exc:
            # 失敗しても **そこまでに実課金した分**を呼び出し側へ渡す。
            # 渡さないと、出力が壊れ続ける日は日次の費用 cap が一度も発火しない。
            annotate_spend(exc, spent_usd=ledger.spent_usd, calls=ledger.calls)
            raise
        return ClipProposalAnalysis(
            client_name=client_name,
            axes=axes,
            clips=clips,
            dropped=dropped,
            quality_note=quality_note_for(long_edge),
            long_edge_used=long_edge,
            cost_usd=round(ledger.spent_usd, 6),
            gemini_calls=ledger.calls,
            model_id=call1.model_id or call2.model_id,
        )

    def _call(self, ledger: CostLedger, invoke: Callable[[], GeminiCall]) -> GeminiCall:
        if ledger.calls >= self.max_calls:
            raise ClipCostGateError("CLIP_MAX_CALLS_EXCEEDED")
        if ledger.exhausted:
            raise ClipCostGateError("CLIP_COST_CAP_EXCEEDED")
        call = invoke()
        if call.finish_reason == "MAX_TOKENS":
            # 寛容パースに渡さない（途中で切れた JSON を「読めた」ことにしない）。
            # このコールは **既に課金されている**ので、その分も載せて上げる。
            raise ClipAnalysisError(
                "CLIP_OUTPUT_TRUNCATED",
                spent_usd=ledger.spent_usd + max(0.0, call.cost_usd),
                calls=ledger.calls + 1,
            )
        return call

    def _raise_if_capped(self, ledger: CostLedger) -> None:
        if ledger.exhausted:
            raise ClipCostGateError("CLIP_COST_CAP_EXCEEDED", f"{ledger.spent_usd:.4f}")


def select_own_video(
    files: Sequence[Any],
    *,
    uploader_id: str,
    file_id: str = "",
    max_bytes: int,
) -> tuple[dict[str, Any] | None, str]:
    """素材の同意: **依頼スレッド内、かつ本人がアップロードした動画** だけを候補にする。

    チャンネル履歴へフォールバックしない（他人が貼った動画を外部へ送り再配布する
    経路を塞ぐ）。返り値 ``(file, reason)``・``reason`` は ``not_found`` /
    ``too_large`` / ``not_owner``。
    """

    from teamagent.skills.video_capture.attachment import is_video_attachment

    if not uploader_id:
        return None, "not_owner"
    oversized = False
    foreign = False
    for item in files:
        if not isinstance(item, dict) or not is_video_attachment(item):
            continue
        if file_id and str(item.get("id") or "") != file_id:
            continue
        if str(item.get("user") or "") != uploader_id:
            foreign = True
            continue
        size = item.get("size")
        if isinstance(size, int) and size > max_bytes:
            oversized = True
            continue
        return dict(item), ""
    if oversized:
        return None, "too_large"
    return None, "not_owner" if foreign else "not_found"


__all__ = [
    "AXIS_COUNT",
    "CELL_COUNT",
    "CLIP_SECONDS_MAX",
    "CLIP_SECONDS_MIN",
    "COMMUNITY_CELLS",
    "HOOK_SECONDS_DEFAULT",
    "INSIGHT_CELLS",
    "LONG_EDGE_LADDER",
    "PROXY_LIMIT_BYTES",
    "TOKENS_PER_PROXY_KIB",
    "TRANSCRIPT_PROMPT",
    "AppealAxis",
    "ClipAnalysisError",
    "ClipCostGateError",
    "ClipPlan",
    "ClipProposalAnalysis",
    "ClipProposalAnalyzer",
    "ClipWindow",
    "Community",
    "CommunityTerm",
    "DroppedClip",
    "GeminiCall",
    "Insight",
    "Transcript",
    "TranscriptSegment",
    "annotate_spend",
    "build_clips_prompt",
    "clamp_window",
    "estimate_input_tokens",
    "is_safe_output_text",
    "normalize_for_match",
    "parse_axes",
    "parse_clips_payload",
    "parse_transcript_payload",
    "plan_long_edge_ladder",
    "quality_note_for",
    "select_own_video",
    "spend_of",
]
