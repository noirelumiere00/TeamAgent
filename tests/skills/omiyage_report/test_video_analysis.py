"""動画解析パイプライン（DL→フレーム→視覚AI）の決定論境界・コスト上限・失敗監査。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from teamagent.skills.omiyage_report.metrics import PostRecord
from teamagent.skills.omiyage_report.video_analysis import (
    DEFAULT_CLUSTER_RULES,
    ClusterRules,
    OmiyageVideoAnalyzer,
    VisionParseError,
    VisionVerdict,
    parse_vision_json,
    plan_timecodes,
)


def _post(video_id: str, *, duration: int = 10) -> PostRecord:
    return PostRecord(
        video_id=video_id,
        url=f"https://www.tiktok.com/@a/video/{video_id}",
        author="a",
        caption="",
        hashtags=(),
        rank=1,
        duration_sec=duration,
    )


@dataclass
class _FakeMediaClient:
    fail_acquire_ids: set[str] = field(default_factory=set)
    fail_frames_ids: set[str] = field(default_factory=set)
    acquire_calls: list[str] = field(default_factory=list)

    def acquire_video(
        self, url: str, *, request_fingerprint: str, **kwargs: object
    ) -> tuple[bytes, str]:
        video_id = url.rsplit("/", 1)[-1]
        self.acquire_calls.append(video_id)
        if video_id in self.fail_acquire_ids:
            raise RuntimeError("MEDIA_TIKTOK_BOT_WALL: blocked")
        return b"\x00\x00\x00\x18ftypmp42" + video_id.encode(), "video/mp4"

    def extract_frames(
        self,
        data: bytes,
        mime: str,
        timecodes: list[float],
        *,
        request_fingerprint: str,
        width: int = 480,
        **kwargs: object,
    ) -> list[tuple[float, bytes]]:
        if any(vid in request_fingerprint for vid in self.fail_frames_ids):
            raise RuntimeError("MEDIA_FRAME_FAILED")
        return [(second, b"jpeg-bytes") for second in timecodes]


@dataclass
class _FakeVision:
    cluster: str = "正直レビュー/検証系"
    telop: str = "テロップ文字"
    cost: float = 0.01
    parse_fail: bool = False
    calls: int = 0

    def __call__(
        self,
        frames: Sequence[tuple[float, bytes]],
        rules: ClusterRules,
        request_id: str,
    ) -> VisionVerdict:
        self.calls += 1
        if self.parse_fail:
            raise VisionParseError("VISION_CLUSTER_OUT_OF_VOCABULARY")
        return VisionVerdict(
            cluster=self.cluster,
            telop_text=self.telop,
            cost_usd=self.cost,
            model_id="test-model",
        )


def _analyzer(
    client: _FakeMediaClient,
    vision: _FakeVision,
    *,
    max_videos: int = 20,
    concurrency: int = 2,
    cost_cap: float = 1.0,
    media_cost: float = 0.005,
) -> OmiyageVideoAnalyzer:
    return OmiyageVideoAnalyzer(
        request_id="va-test",
        media_client_factory=lambda: client,
        vision_caller=vision,
        max_videos=max_videos,
        concurrency=concurrency,
        cost_cap_usd=cost_cap,
        media_cost_usd_per_video=media_cost,
    )


def test_plan_timecodes_one_frame_per_second_up_to_contract_limit() -> None:
    assert plan_timecodes(0) == [0.5]
    assert plan_timecodes(3) == [0.5, 1.5, 2.5]
    assert plan_timecodes(12) == [i + 0.5 for i in range(12)]
    long = plan_timecodes(60)
    assert len(long) == 12  # extract_frames 契約の max 12 へ等間隔間引き
    assert long == sorted(long)
    assert long[0] == pytest.approx(2.5)
    assert long[-1] < 60


def test_parse_vision_json_accepts_fenced_json_and_rejects_out_of_vocabulary() -> None:
    cluster, telop = parse_vision_json(
        '結果は次です。```json\n{"cluster": "成分オタク系", "telop_text": "成分表"}\n```',
        DEFAULT_CLUSTER_RULES,
    )
    assert cluster == "成分オタク系"
    assert telop == "成分表"
    with pytest.raises(VisionParseError):
        parse_vision_json('{"cluster": "存在しない界隈", "telop_text": ""}', DEFAULT_CLUSTER_RULES)
    with pytest.raises(VisionParseError):
        parse_vision_json("json じゃない応答", DEFAULT_CLUSTER_RULES)


def test_analyze_success_aggregates_telops_assignments_and_cost() -> None:
    client = _FakeMediaClient()
    vision = _FakeVision()
    report = _analyzer(client, vision).analyze([_post("1"), _post("2"), _post("3")])
    assert report.analyzed == 3
    assert report.requested == 3
    assert report.failures == ()
    assert report.skipped_video_ids == ()
    assert set(report.assignments) == {"1", "2", "3"}
    assert report.telops["1"] == "テロップ文字"
    assert report.model_id == "test-model"
    # コスト = (vision 0.01 + media 0.005) × 3
    assert report.cost_usd_estimate == pytest.approx(0.045)
    assert vision.calls == 3


def test_analyze_records_failures_per_stage_without_polluting_results() -> None:
    client = _FakeMediaClient(fail_acquire_ids={"2"}, fail_frames_ids={"3"})
    vision = _FakeVision()
    report = _analyzer(client, vision, concurrency=1).analyze([_post("1"), _post("2"), _post("3")])
    assert report.analyzed == 1
    stages = {failure.video_id: failure.stage for failure in report.failures}
    assert stages == {"2": "acquire", "3": "frames"}
    codes = {failure.video_id: failure.code for failure in report.failures}
    assert codes["2"] == "MEDIA_TIKTOK_BOT_WALL"
    # 失敗した動画はクラスタ・テロップに混ざらない
    assert set(report.assignments) == {"1"}
    audit = report.to_audit()
    assert audit["analyzed"] == 1
    assert len(audit["failures"]) == 2


def test_vision_parse_failure_is_recorded_as_parse_stage() -> None:
    client = _FakeMediaClient()
    vision = _FakeVision(parse_fail=True)
    report = _analyzer(client, vision, concurrency=1).analyze([_post("1")])
    assert report.analyzed == 0
    assert report.failures[0].stage == "parse"
    assert report.failures[0].code == "VISION_CLUSTER_OUT_OF_VOCABULARY"


def test_cost_cap_stops_remaining_chunks_and_discloses_skip() -> None:
    client = _FakeMediaClient()
    vision = _FakeVision(cost=0.30)
    report = _analyzer(client, vision, concurrency=2, cost_cap=0.5, media_cost=0.0).analyze(
        [_post(str(i)) for i in range(1, 7)]
    )
    # chunk1(2本)=0.6 → cap 0.5 到達 → 以降のチャンクは実行しない
    assert report.analyzed == 2
    assert report.skip_reason == "cost_cap"
    assert set(report.skipped_video_ids) == {"3", "4", "5", "6"}
    assert client.acquire_calls == ["1", "2"]
    assert report.cost_usd_estimate == pytest.approx(0.6)
    assert report.cost_cap_usd == 0.5


def test_max_videos_limit_skips_the_tail() -> None:
    client = _FakeMediaClient()
    vision = _FakeVision()
    report = _analyzer(client, vision, max_videos=2).analyze(
        [_post("1"), _post("2"), _post("3"), _post("4")]
    )
    assert report.analyzed == 2
    assert report.skip_reason == "max_videos"
    assert set(report.skipped_video_ids) == {"3", "4"}
    assert report.requested == 4
