"""Slack の一覧依頼を決定的に検索・整形する（要約器は呼ばない）。"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any

from teamagent.adapters.slack_user_reader import SlackSearchMatch
from teamagent.skills.search.answer_mode import classify_answer_mode
from teamagent.skills.slack_search.render import excerpt, jst_from_ts, to_hit
from teamagent.skills.slack_search.schema import SlackSearchOutput
from teamagent.skills.slack_summary.period import JST, resolve_period

_LIST = re.compile(r"全部|全て|すべて|一覧|リスト|遡|さかのぼ|誰が|だれが|URL|リンク", re.I)
_PERIOD = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}(?:\s*[〜～~]\s*\d{4}-\d{1,2}-\d{1,2})?"
    r"|(?:\d{4}年)?\d{1,2}月(?:\d{1,2}日(?:[〜～~]\d{1,2}日)?)?"
    r"|先々週|先週|今週|先月|今月|一昨日|昨日|今日|(?:直近|過去)\d+日(?:間)?"
)
_SELF = re.compile(r"自分が|自分の|私が|私の|本人が|from:(?:me|@me)\b")


def wants_list(text: str) -> bool:
    text = unicodedata.normalize("NFKC", text)
    return bool(_LIST.search(text)) or classify_answer_mode(text) == "list"


def list_query(
    query: str,
    request: str,
    *,
    channel: str = "",
    period: str = "",
    user_id: str = "",
) -> tuple[str, tuple[str, str] | None]:
    """既存 query は保ち、原文のチャンネル・月・本人・リンク条件を補う。"""
    request = unicodedata.normalize("NFKC", request)
    query = unicodedata.normalize("NFKC", query)
    if not period:
        found = _PERIOD.search(re.sub(r"(?<!\S)(?:after|before|on|during):\S+", "", request))
        period = found[0] if found else ""
    all_time = bool(re.search(r"過去全部|全期間|遡|さかのぼ", request)) and not period
    bounds = resolve_period(period or "過去全部") if period or all_time else None
    # 修飾子の値（#一覧等）を依頼表現として削らない。
    modifier = r"(?<!\S)-?(?:in|from|after|before|on|during|has|is|with):\S+"
    modifiers = re.findall(modifier, query)
    query = re.sub(modifier, "", query)
    # query に自然文が来た場合だけ、依頼表現を検索語にしない。
    if wants_list(query):
        query = _PERIOD.sub("", query)
        query = re.sub(r"自分が|私が|毎週投稿している|毎週投稿してる", "", query)
        query = re.sub(
            r"(?:の投稿|(?<!\w)投稿|スレッド)を?(?:全部|全て|すべて)?(?:一覧)?", "", query
        )
        query = re.sub(r"誰が応募したか|だれが応募したか|過去まで|遡って|さかのぼって", "", query)
        query = re.sub(
            r"(?:の)?(?:URL|リンク)(?:が|を)?(?:ほしい|欲しい|教えて)?", "", query, flags=re.I
        )
        query = re.sub(
            r"(?:を|の)?(?:全部|全て|すべて|一覧|リスト)(?:で|に|して|ほしい)?", "", query
        )
        query = re.sub(r"(?:チャンネル)?(?:で|の)?(?:共有された|募集された)", "", query)
        query = re.sub(
            r"(?:を|の|で|に|して|見て|ほしい|欲しい|ください|教えて)+$", "", query.strip()
        )
    if not query.strip():
        query = next((word for word in ("募集", "定期投稿", "レポート") if word in request), "")
    query = " ".join((query, *modifiers))
    if channel:
        query = re.sub(r"(?<!\S)in:\S+", "", query)
        query += f" in:{channel}"
    elif not re.search(r"(?<!\S)in:", query):
        named = re.search(r"<#([CG][A-Z0-9]+)(?:\|[^>]*)?>|#([\w-]+)", request)
        if named:
            query = re.sub(r"<#([^>]+)>|#[\w-]+", "", query)
            query += f" in:{named[1] or '#' + named[2]}"
    if _SELF.search(request + " " + query):
        if not re.fullmatch(r"[UW][A-Z0-9]{2,}", user_id):
            raise ValueError("本人の Slack ID を確認できません。Slack を連携し直してください。")
        query = re.sub(r"(?<!\S)from:\S+", "", query)
        query += f" from:{user_id}"
    elif not re.search(r"(?<!\S)from:", query):
        author = re.search(r"<@([UW][A-Z0-9]+)>\s*(?:が|の投稿)", request)
        if author:
            query += f" from:{author[1]}"
    if bounds:
        query = re.sub(r"(?<!\S)(?:after|before|on|during):\S+", "", query)
        start, end = (datetime.fromtimestamp(float(ts), JST) for ts in bounds)
        if not all_time and period not in ("過去全部", "全部", "全期間", "すべて", "全て"):
            query += f" after:{(start - timedelta(days=1)).date()}"
        query += f" before:{(end + timedelta(days=1)).date()}"
    if not re.search(r"(?<!\S)has:", query):
        if "添付" in request:
            query += " has:file"
        elif re.search(r"リンク|URL|レポート", request, re.I):
            query += " has:link"
    return " ".join(query.split()), bounds


def run_listing(
    reader: Any,
    query: str,
    request: str,
    ctx: Any,
    *,
    dm_surface: bool,
    requester: str,
    bounds: tuple[str, str] | None,
    thread_ts: str = "",
) -> SlackSearchOutput:
    # 遅延 import: summary もこの経路を使い、既存の表示名キャッシュ・面ガードを共用する。
    from teamagent.skills.slack_search.skill import _partition, _resolve_names

    # 全期間は Slack の総数を使えるため、古い境界の計数用ページを読まない。
    bounded = bounds if bounds and float(bounds[0]) > 0 else None
    result = reader.search_all_checked(
        query,
        ctx.request_id,
        oldest=bounded[0] if bounded else "",
        latest=bounded[1] if bounded else "",
    )
    if result.error:
        return SlackSearchOutput(error=result.error)
    shown, hidden = _partition(result.matches, dm_surface=dm_surface)
    replies_needed = bool(re.search(r"誰が|だれが|返信者|応募者", request))
    replies: dict[tuple[str, str], str] = {}
    selected: list[SlackSearchMatch] = []
    seen_threads: set[tuple[str, str]] = set()
    truncated = result.truncated
    for match in shown:
        root = match.thread_ts or match.ts
        if thread_ts and root != thread_ts:
            continue
        key = (match.channel_id, root)
        if replies_needed:
            if key in seen_threads:
                continue
            seen_threads.add(key)
            thread = reader.read_thread_checked(
                match.channel_id, root, ctx.request_id, limit=500, paginate=True
            )
            if thread.error:
                truncated = True
                replies[key] = "返信を取得できませんでした（応募者は未確認）"
            else:
                # 返信しただけでは応募と断定せず、根拠の発言を名前に添える。
                participants: dict[str, list[str]] = {}
                for reply in thread.messages:
                    if reply.ts != root and reply.user:
                        participants.setdefault(reply.user, []).append(excerpt(reply.text, 80))
                labels = []
                for uid, texts in participants.items():
                    name = reader.get_display_name(uid, ctx.request_id) or "（表示名不明）"
                    labels.append(f"{excerpt(name)}: {' / '.join(dict.fromkeys(texts))}")
                replies[key] = "返信者: " + ("、".join(labels) or "なし")
                if thread.truncated:
                    truncated = True
                    replies[key] += "（返信は取得上限に達したため一部のみ）"
        selected.append(match)
    names = _resolve_names(reader, requester, selected, ctx.request_id)
    hits = [to_hit(match, names) for match in selected]
    if bounds:
        start = jst_from_ts(bounds[0]) if float(bounds[0]) else "過去すべて（開始指定なし）"
        period_line = f"検索期間: {start} 〜 {jst_from_ts(bounds[1])} 未満（JST）"
    else:
        dates = re.findall(r"(?<!\S)(?:after|before|on|during):\S+", query)
        period_line = "検索期間: " + (
            " ・ ".join(dates) if dates else "過去すべて（開始指定なし）〜検索時点"
        )
    lines = [f"Slack 一覧（{len(hits)} 件）", period_line]
    if hits:
        dates_shown = [hit.posted_at for hit in hits if hit.posted_at]
        if dates_shown:
            lines.append(f"表示範囲: {min(dates_shown)} 〜 {max(dates_shown)}（JST）")
    for hit, match in zip(hits, selected, strict=True):
        detail = replies.get((match.channel_id, match.thread_ts or match.ts), "")
        if re.search(r"URL|リンク|レポート", request, re.I):
            links = re.findall(r"https?://[^\s<>|]+", match.text)
            links.extend(str(file.get("permalink") or "") for file in match.files)
            missing = [
                link
                for link in dict.fromkeys(links)
                if link.startswith("https://") and link not in hit.excerpt
            ]
            if missing:
                detail += " ・ " + " ・ ".join(missing)
        lines.append(
            f"{hit.posted_at} ・ {excerpt(hit.sender)} ・ {hit.excerpt or '（本文なし）'}"
            f"{' ・ ' + detail if detail else ''} ・ {hit.permalink or '（リンク取得不可）'}"
        )
    if not hits:
        lines.append("該当する投稿はありません。")
    if hidden:
        lines.append(f"非公開の結果 {hidden} 件は、ここには出していません。DM で聞いてください。")
    if result.truncated:
        if dm_surface:
            lines.append(f"ほか {max(0, result.total - len(result.matches))} 件（表示上限500件）。")
        else:
            lines.append("取得上限500件に達しました。非公開分を含む残件数はDMで確認してください。")
    if replies_needed:
        lines.append("返信者と発言を記載しています。返信だけで応募したとは断定していません。")
    from teamagent.skills.slack_summary.skill import _defuse_slack_pings

    return SlackSearchOutput(
        matches=hits,
        match_count=len(hits),
        hidden_count=hidden,
        total_hits=result.total if dm_surface else 0,
        truncated=truncated,
        message=_defuse_slack_pings("\n".join(lines)),
    )
