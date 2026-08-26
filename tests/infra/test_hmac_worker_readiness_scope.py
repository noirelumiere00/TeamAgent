"""worker HMAC readiness を共通 HMAC readiness から分離した契約を固定する。

2026-08-26 の裁定。承認済み worker archive の所在が repo・AWS・ローカルのいずれからも
特定できなかったため、`worker_hmac_artifact_sha256` を bootstrap_pin / canonical rotation の
必須条件から外す。ただし **検査を消すのではなく fail-closed の位置を移す**:

    common HMAC readiness   … manifest / rollout control / selector / VersionId pin / domain contract
    worker readiness        … enable_hmac_worker_deploy が真のときだけ artifact SHA 必須

要求される真理値表:

    bootstrap_pin      + worker disabled + SHA 空      → GREEN
    canonical rotation + worker disabled + SHA 空      → GREEN
    worker enabled     + SHA 空                        → RED
    worker_verified 以降 + artifact 無し               → RED（gate CLI 側）
    worker enabled     + bogus SHA                     → RED
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEYRINGS_TF = ROOT / "infra" / "terraform" / "hmac_keyrings.tf"
WORKER_TF = ROOT / "infra" / "terraform" / "hmac_worker_deploy.tf"
ROLLOUT_GATE = ROOT / "scripts" / "hmac_rollout_gate.py"

_SHA_VAR = "worker_hmac_artifact_sha256"
_SHA_RE = re.compile(r"^[a-f0-9]{64}$")


def _keyrings() -> str:
    return KEYRINGS_TF.read_text(encoding="utf-8")


def _config_ready_body(source: str, domain: str) -> str:
    start = source.index(f"{domain}_hmac_config_ready = (")
    end = source.index("_hmac_transition_valid = (", start)
    return source[start:end]


def _worker_ready_expression(source: str) -> str:
    start = source.index("hmac_worker_artifact_ready = (")
    end = source.index("\n  )", start)
    return source[start : end + 4]


# ── 分離そのもの ────────────────────────────────────────────────────────────


def test_common_config_ready_no_longer_requires_the_worker_artifact_directly() -> None:
    """共通 readiness から artifact SHA の直接検査が消えていること。"""
    source = _keyrings()
    for domain in ("mail_action", "report_link"):
        body = _config_ready_body(source, domain)
        assert _SHA_VAR not in body, domain
        # 代わりに worker readiness を参照する
        assert "local.hmac_worker_artifact_ready" in body, domain


def test_worker_readiness_is_scoped_to_the_worker_feature_flag() -> None:
    """worker in scope の定義が feature flag そのものであること。"""
    source = _keyrings()
    assert "hmac_worker_in_scope = var.enable_hmac_worker_deploy" in source


def test_worker_readiness_expression_has_the_exact_expected_shape() -> None:
    """`!in_scope || regex(64hex)` の形ちょうど。緩めたら赤にする。"""
    expression = _worker_ready_expression(_keyrings())
    normalized = " ".join(expression.split())
    assert normalized == (
        "hmac_worker_artifact_ready = ( !local.hmac_worker_in_scope "
        f'|| can(regex("^[a-f0-9]{{64}}$", var.{_SHA_VAR})) )'
    ), normalized


# ── 真理値表（上の shape 検査で意味が固定されているのでその意味を確認する）────


def _worker_artifact_ready(*, worker_enabled: bool, sha: str) -> bool:
    """tf の `hmac_worker_artifact_ready` と同じ意味。

    式そのものは `test_worker_readiness_expression_has_the_exact_expected_shape` が
    形で固定しているため、ここではその意味を真理値表として確認する。
    """
    return (not worker_enabled) or bool(_SHA_RE.match(sha))


def test_bootstrap_pin_with_worker_disabled_and_empty_sha_is_green() -> None:
    assert _worker_artifact_ready(worker_enabled=False, sha="") is True


def test_canonical_rotation_with_worker_disabled_and_empty_sha_is_green() -> None:
    # phase に依らず worker が範囲外なら readiness は満たされる
    assert _worker_artifact_ready(worker_enabled=False, sha="") is True


def test_worker_enabled_with_empty_sha_is_red() -> None:
    assert _worker_artifact_ready(worker_enabled=True, sha="") is False


def test_worker_enabled_with_bogus_sha_is_red() -> None:
    for bogus in (
        "not-a-sha",
        "ABCDEF" + "0" * 58,  # 大文字は不可
        "a" * 63,  # 短い
        "a" * 65,  # 長い
        " " + "a" * 64,  # 前後の空白
    ):
        assert _worker_artifact_ready(worker_enabled=True, sha=bogus) is False, bogus


def test_worker_enabled_with_a_well_formed_sha_is_green() -> None:
    assert _worker_artifact_ready(worker_enabled=True, sha="a" * 64) is True


# ── worker を有効化した瞬間に hard blocker が復活すること ────────────────────


def test_worker_resource_fails_closed_on_a_missing_artifact_sha() -> None:
    """worker deploy リソース側に artifact SHA の precondition があること。"""
    source = WORKER_TF.read_text(encoding="utf-8")
    block = source[source.index('resource "terraform_data" "hmac_worker_deploy"') :]
    lifecycle = block[block.index("lifecycle {") :]
    assert f'can(regex("^[a-f0-9]{{64}}$", var.{_SHA_VAR}))' in lifecycle
    assert "requires the reviewed worker archive" in lifecycle


def test_worker_verified_stage_still_requires_the_rollback_artifact() -> None:
    """「worker_verified 以降」の fail-closed は gate CLI 側に残っていること。

    tf ではなく `hmac_rollout_gate.py` が artifact の提示を要求する。
    ここが消えると、worker を進める経路だけ provenance 検査を素通りできてしまう。
    """
    source = ROLLOUT_GATE.read_text(encoding="utf-8")
    assert "worker-verified" in source
    assert "worker_rollback_artifact" in source
    assert "rollback_artifact=Path(args.worker_rollback_artifact)" in source


def test_provenance_requirement_is_documented_as_deferred_not_deleted() -> None:
    """「消した」のではなく「移した」ことが読めること（将来の再有効化のため）。"""
    source = _keyrings()
    marker = source.index("hmac_worker_in_scope = var.enable_hmac_worker_deploy")
    note = source[max(0, marker - 900) : marker]
    assert "消す" in note
    assert "worker readiness" in note
    assert "hard blocker" in note
