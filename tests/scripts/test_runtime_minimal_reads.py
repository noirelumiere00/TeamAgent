"""PR2-A0.2.2a: guarded plan の refresh に必要な non-secret minimal read の契約。

2026-08-20T05:45Z に trusted automation role が実行した refresh の CloudTrail 実測で、
認可失敗は ① ec2:DescribeInstanceTypes ② s3:GetBucketCORS（exact 6 bucket）
③ secretsmanager:GetSecretValue（別 PR = A0.2.2b）の 3 種のみだった。

s3 側は CORS で refresh が abort するため残り 6 action の 403 は観測できていないが、
simulate-principal-policy が全 bucket で implicitDeny を返すこと（= identity policy に
無い）と、bootstrap seed-stack の ReadExactTerraformBuckets が同じ provider 版の
aws_s3_bucket read へこの 7 action 集合を付与済みであることの 2 点で必要と判定した。
「CORS だけ足す」と次の refresh で同じ壁を 6 回踏む（P0 の 403 事故と同型）。

各ガードは変異で壊すと赤くなることを実証する（リポジトリ規約）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_EVIDENCE_TF = ROOT / "infra/terraform/runtime_evidence.tf"
BOOTSTRAP_TEST = ROOT / "tests/bootstrap/test_provenance_iam_bootstrap.py"

# provider が aws_s3_bucket 1 件を読む際に呼ぶ bucket 設定 read の集合。
# bootstrap seed-stack の ReadExactTerraformBuckets と同一であることを下でテストする。
BUCKET_READ_ACTIONS = {
    "s3:GetAccelerateConfiguration",
    "s3:GetBucketCORS",
    "s3:GetBucketLogging",
    "s3:GetBucketRequestPayment",
    "s3:GetBucketWebsite",
    "s3:GetLifecycleConfiguration",
    "s3:GetReplicationConfiguration",
}

# 403 を実測した 6 bucket（media_jobs は count=0 のため実測対象外だが将来 ON で必要）。
OBSERVED_BUCKETS = {
    "raw_files",
    "image_release_evidence",
    "openclaw_build_evidence",
    "openclaw_rollout_evidence",
    "cloudtrail",
    "bedrock_logs",
}


def _statement(sid: str) -> str:
    """sid から次の statement 開始までを切り出す（terraform fmt の整列差を吸収）。"""
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    match = re.search(rf'sid\s*=\s*"{re.escape(sid)}"', tf)
    assert match, f"sid が見つかりません: {sid}"
    end = tf.index("statement {", match.start())
    return tf[match.start() : end]


def _strip_comments(text: str) -> str:
    """コメント行を落とす。コメント本文が assertion を誤爆させる事故を防ぐ。"""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _evidence_doc_span() -> tuple[int, int]:
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    return (
        tf.index('data "aws_iam_policy_document" "runtime_evidence_automation"'),
        tf.index('resource "aws_iam_role_policy" "runtime_evidence_automation"'),
    )


# ── s3 bucket 設定 read ────────────────────────────────────────────────────


def test_bucket_config_read_grants_exactly_the_provider_read_set() -> None:
    """許可は provider の bucket read 7 action ちょうど。ワイルドカード・書き込みなし。"""
    stmt = _statement("ReadExactTerraformBucketConfigurations")
    assert set(re.findall(r'"(s3:[A-Za-z]+)"', stmt)) == BUCKET_READ_ACTIONS
    assert ":*" not in stmt
    for write in ("Put", "Delete", "Create", "Restore", "Replicate"):
        assert f"s3:{write}" not in stmt


def test_bucket_config_read_matches_the_bootstrap_precedent() -> None:
    """同じ provider read 経路の先例（bootstrap の ReadExactTerraformBuckets）と action 集合が一致。

    ここが乖離したら、片方だけ壁を踏む状態になる（どちらかの列挙が古い）。
    """
    body = BOOTSTRAP_TEST.read_text(encoding="utf-8")
    # 先例の action 集合リテラルを、既知の 1 要素から囲みブレースを辿って取り出す
    anchor = body.index('"s3:GetAccelerateConfiguration"')
    open_brace = body.rindex("{", 0, anchor)
    close_brace = body.index("}", anchor)
    block = body[open_brace:close_brace]
    precedent = set(re.findall(r'"(s3:[A-Za-z0-9]+)"', block))
    assert precedent == BUCKET_READ_ACTIONS


def test_bucket_config_read_never_uses_the_misspelled_lifecycle_action() -> None:
    """s3:GetBucketLifecycleConfiguration は存在しない action 名（bootstrap で訂正済み）。"""
    stmt = _statement("ReadExactTerraformBucketConfigurations")
    assert "s3:GetBucketLifecycleConfiguration" not in stmt
    assert "s3:GetLifecycleConfiguration" in stmt


def test_preexisting_misspelled_lifecycle_action_count_is_pinned() -> None:
    """既存 statement に残る誤 action 名の件数を固定する（A0.2.2a のスコープ外）。

    s3:GetBucketLifecycleConfiguration は存在しない action 名で、bootstrap 側は
    commit 722a94a で訂正済みだが runtime 側には inert のまま残っている。
    A0.2.2a では既存 statement を触らない（IAM の変更面を最小に保つ）ため、
    「増えていない」ことだけを固定し、訂正は別 PR の backlog とする。
    """
    tf = _strip_comments(RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8"))
    assert tf.count('"s3:GetBucketLifecycleConfiguration"') == 2


def _declared_bucket_name_expressions() -> dict[str, str]:
    """宣言済み aws_s3_bucket ごとの bucket 名の式（正規化済み）。

    ARN は bucket resource ではなく **名前の式** から組み立てているため、
    カバレッジは「各 bucket resource が名前に使っている式が statement に現れるか」で見る。
    """
    expressions: dict[str, str] = {}
    for tf_file in (ROOT / "infra/terraform").glob("*.tf"):
        body = tf_file.read_text(encoding="utf-8")
        for match in re.finditer(r'^resource "aws_s3_bucket" "([a-z0-9_]+)" \{', body, re.M):
            name = match.group(1)
            tail = body[match.end() : match.end() + 900]
            arg = re.search(r"^\s+bucket\s*=\s*(.+)$", tail, re.M)
            assert arg, f"bucket 引数が読めません: {name}"
            value = arg.group(1).strip()
            if value.startswith('"'):
                value = value.strip('"')
            else:
                value = "${" + value + "}"
            expressions[name] = value
    return expressions


def test_bucket_config_read_covers_every_declared_bucket() -> None:
    """宣言済み aws_s3_bucket を過不足なくカバーする（新 bucket 追加時の 403 再発防止）。"""
    stmt = _statement("ReadExactTerraformBucketConfigurations")
    expressions = _declared_bucket_name_expressions()
    assert len(expressions) == 7, expressions
    missing = [name for name, expr in expressions.items() if f'"arn:aws:s3:::{expr}"' not in stmt]
    assert not missing, f"ARN が組み立てられていない bucket: {missing}"
    assert OBSERVED_BUCKETS <= set(expressions)


def test_bucket_config_read_does_not_reference_bucket_resources() -> None:
    """bucket resource を参照しない（bootstrap reviewed closure への流入を防ぐ）。

    resource 参照にすると managed resource が 4 件 closure へ入り、
    infra/bootstrap/bootstrap_contract.json（existing_dependency_addresses 等）まで
    書き換えが波及する。read-only grant のためにその契約を広げない。
    """
    stmt = _strip_comments(_statement("ReadExactTerraformBucketConfigurations"))
    assert "aws_s3_bucket." not in stmt
    assert stmt.count("arn:aws:s3:::") == 7


def test_bucket_config_read_is_bucket_level_not_object_level() -> None:
    """bucket 設定 read なので resource に /* を付けない（object 権限の混入防止）。"""
    stmt = _statement("ReadExactTerraformBucketConfigurations")
    assert "/*" not in stmt


def test_bucket_config_read_gates_conditional_buckets_behind_their_count() -> None:
    """count 条件付き bucket は無効時に付与しない（先例 ReadExactDeploymentSubjectGraph と同形）。

    count の条件式と同じ式でゲートする。無効なら concat() の要素が空リストになり、
    ARN は一切現れない。
    """
    stmt = _statement("ReadExactTerraformBucketConfigurations")
    assert "concat(" in stmt
    for guard, marker in (
        ("var.enable_cloudtrail", "-cloudtrail-"),
        ("var.enable_bedrock_invocation_logging", "-bedrock-logs-"),
        ("local.tk_enabled == 1", "local.media_bucket_name"),
    ):
        assert guard in stmt, guard
        assert marker in stmt, marker
        # 条件が対象 ARN より前に現れる = 三項演算子で包まれている
        assert stmt.index(guard) < stmt.index(marker)
        assert "? [" in stmt and "] : []" in stmt


# ── ec2 instance type read ────────────────────────────────────────────────


def test_instance_type_read_is_a_single_readonly_action() -> None:
    """ec2:DescribeInstanceTypes だけ。他の ec2 action を相乗りさせない。"""
    stmt = _statement("ReadExactInstanceTypeCatalog")
    assert set(re.findall(r'"(ec2:[A-Za-z]+)"', stmt)) == {"ec2:DescribeInstanceTypes"}
    for forbidden in ("ec2:Run", "ec2:Create", "ec2:Terminate", "ec2:Modify", "ec2:Delete"):
        assert forbidden not in stmt


def test_instance_type_read_documents_why_resource_is_star() -> None:
    """resource="*" の理由（ARN を取らない API）がコメントに残っている。"""
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    span = tf[: re.search(r'sid\s*=\s*"ReadExactInstanceTypeCatalog"', tf).start()]
    comment = span[span.rindex("# PR2-A0.2.2a") :]
    assert "ARN" in comment
    assert "bastion" in comment and "worker" in comment
    # コメントに Terraform リソースアドレスを書かない（closure テストが本文も走査する）
    assert "aws_instance." not in comment


# ── 追加場所（managed policy を汚さない） ───────────────────────────────────


def test_new_statements_live_in_the_evidence_inline_policy() -> None:
    """statement は runtime_evidence_automation（inline -evidence）の中にあること。

    managed policy 側（manage-a/b/core）に足すと action ハッシュ・statement 数の
    contract test 群と衝突し、6144 文字の size precondition にも影響する。
    """
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    doc_start, doc_end = _evidence_doc_span()
    for sid in ("ReadExactTerraformBucketConfigurations", "ReadExactInstanceTypeCatalog"):
        position = tf.index(f'"{sid}"')
        assert doc_start < position < doc_end, sid


def test_new_statements_carry_no_conditions() -> None:
    """condition を付けない（Null-condition 数一致テストへの抵触を避ける既存規約）。"""
    for sid in ("ReadExactTerraformBucketConfigurations", "ReadExactInstanceTypeCatalog"):
        assert "condition" not in _strip_comments(_statement(sid))


def test_a022a_statements_grant_no_secret_value_read() -> None:
    """A0.2.2a の statement 自体には secret 値 read を含めない。

    PR2-A0.2.2b で evidence policy に別 statement として
    ReadExactTerraformManagedSecretValue が追加された（db_password の exact ARN）。
    その境界越えは意図的で、契約は tests/scripts/test_runtime_secret_read.py が持つ。
    ここでは A0.2.2a の 2 statement に相乗りしていないことだけを固定する。
    """
    for sid in ("ReadExactTerraformBucketConfigurations", "ReadExactInstanceTypeCatalog"):
        assert "secretsmanager:" not in _strip_comments(_statement(sid))


def test_derivation_of_each_action_class_is_documented() -> None:
    """403 実測と「同経路継続」の由来がコメントに区別して残っている（推測追加の禁止）。"""
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    comment = tf[
        tf.index("# PR2-A0.2.2a") : tf.index('sid = "ReadExactTerraformBucketConfigurations"')
    ]
    assert "403 実測" in comment
    assert "simulate-principal-policy" in comment
    assert "ReadExactTerraformBuckets" in comment


def test_new_comments_inject_no_terraform_graph_edges() -> None:
    """コメントに Terraform リソースアドレスやワイルドカード action を書かない。

    2026-08-24 実測: bootstrap closure テストは tf ファイル本文を（コメントも含めて）
    走査して参照を抽出するため、コメント内の `aws_instance.worker` のような文字列が
    偽のグラフ辺を作り reviewed graph を壊す。同様に `ec2:Describe*` のような
    ワイルドカード表記もコメントに書くと「ワイルドカード禁止」アサーションを誤爆させる。
    """
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    comments = "\n".join(line for line in tf.splitlines() if line.lstrip().startswith("#"))
    for token in ("aws_instance.", "aws_s3_bucket.", "aws_secretsmanager_secret_version."):
        assert token not in comments, f"コメントにリソースアドレス: {token}"
    assert "ec2:Describe*" not in comments
    assert "s3:Get*" not in comments


# ── PR2-A0.2.2c: connect /app snapshot object の exact read ─────────────────
#
# A0.3.2 で adopt-plan が normal guarded plan と同じ live snapshot 経路を共有する
# ようになった帰結で必要になった read。2026-08-25 の preflight で HeadObject 403 を
# 実測し、simulate で不足 3 action を確定した。GetObjectVersion は既許可のため追加しない。

CONNECT_APP_SID = "ReadExactConnectAppSnapshotObject"
CONNECT_APP_ACTIONS = {"s3:GetObject", "s3:GetObjectRetention", "s3:GetObjectTagging"}


def test_connect_app_read_grants_exactly_the_three_measured_actions() -> None:
    """403 実測で確定した 3 action ちょうど。GetObjectVersion は既許可なので足さない。"""
    stmt = _statement(CONNECT_APP_SID)
    assert set(re.findall(r'"(s3:[A-Za-z]+)"', stmt)) == CONNECT_APP_ACTIONS
    assert "s3:GetObjectVersion" not in stmt
    for write in ("Put", "Delete", "Create", "Restore"):
        assert f"s3:{write}" not in stmt


def test_connect_app_read_is_scoped_to_one_exact_object() -> None:
    """prefix wildcard 化は禁止。exact object ARN 1 本のみ。"""
    stmt = _strip_comments(_statement(CONNECT_APP_SID))
    arns = re.findall(r'"(arn:aws:s3:::[^"]+)"', stmt)
    assert len(arns) == 1, arns
    arn = arns[0]
    assert arn.endswith("/codebuild/connect-web-app.html"), arn
    assert "*" not in arn
    # bucket 名は config 由来の式で組み立てる（丸ごとの literal にしない）
    assert "${var.project_name}-${var.environment}-raw-files" in arn


def test_connect_app_read_matches_what_the_guard_actually_reads() -> None:
    """guard が読む bucket / key と statement の resource が一致する。

    ここが乖離すると preflight が再び 403 で止まる。
    """
    guard = (ROOT / "infra/deploy/terraform_runtime_guard.sh").read_text(encoding="utf-8")
    assert 'connect_app_bucket="${PROJECT}-${ENVIRONMENT}-raw-files"' in guard
    assert 'connect_app_key="codebuild/connect-web-app.html"' in guard
    stmt = _strip_comments(_statement(CONNECT_APP_SID))
    assert "-raw-files/codebuild/connect-web-app.html" in stmt


def test_connect_app_read_documents_the_a032_origin() -> None:
    """なぜ後から必要になったか（A0.3.2 の snapshot 経路共有）が残っている。"""
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    comment = tf[tf.index("# PR2-A0.2.2c") : tf.index(f'sid = "{CONNECT_APP_SID}"')]
    assert "A0.3.2" in comment
    assert "403" in comment
    assert "wildcard" in comment


def test_connect_app_read_lives_in_the_evidence_inline_policy() -> None:
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    doc_start, doc_end = _evidence_doc_span()
    assert doc_start < tf.index(f'"{CONNECT_APP_SID}"') < doc_end
