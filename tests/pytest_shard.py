"""Duration-balanced test shards and cross-run collection/execution accounting.

Run pytest with ``-p tests.pytest_shard``; verify downloaded manifests with
``python tests/pytest_shard.py MANIFEST...``. No additional dependency.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Fake tokens in parametrized IDs trip GitHub push protection when the timings file is committed.
SECRET_LIKE = re.compile(r"xox[a-z]-|ghp_|AKIA[0-9A-Z]{8}|sk-[A-Za-z0-9]{10}|AIza")


def committable_durations(durations: dict[str, float]) -> dict[str, float]:
    return {k: round(v, 6) for k, v in sorted(durations.items()) if not SECRET_LIKE.search(k)}


def partition(nodeids: list[str], durations: dict[str, float], count: int) -> list[list[str]]:
    """Assign the longest remaining test to the lightest shard, with stable ties."""
    default = statistics.median(durations.values()) if durations else 0.1
    weights = {nodeid: max(durations.get(nodeid, default), 0.001) for nodeid in nodeids}
    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for nodeid in sorted(nodeids, key=lambda nodeid: (-weights[nodeid], nodeid)):
        index = min(range(count), key=lambda index: (loads[index], index))
        shards[index].append(nodeid)
        loads[index] += weights[nodeid]
    return shards


def verify(manifests: list[dict[str, Any]]) -> None:
    first = manifests[0]
    count = first["shards"]
    assert len(manifests) == count, "missing shard manifest"
    assert sorted(m["shard"] for m in manifests) == list(range(1, count + 1))
    selected: list[str] = []
    executed: list[str] = []
    for manifest in manifests:
        assert (manifest["shards"], manifest["total"], manifest["digest"]) == (
            count,
            first["total"],
            first["digest"],
        ), "shards collected different suites"
        assert Counter(manifest["executed"]) == Counter(manifest["selected"]), (
            "selected tests were not executed exactly once"
        )
        selected.extend(manifest["selected"])
        executed.extend(manifest["executed"])
    assert len(selected) == len(set(selected)) == first["total"], "missing/duplicate tests"
    digest = hashlib.sha256("\n".join(sorted(selected)).encode()).hexdigest()
    assert digest == first["digest"], "shard union differs from full collection"
    assert len(executed) == first["total"]
    print(f"Verified {count} shards: {first['total']} collected and executed exactly once")


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ci-shard")
    group.addoption("--ci-shards", type=int, default=1)
    group.addoption("--ci-shard", type=int, default=1)
    group.addoption("--ci-durations", default=str(ROOT / ".github/pytest-durations.json"))
    group.addoption("--ci-manifest")
    group.addoption("--ci-write-durations")


def pytest_configure(config: pytest.Config) -> None:
    count, index = config.getoption("ci_shards"), config.getoption("ci_shard")
    if not 1 <= index <= count:
        raise pytest.UsageError("require 1 <= --ci-shard <= --ci-shards")
    config.pluginmanager.register(ShardRun(config), "ci-shard-run")


class ShardRun:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.manifest: dict[str, Any] | None = None
        self.executed: list[str] = []
        self.durations: dict[str, float] = defaultdict(float)

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        count = self.config.getoption("ci_shards")
        index = self.config.getoption("ci_shard")
        # xdist appends group names to nodeids; use those same IDs for execution accounting.
        nodeids = [item.nodeid for item in items]
        if len(nodeids) != len(set(nodeids)):
            raise pytest.UsageError("duplicate collected nodeids")
        durations = json.loads(Path(self.config.getoption("ci_durations")).read_text())
        shards = partition(nodeids, durations, count)
        selected = set(shards[index - 1])
        deselected = [item for item in items if item.nodeid not in selected]
        items[:] = [item for item in items if item.nodeid in selected]
        self.config.hook.pytest_deselected(items=deselected)
        self.manifest = {
            "shards": count,
            "shard": index,
            "total": len(nodeids),
            "digest": hashlib.sha256("\n".join(sorted(nodeids)).encode()).hexdigest(),
            "selected": [item.nodeid for item in items],
        }

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
            self.executed.append(report.nodeid)
        self.durations[report.nodeid] += report.duration

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: Any, error: Any) -> None:
        manifest = node.workeroutput.get("ci_shard")
        if manifest is not None:
            if self.manifest is not None and self.manifest != manifest:
                raise pytest.UsageError("xdist workers collected different shard inventories")
            self.manifest = manifest

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if hasattr(self.config, "workerinput"):
            self.config.workeroutput["ci_shard"] = self.manifest
            return
        if path := self.config.getoption("ci_manifest"):
            assert self.manifest is not None, "no collection manifest"
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps({**self.manifest, "executed": self.executed}))
        if path := self.config.getoption("ci_write_durations"):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(
                json.dumps(committable_durations(self.durations), indent=2) + "\n"
            )


if __name__ == "__main__":
    verify([json.loads(Path(path).read_text()) for path in sys.argv[1:]])
