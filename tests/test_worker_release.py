from __future__ import annotations

import os
from pathlib import Path

from teamagent.worker_release import seal_release, verify_release


def test_release_measurement_binds_files_symlinks_modes_and_immutability(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    app = root / "app"
    executable = app / ".venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"pinned-python-runtime")
    executable.chmod(0o755)
    (app / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (app / "module-link.py").symlink_to("module.py")

    digest, executable_digest = seal_release(
        root,
        final_path=Path("/opt/teamagent/releases/source-digest"),
        executable=executable,
    )

    assert executable_digest
    assert verify_release(root, expected_sha256=digest)
    assert os.stat(root).st_mode & 0o222 == 0
    assert os.stat(executable).st_mode & 0o222 == 0

    executable.chmod(0o755)
    executable.write_bytes(b"replaced-runtime")
    executable.chmod(0o555)
    assert not verify_release(root, expected_sha256=digest)


def test_release_verification_rejects_metadata_rewrite(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    executable = root / "app" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"runtime")
    executable.chmod(0o755)
    digest, _ = seal_release(
        root,
        final_path=Path("/opt/teamagent/releases/source-digest"),
        executable=executable,
    )

    manifest = root / ".release-tree.json"
    manifest.chmod(0o600)
    manifest.write_text('{"entries":[],"kind":"teamagent.worker-release-tree","schema":1}\n')
    manifest.chmod(0o400)

    assert not verify_release(root, expected_sha256=digest)
