"""CAS expressions and the checkpoint attribute survive a DynamoDB round trip."""

from __future__ import annotations

from typing import Any

from teamagent.adapters.proposal_job_store import ProposalJobStore
from tests.adapters.test_proposal_job_store import _FakeDynamo


class ParentDynamo(_FakeDynamo):
    @staticmethod
    def _condition_matches(item: dict[str, dict[str, str]], kwargs: dict[str, Any]) -> bool:
        names, values = kwargs["ExpressionAttributeNames"], kwargs["ExpressionAttributeValues"]
        if "#checkpoint" not in names:
            return _FakeDynamo._condition_matches(item, kwargs)
        condition = kwargs["ConditionExpression"]
        checkpoint = item.get("checkpoint_json")
        if "attribute_not_exists(#checkpoint)" in condition and checkpoint is not None:
            return False
        for placeholder in (":checkpoint", ":previous"):
            if f"#checkpoint = {placeholder}" in condition and checkpoint != values[placeholder]:
                return False
        if (
            "#updated_at = :previous_time" in condition
            and item.get("updated_at") != values[":previous_time"]
        ):
            return False
        if ":queued" in values:
            return item["status"] in (values[":queued"], values[":running"])
        return _FakeDynamo._condition_matches(item, kwargs)


def test_checkpoint_fences_old_worker_with_dynamo_conditional_updates() -> None:
    dynamo = ParentDynamo()
    store = ProposalJobStore(table_name="fake-table", dynamodb_client=dynamo)
    store.create_job("omy_fake", {"edition": "sales25"})
    row = store.get_job("omy_fake")
    assert row is not None
    assert not store.save_checkpoint(
        "omy_fake", "stale", expected_checkpoint=None, expected_updated_at="wrong"
    )
    assert store.save_checkpoint(
        "omy_fake", "first", expected_checkpoint=None, expected_updated_at=row["updated_at"]
    )
    assert not store.save_checkpoint("omy_fake", "duplicate", expected_checkpoint=None)
    assert store.get_job("omy_fake")["checkpoint_json"] == "first"  # type: ignore[index]
    assert store.heartbeat("omy_fake", expected_checkpoint="first")
    assert store.save_checkpoint("omy_fake", "new-lease", expected_checkpoint="first")
    assert not store.heartbeat("omy_fake", expected_checkpoint="first")
    assert not store.mark_done("omy_fake", "{}", expected_checkpoint="first")
    assert store.mark_done("omy_fake", "{}", expected_checkpoint="new-lease")
    assert not store.save_checkpoint("omy_fake", "late", expected_checkpoint="new-lease")
    assert store.get_job("omy_fake")["checkpoint_json"] == "new-lease"  # type: ignore[index]
