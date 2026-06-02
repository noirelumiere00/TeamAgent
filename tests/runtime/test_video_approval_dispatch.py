"""SkillDispatcher.run_video_approval の単体テスト（シート読取と Gemini をモック）。

Slack 配線の中核（管理番号 → オリエン抽出 → 審査 → Slack 整形）を、
実 API なしで分岐ごとに検証する。
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.runtime.slack_bot import SkillDispatcher
from teamagent.skills.video_approval.schema import (
    ApprovalIssue,
    OrientationBrief,
    VideoApprovalOutput,
)
from teamagent.skills.video_approval.sheet_orientation import OrientationExtract

_DRIVE_URL = "https://drive.google.com/file/d/ABC123ABC123ABC123ABC123x/view"


def _extract(*, has_video: bool = True) -> OrientationExtract:
    return OrientationExtract(
        management_no="E01-01",
        orientation=OrientationBrief(
            product_name="伊藤園",
            main_message="日本茶の未来、ついに動き出した。",
            required_telops=["日本茶の未来戦略3本柱"],
        ),
        video_url=_DRIVE_URL if has_video else " 【E01-01】_03.mp4",
        has_drive_video=has_video,
    )


class _FakeSkill:
    def __init__(self, out: VideoApprovalOutput) -> None:
        self._out = out
        self.calls: list[Any] = []

    def run(self, input_obj: Any, ctx: Any) -> VideoApprovalOutput:
        self.calls.append(input_obj)
        return self._out


class _RaisingSkill:
    def __init__(self, err: str) -> None:
        self._err = err

    def run(self, input_obj: Any, ctx: Any) -> VideoApprovalOutput:
        raise RuntimeError(self._err)


def _dispatcher() -> SkillDispatcher:
    # router=object() で SkillRouter 構築をスキップ（run_video_approval は router 不使用）
    return SkillDispatcher(router=object())  # type: ignore[arg-type]


async def test_happy_path_returns_slack_fb(monkeypatch: pytest.MonkeyPatch) -> None:
    out = VideoApprovalOutput(
        verdict="要修正",
        summary="必須テロップ欠落の可能性",
        issues=[
            ApprovalIssue(
                category="必須要素",
                severity="must_fix",
                timecode="0:03",
                detail="「日本茶の未来戦略3本柱」未確認",
            )
        ],
        feedback_text="…",
    )
    disp = _dispatcher()
    monkeypatch.setattr(disp, "_extract_orientation", lambda s, m, r: _extract(has_video=True))
    disp._skill_cache["video_approval"] = _FakeSkill(out)

    text = await disp.run_video_approval("E01-01", "rid", "uid", sheet_id="sid")
    assert "AI一次チェック" in text
    assert "E01-01" in text
    assert "判定: *要修正*" in text
    assert "🔴 *必須要素*: 「日本茶の未来戦略3本柱」未確認 (0:03)" in text
    assert f"<{_DRIVE_URL}|納品動画>" in text


async def test_no_drive_video_prompts_for_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    disp = _dispatcher()
    monkeypatch.setattr(disp, "_extract_orientation", lambda s, m, r: _extract(has_video=False))
    text = await disp.run_video_approval("E01-01", "rid", "uid", sheet_id="sid")
    assert "納品動画" in text and "入っていません" in text


async def test_unknown_management_no(monkeypatch: pytest.MonkeyPatch) -> None:
    disp = _dispatcher()
    monkeypatch.setattr(disp, "_extract_orientation", lambda s, m, r: None)
    text = await disp.run_video_approval("ZZZ-99", "rid", "uid", sheet_id="sid")
    assert "見つかりません" in text


async def test_requires_management_no() -> None:
    disp = _dispatcher()
    text = await disp.run_video_approval(None, "rid", "uid", sheet_id="sid")
    assert "管理番号" in text


async def test_requires_sheet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_APPROVAL_SHEET_ID", raising=False)
    disp = _dispatcher()
    text = await disp.run_video_approval("E01-01", "rid", "uid", sheet_id=None)
    assert "スプレッドシート" in text


async def test_sheet_id_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_APPROVAL_SHEET_ID", "envsheet")
    disp = _dispatcher()
    seen: dict[str, str] = {}

    def _fake_extract(sheet: str, mno: str, rid: str) -> OrientationExtract:
        seen["sheet"] = sheet
        return _extract(has_video=True)

    monkeypatch.setattr(disp, "_extract_orientation", _fake_extract)
    disp._skill_cache["video_approval"] = _FakeSkill(
        VideoApprovalOutput(verdict="OK", summary="問題なし", issues=[], feedback_text="…")
    )
    await disp.run_video_approval("E01-01", "rid", "uid", sheet_id=None)
    assert seen["sheet"] == "envsheet"  # env フォールバックが効く


async def test_gemini_error_is_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    disp = _dispatcher()
    monkeypatch.setattr(disp, "_extract_orientation", lambda s, m, r: _extract(has_video=True))
    disp._skill_cache["video_approval"] = _RaisingSkill("GEMINI_NOT_CONFIGURED")
    text = await disp.run_video_approval("E01-01", "rid", "uid", sheet_id="sid")
    assert "Gemini" in text


async def test_extract_error_is_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    disp = _dispatcher()

    def _boom(s: str, m: str, r: str) -> OrientationExtract:
        raise RuntimeError("invalid_scope: Bad Request")

    monkeypatch.setattr(disp, "_extract_orientation", _boom)
    text = await disp.run_video_approval("E01-01", "rid", "uid", sheet_id="sid")
    assert "OAuth" in text or "権限" in text
