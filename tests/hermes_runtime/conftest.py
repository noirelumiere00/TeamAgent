"""hermes_runtime/ は TeamAgent の wheel に入らない別パッケージなので、テストからは path を足して読む。"""

from __future__ import annotations

import sys
from pathlib import Path

_RUNTIME_ROOT = Path(__file__).resolve().parents[2] / "hermes_runtime"
if str(_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_ROOT))
