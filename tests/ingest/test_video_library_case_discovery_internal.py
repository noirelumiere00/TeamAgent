"""Internal-client voting, safe lookup and fallback without production connections."""

from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog.testing

from teamagent.ingest import video_library_case_discovery as discovery
from teamagent.ingest.video_library_case_models import Case
from tests.ingest.test_video_library_case_discovery_decision import RAW, DecisionPg, engine


def match(name: str, count: int) -> discovery.InternalClient:
    return discovery.InternalClient(client_name=name, document_count=count)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("株式会社OVER's", "OVER's"),
        (" OVER's株式会社 ", "OVER's"),
        ("（株） ＯＶＥＲ＇ｓ ㈱", "OVER's"),
        ("㈱東洋水産", "東洋水産"),
        ("東洋水産(株)", "東洋水産"),
        ("株式会社 株式会社 東洋水産", "東洋水産"),
        ("架空株式会社研究所", "架空株式会社研究所"),
        ("東洋 水産", "東洋 水産"),
        ("東洋水産様", "東洋水産様"),
        ("合同会社東洋水産", "合同会社東洋水産"),
        ("株式会社", ""),
        (" ", ""),
    ],
)
def test_normalizes_only_requested_affixes_at_name_boundaries(name: str, expected: str) -> None:
    assert discovery.internal_client_key(name) == expected


@pytest.mark.parametrize(
    "matches,expected",
    [
        ([], None),
        ([match("東洋水産", 1)], None),
        ([match("東洋水産", 2)], "東洋水産"),
        ([match("東洋水産", 2), match("別社", 1)], "東洋水産"),
        ([match("東洋水産", 3), match("別社", 2)], None),
        ([match("東洋水産", 2), match("別社", 2)], None),
        ([match("株式会社東洋水産", 1), match("東洋水産㈱", 1)], "東洋水産"),
        ([match("㈱東洋水産", 2), match("東洋水産", 2), match("別社", 2)], "東洋水産"),
        ([match("株式会社", 10), match("別社", 1)], None),
    ],
)
def test_normalized_counts_require_two_documents_and_twice_runner_up(
    matches: list[discovery.InternalClient],
    expected: str | None,
) -> None:
    assert discovery.select_internal_advertiser(matches, []) == expected


def test_existing_advertiser_spelling_wins_after_normalization() -> None:
    assert (
        discovery.select_internal_advertiser(
            [match("東洋水産", 4)],
            [Case(advertiser="東洋水産株式会社", campaign="別の施策")],
        )
        == "東洋水産株式会社"
    )


def test_new_advertiser_uses_the_short_name_independently_of_input_order() -> None:
    # The ledger writes companies without 株式会社 (東洋水産・OVER's); new cases follow it.
    matches = [match("株式会社東洋水産", 3), match("東洋水産", 2)]
    assert discovery.select_internal_advertiser(matches, []) == "東洋水産"
    assert discovery.select_internal_advertiser(list(reversed(matches)), []) == "東洋水産"


def test_lookup_uses_one_parameterized_query_and_company_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", " Example.TEST , second.test ")
    pg, conn = MagicMock(), MagicMock()
    pg.connection.return_value.__enter__.return_value = conn
    conn.execute.return_value.fetchall.return_value = [dict(client_name="別社", document_count=3)]
    result = discovery.lookup_internal_clients(
        pg, "50%_Q\\R' OR true --", "tag_1", request_id="safe"
    )
    assert result == [match("別社", 3)]
    pg.connection.assert_called_once_with(
        app_role="teamagent_app",
        user_email=None,
        user_role="admin",
        user_groups=["example.test", "second.test"],
    )
    conn.execute.assert_called_once()
    sql, params = conn.execute.call_args.args
    assert params == (["%50\\%\\_Q\\\\R' OR true --%", "%tag\\_1%"],)
    assert " OR true --" not in sql
    assert "x_research_tool' IS NULL" in sql and "LIMIT 5" in sql
    assert "c.content ILIKE pattern" in sql and "d.title ILIKE pattern" in sql
    assert "ESCAPE '\\'" in sql


def test_blank_terms_never_query() -> None:
    pg = MagicMock()
    assert discovery.lookup_internal_clients(pg, None, " ", request_id="blank") == []
    assert not pg.mock_calls


def test_internal_override_keeps_evidence_and_hides_names_from_logs() -> None:
    pg = DecisionPg()
    pg.internal_clients = [
        m.model_dump()
        for m in (match("株式会社別社秘密", 3), match("別社秘密㈱", 1), match("競合秘密", 2))
    ]
    pg.campaigns["別社秘密", "別の施策"] = {}
    with structlog.testing.capture_logs() as logs:
        result = discovery.decide(
            pg,
            engine(RAW | {"advertiser_source": "knowledge", "confidence": 0.8}),
            request_id="internal",
        )  # type: ignore[arg-type]
    assert result.counts == {"registered": 1}
    row = pg.rows["qtta"]
    assert row["advertiser"] == "別社秘密"
    assert row["evidence"] == {"quotes": RAW["evidence"], "internal_clients": pg.internal_clients}
    assert pg.internal_queries == 1
    assert ("別社秘密", "QTTA") in pg.campaigns
    decided = [log for log in logs if log["event"] == "case_discovery_decided"]
    assert decided[0]["advertiser_source"] == "internal"
    assert "秘密" not in repr(logs) and "https://" not in repr(logs)


@pytest.mark.parametrize(
    "matches,failure",
    [
        ([], False),
        ([match("別社", 1)], False),
        ([match("別社", 3), match("別会社", 2)], False),
        ([], True),
    ],
)
@pytest.mark.parametrize("source,expected", [("text", "東洋水産"), ("knowledge", "QTTA")])
def test_inconclusive_or_failed_lookup_keeps_existing_rules(
    matches: list[discovery.InternalClient],
    failure: bool,
    source: str,
    expected: str,
) -> None:
    pg = DecisionPg()
    pg.internal_clients = [m.model_dump() for m in matches]
    pg.internal_failure = failure
    with structlog.testing.capture_logs() as logs:
        result = discovery.decide(
            pg,
            engine(RAW | {"advertiser_source": source, "confidence": 0.8}),
            request_id="fallback",
        )  # type: ignore[arg-type]
    assert result.counts == {"registered": 1} and pg.internal_queries == 1
    assert pg.rows["qtta"]["advertiser"] == expected
    assert pg.rows["qtta"]["evidence"] == RAW["evidence"]
    assert "private" not in repr(logs)
    assert [
        log["advertiser_source"] for log in logs if log["event"] == "case_discovery_decided"
    ] == [source]


def test_haiku_cannot_claim_internal_source_without_documents() -> None:
    pg = DecisionPg()
    assert discovery.decide(
        pg, engine(RAW | {"advertiser_source": "internal"}), request_id="forged"
    ).counts == {"failed": 1}  # type: ignore[arg-type]
    assert not pg.campaigns and pg.internal_queries == 0


def test_internal_existing_pair_uses_catalog_spelling_without_registration() -> None:
    pg = DecisionPg()
    pg.internal_clients = [match("東洋水産", 3).model_dump()]
    pg.campaigns["東洋水産株式会社", "QTTA"] = {}
    assert discovery.decide(
        pg, engine(RAW | {"decision": "existing"}), request_id="existing"
    ).counts == {"existing": 1}  # type: ignore[arg-type]
    assert len(pg.campaigns) == 1


@pytest.mark.parametrize(
    "changes",
    [{"campaign": "存在しない施策"}, {"evidence": ["存在しない根拠"]}, {"confidence": 0.74}],
)
def test_internal_source_does_not_bypass_other_registration_guards(changes: dict[str, Any]) -> None:
    pg = DecisionPg()
    pg.internal_clients = [match("本文にない会社", 3).model_dump()]
    assert discovery.decide(pg, engine(RAW | changes), request_id="guard").counts == {
        "candidate": 1
    }  # type: ignore[arg-type]
    assert not pg.campaigns
