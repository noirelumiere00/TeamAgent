"""EC2 worker（teamagent-dev-worker）の撤去が戻されていないことを確かめる。

2026-09-28 の裁定で、2026-08-03 から停止していた EC2 worker を terraform で撤去した
（ディスクはスナップショットで保存済み）。撤去は次の 3 点でできている。

- ``infra/terraform/worker.tf`` を削除した（インスタンス・SG・db_from_worker・IAM 4 資源）
- ``enable_hmac_worker_deploy`` に「常に false」の validation を付け、worker の HMAC 配布経路を
  plan の時点で止まるようにした（配布経路の本体 ``terraform_data.hmac_worker_deploy`` は
  count でこの変数につながっている。つながりが切れると封印が効かないので、それも確かめる）
- guard（``validate_exact_runtime_iam_plan``）の必須リストから ``worker_app`` を外した

どれかが戻ると、本番の state と repo が食い違う（空の EC2 が新しい AMI で立つ、guard が
「required IAM resource missing」で全 plan を止める など）ので、ここで赤にする。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TF_ROOT = ROOT / "infra" / "terraform"
WORKER_DEPLOY_TF = TF_ROOT / "hmac_worker_deploy.tf"
HMAC_KEYRINGS_TF = TF_ROOT / "hmac_keyrings.tf"
GUARD = ROOT / "infra" / "deploy" / "terraform_runtime_guard.sh"
SEALED_FLAG = "var.enable_hmac_worker_deploy"

RETIRED_RESOURCES = (
    ("aws_instance", "worker"),
    ("aws_security_group", "worker"),
    ("aws_security_group_rule", "db_from_worker"),
    ("aws_iam_role", "worker"),
    ("aws_iam_role_policy", "worker_app"),
    ("aws_iam_role_policy_attachment", "worker_ssm"),
    ("aws_iam_instance_profile", "worker"),
)
RETIRED_DATA = (
    ("aws_iam_policy_document", "worker_assume"),
    ("aws_iam_policy_document", "worker_app"),
)
RETIRED_OUTPUTS = ("worker_instance_id", "worker_connect_command")
RETIRED_VARIABLES = ("worker_instance_type", "worker_root_gb")

# db-sg に残る 5432 の許可。worker の撤去に巻き込んで消していないことを確かめる。
REMAINING_DB_RULES = (
    "db_from_bastion",
    "db_from_mcp",
    "db_from_connect_web",
    "db_from_ingest",
    "db_from_morning_digest",
)

REMAINING_REQUIRED_IAM = (
    "aws_iam_role_policy.lambda_app",
    "aws_iam_role_policy.mcp_task",
    "aws_iam_role_policy.connect_web_task[0]",
    "aws_iam_role_policy.ingest_task[0]",
    "aws_iam_role_policy.morning_digest_task[0]",
)


def _all_terraform() -> str:
    paths = sorted(TF_ROOT.glob("*.tf"))
    assert paths, "infra/terraform に .tf が見つからない"
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def _reference(address: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_.]){re.escape(address)}(?![A-Za-z0-9_])")


def _scan_hcl(source: str, *, mask: bool) -> str:
    """HCL を 1 文字ずつ読み、コメントと文字列を見分ける。改行の位置は保つ。

    - ``mask=False``: コメント（``#`` ・ ``//`` ・ ``/* */``）を落とし、文字列は残す。
    - ``mask=True``: コメントと文字列（``${ }`` の中を含む）を空白に置き換え、文字位置も保つ。
      波かっこの対応を数えるとき、文字列やコメントの中のかっこを数えないためのもの。

    heredoc は対象の .tf に無いので扱わず、出てきたら止める。
    """
    out: list[str] = []
    # 文字列の文脈のスタック。-1 は文字列の中、0 以上は文字列内の ${ } / %{ } の中の
    # 波かっこの深さ（その中では引用符で内側の文字列が開く）。
    stack: list[int] = []

    def emit(text: str, *, hidden: bool) -> None:
        out.append("".join("\n" if c == "\n" else " " for c in text) if hidden else text)

    index, size = 0, len(source)
    while index < size:
        char = source[index]
        if stack and stack[-1] == -1:
            if char == "\\" and index + 1 < size:
                step = 2
            elif source.startswith(("$${", "%%{"), index):
                step = 3
            elif source.startswith(("${", "%{"), index):
                stack.append(0)
                step = 2
            else:
                if char == '"':
                    stack.pop()
                step = 1
            emit(source[index : index + step], hidden=mask)
            index += step
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end == -1:
                raise AssertionError("unterminated block comment")
            comment = source[index : end + 2]
            emit(comment if mask else "\n" * comment.count("\n"), hidden=mask)
            index = end + 2
            continue
        if char == "#" or source.startswith("//", index):
            end = source.find("\n", index)
            end = size if end == -1 else end
            if mask:
                emit(source[index:end], hidden=True)
            index = end
            continue
        if source.startswith("<<", index):
            raise AssertionError("heredoc is not supported by _scan_hcl")
        if char == '"':
            stack.append(-1)
        elif stack and char == "{":
            stack[-1] += 1
        elif stack and char == "}":
            if stack[-1] == 0:
                stack.pop()
            else:
                stack[-1] -= 1
        # 文字列を開く引用符と ${ } の中身（閉じかっこを含む）は、文字列の一部として隠す。
        emit(char, hidden=mask and bool(stack))
        index += 1
    if stack:
        raise AssertionError("unterminated string or template")
    return "".join(out)


def _strip_hcl_comments(source: str) -> str:
    """コメントで囲んだ validation や count を「まだ書いてある」と誤認しないために使う。"""
    return _scan_hcl(source, mask=False)


def _block(source: str, header: str) -> str:
    """コメントを落とした ``source`` から、``header``（例: ``variable "x" {``）の塊を返す。"""
    matches = list(re.finditer(rf"(?m)^[ \t]*{re.escape(header)}", source))
    assert len(matches) == 1, f"{header!r} must appear exactly once, found {len(matches)}"
    masked = _scan_hcl(source, mask=True)
    assert len(masked) == len(source)
    depth = 0
    for index in range(matches[0].end() - 1, len(masked)):
        if masked[index] == "{":
            depth += 1
        elif masked[index] == "}":
            depth -= 1
            if depth == 0:
                return source[matches[0].start() : index + 1]
    raise AssertionError(f"unterminated block: {header}")


def _variable_block(source: str, name: str) -> str:
    return _block(source, f'variable "{name}" {{')


def _normalized_lines(block: str) -> list[str]:
    return [" ".join(line.split()) for line in block.splitlines() if line.strip()]


def _guard_required_iam_addresses() -> list[str]:
    guard = GUARD.read_text(encoding="utf-8")
    start = guard.index("validate_exact_runtime_iam_plan() {")
    end = guard.index("\n}\n", start)
    function = guard[start:end]
    marker = "all(. as $address | $plan | resource($address) | converges(.change))"
    assert function.count(marker) == 1
    head = function[: function.index(marker)]
    # 直前の jq 配列リテラル（1 行 1 アドレス）を丸ごと取る。アドレス内の "[0]" と混同しない。
    array = re.search(r'\[\n((?:[ \t]*"[^"\n]+",?\n)+)[ \t]*\][ \t]*\|\s*$', head)
    assert array is not None, "guard の必須 IAM リストを抽出できない"
    return re.findall(r'"([^"]+)"', array.group(1))


def test_worker_tf_is_gone() -> None:
    assert not (TF_ROOT / "worker.tf").exists()


def test_bastion_is_the_only_ec2_instance() -> None:
    declared = re.findall(r'(?m)^resource\s+"aws_instance"\s+"([^"]+)"', _all_terraform())
    assert declared == ["bastion"]


@pytest.mark.parametrize(("kind", "name"), RETIRED_RESOURCES)
def test_retired_worker_resource_is_neither_declared_nor_referenced(kind: str, name: str) -> None:
    source = _all_terraform()
    assert not re.search(rf'(?m)^resource\s+"{kind}"\s+"{name}"\s*\{{', source)
    assert not _reference(f"{kind}.{name}").search(source)


@pytest.mark.parametrize(("kind", "name"), RETIRED_DATA)
def test_retired_worker_data_source_is_neither_declared_nor_referenced(
    kind: str, name: str
) -> None:
    source = _all_terraform()
    assert not re.search(rf'(?m)^data\s+"{kind}"\s+"{name}"\s*\{{', source)
    assert not _reference(f"data.{kind}.{name}").search(source)


@pytest.mark.parametrize("name", RETIRED_OUTPUTS)
def test_retired_worker_output_is_gone(name: str) -> None:
    assert not re.search(rf'(?m)^output\s+"{name}"', _all_terraform())


@pytest.mark.parametrize("name", RETIRED_VARIABLES)
def test_retired_worker_variable_is_gone(name: str) -> None:
    source = _all_terraform()
    assert not re.search(rf'(?m)^variable\s+"{name}"', source)
    assert not _reference(f"var.{name}").search(source)


@pytest.mark.parametrize("name", REMAINING_DB_RULES)
def test_other_database_ingress_rules_survive(name: str) -> None:
    assert re.search(
        rf'(?m)^resource\s+"aws_security_group_rule"\s+"{name}"\s*\{{', _all_terraform()
    )


def test_bastion_keeps_its_ami_data_source() -> None:
    source = _all_terraform()
    assert re.search(r'(?m)^data\s+"aws_ami"\s+"al2023_arm"\s*\{', source)
    assert "data.aws_ami.al2023_arm.id" in source


def test_hcl_scanner_separates_comments_from_strings() -> None:
    source = (
        'a = "http://x#y" # tail\n'
        "/* validation {\n  condition = true\n} */\n"
        "// count = 1\n"
        'b = "${join("#", ["//", "}"])}" # c\n'
        'c = "$${literal} \\" # still string"\n'
        'variable "v" {\n  d = "}{"\n  validation {\n    e = 1\n  }\n}\nafter {}\n'
    )
    stripped = _strip_hcl_comments(source)
    assert stripped.count("\n") == source.count("\n")
    assert 'a = "http://x#y" \n' in stripped
    assert "tail" not in stripped
    assert "validation {\n  condition" not in stripped
    assert "count" not in stripped
    assert 'b = "${join("#", ["//", "}"])}" \n' in stripped
    assert 'c = "$${literal} \\" # still string"\n' in stripped
    block = _variable_block(stripped, "v")
    assert block.startswith('variable "v" {') and block.endswith("    e = 1\n  }\n}")


def test_worker_hmac_deploy_path_is_sealed_by_validation() -> None:
    # コメントを落としてから読む。validation を /* */ や # で殺した形は「無い」とみなす。
    block = _variable_block(
        _strip_hcl_comments(WORKER_DEPLOY_TF.read_text(encoding="utf-8")),
        "enable_hmac_worker_deploy",
    )
    assert re.search(r"(?m)^\s*default\s*=\s*false\s*$", block)
    validation = _block(block, "validation {")
    assert re.search(rf"(?m)^\s*condition\s*=\s*!{re.escape(SEALED_FLAG)}\s*$", validation)
    assert "retired" in validation


def test_worker_hmac_deploy_resource_only_runs_behind_the_sealed_flag() -> None:
    """validation だけでは封印にならない。配布経路の本体（local-exec で deploy_to_ec2.sh を
    実行する terraform_data）が、封じた変数に count でつながっていることまで確かめる。"""
    source = _strip_hcl_comments(WORKER_DEPLOY_TF.read_text(encoding="utf-8"))
    declared = re.findall(r'(?m)^resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', source)
    assert declared == [("terraform_data", "hmac_worker_deploy")]
    block = _block(source, 'resource "terraform_data" "hmac_worker_deploy" {')
    lines = _normalized_lines(block)
    meta = [line for line in lines if re.match(r"(count|for_each)\s*=", line)]
    assert meta == [f"count = {SEALED_FLAG} ? 1 : 0"]
    assert 'provisioner "local-exec" {' in lines
    assert "deploy_to_ec2.sh" in block
    assert source.count("provisioner ") == block.count("provisioner ") == 1


def test_worker_deploy_script_is_wired_only_in_the_sealed_file() -> None:
    wired = sorted(
        path.name
        for path in TF_ROOT.glob("*.tf")
        if "deploy_to_ec2.sh" in path.read_text(encoding="utf-8")
    )
    assert wired == [WORKER_DEPLOY_TF.name]


@pytest.mark.parametrize("name", ["hmac_worker_in_scope", "worker_enabled"])
def test_hmac_worker_scope_and_release_binding_follow_the_sealed_flag(name: str) -> None:
    lines = _normalized_lines(_strip_hcl_comments(HMAC_KEYRINGS_TF.read_text(encoding="utf-8")))
    assert [line for line in lines if line.split(" =")[0] == name] == [f"{name} = {SEALED_FLAG}"]


def test_guard_no_longer_requires_the_retired_worker_policy() -> None:
    required = _guard_required_iam_addresses()
    assert "aws_iam_role_policy.worker_app" not in required
    assert sorted(required) == sorted(REMAINING_REQUIRED_IAM)
