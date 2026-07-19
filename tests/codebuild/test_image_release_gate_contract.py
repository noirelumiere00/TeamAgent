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
    assert "local.deployment_references_are_digest_only" in body
    assert "local.deployment_contracts_are_ready" in body
    assert "local.deployment_evidence_is_complete" in body
    assert 'tiktok = ""' in body
    assert "tiktok   = false" in body
    assert "!local.deployment_pipeline_enabled[pipeline]" in body
    assert "signed_image_release_gate[0].result.verified" in body


def test_gate_is_replaced_and_consumed_before_any_task_definition_apply() -> None:
    body = GATE.read_text(encoding="utf-8")

    assert "triggers_replace" in body
    assert "plantimestamp()" in body
    assert 'provisioner "local-exec"' in body
    assert "consume-deployment-intent" in body
    assert "TEAMAGENT_SAVED_PLAN_PATH" in body
    assert "TEAMAGENT_APPLY_ATTEMPT_ID" in body
    assert "TEAMAGENT_DEPLOYMENT_GATE_QUERY" in body
    assert "deployment_context_sha256" in body
    assert "receipt_claims_sha256" in body
    assert "deployment_intent_id" in body


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
            assert "terraform_data.production_image_release_gate" in block, (
                f"{address} can bypass the production image release gate"
            )
            assert "terraform_data.runtime_guard" in block, (
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
        "tiktok_acquire.tf:aws_ecs_task_definition.media_worker",
        "x_research.tf:aws_ecs_task_definition.x_buzz_worker",
    }


def test_saved_plan_launchers_enforce_one_external_plan_and_no_target() -> None:
    planner = PLAN_LAUNCHER.read_text(encoding="utf-8")
    applier = APPLY_LAUNCHER.read_text(encoding="utf-8")
    runner = GATE_RUNNER.read_text(encoding="utf-8")
    guard = (
        ROOT / "infra" / "deploy" / "terraform_runtime_guard.sh"
    ).read_text(encoding="utf-8")

    for retired in (planner, applier):
        assert "Retired:" in retired
        assert "terraform_runtime_guard.sh" in retired
        assert "exit 64" in retired
        assert "terraform plan" not in retired
        assert "terraform apply" not in retired
    assert 'TF_ARGS=(' in guard
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
        "assumed-role/teamagent-dev-terraform-runtime-automation/"
        "teamagent-terraform-worker"
    ) in runner
    assert "arn:aws:iam::718959508629:user/AIIAdev" not in runner
    assert ("arn:aws:iam::718959508629:role/teamagent-dev-image-deployment-gate") in runner
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
    assert "image release saved plans cannot contain imports" in evidence
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
    assert "shared_generation_ledger  =" in gate
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

    for repository in (
        "teamagent-mcp@sha256:",
        "teamagent-openclaw@sha256:",
        "teamagent-dev-tiktok-acquire@sha256:",
    ):
        assert repository.replace(".", r"\.") in body or repository in body
    assert "verified-candidates@sha256" not in body
    assert "quarantine@sha256" not in body
    assert "image_release_evidence" in body
    assert "release.ready" in body
    assert "filesha256" in body
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
