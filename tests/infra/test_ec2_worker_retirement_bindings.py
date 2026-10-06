"""EC2 worker 退役の途中で、便δ（HMAC）の worker 配布計画に束縛されたファイルを消させない。

2026-09-28 に EC2 worker（teamagent-dev-worker・2026-08-03 から停止中）の退役を決めた。
repo だけで片付く物（systemd の ingest ユニット・worker IAM を手で書き換えるスクリプト等）は
先に消したが、次のファイルは消せない。

- ``infra/terraform/hmac_worker_deploy.tf`` の ``local.hmac_worker_deploy_files``
- ``scripts/hmac_rollout_gate.py`` の ``prepare-cleanup`` の ``bound_paths``
- ``scripts/deploy_to_ec2.sh`` の ``verify_bound_worker_file``

の 3 か所が、同じファイルの SHA-256 を「保存済み terraform 計画」と突き合わせている。
ファイルが無いと terraform 側は ``hmac_worker_deploy_files_ready`` が偽になって precondition で止まり、
gate 側は ``saved_plan_sha256`` が ``terraform_plan_unreadable`` を投げる。どちらも計画を作る時点で
初めて落ちるので、既存のテストはファイルを消しても緑のまま通ってしまう（2026-09-28 に
requirements-worker.lock を外して確認）。このテストはその穴を塞ぐ。

退役は 2 段階で進める。

- 段階 1（2026-09-28・今回）: worker.tf を消して EC2 の資源を destroy し、配布経路は
  ``enable_hmac_worker_deploy`` の validation（常に false）で封じる。経路のファイルと
  3 か所の束縛は残すので、このテストも残す。
- 段階 2（別 PR・便δを再開する前）: worker 経路を依存の順に撤去し、HMAC の門の
  ``worker_verified`` 段を「EC2 が無いことの証明」に置き換える。そのときに 3 か所の束縛と
  このテストを一緒に消すこと。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM_DIR = ROOT / "infra" / "terraform"
WORKER_DEPLOY_TF = TERRAFORM_DIR / "hmac_worker_deploy.tf"
ROLLOUT_GATE = ROOT / "scripts" / "hmac_rollout_gate.py"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_to_ec2.sh"

# 本番の非秘密 env の素。各自の手元にだけあり git 管理外なので、存在は検査しない。
LOCAL_ONLY = {".env.production"}

# 名前をあえて固定している（意図した摩擦）。理由は 2 つ。
# 1. 3 か所の抽出（正規表現・AST）がそろって何も拾わなくなると ``{} == {} == {}`` で
#    一致判定が素通りし、存在確認も空集合で緑になる。固定しておけば抽出の故障で赤になる。
# 2. 便δの worker 経路に束縛ファイルを足す・外す変更は、退役途中の今は意図して行うべきもの。
#    3 か所をそろえた正当な変更でも、ここの更新で「退役とどう関係するか」を一度考えさせる。
# 退役の段階 2（経路の撤去と門の再設計）で、このテストごと消す（モジュール docstring 参照）。
EXPECTED_NAMES = {
    "atomic_switch",
    "base_environment",
    "base_env_renderer",
    "deploy_overrides",
    "deploy_script",
    "promotion_attester",
    "provenance_verifier",
    "release_measurer",
    "runtime_lock",
}


def _terraform_bindings() -> dict[str, str]:
    full = WORKER_DEPLOY_TF.read_text(encoding="utf-8")
    start = full.index("hmac_worker_deploy_files = {")
    source = full[start : full.index("\n  }\n", start)]
    pattern = re.compile(r'^\s*(\w+)\s*=\s*abspath\("\$\{path\.(?:module|root)\}/([^"]+)"\)', re.M)
    bindings: dict[str, str] = {}
    for name, relative in pattern.findall(source):
        # path.module と path.root はどちらも infra/terraform（ルートモジュール）。
        resolved = (TERRAFORM_DIR / relative).resolve()
        bindings[name] = resolved.relative_to(ROOT).as_posix()
    return bindings


def _gate_bindings() -> dict[str, str]:
    tree = ast.parse(ROLLOUT_GATE.read_text(encoding="utf-8"))
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "bound_paths" for t in node.targets)
            and isinstance(node.value, ast.Dict)
        ):
            continue
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            assert isinstance(key, ast.Constant)
            parts: list[str] = []
            current = value
            while isinstance(current, ast.BinOp) and isinstance(current.op, ast.Div):
                assert isinstance(current.right, ast.Constant)
                parts.append(str(current.right.value))
                current = current.left
            if isinstance(current, ast.Name) and current.id == "repository_root":
                bindings[str(key.value)] = "/".join(reversed(parts))
    return bindings


def _deploy_script_bindings() -> dict[str, str]:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8").replace("\\\n", " ")
    pattern = re.compile(r'verify_bound_worker_file\s+(\w+)\s+"\$ROOT/([^"]+)"')
    return dict(pattern.findall(source))


def test_three_worker_binding_sites_agree() -> None:
    terraform = _terraform_bindings()
    gate = _gate_bindings()
    deploy = _deploy_script_bindings()

    assert set(terraform) == EXPECTED_NAMES
    assert terraform == gate == deploy


def test_bound_worker_files_are_still_in_the_repository() -> None:
    missing = sorted(
        path
        for path in _terraform_bindings().values()
        if path not in LOCAL_ONLY and not (ROOT / path).is_file()
    )
    assert missing == [], (
        "便δの worker 配布計画に束縛されたファイルが消えている。EC2 worker の退役は "
        "hmac_worker_deploy.tf / hmac_rollout_gate.py / deploy_to_ec2.sh の束縛と一緒に行うこと: "
        f"{missing}"
    )
