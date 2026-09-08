"""skills.search.dates（資料名の日付抽出・日付根拠の決定）の純関数テスト。

便A-3: 「最新の更新日からちゃんと出して」に対し、Aico が **根拠のある日付だけ** を
返すための土台。DB / env / LLM を一切使わない。
"""

from __future__ import annotations

import pytest

from teamagent.skills.search.dates import extract_title_date, resolve_date_basis


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 実運用のファイル名（末尾 YYYYMMDD・先頭 YYYYMMDD・命名規約プレフィックス付き）
        ("【NewsTV】SABON様_ご説明資料_20260227.pptx", "2026-02-27"),
        ("20250820花王様限定_縦型ソリューションパッケージ.pdf", "2025-08-20"),
        ("社内共有情報_花王株式会社__20250820花王様限定_縦型.pdf", "2025-08-20"),
        # 提案書 FMT の YYMMDD（トークン先頭）
        ("260708_提案書FMT.pptx", "2026-07-08"),
        ("提案書 260708 版.pptx", "2026-07-08"),
        # 区切り付き
        ("2026-02-27 定例議事録", "2026-02-27"),
        ("2026/3/5_キックオフ", "2026-03-05"),
        ("2026.02.27_資料", "2026-02-27"),
        # 和暦風（日なしは YYYY-MM）
        ("2026年2月27日 提案", "2026-02-27"),
        ("2026年3月 提案書", "2026-03"),
    ],
)
def test_extract_title_date_finds_dates(text: str, expected: str) -> None:
    assert extract_title_date(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "提案書v2.pdf",
        # 暦として無効
        "2026年2月30日 提案",
        "20261301_資料.pdf",
        # 金額・ID・長い数字列は日付にしない
        "売上100000円",
        "ID 12345678",
        "注文番号 202602271234",
        # Slack スレッド題（channel ts）: 小数部 6 桁を YYMMDD に誤読しない
        "proj-anfar 1720000000.250101",
        # 2 桁年はトークン先頭だけ（語中の 6 桁は無視）
        "提案書250101.pdf",
        # 範囲外の年
        "19991231_旧資料.pdf",
    ],
)
def test_extract_title_date_rejects_non_dates(text: str | None) -> None:
    assert extract_title_date(text) is None


def test_extract_title_date_prefers_separated_form_over_compact() -> None:
    """同じ文字列に複数形式があれば区切り付き（明示的）を優先する。"""
    assert extract_title_date("2026-03-05_旧版20250101.pdf") == "2026-03-05"


def test_resolve_date_basis_prefers_title_date() -> None:
    assert resolve_date_basis("2026-09-01", "2026-02-27") == "title_date"


def test_resolve_date_basis_falls_back_to_modified_at() -> None:
    assert resolve_date_basis("2026-09-01", None) == "modified_at"


def test_resolve_date_basis_none_when_no_evidence() -> None:
    assert resolve_date_basis(None, None) == "none"
    assert resolve_date_basis("", "") == "none"
