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
    desired-var                     activation_freeze_enabled（policy の存廃）に注入すべき値
    desired-attachments-var         activation_freeze_attachments_enabled（窓の開閉）に注入すべき値
    assert-plan-preserves-freeze    plan が freeze enforcement を壊していないことを要求
    assert-attachment-window        live の list-entities-for-policy が宣言した窓と exact 一致
    assert-attachment-expectation   宣言の expected_attachments が terraform state と exact 一致
    attachment-commands             窓の開閉に必要な aws
    CLI コマンドを **表示するだけ**（実行しない）

AWS 側 enforcement の第 3 の状態（2026-08-28）:
    state（pending_v2 / active / released）は **repo 側ゲート** の強度だけを決める。
    AWS 側 attachment の存廃は aws_enforcement.mode が決める。両者は直交する:

        state=active + mode=declaration_only … CI ゲートは効いたまま、AWS attachment は 0
        state=active + mode=attached         … 窓が開き、10 principal に deny が効く

    2026-08-26 に 10 attachment を CLI で detach した結果、宣言 active / AWS 0 という
    単一 bool では表現できない状態が生まれた（真になったのがこの分離の実測根拠）。
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
FREEZE_ATTACHMENTS_VAR = "activation_freeze_attachments_enabled"

# AWS 側 enforcement の mode。state とは直交する軸で、attachment の存廃だけを決める。
#   declaration_only … policy object は残すが principal への attachment は 0（窓は閉）
#   attached         … expected_attachments へ attach 済み（窓が開・deny が実際に効く）
AWS_ENFORCEMENT_MODES = ("declaration_only", "attached")
ATTACHED_MODE = "attached"
DECLARATION_ONLY_MODE = "declaration_only"

FREEZE_POLICY_PREFIX = "aws_iam_policy.activation_freeze"
FREEZE_ATTACHMENT_PREFIXES = (
    "aws_iam_user_policy_attachment.activation_freeze_aiia_dev",
    "aws_iam_role_policy_attachment.activation_freeze",
)


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
    _validate_aws_enforcement(doc, state)
    return doc


def _validate_aws_enforcement(doc: dict[str, Any], state: str) -> dict[str, Any]:
    """AWS 側 enforcement 宣言（第 3 の状態）の不変条件。

    宣言が無い / 壊れている場合は fail-closed。ここを緩めると「宣言 active なのに
    AWS は 0 principal」という 2026-08-26 の乖離が、また誰にも見えないまま進む。
    """
    aws = doc.get("aws_enforcement")
    if not isinstance(aws, dict):
        raise FreezeError(
            "aws_enforcement がありません（state と AWS attachment は別の軸です。"
            "attachment を 0 にしたまま state=active を維持するには宣言が要ります）"
        )
    mode = aws.get("mode")
    if mode not in AWS_ENFORCEMENT_MODES:
        raise FreezeError(f"未知の aws_enforcement.mode: {mode!r}")
    for field in ("recorded_at", "reason", "gate"):
        if not aws.get(field):
            raise FreezeError(f"aws_enforcement に {field} がありません（無記名の切替は禁止）")
    expected = aws.get("expected_attachments")
    if not isinstance(expected, dict):
        raise FreezeError("aws_enforcement.expected_attachments がありません")
    if not str(expected.get("policy_arn", "")).startswith("arn:aws:iam::"):
        raise FreezeError("expected_attachments.policy_arn が IAM policy ARN ではありません")
    for field in ("derived_from", "measured_at"):
        if not expected.get(field):
            raise FreezeError(
                f"expected_attachments に {field} がありません"
                "（attach 集合は state 実測から導出した記録が必須）"
            )
    roles = expected.get("roles")
    users = expected.get("users")
    for name, value in (("roles", roles), ("users", users)):
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise FreezeError(f"expected_attachments.{name} が文字列配列ではありません")
        if len(set(value)) != len(value):
            raise FreezeError(f"expected_attachments.{name} に重複があります")
    if not roles and not users:
        raise FreezeError(
            "expected_attachments が空です（窓を開けても 0 件しか attach できません）"
        )
    window = aws.get("window")
    if mode == ATTACHED_MODE:
        if state not in ENFORCING_STATES:
            raise FreezeError(
                f"mode=attached なのに freeze state={state} です"
                "（repo 側ゲートが効いていない状態で AWS deny だけ張るのは矛盾）"
            )
        if not isinstance(window, dict):
            raise FreezeError("mode=attached なのに window がありません（窓は無記名で開けない）")
        for field in ("opened_at", "purpose", "gate"):
            if not window.get(field):
                raise FreezeError(f"window に {field} がありません")
        if window.get("closed_at"):
            raise FreezeError(
                "window に closed_at が入っているのに mode=attached のままです（矛盾）"
            )
    else:
        if window is not None:
            raise FreezeError(
                "mode=declaration_only なのに window が残っています"
                "（閉じた窓は window_history へ移すこと）"
            )
    history = aws.get("window_history", [])
    if not isinstance(history, list):
        raise FreezeError("aws_enforcement.window_history が配列ではありません")
    for index, entry in enumerate(history):
        if not isinstance(entry, dict):
            raise FreezeError(f"window_history[{index}] がオブジェクトではありません")
        for field in ("opened_at", "closed_at", "purpose", "gate"):
            if not entry.get(field):
                raise FreezeError(f"window_history[{index}] に {field} がありません")
    return aws


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


def desired_freeze_attachments_var(freeze_path: Path) -> str:
    """`activation_freeze_attachments_enabled` に注入すべき値を宣言から決める。

    **窓が開いているときだけ "true"**。policy 自体（desired_freeze_var）とは別の軸で、
    ここを true 固定にすると detach 済みの 10 attachment が次の full plan で
    create として現れ、adopt validator が plan ごと拒否する（2026-08-27 実測）。
    逆に policy まで false にすると 11 リソースが destroy 候補になる。
    """
    doc = load_freeze(freeze_path)
    if doc["generation_publisher_freeze"]["state"] != "active":
        return "false"
    return "true" if doc["aws_enforcement"]["mode"] == ATTACHED_MODE else "false"


def aws_enforcement(freeze_path: Path) -> dict[str, Any]:
    return load_freeze(freeze_path)["aws_enforcement"]


def _matches(address: str, prefixes: tuple[str, ...]) -> bool:
    return any(address == prefix or address.startswith(f"{prefix}[") for prefix in prefixes)


def _is_freeze_resource(address: str) -> bool:
    return _matches(address, FREEZE_RESOURCE_PREFIXES)


def _is_freeze_attachment(address: str) -> bool:
    return _matches(address, FREEZE_ATTACHMENT_PREFIXES)


def _is_freeze_policy(address: str) -> bool:
    return _matches(address, (FREEZE_POLICY_PREFIX,))


def assert_plan_preserves_freeze(freeze_path: Path, plan_path: Path) -> str:
    """plan が Freeze v2 の enforcement を壊していないことを要求する。

    2 軸で判定する（2026-08-28 に第 3 の状態を導入）:

    policy 軸（state=active の間は常に）:
      - `aws_iam_policy.activation_freeze` の delete（destroy / replace）は FATAL
      - plan の scope に freeze リソースがあるのに activation_freeze_enabled が
        true でなければ FATAL

    窓（attachment）軸（aws_enforcement.mode）:
      - mode=attached      … attachment の delete は FATAL（開いている窓の巻き戻し）
      - mode=declaration_only … attachment の **create** は FATAL。
        窓を閉じたまま attach すると、detach 済みの deploy principal が再び deny され、
        adopt / rebind が沈黙のうちに詰む（旧 landmine_next_apply の経路そのもの）。
        逆に delete は宣言どおりの収束なので許可する。
      - attachment が plan の scope にあるなら、activation_freeze_attachments_enabled は
        宣言から導いた値と exact 一致でなければ FATAL
    """
    doc = load_freeze(freeze_path)
    state = doc["generation_publisher_freeze"]["state"]
    mode = doc["aws_enforcement"]["mode"]
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    changes = plan.get("resource_changes") or []

    def _addresses(predicate: Any, action: str | None = None) -> list[str]:
        return sorted(
            change["address"]
            for change in changes
            if predicate(change.get("address", ""))
            and (action is None or action in (change.get("change", {}).get("actions") or []))
        )

    policy_deleted = _addresses(_is_freeze_policy, "delete")
    attachment_deleted = _addresses(_is_freeze_attachment, "delete")
    attachment_created = _addresses(_is_freeze_attachment, "create")
    if state != "active":
        destroyed = len(policy_deleted) + len(attachment_deleted)
        return f"freeze state={state} のため plan 検査は情報提供のみ（destroy {destroyed} 件）"
    if policy_deleted:
        raise FreezeError(
            "FREEZE ROLLBACK 違反: Freeze v2 が ACTIVE なのに plan が enforcement を"
            f"削除しようとしています。\n  対象: {policy_deleted}\n"
            "  原因の典型は var.activation_freeze_enabled の未注入（既定 false）です。"
        )
    if mode == ATTACHED_MODE and attachment_deleted:
        raise FreezeError(
            "FREEZE ROLLBACK 違反: 窓が開いている（mode=attached）のに plan が attachment を"
            f"削除しようとしています。\n  対象: {attachment_deleted}\n"
            "  窓を閉じるなら先に aws_enforcement.mode を declaration_only へ倒すこと"
            "（宣言が diff に現れることで human gate が効きます）。"
        )
    if mode == DECLARATION_ONLY_MODE and attachment_created:
        raise FreezeError(
            "FREEZE WINDOW 違反: 窓が閉じている（mode=declaration_only）のに plan が"
            f"attachment を作成しようとしています。\n  対象: {attachment_created}\n"
            "  これは detach 済みの deploy principal を無宣言で再 deny する経路です"
            "（2026-08-26 の detach を無言で巻き戻す）。attach したいなら"
            " aws_enforcement.mode=attached と window を宣言してください。"
        )
    # 変数チェックは **plan の scope に freeze リソースが含まれるときだけ** 適用する。
    # 対象外へ -target した plan や、guard の合成 plan fixture は freeze の存廃に
    # 関与しないため要求しない（誤爆すると無関係な検証がすべて止まる）。
    # なお var が false / 未注入の full plan では policy が delete として現れるので、
    # 上の destroy 検査が先に捕捉する（二重化）。
    in_scope = [
        change["address"] for change in changes if _is_freeze_resource(change.get("address", ""))
    ]
    if not in_scope:
        return "plan の scope に freeze リソースが無いため変数要求は適用しない（destroy 0）"
    variables = plan.get("variables") or {}
    if _plan_var(variables, FREEZE_ENABLED_VAR) != "true":
        raise FreezeError(
            f"FREEZE BINDING 違反: plan の scope に freeze リソース {len(in_scope)} 件が"
            f"含まれるのに {FREEZE_ENABLED_VAR} が true ではありません"
            f"（実際: {_raw_plan_var(variables, FREEZE_ENABLED_VAR)!r}）。"
            "Freeze ACTIVE 中は全 plan 経路で true を注入すること。"
        )
    attachments_in_scope = [
        change["address"] for change in changes if _is_freeze_attachment(change.get("address", ""))
    ]
    if attachments_in_scope:
        desired = desired_freeze_attachments_var(freeze_path)
        actual = _plan_var(variables, FREEZE_ATTACHMENTS_VAR)
        if actual != desired:
            raise FreezeError(
                "FREEZE WINDOW BINDING 違反: plan の scope に freeze attachment "
                f"{len(attachments_in_scope)} 件が含まれるのに {FREEZE_ATTACHMENTS_VAR} が "
                f"{desired} ではありません（実際: "
                f"{_raw_plan_var(variables, FREEZE_ATTACHMENTS_VAR)!r} / 宣言 mode={mode}）。"
            )
    return (
        "plan は Freeze v2 の enforcement を保持している"
        f"（policy destroy 0 / mode={mode} / attachment create "
        f"{len(attachment_created)}・delete {len(attachment_deleted)}）"
    )


def _raw_plan_var(variables: dict[str, Any], name: str) -> Any:
    raw = variables.get(name, {})
    return raw.get("value") if isinstance(raw, dict) else raw


def _plan_var(variables: dict[str, Any], name: str) -> str | None:
    """plan の variables から bool 値を "true" / "false" へ正規化する。

    terraform は JSON plan で bool を true/false で出すが、-var= 経由の値が
    文字列で現れる版もあるため両方を受ける。未注入は None（＝一致しない）。
    """
    value = _raw_plan_var(variables, name)
    if value in (True, "true"):
        return "true"
    if value in (False, "false"):
        return "false"
    return None


def assert_attachment_window(freeze_path: Path, live_path: Path) -> str:
    """live の list-entities-for-policy が宣言した窓と exact 一致することを要求する。

    窓を開けたのに 1 件足りない / 余分に付いている状態で先へ進むと、次の full plan に
    create / destroy が残り adopt が永久に緑にならない（STOP 条件 S6）。ここは
    「10 件ちょうど」を機械で言い切るための検査で、AWS へは触らない
    （呼び出し側が read-only で採取した JSON を渡す）。
    """
    doc = load_freeze(freeze_path)
    aws = doc["aws_enforcement"]
    mode = aws["mode"]
    expected = aws["expected_attachments"]
    live = json.loads(live_path.read_text(encoding="utf-8"))
    actual_roles = {entry["RoleName"] for entry in live.get("PolicyRoles") or []}
    actual_users = {entry["UserName"] for entry in live.get("PolicyUsers") or []}
    actual_groups = {entry["GroupName"] for entry in live.get("PolicyGroups") or []}
    if mode == ATTACHED_MODE:
        want_roles, want_users = set(expected["roles"]), set(expected["users"])
    else:
        want_roles, want_users = set(), set()
    problems = []
    for label, want, actual in (
        ("role", want_roles, actual_roles),
        ("user", want_users, actual_users),
        ("group", set(), actual_groups),
    ):
        missing = sorted(want - actual)
        extra = sorted(actual - want)
        if missing:
            problems.append(f"{label} 不足 {missing}")
        if extra:
            problems.append(f"{label} 余剰 {extra}")
    if problems:
        raise FreezeError(
            f"FREEZE WINDOW 不一致: 宣言 mode={mode} と live の attachment が違います。\n  "
            + "\n  ".join(problems)
        )
    total = len(actual_roles) + len(actual_users) + len(actual_groups)
    return f"attachment は宣言どおり（mode={mode} / live {total} principal）"


def assert_attachment_expectation(freeze_path: Path, state_path: Path) -> str:
    """宣言の expected_attachments が terraform state の実体と exact 一致することを要求する。

    attach 集合を tf の locals（＝これから評価される式）ではなく **state に実在する
    index_key** から導出するための検査。state を JSON として読むだけで terraform は
    起動しない（tflock を取らない）。
    """
    aws = aws_enforcement(freeze_path)
    expected = aws["expected_attachments"]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    roles: set[str] = set()
    users: set[str] = set()
    for resource in state.get("resources") or []:
        address = f"{resource.get('type')}.{resource.get('name')}"
        if address == "aws_iam_role_policy_attachment.activation_freeze":
            for instance in resource.get("instances") or []:
                roles.add(instance["attributes"]["role"])
        elif address == "aws_iam_user_policy_attachment.activation_freeze_aiia_dev":
            for instance in resource.get("instances") or []:
                users.add(instance["attributes"]["user"])
    if roles != set(expected["roles"]) or users != set(expected["users"]):
        raise FreezeError(
            "EXPECTED ATTACHMENTS 不一致: 宣言と terraform state が違います。\n"
            f"  state role : {sorted(roles)}\n  宣言 role  : {sorted(expected['roles'])}\n"
            f"  state user : {sorted(users)}\n  宣言 user  : {sorted(expected['users'])}"
        )
    serial = state.get("serial")
    return (
        f"expected_attachments は state と一致（role {len(roles)} + user {len(users)}"
        f" / state serial {serial}）"
    )


def attachment_commands(freeze_path: Path, action: str) -> str:
    """窓の開閉に必要な aws CLI コマンドを **文字列として** 返す（実行しない）。

    実行をこのスクリプトに持たせないのは意図的。attach / detach は production の
    IAM 変更で、human gate（④⑤）の対象だからである。ここは「取り違えのない
    コマンド列を宣言から機械生成する」ところまでを担う。
    """
    if action not in ("open", "close"):
        raise FreezeError(f"未知の action: {action!r}")
    aws = aws_enforcement(freeze_path)
    expected = aws["expected_attachments"]
    arn = expected["policy_arn"]
    verb = "attach" if action == "open" else "detach"
    lines = [
        f"# action={action} / 宣言 mode={aws['mode']} / policy={arn}",
        f"# 実行前に aws_enforcement.mode を "
        f"{'attached' if action == 'open' else 'declaration_only'} へ倒して commit すること",
    ]
    for role in sorted(expected["roles"]):
        lines.append(f"aws iam {verb}-role-policy --role-name {role} --policy-arn {arn}")
    for user in sorted(expected["users"]):
        lines.append(f"aws iam {verb}-user-policy --user-name {user} --policy-arn {arn}")
    lines.append(
        "aws iam list-entities-for-policy --policy-arn "
        f"{arn} --output json > /tmp/freeze-entities.json"
    )
    lines.append(
        "python3 infra/deploy/activation_freeze_check.py "
        "assert-attachment-window --live /tmp/freeze-entities.json"
    )
    return "\n".join(lines)


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

    # self-reference ガード: execution line 自身から実行してはならない。
    # #315 の patch は execution line にも allowlist の **コピー** を持ち込むため、
    # execution worktree から実行すると stale なコピーで判定してしまう
    # （2026-08-24 実測。dev 側の 1 本だけが authoritative）。
    current = _git(repo, "rev-parse", "HEAD").strip()
    if current == head:
        raise FreezeError(
            "SELF-REFERENCE 違反: execution line 自身から allowlist 検証を実行しています。\n"
            f"  repo HEAD == {ref}（{head[:12]}）\n"
            "  execution line 上の allowlist は #315 の patch が持ち込んだ stale な"
            " inert コピーです。dev 側の作業ツリーから実行してください。"
        )
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
    sub.add_parser("desired-attachments-var")

    preserve = sub.add_parser("assert-plan-preserves-freeze")
    preserve.add_argument("--plan", required=True, type=Path)

    window = sub.add_parser("assert-attachment-window")
    window.add_argument("--live", required=True, type=Path)

    expectation = sub.add_parser("assert-attachment-expectation")
    expectation.add_argument("--state", required=True, type=Path)

    commands = sub.add_parser("attachment-commands")
    commands.add_argument("--action", required=True, choices=("open", "close"))

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
            aws = freeze["aws_enforcement"]
            expected = aws["expected_attachments"]
            window = "開（" + str(aws["window"]["opened_at"]) + "）" if aws["window"] else "閉"
            print(f"aws enforcement mode        : {aws['mode']}（窓 {window}）")
            print(
                "  expected attachments      : "
                f"role {len(expected['roles'])} + user {len(expected['users'])}"
            )
            print(f"  attachments desired var   : {desired_freeze_attachments_var(args.freeze)}")
            prod = freeze["production_deployment_freeze"]["state"]
            print(f"production deployment freeze: {prod}")
            print(f"dev merge freeze            : {freeze['dev_merge_freeze']['state']}")
            print(f"hard boundary               : {freeze['dev_merge_freeze']['hard_boundary']}")
        elif args.command == "desired-var":
            print(desired_freeze_var(args.freeze))
        elif args.command == "desired-attachments-var":
            print(desired_freeze_attachments_var(args.freeze))
        elif args.command == "assert-plan-preserves-freeze":
            print(assert_plan_preserves_freeze(args.freeze, args.plan))
        elif args.command == "assert-attachment-window":
            print(assert_attachment_window(args.freeze, args.live))
        elif args.command == "assert-attachment-expectation":
            print(assert_attachment_expectation(args.freeze, args.state))
        elif args.command == "attachment-commands":
            print(attachment_commands(args.freeze, args.action))
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
