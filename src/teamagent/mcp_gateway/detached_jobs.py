"""video_algorithm を「同じツールのまま切り離す（detach）」仕組み。

フラグは USE_VIDEO_ALGORITHM_DETACH（既定 OFF）。

背景（2026-09-25 本番実測）: 動画分析は 5 本で 9 分前後かかる。OpenClaw は約 6 分（360 秒）で
その実行を打ち切るため、mcp が 555 秒かけて完走しても結果の戻り先が無く、
利用者には何も届かなかった。

仕組み:
- gateway（``server.dispatch_tool``）が video_algorithm だけを専用の daemon thread で始める。
- ``VIDEO_ALGORITHM_DETACH_AFTER_S``（既定 30 秒）以内に終われば、今までどおり同期で返す
  （キャッシュヒット・入力エラー・処理中リースの競合はここで返る）。
- 超えたら受付の payload（``status=running``）を返し、ジョブは走らせ続ける。完了したら
  **署名検証済み claim** の channel_id・thread_ts へ mcp が ``slack_summary`` を直接投稿する。
- OpenClaw 側の打ち切り（CancelledError）もジョブには伝えない（shield 相当）。完了時に同じく届ける。
- 二重依頼は、プロセス内の登録簿（キー＝検証済み slack_user_id＋正規化した query）で止める。
- 再デプロイ時は、処理中ジョブの宛先へ「システム更新で中断」を送る（``notify_interrupted``）。

段階公開のための env（どれも TD の env で変えられる）:
- ``USE_VIDEO_ALGORITHM_DETACH``: 既定 OFF＝今と完全に同じ（同期のまま）。
- ``VIDEO_ALGORITHM_DETACH_ALLOWED_EMAILS``: カンマ区切り。**空なら誰にも適用しない**
  （``skills/_shared/rollout.py`` の「空＝全員許可」とは逆。
  第 1 段階は小俣さん本人だけを入れる想定）。
- ``VIDEO_ALGORITHM_DETACH_DM_ONLY``: 既定 1＝1 対 1 DM（D…）だけ。
- ``VIDEO_ALGORITHM_DETACH_AFTER_S``: 既定 30・5〜240 に丸める。
- ``VIDEO_ALGORITHM_MAX_BACKGROUND``: 既定 2（全利用者の合計）。超えた分は**順番待ち**にする
  （同期には戻さない。戻すと 360 秒の打ち切りが再発する）。受付文は「順番待ちです」で返し、
  ジョブの thread の中で空きを待ってから skill.run を始める（待っている間は quota を使わない）。
  待ち行列も ``DEFAULT_MAX_QUEUED``（10）件で満杯なら、quota を使う前に「混み合っています」と返す。

終了処理（SIGTERM）: 初回の ``notify_interrupted`` で登録簿に closing の印を立てる。以降の
新しい依頼・切り離しは受付文の代わりに中断文を返し、順番待ちのジョブは始めずに終える。
登録簿の項目は実行開始から ``STALE_AFTER_S``（45 分）を超えたら、次の依頼時に掃除して枠を返す。

利用者向けの文には内部語（job_id・error_code・S3 URL・ツール名）を出さない（SOUL.md の禁止語）。

登録簿（``REGISTRY``）は検索上位チェックの 2 段目（``surface_video_followup.py``）も使う。
同時実行の上限・待ち行列・終了処理の中断通知を共有し、中断文はジョブごとに登録時に渡せる
（``interrupted_message``。無ければ動画分析の中断文）。待ち行列は優先度つきで、利用者が明示的に
頼んだ動画分析（``PRIORITY_EXPLICIT``・既定）を、自動の 2 段目（``PRIORITY_AUTO``）より先に
始める（同じ優先度は来た順）。
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from teamagent.skills._shared.slack_blocks import RichMessage, render_or_none
from teamagent.skills._shared.slack_mrkdwn import markdown_bold_to_mrkdwn

logger = structlog.get_logger(__name__)

# 切り離しの対象ツール（いまは動画分析だけ）。
DETACHABLE_TOOLS = frozenset({"video_algorithm"})

ENABLED_ENV = "USE_VIDEO_ALGORITHM_DETACH"
AFTER_ENV = "VIDEO_ALGORITHM_DETACH_AFTER_S"
ALLOWED_EMAILS_ENV = "VIDEO_ALGORITHM_DETACH_ALLOWED_EMAILS"
DM_ONLY_ENV = "VIDEO_ALGORITHM_DETACH_DM_ONLY"
MAX_BACKGROUND_ENV = "VIDEO_ALGORITHM_MAX_BACKGROUND"

DEFAULT_DETACH_AFTER_S = 30.0
MIN_DETACH_AFTER_S = 5.0
MAX_DETACH_AFTER_S = 240.0
DEFAULT_MAX_BACKGROUND = 2
MAX_MAX_BACKGROUND = 10
# 同時実行の上限を超えた分の待ち行列の上限（全利用者の合計）。超えたら「混み合っています」。
DEFAULT_MAX_QUEUED = 10
# 待ち行列の優先度（大きいほど先に始める）。明示の依頼を、自動で始めた 2 段目に押し出させない。
PRIORITY_EXPLICIT = 1
PRIORITY_AUTO = 0
# 登録簿の項目の寿命（実行開始から）。処理中リース（既定 1800 秒）より長くとる。
STALE_AFTER_S = 45 * 60.0

# 受付文の目安（5 本で 9 分前後の実測に合わせた概算）。
ETA_MINUTES = 10

# 1 対 1 DM の channel_id（personal_memory/gate.py と同じ判定・strip しない）。
_DM_CHANNEL_RE = re.compile(r"D[A-Z0-9]{8,}")
# Slack の message ts（例: 1784424000.000001）。チャンネル直下の依頼をスレッドにする時だけ使う。
_SLACK_TS_RE = re.compile(r"\d{9,}\.\d{1,9}")

# 完了投稿の Slack 往復。1 回あたり短く切り、1 回だけ再試行する。
_POST_TIMEOUT_S = 10
_POST_ATTEMPTS = 2
_POST_RETRY_WAIT_S = 2.0
# 再デプロイ時の中断通知（ECS の stopTimeout 30 秒の枠内に収める）。
_INTERRUPT_POST_TIMEOUT_S = 5
DEFAULT_INTERRUPT_BUDGET_S = 20.0


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes"}


def _float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    if value != value:  # NaN
        value = default
    return min(maximum, max(minimum, value))


def _int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


@dataclass(frozen=True)
class DetachPolicy:
    """切り離しを使うかどうかの決まり（env から読む）。既定は「使わない」。"""

    enabled: bool = False
    detach_after_s: float = DEFAULT_DETACH_AFTER_S
    allowed_emails: frozenset[str] = frozenset()
    dm_only: bool = True
    max_background: int = DEFAULT_MAX_BACKGROUND
    # 待ち行列の上限（env では変えない）。
    max_queued: int = DEFAULT_MAX_QUEUED

    @classmethod
    def from_env(cls) -> DetachPolicy:
        dm_raw = os.environ.get(DM_ONLY_ENV)
        return cls(
            enabled=_truthy(os.environ.get(ENABLED_ENV)),
            detach_after_s=_float_env(
                AFTER_ENV,
                DEFAULT_DETACH_AFTER_S,
                minimum=MIN_DETACH_AFTER_S,
                maximum=MAX_DETACH_AFTER_S,
            ),
            allowed_emails=frozenset(
                e.strip().lower()
                for e in os.environ.get(ALLOWED_EMAILS_ENV, "").split(",")
                if e.strip()
            ),
            # 未設定・空は既定 1（DM だけ）。明示の 0/false/no のときだけ外す。
            dm_only=True
            if dm_raw is None or not dm_raw.strip()
            else dm_raw.strip().lower() not in {"0", "false", "no"},
            max_background=_int_env(
                MAX_BACKGROUND_ENV,
                DEFAULT_MAX_BACKGROUND,
                minimum=0,
                maximum=MAX_MAX_BACKGROUND,
            ),
        )


def load_policy() -> DetachPolicy:
    """呼び出しごとに env を読む（TD 差し替えだけで段階を進められる。テストの差し替え口）。"""
    return DetachPolicy.from_env()


@dataclass(frozen=True)
class Destination:
    """完了投稿の宛先。必ず署名検証済み claim から作る（``_user_context`` の申告値は使わない）。"""

    channel_id: str
    thread_ts: str | None

    @property
    def is_dm(self) -> bool:
        return _DM_CHANNEL_RE.fullmatch(self.channel_id) is not None


def is_dm_channel(channel_id: object) -> bool:
    return isinstance(channel_id, str) and _DM_CHANNEL_RE.fullmatch(channel_id) is not None


def destination_from_claim(verified_caller: Any) -> Destination | None:
    """署名検証済み claim から宛先を作る。

    チャンネル直下へは投げない（スレッドが決まらなければ None）。
    """
    channel = getattr(verified_caller, "channel_id", None)
    if not isinstance(channel, str) or not channel:
        return None
    thread_ts = getattr(verified_caller, "thread_ts", None)
    thread_ts = thread_ts if isinstance(thread_ts, str) and thread_ts else None
    if is_dm_channel(channel) or thread_ts:
        return Destination(channel_id=channel, thread_ts=thread_ts)
    # チャンネル直下の依頼: 依頼メッセージ自体を親にしてスレッドへ返す。
    # message_id が Slack の ts であることは実機未確認のため、ts の形のときだけ使う。
    message_id = getattr(verified_caller, "message_id", None)
    if isinstance(message_id, str) and _SLACK_TS_RE.fullmatch(message_id):
        return Destination(channel_id=channel, thread_ts=message_id)
    return None


def decide(
    policy: DetachPolicy,
    *,
    tool: str,
    verified_caller: Any,
    metadata: dict[str, Any],
) -> tuple[Destination | None, str]:
    """切り離してよいかを決める。

    ``(宛先, 理由)`` を返す。宛先が None なら同期のまま（今と同じ）。
    """
    if tool not in DETACHABLE_TOOLS:
        return None, "tool"
    if not policy.enabled:
        return None, "disabled"
    if verified_caller is None:
        # LEGACY（resolver 無し）や未検証の呼び出しは、宛先を信用できないので切り離さない。
        return None, "legacy"
    if metadata.get("identity_verified") is not True:
        return None, "unverified"
    email = metadata.get("user_email")
    # 照合するのは resolver が解決した email（_user_context の申告値ではない）。
    # 空の allowlist は全員拒否。
    if not isinstance(email, str) or email.strip().lower() not in policy.allowed_emails:
        return None, "not_allowed"
    destination = destination_from_claim(verified_caller)
    if destination is None:
        return None, "no_destination"
    if policy.dm_only and not destination.is_dm:
        return None, "not_dm"
    if policy.max_background <= 0:
        return None, "capacity"
    return destination, "ok"


def inflight_key(slack_user_id: str, query: str) -> str:
    """二重依頼の判定キー（検証済み slack_user_id＋正規化した query）。本数などの引数は含めない。"""
    normalized = " ".join(unicodedata.normalize("NFKC", query or "").casefold().split())
    return f"{slack_user_id}\x1f{normalized}"


# ── 利用者向けの文（内部語を出さない）──────────────────────────────────────


def receipt_text(query: str) -> str:
    return (
        f"🔎 「{query}」の動画分析を続けています。"
        f"終わったらこの会話にお届けします（目安 {ETA_MINUTES} 分）。"
    )


def queued_receipt_text(query: str) -> str:
    """同時実行の上限に当たった依頼の受付文（順番待ち・quota はまだ使っていない）。"""
    return (
        f"🔎 「{query}」の動画分析は順番待ちです。"
        "始まり次第分析し、終わったらこの会話にお届けします。"
    )


def busy_text(query: str) -> str:
    """待ち行列も満杯のときの文（分析は始めていない＝quota は使っていない）。"""
    return (
        f"🔎 「{query}」の動画分析は、いま混み合っています。"
        "まだ始めていない（分析の回数も使っていない）ので、数分後にもう一度依頼してください。"
    )


def in_progress_text(query: str, *, same_conversation: bool) -> str:
    where = "この会話" if same_conversation else "最初にご依頼いただいた会話"
    return f"🔎 「{query}」はまだ分析中です。終わったら{where}にお届けします。"


def interrupted_text(query: str) -> str:
    return (
        f"🔎 「{query}」の動画分析は、システム更新で中断されました。"
        "お手数ですが、同じ内容でもう一度依頼してください。"
    )


_TEMPORARY_FAILURE = (
    "一時的な不具合で分析を完了できませんでした。数分おいて、同じ内容でもう一度依頼してください。"
)
_MEDIA_FAILURE = (
    "動画の取得・変換で一時的な不具合が起き、分析を完了できませんでした。"
    "数分おいて、同じ内容でもう一度依頼してください。"
)
_GENERIC_FAILURE = (
    "分析の途中で問題が起きて完了できませんでした。数分おいて、同じ内容でもう一度依頼してください。"
)


def user_message_for_error(error: BaseException) -> str:
    """skill の RuntimeError（``CODE: 説明`` 形式）を利用者向けの文に写す（コード名は出さない）。"""
    message = str(error) if isinstance(error, RuntimeError) else ""
    code, sep, rest = message.partition(":")
    code = code.strip() if sep else ""
    rest = rest.strip()
    if code in {"VIDEO_QUOTA_EXCEEDED", "VIDEO_QUOTA_PARTIAL_AVAILABLE"} and rest:
        # quota_store.quota_block_message が作る利用者向けの文面（残数と選択肢）をそのまま使う。
        return rest
    if code == "VIDEO_QUOTA_IDENTITY_REQUIRED":
        return (
            "ご依頼者の確認ができなかったため、分析を始められませんでした。"
            "管理者に連絡してください。"
        )
    if code == "VIDEO_ALGORITHM_IN_PROGRESS":
        return (
            "同じ内容の分析がまだ続いています。数分おいて、同じ内容でもう一度依頼してください"
            "（終わっていれば、すぐに結果をお返しします）。"
        )
    if code in {
        "VIDEO_ALGORITHM_CACHE_UNAVAILABLE",
        "VIDEO_ALGORITHM_LEASE_LOST",
        "VIDEO_ALGORITHM_CACHE_COMMIT_FAILED",
    }:
        return _TEMPORARY_FAILURE
    if code.startswith("MEDIA_") or message.startswith("MEDIA_"):
        return _MEDIA_FAILURE
    return _GENERIC_FAILURE


def is_in_progress_error(error: BaseException) -> bool:
    return isinstance(error, RuntimeError) and str(error).startswith("VIDEO_ALGORITHM_IN_PROGRESS")


def error_text(query: str, error: BaseException) -> str:
    return f"🔎 「{query}」の動画分析: {user_message_for_error(error)}"


# skill の _slack_summary が report_url 無しのときに出す行。直接投稿ではファイルを添付しないので
# 事実と違う案内になる。キャッシュ済みの結果からレポートを作り直す経路（Gemini・quota を使わない）
# があるので、再依頼を案内する。
_ATTACHED_REPORT_LINE = "📄 詳細は添付の HTML レポートをご覧ください"
_REPORT_PUBLISH_FAILED_LINE = (
    "📄 レポートの発行に失敗しました。同じ内容でもう一度依頼すると、課金なしで再発行します"
)


def completion_text(output: Any, query: str) -> str:
    """完了投稿の本文。Slack API へ直接出すので `**語**` を mrkdwn の `*語*` に直す。"""
    summary = getattr(output, "slack_summary", "")
    if not isinstance(summary, str) or not summary.strip():
        summary = f"🔎 「{query}」の動画分析が完了しました。"
    if not getattr(output, "report_url", None):
        summary = summary.replace(_ATTACHED_REPORT_LINE, _REPORT_PUBLISH_FAILED_LINE)
    return markdown_bold_to_mrkdwn(summary)


def completion_message(output: Any, *, request_id: str) -> RichMessage | None:
    """完了投稿の Block Kit 版（``video_algorithm/slack_render.py``）。

    描けない（分析した動画が無い・想定外の出力・描画の例外）ときは None＝``completion_text`` の
    文字だけの投稿に戻す（結果を消さない）。
    """

    def _render() -> RichMessage | None:
        from teamagent.skills.video_algorithm.slack_render import completion_message as render

        return render(output)

    return render_or_none(_render, request_id=request_id, kind="video_algorithm")


def slack_escape(text: str) -> str:
    """Slack の制御文字（& < >）をエスケープし、裸の URL を ``<URL>`` で囲む。

    直接投稿の直前に 1 回だけ掛ける。

    本文には第三者のキャプションを読んだ Gemini の出力が入るため、``<!channel>``・``<@U…>``・
    ``<https://…|偽の表示名>`` がそのまま描画されないようにする。

    裸の URL は、エスケープの後で ASCII の範囲だけを ``<URL>`` で囲む。Slack の自動リンクは
    URL の直後に続く全角の文字（「（タイムライン/…）」など）まで URL に含めることがあり、
    09-28 の本番でレポートのリンクが 404 になった。囲むのは本文に見えている URL そのもの
    だけなので、表示名の偽装（``|名前``）は作れない。
    """
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _BARE_URL.sub(_wrap_bare_url, escaped)


# URL に使える ASCII の文字だけ（``|`` と ``<`` ``>`` は含めない）。全角の文字で必ず止まる。
_BARE_URL = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
# 文末の句読点・太字の ``*``・エスケープ済みの ``&gt;`` などは URL に含めない。
# ``_`` と ``-`` は署名つき短縮 URL（base64url）の末尾に来るので削らない。
_URL_TRAILER = re.compile(r"(?:&gt;|&lt;|&amp;|[.,;:!?'\")\]*])+$")


def _wrap_bare_url(match: re.Match[str]) -> str:
    url = match.group(0)
    trailer = _URL_TRAILER.search(url)
    tail = ""
    if trailer is not None:
        url, tail = url[: trailer.start()], url[trailer.start() :]
    if "://" not in url or url.endswith("://"):
        return match.group(0)
    return f"<{url}>{tail}"


# ── Slack 投稿（既存の直接配信と同じ SlackClient.from_env(timeout_seconds=...)）──────


def _slack_client(timeout_seconds: int) -> Any:
    """共有 Bot Token（SLACK_BOT_TOKEN）の SlackClient。テストはここを差し替える。"""
    from teamagent.adapters.slack_client import SlackClient

    return SlackClient.from_env(timeout_seconds=timeout_seconds)


async def _post_once(
    text: str,
    destination: Destination,
    *,
    request_id: str,
    timeout_s: int,
    rich: RichMessage | None = None,
) -> bool:
    """1 回投稿する。``rich`` があれば Block Kit（text は描画側でエスケープ済みの通知文）。

    ``rich`` が無ければ今までどおり ``text`` に ``slack_escape`` を掛けて文字だけで出す。
    Block Kit の文面は部品ごとにエスケープ済みなので、全体には掛けない（自前のリンクが壊れる）。
    """
    slack = _slack_client(timeout_s)
    extra: dict[str, Any] = {}
    if rich is not None:
        body = rich.text
        extra["blocks"] = rich.blocks
    else:
        body = slack_escape(text)
    result = await asyncio.wait_for(
        slack.post_message(
            channel=destination.channel_id,
            text=body,
            request_id=request_id,
            thread_ts=destination.thread_ts,
            **extra,
        ),
        timeout=timeout_s + 1,
    )
    return bool(getattr(result, "ok", False))


async def _post_to_user_dm(text: str, *, user_id: str, request_id: str, timeout_s: int) -> bool:
    """検証済み slack_user_id の DM へ退避投稿する（omiyage_report の本人 DM 退避と同じ型）。"""
    slack = _slack_client(timeout_s)
    channel = await asyncio.wait_for(slack.open_dm(user_id, request_id), timeout=timeout_s + 1)
    if not isinstance(channel, str) or not channel:
        return False
    return await _post_once(
        text,
        Destination(channel_id=channel, thread_ts=None),
        request_id=request_id,
        timeout_s=timeout_s,
    )


def post_to_origin(
    text: str,
    destination: Destination,
    *,
    request_id: str,
    fallback_user_id: str | None = None,
    rich: RichMessage | None = None,
) -> bool:
    """依頼元の会話へ投稿する（ジョブの thread から呼ぶ・新しい event loop で 1 回だけ再試行）。

    - ok=False や接続エラーのときだけ再試行する。タイムアウト系（``TimeoutError``。aiohttp の
      ServerTimeoutError も含む）は Slack 側で届いている可能性があるので、再試行も退避もしない
      （完了投稿が 2 通になるのを避ける）。
    - ``rich``（Block Kit）があれば 1 回目はそれで出す。弾かれたら（invalid_blocks など）2 回目は
      ``text`` だけで出し直す（何も届かない経路を残さない）。
    - 2 回とも届かなければ、``fallback_user_id``（署名検証済みの slack_user_id）の DM へ退避する
      （文字だけ）。
    """
    status = post_to_origin_status(
        text, destination, request_id=request_id, fallback_user_id=fallback_user_id, rich=rich
    )
    return status == "posted"


def post_to_origin_status(
    text: str,
    destination: Destination,
    *,
    request_id: str,
    fallback_user_id: str | None = None,
    rich: RichMessage | None = None,
) -> str:
    """``post_to_origin`` と同じ投稿。結果を ``posted`` / ``uncertain`` / ``failed`` で返す。

    ``uncertain`` はタイムアウト（Slack 側で届いている可能性がある）。呼び出し元が「届いたかも
    しれないので同じ文を別経路で出し直さない」判断に使う（``direct_summary``）。
    """
    for attempt in range(1, _POST_ATTEMPTS + 1):
        # Block Kit は 1 回目だけ。2 回目は今までどおりの文字だけの投稿。
        use_rich = rich if attempt == 1 else None
        try:
            if asyncio.run(
                _post_once(
                    text,
                    destination,
                    request_id=request_id,
                    timeout_s=_POST_TIMEOUT_S,
                    rich=use_rich,
                )
            ):
                logger.info(
                    "video_algorithm_detach_posted",
                    request_id=request_id,
                    attempt=attempt,
                    dm=destination.is_dm,
                    blocks=use_rich is not None,
                )
                return "posted"
            logger.warning(
                "video_algorithm_detach_post_not_ok",
                request_id=request_id,
                attempt=attempt,
                blocks=use_rich is not None,
            )
        except TimeoutError:
            logger.warning(
                "video_algorithm_detach_post_uncertain",
                request_id=request_id,
                attempt=attempt,
                error="TimeoutError",
            )
            return "uncertain"
        except Exception as exc:
            logger.warning(
                "video_algorithm_detach_post_failed",
                request_id=request_id,
                attempt=attempt,
                error=type(exc).__name__,
                blocks=use_rich is not None,
            )
        if attempt < _POST_ATTEMPTS:
            threading.Event().wait(_POST_RETRY_WAIT_S)
    if not fallback_user_id:
        return "failed"
    try:
        ok = asyncio.run(
            _post_to_user_dm(
                text, user_id=fallback_user_id, request_id=request_id, timeout_s=_POST_TIMEOUT_S
            )
        )
    except Exception as exc:
        logger.warning(
            "video_algorithm_detach_dm_fallback_failed",
            request_id=request_id,
            error=type(exc).__name__,
        )
        return "failed"
    logger.info("video_algorithm_detach_dm_fallback", request_id=request_id, ok=ok)
    return "posted" if ok else "failed"


# ── ジョブと登録簿 ─────────────────────────────────────────────────────────

# 完了時（切り離し後）の処理: (result, error, interrupted) を受け取る。gateway が渡す。
DetachedDone = Callable[[Any, BaseException | None, bool], None]


class DetachInterruptedError(RuntimeError):
    """終了処理（closing）に入ったため、順番待ちのジョブを始めずに終えた（skill.run は未実行）。"""

    def __init__(self) -> None:
        super().__init__("VIDEO_ALGORITHM_INTERRUPTED: shutting down before the job started")


# detach() の結果
DETACH_DONE = "done"  # 既に完了していた（呼び出し側が結果を扱う）
DETACH_DETACHED = "detached"  # 切り離した（完了時にジョブの thread が届ける）
DETACH_INTERRUPTED = "interrupted"  # 終了処理中に切り離した（受付文の代わりに中断文を返す）


class DetachedJob:
    """1 回の video_algorithm 実行。状態の出入りはすべて lock の下で行う。

    lock の順序は「ジョブ → 登録簿」。登録簿の lock を持ったままジョブの lock は取らない。
    """

    def __init__(
        self,
        *,
        registry: DetachedJobRegistry,
        key: str,
        tool: str,
        query: str,
        request_id: str,
        destination: Destination,
        target: Callable[[], Any],
        on_detached_done: DetachedDone,
        interrupted_message: str | None = None,
        priority: int = PRIORITY_EXPLICIT,
    ) -> None:
        self.key = key
        self.priority = priority
        self.tool = tool
        self.query = query
        self.request_id = request_id
        self.destination = destination
        # 再デプロイで中断したときの文（None なら動画分析の中断文）。
        self.interrupted_message = interrupted_message
        self._registry = registry
        self._target = target
        self._on_detached_done = on_detached_done
        self._lock = threading.Lock()
        self._running = False
        self._done = False
        self._detached = False
        self._interrupted = False
        self._handled = False
        self._result: Any = None
        self._error: BaseException | None = None
        self._wakers: list[Callable[[], None]] = []

    # 状態の参照（テスト・ログ用）
    @property
    def done(self) -> bool:
        with self._lock:
            return self._done

    @property
    def detached(self) -> bool:
        with self._lock:
            return self._detached

    @property
    def queued(self) -> bool:
        """まだ順番待ち（skill.run を始めていない）なら True。"""
        with self._lock:
            return not self._running and not self._done

    def start(self) -> None:
        thread = threading.Thread(
            target=self._run,
            name=f"{self.tool}-detach-{self.request_id}",
            daemon=True,
        )
        thread.start()

    def _run(self) -> None:
        result: Any = None
        error: BaseException | None = None
        try:
            # 同時実行の枠が空くまで待つ（順番待ち）。終了処理に入ったら始めずに終える。
            if self._registry.acquire_slot(self):
                with self._lock:
                    self._running = True
                try:
                    result = self._target()
                except BaseException as exc:  # thread の外へ漏らさず、結果として扱う
                    error = exc
            else:
                error = DetachInterruptedError()
            with self._lock:
                self._done = True
                self._result = result
                self._error = error
                detached = self._detached
                wakers = list(self._wakers)
                self._wakers.clear()
            if detached:
                self._finish_detached()
            else:
                for wake in wakers:
                    try:
                        wake()
                    except Exception as exc:
                        logger.warning(
                            "video_algorithm_detach_wake_failed",
                            request_id=self.request_id,
                            error=type(exc).__name__,
                        )
        finally:
            self._registry.release(self)

    def add_waker(self, wake: Callable[[], None]) -> None:
        """完了時に呼ぶ関数を登録する（すでに完了していれば即呼ぶ）。"""
        with self._lock:
            if not self._done:
                self._wakers.append(wake)
                return
        wake()

    def detach(self) -> str:
        """切り離す。

        - まだ終わっていなければ ``DETACH_DETACHED``（完了時に thread が届ける）。
        - 終了処理（closing）に入っていれば ``DETACH_INTERRUPTED``。中断扱いにするので、完了しても
          投稿はしない（後始末と usage 記録だけ行う）。呼び出し側は受付文の代わりに中断文を返す。
        - 既に終わっていれば ``DETACH_DONE``。
        """
        with self._lock:
            if self._done:
                return DETACH_DONE
            self._detached = True
            if self._registry.closing and not self._interrupted:
                self._interrupted = True
                return DETACH_INTERRUPTED
            return DETACH_DETACHED

    def outcome(self) -> Any:
        """完了済みジョブの結果を返す（失敗なら skill の例外をそのまま送出する）。"""
        with self._lock:
            if not self._done:
                raise RuntimeError("detached job is still running")
            error = self._error
            result = self._result
        if error is not None:
            raise error
        return result

    def deliver_in_background(self) -> None:
        """完了済みなのに受け取り手が居なくなった（打ち切られた）ときに、別 thread で届ける。"""
        with self._lock:
            self._detached = True
        thread = threading.Thread(
            target=self._finish_detached,
            name=f"{self.tool}-detach-deliver-{self.request_id}",
            daemon=True,
        )
        thread.start()

    def interrupted_notice(self) -> str:
        """このジョブの中断文（登録時に指定が無ければ動画分析の中断文）。"""
        return self.interrupted_message or interrupted_text(self.query)

    def post_interrupted_in_background(self) -> None:
        """終了処理中に打ち切られた（返す相手が居ない）ときに、中断文を別 thread で投稿する。"""
        thread = threading.Thread(
            target=post_to_origin,
            args=(self.interrupted_notice(), self.destination),
            kwargs={"request_id": self.request_id},
            name=f"{self.tool}-detach-interrupt-{self.request_id}",
            daemon=True,
        )
        thread.start()

    def mark_interrupted(self) -> bool:
        """再デプロイで中断扱いにする（切り離し済み・未完了のジョブだけ True）。"""
        with self._lock:
            if self._done or not self._detached or self._interrupted:
                return False
            self._interrupted = True
            return True

    def _finish_detached(self) -> None:
        with self._lock:
            if self._handled:
                return
            self._handled = True
            result = self._result
            error = self._error
            interrupted = self._interrupted
        try:
            self._on_detached_done(result, error, interrupted)
        except Exception as exc:
            logger.warning(
                "video_algorithm_detach_finish_failed",
                request_id=self.request_id,
                error=type(exc).__name__,
            )


class DetachedJobRegistry:
    """プロセス内の in-flight 登録簿（キー＝検証済み slack_user_id＋正規化 query）。

    実行中（枠を持つ）と順番待ちの両方を載せる。二重依頼の判定・同時実行の上限・待ち行列の上限・
    終了処理（closing）の印・古い項目の掃除をここで行う。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._jobs: dict[str, DetachedJob] = {}
        self._waiting: deque[DetachedJob] = deque()
        # 枠を持っているジョブ → 実行開始の monotonic 時刻（寿命の判定に使う）
        self._running: dict[DetachedJob, float] = {}
        self._limit = DEFAULT_MAX_BACKGROUND
        self._closing = False

    @property
    def closing(self) -> bool:
        with self._lock:
            return self._closing

    def get(self, key: str) -> DetachedJob | None:
        with self._lock:
            self._sweep_stale_locked()
            return self._jobs.get(key)

    def active_count(self) -> int:
        with self._lock:
            return len(self._jobs)

    def queued_count(self) -> int:
        with self._lock:
            return len(self._waiting)

    def _sweep_stale_locked(self) -> None:
        """実行開始から STALE_AFTER_S を超えた項目を外して枠を返す（skill.run が戻らない保険）。"""
        now = time.monotonic()
        stale = [job for job, began in self._running.items() if now - began > STALE_AFTER_S]
        for job in stale:
            del self._running[job]
            if self._jobs.get(job.key) is job:
                del self._jobs[job.key]
            logger.warning(
                "video_algorithm_detach_stale_swept",
                request_id=job.request_id,
                stale_after_s=STALE_AFTER_S,
            )
        if stale:
            self._cond.notify_all()

    def start(
        self,
        *,
        key: str,
        max_background: int,
        tool: str,
        query: str,
        request_id: str,
        destination: Destination,
        target: Callable[[], Any],
        on_detached_done: DetachedDone,
        max_queued: int = DEFAULT_MAX_QUEUED,
        interrupted_message: str | None = None,
        priority: int = PRIORITY_EXPLICIT,
    ) -> tuple[DetachedJob | None, str]:
        """登録して開始する（枠が空いていなければ、ジョブの thread の中で順番を待つ）。

        返り値は ``(job, "started")`` / ``(既存 job, "duplicate")`` /
        ``(None, "busy")``（実行中＋待ちが上限まで埋まっている）/
        ``(None, "closing")``（終了処理中）。
        """
        with self._lock:
            if self._closing:
                return None, "closing"
            self._sweep_stale_locked()
            existing = self._jobs.get(key)
            if existing is not None:
                return existing, "duplicate"
            if len(self._jobs) >= max(0, max_background) + max(0, max_queued):
                return None, "busy"
            job = DetachedJob(
                registry=self,
                key=key,
                tool=tool,
                query=query,
                request_id=request_id,
                destination=destination,
                target=target,
                on_detached_done=on_detached_done,
                interrupted_message=interrupted_message,
                priority=priority,
            )
            self._jobs[key] = job
            self._enqueue_locked(job)
            self._limit = max_background
        try:
            job.start()
        except BaseException:
            self.release(job)
            raise
        return job, "started"

    def _enqueue_locked(self, job: DetachedJob) -> None:
        """優先度の高い順（同じ優先度は来た順）に待ち行列へ入れる。"""
        for i, waiting in enumerate(self._waiting):
            if waiting.priority < job.priority:
                self._waiting.insert(i, job)
                return
        self._waiting.append(job)

    def acquire_slot(self, job: DetachedJob) -> bool:
        """ジョブの thread から呼ぶ。

        先頭の順番が来て枠が空いたら True、終了処理に入ったら False。
        """
        with self._cond:
            while True:
                if self._closing or job not in self._waiting:
                    if job in self._waiting:
                        self._waiting.remove(job)
                    self._cond.notify_all()
                    return False
                if self._waiting[0] is job and len(self._running) < self._limit:
                    self._waiting.popleft()
                    self._running[job] = time.monotonic()
                    self._cond.notify_all()
                    return True
                # 掃除（寿命切れ）でも枠が空くので、たまに起きて確かめる。
                self._cond.wait(timeout=5.0)
                self._sweep_stale_locked()

    def release(self, job: DetachedJob) -> None:
        with self._cond:
            self._running.pop(job, None)
            if job in self._waiting:
                self._waiting.remove(job)
            if self._jobs.get(job.key) is job:
                del self._jobs[job.key]
            self._cond.notify_all()

    def interrupt_all(self) -> list[DetachedJob]:
        """終了処理に入る印を立て、切り離し済みで未完了のジョブを中断扱いにして返す。

        印を立てた後の新しい依頼・切り離しは中断文を返し、順番待ちのジョブは始めずに終わる。
        """
        with self._cond:
            self._closing = True
            jobs = list(self._jobs.values())
            self._cond.notify_all()
        return [job for job in jobs if job.mark_interrupted()]


REGISTRY = DetachedJobRegistry()


async def notify_interrupted(
    *,
    budget_s: float = DEFAULT_INTERRUPT_BUDGET_S,
    registry: DetachedJobRegistry | None = None,
) -> int:
    """再デプロイ（プロセス終了）時に、処理中ジョブの宛先へ中断を知らせる。

    何度呼んでも 1 回だけ送る。
    """
    reg = registry or REGISTRY
    jobs = reg.interrupt_all()
    if not jobs:
        return 0
    logger.warning("video_algorithm_detach_interrupted", count=len(jobs))

    async def _one(job: DetachedJob) -> None:
        try:
            ok = await _post_once(
                job.interrupted_notice(),
                job.destination,
                request_id=job.request_id,
                timeout_s=_INTERRUPT_POST_TIMEOUT_S,
            )
            logger.info(
                "video_algorithm_detach_interrupt_notified", request_id=job.request_id, ok=ok
            )
        except Exception as exc:
            logger.warning(
                "video_algorithm_detach_interrupt_notify_failed",
                request_id=job.request_id,
                error=type(exc).__name__,
            )

    try:
        await asyncio.wait_for(asyncio.gather(*(_one(job) for job in jobs)), timeout=budget_s)
    except TimeoutError:
        logger.warning("video_algorithm_detach_interrupt_budget_exceeded", count=len(jobs))
    return len(jobs)


__all__ = [
    "DETACHABLE_TOOLS",
    "DETACH_DETACHED",
    "DETACH_DONE",
    "DETACH_INTERRUPTED",
    "PRIORITY_AUTO",
    "PRIORITY_EXPLICIT",
    "REGISTRY",
    "Destination",
    "DetachInterruptedError",
    "DetachPolicy",
    "DetachedJob",
    "DetachedJobRegistry",
    "busy_text",
    "completion_message",
    "completion_text",
    "decide",
    "destination_from_claim",
    "error_text",
    "in_progress_text",
    "inflight_key",
    "interrupted_text",
    "is_dm_channel",
    "is_in_progress_error",
    "load_policy",
    "notify_interrupted",
    "post_to_origin",
    "post_to_origin_status",
    "queued_receipt_text",
    "receipt_text",
    "slack_escape",
    "user_message_for_error",
]
