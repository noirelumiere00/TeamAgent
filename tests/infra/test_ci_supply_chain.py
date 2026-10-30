from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
FULL_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def test_python315_pytest_is_non_blocking_and_checks_all_three_locked_shards() -> None:
    jobs = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())["jobs"]
    experimental = jobs["python315-pytest"]
    stable = jobs["lockfile-pytest"]
    assert experimental["continue-on-error"] is True
    assert experimental["strategy"]["matrix"]["shard"] == [1, 2, 3]
    assert experimental["services"] == stable["services"]
    assert experimental["env"] == stable["env"]
    setup = next(
        s for s in experimental["steps"] if s.get("uses", "").startswith("actions/setup-python@")
    )
    assert setup["with"]["python-version"] == "3.15"
    install = next(
        s["run"]
        for s in experimental["steps"]
        if s.get("name") == "Install dependencies from uv.lock"
    )
    assert "uv export --frozen" in install
    assert "pip install --no-deps --require-hashes" in install
    assert "pip install --no-deps -e ." in install
    assert "--extra dev --extra mcp --extra media" in install
    run = next(
        s["run"]
        for s in experimental["steps"]
        if s.get("name") == "Pytest (duration-balanced shard)"
    )
    assert "--ci-shards=3 --ci-shard=${{ matrix.shard }}" in run
    upload = experimental["steps"][-1]
    assert upload["with"]["name"] == "pytest-lock-3.15-shard-${{ matrix.shard }}"
    summary = jobs["pytest-summary"]
    assert "python315-pytest" in summary["needs"]
    check = next(
        s
        for s in summary["steps"]
        if s.get("name") == "Verify experimental Python 3.15 shard inventory"
    )
    assert check["continue-on-error"] is True
    assert "pytest-lock-3.15-shard-*/manifest.json" in check["run"]
    gate = summary["steps"][-1]["run"]
    assert "python315-pytest" not in gate
    assert "needs.lint-and-test.result" in gate and "needs.lockfile-pytest.result" in gate
    assert "continue-on-error" not in stable


def test_third_party_actions_are_pinned_to_full_commit_shas() -> None:
    for workflow in WORKFLOWS.glob("*.y*ml"):
        for line_number, line in enumerate(workflow.read_text().splitlines(), start=1):
            match = re.match(r"\s*(?:-\s*)?uses:\s*([^\s#]+)", line)
            if match is None:
                continue

            action = match.group(1)
            if action.startswith("./"):
                continue
            _, separator, revision = action.rpartition("@")
            assert separator and FULL_COMMIT_SHA.fullmatch(revision), (
                f"{workflow.relative_to(ROOT)}:{line_number}: "
                "external actions must use a full commit SHA"
            )


def test_gitleaks_container_is_pinned_to_an_immutable_digest() -> None:
    workflow = (WORKFLOWS / "ci.yml").read_text()
    image = re.search(
        r"zricethezav/gitleaks:[^\s\\@]+@sha256:([0-9a-f]{64})\s+detect",
        workflow,
    )
    assert image is not None


def test_terraform_ci_proves_authoritative_lock_and_cache_free_offline_mirror() -> None:
    workflow = (WORKFLOWS / "ci.yml").read_text()
    terraform_job = workflow.split("\n  terraform:\n", 1)[1].split(
        "\n  smoke-test:\n",
        1,
    )[0]

    assert "TF_PLUGIN_CACHE_DIR:" not in terraform_job
    assert "actions/cache@" not in terraform_job
    assert terraform_job.count("unset TF_PLUGIN_CACHE_DIR") == 3
    assert terraform_job.count("TF_CLI_CONFIG_FILE: /dev/null") == 2
    assert "terraform providers lock \\" in terraform_job
    assert "-platform=darwin_arm64" in terraform_job
    assert terraform_job.count("-platform=linux_amd64") == 2
    assert terraform_job.count("-platform=linux_arm64") == 2
    assert "git diff --exit-code -- .terraform.lock.hcl" in terraform_job
    assert "-enable-plugin-cache" not in terraform_job
    assert "-fs-mirror" not in terraform_job
    assert "-net-mirror" not in terraform_job

    mirror = terraform_job.index("terraform providers mirror")
    offline_init = terraform_job.index(
        "terraform init -backend=false -input=false -lockfile=readonly"
    )
    provider_inventory = terraform_job.index(
        'required_providers="$(grep -Ec \'^provider "registry\\.terraform\\.io/hashicorp/[a-z0-9_-]+"\' .terraform.lock.hcl)"'
    )
    validate = terraform_job.index("terraform validate")
    assert mirror < offline_init < provider_inventory < validate
    assert "rm -rf .terraform provider-mirror" in terraform_job
    assert "rm -rf .terraform\n" in terraform_job
    assert terraform_job.count("find provider-mirror -type f") == 2
    assert 'test "$required_providers" -gt 0' in terraform_job
    assert (
        "test \"$(find provider-mirror -type f -name '*_linux_amd64.zip' | wc -l)\" "
        '-eq "$required_providers"'
    ) in terraform_job
    assert (
        "test \"$(find provider-mirror -type f -name '*_linux_arm64.zip' | wc -l)\" "
        '-eq "$required_providers"'
    ) in terraform_job
    assert "${{ github.workspace }}/infra/terraform/ci.terraformrc" in terraform_job

    cli_config = (ROOT / "infra" / "terraform" / "ci.terraformrc").read_text()
    assert "filesystem_mirror {" in cli_config
    assert 'include = ["registry.terraform.io/hashicorp/*"]' in cli_config
    assert re.search(r"(?m)^\s*direct\s*\{", cli_config) is None
