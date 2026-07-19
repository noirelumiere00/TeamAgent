from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SWITCH = ROOT / "scripts" / "worker_atomic_release_switch.sh"
PROMOTION = ROOT / "scripts" / "worker_promotion_attest.sh"
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
    (new_release / ".release-tree-sha256").write_text(
        f"{new_digest}\n",
        encoding="utf-8",
    )
    input_digest = "d" * 64
    (new_release / ".release-input-sha256").write_text(
        f"{input_digest}\n",
        encoding="utf-8",
    )
    (new_release / "teamagent-bot.service").write_text("new bot unit\n", encoding="utf-8")
    (new_release / "teamagent-connect.service").write_text(
        "new connect unit\n",
        encoding="utf-8",
    )
    install_root.mkdir(exist_ok=True)
    (install_root / "current").symlink_to(old_release)
    prior_restart = "TEAMAGENT_HMAC_RESTART_NONCE=" + ("c" * 64) + "\n"
    (install_root / "restart.env").write_text(prior_restart, encoding="utf-8")
    prior_promotion = install_root / "promotion-attestation"
    prior_promotion.mkdir(mode=0o700)
    (prior_promotion / "prior.marker").write_text("prior\n", encoding="utf-8")
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
    for name in ("flock", "fuser", "pkill", "sleep"):
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
            input_digest,
            "e" * 64,
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
    assert (install_root / "promotion-attestation" / "prior.marker").read_text(
        encoding="utf-8"
    ) == "prior\n"
    assert (systemd_root / "teamagent-bot.service").read_text(encoding="utf-8") == (
        "old bot unit\n"
    )
    assert (systemd_root / "teamagent-connect.service").read_text(encoding="utf-8") == (
        "old connect unit\n"
    )
    assert not (install_root / "release-transactions" / ".active").exists()
    assert not list((install_root / "release-transactions").glob(f".{TRANSACTION_ID}.prepare.*"))
    assert "worker release transaction failed" not in result.stdout

    retry = subprocess.run(
        ["bash", str(SWITCH), "reconcile", TRANSACTION_ID, "rollback"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert retry.returncode == 0
    assert (install_root / "current").resolve() == old_release
    assert (transaction / "status").read_text(encoding="utf-8") == "rolled_back\n"


def test_ordinary_reboot_or_crash_start_has_no_promotion_nonce_loop(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "teamagent"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(fake_bin / "flock", "#!/usr/bin/env sh\nexit 0\n")
    log = tmp_path / "python.log"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEAMAGENT_INSTALL_ROOT": str(install_root),
        "PYTHON_LOG": str(log),
    }

    for _ in range(3):
        result = subprocess.run(
            ["bash", str(PROMOTION), "bot", str(os.getpid())],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert result.returncode == 0

    assert not log.exists()
    assert not (install_root / "promotion-attestation").exists()

    markers = install_root / "promotion-attestation"
    markers.mkdir(mode=0o700, parents=True)
    (markers / "bot.pending").symlink_to(markers / "missing")
    corrupted = subprocess.run(
        ["bash", str(PROMOTION), "bot", str(os.getpid())],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert corrupted.returncode == 1


def test_residual_marker_reattests_new_process_generation_until_commit(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "teamagent"
    release = install_root / "releases" / ("a" * 64)
    app = release / "app"
    python = app / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    (app / "scripts").mkdir()
    _executable(
        python,
        "#!/usr/bin/env sh\n"
        "printf '%s\\n' \"${TEAMAGENT_HMAC_RESTART_REQUIRE_COMPLETE:-0}\" "
        '>> "$PYTHON_LOG"\n'
        "exit 0\n",
    )
    (app / "scripts" / "load_secrets.sh").write_text("return 0\n", encoding="utf-8")
    for name in ("teamagent.env.base", "hmac.env", "runtime.env"):
        (release / name).write_text("APP_ENV=production\n", encoding="utf-8")
    install_root.mkdir(exist_ok=True)
    (install_root / "current").symlink_to(release)
    markers = install_root / "promotion-attestation"
    markers.mkdir(mode=0o700)
    pending = markers / "connect.pending"
    pending.write_text(
        "TEAMAGENT_HMAC_RESTART_NONCE=" + ("b" * 64) + "\n"
        f"TEAMAGENT_RELEASE_TRANSACTION_ID={TRANSACTION_ID}\n"
        "TEAMAGENT_PROMOTION_SERVICE=connect\n",
        encoding="utf-8",
    )
    pending.chmod(0o600)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(fake_bin / "flock", "#!/usr/bin/env sh\nexit 0\n")
    _executable(fake_bin / "curl", "#!/usr/bin/env sh\nexit 0\n")
    _executable(
        fake_bin / "ss",
        f"#!/usr/bin/env sh\nprintf '%s\\n' 'LISTEN users:((\"python\",pid={os.getpid()},fd=7))'\n",
    )
    _executable(fake_bin / "sleep", "#!/usr/bin/env sh\nexit 0\n")
    _executable(fake_bin / "stat", "#!/usr/bin/env sh\nprintf '0:600\\n'\n")
    log = tmp_path / "python.log"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEAMAGENT_INSTALL_ROOT": str(install_root),
        "PYTHON_LOG": str(log),
    }

    first = subprocess.run(
        ["bash", str(PROMOTION), "connect", str(os.getpid())],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert first.returncode == 0
    assert not pending.exists()
    assert (markers / "connect.attested").is_file()

    second = subprocess.run(
        ["bash", str(PROMOTION), "connect", str(os.getpid()), "commit"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert second.returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["0", "1"]


def test_reconcile_clears_only_same_attempt_pretransaction_crash_state(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "teamagent"
    transactions = install_root / "release-transactions"
    active = transactions / ".active"
    active.mkdir(parents=True)
    staging = transactions / f".{TRANSACTION_ID}.prepare.crashed"
    staging.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(fake_bin / "flock", "#!/usr/bin/env sh\nexit 0\n")
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEAMAGENT_INSTALL_ROOT": str(install_root),
    }

    reconciled = subprocess.run(
        ["bash", str(SWITCH), "reconcile", TRANSACTION_ID, "rollback"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert reconciled.returncode == 0
    assert not active.exists()
    assert not staging.exists()

    active.mkdir()
    (active / "transaction-id").write_text(
        "87654321-4321-4123-8123-123456789abc\n",
        encoding="utf-8",
    )
    foreign = subprocess.run(
        ["bash", str(SWITCH), "reconcile", TRANSACTION_ID, "rollback"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert foreign.returncode == 1
    assert active.is_dir()
