"""レビュー MED 2件の回帰: 社内原典URLの印字と publish env ゲートバイパス。"""

import pytest

from teamagent.skills.proposal_builder.skill import (
    _is_internal_source_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://drive.google.com/file/d/abc/view",
        "https://docs.google.com/presentation/d/xyz/edit",
        "https://connect.newstv.co.jp/app",
        "https://mail.vectorinc.co.jp/x",
    ],
)
def test_internal_origins_are_masked(url: str) -> None:
    assert _is_internal_source_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/@user/video/123",
        "https://x.com/user/status/1",
        "https://prtimes.jp/main/html/rd/p/1.html",
    ],
)
def test_external_citations_stay_visible(url: str) -> None:
    assert _is_internal_source_url(url) is False


def test_lookalike_host_is_not_treated_as_internal() -> None:
    # サフィックス一致は「.」境界必須（evil-newstv.co.jp 等を内部扱いしない…
    # 逆に外部を内部に誤判定しても漏洩はしないが、出典が消える副作用を防ぐ）
    assert _is_internal_source_url("https://fakenewstv.co.jp/a") is False
