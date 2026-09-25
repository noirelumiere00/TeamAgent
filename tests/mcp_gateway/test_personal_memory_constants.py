"""本人メモの複製定数の一致（Hermes ⇄ クライアント ⇄ guard ⇄ 入力 ⇄ バッファ ⇄ claim）。

Hermes 側との一致は tests/personal_memory/test_hermes_learn_client.py が固定している。
"""

from __future__ import annotations

import secrets
import uuid

from teamagent.adapters import hermes_learn_client as hlc
from teamagent.adapters import personal_memory_store as store
from teamagent.mcp_gateway.caller_claim import _OPAQUE_INVOCATION_ID_RE
from teamagent.mcp_gateway.personal_memory import buffer, gate, schemas
from teamagent.personal_memory import guard


def test_utterance_limits_agree() -> None:
    assert schemas.MAX_UTTERANCE_CHARS == guard.MAX_UTTERANCE_CHARS == hlc.MAX_UTTERANCE_CHARS
    assert buffer.FLUSH_AFTER_UTTERANCES == hlc.MAX_UTTERANCES


def test_entry_limits_agree() -> None:
    assert store.MAX_ENTRY_CHARS == guard.MAX_ENTRY_CHARS == hlc.MAX_ENTRY_CHARS
    assert store.MAX_ENTRIES_PER_TARGET == hlc.MAX_SNAPSHOT_ENTRIES
    assert store.TARGET_CHAR_LIMIT == {
        "user": hlc.USER_CHAR_LIMIT,
        "memory": hlc.MEMORY_CHAR_LIMIT,
    }
    assert store.ENTRY_DELIMITER == hlc.ENTRY_DELIMITER
    # 一覧の番号は user と memory を合わせた最大件数まで
    assert schemas.MAX_ITEM_NO == 2 * store.MAX_ENTRIES_PER_TARGET


def test_reserved_invocation_fits_caller_claim_format() -> None:
    for kind in ("obs", "ctx", "cmd"):
        call_id = f"aico-pm-{kind}-{secrets.token_hex(16)}"
        assert gate.RESERVED_INVOCATION_RE.fullmatch(call_id)
        assert _OPAQUE_INVOCATION_ID_RE.fullmatch(call_id)
        assert len(call_id) <= 256
    # plugin が使う uuid4().hex でも作れる形
    assert gate.RESERVED_INVOCATION_RE.fullmatch(f"aico-pm-obs-{uuid.uuid4().hex}")


def test_erase_window_agrees() -> None:
    assert store.ERASE_WINDOW_S == 600
