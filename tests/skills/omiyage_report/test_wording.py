"""便A-2: 利用者向け文言の固定（busy / needs_input / 受付）。

固定する不変量:
  1. 利用者向け文（busy・受付・needs_input）にツール名（``omiyage_`` を含む識別子）が出ない
  2. busy 文は「順番待ち N 番目・目安あと約 M 分」で、秒数の再送指示を含まない
  3. needs_input の末尾に骨子（文章）へ切り替える逃げ道 1 行がある
  4. 受付文の所要目安は固定の「10〜30 分」でなく依頼内容と環境から算出した分数
  5. 順番待ちの N 番目は最初に断られた順（同じ人の再送で位置は動かない・受付で外れる）
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.base import SkillContext
from teamagent.skills.omiyage_report.preflight import (
    OUTLINE_FALLBACK_LINE,
    build_accepted_message,
    build_busy_message,
    build_needs_input_message,
    estimate_duration,
    run_preflight,
)
from teamagent.skills.omiyage_report.schema import OmiyageReportSubmitInput
from teamagent.skills.omiyage_report.skill import JobAdmission, OmiyageReportSubmitSkill

_TOOL_NAME = re.compile(r"omiyage_[a-z_]+|proposal_[a-z_]+|tiktok_[a-z_]+")


def _input(brand: str = "エムキュア") -> OmiyageReportSubmitInput:
    return OmiyageReportSubmitInput(brand=brand, competitors=["ラサーナ"], keywords=["ヘアケア"])


def _ctx(user_id: str) -> SkillContext:
    return SkillContext(request_id=f"req-{user_id}", user_id=user_id, metadata={})


class _NeverRunLauncher:
    """背景スレッドを起動しない（枠を占有したままにして busy を作る）。"""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, target: Any, name: str) -> None:
        self.count += 1


def _skill(admission: JobAdmission, launcher: _NeverRunLauncher) -> OmiyageReportSubmitSkill:
    return OmiyageReportSubmitSkill(
        store=ProposalJobStore(table_name="", memory={}),
        thread_launcher=launcher,
        admission=admission,
        heartbeat_seconds=0,
        analysis_per_axis=10,
    )


# ── 1. ツール名を出さない ──────────────────────────────────────────────


def test_user_facing_messages_never_contain_tool_names() -> None:
    estimate = estimate_duration(axes=3, analysis_videos=25, analysis_concurrency=2)
    partial = OmiyageReportSubmitInput(brand="エムキュア")
    messages = [
        build_busy_message(running=3, position=2, wait_minutes=17),
        build_accepted_message(_input(), estimate),
        build_needs_input_message(partial, run_preflight(partial)),
    ]
    for message in messages:
        assert "omiyage_" not in message
        assert _TOOL_NAME.search(message) is None, message


# ── 2. busy 文 ───────────────────────────────────────────────────────────


def test_busy_message_states_queue_position_and_minutes_not_seconds() -> None:
    message = build_busy_message(running=3, position=2, wait_minutes=17)
    assert "いまお土産資料を3件作成中" in message
    assert "順番待ち 2 番目" in message
    assert "目安あと約 17 分" in message
    assert "まだ着手していません" in message
    assert "約 17 分後に同じ内容でもう一度" in message
    assert "『まだ？』で確認できます" in message
    assert "秒" not in message
    assert "omiyage_report_status" not in message


def test_busy_message_never_promises_zero_minutes() -> None:
    assert "目安あと約 1 分" in build_busy_message(running=1, position=1, wait_minutes=0)


# ── 3. needs_input の逃げ道 ─────────────────────────────────────────────


def test_needs_input_ends_with_outline_fallback_line() -> None:
    partial = OmiyageReportSubmitInput(brand="サントリー天然水")
    message = build_needs_input_message(partial, run_preflight(partial))
    lines = message.split("\n")
    assert lines[-1] == OUTLINE_FALLBACK_LINE
    assert "『骨子で』とだけ返信してください" in lines[-1]
    # 回答欄（コピーして返す部分）の後ろに置く＝営業が回答欄に混ぜて送らない
    assert lines.index("指示：この内容で資料を作成してください") < len(lines) - 1


# ── 4. 所要目安の算出 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("axes", "videos", "concurrency", "raw", "rounded"),
    [
        # 3 軸 ×2 分 + ceil(25/2)=13 ×2.5 分 + 1 分 = 39.5 → 40
        (3, 25, 2, 39.5, 40),
        # 並列 4 にすると ceil(25/4)=7 ×2.5 = 17.5 → 6+17.5+1 = 24.5 → 25
        (3, 25, 4, 24.5, 25),
        # 動画分析なし（1 軸だけ）でも最低 5 分と言う
        (1, 0, 2, 3.0, 5),
        # 5 軸（KW3+ブランド+競合1）・並列 2
        (5, 25, 2, 43.5, 45),
    ],
)
def test_duration_estimate_model(
    axes: int, videos: int, concurrency: int, raw: float, rounded: int
) -> None:
    estimate = estimate_duration(
        axes=axes, analysis_videos=videos, analysis_concurrency=concurrency
    )
    assert estimate.minutes == pytest.approx(raw)
    assert estimate.rounded_minutes == rounded


def test_accepted_message_uses_request_axes_and_env_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMIYAGE_VA_MAX_VIDEOS", "8")
    monkeypatch.setenv("OMIYAGE_VA_CONCURRENCY", "4")
    launcher = _NeverRunLauncher()
    skill = _skill(JobAdmission(2), launcher)
    input = OmiyageReportSubmitInput(
        brand="エムキュア", competitors=["ラサーナ", "いち髪"], keywords=["ヘアケア", "シャンプー"]
    )
    accepted = skill.run(input, _ctx("U1"))
    assert accepted.status == "queued"
    # 軸 = KW2 + ブランド1 + 競合2 = 5、分析本数 = min(env 8, 10×2+5) = 8、並列 4
    # 5×2 + ceil(8/4)=2 ×2.5 + 1 = 16 → 20 分
    assert "目安 約 20 分（TikTok 取得 5 軸＋動画分析 最大 8 本）" in accepted.message
    assert "10〜30" not in accepted.message


# ── 5. 順番待ちの N 番目・あと M 分 ─────────────────────────────────────────


def test_busy_position_counts_first_rejection_order_and_clears_on_accept() -> None:
    now = [1000.0]
    admission = JobAdmission(1, clock=lambda: now[0])
    launcher = _NeverRunLauncher()
    skill = _skill(admission, launcher)

    assert skill.run(_input("A"), _ctx("U1")).status == "queued"
    first = skill.run(_input("B"), _ctx("U2"))
    second = skill.run(_input("C"), _ctx("U3"))
    again = skill.run(_input("B"), _ctx("U2"))  # 同じ人の再送は位置を変えない

    assert first.status == second.status == again.status == "busy"
    assert "順番待ち 1 番目" in first.message
    assert "順番待ち 2 番目" in second.message
    assert "順番待ち 1 番目" in again.message
    assert first.job_id == second.job_id == ""

    # 1 本目が終わって U2 が受付されると、U3 の順番が繰り上がる
    admission.release()
    assert skill.run(_input("B"), _ctx("U2")).status == "queued"
    third = skill.run(_input("C"), _ctx("U3"))
    assert third.status == "busy"
    assert "順番待ち 1 番目" in third.message


def test_busy_wait_minutes_follow_oldest_running_job_and_retry_after_matches() -> None:
    now = [0.0]
    admission = JobAdmission(1, clock=lambda: now[0])
    launcher = _NeverRunLauncher()
    skill = _skill(admission, launcher)

    accepted = skill.run(_input("A"), _ctx("U1"))
    assert accepted.status == "queued"
    assert "目安 約 40 分" in accepted.message  # 3 軸・25 本・並列 2 → 39.5 分

    now[0] += 30 * 60  # 30 分経過
    busy = skill.run(_input("B"), _ctx("U2"))
    assert busy.status == "busy"
    # 残り = 39.5 − 30 = 9.5 → 切り上げ 10 分
    assert "目安あと約 10 分" in busy.message
    assert busy.retry_after_seconds == 10 * 60

    # 2 番目の人は「最古の枠が空く 9.5 分」＋「1 ジョブぶん 39.5 分」= 49 分
    later = skill.run(_input("C"), _ctx("U3"))
    assert "順番待ち 2 番目" in later.message
    assert "目安あと約 49 分" in later.message


def test_stale_waiters_drop_out_of_the_queue() -> None:
    now = [0.0]
    admission = JobAdmission(1, clock=lambda: now[0])
    skill = _skill(admission, _NeverRunLauncher())

    assert skill.run(_input("A"), _ctx("U1")).status == "queued"
    assert "順番待ち 1 番目" in skill.run(_input("B"), _ctx("U2")).message
    now[0] += 16 * 60  # U2 は 15 分以上再送していない＝諦めた扱い
    assert "順番待ち 1 番目" in skill.run(_input("C"), _ctx("U3")).message
