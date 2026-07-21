"""/search の AI 要約用4段階評価ウィジェットの文字列マーカーテスト。"""

from __future__ import annotations

from tests.connect_web.test_search_routes import _auth_cookie, _build


def _search_html() -> str:
    client, _, _ = _build()
    response = client.get("/search", cookies=_auth_cookie())
    assert response.status_code == 200
    return response.text


def test_answer_rating_widget_has_required_ui_markers() -> None:
    """問いかけ、4段階ラベル、コメント欄と補助文言を /search HTML に埋め込む。"""
    html = _search_html()

    for marker in (
        "この回答は期待に合いましたか？",
        "◎ 期待どおり",
        "○ おおむね",
        "△ 物足りない",
        "× 見当違い",
        "評価を送信しました（あとから変更できます）",
        "評価を更新しました",
        "コメントを送る",
        "欲しかった資料の種類・足りなかった観点など。クライアント名・個人名を書いたり資料本文を貼ったりしないでください",
        "評価は検索の改善にだけ使います",
        "送信できませんでした",
        "セッションが切れました。再ログインしてください",
    ):
        assert marker in html

    assert "note.maxLength=500" in html
    assert ".rate4Note.open{" in html
    assert "credentials:'same-origin'" in html


def test_answer_rating_widget_is_defined_but_not_wired_to_answer_card() -> None:
    """T4a は rate4Bar 本体のみで、attachAnswer 等からはまだ呼び出さない。"""
    html = _search_html()

    assert "function rate4Bar(query,sessionId,answerId){" in html
    assert html.count("rate4Bar(") == 1
    assert "rate4Teardown" in html


def test_answer_rating_widget_payload_always_contains_score_and_note() -> None:
    """評価タップ、コメント送信、teardown は同じ score/note payload 組立を利用する。"""
    html = _search_html()

    assert "score:score,note:noteValue" in html
    assert "postRate4(payloadFor(score,currentNote()),'score',false,wasUpdate)" in html
    assert "postRate4(payloadFor(currentScore,currentNote()),'note',false)" in html
    assert "postRate4(payloadFor(currentScore,value),'note',true)" in html
