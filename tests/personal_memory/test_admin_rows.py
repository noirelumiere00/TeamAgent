"""personal_memory_admin が行を dict（本番の dict_row）でも tuple（psycopg の既定）でも読めること。

実 DB の確認は test_migration_0029_postgres.py（dict_row の接続）。ここは DB 無しで両方の形を固定する。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from teamagent.adapters import personal_memory_admin as pma

TEAM = "T0123456789"
USER = "U0123456789"
_VIEW_COLS = ("entry_no", "entry_target", "entry_content", "entry_updated_at")


class _Cursor:
    def __init__(self, rows: dict[str, list[Any]]) -> None:
        self._rows = rows
        self._last: list[Any] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> None:
        self._last = next((v for k, v in self._rows.items() if k in query), [])

    def fetchall(self) -> list[Any]:
        return list(self._last)

    def fetchone(self) -> Any:
        return self._last[0] if self._last else None


def _admin(rows: dict[str, list[Any]]) -> pma.PersonalMemoryAdmin:
    @contextmanager
    def factory(role: str) -> Iterator[Any]:
        class _Conn:
            def cursor(self) -> _Cursor:
                return _Cursor(rows)

        yield _Conn()

    return pma.PersonalMemoryAdmin(connection_factory=factory)


def _shape(as_dict: bool, cols: tuple[str, ...], values: tuple[Any, ...]) -> Any:
    return dict(zip(cols, values, strict=True)) if as_dict else values


@pytest.mark.parametrize("as_dict", [True, False], ids=["dict_row", "tuple_row"])
def test_admin_reads_both_row_shapes(as_dict: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pma.ADMIN_EMAILS_ENV, "owner@example.com")
    admin = _admin(
        {
            "personal_memory_admin_view": [
                _shape(as_dict, _VIEW_COLS, (1, "user", "返事は短め", None)),
                _shape(as_dict, _VIEW_COLS, (2, "memory", "花王の案件が多い", None)),
            ],
            "personal_memory_retire_candidates": [
                _shape(as_dict, ("team_id", "slack_user_id"), (TEAM, USER))
            ],
            "personal_memory_retire_delete": [_shape(as_dict, ("deleted",), (3,))],
            "personal_memory_retire_freeze": [_shape(as_dict, ("frozen",), (True,))],
        }
    )
    rows = admin.view(admin_email="owner@example.com", team_id=TEAM, slack_user_id=USER)
    assert [(r.no, r.target, r.content) for r in rows] == [
        (1, "user", "返事は短め"),
        (2, "memory", "花王の案件が多い"),
    ]
    assert admin.candidates() == [(TEAM, USER)]
    assert admin.retire_delete(TEAM, USER) == 3
    assert admin.retire_freeze(TEAM, USER) is True


def test_missing_column_name_is_db_failed() -> None:
    # 本番の dict 行で別名が合わなければ、素の KeyError ではなく db_failed に揃う（削除も確定しない）
    admin = _admin({"personal_memory_retire_delete": [{"personal_memory_retire_delete": 3}]})
    with pytest.raises(pma.PersonalMemoryAdminError) as err:
        admin.retire_delete(TEAM, USER)
    assert err.value.code == "db_failed"


def test_no_row_reads_as_nothing() -> None:
    admin = _admin({})
    assert admin.retire_delete(TEAM, USER) == 0
    assert admin.retire_freeze(TEAM, USER) is False
