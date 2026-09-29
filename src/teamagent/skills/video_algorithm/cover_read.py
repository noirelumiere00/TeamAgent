"""サムネ（一覧の表紙）の読み取り: 取得 → Gemini（画像 1 枚・1 回）→ parse。LLM の数え上げは無い。

検索一覧でタップされるかを左右する表紙の要素（写っているもの・文字と位置と大きさ・顔・寄り・質感・
商品・背景・読みやすさ）を、表紙の画像だけから Gemini に JSON で書かせる。数・段階・差・指示の
土台は cover_facts.py（コード）が決める。

守ること:
- Gemini に渡すのは画像だけ（検索 KW・キャプション・テロップ・順位は渡さない）。画像に無い文字を
  「読んだ」ことにさせないため。user プロンプトは COVER_USER_PROMPT に固定する。
- 1 枚の失敗（取得の 403・JSON の崩れ・例外・締め切り）は、その 1 枚の status にして全体は続ける。
- 取得は 1 本 1 回（幅 540）。表示用の画像と色（ThumbColor）も同じ取得を使い回す
  （skill._build_thumb が ``image_for`` で待つ）。
- 並列は取得（ECS の起動を待つだけ）と Gemini の呼び出しで別のプールにする。締め切りを過ぎた分は
  ``timeout`` にして、プールは待たずに片付ける（``with ThreadPoolExecutor`` は
  終わるまで待ってしまう）。
- 費用は、呼び出しが返った分を足す（MagicMock の費用は数えない＝float のときだけ）。締め切りの
  後に返った分は合計に入らないので、ログ（abandoned_cost_usd）で数える。

env（0/false/off/no は OFF）:
- VIDEO_ALGO_COVER_READ（既定 1）: 上位 n 本の表紙を読む
- VIDEO_ALGO_COVER_BOARD（既定 0）: 6〜30 位の表紙も読む（ECS が最大 +25 本・所要は未計測）
- VIDEO_ALGO_COVER_WORKERS（既定 2・1〜8）: Gemini の並列（動画分析と 429 の枠を取り合うので小さく）
- VIDEO_ALGO_COVER_FETCH_WORKERS（既定 4・1〜16）: 取得の並列
  （dispatcher と Fargate の同時数を守る）
- VIDEO_ALGO_COVER_WAIT_S（既定 45）: 動画分析の後に待つ上限の秒
- VIDEO_ALGO_COVER_BUDGET_S（既定 240）: run の開始からの経過の上限（OC の 6 分の打ち切りの内側）
- VIDEO_ALGO_COVER_TIMEOUT_S（既定 30）: Gemini 1 回の HTTP の上限
- VIDEO_ALGO_COVER_FETCH_TIMEOUT_S（既定 90）: 取得 1 回の上限
- VIDEO_ALGO_COVER_THINKING（既定 low・空で付けない）: Gemini の thinking の段階
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Event, Lock
from typing import Any, Literal

import structlog
from pydantic import ValidationError

from teamagent.skills.video_algorithm.schema import (
    COVER_CODE_ONLY,
    COVER_REQUIRED_KEYS,
    CoverRead,
    VideoMeta,
)

logger = structlog.get_logger(__name__)

COVER_PROMPT_SKILL = "video_algorithm_cover"
COVER_PROMPT_VERSION = "v1"
# 読み取りに渡す画像の幅（スマホの 2 列の一覧のタイルは実画素で 500〜590px 程度）。
COVER_WIDTH = 540
# parse の規則の版（規則を変えたらここを上げる＝結果キャッシュのキーが変わる）。
COVER_PARSE_VERSION = "p1"
COVER_USER_PROMPT = (
    "この画像は TikTok の動画の表紙（検索結果の一覧に出る画像）1 枚です。"
    "システム指示の JSON だけを出力してください。"
)
_OFF = frozenset({"0", "false", "off", "no"})
_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in _OFF


def _env_num(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return min(hi, max(lo, value))


@dataclass(frozen=True)
class CoverSettings:
    """表紙の読み取りの設定（env から読む・テストは直接作る）。"""

    enabled: bool = True
    board: bool = False
    workers: int = 2
    fetch_workers: int = 4
    wait_s: float = 45.0
    budget_s: float = 240.0
    timeout_s: float = 30.0
    fetch_timeout_s: float = 90.0
    thinking: str | None = "low"
    media_resolution: str = "high"

    @classmethod
    def from_env(cls) -> CoverSettings:
        thinking = os.environ.get("VIDEO_ALGO_COVER_THINKING")
        return cls(
            enabled=_env_flag("VIDEO_ALGO_COVER_READ", True),
            board=_env_flag("VIDEO_ALGO_COVER_BOARD", False),
            workers=int(_env_num("VIDEO_ALGO_COVER_WORKERS", 2, 1, 8)),
            fetch_workers=int(_env_num("VIDEO_ALGO_COVER_FETCH_WORKERS", 4, 1, 16)),
            wait_s=_env_num("VIDEO_ALGO_COVER_WAIT_S", 45, 0, 120),
            budget_s=_env_num("VIDEO_ALGO_COVER_BUDGET_S", 240, 30, 900),
            timeout_s=_env_num("VIDEO_ALGO_COVER_TIMEOUT_S", 30, 5, 120),
            fetch_timeout_s=_env_num("VIDEO_ALGO_COVER_FETCH_TIMEOUT_S", 90, 10, 300),
            thinking=("low" if thinking is None else thinking.strip().lower() or None),
        )

    @property
    def mode(self) -> Literal["off", "top", "board"]:
        """出力の cover_read_mode（off／top／board）。"""
        if not self.enabled:
            return "off"
        return "board" if self.board else "top"


def cover_version(settings: CoverSettings, model_id: str) -> str:
    """結果キャッシュのキーに入れる表紙の読み取りの版（止めているときは空＝従来のキー）。

    手書きの版名ではなく、プロンプトの sha256・user プロンプト・モデル・thinking・解像度・
    入力の幅・parse の規則の版から作る（プロンプトをその場で直したとき古い結果を返さない）。
    """
    if not settings.enabled:
        return ""
    from teamagent.prompts.loader import load_prompt

    system = load_prompt(COVER_PROMPT_SKILL, COVER_PROMPT_VERSION, "system")
    raw = json.dumps(
        {
            "prompt": hashlib.sha256(system.encode("utf-8")).hexdigest(),
            "user": hashlib.sha256(COVER_USER_PROMPT.encode("utf-8")).hexdigest(),
            "model": model_id,
            "thinking": settings.thinking or "",
            "media_resolution": settings.media_resolution,
            "width": COVER_WIDTH,
            "parse": COVER_PARSE_VERSION,
        },
        sort_keys=True,
    )
    tag = f"{COVER_PROMPT_VERSION}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"
    return f"{tag}+board" if settings.board else tag


# ── parse ───────────────────────────────────────────────────────────────


def _first_json_object(text: str) -> Any:
    """``` のフェンスや前後の文があっても、最初の { から始まる JSON を 1 つ読む。"""
    start = text.find("{")
    if start < 0:
        return None
    try:
        value, _end = json.JSONDecoder().raw_decode(text[start:])
    except (json.JSONDecodeError, ValueError):
        return None
    return value


def parse_cover(text: Any) -> CoverRead | None:
    """Gemini の出力を CoverRead に読む。読めなければ None（呼んだ側が read_failed にする）。

    - JSON が壊れている・途中で切れている・dict でない → None
    - 必須の欄（elements・texts・face）が 1 つでも無い → None（空の JSON を「全部無い」と数えない）
    - コードだけの欄（rank・status など）は捨てる
    - 知らない列挙の値は unknown・リストは知らない要素だけ捨てる（1 欄が壊れてもほかは読む）
    status は既定の read_failed のまま返す（呼んだ側が ok にする）。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    data = _first_json_object(text)
    if not isinstance(data, dict):
        return None
    if any(key not in data for key in COVER_REQUIRED_KEYS):
        return None
    for key in COVER_CODE_ONLY:
        data.pop(key, None)
    try:
        return CoverRead.model_validate(data)
    except ValidationError:
        return None


# ── 取得した画像 ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CoverImage:
    """取得した表紙の画像。color は media worker が同じ取得で計算した色（無ければ None）。"""

    data: bytes
    mime: str
    via: str
    color: dict[str, Any] | None = None
    width: int = 0
    height: int = 0


def sniff_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypheic", b"ftypheif", b"ftypmif1"):
        return "image/heic"
    return "image/jpeg"


def image_size(data: bytes) -> tuple[int, int]:
    """画像の (幅, 高さ)。JPEG の SOF か PNG の IHDR だけ読む（画像は開かない）。

    読めなければ (0, 0)。
    """
    from teamagent.skills.video_algorithm.facts import jpeg_size

    size = jpeg_size(data)
    if size is not None:
        return size
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24 and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0, 0


def image_of(data: bytes, *, via: str, color: dict[str, Any] | None = None) -> CoverImage:
    w, h = image_size(data)
    return CoverImage(data=data, mime=sniff_mime(data), via=via, color=color, width=w, height=h)


def _reason(exc: BaseException) -> str:
    text = str(exc).split(":", 1)[0].strip()
    return text if _CODE_RE.fullmatch(text) else type(exc).__name__


def _cost(resp: Any) -> float:
    value = getattr(resp, "cost_usd", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0  # MagicMock の __float__ は 1.0 を返すので float() で足さない
    return float(value)


# ── 読み取りの本体 ───────────────────────────────────────────────────────


@dataclass
class _Job:
    rank: int
    group: str
    fetched: Event = field(default_factory=Event)
    done: Event = field(default_factory=Event)
    image: CoverImage | None = None
    result: CoverRead | None = None


class CoverReader:
    """表紙を 1 枚ずつ取得して Gemini に読ませる（2 つのプール・締め切りつき）。

    使い方: ``submit`` で投入（順位で重複を除く）→ ``image_for`` で表示用の画像を待つ（任意）→
    ``join`` で締め切りまで待ち、順位ごとの CoverRead を受け取る（プールはここで片付ける）。
    """

    def __init__(
        self,
        *,
        gemini: Any,
        system: str,
        fetch: Callable[[VideoMeta], CoverImage],
        request_id: str,
        settings: CoverSettings,
        version: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gemini = gemini
        self._system = system
        self._fetch = fetch
        self._rid = request_id
        self._settings = settings
        self._version = version
        self._clock = clock
        self._started = clock()
        self._lock = Lock()
        self._jobs: dict[int, _Job] = {}
        self._cost = 0.0
        self._abandoned_cost = 0.0
        self._joined = False
        self._fetch_pool = ThreadPoolExecutor(
            max_workers=max(1, settings.fetch_workers), thread_name_prefix="cover-fetch"
        )
        self._read_pool = ThreadPoolExecutor(
            max_workers=max(1, settings.workers), thread_name_prefix="cover-read"
        )

    # --- 投入 ---
    def submit(self, metas: Iterable[VideoMeta], group: str, *, all_zero: bool = False) -> None:
        """順位ごとに 1 回だけ投入する。

        画像投稿（尺 0）は読まない（board の尺が全部 0 なら読む）。
        """
        for meta in metas:
            with self._lock:
                if self._joined or meta.rank in self._jobs:
                    continue
                job = _Job(rank=meta.rank, group=group)
                self._jobs[meta.rank] = job
            if meta.duration_sec <= 0 and not all_zero:
                self._finish(job, "skipped", "image_post")
                continue
            if not meta.cover_url:
                self._finish(job, "no_cover", "no_cover_url")
                continue
            try:
                self._fetch_pool.submit(self._run_fetch, job, meta)
            except RuntimeError:  # 片付けた後
                self._finish(job, "timeout", "shutdown")

    def ranks(self) -> set[int]:
        with self._lock:
            return set(self._jobs)

    # --- 表示用の画像（1 本 1 回の取得を使い回す）---
    def image_for(self, rank: int, timeout_s: float | None = None) -> CoverImage | None:
        """その順位の取得が終わるまで待ち、画像を返す（失敗・時間切れ・投入なしは None）。"""
        with self._lock:
            job = self._jobs.get(rank)
        if job is None:
            return None
        wait = self._settings.fetch_timeout_s + 5 if timeout_s is None else timeout_s
        job.fetched.wait(max(0.0, wait))
        return job.image

    # --- 1 枚の処理（例外は外に出さない）---
    def _finish(
        self, job: _Job, status: str, reason: str = "", read: CoverRead | None = None
    ) -> None:
        base = read if read is not None else CoverRead()
        img = job.image
        result = base.model_copy(
            update={
                "rank": job.rank,
                "group": job.group,
                "status": status,
                "reason": reason,
                "version": self._version,
                "via": img.via if img is not None else "",
                "img_w": img.width if img is not None else 0,
                "img_h": img.height if img is not None else 0,
            }
        )
        with self._lock:
            if job.done.is_set():
                return
            job.result = result
            job.fetched.set()
            job.done.set()

    def _run_fetch(self, job: _Job, meta: VideoMeta) -> None:
        started = time.perf_counter()
        try:
            image = self._fetch(meta)
            if not image.data:
                raise ValueError("EMPTY_COVER")
        except Exception as exc:  # 403（署名 URL の失効）・空の bytes・未設定も 1 枚だけ落とす
            logger.info(
                "video_algorithm_cover_fetch_failed",
                request_id=self._rid,
                rank=job.rank,
                error=_reason(exc),
            )
            self._finish(job, "fetch_failed", _reason(exc))
            return
        job.image = image
        job.fetched.set()
        logger.info(
            "video_algorithm_stage",
            stage="cover_fetch",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            rank=job.rank,
            outcome="ok",
            request_id=self._rid,
        )
        try:
            self._read_pool.submit(self._run_read, job)
        except RuntimeError:
            self._finish(job, "timeout", "shutdown")

    def _run_read(self, job: _Job) -> None:
        image = job.image
        assert image is not None
        started = time.perf_counter()
        outcome = "ok"
        try:
            for attempt in range(2):  # JSON が崩れたら 1 回だけやり直す（約 $0.01）
                try:
                    resp = self._gemini.analyze_image_bytes(
                        data=image.data,
                        mime_type=image.mime,
                        prompt=COVER_USER_PROMPT,
                        request_id=self._rid,
                        system=self._system,
                        timeout_s=self._settings.timeout_s,
                        json_mode=True,
                        thinking_level=self._settings.thinking,
                        media_resolution=self._settings.media_resolution,
                    )
                except Exception as exc:
                    outcome = "error"
                    self._finish(job, "read_failed", f"gemini:{_reason(exc)}")
                    return
                self._add_cost(_cost(resp))
                read = parse_cover(getattr(resp, "text", None))
                if read is not None:
                    self._finish(job, "ok", "", read)
                    return
                logger.info(
                    "video_algorithm_cover_unparsed",
                    request_id=self._rid,
                    rank=job.rank,
                    attempt=attempt + 1,
                )
            outcome = "error"
            self._finish(job, "read_failed", "json")
        finally:
            logger.info(
                "video_algorithm_stage",
                stage="cover_read",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                rank=job.rank,
                outcome=outcome,
                request_id=self._rid,
            )

    def _add_cost(self, value: float) -> None:
        with self._lock:
            if self._joined:
                self._abandoned_cost += value
            else:
                self._cost += value

    # --- 締め切り ---
    def remaining_budget_s(self, run_started: float) -> float:
        """run の開始からの経過で、待ってよい残りの秒（待ちの上限 wait_s で頭打ち）。"""
        left = self._settings.budget_s - (self._clock() - run_started)
        return max(0.0, min(self._settings.wait_s, left))

    def join(self, wait_s: float) -> tuple[dict[int, CoverRead], float]:
        """締め切り（今から wait_s 秒）まで待ち、順位ごとの結果と費用を返す。

        プールは待たずに片付ける。
        """
        deadline = self._clock() + max(0.0, wait_s)
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.done.wait(max(0.0, deadline - self._clock()))
        abandoned = 0
        for job in jobs:
            if not job.done.is_set():
                abandoned += 1
                self._finish(job, "timeout", "deadline")
        with self._lock:
            self._joined = True
            cost = round(self._cost, 6)
        self._fetch_pool.shutdown(wait=False, cancel_futures=True)
        self._read_pool.shutdown(wait=False, cancel_futures=True)
        results = {job.rank: job.result for job in jobs if job.result is not None}
        counts: dict[str, int] = {}
        for read in results.values():
            counts[read.status] = counts.get(read.status, 0) + 1
        logger.info(
            "video_algorithm_cover_summary",
            request_id=self._rid,
            statuses=counts,
            cost_usd=cost,
            abandoned=abandoned,
            elapsed_ms=int((self._clock() - self._started) * 1000),
            version=self._version,
        )
        return results, cost

    def abandoned_cost(self) -> float:
        with self._lock:
            return round(self._abandoned_cost, 6)


__all__ = [
    "COVER_PARSE_VERSION",
    "COVER_PROMPT_SKILL",
    "COVER_PROMPT_VERSION",
    "COVER_USER_PROMPT",
    "COVER_WIDTH",
    "CoverImage",
    "CoverReader",
    "CoverSettings",
    "cover_version",
    "image_of",
    "image_size",
    "parse_cover",
    "sniff_mime",
]
