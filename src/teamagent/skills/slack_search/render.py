"""slack_search の純粋関数群（公開範囲の判定・1 件の整形・返答文の組み立て）。

I/O を持たないのでテストで直接叩ける。判定の核は :func:`classify_visibility`。

公開範囲の判定に使う search.messages の値（Slack API 仕様 search.messages の応答例と、
「IM の一致は ``type`` が ``"im"``・``channel.name`` は相手の user ID」という記述が根拠）:
  - ``channel.id`` の接頭辞: ``C`` = チャンネル（公開・非公開の両方あり）、
    ``G`` = 旧式の非公開チャンネル／グループ DM、``D`` = DM。
  - ``channel.is_private`` / ``channel.is_mpim``: 応答例に載っている真偽値。
  - ``type == "im"``: DM の一致。
  - ``channel.is_im`` / ``channel.is_group``: 応答例には無いが、
    返ってきたら非公開側へ倒す材料に使う。
**公開と言えるのは「C 始まり かつ is_private が False かつ is_mpim が False」だけ**。
値が欠けている・bool でない一致は ``unknown``（公開ではない）＝fail-closed。
"""

from __future__ import annotations

import datetime as _dt
import re

from teamagent.adapters.slack_user_reader import SlackSearchMatch
from teamagent.skills._shared.grapheme_cut import truncate_graphemes
from teamagent.skills._shared.source_url import slack_permalink
from teamagent.skills.slack_search.schema import SlackSearchHit, Visibility
from teamagent.skills.slack_summary.skill import _defuse_slack_pings

_JST = _dt.timezone(_dt.timedelta(hours=9))
EXCERPT_CHARS = 200
_QUERY_ECHO_CHARS = 60
# Slack が返す permalink の形（ワークスペース / Enterprise Grid の両方）。これ以外は使わない。
_PERMALINK_RE = re.compile(
    r"^https://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.slack\.com/archives/"
    r"[A-Za-z0-9]+/p\d+(?:\?[A-Za-z0-9_.=&%-]*)?$"
)


def classify_visibility(match: SlackSearchMatch) -> Visibility:
    """一致した場所の公開範囲を決める（非公開側の証拠を先に見る・欠損は unknown）。"""
    cid = match.channel_id.strip()
    if match.channel_is_im is True or match.match_type == "im" or cid.startswith("D"):
        return "dm"
    if match.channel_is_mpim is True:
        return "group_dm"
    if match.channel_is_private is True or match.channel_is_group is True:
        return "private"
    if cid.startswith("C") and match.channel_is_private is False and match.channel_is_mpim is False:
        return "public"
    return "unknown"


def is_shareable_on_channel(match: SlackSearchMatch) -> bool:
    """チャンネル（本人以外も読む面）へ出してよい一致か。公開チャンネルだけが真。"""
    return classify_visibility(match) == "public"


def channel_label(match: SlackSearchMatch, visibility: Visibility) -> str:
    """表示用の場所。DM の channel.name は相手の user ID なので出さない。"""
    name = match.channel_name.strip()
    if visibility == "dm":
        return "DM"
    if visibility == "group_dm":
        return "グループDM"
    if not name:
        return "（場所不明）"
    if visibility == "public":
        return f"#{name}"
    return f"🔒#{name}"


def jst_from_ts(ts: str) -> str:
    """Slack ts（epoch 秒）→ ``YYYY-MM-DD HH:MM``（JST）。読めなければ空文字。"""
    try:
        seconds = float(str(ts).strip())
    except ValueError:
        return ""
    if seconds <= 0:
        return ""
    return _dt.datetime.fromtimestamp(seconds, tz=_JST).strftime("%Y-%m-%d %H:%M")


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """1 行へ潰し、通知記法を無害化し、書記素を割らずに切る（切ったら … を付ける）。"""
    flat = _defuse_slack_pings(" ".join(str(text or "").split()))
    cut = truncate_graphemes(flat, limit)
    return f"{cut}…" if len(cut) < len(flat) else cut


def safe_permalink(match: SlackSearchMatch) -> str:
    """Slack が返した permalink（形を検査）→ 無ければ機械的に組み立てる → それも無理なら空。"""
    link = match.permalink.strip()
    if _PERMALINK_RE.fullmatch(link):
        return link
    return slack_permalink(match.channel_id, match.ts) or ""


def to_hit(match: SlackSearchMatch, names: dict[str, str]) -> SlackSearchHit:
    """一致 1 件を表示用に整形する（差出人は表示名 → ハンドル → 不明の順）。"""
    visibility = classify_visibility(match)
    uid = match.user if isinstance(match.user, str) else ""
    sender = names.get(uid) or match.username.strip() or "（差出人不明）"
    return SlackSearchHit(
        channel_id=match.channel_id,
        channel_label=channel_label(match, visibility),
        visibility=visibility,
        sender=_defuse_slack_pings(sender),
        posted_at=jst_from_ts(match.ts),
        excerpt=excerpt(match.text),
        permalink=safe_permalink(match),
    )


def build_message(
    query: str,
    hits: list[SlackSearchHit],
    *,
    hidden_count: int,
    total_hits: int,
    may_have_more: bool,
    focus: str,
    summary: str,
    summary_failed: bool,
) -> str:
    """Slack へそのまま返す決定的な文。非公開分は件数だけ（中身・場所名は出さない）。"""
    q = truncate_graphemes(query, _QUERY_ECHO_CHARS)
    lines: list[str] = []
    if hits:
        head = f"🔎 Slack 検索「{q}」の結果（{len(hits)} 件・新しい順）"
        if total_hits > len(hits):
            head += f"\n全 {total_hits} 件のうち新しい {len(hits)} 件を出しています。"
        lines.append(head)
    elif hidden_count:
        lines.append(f"🔎 Slack 検索「{q}」: 公開チャンネルには一致がありませんでした。")
    else:
        lines.append(f"🔎 Slack 検索「{q}」に一致するメッセージは見つかりませんでした。")

    if summary:
        focus_label = truncate_graphemes(" ".join(focus.split()), 40)
        lines.append(f"📝 まとめ（{focus_label}）\n{summary}")
    elif summary_failed:
        lines.append("（まとめは作れませんでした。一覧はそのまま出しています）")

    for i, hit in enumerate(hits, start=1):
        meta = " ・ ".join(x for x in (hit.channel_label, hit.sender, hit.posted_at) if x)
        entry = f"{i}. {meta}\n    {hit.excerpt or '（本文なし）'}"
        if hit.permalink:
            entry += f"\n    {hit.permalink}"
        lines.append(entry)

    if hidden_count:
        lines.append(
            f"🔒 非公開の結果 {hidden_count} 件は、ここには出していません。DM で聞いてください。"
        )
    if hits and (may_have_more or total_hits > len(hits)):
        lines.append(
            "さらに探すときは、期間（after:YYYY-MM-DD）や場所（in:#チャンネル名）で絞れます。"
        )
    return _defuse_slack_pings("\n\n".join(lines))


__all__ = [
    "EXCERPT_CHARS",
    "build_message",
    "channel_label",
    "classify_visibility",
    "excerpt",
    "is_shareable_on_channel",
    "jst_from_ts",
    "safe_permalink",
    "to_hit",
]
