"""Gmail クライアント（取り込み + 下書き作成）。

CLAUDE.md 6-bis Adapter 層。Skill から googleapiclient を直接呼ばない。
Sprint 3 / PR-3 で雛形を導入。S3-11 / S3-12 / Sprint 8 (Phase 4-b) で本実装。

設計判断（2026-05-26 セキュリティ Agent 調査 + ユーザー確認）:
- **OAuth スコープ**: `gmail.modify` 1 本（**Sensitive Tier 2**、CASA 不要）
  - 読み + 下書き作成 + ラベル管理を 1 スコープで満たす
  - `gmail.readonly` は Restricted Tier 3（CASA 必須）なので避ける
- **隠しラベル管理**: ユーザー UI に見せず TeamAgent の状態管理に使う
  - `TeamAgent/processed`: 取り込み済（重複処理防止）
  - `TeamAgent/draft-pending`: 下書き作成中
  - `TeamAgent/error`: 処理失敗
  - `TeamAgent/skip`: ユーザーが除外指定
  - 全て labelListVisibility=labelHide + messageListVisibility=hide
- **ACL**: thread 参加者の email を documents.acl_emails に写像
- **idempotency**: Gmail `messageId` を documents.external_id (UNIQUE) に使用

Usage:
    client = GmailClient.from_env()
    msgs, next_token = client.list_messages(query="from:client@x.com", request_id="r")
    for m in msgs:
        full = client.get_message(m.id, request_id="r")
        text = extract_plain_text(full.payload)
        client.add_labels(m.id, ["TeamAgent/processed"], request_id="r")

テスト時は FakeGmailService を inject:
    fake = FakeGmailService(messages_response=[...])
    client = GmailClient(service=fake)
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from typing import Any

import structlog

from teamagent.adapters.oauth_token_store import OAuthToken
from teamagent.observability import capture_skill_exception

logger = structlog.get_logger(__name__)


# -----------------------------------------------------------
# 破壊的メソッド denylist（adapter 層で物理封鎖）
# -----------------------------------------------------------
# OAuth スコープは `gmail.modify` で広い権限を持つが、コード側で「破壊的メソッドを
# 物理的に呼べない」状態にする（Day 6, 2026-05-26 設計判断）。
#
# 出典: Google Gmail API v1 公式リファレンス
#   https://developers.google.com/gmail/api/reference/rest
# - users.messages.delete / batchDelete: 完全削除（ゴミ箱経由せず）
# - users.messages.trash / untrash: ゴミ箱送り / 復元
# - users.threads.delete / trash / untrash: スレッド単位の削除・ゴミ箱送り
# - users.labels.delete / patch / update: ラベル削除・更新（隠しラベルが書き換わるリスク）
# - users.settings.filters.delete: フィルタルール削除
# - users.settings.forwardingAddresses.delete: 自動転送先削除
# - users.settings.sendAs.delete: 送信元エイリアス削除
# - users.settings.cse.identities.delete: クライアントサイド暗号化 ID 削除
# - users.settings.cse.keypairs.disable / obliterate: CSE 鍵ペア無効化・完全消去
# - users.watch / stop: Pub/Sub プッシュ通知の有効化・停止（外部副作用を伴う）
_GMAIL_DESTRUCTIVE_METHODS: frozenset[str] = frozenset(
    {
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
        # 送信は物理封鎖（gmail.modify は送信も許すが、本 Bot は「下書き作成まで・送信は人間」。
        # drafts.create は許可、drafts.send / messages.send は封鎖）。
        "users.messages.send",
        "users.drafts.send",
    }
)


class _GmailSafePolicy:
    """Gmail API method path を denylist 評価する policy。

    Skill / runtime / テストいずれの経路でも `_PolicyEnforcedResource` 経由で
    `assert_safe(method_path)` が呼ばれ、`_GMAIL_DESTRUCTIVE_METHODS` に含まれる
    method を呼んだ瞬間に RuntimeError を上げて Sentry に通知する。

    スコープ (`gmail.modify`) では封じられない破壊的呼び出しを物理封鎖するための
    最終防衛層。
    """

    def __init__(
        self,
        *,
        denylist: frozenset[str] = _GMAIL_DESTRUCTIVE_METHODS,
    ) -> None:
        self._denylist = denylist

    def assert_safe(self, method_path: str) -> None:
        if method_path not in self._denylist:
            return
        logger.error(
            "gmail_destructive_call_blocked",
            method_path=method_path,
            policy="GmailSafePolicy",
            scope="gmail.modify",
        )
        exc = RuntimeError(
            f"Gmail destructive method '{method_path}' is blocked by adapter-layer denylist. "
            "Even though the OAuth scope grants write access, this method is physically "
            "unreachable through GmailClient. If this call is legitimate, the policy must "
            "be revised explicitly (see _GMAIL_DESTRUCTIVE_METHODS)."
        )
        # Sentry に send（DSN 未設定環境では no-op）。ここで raise する前に通知する。
        capture_skill_exception(
            exc,
            request_id="gmail_adapter_policy",
            skill="gmail_adapter",
            extra={"method_path": method_path},
        )
        raise exc


class _PolicyEnforcedResource:
    """googleapiclient.discovery.Resource を method path 付きで包む wrapper。

    __getattr__ で属性アクセスを intercept し、呼び出しチェーンを点ドット形式の
    method path に組み立てつつ wrapper を返す。末端で `execute()` を呼ぶ直前に
    `policy.assert_safe(method_path)` を発火させて、denylist 該当呼び出しを
    RuntimeError に転化する。

    例：
        service.users().messages().delete(id="x").execute()
                └ "users" └ "messages" └ "delete" → method_path="users.messages.delete"

    Resource.execute() は API call の最終トリガーなので、ここで block すれば
    実際の HTTP リクエストは絶対に走らない。

    注意:
    - googleapiclient は属性アクセスのたびに新しい Resource を返す
      （`.users()` は呼び出しのたび別インスタンス）。本 wrapper も同じ要領で
      新しい `_PolicyEnforcedResource` を返し、path を蓄積する。
    - `execute` / `_resource` / `_policy` / `_path` は wrapper 自身の属性なので、
      被ラップ resource への通り抜けが起きないよう __getattr__ より優先する。
    """

    __slots__ = ("_path", "_policy", "_resource")

    def __init__(
        self,
        resource: Any,
        policy: _GmailSafePolicy,
        *,
        path: tuple[str, ...] = (),
    ) -> None:
        object.__setattr__(self, "_resource", resource)
        object.__setattr__(self, "_policy", policy)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name: str) -> Any:
        # __slots__ 経由の属性は通常の属性アクセスで返るのでここに来ない。
        inner = getattr(self._resource, name)
        if callable(inner):
            policy = self._policy
            current_path = self._path

            def _wrapped(*args: Any, **kwargs: Any) -> Any:
                next_path = (*current_path, name)
                result = inner(*args, **kwargs)
                # 末端: execute() を持つ HttpRequest が返ってきたら、
                # execute 前に policy 判定して block する。
                if hasattr(result, "execute") and callable(result.execute):
                    method_path = ".".join(next_path)
                    return _PolicyEnforcedHttpRequest(result, policy, method_path)
                # 中間: 別 Resource なので再帰的に包む
                return _PolicyEnforcedResource(result, policy, path=next_path)

            return _wrapped
        return inner


class _PolicyEnforcedHttpRequest:
    """googleapiclient HttpRequest の execute() を policy で gate する wrapper。"""

    __slots__ = ("_method_path", "_policy", "_request")

    def __init__(self, request: Any, policy: _GmailSafePolicy, method_path: str) -> None:
        object.__setattr__(self, "_request", request)
        object.__setattr__(self, "_policy", policy)
        object.__setattr__(self, "_method_path", method_path)

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self._policy.assert_safe(self._method_path)
        return self._request.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._request, name)


# -----------------------------------------------------------
# データ型
# -----------------------------------------------------------
@dataclass(frozen=True)
class GmailMessageRef:
    """list_messages の戻り値 1 件（軽量、id + threadId のみ）。"""

    id: str
    thread_id: str


@dataclass(frozen=True)
class GmailMessage:
    """get_message format='full' / 'metadata' の戻り値。

    payload は再帰的な MIME 構造（parts に子）なので生 dict で保持し、
    extract_plain_text() で本文を取り出す責務を呼び出し側に持たせる。
    """

    id: str
    thread_id: str
    label_ids: tuple[str, ...]
    snippet: str
    internal_date_ms: int | None  # epoch ms（modified_at 比較用）
    headers: dict[str, str]  # From / To / Cc / Subject / Date
    payload: dict[str, Any]  # 生 MIME tree（extract_plain_text 用）


@dataclass(frozen=True)
class GmailLabel:
    """labels.list / labels.create の戻り値。

    type: 'system' | 'user'
    label_list_visibility: 'labelShow' | 'labelHide' | 'labelShowIfUnread'
    message_list_visibility: 'show' | 'hide'
    """

    id: str
    name: str
    type: str
    label_list_visibility: str | None = None
    message_list_visibility: str | None = None


@dataclass(frozen=True)
class GmailDraft:
    """drafts.create の戻り値。"""

    id: str
    message_id: str
    thread_id: str | None = None


# TeamAgent が内部状態管理に使う隠しラベル名（labelHide / hide で UI 非表示）
class TeamAgentLabels:
    PROCESSED = "TeamAgent/processed"
    DRAFT_PENDING = "TeamAgent/draft-pending"
    ERROR = "TeamAgent/error"
    SKIP = "TeamAgent/skip"

    @classmethod
    def all(cls) -> tuple[str, ...]:
        return (cls.PROCESSED, cls.DRAFT_PENDING, cls.ERROR, cls.SKIP)


# -----------------------------------------------------------
# クライアント本体
# -----------------------------------------------------------
class GmailClient:
    """Gmail API v1 の薄ラッパー。

    認証パターン（gdrive_client.py と統一）：
      1. OAuth リフレッシュトークン（推奨）: GOOGLE_OAUTH_REFRESH_TOKEN
         + GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
      2. Service Account + DWD: GOOGLE_APPLICATION_CREDENTIALS=path/to/sa.json
         + GOOGLE_GMAIL_IMPERSONATE_USER=user@workspace.co.jp（DWD で impersonate）
    """

    # gmail.modify: 読み + 下書き + ラベル + 送信（送信は呼び出し側で要承認ガード）
    # Sensitive Tier 2 → CASA 不要、Google verification は必要
    SCOPES_MODIFY: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.modify",)
    # 万一 readonly に絞る場合（Restricted Tier 3、CASA 必須）
    SCOPES_READONLY: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.readonly",)

    def __init__(
        self,
        credentials: Any | None = None,
        *,
        service: Any | None = None,
        scopes: tuple[str, ...] | None = None,
        impersonate_user: str | None = None,
        safe_policy: _GmailSafePolicy | None = None,
    ) -> None:
        self._credentials = credentials
        self._service = service
        self._scopes = scopes or self.SCOPES_MODIFY
        self._impersonate_user = impersonate_user
        self._safe_policy = safe_policy or _GmailSafePolicy()
        # service が事前注入されていれば（テスト経路）即座にラップする。
        # 未注入なら _ensure_safe_service() の最初の呼び出しで遅延構築する。
        self._service_safe: Any | None = (
            _PolicyEnforcedResource(service, self._safe_policy) if service is not None else None
        )

    @classmethod
    def from_env(
        cls, *, readonly: bool = False, impersonate_user: str | None = None
    ) -> GmailClient:
        scopes = cls.SCOPES_READONLY if readonly else cls.SCOPES_MODIFY
        # 明示 impersonate_user を優先（本人受信箱を呼び出し側で束縛）。
        # 未指定時のみ env GOOGLE_GMAIL_IMPERSONATE_USER へフォールバック（後方互換）。
        if impersonate_user is None:
            impersonate_user = os.environ.get("GOOGLE_GMAIL_IMPERSONATE_USER")
        if not (
            os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        ):
            logger.warning(
                "gmail_credentials_missing",
                hint=(
                    "GOOGLE_CLIENT_ID + refresh token、または "
                    "GOOGLE_APPLICATION_CREDENTIALS + GOOGLE_GMAIL_IMPERSONATE_USER を設定"
                ),
            )
        return cls(credentials=None, scopes=scopes, impersonate_user=impersonate_user)

    @classmethod
    def from_user_token(cls, token: OAuthToken, *, readonly: bool = True) -> GmailClient:
        """per-user: 本人の refresh token から構築（本人の受信箱のみ参照可）。"""
        from teamagent.adapters.google_auth import build_user_credentials

        scopes = cls.SCOPES_READONLY if readonly else cls.SCOPES_MODIFY
        return cls(credentials=build_user_credentials(token), scopes=scopes)

    # -------------------------------------------------------
    # メッセージ一覧
    # -------------------------------------------------------
    def list_messages(
        self,
        query: str | None,
        request_id: str,
        *,
        label_ids: list[str] | None = None,
        max_results: int = 50,
        page_token: str | None = None,
        include_spam_trash: bool = False,
        user_id: str = "me",
    ) -> tuple[list[GmailMessageRef], str | None]:
        """messages.list を 1 ページ取得。

        query は Gmail 検索クエリ（例 'from:foo@x.com newer_than:7d'）。
        label_ids で系統的に絞る（例 ['INBOX', '<隠しラベル ID>']）。
        """
        service = self._ensure_safe_service()
        kwargs: dict[str, Any] = {
            "userId": user_id,
            "maxResults": max_results,
            "includeSpamTrash": include_spam_trash,
        }
        if query:
            kwargs["q"] = query
        if label_ids:
            kwargs["labelIds"] = label_ids
        if page_token:
            kwargs["pageToken"] = page_token

        start = time.perf_counter()
        resp = service.users().messages().list(**kwargs).execute()
        latency_ms = int((time.perf_counter() - start) * 1000)

        raw = resp.get("messages", []) or []
        msgs = [
            GmailMessageRef(id=str(m.get("id", "")), thread_id=str(m.get("threadId", "")))
            for m in raw
        ]
        next_token = resp.get("nextPageToken")
        logger.info(
            "gmail_list_messages",
            request_id=request_id,
            query_len=len(query) if query else 0,
            label_count=len(label_ids) if label_ids else 0,
            returned=len(msgs),
            has_next=bool(next_token),
            latency_ms=latency_ms,
        )
        return msgs, next_token

    # -------------------------------------------------------
    # メッセージ取得
    # -------------------------------------------------------
    def get_message(
        self,
        msg_id: str,
        request_id: str,
        *,
        format: str = "full",  # 'full' | 'metadata' | 'raw' | 'minimal'
        user_id: str = "me",
    ) -> GmailMessage:
        """messages.get でメッセージ詳細を取得する。"""
        service = self._ensure_safe_service()
        kwargs: dict[str, Any] = {"userId": user_id, "id": msg_id, "format": format}
        if format == "metadata":
            kwargs["metadataHeaders"] = ["From", "To", "Cc", "Subject", "Date"]

        start = time.perf_counter()
        resp = service.users().messages().get(**kwargs).execute()
        latency_ms = int((time.perf_counter() - start) * 1000)

        msg = _message_from_resp(resp)
        logger.info(
            "gmail_get_message",
            request_id=request_id,
            msg_id=msg_id,
            format=format,
            label_count=len(msg.label_ids),
            latency_ms=latency_ms,
        )
        return msg

    def get_thread(
        self,
        thread_id: str,
        request_id: str,
        *,
        format: str = "full",  # 'full' | 'metadata' | 'minimal'
        user_id: str = "me",
    ) -> list[GmailMessage]:
        """threads.get でスレッド内の全メッセージを時系列に取得する（read-only）。

        返信下書きに「これまでの経緯」を渡すために使う。`users.threads.get` は
        denylist 非該当（破壊的でない）なので _ensure_safe_service() 経由でそのまま通る。
        """
        service = self._ensure_safe_service()
        start = time.perf_counter()
        resp = service.users().threads().get(userId=user_id, id=thread_id, format=format).execute()
        latency_ms = int((time.perf_counter() - start) * 1000)
        raw_msgs = resp.get("messages", []) or []
        msgs = [_message_from_resp(m) for m in raw_msgs]
        logger.info(
            "gmail_get_thread",
            request_id=request_id,
            thread_id=thread_id,
            message_count=len(msgs),
            latency_ms=latency_ms,
        )
        return msgs

    # -------------------------------------------------------
    # ラベル管理（隠しラベルで TeamAgent 状態を管理）
    # -------------------------------------------------------
    def list_labels(self, request_id: str, *, user_id: str = "me") -> list[GmailLabel]:
        """全ラベルを列挙する（hidden 含む）。"""
        service = self._ensure_safe_service()
        resp = service.users().labels().list(userId=user_id).execute()
        raw = resp.get("labels", []) or []
        labels = [
            GmailLabel(
                id=str(label.get("id", "")),
                name=str(label.get("name", "")),
                type=str(label.get("type", "user")),
                label_list_visibility=label.get("labelListVisibility"),
                message_list_visibility=label.get("messageListVisibility"),
            )
            for label in raw
        ]
        logger.info("gmail_list_labels", request_id=request_id, count=len(labels))
        return labels

    def create_hidden_label(
        self,
        name: str,
        request_id: str,
        *,
        user_id: str = "me",
    ) -> GmailLabel:
        """ユーザー UI に見えない隠しラベルを作る。

        labelListVisibility=labelHide → 左サイドバーに表示しない
        messageListVisibility=hide → メール一覧でラベル chip 非表示
        """
        service = self._ensure_safe_service()
        body = {
            "name": name,
            "labelListVisibility": "labelHide",
            "messageListVisibility": "hide",
            "type": "user",
        }
        resp = service.users().labels().create(userId=user_id, body=body).execute()
        label = GmailLabel(
            id=str(resp.get("id", "")),
            name=str(resp.get("name", name)),
            type=str(resp.get("type", "user")),
            label_list_visibility=resp.get("labelListVisibility"),
            message_list_visibility=resp.get("messageListVisibility"),
        )
        logger.info("gmail_create_hidden_label", request_id=request_id, name=name, id=label.id)
        return label

    def ensure_team_agent_labels(self, request_id: str, *, user_id: str = "me") -> dict[str, str]:
        """TeamAgent 標準の隠しラベル群を「無ければ作る」。

        Returns: {label_name: label_id}
        """
        existing = {label.name: label.id for label in self.list_labels(request_id, user_id=user_id)}
        result: dict[str, str] = {}
        for name in TeamAgentLabels.all():
            if name in existing:
                result[name] = existing[name]
                continue
            created = self.create_hidden_label(name, request_id, user_id=user_id)
            result[name] = created.id
        logger.info(
            "gmail_team_agent_labels_ready", request_id=request_id, label_ids=list(result.values())
        )
        return result

    def modify_message_labels(
        self,
        msg_id: str,
        request_id: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        user_id: str = "me",
    ) -> None:
        """メッセージのラベルを追加 / 削除する。

        add / remove は label_id を渡す（名前ではなく ID）。
        ensure_team_agent_labels() の戻り値を使うのが楽。
        """
        service = self._ensure_safe_service()
        body: dict[str, Any] = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        if not body:
            return
        service.users().messages().modify(userId=user_id, id=msg_id, body=body).execute()
        logger.info(
            "gmail_modify_message_labels",
            request_id=request_id,
            msg_id=msg_id,
            added=len(add or []),
            removed=len(remove or []),
        )

    # -------------------------------------------------------
    # 下書き作成（gmail.modify スコープに含まれる）
    # -------------------------------------------------------
    def create_draft(
        self,
        *,
        to: str,
        subject: str,
        body_text: str,
        request_id: str,
        thread_id: str | None = None,
        cc: str | None = None,
        in_reply_to_message_id: str | None = None,
        user_id: str = "me",
    ) -> GmailDraft:
        """下書きを作成する。Gmail UI の「下書き」フォルダに現れる。

        thread_id を渡すと既存スレッドへの返信 draft として作成される。
        in_reply_to_message_id を渡すと In-Reply-To / References ヘッダを設定。
        """
        service = self._ensure_safe_service()
        raw_email = _build_raw_email(
            to=to,
            subject=subject,
            body_text=body_text,
            cc=cc,
            in_reply_to_message_id=in_reply_to_message_id,
        )
        message: dict[str, Any] = {"raw": raw_email}
        if thread_id:
            message["threadId"] = thread_id
        body = {"message": message}
        resp = service.users().drafts().create(userId=user_id, body=body).execute()
        msg = resp.get("message", {}) or {}
        draft = GmailDraft(
            id=str(resp.get("id", "")),
            message_id=str(msg.get("id", "")),
            thread_id=msg.get("threadId"),
        )
        logger.info(
            "gmail_create_draft",
            request_id=request_id,
            draft_id=draft.id,
            thread_id=draft.thread_id,
            subject_len=len(subject),
            body_len=len(body_text),
        )
        return draft

    def list_drafts(
        self,
        request_id: str,
        *,
        max_results: int = 100,
        max_pages: int = 20,
        user_id: str = "me",
    ) -> list[GmailDraft]:
        """既存の下書きを全ページ列挙する（readonly・drafts.list）。

        冪等性に使う：毎日の digest が同じスレッドに下書きを二重作成しないよう、
        既存下書きの thread_id 集合を引く。drafts.list は 1 ページ最大 100 件＋nextPageToken の
        ため、**ページングしないと 51 件目以降を取りこぼし冪等性が壊れる**（重複下書き）。
        max_pages で暴走を防ぎつつ全ページ辿る。本文・件名は取得しない（G3/G7）。
        """
        service = self._ensure_safe_service()
        start = time.perf_counter()
        out: list[GmailDraft] = []
        page_token: str | None = None
        for _ in range(max_pages):
            kwargs: dict[str, Any] = {"userId": user_id, "maxResults": max_results}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = service.users().drafts().list(**kwargs).execute()
            for d in resp.get("drafts", []) or []:
                m = d.get("message", {}) or {}
                out.append(
                    GmailDraft(
                        id=str(d.get("id", "")),
                        message_id=str(m.get("id", "")),
                        thread_id=m.get("threadId"),
                    )
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "gmail_list_drafts", request_id=request_id, count=len(out), latency_ms=latency_ms
        )
        return out

    # -------------------------------------------------------
    # 内部
    # -------------------------------------------------------
    def _ensure_service(self) -> Any:
        """Raw googleapiclient Resource を返す。直接の API 呼び出し用ではない。

        ⚠️ このメソッドは internal/debug 用途のみ。実 API 呼び出しは必ず
        `_ensure_safe_service()` 経由で denylist 評価される wrapper を使う。
        """
        if self._service is not None:
            return self._service
        from googleapiclient.discovery import build

        if self._credentials is None:
            self._credentials = self._build_credentials()
        self._service = build("gmail", "v1", credentials=self._credentials, cache_discovery=False)
        return self._service

    def _ensure_safe_service(self) -> Any:
        """破壊的メソッドが物理封鎖された Gmail Resource を返す。

        全 public メソッドはこれを経由する。`_GmailSafePolicy` で `users.messages.delete`
        などの denylist 該当呼び出しを `execute()` 直前に RuntimeError に転化する。
        """
        if self._service_safe is not None:
            return self._service_safe
        raw = self._ensure_service()
        self._service_safe = _PolicyEnforcedResource(raw, self._safe_policy)
        return self._service_safe

    def _build_credentials(self) -> Any:
        sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if sa_path:
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(
                sa_path, scopes=list(self._scopes)
            )
            # DWD: impersonate user 指定で全社員になりすまし可能（要 Workspace Admin 承認）
            if self._impersonate_user:
                creds = creds.with_subject(self._impersonate_user)
            return creds
        refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
        client_id = os.environ.get("GOOGLE_CLIENT_ID")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
        if refresh_token and client_id and client_secret:
            from google.oauth2.credentials import Credentials

            return Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=list(self._scopes),
            )
        raise NotImplementedError(
            "Gmail credentials が未設定です。Sprint 3 の S3-09 で OAuth クライアントを "
            "用意してから本実装。テストでは service=Fake service を渡してください。"
        )


# -----------------------------------------------------------
# ヘルパー: MIME 解析 / ACL 抽出
# -----------------------------------------------------------
def _message_from_resp(resp: dict[str, Any]) -> GmailMessage:
    """messages.get / threads.get の 1 メッセージ dict を GmailMessage へ写像する。"""
    payload = resp.get("payload", {}) or {}
    headers_list = payload.get("headers", []) or []
    headers = {h.get("name", ""): h.get("value", "") for h in headers_list if h.get("name")}
    return GmailMessage(
        id=str(resp.get("id", "")),
        thread_id=str(resp.get("threadId", "")),
        label_ids=tuple(resp.get("labelIds", []) or ()),
        snippet=str(resp.get("snippet", "")),
        internal_date_ms=int(resp["internalDate"]) if resp.get("internalDate") else None,
        headers=headers,
        payload=payload,
    )


def extract_plain_text(payload: dict[str, Any]) -> str:
    """payload (MIME tree) から text/plain 部分を抽出する。

    multipart の場合は parts を再帰探索。text/html しか無い場合は空文字を返す
    （HTML パースは別レイヤー責務、ここでは plain text のみ）。
    """

    def _walk(node: dict[str, Any]) -> str | None:
        mime = node.get("mimeType", "")
        if mime == "text/plain":
            data = (node.get("body") or {}).get("data")
            if data:
                return _decode_b64url(data)
        for child in node.get("parts", []) or []:
            text = _walk(child)
            if text:
                return text
        return None

    return _walk(payload) or ""


def _decode_b64url(s: str) -> str:
    """Gmail 本文は base64url エンコード。padding 補完してデコード。"""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad).decode("utf-8", errors="replace")


def extract_thread_participants(headers: dict[str, str]) -> list[str]:
    """From / To / Cc ヘッダから email を抽出する。documents.acl_emails 用。

    "Foo <foo@x.com>, bar@y.com" → ['foo@x.com', 'bar@y.com']
    重複は保持しない（呼び出し側で set 化推奨）。
    """
    import re

    out: list[str] = []
    pattern = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
    for field in ("From", "To", "Cc", "Bcc"):
        v = headers.get(field, "")
        if not v:
            continue
        out.extend(pattern.findall(v))
    # 順序保持の dedup
    seen: set[str] = set()
    result: list[str] = []
    for e in out:
        lower = e.lower()
        if lower not in seen:
            seen.add(lower)
            result.append(e)
    return result


def _build_raw_email(
    *,
    to: str,
    subject: str,
    body_text: str,
    cc: str | None = None,
    in_reply_to_message_id: str | None = None,
) -> str:
    """RFC 2822 email を base64url で encode した raw string を返す。

    Gmail API drafts.create の body.message.raw に渡す形式。
    EmailMessage + base64.urlsafe_b64encode で組み立てる（subject / body 日本語対応）。
    """
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    if in_reply_to_message_id:
        msg["In-Reply-To"] = in_reply_to_message_id
        msg["References"] = in_reply_to_message_id
    msg.set_content(body_text)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
