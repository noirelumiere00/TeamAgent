"""Keep the final migration guard and its pre-rollback ACL evidence together."""

from __future__ import annotations

from typing import Any


def apply_with_acl_evidence(conn: Any, migration: str, tables: list[str]) -> None:
    """Apply in one transaction; preserve ACL evidence if the final DO rejects it.

    The savepoint only makes diagnostics possible after the guard raises. The
    original exception is re-raised, so the outer transaction still rolls back.
    """
    body, guard = migration.rsplit("\nDO $$", 1)
    conn.execute(body)
    try:
        with conn.transaction():
            conn.execute("DO $$" + guard)
    except Exception as exc:
        memberships = conn.execute(
            "SELECT r.rolname, m.inherit_option, m.set_option, "
            "pg_has_role(current_user, r.oid, 'USAGE'), "
            "pg_has_role(current_user, r.oid, 'SET') "
            "FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
            "WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = current_user) "
            "ORDER BY r.rolname"
        ).fetchall()
        acl = conn.execute(
            "SELECT c.relname, pg_get_userbyid(c.relowner), "
            "pg_get_userbyid(a.grantor), "
            "CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END, "
            "a.privilege_type, a.is_grantable, "
            "CASE WHEN a.grantee = 0 THEN true "
            "ELSE pg_has_role(current_user, a.grantee, 'USAGE') END "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN LATERAL aclexplode(c.relacl) a "
            "WHERE n.nspname = 'public' AND c.relname = ANY(%s) "
            "ORDER BY c.relname, a.grantee, a.privilege_type",
            (tables,),
        ).fetchall()
        effective = conn.execute(
            "SELECT t, has_table_privilege(current_user, 'public.' || t, 'SELECT'), "
            "has_table_privilege(current_user, 'public.' || t, 'INSERT') "
            "FROM unnest(%s::text[]) t",
            (tables,),
        ).fetchall()
        exc.add_note(
            f"pre-rollback memberships (role, inherit, set, usage, can_set): {memberships!r}"
        )
        exc.add_note(
            f"pre-rollback ACL (table, owner, grantor, grantee, privilege, grantable, inherited): {acl!r}"
        )
        exc.add_note(f"pre-rollback effective (table, select, insert): {effective!r}")
        raise
