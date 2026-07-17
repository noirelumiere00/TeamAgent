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
    mcp      = filesha256("${path.module}/../codebuild/teamagent_runtime_contract.json")
    openclaw = filesha256("${path.module}/../codebuild/openclaw_bundle_contract.json")
    tiktok   = filesha256("${path.module}/../codebuild/tiktok_release_contract.json")
  }
  deployment_contract_ready = {
    mcp = jsondecode(
      file("${path.module}/../codebuild/teamagent_runtime_contract.json")
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
  deployment_gate_preconditions = (
    local.deployment_references_are_digest_only &&
    local.deployment_contracts_are_ready &&
    local.deployment_evidence_is_complete
  )
}

data "external" "signed_image_release_gate" {
  count = local.deployment_requested && local.deployment_gate_preconditions ? 1 : 0

  program = [
    "python3",
    "${path.module}/../codebuild/release_evidence.py",
    "terraform-gate",
  ]

  query = {
    images_json         = jsonencode(local.deployment_images)
    evidence_json       = jsonencode(var.image_release_evidence)
    contracts_json      = jsonencode(local.deployment_contract_sha256)
    contract_ready_json = jsonencode(local.deployment_contract_ready)
    signing_key_arn     = aws_kms_key.image_attestor_signing.arn
    encryption_key_arn  = aws_kms_key.image_release_evidence.arn
  }
}

resource "terraform_data" "production_image_release_gate" {
  input = {
    requested_images = local.deployment_images
  }

  lifecycle {
    precondition {
      condition = (
        !local.deployment_requested ||
        (
          local.deployment_gate_preconditions &&
          try(data.external.signed_image_release_gate[0].result.verified == "true", false)
        )
      )
      error_message = "Production images require a release-repository digest, release.ready=true, and a fresh immutable KMS-signed active/rollback receipt with the exact contract hash."
    }
  }
}
