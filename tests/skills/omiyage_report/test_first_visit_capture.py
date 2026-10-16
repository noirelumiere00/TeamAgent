"""本番の縮約・関連性除外・S3障害の境界で初訪保存を検証する。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.adapters.tiktok_scraper import TikTokScrapeError, TikTokSearchResult
from teamagent.skills.omiyage_report import skill as skill_module
from teamagent.skills.omiyage_report.input_check import RelevanceCheck
from teamagent.skills.omiyage_report.schema import OmiyageReportSubmitInput
from teamagent.skills.omiyage_report.skill import OmiyageReportSubmitSkill

from .test_integration import _ctx, _Slack, _Uploader, _video


class CaptureUploader:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def __call__(self, key: str, body: bytes, content_type: str) -> dict[str, Any]:
        self.objects[key] = body
        return {
            "bucket": "private-capture",
            "key": key,
            "version_id": "version-1",
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
            "content_type": content_type,
        }

    def snapshot(self) -> dict[str, Any]:
        key = max(k for k in self.objects if k.endswith("capture-manifest.json"))
        manifest = json.loads(self.objects[key])
        return json.loads(self.objects[manifest["capture"]["key"]])  # type: ignore[no-any-return]


def request() -> OmiyageReportSubmitInput:
    return OmiyageReportSubmitInput(
        brand="架空ブランド", competitors=["架空競合"], keywords=["ヘアケア"]
    )


def evidence(query: str) -> dict[str, Any]:
    return {
        "query": query,
        "status": "ok",
        "search_type": "keyword",
        "requested": 2,
        "acquired_on": "2026-10-14T13:45:00+00:00",
        "order_basis": "display",
        "sessions": 1,
        "stop_reason": "target_reached",
        "shortfalls": [],
        "unfiltered": True,
        "missingness_preserved": True,
        "media_job_id": "mj_" + "a" * 24,
        "artifacts": [
            {
                "bucket": "media",
                "key": "jobs/raw.json",
                "version_id": "v1",
                "sha256": "a" * 64,
                "size": 8,
                "content_type": "application/json",
            }
        ],
        "posts": [
            {
                "provider_id": "7100000000000000001",
                "url": "https://www.tiktok.com/@creator/video/7100000000000000001",
                "kw": query,
                "rank_display": 7,
                "plays": None,
                "likes": 0,
                "isAd": True,
                "mediaType": "photo",
                "imageCount": 3,
            },
            {
                "provider_id": "7100000000000000002",
                "url": "https://www.tiktok.com/@creator/video/7100000000000000002",
                "kw": query,
                "rank_display": 9,
                "plays": 0,
                "isAd": False,
            },
        ],
    }


def builder(plan: str, out_dir: str, request_id: str) -> tuple[str, str]:
    paths = [Path(out_dir) / "image.pptx", Path(out_dir) / "editable.pptx"]
    for path in paths:
        path.write_bytes(b"same-deck")
    return str(paths[0]), str(paths[1])


def run_report(
    monkeypatch: pytest.MonkeyPatch,
    flag: str | None,
    uploader: Any,
    *,
    emit_capture: bool = True,
) -> tuple[Any, _Uploader, list[dict[str, Any]]]:
    if flag is None:
        monkeypatch.delenv("FIRST_VISIT_CAPTURE", raising=False)
    else:
        monkeypatch.setenv("FIRST_VISIT_CAPTURE", flag)
    monkeypatch.setattr(skill_module, "_utc_now", lambda: datetime(2026, 10, 15, tzinfo=UTC))
    plan = _Uploader()
    calls = []

    def search(query: str, **kwargs: Any) -> TikTokSearchResult:
        calls.append(dict(kwargs))
        if emit_capture and (callback := kwargs.get("capture")):
            callback(evidence(query))
            # 元情報は PostRecord 化より前に永続化済みであること。
            if isinstance(uploader, CaptureUploader):
                assert (
                    uploader.snapshot()["axes"][len(calls) - 1]["attempts"][-1]["posts"][0][
                        "rank_display"
                    ]
                    == 7
                )
        return TikTokSearchResult(
            query=query,
            search_type="keyword",
            videos=(
                _video("7100000000000000001", "同名の別作品"),
                _video("7100000000000000002", "商材の実演"),
            ),
        )

    skill = OmiyageReportSubmitSkill(
        store=ProposalJobStore(table_name="", memory={}),
        searcher=search,
        deck_builder=builder,
        slack=_Slack(),
        analyzer_factory=lambda _: None,
        plan_uploader=plan,
        capture_uploader=uploader,
        search_depth=2,
        relevance_checker=lambda **_: RelevanceCheck(
            excluded_ids=frozenset({"7100000000000000001"}), checked=True
        ),
    )
    return skill._execute(request(), _ctx()), plan, calls


def test_search_result_without_capture_callback_still_saves_normalized_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploader = CaptureUploader()
    result, _, _ = run_report(monkeypatch, "1", uploader, emit_capture=False)
    assert all(axis.fetched == 1 for axis in result.axes)
    snapshot = uploader.snapshot()
    for axis in snapshot["axes"]:
        saved = axis["attempts"][-1]
        assert saved["query"] == axis["query"]
        assert saved["status"] == "ok"
        assert saved["missingness_preserved"] is False
        assert [post["rank_display"] for post in saved["posts"]] == [1, 2]
        assert [post["provider_id"] for post in saved["posts"]] == [
            "7100000000000000001",
            "7100000000000000002",
        ]
        assert saved["posts"][0]["plays"] == 10_000
        assert saved["posts"][0]["missingFields"] == []


def test_capture_before_postrecord_and_relevance_retains_unfiltered_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploader = CaptureUploader()
    result, _, _ = run_report(monkeypatch, "1", uploader)
    snapshot = uploader.snapshot()
    assert len(snapshot["axes"]) == 3
    assert snapshot["request"]["brand"] == "架空ブランド"
    assert snapshot["owner_user_id"] == "U123"
    for axis in snapshot["axes"]:
        saved = axis["attempts"][-1]
        assert saved == evidence(axis["query"])
        assert [p["rank_display"] for p in saved["posts"]] == [7, 9]
        assert saved["posts"][0]["plays"] is None
        assert saved["posts"][1]["plays"] == 0
    assert all(axis.fetched == 1 for axis in result.axes)
    key = max(k for k in uploader.objects if k.endswith("relevance.json"))
    relevance = json.loads(uploader.objects[key])
    assert len(relevance["axes"]) == 3
    assert relevance["axes"][0]["excluded"][0] == {
        "id": "7100000000000000001",
        "reason": "unrelated_to_requested_category",
    }


@pytest.mark.parametrize("flag", ["0", "false", ""])
def test_flag_off_preserves_output_and_existing_s3_bytes(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    def forbidden(*args: Any) -> Any:
        raise AssertionError("OFF must never write capture")

    before, before_s3, before_calls = run_report(monkeypatch, None, forbidden)
    after, after_s3, after_calls = run_report(monkeypatch, flag, forbidden)
    assert before.model_dump_json() == after.model_dump_json()
    assert before_s3.objects == after_s3.objects
    assert before_calls == after_calls
    assert all("capture" not in call for call in before_calls)


def test_capture_storage_failure_does_not_fail_omiyage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def denied(*args: Any) -> Any:
        raise PermissionError("S3 AccessDenied")

    baseline, baseline_s3, _ = run_report(monkeypatch, "0", denied)
    result, actual_s3, _ = run_report(monkeypatch, "1", denied)
    assert result.model_dump_json() == baseline.model_dump_json()
    assert actual_s3.objects == baseline_s3.objects
    assert "first_visit_capture_save_failed" in capsys.readouterr().out


def test_all_failed_still_saves_each_axis_without_claiming_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRST_VISIT_CAPTURE", "1")
    uploader = CaptureUploader()

    def failed(query: str, **kwargs: Any) -> Any:
        raise TikTokScrapeError("TIKTOK_MEDIA_JOB_FAILED: media denied")

    skill = OmiyageReportSubmitSkill(
        store=ProposalJobStore(table_name="", memory={}), searcher=failed, capture_uploader=uploader
    )
    with pytest.raises(RuntimeError, match="検索がすべて失敗"):
        skill._execute(request(), _ctx())
    saved = uploader.snapshot()
    assert len(saved["axes"]) == 3
    for axis in saved["axes"]:
        assert axis["attempts"][-1]["status"] == "failed"
        assert axis["attempts"][-1]["error_code"] == "TIKTOK_MEDIA_JOB_FAILED"
        assert axis["attempts"][-1]["missingness_preserved"] is False


def test_s3_capture_uses_existing_private_bucket_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.skills.first_visit.recording import upload_capture

    writes: list[dict[str, Any]] = []

    class S3:
        def put_object(self, **kwargs: Any) -> dict[str, str]:
            writes.append(kwargs)
            return {"VersionId": "actual-version"}

    import boto3

    monkeypatch.setenv("OMIYAGE_DECK_PLAN_BUCKET", "existing-private-bucket")
    monkeypatch.setenv("OMIYAGE_DECK_PLAN_PREFIX", "existing-prefix/")
    monkeypatch.setattr(boto3, "client", lambda _: S3())
    ref = upload_capture("day/request/first-visit/capture.json", b"{}", "application/json")
    assert ref == {
        "bucket": "existing-private-bucket",
        "key": "existing-prefix/day/request/first-visit/capture.json",
        "version_id": "actual-version",
        "sha256": hashlib.sha256(b"{}").hexdigest(),
        "size": 2,
        "content_type": "application/json",
    }
    assert writes == [
        {
            "Bucket": ref["bucket"],
            "Key": ref["key"],
            "Body": b"{}",
            "ContentType": "application/json",
            "IfNoneMatch": "*",
        }
    ]


def test_retry_preserves_failed_attempt_and_parent_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.adapters import retry

    monkeypatch.setenv("FIRST_VISIT_CAPTURE", "1")
    monkeypatch.setenv("USE_LONG_JOB_RETRY", "1")
    monkeypatch.setattr(retry.time, "sleep", lambda _: None)
    uploader = CaptureUploader()
    calls: dict[str, int] = {}

    def search(query: str, **kwargs: Any) -> TikTokSearchResult:
        calls[query] = calls.get(query, 0) + 1
        if calls[query] == 1:
            raise TimeoutError("before response")
        kwargs["capture"](evidence(query))
        return TikTokSearchResult(
            query=query, search_type="keyword", videos=(_video("7100000000000000001", "実演"),)
        )

    skill = OmiyageReportSubmitSkill(
        store=ProposalJobStore(table_name="", memory={}),
        searcher=search,
        capture_uploader=uploader,
        plan_uploader=_Uploader(),
        deck_builder=builder,
        slack=_Slack(),
        analyzer_factory=lambda _: None,
    )
    job_id = "omy_" + "1" * 32
    skill._execute(request(), _ctx(), job_id=job_id)
    snapshot = uploader.snapshot()
    assert snapshot["omiyage_job_id"] == job_id
    for axis in snapshot["axes"]:
        assert [attempt["status"] for attempt in axis["attempts"]] == ["failed", "ok"]
        assert axis["attempts"][0]["acquired_on"] is None
        assert axis["attempts"][1] == evidence(axis["query"])
