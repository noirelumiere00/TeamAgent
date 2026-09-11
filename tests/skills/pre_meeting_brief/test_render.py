"""描画が DELTA §3 の実物例と一致することを固定する。"""

from __future__ import annotations

import datetime as _dt

from teamagent.skills.pre_meeting_brief.render import (
    EARLY_NOTICE_LINE,
    MASTER_SHEET_SOURCE,
    NO_EXTERNAL_LINE,
    SOURCES_HEADER,
    render_brief_lines,
)
from teamagent.skills.pre_meeting_brief.schema import (
    CaseRef,
    PreMeetingBriefItem,
    PreMeetingBriefOutput,
)

DAY = _dt.date(2026, 9, 11)


def _out(**kw: object) -> PreMeetingBriefOutput:
    base: dict[str, object] = {"scanned": True, "corpus_available": True, "date": DAY.isoformat()}
    base.update(kw)
    return PreMeetingBriefOutput(**base)  # type: ignore[arg-type]


def test_corpus_missing_renders_nothing_at_all() -> None:
    """事例集が未取込のときは **節そのものを出さない**（「できません」を毎朝配らない）。"""
    assert render_brief_lines(_out(corpus_available=False), DAY) == []


def test_unscanned_renders_nothing() -> None:
    assert render_brief_lines(_out(scanned=False), DAY) == []


def test_zero_external_renders_single_line() -> None:
    lines = render_brief_lines(_out(items=[]), DAY)
    assert lines == ["🌞 9/11(金) アポ前 事例ブリーフィング", NO_EXTERNAL_LINE]


def test_early_notice_line_is_prepended() -> None:
    lines = render_brief_lines(_out(items=[]), DAY, early_notice=True)
    assert lines[0] == EARLY_NOTICE_LINE


def test_full_example_matches_delta_format() -> None:
    item = PreMeetingBriefItem(
        start_at="2026-09-11T14:00:00+09:00",
        end_at="2026-09-11T15:00:00+09:00",
        title_display="【社外】電通吉田様",
        clients_display=["富士急"],
        client_industries=["レジャー・テーマパーク"],
        agency_display="電通（吉田様）",
        cases=[
            CaseRef(
                company_display="ジャングリア沖縄",
                industry_display="観光・テーマパーク",
                effect_display=(
                    "サテライトアカウント＋TTO80本でSNSのネガ情報比率を改善、施策評価◎。"
                ),
                owner_display="清水",
                external_use="ng",
                external_use_note="⚠口頭紹介のみ",
                source_title="260706_NewsTV事業本部ショート動画事例_v2.pptx",
            )
        ],
        no_exact_note="「富士急」自体の実施事例はDrive上で確認できず（観光・テーマパークで近い実績）。",
    )
    lines = render_brief_lines(
        _out(
            items=[item],
            source_lines=[MASTER_SHEET_SOURCE, "260706_NewsTV事業本部ショート動画事例_v2.pptx"],
        ),
        DAY,
    )
    text = "\n".join(lines)
    assert lines[0] == "🌞 9/11(金) アポ前 事例ブリーフィング"
    assert lines[1] == "本日の社外MTG：1件"
    assert "▶️ 14:00–15:00  【社外】電通吉田様" in text  # en dash
    assert "  クライアント：富士急（レジャー・テーマパーク）／代理店：電通(吉田様)" in text
    assert "└ ジャングリア沖縄（観光・テーマパーク） — " in text
    assert "社内担当: 清水" in text
    assert "⚠口頭紹介のみ" in text
    assert "  ※「富士急」自体の実施事例はDrive上で確認できず" in text
    assert SOURCES_HEADER in text
    # ⚠️ 無害化は NFKC を通すので、データ由来の全角括弧は半角に正規化される。
    # 節見出し・業種括弧など **描画側が置くリテラル** は全角のまま（上の assert 群）。
    assert "• 📍ショート動画施策事例集(マスター表・営業担当列より)" in text
    assert MASTER_SHEET_SOURCE.startswith("📍ショート動画施策事例集")


def test_multi_client_uses_group_prefix() -> None:
    item = PreMeetingBriefItem(
        start_at="2026-09-11T15:00:00+09:00",
        end_at="2026-09-11T16:00:00+09:00",
        title_display="【外出】電通浦部さま",
        clients_display=["すかいらーく", "ヤクルト"],
        client_industries=["外食", "飲料/健康"],
        agency_display="電通（浦部さま）",
        cases=[
            CaseRef(
                company_display="伊藤ハム",
                product_display="クイックディナー",
                industry_display="食品",
                effect_display="目標再生数120%超で着地。",
                owner_display="小池",
                external_use="ng",
                external_use_note="⚠数値は開示NG",
                client_group="すかいらーく",
            ),
            CaseRef(
                company_display="ヤクルト本社",
                product_display="ヤクルト化粧品",
                effect_display="ゼロだった指名検索を期間中継続的に創出。",
                owner_display="高林",
                external_use="ng",
                external_use_note="⚠開示NG（confidential）",
                client_group="ヤクルト",
                same_client=True,
            ),
        ],
    )
    text = "\n".join(render_brief_lines(_out(items=[item]), DAY))
    assert "└ [すかいらーく系] 伊藤ハム「クイックディナー」（食品）" in text
    assert "└ [ヤクルト系] ヤクルト本社「ヤクルト化粧品」（同一クライアント）" in text
    assert "クライアント：すかいらーく（外食）・ヤクルト（飲料/健康）" in text


def test_single_client_has_no_group_prefix() -> None:
    item = PreMeetingBriefItem(
        title_display="【社外】初田製作所様",
        clients_display=["初田製作所"],
        cases=[CaseRef(company_display="初田製作所", client_group="初田製作所")],
    )
    text = "\n".join(render_brief_lines(_out(items=[item]), DAY))
    assert "[初田製作所系]" not in text


def test_missing_effect_falls_back_to_fixed_text() -> None:
    item = PreMeetingBriefItem(
        title_display="【社外】A様",
        clients_display=["A"],
        cases=[CaseRef(company_display="B", owner_display="")],
    )
    text = "\n".join(render_brief_lines(_out(items=[item]), DAY))
    assert "（効果は資料内・リンク参照）" in text
    assert "社内担当: 未登録" in text


def test_uncertain_verdict_is_marked() -> None:
    item = PreMeetingBriefItem(title_display="田中様 打合せ", verdict="uncertain")
    text = "\n".join(render_brief_lines(_out(items=[item]), DAY))
    assert "（社外か要確認）" in text
    assert "└ 該当事例なし（要確認）" in text


def test_rendered_lines_never_contain_slack_markup() -> None:
    """予定タイトル・社名に Slack 記法が混ざっても組み立て不能。"""
    item = PreMeetingBriefItem(
        title_display="<!channel> 重要",
        clients_display=["<@U123>"],
        cases=[CaseRef(company_display="<https://evil.example|クリック>")],
    )
    text = "\n".join(render_brief_lines(_out(items=[item]), DAY))
    assert "<" not in text
    assert ">" not in text
    assert "@" not in text
