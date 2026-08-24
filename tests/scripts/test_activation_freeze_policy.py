"""Activation Freeze v2 の persistent explicit-deny 契約（PR2-A0.x）。

2026-08-24 ユーザー裁定:
  - session-only control は禁止。実際に mutation できる principal 側へ
    persistent な explicit Deny を置く
  - 対象は CloudTrail + deploy code の census から決める（AIIAdev だけでは不足）
  - Freeze v2 の境界は「最後の変更時刻」ではなく「policy 適用後に mutation が
    deny され in-flight=0 を確認した時刻」
  - repo 実装と saved targeted plan 生成まで GO。production apply 直前で human gate

freeze が守るべき surface は 3 つで、うち s3 だけは prefix scope が必須
（"*" にすると Terraform state 書き込みまで止まり state 操作が不能になる）。

各ガードは変異で壊すと赤くなることを実証する（リポジトリ規約）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FREEZE_TF = ROOT / "infra/terraform/activation_freeze_policy.tf"
RUNBOOK = ROOT / "docs/runbooks/activation_freeze.md"

WORKLOAD_DENY_ACTIONS = {
    "ecs:DeregisterTaskDefinition",
    "ecs:RegisterTaskDefinition",
    "ecs:UpdateService",
    "events:PutRule",
    "events:PutTargets",
    "events:RemoveTargets",
    "lambda:UpdateFunctionConfiguration",
}
GENERATION_DENY_ACTIONS = {
    "codebuild:RetryBuild",
    "codebuild:RetryBuildBatch",
    "codebuild:StartBuild",
    "codebuild:StartBuildBatch",
    "codebuild:UpdateProject",
}
BUILDSPEC_DENY_ACTIONS = {
    "s3:DeleteObject",
    "s3:DeleteObjectVersion",
    "s3:PutObject",
    "s3:PutObjectRetention",
}
# census で mutation 到達性を確認した role（terraform のリソース名）
CENSUSED_ROLES = {
    "runtime_automation",
    "codebuild_launcher",
    "approval_caller",
    "openclaw_publisher",
    "release_launcher",
    "release_control_updater",
    "image_deployment_gate",
    "media_cutover_attestor",
    "tiktok_build_launcher",
}


def _tf() -> str:
    return FREEZE_TF.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _statement(sid: str) -> str:
    tf = _tf()
    match = re.search(rf'sid\s*=\s*"{re.escape(sid)}"', tf)
    assert match, sid
    tail = tf[match.start() :]
    end = tail.find("\n  statement {")
    return tail if end == -1 else tail[:end]


# ── 3 つの deny surface ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("sid", "expected"),
    [
        ("DenyWorkloadDeploymentDuringActivationFreeze", WORKLOAD_DENY_ACTIONS),
        ("DenyGenerationPublisherDuringActivationFreeze", GENERATION_DENY_ACTIONS),
        ("DenyBuildspecGenerationWritesDuringActivationFreeze", BUILDSPEC_DENY_ACTIONS),
    ],
)
def test_each_deny_surface_has_the_exact_action_set(sid: str, expected: set[str]) -> None:
    stmt = _strip_comments(_statement(sid))
    assert 'effect  = "Deny"' in stmt or 'effect = "Deny"' in stmt
    actions = set(re.findall(r'"([a-z0-9]+:[A-Za-z0-9]+)"', stmt))
    assert actions == expected, f"{sid}: {actions ^ expected}"


def test_workload_deny_covers_every_action_observed_in_the_violations() -> None:
    """実際に B3 を作り直した action が全て deny 対象に入っている。

    2026-08-21 08:45-08:54Z の実測: RegisterTaskDefinition ×10 /
    UpdateService ×4 / PutTargets ×4 / Lambda env 更新。
    """
    stmt = _strip_comments(_statement("DenyWorkloadDeploymentDuringActivationFreeze"))
    for observed in (
        "ecs:RegisterTaskDefinition",
        "ecs:UpdateService",
        "events:PutTargets",
        "lambda:UpdateFunctionConfiguration",
    ):
        assert f'"{observed}"' in stmt, observed


def test_generation_deny_covers_the_publisher_path() -> None:
    """2 波の publish を生んだ StartBuild / UpdateProject を deny する。"""
    stmt = _strip_comments(_statement("DenyGenerationPublisherDuringActivationFreeze"))
    assert '"codebuild:StartBuild"' in stmt
    assert '"codebuild:UpdateProject"' in stmt


# ── s3 は prefix scope 必須 ────────────────────────────────────────────────


def test_buildspec_deny_is_prefix_scoped_not_star() -> None:
    """s3 の deny を "*" にすると Terraform state 書き込みまで止まる（state 操作不能）。"""
    stmt = _strip_comments(_statement("DenyBuildspecGenerationWritesDuringActivationFreeze"))
    assert 'resources = ["*"]' not in stmt
    assert "codebuild-buildspecs/*" in stmt
    assert "release_evidence_bucket" in stmt


def test_state_bucket_is_never_in_any_deny_resource() -> None:
    """state backend bucket が deny 対象に入っていないこと（rebind / adopt の生命線）。"""
    tf = _strip_comments(_tf())
    assert "teamagent-tfstate" not in tf
    assert "tfstate" not in tf


# ── 対象 principal の census ───────────────────────────────────────────────


def test_policy_attaches_to_the_censused_user_and_roles() -> None:
    """AIIAdev だけでなく release/deploy 実行 role も対象（裁定 2）。"""
    tf = _strip_comments(_tf())
    assert "aws_iam_user_policy_attachment" in tf
    assert "data.aws_iam_user.aiia_dev.user_name" in tf
    referenced = set(re.findall(r"aws_iam_role\.([a-z0-9_]+)", tf))
    assert referenced == CENSUSED_ROLES, f"差分: {referenced ^ CENSUSED_ROLES}"


def test_conditional_role_is_gated_behind_its_count() -> None:
    """count 付き role は無効時に対象から外す（存在しない資源への attach を防ぐ）。"""
    tf = _strip_comments(_tf())
    assert "local.tk_enabled == 1 ? [aws_iam_role.tiktok_build_launcher[0].name] : []" in tf


def test_freeze_is_disabled_by_default_and_gated_by_one_flag() -> None:
    """既定は無効。有効化は 1 つの変数だけで、apply は human gate を経る。"""
    tf = _tf()
    assert 'variable "activation_freeze_enabled"' in tf
    block = tf[tf.index('variable "activation_freeze_enabled"') :]
    assert re.search(r"default\s*=\s*false", block[: block.index("}")])
    stripped = _strip_comments(tf)
    assert stripped.count("var.activation_freeze_enabled") == 2
    for resource in (
        'resource "aws_iam_policy" "activation_freeze"',
        'resource "aws_iam_user_policy_attachment" "activation_freeze_aiia_dev"',
    ):
        assert resource in tf
        tail = tf[tf.index(resource) :]
        assert "local.activation_freeze_count" in tail[: tail.index("\n}")]


# ── root の限界を明文化 ────────────────────────────────────────────────────


def test_root_limitation_is_documented_in_code_and_runbook() -> None:
    """root は identity policy の Deny で止まらない。この限界を残す（誤った安心の防止）。

    CloudTrail 実測で root が PutTargets ×23 / DeregisterTaskDefinition ×40 を
    実行している。root の封鎖には SCP が必要で本 policy の射程外。
    """
    tf = _tf()
    assert "root" in tf
    comment = tf[: tf.index('variable "activation_freeze_enabled"')]
    assert "SCP" in comment
    assert "バイパス" in comment
    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert "root" in runbook and "SCP" in runbook


def test_resource_scope_rationale_is_recorded() -> None:
    """ "*" を使う action と exact が使える action の根拠がコメントに残っている。"""
    comment = _tf()[: _tf().index('variable "activation_freeze_enabled"')]
    assert "resource scope の根拠" in comment
    assert "exact project ARN" in comment
    assert "Deny" in comment and "安全側" in comment


def test_census_sources_are_recorded() -> None:
    """principal を推測で選んでいないこと（CloudTrail + repo policy の 2 面）。"""
    comment = _tf()[: _tf().index('variable "activation_freeze_enabled"')]
    assert "CloudTrail" in comment
    assert "census" in comment
    assert "simulate" in comment


def test_runbook_defines_the_v2_boundary_as_post_apply_confirmation() -> None:
    """Freeze v2 の境界が「policy 適用後に deny と in-flight=0 を確認した時刻」であること。"""
    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert "in-flight" in runbook
    assert "saved targeted plan" in runbook
    assert "activation_freeze_enabled" in runbook
