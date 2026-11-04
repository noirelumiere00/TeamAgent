"""Only the two explicit decisions are seeded; no AWS or database is used here."""

from contextlib import nullcontext
from unittest.mock import MagicMock

import pytest

from scripts.register_video_case_aliases import INITIAL_ALIASES, register_initial_aliases


def test_register_explicit_aliases_records_operator_without_changing_campaign_for_aeon() -> None:
    repo = MagicMock()
    conn = MagicMock()
    repo.connection.return_value = nullcontext(conn)
    conn.execute.return_value.rowcount = 1
    assert register_initial_aliases(repo, decided_by="operator") == len(INITIAL_ALIASES) == 4
    params = [c.args[1] for c in conn.execute.call_args_list]
    assert params == [
        ("TikTok", "20260717", "ラフォーレ", "セール", "operator"),
        ("トップバリュ", "昭和レトロ", "AEON", None, "operator"),
        ("アップガレージ", "メディア", "アップガレージ", "アプリキャンペーン", "operator"),
        ("ローソン九州", "ばりスゴ", "ローソン九州", "ホークス応援", "operator"),
    ]


def test_register_requires_attributed_decision() -> None:
    repo = MagicMock()
    with pytest.raises(ValueError, match="missing_decided_by"):
        register_initial_aliases(repo, decided_by=" ")
    repo.connection.assert_not_called()
