"""Grounded registration, shared Haiku budget and filtered same-night inference."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog.testing
from botocore.exceptions import NoCredentialsError

from teamagent.adapters import bedrock_client
from teamagent.ingest import video_library_case_discovery as discovery
from teamagent.ingest.video_library_case_models import DEFAULT_MODEL
from tests.ingest.test_video_library_case_discovery import NOW, FakePg, video
from tests.ingest.test_video_library_case_inference import client as bedrock
from tests.migration_postgres import apply_video_library_migration

RAW: dict[str, Any] = {
    "decision": "new",
    "advertiser": "東洋水産",
    "campaign": "QTTA",
    "advertiser_source": "text",
    "confidence": 0.9,
    "evidence": ["東洋水産 QTTA"],
}
CAPTION = "東洋水産 QTTA #PR 投稿文の秘密"


class DecisionPg(FakePg):
    def __init__(self) -> None:
        super().__init__([video(i, "ＱＴＴＡ") for i in range(1, 11)] + [video(99, "other")])
        self.rows["qtta"] = dict(
            tag="qtta",
            samples=[dict(video_id="1", caption=CAPTION, subtitle_text="字幕の秘密")],
            video_count=10,
            status="sampled",
            attempted_at=NOW,
        )
        self.campaigns: dict[tuple[str, str], dict[str, str]] = {}
        self.fail_insert = False
        self.stale = False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> MagicMock:
        cursor = MagicMock(rowcount=1)
        if "SELECT DISTINCT COALESCE" in sql:
            cursor.fetchall.return_value = [
                dict(advertiser=a, campaign=c) for a, c in self.campaigns
            ]
        elif "SELECT tag,samples,attempted_at" in sql:
            cursor.fetchall.return_value = [
                r.copy() for r in self.rows.values() if r["status"] == "sampled"
            ][: params[0]]
        elif "UPDATE vl_case_discovery" in sql:
            status, advertiser, campaign, confidence, evidence, tag, attempted = params
            row = self.rows[tag]
            if self.stale or row["status"] != "sampled" or row["attempted_at"] != attempted:
                cursor.rowcount = 0
            else:
                row.update(
                    status=status,
                    advertiser=advertiser,
                    campaign=campaign,
                    confidence=confidence,
                    evidence=evidence.obj if evidence else None,
                )
        elif "INSERT INTO vl_campaigns" in sql:
            if self.fail_insert:
                raise RuntimeError("private DB error")
            self.campaigns.setdefault(
                params, dict(created_by="case_discovery@system", created_via="case_discovery")
            )
        else:
            return super().execute(sql, params)
        return cursor


def engine(raw: dict[str, Any] | str | None = None) -> discovery.DiscoveryInference:
    return discovery.DiscoveryInference(
        bedrock(
            raw
            if isinstance(raw, str)
            else json.dumps(RAW if raw is None else raw, ensure_ascii=False)
        ),
        max_cost_usd=0.2,
    )


@pytest.mark.parametrize(
    "changes,status,advertiser",
    [
        ({}, "registered", "東洋水産"),
        ({"campaign": "字幕の秘密"}, "candidate", "東洋水産"),
        ({"evidence": ["存在しない本文"]}, "candidate", "東洋水産"),
        ({"evidence": ["   "]}, "candidate", "東洋水産"),
        ({"evidence": []}, "candidate", "東洋水産"),
        ({"confidence": 0.749}, "candidate", "東洋水産"),
        ({"confidence": 0.75}, "registered", "東洋水産"),
        ({"advertiser_source": "knowledge", "confidence": 0.849}, "registered", "QTTA"),
        (
            {"advertiser_source": "knowledge", "advertiser": None, "confidence": 0.8},
            "registered",
            "QTTA",
        ),
        ({"advertiser_source": "knowledge", "confidence": 0.85}, "registered", "東洋水産"),
        ({"advertiser_source": "knowledge", "confidence": 0.7}, "candidate", "QTTA"),
        (
            {
                "decision": "unknown",
                "advertiser": None,
                "campaign": None,
                "advertiser_source": None,
                "confidence": 0,
                "evidence": [],
            },
            "unidentifiable",
            None,
        ),
        ({"decision": "existing"}, "candidate", "東洋水産"),
        ({"advertiser": "架空の会社"}, "candidate", "架空の会社"),
        ({"advertiser": "東洋\n水産"}, "candidate", "東洋\n水産"),
        ({"campaign": "QTTA\x00"}, "candidate", "東洋水産"),
        ({"campaign": "QTTA\n"}, "candidate", "東洋水産"),
        ({"campaign": "x" * 81}, "candidate", "東洋水産"),
        ({"advertiser": " 東洋水産 ", "campaign": " QTTA "}, "registered", "東洋水産"),
    ],
)
def test_decision_grounding_names_threshold_and_knowledge(
    changes: dict[str, Any],
    status: str,
    advertiser: str | None,
) -> None:
    pg = DecisionPg()
    with structlog.testing.capture_logs() as logs:
        result = discovery.decide(pg, engine(RAW | changes), request_id="decision")  # type: ignore[arg-type]
    assert result.counts == {status: 1}
    assert pg.rows["qtta"]["status"] == status and pg.rows["qtta"]["advertiser"] == advertiser
    assert len(pg.campaigns) == int(status == "registered")
    assert result.video_ids == ({str(i) for i in range(1, 11)} if status == "registered" else set())
    if status == "registered":
        assert next(iter(pg.campaigns.values())) == dict(
            created_by="case_discovery@system", created_via="case_discovery"
        )
    assert "秘密" not in repr(logs) and "東洋水産" not in repr(logs) and "QTTA" not in repr(logs)


def test_json_fence_existing_case_and_cached_prompt_input() -> None:
    pg = DecisionPg()
    pg.campaigns["東洋水産", "QTTA"] = {}
    inference = engine("```json\n" + json.dumps(RAW | {"decision": "existing"}) + "\n```")
    result = discovery.decide(pg, inference, request_id="existing")  # type: ignore[arg-type]
    assert result.counts == {"existing": 1} and not result.video_ids and len(pg.campaigns) == 1
    kwargs = inference.client.converse.call_args.kwargs
    assert kwargs["temperature"] == 0.1 and kwargs["cache_system"] and kwargs["max_attempts"] == 1
    data = json.loads(kwargs["messages"][0]["content"][0]["text"])
    assert data["existing_cases"] == [dict(advertiser="東洋水産", campaign="QTTA")]
    assert data["samples"][0]["caption"] == CAPTION
    assert "advertiser_source" in kwargs["system"]
    # A second pass cannot decide a terminal row again.
    assert discovery.decide(pg, inference, request_id="repeat").counts == {}  # type: ignore[arg-type]
    assert inference.client.converse.call_count == 1


@pytest.mark.parametrize(
    "failure",
    [
        "{broken private JSON",
        "```json\n{}\n```",
        json.dumps(RAW | {"confidence": 2}),
        TimeoutError("private caption"),
    ],
)
def test_broken_or_failed_bedrock_response_is_failed_without_raw_logs(
    failure: str | Exception,
) -> None:
    pg = DecisionPg()
    inference = engine(failure if isinstance(failure, str) else None)
    if isinstance(failure, Exception):
        inference.client.converse.side_effect = failure
    with structlog.testing.capture_logs() as logs:
        result = discovery.decide(pg, inference, request_id="failure")  # type: ignore[arg-type]
    assert result.counts == {"failed": 1} and not pg.campaigns
    assert "private" not in repr(logs) and "秘密" not in repr(logs)
    assert inference.reserved_usd > 0


def test_evidence_may_use_subtitles_but_campaign_must_be_in_caption() -> None:
    pg = DecisionPg()
    inference = engine(RAW | {"evidence": ["字幕の秘密"]})
    assert discovery.decide(pg, inference, request_id="subtitle").counts == {"registered": 1}  # type: ignore[arg-type]


def test_normalized_grounding_matches_existing_inference() -> None:
    pg = DecisionPg()
    pg.rows["qtta"]["samples"][0]["caption"] = "東洋水産 ｑ ｔ ｔ ａ #PR"
    assert discovery.decide(
        pg, engine(RAW | {"evidence": ["ｑｔｔａ"]}), request_id="normalized"
    ).counts == {"registered": 1}  # type: ignore[arg-type]


def test_budget_stops_before_call_and_limits_tags() -> None:
    pg = DecisionPg()
    pg.rows["other"] = dict(pg.rows["qtta"], tag="other")
    inference = engine()
    inference.max_cost_usd = 0.000001
    assert discovery.decide(pg, inference, request_id="budget").counts == {"budget_stop": 1}  # type: ignore[arg-type]
    inference.client.converse.assert_not_called()
    assert all(row["status"] == "sampled" for row in pg.rows.values())
    inference.max_cost_usd = 0.2
    assert discovery.decide(pg, inference, request_id="limit", max_tags=1).counts == {
        "registered": 1
    }  # type: ignore[arg-type]
    assert pg.rows["other"]["status"] == "sampled"


def test_registration_and_status_are_atomic_and_stale_rows_are_not_registered() -> None:
    pg = DecisionPg()
    pg.fail_insert = True
    with pytest.raises(RuntimeError, match="private DB error"):
        discovery.decide(pg, engine(), request_id="rollback")  # type: ignore[arg-type]
    assert pg.rows["qtta"]["status"] == "sampled" and not pg.campaigns
    pg.fail_insert, pg.stale = False, True
    assert discovery.decide(pg, engine(), request_id="stale").counts == {"stale": 1}  # type: ignore[arg-type]
    assert not pg.campaigns


@pytest.fixture
def enabled_main(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setenv("VIDEO_LIBRARY_CASE_DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("APIFY_API_TOKEN", "fake")
    monkeypatch.setattr(discovery, "configure_logging", lambda: None)
    monkeypatch.setattr(discovery.BedrockClient, "credentials_available", lambda: True)
    calls = MagicMock()
    calls.bedrock.return_value = bedrock()
    calls.collect.return_value = discovery.CollectResult()
    calls.decide.return_value = discovery.DecisionResult(
        video_ids={"1", "2"}, counts={"registered": 1}
    )
    calls.infer.return_value = {}
    monkeypatch.setattr(discovery.PgVectorClient, "from_env", calls.pg)
    monkeypatch.setattr(discovery.BedrockClient, "from_env", calls.bedrock)
    monkeypatch.setattr(discovery.ApifyClient, "from_env", calls.apify)
    monkeypatch.setattr(discovery, "collect", calls.collect)
    monkeypatch.setattr(discovery, "decide", calls.decide)
    monkeypatch.setattr(discovery, "infer_cases", calls.infer)
    return calls


def test_main_order_separate_attach_budget_and_filtered_inference(
    enabled_main: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections import Counter

    calls = enabled_main
    calls.infer.return_value = Counter(applied=2)
    monkeypatch.setenv("VIDEO_LIBRARY_CASE_MODEL", DEFAULT_MODEL)
    monkeypatch.setenv("VIDEO_LIBRARY_CASE_DISCOVERY_HAIKU_MAX_USD", "0.12")
    assert discovery.main([]) == 0
    names = [call[0] for call in calls.mock_calls]
    assert names.index("collect") < names.index("decide") < names.index("infer")
    calls.bedrock.assert_called_once_with(model_id_override=DEFAULT_MODEL)
    inference = calls.decide.call_args.args[1]
    assert inference.max_cost_usd == 0.12
    # Same-night attach must not share the small discovery cap (reservation is ~7x actual).
    attach = calls.infer.call_args.args[1]
    assert attach is not inference and attach.client is inference.client
    assert attach.max_cost_usd == 2.0
    kwargs = calls.infer.call_args.kwargs
    assert kwargs["video_ids"] == {"1", "2"} and kwargs["limit"] == 200 and not kwargs["dry_run"]
    assert kwargs["policy"] is not None
    calls.pg.return_value.close.assert_called_once()


@pytest.mark.parametrize(
    "reason", ["disabled", "apify_unconfigured", "bedrock_unconfigured", "haiku_budget_zero"]
)
def test_main_skip_is_free_and_logs_reason(
    enabled_main: MagicMock, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    if reason == "disabled":
        monkeypatch.setenv("VIDEO_LIBRARY_CASE_DISCOVERY_ENABLED", "0")
    elif reason == "apify_unconfigured":
        monkeypatch.delenv("APIFY_API_TOKEN")
    elif reason == "bedrock_unconfigured":
        monkeypatch.setattr(discovery.BedrockClient, "credentials_available", lambda: False)
    else:
        monkeypatch.setenv("VIDEO_LIBRARY_CASE_DISCOVERY_HAIKU_MAX_USD", "0")
    with structlog.testing.capture_logs() as logs:
        assert discovery.main([]) == 0
    assert logs[0]["reason"] == reason
    assert not enabled_main.mock_calls


def test_code_defaults_to_enabled(enabled_main: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_LIBRARY_CASE_DISCOVERY_ENABLED")
    enabled_main.decide.return_value = discovery.DecisionResult()
    assert discovery.main([]) == 0
    enabled_main.collect.assert_called_once()
    enabled_main.infer.assert_not_called()


@pytest.mark.parametrize("credentials", [None, object(), NoCredentialsError()])
def test_bedrock_availability_uses_aws_chain_not_a_required_api_key(
    monkeypatch: pytest.MonkeyPatch, credentials: Any
) -> None:
    session = MagicMock()
    monkeypatch.setattr(bedrock_client.boto3, "Session", lambda: session)
    if isinstance(credentials, Exception):
        session.get_credentials.side_effect = credentials
    else:
        session.get_credentials.return_value = credentials
    assert discovery.BedrockClient.credentials_available() is (
        credentials is not None and not isinstance(credentials, Exception)
    )


def test_migration_fixture_isolates_0044_schema_and_role() -> None:
    path = Path(__file__).resolve().parents[2] / "infra/migrations/0044_library_mcp_oauth.sql"
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"name": "vl_test_sample"}
    apply_video_library_migration(conn, path)
    sql = conn.execute.call_args.args[0]
    assert "CREATE TABLE vl_test_sample.library_mcp_clients" in sql
    assert "public." not in sql and "SCHEMA public" not in sql and "library_mcp_definer" not in sql
    assert "CREATE ROLE mcp_owner_vl_test_sample" in sql
