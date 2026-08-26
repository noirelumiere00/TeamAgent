"""Bootstrap Pin の semantic no-op 契約（差分 whitelist = VersionId のみ）。

「AWSCURRENT → 同じ AWSCURRENT の VersionId 固定」以外の差分が
**1 byte でも** あれば RED、を機械的に固定する。
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "bootstrap_pin_semantic_diff", ROOT / "infra/deploy/bootstrap_pin_semantic_diff.py"
)
assert _SPEC and _SPEC.loader
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

BootstrapPinError = _MOD.BootstrapPinError
assert_semantic_no_op_pin = _MOD.assert_semantic_no_op_pin

_MAIL_ARN = (
    "arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/database-url-4pJMDr"
)
_REPORT_ARN = (
    "arn:aws:secretsmanager:ap-northeast-1:718959508629:"
    "secret:teamagent/dev/report-link-hmac-RKEHWS"
)
_MAIL_VERSION = "0676b20c-b6c2-4486-aebb-74b1b9972031"
_REPORT_VERSION = "cfe6f92c-1697-4191-8fb3-5b396d21c2f3"
_DB_ARN = (
    "arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/database-url-4pJMDr"
)

_RESOLVED = {_MAIL_ARN: _MAIL_VERSION, _REPORT_ARN: _REPORT_VERSION}
_PINNABLE = frozenset({"MAIL_ACTION_HMAC_SECRET", "REPORT_LINK_HMAC_SECRET"})


def _live() -> dict:
    return {
        "name": "teamagent-mcp",
        "image": "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@sha256:"
        + "b" * 64,
        "cpu": 1024,
        "memory": 2048,
        "essential": True,
        "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
        "environment": [
            {"name": "LOG_LEVEL", "value": "INFO"},
            {"name": "SEARCH_HNSW_EF_SEARCH", "value": "100"},
        ],
        "secrets": [
            {"name": "DATABASE_URL", "valueFrom": _DB_ARN},
            {"name": "MAIL_ACTION_HMAC_SECRET", "valueFrom": _MAIL_ARN},
            {"name": "REPORT_LINK_HMAC_SECRET", "valueFrom": _REPORT_ARN},
        ],
        "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": "/x"}},
    }


def _pinned() -> dict:
    candidate = copy.deepcopy(_live())
    for entry in candidate["secrets"]:
        if entry["name"] == "MAIL_ACTION_HMAC_SECRET":
            entry["valueFrom"] = f"{_MAIL_ARN}:::{_MAIL_VERSION}"
        elif entry["name"] == "REPORT_LINK_HMAC_SECRET":
            entry["valueFrom"] = f"{_REPORT_ARN}:::{_REPORT_VERSION}"
    return candidate


def _check(candidate: dict, *, live: dict | None = None) -> dict[str, str]:
    return assert_semantic_no_op_pin(
        live=live if live is not None else _live(),
        candidate=candidate,
        resolved_versions=_RESOLVED,
        pinnable_secret_names=_PINNABLE,
        scope="mcp",
    )


# ── GREEN: VersionId 固定だけ ────────────────────────────────────────────────


def test_pin_only_is_accepted() -> None:
    applied = _check(_pinned())
    assert applied == {
        "MAIL_ACTION_HMAC_SECRET": _MAIL_VERSION,
        "REPORT_LINK_HMAC_SECRET": _REPORT_VERSION,
    }


# ── RED: 一致必須の項目が動いたら全部拒否 ───────────────────────────────────


@pytest.mark.parametrize(
    ("field", "mutate"),
    [
        ("image digest", lambda d: d.__setitem__("image", d["image"][:-1] + "c")),
        ("cpu", lambda d: d.__setitem__("cpu", 2048)),
        ("memory", lambda d: d.__setitem__("memory", 4096)),
        ("essential", lambda d: d.__setitem__("essential", False)),
        ("network(portMappings)", lambda d: d["portMappings"].append({"containerPort": 9090})),
        (
            "non-HMAC env 値",
            lambda d: d["environment"].__setitem__(0, {"name": "LOG_LEVEL", "value": "DEBUG"}),
        ),
        ("non-HMAC env 追加", lambda d: d["environment"].append({"name": "NEW", "value": "1"})),
        ("env の順序", lambda d: d["environment"].reverse()),
        ("container name", lambda d: d.__setitem__("name", "other")),
        (
            "logConfiguration",
            lambda d: d["logConfiguration"]["options"].__setitem__("awslogs-group", "/y"),
        ),
        ("未知フィールド追加", lambda d: d.__setitem__("privileged", True)),
    ],
)
def test_any_non_pin_difference_is_rejected(field: str, mutate) -> None:
    candidate = _pinned()
    mutate(candidate)
    with pytest.raises(BootstrapPinError) as exc:
        _check(candidate)
    assert exc.value.code == "non_pin_difference_detected", field


def test_non_hmac_secret_change_is_rejected() -> None:
    """secret material / ARN の差し替えは HMAC 以外でも拒否。"""
    candidate = _pinned()
    for entry in candidate["secrets"]:
        if entry["name"] == "DATABASE_URL":
            entry["valueFrom"] = _DB_ARN + ":::" + _MAIL_VERSION
    with pytest.raises(BootstrapPinError) as exc:
        _check(candidate)
    assert exc.value.code == "non_hmac_secret_changed"


def test_secret_addition_or_removal_is_rejected() -> None:
    added = _pinned()
    added["secrets"].append({"name": "EXTRA", "valueFrom": _DB_ARN})
    with pytest.raises(BootstrapPinError) as exc:
        _check(added)
    assert exc.value.code in {"secret_set_changed", "non_pin_difference_detected"}

    removed = _pinned()
    removed["secrets"] = [e for e in removed["secrets"] if e["name"] != "DATABASE_URL"]
    with pytest.raises(BootstrapPinError) as exc:
        _check(removed)
    assert exc.value.code in {"secret_set_changed", "non_pin_difference_detected"}


def test_secret_arn_swap_is_rejected() -> None:
    """同じ secret 名で別 ARN を pin するのは NG（material の差し替え）。"""
    candidate = _pinned()
    for entry in candidate["secrets"]:
        if entry["name"] == "MAIL_ACTION_HMAC_SECRET":
            entry["valueFrom"] = f"{_REPORT_ARN}:::{_REPORT_VERSION}"
    with pytest.raises(BootstrapPinError) as exc:
        _check(candidate)
    assert exc.value.code == "secret_arn_changed"


def test_version_other_than_awscurrent_is_rejected() -> None:
    """解決した AWSCURRENT 以外の VersionId は拒否（古い世代への固定を防ぐ）。"""
    candidate = _pinned()
    for entry in candidate["secrets"]:
        if entry["name"] == "MAIL_ACTION_HMAC_SECRET":
            entry["valueFrom"] = f"{_MAIL_ARN}:::11111111-2222-3333-4444-555555555555"
    with pytest.raises(BootstrapPinError) as exc:
        _check(candidate)
    assert exc.value.code == "version_not_awscurrent"


def test_unresolved_secret_is_rejected() -> None:
    """live から VersionId を解決できていない secret は pin できない。"""
    candidate = _pinned()
    with pytest.raises(BootstrapPinError) as exc:
        assert_semantic_no_op_pin(
            live=_live(),
            candidate=candidate,
            resolved_versions={_REPORT_ARN: _REPORT_VERSION},
            pinnable_secret_names=_PINNABLE,
            scope="mcp",
        )
    assert exc.value.code == "version_not_resolved"


def test_already_pinned_live_is_rejected() -> None:
    """live が既に pin 済みなら Bootstrap Pin の対象ではない。"""
    live = _live()
    for entry in live["secrets"]:
        if entry["name"] == "MAIL_ACTION_HMAC_SECRET":
            entry["valueFrom"] = f"{_MAIL_ARN}:::{_MAIL_VERSION}"
    candidate = copy.deepcopy(live)
    for entry in candidate["secrets"]:
        if entry["name"] == "MAIL_ACTION_HMAC_SECRET":
            entry["valueFrom"] = f"{_MAIL_ARN}:::99999999-2222-3333-4444-555555555555"
    with pytest.raises(BootstrapPinError) as exc:
        _check(candidate, live=live)
    assert exc.value.code == "live_already_pinned"


def test_unpinning_is_rejected() -> None:
    """pin を外す方向も許さない。"""
    live = _live()
    for entry in live["secrets"]:
        if entry["name"] == "MAIL_ACTION_HMAC_SECRET":
            entry["valueFrom"] = f"{_MAIL_ARN}:::{_MAIL_VERSION}"
    with pytest.raises(BootstrapPinError) as exc:
        _check(_live(), live=live)
    assert exc.value.code == "live_already_pinned"


def test_no_change_at_all_is_rejected() -> None:
    """何も pin しない plan を Bootstrap Pin として通さない。"""
    with pytest.raises(BootstrapPinError) as exc:
        _check(_live())
    assert exc.value.code == "no_pin_applied"


def test_malformed_reference_is_rejected() -> None:
    candidate = _pinned()
    for entry in candidate["secrets"]:
        if entry["name"] == "MAIL_ACTION_HMAC_SECRET":
            entry["valueFrom"] = f"{_MAIL_ARN}:::"
    with pytest.raises(BootstrapPinError) as exc:
        _check(candidate)
    assert exc.value.code == "reference_malformed"


def test_normal_rollout_constraints_are_not_reused_here() -> None:
    """通常 rollout の制約をこの経路へ持ち込んでいないこと。

    worker_verified / canary anchor / morning DISABLED / rollback_image は
    Bootstrap Pin の判定に現れてはいけない（別の security boundary を使う）。
    """
    source = (ROOT / "infra/deploy/bootstrap_pin_semantic_diff.py").read_text(encoding="utf-8")
    body = source.split('"""', 2)[2]  # module docstring より後ろだけを見る
    for forbidden in ("worker_verified", "canary", "rollback_image", "DISABLED"):
        assert forbidden not in body, forbidden
