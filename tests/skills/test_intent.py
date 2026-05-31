"""skills/intent.py の自動ルーティング判定テスト。"""

from __future__ import annotations

import pytest

from teamagent.skills.intent import detect_skill


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
