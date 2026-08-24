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
GUARD = ROOT / "infra/deploy/terraform_runtime_guard.sh"

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
    assert len(doc["generation_publisher_freeze"]["v1"]["violations"]) == 8


def test_v1_violations_record_what_when_and_which_version() -> None:
    """失効の根拠は「何が・いつ・どの VersionId で」越えたかを保持する。"""
    violations = _freeze_doc()["generation_publisher_freeze"]["v1"]["violations"]
    # 訂正: 当初 6 件と報告したが image-builder を両波で見落としていた
    assert len(violations) == 8
    assert sum(1 for v in violations if v["project"].endswith("image-builder")) == 2
    for entry in violations:
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
    """state=active を主張するなら v2 境界の記録が必須（口頭 freeze の再発防止）。

    現在の宣言値に依存しないよう、active かつ started_at 空という組み合わせを
    明示的に作って拒否されることを確かめる。
    """
    doc = _freeze_doc()
    doc["generation_publisher_freeze"]["state"] = "active"
    doc["generation_publisher_freeze"]["v2"]["started_at"] = None
    with pytest.raises(FreezeError, match=r"v2\.started_at"):
        load_freeze(_write(tmp_path, "f.json", doc))


def test_pending_v2_cannot_carry_a_boundary(tmp_path: Path) -> None:
    """pending_v2 のまま境界を書き込む矛盾を拒否（勝手な境界確定の防止）。"""
    doc = _freeze_doc()
    doc["generation_publisher_freeze"]["state"] = "pending_v2"
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


def _mini_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = ["-c", "user.email=t@example.com", "-c", "user.name=t"]

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *env, *args], capture_output=True, text=True, check=True
        ).stdout

    git("init", "-q", "-b", "main")
    (repo / "a.txt").write_text("base\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base commit")
    return repo


def _rev(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_execution_line_detects_a_rewritten_history_as_force_push(tmp_path: Path) -> None:
    """expected_head が現 HEAD の祖先でない = force push / rebase を専用メッセージで検出。

    commit 列の SHA 照合とは独立のガード。履歴が作り直されると、subject が同じでも
    SHA が変わるため「allowlist 外 commit」ではなく「履歴改変」として止める必要がある。
    """
    repo = _mini_repo(tmp_path)
    env = ["-c", "user.email=t@example.com", "-c", "user.name=t"]

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *env, *args], capture_output=True, check=True)

    base = _rev(repo, "HEAD")
    (repo / "b.txt").write_text("approved\n")
    git("add", "-A")
    git("commit", "-q", "-m", "approved change")
    original_head = _rev(repo, "HEAD")

    # 履歴を作り直す（同じ subject・別 SHA）。amend で親は base のまま
    (repo / "b.txt").write_text("approved but rewritten\n")
    git("add", "-A")
    git("commit", "-q", "--amend", "-m", "approved change")
    rewritten_head = _rev(repo, "HEAD")
    assert rewritten_head != original_head

    allowlist = _write(
        tmp_path,
        "a.json",
        {
            "schema_version": 1,
            "execution_ref": "main",
            "execution_base": {"sha": base, "subject": "base commit"},
            "approved_commits": [
                {"sha": original_head, "subject": "approved change", "gate": "test gate"}
            ],
            "expected_head": original_head,
        },
    )
    with pytest.raises(FreezeError, match="force push"):
        assert_execution_line(repo, allowlist, "main")


def test_execution_line_accepts_the_recorded_history_in_a_clean_repo(tmp_path: Path) -> None:
    """上と同じ構成で履歴を書き換えなければ通る（force push 検出の偽陽性防止）。"""
    repo = _mini_repo(tmp_path)
    env = ["-c", "user.email=t@example.com", "-c", "user.name=t"]
    base = _rev(repo, "HEAD")
    (repo / "b.txt").write_text("approved\n")
    subprocess.run(["git", "-C", str(repo), *env, "add", "-A"], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(repo), *env, "commit", "-q", "-m", "approved change"],
        capture_output=True,
        check=True,
    )
    head = _rev(repo, "HEAD")
    allowlist = _write(
        tmp_path,
        "a.json",
        {
            "schema_version": 1,
            "execution_ref": "main",
            "execution_base": {"sha": base, "subject": "base commit"},
            "approved_commits": [{"sha": head, "subject": "approved change", "gate": "test gate"}],
            "expected_head": head,
        },
    )
    assert "検証済み" in assert_execution_line(repo, allowlist, "main")


# ── 裁定⑦: integrity surface は adopt 対象から導出しない ────────────────────


def test_release_chain_inventory_is_the_integrity_surface_not_adopt_targets() -> None:
    """generation freeze integrity は release chain 全体を対象にする。

    2026-08-24 実測: adopt 台帳の 4 プロジェクトから S3 prefix を導出したため
    teamagent-dev-image-builder を両波で見落とし、違反を 6 件と誤報告した。
    adopt purity（4 forget + 4 import）と generation freeze integrity は別概念。
    """
    surface = _freeze_doc()["frozen_change_surface"]
    chain = set(surface["generation_release_chain_projects"])
    adopt_targets = {
        "teamagent-dev-mcp-source-publisher",
        "teamagent-dev-image-attestor",
        "teamagent-dev-image-promoter",
        "teamagent-dev-approval-publisher",
    }
    assert adopt_targets < chain, "release chain は adopt 対象の真の上位集合であること"
    assert "teamagent-dev-image-builder" in chain
    assert "別概念" in surface["generation_release_chain_note"]


def test_violation_count_correction_is_recorded() -> None:
    """誤報告の訂正理由が残っている（同じ導出ミスの再発防止）。"""
    note = _freeze_doc()["generation_publisher_freeze"]["v1"]["violation_count_correction"]
    assert "image-builder" in note
    assert "adopt 対象から導出してはならない" in note


# ── 裁定①: production deployment freeze 違反（B3 再発）の記録 ──────────────


def test_production_freeze_violation_records_the_b3_recurrence() -> None:
    """rebind 完了後に B3 を作り直したデプロイが実測つきで記録されている。"""
    prod = _freeze_doc()["production_deployment_freeze"]
    violation = prod["violations"][0]
    assert violation["principal"] == "user/AIIAdev"
    assert violation["events"]["RegisterTaskDefinition"] == 10
    assert violation["events"]["UpdateService"] == 4
    assert "B3" in violation["effect"]
    drift = violation["drift"]
    assert drift["mcp"] == "state :86 / live :88"
    assert drift["tiktok_acquire"].startswith("一致")


def test_production_freeze_points_at_the_persistent_deny_and_root_gap() -> None:
    """repo lock だけでは不足であること、root は SCP が要ることを宣言に残す。"""
    enforcement = _freeze_doc()["production_deployment_freeze"]["enforcement"]
    assert "activation_freeze_policy.tf" in enforcement
    assert "root" in enforcement and "SCP" in enforcement


# ── Freeze v2 の scope 定義（root は break-glass 例外） ──────────────────────


def test_freeze_v2_scope_is_not_claimed_to_be_mechanically_complete() -> None:
    """「production mutation が機械的に不可能」とは主張しない（2026-08-24 裁定）。

    root は identity policy をバイパスするため deny できない。誤った安心を
    与えないよう、宣言の文言レベルで固定する。
    """
    scope = _freeze_doc()["generation_publisher_freeze"]["v2"]["scope_definition"]
    assert "機械的に不可能な状態」ではない" in scope["statement"]
    assert "break-glass" in scope["statement"]
    components = scope["components"]
    assert len(components) == 4
    joined = "\n".join(components)
    assert "non-root" in joined
    assert "break-glass" in joined
    assert "root credential" in joined and "禁止" in joined
    assert "CloudTrail" in joined and "0" in joined


def test_root_residual_risk_is_explicitly_left_open() -> None:
    """SCP / root key 無効化を activation のついでにやらないことを明記する。"""
    scope = _freeze_doc()["generation_publisher_freeze"]["v2"]["scope_definition"]
    assert "SCP" in scope["residual_risk"]
    assert "ついでにはやらない" in scope["residual_risk"]


def test_root_baseline_is_recorded_with_measurements() -> None:
    """監視の基準となる root mutation ベースラインが実測つきで残っている。"""
    baseline = _freeze_doc()["generation_publisher_freeze"]["v2"]["scope_definition"][
        "root_mutation_baseline"
    ]
    assert baseline["since_2026_07_01"]["total"] == 64
    assert baseline["since_freeze_v1_window_2026_08_20T09_15_00Z"]["total"] == 0
    assert baseline["since_last_violation_2026_08_21T08_54_16Z"]["total"] == 0


def test_monitored_events_cover_every_deny_surface_action_family() -> None:
    """監視対象 event が freeze policy の deny surface を取りこぼさない。"""
    events = set(
        _freeze_doc()["generation_publisher_freeze"]["v2"]["scope_definition"]["monitored_events"]
    )
    for required in (
        "RegisterTaskDefinition",
        "DeregisterTaskDefinition",
        "UpdateService",
        "PutTargets",
        "PutRule",
        "RemoveTargets",
        "UpdateFunctionConfiguration",
        "StartBuild",
        "UpdateProject",
    ):
        assert required in events, required


# ── Freeze v2 発効の記録（2026-08-24 apply 後） ─────────────────────────────


def test_freeze_v2_is_active_with_a_recorded_boundary() -> None:
    """state=active と v2.started_at が揃っている（checker が両立を要求する）。"""
    doc = load_freeze(FREEZE)
    publisher = doc["generation_publisher_freeze"]
    assert publisher["state"] == "active"
    assert publisher["v2"]["started_at"] == "2026-08-24T08:24:04Z"
    assert publisher["v2"]["recorded_by"]


def test_boundary_is_after_the_enforcement_apply_not_the_last_change() -> None:
    """境界は「最後の変更時刻」ではなく「deny を確認した時刻」。

    apply（08:19:16Z）より後、かつ検証完了時刻と一致していること。
    """
    v2 = _freeze_doc()["generation_publisher_freeze"]["v2"]
    applied = v2["enforcement_applied"]["applied_at"]
    started = v2["started_at"]
    verified = v2["boundary_verification"]["verified_at"]
    assert applied < started, "境界が apply より前になっている"
    assert started == verified, "境界は検証完了時刻と一致すること"


def test_enforcement_record_pins_the_authorized_plan() -> None:
    """承認された plan の SHA と結果が記録されている（別 plan での適用を後から見分けられる）。"""
    applied = _freeze_doc()["generation_publisher_freeze"]["v2"]["enforcement_applied"]
    assert (
        applied["plan_sha256"] == "3df18fd30a72115804280189c54c8329035e4b2d37ee5c72444e560e2923b338"
    )
    assert applied["apply_result"] == "11 added / 0 changed / 0 destroyed"
    assert len(applied["attached_principals"]["users"]) == 1
    assert len(applied["attached_principals"]["roles"]) == 9


def test_boundary_verification_records_all_nine_checks() -> None:
    """発効判定の 9 項目が実測値つきで残っている。"""
    results = _freeze_doc()["generation_publisher_freeze"]["v2"]["boundary_verification"]["results"]
    assert results["resources_created"].startswith("11/11")
    assert results["principals_attached"].startswith("10/10")
    assert "explicitDeny" in results["deny_simulation"]
    assert "explicitDeny" in results["buildspec_prefix_write"]
    assert "allowed" in results["tfstate_write_preserved"], (
        "state 書き込みの温存が記録されていること"
    )
    assert results["in_flight_builds"] == 0
    assert results["root_mutations"] == 0
    assert "break-glass" in results["root_status"]


# ── P0: Freeze desired-state binding（既定 false による巻き戻しの防止） ──────


def test_desired_var_is_true_while_freeze_is_active() -> None:
    """宣言の state を単一の真実源として注入値を決める。"""
    from activation_freeze_check import desired_freeze_var

    assert desired_freeze_var(FREEZE) == "true"


def test_desired_var_follows_the_declaration(tmp_path: Path) -> None:
    """state が active 以外なら false（宣言と乖離した固定値を返さない）。"""
    from activation_freeze_check import desired_freeze_var

    doc = _freeze_doc()
    doc["generation_publisher_freeze"]["state"] = "pending_v2"
    doc["generation_publisher_freeze"]["v2"]["started_at"] = None
    assert desired_freeze_var(_write(tmp_path, "f.json", doc)) == "false"


def test_guard_injects_the_freeze_var_into_both_plan_paths() -> None:
    """normal plan と adopt-plan が共有する注入配列に freeze 変数が入っている。

    A0.3.2 で両経路は build_live_injection_args を共有しているので、ここ 1 箇所で
    両方が守られる。注入値は宣言から決まる（ハードコードしない）。
    """
    guard = GUARD.read_text(encoding="utf-8")
    stripped = "\n".join(line for line in guard.splitlines() if not line.lstrip().startswith("#"))
    assert stripped.count('"-var=activation_freeze_enabled=$FREEZE_DESIRED_ENABLED"') == 1
    assert "freeze_desired_state_binding" in stripped
    # 注入は配列構築の直前に決まること（順序契約）
    binding = stripped.index("freeze_desired_state_binding\n")
    array = stripped.index("LIVE_INJECTION_TF_ARGS=(")
    assert binding < array


def test_guard_fails_closed_when_the_declaration_is_unreadable() -> None:
    """宣言が読めない / 判定が不正なら die する（fail-open で freeze を溶かさない）。"""
    guard = GUARD.read_text(encoding="utf-8")
    section = guard[guard.index("freeze_desired_state_binding() {") :]
    section = section[: section.index("\n}\n")]
    assert "die" in section
    assert section.count("die") >= 3, "宣言欠落 / checker 欠落 / 判定失敗の 3 経路で die すること"


def test_both_plan_paths_run_the_preservation_check() -> None:
    """normal plan と adopt-plan の両方が assert-plan-preserves-freeze を通る。"""
    guard = GUARD.read_text(encoding="utf-8")
    assert guard.count("assert-plan-preserves-freeze") == 2


def test_freeze_check_runs_after_plan_integrity_validation() -> None:
    """freeze 検査は plan 自体の整合性検査より **後** に走ること。

    2026-08-24 実測: 先に置くと malformed plan に対して JSON parse エラーで死に、
    guard 本来の「plan から HMAC metadata を一意に取得できません」という診断を
    奪ってしまう。freeze 検査は追加の不変条件であって整合性検査の代替ではない。
    """
    guard = GUARD.read_text(encoding="utf-8")
    # normal path: validate_plan（plan 検証）→ freeze 検査
    normal = guard.index('die "plan検証中の差替えを検出しました"')
    normal_freeze = guard.index('--plan "$TMP_ROOT/plan.json"')
    assert normal < normal_freeze
    assert guard.index("hmac_from_plan") < normal_freeze
    # adopt path: ADOPT_VALIDATOR / crosscheck → freeze 検査
    adopt_validator = guard.index('"$ADOPT_VALIDATOR" --plan "$out_dir/adopt-plan.json"')
    adopt_freeze = guard.index('--plan "$out_dir/adopt-plan.json"', adopt_validator + 10)
    assert adopt_validator < adopt_freeze


def test_runbook_requires_the_var_on_guard_free_plan_paths() -> None:
    """guard を通らない IAM targeted plan でも変数の明示と検査を要求する。"""
    text = (ROOT / "docs/runbooks/activation_freeze.md").read_text(encoding="utf-8")
    assert "desired-state binding" in text
    assert "-var=activation_freeze_enabled=true" in text
    assert "assert-plan-preserves-freeze" in text
    assert "destroy 候補" in text


# ── plan 検査ロジックそのものの契約（合成 plan による検出の実証） ────────────


def _plan_doc(changes: list[dict[str, Any]], var: Any = True) -> dict[str, Any]:
    doc: dict[str, Any] = {"resource_changes": changes}
    if var is not None:
        doc["variables"] = {"activation_freeze_enabled": {"value": var}}
    return doc


@pytest.mark.parametrize(
    "address",
    [
        "aws_iam_policy.activation_freeze[0]",
        "aws_iam_user_policy_attachment.activation_freeze_aiia_dev[0]",
        'aws_iam_role_policy_attachment.activation_freeze["teamagent-dev-release-launcher"]',
    ],
)
def test_plan_check_rejects_destroy_of_each_freeze_resource_kind(
    tmp_path: Path, address: str
) -> None:
    """freeze の 3 種のリソースいずれの delete も FATAL（巻き戻しの検出）。"""
    from activation_freeze_check import assert_plan_preserves_freeze

    plan = _write(
        tmp_path,
        "p.json",
        _plan_doc([{"address": address, "change": {"actions": ["delete"]}}]),
    )
    with pytest.raises(FreezeError, match="FREEZE ROLLBACK"):
        assert_plan_preserves_freeze(FREEZE, plan)


def test_plan_check_rejects_replace_not_just_plain_delete(tmp_path: Path) -> None:
    """delete+create（replace）も巻き戻しなので拒否する。"""
    from activation_freeze_check import assert_plan_preserves_freeze

    plan = _write(
        tmp_path,
        "p.json",
        _plan_doc(
            [
                {
                    "address": "aws_iam_policy.activation_freeze[0]",
                    "change": {"actions": ["delete", "create"]},
                }
            ]
        ),
    )
    with pytest.raises(FreezeError, match="FREEZE ROLLBACK"):
        assert_plan_preserves_freeze(FREEZE, plan)


@pytest.mark.parametrize("var", [False, "false", None])
def test_plan_check_requires_the_var_when_freeze_resources_are_in_scope(
    tmp_path: Path, var: Any
) -> None:
    """scope に freeze リソースがあるのに false / 未注入なら FATAL。"""
    from activation_freeze_check import assert_plan_preserves_freeze

    plan = _write(
        tmp_path,
        "p.json",
        _plan_doc(
            [{"address": "aws_iam_policy.activation_freeze[0]", "change": {"actions": ["no-op"]}}],
            var=var,
        ),
    )
    with pytest.raises(FreezeError, match="FREEZE BINDING"):
        assert_plan_preserves_freeze(FREEZE, plan)


@pytest.mark.parametrize("var", [False, None])
def test_plan_check_skips_the_var_when_freeze_is_out_of_scope(tmp_path: Path, var: Any) -> None:
    """freeze リソースを含まない plan（-target / 合成 fixture）には変数を要求しない。

    2026-08-24 実測: 無条件に要求すると guard の既存テスト fixture が全滅する。
    var 欠落の full plan は 11 リソースが delete として現れるため destroy 検査が捕捉する。
    """
    from activation_freeze_check import assert_plan_preserves_freeze

    plan = _write(
        tmp_path,
        "p.json",
        _plan_doc([{"address": "aws_ecs_service.mcp", "change": {"actions": ["update"]}}], var=var),
    )
    assert "適用しない" in assert_plan_preserves_freeze(FREEZE, plan)


def test_plan_check_accepts_a_healthy_plan(tmp_path: Path) -> None:
    """正常な plan は通す（偽陽性の防止）。"""
    from activation_freeze_check import assert_plan_preserves_freeze

    plan = _write(
        tmp_path,
        "p.json",
        _plan_doc(
            [
                {
                    "address": "aws_iam_policy.activation_freeze[0]",
                    "change": {"actions": ["no-op"]},
                }
            ]
        ),
    )
    assert "保持している" in assert_plan_preserves_freeze(FREEZE, plan)


def test_plan_check_ignores_unrelated_destroys(tmp_path: Path) -> None:
    """freeze と無関係なリソースの destroy まで止めない（過剰ブロックの防止）。"""
    from activation_freeze_check import assert_plan_preserves_freeze

    plan = _write(
        tmp_path,
        "p.json",
        _plan_doc(
            [{"address": "aws_iam_policy.something_else", "change": {"actions": ["delete"]}}]
        ),
    )
    assert assert_plan_preserves_freeze(FREEZE, plan)


@pytest.mark.parametrize(
    "address",
    [
        "aws_iam_policy.activation_freeze_lookalike",
        "aws_iam_policy.other_activation_freeze",
        "aws_iam_role_policy_attachment.activation_freeze_other",
        "aws_ecs_task_definition.mcp",
        "aws_s3_bucket.raw_files",
    ],
)
def test_plan_check_does_not_over_block_lookalike_addresses(tmp_path: Path, address: str) -> None:
    """判定を広げすぎると通常の destroy が一切できなくなる（過剰ブロックの防止）。

    prefix 完全一致 + index 付き（`[`）のみを freeze リソースとみなすこと。
    """
    from activation_freeze_check import _is_freeze_resource, assert_plan_preserves_freeze

    assert not _is_freeze_resource(address), address
    plan = _write(
        tmp_path,
        "p.json",
        _plan_doc([{"address": address, "change": {"actions": ["delete"]}}]),
    )
    # 例外を出さないことが本質（メッセージは scope 有無で変わる）
    assert assert_plan_preserves_freeze(FREEZE, plan)


def test_freeze_resource_matcher_is_exact_about_prefixes() -> None:
    """freeze リソース判定の境界を両方向で固定する。"""
    from activation_freeze_check import _is_freeze_resource

    for hit in (
        "aws_iam_policy.activation_freeze",
        "aws_iam_policy.activation_freeze[0]",
        'aws_iam_role_policy_attachment.activation_freeze["x"]',
        "aws_iam_user_policy_attachment.activation_freeze_aiia_dev[0]",
    ):
        assert _is_freeze_resource(hit), hit
    for miss in (
        "aws_iam_policy.activation_freeze_lookalike",
        "aws_iam_policy.other_activation_freeze",
        "aws_iam_role_policy_attachment.activation_freeze_other",
        "aws_ecs_service.mcp",
    ):
        assert not _is_freeze_resource(miss), miss


# ── 変数要求の scope 契約（3 ケースを exact に固定）─────────────────────────
#
# 2026-08-24 実測: 変数を無条件に要求すると guard の合成 plan fixture が全滅した。
# 「入力（変数注入）で守る + 出力（destroy 検出）でも守る」二重化を保ちつつ、
# 誤爆しない境界を次の 3 ケースで固定する。


def test_case1_full_plan_with_freeze_in_scope_and_missing_var_is_fatal(tmp_path: Path) -> None:
    """① full plan + freeze resources present + var missing/false → FATAL。"""
    from activation_freeze_check import assert_plan_preserves_freeze

    for var in (None, False, "false"):
        plan = _write(
            tmp_path,
            f"c1-{var}.json",
            _plan_doc(
                [
                    {
                        "address": 'aws_iam_role_policy_attachment.activation_freeze["r"]',
                        "change": {"actions": ["no-op"]},
                    }
                ],
                var=var,
            ),
        )
        with pytest.raises(FreezeError, match="FREEZE BINDING"):
            assert_plan_preserves_freeze(FREEZE, plan)


def test_case2_targeted_plan_without_freeze_does_not_require_the_var(tmp_path: Path) -> None:
    """② targeted plan + freeze resources absent → 変数要求は発火しない。"""
    from activation_freeze_check import assert_plan_preserves_freeze

    for var in (None, False):
        plan = _write(
            tmp_path,
            f"c2-{var}.json",
            _plan_doc(
                [
                    {
                        "address": "aws_iam_role_policy.runtime_evidence_automation",
                        "change": {"actions": ["update"]},
                    }
                ],
                var=var,
            ),
        )
        assert "適用しない" in assert_plan_preserves_freeze(FREEZE, plan)


@pytest.mark.parametrize("actions", [["delete"], ["delete", "create"], ["create", "delete"]])
@pytest.mark.parametrize("var", [None, False, True])
def test_case3_freeze_destroy_is_fatal_regardless_of_the_var(
    tmp_path: Path, actions: list[str], var: Any
) -> None:
    """③ freeze resource の delete / replace → var の有無に関係なく FATAL。

    destroy 検査が変数検査より先に走ることを、var=True の場合も含めて固定する。
    """
    from activation_freeze_check import assert_plan_preserves_freeze

    plan = _write(
        tmp_path,
        "c3.json",
        _plan_doc(
            [{"address": "aws_iam_policy.activation_freeze[0]", "change": {"actions": actions}}],
            var=var,
        ),
    )
    with pytest.raises(FreezeError, match="FREEZE ROLLBACK"):
        assert_plan_preserves_freeze(FREEZE, plan)
