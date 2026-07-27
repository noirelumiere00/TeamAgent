# ============================================================
# TeamAgent MCP release approval authority
# ============================================================
# This surface is installed by the AIIAdev successor bootstrap.  Runtime
# automation may read approvals for the deployment gate, but it cannot mutate
# the key, roles, project, locked buildspec, or approval objects.

locals {
  approval_publisher_project_name = (
    "${var.project_name}-${var.environment}-approval-publisher"
  )
  approval_publisher_project_arn = (
    "arn:aws:codebuild:${local.expected_build_region}:${local.expected_build_account_id}:project/${local.approval_publisher_project_name}"
  )
  approval_caller_role_name = (
    "${var.project_name}-${var.environment}-approval-caller"
  )
  approval_publisher_role_name = (
    "${var.project_name}-${var.environment}-codebuild-approval-publisher"
  )
  approval_caller_session_name = "teamagent-approval-caller"
  approval_caller_source_identity = (
    "teamagent-production-release-approval"
  )
  approval_evidence_prefix = "approval-records/mcp"
  approval_signing_key_alias = (
    "alias/${var.project_name}-${var.environment}-mcp-approval"
  )

  approval_caller_role_arn = (
    "arn:aws:iam::${local.expected_build_account_id}:role/${local.approval_caller_role_name}"
  )
  approval_publisher_role_arn = (
    "arn:aws:iam::${local.expected_build_account_id}:role/${local.approval_publisher_role_name}"
  )
  approval_reader_role_arns = [
    "arn:aws:iam::${local.expected_build_account_id}:role/${var.project_name}-${var.environment}-codebuild-image",
    "arn:aws:iam::${local.expected_build_account_id}:role/${var.project_name}-${var.environment}-codebuild-mcp-source-publisher",
    "arn:aws:iam::${local.expected_build_account_id}:role/${var.project_name}-${var.environment}-codebuild-image-attestor",
    "arn:aws:iam::${local.expected_build_account_id}:role/${var.project_name}-${var.environment}-codebuild-image-promoter",
    "arn:aws:iam::${local.expected_build_account_id}:role/${local.launcher_role_name}",
    "arn:aws:iam::${local.expected_build_account_id}:role/${local.release_launcher_role_name}",
    "arn:aws:iam::${local.expected_build_account_id}:role/${local.image_deployment_gate_role_name}",
    local.terraform_automation_role_arn,
  ]

  # Keep this as one ordered object and render it once.  The resource, contract
  # output, and fixed SHA below all consume these exact jsonencode bytes.
  # GetKeyPolicy byte preservation remains an AWS-measurement gate.
  approval_signing_key_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowApprovalKeyAdministration"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.expected_build_account_id}:root"
        }
        Action = [
          "kms:CancelKeyDeletion",
          "kms:CreateAlias",
          "kms:CreateGrant",
          "kms:DeleteAlias",
          "kms:DescribeKey",
          "kms:DisableKey",
          "kms:EnableKey",
          "kms:GetKeyPolicy",
          "kms:GetPublicKey",
          "kms:ListGrants",
          "kms:ListKeyPolicies",
          "kms:ListResourceTags",
          "kms:ListRetirableGrants",
          "kms:PutKeyPolicy",
          "kms:RevokeGrant",
          "kms:ScheduleKeyDeletion",
          "kms:TagResource",
          "kms:UntagResource",
          "kms:UpdateAlias",
          "kms:UpdateKeyDescription",
          "kms:UpdatePrimaryRegion",
        ]
        Resource = "*"
      },
      {
        Sid    = "AllowOnlyApprovalPublisherSigning"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.expected_build_account_id}:root"
        }
        Condition = {
          ArnEquals = {
            "aws:PrincipalArn" = local.approval_publisher_role_arn
          }
        }
        Action   = ["kms:Sign"]
        Resource = "*"
      },
      {
        Sid    = "AllowOnlyApprovalReadersVerification"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.expected_build_account_id}:root"
        }
        Condition = {
          ArnEquals = {
            "aws:PrincipalArn" = local.approval_reader_role_arns
          }
        }
        Action   = ["kms:Verify"]
        Resource = "*"
      },
    ]
  }
  approval_signing_key_policy_json = jsonencode(
    local.approval_signing_key_policy
  )
  approval_signing_key_policy_sha256 = sha256(
    local.approval_signing_key_policy_json
  )
  approval_signing_key_policy_expected_sha256 = "057f8a2ff1f06aa1e5ec03e5c4ecdfed4b3313146aca7d88c9ced95dca72709b"

  approval_publisher_environment_names = [
    "APPROVAL_DECISION",
    "EXPECTED_COMMIT",
    "FORCED_ROLLBACK_EVIDENCE_JSON",
  ]
  approval_caller_override_guard_a = local.launcher_denied_override_condition_keys_manage_a
  approval_caller_override_guard_b = local.launcher_denied_override_condition_keys_manage_b
  approval_caller_override_guard_c = local.launcher_denied_override_condition_keys_guardrails

  # The trusted helpers are embedded into the locked buildspec instead of being
  # imported from the commit being approved.  A3a fixes the security-critical
  # skeleton through canonical payload/signature publication; A3b or a later
  # stage still owns strict forced-gate transport verification, post-Put
  # re-verification, and the operator-facing locator handoff workflow.
  approval_publisher_buildspec = <<-YAML
    version: 0.2

    env:
      shell: bash

    phases:
      install:
        commands:
          - |
            set -euo pipefail
            install -d -m 0700 /tmp/teamagent-approval-tools
            printf '%s' '${filebase64("${path.module}/../codebuild/teamagent_schema_versions.py")}' | base64 -d > /tmp/teamagent-approval-tools/teamagent_schema_versions.py
            printf '%s' '${filebase64("${path.module}/../codebuild/teamagent_release_approval.py")}' | base64 -d > /tmp/teamagent-approval-tools/teamagent_release_approval.py
            printf '%s' '${filebase64("${path.module}/../codebuild/source_provenance.py")}' | base64 -d > /tmp/teamagent-approval-tools/source_provenance.py
            printf '%s' '${filebase64("${path.module}/../codebuild/teamagent_bundle_provenance.py")}' | base64 -d > /tmp/teamagent-approval-tools/teamagent_bundle_provenance.py
            chmod 0500 /tmp/teamagent-approval-tools/*.py
      build:
        commands:
          - |
            set -euo pipefail
            : "$${APPROVAL_BUILDSPEC_BUCKET:?}"
            : "$${APPROVAL_BUILDSPEC_KEY:?}"
            : "$${APPROVAL_BUILDSPEC_SHA256:?}"
            : "$${APPROVAL_DECISION:?}"
            : "$${APPROVAL_SIGNING_KEY_ARN:?}"
            : "$${APPROVED_BY_ROLE_ARN:?}"
            : "$${EVIDENCE_BUCKET:?}"
            : "$${EVIDENCE_KMS_KEY_ARN:?}"
            : "$${EXPECTED_COMMIT:?}"
            : "$${FORCED_ROLLBACK_EVIDENCE_JSON:?}"
            : "$${PUBLISHER_PROJECT_ARN:?}"

            case "$EXPECTED_COMMIT" in
              *[!0-9a-f]*|"") echo "FATAL: EXPECTED_COMMIT must be lowercase hex" >&2; exit 2 ;;
            esac
            test "$${#EXPECTED_COMMIT}" -eq 40
            case "$APPROVAL_DECISION" in
              "APPROVED: "*) ;;
              *) echo "FATAL: APPROVAL_DECISION must begin with APPROVED: " >&2; exit 2 ;;
            esac

            aws s3api get-object \
              --bucket "$APPROVAL_BUILDSPEC_BUCKET" \
              --key "$APPROVAL_BUILDSPEC_KEY" \
              --expected-bucket-owner "${local.expected_build_account_id}" \
              /tmp/approval-publisher-buildspec.yml >/tmp/approval-buildspec-get.json
            test "$(sha256sum /tmp/approval-publisher-buildspec.yml | awk '{print $1}')" = "$APPROVAL_BUILDSPEC_SHA256"
            case "$APPROVAL_BUILDSPEC_KEY" in
              *"/$APPROVAL_BUILDSPEC_SHA256.yml") ;;
              *) echo "FATAL: approval buildspec key is not content-addressed" >&2; exit 2 ;;
            esac

            test "$(
              aws s3api get-bucket-versioning \
                --bucket "$EVIDENCE_BUCKET" \
                --expected-bucket-owner "${local.expected_build_account_id}" \
                --query Status --output text
            )" = "Enabled"
            aws s3api get-object-lock-configuration \
              --bucket "$EVIDENCE_BUCKET" \
              --expected-bucket-owner "${local.expected_build_account_id}" \
              --output json > /tmp/approval-object-lock.json
            python3 - <<'PY'
            import json
            from pathlib import Path

            response = json.loads(
                Path("/tmp/approval-object-lock.json").read_text()
            )
            lock = response.get("ObjectLockConfiguration")
            if not isinstance(lock, dict):
                raise SystemExit(
                    "FATAL: evidence bucket Object Lock response is malformed"
                )
            default = lock.get("Rule", {}).get("DefaultRetention", {})
            if lock.get("ObjectLockEnabled") != "Enabled":
                raise SystemExit("FATAL: evidence bucket Object Lock is not enabled")
            if (
                default.get("Mode") not in {"COMPLIANCE", "GOVERNANCE"}
                or default.get("Days") != 3650
            ):
                raise SystemExit(
                    "FATAL: evidence bucket retention is not durable/3650"
                )
            PY

            git fetch --force --prune origin \
              +refs/heads/dev:refs/remotes/origin/dev
            test "$(git rev-parse refs/remotes/origin/dev)" = "$EXPECTED_COMMIT"
            git checkout --detach "$EXPECTED_COMMIT"
            test "$(git rev-parse HEAD)" = "$EXPECTED_COMMIT"
            test -z "$(git status --porcelain --untracked-files=no)"
            export SOURCE_TREE_OID="$(git rev-parse "$EXPECTED_COMMIT^{tree}")"

            python3 /tmp/teamagent-approval-tools/source_provenance.py \
              assert-contract-ready \
              --contract infra/codebuild/teamagent_runtime_contract.json
            python3 /tmp/teamagent-approval-tools/teamagent_bundle_provenance.py \
              assert-contract-ready \
              --contract infra/codebuild/teamagent_core_media_release_contract.json
            python3 /tmp/teamagent-approval-tools/teamagent_bundle_provenance.py \
              validate-contract-pair \
              --runtime-contract infra/codebuild/teamagent_runtime_contract.json \
              --contract infra/codebuild/teamagent_core_media_release_contract.json \
              --repo-root .

            export PYTHONPATH=/tmp/teamagent-approval-tools
            python3 - <<'PY'
            import datetime as dt
            import hashlib
            import json
            import os
            import uuid
            from pathlib import Path

            from teamagent_bundle_provenance import approval_observation_values
            from teamagent_release_approval import (
                canonical_json_bytes,
                validate_approval_payload,
            )

            inner_path = Path("infra/codebuild/teamagent_runtime_contract.json")
            outer_path = Path(
                "infra/codebuild/teamagent_core_media_release_contract.json"
            )
            inner_sha = hashlib.sha256(inner_path.read_bytes()).hexdigest()
            outer_sha = hashlib.sha256(outer_path.read_bytes()).hexdigest()
            values = approval_observation_values(inner_path, outer_path)
            now = dt.datetime.now(dt.UTC).replace(microsecond=0)
            expires_at = now + dt.timedelta(hours=1)
            retain_until = now + dt.timedelta(days=3650)
            forced_gate = json.loads(os.environ["FORCED_ROLLBACK_EVIDENCE_JSON"])
            approved_at_text = now.strftime("%Y-%m-%dT%H:%M:%SZ")

            authority = {
                "publisher_project_arn": os.environ["PUBLISHER_PROJECT_ARN"],
                "publisher_build_id": os.environ["CODEBUILD_BUILD_ID"],
                "kms_key_arn": os.environ["APPROVAL_SIGNING_KEY_ARN"],
            }
            payload = {
                "schema_version": 1,
                "kind": "teamagent.core-media-release-approval",
                "approval_id": str(uuid.uuid4()),
                "pipeline": "mcp",
                "environment": "dev",
                "approved_at_utc": approved_at_text,
                "expires_at_utc": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "approved_by": os.environ["APPROVED_BY_ROLE_ARN"],
                "source_commit": os.environ["EXPECTED_COMMIT"],
                "source_tree_oid": os.environ["SOURCE_TREE_OID"],
                "contracts": {
                    "inner": {"schema_version": 5, "raw_sha256": inner_sha},
                    "outer": {"schema_version": 3, "raw_sha256": outer_sha},
                },
                "observations": [
                    {
                        "key": key,
                        "value": values[key],
                        "observed_at_utc": approved_at_text,
                        "source": f"contract://{outer_sha}/{key}",
                    }
                    for key in sorted(values)
                ],
                "decision": os.environ["APPROVAL_DECISION"],
                "gates": {"forced_rollback_evidence": forced_gate},
                "approval_authority": authority,
            }
            expected = {
                "commit": os.environ["EXPECTED_COMMIT"],
                "tree_oid": os.environ["SOURCE_TREE_OID"],
                "inner_sha": inner_sha,
                "outer_sha": outer_sha,
                "observations": values,
                "pipeline": "mcp",
                "environment": "dev",
                "approved_by": os.environ["APPROVED_BY_ROLE_ARN"],
                "authority": authority,
                "forced_rollback_state": forced_gate.get("state"),
                "forced_rollback_evidence": forced_gate,
            }
            validate_approval_payload(payload, expected, now=now)
            Path("/tmp/approval-payload.json").write_bytes(
                canonical_json_bytes(payload)
            )
            Path("/tmp/approval-retain-until.txt").write_text(
                retain_until.strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            PY

            payload_sha256="$(
              sha256sum /tmp/approval-payload.json | awk '{print $1}'
            )"
            payload_key="${local.approval_evidence_prefix}/$EXPECTED_COMMIT/$payload_sha256.json"
            signature_key="$payload_key.sig"
            retain_until="$(cat /tmp/approval-retain-until.txt)"
            openssl dgst -sha256 -binary \
              /tmp/approval-payload.json > /tmp/approval-payload.digest
            aws kms sign \
              --region "${local.expected_build_region}" \
              --key-id "$APPROVAL_SIGNING_KEY_ARN" \
              --message-type DIGEST \
              --message fileb:///tmp/approval-payload.digest \
              --signing-algorithm RSASSA_PSS_SHA_256 \
              --query Signature --output text |
              base64 -d > /tmp/approval-payload.sig
            signature_sha256="$(
              sha256sum /tmp/approval-payload.sig | awk '{print $1}'
            )"

            payload_version_id="$(
              aws s3api put-object \
                --bucket "$EVIDENCE_BUCKET" \
                --key "$payload_key" \
                --body /tmp/approval-payload.json \
                --content-type application/json \
                --server-side-encryption aws:kms \
                --ssekms-key-id "$EVIDENCE_KMS_KEY_ARN" \
                --bucket-key-enabled \
                --object-lock-mode GOVERNANCE \
                --object-lock-retain-until-date "$retain_until" \
                --if-none-match '*' \
                --expected-bucket-owner "${local.expected_build_account_id}" \
                --query VersionId --output text
            )"
            signature_version_id="$(
              aws s3api put-object \
                --bucket "$EVIDENCE_BUCKET" \
                --key "$signature_key" \
                --body /tmp/approval-payload.sig \
                --content-type application/octet-stream \
                --server-side-encryption aws:kms \
                --ssekms-key-id "$EVIDENCE_KMS_KEY_ARN" \
                --bucket-key-enabled \
                --object-lock-mode GOVERNANCE \
                --object-lock-retain-until-date "$retain_until" \
                --if-none-match '*' \
                --expected-bucket-owner "${local.expected_build_account_id}" \
                --query VersionId --output text
            )"
            test -n "$payload_version_id"
            test "$payload_version_id" != "None"
            test -n "$signature_version_id"
            test "$signature_version_id" != "None"
            export payload_key payload_sha256 payload_version_id
            export signature_key signature_sha256 signature_version_id

            python3 - <<'PY'
            import json
            import os

            locator = {
                "mcp": {
                    "payload": {
                        "bucket": os.environ["EVIDENCE_BUCKET"],
                        "key": os.environ["payload_key"],
                        "version_id": os.environ["payload_version_id"],
                        "sha256": os.environ["payload_sha256"],
                    },
                    "signature": {
                        "bucket": os.environ["EVIDENCE_BUCKET"],
                        "key": os.environ["signature_key"],
                        "version_id": os.environ["signature_version_id"],
                        "sha256": os.environ["signature_sha256"],
                    },
                }
            }
            print(json.dumps(locator, sort_keys=True, separators=(",", ":")))
            PY
  YAML
  approval_publisher_buildspec_sha256 = sha256(
    local.approval_publisher_buildspec
  )
  approval_publisher_buildspec_s3_key = (
    "codebuild-buildspecs/${local.approval_publisher_project_name}/${local.approval_publisher_buildspec_sha256}.yml"
  )

  approval_runtime_iam_protected_arns = [
    local.approval_caller_role_arn,
    local.approval_publisher_role_arn,
    "arn:aws:iam::${local.expected_build_account_id}:policy/${local.approval_caller_role_name}-override-a",
    "arn:aws:iam::${local.expected_build_account_id}:policy/${local.approval_caller_role_name}-override-b",
    "arn:aws:iam::${local.expected_build_account_id}:policy/${local.approval_caller_role_name}-override-c",
    "arn:aws:iam::${local.expected_build_account_id}:policy/${var.project_name}-${var.environment}-approval-reader",
  ]
}

data "aws_iam_policy_document" "approval_publisher_assume" {
  statement {
    sid     = "ExactApprovalPublisherProject"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.expected_build_account_id]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [local.approval_publisher_project_arn]
    }
  }
}

resource "aws_iam_role" "approval_publisher" {
  name                 = local.approval_publisher_role_name
  assume_role_policy   = data.aws_iam_policy_document.approval_publisher_assume.json
  max_session_duration = 3600
}

resource "aws_kms_key" "approval_signing" {
  description              = "Sign TeamAgent MCP human release approvals"
  deletion_window_in_days  = 30
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "RSA_3072"
  policy                   = local.approval_signing_key_policy_json

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "approval_signing" {
  name          = local.approval_signing_key_alias
  target_key_id = aws_kms_key.approval_signing.key_id
}

data "aws_iam_policy_document" "approval_caller_assume" {
  statement {
    sid = "ExactAIIAdevMfaApprovalSession"
    actions = [
      "sts:AssumeRole",
      "sts:SetSourceIdentity",
    ]

    principals {
      type        = "AWS"
      identifiers = [data.aws_iam_user.aiia_dev.arn]
    }

    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }

    condition {
      test     = "StringEquals"
      variable = "sts:RoleSessionName"
      values   = [local.approval_caller_session_name]
    }

    condition {
      test     = "StringEquals"
      variable = "sts:SourceIdentity"
      values   = [local.approval_caller_source_identity]
    }
  }
}

resource "aws_iam_role" "approval_caller" {
  name                 = local.approval_caller_role_name
  assume_role_policy   = data.aws_iam_policy_document.approval_caller_assume.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "approval_caller" {
  statement {
    sid       = "StartOnlyFixedApprovalPublisher"
    actions   = ["codebuild:StartBuild"]
    resources = [local.approval_publisher_project_arn]

    condition {
      test     = "Null"
      variable = "codebuild:environment.environmentVariables.name"
      values   = ["false"]
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables.name"
      values   = local.approval_publisher_environment_names
    }
  }

  statement {
    sid       = "PollOnlyFixedApprovalPublisher"
    actions   = ["codebuild:BatchGetBuilds"]
    resources = [local.approval_publisher_project_arn]
  }

  statement {
    sid    = "DenyAlternateApprovalBuildEntryPoints"
    effect = "Deny"
    actions = [
      "codebuild:RetryBuild",
      "codebuild:RetryBuildBatch",
      "codebuild:StartBuildBatch",
      "codebuild:StartCommandExecution",
      "codebuild:StartSandbox",
      "codebuild:StartSandboxConnection",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "approval_caller" {
  name   = local.approval_caller_role_name
  role   = aws_iam_role.approval_caller.id
  policy = data.aws_iam_policy_document.approval_caller.json
}

data "aws_iam_policy_document" "approval_caller_override_a" {
  dynamic "statement" {
    for_each = local.approval_caller_override_guard_a
    content {
      effect    = "Deny"
      actions   = ["codebuild:StartBuild"]
      resources = [local.approval_publisher_project_arn]
      condition {
        test     = "Null"
        variable = statement.value
        values   = ["false"]
      }
    }
  }
}

data "aws_iam_policy_document" "approval_caller_override_b" {
  dynamic "statement" {
    for_each = local.approval_caller_override_guard_b
    content {
      effect    = "Deny"
      actions   = ["codebuild:StartBuild"]
      resources = [local.approval_publisher_project_arn]
      condition {
        test     = "Null"
        variable = statement.value
        values   = ["false"]
      }
    }
  }
}

data "aws_iam_policy_document" "approval_caller_override_c" {
  dynamic "statement" {
    for_each = local.approval_caller_override_guard_c
    content {
      effect    = "Deny"
      actions   = ["codebuild:StartBuild"]
      resources = [local.approval_publisher_project_arn]
      condition {
        test     = "Null"
        variable = statement.value
        values   = ["false"]
      }
    }
  }
}

resource "aws_iam_policy" "approval_caller_override_a" {
  name   = "${local.approval_caller_role_name}-override-a"
  policy = data.aws_iam_policy_document.approval_caller_override_a.json
}

resource "aws_iam_policy" "approval_caller_override_b" {
  name   = "${local.approval_caller_role_name}-override-b"
  policy = data.aws_iam_policy_document.approval_caller_override_b.json
}

resource "aws_iam_policy" "approval_caller_override_c" {
  name   = "${local.approval_caller_role_name}-override-c"
  policy = data.aws_iam_policy_document.approval_caller_override_c.json
}

resource "aws_iam_role_policy_attachment" "approval_caller_override_a" {
  role       = aws_iam_role.approval_caller.name
  policy_arn = aws_iam_policy.approval_caller_override_a.arn
}

resource "aws_iam_role_policy_attachment" "approval_caller_override_b" {
  role       = aws_iam_role.approval_caller.name
  policy_arn = aws_iam_policy.approval_caller_override_b.arn
}

resource "aws_iam_role_policy_attachment" "approval_caller_override_c" {
  role       = aws_iam_role.approval_caller.name
  policy_arn = aws_iam_policy.approval_caller_override_c.arn
}

resource "aws_cloudwatch_log_group" "codebuild_approval_publisher" {
  name              = "/aws/codebuild/${local.approval_publisher_project_name}"
  retention_in_days = local.codebuild_log_retention_days

  lifecycle {
    prevent_destroy = true
  }
}

data "aws_iam_policy_document" "approval_publisher" {
  statement {
    sid     = "WriteOwnApprovalPublisherLogs"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "${aws_cloudwatch_log_group.codebuild_approval_publisher.arn}:*",
    ]
  }

  statement {
    sid = "ReadProtectedTeamAgentSource"
    actions = [
      "codeconnections:GetConnection",
      "codeconnections:GetConnectionToken",
    ]
    resources = [aws_codestarconnections_connection.openclaw_codebuild.arn]
  }

  # GATE5 measured that CodeBuild's S3 buildspec retrieval needs ListBucket.
  # Keep this bucket-level permission separate from approval object reads.
  statement {
    sid       = "ListOnlyLockedApprovalBuildspec"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.image_release_evidence.arn]

    condition {
      test     = "StringEquals"
      variable = "s3:prefix"
      values   = [local.approval_publisher_buildspec_s3_key]
    }
  }

  statement {
    sid       = "ReadOnlyLockedApprovalBuildspec"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.image_release_evidence.arn}/${local.approval_publisher_buildspec_s3_key}"]
  }

  statement {
    sid = "CheckApprovalEvidenceBucketHardening"
    actions = [
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketVersioning",
    ]
    resources = [aws_s3_bucket.image_release_evidence.arn]
  }

  statement {
    sid = "PutOnlyImmutableMcpApprovals"
    actions = [
      "s3:PutObject",
      "s3:PutObjectRetention",
    ]
    resources = [
      "${aws_s3_bucket.image_release_evidence.arn}/${local.approval_evidence_prefix}/*",
    ]
  }

  statement {
    sid = "UseOnlyEvidenceEncryptionKey"
    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
    ]
    resources = [aws_kms_key.image_release_evidence.arn]
  }

  statement {
    sid       = "SignOnlyReleaseApprovals"
    actions   = ["kms:Sign"]
    resources = [aws_kms_key.approval_signing.arn]
  }

  statement {
    sid    = "DenyOtherEvidencePrefixWrites"
    effect = "Deny"
    actions = [
      "s3:DeleteObject*",
      "s3:PutObject*",
      "s3:RestoreObject",
    ]
    not_resources = [
      "${aws_s3_bucket.image_release_evidence.arn}/${local.approval_evidence_prefix}/*",
    ]
  }

  statement {
    sid     = "DenySourceAndAttestorKeySigning"
    effect  = "Deny"
    actions = ["kms:Sign"]
    resources = [
      aws_kms_key.image_attestor_signing.arn,
      aws_kms_key.mcp_source_publisher_signing.arn,
    ]
  }

  statement {
    sid    = "DenyEcrAndAlternateExecution"
    effect = "Deny"
    actions = [
      "ecr:*",
      "secretsmanager:GetSecretValue",
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:StartSession",
      "ssmmessages:*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "approval_publisher" {
  name   = local.approval_publisher_role_name
  role   = aws_iam_role.approval_publisher.id
  policy = data.aws_iam_policy_document.approval_publisher.json
}

data "aws_iam_policy_document" "approval_reader" {
  statement {
    sid = "ReadExactVersionedMcpApprovals"
    actions = [
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
    ]
    resources = [
      "${aws_s3_bucket.image_release_evidence.arn}/${local.approval_evidence_prefix}/*",
    ]
  }

  statement {
    sid       = "DecryptOnlyApprovalObjects"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.image_release_evidence.arn]
  }

  statement {
    sid       = "VerifyOnlyApprovalSignatures"
    actions   = ["kms:Verify"]
    resources = [aws_kms_key.approval_signing.arn]
  }
}

resource "aws_iam_policy" "approval_reader" {
  name        = "${var.project_name}-${var.environment}-approval-reader"
  description = "Read exact immutable MCP approvals and verify their dedicated signature"
  policy      = data.aws_iam_policy_document.approval_reader.json

  lifecycle {
    precondition {
      condition     = length(replace(data.aws_iam_policy_document.approval_reader.json, "/\\s/", "")) < 6144
      error_message = "Approval reader policy must remain below 6,144 non-whitespace characters."
    }
  }
}

resource "aws_iam_role_policy_attachment" "approval_reader_main_builder" {
  role       = aws_iam_role.codebuild.name
  policy_arn = aws_iam_policy.approval_reader.arn
}

resource "aws_iam_role_policy_attachment" "approval_reader_source_publisher" {
  role       = aws_iam_role.mcp_source_publisher.name
  policy_arn = aws_iam_policy.approval_reader.arn
}

resource "aws_iam_role_policy_attachment" "approval_reader_attestor" {
  role       = aws_iam_role.image_attestor.name
  policy_arn = aws_iam_policy.approval_reader.arn
}

resource "aws_iam_role_policy_attachment" "approval_reader_promoter" {
  role       = aws_iam_role.image_promoter.name
  policy_arn = aws_iam_policy.approval_reader.arn
}

resource "aws_iam_role_policy_attachment" "approval_reader_build_launcher" {
  role       = aws_iam_role.codebuild_launcher.name
  policy_arn = aws_iam_policy.approval_reader.arn
}

resource "aws_iam_role_policy_attachment" "approval_reader_release_launcher" {
  role       = aws_iam_role.release_launcher.name
  policy_arn = aws_iam_policy.approval_reader.arn
}

resource "aws_iam_role_policy_attachment" "approval_reader_deployment_gate" {
  role       = aws_iam_role.image_deployment_gate.name
  policy_arn = aws_iam_policy.approval_reader.arn
}

resource "aws_iam_role_policy_attachment" "approval_reader_runtime_automation" {
  role       = aws_iam_role.runtime_automation.name
  policy_arn = aws_iam_policy.approval_reader.arn
}

resource "aws_s3_object" "approval_publisher_buildspec" {
  bucket                        = aws_s3_bucket.image_release_evidence.id
  key                           = local.approval_publisher_buildspec_s3_key
  content                       = local.approval_publisher_buildspec
  content_type                  = "text/yaml"
  source_hash                   = local.approval_publisher_buildspec_sha256
  server_side_encryption        = "aws:kms"
  kms_key_id                    = aws_kms_key.image_release_evidence.arn
  bucket_key_enabled            = true
  object_lock_mode              = "GOVERNANCE"
  object_lock_retain_until_date = local.codebuild_buildspec_retain_until_date

  depends_on = [
    aws_s3_bucket_object_lock_configuration.image_release_evidence,
    aws_s3_bucket_policy.image_release_evidence,
  ]

  # A changed body must be published under a new Terraform address/key.  This
  # address is the immutable A3a bootstrap object.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_codebuild_project" "approval_publisher" {
  name           = local.approval_publisher_project_name
  description    = "Validate protected dev and issue immutable signed MCP approvals"
  service_role   = aws_iam_role.approval_publisher.arn
  source_version = "refs/heads/dev"

  artifacts { type = "NO_ARTIFACTS" }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
    type            = "ARM_CONTAINER"
    privileged_mode = false

    environment_variable {
      name  = "APPROVAL_BUILDSPEC_BUCKET"
      value = aws_s3_bucket.image_release_evidence.id
    }

    environment_variable {
      name  = "APPROVAL_BUILDSPEC_KEY"
      value = local.approval_publisher_buildspec_s3_key
    }

    environment_variable {
      name  = "APPROVAL_BUILDSPEC_SHA256"
      value = local.approval_publisher_buildspec_sha256
    }

    environment_variable {
      name  = "APPROVAL_SIGNING_KEY_ARN"
      value = aws_kms_key.approval_signing.arn
    }

    environment_variable {
      name  = "APPROVED_BY_ROLE_ARN"
      value = local.approval_caller_role_arn
    }

    environment_variable {
      name  = "EVIDENCE_BUCKET"
      value = aws_s3_bucket.image_release_evidence.id
    }

    environment_variable {
      name  = "EVIDENCE_KMS_KEY_ARN"
      value = aws_kms_key.image_release_evidence.arn
    }

    environment_variable {
      name  = "PUBLISHER_PROJECT_ARN"
      value = local.approval_publisher_project_arn
    }
  }

  source {
    type                = "GITHUB"
    location            = "https://github.com/noirelumiere00/TeamAgent.git"
    git_clone_depth     = 0
    report_build_status = false
    buildspec           = "${aws_s3_bucket.image_release_evidence.arn}/${local.approval_publisher_buildspec_s3_key}"

    auth {
      type     = "CODECONNECTIONS"
      resource = aws_codestarconnections_connection.openclaw_codebuild.arn
    }
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild_approval_publisher.name
    }
  }

  depends_on = [
    aws_iam_role_policy.approval_publisher,
    aws_s3_object.approval_publisher_buildspec,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

check "approval_foundation_preconditions" {
  assert {
    condition = (
      local.approval_publisher_project_name ==
      "teamagent-dev-approval-publisher" &&
      local.approval_caller_role_name ==
      "teamagent-dev-approval-caller" &&
      local.approval_signing_key_alias ==
      "alias/teamagent-dev-mcp-approval" &&
      local.approval_signing_key_policy_sha256 ==
      local.approval_signing_key_policy_expected_sha256 &&
      aws_kms_key.approval_signing.key_usage == "SIGN_VERIFY" &&
      aws_kms_key.approval_signing.customer_master_key_spec == "RSA_3072"
    )
    error_message = "The MCP approval authority names, key shape, or deterministic policy bytes changed."
  }
}

output "approval_signing_key_arn" {
  value = aws_kms_key.approval_signing.arn
}

output "approval_signing_key_policy_contract" {
  value = local.approval_signing_key_policy_json
}

output "approval_signing_key_policy_sha256" {
  value = local.approval_signing_key_policy_sha256
}

output "approval_caller_role_arn" {
  value = aws_iam_role.approval_caller.arn
}

output "approval_publisher_project_arn" {
  value = aws_codebuild_project.approval_publisher.arn
}

output "approval_publisher_buildspec_contract" {
  value = {
    bucket = aws_s3_bucket.image_release_evidence.id
    key    = aws_s3_object.approval_publisher_buildspec.key
    sha256 = local.approval_publisher_buildspec_sha256
  }
}
