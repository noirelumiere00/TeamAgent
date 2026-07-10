"""AiLa メール機能 — 複数ユーザー / runner 層の「多数シチュエーション」検証マトリクス。

単一ユーザーのスキル検証(test_mail_feature_scenarios.py)を補完し、
「テストユーザーが複数いた場合」のユーザー解決・除外・per-user 分離・耐障害を検証する。
runner(scripts/run_morning_digest_fargate.py)は package でないため importlib でロード。
"""

from __future__ import annotations

# シナリオ ID（test_M01 等）は大文字を意図的に使うため N802 を無効化。
# ruff: noqa: N802
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from teamagent.skills.morning_digest.schema import MorningDigestOutput

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_morning_digest_fargate.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("run_md_multiuser_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_md_multiuser_under_test"] = module
    spec.loader.exec_module(module)
    return module


mod = _load()
SKILLMOD = "teamagent.skills.morning_digest.skill"


def _make_fake_skill(recorder: dict[str, list[str]], behavior: dict[str, str]) -> type:
    """main() が new する MorningDigestSkill を差し替えるフェイク。

    run() は ctx.user_email を recorder に記録し、behavior に応じて
    成功 / PermissionError(未連携) / 例外 を返す。
    """

    class _FakeSkill:
        def __init__(self, token_store: Any = None, **_: Any) -> None:
            self._ts = token_store

        def run(self, _inp: Any, ctx: Any) -> MorningDigestOutput:
            email = ctx.metadata.get("user_email")
            recorder["run"].append(email)
            b = behavior.get(email, "ok")
            if b == "permission":
                raise PermissionError("メール連携が未完了です")
            if b == "error":
                raise RuntimeError("boom")
            return MorningDigestOutput(user_email_masked=f"{email[:1]}***")

    return _FakeSkill


def _patch_main(monkeypatch, users, behavior=None, deliver_fail=None):
    """main() の依存(ユーザー解決・token store・skill・配信)を差し替える。"""
    recorder: dict[str, list[str]] = {"run": [], "deliver": []}
    behavior = behavior or {}
    deliver_fail = set(deliver_fail or [])
    monkeypatch.setattr(mod, "_resolve_target_users", lambda: list(users))
    monkeypatch.setattr(mod, "_build_token_store", lambda: object())
    monkeypatch.setattr(mod, "_format_block_kit", lambda digest, email: ("t", []))
    monkeypatch.setattr(f"{SKILLMOD}.MorningDigestSkill", _make_fake_skill(recorder, behavior))

    async def _fake_deliver(email, text, blocks):
        recorder["deliver"].append(email)
        # 新契約（v0.3 Task5）: (delivered, im_channel) を返す。
        ok = email not in deliver_fail
        return (ok, "D_IM" if ok else None)

    monkeypatch.setattr(mod, "_deliver_to_slack", _fake_deliver)
    return recorder


U1, U2, U3 = "a@vectorinc.co.jp", "b@vectorinc.co.jp", "c@vectorinc.co.jp"


# ════════════ L. ユーザー解決・除外（runner）════════════


def test_M01_explicit_users_lowercased(monkeypatch):
    monkeypatch.setenv("MORNING_DIGEST_USERS", "A@VectorInc.co.jp, b@vectorinc.co.jp , ")
    monkeypatch.delenv("MORNING_DIGEST_EXCLUDE", raising=False)
    assert mod._resolve_target_users() == ["a@vectorinc.co.jp", "b@vectorinc.co.jp"]


def test_M02_empty_users_falls_back_to_rds(monkeypatch):
    monkeypatch.delenv("MORNING_DIGEST_USERS", raising=False)
    monkeypatch.delenv("MORNING_DIGEST_EXCLUDE", raising=False)
    monkeypatch.setattr(mod, "_fetch_connected_users_from_rds", lambda: [U1, U2, U3])
    assert mod._resolve_target_users() == [U1, U2, U3]


def test_M03_exclude_removes_multiple_case_insensitive(monkeypatch):
    monkeypatch.setenv("MORNING_DIGEST_EXCLUDE", "B@vectorinc.co.jp, c@vectorinc.co.jp")
    assert mod._apply_exclude([U1, U2, U3]) == [U1]


def test_M04_no_exclude_keeps_all(monkeypatch):
    monkeypatch.delenv("MORNING_DIGEST_EXCLUDE", raising=False)
    assert mod._apply_exclude([U1, U2, U3]) == [U1, U2, U3]


def test_M05_explicit_plus_exclude(monkeypatch):
    monkeypatch.setenv("MORNING_DIGEST_USERS", f"{U1},{U2},{U3}")
    monkeypatch.setenv("MORNING_DIGEST_EXCLUDE", f"{U2},{U3}")
    assert mod._resolve_target_users() == [U1]


def test_M06_rds_plus_exclude(monkeypatch):
    monkeypatch.delenv("MORNING_DIGEST_USERS", raising=False)
    monkeypatch.setenv("MORNING_DIGEST_EXCLUDE", U3)
    monkeypatch.setattr(mod, "_fetch_connected_users_from_rds", lambda: [U1, U2, U3])
    assert mod._resolve_target_users() == [U1, U2]


def test_M07_owner_not_excluded_unless_listed(monkeypatch):
    monkeypatch.setenv("MORNING_DIGEST_EXCLUDE", f"{U2},{U3}")  # owner=U1 は対象外
    assert U1 in mod._apply_exclude([U1, U2, U3])


def test_M08_zero_targets_is_noop(monkeypatch):
    monkeypatch.setattr(mod, "_resolve_target_users", lambda: [])
    assert mod.main() == 0


# ════════════ M. 複数ユーザーの分離・耐障害（main loop）════════════


def test_M09_three_users_each_get_their_own_digest(monkeypatch, capsys):
    rec = _patch_main(monkeypatch, [U1, U2, U3])
    assert mod.main() == 0
    assert rec["run"] == [U1, U2, U3]  # 各ユーザーで1回ずつ・本人のemailで
    assert rec["deliver"] == [U1, U2, U3]
    assert '"delivered": 3' in capsys.readouterr().out


def test_M10_unconnected_user_skipped_others_continue(monkeypatch, capsys):
    rec = _patch_main(monkeypatch, [U1, U2, U3], behavior={U2: "permission"})
    mod.main()
    out = capsys.readouterr().out
    assert rec["run"] == [U1, U2, U3]  # 全員試行
    assert rec["deliver"] == [U1, U3]  # 未連携の U2 には配信しない
    assert '"skipped": 1' in out and '"delivered": 2' in out


def test_M11_one_user_exception_others_continue(monkeypatch, capsys):
    rec = _patch_main(monkeypatch, [U1, U2, U3], behavior={U2: "error"})
    mod.main()
    out = capsys.readouterr().out
    assert rec["deliver"] == [U1, U3]
    assert '"errors": 1' in out and '"delivered": 2' in out


def test_M12_per_user_isolation_no_cross_user_email(monkeypatch):
    rec = _patch_main(monkeypatch, [U1, U2, U3])
    mod.main()
    # 各 run() は「その回のユーザー1人」の email だけを受け取る（混線なし）
    assert rec["run"] == [U1, U2, U3]
    assert len(set(rec["run"])) == 3


def test_M13_delivery_targets_each_owner(monkeypatch):
    rec = _patch_main(monkeypatch, [U1, U2])
    mod.main()
    # 配信は skill.run と同じ本人 email へ（取り違えなし）
    assert rec["deliver"] == rec["run"] == [U1, U2]


def test_M14_delivery_failure_counts_as_error(monkeypatch, capsys):
    _patch_main(monkeypatch, [U1, U2], deliver_fail={U2})
    mod.main()
    out = capsys.readouterr().out
    assert '"delivered": 1' in out and '"errors": 1' in out


def test_M15_duplicate_emails_do_not_crash(monkeypatch, capsys):
    rec = _patch_main(monkeypatch, [U1, U1])
    assert mod.main() == 0
    assert rec["run"] == [U1, U1]  # 重複しても破綻しない


def test_M16_scale_many_users_all_independent(monkeypatch, capsys):
    users = [f"u{i}@vectorinc.co.jp" for i in range(20)]
    rec = _patch_main(monkeypatch, users)
    assert mod.main() == 0
    assert rec["run"] == users and rec["deliver"] == users
    assert '"delivered": 20' in capsys.readouterr().out


# ════════════ N. スキル層の per-user トークン分離（G1）════════════


def test_M17_skill_resolves_own_token_per_user():
    """skill は ctx.user_email 本人のトークンだけを引く（他人のトークンを使わない）。"""
    from teamagent.skills.morning_digest.skill import MorningDigestSkill

    asked: list[str] = []

    class _RecordingStore:
        def __init__(self, present: set[str]):
            self._present = present

        def get(self, email: str) -> Any:
            asked.append(email)
            return object() if email.lower() in self._present else None

    skill = MorningDigestSkill(token_store=_RecordingStore({U1}))
    # 本人(U1)は解決できる
    skill._resolve_token(U1)
    # 別人(U2・未連携)は None → fail-closed
    with pytest.raises(PermissionError):
        skill._resolve_token(U2)
    assert asked == [U1, U2]  # 渡された email の分だけ・取り違えなし


def test_M21_concurrency_processes_all_users(monkeypatch, capsys):
    """MORNING_DIGEST_CONCURRENCY>1 で並列実行しても全員を処理（順不同可・取りこぼしなし）。"""
    import teamagent.adapters.bedrock_client as bc

    monkeypatch.setenv("MORNING_DIGEST_CONCURRENCY", "4")
    monkeypatch.setattr(bc.BedrockClient, "from_env", classmethod(lambda cls: object()))
    users = [f"u{i}@vectorinc.co.jp" for i in range(12)]
    rec = _patch_main(monkeypatch, users)
    assert mod.main() == 0
    assert sorted(rec["run"]) == sorted(users)  # 並列でも全員 skill.run
    assert sorted(rec["deliver"]) == sorted(users)  # 全員に配信
    assert '"delivered": 12' in capsys.readouterr().out


def test_M20_delivery_exception_is_contained(monkeypatch, capsys):
    """配信中に1人で例外が出ても main 全体は落ちず、後続ユーザーまで処理する（耐障害）。"""
    rec = _patch_main(monkeypatch, [U1, U2, U3])

    async def _boom(email, text, blocks):
        rec["deliver"].append(email)
        if email == U2:
            raise RuntimeError("slack render boom")
        return (True, "D_IM")

    monkeypatch.setattr(mod, "_deliver_to_slack", _boom)
    assert mod.main() == 0  # クラッシュしない
    out = capsys.readouterr().out
    assert rec["deliver"] == [U1, U2, U3]  # U2 で例外でも U3 まで到達
    assert '"delivered": 2' in out and '"errors": 1' in out


def test_M19_error_log_masks_email_no_pii(monkeypatch, capsys):
    """1 人の処理で例外時、ログに生メールアドレスを出さない（マスクのみ・G3/G7）。"""
    raw = "tanaka.taro@vectorinc.co.jp"
    _patch_main(monkeypatch, [raw], behavior={raw: "error"})
    mod.main()
    err = capsys.readouterr().err
    assert raw not in err  # 生アドレスは出ない
    assert "t***@vectorinc.co.jp" in err  # マスク版で記録


def test_M18_missing_user_email_blocks_run(monkeypatch):
    from teamagent.skills.base import SkillContext
    from teamagent.skills.morning_digest.schema import MorningDigestInput
    from teamagent.skills.morning_digest.skill import MorningDigestSkill

    skill = MorningDigestSkill(token_store=object())
    with pytest.raises(PermissionError, match="user_email"):
        skill.run(MorningDigestInput(), SkillContext(request_id="r", metadata={}))
