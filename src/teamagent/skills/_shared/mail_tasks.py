"""本人のメールから依頼・期間・ボールを判定する。本文は LLM に渡さない。"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import re
import unicodedata
from collections.abc import Sequence
from email.utils import getaddresses, parseaddr
from typing import Any, Literal

from bs4 import BeautifulSoup

from teamagent.adapters.gmail_client import extract_plain_text
from teamagent.identity import shared_company_domains_from_env
from teamagent.observability import scrub_value
from teamagent.skills._shared.mail_compose import gmail_thread_url, should_skip_mail
from teamagent.skills._shared.timefmt import jst_display_or_none, jst_iso_or_none
from teamagent.skills.mail_followup.schema import FollowupItem
from teamagent.skills.morning_digest.calendar_window import JST, now_jst

MailPeriod = Literal["", "今日", "昨日", "今週", "先週", "今月"]

_REQUEST = re.compile(
    r"[?？]|(?:お願い|御願い|依頼(?:です|します|いたします)|ください|下さい|教えて|いただけ|頂け|頂戴|願います|"
    r"ますか|でしょうか|ませんか|いかが|どうでしょう|要返信|締切|締め切り|期限|納期)|"
    r"(?:本日|今日|明日|今週|来週|\d{1,2}[月/日])[^。\n]{0,20}まで|"
    r"\b(?:please|could you|can you|would you|deadline|due by)\b",
    re.IGNORECASE,
)
_COURTESY = re.compile(
    r"^(?:(?:何卒|どうぞ|引き続き|今後とも|以上|それでは)[、,\s]*)*"
    r"よろしく(?:お願い|御願い)(?:します|いたします|致します|申し上げます)[!！\s]*$|"
    r"^(?:何か|ご不明|不明点|ご質問|ご不都合).*(?:ございましたら|ありましたら|あれば|ある場合)",
)
_NO_ACTION = re.compile(
    r"返信(?:は|の)?(?:不要|不要です|必要ありません)|対応(?:は|の)?不要|"
    r"ご(?:返信|対応)(?:は)?(?:不要|には及びません)|ご放念|"
    r"\b(?:no (?:reply|response|action) (?:is )?(?:needed|required)|fyi)\b",
    re.IGNORECASE,
)
_QUOTE_START = re.compile(
    r"^\s*(?:On .+wrote:|[-_]{2,}\s*(?:Original Message|Forwarded message|転送|元のメッセージ)|"
    r"Begin forwarded message:|From:|差出人:|\d{4}年.+(?:書きました|<))",
    re.IGNORECASE,
)
_AUTOMATIC = re.compile(
    r"mailer-daemon|postmaster|notifications?[@<]|自動(?:送信|配信|通知)|"
    r"メルマガ|メールマガジン|ニュースレター|\bnewsletter\b",
    re.IGNORECASE,
)


def headers_of(msg: Any) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (getattr(msg, "headers", {}) or {}).items()}


def addresses(value: str) -> set[str]:
    return {addr.strip().lower() for _, addr in getaddresses([value]) if addr.strip()}


def addressed_to(msg: Any, requester: str) -> bool:
    h = headers_of(msg)
    return requester.lower() in (addresses(h.get("to", "")) | addresses(h.get("cc", "")))


def from_self(msg: Any, requester: str) -> bool:
    return "SENT" in getattr(msg, "label_ids", ()) or requester.lower() in addresses(
        headers_of(msg).get("from", "")
    )


def sent_messages(thread: Sequence[Any]) -> list[Any]:
    return sorted(
        (m for m in thread if "DRAFT" not in getattr(m, "label_ids", ())),
        key=lambda m: getattr(m, "internal_date_ms", 0) or 0,
    )


def period_bounds(period: MailPeriod, days: int, now_ms: int | None) -> tuple[int, int]:
    now = dt.datetime.fromtimestamp(now_ms / 1000, JST) if now_ms is not None else now_jst()
    now = now.astimezone(JST)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now + dt.timedelta(milliseconds=1)
    start = now - dt.timedelta(days=days)
    if period == "今日":
        start = today
    elif period == "昨日":
        start, end = today - dt.timedelta(days=1), today
    elif period in ("今週", "先週"):
        start = today - dt.timedelta(days=today.weekday())
        if period == "先週":
            start, end = start - dt.timedelta(days=7), start
    elif period == "今月":
        start = today.replace(day=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def period_query(period: MailPeriod, days: int, now_ms: int | None) -> str:
    if not period:
        return f"newer_than:{days}d"
    start, end = period_bounds(period, days, now_ms)
    # Gmail after は秒精度で排他的。境界の 1 秒を含め、ms の最終判定はコードで行う。
    return f"after:{start // 1000 - 1} before:{(end + 999) // 1000}"


def in_period(msg: Any, bounds: tuple[int, int]) -> bool:
    stamp = getattr(msg, "internal_date_ms", None)
    return stamp is not None and bounds[0] <= stamp < bounds[1]


def message_text(payload: dict[str, Any]) -> str:
    plain = extract_plain_text(payload)
    if plain:
        return plain
    if payload.get("mimeType") == "text/html":
        data = (payload.get("body") or {}).get("data", "")
        html = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
            "utf-8", errors="replace"
        )
        soup = BeautifulSoup(html, "html.parser")
        for quote in soup.select("blockquote, .gmail_quote, .gmail_signature, script, style"):
            quote.decompose()
        return soup.get_text(" ")
    return "\n".join(message_text(part) for part in payload.get("parts", []))


def request_summary(msg: Any) -> str:
    h = headers_of(msg)
    if should_skip_mail(h) or _AUTOMATIC.search(h.get("from", "") + " " + h.get("subject", "")):
        return ""
    body = message_text(getattr(msg, "payload", {}) or {})
    current: list[str] = []
    for line in body.splitlines():
        if _QUOTE_START.search(line) or line.strip() in ("--", "-- "):
            break
        if not line.lstrip().startswith(">"):
            current.append(line)
    text = unicodedata.normalize("NFKC", "\n".join(current))
    if _NO_ACTION.search(text):
        return ""
    for sentence in re.split(r"[。\n]+|(?<=[!?])\s*", text):
        if _REQUEST.search(sentence) and not _COURTESY.search(sentence.strip()):
            # shortcut: 定型の質問・依頼・期限を抽出。婉曲表現が漏れる場合は語彙を追加する。
            return " ".join(str(scrub_value(sentence)).split())[:120]
    return ""


def unreplied_request(thread: Sequence[Any], requester: str, bounds: tuple[int, int]) -> Any | None:
    pending = None
    for msg in sent_messages(thread):
        if from_self(msg, requester):
            pending = None
        elif addressed_to(msg, requester) and in_period(msg, bounds) and request_summary(msg):
            if parseaddr(headers_of(msg).get("from", ""))[1]:
                pending = msg
    return pending


def ball_request(
    thread: Sequence[Any], requester: str, bounds: tuple[int, int]
) -> tuple[Any, bool] | None:
    messages = sent_messages(thread)
    if not messages or not any(addressed_to(m, requester) for m in messages):
        return None
    last = messages[-1]
    if not in_period(last, bounds) or not request_summary(last):
        return None
    sender = parseaddr(headers_of(last).get("from", ""))[1].lower()
    if not sender:
        return None
    domains = (shared_company_domains_from_env() or frozenset()) | {
        requester.rsplit("@", 1)[-1].lower()
    }
    own_side = from_self(last, requester) or sender.rsplit("@", 1)[-1] in domains
    if own_side:
        h = headers_of(last)
        external = [
            addr
            for _, addr in getaddresses([h.get("to", ""), h.get("cc", "")])
            if "@" in addr and addr.rsplit("@", 1)[-1].lower() not in domains
        ]
        if not external:
            return None
    return last, own_side


def task_item(msg: Any, requester: str, now_ms: int, *, outgoing: bool = False) -> FollowupItem:
    h = headers_of(msg)
    candidates = getaddresses([h.get("from", "")])
    if outgoing:
        domains = (shared_company_domains_from_env() or frozenset()) | {
            requester.rsplit("@", 1)[-1].lower()
        }
        candidates = [
            (name, addr)
            for name, addr in getaddresses([h.get("to", ""), h.get("cc", "")])
            if "@" in addr and addr.rsplit("@", 1)[-1].lower() not in domains
        ]
    name, addr = candidates[0] if candidates else ("", "")
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else "不明"
    # 表示名に会社が無ければ、ドメインを根拠として表示する（会社名・姓を創作しない）。
    display = str(scrub_value(name))[:60] if name and "@" not in name else "氏名不明"
    stamp = getattr(msg, "internal_date_ms", None)
    return FollowupItem(
        counterpart_masked=f"{addr[:1]}***@{domain}" if addr else "***",
        counterpart_name=display,
        company_domain=domain,
        request_summary=request_summary(msg),
        mail_url=gmail_thread_url(str(getattr(msg, "thread_id", ""))),
        subject_scrubbed=str(scrub_value(h.get("subject", "")))[:80],
        idle_days=max(0, (now_ms - (stamp or now_ms)) // 86_400_000),
        occurred_at=jst_iso_or_none(stamp),
        occurred_at_display=jst_display_or_none(stamp),
        evidence_ref=hashlib.sha256(str(msg.id).encode()).hexdigest()[:12],
    )


def render_task(item: FollowupItem) -> str:
    return (
        f"{item.counterpart_name}（{item.company_domain}）「{item.subject_scrubbed}」 / "
        f"{item.occurred_at_display or '日時不明'} / {item.request_summary} / {item.mail_url}"
    )
