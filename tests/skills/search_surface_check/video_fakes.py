"""2 段目（上位の動画の中身）のテスト用の偽物。

本番の失敗モードを再現する:
- Gemini は本番どおり ``### 所見`` の後にコードフェンスの JSON を返す（parse_analysis が読む形）。
  順位はプロンプトの「この動画の表示順位: N位」から読む＝どの動画を渡されたかを記録できる。
- 取得（downloader）は URL ごとに失敗させられる（本番の MEDIA_ACQUIRE 失敗）。Event で止められる
  （再デプロイ中に走っているジョブの再現）。
- Bedrock は 1 段目（分類・結論）を fixtures.FakeBedrock に任せ、2 段目の読みだけ差し替える。
  構成メモは本番どおり maxTokens で出力が途中で切れる（日本語 1 字 ≈ 1.5 トークンで見積もる）。
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any

from teamagent.adapters.gemini_client import GeminiResponse
from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill
from tests.skills.search_surface_check.fixtures import FakeBedrock, _Resp

# fixtures の上位 5 本（スパイスカレー 作り方）に合わせた、1 本ずつの分析（Gemini の JSON）。
ANALYSES: dict[int, dict[str, Any]] = {
    1: {
        "duration_sec": 58,
        "hook_type": "number",
        "hook_summary": "4つでいい本格スパイスカレー",
        "hook_has_caption": True,
        "telop_density": "medium",
        "telops": [{"sec": 0.5, "text": "4つでいい", "position": "center", "kw_match": True}],
        "cut_count": 12,
        "pacing": "fast",
        "main_message": "スパイス4つで本格カレー",
        "cta_type": ["save"],
        "has_narration": True,
        "is_trending_sound": "no",
        "spoken_keywords": [{"keyword": "スパイスカレー", "matched": True, "layer": "narration"}],
        "message_coherence": 85,
        "win_factors": ["数字で手軽さを示す"],
        "save_share_motivation": "材料4つのメモとして見返す",
    },
    2: {
        "duration_sec": 45,
        "hook_type": "number",
        "hook_summary": "まずはこの4つだけ",
        "telops": [{"sec": 1.0, "text": "スパイスカレー", "position": "top", "kw_match": True}],
        "cut_count": 15,
        "pacing": "fast",
        "main_message": "クミンなど4種の役割",
        "cta_type": ["save", "follow"],
        "has_narration": True,
        "is_trending_sound": "no",
        "spoken_keywords": [{"keyword": "スパイスカレー", "matched": True, "layer": "narration"}],
        "message_coherence": 80,
        "win_factors": ["配合の比率を明示"],
        "save_share_motivation": "配合の比率を保存",
    },
    3: {
        "duration_sec": 62,
        "hook_type": "visual",
        "hook_summary": "鍋から立つ湯気",
        "telops": [],
        "cut_count": 20,
        "pacing": "very_fast",
        "main_message": "家族の食卓",
        "cta_type": [],
        "has_narration": False,
        "is_trending_sound": "yes",
        "message_coherence": 60,
        "win_factors": ["シズル感"],
        "save_share_motivation": "",
    },
    4: {
        "duration_sec": 75,
        "hook_type": "problem",
        "hook_summary": "カレールーは卒業",
        "telops": [{"sec": 2.0, "text": "30分で作れる", "position": "center", "kw_match": False}],
        "cut_count": 8,
        "pacing": "moderate",
        "main_message": "ルーなしで作れる",
        "cta_type": ["follow"],
        "has_narration": True,
        "is_trending_sound": "no",
        "message_coherence": 70,
        "win_factors": ["悩みの言語化"],
        "save_share_motivation": "作り方の手順を見返す",
    },
    5: {
        "duration_sec": 90,
        "hook_type": "question",
        "hook_summary": "料理人が辿り着いた答えは？",
        "telops": [{"sec": 5.0, "text": "スパイスカレー", "position": "bottom", "kw_match": True}],
        "cut_count": 10,
        "pacing": "moderate",
        "main_message": "プロの手順",
        "cta_type": ["save"],
        "has_narration": True,
        "is_trending_sound": "unknown",
        "message_coherence": 75,
        "win_factors": ["プロの権威"],
        "save_share_motivation": "プロの手順を保存",
    },
}
DEFAULT_ANALYSIS: dict[str, Any] = {
    "duration_sec": 40,
    "hook_type": "other",
    "cut_count": 9,
    "pacing": "moderate",
    "message_coherence": 50,
}

_RANK_RE = re.compile(r"この動画の表示順位: (\d+)位")


def analysis_text(analysis: dict[str, Any]) -> str:
    return (
        "### 所見\n動画を見ました。\n\n```json\n"
        + json.dumps(analysis, ensure_ascii=False)
        + "\n```"
    )


class FakeGemini:
    """GeminiClient.analyze_video_bytes の代役（順位ごとの分析を返し、呼ばれ方を記録する）。"""

    def __init__(
        self,
        analyses: dict[int, dict[str, Any]] | None = None,
        *,
        fail_ranks: frozenset[int] = frozenset(),
        cost: float = 0.01,
    ) -> None:
        self.analyses = ANALYSES if analyses is None else analyses
        self.fail_ranks = fail_ranks
        self.cost = cost
        self.calls: list[tuple[int, str]] = []
        self._lock = threading.Lock()

    def analyze_video_bytes(
        self, *, data: bytes, mime_type: str, prompt: str, request_id: str, system: str
    ) -> GeminiResponse:
        m = _RANK_RE.search(prompt)
        assert m, "prompt should carry the rank"
        assert system, "system prompt must be the video_algorithm system prompt"
        rank = int(m.group(1))
        with self._lock:
            self.calls.append((rank, mime_type))
        if rank in self.fail_ranks:
            raise RuntimeError("gemini 500")
        analysis = self.analyses.get(rank, DEFAULT_ANALYSIS)
        return GeminiResponse(
            text=analysis_text(analysis),
            input_tokens=6000,
            output_tokens=400,
            cost_usd=self.cost,
            model_id="gemini-3.5-flash",
            latency_ms=1000,
        )

    @property
    def ranks(self) -> list[int]:
        return sorted(r for r, _ in self.calls)


class FakeDownloader:
    """動画の取得（media job の acquire）の代役。URL ごとに失敗・Event で待機できる。"""

    def __init__(
        self, *, fail_urls: set[str] | None = None, gate: threading.Event | None = None
    ) -> None:
        self.fail_urls = fail_urls or set()
        self.gate = gate
        self.started = threading.Event()
        self.urls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, url: str) -> tuple[bytes, str]:
        with self._lock:
            self.urls.append(url)
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(10), "test forgot to release the downloader"
        if url in self.fail_urls:
            raise RuntimeError("MEDIA_ACQUIRE_FAILED: blocked")
        return b"\x00\x00\x00\x18ftypmp42" + url.encode(), "video/mp4"


def make_engine(gemini: FakeGemini, downloader: FakeDownloader) -> VideoAlgorithmSkill:
    return VideoAlgorithmSkill(
        gemini=gemini,  # type: ignore[arg-type]
        downloader=downloader,
        proxy=lambda d, m: (d, m),
        max_workers=3,
    )


# 2 段目の読み（集計と一覧にある数字だけ）。
GROUNDED_DIGEST: dict[str, Any] = {
    "headline": "数字フックが2本、冒頭テロップは3/5本の上位",
    "winning": {
        "text": "冒頭3秒に数字のテロップを置き、保存を促す型が1・2位に共通",
        "ranks": [1, 2],
    },
    "save_reason": {"text": "配合の比率を保存する動機。2位は保存率6.8%", "ranks": [2, 99]},
}


# 2 段目の構成メモ（学べること・弱点・絵コンテ案）。数字はその動画の構成表にあるものだけ。
GROUNDED_NOTES: dict[str, Any] = {
    "videos": [
        {
            "rank": 1,
            "learn": ["0.5秒で「4つでいい」とテロップを出し、材料の少なさで止める"],
            "weak": ["CTAの秒が分からず、最後の呼びかけが弱い"],
        },
        {"rank": 2, "learn": ["1秒でKWのテロップを出す"], "weak": []},
    ],
    "storyboard": [
        {"show": "完成した料理の寄り", "telop": "4つでいい"},
        {"show": "材料を並べる", "telop": "材料はこれだけ"},
        {"show": "手順を早送りで見せる", "telop": "30分で作れる"},
        {"show": "保存を呼びかける", "telop": "保存して見返してね"},
    ],
}


# 偽の Bedrock が出力の長さを見積もる、日本語 1 字あたりのトークン数。
FAKE_TOKENS_PER_CHAR = 1.5


class VideoBedrock(FakeBedrock):
    """1 段目は FakeBedrock のまま、2 段目の読み（動画の中身の読み）と構成メモだけ差し替える。"""

    def __init__(
        self,
        *,
        digest: dict[str, Any] | str | None = None,
        digest_error: Exception | None = None,
        notes: dict[str, Any] | str | None = None,
        notes_error: Exception | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.digest = GROUNDED_DIGEST if digest is None else digest
        self.digest_error = digest_error
        self.digest_prompts: list[str] = []
        self.notes = GROUNDED_NOTES if notes is None else notes
        self.notes_error = notes_error
        self.notes_prompts: list[str] = []
        self.notes_max_tokens: list[int] = []

    def converse(self, messages: list[dict[str, Any]], **kw: Any) -> _Resp:
        text = messages[0]["content"][0]["text"]
        if "上位動画の構成メモ" in text:
            self.notes_prompts.append(text)
            max_tokens = int(kw.get("max_tokens", 4096))
            self.notes_max_tokens.append(max_tokens)
            if self.notes_error is not None:
                raise self.notes_error
            body = (
                self.notes
                if isinstance(self.notes, str)
                else json.dumps(self.notes, ensure_ascii=False)
            )
            # 本番の Bedrock は maxTokens に達すると途中で切った文を返す（stop_reason=max_tokens）。
            return _Resp(body[: int(max_tokens / FAKE_TOKENS_PER_CHAR)], 0.004)
        if "上位の動画の中身の読み" in text:
            self.digest_prompts.append(text)
            if self.digest_error is not None:
                raise self.digest_error
            body = (
                self.digest
                if isinstance(self.digest, str)
                else json.dumps(self.digest, ensure_ascii=False)
            )
            return _Resp(body, 0.003)
        return super().converse(messages, **kw)
