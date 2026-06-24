"""adapters/gmail_client.py のユニットテスト。

実 Gmail API は呼ばず、FakeGmailService で response 構造をモックする。
検証ポイント：
- list_messages: kwargs 構築 / pagination / label / query
- get_message: payload 構造のマップ + headers dict 化
- list_labels / create_hidden_label: labelHide / hide オプションの送信
- ensure_team_agent_labels: 既存ラベルは作らず、無いものだけ作る
- modify_message_labels: add/remove kwargs
- create_draft: thread_id / In-Reply-To 設定
- extract_plain_text: multipart + base64url + HTML 部分の除外
- extract_thread_participants: From / To / Cc から email 抽出 + dedup
- from_env: credentials 未設定の warning 経路
- scopes: gmail.modify が既定 / readonly=True で gmail.readonly
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from teamagent.adapters.gmail_client import (
    GmailClient,
    GmailDraft,
    GmailLabel,
    GmailMessage,
    GmailMessageRef,
    TeamAgentLabels,
    _build_raw_email,
    extract_plain_text,
    extract_thread_participants,
)


# -----------------------------------------------------------
# Fake Gmail Service
# -----------------------------------------------------------
class _FakeReq:
    def __init__(self, response: Any) -> None:
        self._r = response

    def execute(self) -> Any:
        return self._r


class _FakeMessages:
    def __init__(
        self, list_response: Any, get_response: Any, modify_response: Any | None = None
    ) -> None:
        self._list = list_response
        self._get = get_response
        self._modify = modify_response or {}
        self.last_list_kwargs: dict[str, Any] = {}
        self.last_get_kwargs: dict[str, Any] = {}
        self.last_modify_kwargs: dict[str, Any] = {}

    def list(self, **kwargs: Any) -> _FakeReq:
        self.last_list_kwargs = kwargs
        return _FakeReq(self._list)

    def get(self, **kwargs: Any) -> _FakeReq:
        self.last_get_kwargs = kwargs
        return _FakeReq(self._get)

    def modify(self, **kwargs: Any) -> _FakeReq:
        self.last_modify_kwargs = kwargs
        return _FakeReq(self._modify)


class _FakeLabels:
    def __init__(
        self,
        list_response: Any,
        create_response: Any,
    ) -> None:
        self._list = list_response
        self._create = create_response
        self.last_create_kwargs: dict[str, Any] = {}

    def list(self, **kwargs: Any) -> _FakeReq:
        return _FakeReq(self._list)

    def create(self, **kwargs: Any) -> _FakeReq:
        self.last_create_kwargs = kwargs
        return _FakeReq(self._create)


class _FakeDrafts:
    def __init__(self, create_response: Any) -> None:
        self._create = create_response
        self.last_create_kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> _FakeReq:
        self.last_create_kwargs = kwargs
        return _FakeReq(self._create)


class _FakeThreads:
    def __init__(self, get_response: Any) -> None:
        self._get = get_response
        self.last_get_kwargs: dict[str, Any] = {}

    def get(self, **kwargs: Any) -> _FakeReq:
        self.last_get_kwargs = kwargs
        return _FakeReq(self._get)


class _FakeUsersResource:
    def __init__(
        self,
        messages: _FakeMessages,
        labels: _FakeLabels,
        drafts: _FakeDrafts,
        threads: _FakeThreads,
    ) -> None:
        self._messages = messages
        self._labels = labels
        self._drafts = drafts
        self._threads = threads

    def messages(self) -> _FakeMessages:
        return self._messages

    def labels(self) -> _FakeLabels:
        return self._labels

    def drafts(self) -> _FakeDrafts:
        return self._drafts

    def threads(self) -> _FakeThreads:
        return self._threads


class FakeGmailService:
    def __init__(
        self,
        *,
        messages_list: Any | None = None,
        message_get: Any | None = None,
        labels_list: Any | None = None,
        label_create: Any | None = None,
        draft_create: Any | None = None,
        message_modify: Any | None = None,
        thread_get: Any | None = None,
    ) -> None:
        self._users = _FakeUsersResource(
            _FakeMessages(
                messages_list or {"messages": [], "nextPageToken": None},
                message_get or {},
                message_modify,
            ),
            _FakeLabels(
                labels_list or {"labels": []},
                label_create or {"id": "Label_NEW", "name": "TeamAgent/x", "type": "user"},
            ),
            _FakeDrafts(
                draft_create or {"id": "DRAFT_1", "message": {"id": "MSG_1", "threadId": "T_1"}}
            ),
            _FakeThreads(thread_get or {"messages": []}),
        )

    def users(self) -> _FakeUsersResource:
        return self._users


# -----------------------------------------------------------
# list_messages
# -----------------------------------------------------------
def test_list_messages_returns_refs_with_pagination() -> None:
    fake = FakeGmailService(
        messages_list={
            "messages": [
                {"id": "M1", "threadId": "T1"},
                {"id": "M2", "threadId": "T2"},
            ],
            "nextPageToken": "P2",
        }
    )
    client = GmailClient(service=fake)
    msgs, next_token = client.list_messages(
        query="from:foo@x.com", request_id="r", label_ids=["INBOX"], max_results=10
    )
    assert len(msgs) == 2
    assert all(isinstance(m, GmailMessageRef) for m in msgs)
    assert msgs[0].id == "M1"
    assert next_token == "P2"
    # kwargs に query / labelIds / maxResults が渡る
    kwargs = fake.users().messages().last_list_kwargs
    assert kwargs["q"] == "from:foo@x.com"
    assert kwargs["labelIds"] == ["INBOX"]
    assert kwargs["maxResults"] == 10


def test_list_messages_handles_empty_response() -> None:
    fake = FakeGmailService()
    client = GmailClient(service=fake)
    msgs, next_token = client.list_messages(query=None, request_id="r")
    assert msgs == []
    assert next_token is None


# -----------------------------------------------------------
# get_message
# -----------------------------------------------------------
def test_get_message_maps_payload_and_headers() -> None:
    fake = FakeGmailService(
        message_get={
            "id": "M1",
            "threadId": "T1",
            "labelIds": ["INBOX", "Label_TA"],
            "snippet": "sample snippet",
            "internalDate": "1700000000000",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "Alice <alice@x.com>"},
                    {"name": "Subject", "value": "Hello"},
                ],
                "body": {"data": ""},
            },
        }
    )
    client = GmailClient(service=fake)
    msg = client.get_message(msg_id="M1", request_id="r")
    assert isinstance(msg, GmailMessage)
    assert msg.id == "M1"
    assert msg.label_ids == ("INBOX", "Label_TA")
    assert msg.headers["From"] == "Alice <alice@x.com>"
    assert msg.headers["Subject"] == "Hello"
    assert msg.internal_date_ms == 1700000000000


# -----------------------------------------------------------
# extract_plain_text
# -----------------------------------------------------------
def test_extract_plain_text_decodes_base64url() -> None:
    raw = "Hello こんにちは"
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
    payload = {
        "mimeType": "text/plain",
        "body": {"data": encoded},
    }
    assert extract_plain_text(payload) == raw


def test_extract_plain_text_walks_multipart() -> None:
    """multipart の場合に text/plain を探して返す。"""
    text = "plain body"
    encoded = base64.urlsafe_b64encode(text.encode()).decode("ascii").rstrip("=")
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": "PHA+aGk8L3A+"}},  # <p>hi</p>
            {"mimeType": "text/plain", "body": {"data": encoded}},
        ],
    }
    assert extract_plain_text(payload) == text


def test_extract_plain_text_returns_empty_when_html_only() -> None:
    payload = {
        "mimeType": "text/html",
        "body": {"data": "PHA+aGk8L3A+"},
    }
    assert extract_plain_text(payload) == ""


# -----------------------------------------------------------
# extract_thread_participants
# -----------------------------------------------------------
def test_extract_thread_participants_parses_addresses() -> None:
    headers = {
        "From": "Alice <alice@x.com>",
        "To": "bob@y.com, Carol <carol@x.com>",
        "Cc": "dave@z.com",
    }
    emails = extract_thread_participants(headers)
    assert "alice@x.com" in emails
    assert "bob@y.com" in emails
    assert "carol@x.com" in emails
    assert "dave@z.com" in emails


def test_extract_thread_participants_deduplicates() -> None:
    headers = {
        "From": "alice@x.com",
        "To": "Alice <alice@x.com>, bob@y.com",
        "Cc": "ALICE@X.COM",
    }
    emails = extract_thread_participants(headers)
    # alice@x.com は大小文字違い含めて 1 つだけ
    assert sum(1 for e in emails if e.lower() == "alice@x.com") == 1
    assert "bob@y.com" in emails


# -----------------------------------------------------------
# Labels
# -----------------------------------------------------------
def test_create_hidden_label_sets_visibility_hide() -> None:
    fake = FakeGmailService(
        label_create={
            "id": "Label_HIDDEN",
            "name": "TeamAgent/processed",
            "type": "user",
            "labelListVisibility": "labelHide",
            "messageListVisibility": "hide",
        }
    )
    client = GmailClient(service=fake)
    label = client.create_hidden_label("TeamAgent/processed", request_id="r")
    assert isinstance(label, GmailLabel)
    assert label.id == "Label_HIDDEN"
    assert label.label_list_visibility == "labelHide"
    assert label.message_list_visibility == "hide"
    # 送信ボディが正しい
    body = fake.users().labels().last_create_kwargs["body"]
    assert body["labelListVisibility"] == "labelHide"
    assert body["messageListVisibility"] == "hide"


def test_ensure_team_agent_labels_reuses_existing() -> None:
    """既にある TeamAgent/processed は再作成せず、無いものだけ作る。"""
    fake = FakeGmailService(
        labels_list={
            "labels": [
                {"id": "L_EXIST", "name": "TeamAgent/processed", "type": "user"},
            ]
        },
        label_create={"id": "L_NEW", "name": "TeamAgent/skip", "type": "user"},
    )
    client = GmailClient(service=fake)
    result = client.ensure_team_agent_labels(request_id="r")
    # 4 種全部 ID 返ってる
    assert set(result.keys()) == set(TeamAgentLabels.all())
    # 既存ラベルは再利用
    assert result["TeamAgent/processed"] == "L_EXIST"


def test_modify_message_labels_passes_add_remove() -> None:
    fake = FakeGmailService()
    client = GmailClient(service=fake)
    client.modify_message_labels(msg_id="M1", request_id="r", add=["L_PROC"], remove=["L_ERROR"])
    kwargs = fake.users().messages().last_modify_kwargs
    assert kwargs["id"] == "M1"
    assert kwargs["body"]["addLabelIds"] == ["L_PROC"]
    assert kwargs["body"]["removeLabelIds"] == ["L_ERROR"]


def test_modify_message_labels_skips_when_both_empty() -> None:
    """add も remove も無ければ API を呼ばない（呼んでも空 body で API がエラーになる）。"""
    fake = FakeGmailService()
    client = GmailClient(service=fake)
    client.modify_message_labels(msg_id="M1", request_id="r")
    # last_modify_kwargs は更新されない（呼ばれていない）
    assert fake.users().messages().last_modify_kwargs == {}


# -----------------------------------------------------------
# create_draft
# -----------------------------------------------------------
def test_create_draft_returns_draft_with_ids() -> None:
    fake = FakeGmailService(draft_create={"id": "D1", "message": {"id": "M1", "threadId": "T1"}})
    client = GmailClient(service=fake)
    draft = client.create_draft(
        to="alice@x.com",
        subject="Re: hello",
        body_text="Hi Alice, thanks!",
        request_id="r",
        thread_id="T1",
        in_reply_to_message_id="<orig@x.com>",
    )
    assert isinstance(draft, GmailDraft)
    assert draft.id == "D1"
    assert draft.message_id == "M1"
    assert draft.thread_id == "T1"
    # 送信本文に raw + threadId が含まれる
    body = fake.users().drafts().last_create_kwargs["body"]
    assert "raw" in body["message"]
    assert body["message"]["threadId"] == "T1"


def test_get_thread_maps_messages_in_order() -> None:
    fake = FakeGmailService(
        thread_get={
            "messages": [
                {
                    "id": "M1",
                    "threadId": "T1",
                    "internalDate": "1700000000000",
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [{"name": "From", "value": "alice@x.com"}],
                        "body": {"data": ""},
                    },
                },
                {
                    "id": "M2",
                    "threadId": "T1",
                    "internalDate": "1700000100000",
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [{"name": "From", "value": "bob@y.com"}],
                        "body": {"data": ""},
                    },
                },
            ]
        }
    )
    client = GmailClient(service=fake)
    msgs = client.get_thread("T1", request_id="r")
    assert [m.id for m in msgs] == ["M1", "M2"]
    assert all(isinstance(m, GmailMessage) for m in msgs)
    assert msgs[0].headers["From"] == "alice@x.com"
    assert msgs[1].thread_id == "T1"
    # threads.get に正しい id が渡る
    assert fake.users().threads().last_get_kwargs["id"] == "T1"


def test_get_thread_handles_empty() -> None:
    client = GmailClient(service=FakeGmailService())
    assert client.get_thread("T_missing", request_id="r") == []


def test_build_raw_email_encodes_subject_and_body_in_utf8() -> None:
    raw_b64 = _build_raw_email(
        to="alice@x.com",
        subject="件名（日本語）",
        body_text="本文 こんにちは",
    )
    # base64url decode 可能であること
    pad = "=" * (-len(raw_b64) % 4)
    decoded = base64.urlsafe_b64decode(raw_b64 + pad).decode("utf-8", errors="replace")
    # MIME-encoded subject の存在チェック（UTF-8 base64 もしくは quoted-printable で encode される）
    assert "alice@x.com" in decoded
    # 件名は MIME-encoded（=?UTF-8?...?=）なので生で日本語は出ない、件名キーは出る
    assert "Subject:" in decoded


def test_build_raw_email_includes_cc_header() -> None:
    raw_b64 = _build_raw_email(
        to="alice@x.com", subject="s", body_text="b", cc="carol@x.com, dave@x.com"
    )
    pad = "=" * (-len(raw_b64) % 4)
    decoded = base64.urlsafe_b64decode(raw_b64 + pad).decode("utf-8", errors="replace")
    assert "Cc:" in decoded
    assert "carol@x.com" in decoded
    assert "dave@x.com" in decoded


# -----------------------------------------------------------
# from_env / scopes
# -----------------------------------------------------------
def test_from_env_default_scope_is_modify() -> None:
    client = GmailClient.from_env()
    assert "https://www.googleapis.com/auth/gmail.modify" in client._scopes


def test_from_env_readonly_scope_when_explicit() -> None:
    client = GmailClient.from_env(readonly=True)
    assert "https://www.googleapis.com/auth/gmail.readonly" in client._scopes


def test_from_env_picks_up_impersonate_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_GMAIL_IMPERSONATE_USER", "ai-svc@vectorinc.co.jp")
    client = GmailClient.from_env()
    assert client._impersonate_user == "ai-svc@vectorinc.co.jp"


def test_build_credentials_raises_when_nothing_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for k in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_OAUTH_REFRESH_TOKEN",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(k, raising=False)
    client = GmailClient(service=None)
    with pytest.raises(NotImplementedError):
        client._build_credentials()


def test_ensure_service_uses_injected_service() -> None:
    fake = FakeGmailService()
    client = GmailClient(service=fake)
    assert client._ensure_service() is fake


def test_team_agent_labels_constants() -> None:
    """ハードコード回避：定数経由でアクセスできる。"""
    assert TeamAgentLabels.PROCESSED == "TeamAgent/processed"
    assert TeamAgentLabels.DRAFT_PENDING == "TeamAgent/draft-pending"
    assert TeamAgentLabels.ERROR == "TeamAgent/error"
    assert TeamAgentLabels.SKIP == "TeamAgent/skip"
    assert len(TeamAgentLabels.all()) == 4


def test_list_drafts_paginates_all_pages() -> None:
    """list_drafts は nextPageToken を辿り全ページ取得する。

    ページングしないと 51 件目以降を取りこぼし、digest の冪等性（重複下書き防止）が
    壊れる。2 ページのフェイクで全件取得＋pageToken 追跡を確認する。
    """
    pages = {
        None: {
            "drafts": [{"id": "d1", "message": {"id": "m1", "threadId": "t1"}}],
            "nextPageToken": "P2",
        },
        "P2": {"drafts": [{"id": "d2", "message": {"id": "m2", "threadId": "t2"}}]},
    }
    seen_tokens: list[Any] = []

    class _Drafts:
        def list(self, **kw: Any) -> _FakeReq:
            seen_tokens.append(kw.get("pageToken"))
            return _FakeReq(pages[kw.get("pageToken")])

    class _Users:
        def drafts(self) -> _Drafts:
            return _Drafts()

    class _Svc:
        def users(self) -> _Users:
            return _Users()

    client = GmailClient(service=_Svc())
    out = client.list_drafts("r")
    assert [d.thread_id for d in out] == ["t1", "t2"]  # 両ページの下書きを取得
    assert seen_tokens == [None, "P2"]  # pageToken を辿った


def test_decode_rfc2047_japanese_headers() -> None:
    """RFC2047 エンコードされた日本語 Subject/From を人間可読にデコードする。

    Gmail API は非 ASCII ヘッダを =?UTF-8?B?...?= で返すため、デコードしないと
    日本語の件名・差出人名が DM/下書きで文字化けする。
    """
    from email.header import Header

    from teamagent.adapters.gmail_client import _decode_header_value, _message_from_resp

    enc_subj = Header("重要なご相談", "utf-8").encode()
    assert "=?" in enc_subj  # 実際にエンコードされている
    assert _decode_header_value(enc_subj) == "重要なご相談"
    assert _decode_header_value("Re: meeting") == "Re: meeting"  # ASCII は素通り
    assert _decode_header_value("") == ""

    frm = Header("山田 太郎", "utf-8").encode() + " <yamada@acme.co.jp>"
    resp = {
        "id": "m1",
        "threadId": "t1",
        "payload": {
            "headers": [
                {"name": "Subject", "value": Header("見積もりの件", "utf-8").encode()},
                {"name": "From", "value": frm},
                {"name": "Message-ID", "value": "<abc@x>"},
            ]
        },
    }
    msg = _message_from_resp(resp)
    assert msg.headers["Subject"] == "見積もりの件"  # デコード済み
    assert "山田 太郎" in msg.headers["From"]
    assert "yamada@acme.co.jp" in msg.headers["From"]

    # CRLF/TAB を含むデコード結果は無害化（ヘッダ注入/Slack 書式崩れ防止）
    evil = Header("1行目\n偽の警告\r攻撃", "utf-8").encode()
    out = _decode_header_value(evil)
    assert "\n" not in out and "\r" not in out
    assert "1行目" in out and "偽の警告" in out  # 内容は残るが改行は除去


def test_extract_plain_text_respects_charset() -> None:
    """本文の Content-Type charset を尊重する（ISO-2022-JP/Shift_JIS の日本語が化けない）。"""
    import base64

    from teamagent.adapters.gmail_client import extract_plain_text

    jis = "請求書の件、ご確認ください。"
    data = base64.urlsafe_b64encode(jis.encode("iso-2022-jp")).decode().rstrip("=")
    part = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": "text/plain; charset=ISO-2022-JP"}],
        "body": {"data": data},
    }
    assert extract_plain_text(part) == jis  # 文字化けしない

    sjis = "見積もり"
    part2 = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": 'text/plain; charset="Shift_JIS"'}],
        "body": {"data": base64.urlsafe_b64encode(sjis.encode("cp932")).decode().rstrip("=")},
    }
    assert extract_plain_text(part2) == sjis

    # charset 指定なしは UTF-8 として従来どおり
    utf = {
        "mimeType": "text/plain",
        "body": {"data": base64.urlsafe_b64encode("こんにちは".encode()).decode().rstrip("=")},
    }
    assert extract_plain_text(utf) == "こんにちは"
