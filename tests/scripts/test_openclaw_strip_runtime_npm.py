"""strip-runtime-npm.mjs: OpenClaw runtime から npm-12 / node-gyp を apk del 相当で外す（2026-10-01）。

固定すること:
- apk installed db に載っている 2 パッケージのファイルだけを消し、db からも外す
- 他パッケージも持つディレクトリ（usr/bin 等）と、他パッケージのファイルは残す
- 残すパッケージが外す側に依存していたら（名前・provides どちらでも）ビルドを止める
- db と実ファイルが食い違う（載っているファイルが無い）ならビルドを止める
- 外すパッケージが db に無い（ベースの構成が変わった）ならビルドを止める

実物（cgr.dev/chainguard/node 3de92bf8…）の db 形式（P/V/D/p/F/R 行・空行区切り・末尾空行・
中間ディレクトリもすべて F: 行に載る）を写したフェイク。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra/openclaw/strip-runtime-npm.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")

NODEJS = """P:nodejs-26
V:26.10.0-r2
D:so:libc.so.6 so:libuv.so.1
p:cmd:node=26.10.0-r2 nodejs=26.10.0-r2
F:usr
F:usr/bin
R:node
F:var/lib/db/sbom
R:nodejs-26-26.10.0-r2.spdx.json"""

NODE_GYP = """P:node-gyp
V:13.0.2-r1
D:nodejs
p:
F:usr
F:usr/bin
R:node-gyp
F:usr/lib
F:usr/lib/node_modules
F:usr/lib/node_modules/node-gyp
R:package.json
F:var/lib/db/sbom
R:node-gyp-13.0.2-r1.spdx.json"""

NPM = """P:npm-12
V:12.1.0-r2
D:node-gyp
p:npm=12.1.0-r2
F:usr
F:usr/bin
R:npm
R:npx
F:usr/lib
F:usr/lib/node_modules
F:usr/lib/node_modules/npm
R:package.json
F:usr/lib/node_modules/npm/node_modules
F:usr/lib/node_modules/npm/node_modules/brace-expansion
R:package.json
F:var/lib/db/sbom
R:npm-12-12.1.0-r2.spdx.json"""


def _rootfs(tmp_path: Path, blocks: list[str]) -> Path:
    root = tmp_path / "rootfs"
    db = root / "usr/lib/apk/db/installed"
    db.parent.mkdir(parents=True)
    db.write_text("\n\n".join(blocks) + "\n\n")
    for block in blocks:
        directory = ""
        for line in block.split("\n"):
            if line.startswith("F:"):
                directory = line[2:]
                (root / directory).mkdir(parents=True, exist_ok=True)
            elif line.startswith("R:"):
                (root / directory / line[2:]).write_text("x")
    # Real npm/npx are symlinks into node_modules.
    for name, target in (("npm", "npm-cli.js"), ("npx", "npx-cli.js")):
        (root / "usr/bin" / name).unlink(missing_ok=True)
        (root / "usr/bin" / name).symlink_to(f"../lib/node_modules/npm/bin/{target}")
    return root


def _strip(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(SCRIPT), str(root)], capture_output=True, text=True, check=False
    )


def test_removes_npm_and_node_gyp_files_and_db_entries(tmp_path: Path) -> None:
    root = _rootfs(tmp_path, [NODEJS, NODE_GYP, NPM])
    result = _strip(root)
    assert result.returncode == 0, result.stderr
    assert '"removedPackages":["node-gyp-13.0.2-r1","npm-12-12.1.0-r2"]' in result.stdout

    for gone in (
        "usr/bin/npm",
        "usr/bin/npx",
        "usr/bin/node-gyp",
        "usr/lib/node_modules",
        "var/lib/db/sbom/npm-12-12.1.0-r2.spdx.json",
        "var/lib/db/sbom/node-gyp-13.0.2-r1.spdx.json",
    ):
        assert not (root / gone).is_symlink()
        assert not (root / gone).exists(), gone
    # nodejs のファイルと共有ディレクトリは残る
    assert (root / "usr/bin/node").read_text() == "x"
    assert (root / "var/lib/db/sbom/nodejs-26-26.10.0-r2.spdx.json").exists()
    assert (root / "usr/lib/apk/db/installed").read_text() == NODEJS + "\n\n"


@pytest.mark.parametrize(
    "dependency",
    ["npm", "npm-12", "node-gyp", "npm>=12"],
)
def test_fails_when_a_kept_package_depends_on_a_removed_one(
    tmp_path: Path, dependency: str
) -> None:
    dependent = f"P:some-tool\nV:1.0-r0\nD:{dependency}\np:\nF:usr\nF:usr/bin\nR:some-tool"
    root = _rootfs(tmp_path, [NODEJS, NODE_GYP, NPM, dependent])
    result = _strip(root)
    assert result.returncode != 0
    assert "still depends on removed" in result.stderr
    assert (root / "usr/bin/npm").is_symlink()  # 何も消していない


def test_fails_when_a_listed_file_is_missing(tmp_path: Path) -> None:
    root = _rootfs(tmp_path, [NODEJS, NODE_GYP, NPM])
    (root / "usr/lib/node_modules/npm/package.json").unlink()
    result = _strip(root)
    assert result.returncode != 0
    assert "ENOENT" in result.stderr


def test_fails_when_a_package_is_not_in_the_base(tmp_path: Path) -> None:
    root = _rootfs(tmp_path, [NODEJS, NPM.replace("D:node-gyp", "D:")])
    result = _strip(root)
    assert result.returncode != 0
    assert "expected apk packages" in result.stderr


def test_keeps_a_directory_another_package_still_owns(tmp_path: Path) -> None:
    """空になっても、残すパッケージが F: で持つディレクトリは消さない（apk del と同じ）。"""
    keeper = "P:keeper\nV:1.0-r0\nD:\np:\nF:usr/lib/node_modules"
    root = _rootfs(tmp_path, [NODEJS, NODE_GYP, NPM, keeper])
    result = _strip(root)
    assert result.returncode == 0, result.stderr
    assert (root / "usr/lib/node_modules").is_dir()
    assert not (root / "usr/lib/node_modules/npm").exists()
