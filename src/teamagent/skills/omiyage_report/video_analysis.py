"""便1動画解析（便2の核の前倒し統合・2026-08-24 ユーザー裁定）。

パイプライン: 動画DL（media worker yt-dlp）→ フレーム切り出し（1秒1フレーム・
最大12枚/本 = extract_frames 契約上限）→ Bedrock視覚AI 1コールで
(a) 界隈クラスタ分類（既定語彙・案件ごとに差し替え可能な rules 設計）
(b) テロップ文字の読取
を同時に行う。コンタクトシート合成オペは media worker に存在しないため、
12枚/コールをそのまま画像ブロックで渡す（視覚AI呼び出しは1本=1コールに節約済み）。

規律:
- 同時実行数（chunk 並列）と1ジョブのコスト上限を持つ。上限到達で残りは
  skipped（cost_cap）として監査記録に残し、解析済み分だけで集計する。
- 取得・解析に失敗した動画は失敗一覧（stage/code付き）へ記録し、
  未解析のままクラスタ・テロップ分母へ混ぜない。
- アクセス制限（BOT_WALL 等）は回避しない。失敗として開示する（CANON §3）。
- 実行後の概算コスト（Bedrock実測 + media実行の見積り）をジョブ記録へ残す。
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from teamagent.skills.omiyage_report.metrics import PostRecord

_MAX_FRAMES_PER_VIDEO = 12  # extract_frames 契約の timecodes max 12

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_SAFE_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,64}\b")


def _envint(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def _envfloat(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


@dataclass(frozen=True)
class ClusterRules:
    """界隈クラスタの分類語彙（案件ごとに差し替え可能・SKILL §6 market_category_rules と同型）。"""

    vocabulary: tuple[str, ...]
    guidance: str = ""


DEFAULT_CLUSTER_RULES = ClusterRules(
    vocabulary=(
        "正直レビュー/検証系",
        "成分オタク系",
        "ベスコス/まとめ系",
        "メンズ美容系",
        "専門家/医師系",
        "PR/タイアップ明記",
    ),
    guidance=(
        "動画のフレーム列から投稿の性格を1つだけ選ぶ。"
        "使用感を率直に検証していれば正直レビュー/検証系、"
        "成分・処方の解説が主なら成分オタク系、"
        "複数商品の比較・ランキングならベスコス/まとめ系、"
        "男性向け美容文脈ならメンズ美容系、"
        "医師・専門家の立場からの解説なら専門家/医師系、"
        "画面上にPR・タイアップの明記が見えるならPR/タイアップ明記。"
    ),
)


@dataclass(frozen=True)
class VideoAnalysisSuccess:
    video_id: str
    url: str
    cluster: str
    telop_text: str
    frames_used: int
    cost_usd: float
    model_id: str = ""
    # 1フレーム目の実画像（JPEG/PNG bytes）。デッキのカード画像（image_kind=real_frame）
    # に data URI 埋め込みで再利用する。監査JSON・ジョブ結果には載せない（bytesのまま）。
    thumb_bytes: bytes = b""


@dataclass(frozen=True)
class VideoAnalysisFailure:
    video_id: str
    url: str
    stage: str  # acquire | frames | vision | parse
    code: str


@dataclass(frozen=True)
class VideoAnalysisReport:
    """解析ジョブ1回分の結果と監査材料。"""

    results: tuple[VideoAnalysisSuccess, ...] = ()
    failures: tuple[VideoAnalysisFailure, ...] = ()
    skipped_video_ids: tuple[str, ...] = ()
    skip_reason: str = ""
    requested: int = 0
    cost_cap_usd: float = 0.0
    cost_usd_estimate: float = 0.0
    model_id: str = ""
    sampling_note: str = ""
    vocabulary: tuple[str, ...] = ()

    @property
    def analyzed(self) -> int:
        return len(self.results)

    @property
    def telops(self) -> dict[str, str]:
        return {result.video_id: result.telop_text for result in self.results}

    @property
    def assignments(self) -> dict[str, str]:
        return {result.video_id: result.cluster for result in self.results}

    @property
    def thumbs(self) -> dict[str, bytes]:
        """実フレーム画像（video_id → bytes）。取得できた動画のみ。"""
        return {
            result.video_id: result.thumb_bytes for result in self.results if result.thumb_bytes
        }

    def to_audit(self) -> dict[str, object]:
        """監査JSON断片（取得失敗一覧・スキップ・コスト・サンプリング方針）。"""
        return {
            "requested": self.requested,
            "analyzed": self.analyzed,
            "failures": [
                {
                    "video_id": failure.video_id,
                    "url": failure.url,
                    "stage": failure.stage,
                    "code": failure.code,
                }
                for failure in self.failures
            ],
            "skipped_video_ids": list(self.skipped_video_ids),
            "skip_reason": self.skip_reason,
            "cost_cap_usd": self.cost_cap_usd,
            "cost_usd_estimate": round(self.cost_usd_estimate, 4),
            "model_id": self.model_id,
            "sampling_note": self.sampling_note,
            "cluster_vocabulary": list(self.vocabulary),
        }


@dataclass(frozen=True)
class VisionVerdict:
    """視覚AI 1コールの構造化結果。"""

    cluster: str
    telop_text: str
    cost_usd: float
    model_id: str = ""


# frames: [(秒, jpeg bytes)] を受け取り VisionVerdict を返す（テストでは fake を注入）。
VisionCaller = Callable[[Sequence[tuple[float, bytes]], "ClusterRules", str], VisionVerdict]


class VisionParseError(RuntimeError):
    """視覚AI出力が契約（語彙内クラスタ+telop_text）を満たさない。"""


def plan_timecodes(duration_sec: int, *, max_frames: int = _MAX_FRAMES_PER_VIDEO) -> list[float]:
    """1秒1フレーム（各秒の中央 0.5s 起点）。契約上限12枚を超える尺は等間隔に間引く。"""
    if duration_sec <= 1:
        return [0.5]
    seconds = min(duration_sec, max_frames)
    if duration_sec <= max_frames:
        return [i + 0.5 for i in range(seconds)]
    step = duration_sec / max_frames
    return [round(step * i + step / 2, 2) for i in range(max_frames)]


SAMPLING_NOTE = (
    "フレームは1秒1コマ（動画中央合わせ）で切り出し。"
    "12秒を超える動画は契約上限12コマへ等間隔に間引いて解析。"
)


def parse_vision_json(text: str, rules: ClusterRules) -> tuple[str, str]:
    """視覚AIの応答テキストから {cluster, telop_text} を決定論で取り出す。

    語彙外クラスタ・JSON崩れは VisionParseError（解析失敗として監査へ・推測で埋めない）。
    """
    match = _JSON_BLOCK_RE.search(text or "")
    if match is None:
        raise VisionParseError("VISION_JSON_MISSING")
    try:
        payload = json.loads(match.group(0))
    except ValueError as exc:
        raise VisionParseError("VISION_JSON_INVALID") from exc
    if not isinstance(payload, dict):
        raise VisionParseError("VISION_JSON_INVALID")
    cluster = str(payload.get("cluster", "")).strip()
    if cluster not in rules.vocabulary:
        raise VisionParseError("VISION_CLUSTER_OUT_OF_VOCABULARY")
    telop = payload.get("telop_text", "")
    if not isinstance(telop, str):
        raise VisionParseError("VISION_TELOP_INVALID")
    return cluster, telop.strip()


def _default_vision_caller(
    frames: Sequence[tuple[float, bytes]],
    rules: ClusterRules,
    request_id: str,
) -> VisionVerdict:
    """本番: Bedrock Converse へ画像ブロックで1コール（コストは usage 実測）。"""
    from teamagent.adapters.bedrock_client import BedrockClient

    client = BedrockClient.from_env(model_id_override=os.environ.get("OMIYAGE_VA_BEDROCK_MODEL_ID"))
    content: list[dict[str, object]] = [
        {"image": {"format": "jpeg", "source": {"bytes": jpeg}}} for _second, jpeg in frames
    ]
    vocabulary = " / ".join(rules.vocabulary)
    content.append(
        {
            "text": (
                "これはTikTok動画から1秒間隔で切り出したフレーム列です。"
                f"{rules.guidance}\n"
                f"クラスタは必ず次のいずれか1つ: {vocabulary}\n"
                "また、フレーム内に表示されているテロップ（画面上の文字）を"
                "読める範囲でそのまま書き出してください。読めない・存在しない場合は空文字。\n"
                'JSONのみで応答: {"cluster": "<語彙のいずれか>",'
                ' "telop_text": "<読み取ったテロップ全文>"}'
            )
        }
    )
    response = client.converse(
        [{"role": "user", "content": content}],
        request_id,
        temperature=0.0,
        max_tokens=1024,
    )
    cluster, telop = parse_vision_json(response.text, rules)
    return VisionVerdict(
        cluster=cluster,
        telop_text=telop,
        cost_usd=float(response.usage.cost_usd),
        model_id=response.model_id,
    )


def _safe_code(exc: BaseException) -> str:
    if isinstance(exc, VisionParseError):
        return str(exc)
    match = _SAFE_CODE_RE.search(str(exc))
    return match.group(0) if match else type(exc).__name__


def _chunked(items: Sequence[PostRecord], size: int) -> Iterable[Sequence[PostRecord]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


@dataclass
class OmiyageVideoAnalyzer:
    """便1の動画解析実行体（media client と視覚AIは注入可能）。"""

    request_id: str
    media_client_factory: Callable[[], object] | None = None
    vision_caller: VisionCaller = _default_vision_caller
    rules: ClusterRules = DEFAULT_CLUSTER_RULES
    # 既定25 = ブランド10 + 競合10 + TOP5サムネ用5（skill._analysis_targets と対）
    max_videos: int = field(
        default_factory=lambda: _envint("OMIYAGE_VA_MAX_VIDEOS", 25, minimum=1, maximum=60)
    )
    concurrency: int = field(
        default_factory=lambda: _envint("OMIYAGE_VA_CONCURRENCY", 2, minimum=1, maximum=4)
    )
    cost_cap_usd: float = field(
        default_factory=lambda: _envfloat(
            "OMIYAGE_VA_COST_CAP_USD", 1.0, minimum=0.01, maximum=20.0
        )
    )
    media_cost_usd_per_video: float = field(
        default_factory=lambda: _envfloat(
            "OMIYAGE_VA_MEDIA_COST_USD_PER_VIDEO", 0.005, minimum=0.0, maximum=1.0
        )
    )

    def _media_client(self) -> object:
        if self.media_client_factory is not None:
            return self.media_client_factory()
        from teamagent.adapters.media_job import MediaJobClient

        return MediaJobClient()

    def _analyze_one(
        self, post: PostRecord
    ) -> tuple[VideoAnalysisSuccess | None, VideoAnalysisFailure | None]:
        client = self._media_client()
        try:
            data, mime = client.acquire_video(  # type: ignore[attr-defined]
                post.url,
                request_fingerprint=f"{self.request_id}:omiyage-va-acquire:{post.video_id}",
            )
        except Exception as exc:
            return None, VideoAnalysisFailure(
                video_id=post.video_id, url=post.url, stage="acquire", code=_safe_code(exc)
            )
        timecodes = plan_timecodes(post.duration_sec)
        try:
            frames = client.extract_frames(  # type: ignore[attr-defined]
                data,
                mime,
                timecodes,
                request_fingerprint=f"{self.request_id}:omiyage-va-frames:{post.video_id}",
                width=480,
            )
        except Exception as exc:
            return None, VideoAnalysisFailure(
                video_id=post.video_id, url=post.url, stage="frames", code=_safe_code(exc)
            )
        if not frames:
            return None, VideoAnalysisFailure(
                video_id=post.video_id, url=post.url, stage="frames", code="NO_FRAMES"
            )
        try:
            verdict = self.vision_caller(frames, self.rules, self.request_id)
        except VisionParseError as exc:
            return None, VideoAnalysisFailure(
                video_id=post.video_id, url=post.url, stage="parse", code=str(exc)
            )
        except Exception as exc:
            return None, VideoAnalysisFailure(
                video_id=post.video_id, url=post.url, stage="vision", code=_safe_code(exc)
            )
        return (
            VideoAnalysisSuccess(
                video_id=post.video_id,
                url=post.url,
                cluster=verdict.cluster,
                telop_text=verdict.telop_text,
                frames_used=len(frames),
                cost_usd=verdict.cost_usd + self.media_cost_usd_per_video,
                model_id=verdict.model_id,
                thumb_bytes=frames[0][1],
            ),
            None,
        )

    def analyze(self, posts: Sequence[PostRecord]) -> VideoAnalysisReport:
        """対象投稿列を解析する。コスト上限到達で残りを skipped として打ち切る。"""
        targets = list(posts[: self.max_videos])
        over_limit = [post.video_id for post in posts[self.max_videos :]]
        results: list[VideoAnalysisSuccess] = []
        failures: list[VideoAnalysisFailure] = []
        skipped: list[str] = list(over_limit)
        skip_reason = "max_videos" if over_limit else ""
        cost_lock = threading.Lock()
        cost_total = 0.0

        remaining = list(targets)
        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            for chunk in _chunked(remaining, self.concurrency):
                with cost_lock:
                    if cost_total >= self.cost_cap_usd:
                        skipped.extend(post.video_id for post in chunk)
                        skip_reason = "cost_cap"
                        continue
                for success, failure in executor.map(self._analyze_one, chunk):
                    if success is not None:
                        results.append(success)
                        with cost_lock:
                            cost_total += success.cost_usd
                    elif failure is not None:
                        failures.append(failure)
                        with cost_lock:
                            cost_total += self.media_cost_usd_per_video

        model_id = next((result.model_id for result in results if result.model_id), "")
        return VideoAnalysisReport(
            results=tuple(results),
            failures=tuple(failures),
            skipped_video_ids=tuple(skipped),
            skip_reason=skip_reason,
            requested=len(posts),
            cost_cap_usd=self.cost_cap_usd,
            cost_usd_estimate=cost_total,
            model_id=model_id,
            sampling_note=SAMPLING_NOTE,
            vocabulary=self.rules.vocabulary,
        )


__all__ = [
    "DEFAULT_CLUSTER_RULES",
    "SAMPLING_NOTE",
    "ClusterRules",
    "OmiyageVideoAnalyzer",
    "VideoAnalysisFailure",
    "VideoAnalysisReport",
    "VideoAnalysisSuccess",
    "VisionCaller",
    "VisionParseError",
    "VisionVerdict",
    "parse_vision_json",
    "plan_timecodes",
]
