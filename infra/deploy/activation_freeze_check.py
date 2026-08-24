#!/usr/bin/env python3
"""PR2-A0.x activation freeze の機械強制（fail-closed）。

背景（2026-08-24 ユーザー裁定）:
    generation publisher freeze は口頭合意だけを hard safety control にしていたため
    2 度破られた（2026-08-20 / 08-21 の 2 波・S3 6 objects + CodeBuild UpdateProject ×14）。
    dev merge freeze も複数回破られている。よって

      - freeze 対象の変更は **CI で落とす**（宣言なしに frozen surface を触れない）
      - activation の安全性は dev tip の不変性ではなく
        **activation-execution-base + approved commit allowlist + fast-forward only**
        に置く

    本 checker は判定だけを持ち、AWS へは一切アクセスしない（境界の記録は human gate）。
    「最後に変更された時刻」を freeze 境界にしてはならない。境界は
    「変更できない状態を確認した時刻」であり、v2 の started_at は人間が記録する。

サブコマンド:
    status                          freeze 宣言の要約を出す
    assert-frozen-surface           base..head の diff が frozen surface を触っていないことを要求
    assert-execution-line           execution line の commit 列が allowlist と exact 一致し、
                                    force push / 履歴改変が無いことを要求
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

FREEZE_FILENAME = "activation_freeze.json"
ALLOWLIST_FILENAME = "activation_execution_allowlist.json"
GENERATION_MANIFEST = "infra/deploy/buildspec_generation_inputs.json"

SCHEMA_VERSION = 1
FREEZE_STATES = ("pending_v2", "active", "released")

# frozen surface の変更を CI で落とす state。released 以外は落とす（fail-closed。
# pending_v2 = v1 が失効し v2 未確定という最も危険な期間なので、当然ここでも落とす）。
ENFORCING_STATES = ("pending_v2", "active")

# Freeze v2 の enforcement を構成する Terraform リソース。ACTIVE な間はこの
# いずれも destroy / replace されてはならない（destroy = freeze の巻き戻し）。
# var.activation_freeze_enabled が false のまま plan すると全て destroy 候補になる。
FREEZE_RESOURCE_PREFIXES = (
    "aws_iam_policy.activation_freeze",
    "aws_iam_user_policy_attachment.activation_freeze_aiia_dev",
    "aws_iam_role_policy_attachment.activation_freeze",
)
FREEZE_ENABLED_VAR = "activation_freeze_enabled"


class FreezeError(Exception):
    """freeze の不変条件が満たされない。呼び出し側は必ず fail-closed で扱う。"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise FreezeError(f"git {' '.join(args)} が失敗しました: {result.stderr.strip()}")
    return result.stdout


def load_freeze(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise FreezeError("freeze 宣言がオブジェクトではありません")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise FreezeError(f"未対応の schema_version: {doc.get('schema_version')!r}")
    publisher = doc.get("generation_publisher_freeze")
    if not isinstance(publisher, dict):
        raise FreezeError("generation_publisher_freeze がありません")
    state = publisher.get("state")
    if state not in FREEZE_STATES:
        raise FreezeError(f"未知の freeze state: {state!r}")
    v1 = publisher.get("v1")
    if not isinstance(v1, dict) or v1.get("status") != "voided":
        raise FreezeError(
            "v1 は失効済みでなければなりません（2 波の違反実測により 2026-08-24 に失効）"
        )
    if not v1.get("violations"):
        raise FreezeError("v1 の違反実測が記録されていません（失効の根拠が消えています）")
    v2 = publisher.get("v2")
    if not isinstance(v2, dict) or "started_at" not in v2:
        raise FreezeError("v2 宣言がありません")
    if state == "active" and not v2.get("started_at"):
        raise FreezeError(
            "state=active なのに v2.started_at が空です。"
            "境界は publisher 停止を確認した時点で人間が記録すること"
        )
    if state == "pending_v2" and v2.get("started_at"):
        raise FreezeError("state=pending_v2 なのに v2.started_at が入っています（矛盾）")
    unlock = doc.get("unlock")
    if not isinstance(unlock, dict) or "active" not in unlock:
        raise FreezeError("unlock 宣言がありません")
    if unlock["active"] and not unlock.get("scope_paths"):
        raise FreezeError("unlock が active なのに scope_paths が空です")
    if unlock["active"] and not unlock.get("reason"):
        raise FreezeError("unlock が active なのに reason がありません")
    if unlock["active"] and not unlock.get("gate"):
        raise FreezeError("unlock が active なのに gate（human 承認の出所）がありません")
    if not unlock["active"] and (unlock.get("scope_paths") or unlock.get("reason")):
        raise FreezeError("unlock が非 active なのに scope_paths / reason が残っています")
    return doc


def frozen_paths(repo: Path, freeze: dict[str, Any], ref: str) -> set[str]:
    """frozen surface の path 集合。

    generation inputs は manifest を単一の真実源として **その ref の内容から** 読む
    （手書きリストの陳腐化で守れなくなる事故を防ぐ）。
    """
    surface = freeze.get("frozen_change_surface")
    if not isinstance(surface, dict):
        raise FreezeError("frozen_change_surface がありません")
    source = surface.get("generation_inputs_source")
    if source != f"{GENERATION_MANIFEST}#inputs":
        raise FreezeError(f"generation_inputs_source が想定外です: {source!r}")
    manifest = json.loads(_git(repo, "show", f"{ref}:{GENERATION_MANIFEST}"))
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        raise FreezeError("manifest の inputs を読めません")
    extra = surface.get("additional_publisher_paths")
    if not isinstance(extra, list):
        raise FreezeError("additional_publisher_paths が不正です")
    return set(inputs) | set(extra)


def assert_frozen_surface(repo: Path, freeze_path: Path, base: str, head: str) -> str:
    freeze = load_freeze(freeze_path)
    state = freeze["generation_publisher_freeze"]["state"]
    changed = set(_git(repo, "diff", "--name-only", f"{base}..{head}").split())
    surface = frozen_paths(repo, freeze, base)
    touched = sorted(changed & surface)
    if not touched:
        return f"frozen surface 無変更（freeze state={state} / 監視対象 {len(surface)} path）"
    if state not in ENFORCING_STATES:
        return f"frozen surface を {len(touched)} path 変更（freeze state={state} のため許可）"
    unlock = freeze["unlock"]
    if not unlock["active"]:
        raise FreezeError(
            "FROZEN SURFACE 違反: generation freeze 中に frozen surface を変更しています。\n"
            f"  freeze state : {state}\n"
            f"  変更 path    : {touched}\n"
            "  この変更は新しい generation の publish を強制し、freeze を破ります。\n"
            "  意図的な変更なら、同じ PR 内で activation_freeze.json の unlock を\n"
            "  active にし、scope_paths / reason / gate を明記してください"
            "（宣言が diff に現れることで human gate が効きます）。"
        )
    scope = set(unlock["scope_paths"])
    outside = sorted(set(touched) - scope)
    if outside:
        raise FreezeError(
            "FROZEN SURFACE 違反: unlock の scope_paths 外の frozen path を変更しています。\n"
            f"  scope_paths  : {sorted(scope)}\n"
            f"  scope 外変更 : {outside}"
        )
    unused = sorted(scope - set(touched))
    if unused:
        raise FreezeError(
            "unlock の scope_paths に、実際には変更していない path が含まれています"
            f"（過剰な unlock は禁止）: {unused}"
        )
    return (
        f"unlock 宣言済みの frozen surface 変更 {len(touched)} path を許可"
        f"（gate: {unlock['gate']}）"
    )


def desired_freeze_var(freeze_path: Path) -> str:
    """`activation_freeze_enabled` に注入すべき値を宣言から決める。

    state=active なら "true"。それ以外は "false"。宣言が壊れていれば load_freeze が
    例外を投げ、呼び出し側（guard）は fail-closed で停止する。
    """
    state = load_freeze(freeze_path)["generation_publisher_freeze"]["state"]
    return "true" if state == "active" else "false"


def _is_freeze_resource(address: str) -> bool:
    return any(
        address == prefix or address.startswith(f"{prefix}[") for prefix in FREEZE_RESOURCE_PREFIXES
    )


def assert_plan_preserves_freeze(freeze_path: Path, plan_path: Path) -> str:
    """plan が Freeze v2 の enforcement を壊していないことを要求する。

    Freeze ACTIVE 中に次のいずれかがあれば FATAL:
      - freeze リソースの delete（destroy / replace を含む）
      - plan の variables に activation_freeze_enabled=true が入っていない
        （未注入 / false 注入はどちらも 11 リソースを destroy 候補にする）
    """
    state = load_freeze(freeze_path)["generation_publisher_freeze"]["state"]
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    changes = plan.get("resource_changes") or []
    destroyed = sorted(
        change["address"]
        for change in changes
        if _is_freeze_resource(change.get("address", ""))
        and "delete" in (change.get("change", {}).get("actions") or [])
    )
    if state != "active":
        return f"freeze state={state} のため plan 検査は情報提供のみ（destroy {len(destroyed)} 件）"
    if destroyed:
        raise FreezeError(
            "FREEZE ROLLBACK 違反: Freeze v2 が ACTIVE なのに plan が enforcement を"
            f"削除しようとしています。\n  対象: {destroyed}\n"
            "  原因の典型は var.activation_freeze_enabled の未注入（既定 false）です。"
        )
    # 変数チェックは **plan の scope に freeze リソースが含まれるときだけ** 適用する。
    # 対象外へ -target した plan や、guard の合成 plan fixture は freeze の存廃に
    # 関与しないため要求しない（誤爆すると無関係な検証がすべて止まる）。
    # なお var が false / 未注入の full plan では 11 リソースが delete として現れるので、
    # 上の destroy 検査が先に捕捉する（二重化）。
    in_scope = [
        change["address"] for change in changes if _is_freeze_resource(change.get("address", ""))
    ]
    if not in_scope:
        return "plan の scope に freeze リソースが無いため変数要求は適用しない（destroy 0）"
    variables = plan.get("variables") or {}
    raw = variables.get(FREEZE_ENABLED_VAR, {})
    value = raw.get("value") if isinstance(raw, dict) else raw
    if value not in (True, "true"):
        raise FreezeError(
            f"FREEZE BINDING 違反: plan の scope に freeze リソース {len(in_scope)} 件が"
            f"含まれるのに {FREEZE_ENABLED_VAR} が true ではありません（実際: {value!r}）。"
            "Freeze ACTIVE 中は全 plan 経路で true を注入すること。"
        )
    return "plan は Freeze v2 の enforcement を保持している（destroy 0 / 変数 true）"


def load_allowlist(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise FreezeError("allowlist がオブジェクトではありません")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise FreezeError(f"未対応の allowlist schema_version: {doc.get('schema_version')!r}")
    base = doc.get("execution_base")
    if not isinstance(base, dict) or not _is_sha(base.get("sha")):
        raise FreezeError("execution_base.sha が完全な 40 桁 SHA ではありません")
    approved = doc.get("approved_commits")
    if not isinstance(approved, list):
        raise FreezeError("approved_commits が配列ではありません")
    seen: set[str] = set()
    for index, entry in enumerate(approved):
        if not isinstance(entry, dict):
            raise FreezeError(f"approved_commits[{index}] がオブジェクトではありません")
        for field in ("sha", "subject", "gate"):
            if not entry.get(field):
                raise FreezeError(f"approved_commits[{index}] に {field} がありません")
        if not _is_sha(entry["sha"]):
            raise FreezeError(
                f"approved_commits[{index}].sha が完全な 40 桁 SHA ではありません"
                "（短縮 SHA は衝突と取り違えを許すため禁止）"
            )
        if entry["sha"] in seen:
            raise FreezeError(f"approved_commits に重複 SHA: {entry['sha']}")
        seen.add(entry["sha"])
    if not _is_sha(doc.get("expected_head")):
        raise FreezeError("expected_head が完全な 40 桁 SHA ではありません")
    expected_tail = approved[-1]["sha"] if approved else base["sha"]
    if doc["expected_head"] != expected_tail:
        raise FreezeError(
            "expected_head が approved_commits の末尾と一致しません"
            f"（{doc['expected_head'][:12]} != {expected_tail[:12]}）"
        )
    return doc


def _is_sha(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 40:
        return False
    return all(c in "0123456789abcdef" for c in value)


def assert_execution_line(repo: Path, allowlist_path: Path, ref: str | None = None) -> str:
    doc = load_allowlist(allowlist_path)
    ref = ref or doc["execution_ref"]
    head = _git(repo, "rev-parse", ref).strip()
    base = doc["execution_base"]["sha"]
    expected_head = doc["expected_head"]

    if not _ancestor(repo, base, head):
        raise FreezeError(
            f"EXECUTION LINE 違反: execution_base が {ref} の祖先ではありません"
            "（履歴が作り直されています）"
        )
    # force push / 履歴改変の検出。allowlist の expected_head が現 HEAD の
    # 祖先でも一致でもないなら、記録済みの列とは別の履歴になっている。
    if head != expected_head and not _ancestor(repo, expected_head, head):
        raise FreezeError(
            "EXECUTION LINE 違反: expected_head が現 HEAD の祖先でも一致でもありません"
            f"（force push / rebase の疑い）\n  expected_head: {expected_head[:12]}\n"
            f"  actual head  : {head[:12]}"
        )
    actual = [
        line.split("\t", 1)
        for line in _git(repo, "log", "--format=%H%x09%s", "--reverse", f"{base}..{head}")
        .strip()
        .splitlines()
        if line
    ]
    approved = doc["approved_commits"]
    if len(actual) != len(approved):
        raise FreezeError(
            "EXECUTION LINE 違反: commit 数が allowlist と一致しません\n"
            f"  実際 {len(actual)} 件: {[c[0][:8] for c in actual]}\n"
            f"  許可 {len(approved)} 件: {[e['sha'][:8] for e in approved]}"
        )
    for position, ((sha, subject), entry) in enumerate(zip(actual, approved, strict=True)):
        if sha != entry["sha"]:
            raise FreezeError(
                f"EXECUTION LINE 違反: {position} 番目の commit が allowlist 外です\n"
                f"  実際: {sha[:12]} {subject}\n  許可: {entry['sha'][:12]} {entry['subject']}"
            )
        if subject != entry["subject"]:
            raise FreezeError(
                f"EXECUTION LINE 違反: {sha[:12]} の subject が allowlist と違います"
                "（履歴書き換えの疑い）"
            )
    if head != expected_head:
        raise FreezeError(
            "EXECUTION LINE 違反: HEAD が expected_head と一致しません"
            f"（{head[:12]} != {expected_head[:12]}）"
        )
    return (
        f"execution line 検証済み: {ref} = {head[:12]}"
        f"（base {base[:12]} + 承認済み {len(approved)} commit・force push なし）"
    )


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=here.parents[1])
    parser.add_argument("--freeze", type=Path, default=here / FREEZE_FILENAME)
    parser.add_argument("--allowlist", type=Path, default=here / ALLOWLIST_FILENAME)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status")

    surface = sub.add_parser("assert-frozen-surface")
    surface.add_argument("--base", required=True)
    surface.add_argument("--head", default="HEAD")

    line = sub.add_parser("assert-execution-line")
    line.add_argument("--ref", default=None)

    sub.add_parser("desired-var")

    preserve = sub.add_parser("assert-plan-preserves-freeze")
    preserve.add_argument("--plan", required=True, type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            freeze = load_freeze(args.freeze)
            publisher = freeze["generation_publisher_freeze"]
            print(f"generation publisher freeze : {publisher['state']}")
            v1 = publisher["v1"]
            print(f"  v1 {v1['started_at']} → {v1['status']}（違反 {len(v1['violations'])} 件）")
            print(f"  v2 started_at             : {publisher['v2']['started_at'] or '(未確定)'}")
            unlock_state = "active" if freeze["unlock"]["active"] else "なし"
            print(f"unlock                      : {unlock_state}")
            prod = freeze["production_deployment_freeze"]["state"]
            print(f"production deployment freeze: {prod}")
            print(f"dev merge freeze            : {freeze['dev_merge_freeze']['state']}")
            print(f"hard boundary               : {freeze['dev_merge_freeze']['hard_boundary']}")
        elif args.command == "desired-var":
            print(desired_freeze_var(args.freeze))
        elif args.command == "assert-plan-preserves-freeze":
            print(assert_plan_preserves_freeze(args.freeze, args.plan))
        elif args.command == "assert-frozen-surface":
            print(assert_frozen_surface(args.repo, args.freeze, args.base, args.head))
        else:
            print(assert_execution_line(args.repo, args.allowlist, args.ref))
    except (FreezeError, json.JSONDecodeError, OSError, KeyError) as error:
        print(f"activation freeze check failed: {error}", file=sys.stderr)
        return 1
    return 0


def _ancestor(repo: Path, maybe_ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", maybe_ancestor, descendant],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


if __name__ == "__main__":
    raise SystemExit(main())
