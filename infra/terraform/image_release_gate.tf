# Plan-time gate for every production image variable. Task definitions remain
# owned by their existing stacks; this file rejects unsafe inputs before any
# task-definition diff can be planned or applied.

variable "image_release_evidence" {
  description = "Exact immutable S3 VersionIds for fresh KMS-signed active/rollback release receipts, keyed by mcp/openclaw/tiktok."
  type = map(object({
    bucket               = string
    key                  = string
    version_id           = string
    signature_key        = string
    signature_version_id = string
  }))
  default = {}

  validation {
    condition = alltrue([
      for pipeline in keys(var.image_release_evidence) :
      contains(["mcp", "openclaw", "tiktok"], pipeline)
    ])
    error_message = "image_release_evidence accepts only mcp, openclaw, and tiktok keys."
  }
}

variable "image_deployment_intent_id" {
  description = "Ephemeral UUIDv4 generated only by plan_image_release.sh; never persist in tfvars."
  type        = string
  default     = ""

  validation {
    condition = (
      var.image_deployment_intent_id == "" ||
      can(regex(
        "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        var.image_deployment_intent_id,
      ))
    )
    error_message = "image_deployment_intent_id must be blank or a lowercase UUIDv4."
  }
}

variable "image_release_shared_generation_ledger" {
  description = "Optional non-secret HMAC-worker ledger snapshot bound into the one-time image release intent. The HMAC stack owns the table and live preflight."
  type = object({
    table_arn     = string
    generation    = number
    high_water_t0 = string
    stage         = string
  })
  default  = null
  nullable = true

  validation {
    condition = var.image_release_shared_generation_ledger == null ? true : (
      can(regex(
        "^arn:aws:dynamodb:ap-northeast-1:718959508629:table/[A-Za-z0-9_.-]{3,255}$",
        var.image_release_shared_generation_ledger.table_arn,
      )) &&
      var.image_release_shared_generation_ledger.generation >= 0 &&
      var.image_release_shared_generation_ledger.generation <= 9223372036854775807 &&
      var.image_release_shared_generation_ledger.generation == floor(
        var.image_release_shared_generation_ledger.generation
      ) &&
      can(regex(
        "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$",
        var.image_release_shared_generation_ledger.high_water_t0,
      )) &&
      can(formatdate(
        "YYYY-MM-DD'T'hh:mm:ss'Z'",
        var.image_release_shared_generation_ledger.high_water_t0,
      )) &&
      can(regex(
        "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
        var.image_release_shared_generation_ledger.stage,
      ))
    )
    error_message = "image_release_shared_generation_ledger must contain only a fixed-account table ARN, nonnegative integer generation, RFC3339 high_water_t0, and canonical stage."
  }
}

locals {
  deployment_images = {
    mcp      = var.mcp_image
    openclaw = var.openclaw_image
    tiktok   = var.enable_tiktok_acquire ? var.tiktok_acquire_image : ""
  }
  deployment_pipeline_enabled = {
    mcp      = var.mcp_image != ""
    openclaw = var.openclaw_image != ""
    tiktok   = var.enable_tiktok_acquire
  }
  deployment_contract_sha256 = {
    mcp      = filesha256("${path.module}/../codebuild/teamagent_core_media_release_contract.json")
    openclaw = filesha256("${path.module}/../codebuild/openclaw_bundle_contract.json")
    tiktok   = filesha256("${path.module}/../codebuild/tiktok_release_contract.json")
  }
  deployment_contract_ready = {
    mcp = jsondecode(
      file("${path.module}/../codebuild/teamagent_core_media_release_contract.json")
    ).release.ready
    openclaw = jsondecode(
      file("${path.module}/../codebuild/openclaw_bundle_contract.json")
    ).release.ready
    tiktok = jsondecode(
      file("${path.module}/../codebuild/tiktok_release_contract.json")
    ).release.ready
  }
  deployment_image_patterns = {
    mcp      = "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-mcp@sha256:[0-9a-f]{64}$"
    openclaw = "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-openclaw@sha256:[0-9a-f]{64}$"
    tiktok   = "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-dev-tiktok-acquire@sha256:[0-9a-f]{64}$"
  }
  deployment_requested = anytrue(values(local.deployment_pipeline_enabled))
  deployment_intent_is_valid = (
    !local.deployment_requested ||
    can(regex(
      "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
      var.image_deployment_intent_id,
    ))
  )
  deployment_references_are_digest_only = alltrue([
    for pipeline, image in local.deployment_images :
    !local.deployment_pipeline_enabled[pipeline] ||
    can(regex(local.deployment_image_patterns[pipeline], image))
  ])
  deployment_contracts_are_ready = alltrue([
    for pipeline, enabled in local.deployment_pipeline_enabled :
    !enabled || local.deployment_contract_ready[pipeline]
  ])
  deployment_evidence_is_complete = alltrue([
    for pipeline, enabled in local.deployment_pipeline_enabled :
    !enabled || contains(keys(var.image_release_evidence), pipeline)
  ])
  deployment_application_provenance = {
    mcp = {
      bucket                    = aws_s3_bucket.raw_files.bucket
      key                       = "codebuild/connect-web-app.html"
      version_id                = var.connect_app_html_s3_version_id
      sha256                    = var.connect_app_html_sha256
      vault_manifest_sha256     = var.connect_app_html_manifest_sha256
      build_inputs_sha256       = var.connect_app_html_build_inputs_sha256
      baked_fallback_version_id = local.canonical_baked_app_html_version_id
      baked_fallback_sha256     = local.canonical_baked_app_html_sha256
    }
  }
  # jsondecode preserves the generation number while allowing an empty object
  # until the separately owned HMAC ledger stack is integrated.
  deployment_shared_generation_ledger = jsondecode(
    var.image_release_shared_generation_ledger == null
    ? "{}"
    : jsonencode(var.image_release_shared_generation_ledger)
  )
  deployment_gate_preconditions = (
    local.deployment_references_are_digest_only &&
    local.deployment_contracts_are_ready &&
    local.deployment_evidence_is_complete &&
    local.deployment_intent_is_valid
  )
  deployment_gate_query = {
    images_json         = jsonencode(local.deployment_images)
    evidence_json       = jsonencode(var.image_release_evidence)
    contracts_json      = jsonencode(local.deployment_contract_sha256)
    contract_ready_json = jsonencode(local.deployment_contract_ready)
    application_json    = jsonencode(local.deployment_application_provenance)
    shared_generation_ledger_json = jsonencode(
      local.deployment_shared_generation_ledger
    )
    signing_key_arn      = aws_kms_key.image_attestor_signing.arn
    encryption_key_arn   = aws_kms_key.image_release_evidence.arn
    deployment_intent_id = var.image_deployment_intent_id
  }
}

data "external" "signed_image_release_gate" {
  count = local.deployment_requested && local.deployment_gate_preconditions ? 1 : 0

  program = [
    "bash",
    "${path.module}/../deploy/run_image_deployment_gate.sh",
    "terraform-gate",
  ]

  query = local.deployment_gate_query

  depends_on = [
    aws_dynamodb_table.image_deployment_intents,
    aws_iam_role_policy.image_deployment_gate,
  ]
}

resource "terraform_data" "production_image_release_gate" {
  input = {
    deployment_intent_id      = var.image_deployment_intent_id
    deployment_context_sha256 = try(data.external.signed_image_release_gate[0].result.deployment_context_sha256, "")
    receipt_claims_sha256     = try(data.external.signed_image_release_gate[0].result.receipt_claims_sha256, "")
    requested_images          = local.deployment_images
    release_channels = try(
      jsondecode(data.external.signed_image_release_gate[0].result.release_channels_json),
      {},
    )
    application_provenance   = local.deployment_application_provenance
    shared_generation_ledger = local.deployment_shared_generation_ledger
  }

  # Every plan gets a new apply-time gate action. This prevents an old gate
  # instance in Terraform state from making a targeted task-definition apply a
  # no-op dependency.
  triggers_replace = local.deployment_requested ? [plantimestamp()] : []

  lifecycle {
    precondition {
      condition = (
        !local.deployment_requested ||
        (
          local.deployment_gate_preconditions &&
          try(data.external.signed_image_release_gate[0].result.verified == "true", false)
        )
      )
      error_message = "Production images require plan_image_release.sh, a unique deployment intent, release-repository digests, release.ready=true, an exact application VersionId contract, and fresh immutable KMS-signed active/rollback receipts."
    }
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -euo pipefail
      if ${local.deployment_requested}; then
        test -n "$TEAMAGENT_SAVED_PLAN_PATH"
        test -f "$TEAMAGENT_SAVED_PLAN_PATH"
        test -n "$TEAMAGENT_APPLY_ATTEMPT_ID"
        bash "${path.module}/../deploy/run_image_deployment_gate.sh" \
          consume-deployment-intent \
          --plan "$TEAMAGENT_SAVED_PLAN_PATH" \
          --apply-attempt-id "$TEAMAGENT_APPLY_ATTEMPT_ID"
      fi
    EOT

    environment = {
      TEAMAGENT_DEPLOYMENT_GATE_QUERY = jsonencode(local.deployment_gate_query)
    }
    interpreter = ["/bin/bash", "-c"]
  }
}
