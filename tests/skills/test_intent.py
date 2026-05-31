"""skills/intent.py の自動ルーティング判定テスト。"""

from __future__ import annotations

import pytest

from teamagent.skills.intent import detect_skill, extract_video_url


@pytest.mark.parametrize(
    "msg,expected_url",
    [
        ("この動画分析して https://youtube.com/shorts/abc123", "https://youtube.com/shorts/abc123"),
        ("<https://youtu.be/xYz>", "https://youtu.be/xYz"),
        ("競合 https://www.tiktok.com/@u/video/123 を見て", "https://www.tiktok.com/@u/video/123"),
        ("普通の質問です", None),
    ],
)
def test_extract_video_url(msg: str, expected_url: str | None) -> None:
    assert extract_video_url(msg) == expected_url


@pytest.mark.parametrize(
    "msg",
    [
        "この動画分析して https://youtube.com/shorts/abc123",
        "https://youtu.be/xYz これどう？",
        "https://www.instagram.com/reel/abc/ の構成教えて",
    ],
)
def test_routes_to_video_analysis(msg: str) -> None:
    intent = detect_skill(msg)
    assert intent.skill == "video_analysis"
    assert intent.video_url is not None


@pytest.mark.parametrize(
    "msg",
    [
        "日本ガイシの提案を作って",
        "コスメブランドのTikTok提案のドラフトちょうだい",
        "飲食チェーンの提案骨子を考えて",
        "この案件のたたき台作って",
        "マンダム向けにどう提案すればいい？",
    ],
)
def test_routes_to_proposal_draft(msg: str) -> None:
    assert detect_skill(msg).skill == "proposal_draft"


@pytest.mark.parametrize(
    "msg",
    [
        "この提案レビューして：飲食チェーン向けTikTok…",
        "提案を添削してほしい",
        "この提案の診断おねがい",
        "提案をブラッシュアップして",
    ],
)
def test_routes_to_proposal_review(msg: str) -> None:
    assert detect_skill(msg).skill == "proposal_review"


@pytest.mark.parametrize(
    "msg,client",
    [
        ("日本ガイシのカルテ", "日本ガイシ"),
        ("マンダムの状況教えて", "マンダム"),
        ("日本ガイシって今どう？", "日本ガイシ"),
        ("サントリーの近況は？", "サントリー"),
        ("東芝の温度感どんな感じ", "東芝"),
    ],
)
def test_routes_to_clientkarte_with_client(msg: str, client: str) -> None:
    intent = detect_skill(msg)
    assert intent.skill == "clientkarte"
    assert intent.client_name == client


@pytest.mark.parametrize(
    "msg",
    [
        "飲食店のPR事例を教えて",
        "BtoBで刺さった訴求は？",
        "過去のショート動画提案でうまくいったもの",
        "UGC施策の成功例",
    ],
)
def test_routes_to_search_by_default(msg: str) -> None:
    assert detect_skill(msg).skill == "search"


def test_karte_trigger_without_client_falls_back_to_search() -> None:
    """『状況』だけでクライアント名が無いものは search に倒す (誤爆防止)。"""
    assert detect_skill("状況を教えて").skill == "search"


def test_draft_takes_precedence_over_karte() -> None:
    """提案作成意図はカルテより優先。"""
    assert detect_skill("マンダムの状況を踏まえて提案を作って").skill == "proposal_draft"
