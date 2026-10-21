"""表示済み資料の参照を本人RLS下で10分だけ共有する。本文・質問は保存しない。"""

from __future__ import annotations

import json
import re
from typing import Any

_GET = """
SELECT refs FROM document_followup_refs
WHERE user_email = %(email)s AND conversation_key = %(key)s AND expires_at > NOW()
"""
_EXPIRE = """
DELETE FROM document_followup_refs WHERE user_email = %(email)s AND expires_at <= NOW()
"""
_SAVE = """
INSERT INTO document_followup_refs (user_email, conversation_key, refs, expires_at)
VALUES (%(email)s, %(key)s, %(refs)s::jsonb, NOW() + INTERVAL '10 minutes')
ON CONFLICT (user_email, conversation_key) DO UPDATE
SET refs = EXCLUDED.refs, expires_at = EXCLUDED.expires_at
"""


def _identity(user_email: str, conversation_key: str) -> dict[str, str]:
    email = user_email.strip().lower()
    if not email or "@" not in email or re.fullmatch(r"[0-9a-f]{64}", conversation_key) is None:
        raise ValueError("invalid reference owner or conversation key")
    return {"email": email, "key": conversation_key}


def _refs(value: Any) -> list[dict[str, Any]]:
    if (
        not isinstance(value, list)
        or len(value) > 5
        or any(not isinstance(ref, dict) for ref in value)
    ):
        raise ValueError("invalid displayed references")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 65_536:
        raise ValueError("displayed references too large")
    return value


class DocumentReferenceStore:
    """別MCP taskでも同じ本人・実会話の表示済み参照を読める。失敗は呼出側で非再検索に倒す。"""

    def __init__(self, pgvector: Any) -> None:
        self._pg = pgvector

    def get(self, user_email: str, conversation_key: str) -> list[dict[str, Any]]:
        params = _identity(user_email, conversation_key)
        with self._pg.connection(app_role="teamagent_app", user_email=params["email"]) as conn:
            with conn.cursor() as cur:
                cur.execute(_GET, params)
                row = cur.fetchone()
        return _refs(row["refs"]) if row else []

    def save(self, user_email: str, conversation_key: str, refs: list[dict[str, Any]]) -> None:
        params = _identity(user_email, conversation_key)
        payload = json.dumps(_refs(refs), ensure_ascii=False)
        with self._pg.connection(app_role="teamagent_app", user_email=params["email"]) as conn:
            with conn.cursor() as cur:
                cur.execute(_EXPIRE, params)
                cur.execute(_SAVE, {**params, "refs": payload})
