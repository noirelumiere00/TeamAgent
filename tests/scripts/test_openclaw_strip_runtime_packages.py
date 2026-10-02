"""strip-runtime-packages.mjs: OpenClaw runtime から npm-12 / node-gyp / busybox を apk del 相当で外す。

2026-10-01: npm-12・node-gyp（ECR ゲートの所見）→ 同日 busybox（/bin/sh と applet 218 個）を追加。

固定すること:
- apk installed db に載っている 3 パッケージのファイルだけを消し、db からも外す
- busybox の applet リンク（パッケージのファイルではなく trigger が作る）は、
  etc/busybox-paths.d/busybox の一覧と実リンクが一致するときだけ消す
- 他パッケージも持つディレクトリ（usr/bin 等）と、他パッケージのファイル・リンクは残す
- 残すパッケージが外す側に依存していたら（名前・provides どちらでも）ビルドを止める
- db と実ファイルが食い違う・外すパッケージが無い・結果に宙吊りリンクが残る、ならビルドを止める
- 絶対パスのリンク先は <rootfs> の中で解決する（builder 自身の / では解決しない）

実物（cgr.dev/chainguard/node 3de92bf8…）を写したフェイク: db 形式（P/V/D/p/F/R 行・空行区切り・
末尾空行・中間ディレクトリもすべて F: 行に載る）、/bin -> usr/bin、applet は /bin/busybox への絶対リンク。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra/openclaw/strip-runtime-packages.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")

BASELAYOUT = """P:wolfi-baselayout
V:20230201-r30
D:
p:
F:etc
R:os-release
F:usr
F:usr/bin
F:usr/lib
F:var/lib/db/sbom
R:wolfi-baselayout-20230201-r30.spdx.json"""

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
F:usr/lib/node_modules/npm/bin
R:npm-cli.js
R:npx-cli.js
F:usr/lib/node_modules/npm/node_modules
F:usr/lib/node_modules/npm/node_modules/brace-expansion
R:package.json
F:var/lib/db/sbom
R:npm-12-12.1.0-r2.spdx.json"""

BUSYBOX = """P:busybox
V:1.38.0-r2
D:so:libc.so.6
p:cmd:busybox=1.38.0-r2
F:etc
R:securetty
F:etc/busybox-paths.d
R:busybox
F:usr
F:usr/bin
R:busybox
F:var/lib/db/sbom
R:busybox-1.38.0-r2.spdx.json"""

APPLETS = ["usr/bin/sh", "usr/bin/ls", "usr/bin/env", "usr/bin/[["]
ALL = [BASELAYOUT, NODEJS, NODE_GYP, NPM, BUSYBOX]


def _rootfs(tmp_path: Path, blocks: list[str], applets: list[str] | None = None) -> Path:
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
    (root / "bin").symlink_to("usr/bin")  # 実物と同じ merged-usr
    (root / "lib").symlink_to("usr/lib")
    # 実物の npm/npx は node_modules への相対リンク
    for name, target in (("npm", "npm-cli.js"), ("npx", "npx-cli.js")):
        (root / "usr/bin" / name).unlink(missing_ok=True)
        (root / "usr/bin" / name).symlink_to(f"../lib/node_modules/npm/bin/{target}")
    if "P:busybox" in "\n".join(blocks):
        listed = APPLETS if applets is None else applets
        (root / "etc/busybox-paths.d/busybox").write_text("\n".join(listed) + "\n")
        for rel in APPLETS:
            (root / rel).symlink_to("/bin/busybox")
    return root


def _strip(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(SCRIPT), str(root)], capture_output=True, text=True, check=False
    )


def _gone(path: Path) -> bool:
    return not path.is_symlink() and not path.exists()


def test_removes_packages_applets_and_db_entries(tmp_path: Path) -> None:
    root = _rootfs(tmp_path, ALL)
    result = _strip(root)
    assert result.returncode == 0, result.stderr
    assert (
        '"removedPackages":["node-gyp-13.0.2-r1","npm-12-12.1.0-r2","busybox-1.38.0-r2"]'
        in result.stdout
    )
    assert '"removedBusyboxApplets":4' in result.stdout

    for gone in (
        "usr/bin/npm",
        "usr/bin/npx",
        "usr/bin/node-gyp",
        "usr/lib/node_modules",
        "usr/bin/busybox",
        *APPLETS,
        "etc/securetty",
        "etc/busybox-paths.d",
        "var/lib/db/sbom/npm-12-12.1.0-r2.spdx.json",
        "var/lib/db/sbom/node-gyp-13.0.2-r1.spdx.json",
        "var/lib/db/sbom/busybox-1.38.0-r2.spdx.json",
    ):
        assert _gone(root / gone), gone
    # 残すパッケージのファイル・共有ディレクトリ・merged-usr のリンクは残る
    assert (root / "usr/bin/node").read_text() == "x"
    assert (root / "etc/os-release").exists()
    assert (root / "var/lib/db/sbom/nodejs-26-26.10.0-r2.spdx.json").exists()
    assert (root / "bin").is_symlink()
    assert (root / "usr/lib/apk/db/installed").read_text() == f"{BASELAYOUT}\n\n{NODEJS}\n\n"


@pytest.mark.parametrize(
    "dependency",
    ["npm", "npm-12", "node-gyp", "npm>=12", "busybox", "cmd:busybox"],
)
def test_fails_when_a_kept_package_depends_on_a_removed_one(
    tmp_path: Path, dependency: str
) -> None:
    dependent = f"P:some-tool\nV:1.0-r0\nD:{dependency}\np:\nF:usr\nF:usr/bin\nR:some-tool"
    root = _rootfs(tmp_path, [*ALL, dependent])
    result = _strip(root)
    assert result.returncode != 0
    assert "still depends on removed" in result.stderr
    assert (root / "usr/bin/npm").is_symlink()  # 何も消していない
    assert (root / "usr/bin/sh").is_symlink()


def test_fails_when_a_listed_file_is_missing(tmp_path: Path) -> None:
    root = _rootfs(tmp_path, ALL)
    (root / "usr/lib/node_modules/npm/package.json").unlink()
    result = _strip(root)
    assert result.returncode != 0
    assert "ENOENT" in result.stderr


def test_fails_when_a_package_is_not_in_the_base(tmp_path: Path) -> None:
    root = _rootfs(tmp_path, [BASELAYOUT, NODEJS, NODE_GYP, NPM])
    result = _strip(root)
    assert result.returncode != 0
    assert "expected apk packages" in result.stderr


@pytest.mark.parametrize(
    "applets",
    [
        APPLETS[:-1],  # 実リンクがあるのに一覧に無い applet
        [*APPLETS, "usr/bin/vi"],  # 一覧にあるのに実リンクが無い applet
    ],
)
def test_fails_when_applet_list_and_links_differ(tmp_path: Path, applets: list[str]) -> None:
    root = _rootfs(tmp_path, ALL, applets=applets)
    result = _strip(root)
    assert result.returncode != 0
    assert "applet list and links differ" in result.stderr
    assert (root / "usr/bin/sh").is_symlink()  # 何も消していない


def test_fails_on_unexpected_busybox_link_target(tmp_path: Path) -> None:
    root = _rootfs(tmp_path, ALL)
    (root / "usr/bin/ls").unlink()
    (root / "usr/bin/ls").symlink_to("busybox")  # 相対リンク（実物は /bin/busybox だけ）
    result = _strip(root)
    assert result.returncode != 0
    assert "unexpected busybox link target" in result.stderr


def test_fails_when_a_remaining_symlink_dangles(tmp_path: Path) -> None:
    """外したパッケージの中を指す、他パッケージのリンクが残るならビルドを止める。"""
    root = _rootfs(tmp_path, ALL)
    (root / "usr/bin/corepack").symlink_to("../lib/node_modules/npm/bin/npm-cli.js")
    result = _strip(root)
    assert result.returncode != 0
    assert "dangling symlinks: /usr/bin/corepack" in result.stderr


def test_absolute_links_resolve_inside_rootfs(tmp_path: Path) -> None:
    """絶対リンクは builder の / ではなく <rootfs> で解決する。

    /usr/lib/node_modules/npm/package.json は <rootfs> にだけあり、削除後に消える。
    builder の / で解決していたら「元から宙吊り」と誤認して通してしまう。
    """
    root = _rootfs(tmp_path, ALL)
    (root / "usr/bin/nodejs").symlink_to(
        "/bin/node"
    )  # <rootfs>/usr/bin/node（/bin -> usr/bin 経由）
    (root / "usr/bin/ghost").symlink_to("/usr/lib/node_modules/npm/package.json")
    result = _strip(root)
    assert result.returncode != 0
    assert "dangling symlinks: /usr/bin/ghost" in result.stderr
    assert "nodejs" not in result.stderr


def test_keeps_a_directory_another_package_still_owns(tmp_path: Path) -> None:
    """空になっても、残すパッケージが F: で持つディレクトリは消さない（apk del と同じ）。"""
    keeper = "P:keeper\nV:1.0-r0\nD:\np:\nF:usr/lib/node_modules"
    root = _rootfs(tmp_path, [*ALL, keeper])
    result = _strip(root)
    assert result.returncode == 0, result.stderr
    assert (root / "usr/lib/node_modules").is_dir()
    assert not (root / "usr/lib/node_modules/npm").exists()


def test_links_that_already_dangle_in_the_base_are_allowed(tmp_path: Path) -> None:
    """実物の /etc/mtab -> ../proc/self/mounts は build 時に宙吊りだが実行時に有効。触らず通す。"""
    root = _rootfs(tmp_path, ALL)
    (root / "etc/mtab").symlink_to("../proc/self/mounts")
    result = _strip(root)
    assert result.returncode == 0, result.stderr
    assert (root / "etc/mtab").is_symlink()
