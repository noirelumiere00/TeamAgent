# Worker runtime mutation is an optional member of the same one-use saved plan as the production
# image and HMAC ECS gates. Paths are local apply-runner inputs; only their SHA-256 values enter
# Terraform state and the release intent.

variable "enable_hmac_worker_deploy" {
  description = "Run the signed, atomic EC2 worker deployment from the trusted saved-plan apply."
  type        = bool
  default     = false
}

variable "hmac_worker_deploy_mode" {
  description = "Worker mutation mode: candidate, cleanup, or rollback."
  type        = string
  default     = "candidate"

  validation {
    condition     = contains(["candidate", "cleanup", "rollback"], var.hmac_worker_deploy_mode)
    error_message = "hmac_worker_deploy_mode must be candidate, cleanup, or rollback."
  }
}

variable "hmac_worker_advance_stage" {
  description = "Advance worker-verified after preload. Must be false for cleanup and rollback."
  type        = bool
  default     = true

  validation {
    condition = (
      var.hmac_worker_deploy_mode == "candidate"
      || !var.hmac_worker_advance_stage
    )
    error_message = "hmac_worker_advance_stage must be false for cleanup and rollback."
  }
}

variable "hmac_worker_artifact_path" {
  description = "Local exact candidate/cleanup worker tar.gz."
  type        = string
  default     = ""
}

variable "hmac_worker_env_path" {
  description = "Local exact secret-free candidate/cleanup worker HMAC environment."
  type        = string
  default     = ""
}

variable "hmac_worker_rollback_artifact_path" {
  description = "Local exact rollback worker tar.gz."
  type        = string
  default     = ""
}

variable "hmac_worker_rollback_env_path" {
  description = "Local exact secret-free rollback worker HMAC environment."
  type        = string
  default     = ""
}

variable "hmac_worker_provenance_receipt_path" {
  description = "Local clean-origin signed candidate worker provenance receipt."
  type        = string
  default     = ""
}

variable "hmac_worker_provenance_signature_path" {
  description = "Local KMS signature for the candidate worker provenance receipt."
  type        = string
  default     = ""
}

variable "hmac_worker_rollback_provenance_receipt_path" {
  description = "Local clean-origin signed rollback worker provenance receipt."
  type        = string
  default     = ""
}

variable "hmac_worker_rollback_provenance_signature_path" {
  description = "Local KMS signature for the rollback worker provenance receipt."
  type        = string
  default     = ""
}

locals {
  hmac_worker_deploy_files = {
    rollback_artifact   = var.hmac_worker_rollback_artifact_path
    rollback_env        = var.hmac_worker_rollback_env_path
    rollback_receipt    = var.hmac_worker_rollback_provenance_receipt_path
    rollback_signature  = var.hmac_worker_rollback_provenance_signature_path
    candidate_artifact  = var.hmac_worker_artifact_path
    candidate_env       = var.hmac_worker_env_path
    candidate_receipt   = var.hmac_worker_provenance_receipt_path
    candidate_signature = var.hmac_worker_provenance_signature_path
    reviewed_manifest   = var.hmac_live_manifest_path
    rollout_control     = var.hmac_rollout_control_path
    base_environment    = abspath("${path.root}/../../.env.production")
    deploy_overrides    = abspath("${path.module}/../deploy/ec2.overrides.env")
    deploy_script       = abspath("${path.module}/../../scripts/deploy_to_ec2.sh")
    provenance_verifier = abspath("${path.module}/../../scripts/verify_worker_bundle_provenance.py")
  }
  hmac_worker_deploy_files_ready = alltrue([
    for path in values(local.hmac_worker_deploy_files) :
    path != "" && fileexists(path)
  ])
  hmac_worker_deploy_hashes = (
    local.hmac_worker_deploy_files_ready
    ? {
      for name, path in local.hmac_worker_deploy_files :
      name => filesha256(path)
    }
    : {}
  )
}

resource "terraform_data" "hmac_worker_deploy" {
  count = var.enable_hmac_worker_deploy ? 1 : 0

  input = {
    rotation_epoch     = var.hmac_rotation_epoch
    mode               = var.hmac_worker_deploy_mode
    cleanup_domain     = var.hmac_cleanup_domain
    advance_stage      = var.hmac_worker_advance_stage
    provenance_key_arn = aws_kms_key.mcp_source_publisher_signing.arn
    complete_artifacts = local.hmac_worker_deploy_hashes
  }

  triggers_replace = [
    jsonencode(local.hmac_worker_deploy_hashes),
    var.hmac_worker_deploy_mode,
    var.hmac_cleanup_domain,
    tostring(var.hmac_worker_advance_stage),
  ]

  lifecycle {
    precondition {
      condition = (
        local.hmac_worker_deploy_files_ready
        && try(
          local.hmac_worker_deploy_hashes.candidate_artifact
          != local.hmac_worker_deploy_hashes.rollback_artifact,
          false,
        )
        && var.hmac_worker_deploy_mode == var.hmac_gate_mode
        && (
          var.hmac_worker_deploy_mode != "cleanup"
          || var.hmac_cleanup_domain != ""
        )
      )
      error_message = "Worker deploy requires complete distinct signed candidate/rollback artifacts and an explicit cleanup domain."
    }
  }

  provisioner "local-exec" {
    command     = "\"$HMAC_WORKER_DEPLOY_SCRIPT\" --go"
    interpreter = ["/usr/bin/env", "bash", "-c"]
    working_dir = path.root
    environment = {
      TEAMAGENT_HMAC_DEPLOY_FROM_TERRAFORM      = "1"
      HMAC_WORKER_DEPLOY_SCRIPT                 = abspath("${path.module}/../../scripts/deploy_to_ec2.sh")
      HMAC_PREFLIGHT_MANIFEST                   = var.hmac_live_manifest_path
      HMAC_ROLLOUT_CONTROL                      = var.hmac_rollout_control_path
      HMAC_WORKER_MODE                          = var.hmac_worker_deploy_mode
      HMAC_CLEANUP_DOMAIN                       = var.hmac_cleanup_domain
      HMAC_WORKER_ADVANCE_STAGE                 = var.hmac_worker_advance_stage ? "1" : "0"
      HMAC_WORKER_ARTIFACT                      = local.hmac_worker_deploy_files.candidate_artifact
      HMAC_WORKER_ENV                           = local.hmac_worker_deploy_files.candidate_env
      HMAC_WORKER_ROLLBACK_ARTIFACT             = var.hmac_worker_rollback_artifact_path
      HMAC_WORKER_ROLLBACK_ENV                  = var.hmac_worker_rollback_env_path
      HMAC_WORKER_PROVENANCE_RECEIPT            = local.hmac_worker_deploy_files.candidate_receipt
      HMAC_WORKER_PROVENANCE_SIGNATURE          = local.hmac_worker_deploy_files.candidate_signature
      HMAC_WORKER_ROLLBACK_PROVENANCE_RECEIPT   = var.hmac_worker_rollback_provenance_receipt_path
      HMAC_WORKER_ROLLBACK_PROVENANCE_SIGNATURE = var.hmac_worker_rollback_provenance_signature_path
      HMAC_WORKER_PROVENANCE_KEY_ARN            = aws_kms_key.mcp_source_publisher_signing.arn
      HMAC_WORKER_EXPECTED_HASHES               = jsonencode(local.hmac_worker_deploy_hashes)
    }
  }

  depends_on = [
    terraform_data.production_image_release_gate,
    terraform_data.hmac_live_task_gate,
  ]
}
