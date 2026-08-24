"""PR2-A0.2.2b: secret 値 read の分離実装の契約。

A0.2.2a（non-secret read）とは意図的に別 PR / 別 statement に分ける。secret 面は
境界判断が異なるため、独立にレビューできる形を機械で固定する。

対象は `aws_secretsmanager_secret_version.db_password`（infra/terraform/rds.tf）の
provider read ちょうど 1 つで、2026-08-20T05:45:10Z の refresh で AccessDenied を実測。

kms:Decrypt は追加しない（実測 2 点で不要）:
  1. db_password の KmsKeyId 未設定 = AWS managed key。その key policy が
     Principal {"AWS":"*"} + kms:ViaService + kms:CallerAccount で Decrypt を直接許可
  2. 対照実験: teamagent-dev-ecs-exec-mcp は同じ AWS managed key の
     teamagent/dev/database-url に GetSecretValue=allowed / kms:Decrypt=implicitDeny
     のまま本番稼働

各ガードは変異で壊すと赤くなることを実証する（リポジトリ規約）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_EVIDENCE_TF = ROOT / "infra/terraform/runtime_evidence.tf"
RDS_TF = ROOT / "infra/terraform/rds.tf"
GUARD = ROOT / "infra/deploy/terraform_runtime_guard.sh"
RUNBOOK = ROOT / "docs/runbooks/secret_bearing_plan.md"

SID = "ReadExactTerraformManagedSecretValue"


def _statement(sid: str) -> str:
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    match = re.search(rf'sid\s*=\s*"{re.escape(sid)}"', tf)
    assert match, f"sid が見つかりません: {sid}"
    end = tf.index("statement {", match.start())
    return tf[match.start() : end]


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _sid_position(tf: str) -> int:
    match = re.search(rf'sid\s*=\s*"{re.escape(SID)}"', tf)
    assert match, SID
    return match.start()


def _evidence_doc() -> str:
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    return tf[
        tf.index('data "aws_iam_policy_document" "runtime_evidence_automation"') : tf.index(
            'resource "aws_iam_role_policy" "runtime_evidence_automation"'
        )
    ]


# ── 1 secret × 1 action ────────────────────────────────────────────────────


def test_secret_read_grants_exactly_one_action() -> None:
    """許可は secretsmanager:GetSecretValue のみ。他の secretsmanager action を混ぜない。"""
    stmt = _statement(SID)
    assert set(re.findall(r'"(secretsmanager:[A-Za-z]+)"', stmt)) == {
        "secretsmanager:GetSecretValue"
    }
    for forbidden in ("Put", "Create", "Delete", "Update", "Restore", "Tag"):
        assert f"secretsmanager:{forbidden}" not in stmt


def test_secret_read_targets_exactly_the_db_password_secret() -> None:
    """resource は db_password の exact ARN 1 本のみ（他 secret へ広げない）。"""
    stmt = _strip_comments(_statement(SID))
    arns = re.findall(r'"(arn:aws:secretsmanager:[^"]+)"', stmt)
    assert len(arns) == 1, arns
    arn = arns[0]
    assert arn.endswith("/db_password-ObO75F"), arn
    # ワイルドカードは guard の exact_secret_arn 契約違反
    assert "*" not in stmt
    # region / account / 名前は config 由来の式で組み立てる（丸ごとの literal にしない）
    assert "${var.aws_region}" in arn
    assert "${data.aws_caller_identity.current.account_id}" in arn
    assert "${var.project_name}/${var.environment}/db_password-" in arn


def test_secret_read_does_not_reference_the_secret_resource() -> None:
    """secret resource を参照しない（bootstrap reviewed closure への流入を防ぐ）。

    2026-08-24 実測: aws_secretsmanager_secret.db_password を参照すると managed
    resource が closure へ入り、EXPECTED_POST_CUT_MANAGED と
    infra/bootstrap/bootstrap_contract.json（existing_dependency_addresses / targets）
    まで書き換えが波及する。read grant のためにその契約は広げない。
    6 桁 suffix は AWS 生成で config から導出できないため、そこだけ literal になる。
    """
    stmt = _strip_comments(_statement(SID))
    assert "aws_secretsmanager_secret." not in stmt


def test_the_only_secret_value_read_in_config_is_db_password() -> None:
    """config 側で値を読む箇所が db_password だけであること（許可対象の妥当性）。

    data "aws_secretsmanager_secret_version" が増えたら、その secret も
    refresh で読まれるため、この許可だけでは足りなくなる（403 の再発）。
    """
    tf_dir = ROOT / "infra/terraform"
    data_sources = []
    managed = []
    for tf_file in tf_dir.glob("*.tf"):
        body = tf_file.read_text(encoding="utf-8")
        data_sources += re.findall(r'data "aws_secretsmanager_secret_version" "([a-z0-9_]+)"', body)
        managed += re.findall(r'resource "aws_secretsmanager_secret_version" "([a-z0-9_]+)"', body)
    assert data_sources == [], f"値読み data source が増えています: {data_sources}"
    assert managed == ["db_password"], managed
    assert 'resource "aws_secretsmanager_secret_version" "db_password"' in RDS_TF.read_text()


# ── kms:Decrypt を足さない ────────────────────────────────────────────────


def test_secret_read_does_not_add_kms_decrypt() -> None:
    """kms:Decrypt は追加しない（AWS managed key の key policy が直接許可）。"""
    assert "kms:" not in _strip_comments(_statement(SID))


def test_kms_decision_is_documented_with_both_measurements() -> None:
    """不要と判断した根拠 2 点がコメントに残っている（後任が再現できる形）。"""
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    comment = tf[tf.index("# PR2-A0.2.2b") : _sid_position(tf)]
    assert "AWS managed key" in comment
    assert "ViaService" in comment
    assert "対照実験" in comment
    assert "customer managed key" in comment


# ── exact ARN 契約（guard との整合） ───────────────────────────────────────


def test_guard_requires_exact_secret_arn_for_secretsmanager_allows() -> None:
    """guard 側が secretsmanager: の Allow へ exact secret ARN を要求している。

    ワイルドカード ARN で許可すると、この契約に抵触して plan が落ちる。
    """
    guard = GUARD.read_text(encoding="utf-8")
    assert "def exact_secret_arn:" in guard
    assert "db_password" in guard[guard.index("def exact_secret_arn:") :][:900]
    assert 'allows_action_prefix("secretsmanager:")' in guard


def test_secret_read_is_separate_from_the_non_secret_statements() -> None:
    """A0.2.2a の statement に secret read を相乗りさせない（分離実装の担保）。"""
    for sid in ("ReadExactTerraformBucketConfigurations", "ReadExactInstanceTypeCatalog"):
        assert "secretsmanager:" not in _strip_comments(_statement(sid))
    doc = _strip_comments(_evidence_doc())
    assert doc.count('"secretsmanager:GetSecretValue"') == 1


def test_secret_read_lives_in_the_evidence_inline_policy() -> None:
    """evidence inline policy 内に置く。

    manage-b は action 数 154 と sha256 f419e9d1… の pin があり、新 statement を
    足すだけでも（document 全体を走査する正規表現のため）確実に赤くなる。
    """
    assert f'"{SID}"' in _evidence_doc()
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    manage_b = tf[
        tf.index(
            'data "aws_iam_policy_document" "runtime_automation_control_plane_manage_b"'
        ) : tf.index('data "aws_iam_policy_document" "runtime_automation_control_plane_core"')
    ]
    assert "secretsmanager:GetSecretValue" not in manage_b


def test_secret_read_carries_no_condition() -> None:
    assert "condition" not in _strip_comments(_statement(SID))


# ── secret 含有成果物の取り扱い規約 ────────────────────────────────────────


def test_secret_bearing_artifact_rules_are_written_down() -> None:
    """plan 成果物が secret を含む前提の規約が runbook に明記されている。"""
    text = RUNBOOK.read_text(encoding="utf-8")
    for rule in ("repository の外", "0600", "生 JSON を出さない", "redacted", "破棄"):
        assert rule in text, rule
    assert "kms:Decrypt" in text
    assert "customer managed key" in text


def test_statement_comment_points_at_the_runbook() -> None:
    """コード側からも取り扱い規約へ辿れる。"""
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    comment = tf[tf.index("# PR2-A0.2.2b") : _sid_position(tf)]
    assert "secret_bearing_plan.md" in comment
