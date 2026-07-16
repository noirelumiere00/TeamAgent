"""既存 non-stale Google Drive documents の ACL 3 列だけを安全に再同期する。

本文を再 download / chunk / embed せず、Drive ``permissions.list`` の全ページを取得して
``owner_email`` / ``acl_emails`` / ``acl_groups`` だけを更新する。本番用の安全契約:

- 既定 dry-run。書込みには ``--commit --expect-plan-sha <dry-runのSHA>`` が必須。
- permissions.list の HTTP 404 は、保存済み owner だけに ACL を縮小する隔離候補として
  plan に残す。commit には ``--allow-unreachable-revoke`` が必須。
- 404 以外の API 失敗・permissions ページ打切り・対象 0 件・plan SHA 不一致は
  override 不可で write 0。
- company-domain access loss は ``--allow-company-access-loss`` が無い限り commit を拒否。
- ACL 変更率 >50% は ``--allow-mass-acl-change`` が無い限り commit を拒否（>30% は警告）。
- company 共有行が >20% 減る場合は上記 2 override の両方が必要。
- DB 書込みは repository の単一 transaction + xmin 楽観 lock。本文/chunks/metadata/stale/
  modified_at/ingested_at は SELECT/SET しない（metadata は stale の WHERE 判定だけ）。
- 標準出力・エラーは件数と SHA だけ。email、title、file ID は表示しない。

Usage (SSM tunnel と DATABASE_URL / Google OAuth env を設定後):
    uv run --extra dev python scripts/sync_gdrive_acl.py
    uv run --extra dev python scripts/sync_gdrive_acl.py \
      --commit --expect-plan-sha <dry-runで表示されたSHA>
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from googleapiclient.errors import HttpError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from teamagent.adapters.gdrive_client import (  # noqa: E402
    DEFAULT_GOOGLE_API_RETRIES,
    DEFAULT_PERMISSIONS_MAX_PAGES,
    DrivePermission,
    GDriveClient,
    extract_acl_emails,
)
from teamagent.ingest.repository import (  # noqa: E402
    GDriveAclSnapshot,
    GDriveAclUpdate,
    IngestRepository,
)

MASS_CHANGE_WARNING_RATIO = 0.30
MASS_CHANGE_BLOCK_RATIO = 0.50
COMPANY_SHARED_DECREASE_BLOCK_RATIO = 0.20
_PLAN_SCHEMA = "teamagent-gdrive-acl-plan-v2"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class _AclRepository(Protocol):
    def list_nonstale_gdrive_acl_snapshot(self) -> list[GDriveAclSnapshot]: ...

    def update_gdrive_acls(self, updates: list[GDriveAclUpdate]) -> int: ...


class _PermissionClient(Protocol):
    def list_permissions(
        self,
        file_id: str,
        request_id: str,
        *,
        max_pages: int,
        api_retries: int,
    ) -> list[DrivePermission]: ...


class AclSyncError(RuntimeError):
    """利用者へ安全に表示できる（PII を含まない）ACL 同期エラー。"""


class EmptyAclTargetError(AclSyncError):
    """同期対象が 0 件。誤接続や誤 filter の可能性があるため続行不可。"""


class PermissionCollectionError(AclSyncError):
    """全対象の permissions を完全取得できず、計画を破棄した。"""


class PlanShaMismatchError(AclSyncError):
    """dry-run 後に DB または Drive ACL が変化した。"""


class AclSafetyGuardError(AclSyncError):
    """明示 override の無い危険な ACL 縮小/大量変更。"""


@dataclass(frozen=True)
class AclPlanItem:
    snapshot: GDriveAclSnapshot
    before_owner: str
    before_emails: tuple[str, ...]
    before_groups: tuple[str, ...]
    after_owner: str
    after_emails: tuple[str, ...]
    after_groups: tuple[str, ...]
    # permissions.list が HTTP 404。削除済み/アクセス剥奪のどちらかは
    # 区別できないため、識別子を出さず owner-only 隔離候補として扱う。
    unreachable: bool = False

    @property
    def changed(self) -> bool:
        return (
            self.before_owner,
            self.before_emails,
            self.before_groups,
        ) != (
            self.after_owner,
            self.after_emails,
            self.after_groups,
        )


@dataclass(frozen=True)
class AclSyncPlan:
    items: tuple[AclPlanItem, ...]
    company_domain: str
    plan_sha256: str

    @property
    def target_count(self) -> int:
        return len(self.items)

    @property
    def changed_count(self) -> int:
        return sum(item.changed for item in self.items)

    @property
    def unreachable_count(self) -> int:
        return sum(item.unreachable for item in self.items)

    @property
    def change_ratio(self) -> float:
        return self.changed_count / self.target_count

    @property
    def company_before_count(self) -> int:
        return sum(self.company_domain in item.before_groups for item in self.items)

    @property
    def company_after_count(self) -> int:
        return sum(self.company_domain in item.after_groups for item in self.items)

    @property
    def company_loss_count(self) -> int:
        return sum(
            self.company_domain in item.before_groups
            and self.company_domain not in item.after_groups
            for item in self.items
        )

    @property
    def company_decrease_ratio(self) -> float:
        before = self.company_before_count
        if before == 0:
            return 0.0
        return max(0, before - self.company_after_count) / before

    @property
    def requires_company_override(self) -> bool:
        return (
            self.company_loss_count > 0
            or self.company_decrease_ratio > COMPANY_SHARED_DECREASE_BLOCK_RATIO
        )

    @property
    def requires_mass_override(self) -> bool:
        return (
            self.change_ratio > MASS_CHANGE_BLOCK_RATIO
            or self.company_decrease_ratio > COMPANY_SHARED_DECREASE_BLOCK_RATIO
        )

    @property
    def requires_unreachable_override(self) -> bool:
        return self.unreachable_count > 0

    def updates(self) -> list[GDriveAclUpdate]:
        return [
            GDriveAclUpdate(
                document_id=item.snapshot.document_id,
                external_id=item.snapshot.external_id,
                expected_row_version=item.snapshot.row_version,
                owner_email=item.after_owner,
                acl_emails=item.after_emails,
                acl_groups=item.after_groups,
            )
            for item in self.items
            if item.changed
        ]


@dataclass(frozen=True)
class AclSyncResult:
    plan: AclSyncPlan
    updated_count: int
    committed: bool


def _normalize_one(value: str) -> str:
    return value.strip().lower()


def _normalize_many(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(sorted({_normalize_one(value) for value in values if value.strip()}))


def _target_acl(
    snapshot: GDriveAclSnapshot,
    permissions: list[DrivePermission],
    *,
    company_domain: str,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    owner = _normalize_one(snapshot.owner_email)
    for permission in permissions:
        if (
            permission.role == "owner"
            and permission.type == "user"
            and permission.email_address
            and not permission.deleted
        ):
            owner = _normalize_one(permission.email_address)
            break
    if not owner:
        raise PermissionCollectionError("permissions produced an empty owner; no rows updated")

    emails, groups = extract_acl_emails(permissions, workspace_domain=company_domain)
    after_emails = _normalize_many([*emails, owner])
    after_groups = _normalize_many(groups)
    return owner, after_emails, after_groups


def _is_http_not_found(exc: Exception) -> bool:
    """Google API の本物の HTTP 404 だけを隔離候補として識別する。"""
    if not isinstance(exc, HttpError):
        return False
    status: object = getattr(exc.resp, "status", None)
    return status in (404, "404")


def _unreachable_target_acl(
    snapshot: GDriveAclSnapshot,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """404 の項目を保存済み owner-only に縮小する。"""
    owner = _normalize_one(snapshot.owner_email)
    if not owner:
        raise PermissionCollectionError(
            "unreachable item has an empty stored owner; no rows updated"
        )
    return owner, (owner,), ()


def _plan_digest(items: list[AclPlanItem], *, company_domain: str) -> str:
    """対象・xmin・before/after ACL を束ねた再現可能 SHA（中身は出力しない）。"""
    payload = {
        "schema": _PLAN_SCHEMA,
        "company_domain": company_domain,
        "items": [
            {
                "document_id": item.snapshot.document_id,
                "external_id": item.snapshot.external_id,
                "row_version": item.snapshot.row_version,
                "unreachable": item.unreachable,
                "before": {
                    "owner_email": item.before_owner,
                    "acl_emails": item.before_emails,
                    "acl_groups": item.before_groups,
                },
                "after": {
                    "owner_email": item.after_owner,
                    "acl_emails": item.after_emails,
                    "acl_groups": item.after_groups,
                },
            }
            for item in items
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_acl_plan(
    snapshots: list[GDriveAclSnapshot],
    client: _PermissionClient,
    *,
    company_domain: str,
    request_id: str,
    max_permission_pages: int = DEFAULT_PERMISSIONS_MAX_PAGES,
    api_retries: int = DEFAULT_GOOGLE_API_RETRIES,
) -> AclSyncPlan:
    """permissions が揃うか HTTP 404 を隔離候補化できた場合だけ計画を返す。"""
    normalized_domain = _normalize_one(company_domain)
    if not normalized_domain:
        raise ValueError("company_domain must not be empty")
    if not snapshots:
        raise EmptyAclTargetError("gdrive ACL target count is zero; no rows updated")

    items: list[AclPlanItem] = []
    for snapshot in snapshots:
        unreachable = False
        try:
            permissions = client.list_permissions(
                file_id=snapshot.external_id,
                request_id=request_id,
                max_pages=max_permission_pages,
                api_retries=api_retries,
            )
        except Exception as exc:
            if _is_http_not_found(exc):
                after_owner, after_emails, after_groups = _unreachable_target_acl(snapshot)
                unreachable = True
            else:
                # Google の例外文字列には URL/file ID が入ることがあるため連鎖だけ保持し、
                # 利用者向け message は件数のみの固定文言にする。
                raise PermissionCollectionError(
                    "permissions enumeration was incomplete; no rows updated"
                ) from exc
        else:
            after_owner, after_emails, after_groups = _target_acl(
                snapshot,
                permissions,
                company_domain=normalized_domain,
            )
        items.append(
            AclPlanItem(
                snapshot=snapshot,
                before_owner=_normalize_one(snapshot.owner_email),
                before_emails=_normalize_many(snapshot.acl_emails),
                before_groups=_normalize_many(snapshot.acl_groups),
                after_owner=after_owner,
                after_emails=after_emails,
                after_groups=after_groups,
                unreachable=unreachable,
            )
        )

    digest = _plan_digest(items, company_domain=normalized_domain)
    return AclSyncPlan(tuple(items), normalized_domain, digest)


def validate_commit(
    plan: AclSyncPlan,
    *,
    expect_plan_sha: str | None,
    allow_company_access_loss: bool,
    allow_mass_acl_change: bool,
    allow_unreachable_revoke: bool = False,
) -> None:
    """commit の SHA と安全 override を検証する（DB 書込み前にのみ呼ぶ）。"""
    expected = (expect_plan_sha or "").strip().lower()
    if not _SHA256_RE.fullmatch(expected):
        raise PlanShaMismatchError("commit requires a valid --expect-plan-sha; no rows updated")
    if not hmac.compare_digest(expected, plan.plan_sha256):
        raise PlanShaMismatchError("ACL plan SHA changed since dry-run; no rows updated")

    missing: list[str] = []
    if plan.requires_company_override and not allow_company_access_loss:
        missing.append("--allow-company-access-loss")
    if plan.requires_mass_override and not allow_mass_acl_change:
        missing.append("--allow-mass-acl-change")
    if plan.requires_unreachable_override and not allow_unreachable_revoke:
        missing.append("--allow-unreachable-revoke")
    if missing:
        raise AclSafetyGuardError(
            "ACL safety guard blocked commit; required_overrides=" + ",".join(missing)
        )


def synchronize_gdrive_acl(
    repository: _AclRepository,
    client: _PermissionClient,
    *,
    company_domain: str,
    commit: bool = False,
    expect_plan_sha: str | None = None,
    allow_company_access_loss: bool = False,
    allow_mass_acl_change: bool = False,
    allow_unreachable_revoke: bool = False,
    max_permission_pages: int = DEFAULT_PERMISSIONS_MAX_PAGES,
    api_retries: int = DEFAULT_GOOGLE_API_RETRIES,
    request_id: str | None = None,
) -> AclSyncResult:
    """snapshot → Drive 全 ACL → guard → 単一 tx 更新を実行する。"""
    snapshots = repository.list_nonstale_gdrive_acl_snapshot()
    plan = build_acl_plan(
        snapshots,
        client,
        company_domain=company_domain,
        request_id=request_id or f"gdrive-acl-{uuid.uuid4().hex[:12]}",
        max_permission_pages=max_permission_pages,
        api_retries=api_retries,
    )
    if not commit:
        return AclSyncResult(plan=plan, updated_count=0, committed=False)

    validate_commit(
        plan,
        expect_plan_sha=expect_plan_sha,
        allow_company_access_loss=allow_company_access_loss,
        allow_mass_acl_change=allow_mass_acl_change,
        allow_unreachable_revoke=allow_unreachable_revoke,
    )
    updated = repository.update_gdrive_acls(plan.updates())
    return AclSyncResult(plan=plan, updated_count=updated, committed=True)


def summary_line(result: AclSyncResult) -> str:
    """PII を含まない監査用 1 行集計。"""
    plan = result.plan
    mode = "commit" if result.committed else "dry-run"
    return (
        f"mode={mode} target={plan.target_count} changed={plan.changed_count} "
        f"unchanged={plan.target_count - plan.changed_count} unreachable={plan.unreachable_count} "
        f"updated={result.updated_count} "
        f"change_pct={plan.change_ratio:.2%} company_before={plan.company_before_count} "
        f"company_after={plan.company_after_count} company_loss={plan.company_loss_count} "
        f"company_decrease_pct={plan.company_decrease_ratio:.2%} "
        f"requires_company_override={str(plan.requires_company_override).lower()} "
        f"requires_mass_override={str(plan.requires_mass_override).lower()} "
        f"requires_unreachable_override={str(plan.requires_unreachable_override).lower()} "
        f"plan_sha256={plan.plan_sha256}"
    )


def warning_lines(plan: AclSyncPlan) -> list[str]:
    """dry-run 監査で見る件数ベース warning（識別子・email は出さない）。"""
    warnings: list[str] = []
    if plan.unreachable_count:
        warnings.append(
            "[WARN] unreachable Drive items require owner-only ACL quarantine: "
            f"unreachable={plan.unreachable_count}"
        )
    if plan.change_ratio > MASS_CHANGE_WARNING_RATIO:
        warnings.append(
            f"[WARN] ACL change ratio exceeds 30%: changed={plan.changed_count} "
            f"target={plan.target_count} change_pct={plan.change_ratio:.2%}"
        )
    if plan.company_loss_count:
        warnings.append(
            f"[WARN] company access loss planned: loss={plan.company_loss_count} "
            f"before={plan.company_before_count} after={plan.company_after_count}"
        )
    if plan.company_decrease_ratio > COMPANY_SHARED_DECREASE_BLOCK_RATIO:
        warnings.append(
            "[WARN] company shared rows decrease exceeds 20%: "
            f"decrease_pct={plan.company_decrease_ratio:.2%}"
        )
    return warnings


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="既定 dry-run。指定時のみ ACL UPDATE")
    parser.add_argument(
        "--expect-plan-sha",
        help="同じ環境で直前に dry-run した plan_sha256（--commit で必須）",
    )
    parser.add_argument(
        "--company-domain",
        default=os.environ.get("WORKSPACE_DOMAIN", "vectorinc.co.jp"),
        help="会社共有を表す acl_groups 値（既定 WORKSPACE_DOMAIN）",
    )
    parser.add_argument(
        "--allow-company-access-loss",
        action="store_true",
        help="company-domain access loss をレビュー済みの場合だけ指定",
    )
    parser.add_argument(
        "--allow-mass-acl-change",
        action="store_true",
        help="対象の 50%% 超が変わる計画をレビュー済みの場合だけ指定",
    )
    parser.add_argument(
        "--allow-unreachable-revoke",
        action="store_true",
        help="HTTP 404 の Drive 項目を owner-only ACL に縮小する計画をレビュー済みの場合だけ指定",
    )
    parser.add_argument(
        "--max-permission-pages",
        type=int,
        default=DEFAULT_PERMISSIONS_MAX_PAGES,
        help="1 file の permissions ページ上限（残 token があれば常に中止）",
    )
    parser.add_argument(
        "--api-retries",
        type=int,
        default=DEFAULT_GOOGLE_API_RETRIES,
        help="permissions API のページ単位 retry 回数",
    )
    parser.add_argument(
        "--owner-email",
        default=os.environ.get("INGEST_OWNER_EMAIL"),
        help="DB RLS session 用。省略時は INGEST_OWNER_EMAIL",
    )
    parser.add_argument(
        "--app-role",
        default="teamagent_app",
        help="Postgres SET ROLE（none で無効）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not os.environ.get("DATABASE_URL"):
        print("[ERROR] DATABASE_URL is required; no rows updated", file=sys.stderr)
        return 2
    if args.commit and not args.expect_plan_sha:
        print("[ERROR] --commit requires --expect-plan-sha; no rows updated", file=sys.stderr)
        return 2

    from teamagent.adapters.pgvector_client import PgVectorClient

    pgvector: PgVectorClient | None = None
    try:
        pgvector = PgVectorClient.from_env()
        repository = IngestRepository(
            pgvector,
            app_role=None if args.app_role.lower() == "none" else args.app_role,
            owner_email=args.owner_email,
        )
        client = GDriveClient.from_env(readonly=True)
        result = synchronize_gdrive_acl(
            repository,
            client,
            company_domain=args.company_domain,
            commit=args.commit,
            expect_plan_sha=args.expect_plan_sha,
            allow_company_access_loss=args.allow_company_access_loss,
            allow_mass_acl_change=args.allow_mass_acl_change,
            allow_unreachable_revoke=args.allow_unreachable_revoke,
            max_permission_pages=args.max_permission_pages,
            api_retries=args.api_retries,
        )
    except AclSyncError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # DB/Google 例外本文には DSN・URL・file ID 等が混ざり得るので class 名だけを出す。
        print(f"[ERROR] ACL sync failed ({type(exc).__name__}); no rows updated", file=sys.stderr)
        return 1
    finally:
        if pgvector is not None:
            pgvector.close()

    print(summary_line(result))
    for warning in warning_lines(result.plan):
        print(warning)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
