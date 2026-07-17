"""Offline contract tests for the CodeBuild image launcher (no AWS writes)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra" / "deploy" / "build_teamagent_image.sh"
PROVENANCE = ROOT / "infra" / "codebuild" / "source_provenance.py"
IMAGE_RESOLVER = ROOT / "infra" / "codebuild" / "resolve_ecr_image.py"
RUNTIME_CONTRACT_PATH = ROOT / "infra" / "codebuild" / "teamagent_runtime_contract.json"
READY_RUNTIME_CONTRACT_PATH = (
    ROOT / "tests" / "codebuild" / "fixtures" / "teamagent_runtime_contract.ready.json"
)
RUNTIME_CONTRACT = json.loads(READY_RUNTIME_CONTRACT_PATH.read_text(encoding="utf-8"))


def _load_provenance() -> object:
    spec = importlib.util.spec_from_file_location("launcher_test_provenance", PROVENANCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROVENANCE_MODULE = _load_provenance()
RUNTIME_CONTRACT_SHA256 = hashlib.sha256(READY_RUNTIME_CONTRACT_PATH.read_bytes()).hexdigest()
RUNTIME_LABELS = PROVENANCE_MODULE.runtime_expected_labels(
    RUNTIME_CONTRACT,
    RUNTIME_CONTRACT_SHA256,
)
APP_HTML_BYTES = b"<!doctype html><title>versioned fixture</title>\n"
APP_HTML_SHA256 = hashlib.sha256(APP_HTML_BYTES).hexdigest()

FAKE_AWS = r"""#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

args = sys.argv[1:]


def value(flag):
    return args[args.index(flag) + 1]


def image_values():
    config_bytes = Path(os.environ["FAKE_CONFIG"]).read_bytes()
    config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "size": len(config_bytes),
            "digest": config_digest,
        },
        "layers": [],
    }
    manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    image_digest = "sha256:" + hashlib.sha256(manifest_raw.encode()).hexdigest()
    return config_digest, manifest_raw, image_digest


record = {
    "args": args,
    "access_key": os.environ.get("AWS_ACCESS_KEY_ID"),
    "profile": os.environ.get("AWS_PROFILE"),
    "endpoint_url": os.environ.get("AWS_ENDPOINT_URL"),
    "endpoint_url_ecr": os.environ.get("AWS_ENDPOINT_URL_ECR"),
    "ignore_configured_endpoints": os.environ.get("AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"),
}
if args[:2] == ["codebuild", "start-build"]:
    env_path = value("--environment-variables-override").removeprefix("file://")
    record["environment"] = json.loads(Path(env_path).read_text())
with Path(os.environ["FAKE_AWS_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\n")

if args[:2] == ["sts", "get-caller-identity"]:
    if os.environ.get("AWS_ACCESS_KEY_ID") == "ASIAFIXTURELAUNCHER":
        account = os.environ.get("FAKE_SESSION_ACCOUNT_ID", "718959508629")
        arn = os.environ.get(
            "FAKE_SESSION_ARN",
            "arn:aws:sts::718959508629:assumed-role/"
            "teamagent-dev-codebuild-launcher/teamagent-build-launcher",
        )
    else:
        account = os.environ.get("FAKE_ACCOUNT_ID", "718959508629")
        arn = os.environ.get("FAKE_CALLER_ARN", "arn:aws:iam::718959508629:user/AIIAdev")
    print(f"{account}\t{arn}")
elif args[:2] == ["sts", "assume-role"]:
    print("ASIAFIXTURELAUNCHER\tfixture-secret\tfixture-token\t2099-01-01T00:00:00Z")
elif args[:2] == ["s3api", "get-bucket-versioning"]:
    print(os.environ.get("FAKE_APP_BUCKET_VERSIONING", "Enabled"))
elif args[:2] == ["s3api", "head-object"]:
    print(os.environ.get("FAKE_APP_VERSION", "app-version-456"))
elif args[:2] == ["s3api", "get-object"]:
    shutil.copyfile(os.environ["FAKE_APP_HTML"], args[-1])
    print(os.environ.get("FAKE_DOWNLOADED_APP_VERSION", os.environ["FAKE_APP_VERSION"]))
elif args[:2] == ["s3api", "put-object"]:
    shutil.copyfile(value("--body"), os.environ["CAPTURE_ZIP"])
    print(os.environ.get("FAKE_SOURCE_VERSION", "source-version-123"))
elif args[:2] == ["codebuild", "start-build"]:
    print("fixture-project:11111111-2222-3333-4444-555555555555")
elif args[:2] == ["codebuild", "batch-get-builds"]:
    resolved = os.environ.get(
        "FAKE_RESOLVED_VERSION",
        os.environ.get("FAKE_SOURCE_VERSION", "source-version-123"),
    )
    print(f"{os.environ.get('FAKE_BUILD_STATUS', 'SUCCEEDED')}\t{resolved}")
elif args[:2] == ["ecr", "describe-images"]:
    _config_digest, _manifest_raw, image_digest = image_values()
    repository = value("--repository-name")
    if repository == "teamagent-mcp":
        print(os.environ.get("FAKE_RELEASE_DIGEST", image_digest))
    else:
        print(image_digest)
elif args[:2] == ["ecr", "batch-get-image"]:
    _config_digest, manifest_raw, image_digest = image_values()
    print(json.dumps({
        "images": [{"imageId": {"imageDigest": image_digest}, "imageManifest": manifest_raw}],
        "failures": [],
    }))
elif args[:2] == ["ecr", "get-download-url-for-layer"]:
    print("https://example.invalid/private-oci-config")
else:
    print(f"unexpected fake aws command: {args}", file=sys.stderr)
    raise SystemExit(91)
"""

FAKE_CURL = r"""#!/usr/bin/env python3
import os
import shutil
import sys

args = sys.argv[1:]
output = args[args.index("--output") + 1]
shutil.copyfile(os.environ["FAKE_CONFIG"], output)
"""


def _run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _fixture(
    tmp_path: Path,
    *,
    profile_label: str = "true",
    app_sha_label: str = APP_HTML_SHA256,
    app_version_label: str = "app-version-456",
) -> tuple[Path, str, dict[str, str], Path, Path]:
    repo = tmp_path / "repo"
    (repo / "infra" / "deploy").mkdir(parents=True)
    (repo / "infra" / "codebuild").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "infra" / "deploy" / SCRIPT.name)
    shutil.copy2(PROVENANCE, repo / "infra" / "codebuild" / PROVENANCE.name)
    shutil.copy2(IMAGE_RESOLVER, repo / "infra" / "codebuild" / IMAGE_RESOLVER.name)
    shutil.copy2(
        READY_RUNTIME_CONTRACT_PATH,
        repo / "infra" / "codebuild" / RUNTIME_CONTRACT_PATH.name,
    )
    (repo / ".gitignore").write_text("ignored-secret.env\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("archive me\n", encoding="utf-8")
    _run_git(repo, "init", "-b", "dev")
    _run_git(repo, "config", "user.name", "CodeBuild Test")
    _run_git(repo, "config", "user.email", "codebuild-test@example.invalid")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "fixture")
    commit = _run_git(repo, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=dev", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_git(repo, "remote", "add", "origin", "git@github.com:noirelumiere00/TeamAgent.git")
    _run_git(
        repo,
        "config",
        f"url.file://{remote}.insteadOf",
        "git@github.com:noirelumiere00/TeamAgent.git",
    )
    _run_git(repo, "push", "origin", "dev")
    (repo / "ignored-secret.env").write_text("DO_NOT_ARCHIVE=secret-value\n", encoding="utf-8")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "aws", FAKE_AWS)
    _write_executable(fake_bin / "curl", FAKE_CURL)
    log_path = tmp_path / "aws.jsonl"
    captured_zip = tmp_path / "captured-source.zip"
    app_html_path = tmp_path / "versioned-app.html"
    app_html_path.write_bytes(APP_HTML_BYTES)
    config_path = tmp_path / "config.json"
    config = {
        "architecture": "arm64",
        "os": "linux",
        "config": {
            "Labels": {
                "org.opencontainers.image.revision": commit,
                "io.teamagent.build.with-scrape-tools": profile_label,
                "io.teamagent.build.app-html-sha256": app_sha_label,
                "io.teamagent.build.app-html-version-id": app_version_label,
                **RUNTIME_LABELS,
            }
        },
    }
    config_path.write_text(
        json.dumps(config, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "FAKE_AWS_LOG": str(log_path),
            "FAKE_CONFIG": str(config_path),
            "FAKE_APP_HTML": str(app_html_path),
            "CAPTURE_ZIP": str(captured_zip),
            "FAKE_APP_VERSION": "app-version-456",
            "FAKE_SOURCE_VERSION": "source-version-123",
            "AWS_ACCESS_KEY_ID": "AKIAFIXTUREUSER",
            "AWS_SECRET_ACCESS_KEY": "fixture-user-secret",
        }
    )
    return repo, commit, env, log_path, captured_zip


def _args(repo: Path, *, tag: str = "candidate-123", profile: str = "true") -> list[str]:
    return [
        "bash",
        str(repo / "infra" / "deploy" / SCRIPT.name),
        "--image-tag",
        tag,
        "--with-scrape-tools",
        profile,
        "--poll-seconds",
        "1",
        "--timeout-seconds",
        "30",
    ]


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _record(records: list[dict[str, Any]], prefix: list[str]) -> dict[str, Any]:
    return next(record for record in records if record["args"][: len(prefix)] == prefix)


def test_bash_syntax_is_valid() -> None:
    completed = _run(["bash", "-n", str(SCRIPT)], cwd=ROOT)
    assert completed.returncode == 0, completed.stderr


def test_scrape_profile_is_required_and_has_no_default() -> None:
    completed = _run(["bash", str(SCRIPT), "--image-tag", "candidate-1"], cwd=ROOT)

    assert completed.returncode != 0
    assert "--with-scrape-tools true is required" in completed.stderr


def test_thin_profile_is_rejected_before_any_aws_call() -> None:
    completed = _run(
        [
            "bash",
            str(SCRIPT),
            "--image-tag",
            "candidate-1",
            "--with-scrape-tools",
            "false",
        ],
        cwd=ROOT,
    )

    assert completed.returncode != 0
    assert "must be explicitly set to true" in completed.stderr


@pytest.mark.parametrize(
    "option",
    ("--region", "--source-bucket", "--project-name", "--repository-name"),
)
def test_production_endpoints_cannot_be_overridden(option: str) -> None:
    completed = _run(
        [
            "bash",
            str(SCRIPT),
            "--image-tag",
            "candidate-1",
            "--with-scrape-tools",
            "true",
            option,
            "attacker-controlled",
        ],
        cwd=ROOT,
    )

    assert completed.returncode != 0
    assert f"unknown argument: {option}" in completed.stderr


@pytest.mark.parametrize("tag", ["bad/tag", "-leading-dash", "space tag", "x" * 129])
def test_unsafe_image_tag_fails_before_any_aws_call(tag: str) -> None:
    completed = _run(
        [
            "bash",
            str(SCRIPT),
            "--image-tag",
            tag,
            "--with-scrape-tools",
            "true",
        ],
        cwd=ROOT,
    )

    assert completed.returncode != 0
    assert "unsafe image tag" in completed.stderr


def test_dirty_or_untracked_worktree_fails_before_upload(tmp_path: Path) -> None:
    repo, _commit, env, log_path, _captured_zip = _fixture(tmp_path)
    (repo / "untracked-sensitive.txt").write_text("not uploaded\n", encoding="utf-8")

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "Git worktree is dirty" in completed.stderr
    assert _records(log_path) == []


def test_wrong_aws_account_fails_before_any_remote_write(tmp_path: Path) -> None:
    repo, _commit, env, log_path, _captured_zip = _fixture(tmp_path)
    env["FAKE_ACCOUNT_ID"] = "000000000000"

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "refusing to build in AWS account 000000000000" in completed.stderr
    records = _records(log_path)
    assert [record["args"][:2] for record in records] == [["sts", "get-caller-identity"]]


def test_wrong_initial_caller_fails_before_assume_role(tmp_path: Path) -> None:
    repo, _commit, env, log_path, _captured_zip = _fixture(tmp_path)
    env["FAKE_CALLER_ARN"] = "arn:aws:iam::718959508629:user/NotAIIAdev"

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "launcher must start as arn:aws:iam::718959508629:user/AIIAdev" in completed.stderr
    assert [record["args"][:2] for record in _records(log_path)] == [["sts", "get-caller-identity"]]


def test_non_dev_or_diverged_dev_fails_before_aws(tmp_path: Path) -> None:
    repo, commit, env, log_path, _captured_zip = _fixture(tmp_path)
    _run_git(repo, "branch", "-m", "feature/unsafe")

    wrong_branch = _run(_args(repo), cwd=repo, env=env)

    assert wrong_branch.returncode != 0
    assert "local dev branch" in wrong_branch.stderr
    assert _records(log_path) == []

    _run_git(repo, "branch", "-m", "dev")
    (repo / "tracked.txt").write_text("local-only commit\n", encoding="utf-8")
    _run_git(repo, "add", "tracked.txt")
    _run_git(repo, "commit", "-m", "not pushed")
    assert _run_git(repo, "rev-parse", "HEAD") != commit

    diverged = _run(_args(repo), cwd=repo, env=env)

    assert diverged.returncode != 0
    assert "must exactly equal remote origin/dev" in diverged.stderr
    assert _records(log_path) == []


def test_git_archive_version_pin_env_binding_and_remote_labels(tmp_path: Path) -> None:
    repo, commit, env, log_path, captured_zip = _fixture(tmp_path)

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    records = _records(log_path)
    identities = [
        record for record in records if record["args"][:2] == ["sts", "get-caller-identity"]
    ]
    assert len(identities) == 2
    assert identities[0]["args"][identities[0]["args"].index("--query") + 1] == ("[Account,Arn]")
    assume = _record(records, ["sts", "assume-role"])
    assert assume["args"][assume["args"].index("--role-arn") + 1] == (
        "arn:aws:iam::718959508629:role/teamagent-dev-codebuild-launcher"
    )
    assert assume["args"][assume["args"].index("--role-session-name") + 1] == (
        "teamagent-build-launcher"
    )
    assume_index = records.index(assume)
    assert all(
        record["access_key"] == "ASIAFIXTURELAUNCHER" for record in records[assume_index + 1 :]
    )
    versioning = _record(records, ["s3api", "get-bucket-versioning"])
    assert versioning["args"][versioning["args"].index("--bucket") + 1] == (
        "teamagent-dev-raw-files"
    )
    assert versioning["args"][versioning["args"].index("--expected-bucket-owner") + 1] == (
        "718959508629"
    )
    head = _record(records, ["s3api", "head-object"])
    assert head["args"][head["args"].index("--key") + 1] == ("codebuild/connect-web-app.html")
    get = _record(records, ["s3api", "get-object"])
    assert get["args"][get["args"].index("--version-id") + 1] == "app-version-456"
    put = _record(records, ["s3api", "put-object"])
    assert put["args"][put["args"].index("--key") + 1] == "codebuild/source.zip"
    assert put["args"][put["args"].index("--content-type") + 1] == "application/zip"
    assert put["args"][put["args"].index("--server-side-encryption") + 1] == "AES256"
    start = _record(records, ["codebuild", "start-build"])
    assert start["args"][start["args"].index("--source-version") + 1] == ("source-version-123")
    assert "--buildspec-override" not in start["args"]
    poll = _record(records, ["codebuild", "batch-get-builds"])
    query = poll["args"][poll["args"].index("--query") + 1]
    assert "sourceVersion" in query
    assert "resolvedSourceVersion" not in query
    overrides = {item["name"]: item["value"] for item in start["environment"]}
    assert overrides == {
        "GIT_COMMIT": commit,
        "GIT_BRANCH": "dev",
        "IMAGE_TAG": "candidate-123",
        "WITH_SCRAPE_TOOLS": "true",
        "APP_HTML_VERSION_ID": "app-version-456",
        "APP_HTML_SHA256": APP_HTML_SHA256,
        "RUNTIME_CONTRACT_SHA256": RUNTIME_CONTRACT_SHA256,
    }
    assert all(item["type"] == "PLAINTEXT" for item in start["environment"])

    with zipfile.ZipFile(captured_zip) as source_zip:
        names = set(source_zip.namelist())
        manifest = json.loads(source_zip.read(".teamagent-source-manifest.json"))
    assert "tracked.txt" in names
    assert "ignored-secret.env" not in names
    assert not any(name == ".git" or name.startswith(".git/") for name in names)
    assert manifest["schema_version"] == 3
    assert manifest["commit"] == commit
    assert manifest["branch"] == "dev"
    assert manifest["build_parameters"] == {
        "with_scrape_tools": True,
        "app_html": {
            "bucket": "teamagent-dev-raw-files",
            "key": "codebuild/connect-web-app.html",
            "version_id": "app-version-456",
            "sha256": APP_HTML_SHA256,
        },
        "runtime_contract": {
            "path": "infra/codebuild/teamagent_runtime_contract.json",
            "sha256": RUNTIME_CONTRACT_SHA256,
        },
    }
    assert "private-oci-config" not in completed.stdout + completed.stderr
    for secret in ("fixture-user-secret", "fixture-secret", "fixture-token"):
        assert secret not in completed.stdout + completed.stderr
    assert "verified_quarantine_digest=sha256:" in completed.stdout
    assert "release_digest=sha256:" in completed.stdout
    assert "no deployment performed" in completed.stdout
    assert not any(record["args"][0] in {"ecs", "events"} for record in records)


def test_hostile_aws_endpoint_and_repository_environment_is_ignored(tmp_path: Path) -> None:
    repo, _commit, env, log_path, _captured_zip = _fixture(tmp_path)
    env.update(
        {
            "AWS_ENDPOINT_URL": "https://attacker.invalid",
            "AWS_ENDPOINT_URL_ECR": "https://ecr.attacker.invalid",
            "ECR_REGISTRY": "attacker.invalid",
            "MCP_REPO": "attacker.invalid/repository",
        }
    )

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    records = _records(log_path)
    assert records
    assert all(record["endpoint_url"] is None for record in records)
    assert all(record["endpoint_url_ecr"] is None for record in records)
    assert all(record["ignore_configured_endpoints"] == "true" for record in records)
    ecr_records = [record for record in records if record["args"][0] == "ecr"]
    assert {
        record["args"][record["args"].index("--registry-id") + 1] for record in ecr_records
    } == {"718959508629"}
    assert {
        record["args"][record["args"].index("--repository-name") + 1] for record in ecr_records
    } == {"teamagent-mcp", "teamagent-mcp-quarantine"}


def test_missing_s3_version_id_fails_before_start_build(tmp_path: Path) -> None:
    repo, _commit, env, log_path, _captured_zip = _fixture(tmp_path)
    env["FAKE_SOURCE_VERSION"] = "None"

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "S3 did not return a usable VersionId" in completed.stderr
    assert not any(
        record["args"][:2] == ["codebuild", "start-build"] for record in _records(log_path)
    )


def test_source_bucket_without_enabled_versioning_fails_before_object_lookup(
    tmp_path: Path,
) -> None:
    repo, _commit, env, log_path, _captured_zip = _fixture(tmp_path)
    env["FAKE_APP_BUCKET_VERSIONING"] = "Suspended"

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "source/app S3 bucket versioning must be Enabled" in completed.stderr
    records = _records(log_path)
    assert not any(record["args"][:2] == ["s3api", "head-object"] for record in records)
    assert not any(record["args"][:2] == ["s3api", "put-object"] for record in records)


def test_app_object_without_version_id_fails_before_source_upload(tmp_path: Path) -> None:
    repo, _commit, env, log_path, _captured_zip = _fixture(tmp_path)
    env["FAKE_APP_VERSION"] = "None"

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "app.html S3 object did not return a usable VersionId" in completed.stderr
    assert not any(
        record["args"][:2] in (["s3api", "get-object"], ["s3api", "put-object"])
        for record in _records(log_path)
    )


def test_app_downloaded_version_mismatch_fails_before_source_upload(tmp_path: Path) -> None:
    repo, _commit, env, log_path, _captured_zip = _fixture(tmp_path)
    env["FAKE_DOWNLOADED_APP_VERSION"] = "different-app-version"

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "downloaded app.html VersionId does not match" in completed.stderr
    assert not any(record["args"][:2] == ["s3api", "put-object"] for record in _records(log_path))


def test_build_source_version_mismatch_fails_before_ecr_lookup(tmp_path: Path) -> None:
    repo, _commit, env, log_path, _captured_zip = _fixture(tmp_path)
    env["FAKE_RESOLVED_VERSION"] = "different-version"

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "sourceVersion does not match" in completed.stderr
    assert not any(record["args"][0] == "ecr" for record in _records(log_path))


def test_failed_build_never_reaches_ecr_verification(tmp_path: Path) -> None:
    repo, _commit, env, log_path, _captured_zip = _fixture(tmp_path)
    env["FAKE_BUILD_STATUS"] = "FAILED"

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "candidate failed with status FAILED" in completed.stderr
    assert not any(record["args"][0] == "ecr" for record in _records(log_path))


def test_release_digest_must_equal_verified_quarantine_digest(tmp_path: Path) -> None:
    repo, _commit, env, log_path, _captured_zip = _fixture(tmp_path)
    env["FAKE_RELEASE_DIGEST"] = "sha256:" + "f" * 64

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "release digest does not equal the verified quarantine digest" in completed.stderr
    release_reads = [
        record
        for record in _records(log_path)
        if record["args"][:2] == ["ecr", "batch-get-image"]
        and record["args"][record["args"].index("--repository-name") + 1] == "teamagent-mcp"
    ]
    assert release_reads == []


def test_remote_candidate_scrape_label_mismatch_fails_closed(tmp_path: Path) -> None:
    repo, _commit, env, _log_path, _captured_zip = _fixture(tmp_path, profile_label="false")

    completed = _run(_args(repo, profile="true"), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "io.teamagent.build.with-scrape-tools mismatch" in completed.stderr


def test_remote_candidate_app_sha_label_mismatch_fails_closed(tmp_path: Path) -> None:
    repo, _commit, env, _log_path, _captured_zip = _fixture(
        tmp_path,
        app_sha_label="f" * 64,
    )

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "io.teamagent.build.app-html-sha256 mismatch" in completed.stderr


def test_remote_candidate_app_version_label_mismatch_fails_closed(tmp_path: Path) -> None:
    repo, _commit, env, _log_path, _captured_zip = _fixture(
        tmp_path,
        app_version_label="different-app-version",
    )

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "io.teamagent.build.app-html-version-id mismatch" in completed.stderr
