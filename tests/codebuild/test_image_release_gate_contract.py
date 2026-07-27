from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "infra" / "terraform" / "image_release_gate.tf"
ECR = ROOT / "infra" / "terraform" / "ecr.tf"
CODEBUILD = ROOT / "infra" / "terraform" / "codebuild.tf"
EVIDENCE = ROOT / "infra" / "codebuild" / "release_evidence.py"
ATTESTOR = ROOT / "infra" / "codebuild" / "image-attestor-buildspec.yml"
PROMOTER = ROOT / "infra" / "codebuild" / "image-promoter-buildspec.yml"
GATE_RUNNER = ROOT / "infra" / "deploy" / "run_image_deployment_gate.sh"
PLAN_LAUNCHER = ROOT / "infra" / "terraform" / "plan_image_release.sh"
APPLY_LAUNCHER = ROOT / "infra" / "terraform" / "apply_image_release_plan.sh"
RUNTIME_GUARD = ROOT / "infra" / "deploy" / "terraform_runtime_guard.sh"
FARGATE_VARIABLES = ROOT / "infra" / "terraform" / "variables_fargate.tf"
RELEASE_CONTEXT = ROOT / "infra" / "terraform" / "image_release_context.py"
CONSUMER_REGISTRY = ROOT / "infra" / "codebuild" / "image_deployment_consumers.json"
BOOTSTRAP_TARGETS = ROOT / "infra" / "terraform" / "codebuild_provenance_bootstrap_targets.txt"


def _run_promoter_build_command(
    tmp_path: Path,
    *,
    subject_name: str,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    image_digest = f"sha256:{'a' * 64}"
    sbom_digest = f"sha256:{'b' * 64}"
    provenance_digest = f"sha256:{'c' * 64}"
    image_signature_digest = f"sha256:{'d' * 64}"
    sbom_signature_digest = f"sha256:{'e' * 64}"
    provenance_signature_digest = f"sha256:{'f' * 64}"
    sbom_payload_sha256 = "1" * 64
    provenance_payload_sha256 = "2" * 64
    subject = {
        "name": subject_name,
        "quarantine_repository": "teamagent-dev-tiktok-acquire-quarantine",
        "candidate_repository": "teamagent-dev-tiktok-acquire-verified-candidates",
        "release_repository": "teamagent-dev-tiktok-acquire",
        "release_tag": f"active-{'0' * 40}",
        "digest": image_digest,
        "sbom": {
            "digest": sbom_digest,
            "payload_sha256": sbom_payload_sha256,
            "signature": {"referrer_digest": sbom_signature_digest},
        },
        "provenance": {
            "digest": provenance_digest,
            "payload_sha256": provenance_payload_sha256,
            "signature": {"referrer_digest": provenance_signature_digest},
        },
        "image_signature": {"referrer_digest": image_signature_digest},
    }
    receipt_bytes = json.dumps({"subjects": [subject]}).encode()

    referrers = {
        "nextToken": None,
        "referrers": [
            {
                "digest": sbom_digest,
                "artifactType": "application/spdx+json",
                "artifactStatus": "ACTIVE",
                "annotations": {
                    "io.teamagent.build.payload-sha256": sbom_payload_sha256,
                },
            },
            {
                "digest": provenance_digest,
                "artifactType": "application/vnd.in-toto+json",
                "artifactStatus": "ACTIVE",
                "annotations": {
                    "io.teamagent.build.payload-sha256": provenance_payload_sha256,
                },
            },
            {
                "digest": image_signature_digest,
                "artifactType": "application/vnd.dsse.envelope.v1+json",
                "artifactStatus": "ACTIVE",
            },
            {
                "digest": sbom_signature_digest,
                "artifactType": "application/vnd.dsse.envelope.v1+json",
                "artifactStatus": "ACTIVE",
            },
            {
                "digest": provenance_signature_digest,
                "artifactType": "application/vnd.dsse.envelope.v1+json",
                "artifactStatus": "ACTIVE",
            },
        ],
    }

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_call_log = tmp_path / "aws-calls.log"
    aws = bin_dir / "aws"
    aws.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import os",
                "import sys",
                "with open(os.environ['AWS_CALL_LOG'], 'a', encoding='utf-8') as log:",
                "    log.write(' '.join(sys.argv[1:]) + '\\n')",
                "if sys.argv[1:3] == ['ecr', 'describe-images']:",
                "    print(os.environ['PROMOTER_IMAGE_DIGEST'])",
                "elif sys.argv[1:3] == ['ecr', 'list-image-referrers']:",
                "    print(os.environ['PROMOTER_REFERRERS_JSON'])",
                "else:",
                "    raise SystemExit(f'unexpected aws call: {sys.argv[1:]}')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    aws.chmod(0o755)
    oras_called = tmp_path / "oras-called"
    oras = bin_dir / "oras"
    oras.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import os",
                "from pathlib import Path",
                "Path(os.environ['ORAS_CALLED']).touch()",
                "raise SystemExit('oras cp must not run for an exact resumable promotion')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    oras.chmod(0o755)

    buildspec = yaml.safe_load(PROMOTER.read_text(encoding="utf-8"))
    build_command = buildspec["phases"]["build"]["commands"][0]
    assert isinstance(build_command, str)
    environment = {
        **os.environ,
        "AWS_CALL_LOG": str(aws_call_log),
        "CODEBUILD_BUILD_SUCCEEDING": "1",
        "ORAS_CALLED": str(oras_called),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PIPELINE": "tiktok",
        "PROMOTER_IMAGE_DIGEST": image_digest,
        "PROMOTER_REFERRERS_JSON": json.dumps(referrers),
        "PROMOTION_CHANNEL": "active",
    }
    contract_paths = [
        Path("/tmp/release-receipt.json"),
        Path("/tmp/destination-tiktok-tiktok.err"),
        Path("/tmp/promoted-subject-referrers.json"),
        Path("/tmp/promoted-artifact-signatures.json"),
    ]
    lock_path = Path("/tmp/teamagent-promoter-buildspec-test.lock")
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous_contents = {
            path: path.read_bytes() if path.exists() else None for path in contract_paths
        }
        contract_paths[0].write_bytes(receipt_bytes)
        try:
            completed = subprocess.run(
                ["bash", "-c", build_command],
                cwd=tmp_path,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        finally:
            for path, previous_content in previous_contents.items():
                if previous_content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(previous_content)
    return completed, aws_call_log, oras_called


def _hcl_block(body: str, opening_brace: int) -> str:
    depth = 0
    in_string = False
    escaped = False
    line_comment = False
    block_comment = False
    index = opening_brace
    while index < len(body):
        character = body[index]
        following = body[index + 1] if index + 1 < len(body) else ""
        if line_comment:
            if character == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if character == "*" and following == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
        elif character == "#":
            line_comment = True
        elif character == "/" and following == "/":
            line_comment = True
            index += 1
        elif character == "/" and following == "*":
            block_comment = True
            index += 1
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return body[opening_brace : index + 1]
        index += 1
    raise AssertionError("unterminated HCL block")


def test_terraform_uses_a_hard_precondition_not_a_warning_only_check() -> None:
    body = GATE.read_text(encoding="utf-8")

    assert 'resource "terraform_data" "production_image_release_gate"' in body
    assert "lifecycle {" in body
    assert "precondition {" in body
    assert 'check "production_images_require_signed_digest_release_evidence"' not in body
    assert 'variable "image_release_receipt_catalog"' in body
    assert 'variable "image_release_consumer_receipt_bindings"' in body
    assert 'variable "image_deployment_consumer_manifest"' in body
    assert "image_deployment_consumers.json" in body
    assert "local.deployment_consumer_registry_sha256" in body
    assert "length(local.deployment_consumer_registry.consumers) == 8" in body
    assert "local.deployment_manifest_registry_is_exact" in body
    assert "local.deployment_manifest_after_images_are_exact" in body
    assert "local.deployment_binding_consumers_are_exact" in body
    assert "local.deployment_catalog_bindings_are_exact" in body
    assert "local.deployment_contracts_are_ready" in body
    assert "signed_image_release_gate[0].result.verified" in body
    assert "signed_image_release_gate[0].result.deployment_mode" in body
    assert "var.image_deployment_consumer_manifest.mode" in body
    assert "consumer_manifest_json" in body
    assert "receipt_catalog_json" in body
    assert "consumer_receipt_bindings_json" in body
    assert "release_channels_json" in body
    assert "release_channels" in body
    assert 'variable "image_release_evidence"' not in body
    assert "evidence_json" not in body
    assert "tiktok_release_contract.json" not in body


def test_empty_receipt_bindings_cannot_vacuously_approve_an_unready_release() -> None:
    gate = GATE.read_text(encoding="utf-8")
    locals_start = gate.index("locals {")
    locals_block = _hcl_block(gate, gate.index("{", locals_start))
    application_start = gate.index("deployment_application_provenance = {")
    application_block = _hcl_block(
        gate,
        gate.index("{", application_start),
    )

    assert "deployment_release_consumer_ids = toset(" in locals_block
    assert 'var.image_deployment_consumer_manifest.mode == "no-image-transition"' in locals_block
    assert "for consumer_id in local.deployment_release_consumer_ids" in locals_block
    assert "deployment_bound_pipelines" not in locals_block
    assert "length(local.deployment_release_pipelines) > 0" not in locals_block
    assert "local.deployment_contract_ready[pipeline] == true" in locals_block
    assert "if contains(local.deployment_release_pipelines, pipeline)" in locals_block
    assert "local.deployment_release_pipelines" in application_block
    assert "deployment_binding_consumer_ids" not in application_block
    assert (
        "contracts_json                 = jsonencode(local.deployment_contract_bindings)"
    ) in gate
    assert (
        "contract_ready_json            = jsonencode(local.deployment_contract_ready_bindings)"
    ) in gate
    assert (
        "empty receipt bindings and no-image-transition mode do not waive this requirement"
    ) in gate


def test_malformed_consumer_manifest_cannot_fall_back_to_no_required_receipts() -> None:
    gate = GATE.read_text(encoding="utf-8")
    resource_start = gate.index('resource "terraform_data" "production_image_release_gate"')
    resource_block = _hcl_block(gate, gate.index("{", resource_start))
    lifecycle_start = resource_block.index("lifecycle {")
    lifecycle_block = _hcl_block(
        resource_block,
        resource_block.index("{", lifecycle_start),
    )
    manifest_precondition_start = lifecycle_block.index("precondition {")
    manifest_precondition = _hcl_block(
        lifecycle_block,
        lifecycle_block.index("{", manifest_precondition_start),
    )
    signed_gate_start = gate.index('data "external" "signed_image_release_gate"')
    signed_gate_block = _hcl_block(gate, gate.index("{", signed_gate_start))

    assert "deployment_manifest_structure_is_exact" in gate
    assert "deployment_manifest_consumers = try(" not in gate
    assert "deployment_receipt_required_consumer_ids = toset(try(" not in gate
    assert "local.deployment_manifest_presence_is_exact ? [" in gate
    assert (
        "deployment_manifest_presence_is_exact = (\n"
        "    local.deployment_manifest_structure_is_exact &&"
    ) in gate
    assert (
        "!local.deployment_requested ||\n"
        "        (\n"
        "          local.deployment_manifest_structure_is_exact &&\n"
        "          local.deployment_manifest_presence_is_exact\n"
        "        )"
    ) in manifest_precondition
    assert (
        "deployment_gate_preconditions = (\n"
        "    local.deployment_manifest_structure_is_exact &&\n"
        "    local.deployment_manifest_presence_is_exact &&"
    ) in gate
    assert (
        "count = local.deployment_requested && local.deployment_gate_preconditions ? 1 : 0"
    ) in signed_gate_block
    assert (
        "Production image deployment consumer manifest is malformed or does not exactly match"
    ) in manifest_precondition


def test_disabled_consumers_do_not_permanently_block_the_release_gate() -> None:
    gate = GATE.read_text(encoding="utf-8")

    expected_enable_bindings = (
        'connect_web    = var.enable_connect_web && var.mcp_image != ""',
        'canary         = var.enable_canary_health && var.mcp_image != ""',
        'ingest         = var.enable_ingest_schedule && var.mcp_image != ""',
        'morning_digest = var.enable_morning_digest && var.mcp_image != ""',
        'x_buzz_worker  = var.enable_x_research && var.x_buzz_image != ""',
        ('tiktok_acquire = local.media_worker_enabled && local.media_worker_image != ""'),
    )
    for binding in expected_enable_bindings:
        assert binding in gate
    assert 'sort(keys(consumer[phase])) == ["absent"]' in gate
    assert "deployment_manifest_presence_is_exact" in gate
    assert "consumer.live.absent == true" in gate
    assert "consumer.before.absent == true" in gate
    assert "consumer.after.absent == true" in gate
    assert "!can(consumer.before.image)" in gate
    assert ("!local.deployment_consumer_enabled[consumer.consumer_id] ? true : try(") in gate
    assert gate.count("if local.deployment_consumer_enabled[consumer.consumer_id] && (") >= 2
    assert "consumer.before.task_definition_arn !=" in gate
    assert "consumer.before.activation.desired_count !=" in gate
    assert "consumer.before.activation.state !=" in gate
    assert "consumer.before.activation.event_source_mapping_enabled !=" in gate


def test_terraform_uses_the_python_canonical_consumer_registry_digest() -> None:
    gate = GATE.read_text(encoding="utf-8")
    context = RELEASE_CONTEXT.read_text(encoding="utf-8")
    registry_data_start = gate.index('data "external" "deployment_consumer_registry_sha256"')
    registry_data_block = _hcl_block(
        gate,
        gate.index("{", registry_data_start),
    )

    assert '"${path.module}/image_release_context.py"' in registry_data_block
    assert '"registry-sha256"' in registry_data_block
    assert "data.external.deployment_consumer_registry_sha256.result.sha256" in gate
    assert "jsonencode(local.deployment_consumer_registry)" not in gate
    assert 'commands.add_parser("registry-sha256")' in context
    assert 'elif args.command == "registry-sha256":' in context
    assert "registry_sha256 = consumer_registry_sha256()" in context
    assert '_canonical_bytes({"sha256": registry_sha256})' in context


def test_consumer_receipt_claims_are_exact_and_shared_only_as_distinct_claims() -> None:
    gate = GATE.read_text(encoding="utf-8")
    evidence = EVIDENCE.read_text(encoding="utf-8")
    registry = json.loads(CONSUMER_REGISTRY.read_text(encoding="utf-8"))

    assert registry["schema_version"] == 1
    assert len(registry["consumers"]) == 8
    assert len({item["consumer_id"] for item in registry["consumers"]}) == 8
    assert {item["receipt"]["pipeline"] for item in registry["consumers"]} == {"mcp", "openclaw"}
    tiktok = next(item for item in registry["consumers"] if item["consumer_id"] == "tiktok_acquire")
    assert tiktok["receipt"] == {"pipeline": "mcp", "subject": "media"}
    assert tiktok["release_repository"] == "teamagent-media-worker"

    assert "deployment_bound_claim_ids = toset(" in gate
    assert "values(var.image_release_consumer_receipt_bindings)" in gate
    assert "deployment_receipt_required_consumer_ids" in gate
    assert "consumer.before.image != consumer.after.image" in gate
    assert "consumer.before.activation.desired_count !=" in gate
    assert "consumer.before.activation.state != consumer.after.activation.state" in gate
    assert "sorted(set(normalized))" in evidence
    assert "deployment receipt claims contain a duplicate" not in evidence
    assert "consumer receipt bindings do not exactly match receipt-requiring consumers" in evidence
    assert "receipt catalog must contain exactly the claims used by consumer bindings" in evidence
    assert "one subject and release repository cannot target different digests" in evidence
    assert "no-image-transition forbids receipts and consumer bindings" in evidence
    assert "no-image-transition contains release evidence" in evidence
    assert "receipt-required manifest contains no receipt-requiring change" in evidence
    assert "execution increase requires a fresh active receipt" in evidence
    assert "MAX_DEPLOYMENT_INTENT_LIFETIME_SECONDS = 3600" in evidence
    assert '"deployment_mode": mode' in evidence
    assert '"aws_ecs_task_definition.tiktok_acquire": "tiktok_acquire"' in evidence
    context = RELEASE_CONTEXT.read_text(encoding="utf-8")
    assert "consumer manifest mode does not match the derived comparison" in context


def test_saved_gate_query_is_consumed_before_terraform_apply_can_start() -> None:
    body = GATE.read_text(encoding="utf-8")
    evidence = EVIDENCE.read_text(encoding="utf-8")
    guard = RUNTIME_GUARD.read_text(encoding="utf-8")

    assert "triggers_replace" in body
    assert "[var.image_deployment_intent_id]" in body
    assert "plantimestamp()" not in body
    assert 'provisioner "local-exec"' not in body
    assert "deployment_gate_query    = local.deployment_gate_query" in body
    assert "receipt_authorization_expires_at" in body
    assert "deployment_context_sha256" in body
    assert "receipt_claims_sha256" in body
    assert "deployment_intent_id" in body
    assert "_verified_receipt_claims_for_saved_plan" in evidence
    assert "_consume_applying_deployment_intent" in evidence
    assert evidence.index("_verified_receipt_claims_for_saved_plan(") < evidence.index(
        "_consume_applying_deployment_intent(",
        evidence.index("def validate_deployment_preflight"),
    )
    assert guard.index("validate-deployment-preflight") < guard.index('python3 "$APPLY_SUPERVISOR"')


def test_every_discovered_ecs_task_definition_depends_on_release_gate() -> None:
    discovered: dict[str, str] = {}
    terraform_dir = ROOT / "infra" / "terraform"
    declaration = re.compile(r'resource\s+"aws_ecs_task_definition"\s+"([^"]+)"\s*(\{)')

    for path in sorted(terraform_dir.rglob("*.tf")):
        if ".terraform" in path.parts:
            continue
        body = path.read_text(encoding="utf-8")
        for match in declaration.finditer(body):
            relative = path.relative_to(terraform_dir)
            address = f"{relative}:aws_ecs_task_definition.{match.group(1)}"
            block = _hcl_block(body, match.start(2))
            assert "container_definitions" in block, address
            dependencies = re.search(r"depends_on\s*=\s*\[(.*?)\]", block, re.DOTALL)
            assert dependencies, f"{address} has no explicit release-gate dependency"
            assert "terraform_data.production_image_release_gate" in dependencies.group(1), (
                f"{address} can bypass the production image release gate"
            )
            assert "terraform_data.runtime_guard" in dependencies.group(1), (
                f"{address} can bypass the runtime guard"
            )
            discovered[address] = block

    assert set(discovered) == {
        "canary_schedule.tf:aws_ecs_task_definition.canary",
        "connect_web.tf:aws_ecs_task_definition.connect_web",
        "fargate.tf:aws_ecs_task_definition.mcp",
        "fargate.tf:aws_ecs_task_definition.openclaw",
        "ingest_schedule.tf:aws_ecs_task_definition.ingest",
        "morning_digest_schedule.tf:aws_ecs_task_definition.morning_digest",
        "tiktok_acquire.tf:aws_ecs_task_definition.tiktok_acquire",
        "x_research.tf:aws_ecs_task_definition.x_buzz_worker",
    }


def test_saved_plan_launchers_enforce_one_external_plan_and_no_target() -> None:
    planner = PLAN_LAUNCHER.read_text(encoding="utf-8")
    applier = APPLY_LAUNCHER.read_text(encoding="utf-8")
    runner = GATE_RUNNER.read_text(encoding="utf-8")
    guard = (ROOT / "infra" / "deploy" / "terraform_runtime_guard.sh").read_text(encoding="utf-8")

    for retired in (planner, applier):
        assert "Retired:" in retired
        assert "terraform_runtime_guard.sh" in retired
        assert "exit 64" in retired
        assert "terraform plan" not in retired
        assert "terraform apply" not in retired
    assert "TF_ARGS=(" in guard
    assert 'terraform -chdir="$TF_DIR" "${TF_ARGS[@]}"' in guard
    assert "-refresh=true" in guard
    assert "-lock-timeout=5m" in guard
    assert "-out=$STAGE_PLAN" in guard
    assert "prepare_image_deployment_intent" in guard
    assert "image_deployment_intent_id=$IMAGE_DEPLOYMENT_INTENT_ID" in guard
    assert 'python3 "$APPLY_SUPERVISOR" \\' in guard
    supervisor = (ROOT / "infra" / "terraform" / "terraform_apply_supervisor.py").read_text(
        encoding="utf-8"
    )
    assert '"apply",' in supervisor
    assert '"-lock=true",' in supervisor
    assert "mark-deployment-intent-outcome" in guard
    assert "terraform_apply_supervisor.py" in guard
    assert "heartbeat-deployment-lock" in guard
    assert (
        "assumed-role/teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker"
    ) in runner
    assert "arn:aws:iam::718959508629:user/AIIAdev" not in runner
    assert "arn:aws:iam::718959508629:role/teamagent-dev-image-deployment-gate" not in runner
    assert "sts assume-role" not in runner
    assert "exec python3" in runner
    assert "acquire-deployment-lock" in runner
    assert "validate-deployment-preflight" in runner

    for launcher in (PLAN_LAUNCHER, APPLY_LAUNCHER):
        completed = subprocess.run(
            ["bash", str(launcher), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert completed.returncode == 64
        assert "retired launcher" in completed.stderr


def test_empty_runtime_images_and_ungated_destructive_plans_fail_closed() -> None:
    variables = FARGATE_VARIABLES.read_text(encoding="utf-8")
    context = RELEASE_CONTEXT.read_text(encoding="utf-8")
    evidence = EVIDENCE.read_text(encoding="utf-8")

    for variable_name, repository in (
        ("mcp_image", "teamagent-mcp@sha256:"),
        ("openclaw_image", "teamagent-openclaw@sha256:"),
    ):
        start = variables.index(f'variable "{variable_name}"')
        block = _hcl_block(variables, variables.index("{", start))
        assert 'default     = ""' not in block
        assert repository in block
        assert "nonempty fixed release-repository digest" in block

    assert "CONTEXT_SCHEMA = 3" in context
    assert "CONSUMER_MANIFEST_SCHEMA = 1" in context
    assert "derive_consumer_manifest_mode" in context
    assert "no-image-transition" in context
    assert '"delete_change_count"' in context
    assert '"replace_change_count"' in context
    assert '"transition_sha256"' in context
    assert "nonempty release digest" in context
    assert "_saved_plan_transition_classification" in evidence
    assert "_require_destructive_rollback_channels" in evidence
    assert "image-empty destructive state is forbidden" in evidence
    assert "requires a fresh rollback receipt" in evidence
    assert "unscoped destructive transition" in evidence


def test_deployment_intents_use_a_durable_protected_conditional_ledger() -> None:
    terraform = CODEBUILD.read_text(encoding="utf-8")
    evidence = EVIDENCE.read_text(encoding="utf-8")

    assert 'resource "aws_dynamodb_table" "image_deployment_intents"' in terraform
    assert 'billing_mode = "PAY_PER_REQUEST"' in terraform
    assert "point_in_time_recovery" in terraform
    assert "deletion_protection_enabled = true" in terraform
    assert "prevent_destroy = true" in terraform
    assert 'attribute_name = "audit_expires_at"' in terraform
    assert '"dynamodb:TransactWriteItems"' in terraform
    assert '"dynamodb:UpdateItem"' in terraform
    assert '"dynamodb:PutItem"' in terraform
    assert '"dynamodb:GetItem"' in terraform
    assert '"dynamodb:DeleteItem"' in terraform

    assert '"attribute_not_exists(record_id)"' in evidence
    assert '"dynamodb",\n        "transact-write-items"' in evidence
    assert "authorization_expires_at > :now_epoch" in evidence
    assert "control_commit = :control_commit" in evidence
    assert "deployment intent control commit differs from the apply checkout" in evidence
    assert '"#state = :prepared "' in evidence
    assert '":applying": "APPLYING"' in evidence
    assert '"#state = :applying "' in evidence
    assert "--client-request-token" in evidence
    assert "release receipt has already authorized a deployment" in evidence
    assert "saved Terraform plan will not run the apply-time gate" in evidence
    assert "saved Terraform plan is incomplete" in evidence
    assert "ALLOWED_EXISTING_LOG_IMPORTS" in evidence
    assert "/aws/ecs/containerinsights/teamagent-dev/performance" in evidence
    assert "/aws/ecs/containerinsights/teamagent-dev-tiktok/performance" in evidence
    assert 'not in (["no-op"], ["update"])' in evidence
    assert "image release saved plan import is outside the exact " in evidence
    assert "existing-log allowlist" in evidence
    assert "DEPLOYMENT_LOCK_RECORD_ID" in evidence
    assert "lease_expires_at > :now_epoch" in evidence


def test_release_intent_binds_the_hmac_workers_nonsecret_ledger_snapshot() -> None:
    gate = GATE.read_text(encoding="utf-8")
    evidence = EVIDENCE.read_text(encoding="utf-8")
    terraform = CODEBUILD.read_text(encoding="utf-8")

    assert 'variable "image_release_shared_generation_ledger"' in gate
    for field in ("table_arn", "generation", "high_water_t0", "stage"):
        assert field in gate
    assert "ap-northeast-1:718959508629:table/" in gate
    assert "shared_generation_ledger_json" in gate
    assert "shared_generation_ledger =" in gate
    assert "shared_ledger_sha256" in evidence
    assert evidence.count('"shared_ledger_sha256"') >= 5
    assert "_validate_shared_generation_ledger_binding" in evidence
    assert '"secret"' not in gate

    # The HMAC worker owns its durable table. This branch only binds metadata
    # and must not duplicate a generation/high-water/stage ledger resource.
    assert 'resource "aws_dynamodb_table" "image_deployment_intents"' in terraform
    assert 'resource "aws_dynamodb_table"' not in gate


def test_every_deployable_pipeline_accepts_only_a_fixed_release_repo_digest() -> None:
    body = GATE.read_text(encoding="utf-8")
    evidence = EVIDENCE.read_text(encoding="utf-8")
    registry = json.loads(CONSUMER_REGISTRY.read_text(encoding="utf-8"))

    assert {consumer["release_repository"] for consumer in registry["consumers"]} == {
        "teamagent-mcp",
        "teamagent-openclaw",
        "teamagent-media-worker",
    }
    assert {consumer["receipt"]["pipeline"] for consumer in registry["consumers"]} == {
        "mcp",
        "openclaw",
    }
    assert "consumer.release_repository" in body
    assert "deployment_manifest_after_images_are_exact" in body
    assert "verified-candidates@sha256" not in body
    assert "quarantine@sha256" not in body
    assert "image_release_receipt_catalog" in body
    assert "image_release_consumer_receipt_bindings" in body
    assert "release.ready" in body
    assert "filesha256" in body
    assert "tiktok_release_contract.json" not in body
    assert "teamagent-dev-tiktok-acquire@sha256:" not in body
    assert '"terraform-gate"' in body
    assert "signing_key_arn" in body
    assert "encryption_key_arn" in body
    assert "_validate_promoted_release(validated_receipt)" in evidence
    assert '"batch-get-image"' in evidence
    assert evidence.count('"list-image-referrers"') == evidence.count('"50"')


def test_promoter_is_receipt_only_and_never_executes_repository_source() -> None:
    body = PROMOTER.read_text(encoding="utf-8")

    verify = body.index("aws kms verify")
    schema = body.index("verify-release-receipt")
    guard = body.index("promoter gate failed before release write")
    copy = body.index("oras cp --recursive")
    assert verify < schema < guard < copy
    assert "git " not in body
    assert "source.zip" not in body
    assert "aws kms sign" not in body
    assert "aws s3api put-object" not in body
    assert body.count("aws ecr list-image-referrers") == body.count("--max-results 50")
    assert "DESTINATION_DIGEST" in body
    assert '[ "$DESTINATION_DIGEST" = "$DIGEST" ]' in body
    assert body.count("signature.referrer_digest") == 3
    assert ".digest == $image_signature" in body
    assert ".digest == $signature" in body
    destination_lookup = body.index('DESTINATION_LOOKUP_ERROR="/tmp/destination-')
    exact_existing = body.index(
        "immutable promotion tag already binds a different digest",
        destination_lookup,
    )
    copy = body.index("oras cp --recursive", destination_lookup)
    graph_check = body.index("aws ecr list-image-referrers", copy)
    assert destination_lookup < exact_existing < copy < graph_check
    assert "ImageNotFoundException" in body[destination_lookup:copy]
    assert "Resuming verified existing promotion" in body[destination_lookup:copy]
    assert "destination tag lookup failed closed" in body[destination_lookup:copy]


def test_promoter_yaml_shell_resumes_only_after_the_exact_existing_digest(
    tmp_path: Path,
) -> None:
    completed, aws_call_log, oras_called = _run_promoter_build_command(
        tmp_path,
        subject_name="tiktok",
    )

    assert completed.returncode == 0, completed.stderr
    assert (
        "Resuming verified existing promotion for teamagent-dev-tiktok-acquire:active-" + "0" * 40
    ) in completed.stdout
    calls = aws_call_log.read_text(encoding="utf-8").splitlines()
    assert sum("ecr describe-images" in call for call in calls) == 1
    assert sum("ecr list-image-referrers" in call for call in calls) == 3
    assert not oras_called.exists()


def test_promoter_yaml_shell_rejects_subject_name_before_any_aws_use(
    tmp_path: Path,
) -> None:
    completed, aws_call_log, oras_called = _run_promoter_build_command(
        tmp_path,
        subject_name="../tiktok",
    )

    assert completed.returncode != 0
    assert "FATAL: receipt subject name is not allowlisted" in completed.stdout
    assert not aws_call_log.exists()
    assert not oras_called.exists()


def test_active_or_rollback_attestation_rechecks_candidate_signatures() -> None:
    body = ATTESTOR.read_text(encoding="utf-8")

    assert 'KMS_URI="awskms:///$SIGNING_KEY_ARN"' in body
    assert "verify-release-locator" in body
    assert "candidate is missing, changed, or an image index" in body
    assert body.count("cosign verify --experimental-oci11") == 3
    assert "verify_cosign_claim" in body
    assert body.count("signature.referrer_digest") == 3
    assert ".digest == $image_signature" in body
    assert ".digest == $signature" in body
    assert "authorize-release-receipt" in body
    assert "date -u -d '+3650 days'" in body
    assert "date -u -d '+30 minutes'" in body
    assert body.count("aws ecr list-image-referrers") == body.count("--max-results 50")


def test_lifecycle_never_expires_a_production_release_evidence_graph() -> None:
    body = ECR.read_text(encoding="utf-8")
    evidence = EVIDENCE.read_text(encoding="utf-8")

    assert 'description  = "expire all quarantined candidates after 2 days"' in body
    assert 'tagStatus   = "any"' in body
    assert "Production release repositories intentionally have no lifecycle policy" in body
    assert "can match only an explicitly" in body
    assert "ecr_release_lifecycle_policy" not in body
    for name in (
        "mcp",
        "mcp_media",
        "openclaw",
        "openclaw_media",
        "tiktok_acquire",
    ):
        assert f'aws_ecr_lifecycle_policy" "{name}"' not in body
    candidate_policy = body.split(
        "ecr_verified_candidate_lifecycle_policy = jsonencode(",
        maxsplit=1,
    )[1].split("ecr_quarantine_lifecycle_policy = jsonencode(", maxsplit=1)[0]
    assert '"tagged"' in candidate_policy
    assert 'tagPrefixList = ["rejected-"]' in candidate_policy
    assert 'tagPrefixList = ["verified-"]' not in candidate_policy
    assert '"any"' not in candidate_policy
    for name in (
        "mcp_verified_candidates",
        "mcp_media_verified_candidates",
        "openclaw_verified_candidates",
        "openclaw_media_verified_candidates",
        "tiktok_acquire_verified_candidates",
    ):
        lifecycle = body.split(
            f'resource "aws_ecr_lifecycle_policy" "{name}"',
            maxsplit=1,
        )[1].split('\nresource "', maxsplit=1)[0]
        assert "local.ecr_verified_candidate_lifecycle_policy" in lifecycle
    assert "DenyQuarantineRuntimePull" in body
    assert "_assert_no_release_lifecycle_policy" in evidence
    assert "lifecycle-policy absence could not be verified" in evidence
    assert "must not have an ECR lifecycle policy" in evidence


def test_ready_false_bootstrap_has_no_target_bypass() -> None:
    assert not BOOTSTRAP_TARGETS.exists()
    readme = (ROOT / "infra" / "terraform" / "README.md").read_text(encoding="utf-8")
    assert "codebuild_provenance_bootstrap_targets.txt" not in readme
    assert 'target_args+=("-target=$address")' not in readme


def test_task_definition_body_is_part_of_the_change_comparison() -> None:
    """A digest can stay put while the container body changes underneath it.

    The adversarial review reproduced exactly that: identical images plus a new
    command, environment, secrets and task role, classified as
    no-image-transition and waved through with zero AWS calls. Both consumer
    derivations therefore have to diff the task definition body, not only the
    image string, and both have to say so here so that a later simplification
    cannot quietly restore the hole.
    """
    gate = GATE.read_text(encoding="utf-8")

    body_comparison = (
        "jsonencode(consumer.before.task_definition) !=\n"
        "          jsonencode(consumer.after.task_definition)"
    )
    assert gate.count(body_comparison) == 2, (
        "both deployment_receipt_required_consumer_ids and "
        "deployment_affected_consumer_ids must diff the task definition body"
    )

    for local_name in (
        "deployment_receipt_required_consumer_ids",
        "deployment_affected_consumer_ids",
    ):
        derivation = _hcl_block(gate, gate.index("(", gate.index(f"{local_name} = toset")))
        assert body_comparison in derivation, f"{local_name} ignores the task definition body"

    # The manifest schema has to carry the body for the comparison to mean
    # anything, and it is the activator-shaped keys that make it exact.
    for key in (
        "container_definitions",
        "task_role_arn",
        "execution_role_arn",
        "network_mode",
        "volumes",
    ):
        assert f'"{key}",' in gate, f"consumer snapshot schema omits {key}"


def test_release_ready_is_required_by_the_precondition_not_hardcoded_true() -> None:
    """`alltrue([])` is true, so an empty binding set must not waive readiness.

    The precondition has to reference the computed local. Replacing it with a
    literal `true` was the mutation that survived the first pass.
    """
    gate = GATE.read_text(encoding="utf-8")

    assert "deployment_contracts_are_ready = " in gate
    ready_preconditions = [
        block
        for block in gate.split("precondition {")[1:]
        if "local.deployment_contracts_are_ready" in block
    ]
    assert len(ready_preconditions) == 1, (
        "exactly one precondition must require local.deployment_contracts_are_ready"
    )
    guard = ready_preconditions[0]
    assert "!local.deployment_requested ||" in guard
    assert "no-image-transition mode do not waive this requirement" in guard

    # The malformed-manifest and verifier preconditions are what make the
    # "nobody needs a receipt" fallbacks unreachable during a real apply.
    assert "local.deployment_manifest_structure_is_exact &&" in gate
    assert "local.deployment_manifest_presence_is_exact" in gate
    assert (
        "count = local.deployment_requested && local.deployment_gate_preconditions ? 1 : 0" in gate
    )


def test_no_fail_open_try_defaults_in_the_consumer_derivations() -> None:
    """`try(x, [])` and `try(x, {})` collapse to "nothing is required".

    Every `try` in this file must default to the blocking side: `false` for a
    predicate, `true` for "assume this consumer changed", or an empty string for
    a value that a separate precondition then rejects.
    """
    # Scope to the derivation locals. The `terraform_data` input block below them
    # defaults to "" / {} on purpose: those fields only exist when the verifier
    # data source exists, and a separate precondition already demands
    # verified == "true" before an apply can proceed.
    full = GATE.read_text(encoding="utf-8")
    gate = full[: full.index('resource "terraform_data" "production_image_release_gate"')]
    offenders = []
    for index, _ in enumerate(gate.split("try(")[1:]):
        opening = _nth_try_paren(gate, index)
        inner = _paren_block(gate, opening)
        segments, depth, current = [], 0, ""
        for character in inner:
            if character in "([{":
                depth += 1
            elif character in ")]}":
                depth -= 1
            if character == "," and depth == 0:
                segments.append(current)
                current = ""
            else:
                current += character
        segments.append(current)
        stripped = [segment.strip() for segment in segments if segment.strip()]
        default = stripped[-1] if stripped else ""
        if default in {"[]", "{}"}:
            offenders.append(default)
    assert not offenders, f"fail-open try() defaults in the derivations: {offenders}"

    # The one {} default that does exist is the verifier-sourced release channels,
    # and it must stay behind the verified=="true" precondition.
    assert full.count("      {},\n    )") == 1
    assert 'data.external.signed_image_release_gate[0].result.verified == "true"' in full


def _nth_try_paren(body: str, index: int) -> int:
    offset = -1
    for _ in range(index + 1):
        offset = body.index("try(", offset + 1)
    return body.index("(", offset)


def _paren_block(body: str, opening: int) -> str:
    depth = 0
    for offset in range(opening, len(body)):
        if body[offset] == "(":
            depth += 1
        elif body[offset] == ")":
            depth -= 1
            if depth == 0:
                return body[opening + 1 : offset]
    raise AssertionError("unterminated parenthesis")
