from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BUILDSPEC = ROOT / "infra/codebuild/apk-probe-buildspec.yml"
DOCKERFILE = "infra/docker/Dockerfile.teamagent-media-worker"
RUN_ID = "20261019-120000-123456abcdef"
BUCKET = "teamagent-dev-apk-probe-718959508629"


def _command() -> str:
    spec = yaml.safe_load(BUILDSPEC.read_text(encoding="utf-8"))
    assert spec["version"] == 0.2
    assert spec["env"]["shell"] == "bash"
    return spec["phases"]["build"]["commands"][0]


def test_runtime_target_needs_only_the_dockerfile_and_public_ca_bundle() -> None:
    dockerfile = (ROOT / DOCKERFILE).read_text(encoding="utf-8")
    runtime = dockerfile.split(" AS runtime-packages\n", 1)[1].split(
        "FROM runtime-packages AS python-deps", 1
    )[0]
    assert "\nCOPY " not in runtime and "\nADD " not in runtime
    assert "--mount=type=secret,id=teamagent_ca,required=false" in runtime
    assert "cat /etc/ssl/certs/ca-certificates.crt > /tmp/teamagent-build-ca.pem" in runtime
    assert "--secret" not in _command()


def _run_probe(
    tmp_path: Path,
    *,
    args: str = "LIBEXPAT_PACKAGE_VERSION=2.9.0-r0\n",
    members: tuple[tuple[str, bytes | str], ...] | None = None,
    docker_failure: str = "",
    lock: str | None = None,
    run_id: str = RUN_ID,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], Path]:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    with tarfile.open(inputs / "repo.tar.gz", "w:gz") as archive:
        for name, content in members or ((DOCKERFILE, b"FROM scratch\n"),):
            member = tarfile.TarInfo(name)
            if isinstance(content, str):
                member.type = tarfile.SYMTYPE
                member.linkname = content
                archive.addfile(member)
            else:
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    (inputs / "build-args.txt").write_text(args, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.jsonl"
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    fake = bin_dir / "fake.py"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, shutil, sys\n"
        "name = pathlib.Path(sys.argv[0]).name\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['FAKE_CALLS'], 'a') as f:\n"
        "    f.write(json.dumps([name, *args]) + '\\n')\n"
        "if name == 'uname':\n"
        "    print('aarch64')\n"
        "elif name == 'aws':\n"
        "    if args[:2] == ['s3', 'cp']:\n"
        "        source, target = args[2:4]\n"
        "        if source.startswith('s3://'):\n"
        "            source = pathlib.Path(os.environ['FAKE_INPUTS']) / source.rsplit('/', 1)[-1]\n"
        "        else:\n"
        "            target = pathlib.Path(os.environ['FAKE_OUTPUTS']) / target.rsplit('/', 1)[-1]\n"
        "        shutil.copyfile(source, target)\n"
        "    elif args[:2] == ['ecr', 'get-login-password']:\n"
        "        print('fake-password')\n"
        "    else:\n"
        "        raise SystemExit('unexpected fake AWS command')\n"
        "elif name == 'docker':\n"
        "    if args[0] == 'login':\n"
        "        assert sys.stdin.read() == 'fake-password\\n'\n"
        "    elif args[:3] == ['buildx', 'inspect', 'default']:\n"
        "        print('docker')\n"
        "    elif args[:2] == ['buildx', 'build']:\n"
        "        assert pathlib.Path('infra/docker/Dockerfile.teamagent-media-worker').is_file()\n"
        "        if os.environ.get('FAKE_DOCKER_FAILURE'):\n"
        "            print(os.environ['FAKE_DOCKER_FAILURE'], file=sys.stderr)\n"
        "            raise SystemExit(17)\n"
        "    elif args[0] == 'run':\n"
        "        print(os.environ['FAKE_LOCK'], end='')\n"
        "    elif args[:2] != ['buildx', 'version'] and args[:2] != ['buildx', 'prune'] and args != ['info']:\n"
        "        raise SystemExit('unexpected fake Docker command')\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    for command in ("aws", "docker", "uname"):
        (bin_dir / command).symlink_to(fake)
    script = tmp_path / "probe.sh"
    script.write_text(_command(), encoding="utf-8")
    environment = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "APK_PROBE_BUCKET": BUCKET,
        "APK_PROBE_REGISTRY": "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com",
        "APK_PROBE_RUN_ID": run_id,
        "FAKE_CALLS": str(calls),
        "FAKE_INPUTS": str(inputs),
        "FAKE_OUTPUTS": str(outputs),
        "FAKE_DOCKER_FAILURE": docker_failure,
        "FAKE_LOCK": lock if lock is not None else "".join(f"package-{i}-1-r0\n" for i in range(101)),
    }
    completed = subprocess.run(
        ["bash", str(script)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    recorded = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    return completed, recorded, outputs


@pytest.mark.parametrize("args", ["", "LIBEXPAT_PACKAGE_VERSION=2.9.0-r0\nZLIB_PACKAGE_VERSION=1.3.2-r1\n"])
def test_probe_builds_exact_runtime_target_and_publishes_inventory(tmp_path: Path, args: str) -> None:
    result, calls, outputs = _run_probe(tmp_path, args=args)
    assert result.returncode == 0, result.stdout + result.stderr
    build = next(call for call in calls if call[:3] == ["docker", "buildx", "build"])
    expected_args = [value for arg in args.splitlines() for value in ("--build-arg", arg)]
    assert build == [
        "docker", "buildx", "build", "--builder", "default", "--platform", "linux/arm64",
        "--file", DOCKERFILE, "--target", "runtime-packages", "--no-cache-filter",
        "runtime-packages", "--progress", "plain", *expected_args, "--load", "-t",
        f"media-apk-probe:{RUN_ID}", ".",
    ]
    assert ["docker", "buildx", "prune", "--builder", "default", "--filter", "type=exec.cachemount", "--force"] in calls
    assert ["docker", "run", "--rm", "--platform", "linux/arm64", "--entrypoint", "cat",
            f"media-apk-probe:{RUN_ID}", "/usr/share/teamagent/provenance/apk-installed.lock"] in calls
    assert len((outputs / "apk-installed.lock").read_text().splitlines()) == 101
    assert (outputs / "apk-build.log").is_file()
    s3_calls = [call for call in calls if call[:3] == ["aws", "s3", "cp"]]
    assert [call[3] for call in s3_calls[:2]] == [
        f"s3://{BUCKET}/inputs/{RUN_ID}/repo.tar.gz",
        f"s3://{BUCKET}/inputs/{RUN_ID}/build-args.txt",
    ]
    assert [call[4] for call in s3_calls[2:]] == [
        f"s3://{BUCKET}/outputs/{RUN_ID}/apk-installed.lock",
        f"s3://{BUCKET}/outputs/{RUN_ID}/apk-build.log",
    ]


def test_failed_build_preserves_complete_log_and_never_publishes_lock(tmp_path: Path) -> None:
    error = "ERROR: unable to select packages: libexpat breaks: pinned version missing"
    result, calls, outputs = _run_probe(tmp_path, docker_failure=error)
    assert result.returncode == 17
    assert error in (outputs / "apk-build.log").read_text()
    assert not (outputs / "apk-installed.lock").exists()
    assert not any(call[:2] == ["docker", "run"] for call in calls)


@pytest.mark.parametrize(
    "args",
    [
        "LIBEXPAT_PACKAGE_VERSION=$(touch should-not-exist)\n",
        "NODE_PACKAGE_VERSION=24.18.2-r0\n",
        "FFMPEG_PACKAGE_VERSION=8.1.3-r0\n",
        "PYTHON_PACKAGE_VERSION=3.14.9-r0\n",
        "ALPINE_EDGE_REPOSITORY=https://example.invalid\n",
        "\n",
    ],
)
def test_invalid_or_binary_arguments_fail_before_docker(tmp_path: Path, args: str) -> None:
    result, calls, outputs = _run_probe(tmp_path, args=args)
    assert result.returncode == 2
    assert "FATAL:" in (outputs / "apk-build.log").read_text()
    assert not any(call[0] == "docker" for call in calls)
    assert not (tmp_path / "should-not-exist").exists()


@pytest.mark.parametrize(
    "members",
    [
        (("../../outside", b"bad"),),
        ((f"/{DOCKERFILE}", b"bad"),),
        ((DOCKERFILE, "../../outside"),),
        ((DOCKERFILE, b"FROM scratch\n"), ("extra", b"bad")),
        ((DOCKERFILE, b"first"), (DOCKERFILE, b"second")),
    ],
)
def test_unexpected_tar_members_are_rejected_without_extraction(tmp_path: Path, members: tuple) -> None:
    result, calls, outputs = _run_probe(tmp_path, members=members)
    assert result.returncode != 0
    assert "FATAL:" in (outputs / "apk-build.log").read_text()
    assert not any(call[0] == "docker" for call in calls)
    assert not (outputs / "apk-installed.lock").exists()


@pytest.mark.parametrize("lock", ["", "package-1-r0\n", "x\n" * 100 + "unterminated"])
def test_invalid_lock_is_not_published(tmp_path: Path, lock: str) -> None:
    result, _, outputs = _run_probe(tmp_path, lock=lock)
    assert result.returncode != 0
    assert "FATAL: invalid generated apk-installed.lock" in (outputs / "apk-build.log").read_text()
    assert not (outputs / "apk-installed.lock").exists()


def test_invalid_run_id_cannot_choose_an_s3_key(tmp_path: Path) -> None:
    result, calls, outputs = _run_probe(tmp_path, run_id="../../outside")
    assert result.returncode == 2
    assert "FATAL: invalid APK_PROBE_RUN_ID" in result.stdout
    assert not calls
    assert not list(outputs.iterdir())
