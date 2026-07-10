"""AnalysisCache＋video skill 配線（v0.3 Task 10・FinOps）のテスト（外部I/O無し）。

検証主眼: URL正規化（YouTube表記ゆれ同一視/非YouTubeはNone）・キーに prompt_version/
model/focus が効く・get/put fail-open・skill 配線（ヒット時 Gemini 不呼び出し＋cost 0・
ミス時 put・既定OFFで完全素通し）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from teamagent.adapters.analysis_cache import (
    AnalysisCache,
    content_basis,
    normalize_video_url,
)
from teamagent.skills.base import SkillContext
from teamagent.skills.video.schema import VideoAnalysisInput
from teamagent.skills.video.skill import VideoAnalysisSkill

# ── 正規化・キー ────────────────────────────────────────────────────────────


def test_normalize_youtube_variants_same_basis() -> None:
    a = normalize_video_url("https://www.youtube.com/watch?v=abc123XYZ_-&t=10s")
    b = normalize_video_url("https://youtu.be/abc123XYZ_-")
    c = normalize_video_url("https://www.youtube.com/shorts/abc123XYZ_-")
    assert a == b == c == "yt:abc123XYZ_-"


def test_normalize_non_youtube_is_none() -> None:
    assert normalize_video_url("https://www.tiktok.com/@x/video/123") is None


def test_cache_key_varies_by_prompt_model_focus() -> None:
    base = {"basis": "yt:abc", "prompt_version": "v1", "model_id": "gemini-2.5-flash"}
    k0 = AnalysisCache.cache_key(**base)
    assert AnalysisCache.cache_key(**base) == k0  # 決定的
    assert AnalysisCache.cache_key(**{**base, "prompt_version": "v2"}) != k0
    assert AnalysisCache.cache_key(**{**base, "model_id": "gemini-3"}) != k0
    assert AnalysisCache.cache_key(**base, focus="フック") != k0
    assert len(k0) == 64  # 生 URL/focus が S3 キーに露出しない（sha256 のみ）


def test_content_basis_is_stable() -> None:
    assert content_basis(b"same") == content_basis(b"same")
    assert content_basis(b"a") != content_basis(b"b")


# ── S3 フェイクでの get/put ────────────────────────────────────────────────


class _FakeS3:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def get_object(self, Bucket: str, Key: str) -> Any:  # noqa: N803 - boto3 命名
        if Key not in self.store:
            raise type("NoSuchKey", (Exception,), {})()
        body = self.store[Key]
        return {"Body": type("B", (), {"read": lambda self2: body})()}

    def put_object(self, Bucket: str, Key: str, Body: bytes, **kw: Any) -> None:  # noqa: N803
        self.store[Key] = Body


def test_get_put_roundtrip_and_miss() -> None:
    s3 = _FakeS3()
    cache = AnalysisCache(bucket="b", prefix="analysis-cache/", client=s3)
    key = AnalysisCache.cache_key(basis="yt:x", prompt_version="v1", model_id="m")
    assert cache.get(key, request_id="r") is None  # miss
    cache.put(key, text="分析結果", model_id="m", cost_usd=0.12, request_id="r")
    hit = cache.get(key, request_id="r")
    assert hit is not None and hit.text == "分析結果" and hit.model_id == "m"
    assert hit.original_cost_usd == 0.12


def test_get_failure_fail_open() -> None:
    class _Boom:
        def get_object(self, **kw: Any) -> Any:
            raise RuntimeError("s3 down")

    cache = AnalysisCache(bucket="b", client=_Boom())
    assert cache.get("k", request_id="r") is None  # 障害は miss 扱い（分析本体へ進む）


# ── video skill 配線 ────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, text: str = "新規分析", cost: float = 0.5) -> None:
        self.text = text
        self.cost_usd = cost
        self.model_id = "gemini-2.5-flash"


class _FakeGemini:
    def __init__(self) -> None:
        self.url_calls = 0

    def analyze_video_url(self, **kw: Any) -> _Resp:
        self.url_calls += 1
        return _Resp()


@pytest.fixture
def _cache_on(monkeypatch: pytest.MonkeyPatch) -> _FakeS3:
    monkeypatch.setenv("ANALYSIS_CACHE_ENABLED", "1")
    s3 = _FakeS3()
    import teamagent.adapters.analysis_cache as ac

    monkeypatch.setattr(ac.AnalysisCache, "_ensure_client", lambda self: s3)
    return s3


def _run(skill: VideoAnalysisSkill, url: str) -> Any:
    return skill.run(
        VideoAnalysisInput(url=url),
        SkillContext(request_id="r", metadata={"user_email": "a@b.co"}),
    )


def test_skill_miss_then_hit_skips_gemini(_cache_on: _FakeS3) -> None:
    gemini = _FakeGemini()
    skill = VideoAnalysisSkill(gemini=gemini)  # type: ignore[arg-type]
    url = "https://www.youtube.com/watch?v=abc123XYZ_-"
    out1 = _run(skill, url)
    assert out1.total_cost_usd == 0.5 and gemini.url_calls == 1
    # 2回目（表記ゆれ URL）: キャッシュヒット＝Gemini 不呼び出し・cost 0。
    out2 = _run(skill, "https://youtu.be/abc123XYZ_-")
    assert gemini.url_calls == 1  # 呼ばれていない
    assert out2.total_cost_usd == 0.0 and out2.analysis == "新規分析"
    # S3 に保存されたのは出力テキストのみ（動画 bytes は無い）。
    stored = json.loads(next(iter(_cache_on.store.values())).decode())
    assert set(stored.keys()) == {"text", "model_id", "cost_usd"}


def test_skill_cache_disabled_is_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANALYSIS_CACHE_ENABLED", raising=False)
    gemini = _FakeGemini()
    skill = VideoAnalysisSkill(gemini=gemini)  # type: ignore[arg-type]
    url = "https://www.youtube.com/watch?v=abc123XYZ_-"
    _run(skill, url)
    _run(skill, url)
    assert gemini.url_calls == 2  # 既定OFF＝毎回分析（従来挙動そのまま）
