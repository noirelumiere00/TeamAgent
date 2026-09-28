"""proposal_builder の資材用 KMS 鍵（2026-09-15）の契約検査。

adapters/proposal_assets.py は資材の SSE-KMS 鍵 ARN を **完全一致** で検査するため、
鍵は専用・ローテーション有効・削除猶予つきで、fargate.tf の var 経由の権限付与と
ずれないことをテキストで固定する（terraform を実行せずに検査する既存の型に合わせる）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSETS = (ROOT / "infra/terraform/proposal_builder_assets.tf").read_text(encoding="utf-8")
FARGATE = (ROOT / "infra/terraform/fargate.tf").read_text(encoding="utf-8")
VARIABLES = (ROOT / "infra/terraform/variables_fargate.tf").read_text(encoding="utf-8")


def test_dedicated_kms_key_with_rotation_and_alias() -> None:
    assert 'resource "aws_kms_key" "proposal_builder_assets"' in ASSETS
    assert "enable_key_rotation     = true" in ASSETS
    assert "deletion_window_in_days = 30" in ASSETS
    assert 'resource "aws_kms_alias" "proposal_builder_assets"' in ASSETS
    assert (
        'name          = "alias/${var.project_name}-${var.environment}-proposal-builder-assets"'
        in ASSETS
    )
    # 鍵ポリシーはアカウント root のみ（サービスプリンシパルや固定アカウント ID を焼き込まない）
    assert "data.aws_caller_identity.current.account_id" in ASSETS
    assert re.search(r"arn:aws:iam::\d{12}:", ASSETS) is None
    assert "Service" not in ASSETS.split("Statement")[1]


def test_key_arn_is_exposed_for_tfvars_and_consumed_via_variable() -> None:
    """鍵 ARN は output で tfvars に貼り、fargate.tf は var 経由で権限を付ける（直参照しない）。"""
    assert 'output "proposal_builder_assets_kms_key_arn"' in ASSETS
    assert "aws_kms_key.proposal_builder_assets.arn" in ASSETS
    assert 'variable "proposal_builder_assets_kms_key_arn"' in VARIABLES
    assert "resources = [var.proposal_builder_assets_kms_key_arn]" in FARGATE
    assert "aws_kms_key.proposal_builder_assets" not in FARGATE
