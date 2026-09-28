"""§M: video_algorithm のレポートS3発行（report_url）配線を外部I/O無しで検証する。

skill は publisher 注入で S3 を差し替え可。未注入かつ VSEO_REPORT_BUCKET 未設定では S3 を叩かず None。
slack_summary は report_url があればそれを案内する（OpenClaw 等 金庫外が読める形）。
"""

from __future__ import annotations

import pytest

from teamagent.skills.video_algorithm.schema import VideoAlgorithmOutput
from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill


def test_publish_uses_injected_publisher() -> None:
    seen: dict[str, str] = {}

    def fake_pub(path: str, *, request_id: str, query: str) -> str | None:
        seen["path"] = path
        seen["query"] = query
        return "https://signed.example/report.html"

    skill = VideoAlgorithmSkill(publisher=fake_pub)
    url = skill._publish("/tmp/r.html", "req1", "kw")
    assert url == "https://signed.example/report.html"
    assert seen == {"path": "/tmp/r.html", "query": "kw"}


def test_publish_no_publisher_no_bucket_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VSEO_REPORT_BUCKET", raising=False)
    skill = VideoAlgorithmSkill()  # publisher 未注入＋bucket未設定 → S3 を叩かず None
    assert skill._publish("/tmp/r.html", "req1", "kw") is None


def test_slack_summary_uses_report_url_when_present() -> None:
    out = VideoAlgorithmOutput(query="kw", report_url="https://signed.example/r.html")
    summary = VideoAlgorithmSkill()._slack_summary(out)
    assert "https://signed.example/r.html" in summary


def test_slack_summary_falls_back_without_url() -> None:
    out = VideoAlgorithmOutput(query="kw")  # report_url None
    summary = VideoAlgorithmSkill()._slack_summary(out)
    assert "添付の HTML レポート" in summary


def test_slack_summary_puts_every_url_at_the_end_of_its_line() -> None:
    """URL の直後に文字を続けない（09-28 本番: 「（タイムライン/…）」まで URL にされ 404）。"""
    import re

    out = VideoAlgorithmOutput(
        query="kw",
        report_url="https://signed.example/r/eyJ.abc",
        slides_url="https://signed.example/r/eyJ.def",
        pptx_url="https://signed.example/r/eyJ.ghi",
    )
    summary = VideoAlgorithmSkill()._slack_summary(out)
    urls = re.findall(r"https://\S+", summary)
    assert urls == [
        "https://signed.example/r/eyJ.abc",
        "https://signed.example/r/eyJ.ghi",
        "https://signed.example/r/eyJ.def",
    ]
    for line in summary.splitlines():
        if "https://" in line:
            assert re.search(r"https://[\x21-\x7e]+$", line), line
    assert "（タイムライン/テロップ位置/ブランド検出/勝ち筋・7日有効）: https://" in summary
