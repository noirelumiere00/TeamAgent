"""adapters/gmail_client.py の denylist 物理封鎖テスト。

設計の前提:
- OAuth スコープ `gmail.modify` は破壊的メソッドを許可してしまう（広いスコープ）
- そのため adapter 層で「破壊的メソッドを物理的に呼べない」状態にする（Day 6, 2026-05-26）

ここで検証するもの:
1. `_GMAIL_DESTRUCTIVE_METHODS` の snapshot（16 個以上、Google 公式 docs ベース）
2. 各 destructive method を mock GmailClient 経由で呼んだとき RuntimeError が出る
3. 違反時に structured log "gmail_destructive_call_blocked" が ERROR レベルで出る
4. 非破壊メソッド（list / get / send / create_draft）は wrapper を素通りする

テストは実 Gmail API を呼ばない。`_FakeServiceRoot` で
googleapiclient.discovery.Resource の体裁だけを模した stub を注入する。
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
import structlog
from structlog.testing import capture_logs

from teamagent.adapters.gmail_client import (
    _GMAIL_DESTRUCTIVE_METHODS,
    GmailClient,
    _GmailSafePolicy,
    _PolicyEnforcedResource,
)


# -----------------------------------------------------------
# Fake googleapiclient Resource
# -----------------------------------------------------------
class _FakeHttpRequest:
    """googleapiclient の HttpRequest 相当。execute() を持つだけ。"""

    def __init__(self, tag: str = "ok") -> None:
        self._tag = tag
        self.executed = False

    def execute(self) -> dict[str, Any]:
        self.executed = True
        return {"ok": True, "tag": self._tag}


class _FakeMethodChain:
    """属性アクセスを再帰的に解決して最終的に _FakeHttpRequest を返す Resource stub。

    例: chain.users().messages().delete(id="x") → _FakeHttpRequest("delete")
    末端メソッドは何でも呼べる（args/kwargs を吸収）ようにし、
    googleapiclient の動的 attribute discovery を模す。
    """

    # 中間段（path セグメント）として扱うキー
    _INTERMEDIATES: ClassVar[set[str]] = {
        "users",
        "messages",
        "threads",
        "labels",
        "drafts",
        "settings",
        "filters",
        "forwardingAddresses",
        "sendAs",
        "cse",
        "identities",
        "keypairs",
    }
    # 末端段（execute() に到達するメソッド）として扱うキー
    _TERMINALS: ClassVar[set[str]] = {
        "delete",
        "batchDelete",
        "trash",
        "untrash",
        "patch",
        "update",
        "list",
        "get",
        "create",
        "send",
        "modify",
        "disable",
        "obliterate",
        "watch",
        "stop",
        # 120点ハードニングで追加した封鎖対象メソッド
        # （googleapiclient は予約語 import を import_ に改名＝実属性名で持つ。v0.3 Task2 修正）
        "import_",
        "insert",
        "verify",
        "enable",
        "updateAutoForwarding",
        "updateImap",
        "updatePop",
        "updateVacation",
        "updateLanguage",
    }

    def __init__(self) -> None:
        self.last_terminal: str | None = None

    def __getattr__(self, name: str) -> Any:
        if name in self._INTERMEDIATES:

            def _intermediate(*_args: Any, **_kwargs: Any) -> _FakeMethodChain:
                return self  # 同じ chain を返すだけ（path 自体は wrapper 側で追跡）

            return _intermediate
        if name in self._TERMINALS:

            def _terminal(*_args: Any, **_kwargs: Any) -> _FakeHttpRequest:
                self.last_terminal = name
                return _FakeHttpRequest(tag=name)

            return _terminal
        raise AttributeError(name)


class _FakeServiceRoot:
    """googleapiclient.discovery.Resource のトップレベル相当（service.users() ... の起点）。"""

    def __init__(self) -> None:
        self._chain = _FakeMethodChain()

    def users(self) -> _FakeMethodChain:
        return self._chain

    def watch(self) -> _FakeHttpRequest:
        """ルート直下の users.watch ではない方の watch（Gmail には無いが念のため）。"""
        return _FakeHttpRequest(tag="root_watch")


# -----------------------------------------------------------
# 1. Denylist snapshot（16 個以上、Google 公式 docs ベース）
# -----------------------------------------------------------
def test_destructive_methods_contains_at_least_16_entries() -> None:
    """denylist は最低 16 個。意図せず縮んでいないことを担保する snapshot。"""
    assert len(_GMAIL_DESTRUCTIVE_METHODS) >= 16


# Google Gmail API v1 公式ドキュメントから直接挙げた破壊的 method path。
# https://developers.google.com/gmail/api/reference/rest
_EXPECTED_DESTRUCTIVE_METHODS = (
    "users.messages.delete",
    "users.messages.batchDelete",
    "users.messages.trash",
    "users.messages.untrash",
    "users.threads.delete",
    "users.threads.trash",
    "users.threads.untrash",
    "users.labels.delete",
    "users.labels.patch",
    "users.labels.update",
    "users.settings.filters.delete",
    "users.settings.forwardingAddresses.delete",
    "users.settings.sendAs.delete",
    "users.settings.cse.identities.delete",
    "users.settings.cse.keypairs.disable",
    "users.settings.cse.keypairs.obliterate",
    "users.watch",
    "users.stop",
    # gmail.modify 付与に伴い送信系も物理封鎖（下書き作成 drafts.create のみ許可）。
    "users.messages.send",
    "users.drafts.send",
    # 注入・改竄・情報持ち出し（exfiltration）系を物理封鎖（120点ハードニング）。
    "users.messages.import_",
    "users.messages.insert",
    "users.threads.modify",
    "users.drafts.update",
    "users.drafts.delete",
    "users.settings.updateAutoForwarding",
    "users.settings.forwardingAddresses.create",
    "users.settings.sendAs.create",
    "users.settings.sendAs.update",
    "users.settings.filters.create",
    "users.settings.cse.keypairs.create",
)


@pytest.mark.parametrize(
    "method_path",
    [
        "users.messages.list",
        "users.messages.get",
        "users.threads.get",
        "users.labels.list",
        "users.labels.create",  # 隠しラベル作成（使用）
        "users.messages.modify",  # ラベル付け（使用）
        "users.drafts.create",  # 下書き作成（使用）
        "users.drafts.list",  # 冪等性（使用）
    ],
)
def test_legit_methods_remain_allowed(method_path: str) -> None:
    """Bot が実際に使う read/label/draft 系は封鎖しない（過剰封鎖の回帰防止）。"""
    assert method_path not in _GMAIL_DESTRUCTIVE_METHODS
    _GmailSafePolicy().assert_safe(method_path)  # 例外が出なければOK


@pytest.mark.parametrize("method_path", _EXPECTED_DESTRUCTIVE_METHODS)
def test_each_expected_destructive_method_is_in_denylist(method_path: str) -> None:
    """公式 docs ベースの 18 メソッドが全て denylist に含まれることを検証。"""
    assert method_path in _GMAIL_DESTRUCTIVE_METHODS


# -----------------------------------------------------------
# 2. 各 destructive method を呼ぶと RuntimeError + log が出る
# -----------------------------------------------------------
def _invoke_method_via_wrapper(client: GmailClient, method_path: str) -> None:
    """`users.messages.delete` 形式の path を辿って末端 execute() を呼ぶ。

    末端の手前まで途中段は `()` で呼ぶ（googleapiclient と同じ流儀）。
    """
    segments = method_path.split(".")
    node: Any = client._ensure_safe_service()
    for seg in segments[:-1]:
        node = getattr(node, seg)()
    terminal = getattr(node, segments[-1])
    request = terminal()  # delete(...) / trash(...) / watch(...) を呼ぶ
    request.execute()


@pytest.mark.parametrize("method_path", _EXPECTED_DESTRUCTIVE_METHODS)
def test_destructive_method_raises_runtime_error(method_path: str) -> None:
    fake = _FakeServiceRoot()
    client = GmailClient(service=fake)
    with pytest.raises(RuntimeError) as ei:
        _invoke_method_via_wrapper(client, method_path)
    # error message に method_path が含まれて、原因特定が容易であること
    assert method_path in str(ei.value)
    # 実 service の末端メソッドは呼ばれない（execute 直前で block）
    # _FakeMethodChain.last_terminal は terminal() 呼び出し時にセットされるので、
    # _terminal は呼ばれているが execute() はされていない（HttpRequest.executed=False）
    # ここでは「policy が必ず block する」のみ確認する。


def test_destructive_method_emits_structured_log() -> None:
    """違反時に structlog ERROR で "gmail_destructive_call_blocked" が出ること。"""
    fake = _FakeServiceRoot()
    client = GmailClient(service=fake)
    structlog.configure(processors=[structlog.testing.LogCapture()])
    with capture_logs() as logs:
        with pytest.raises(RuntimeError):
            _invoke_method_via_wrapper(client, "users.messages.delete")
    blocked = [r for r in logs if r.get("event") == "gmail_destructive_call_blocked"]
    assert len(blocked) == 1
    rec = blocked[0]
    assert rec["log_level"] == "error"
    assert rec["method_path"] == "users.messages.delete"
    assert rec["policy"] == "GmailSafePolicy"


# -----------------------------------------------------------
# 3. 非破壊メソッドは wrapper を素通り
# -----------------------------------------------------------
_SAFE_METHODS = (
    "users.messages.list",
    "users.messages.get",
    "users.messages.modify",
    "users.threads.list",
    "users.threads.get",
    "users.labels.list",
    "users.labels.get",
    "users.labels.create",
    "users.drafts.create",  # 下書き作成は許可（送信 drafts.send は封鎖）
    "users.drafts.get",
    "users.drafts.list",
)


@pytest.mark.parametrize("method_path", _SAFE_METHODS)
def test_safe_method_passes_through(method_path: str) -> None:
    """非破壊メソッドは wrapper を素通りして execute() の戻り値が返ること。"""
    fake = _FakeServiceRoot()
    client = GmailClient(service=fake)
    segments = method_path.split(".")
    node: Any = client._ensure_safe_service()
    for seg in segments[:-1]:
        node = getattr(node, seg)()
    request = getattr(node, segments[-1])()
    result = request.execute()
    assert result == {"ok": True, "tag": segments[-1]}


# -----------------------------------------------------------
# 4. _GmailSafePolicy 単体の挙動
# -----------------------------------------------------------
def test_safe_policy_assert_safe_allows_unknown_method() -> None:
    """denylist に無い method はそのまま通る（明示的に許可せず deny only）。"""
    _GmailSafePolicy().assert_safe("users.messages.list")
    _GmailSafePolicy().assert_safe("some.future.method")


def test_safe_policy_assert_safe_blocks_destructive() -> None:
    with pytest.raises(RuntimeError):
        _GmailSafePolicy().assert_safe("users.messages.delete")


def test_policy_enforced_resource_wraps_intermediate_calls() -> None:
    """中間段の attribute は再帰的に _PolicyEnforcedResource になる。"""
    fake = _FakeServiceRoot()
    wrapped = _PolicyEnforcedResource(fake, _GmailSafePolicy())
    users = wrapped.users()
    # 中間段は wrapper のまま（execute 直前まで policy 評価を遅延）
    assert isinstance(users, _PolicyEnforcedResource)


# -----------------------------------------------------------
# 5. client.__init__ で safe wrapper が自動構築されること
# -----------------------------------------------------------
def test_client_init_builds_service_safe_when_service_injected() -> None:
    fake = _FakeServiceRoot()
    client = GmailClient(service=fake)
    assert client._service_safe is not None
    assert isinstance(client._service_safe, _PolicyEnforcedResource)


def test_client_ensure_safe_service_returns_wrapper() -> None:
    fake = _FakeServiceRoot()
    client = GmailClient(service=fake)
    safe = client._ensure_safe_service()
    raw = client._ensure_service()
    assert safe is not raw
    assert isinstance(safe, _PolicyEnforcedResource)
