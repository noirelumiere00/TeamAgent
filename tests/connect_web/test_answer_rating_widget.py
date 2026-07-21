"""/search の AI 要約用4段階評価ウィジェットの文字列マーカーテスト。"""

from __future__ import annotations

import pytest

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


def test_answer_rating_widget_is_not_wired_to_answer_card_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既定OFFでは rate4Bar 本体だけを出し、従来の answer 👍/👎 を維持する。"""
    monkeypatch.delenv("CONNECT_ANSWER_RATING", raising=False)
    html = _search_html()

    assert "function rate4Bar(query,sessionId,answerId){" in html
    assert html.count("rate4Bar(") == 1
    assert "rate4Teardown" in html
    assert "/*ANSWER_RATING_" not in html
    pending = html.split("function renderAnswerPending(sessionId){", 1)[1].split("return a;", 1)[0]
    assert "fbButtons('answer',null,null,sessionId)" in pending


def test_answer_rating_widget_is_wired_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ON 時だけ pending の answer 👍/👎 を外し、成功した要約へ評価UIを付ける。"""
    monkeypatch.setenv("CONNECT_ANSWER_RATING", " yes ")
    html = _search_html()

    pending = html.split("function renderAnswerPending(sessionId){", 1)[1].split("return a;", 1)[0]
    assert "fbButtons('answer'" not in pending
    assert "if(!card.querySelector('.rate4')){" in html
    assert "rate4Bar(query,sessionId,data.answer_id||null)" in html
    assert "oldRate4.rate4Teardown()" in html
    assert "try{oldRate4.rate4Teardown();}catch(e){}" in html
    search_body = html.split("async function search(){", 1)[1].split(
        "const body=buildBody(query);", 1
    )[0]
    assert search_body.index("oldRate4.rate4Teardown()") < search_body.index("renderSkeleton();")


def test_search_session_id_is_created_and_sent_for_chunk_feedback() -> None:
    """各検索に UUID を採番し、既存 chunk 👍/👎 payload にも含める。"""
    html = _search_html()

    assert "const sessionId=crypto.randomUUID();" in html
    assert "search_session_id:sessionId" in html
    assert "fbButtons('chunk',h.doc_id||null,h.chunk_id||null,sessionId)" in html


def test_answer_rating_widget_payload_always_contains_score_and_note() -> None:
    """評価タップ、コメント送信、teardown は同じ score/note payload 組立を利用する。"""
    html = _search_html()

    assert "score:score,note:noteValue" in html
    assert "postRate4(payloadFor(score,currentNote()),'score',false,wasUpdate)" in html
    assert "postRate4(payloadFor(currentScore,currentNote()),'note',false)" in html
    assert "postRate4(payloadFor(currentScore,value),'note',true)" in html


def test_legacy_feedback_buttons_only_confirm_successful_responses() -> None:
    html = _search_html()

    assert "const resp=await fetch('/api/v1/feedback'" in html
    assert "if(!resp.ok)throw new Error('http '+resp.status);" in html
    assert "b.classList.remove(r[2]);b.disabled=false;" in html
