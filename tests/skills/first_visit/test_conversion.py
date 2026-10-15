"""Conversion is additive and keeps the capture evidence without inventing values."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from teamagent.skills.first_visit.contract import convert_inputs, write_inputs
from teamagent.skills.omiyage_report.metrics import AxisData, PostRecord
from teamagent.skills.omiyage_report.schema import OmiyageReportSubmitInput

from .fixture import VID

OTHER = "7444389560311745810"
URL = f"https://www.tiktok.com/@synthetic_fixture/video/{VID}"
Sources = tuple[
    OmiyageReportSubmitInput,
    list[AxisData],
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, Any],
]


def synthetic_sources() -> Sources:
    request = OmiyageReportSubmitInput(
        brand="合成アルファ", competitors=["合成ベータ"], keywords=["手順"], category="手順"
    )
    post = PostRecord(
        video_id=VID,
        url=URL,
        author="synthetic_fixture",
        caption="合成手順の説明 #PR",
        hashtags=("手順", "PR"),
        rank=1,
        plays=120,
        likes=5,
        comments=0,
        shares=2,
        saves=3,
        followers=0,
        nickname="合成投稿者",
        cover_url="https://example.invalid/cover.jpg",
        duration_sec=20,
    )
    axes = [
        AxisData("general", "一般KW「手順」検索", "手順", 30, (post,)),
        AxisData(
            "brand",
            "ブランド名「合成アルファ」検索（「合成アルファ 手順」で検索）",
            "合成アルファ 手順",
            20,
            (post,),
        ),
        AxisData(
            "competitor",
            "競合「合成ベータ」検索（「合成ベータ 手順」で検索）",
            "合成ベータ 手順",
            20,
            (post,),
        ),
    ]
    normalized = {
        "posts": [
            {
                "pid": f"p{index:04d}",
                "provider_id": VID,
                "kw": axis.query,
                "rank_display": 1,
                "url": URL,
                "title": post.caption,
                "account_id": post.author,
                "account_name": post.nickname,
                "followers": 0,
                "plays": 120,
                "likes": 5,
                "comments": 0,
                "shares": 2,
                "saves": 3,
                "create_time": 1791936000,
                "duration": 20,
                "hashtags": list(post.hashtags),
                "cover_url": post.cover_url,
                "eg_rate": 999.0,
                "save_rate": 999.0,
                "isAd": False,
                "mediaType": "video",
                "imageCount": 0,
                "textLanguage": "ja",
                "missingFields": [],
            }
            for index, axis in enumerate(axes)
        ],
        "shortfalls": [],
    }
    acquisition = {
        f"{axis.role}:{axis.label}": {
            "status": "ok",
            "acquired_on": "2026-10-14T03:00:00Z",
            "order_basis": "search_display_order",
            "sessions": 1,
            "stop_reason": "exhausted",
            "unfiltered": True,
            "missingness_preserved": True,
        }
        for axis in axes
    }
    details = {
        "vocab": "general",
        "focus_products": ["合成手順キット"],
        "client_short": "合成A",
        "project": {"project_title": "合成初訪入力の検証", "recipient": "合成担当者御中"},
        "research": {"review_collect": False},
        "brand_details": {
            "合成アルファ": {"short": "合成A", "official_status": "none", "official": []},
            "合成ベータ": {"short": "合成B", "official_status": "none", "official": []},
        },
    }
    return request, axes, normalized, acquisition, details


def synthetic_inputs() -> dict[str, Any]:
    return _convert(synthetic_sources())


def _convert(sources: Sources, **kwargs: Any) -> dict[str, Any]:
    request, axes, normalized, acquisition, details = sources
    return convert_inputs(
        request,
        axes,
        normalized,
        acquisition=acquisition,
        case_details=details,
        selection=kwargs.pop("selection", [{"id": VID, "url": URL}]),
        **kwargs,
    )


def test_maps_provider_fields_without_copying_rounded_rates() -> None:
    inputs = synthetic_inputs()
    video = inputs["raw/axis_01.json"]["videos"][0]
    assert video == {
        "id": VID,
        "url": URL,
        "desc": "合成手順の説明 #PR",
        "hashtags": ["手順", "PR"],
        "author": {"uniqueId": "synthetic_fixture", "nickname": "合成投稿者", "followerCount": 0},
        "stats": {
            "playCount": 120,
            "diggCount": 5,
            "commentCount": 0,
            "shareCount": 2,
            "collectCount": 3,
        },
        "coverUrl": "https://example.invalid/cover.jpg",
        "createTime": 1791936000,
        "duration": 20,
        "rank_display": 1,
        "isAd": False,
        "mediaType": "video",
        "imageCount": 0,
        "textLanguage": "ja",
        "missingFields": [],
    }
    assert inputs["selection.json"] == [{"id": VID, "url": URL}]


def test_uses_canonical_names_with_real_decorated_axis_labels() -> None:
    inputs = synthetic_inputs()
    cfg = inputs["case.json"]
    assert [(b["name"], b["own"], b["file"]) for b in cfg["brands"]] == [
        ("合成アルファ", True, "raw/axis_02.json"),
        ("合成ベータ", False, "raw/axis_03.json"),
    ]
    assert cfg["keywords"] == [
        {"name": "手順", "query": "手順", "file": "raw/axis_01.json", "primary": True}
    ]
    assert cfg["brands"][0]["query"] == "合成アルファ 手順"
    assert cfg["client"] == {"client_name": "合成アルファ", "short": "合成A"}
    assert all(
        inputs[f"raw/axis_{index:02d}.json"]["videos"][0]["id"] == VID for index in range(1, 4)
    )


def test_acquisition_date_is_kit_compatible_and_original_timestamp_is_retained() -> None:
    inputs = synthetic_inputs()
    assert inputs["case.json"]["acquired_on"] == "2026-10-14"
    raw = inputs["raw/axis_01.json"]
    assert raw["fetched_on"] == "2026-10-14"
    assert raw["aico"]["source"]["acquired_on"] == "2026-10-14T03:00:00Z"


def test_canonical_axis_names_do_not_depend_on_relative_axis_order() -> None:
    request, axes, normalized, acquisition, details = synthetic_sources()
    request.competitors.append("合成ガンマ")
    gamma = replace(axes[2], label="競合「合成ガンマ」検索", query="合成ガンマ")
    acquisition[f"{gamma.role}:{gamma.label}"] = dict(
        acquisition[f"{axes[2].role}:{axes[2].label}"]
    )
    inputs = convert_inputs(
        request, [gamma, *axes], normalized, acquisition=acquisition, case_details=details
    )
    assert [(b["name"], b["query"]) for b in inputs["case.json"]["brands"]] == [
        ("合成ガンマ", "合成ガンマ"),
        ("合成アルファ", "合成アルファ 手順"),
        ("合成ベータ", "合成ベータ 手順"),
    ]


def test_missing_measurements_are_null_and_named_while_measured_zero_survives() -> None:
    sources = synthetic_sources()
    row = sources[2]["posts"][0]
    row.update(plays=0, likes=None, saves=0, missingFields=["stats.collectCount"])
    video = _convert(sources)["raw/axis_01.json"]["videos"][0]
    assert video["stats"] == {
        "playCount": 0,
        "diggCount": None,
        "commentCount": 0,
        "shareCount": 2,
        "collectCount": None,
    }
    assert set(video["missingFields"]) == {"stats.diggCount", "stats.collectCount"}
    assert video["author"]["followerCount"] == 0


def test_legacy_normalized_zero_stays_ambiguous_without_preserved_missingness() -> None:
    sources = synthetic_sources()
    axis = sources[1][0]
    sources[3][f"{axis.role}:{axis.label}"]["missingness_preserved"] = False
    sources[2]["posts"][0]["plays"] = 0
    video = _convert(sources)["raw/axis_01.json"]["videos"][0]
    assert video["stats"]["playCount"] is None
    assert video["stats"]["commentCount"] is None
    assert video["author"]["followerCount"] is None
    assert set(video["missingFields"]) >= {
        "stats.playCount",
        "stats.commentCount",
        "author.followerCount",
    }


def test_postrecord_fallback_preserves_known_values_and_exposes_unavailable_metadata() -> None:
    request, axes, _, acquisition, details = synthetic_sources()
    inputs = convert_inputs(request, axes, None, acquisition=acquisition, case_details=details)
    raw = inputs["raw/axis_01.json"]
    video = raw["videos"][0]
    assert video["id"] == VID and video["stats"]["playCount"] == 120
    assert video["stats"]["commentCount"] is None
    assert video["isAd"] is None and video["mediaType"] is None and video["createTime"] is None
    assert raw["aico"]["media_present"] is False


def test_cross_axis_ranks_and_unfiltered_source_order_are_not_sorted_or_compacted() -> None:
    sources = synthetic_sources()
    posts = sources[2]["posts"]
    posts[0]["rank_display"] = 7
    posts[1]["rank_display"] = 2
    extra = dict(
        posts[0],
        pid="p9999",
        provider_id=OTHER,
        rank_display=1,
        url=f"https://www.tiktok.com/@synthetic_fixture/video/{OTHER}",
    )
    posts.insert(1, extra)
    inputs = _convert(sources)
    assert [(v["id"], v["rank_display"]) for v in inputs["raw/axis_01.json"]["videos"]] == [
        (VID, 7),
        (OTHER, 1),
    ]
    assert inputs["raw/axis_02.json"]["videos"][0]["rank_display"] == 2
    assert inputs["selection.json"] == [{"id": VID, "url": URL}]


@pytest.mark.parametrize("ad", [True, False, None])
def test_advertisement_measurement_is_not_inferred_from_pr_hashtag(ad: bool | None) -> None:
    sources = synthetic_sources()
    sources[2]["posts"][0]["isAd"] = ad
    video = _convert(sources)["raw/axis_01.json"]["videos"][0]
    assert video["isAd"] is ad
    assert video["hashtags"] == ["手順", "PR"]
    assert "is_pr" not in video


@pytest.mark.parametrize(
    "field",
    ["plays", "likes", "comments", "shares", "saves", "followers", "create_time", "duration"],
)
@pytest.mark.parametrize("invalid", [-1, True, 1.5, float("nan"), "1"])
def test_rejects_invalid_numeric_counts(field: str, invalid: Any) -> None:
    sources = synthetic_sources()
    sources[2]["posts"][0][field] = invalid
    with pytest.raises(ValueError):
        _convert(sources)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_id": None, "pid": VID},
        {"provider_id": 7646064651251371272},
        {"provider_id": "../video"},
        {"url": f"https://www.tiktok.com/@synthetic_fixture/video/{OTHER}"},
        {"url": f"https://example.invalid/@synthetic_fixture/video/{VID}"},
        {"isAd": "false"},
        {"missingFields": "stats.playCount"},
    ],
)
def test_rejects_pid_fallback_id_url_mismatch_and_malformed_flags(changes: dict[str, Any]) -> None:
    sources = synthetic_sources()
    sources[2]["posts"][0].update(changes)
    with pytest.raises(ValueError):
        _convert(sources)


@pytest.mark.parametrize(
    "selection",
    [
        [{"id": VID, "url": "https://example.invalid/wrong"}],
        [{"id": OTHER, "url": URL}],
        [{"id": int(VID), "url": URL}],
        [{"id": VID, "url": URL}, {"id": VID, "url": URL}],
    ],
)
def test_selection_requires_unique_acquired_id_and_exact_url(
    selection: list[dict[str, Any]],
) -> None:
    with pytest.raises(ValueError):
        _convert(synthetic_sources(), selection=selection)


def test_failed_acquisition_is_distinct_from_observed_zero() -> None:
    sources = synthetic_sources()
    axis = sources[1][1]
    sources[2]["posts"] = [row for row in sources[2]["posts"] if row["kw"] != axis.query]
    meta = sources[3][f"{axis.role}:{axis.label}"]
    meta["status"] = "observed_zero"
    zero = _convert(sources)["raw/axis_02.json"]
    meta["status"] = "failed"
    sources[1][1] = replace(axis, failed=True, failure_code="BOT_WALL")
    failed = _convert(sources)["raw/axis_02.json"]
    assert zero["videos"] == failed["videos"] == []
    assert zero["aico"]["status"] == "observed_zero"
    assert zero["errorCode"] == "TIKTOK_TRULY_EMPTY"
    assert failed["aico"]["status"] == "failed" and failed["errorCode"] == "BOT_WALL"


def test_inputs_and_source_evidence_are_not_mutated() -> None:
    sources = synthetic_sources()
    before = copy.deepcopy(sources)
    inputs = _convert(sources)
    assert sources == before
    assert inputs["case.json"]["research"]["review_collect"] is False


def test_writer_creates_json_files_and_does_not_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "case"
    inputs = synthetic_inputs()
    write_inputs(root, inputs)
    assert {p.relative_to(root).as_posix() for p in root.rglob("*.json")} == set(inputs)
    assert json.loads((root / "raw/axis_01.json").read_text()) == inputs["raw/axis_01.json"]
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(FileExistsError):
        write_inputs(root, {"case.json": {"changed": True}})
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "relative", ["../escape.json", "raw/../../escape.json", "/tmp/first_visit_escape.json"]
)
def test_writer_rejects_path_escape_before_creating_case(tmp_path: Path, relative: str) -> None:
    root = tmp_path / "case"
    with pytest.raises(ValueError):
        write_inputs(root, {relative: {}})
    assert not root.exists()
