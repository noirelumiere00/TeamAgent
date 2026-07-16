"""scripts/sync_gdrive_acl.py の plan/guard/write-zero 契約テスト。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from teamagent.adapters.gdrive_client import DrivePermission
from teamagent.ingest.repository import GDriveAclSnapshot, GDriveAclUpdate

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "sync_gdrive_acl", _ROOT / "scripts" / "sync_gdrive_acl.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["sync_gdrive_acl"] = _mod
_spec.loader.exec_module(_mod)


COMPANY = "company.example"


class _FakeRepository:
    def __init__(self, snapshots: list[GDriveAclSnapshot]) -> None:
        self.snapshots = snapshots
        self.update_calls: list[list[GDriveAclUpdate]] = []

    def list_nonstale_gdrive_acl_snapshot(self) -> list[GDriveAclSnapshot]:
        return self.snapshots

    def update_gdrive_acls(self, updates: list[GDriveAclUpdate]) -> int:
        self.update_calls.append(updates)
        return len(updates)


class _FakeClient:
    def __init__(
        self,
        permissions: dict[str, list[DrivePermission]],
        *,
        fail_on: str | None = None,
        fail_status: int | None = None,
    ) -> None:
        self.permissions = permissions
        self.fail_on = fail_on
        self.fail_status = fail_status
        self.calls: list[tuple[str, int, int]] = []

    def list_permissions(
        self,
        file_id: str,
        request_id: str,
        *,
        max_pages: int,
        api_retries: int,
    ) -> list[DrivePermission]:
        self.calls.append((file_id, max_pages, api_retries))
        if file_id == self.fail_on:
            if self.fail_status is not None:
                response = SimpleNamespace(status=self.fail_status, reason="sensitive reason")
                raise HttpError(
                    response,
                    b'{"error":{"message":"sensitive permission failure"}}',
                    uri=f"https://sensitive.example/files/{file_id}",
                )
            raise RuntimeError(f"sensitive URL for {file_id}")
        return self.permissions[file_id]


def _snapshot(
    index: int,
    *,
    company: bool = False,
    extra_reader: bool = False,
) -> GDriveAclSnapshot:
    owner = f"person{index}@secret.example"
    acl_emails = (owner, f"reader{index}@secret.example") if extra_reader else (owner,)
    return GDriveAclSnapshot(
        document_id=f"00000000-0000-0000-0000-{index:012d}",
        external_id=f"drive-secret-{index}",
        owner_email=owner,
        acl_emails=acl_emails,
        acl_groups=(COMPANY,) if company else (),
        row_version=str(1000 + index),
    )


def _permissions(
    index: int, *, company: bool = False, owner_suffix: str = ""
) -> list[DrivePermission]:
    owner = f"person{index}{owner_suffix}@secret.example"
    permissions = [
        DrivePermission(
            id=f"p-{index}",
            type="user",
            role="owner",
            email_address=owner,
        )
    ]
    if company:
        permissions.append(
            DrivePermission(
                id=f"domain-{index}",
                type="domain",
                role="reader",
                domain=COMPANY,
            )
        )
    return permissions


def _client_for(
    snapshots: list[GDriveAclSnapshot],
    *,
    company_indexes: set[int] | None = None,
    changed_indexes: set[int] | None = None,
) -> _FakeClient:
    company_indexes = company_indexes or set()
    changed_indexes = changed_indexes or set()
    return _FakeClient(
        {
            snapshot.external_id: _permissions(
                index,
                company=index in company_indexes,
                owner_suffix="-new" if index in changed_indexes else "",
            )
            for index, snapshot in enumerate(snapshots, start=1)
        }
    )


def test_default_dry_run_writes_zero_and_summary_contains_no_pii() -> None:
    snapshots = [_snapshot(1, company=True), _snapshot(2)]
    repository = _FakeRepository(snapshots)
    client = _client_for(snapshots, company_indexes={1}, changed_indexes={2})

    result = _mod.synchronize_gdrive_acl(
        repository,
        client,
        company_domain=COMPANY,
        request_id="test-request",
    )

    assert result.committed is False
    assert result.updated_count == 0
    assert result.plan.target_count == 2
    assert result.plan.changed_count == 1
    assert repository.update_calls == []
    summary = _mod.summary_line(result)
    assert "target=2" in summary and "changed=1" in summary
    assert len(result.plan.plan_sha256) == 64
    assert "secret.example" not in summary
    assert "drive-secret" not in summary


def test_api_failure_after_partial_collection_is_write_zero_and_pii_safe() -> None:
    snapshots = [_snapshot(1), _snapshot(2)]
    repository = _FakeRepository(snapshots)
    client = _client_for(snapshots)
    client.fail_on = snapshots[1].external_id

    with pytest.raises(_mod.PermissionCollectionError) as caught:
        _mod.synchronize_gdrive_acl(
            repository,
            client,
            company_domain=COMPANY,
            commit=True,
            expect_plan_sha="0" * 64,
            allow_company_access_loss=True,
            allow_mass_acl_change=True,
            request_id="test-request",
        )

    assert repository.update_calls == []
    assert "drive-secret" not in str(caught.value)
    assert "secret.example" not in str(caught.value)


def test_http_404_becomes_pii_safe_owner_only_quarantine_dry_run_and_is_hashed() -> None:
    snapshots = [
        _snapshot(1),
        _snapshot(2, company=True, extra_reader=True),
        _snapshot(3),
    ]
    repository = _FakeRepository(snapshots)
    client = _client_for(snapshots)
    client.fail_on = snapshots[1].external_id
    client.fail_status = 404

    result = _mod.synchronize_gdrive_acl(
        repository,
        client,
        company_domain=COMPANY,
        request_id="test-request",
    )

    assert result.committed is False
    assert result.updated_count == 0
    assert repository.update_calls == []
    # 途中の 404 で止めず、後続も列挙して完全な plan を作る。
    assert [call[0] for call in client.calls] == [
        snapshot.external_id for snapshot in snapshots
    ]
    item = result.plan.items[1]
    assert item.unreachable is True
    assert item.after_owner == item.before_owner
    assert item.after_emails == (item.before_owner,)
    assert item.after_groups == ()
    assert result.plan.unreachable_count == 1
    assert result.plan.requires_unreachable_override is True

    summary = _mod.summary_line(result)
    warnings = "\n".join(_mod.warning_lines(result.plan))
    assert "unreachable=1" in summary
    assert "requires_unreachable_override=true" in summary
    assert "unreachable=1" in warnings
    for text in (summary, warnings):
        assert "secret.example" not in text
        assert "drive-secret" not in text

    # before/after が同じ plan でも unreachable 判定そのものが SHA に束縛される。
    reachable = _mod.synchronize_gdrive_acl(
        _FakeRepository(snapshots),
        _client_for(snapshots),
        company_domain=COMPANY,
        request_id="test-request",
    )
    assert reachable.plan.items[1].after_emails == item.after_emails
    assert reachable.plan.items[1].after_groups == item.after_groups
    assert reachable.plan.plan_sha256 != result.plan.plan_sha256


def test_http_404_commit_without_unreachable_override_is_write_zero() -> None:
    snapshots = [_snapshot(1, extra_reader=True), _snapshot(2), _snapshot(3)]
    repository = _FakeRepository(snapshots)
    client = _client_for(snapshots)
    client.fail_on = snapshots[0].external_id
    client.fail_status = 404
    dry_run = _mod.synchronize_gdrive_acl(
        repository,
        client,
        company_domain=COMPANY,
        request_id="test-request",
    )

    with pytest.raises(_mod.AclSafetyGuardError, match="allow-unreachable-revoke"):
        _mod.synchronize_gdrive_acl(
            repository,
            client,
            company_domain=COMPANY,
            commit=True,
            expect_plan_sha=dry_run.plan.plan_sha256,
            allow_company_access_loss=True,
            allow_mass_acl_change=True,
            request_id="test-request",
        )

    assert repository.update_calls == []


def test_http_404_commit_requires_unreachable_and_existing_guards_then_quarantines() -> None:
    snapshots = [_snapshot(1, company=True, extra_reader=True)]
    repository = _FakeRepository(snapshots)
    client = _client_for(snapshots)
    client.fail_on = snapshots[0].external_id
    client.fail_status = 404
    dry_run = _mod.synchronize_gdrive_acl(
        repository,
        client,
        company_domain=COMPANY,
        request_id="test-request",
    )

    with pytest.raises(_mod.AclSafetyGuardError) as caught:
        _mod.synchronize_gdrive_acl(
            repository,
            client,
            company_domain=COMPANY,
            commit=True,
            expect_plan_sha=dry_run.plan.plan_sha256,
            allow_unreachable_revoke=True,
            request_id="test-request",
        )
    assert "--allow-company-access-loss" in str(caught.value)
    assert "--allow-mass-acl-change" in str(caught.value)
    assert repository.update_calls == []

    committed = _mod.synchronize_gdrive_acl(
        repository,
        client,
        company_domain=COMPANY,
        commit=True,
        expect_plan_sha=dry_run.plan.plan_sha256,
        allow_company_access_loss=True,
        allow_mass_acl_change=True,
        allow_unreachable_revoke=True,
        request_id="test-request",
    )

    assert committed.updated_count == 1
    assert len(repository.update_calls) == 1
    update = repository.update_calls[0][0]
    assert update.owner_email == snapshots[0].owner_email
    assert update.acl_emails == (snapshots[0].owner_email,)
    assert update.acl_groups == ()


@pytest.mark.parametrize("status", [403, 429, 500])
def test_non_404_http_errors_are_write_zero_even_with_all_overrides(status: int) -> None:
    snapshots = [_snapshot(1, company=True, extra_reader=True)]
    repository = _FakeRepository(snapshots)
    client = _client_for(snapshots)
    client.fail_on = snapshots[0].external_id
    client.fail_status = status

    with pytest.raises(_mod.PermissionCollectionError) as caught:
        _mod.synchronize_gdrive_acl(
            repository,
            client,
            company_domain=COMPANY,
            commit=True,
            expect_plan_sha="0" * 64,
            allow_company_access_loss=True,
            allow_mass_acl_change=True,
            allow_unreachable_revoke=True,
            request_id="test-request",
        )

    assert repository.update_calls == []
    assert "secret.example" not in str(caught.value)
    assert "drive-secret" not in str(caught.value)


def test_empty_target_cannot_be_overridden() -> None:
    repository = _FakeRepository([])
    client = _FakeClient({})
    with pytest.raises(_mod.EmptyAclTargetError, match="zero"):
        _mod.synchronize_gdrive_acl(
            repository,
            client,
            company_domain=COMPANY,
            commit=True,
            expect_plan_sha="0" * 64,
            allow_company_access_loss=True,
            allow_mass_acl_change=True,
        )
    assert repository.update_calls == []
    assert client.calls == []


def test_plan_sha_mismatch_is_write_zero_even_with_all_overrides() -> None:
    snapshots = [_snapshot(1)]
    repository = _FakeRepository(snapshots)
    client = _client_for(snapshots, changed_indexes={1})
    with pytest.raises(_mod.PlanShaMismatchError, match="SHA changed"):
        _mod.synchronize_gdrive_acl(
            repository,
            client,
            company_domain=COMPANY,
            commit=True,
            expect_plan_sha="0" * 64,
            allow_company_access_loss=True,
            allow_mass_acl_change=True,
            request_id="test-request",
        )
    assert repository.update_calls == []


def test_any_company_access_loss_requires_explicit_override() -> None:
    # 5行中1行（20%ちょうど）の company loss: >20% guard ではなく loss 1件 guard を検証。
    snapshots = [_snapshot(index, company=True) for index in range(1, 6)]
    repository = _FakeRepository(snapshots)
    client = _client_for(snapshots, company_indexes={2, 3, 4, 5})
    dry_run = _mod.synchronize_gdrive_acl(
        repository, client, company_domain=COMPANY, request_id="test-request"
    )
    assert dry_run.plan.company_loss_count == 1
    assert dry_run.plan.company_decrease_ratio == pytest.approx(0.2)

    with pytest.raises(_mod.AclSafetyGuardError, match="allow-company-access-loss"):
        _mod.synchronize_gdrive_acl(
            repository,
            client,
            company_domain=COMPANY,
            commit=True,
            expect_plan_sha=dry_run.plan.plan_sha256,
            request_id="test-request",
        )
    assert repository.update_calls == []


def test_more_than_half_changed_requires_mass_override() -> None:
    snapshots = [_snapshot(index) for index in range(1, 5)]
    repository = _FakeRepository(snapshots)
    client = _client_for(snapshots, changed_indexes={1, 2, 3})
    dry_run = _mod.synchronize_gdrive_acl(
        repository, client, company_domain=COMPANY, request_id="test-request"
    )
    assert dry_run.plan.change_ratio == pytest.approx(0.75)
    assert any("exceeds 30%" in warning for warning in _mod.warning_lines(dry_run.plan))

    with pytest.raises(_mod.AclSafetyGuardError, match="allow-mass-acl-change"):
        _mod.synchronize_gdrive_acl(
            repository,
            client,
            company_domain=COMPANY,
            commit=True,
            expect_plan_sha=dry_run.plan.plan_sha256,
            request_id="test-request",
        )
    assert repository.update_calls == []


def test_company_shared_decrease_over_20pct_requires_both_overrides_then_commits() -> None:
    snapshots = [_snapshot(index, company=True) for index in range(1, 5)]
    repository = _FakeRepository(snapshots)
    client = _client_for(snapshots, company_indexes={4})
    dry_run = _mod.synchronize_gdrive_acl(
        repository, client, company_domain=COMPANY, request_id="test-request"
    )
    assert dry_run.plan.company_decrease_ratio == pytest.approx(0.75)

    with pytest.raises(_mod.AclSafetyGuardError) as caught:
        _mod.synchronize_gdrive_acl(
            repository,
            client,
            company_domain=COMPANY,
            commit=True,
            expect_plan_sha=dry_run.plan.plan_sha256,
            request_id="test-request",
        )
    assert "--allow-company-access-loss" in str(caught.value)
    assert "--allow-mass-acl-change" in str(caught.value)
    assert repository.update_calls == []

    committed = _mod.synchronize_gdrive_acl(
        repository,
        client,
        company_domain=COMPANY,
        commit=True,
        expect_plan_sha=dry_run.plan.plan_sha256,
        allow_company_access_loss=True,
        allow_mass_acl_change=True,
        request_id="test-request",
    )
    assert committed.updated_count == 3
    assert len(repository.update_calls) == 1
    assert len(repository.update_calls[0]) == 3


def test_exactly_half_changed_does_not_require_mass_override() -> None:
    snapshots = [_snapshot(1), _snapshot(2)]
    repository = _FakeRepository(snapshots)
    client = _client_for(snapshots, changed_indexes={1})
    dry_run = _mod.synchronize_gdrive_acl(
        repository, client, company_domain=COMPANY, request_id="test-request"
    )
    committed = _mod.synchronize_gdrive_acl(
        repository,
        client,
        company_domain=COMPANY,
        commit=True,
        expect_plan_sha=dry_run.plan.plan_sha256,
        request_id="test-request",
    )
    assert committed.updated_count == 1


@pytest.mark.parametrize("expect_plan_sha", [None, "", "not-a-sha", "A" * 64])
def test_commit_requires_lowercase_sha256(expect_plan_sha: str | None) -> None:
    snapshots = [_snapshot(1)]
    plan = _mod.build_acl_plan(
        snapshots,
        _client_for(snapshots),
        company_domain=COMPANY,
        request_id="test-request",
    )
    if expect_plan_sha == "A" * 64:
        # CLI/API は入力を lower 化するため形式は有効、ただし内容 mismatch。
        with pytest.raises(_mod.PlanShaMismatchError, match="SHA changed"):
            _mod.validate_commit(
                plan,
                expect_plan_sha=expect_plan_sha,
                allow_company_access_loss=False,
                allow_mass_acl_change=False,
            )
    else:
        with pytest.raises(_mod.PlanShaMismatchError, match="valid"):
            _mod.validate_commit(
                plan,
                expect_plan_sha=expect_plan_sha,
                allow_company_access_loss=False,
                allow_mass_acl_change=False,
            )
