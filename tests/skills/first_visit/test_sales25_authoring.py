"""Anonymous production authoring exercises the pinned 25-page runner without external calls."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from teamagent.media.deck_qa import inspect_pptx
from teamagent.skills.first_visit import sales25, sales25_analysis, sales25_authoring
from teamagent.skills.first_visit import sales25_contract as contract
from tests.skills.first_visit.sales25_fixture import OWNER, inputs
from tests.skills.first_visit.test_sales25_analysis import fake_model, package, reply


def relevance_model() -> Any:
    model = fake_model()
    slides = model.generate_text.side_effect

    def text(prompt: str, request_id: str, **kwargs: Any) -> Any:
        if not request_id.startswith("sales25-relevance"):
            return slides(prompt, request_id, **kwargs)
        data = json.loads(prompt.split("Input:\n", 1)[1])
        return reply(
            {
                "decisions": [
                    {
                        "id": p["id"],
                        "eligible": True,
                        "reason": "案件の同義語に合致する合成投稿。",
                        "brand_matches": [next(iter(data["brands"]))],
                    }
                    for p in data["posts"]
                ]
            }
        )

    model.generate_text.side_effect = text
    return model


def case_inputs(count: int = 100) -> tuple[dict[str, Any], dict[str, Any]]:
    case, raw, _ = inputs()
    for q in case["queries"][:3]:
        q["aliases"] = [q["group"], "関連名"]
    case["queries"][0]["aliases"] += ["コープ", "COOP", "生協"]
    for value in raw.values():
        value["videos"] = value["videos"][:count]
        value["stop_reason"] = "no_new" if count < 100 else "capped"
    return case, raw


def real_images(operation: Any) -> dict[str, bytes]:
    artifacts = package({"id": operation.post_id, "url": operation.url}, audio=False)
    manifest = json.loads(artifacts["media-manifest.json"])
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for index, asset in enumerate(manifest["assets"]):
            image = io.BytesIO()
            Image.new("RGB", (72, 128), (40 * index, 100, 180)).save(image, "PNG")
            body = image.getvalue()
            asset.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
            archive.writestr(asset["path"], body)
    manifest["identity"]["source_sha256"] = manifest["assets"][0]["sha256"]
    return {
        "media-manifest.json": contract.json_bytes(manifest),
        "media-bundle.zip": output.getvalue(),
    }


def test_relevance_every_row_aliases_bounded_calls_and_raw_unchanged() -> None:
    case, raw = case_inputs()
    before = copy.deepcopy(raw)
    model = relevance_model()
    values = sales25_authoring.prepare_inputs(case, raw, owner_id=OWNER, model=model)
    assert raw == before
    assert model.generate_text.call_count == 24
    for call in model.generate_text.call_args_list:
        assert call.kwargs["max_cost_usd"] == sales25_authoring.RELEVANCE_MAX_COST_USD
        assert all(alias in call.args[0] for alias in ("コープ", "COOP", "生協"))
    for qid in raw:
        decisions = values[f"selection/decisions_{qid}.json"]
        assert len(decisions) == 100
        assert all(d["reason"] and len(d["input_sha256"]) == 64 for d in decisions)
    assert [s["role"] for s in values["deck-plan.json"]["slides"]] == list(contract.ROLES)


def test_model_cannot_omit_a_row_or_invent_brand() -> None:
    case, raw = case_inputs(6)
    model = relevance_model()
    model.generate_text.side_effect = None
    model.generate_text.return_value = reply({"decisions": []})
    with pytest.raises(ValueError, match="every batch ID"):
        sales25_authoring.prepare_inputs(case, raw, owner_id=OWNER, model=model)


@pytest.mark.parametrize("long_aliases", [False, True])
def test_partial_authoring_real_pinned_runner_25_pages(tmp_path: Path, long_aliases: bool) -> None:
    case, raw = case_inputs(6)
    if long_aliases:
        for query in case["queries"][:3]:
            query["aliases"].append("合成の長い同義語" * 10)
    root = tmp_path / "run"
    model = relevance_model()
    prepared = sales25_authoring.prepare_inputs(case, raw, owner_id=OWNER, model=model)
    contract.write_inputs(root, prepared)
    result = sales25_analysis.analyze_selection(
        root, owner_id=OWNER, model=model, acquire=real_images
    )
    assert result["status"] == "unmeasured"
    sales25_authoring.build_manuscript(root, owner_id=OWNER)
    contract.seal_inputs(root, owner_id=OWNER)
    output = tmp_path / "output"
    report = sales25.generate(root, output, owner_id=OWNER)
    assert report["status"] == "incomplete"
    assert all(q["actual"] == 6 for q in report["acquisition"])
    with zipfile.ZipFile(output / "deck.pptx") as archive:
        pages = [
            n for n in archive.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")
        ]
        assert len(pages) == 25
        content = "".join(archive.read(n).decode() for n in pages)
        assert "原音未確認" in content and "見た目未確認" in content
    data = contract.read_json(root / "deck-input.json")
    assert data["texts"]["n001"] == "関連6本\n100本目標→6本取得"
    assert data["texts"]["n003"] == "6,000"
    assert data["texts"]["n304"] == "6"  # Model's explicit family match.
    assert data["images"]["p15_image_01"]["unavailable_reason"] == "実媒体分析の対象外"
    assert report["fill_limitations"]
    with zipfile.ZipFile(output / "deck.pptx") as archive:
        for page in (2, 3, 7, 11, 15, 17, 19, 25):
            assert "100本目標→6本取得" in archive.read(f"ppt/slides/slide{page}.xml").decode()
        assert "数え方：" in archive.read("ppt/slides/slide25.xml").decode()
        page = archive.read("ppt/slides/slide25.xml").decode()
        assert ("全同義語は" if long_aliases else "COOP") in page
        for query in case["queries"][:3]:
            assert all(alias in data["notes"]["25"] for alias in query["aliases"])
    qa = inspect_pptx(output / "deck.pptx", expected_slides=25)
    assert qa.error_count == 0
    assert not [f for f in qa.findings if f.kind in {"text_overflow", "text_autofit_collision"}]


@pytest.mark.parametrize(
    "mutation", ["missing_reason", "missing_cover", "fake_complete", "fake_post_complete"]
)
def test_audio_partial_mutations_do_not_weaken_identity_gate(tmp_path: Path, mutation: str) -> None:
    case, raw = case_inputs(6)
    root = tmp_path / "run"
    model = relevance_model()
    contract.write_inputs(
        root, sales25_authoring.prepare_inputs(case, raw, owner_id=OWNER, model=model)
    )
    result = sales25_analysis.analyze_selection(
        root, owner_id=OWNER, model=model, acquire=real_images
    )
    sales25_authoring.build_manuscript(root, owner_id=OWNER)
    review = next(iter(result["posts"].values()))
    if mutation == "missing_reason":
        review["audio"]["reason"] = ""
    elif mutation == "missing_cover":
        review["cover"]["status"] = "unmeasured"
    elif mutation == "fake_complete":
        result["status"] = "complete"
    else:
        review["status"] = "complete"
    (root / "top5-analysis.json").write_bytes(contract.json_bytes(result))
    with pytest.raises(ValueError, match="full-media analysis incomplete"):
        contract.seal_inputs(root, owner_id=OWNER)


def test_relevance_budget_leaves_room_for_thinking_and_a_full_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 10-28 本番: 入力 5,485・思考 2,706 で出力が ~1,800 に削られ、25 件の JSON が途中で切れた。
    from types import SimpleNamespace

    from teamagent.adapters import gemini_client

    client = gemini_client.GeminiClient.__new__(gemini_client.GeminiClient)
    client.model_id = "gemini-3.5-flash"
    counted = SimpleNamespace(total_tokens=5485)
    fake = SimpleNamespace(models=SimpleNamespace(count_tokens=lambda **_: counted))
    monkeypatch.setattr(client, "_ensure_client", lambda: fake)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(client, "_generate_video", lambda *a, **kw: seen.update(kw))
    client.generate_text(
        "x", "r", json_mode=True, max_cost_usd=sales25_authoring.RELEVANCE_MAX_COST_USD
    )
    assert seen["max_output_tokens"] >= 2706 + 2500


@pytest.mark.parametrize("failures", [1, 2])
def test_relevance_retries_one_transient_server_error(failures: int) -> None:
    # 10-28 本番: 25 件の判定 1 回が Gemini 504 DEADLINE_EXCEEDED で落ち、資料全体が止まった。
    case, raw = case_inputs()
    model = relevance_model()
    judge = model.generate_text.side_effect
    seen: list[str] = []

    def flaky(prompt: str, request_id: str, **kwargs: Any) -> Any:
        if request_id.startswith("sales25-relevance") and seen.count(request_id) < failures:
            seen.append(request_id)
            raise RuntimeError("504 DEADLINE_EXCEEDED")
        return judge(prompt, request_id, **kwargs)

    model.generate_text.side_effect = flaky
    if failures == 1:
        values = sales25_authoring.prepare_inputs(case, raw, owner_id=OWNER, model=model)
        assert all(len(values[f"selection/decisions_{qid}.json"]) == 100 for qid in raw)
    else:
        with pytest.raises(RuntimeError, match="504"):
            sales25_authoring.prepare_inputs(case, raw, owner_id=OWNER, model=model)


@pytest.mark.parametrize("broken_every_time", [False, True])
def test_relevance_reasks_with_a_fresh_request_when_the_answer_is_malformed(
    broken_every_time: bool,
) -> None:
    # 10-29 本番: 13 件の判定の答えが形を崩し（ID の欠け等）、答えはキャッシュされるので
    # 同じ request_id で何度やり直しても同じ壊れた答えが返り、資料全体が止まった。
    case, raw = case_inputs()
    model = relevance_model()
    judge = model.generate_text.side_effect

    def cached_and_sometimes_broken(prompt: str, request_id: str, **kwargs: Any) -> Any:
        good = judge(prompt, request_id, **kwargs)
        if request_id.startswith("sales25-relevance") and (
            broken_every_time or not request_id.endswith("-again")
        ):
            data = json.loads(good.text)
            data["decisions"] = data["decisions"][
                1:
            ]  # 1 件欠ける（同じ id には毎回同じ壊れた答え）
            return reply(data)
        return good

    model.generate_text.side_effect = cached_and_sometimes_broken
    if broken_every_time:
        with pytest.raises(ValueError, match="every batch ID"):
            sales25_authoring.prepare_inputs(case, raw, owner_id=OWNER, model=model)
    else:
        values = sales25_authoring.prepare_inputs(case, raw, owner_id=OWNER, model=model)
        assert all(len(values[f"selection/decisions_{qid}.json"]) == 100 for qid in raw)
