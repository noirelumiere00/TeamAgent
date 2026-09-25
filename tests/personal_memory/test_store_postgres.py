"""PersonalMemoryStore の実 PostgreSQL 試験（migration 0029 を本番に似せた役で流した使い捨て DB）。

本番と同じく、接続は master 役でつなぎ ``SET ROLE personal_memory_app`` してから使う。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from teamagent.adapters.personal_memory_store import (
    PersonalMemoryStore,
    PersonalMemoryStoreError,
    Principal,
)
from tests.personal_memory.conftest import PmDb

_TEAM = "T0123ABCDE"


def _principal(user: str) -> Principal:
    return Principal(team_id=_TEAM, slack_user_id=user, user_email=f"{user.lower()}@example.com")


def _factory(pm_db: PmDb) -> Any:
    import psycopg
    from psycopg.rows import dict_row

    @contextmanager
    def factory() -> Iterator[Any]:
        # PgVectorClient.connection(app_role=...) と同じ手順: SET ROLE → yield → commit/rollback
        conn = psycopg.connect(pm_db.migrator_dsn, row_factory=dict_row)
        try:
            with conn.cursor() as cur:
                cur.execute("SET ROLE personal_memory_app")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return factory


@pytest.fixture(scope="module")
def store(pm_db: PmDb) -> PersonalMemoryStore:
    return PersonalMemoryStore(connection_factory=_factory(pm_db))


def _audit_actions(pm_db: PmDb, principal: Principal) -> list[tuple[str, str, int]]:
    import psycopg

    with psycopg.connect(pm_db.admin_dsn) as su, su.cursor() as cur:
        cur.execute(
            "SELECT actor_kind, action, item_count FROM personal_memory_audit "
            "WHERE profile_sha16 = %s ORDER BY occurred_at, audit_id",
            (principal.sha16,),
        )
        return [tuple(r) for r in cur.fetchall()]


def test_load_missing_profile_returns_none(store: PersonalMemoryStore) -> None:
    assert store.load(_principal("U0NONE0001")) is None


def test_notice_then_learn_then_load(store: PersonalMemoryStore, pm_db: PmDb) -> None:
    p = _principal("U0FLOW0001")
    store.mark_noticed(p)
    snap = store.load(p)
    assert snap is not None and snap.noticed and snap.state == "active" and snap.entries == ()
    version = store.apply_learned(
        p,
        expected_version=snap.version,
        adds=[("user", "返事は結論から"), ("user", "資料は表形式")],
        remove_ids=[],
    )
    snap2 = store.load(p)
    assert snap2 is not None and snap2.version == version == snap.version + 1
    assert sorted(snap2.contents("user")) == ["資料は表形式", "返事は結論から"]
    # 1 項目を置き換える（remove + add）
    old = next(e for e in snap2.entries if e.content == "返事は結論から")
    store.apply_learned(
        p, expected_version=version, adds=[("user", "返事は結論から3行")], remove_ids=[old.entry_id]
    )
    snap3 = store.load(p)
    assert snap3 is not None
    assert sorted(snap3.contents("user")) == ["資料は表形式", "返事は結論から3行"]
    assert _audit_actions(pm_db, p) == [
        ("self", "notice_ack", 0),
        ("system", "learn_applied", 2),
        ("system", "learn_applied", 2),
    ]


def test_learn_rejected_before_notice_or_when_frozen(store: PersonalMemoryStore) -> None:
    p = _principal("U0GATE0001")
    store.set_state(p, "active")  # profile はあるが告知前
    snap = store.load(p)
    assert snap is not None and not snap.noticed
    with pytest.raises(PersonalMemoryStoreError) as exc:
        store.apply_learned(p, expected_version=snap.version, adds=[("user", "x")], remove_ids=[])
    assert exc.value.code == "not_active"
    store.mark_noticed(p)
    store.set_state(p, "frozen")
    snap = store.load(p)
    assert snap is not None and snap.state == "frozen"
    with pytest.raises(PersonalMemoryStoreError) as exc:
        store.apply_learned(p, expected_version=snap.version, adds=[("user", "x")], remove_ids=[])
    assert exc.value.code == "not_active"


def test_version_conflict_rejects_stale_learning(store: PersonalMemoryStore) -> None:
    p = _principal("U0VERS0001")
    store.mark_noticed(p)
    snap = store.load(p)
    assert snap is not None
    store.apply_learned(p, expected_version=snap.version, adds=[("user", "a")], remove_ids=[])
    # 学習ジョブの間に本人がコマンドを使った（版が進んだ）ケース
    with pytest.raises(PersonalMemoryStoreError) as exc:
        store.apply_learned(p, expected_version=snap.version, adds=[("user", "b")], remove_ids=[])
    assert exc.value.code == "version_conflict"


@pytest.mark.parametrize("content", ["区切り§入り", "x" * 201, " 前後に空白 ", ""])
def test_bad_content_rejected_before_db(store: PersonalMemoryStore, content: str) -> None:
    p = _principal("U0BADC0001")
    store.mark_noticed(p)
    snap = store.load(p)
    assert snap is not None
    with pytest.raises(PersonalMemoryStoreError) as exc:
        store.apply_learned(
            p, expected_version=snap.version, adds=[("user", content)], remove_ids=[]
        )
    assert exc.value.code == "bad_entry"


def test_over_limit_rejected(store: PersonalMemoryStore) -> None:
    p = _principal("U0LIMT0001")
    store.mark_noticed(p)
    snap = store.load(p)
    assert snap is not None
    adds = [("user", "あ" * 200)] * 7  # 7*200 + 区切り > 1375
    with pytest.raises(PersonalMemoryStoreError) as exc:
        store.apply_learned(p, expected_version=snap.version, adds=adds, remove_ids=[])
    assert exc.value.code == "over_limit"
    assert store.load(p).entries == ()  # type: ignore[union-attr]


def test_cannot_remove_other_users_entries(store: PersonalMemoryStore) -> None:
    a, b = _principal("U0OWNA0001"), _principal("U0OWNB0001")
    for p in (a, b):
        store.mark_noticed(p)
    snap_b = store.load(b)
    assert snap_b is not None
    store.apply_learned(
        b, expected_version=snap_b.version, adds=[("user", "B の癖")], remove_ids=[]
    )
    b_entry = store.load(b).entries[0]  # type: ignore[union-attr]
    snap_a = store.load(a)
    assert snap_a is not None
    # 他人の entry_id は RLS で見えないので「知らない項目」として拒否される
    with pytest.raises(PersonalMemoryStoreError) as exc:
        store.apply_learned(
            a, expected_version=snap_a.version, adds=[], remove_ids=[b_entry.entry_id]
        )
    assert exc.value.code == "unknown_entry"
    assert store.forget(a, b_entry.entry_id) is False
    assert store.load(b).contents("user") == ("B の癖",)  # type: ignore[union-attr]


def test_forget_and_freeze_and_resume(store: PersonalMemoryStore, pm_db: PmDb) -> None:
    p = _principal("U0CMDS0001")
    store.mark_noticed(p)
    snap = store.load(p)
    assert snap is not None
    store.apply_learned(
        p, expected_version=snap.version, adds=[("user", "a"), ("user", "b")], remove_ids=[]
    )
    target = next(e for e in store.load(p).entries if e.content == "a")  # type: ignore[union-attr]
    assert store.forget(p, target.entry_id) is True
    assert store.load(p).contents("user") == ("b",)  # type: ignore[union-attr]
    store.set_state(p, "frozen")
    store.set_state(p, "active")
    actions = [a for _, a, _ in _audit_actions(pm_db, p)]
    assert actions == ["notice_ack", "learn_applied", "forget", "freeze", "resume"]


def test_erase_requires_confirmation_within_window(store: PersonalMemoryStore, pm_db: PmDb) -> None:
    p = _principal("U0ERAS0001")
    store.mark_noticed(p)
    snap = store.load(p)
    assert snap is not None
    store.apply_learned(
        p, expected_version=snap.version, adds=[("user", "a"), ("memory", "b")], remove_ids=[]
    )
    with pytest.raises(PersonalMemoryStoreError) as exc:
        store.confirm_erase(p)  # 依頼の前
    assert exc.value.code == "erase_not_requested"
    store.request_erase(p, window_s=0)  # 期限切れを作る
    with pytest.raises(PersonalMemoryStoreError):
        store.confirm_erase(p)
    store.request_erase(p)
    assert store.confirm_erase(p) == 2
    snap = store.load(p)
    assert snap is not None and snap.entries == () and snap.state == "frozen"
    assert _audit_actions(pm_db, p)[-1] == ("self", "erase_all", 2)


def test_principal_repr_hides_identity() -> None:
    p = _principal("U0SECR0001")
    assert "U0SECR0001" not in repr(p) and "@" not in repr(p)
    with pytest.raises(PersonalMemoryStoreError):
        Principal(team_id="bad", slack_user_id="U0SECR0001", user_email="a@example.com")


def test_store_errors_hide_database_details(pm_db: PmDb) -> None:
    import psycopg

    @contextmanager
    def broken() -> Iterator[Any]:
        raise psycopg.OperationalError("connection to 10.0.0.1 failed: password for teamagent")
        yield  # pragma: no cover

    store = PersonalMemoryStore(connection_factory=broken)
    with pytest.raises(PersonalMemoryStoreError) as exc:
        store.load(_principal("U0DOWN0001"))
    assert exc.value.code == "store_unavailable"
    assert exc.value.__cause__ is None and "password" not in str(exc.value)
