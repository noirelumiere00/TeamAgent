"""Anonymous byte fixtures exercise the core trust boundary; no SaaS/model requests."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gemini_client import GeminiClient, GeminiResponse
from teamagent.skills.first_visit import sales25_analysis as analysis
from teamagent.skills.first_visit import sales25_contract as contract
from tests.skills.first_visit.sales25_fixture import OWNER, prepare


def package(
    post: dict[str, Any], *, audio: bool = True, cover: bool = True, photo_count: int = 3
) -> dict[str, bytes]:
    files = {
        f"photo-{n}.jpg": b"\xff\xd8\xff" + f"anonymous-photo-{n}".encode()
        for n in range(1, photo_count + 1)
    }
    if audio:
        files["original.wav"] = b"anonymous-original-audio"
        files["original-audio-source.bin"] = b"anonymous-original-source"
    if cover:
        files["cover.jpg"] = b"\xff\xd8\xffanonymous-cover"
    assets = []
    for index, (path, data) in enumerate(files.items(), 1):
        kind = (
            "photo"
            if path.startswith("photo")
            else "audio"
            if path.endswith("wav")
            else "audio_source"
            if path.endswith("bin")
            else "cover"
        )
        assets.append(
            {
                "path": path,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "kind": kind,
                "timestamp_sec": None,
                "photo_index": index if kind == "photo" else None,
            }
        )
    manifest = {
        "schema_version": 1,
        "post_id": post["id"],
        "url": post["url"],
        "media_type": "photo",
        "status": "ready",
        "reason": "",
        "duration_sec": 5.0 if audio else None,
        "photo_count": photo_count,
        "audio_status": "available" if audio else "unmeasured",
        "assets": assets,
        "identity": {
            "returned_post_id": post["id"],
            "returned_url": post["url"],
            "source_sha256": assets[0]["sha256"],
            "expected_sha256": None,
            "verified": True,
            "cover_match": "unmeasured",
        },
        "coverage": {"timestamps_sec": [], "max_gap_sec": 0, "complete": True},
    }
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return {
        "media-manifest.json": contract.json_bytes(manifest),
        "media-bundle.zip": body.getvalue(),
    }


def reply(value: dict[str, Any]) -> GeminiResponse:
    return GeminiResponse(
        text=json.dumps(value),
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        model_id="approved-fixture",
        latency_ms=1,
    )


def fake_model() -> Any:
    model = MagicMock(spec=GeminiClient)
    model.analyze_image_bytes.return_value = reply(
        {
            "observation": "合成図形を表示。",
            "text": "合成",
            "interpretation": "識別を助ける仮説。",
            "proposal": "順序を試す。",
            "product_visible": True,
            "unconfirmed": [],
        }
    )
    model.compare_image_bytes.return_value = reply({"match": True, "basis": "同じ合成図形。"})
    model.analyze_audio_bytes.return_value = reply(
        {
            "status": "listened",
            "summary": "合成音を確認。",
            "start_sec": 0,
            "end_sec": 5.0,
            "segments": [{"start_sec": 0, "end_sec": 5.0, "observation": "合成音。"}],
        }
    )

    def text(prompt: str, request_id: str, **kwargs: Any) -> GeminiResponse:
        context = json.loads(prompt.split("evidence:\n", 1)[1])
        return reply(
            {
                "elements": [
                    {
                        "text": "合成図形を表示。順序を試す。",
                        "observation": "合成図形を表示。",
                        "interpretation": "識別を助ける仮説。",
                        "proposal": "順序を試す。",
                        "refs": [{"post_id": context["rankings"][-1], "evidence": "product"}],
                    }
                ]
            }
        )

    model.generate_text.side_effect = text
    return model


@pytest.fixture
def run(tmp_path: Path) -> Path:
    root = tmp_path / "run"
    prepare(root)
    (root / "top5-analysis.json").unlink()
    return root


def test_acquires_union_once_and_records_rank_ten_evidence(run: Path) -> None:
    called = []
    model = fake_model()

    def acquire(operation: Any) -> dict[str, bytes]:
        called.append(operation.post_id)
        return package({"id": operation.post_id, "url": operation.url})

    result = analysis.analyze_selection(run, owner_id=OWNER, acquire=acquire, model=model)
    assert len(called) == len(set(called)) == 30
    assert result["status"] == "complete"
    assert all(len(p["timeline"]) == 3 for p in result["posts"].values())
    assert result["slides"][0]["elements"][0]["refs"][0]["post_id"] == "9000000010"
    assert any(ref.get("rank") == 10 for ref in result["posts"]["9000000010"]["selections"])
    assert any("page" in ref for ref in result["posts"]["9000000001"]["selections"])
    audio = result["posts"]["9000000001"]["audio"]
    assert audio["input_sha256"] == hashlib.sha256(b"anonymous-original-audio").hexdigest()
    assert model.analyze_audio_bytes.call_args.args[0] == b"anonymous-original-audio"
    assert contract.read_json(run / "top5-analysis.json") == result


@pytest.mark.parametrize("tag", ["PR", "pr", "Pr", "product", 1])
def test_pr_examples_outside_top10_are_added(run: Path, tag: str | int) -> None:
    plan = contract.read_json(run / "deck-plan.json")
    pid = "9000003020"
    for relative in ("raw/q3.json", "selection/source_q3.json"):
        data = contract.read_json(run / relative)
        rows = data["videos"] if "videos" in data else data["posts"]
        next(row for row in rows if row["id"] == pid).update(isAd=False, hashtags=[tag])
        (run / relative).write_bytes(contract.json_bytes(data))
    plan["pr_examples"][0].update(query_id="q3", post_id=pid)
    (run / "deck-plan.json").write_bytes(contract.json_bytes(plan))
    if tag in ("product", 1):
        with pytest.raises(ValueError, match="PR disclosure missing"):
            analysis.selected_posts(run, OWNER)
        return
    posts = analysis.selected_posts(run, OWNER)
    assert len(posts) == 31
    assert posts[-1]["id"] == pid


@pytest.mark.parametrize("audio,cover", [(False, True), (True, False)])
def test_missing_original_audio_or_cover_cannot_complete(
    run: Path, audio: bool, cover: bool
) -> None:
    model = fake_model()
    result = analysis.analyze_selection(
        run,
        owner_id=OWNER,
        model=model,
        acquire=lambda op: package({"id": op.post_id, "url": op.url}, audio=audio, cover=cover),
    )
    assert result["status"] == "unmeasured"
    assert all(p["status"] == "unmeasured" for p in result["posts"].values())
    if not audio:
        model.analyze_audio_bytes.assert_not_called()
        assert all(p["audio"]["status"] == "unmeasured" for p in result["posts"].values())


@pytest.mark.parametrize("mutation", ["asr", "gap", "truncated"])
def test_asr_and_incomplete_audio_never_become_listening(run: Path, mutation: str) -> None:
    model = fake_model()
    data = json.loads(model.analyze_audio_bytes.return_value.text)
    if mutation == "asr":
        data["status"] = "transcribed"
    elif mutation == "gap":
        data["segments"][0]["start_sec"] = 1
    else:
        data["end_sec"] = 3
    model.analyze_audio_bytes.return_value = reply(data)
    result = analysis.analyze_selection(
        run,
        owner_id=OWNER,
        model=model,
        acquire=lambda op: package({"id": op.post_id, "url": op.url}),
    )
    assert result["status"] == "unmeasured"


@pytest.mark.parametrize(
    "mutation", ["id", "hash", "count", "truncated", "extra", "zip_path", "symlink", "zip_extra"]
)
def test_untrusted_media_rejected_before_writes(run: Path, mutation: str) -> None:
    post = analysis.selected_posts(run, OWNER)[0]
    operation = analysis.media_operation(post)
    artifacts = package(post)
    manifest = json.loads(artifacts["media-manifest.json"])
    if mutation == "id":
        manifest["post_id"] = "9000009999"
    elif mutation == "hash":
        manifest["assets"][0]["sha256"] = "0" * 64
    elif mutation == "count":
        manifest["photo_count"] = 2
    elif mutation == "truncated":
        manifest["coverage"]["complete"] = False
    elif mutation == "extra":
        manifest["unexpected"] = True
    elif mutation == "zip_path":
        manifest["assets"][0]["path"] = "../outside.jpg"
    else:
        buffer = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(artifacts["media-bundle.zip"])) as old,
            zipfile.ZipFile(buffer, "w") as new,
        ):
            for index, member in enumerate(old.infolist()):
                if mutation == "symlink" and index == 0:
                    member.external_attr = (stat.S_IFLNK | 0o777) << 16
                new.writestr(member, old.read(member))
            if mutation == "zip_extra":
                new.writestr("unexpected.txt", "bad")
        artifacts["media-bundle.zip"] = buffer.getvalue()
    artifacts["media-manifest.json"] = contract.json_bytes(manifest)
    with pytest.raises(ValueError):
        analysis.validate_media(operation, artifacts)
    assert not (run / "media").exists()


def test_same_id_identity_conflict_is_rejected(run: Path) -> None:
    # Move a selected ID into another query; preserve #682 raw/source equivalence.
    pid = "9000000001"
    old = "9000001001"
    for relative in ("raw/q1.json", "selection/source_q1.json", "selection/decisions_q1.json"):
        data = contract.read_json(run / relative)
        rows = data if isinstance(data, list) else data.get("videos", data.get("posts"))
        row = next(row for row in rows if row["id"] == old)
        row["id"] = pid
        if "url" in row:
            row["url"] = f"https://www.tiktok.com/@different/photo/{pid}"
        (run / relative).write_bytes(contract.json_bytes(data))
    plan = contract.read_json(run / "deck-plan.json")
    for ranking in plan["analysis_selection"]["queries"][1]["rankings"].values():
        ranking[0] = pid
    for slide in plan["slides"]:
        if old in slide.get("post_ids", []):
            slide["post_ids"] = [pid if value == old else value for value in slide["post_ids"]]
    (run / "deck-plan.json").write_bytes(contract.json_bytes(plan))
    with pytest.raises(ValueError, match="same-ID"):
        analysis.selected_posts(run, OWNER)


def test_original_audio_adapter_sends_bytes_and_comparison_sends_both_images() -> None:
    pytest.importorskip("google.genai")
    model = GeminiClient(api_key="offline")
    model._generate_video = MagicMock(return_value=reply({}))  # type: ignore[method-assign]
    model.analyze_audio_bytes(b"original", "audio/wav", "observe", "rid")
    args = model._generate_video.call_args.args
    assert args[0][0].inline_data.data == b"original"
    model.compare_image_bytes(b"cover", b"source", "compare", "rid")
    parts = model._generate_video.call_args.args[0]
    assert [p.inline_data.data for p in parts[:2]] == [b"cover", b"source"]
    with pytest.raises(ValueError):
        model.analyze_audio_bytes(b"transcript", "text/plain", "observe", "rid")


@pytest.mark.parametrize("fabricated_top_status", ["unmeasured", "complete"])
def test_full_media_gate_blocks_unmeasured_even_with_fabricated_complete(
    run: Path, fabricated_top_status: str
) -> None:
    result = analysis.analyze_selection(
        run,
        owner_id=OWNER,
        model=fake_model(),
        acquire=lambda op: package({"id": op.post_id, "url": op.url}, cover=False),
    )
    result["status"] = fabricated_top_status
    (run / "top5-analysis.json").write_bytes(contract.json_bytes(result))
    with pytest.raises(ValueError, match="full-media analysis incomplete"):
        contract.validate_inputs(run, owner_id=OWNER)


def test_audio_adapter_failure_is_recorded_as_unmeasured(run: Path) -> None:
    model = fake_model()
    model.analyze_audio_bytes.side_effect = OSError("offline synthetic adapter failure")
    result = analysis.analyze_selection(
        run,
        owner_id=OWNER,
        model=model,
        acquire=lambda op: package({"id": op.post_id, "url": op.url}),
    )
    assert result["status"] == "unmeasured"
    assert result["posts"]["9000000001"]["audio"]["error_type"] == "OSError"


def test_slide_evidence_cannot_cross_query(run: Path) -> None:
    model = fake_model()
    model.generate_text.side_effect = None
    model.generate_text.return_value = reply(
        {
            "elements": [
                {
                    "text": "合成図形を表示。",
                    "observation": "合成図形を表示。",
                    "interpretation": "仮説。",
                    "proposal": "試す。",
                    "refs": [{"post_id": "9000001001", "evidence": "product"}],
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="outside query/metric"):
        analysis.analyze_selection(
            run,
            owner_id=OWNER,
            model=model,
            acquire=lambda op: package({"id": op.post_id, "url": op.url}),
        )


def test_complete_output_passes_pinned_top10_checker(run: Path, tmp_path: Path) -> None:
    import subprocess
    import sys
    import xml.etree.ElementTree as ET

    result = analysis.analyze_selection(
        run,
        owner_id=OWNER,
        model=fake_model(),
        acquire=lambda op: package({"id": op.post_id, "url": op.url}),
    )
    pptx = tmp_path / "anonymous-checker-only.pptx"
    text_by_page = {s["page"]: "".join(e["text"] for e in s["elements"]) for s in result["slides"]}
    with (
        zipfile.ZipFile(contract.BUNDLE / "assets/template.pptx") as template,
        zipfile.ZipFile(pptx, "w") as output,
    ):
        for member in template.infolist():
            body = template.read(member)
            if member.filename.startswith("ppt/slides/slide") and member.filename.endswith(".xml"):
                page = int(Path(member.filename).stem.removeprefix("slide"))
                if page in text_by_page:
                    tree = ET.fromstring(body)
                    node = tree.find(".//{http://schemas.openxmlformats.org/drawingml/2006/main}t")
                    assert node is not None
                    node.text = text_by_page[page]
                    body = ET.tostring(tree)
            output.writestr(member, body)
    checked = subprocess.run(
        [
            sys.executable,
            "-B",  # 固定キットの中に __pycache__ を作らない（lock のファイル構成検査が落ちる）
            str(contract.BUNDLE / "scripts/check_top5_analysis.py"),
            "--plan",
            str(run / "deck-plan.json"),
            "--reviews",
            str(run / "top5-analysis.json"),
            "--run-dir",
            str(run),
            "--pptx",
            str(pptx),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert json.loads(checked.stdout)["reviewed_n"] == 30


@pytest.mark.parametrize("sound", [True, False])
def test_synthetic_worker_outputs_feed_core_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sound: bool
) -> None:
    from tests.media.test_sales25_media import POST_ID, URL, _fixture_acquire, _run, _video

    source = tmp_path / "synthetic.mp4"
    _video(source, sound=sound)
    _fixture_acquire(monkeypatch, [source])
    post = {"id": POST_ID, "url": URL, "mediaType": "video", "duration": 9, "selections": []}
    produced = _run(tmp_path, expected_duration_sec=9)
    manifest, files = analysis.validate_media(
        analysis.media_operation(post), {a.name: a.path.read_bytes() for a in produced.artifacts}
    )
    model = fake_model()
    response = reply(
        {
            "status": "listened" if sound else "silent",
            "summary": "Synthetic sound or silence.",
            "start_sec": 0,
            "end_sec": 9,
            "segments": [
                {"start_sec": 0, "end_sec": 9, "observation": "Synthetic sound or silence."}
            ],
        }
    )
    model.analyze_audio_bytes.return_value = response
    model.analyze_video_bytes.return_value = response
    run = tmp_path / "review"
    run.mkdir()
    reviewed = analysis._review(run, post, manifest, files, model)
    assert reviewed["audio"]["status"] == ("listened" if sound else "silent")
    assert reviewed["audio"]["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert len(reviewed["timeline"]) >= 3
    assert reviewed["evidence"]["ending"]["sec"] >= 6
    assert reviewed["status"] == "unmeasured"  # no post cover in this fixture


def test_photo_zero_duration_uses_fetched_soundtrack_duration(run: Path) -> None:
    post = analysis.selected_posts(run, OWNER)[0]
    post["duration"] = 0
    assert analysis.media_operation(post).expected_duration_sec is None
    video = {
        "id": "9000000001",
        "url": "https://www.tiktok.com/@anonymous/video/9000000001",
        "mediaType": "video",
        "duration": 0,
    }
    with pytest.raises(ValueError):
        analysis.media_operation(video)


@pytest.mark.parametrize("image_count", [3, 4])
def test_same_id_across_queries_normalizes_photo_count_and_preserves_rankings(
    run: Path, image_count: int
) -> None:
    pid, old = "9000000001", "9000001001"
    for relative in ("raw/q1.json", "selection/source_q1.json", "selection/decisions_q1.json"):
        data = contract.read_json(run / relative)
        rows = data if isinstance(data, list) else data.get("videos", data.get("posts"))
        row = next(row for row in rows if row["id"] == old)
        row["id"] = pid
        if "url" in row:
            row["url"] = f"https://www.tiktok.com/@synthetic/photo/{pid}"
            row.pop("photoCount")
            row["imageCount"] = image_count
        (run / relative).write_bytes(contract.json_bytes(data))
    plan = contract.read_json(run / "deck-plan.json")
    for ranking in plan["analysis_selection"]["queries"][1]["rankings"].values():
        ranking[0] = pid
    for slide in plan["slides"]:
        if old in slide.get("post_ids", []):
            slide["post_ids"] = [pid if value == old else value for value in slide["post_ids"]]
    plan["pr_examples"][1]["post_id"] = pid
    (run / "deck-plan.json").write_bytes(contract.json_bytes(plan))
    if image_count != 3:
        with pytest.raises(ValueError, match="same-ID photo count"):
            analysis.selected_posts(run, OWNER)
        return
    selected = analysis.selected_posts(run, OWNER)
    assert len(selected) == 29
    references = next(p for p in selected if p["id"] == pid)["selections"]
    assert {r["query_id"] for r in references} == {"q0", "q1"}
    assert len([r for r in references if "metric" in r]) == 6


@pytest.mark.parametrize(
    "counts", [{"imageCount": 3}, {"photoCount": 3}, {"imageCount": 3, "photoCount": 3}]
)
def test_search_photo_count_aliases_reject_incomplete_media(counts: dict[str, int]) -> None:
    post = {
        "id": "9000000001",
        "url": "https://www.tiktok.com/@synthetic/photo/9000000001",
        "mediaType": "photo",
        **counts,
    }
    operation = analysis.media_operation(post)
    assert operation.expected_photo_count == 3
    with pytest.raises(ValueError, match="photo count/order"):
        analysis.validate_media(operation, package(post, photo_count=2))


@pytest.mark.parametrize("other", [2, 3.0, None, True])
def test_conflicting_photo_count_aliases_are_rejected(other: Any) -> None:
    post = {
        "id": "9000000001",
        "url": "https://www.tiktok.com/@synthetic/photo/9000000001",
        "mediaType": "photo",
        "imageCount": 3,
        "photoCount": other,
    }
    with pytest.raises(ValueError, match="conflicting photo count aliases"):
        analysis.media_operation(post)


def test_manifest_cannot_overwrite_verified_media(run: Path) -> None:
    post = analysis.selected_posts(run, OWNER)[0]
    artifacts = package(post)
    manifest = json.loads(artifacts["media-manifest.json"])
    manifest["assets"][0]["path"] = "media-manifest.json"
    artifacts["media-manifest.json"] = contract.json_bytes(manifest)
    with pytest.raises(ValueError, match="unsafe"):
        analysis.validate_media(analysis.media_operation(post), artifacts)


@pytest.mark.parametrize("visible", [True, False])
def test_product_evidence_uses_observed_presence(run: Path, visible: bool) -> None:
    post = analysis.selected_posts(run, OWNER)[0]
    manifest, files = analysis.validate_media(analysis.media_operation(post), package(post))
    model = fake_model()
    point = json.loads(model.analyze_image_bytes.return_value.text)
    point["product_visible"] = visible
    model.analyze_image_bytes.return_value = reply(point)
    review = analysis._review(run, post, manifest, files, model)
    assert review["evidence"]["product"]["photo_index"] == 1
    assert ("Product not observed" in review["evidence"]["product"]["observation"]) is not visible
