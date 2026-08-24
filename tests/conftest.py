"""Suite-wide isolation for process-local HMAC rotation clock state."""

from collections.abc import Iterator

import pytest

import teamagent.hmac_keyring as hmac_keyring_module


@pytest.fixture(autouse=True)
def _isolate_mail_process_local_state() -> Iterator[None]:
    """受信箱スキャンのプロセス内キャッシュと日次上限カウンタをテスト間で持ち越さない。

    どちらも「本番はツール呼び出しごとに Skill を作り直す」ことへの対処でプロセス内に
    置いてある（インスタンス変数だと本番で 1 度も効かない）。テストは 1 プロセスなので、
    明示的に消さないと前のテストの受信箱・消費済み枠が次のテストへ漏れる。
    """
    from teamagent.skills.mail_draft.skill import reset_daily_quota
    from teamagent.skills.mail_followup.skill import clear_scan_cache

    clear_scan_cache()
    reset_daily_quota()
    yield
    clear_scan_cache()
    reset_daily_quota()


@pytest.fixture(autouse=True)
def _isolate_hmac_rotation_runtime_state() -> Iterator[None]:
    """Do not let one test's synthetic epoch affect another test's process clock."""
    with hmac_keyring_module._rotation_runtime_lock:
        hmac_keyring_module._rotation_runtime_states.clear()
        hmac_keyring_module._purpose_clock_high_water.clear()
    yield
    with hmac_keyring_module._rotation_runtime_lock:
        hmac_keyring_module._rotation_runtime_states.clear()
        hmac_keyring_module._purpose_clock_high_water.clear()
