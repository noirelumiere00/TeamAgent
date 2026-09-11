"""注意文 4 点＋対外利用の可否が、添付コメントから 1 行も落ちないこと。

1 行消すと赤くなるように、``REQUIRED_NOTICES`` の各要素を個別に検査する
（「4 件ある」だけの検査では、入れ替わっても緑のまま通ってしまう）。
"""

from __future__ import annotations

import pytest

from teamagent.skills.clip_proposal.notices import (
    NOTICE_COMMUNITY,
    NOTICE_DRAFT,
    NOTICE_DURATION,
    NOTICE_EXTERNAL_USE,
    NOTICE_STILL_IMAGE,
    REQUIRED_NOTICES,
    build_delivery_comment,
    build_notices,
    has_all_required_notices,
)


def test_required_notices_cover_the_four_agreed_points_plus_external_use() -> None:
    assert NOTICE_DRAFT in REQUIRED_NOTICES
    assert NOTICE_DURATION in REQUIRED_NOTICES
    assert NOTICE_COMMUNITY in REQUIRED_NOTICES
    assert NOTICE_STILL_IMAGE in REQUIRED_NOTICES
    assert NOTICE_EXTERNAL_USE in REQUIRED_NOTICES


@pytest.mark.parametrize(
    ("notice", "marker"),
    [
        (NOTICE_DRAFT, "今時点のイメージ"),
        (NOTICE_DRAFT, "デザインはこれから"),
        (NOTICE_DURATION, "編集で"),
        (NOTICE_DURATION, "尺は数秒短くなります"),
        (NOTICE_COMMUNITY, "（未検証）"),
        (NOTICE_COMMUNITY, "仮定値"),
        (NOTICE_STILL_IMAGE, "静止画"),
        (NOTICE_EXTERNAL_USE, "対外利用の可否"),
    ],
)
def test_each_notice_states_its_point(notice: str, marker: str) -> None:
    assert marker in notice


def test_delivery_comment_carries_every_notice() -> None:
    comment = build_delivery_comment(
        client_name="〇〇製作所", clip_count=10, notices=build_notices()
    )
    assert has_all_required_notices(comment)
    for notice in REQUIRED_NOTICES:
        assert f"・{notice}" in comment


def test_delivery_comment_states_the_client_and_clip_count() -> None:
    comment = build_delivery_comment(
        client_name="〇〇製作所", clip_count=8, notices=build_notices(), dropped_clip_count=2
    )
    assert "「〇〇製作所」" in comment
    assert "切り抜き 8 本" in comment
    assert "2 本を落としています" in comment


def test_delivery_comment_admits_an_unknown_client() -> None:
    comment = build_delivery_comment(client_name="", clip_count=10, notices=build_notices())
    assert "クライアント名 未確定" in comment


def test_quality_note_is_appended_after_the_required_notices() -> None:
    notices = build_notices(quality_note="長辺 480px で解析しました。")
    assert notices[: len(REQUIRED_NOTICES)] == list(REQUIRED_NOTICES)
    assert notices[-1] == "長辺 480px で解析しました。"


def test_dropping_one_notice_is_detected() -> None:
    partial = "\n".join(REQUIRED_NOTICES[:-1])
    assert not has_all_required_notices(partial)


def test_no_real_client_name_is_baked_into_the_tool_surface() -> None:
    """MCP ツールの入力スキーマに実在クライアント名を焼き込まない。

    ``description`` は便C で tools/list に載れば LLM と CloudWatch の両方へ毎回流れる。
    B-9 が「CloudWatch に社名が出ていない」を完了条件にしている規律と揃える。
    例は架空名（〇〇製作所）で固定する。
    """

    from teamagent.skills.clip_proposal.schema import ClipProposalSubmitInput

    description = ClipProposalSubmitInput.model_fields["client_name"].description or ""
    assert "〇〇製作所" in description
    # 「例: 」の後ろに実在しそうな固有名詞（伏せ字を含まない社名）を置かない。
    example = description.split("例:", 1)[1].split("）", 1)[0].strip()
    assert example.startswith("〇〇")
