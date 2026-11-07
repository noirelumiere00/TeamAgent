"""Employee account intake: preview or insert without changing existing accounts."""

from __future__ import annotations

import re
from collections import Counter
from typing import Literal
from urllib.parse import urlsplit

import structlog
from pydantic import BaseModel

from teamagent.adapters.pgvector_client import PgVectorClient

logger = structlog.get_logger(__name__)
OWN_ACCOUNT_OPERATORS = ("NewsTV", "ベクトル（VCL）", "ベクトル九州", "イニシャル", "gracemode")
_HANDLE = r"[A-Za-z0-9_.]{2,24}"
_HOSTS = {"www.tiktok.com", "tiktok.com", "m.tiktok.com"}
_ACCOUNT_SELECT = """SELECT operator_label FROM vl_accounts WHERE platform='tiktok'
    AND lower(ltrim(handle,'@'))=%s ORDER BY handle LIMIT 1"""


class AccountAddError(ValueError):
    """Fixed codes only, so failures do not expose input or email in logs."""


class AccountAddResult(BaseModel):
    line_no: int
    handle: str | None = None
    status: Literal["added", "exists", "invalid", "duplicate"]
    operator_label: str | None = None
    duplicate_of: int | None = None
    error_code: str | None = None


def normalize_account_handle(value: str) -> str:
    value = value.strip()
    if re.fullmatch(rf"@?({_HANDLE})", value):
        return value.removeprefix("@").lower()
    if re.search(r"[\s\x00-\x1f\x7f\\]", value):
        raise AccountAddError("invalid_account")
    try:
        parsed = urlsplit(value if "://" in value else f"https://{value}")
        if parsed.scheme != "https" or parsed.netloc.lower() not in _HOSTS:
            raise AccountAddError("invalid_account")
        match = re.fullmatch(rf"/@({_HANDLE})(?:/video/[0-9]+)?/?", parsed.path)
        if match:
            return match[1].lower()
    except ValueError:
        pass
    raise AccountAddError("invalid_account")


def inspect_account_input(lines: list[str]) -> list[AccountAddResult]:
    if len(lines) > 50:
        raise AccountAddError("too_many_lines")
    results: list[AccountAddResult] = []
    seen: dict[str, int] = {}
    for line_no, value in enumerate(lines, 1):
        if not value.strip():
            continue
        try:
            handle = normalize_account_handle(value)
        except AccountAddError:
            results.append(
                AccountAddResult(line_no=line_no, status="invalid", error_code="invalid_account")
            )
            continue
        duplicate_of = seen.get(handle)
        results.append(
            AccountAddResult(
                line_no=line_no,
                handle=handle,
                status="duplicate" if duplicate_of else "added",
                duplicate_of=duplicate_of,
            )
        )
        seen.setdefault(handle, line_no)
    return results


class AccountAddRepository:
    def __init__(self, pg: PgVectorClient) -> None:
        self.pg = pg

    def preview(
        self,
        lines: list[str],
        *,
        operator_label: str,
        user_email: str,
        request_id: str,
        user_role: str = "viewer",
    ) -> list[AccountAddResult]:
        """Return `added` for planned additions; perform no writes."""
        return self._process(lines, operator_label, user_email, user_role, request_id, write=False)

    def add(
        self,
        lines: list[str],
        *,
        operator_label: str,
        user_email: str,
        request_id: str,
        user_role: str = "viewer",
    ) -> list[AccountAddResult]:
        return self._process(lines, operator_label, user_email, user_role, request_id, write=True)

    def _process(
        self,
        lines: list[str],
        operator_label: str,
        user_email: str,
        user_role: str,
        request_id: str,
        *,
        write: bool,
    ) -> list[AccountAddResult]:
        if operator_label not in OWN_ACCOUNT_OPERATORS:
            raise AccountAddError("invalid_operator")
        if not user_email.strip() or "\x00" in user_email:
            raise AccountAddError("invalid_user_email")
        results = inspect_account_input(lines)
        with self.pg.connection(
            app_role="teamagent_app",
            user_email=user_email,
            user_role=user_role,
            application_name="teamagent-account-add",
        ) as conn:
            for row in results:
                if row.status != "added":
                    continue
                row.operator_label = operator_label
                existing = conn.execute(_ACCOUNT_SELECT, (row.handle,)).fetchone()
                if existing is None and write:
                    inserted = conn.execute(
                        """INSERT INTO vl_accounts
                           (platform,handle,operator_label,added_by,added_at)
                           VALUES ('tiktok',%s,%s,%s,now())
                           ON CONFLICT (platform,handle) DO NOTHING RETURNING handle""",
                        (row.handle, operator_label, user_email),
                    ).fetchone()
                    if inserted:
                        continue
                    existing = conn.execute(_ACCOUNT_SELECT, (row.handle,)).fetchone()
                if existing is not None:
                    row.status = "exists"
                    row.operator_label = existing["operator_label"]
        logger.info(
            "video_library_account_add",
            request_id=request_id,
            preview=not write,
            counts=dict(Counter(row.status for row in results)),
        )
        return results
