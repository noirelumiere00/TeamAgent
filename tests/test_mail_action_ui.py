"""mail_action_ui / gmail_links の純粋ロジックテスト（インタラクティブ下書きボタン）。"""

from __future__ import annotations

from teamagent import mail_action_ui as ui
from teamagent.gmail_links import gmail_account_base, gmail_thread_url

TID = "18f2c9a3b7e1d0aa"
ME = "s-komata@vectorinc.co.jp"


def test_gmail_thread_url_authuser_and_fallback() -> None:
    assert gmail_thread_url(TID, ME) == (f"https://mail.google.com/mail/?authuser={ME}#all/{TID}")
    assert gmail_thread_url(TID, "") == f"https://mail.google.com/mail/u/0/#all/{TID}"
    assert gmail_thread_url("", ME) is None
    assert gmail_account_base("") == "https://mail.google.com/mail/u/0/"


def test_encode_decode_round_trip() -> None:
    assert ui.decode_value(ui.encode_value(TID)) == {"t": TID}
    assert ui.decode_value(None) == {}
    assert ui.decode_value("not json") == {}  # 壊れても落ちない


def test_mail_action_block_has_draft_button_and_gmail_url() -> None:
    block = ui.mail_action_block(TID, ME)
    assert block["type"] == "actions"
    els = block["elements"]
    # 1つ目=下書き作成（action_id 付き・url 無し＝押下が OC に届く）
    assert els[0]["action_id"] == ui.ACTION_DRAFT == "aila:mail_draft"
    assert "url" not in els[0]
    assert ui.decode_value(els[0]["value"]) == {"t": TID}
    assert "下書き作成" in els[0]["text"]["text"]
    # 2つ目=Gmailを開く（url ボタン・本人アカウント固定）
    assert els[1]["url"] == f"https://mail.google.com/mail/?authuser={ME}#all/{TID}"
    assert "action_id" not in els[1]


def test_draft_taken_blocks_shows_body_and_open_link() -> None:
    blocks = ui.draft_taken_blocks(
        thread_id=TID,
        user_email=ME,
        subject="動画提出",
        draft_body="タテガタ様\n本日中に審査します。",
    )
    dump = str(blocks)
    assert "返信下書きを作成しました" in dump
    assert "Slackでは送信しません" in dump
    assert ">本日中に審査します。" in dump  # 引用ブロックで本文表示
    assert f"#all/{TID}" in dump  # Gmailを開くリンク
    assert "確認して送信" in dump


def test_draft_taken_blocks_escapes_mrkdwn() -> None:
    blocks = ui.draft_taken_blocks(thread_id=TID, user_email=ME, draft_body="A < B & C > D")
    dump = str(blocks)
    assert "&lt;" in dump and "&amp;" in dump and "&gt;" in dump
    assert "A < B" not in dump  # 生の < は残さない
