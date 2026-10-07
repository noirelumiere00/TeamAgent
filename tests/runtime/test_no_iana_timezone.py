"""本番の実行環境に時間帯のデータが無いことへの備え（2026-10-07 r48 の起動失敗）。

mcp の実行イメージ（chainguard python）には tzdata が入っておらず、``ZoneInfo("Asia/Tokyo")`` の
ような IANA 名の時間帯は import の時点で ZoneInfoNotFoundError になり、mcp 全体が起動しない。
手元やCIには時間帯のデータがあるのでテストは通ってしまう。そこで、本番で読み込むコードが
IANA 名の時間帯を使っていないことを、ソースそのもので固定する（日本時間は固定の +9 時間で表す）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_FORBIDDEN = re.compile(
    r"\bZoneInfo\s*\(|^\s*(?:from|import)\s+zoneinfo\b|\bgettz\s*\(|\bpytz\b", re.M
)


def test_runtime_code_does_not_use_iana_time_zones() -> None:
    offenders: list[str] = []
    for base in ("src", "scripts"):
        for path in sorted((ROOT / base).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for m in _FORBIDDEN.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert offenders == [], (
        "本番の実行環境には時間帯のデータが無いので IANA 名の時間帯は使えない"
        "（timezone(timedelta(hours=9)) を使う）: " + ", ".join(offenders)
    )


_IMPORT_ALL_WITHOUT_TZ = """
import importlib, pkgutil, sys, zoneinfo
sys.modules["tzdata"] = None
zoneinfo.reset_tzpath(to=[])
import teamagent
fails = []
for m in pkgutil.walk_packages(teamagent.__path__, "teamagent."):
    try:
        importlib.import_module(m.name)
    except zoneinfo.ZoneInfoNotFoundError as e:
        fails.append(f"TZFAIL {m.name}: {e}")
print("\\n".join(fails))
"""


def test_every_module_imports_without_time_zone_data() -> None:
    """本番と同じ「時間帯データ無し」で全モジュールを import しても、時間帯で落ちない。

    r48 の失敗（slack_summary.period の import で mcp 全体が起動しない）を、別プロセスで再現して固定する。
    """
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-c", _IMPORT_ALL_WITHOUT_TZ],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=ROOT,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    fails = [line for line in done.stdout.splitlines() if line.startswith("TZFAIL ")]
    assert fails == [], "時間帯データが無いと import で落ちる: " + " / ".join(fails)
