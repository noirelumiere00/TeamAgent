from __future__ import annotations

import copy
import json
import subprocess
import sys
from itertools import chain
from pathlib import Path

import pytest

from tests.pytest_shard import ROOT, committable_durations, partition, verify


def test_partition_balances_durations_including_one_large_file() -> None:
    ids = [f"{name}.py::test_{i}" for name in "abcd" for i in range(2)]
    durations = dict(zip(ids, (8, 6, 4, 2, 1, 1, 1, 1), strict=True))
    shards = partition(ids, durations, 2)
    assert sorted(chain.from_iterable(shards)) == sorted(ids)
    assert [sum(durations[nodeid] for nodeid in shard) for shard in shards] == [12, 12]
    assert ids[0] in shards[0] and ids[1] in shards[1]
    assert [set(shard) for shard in shards] == [
        set(shard) for shard in partition(list(reversed(ids)), durations, 2)
    ]
    assert sorted(chain.from_iterable(partition(ids, {}, 3))) == sorted(ids)


def test_xdist_shards_cover_collection_and_execution_once(tmp_path: Path) -> None:
    # Include a grouped test, a setup skip and an expected failure in the inventory.
    for index in range(3):
        (tmp_path / f"test_sample_{index}.py").write_text(
            "import pytest\n"
            "pytestmark = pytest.mark.xdist_group('db')\n"
            "def test_ok(): assert True\n"
            "@pytest.mark.skip(reason='fixture')\n"
            "def test_skip(): pass\n"
            "@pytest.mark.xfail(strict=True)\n"
            "def test_xfail(): assert False\n"
        )
    manifests = []
    for index in range(1, 4):
        path = tmp_path / f"manifest-{index}.json"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(tmp_path),
                "--rootdir",
                str(tmp_path),
                "-q",
                "-n",
                "2",
                "--dist=loadgroup",
                "-p",
                "tests.pytest_shard",
                "--ci-shards=3",
                f"--ci-shard={index}",
                f"--ci-manifest={path}",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        manifests.append(json.loads(path.read_text()))
    verify(manifests)
    assert manifests[0]["total"] == 9
    for damage in ("missing", "duplicate", "different_suite", "unexecuted"):
        broken = copy.deepcopy(manifests)
        if damage == "missing":
            broken.pop()
        elif damage == "duplicate":
            broken[1]["selected"] = broken[0]["selected"]
            broken[1]["executed"] = broken[0]["executed"]
        elif damage == "different_suite":
            broken[1]["digest"] = "different"
        else:
            broken[1]["executed"].pop()
        with pytest.raises(AssertionError):
            verify(broken)


def test_written_durations_drop_token_like_test_ids() -> None:
    fake = "xox" + "b-1234567890-abcdef"
    kept = committable_durations({f"t.py::test[{fake}]": 1.0, "t.py::ok": 0.1234567})
    assert kept == {"t.py::ok": 0.123457}
