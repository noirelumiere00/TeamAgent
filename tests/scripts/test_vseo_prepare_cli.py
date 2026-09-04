"""vseo_prepare CLI の --max 上限契約（dispatcher の n_per_kw 上限と一致させる）。

31 以上を指定すると取得開始後に search_tiktok の fail-fast
（TIKTOK_MEDIA_JOB_FAILED: ValueError）で落ちるため、引数解析の直後に弾く。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import scripts.vseo_prepare as cli
from teamagent.media.contracts import TIKTOK_N_PER_KW_MAX
from teamagent.skills.vseo.prepare import VseoPrepResult


def _result(project_dir: Path) -> VseoPrepResult:
    return VseoPrepResult(
        project_dir=str(project_dir),
        keywords=["新宿 ランチ"],
        counts={"新宿 ランチ": 30},
        multi_kw_count=0,
        covers_saved=0,
        failed_keywords=[],
    )


def _run(monkeypatch: Any, tmp_path: Path, argv: list[str]) -> list[int]:
    """CLI を実行し、prepare_vseo_data が受け取った max_videos を記録して返す。"""
    seen: list[int] = []

    def fake_prepare(keywords: list[str], out: Path, **kwargs: Any) -> VseoPrepResult:
        max_videos = int(kwargs["max_videos"])
        # 本番の失敗モード（media_job.search_tiktok の fail-fast）を再現する
        if not 1 <= max_videos <= TIKTOK_N_PER_KW_MAX:
            raise ValueError(
                f"TikTok n_per_kw={max_videos} is outside the dispatcher limit "
                f"(1..{TIKTOK_N_PER_KW_MAX})"
            )
        seen.append(max_videos)
        return _result(out)

    monkeypatch.setattr(cli, "prepare_vseo_data", fake_prepare)
    monkeypatch.setattr(
        "sys.argv", ["vseo_prepare.py", "--out", str(tmp_path), "--kw", "新宿 ランチ", *argv]
    )
    cli.main()
    return seen


def test_max_at_dispatcher_limit_passes(monkeypatch: Any, tmp_path: Path) -> None:
    assert _run(monkeypatch, tmp_path, ["--max", str(TIKTOK_N_PER_KW_MAX)]) == [TIKTOK_N_PER_KW_MAX]


def test_max_default_is_dispatcher_limit(monkeypatch: Any, tmp_path: Path) -> None:
    assert _run(monkeypatch, tmp_path, []) == [TIKTOK_N_PER_KW_MAX]


@pytest.mark.parametrize("over", [TIKTOK_N_PER_KW_MAX + 1, 50, 120, 0])
def test_max_outside_dispatcher_limit_exits_before_fetching(
    monkeypatch: Any, tmp_path: Path, over: int, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, tmp_path, ["--max", str(over)])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert f"--max={over}" in err
    assert f"1〜{TIKTOK_N_PER_KW_MAX}" in err
