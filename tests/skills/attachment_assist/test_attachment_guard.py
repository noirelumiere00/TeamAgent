"""attachment_assist のセキュリティ境界テスト（ホスト allowlist / 外部ファイル / 逐次サイズ）。

フェイクは **本番の失敗モードを再現する**こと:
  * ホスト不一致 = Slack の files 配列に混ざった外部ホスト URL（bot token 漏洩経路）
  * is_external = Google Drive 等の外部共有エントリ
  * サイズ超過 = 30MB 超のファイル（共有 mcp タスクの OOM 経路）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from teamagent.adapters.slack_client import SlackClient
from teamagent.adapters.slack_file_guard import (
    SlackFileGuardError,
    is_external_file,
    slack_file_allowed_hosts,
    validate_slack_file_url,
)
from teamagent.skills.attachment_assist.discover import (
    REASON_BAD_URL,
    REASON_EXTERNAL,
    REASON_TOO_LARGE,
    REASON_UNSUPPORTED,
    classify_kind,
    collect_candidates,
    evaluate_file,
)

OK_URL = "https://files.slack.com/files-pri/T01-F01/report.pdf"
PDF_MIME = "application/pdf"


def _file(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "F01",
        "name": "report.pdf",
        "mimetype": PDF_MIME,
        "filetype": "pdf",
        "size": 1234,
        "url_private": OK_URL,
    }
    base.update(over)
    return base


# ── ホスト allowlist ────────────────────────────────────────────────────────


def test_allows_canonical_slack_file_host() -> None:
    assert validate_slack_file_url(OK_URL) == OK_URL


@pytest.mark.parametrize(
    "url",
    [
        # 攻撃者ホスト（Slack の files 配列に外部 URL が混ざる実ケース）
        "https://evil.example.com/files-pri/T01-F01/report.pdf",
        # 接尾辞偽装（files.slack.com を含むが別ドメイン）
        "https://files.slack.com.attacker.jp/x.pdf",
        # 部分文字列偽装
        "https://attacker.jp/?x=files.slack.com",
        # 平文 HTTP（token が平文で流れる）
        "http://files.slack.com/files-pri/T01-F01/report.pdf",
        # userinfo / 非標準ポート
        "https://user:pw@files.slack.com/x.pdf",
        "https://files.slack.com:8443/x.pdf",
        "",
    ],
)
def test_rejects_non_slack_or_non_canonical_urls(url: str) -> None:
    with pytest.raises(SlackFileGuardError):
        validate_slack_file_url(url)


def test_allowed_hosts_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_FILE_ALLOWED_HOSTS", "files.example-grid.com")
    assert slack_file_allowed_hosts() == frozenset({"files.example-grid.com"})
    assert validate_slack_file_url("https://files.example-grid.com/a.pdf")
    with pytest.raises(SlackFileGuardError):
        validate_slack_file_url(OK_URL)


def test_allowed_hosts_env_empty_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_FILE_ALLOWED_HOSTS", "  ,  ")
    assert slack_file_allowed_hosts() == frozenset({"files.slack.com"})


# ── 外部共有ファイル ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "over",
    [
        {"is_external": True},
        {"external_type": "gdrive"},
        {"mode": "external"},
    ],
)
def test_external_files_are_detected(over: dict[str, Any]) -> None:
    assert is_external_file(_file(**over)) is True


def test_internal_file_is_not_external() -> None:
    assert is_external_file(_file(mode="hosted")) is False


# ── evaluate_file（受入判定の順序）────────────────────────────────────────


def test_external_file_rejected_before_anything_else() -> None:
    # 外部ファイルは url_private が外部ホストを指し得る＝そもそも触らない。
    cand, rej = evaluate_file(
        _file(is_external=True, url_private="https://drive.google.com/x"), max_bytes=10**7
    )
    assert cand is None
    assert rej is not None and rej.reason == REASON_EXTERNAL


def test_foreign_host_rejected() -> None:
    cand, rej = evaluate_file(_file(url_private="https://evil.example.com/x.pdf"), max_bytes=10**7)
    assert cand is None
    assert rej is not None and rej.reason == REASON_BAD_URL


def test_oversized_rejected_by_metadata_size() -> None:
    cand, rej = evaluate_file(_file(size=40 * 1024 * 1024), max_bytes=30 * 1024 * 1024)
    assert cand is None
    assert rej is not None and rej.reason == REASON_TOO_LARGE


def test_unsupported_type_rejected() -> None:
    cand, rej = evaluate_file(
        _file(name="photo.png", mimetype="image/png", filetype="png"), max_bytes=10**7
    )
    assert cand is None
    assert rej is not None and rej.reason == REASON_UNSUPPORTED


def test_supported_file_accepted() -> None:
    cand, rej = evaluate_file(_file(), max_bytes=10**7)
    assert rej is None
    assert cand is not None and cand.kind == "pdf" and cand.url == OK_URL


@pytest.mark.parametrize(
    ("mime", "name", "expect"),
    [
        (PDF_MIME, "a.pdf", "pdf"),
        ("", "a.docx", "docx"),
        ("", "a.pptx", "pptx"),
        ("", "a.xlsx", "xlsx"),
        ("text/plain", "a.txt", "text"),
        ("", "a.md", "text"),
        ("application/json", "a.json", "text"),
        ("application/zip", "a.zip", ""),
        ("video/mp4", "a.mp4", ""),
        ("application/msword", "a.doc", ""),  # 旧 Office バイナリは非対応
    ],
)
def test_classify_kind(mime: str, name: str, expect: str) -> None:
    assert classify_kind(mime, name) == expect


# ── collect_candidates ─────────────────────────────────────────────────────


class _Msg:
    def __init__(self, ts: str, files: list[dict[str, Any]]) -> None:
        self.ts = ts
        self.files = tuple(files)


def test_collect_orders_newest_first_and_dedups() -> None:
    old = _file(id="F_old", name="old.pdf")
    new = _file(id="F_new", name="new.pdf")
    msgs = [_Msg("100.0", [old]), _Msg("200.0", [new]), _Msg("300.0", [new])]
    accepted, rejected = collect_candidates(msgs, max_bytes=10**7)
    assert [c.name for c in accepted] == ["new.pdf", "old.pdf"]
    assert rejected == []


def test_collect_reports_rejections() -> None:
    msgs = [_Msg("100.0", [_file(id="F_ext", name="drive.pdf", is_external=True)])]
    accepted, rejected = collect_candidates(msgs, max_bytes=10**7)
    assert accepted == []
    assert [r.reason for r in rejected] == [REASON_EXTERNAL]


# ── adapter: ストリーミング逐次サイズ検査 ────────────────────────────────


class _CountingStream(httpx.AsyncByteStream):
    """要求されたぶんだけチャンクを吐く応答本体（何チャンク読まれたかを数える）。"""

    def __init__(self, chunk: bytes, count: int) -> None:
        self.chunk = chunk
        self.count = count
        self.yielded = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self.count):
            self.yielded += 1
            yield self.chunk


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """SlackClient 内部の httpx.AsyncClient を MockTransport 付きに差し替える。"""
    real = httpx.AsyncClient

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(**kwargs)

    monkeypatch.setattr("teamagent.adapters.slack_client.httpx.AsyncClient", _factory)


async def test_guarded_download_cuts_off_oversized_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全量メモリ展開せず、cap 超過の時点で読むのを止める（OOM 経路を塞ぐ）。"""
    stream = _CountingStream(b"x" * 256 * 1024, 400)  # 100MB 相当

    def _handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    _patch_transport(monkeypatch, _handler)
    client = SlackClient(bot_token="xoxb-test")
    with pytest.raises(SlackFileGuardError, match="TOO_LARGE"):
        await client.download_file_guarded(OK_URL, max_bytes=1024 * 1024)
    # 1MB を超えた直後に切っている（400 チャンク全部は読んでいない）。
    assert stream.yielded <= 6, f"読み過ぎ: {stream.yielded} チャンク"


async def test_guarded_download_rejects_by_content_length_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _CountingStream(b"y" * 1024, 10)

    def _handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"content-length": str(99 * 1024 * 1024)})

    _patch_transport(monkeypatch, _handler)
    client = SlackClient(bot_token="xoxb-test")
    with pytest.raises(SlackFileGuardError, match="TOO_LARGE"):
        await client.download_file_guarded(OK_URL, max_bytes=1024 * 1024)
    assert stream.yielded == 0  # 1 バイトも読んでいない


async def test_guarded_download_rejects_foreign_host_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ホスト不一致は **ネットワークに触れる前**に落ちる（bot token を出さない）。"""
    calls: list[httpx.Request] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, content=b"secret")

    _patch_transport(monkeypatch, _handler)
    client = SlackClient(bot_token="xoxb-test")
    with pytest.raises(SlackFileGuardError):
        await client.download_file_guarded("https://evil.example.com/x.pdf", max_bytes=1024 * 1024)
    assert calls == []


async def test_guarded_download_happy_path_sends_bot_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("authorization", ""))
        return httpx.Response(200, content=b"hello")

    _patch_transport(monkeypatch, _handler)
    client = SlackClient(bot_token="xoxb-test")
    data = await client.download_file_guarded(OK_URL, max_bytes=1024 * 1024)
    assert data == b"hello"
    assert seen == ["Bearer xoxb-test"]
