"""VideoApprovalSkill の単体テスト (Gemini と動画DLをモック)。

実 API / 実動画なしで、オリエン照合FBの構造化を検証する。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gemini_client import GeminiResponse
from teamagent.skills.base import SkillContext
from teamagent.skills.video_approval.schema import OrientationBrief, VideoApprovalInput
from teamagent.skills.video_approval.skill import VideoApprovalSkill

_LLM_OK = """### 一次FB
- **総合**: OK — オリエンの必須要素を満たしています。
- 良かった点: 冒頭フックが明確。

```json
{"verdict": "OK", "summary": "必須要素OK・問題なし", "issues": []}
```
"""

_LLM_NG = """### 一次FB
- **総合**: 要修正 — 必須テロップ欠落とNG表現あり。

1. 必須テロップ「期間限定」が入っていません → 0:05付近に追加してください
2. 「絶対に痩せる」は薬機法NG → 表現を変更してください

```json
{
  "verdict": "要修正",
  "summary": "必須テロップ欠落 + NG表現1件",
  "issues": [
    {"category": "必須要素", "severity": "must_fix", "timecode": "0:05",
     "detail": "必須テロップ『期間限定』が無い", "fix": "冒頭に追加"},
    {"category": "NG事項", "severity": "must_fix", "timecode": "0:12",
     "detail": "『絶対に痩せる』は薬機法NG", "fix": "表現を変更"}
  ]
}
```
"""


def _resp(text: str) -> GeminiResponse:
    return GeminiResponse(
        text=text,
        input_tokens=5000,
        output_tokens=300,
        cost_usd=0.001,
        model_id="gemini-2.5-flash",
        latency_ms=8000,
    )


@pytest.fixture
def orientation() -> OrientationBrief:
    return OrientationBrief(
        product_name="サンプルコスメ",
        target="20-30代女性",
        main_message="毛穴ケアの新習慣",
        required_telops=["期間限定", "#PR"],
        ng_items=["絶対に痩せる", "医薬品的効能"],
        duration_spec="30秒以内",
        format_spec="縦型9:16",
    )


def test_approval_ok(orientation: OrientationBrief) -> None:
    gemini = MagicMock()
    gemini.analyze_video_bytes.return_value = _resp(_LLM_OK)
    skill = VideoApprovalSkill(gemini=gemini, drive_downloader=lambda u: (b"vid", "video/mp4"))
    out = skill.run(
        VideoApprovalInput(
            orientation=orientation,
            video_url="https://drive.google.com/file/d/ABC123ABC123ABC123ABC123x/view",
        ),
        ctx=SkillContext(),
    )
    assert out.verdict == "OK"
    assert out.issues == []
    assert "```json" not in out.feedback_text  # JSON ブロックは本文から除去
    assert out.total_cost_usd == pytest.approx(0.001)


def test_approval_with_issues(orientation: OrientationBrief) -> None:
    gemini = MagicMock()
    gemini.analyze_video_bytes.return_value = _resp(_LLM_NG)
    skill = VideoApprovalSkill(gemini=gemini, drive_downloader=lambda u: (b"vid", "video/mp4"))
    out = skill.run(
        VideoApprovalInput(
            orientation=orientation,
            video_url="https://drive.google.com/file/d/ABC123ABC123ABC123ABC123x/view",
        ),
        ctx=SkillContext(),
    )
    assert out.verdict == "要修正"
    assert len(out.issues) == 2
    cats = {i.category for i in out.issues}
    assert cats == {"必須要素", "NG事項"}
    must = [i for i in out.issues if i.severity == "must_fix"]
    assert len(must) == 2
    assert must[0].timecode == "0:05"


def test_drive_url_uses_drive_downloader(orientation: OrientationBrief) -> None:
    gemini = MagicMock()
    gemini.analyze_video_bytes.return_value = _resp(_LLM_OK)
    drive_dl = MagicMock(return_value=(b"drivevid", "video/mp4"))
    other_dl = MagicMock()
    skill = VideoApprovalSkill(gemini=gemini, drive_downloader=drive_dl, video_downloader=other_dl)
    skill.run(
        VideoApprovalInput(
            orientation=orientation,
            video_url="https://drive.google.com/file/d/ABC123ABC123ABC123ABC123x/view",
        ),
        ctx=SkillContext(),
    )
    drive_dl.assert_called_once()
    other_dl.assert_not_called()
    gemini.analyze_video_bytes.assert_called_once()


def test_youtube_uses_file_uri(orientation: OrientationBrief) -> None:
    gemini = MagicMock()
    gemini.analyze_video_url.return_value = _resp(_LLM_OK)
    skill = VideoApprovalSkill(gemini=gemini)
    skill.run(
        VideoApprovalInput(orientation=orientation, video_url="https://youtu.be/abc"),
        ctx=SkillContext(),
    )
    gemini.analyze_video_url.assert_called_once()
    gemini.analyze_video_bytes.assert_not_called()


def test_orientation_in_prompt(orientation: OrientationBrief) -> None:
    """オリエンの必須テロップ/NG事項が Gemini プロンプトに渡る。"""
    gemini = MagicMock()
    gemini.analyze_video_bytes.return_value = _resp(_LLM_OK)
    skill = VideoApprovalSkill(gemini=gemini, drive_downloader=lambda u: (b"v", "video/mp4"))
    skill.run(
        VideoApprovalInput(
            orientation=orientation,
            video_url="https://drive.google.com/file/d/ABC123ABC123ABC123ABC123x/view",
        ),
        ctx=SkillContext(),
    )
    prompt = gemini.analyze_video_bytes.call_args.kwargs["prompt"]
    assert "期間限定" in prompt  # 必須テロップ
    assert "絶対に痩せる" in prompt  # NG事項
    assert "30秒以内" in prompt  # 尺指定


def test_no_url_returns_check_required(orientation: OrientationBrief) -> None:
    skill = VideoApprovalSkill(gemini=MagicMock())
    out = skill.run(VideoApprovalInput(orientation=orientation, video_url=None), ctx=SkillContext())
    assert out.verdict == "確認要"


def test_malformed_json_still_returns_feedback(orientation: OrientationBrief) -> None:
    """JSON が壊れていても FB 本文は返す (fail-safe)。"""
    gemini = MagicMock()
    gemini.analyze_video_bytes.return_value = _resp("### 一次FB\n総合: 要修正\n（JSON無し）")
    skill = VideoApprovalSkill(gemini=gemini, drive_downloader=lambda u: (b"v", "video/mp4"))
    out = skill.run(
        VideoApprovalInput(
            orientation=orientation,
            video_url="https://drive.google.com/file/d/ABC123ABC123ABC123ABC123x/view",
        ),
        ctx=SkillContext(),
    )
    assert "一次FB" in out.feedback_text
    # JSON 無し → issues 空 → verdict は OK にフォールバック (issuesベース)
    assert out.issues == []
