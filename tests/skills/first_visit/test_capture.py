"""Saved acquisition snapshots stay separate until the existing input gate passes."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from teamagent.skills.first_visit import capture, contract

from .fixture import VID
from .test_conversion import OTHER, URL, synthetic_sources


def saved_capture() -> dict[str, Any]:
    request, axes, normalized, acquisition, _ = synthetic_sources()
    return {
        "schema_version": 1,
        "capture_id": "synthetic-capture",
        "owner_user_id": "synthetic-owner",
        "request_id": "synthetic-request",
        "omiyage_job_id": "synthetic-omiyage-job",
        "request": request.model_dump(mode="json"),
        "axes": [
            {
                "axis_id": f"axis_{index:02d}",
                "role": axis.role,
                "label": axis.label,
                "query": axis.query,
                "requested": axis.requested,
                "attempts": [
                    {
                        **acquisition[f"{axis.role}:{axis.label}"],
                        "query": axis.query,
                        "search_type": "search",
                        "requested": axis.requested,
                        "posts": [normalized["posts"][index - 1]],
                        "shortfalls": [],
                        "media_job_id": f"synthetic-job-{index}",
                        "artifacts": [],
                        "raw_response": {"opaque": f"original-{index}"},
                    }
                ],
            }
            for index, axis in enumerate(axes, 1)
        ],
    }


def stored(value: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
    data = json.dumps(value, ensure_ascii=False).encode()
    return {
        "bucket": "private-artifacts",
        "key": "omiyage/synthetic/capture.json",
        "version_id": "synthetic-version",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "content_type": "application/json",
    }, data


def materialize(
    value: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    ref, data = stored(value)
    monkeypatch.setattr(
        capture, "missing_inputs", lambda *args, **kwargs: ["labels.json: 未作成です"]
    )
    root = tmp_path / "case"
    result = capture.materialize_capture(
        ref,
        root,
        reader=lambda pointer: data if pointer == ref else b"",
        kit_path=tmp_path / "kit",
        case_details=synthetic_sources()[4],
        selection=[{"id": VID, "url": URL}],
    )
    return root, result, ref


def read(root: Path, file: str) -> Any:
    return json.loads((root / file).read_bytes())


def test_saved_rows_keep_original_order_rank_missingness_ads_and_each_axis_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = saved_capture()
    rows = value["axes"][0]["attempts"][0]["posts"]
    rows[0].update(rank_display=7, plays=None, comments=0, isAd=True)
    second = dict(rows[0], provider_id=OTHER, url=URL.replace(VID, OTHER), rank_display=3)
    rows.insert(0, second)
    root, result, ref = materialize(value, tmp_path, monkeypatch)
    videos = read(root, "raw/axis_01.json")["videos"]
    assert [(row["id"], row["rank_display"]) for row in videos] == [(OTHER, 3), (VID, 7)]
    assert videos[1]["url"] == URL
    assert videos[1]["stats"]["playCount"] is None
    assert "stats.playCount" in videos[1]["missingFields"]
    assert videos[1]["stats"]["commentCount"] == 0
    assert videos[1]["isAd"] is True
    assert videos[1]["mediaType"] == "video"
    assert read(root, "raw/axis_02.json")["videos"][0]["id"] == VID
    assert result["missing_inputs"] == ["labels.json: 未作成です"]
    assert result["manifest"] is None
    assert not (root / "manifest.json").exists()
    assert read(root, "capture-manifest.json")["capture_ref"] == ref


def test_identical_queries_use_distinct_axis_snapshots_and_actual_executed_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = saved_capture()
    own = value["axes"][1]["attempts"][0]
    own["query"] = value["axes"][0]["query"]
    own["posts"][0].update(kw=own["query"], rank_display=9, plays=45)
    root, _, _ = materialize(value, tmp_path, monkeypatch)
    general = read(root, "raw/axis_01.json")
    brand = read(root, "raw/axis_02.json")
    assert general["query"] == brand["query"] == own["query"]
    assert len(general["videos"]) == len(brand["videos"]) == 1
    assert general["videos"][0]["stats"]["playCount"] == 120
    assert brand["videos"][0]["stats"]["playCount"] == 45
    assert brand["videos"][0]["rank_display"] == 9


def test_saved_normalized_capture_keeps_unknown_distinct_from_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = saved_capture()
    attempt = value["axes"][0]["attempts"][0]
    attempt["missingness_preserved"] = False
    attempt["posts"][0].update(
        plays=0,
        likes=None,
        missingFields=[],
        unknownFields=["stats.playCount", "stats.diggCount"],
    )
    attempt["shortfalls"] = [
        {
            "kw": attempt["query"],
            "reason": "MEDIA_TIKTOK_RESULT_SHORTFALL",
            "actual": 1,
        }
    ]
    root, _, ref = materialize(value, tmp_path, monkeypatch)
    raw = read(root, "raw/axis_01.json")
    video = raw["videos"][0]
    assert video["stats"]["playCount"] is None
    assert video["stats"]["diggCount"] is None
    assert set(video["unknownFields"]) >= {"stats.playCount", "stats.diggCount"}
    assert video["missingFields"] == []
    assert raw["aico"]["shortfalls"] == attempt["shortfalls"]
    assert raw["aico"]["source"]["media_job_id"] == attempt["media_job_id"]
    assert raw["aico"]["source"]["capture_ref"] == ref
    assert raw["videos"][0]["rank_display"] == attempt["posts"][0]["rank_display"]


def test_last_attempt_is_selected_without_erasing_failed_attempt_or_artifact_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = saved_capture()
    axis = value["axes"][0]
    success = copy.deepcopy(axis["attempts"][0])
    artifact_ref, _ = stored({"synthetic": True})
    success["artifacts"] = [{"name": "posts.normalized.json", "object": artifact_ref}]
    axis["attempts"][0].update(status="failed", posts=[], error_code="UPSTREAM_TIMEOUT")
    axis["attempts"].append(success)
    root, result, ref = materialize(value, tmp_path, monkeypatch)
    manifest = result["capture_manifest"]
    assert manifest == read(root, "capture-manifest.json")
    assert manifest["capture_ref"] == ref
    assert manifest["capture_id"] == value["capture_id"]
    assert manifest["owner_user_id"] == value["owner_user_id"]
    assert manifest["omiyage_job_id"] == value["omiyage_job_id"]
    saved_axis = manifest["axes"][0]
    assert saved_axis["selected_attempt"] == 1
    assert [attempt["status"] for attempt in saved_axis["attempts"]] == ["failed", "ok"]
    assert saved_axis["attempts"][1]["artifacts"][0]["object"] == artifact_ref
    raw = read(root, "raw/axis_01.json")
    assert raw["aico"]["status"] == "ok"
    assert [video["id"] for video in raw["videos"]] == [VID]
    source = raw["aico"]["source"]
    assert source["media_job_id"] == success["media_job_id"]
    assert source["capture_ref"] == ref
    assert source["axis_id"] == "axis_01"
    assert source["attempt_index"] == 1


def test_same_query_shortfall_does_not_poison_another_axis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = saved_capture()
    own = value["axes"][1]["attempts"][0]
    own.update(query=value["axes"][0]["query"], status="failed", posts=[])
    own["shortfalls"] = [{"kw": own["query"], "reason": "MEDIA_TIKTOK_UPSTREAM_TIMEOUT"}]
    root, _, _ = materialize(value, tmp_path, monkeypatch)
    assert read(root, "raw/axis_01.json")["aico"]["status"] == "ok"
    assert read(root, "raw/axis_01.json")["aico"]["shortfalls"] == []
    assert read(root, "raw/axis_02.json")["aico"]["status"] == "failed"
    assert read(root, "raw/axis_02.json")["errorCode"] == "MEDIA_TIKTOK_UPSTREAM_TIMEOUT"


def test_observed_empty_failed_and_not_attempted_remain_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = saved_capture()
    empty = value["axes"][1]["attempts"][0]
    empty.update(status="observed_zero", posts=[], error_code="TIKTOK_TRULY_EMPTY")
    failed = value["axes"][2]["attempts"][0]
    failed.update(status="failed", posts=[], error_code="MEDIA_TIKTOK_UPSTREAM_TIMEOUT")
    root, _, _ = materialize(value, tmp_path, monkeypatch)
    assert read(root, "raw/axis_02.json")["errorCode"] == "TIKTOK_TRULY_EMPTY"
    assert read(root, "raw/axis_02.json")["aico"]["status"] == "observed_zero"
    assert read(root, "raw/axis_03.json")["errorCode"] == "MEDIA_TIKTOK_UPSTREAM_TIMEOUT"
    assert read(root, "raw/axis_03.json")["aico"]["status"] == "failed"
    assert read(root, "raw/axis_02.json")["videos"] == []
    assert read(root, "raw/axis_03.json")["videos"] == []
    other = saved_capture()
    other["axes"][2]["attempts"] = []
    pending_root, _, _ = materialize(other, tmp_path / "pending", monkeypatch)
    assert read(pending_root, "raw/axis_03.json")["aico"]["status"] == "unknown"


@pytest.mark.parametrize("corruption", ["digest", "size", "schema", "axis_id", "identity", "query"])
def test_invalid_saved_capture_fails_before_creating_output(
    tmp_path: Path, corruption: str
) -> None:
    value = saved_capture()
    if corruption == "schema":
        value["schema_version"] = 99
    if corruption == "axis_id":
        value["axes"][1]["axis_id"] = "axis_01"
    if corruption == "identity":
        value["axes"][0]["attempts"][0]["posts"][0]["url"] = URL.replace(VID, OTHER)
    if corruption == "query":
        value["axes"][0]["attempts"][0]["posts"][0]["kw"] = "never executed"
    ref, data = stored(value)
    if corruption == "digest":
        ref["sha256"] = "0" * 64
    if corruption == "size":
        ref["size"] += 1
    root = tmp_path / "case"
    with pytest.raises(ValueError):
        capture.materialize_capture(ref, root, reader=lambda _: data, kit_path=tmp_path / "kit")
    assert not root.exists()


def test_capture_without_bucket_version_is_verified_by_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref, data = stored(saved_capture())
    ref["version_id"] = None
    monkeypatch.setattr(
        capture, "missing_inputs", lambda *args, **kwargs: ["labels.json: 未作成です"]
    )
    result = capture.materialize_capture(
        ref, tmp_path / "case", reader=lambda _: data, kit_path=tmp_path / "kit"
    )
    assert result["capture_manifest"]["capture_ref"]["version_id"] is None


def test_uses_real_missing_inputs_to_report_human_evidence_and_never_seals(
    tmp_path: Path,
) -> None:
    ref, data = stored(saved_capture())
    root = tmp_path / "case"
    result = capture.materialize_capture(
        ref,
        root,
        reader=lambda _: data,
        kit_path=tmp_path / "kit",
        case_details=synthetic_sources()[4],
        selection=[{"id": VID, "url": URL}],
    )
    missing = "\n".join(result["missing_inputs"])
    for name in (
        "labels.json",
        "first_visit.json",
        "transcripts/",
        "anatomy/",
        "authored/synthesis.json",
    ):
        assert name in missing
    assert result["manifest"] is None
    assert not (root / "manifest.json").exists()


def test_seal_only_after_existing_missing_inputs_accepts_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref, data = stored(saved_capture())
    calls: list[Path] = []
    monkeypatch.setattr(capture, "missing_inputs", lambda *args, **kwargs: [])

    def seal(root: Path, **kwargs: Any) -> dict[str, Any]:
        calls.append(root)
        return {"verified": True}

    monkeypatch.setattr(capture, "seal_inputs", seal)
    root = tmp_path / "case"
    result = capture.materialize_capture(
        ref, root, reader=lambda _: data, kit_path=tmp_path / "kit"
    )
    assert result["manifest"] == {"verified": True}
    assert result["missing_inputs"] == []
    assert calls == [root]


@pytest.mark.parametrize("field", ["axis_posts", "axis_shortfalls"])
@pytest.mark.parametrize("bad_value", [None, [], {}, {"axis_01": [{}]}, {"axis_01": "not-rows"}])
def test_keyed_contract_rejects_missing_or_malformed_snapshots(field: str, bad_value: Any) -> None:
    request, axes, normalized, acquisition, _ = synthetic_sources()
    for index, axis in enumerate(axes, 1):
        acquisition[f"{axis.role}:{axis.label}"]["axis_id"] = f"axis_{index:02d}"
    normalized[field] = bad_value
    with pytest.raises(ValueError):
        contract.convert_inputs(request, axes, normalized, acquisition=acquisition)


@pytest.mark.parametrize(
    ("field", "value"),
    [("role", "other"), ("label", None), ("query", ""), ("requested", True), ("requested", -1)],
)
def test_malformed_axis_metadata_is_rejected_before_writes(
    tmp_path: Path, field: str, value: Any
) -> None:
    saved = saved_capture()
    saved["axes"][0][field] = value
    ref, data = stored(saved)
    root = tmp_path / "case"
    with pytest.raises(ValueError, match="capture:"):
        capture.materialize_capture(ref, root, reader=lambda _: data, kit_path=tmp_path / "kit")
    assert not root.exists()
