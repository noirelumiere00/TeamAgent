"""回答評価ボタン（👍/👎）の MCP 側。利用者のメッセージへの返信に付く。

流れ（docs は本ファイルと plugin の ANSWER_FEEDBACK 節が正本）:
  1. OpenClaw の caller-identity plugin が、メッセージ起点の run の返信の直後に
     「この回答は役に立ちましたか？ 👍 / 👎」の小さなメッセージを同じスレッド／DM へ投稿する。
     ボタンの value は plugin が鋳造した **署名トークン**（下の形）。
  2. 押下は Socket Mode で plugin に届き、plugin が隠しツール ``answer_feedback_record`` を
     予約 ID（``aico-fb-<32hex>``・run_id == tool_call_id）と署名済み caller claim で直接呼ぶ。
  3. ここでトークンを検証し（署名・期限・押した人＝質問した人・team）、``search_feedback`` に
     1 行 INSERT する。app ロールは INSERT-only（0022）なので上書きは「追記＋集計時に最後の 1 票」。

トークンの形（plugin の mintAnswerFeedbackToken と 1 対 1）:
  ``base64url(JSON payload) "." base64url(HMAC-SHA256(key, payload_segment)[:16])``
  key = HMAC-SHA256(TEAMAGENT_CALLER_CLAIM_SECRET, KEY_LABEL)（claim の署名とは鍵を分ける）
  payload = {"v":1, "typ":"afb", "q":検索語または元の発言(≤300字), "a":回答ID(16hex),
             "u":質問者 U…, "t":team T…, "e":失効 epoch 秒, "k":使用ツール名(任意・最大5個)}
  k がない旧 v1 トークンも受け付ける。使用ツール名は既存の note 列に保存する。
HMAC が守るのは改竄防止と質問者への束縛（完全性）で、秘匿ではない（ack_token と同じ）。
ボタンは質問した会話（本人の DM か、本人が質問したスレッド）にだけ置かれる。

このツールは list_tools に出さず、ToolSpec にも factory にも入れない（personal_memory と同じ扱い）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

ANSWER_FEEDBACK_TOOL_NAME: Final = "answer_feedback_record"
ANSWER_FEEDBACK_FLAG_ENV: Final = "USE_ANSWER_FEEDBACK_TOOL"

# plugin の ANSWER_FEEDBACK_KEY_LABEL と同じ値（変えるときは両方）。
KEY_LABEL: Final = b"teamagent-answer-feedback-key-v1"
TOKEN_VERSION: Final = 1
TOKEN_TYPE: Final = "afb"
SIG_LEN: Final = 16
# 評価は後から押されることがあるので 7 日（plugin の ANSWER_FEEDBACK_TOKEN_TTL_S と同じ）。
TOKEN_TTL_S: Final = 7 * 24 * 60 * 60
MAX_QUERY_CHARS: Final = 300
MAX_TOKEN_CHARS: Final = 2000
MAX_TOOL_NAMES: Final = 5
MAX_TOOL_NAME_CHARS: Final = 64

RESERVED_INVOCATION_RE: Final = re.compile(r"aico-fb-[0-9a-f]{32}")
_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,1977}\.[A-Za-z0-9_-]{22}")
_ANSWER_ID_RE: Final = re.compile(r"[0-9a-f]{16}")
_SLACK_USER_RE: Final = re.compile(r"U[A-Z0-9]{8,}")
_SLACK_TEAM_RE: Final = re.compile(r"T[A-Z0-9]{8,}")
_PAYLOAD_FIELDS: Final = frozenset({"v", "typ", "q", "a", "u", "t", "e"})
_TOOL_NAME_RE: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class AnswerFeedbackInput(BaseModel):
    """plugin から届く引数（_user_context を除いた業務部分）。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    feedback_token: str = Field(min_length=24, max_length=MAX_TOKEN_CHARS)
    # bool は int の部分型なので StrictInt で弾く（True を 👍 として通さない）。
    rating: StrictInt

    @field_validator("rating")
    @classmethod
    def _rating_is_thumb(cls, value: int) -> int:
        if value not in (-1, 1):
            raise ValueError("rating must be -1 or 1")
        return value


class AnswerFeedbackTokenError(ValueError):
    """トークンの拒否。code は応答・ログに載せてよい固定の識別子（中身を含めない）。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AnswerFeedbackClaim:
    """検証済みトークンの中身。"""

    query: str
    answer_id: str
    slack_user_id: str
    slack_team_id: str
    expires_at: int
    tools: tuple[str, ...] = ()


def reserved_invocation_ok(tool_call_id: object, run_id: object) -> bool:
    """plugin が評価の記録のために作った呼び出し ID か（モデル経由の呼び出しを通さない）。"""
    if not isinstance(tool_call_id, str) or not isinstance(run_id, str):
        return False
    return RESERVED_INVOCATION_RE.fullmatch(tool_call_id) is not None and run_id == tool_call_id


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(segment: str) -> bytes:
    try:
        return base64.b64decode(segment + "=" * (-len(segment) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as error:
        raise AnswerFeedbackTokenError("AFB_TOKEN_INVALID") from error


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AnswerFeedbackTokenError("AFB_TOKEN_INVALID")
        result[key] = value
    return result


def _signature(key: bytes, payload_segment: str) -> bytes:
    return hmac.new(key, payload_segment.encode("ascii"), hashlib.sha256).digest()[:SIG_LEN]


def encode_feedback_token(
    *,
    key: bytes,
    query: str,
    answer_id: str,
    slack_user_id: str,
    slack_team_id: str,
    expires_at: int,
    tools: Sequence[str] | None = None,
) -> str:
    """トークンを作る（本番の鋳造は plugin。ここはテストと形の正本のため）。"""
    payload: dict[str, Any] = {
        "v": TOKEN_VERSION,
        "typ": TOKEN_TYPE,
        "q": query,
        "a": answer_id,
        "u": slack_user_id,
        "t": slack_team_id,
        "e": expires_at,
    }
    if tools is not None:
        payload["k"] = list(tools)
    segment = _b64e(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    return f"{segment}.{_b64e(_signature(key, segment))}"


def verify_feedback_token(
    token: str,
    *,
    key: bytes,
    now: int,
    presser_user_id: str,
    team_id: str,
) -> AnswerFeedbackClaim:
    """署名・形・期限・押した人＝質問した人・team を検証する。どれか外れたら拒否（fail-closed）。

    押した人（presser_user_id）と team は署名済み caller claim の値だけを渡すこと。
    """
    if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
        raise AnswerFeedbackTokenError("AFB_TOKEN_INVALID")
    payload_segment, signature_segment = token.split(".", 1)
    if not hmac.compare_digest(_b64d(signature_segment), _signature(key, payload_segment)):
        raise AnswerFeedbackTokenError("AFB_TOKEN_INVALID")
    try:
        payload = json.loads(_b64d(payload_segment), object_pairs_hook=_reject_duplicate_keys)
    except AnswerFeedbackTokenError:
        raise
    except Exception as error:
        raise AnswerFeedbackTokenError("AFB_TOKEN_INVALID") from error
    if not isinstance(payload, dict) or frozenset(payload) not in (
        _PAYLOAD_FIELDS,
        _PAYLOAD_FIELDS | {"k"},
    ):
        raise AnswerFeedbackTokenError("AFB_TOKEN_INVALID")
    tools = payload.get("k", [])
    if (
        not isinstance(tools, list)
        or len(tools) > MAX_TOOL_NAMES
        or any(not isinstance(tool, str) or _TOOL_NAME_RE.fullmatch(tool) is None for tool in tools)
    ):
        raise AnswerFeedbackTokenError("AFB_TOKEN_INVALID")
    version = payload["v"]
    expires_at = payload["e"]
    query = payload["q"]
    answer_id = payload["a"]
    owner = payload["u"]
    team = payload["t"]
    if (
        isinstance(version, bool)
        or version != TOKEN_VERSION
        or payload["typ"] != TOKEN_TYPE
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or not isinstance(query, str)
        or not query.strip()
        or len(query) > MAX_QUERY_CHARS
        or _CONTROL_RE.search(query) is not None
        or not isinstance(answer_id, str)
        or _ANSWER_ID_RE.fullmatch(answer_id) is None
        or not isinstance(owner, str)
        or _SLACK_USER_RE.fullmatch(owner) is None
        or not isinstance(team, str)
        or _SLACK_TEAM_RE.fullmatch(team) is None
    ):
        raise AnswerFeedbackTokenError("AFB_TOKEN_INVALID")
    if expires_at <= now:
        raise AnswerFeedbackTokenError("AFB_TOKEN_EXPIRED")
    # 鋳造時の TTL より先の失効は作れない（鍵が漏れていない限り起きない形の防壁）。
    if expires_at - now > TOKEN_TTL_S + 300:
        raise AnswerFeedbackTokenError("AFB_TOKEN_INVALID")
    if team != team_id:
        raise AnswerFeedbackTokenError("AFB_TOKEN_INVALID")
    if owner != presser_user_id:
        raise AnswerFeedbackTokenError("AFB_NOT_OWNER")
    return AnswerFeedbackClaim(
        query=query.strip(),
        answer_id=answer_id,
        slack_user_id=owner,
        slack_team_id=team,
        expires_at=expires_at,
        tools=tuple(tools),
    )


def slack_search_session_id(answer_id: str) -> str:
    """search_feedback.search_session_id に入れる値（Slack 由来の印＋回答ID）。

    Web UI（/search）の値はフロント生成の UUID なので、``slack-`` 接頭辞で出どころを分ける。
    """
    return f"slack-{answer_id}"


__all__ = [
    "ANSWER_FEEDBACK_FLAG_ENV",
    "ANSWER_FEEDBACK_TOOL_NAME",
    "KEY_LABEL",
    "TOKEN_TTL_S",
    "AnswerFeedbackClaim",
    "AnswerFeedbackInput",
    "AnswerFeedbackTokenError",
    "encode_feedback_token",
    "reserved_invocation_ok",
    "slack_search_session_id",
    "verify_feedback_token",
]
