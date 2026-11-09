"""案件決定の投稿と台帳を、推測せず日付順にまとめる。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from teamagent.adapters.pgvector_client import PgVectorClient, SearchHit
from teamagent.adapters.slack_user_reader import SlackUserReader
from teamagent.skills._shared.source_url import hit_doc_url
from teamagent.skills.base import SkillContext
from teamagent.skills.search.alias_expansion import resolve_topic_client
from teamagent.skills.search.client_match import aliases, normalize_client, normalize_filter_client
from teamagent.skills.slack_search.render import excerpt, safe_permalink
from teamagent.skills.slack_summary.period import JST

CHANNEL = "C08MH3MG02F"
_YEAR = re.compile(r"(?<!\d)(20\d{2})(?:年)?(?:\s*[〜～~–—-から]+\s*(20\d{2})年?)?(?!\d)")
_DATE = re.compile(r"(?<!\d)(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})日?")


def wants_history(text: str) -> bool:
    text = unicodedata.normalize("NFKC", text)
    if re.search(r"提案書|議事録|レポート|資料|事例|要約", text):
        return False
    return bool(
        re.search(r"撮影|履歴|いつ", text)
        or (_YEAR.search(text) and not re.search(r"\d{1,2}月|\d{4}-\d{1,2}-\d{1,2}", text))
        or ("案件" in text and re.search(r"いつ|年|過去|全部|全て|すべて|遡|一覧", text))
        or re.search(r"\S+\s+案件\s*$", text)
    )


def year_bounds(text: str) -> tuple[date, date] | None:
    match = _YEAR.search(unicodedata.normalize("NFKC", text))
    if not match:
        return None
    start, end = int(match[1]), int(match[2] or match[1])
    if end < start:
        raise ValueError("年の範囲を確認してください（開始年〜終了年）。")
    return date(start, 1, 1), date(end, 12, 31)


def _day(value: str) -> date | None:
    match = _DATE.search(value)
    if match:
        try:
            return date(*map(int, match.groups()))
        except ValueError:
            pass
    return None


def _fields(text: str) -> dict[str, str]:
    # Slack の太字見出し＋次行の値と、シートの「列名: 値」を同じ形にする。
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"^\*([^*\n:]+)\*[ \t]*$", r"\1:", text, flags=re.M)
    text = re.sub(r"[*`【】]", "", text)
    fields: dict[str, str] = {}
    pending = ""
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
            pending = key.strip() if not value.strip() else ""
        elif pending and line.strip():
            fields[pending] = line.strip()
            pending = ""
    return fields


def _value(fields: dict[str, str], *keys: str) -> str:
    return next((fields[k] for k in keys if fields.get(k)), "")


@dataclass
class Project:
    name: str
    kind: str
    shooting: date | None
    decided: date | None
    project_id: str = ""
    links: list[str] = field(default_factory=list)

    @property
    def day(self) -> date | None:
        return self.shooting or self.decided


def project_from_hit(hit: SearchHit, client: str) -> Project | None:
    meta = hit.metadata
    fields = _fields(hit.content)
    company = _value(
        fields,
        "クライアント名",
        "クライアント",
        "クライアント/商材",
        "クライアント名/商材",
        "クライアント名/商材名",
        "取引先",
        "取引先名",
        "顧客名",
        "正式社名",
    ) or str(meta.get("client_name") or "")
    forms = {normalize_client(name) for name in (client, *aliases(client))}
    if company:
        if not any(form in normalize_client(company) for form in forms):
            return None
    elif not any(form in normalize_client(hit.content) for form in forms):
        return None
    name = _value(
        fields,
        "案件名",
        "商材",
        "商材名",
        "クライアント/商材",
        "クライアント名/商材",
        "クライアント名/商材名",
    )
    name = name or str(meta.get("client_case") or "") or company or client
    shooting = _day(_value(fields, "撮影日", "撮影予定日", "撮影日時", "撮影日程"))
    decided = _day(_value(fields, "決定日", "案件決定日", "受注日", "タイムスタンプ", "投稿日"))
    if not decided and meta.get("source_type") == "slack":
        uri = str(meta.get("source_uri") or "")
        try:
            decided = datetime.fromtimestamp(float(uri.rsplit("/", 1)[-1]), JST).date()
        except (ValueError, OverflowError, OSError):
            pass
    link = hit_doc_url(meta)
    return Project(
        name=name,
        kind=_value(fields, "種類", "種別", "案件種別", "施策種別") or "種類の記載なし",
        shooting=shooting,
        decided=decided,
        project_id=_value(fields, "案件ID", "案件Id", "案件id", "商談URL", "Salesforce URL"),
        links=[link] if link else [],
    )


def merge_projects(projects: list[Project]) -> list[Project]:
    merged: list[Project] = []
    for project in projects:
        same = next(
            (
                old
                for old in merged
                if (
                    (project.project_id and project.project_id == old.project_id)
                    or (
                        normalize_client(project.name) == normalize_client(old.name)
                        and not (
                            project.project_id
                            and old.project_id
                            and project.project_id != old.project_id
                        )
                        and (
                            (project.shooting and project.shooting == old.shooting)
                            or (
                                not (project.shooting and old.shooting)
                                and project.decided
                                and project.decided == old.decided
                            )
                        )
                    )
                )
            ),
            None,
        )
        if same is None:
            merged.append(project)
        else:
            same.shooting = same.shooting or project.shooting
            same.decided = same.decided or project.decided
            if same.kind == "種類の記載なし":
                same.kind = project.kind
            same.links = list(dict.fromkeys([*same.links, *project.links]))
    return sorted(merged, key=lambda p: (p.day or date.max, p.name))


@dataclass
class HistoryResult:
    message: str
    count: int = 0
    error: str = ""


def run_history(
    query: str,
    ctx: SkillContext,
    *,
    pgvector: Any = None,
    app_role: str | None = "teamagent_app",
    client: str | None = None,
    slack_store: Any = None,
    reader_factory: Any = None,
) -> HistoryResult | None:
    request = str(ctx.metadata.get("request_text") or query)
    if not wants_history(request + " " + query):
        return None
    requester = str(ctx.metadata.get("user_email") or "").strip()
    if not requester:
        return None
    try:
        bounds = year_bounds(request)
    except ValueError as exc:
        return HistoryResult(str(exc), error="bad_conditions")
    try:
        pg = pgvector if pgvector is not None else PgVectorClient.from_env()
        raw_groups = ctx.metadata.get("user_groups")
        with pg.connection(
            app_role=app_role,
            user_email=requester,
            user_groups=list(raw_groups) if isinstance(raw_groups, (list, tuple)) else None,
            user_role=ctx.metadata.get("user_role"),
        ) as conn:
            resolved = resolve_topic_client(
                request + " " + query,
                pg.list_client_names(conn=conn, request_id=ctx.request_id),
                client,
            )
            client = normalize_filter_client(client or (resolved[1] if resolved else None))
            if not client:
                # 未分類の古い案件も、先頭で名指しした取引先なら検索できる。
                prefix = re.split(r"撮影|案件|履歴|いつ|20\d{2}", query, maxsplit=1)[0]
                client = prefix.strip(" のについて「」\"'")
            if len(normalize_client(client)) < 2:
                return None
            hits = pg.list_project_history(conn)
    except Exception as exc:
        ctx.bind_logger("project_history").warning("history_failed", error=type(exc).__name__)
        return HistoryResult(
            "取込済みの案件履歴を取得できませんでした。0件という意味ではありません。",
            error="search_failed",
        )

    dm = str(ctx.metadata.get("channel_id") or "").startswith("D")
    # 取込済み Slack の ACL は本人の可視範囲。公開面へ転記する際は公開判定も必要。
    visible = [
        h
        for h in hits
        if dm
        or h.metadata.get("source_type") != "slack"
        or (h.metadata.get("is_private") is False and h.metadata.get("is_mpim") is False)
    ]
    warnings: list[str] = []
    if len(visible) < len(hits):
        warnings.append(
            "公開判定できない取込済みSlack投稿は表示していません。DMで確認してください。"
        )
    hits = visible
    try:
        token = slack_store.get(requester) if slack_store is not None else None
        if not token or not token.access_token:
            warnings.append("Slack未連携のため、Slackの全期間は未確認です。")
        else:
            reader = (reader_factory or SlackUserReader.from_user_token)(token.access_token)
            # 案件日と投稿日は異なる。検索時は期間も撮影語も課さず、台帳と統合後に絞る。
            from teamagent.skills.slack_search.skill import _partition

            for form in sorted({client, *aliases(client)}):
                clean = form.replace('"', "").replace("\\", "")
                result = reader.search_all_checked(
                    f'in:{CHANNEL} "{clean}"', ctx.request_id, max_messages=None
                )
                if result.error or result.truncated:
                    raise RuntimeError("incomplete_slack")
                shown, hidden = _partition(result.matches, dm_surface=dm)
                if hidden:
                    warnings.append("非公開の投稿は表示していません。DMで確認してください。")
                for match in shown:
                    hits.append(
                        SearchHit(
                            0,
                            match.text,
                            1.0,
                            {
                                "source_type": "slack",
                                "source_uri": f"slack://{match.channel_id}/{match.ts}",
                                "drive_url": safe_permalink(match),
                            },
                        )
                    )
    except Exception as exc:
        ctx.bind_logger("project_history").warning("history_slack_failed", error=type(exc).__name__)
        warnings.append("Slackの全期間取得に失敗したため、全件の確認は未完了です。")
    projects = merge_projects([p for h in hits if (p := project_from_hit(h, client))])
    undated = sum(p.day is None for p in projects)
    start: date | None
    end: date | None
    if bounds:
        projects = [p for p in projects if p.day and bounds[0] <= p.day <= bounds[1]]
        start, end = bounds
        if undated:
            warnings.append(f"日付の記載がない {undated} 件は指定年に該当するか未確認です。")
    else:
        days = [p.day for p in projects if p.day]
        start, end = (min(days), max(days)) if days else (None, None)
    period = (
        f"{start.year}年{start.month}月〜{end.year}年{end.month}月"
        if start and end
        else "全期間（日付の記載なし）"
    )
    lines = [f"{period}を見ました・{len(projects)} 件" + ("（確認未完了）" if warnings else "")]
    if not bounds:
        lines.append("検索対象: 全期間（開始年・終了年の制限なし）")
    for p in projects:
        when = (
            f"{p.shooting}（撮影日）"
            if p.shooting
            else (f"{p.decided or '決定日の記載なし'}・撮影日の記載なし")
        )
        links = " ・ ".join(p.links) or "リンク取得不可"
        lines.append(f"{when} ・ {excerpt(p.name, 300)} ・ {excerpt(p.kind, 100)} ・ {links}")
    if not projects:
        lines.append("該当する案件の記録はありません。")
    lines.extend(dict.fromkeys(warnings))
    return HistoryResult("\n".join(lines), len(projects))
