"""Common model response failures; no paid calls or source-verification shortcuts."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from teamagent.adapters.gemini_client import GeminiClient
from teamagent.skills.media_case_search.schema import Distillation
from teamagent.skills.media_case_search.skill import _post_error_message
from tests.adapters.test_apify_media_cases import png
from tests.adapters.test_apify_tiktok_video import _FakeTikTokApify
from tests.skills.media_case_search.test_media_cases import (
    HANDLE,
    FakeGemini,
    analyze,
    extraction,
    make_skill,
    raw_post,
)


def changed(path, value):
    result = extraction()
    node = result
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    return result


@pytest.mark.parametrize(
    "path,value,expected",
    [
        (("category",), " TRAVEL ", "旅行"),
        (("category",), "UNKNOWN", "不明"),
        (("category",), None, "不明"),
        (("geography",), " DOMESTIC ", "domestic"),
        (("geography",), "海外", "overseas"),
        (("geography",), None, "unknown"),
        (("campaign_types",), ["CP", "NEW PRODUCT", "EVENT"], ["campaign", "new_product", "event"]),
        (("campaign_types",), ["STORE-OPENING", "ブランド紹介"], ["store_opening", "brand_intro"]),
        (("campaign_types",), None, []),
        (("campaign_tags",), None, []),
        (("summary",), None, ""),
        (("brand",), None, None),
        (("evidence", 0, "source"), " CAPTION ", "caption"),
    ],
)
def test_semantically_equivalent_responses_need_no_retry(ledger, path, value, expected):
    payload = changed(("items", 0, *path), value)
    (metadata, _, _), model = analyze(ledger, replies=[payload])
    actual = metadata["items"][0]
    for part in path:
        actual = actual[part]
    assert actual == expected and len(model.calls) == 1


def test_unknown_keys_are_ignored_at_every_extraction_level(ledger):
    payload = extraction()
    payload["confidence"] = 0.9
    payload["items"][0]["reasoning"] = "unused"
    payload["items"][0]["evidence"][0]["url"] = "https://untrusted.example"
    (metadata, _, _), _ = analyze(ledger, replies=[payload])
    assert metadata["items"][0] == Distillation.model_validate(extraction()).items[0].model_dump()


@pytest.mark.parametrize("items", [[], [{"summary": None, "evidence": None}]])
def test_empty_items_and_null_optional_evidence_do_not_invent_items(ledger, items):
    (metadata, text, _), model = analyze(
        ledger, replies=[{**extraction(), "items": items, "pr_evidence": None}]
    )
    assert metadata["items"] == [] and text == "" and len(model.calls) == 1


@pytest.mark.parametrize(
    "path,value",
    [
        (("is_roundup",), None),
        (("contains_personal_names",), None),
        (("items",), None),
        (("items",), {}),
        (("items",), extraction()["items"] * 11),
        (("items", 0, "category"), "made_up_category"),
        (("items", 0, "geography"), "asia"),
        (("items", 0, "campaign_types"), ["made_up_type"]),
        (("items", 0, "campaign_types"), ["event"] * 7),
        (("items", 0, "brand"), 123),
        (("items", 0, "evidence", 0, "excerpt"), None),
        (("items", 0, "evidence", 0, "excerpt"), ""),
        (("items", 0, "evidence", 0, "source"), "audio"),
        (("items", 0, "evidence", 0, "image_index"), 11),
        (("items", 0, "evidence", 0, "excerpt"), "原文にない引用"),
        (("items", 0, "evidence", 0, "source"), "slide"),
        (("items", 0, "evidence"), extraction()["items"][0]["evidence"] * 9),
        (("pr_evidence",), [{"source": "caption", "excerpt": "#PR"}]),
    ],
)
def test_invalid_answer_gets_one_fresh_paid_attempt(ledger, path, value):
    (metadata, _, cost), model = analyze(ledger, replies=[changed(path, value), extraction()])
    assert len(model.calls) == 2 and model.calls[0][0] != model.calls[1][0]
    assert metadata["items"][0]["brand"] == "テスト旅行社"
    assert cost == pytest.approx(0.0002)
    row = next(v for k, v in ledger._ddb.rows.items() if k.startswith("media_case_gemini#"))
    assert row["units"] == 2 and row["cost_micro"] == 200


def test_second_invalid_answer_fails_and_both_attempts_are_settled(ledger):
    bad = changed(("items",), None)
    with pytest.raises(ValidationError):
        analyze(ledger, replies=[bad, bad, extraction()])
    row = next(v for k, v in ledger._ddb.rows.items() if k.startswith("media_case_gemini#"))
    assert row["units"] == 2 and row["cost_micro"] == 200


def test_real_adapter_makes_uncached_repair_with_minimal_thinking(ledger):
    fake = MagicMock()
    fake.models.count_tokens.return_value = SimpleNamespace(total_tokens=1700)
    fake.models.generate_content.side_effect = [
        SimpleNamespace(
            text=text,
            usage_metadata=SimpleNamespace(
                prompt_token_count=1700, candidates_token_count=360, thoughts_token_count=0
            ),
        )
        for text in ("{broken JSON", json.dumps(extraction()))
    ]
    raw = raw_post()
    skill = make_skill(
        ledger,
        _FakeTikTokApify({f"cover-{raw['id']}.png": png()}, [raw]),
        model=GeminiClient(model_id="gemini-3.5-flash", client=fake),
    )
    skill.ingest_initial([HANDLE])
    assert fake.models.generate_content.call_count == fake.models.count_tokens.call_count == 2
    assert skill.store.post(raw["id"])["cost_usd"] == pytest.approx(0.01158)
    for call in fake.models.generate_content.call_args_list:
        config = call.kwargs["config"]
        assert config.thinking_config.thinking_level == "MINIMAL"
        assert config.cached_content is None and config.http_options.retry_options.attempts == 1
        assert call.kwargs["contents"][0].parts[0].inline_data.data == png()


def test_personal_names_remain_excluded_even_with_invalid_evidence(ledger):
    bad = extraction(evidence=[{"source": "caption", "excerpt": "捏造"}])
    bad["contains_personal_names"] = True
    with pytest.raises(ValueError, match="personal names excluded"):
        analyze(ledger, replies=[bad, extraction()])


def test_overlong_strings_record_coverage_and_full_quote_is_checked(ledger):
    payload = extraction()
    item = payload["items"][0]
    for field in ("brand", "product", "campaign", "facility", "summary"):
        item[field] = "長" * 250
    item["evidence"][0]["excerpt"] = "旅" * 250
    (metadata, _, _), model = analyze(ledger, raw=raw_post(text="旅" * 250), replies=[payload])
    saved = metadata["items"][0]
    assert len(saved["brand"]) == 100 and len(saved["summary"]) == 200
    assert len(saved["evidence"][0]["excerpt"]) == 200
    assert metadata["coverage"]["truncated_fields"] == [
        "brand",
        "campaign",
        "excerpt",
        "facility",
        "product",
        "summary",
    ]
    assert metadata["analysis_complete"] and len(model.calls) == 1
    forged = copy.deepcopy(payload)
    forged["items"][0]["evidence"][0]["excerpt"] += "捏造"
    with pytest.raises(ValueError, match="evidence does not match"):
        analyze(ledger, raw=raw_post(text="旅" * 250), replies=[forged, forged])


def test_truncation_cannot_hide_contacts(ledger):
    bad = changed(("items", 0, "brand"), "長" * 101 + " person@example.com")
    with pytest.raises(ValueError, match="personal contact"):
        analyze(ledger, replies=[bad])


def test_truncation_cannot_hide_image_pr_negation(ledger):
    quote = "旅" * 198 + "広告ではありません"
    payload = extraction()
    payload["pr_evidence"] = [{"source": "thumbnail", "excerpt": quote, "image_index": 0}]
    raw = raw_post()
    (metadata, _, _), _ = analyze(
        ledger, raw=raw, records={f"cover-{raw['id']}.png": png()}, replies=[payload]
    )
    assert metadata["pr_status"] == "unknown" and metadata["pr_evidence"] == []


def test_cover_alias_still_requires_the_supplied_image(ledger):
    payload = extraction(evidence=[{"source": "COVER", "excerpt": "ブランド", "image_index": 0}])
    raw = raw_post()
    (metadata, _, _), _ = analyze(
        ledger, raw=raw, records={f"cover-{raw['id']}.png": png()}, replies=[payload]
    )
    assert metadata["items"][0]["evidence"][0]["source"] == "thumbnail"
    with pytest.raises(ValueError, match="evidence does not match"):
        analyze(ledger, replies=[payload, payload])


def test_post_failure_logs_redacted_bounded_message_and_retains_failed_id(ledger, monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr("teamagent.skills.media_case_search.skill.logger", logger)

    class Broken(FakeGemini):
        def _reply(self, prompt, images, cap):
            raise RuntimeError(
                "failed person@example.com @private 090-1234-5678 https://example.com/private xoxb-123456789012345 "
                + "長" * 300
            )

    raw = raw_post()
    skill = make_skill(ledger, _FakeTikTokApify({}, [raw]), model=Broken())
    skill.ingest_initial([HANDLE])
    message = logger.warning.call_args.kwargs["error_message"]
    assert len(message) == 200 and message.startswith("failed ")
    assert all(
        secret not in message for secret in ("person@", "@private", "090-1234", "https://", "xoxb-")
    )
    assert logger.warning.call_args.args == ("media_case_post_failed",)
    assert logger.warning.call_args.kwargs["error"] == "RuntimeError"
    assert skill.store.state(HANDLE)["failed_ids"] == [raw["id"]]


def test_validation_diagnostics_include_field_and_reason_without_echoing_input():
    with pytest.raises(ValidationError) as error:
        Distillation.model_validate_json(
            json.dumps(changed(("items", 0, "brand"), {"person": "秘密の人物名"}))
        )
    message = _post_error_message(error.value)
    assert "items.0.brand" in message and "valid string" in message
    assert "秘密の人物名" not in message and len(message) <= 200
