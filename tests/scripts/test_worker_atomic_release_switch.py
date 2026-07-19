from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SWITCH = ROOT / "scripts" / "worker_atomic_release_switch.sh"
TRANSACTION_ID = "12345678-1234-4123-8123-123456789abc"


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def test_failed_new_release_readiness_atomically_restores_previous_release(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "teamagent"
    old_release = install_root / "releases" / ("a" * 64)
    new_digest = "b" * 64
    new_release = install_root / "releases" / new_digest
    for release in (old_release, new_release):
        (release / "app").mkdir(parents=True)
    (new_release / ".release-sha256").write_text(f"{new_digest}\n", encoding="utf-8")
    (new_release / "teamagent-bot.service").write_text("new bot unit\n", encoding="utf-8")
    (new_release / "teamagent-connect.service").write_text(
        "new connect unit\n",
        encoding="utf-8",
    )
    install_root.mkdir(exist_ok=True)
    (install_root / "current").symlink_to(old_release)
    prior_restart = "TEAMAGENT_HMAC_RESTART_NONCE=" + ("c" * 64) + "\n"
    (install_root / "restart.env").write_text(prior_restart, encoding="utf-8")
    systemd_root = tmp_path / "systemd"
    systemd_root.mkdir()
    (systemd_root / "teamagent-bot.service").write_text(
        "old bot unit\n",
        encoding="utf-8",
    )
    (systemd_root / "teamagent-connect.service").write_text(
        "old connect unit\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env python3
import sys
args = sys.argv[1:]
if args and args[0] == "show":
    print("222" if args[-1] == "teamagent-connect" else "111")
raise SystemExit(0)
""",
    )
    _executable(
        fake_bin / "curl",
        """#!/usr/bin/env python3
import os
from pathlib import Path
current = Path(os.environ["TEAMAGENT_INSTALL_ROOT"]) / "current"
raise SystemExit(0 if current.resolve() == Path(os.environ["OLD_RELEASE"]) else 22)
""",
    )
    _executable(
        fake_bin / "ss",
        """#!/usr/bin/env python3
print('LISTEN 0 128 127.0.0.1:8788 0.0.0.0:* users:(("python",pid=222,fd=7))')
""",
    )
    for name in ("fuser", "pkill", "sleep"):
        _executable(fake_bin / name, "#!/usr/bin/env sh\nexit 0\n")
    _executable(
        fake_bin / "mv",
        """#!/usr/bin/env python3
import os
import sys
args = sys.argv[1:]
if args[0] == "-Tf" and len(args) == 3:
    os.replace(args[1], args[2])
    raise SystemExit(0)
os.execv("/bin/mv", ["mv", *args])
""",
    )

    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEAMAGENT_INSTALL_ROOT": str(install_root),
        "TEAMAGENT_READY_ATTEMPTS": "1",
        "TEAMAGENT_READY_INTERVAL_S": "0",
        "TEAMAGENT_SYSTEMD_ROOT": str(systemd_root),
        "OLD_RELEASE": str(old_release),
    }
    result = subprocess.run(
        [
            "bash",
            str(SWITCH),
            "switch",
            TRANSACTION_ID,
            str(new_release),
            new_digest,
            "d" * 64,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert (install_root / "current").resolve() == old_release
    assert (install_root / "restart.env").read_text(encoding="utf-8") == prior_restart
    assert old_release.is_dir()
    assert new_release.is_dir()
    transaction = install_root / "release-transactions" / TRANSACTION_ID
    assert (transaction / "status").read_text(encoding="utf-8") == "rolled_back\n"
    assert (transaction / "previous-release").read_text(encoding="utf-8") == (f"{old_release}\n")
    assert (transaction / "previous-restart.env").read_text(encoding="utf-8") == prior_restart
    assert (systemd_root / "teamagent-bot.service").read_text(encoding="utf-8") == (
        "old bot unit\n"
    )
    assert (systemd_root / "teamagent-connect.service").read_text(encoding="utf-8") == (
        "old connect unit\n"
    )
    assert not (install_root / "release-transactions" / ".active").exists()
    assert "worker release transaction failed" not in result.stdout
