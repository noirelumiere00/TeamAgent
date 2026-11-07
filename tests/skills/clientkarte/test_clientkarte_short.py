"""架空のカルテを固定日で短くし、要約にも過去予定の規則を適用する。"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.clientkarte import skill as karte
from teamagent.skills.clientkarte.schema import ClientKarteInput
from teamagent.skills.clientkarte.status import dated_answer
from teamagent.skills.morning_digest.calendar_window import JST
from tests.skills.clientkarte.test_clientkarte_skill import _pgvector_with_hits
from tests.skills.search.test_case_state_evidence import row

CLIENT = "架空あおぞら商事"


@pytest.fixture
def short_skill(monkeypatch: pytest.MonkeyPatch) -> karte.ClientKarteSkill:
    monkeypatch.setattr(karte, "now_jst", lambda: datetime(2026, 11, 6, 10, tzinfo=JST))
    monkeypatch.setenv("KARTE_ATTACH_DOCS_MAX", "0")
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", "owner@example.test")
    monkeypatch.delenv("SLACK_WORKSPACE_DOMAIN", raising=False)
    hits = [
        SearchHit(
            n,
            "架空の案件の記録",
            1.0,
            {"campaign": f"架空商品{n}", "occurred_at": f"2026-10-{n:02d}"},
        )
        for n in range(1, 8)
    ]
    pg = _pgvector_with_hits(hits)
    pg.list_client_names.return_value = [CLIENT]
    source = row(state="lost", state_as_of="2026-11-01", evidence_date="2026-11-01")
    source.metadata.update({"client_name": CLIENT, "title": "架空商品7"})
    pg.find_documents_for_advertiser.return_value = [source]
    pg.list_documents_for_client.return_value = [
        {
            "title": "架空商品7_提案書.pdf",
            "source_type": "gdrive",
            "source_uri": "https://drive.google.com/file/d/fictional-proposal/view",
            "modified_at": "2026-11-01",
            "client_name": CLIENT,
        }
    ]
    bedrock = MagicMock()
    bedrock.converse.return_value.usage.cost_usd = 0.0
    bedrock.converse.return_value.text = (
        f"{CLIENT}: 8/26に提案予定。10月中旬に決定\n\n"
        + "\n\n".join(
            f"**架空商品{n}**\n"
            f"提案の回答待ち [chunk_id: {n}]\n"
            + "\n".join("次の打ち合わせで詳細を確認する。" * 12 for _ in range(5))
            for n in range(1, 8)
        )
    )
    return karte.ClientKarteSkill(bedrock=bedrock, pgvector=pg)


def test_karte_caps_long_body_sorts_five_cases_and_keeps_state_and_original(
    short_skill: karte.ClientKarteSkill,
) -> None:
    out = short_skill.run(
        ClientKarteInput(client_name=CLIENT),
        SkillContext(
            metadata={
                "user_email": "owner@example.test",
                "channel_id": "D01ABCDEFGH",
                "request_text": f"{CLIENT}の案件って今どうなってる？",
            }
        ),
    )
    answer = out.answer
    # 600 字は本文の枠。状態の行・原本リンクは別枠（11-07）。
    assert len(answer.split("\n状態:")[0]) <= 600
    conclusion = answer.splitlines()[0]
    assert conclusion.startswith(CLIENT + ":")
    assert conclusion == f"{CLIENT}: 架空商品7は失注（11/1 時点）"
    blocks = answer.split("\n\n")[1:6]
    for block, n in zip(blocks, range(7, 2, -1), strict=True):
        lines = block.splitlines()
        assert lines[0] == f"**架空商品{n}（10/{n} 時点）**"
        assert 2 <= len(lines[1:]) <= 3
    assert "ほか 2 件" in answer
    assert "架空商品1" not in answer and "架空商品2" not in answer
    assert "\n状態:" in answer and "正式状態: 失注" in answer
    assert "根拠日: 2026-11-01" in answer
    assert "https://example.test/source#1" in answer
    assert (
        "<https://drive.google.com/file/d/fictional-proposal/view|架空商品7_提案書.pdf>" in answer
    )
    assert "chunk_id" not in answer
    message = short_skill._bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
    assert "2026-11-06" in message


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_karte_prompt_requires_short_shape_and_dated_summary(
    short_skill: karte.ClientKarteSkill, version: str
) -> None:
    short_skill._prompt_version = version
    short_skill.run(ClientKarteInput(client_name=CLIENT), SkillContext())
    prompt = short_skill._bedrock.converse.call_args.kwargs["system"]
    assert "結論1行" in prompt and "本文2〜3行" in prompt
    assert "新しい順に最大5件" in prompt
    assert "冒頭の結論も案件本文も" in prompt
    assert "予定していた（結果は未確認）" in prompt
    assert "内部語・技術IDは出さない" in prompt


def test_summary_and_cases_rewrite_past_plans_without_changing_results_or_future() -> None:
    hits = [
        SearchHit(1, "架空", 1.0, {"campaign": "架空採用", "occurred_at": "2026-08-01"}),
        SearchHit(2, "架空", 1.0, {"campaign": "架空広報", "occurred_at": "2026-09-01"}),
    ]
    answer = dated_answer(
        "8月に提案予定。10月中旬に決定。8/26に決定した。11/20に再提案予定\n"
        "**架空採用**\n8/26に提案予定 [chunk_id: 1]\n"
        "**架空広報**\n11/20に再提案予定 [chunk_id: 2]",
        hits,
        CLIENT,
        datetime(2026, 11, 6).date(),
    )
    assert "8月に提案予定していた（結果は未確認）" in answer
    assert "10月中旬に決定を予定していた（結果は未確認）" in answer
    assert "8/26に決定した。11/20に再提案予定" in answer
    assert "8/26に提案予定していた（結果は未確認）" in answer


def test_karte_keeps_omitted_count_when_model_already_returns_five_cases(
    short_skill: karte.ClientKarteSkill,
) -> None:
    short_skill._bedrock.converse.return_value.text = (
        f"{CLIENT}: 回答待ち\n"
        + "\n".join(
            f"**架空商品{n}**\n回答待ち\n11/20に再提案予定\n詳細を確認 [chunk_id: {n}]"
            for n in range(7, 2, -1)
        )
        + "\nほか 2 件"
    )
    answer = short_skill.run(ClientKarteInput(client_name=CLIENT), SkillContext()).answer
    assert answer.count("ほか 2 件") == 1
    assert "11/20に再提案予定" in answer
    assert "chunk_id" not in answer
    assert len(answer) <= 600


def test_karte_summary_rewrites_past_plans_with_fixed_now(
    short_skill: karte.ClientKarteSkill,
) -> None:
    answer = short_skill.run(ClientKarteInput(client_name=CLIENT), SkillContext()).answer
    conclusion = answer.splitlines()[0]
    assert conclusion.endswith("（10/7 時点）")
    assert "8/26に提案予定していた（結果は未確認）" in conclusion
    assert "10月中旬に決定を予定していた（結果は未確認）" in conclusion


def test_long_related_docs_do_not_collapse_case_bodies(
    short_skill: karte.ClientKarteSkill, monkeypatch: pytest.MonkeyPatch
) -> None:
    """11-07 本番: 関連資料の一覧が長いと本文の枠が負になり、案件が「…」だけになった。"""
    monkeypatch.setenv("KARTE_ATTACH_DOCS_MAX", "6")
    short_skill._pgvector.list_documents_for_client.return_value = [
        {
            "title": f"架空商品{n}_とても長い提案書の名前_" + "あ" * 80 + ".pptx",
            "source_type": "gdrive",
            "source_uri": f"https://drive.google.com/file/d/fictional-{n}/view",
            "modified_at": "2026-11-01",
            "client_name": CLIENT,
        }
        for n in range(6)
    ]
    out = short_skill.run(
        ClientKarteInput(client_name=CLIENT),
        SkillContext(metadata={"user_email": "owner@example.test", "channel_id": "D01ABCDEFGH"}),
    )
    body = out.answer.split("\n\n")[1:6]
    assert body, out.answer
    for block in body:
        lines = block.splitlines()
        assert lines[0].startswith("**架空商品")
        assert all(line.strip() != "…" for line in lines[1:]), block
        assert any("回答待ち" in line for line in lines[1:]), block
