"""Core/media deploy contract and local evidence gates."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "infra/docker"
CONTRACT_PATH = DOCKER / "runtime-contract.json"
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
COMPOSE = (DOCKER / "compose.runtime-smoke.yml").read_text(encoding="utf-8")
BUILD = (DOCKER / "build_local_runtime_evidence.sh").read_text(encoding="utf-8")
RUN_SMOKES = (DOCKER / "run_runtime_smokes.sh").read_text(encoding="utf-8")


def test_contract_has_independent_arm64_runtime_tasks() -> None:
    assert CONTRACT["schema_version"] == "1"
    assert CONTRACT["architecture"] == "linux/arm64"
    assert set(CONTRACT["tasks"]) == {"teamagent-mcp-core", "teamagent-media-worker"}
    for task in CONTRACT["tasks"].values():
        assert task["uid"] == task["gid"] == 10001
        assert task["read_only_root_filesystem"] is True
        assert task["memory_limit_mib"] == 4096
        assert task["writable_mounts"] == [
            {"path": "/tmp", "kind": "fresh-named-volume", "mode": "1777"}
        ]
    core = CONTRACT["tasks"]["teamagent-mcp-core"]
    assert core["capabilities_drop"] == ["ALL"]
    assert core["no_new_privileges"] is True
    media = CONTRACT["tasks"]["teamagent-media-worker"]
    assert set(media["capabilities_drop"]) == {
        "AUDIT_WRITE",
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "FSETID",
        "KILL",
        "MKNOD",
        "NET_BIND_SERVICE",
        "NET_RAW",
        "SETFCAP",
        "SETGID",
        "SETPCAP",
        "SETUID",
    }
    assert media["capabilities_retain"] == ["SYS_CHROOT"]
    assert media["capabilities_add"] == []
    assert media["no_new_privileges"] is False


def test_media_worker_role_explicitly_excludes_core_authority_and_secrets() -> None:
    media = CONTRACT["tasks"]["teamagent-media-worker"]
    role = media["worker_role"]
    assert role["aws_clients"] == ["s3", "dynamodb"]
    assert set(role["required_actions"]) == {
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "dynamodb:UpdateItem",
    }
    assert {"bedrock:*", "rds:*", "rds-db:*", "secretsmanager:*", "ssm:*"} <= set(
        role["forbidden_actions"]
    )
    assert {"database", "slack", "oauth", "mcp-bearer", "vertex", "e5"} == set(
        role["forbidden_secret_domains"]
    )
    assert {
        "DATABASE_URL",
        "SLACK_BOT_TOKEN",
        "TEAMAGENT_MCP_BEARER",
        "VERTEX_CREDENTIALS",
    } <= set(media["forbidden_environment"])


def test_core_separates_baked_fallback_from_qa_s3_app_contract() -> None:
    app = CONTRACT["tasks"]["teamagent-mcp-core"]["app_html_contract"]
    assert app == {
        "production_source": "s3",
        "production_sha256": ("46f0079783cde24b066c7823b7d6672bad12b33debf933a4d7a7ff04b7a3b067"),
        "production_s3_version_id": "I1qOb7Kwl.pMg71wqFxbHnbbTqMWjQcY",
        "production_manifest_sha256": (
            "15663a838b1bd648443949244c02e66ccfd6cb7b684390baeb1a86efcdd6d4a2"
        ),
        "production_build_inputs_sha256": (
            "1ca6f0213155d8d4dbef4220f641dbb38310fe79473f6c013ef4e54dfa6a87e2"
        ),
        "baked_fallback_sha256": (
            "716ac25a96516efd6443277c903102d514f3f86729f8706baea41ee48f0ecdeb"
        ),
    }


def test_scan_gate_is_exact_zero_without_suppressions() -> None:
    trivy = CONTRACT["security_gates"]["trivy"]
    assert trivy == {
        "critical_vulnerabilities": 0,
        "high_vulnerabilities": 0,
        "secrets": 0,
        "allow_suppressions": False,
        "ignore_unfixed": False,
    }
    ecr = CONTRACT["security_gates"]["ecr_basic_scan"]
    assert ecr["critical_vulnerabilities"] == ecr["high_vulnerabilities"] == 0
    assert ecr["required_after_authorized_push"] is True
    assert ecr["fail_closed_until_complete"] is True


def test_media_sandbox_uses_exact_playwright_profile_and_fargate_is_fail_closed() -> None:
    sandbox = CONTRACT["tasks"]["teamagent-media-worker"]["chromium_sandbox"]
    assert sandbox["required"] is True
    assert sandbox["no_sandbox_flag_forbidden"] is True
    assert sandbox["additional_allowed_syscalls"] == ["clone", "setns", "unshare"]
    assert sandbox["required_capability"] == "SYS_CHROOT"
    assert sandbox["seccomp_profile_source"] == (
        "https://github.com/microsoft/playwright/blob/v1.60.0/utils/docker/seccomp_profile.json"
    )
    assert sandbox["requires_setuid_sandbox"] is True
    assert sandbox["fargate_actual_smoke_required"] is True
    assert sandbox["fargate_fail_closed_until_verified"] is True
    profile = ROOT / sandbox["seccomp_profile"]
    assert hashlib.sha256(profile.read_bytes()).hexdigest() == sandbox["seccomp_profile_sha256"]
    value = json.loads(profile.read_text(encoding="utf-8"))
    assert value["defaultAction"] == "SCMP_ACT_ERRNO"
    assert value["syscalls"][0]["names"] == ["clone", "setns", "unshare"]
    assert value["syscalls"][0]["comment"] == "Allow create user namespaces"


def test_compose_smokes_enforce_runtime_controls_and_urllib_health() -> None:
    assert COMPOSE.count("read_only: true") == 1  # shared anchor for all three services
    assert "platform: linux/arm64" in COMPOSE
    assert 'user: "10001:10001"' in COMPOSE
    assert "mem_limit: 4096m" in COMPOSE
    assert COMPOSE.count("cap_drop:\n      - ALL") == 2
    assert "      - SYS_CHROOT" not in COMPOSE
    for capability in (
        "AUDIT_WRITE",
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "FSETID",
        "KILL",
        "MKNOD",
        "NET_BIND_SERVICE",
        "NET_RAW",
        "SETFCAP",
        "SETGID",
        "SETPCAP",
        "SETUID",
    ):
        assert f"      - {capability}" in COMPOSE
    assert COMPOSE.count("no-new-privileges:true") == 2
    assert "seccomp=./playwright-seccomp-1.60.0.json" in COMPOSE
    assert "seccomp=unconfined" not in COMPOSE
    assert COMPOSE.count("network_mode: none") == 2
    assert "urllib.request.urlopen" in COMPOSE
    assert "/app/.venv/bin/python" in COMPOSE
    assert "command:\n      - /smoke/smoke_core.py" in COMPOSE
    for volume in ("core_health_tmp:/tmp", "core_smoke_tmp:/tmp", "media_smoke_tmp:/tmp"):
        assert volume in COMPOSE


def test_local_evidence_build_cannot_push_and_requires_exact_clean_head() -> None:
    assert "--load" in BUILD
    assert "--push" not in BUILD
    assert "aws " not in BUILD
    assert "status --porcelain" in BUILD
    assert "GIT_COMMIT=$HEAD" in BUILD
    assert "org.opencontainers.image.revision" in BUILD
    assert "--platform linux/arm64" in BUILD
    assert "--provenance=mode=max" in BUILD
    assert "--sbom=true" in BUILD
    assert "BAKED_APP_HTML_SHA256" in BUILD
    assert "APP_HTML_SOURCE=s3" in BUILD
    assert "--scanners vuln" in BUILD
    assert "--scanners secret" in BUILD
    assert 'git -C "$REPO_ROOT" archive "$HEAD"' in BUILD
    assert "source-tracked-trivy-secret.json" in BUILD
    assert "--exit-code 1" in BUILD
    assert "--skip-dirs" not in BUILD
    assert "--format cyclonedx" in BUILD
    assert "verify_trivy_zero.py" in BUILD
    assert "run_runtime_smokes.sh" in BUILD


def test_smoke_runner_checks_actual_read_only_memory_user_and_arm64() -> None:
    assert "{{.Architecture}}" in RUN_SMOKES
    assert "{{.HostConfig.ReadonlyRootfs}}" in RUN_SMOKES
    assert "{{.HostConfig.Memory}}" in RUN_SMOKES
    assert "{{.Config.User}}" in RUN_SMOKES
    assert "{{range .Mounts}}{{if .RW}}{{println .Destination}}" in RUN_SMOKES
    assert 'test "$writable_mounts" = "/tmp"' in RUN_SMOKES
    assert "CAP_SYS_CHROOT" not in RUN_SMOKES
    assert "CAP_AUDIT_WRITE" in RUN_SMOKES
    assert "seccomp=*unconfined" in RUN_SMOKES
    assert "{{json .Config.Volumes}}" in BUILD
    assert 'test "$volumes" = \'{"/tmp":{}}\'' in BUILD
    assert "true 4294967296 10001:10001" in RUN_SMOKES
    assert "down --volumes" in RUN_SMOKES


def test_python_smoke_and_scan_helpers_parse() -> None:
    for name in ("smoke_core.py", "smoke_media.py", "verify_trivy_zero.py"):
        ast.parse((DOCKER / name).read_text(encoding="utf-8"), filename=name)


def test_media_smoke_covers_browser_transform_download_and_cleanup() -> None:
    text = (DOCKER / "smoke_media.py").read_text(encoding="utf-8")
    for token in (
        "chromium_sandbox=True",
        "page.route",
        "ProxyOperation",
        "FrameOperation",
        "ThumbnailOperation",
        "SlidesOperation",
        "Presentation",
        "AcquireOperation",
        "list_extractor_classes",
        "shahid*.pyc",
        "assert list(jobs.iterdir()) == []",
    ):
        assert token in text
    node = (DOCKER / "smoke_media_node.mjs").read_text(encoding="utf-8")
    assert "chromiumSandbox: true" in node
    assert 'page.route("**/*"' in node
