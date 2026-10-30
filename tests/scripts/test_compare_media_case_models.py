"""Deterministic saved-input comparison and honest denominators; no corpus writes."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts.compare_media_case_models import (
    ComparisonAssets,
    compare,
    main,
    select_posts,
    summarize,
)
from teamagent.adapters.apify_client import ApifyClient, ApifyRunResult
from teamagent.adapters.cost_guard import CostGuard, CostLimitExceededError
from teamagent.skills.media_case_search.distill import CaseDistiller
from tests.adapters.test_apify_media_cases import ledger as ledger_fixture
from tests.adapters.test_apify_media_cases import png
from tests.skills.media_case_search.test_media_cases import FakeGemini as _FakeGemini
from tests.skills.media_case_search.test_media_cases import raw_post as _raw_post

ledger = ledger_fixture
raw_post: Callable[..., dict[str, Any]] = _raw_post
fake_gemini: Callable[..., Any] = _FakeGemini


def test_selection_is_numeric_deduplicated_and_order_independent() -> None:
    rows = [raw_post(n) for n in (3, 1, 2, 1)]
    assert (
        select_posts(rows, 2) == select_posts(list(reversed(rows)), 2) == [raw_post(1), raw_post(2)]
    )
    with pytest.raises(ValueError, match="conflicting"):
        select_posts([raw_post(1), raw_post(1, text="different")], 50)
    with pytest.raises(ValueError):
        select_posts([raw_post(webVideoUrl="https://unapproved.example")], 50)
    with pytest.raises(ValueError):
        select_posts([], 50)


def row(post_id: str, brand: str | None, *, failed: bool = False) -> dict[str, Any]:
    return {
        "post_id": post_id,
        "status": "failed" if failed else "ok",
        "items": [{"brand": brand, "campaign": None, "category": "旅行"}],
        "cost_usd": 0.01,
        "elapsed_s": 2,
    }


def test_summary_counts_failures_costs_missing_values_and_successful_pairs() -> None:
    a = [row("1", "A"), row("2", None), row("3", "C", failed=True)]
    b = [row("3", "C"), row("2", None), row("1", "B")]
    report = summarize({"a": a, "b": b, "empty": []})
    assert report["models"]["a"] == {
        "posts": 3,
        "failed": 1,
        "cost_usd": 0.03,
        "cost_per_post_usd": 0.01,
        "elapsed_s": 6,
        "seconds_per_post": 2,
    }
    fields = report["agreement"][0]["fields"]
    assert fields["brand"] == {"matches": 1, "compared": 2, "both_empty": 1, "rate": 0.5}
    assert fields["campaign"] == {"matches": 2, "compared": 2, "both_empty": 2, "rate": 1}
    assert fields["category"]["rate"] == 1
    assert report["agreement"][1]["fields"]["brand"]["rate"] is None
    a[0]["items"] *= 2
    a[0]["items"].append({"brand": "Z", "campaign": None, "category": "食品"})
    b[-1]["items"] = list(reversed(a[0]["items"]))
    assert summarize({"a": a, "b": b})["agreement"][0]["fields"]["brand"]["rate"] == 1


@pytest.mark.parametrize("source", ["--dataset", "--jsonl"])
def test_cli_default_50_saved_posts_json_table_and_no_corpus_writes(
    ledger: CostGuard,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    source: str,
) -> None:
    rows = [raw_post(n) for n in reversed(range(1, 54))]
    apify = MagicMock(spec=ApifyClient)
    run = ApifyRunResult(rows, "", "", "SUCCEEDED", 0, kvs_id="saved")
    apify.read_media_dataset.return_value = run
    apify.resolve_kvs_id.return_value = "saved"
    apify.list_kvs_keys.return_value = []
    monkeypatch.setattr(ApifyClient, "from_env", MagicMock(return_value=apify))
    monkeypatch.setattr(CostGuard, "from_env", MagicMock(return_value=ledger))

    def factory(ledger: CostGuard, *, model: str) -> CaseDistiller:
        fake = fake_gemini()
        return CaseDistiller(fake, ledger)

    monkeypatch.setattr(CaseDistiller, "from_env", factory)
    # A DB constructor is never needed for comparison.
    monkeypatch.setattr(
        "teamagent.skills.media_case_search.skill.MediaCaseStore",
        MagicMock(side_effect=AssertionError("DB write")),
    )
    path = tmp_path / "posts.jsonl"
    path.write_text("\n".join(json.dumps(raw) for raw in rows))
    assert (
        main(
            [
                source,
                str(path) if source == "--jsonl" else "saved-dataset",
                "--kvs",
                "saved",
                "--models",
                "gemini-3.5-flash",
                "bedrock-haiku",
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["selection"]["post_ids"] == [str(raw_post(n)["id"]) for n in range(1, 51)]
    assert all(m["posts"] == 50 and m["failed"] == 1 for m in report["models"].values())
    assert report["agreement"][0]["fields"]["brand"]["compared"] == 49
    assert "USD/post" in output.err and "brand=49/49 (100.0%)" in output.err
    assert not apify.tiktok_profiles.called and not apify.run_actor_sync.called
    assert apify.read_media_dataset.call_count == (1 if source == "--dataset" else 0)
    assert "ValidationError" in output.out


def test_assets_freeze_images_subtitles_and_failures() -> None:
    apify = MagicMock(spec=ApifyClient)
    apify.list_kvs_keys.return_value = ["cover-123.png", "subtitle-123.txt"]
    apify.get_kvs_record.side_effect = [b"subtitle", png()]
    assets = ComparisonAssets(apify, ApifyRunResult([], "", "", "SUCCEEDED", 0, kvs_id="saved"))
    assert assets.for_post({}, "123") == assets.for_post({}, "123")
    assert assets.image("cover-123.png") == assets.image("cover-123.png")
    assert apify.get_kvs_record.call_count == 2
    apify.get_kvs_record.side_effect = ValueError("missing")
    for _ in range(2):
        with pytest.raises(ValueError):
            assets.image("missing.png")
    assert apify.get_kvs_record.call_count == 3


def test_budget_failure_stops_comparison(ledger: CostGuard) -> None:
    distiller = MagicMock(spec=CaseDistiller)
    distiller.settled_cost_usd = 0
    distiller.analyze.side_effect = CostLimitExceededError("budget")
    report = compare([raw_post(1), raw_post(2)], MagicMock(), {"a": distiller, "b": distiller})
    assert report["stopped_on_budget"]
    assert len(report["results"]["a"]) == 1 and not report["results"]["b"]
    assert report["results"]["a"][0]["error"] == "CostLimitExceededError"


def test_commit_flag_is_not_supported() -> None:
    with pytest.raises(SystemExit):
        main(["--dataset", "saved", "--kvs", "saved", "--commit"])


def test_direct_cli_has_no_import_logs_on_stdout() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "compare_media_case_models.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.startswith("usage:")
    assert "skill_registered" not in result.stdout


def test_comparison_downloads_shared_assets_before_model_timing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    apify = MagicMock(spec=ApifyClient)
    raw = raw_post(1)
    key = f"cover-{raw['id']}.png"
    apify.list_kvs_keys.return_value = [key]

    def download(*args: Any, **kwargs: Any) -> bytes:
        nonlocal now
        now += 100
        return png()

    apify.get_kvs_record.side_effect = download
    assets = ComparisonAssets(apify, ApifyRunResult([], "", "", "SUCCEEDED", 0, kvs_id="saved"))
    model = MagicMock(spec=CaseDistiller)
    model.settled_cost_usd = 0

    def analyze(*args: Any) -> tuple[dict[str, Any], str, float]:
        nonlocal now
        assert apify.get_kvs_record.call_count == 1
        assets.image(key)
        now += 1
        return (
            {
                "items": [],
                "model": "fake",
                "thinking": "minimal",
                "image_count": 1,
                "warnings": [],
                "coverage": {},
                "input_hash": "fake",
                "analysis_complete": True,
            },
            "",
            0,
        )

    monkeypatch.setattr("scripts.compare_media_case_models.time.perf_counter", lambda: now)
    model.analyze.side_effect = analyze
    report = compare([raw], assets, {"a": model, "b": model})
    assert all(value["elapsed_s"] == 1 for value in report["models"].values())
    assert apify.get_kvs_record.call_count == 1
