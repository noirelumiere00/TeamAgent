"""Suite-wide isolation for process-local HMAC rotation clock state."""

from collections.abc import Iterator

import pytest

import teamagent.hmac_keyring as hmac_keyring_module


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
