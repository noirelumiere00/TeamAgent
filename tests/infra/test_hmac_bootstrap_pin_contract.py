"""bootstrap_pin（移行専用 phase）と exact legacy selector 許可の範囲を固定する。

canonical 化のために、live が指している legacy selector を一時的に
primary / previous として表現できる必要がある。しかしこれは **移行専用の互換経路**であり、
「任意の第三 secret を許す」一般化に広げてはならない（2026-08-26 裁定）。

ここで固定するのは 3 点:

1. `bootstrap_pin` phase が両 domain に存在すること
2. legacy selector の許可が **exact ARN 1 本ずつ**であること
   （ワイルドカード・任意 ARN・任意の第三 secret への拡張を禁じる）
3. primary としての legacy 許可が **bootstrap_pin phase に限定**されていること

さらに、canonical 化完了後にこの互換経路を撤去できるよう
`MIGRATION-ONLY` タグで grep 可能にし、件数を pin する。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TF_ROOT = ROOT / "infra" / "terraform"
ROTATION_TF = TF_ROOT / "hmac_rotation.tf"
KEYRINGS_TF = TF_ROOT / "hmac_keyrings.tf"

# live が現在指している legacy selector（2026-08-26 実測）。
# canonical 化が終わったらこの 2 本の許可ごと削除する。
_MAIL_LEGACY_ARN = (
    "arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/database-url-4pJMDr"
)
_REPORT_LEGACY_ARN = (
    "arn:aws:secretsmanager:ap-northeast-1:718959508629:"
    "secret:teamagent/dev/report-link-hmac-RKEHWS"
)
_MIGRATION_TAG = "MIGRATION-ONLY"
_EXPECTED_MIGRATION_TAGS = {"hmac_rotation.tf": 3}


def _rotation() -> str:
    return ROTATION_TF.read_text(encoding="utf-8")


def _keyrings() -> str:
    return KEYRINGS_TF.read_text(encoding="utf-8")


def _validation_block(source: str, variable: str) -> str:
    """変数ブロックの validation 部分だけを切り出す。"""
    start = source.index(f'variable "{variable}"')
    end = (
        source.index('variable "', start + 1)
        if 'variable "' in source[start + 1 :]
        else len(source)
    )
    return source[start:end]


def test_bootstrap_pin_phase_exists_for_both_domains() -> None:
    source = _keyrings()
    for domain in ("mail_action", "report_link"):
        block = _validation_block(source, f"{domain}_hmac_rollout_phase")
        assert '"bootstrap_pin"' in block, domain
        # 既存 phase を落としていない
        for phase in ("blocked", "legacy_migration", "dedicated_rotation", "steady"):
            assert f'"{phase}"' in block, (domain, phase)


def test_legacy_primary_is_allowed_only_during_bootstrap_pin() -> None:
    """legacy selector を primary にできるのは bootstrap_pin のときだけ。"""
    source = _rotation()
    for domain, arn in (
        ("mail_action", _MAIL_LEGACY_ARN),
        ("report_link", _REPORT_LEGACY_ARN),
    ):
        block = _validation_block(source, f"{domain}_hmac_secret_arn")
        assert arn in block, domain
        # phase ガードが同じ条件式の中にある
        assert f'var.{domain}_hmac_rollout_phase == "bootstrap_pin"' in block, domain


def test_legacy_allowance_is_exact_and_never_generalised() -> None:
    """exact ARN のみ。ワイルドカードや任意 secret への一般化を禁じる。"""
    source = _rotation()
    for domain in ("mail_action", "report_link"):
        block = _validation_block(source, f"{domain}_hmac_secret_arn")
        # legacy 許可の枝だけを取り出す（コメント中の bootstrap_pin ではなく実際の phase ガードから）
        guard = f'var.{domain}_hmac_rollout_phase == "bootstrap_pin"'
        legacy_branch = block[block.index(guard) : block.index("error_message", block.index(guard))]
        # exact 文字列比較のみ。regex 由来のワイルドカード類が枝に無いこと
        assert "*" not in legacy_branch, domain
        assert ".+" not in legacy_branch, domain
        assert "[A-Za-z0-9]" not in legacy_branch, domain
        assert "regex" not in legacy_branch, domain
        # 比較対象は exact ARN literal ちょうど 1 本
        arns = re.findall(r'"(arn:aws:secretsmanager:[^"]+)"', legacy_branch)
        assert len(arns) == 1, (domain, arns)


def test_report_previous_accepts_the_exact_legacy_selector_only() -> None:
    """previous 側の migration 許可も exact name + version pin に限定されている。"""
    block = _validation_block(_rotation(), "report_link_hmac_previous_secret_arn")
    assert "report-link-hmac-" in block
    # version pin を必ず要求する
    assert ":::[A-Za-z0-9-]{32,64}$" in block
    # 任意の第三 secret を通す書き方になっていない
    assert "secret:teamagent/dev/[A-Za-z0-9" not in block
    assert "secret:teamagent/dev/.*" not in block


def test_mail_previous_still_refuses_the_report_link_selector() -> None:
    """domain を跨いだ selector の流用を許していない（mail に report の legacy を入れられない）。"""
    block = _validation_block(_rotation(), "mail_action_hmac_previous_secret_arn")
    assert "report-link-hmac" not in block


def test_migration_only_allowances_are_tagged_and_counted() -> None:
    """撤去漏れ防止。互換経路が黙って増減したら赤にする。"""
    counts = {
        "hmac_rotation.tf": _rotation().count(_MIGRATION_TAG),
    }
    assert counts == _EXPECTED_MIGRATION_TAGS


def test_bootstrap_pin_documents_that_it_is_temporary() -> None:
    """phase の意図（selector も material も変えない・撤去する）が残っている。"""
    source = _keyrings()
    marker = source.index("bootstrap_pin は移行専用")
    note = source[marker : marker + 400]
    assert "material" in note
    assert "撤去" in note
