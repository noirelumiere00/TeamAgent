"""Google Docs / Slides アダプタのテスト（課金0・fake service）。

テキスト抽出ロジックと per-user from_user_token の fail-closed を検証する。
service を注入するので googleapiclient/google ライブラリは不要（lazy import）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gdocs_client import GDocsClient, extract_doc_text
from teamagent.adapters.gslides_client import GSlidesClient, extract_slides_text
from teamagent.adapters.oauth_token_store import OAuthToken

# ── Docs ────────────────────────────────────────────────────────────────────


def test_extract_doc_text() -> None:
    body: dict[str, Any] = {
        "content": [
            {"paragraph": {"elements": [{"textRun": {"content": "見出し\n"}}]}},
            {
                "paragraph": {
                    "elements": [
                        {"textRun": {"content": "本文A。"}},
                        {"textRun": {"content": "本文B。\n"}},
                    ]
                }
            },
            {"sectionBreak": {}},  # paragraph 以外はスキップ
        ]
    }
    assert extract_doc_text(body) == "見出し\n本文A。本文B。\n"


def test_gdocs_get_document_text() -> None:
    svc = MagicMock()
    svc.documents().get().execute.return_value = {
        "title": "提案ドラフト",
        "body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "中身"}}]}}]},
    }
    client = GDocsClient(service=svc)
    doc = client.get_document_text("doc-1", request_id="t")
    assert doc.title == "提案ドラフト"
    assert doc.text == "中身"
    assert doc.document_id == "doc-1"


# ── Slides ──────────────────────────────────────────────────────────────────


def test_extract_slides_text() -> None:
    slides: list[dict[str, Any]] = [
        {
            "pageElements": [
                {
                    "shape": {
                        "text": {
                            "textElements": [
                                {"textRun": {"content": "タイトル"}},
                                {"textRun": {"content": "サブ"}},
                            ]
                        }
                    }
                },
                {"image": {}},  # shape 以外はスキップ
            ]
        },
        {
            "pageElements": [
                {"shape": {"text": {"textElements": [{"textRun": {"content": "2枚目"}}]}}},
            ]
        },
    ]
    # スライド境界で改行・末尾 strip
    assert extract_slides_text(slides) == "タイトルサブ\n2枚目"


def test_gslides_get_presentation_text() -> None:
    svc = MagicMock()
    svc.presentations().get().execute.return_value = {
        "title": "ピッチ",
        "slides": [
            {
                "pageElements": [
                    {"shape": {"text": {"textElements": [{"textRun": {"content": "A"}}]}}}
                ]
            },
        ],
    }
    client = GSlidesClient(service=svc)
    pres = client.get_presentation_text("pres-1", request_id="t")
    assert pres.title == "ピッチ"
    assert pres.text == "A"
    assert pres.slide_count == 1


# ── per-user from_user_token の fail-closed ──────────────────────────────────


def test_from_user_token_fail_closed_without_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """OAuth クライアント未設定（W1 未完）なら from_user_token は ValueError（fail-closed）。"""
    monkeypatch.delenv("CONNECT_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("CONNECT_GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    tok = OAuthToken(refresh_token="rt", scopes=("documents.readonly",))
    with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID"):
        GDocsClient.from_user_token(tok)
    with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID"):
        GSlidesClient.from_user_token(tok)


def test_from_user_token_fail_closed_empty_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """クライアントはあっても token 空（未認可）なら ValueError。"""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    with pytest.raises(ValueError, match="refresh_token"):
        GDocsClient.from_user_token(OAuthToken(refresh_token=""))
