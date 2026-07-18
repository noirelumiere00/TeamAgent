"""Core/media deploy contract and local evidence gates."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "infra/docker"
CONTRACT_PATH = DOCKER / "runtime-contract.json"
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
CONSUMERS = json.loads((DOCKER / "runtime-consumers.json").read_text(encoding="utf-8"))
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
    assert core["local_compose_no_new_privileges"] is True
    assert core["fargate_no_new_privileges_claimed"] is False
    media = CONTRACT["tasks"]["teamagent-media-worker"]
    assert media["capabilities_drop"] == ["ALL"]
    assert media["capabilities_add"] == []
    assert media["local_compose_no_new_privileges"] is False
    assert media["fargate_no_new_privileges_claimed"] is False


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


def test_ytdlp_sanitization_is_hash_fixed_to_the_three_site_allowlist() -> None:
    sanitation = CONTRACT["tasks"]["teamagent-media-worker"]["yt_dlp_sanitization"]
    assert sanitation["version"] == "2026.6.9"
    assert sanitation["allowlisted_sites"] == ["youtube", "tiktok", "instagram"]
    assert sanitation["removed_secret_bearing_extractors"] == [
        "adultswim",
        "aenetworks",
        "blackboardcollaborate",
        "cloudflarestream",
        "espn",
        "go",
        "nbc",
        "shahid",
        "tbs",
        "vice",
    ]
    assert sanitation["removed_extractor_set_sha256"] == (
        "ea414688b508a2a77bf006e5928536603a51e7ab3b8664c13dd6d21b1140b80b"
    )
    assert (
        sanitation["sanitizer_sha256"]
        == hashlib.sha256((DOCKER / "sanitize_ytdlp.py").read_bytes()).hexdigest()
    )
    assert sanitation["sanitized_source_tree_sha256"] == (
        "638d0864a2551a143f29fc8dbe1b4da6aa8dcfb9392f1a8907a6e07f7a05118b"
    )


def test_core_separates_baked_fallback_from_qa_s3_app_contract() -> None:
    app = CONTRACT["tasks"]["teamagent-mcp-core"]["app_html_contract"]
    assert app == {
        "production_source": "s3",
        "production_sha256": ("03f8e8cc0adbc397cc636e30fcc8baaffeb1c53502cf74baf1031399cceb391c"),
        "production_s3_version_id": "FTXbcN70D0DCN90TI_hRK1IdQK_HhLee",
        "production_manifest_sha256": (
            "aa451e744d26e9dc13c170b019307b0eb10d3645267960fbff41c4038e9b909e"
        ),
        "production_build_inputs_sha256": (
            "6697acf311f0c9a96b41426e81ae05ad221482a6e6f69799281ad3532c2e78bf"
        ),
        "baked_fallback_sha256": (
            "716ac25a96516efd6443277c903102d514f3f86729f8706baea41ee48f0ecdeb"
        ),
    }


def test_scan_gate_is_exact_zero_without_suppressions() -> None:
    trivy = CONTRACT["security_gates"]["trivy"]
    assert trivy["critical_vulnerabilities"] == 0
    assert trivy["high_vulnerabilities"] == 0
    assert trivy["secrets"] == 0
    assert trivy["allow_suppressions"] is False
    assert trivy["ignore_unfixed"] is False
    assert trivy["required_absent_live_cves"] == [
        "CVE-2026-5450",
        "CVE-2026-13221",
        "CVE-2026-12087",
        "CVE-2026-57433",
    ]
    assert {
        "scan_started_at",
        "scan_finished_at",
        "trivy_version",
        "vulnerability_db_version",
        "vulnerability_db_updated_at",
        "vulnerability_db_downloaded_at",
        "vulnerability_db_next_update",
        "secret_check_bundle_digest",
    } == set(trivy["scanner_receipt_fields"])
    ecr = CONTRACT["security_gates"]["ecr_basic_scan"]
    assert ecr["critical_vulnerabilities"] == ecr["high_vulnerabilities"] == 0
    assert ecr["required_after_authorized_push"] is True
    assert ecr["fail_closed_until_complete"] is True


def test_media_cleanup_window_is_machine_readable_and_fenced() -> None:
    jobs = CONTRACT["media_jobs"]
    assert jobs["schema_version"] == "1"
    assert jobs["maximum_envelope_bytes"] == 128 * 1024
    assert jobs["minimum_artifact_ttl_seconds"] == 300
    assert jobs["maximum_artifact_ttl_seconds"] == 21600
    assert jobs["authoritative_cleanup"] == "owner-version-fenced-scheduled-janitor"
    assert jobs["lifecycle_backstop_days"] == 1
    assert jobs["worker_failure_cleanup_scope"] == "owned-attempt-only"
    assert jobs["synchronous_consumer_deletes_shared_state"] is False


def test_media_sandbox_uses_exact_playwright_profile_and_fargate_is_fail_closed() -> None:
    sandbox = CONTRACT["tasks"]["teamagent-media-worker"]["chromium_sandbox"]
    assert sandbox["required"] is True
    assert sandbox["no_sandbox_flag_forbidden"] is True
    assert sandbox["setuid_sandbox_disabled"] is True
    assert sandbox["namespace_sandbox_required"] is True
    assert sandbox["additional_allowed_syscalls"] == ["clone", "setns", "unshare"]
    assert sandbox["required_capability"] is None
    assert sandbox["seccomp_profile_source"] == (
        "https://github.com/microsoft/playwright/blob/v1.60.0/utils/docker/seccomp_profile.json"
    )
    assert sandbox["requires_setuid_sandbox"] is False
    assert sandbox["custom_seccomp_is_local_smoke_only"] is True
    assert sandbox["fargate_actual_smoke_required"] is True
    assert sandbox["fargate_fail_closed_until_verified"] is True
    profile = ROOT / sandbox["seccomp_profile"]
    assert hashlib.sha256(profile.read_bytes()).hexdigest() == sandbox["seccomp_profile_sha256"]
    value = json.loads(profile.read_text(encoding="utf-8"))
    assert value["defaultAction"] == "SCMP_ACT_ERRNO"
    assert value["syscalls"][0]["names"] == ["clone", "setns", "unshare"]
    assert value["syscalls"][0]["comment"] == "Allow create user namespaces"


def test_compose_smokes_enforce_runtime_controls_and_urllib_health() -> None:
    assert COMPOSE.count("read_only: true") == 1  # shared anchor for every service
    assert "platform: linux/arm64" in COMPOSE
    assert 'user: "10001:10001"' in COMPOSE
    assert "mem_limit: 4096m" in COMPOSE
    assert COMPOSE.count("cap_drop:\n      - ALL") == 9
    assert """test "$cap_drop" = '["ALL"]'""" in RUN_SMOKES
    assert "CAP_ALL" not in RUN_SMOKES
    assert "*no-new-privileges:true*" in RUN_SMOKES
    assert "no-new-privileges=true" not in RUN_SMOKES
    assert "sed '/^[[:space:]]*$/d'" in RUN_SMOKES
    assert "      - SYS_CHROOT" not in COMPOSE
    assert COMPOSE.count("no-new-privileges:true") == 7
    assert "seccomp=./playwright-seccomp-1.60.0.json" in COMPOSE
    assert "seccomp=unconfined" not in COMPOSE
    assert COMPOSE.count("network_mode: none") == 7
    assert "urllib.request.urlopen" in COMPOSE
    assert "/app/.venv/bin/python" in COMPOSE
    assert "entrypoint:\n      - /app/.venv/bin/python" in COMPOSE
    for volume in (
        "core_health_tmp:/tmp",
        "connect_health_tmp:/tmp",
        "core_smoke_tmp:/tmp",
        "canary_tmp:/tmp",
        "ingest_tmp:/tmp",
        "morning_digest_tmp:/tmp",
        "x_buzz_tmp:/tmp",
        "media_smoke_tmp:/tmp",
        "media_composition_tmp:/tmp",
    ):
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
    assert "--severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL" in BUILD
    assert "--severity CRITICAL,HIGH" not in BUILD
    assert 'git -C "$REPO_ROOT" archive --format=tar "$HEAD"' in BUILD
    assert "source-tracked-trivy-secret.json" in BUILD
    assert 'if test -e "$EVIDENCE_DIR"' in BUILD
    assert "image_id=$(docker image inspect --format '{{.Id}}' \"$image\")" in BUILD
    assert '"$image_id"' in BUILD
    assert "trivy version --format json" in BUILD
    assert "--exit-code 1" in BUILD
    assert "--skip-dirs" not in BUILD
    assert "--format cyclonedx" in BUILD
    assert "verify_trivy_zero.py" in BUILD
    assert "verify_runtime_evidence.py" in BUILD
    assert "generate_runtime_receipt.py" in BUILD
    assert "--first-parent" in BUILD
    assert 'test -s "$EVIDENCE_DIR/git-files.txt"' in BUILD
    assert "run_runtime_smokes.sh" in BUILD


def test_smoke_runner_checks_actual_read_only_memory_user_and_arm64() -> None:
    assert "{{.Architecture}}" in RUN_SMOKES
    assert "{{.HostConfig.ReadonlyRootfs}}" in RUN_SMOKES
    assert "{{.HostConfig.Memory}}" in RUN_SMOKES
    assert "{{.Config.User}}" in RUN_SMOKES
    assert "{{range .Mounts}}{{if .RW}}{{println .Destination}}" in RUN_SMOKES
    assert 'test "$writable_mounts" = "/tmp"' in RUN_SMOKES
    assert "CAP_SYS_CHROOT" not in RUN_SMOKES
    assert "seccomp=*unconfined" in RUN_SMOKES
    assert "{{json .Config.Volumes}}" in BUILD
    assert 'test "$volumes" = \'{"/tmp":{}}\'' in BUILD
    assert "true 4294967296 10001:10001" in RUN_SMOKES
    assert "down --volumes" in RUN_SMOKES
    assert "{{.Path}} {{json .Args}}" in RUN_SMOKES
    for consumer in (
        *CONSUMERS["core_image_consumers"].values(),
        *CONSUMERS["media_image_consumers"].values(),
    ):
        assert consumer["dynamic_service"] in RUN_SMOKES


def test_python_smoke_and_scan_helpers_parse() -> None:
    for name in (
        "smoke_core.py",
        "smoke_media.py",
        "verify_trivy_zero.py",
    ):
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
        "REMOVED_YTDLP_EXTRACTORS",
        "protocol_whitelist",
        "protocol_blacklist",
        "teamagent-ffmpeg-disabled",
        "127.0.0.1",
        "assert list(jobs.iterdir()) == []",
    ):
        assert token in text
    node = (DOCKER / "smoke_media_node.mjs").read_text(encoding="utf-8")
    assert "chromiumSandbox: true" in node
    assert 'page.route("**/*"' in node


def _resource_block(path: Path, resource_name: str) -> str:
    text = path.read_text(encoding="utf-8")
    marker = re.search(
        rf'resource\s+"aws_ecs_task_definition"\s+"{re.escape(resource_name)}"\s*\{{',
        text,
    )
    assert marker is not None, f"missing task definition {resource_name} in {path}"
    depth = 0
    for index in range(marker.end() - 1, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[marker.start() : index + 1]
    raise AssertionError(f"unterminated task definition {resource_name} in {path}")


def _all_declared_task_consumers(image_expression: str) -> set[str]:
    discovered: set[str] = set()
    for path in (ROOT / "infra/terraform").glob("*.tf"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r'resource\s+"aws_ecs_task_definition"\s+"([^"]+)"\s*\{',
            text,
        ):
            block = _resource_block(path, match.group(1))
            if re.search(image_expression, block):
                discovered.add(f"{path.relative_to(ROOT)}:{match.group(1)}")
    return discovered


def test_static_completeness_discovers_every_core_and_media_image_consumer() -> None:
    expected_core = {
        f"{value['terraform_resource'].split(':', 1)[0]}:"
        f"{value['terraform_resource'].rsplit('.', 1)[1]}"
        for value in CONSUMERS["core_image_consumers"].values()
    }
    expected_media = {
        f"{value['terraform_resource'].split(':', 1)[0]}:"
        f"{value['terraform_resource'].rsplit('.', 1)[1]}"
        for value in CONSUMERS["media_image_consumers"].values()
    }
    assert _all_declared_task_consumers(r"image\s*=\s*var\.mcp_image") == expected_core
    assert _all_declared_task_consumers(r"image\s*=\s*local\.media_worker_image") == expected_media


def test_every_declared_consumer_composes_hardened_task_and_exact_command() -> None:
    python = "/app/.venv/bin/python"
    for value in CONSUMERS["core_image_consumers"].values():
        raw_path, raw_resource = value["terraform_resource"].split(":", 1)
        resource_name = raw_resource.rsplit(".", 1)[1]
        block = _resource_block(ROOT / raw_path, resource_name)
        assert "merge(local.teamagent_runtime_container" in block
        assert 'cpu_architecture        = "ARM64"' in block
        assert 'name = "runtime-tmp"' in block
        command = value["command"]
        rendered = ", ".join(f'"{item}"' for item in command)
        rendered = rendered.replace(f'"{python}"', "local.teamagent_python")
        assert f"command   = [{rendered}]" in block or f"command = [{rendered}]" in block
        assert value["entry_point"] == []

    media = CONSUMERS["media_image_consumers"]["media-worker"]
    raw_path, raw_resource = media["terraform_resource"].split(":", 1)
    block = _resource_block(ROOT / raw_path, raw_resource.rsplit(".", 1)[1])
    assert "merge(local.teamagent_runtime_container" in block
    assert 'cpu_architecture        = "ARM64"' in block
    assert 'name = "runtime-tmp"' in block
    assert re.search(r"^\s*command\s*=", block, re.MULTILINE) is None
    assert media["entry_point"] == [python, "-m", "teamagent.media.worker"]


def test_shared_fargate_runtime_contract_is_complete_and_compatible() -> None:
    text = (ROOT / "infra/terraform/fargate.tf").read_text(encoding="utf-8")
    local_block = text[
        text.index("teamagent_runtime_container = {") : text.index(
            "teamagent_runtime_container = {"
        )
        + 900
    ]
    for token in (
        'user                   = "10001:10001"',
        "readonlyRootFilesystem = true",
        "privileged             = false",
        'drop = ["ALL"]',
        'sourceVolume  = "runtime-tmp"',
        'containerPath = "/tmp"',
        "readOnly      = false",
    ):
        assert token in local_block
    assert "tmpfs" not in local_block
    assert "no_new_privileges" not in local_block
    assert "dockerSecurityOptions" not in local_block


def test_exact_health_checks_and_deploy_renderers_have_no_shell_or_curl() -> None:
    core = CONSUMERS["core_image_consumers"]
    for name in ("mcp", "connect-web"):
        health = core[name]["health_check"]
        assert health is not None
        assert health[:2] == ["CMD", "/app/.venv/bin/python"]
        assert "urllib.request.urlopen" in health[-1]
        assert not {"CMD-SHELL", "sh", "curl"} & set(health)

    connect = (ROOT / "infra/deploy/deploy_connectweb_unified.sh").read_text(encoding="utf-8")
    ingest = (ROOT / "infra/deploy/register_ingest_td.sh").read_text(encoding="utf-8")
    assert ".containerDefinitions[0].entryPoint=[]" in connect
    assert (
        '.containerDefinitions[0].command=["/app/.venv/bin/python","-m","teamagent.connect_web"]'
    ) in connect
    assert '"command":["CMD","/app/.venv/bin/python","-c","import urllib.request;' in connect
    assert ".containerDefinitions[0].entryPoint = []" in ingest
    assert (
        '.containerDefinitions[0].command = ["/app/.venv/bin/python",'
        '"/app/scripts/run_ingest_fargate.py"]'
    ) in ingest
    for renderer in (connect, ingest):
        assert "CMD-SHELL" not in renderer
        assert '.containerDefinitions[0].command = ["sh"' not in renderer


def test_overrides_only_supply_environment_and_cannot_replace_runtime_command() -> None:
    for relative in (
        "infra/terraform/lambda/x_dispatch/handler.py",
        "infra/terraform/lambda/tiktok_dispatch/handler.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        override = text[text.index('"containerOverrides"') :]
        override = override[: override.index("}", override.index('"environment"')) + 1]
        assert '"environment"' in override
        assert '"command"' not in override
        assert '"entryPoint"' not in override
    ingest_override = (ROOT / "scripts/aws/run_ingest_task.sh").read_text(encoding="utf-8")
    assert "containerOverrides" in ingest_override
    assert "environment:" in ingest_override
    assert "command:" not in ingest_override
