"""video_algorithm のテスト共通の前提。

サムネ（一覧の表紙）の読み取りは既定 ON で、表紙の URL を持つテストは、注入が無ければ
thumbnails.fetch_cover（httpx で実際に取得する）を呼んでしまう。テストでは取得を「注入された
ものだけ」にし、ネットワークに出ない（取得できない＝fetch_failed の形になる）。
表紙の取得を試すテストは、この上から自分で差し替えるか、skill の cover_fetcher を注入する。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_cover_network(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.skills.video_algorithm import thumbnails

    monkeypatch.setattr(thumbnails, "fetch_cover", lambda *args, **kwargs: None)
