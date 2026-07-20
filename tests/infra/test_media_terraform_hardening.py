from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MEDIA_TF = (ROOT / "infra/terraform/tiktok_acquire.tf").read_text(encoding="utf-8")


def _block(kind: str, name: str) -> str:
    marker = f'{kind} "{name}"'
    start = MEDIA_TF.index(marker)
    next_resource = MEDIA_TF.find("\nresource ", start + len(marker))
    next_data = MEDIA_TF.find("\ndata ", start + len(marker))
    ends = [position for position in (next_resource, next_data) if position >= 0]
    return MEDIA_TF[start : min(ends) if ends else len(MEDIA_TF)]


def test_dispatcher_runs_and_tags_only_the_canonical_existing_family() -> None:
    policy = _block(
        'data "aws_iam_policy_document"',
        "tiktok_dispatch_policy",
    )

    assert 'task-definition/${local.media_name}:*"' in policy
    assert "task-definition/${local.media_name}-worker:*" not in policy
    assert 'actions   = ["ecs:TagResource"]' in policy
    assert "task/${aws_ecs_cluster.tiktok[0].name}/*" in policy
    assert 'variable = "ecs:CreateAction"' in policy
    assert 'values   = ["RunTask"]' in policy


def test_media_worker_has_no_unused_secret_injection_or_wildcard_secret_policy() -> None:
    task = _block('resource "aws_ecs_task_definition"', "tiktok_acquire")

    assert "secrets = []" in task
    assert "APIFY_API_TOKEN" not in MEDIA_TF
    assert 'resource "aws_iam_role_policy" "tiktok_exec_secrets"' not in MEDIA_TF
    assert "secret:${var.project_name}/${var.environment}/tiktok/*" not in MEDIA_TF


def test_job_budget_dispatch_concurrency_and_retention_share_one_contract() -> None:
    queue = _block('resource "aws_sqs_queue"', "tiktok_jobs")
    dispatcher = _block('resource "aws_lambda_function"', "tiktok_dispatch")
    mapping = _block(
        'resource "aws_lambda_event_source_mapping"',
        "tiktok_dispatch",
    )
    lifecycle = _block(
        'resource "aws_s3_bucket_lifecycle_configuration"',
        "media_jobs",
    )

    assert "visibility_timeout_seconds = 180" in queue
    assert "maxReceiveCount     = 5" in queue
    assert "reserved_concurrent_executions = 2" in dispatcher
    assert "batch_size              = 1" in mapping
    assert 'function_response_types = ["ReportBatchItemFailures"]' in mapping
    assert "maximum_concurrency = 2" in mapping
    assert "default     = 2592000" in MEDIA_TF
    assert "days = 30" in lifecycle
    assert "noncurrent_days = 30" in lifecycle


def test_artifacts_are_version_bound_and_core_writes_only_job_inputs() -> None:
    versioning = _block('resource "aws_s3_bucket_versioning"', "media_jobs")
    core_policy = _block(
        'data "aws_iam_policy_document"',
        "tiktok_mcp_policy",
    )
    dispatcher_policy = _block(
        'data "aws_iam_policy_document"',
        "tiktok_dispatch_policy",
    )
    task = _block('resource "aws_ecs_task_definition"', "tiktok_acquire")

    assert 'status = "Enabled"' in versioning
    assert 'sid = "S3JobInputsWrite"' in core_policy
    assert "media-jobs/*/input/*" in core_policy
    assert 'sid = "S3JobArtifactsRead"' in core_policy
    assert '"s3:GetObjectVersion"' in core_policy
    assert 'sid = "IssueAndVerifyExactMediaCapabilities"' in dispatcher_policy
    assert "media-jobs/*/attempts/*" in dispatcher_policy
    assert "task_role_arn" not in task
    assert 'resource "aws_iam_role" "tiktok_task"' not in MEDIA_TF
    assert "MEDIA_JOBS_TABLE" not in task
    assert "MEDIA_JOB_BUCKET" not in task


def test_execution_role_can_only_fetch_control_env_not_job_data() -> None:
    policy = _block(
        'data "aws_iam_policy_document"',
        "tiktok_exec_control",
    )

    assert 'actions   = ["s3:GetBucketLocation"]' in policy
    assert 'actions   = ["s3:GetObject"]' in policy
    assert "media-jobs/*/control/*.env" in policy
    assert "media-jobs/*/input/*" not in policy
    assert "media-jobs/*/attempts/*" not in policy


def test_stopped_recovery_has_delivery_and_invocation_failure_sinks() -> None:
    target = _block('resource "aws_cloudwatch_event_target"', "media_task_stopped")
    invoke = _block(
        'resource "aws_lambda_function_event_invoke_config"',
        "tiktok_dispatch",
    )
    delivery_policy = _block(
        'data "aws_iam_policy_document"',
        "media_stopped_delivery_dlq",
    )
    dispatch_policy = _block(
        'data "aws_iam_policy_document"',
        "tiktok_dispatch_policy",
    )

    assert "dead_letter_config" in target
    assert "media_stopped_delivery_dlq" in target
    assert "aws_sqs_queue_policy.media_stopped_delivery_dlq" in target
    assert "maximum_retry_attempts" in invoke
    assert "media_stopped_invocation_dlq" in invoke
    assert 'identifiers = ["events.amazonaws.com"]' in delivery_policy
    assert "aws:SourceArn" in delivery_policy
    assert 'sid       = "WriteAsyncFailureDestination"' in dispatch_policy
    assert 'actions   = ["sqs:SendMessage"]' in dispatch_policy
    assert 'resource "aws_cloudwatch_metric_alarm" "media_stopped_delivery_dlq_depth"' in MEDIA_TF
    assert 'resource "aws_cloudwatch_metric_alarm" "media_stopped_invocation_dlq_depth"' in MEDIA_TF


def test_runtime_guard_keeps_generic_and_compatibility_enable_aliases_active() -> None:
    guard = (ROOT / "infra/deploy/terraform_runtime_guard.sh").read_text(encoding="utf-8")

    assert '"-var=media_worker_image=$DESIRED_TIKTOK_IMAGE"' in guard
    assert '"-var=tiktok_acquire_image="' in guard
    assert '"-var=enable_media_worker=true"' in guard
    assert '"-var=enable_tiktok_acquire=true"' in guard


def test_stopped_reconciler_and_deprecated_ecr_output_are_explicit() -> None:
    rule = _block('resource "aws_cloudwatch_event_rule"', "media_task_stopped")
    legacy_output = _block("output", "tiktok_acquire_ecr_url")
    canonical_output = _block("output", "media_worker_ecr_url")

    assert 'detail-type = ["ECS Task State Change"]' in rule
    assert 'lastStatus = ["STOPPED"]' in rule
    assert 'task-definition/${local.media_name}:"' in rule
    assert "DEPRECATED legacy teamagent-dev-tiktok-acquire repository" in legacy_output
    assert "not a runtime image or release push target" in legacy_output
    assert "aws_ecr_repository.mcp_media.repository_url" in canonical_output
