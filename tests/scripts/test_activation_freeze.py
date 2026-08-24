"""activation freeze の機械強制テスト（PR2-A0.x / Freeze v2）。

2026-08-24 ユーザー裁定の反映:
  - generation publisher freeze は口頭合意だけを hard safety control にしていたため
    2 度破られた（2026-08-20 / 08-21）。以後、frozen surface の変更は CI で落とす
  - dev merge freeze も複数回破られている。activation の安全性は dev tip の不変性ではなく
    activation-execution-base + approved commit allowlist + fast-forward only に置く
  - freeze v2 の境界は「最後に変更された時刻」ではなく「変更できない状態を確認した時刻」

各ガードは変異で壊すと赤くなることを実証する（リポジトリ規約）。
実 commit（27fe776 / d3fe768 / 202398f）に対する挙動も回帰として固定する。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
FREEZE = ROOT / "infra/deploy/activation_freeze.json"
ALLOWLIST = ROOT / "infra/deploy/activation_execution_allowlist.json"
CHECKER = ROOT / "infra/deploy/activation_freeze_check.py"
CI = ROOT / ".github/workflows/ci.yml"

sys.path.insert(0, str(ROOT / "infra/deploy"))

from activation_freeze_check import (  # noqa: E402
    ENFORCING_STATES,
    FreezeError,
    assert_execution_line,
    assert_frozen_surface,
    frozen_paths,
    load_allowlist,
    load_freeze,
)


def _freeze_doc() -> dict[str, Any]:
    return json.loads(FREEZE.read_text(encoding="utf-8"))


def _allowlist_doc() -> dict[str, Any]:
    return json.loads(ALLOWLIST.read_text(encoding="utf-8"))


def _write(tmp_path: Path, name: str, doc: dict[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _has_ref(ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
        ).returncode
        == 0
    )


# ── freeze 宣言の構造契約 ────────────────────────────────────────────────────


def test_committed_freeze_declaration_is_valid() -> None:
    doc = load_freeze(FREEZE)
    assert doc["generation_publisher_freeze"]["v1"]["status"] == "voided"
    assert len(doc["generation_publisher_freeze"]["v1"]["violations"]) == 6


def test_v1_violations_record_what_when_and_which_version() -> None:
    """失効の根拠は「何が・いつ・どの VersionId で」越えたかを保持する。"""
    for entry in _freeze_doc()["generation_publisher_freeze"]["v1"]["violations"]:
        assert entry["wave"] in (1, 2)
        assert entry["published_at"].endswith("Z")
        assert entry["project"].startswith("teamagent-dev-")
        assert len(entry["key_sha256_prefix"]) == 16
        assert entry["version_id"]


def test_v1_cannot_be_quietly_reinstated(tmp_path: Path) -> None:
    """v1 を有効へ戻す / 違反記録を消す変異は拒否（失効の根拠を消せない）。"""
    doc = _freeze_doc()
    doc["generation_publisher_freeze"]["v1"]["status"] = "active"
    with pytest.raises(FreezeError, match="失効済み"):
        load_freeze(_write(tmp_path, "f.json", doc))

    doc = _freeze_doc()
    doc["generation_publisher_freeze"]["v1"]["violations"] = []
    with pytest.raises(FreezeError, match="違反実測"):
        load_freeze(_write(tmp_path, "f2.json", doc))


def test_v2_boundary_cannot_be_left_blank_when_active(tmp_path: Path) -> None:
    """state=active を主張するなら v2 境界の記録が必須（口頭 freeze の再発防止）。"""
    doc = _freeze_doc()
    doc["generation_publisher_freeze"]["state"] = "active"
    with pytest.raises(FreezeError, match=r"v2\.started_at"):
        load_freeze(_write(tmp_path, "f.json", doc))


def test_pending_v2_cannot_carry_a_boundary(tmp_path: Path) -> None:
    """pending_v2 のまま境界を書き込む矛盾を拒否（勝手な境界確定の防止）。"""
    doc = _freeze_doc()
    doc["generation_publisher_freeze"]["v2"]["started_at"] = "2026-08-21T07:18:00Z"
    with pytest.raises(FreezeError, match="矛盾"):
        load_freeze(_write(tmp_path, "f.json", doc))


def test_v2_precondition_states_confirmation_not_last_change() -> None:
    """境界の定義が「変更できない状態を確認した時点」であることを文言で固定する。"""
    v2 = _freeze_doc()["generation_publisher_freeze"]["v2"]
    assert "確認した時点" in v2["precondition"]
    assert "自動的に境界にしてはならない" in v2["precondition"]
    assert v2["evidence_required"]


def test_pending_v2_is_an_enforcing_state() -> None:
    """v1 失効〜v2 確定の谷間が最も危険なので、そこでも frozen surface を守る。"""
    assert "pending_v2" in ENFORCING_STATES
    assert "active" in ENFORCING_STATES
    assert "released" not in ENFORCING_STATES


# ── frozen surface: manifest を真実源とする ────────────────────────────────


def test_frozen_surface_is_derived_from_the_manifest_not_a_handwritten_list() -> None:
    """18 generation inputs は manifest から読む（手書きリストの陳腐化を防ぐ）。"""
    doc = load_freeze(FREEZE)
    surface = frozen_paths(ROOT, doc, "HEAD")
    manifest = json.loads(
        (ROOT / "infra/deploy/buildspec_generation_inputs.json").read_text(encoding="utf-8")
    )
    assert set(manifest["inputs"]) <= surface
    assert len(manifest["inputs"]) == 18
    assert set(doc["frozen_change_surface"]["additional_publisher_paths"]) <= surface


def test_frozen_surface_rejects_a_rewired_inputs_source(tmp_path: Path) -> None:
    """真実源の差し替え（別ファイル参照）を拒否。"""
    doc = _freeze_doc()
    doc["frozen_change_surface"]["generation_inputs_source"] = "infra/deploy/other.json#inputs"
    with pytest.raises(FreezeError, match="generation_inputs_source"):
        frozen_paths(ROOT, load_freeze(_write(tmp_path, "f.json", doc)), "HEAD")


# ── frozen surface: 実 commit に対する回帰 ─────────────────────────────────


@pytest.mark.parametrize(
    ("base", "head", "reason"),
    [
        ("d3fe768", "27fe776", "openssl CVE が 18 inputs のうち 2 件を変更"),
        ("0eab7f2", "d3fe768", "vulkan ドリフト追随が 18 inputs のうち 1 件を変更"),
    ],
)
def test_real_generation_input_changes_are_rejected(base: str, head: str, reason: str) -> None:
    """実際に freeze を破った commit が、この機構では止まることを実 diff で示す。"""
    if not (_has_ref(base) and _has_ref(head)):
        pytest.skip(f"ref 未取得: {base}/{head}")
    with pytest.raises(FreezeError, match="FROZEN SURFACE"):
        assert_frozen_surface(ROOT, FREEZE, base, head)


@pytest.mark.parametrize(
    ("base", "head", "reason"),
    [
        ("d3fe768", "202398f", "台帳/manifest のみの再生成は frozen surface 外"),
        ("86dd782", "9eea313", "mail 機能は generation と無関係"),
    ],
)
def test_unrelated_changes_still_pass(base: str, head: str, reason: str) -> None:
    """freeze が無関係な変更まで止めないこと（過剰ブロックの防止）。"""
    if not (_has_ref(base) and _has_ref(head)):
        pytest.skip(f"ref 未取得: {base}/{head}")
    assert_frozen_surface(ROOT, FREEZE, base, head)


# ── unlock 宣言 ────────────────────────────────────────────────────────────


def test_unlock_is_inactive_and_empty_by_default() -> None:
    unlock = _freeze_doc()["unlock"]
    assert unlock["active"] is False
    assert unlock["scope_paths"] == []
    assert unlock["reason"] is None


def test_unlock_requires_scope_reason_and_gate(tmp_path: Path) -> None:
    """unlock を主張するなら対象 path・理由・human gate の出所が必須。"""
    for missing in ("scope_paths", "reason", "gate"):
        doc = _freeze_doc()
        doc["unlock"] = {
            "active": True,
            "scope_paths": ["infra/codebuild/teamagent_runtime_contract.json"],
            "reason": "test",
            "gate": "test gate",
        }
        doc["unlock"][missing] = [] if missing == "scope_paths" else None
        with pytest.raises(FreezeError):
            load_freeze(_write(tmp_path, f"f-{missing}.json", doc))


def test_unlock_allows_only_its_declared_scope(tmp_path: Path) -> None:
    """scope 内は通し、scope 外の frozen path 変更は通さない。"""
    if not (_has_ref("d3fe768") and _has_ref("27fe776")):
        pytest.skip("ref 未取得")
    doc = _freeze_doc()
    doc["unlock"] = {
        "active": True,
        "scope_paths": [
            "infra/codebuild/teamagent_core_media_release_contract.json",
            "infra/codebuild/teamagent_runtime_contract.json",
        ],
        "reason": "Generation Re-baseline v2",
        "gate": "human gate 2026-08-24",
    }
    assert_frozen_surface(ROOT, _write(tmp_path, "ok.json", doc), "d3fe768", "27fe776")

    doc["unlock"]["scope_paths"] = ["infra/codebuild/teamagent_runtime_contract.json"]
    with pytest.raises(FreezeError, match="scope_paths 外"):
        assert_frozen_surface(ROOT, _write(tmp_path, "narrow.json", doc), "d3fe768", "27fe776")


def test_unlock_cannot_be_broader_than_the_actual_change(tmp_path: Path) -> None:
    """使わない path を混ぜた過剰 unlock（将来の穴）を拒否。"""
    if not (_has_ref("d3fe768") and _has_ref("27fe776")):
        pytest.skip("ref 未取得")
    doc = _freeze_doc()
    doc["unlock"] = {
        "active": True,
        "scope_paths": [
            "infra/codebuild/teamagent_core_media_release_contract.json",
            "infra/codebuild/teamagent_runtime_contract.json",
            "infra/terraform/codebuild.tf",
        ],
        "reason": "over-broad",
        "gate": "human gate",
    }
    with pytest.raises(FreezeError, match="過剰な unlock"):
        assert_frozen_surface(ROOT, _write(tmp_path, "broad.json", doc), "d3fe768", "27fe776")


# ── execution line allowlist（hard boundary） ──────────────────────────────


def test_committed_allowlist_matches_the_real_execution_line() -> None:
    """allowlist が実際の activation-execution-base と exact 一致する。"""
    if not _has_ref("activation-execution-base"):
        pytest.skip("execution line 未取得")
    assert_execution_line(ROOT, ALLOWLIST)


def test_allowlist_records_full_shas_only(tmp_path: Path) -> None:
    """短縮 SHA は衝突と取り違えを許すため禁止。"""
    doc = _allowlist_doc()
    doc["approved_commits"][0]["sha"] = doc["approved_commits"][0]["sha"][:8]
    with pytest.raises(FreezeError, match="40 桁"):
        load_allowlist(_write(tmp_path, "a.json", doc))


def test_expected_head_must_equal_the_last_approved_commit(tmp_path: Path) -> None:
    doc = _allowlist_doc()
    doc["expected_head"] = doc["execution_base"]["sha"]
    with pytest.raises(FreezeError, match="expected_head"):
        load_allowlist(_write(tmp_path, "a.json", doc))


def test_execution_line_rejects_an_unlisted_commit(tmp_path: Path) -> None:
    """allowlist に無い commit が execution line に入っていたら FATAL。"""
    if not _has_ref("activation-execution-base"):
        pytest.skip("execution line 未取得")
    doc = _allowlist_doc()
    dropped = doc["approved_commits"].pop()
    doc["expected_head"] = doc["approved_commits"][-1]["sha"]
    with pytest.raises(FreezeError, match="commit 数"):
        assert_execution_line(ROOT, _write(tmp_path, "a.json", doc))
    assert dropped["sha"]


def test_execution_line_rejects_a_rewritten_subject(tmp_path: Path) -> None:
    """subject の書き換え（履歴改変）を検出する。"""
    if not _has_ref("activation-execution-base"):
        pytest.skip("execution line 未取得")
    doc = _allowlist_doc()
    doc["approved_commits"][0]["subject"] = "feat: 別の何か"
    with pytest.raises(FreezeError, match="subject"):
        assert_execution_line(ROOT, _write(tmp_path, "a.json", doc))


def test_execution_line_rejects_a_foreign_base(tmp_path: Path) -> None:
    """base を無関係な commit に差し替えると祖先関係が崩れて FATAL。"""
    for candidate in ("4f7da58", "9eea313", "27fe776"):
        if _has_ref(candidate):
            foreign = subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", candidate],
                capture_output=True,
                text=True,
            ).stdout.strip()
            break
    else:
        pytest.skip("差し替え用 ref 未取得")
    if not _has_ref("activation-execution-base"):
        pytest.skip("execution line 未取得")
    doc = _allowlist_doc()
    doc["execution_base"]["sha"] = foreign
    with pytest.raises(FreezeError, match="祖先"):
        assert_execution_line(ROOT, _write(tmp_path, "a.json", doc))


def test_excluded_commits_are_recorded_with_reasons() -> None:
    """取り込まない commit とその理由が明文化されている（後任が判断を再現できる）。"""
    excluded = {e["sha"]: e["reason"] for e in _allowlist_doc()["excluded_commits"]}
    for sha in ("4f7da58", "d3fe768", "202398f", "27fe776", "9eea313"):
        assert sha in excluded, sha
        assert excluded[sha]
    assert "単独 cherry-pick" in excluded["202398f"]


def test_dev_freeze_is_not_the_safety_boundary() -> None:
    """安全性を dev tip の不変性に依存させない、が宣言に書かれている。"""
    dev = _freeze_doc()["dev_merge_freeze"]
    assert dev["state"] == "operational_only"
    assert dev["hard_boundary"].endswith("activation_execution_allowlist.json")
    assert "依存させない" in dev["note"]


# ── CI 配線 ────────────────────────────────────────────────────────────────


def test_ci_enforces_the_frozen_surface_on_every_pr() -> None:
    """CI が frozen surface 検査を実行し、full history を取得している。"""
    text = CI.read_text(encoding="utf-8")
    assert "activation_freeze_check.py" in text
    assert "assert-frozen-surface" in text
    # merge-base を取るため full history が必要（shallow だと base が解決できない）
    section = text[text.index("activation-freeze") :]
    assert "fetch-depth: 0" in section


def test_runbook_records_the_boundary_rule_and_the_202398f_prohibition() -> None:
    """runbook が境界の定義と 202398f 単独 cherry-pick の恒久禁止を明記している。"""
    text = (ROOT / "docs/runbooks/activation_freeze.md").read_text(encoding="utf-8")
    assert "「最後に変更された時刻」を境界にしてはならない" in text
    assert "202398f" in text and "恒久禁止" in text
    assert "fast-forward only" in text
    assert "fetch-depth: 0" in text
