"""Short answers keep only citations attached to retained sentences."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.prompts.loader import load_prompt
from teamagent.skills._shared.grounding import NumberGrounder
from teamagent.skills.search import drive_first, timeline_status
from teamagent.skills.search.campaign_files import (
    VIDEO_METRICS_HEADING,
    _footer_campaigns,
    video_stats_footer,
)
from teamagent.skills.search.comparison import compact_comparison, is_comparison_query
from teamagent.skills.search.composite import SlackItem
from teamagent.skills.search.schema import DocumentReference

CTX_KEY = "_search_compact_answer"
EMPTY_ANSWER = "取得した資料では、質問への回答を確認できませんでした。"
_CITATION = re.compile(r"\[S:([\w-]+)\]")
_EXPLICIT_LIST = re.compile(
    r"一覧|リスト(?:アップ)?|列挙|(?:資料|事例|案件)を全部|全部(?:の資料)?を?(?:出|見せ|並べ)"
)
_METRICS = re.compile(r"(?:動画|投稿)(?:ごと|別|毎)|結果(?:を|と|も|が)|数値|数字|再生数|保存数")
_METRIC_PROSE = re.compile(r"再生|保存|いいね|視聴|平均|中央値|合計|投稿|本数")
_QUANTITIES = re.compile(
    r"\d|[〇零一二三四五六七八九十百千万億兆]+"
    r"(?=\s*(?:回|件|本|倍|%|再生|保存|いいね|[、。]|です|で|$))"
)
_DATES = re.compile(
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|"
    r"\d{1,2}[-/]\d{1,2}(?=\s*(?:\d{1,2}:\d{2}\s*)?(?:時点|現在|付))|"
    r"(?:\d{4}年)?\d{1,2}月(?:\d{1,2}日)?|"
    r"\d{4}年|(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)"
)
# title, resolved URL, retrieved content. Keys are assigned by code, never model URLs.
Sources = dict[str, tuple[str, str, str]]


def is_empty(answer: str, *, notice: str = "") -> bool:
    return not _CITATION.sub("", answer.removeprefix(notice)).replace(EMPTY_ANSWER, "").strip()


def normal_answer(answer: str, *, sources: Sources | None = None) -> str:
    # 通常回答の再生成がcompact用の文を繰り返しても、除去した主張を復活させない。
    if sources is not None:
        lines = []
        for line in answer.splitlines():
            keys = _CITATION.findall(line)
            if keys and (
                any(key not in sources for key in keys)
                or not NumberGrounder.from_inputs(
                    "\n".join(sources[key][2] for key in keys), always_allowed=frozenset()
                ).ok(_CITATION.sub("", line))
            ):
                continue
            lines.append(line)
        return "\n".join(lines).replace(EMPTY_ANSWER, "").strip()
    return (
        "\n".join(line for line in answer.splitlines() if not _CITATION.search(line))
        .replace(EMPTY_ANSWER, "")
        .strip()
    )


def append_used_drive_links(
    answer: str,
    hits: Sequence[SearchHit],
    displayed_refs: list[DocumentReference],
    *,
    slack_items: Sequence[SlackItem] = (),
) -> str:
    """通常回答の引用・明記した資料名から、会話と正本を最大3件残す。"""
    from teamagent.skills.search.case_list import case_link

    used_keys = set(_CITATION.findall(answer))
    answer = _CITATION.sub("", answer)
    body = unicodedata.normalize("NFKC", answer).casefold()
    # URL 内のファイル名は、回答本文で使った資料名とはみなさない。
    body = re.sub(r"https?://[^\s<>)]*", "", body)
    links: list[str] = []
    seen = {ref.url for ref in displayed_refs}
    candidates = [(str(h.chunk_id), h) for h in hits]
    candidates.extend(
        (f"slack{i}", drive_first.slack_hit(item, i)) for i, item in enumerate(slack_items)
    )
    channels = {f"slack{i}": item.hit.channel or "" for i, item in enumerate(slack_items)}
    shown = 0
    for key, hit in candidates:
        if used_keys and key not in used_keys:
            continue
        meta = hit.metadata or {}
        # 根拠が投稿本文なら、添付の保存先が判明していても元の会話へ戻す。
        reference_meta = (
            {k: meta.get(k) for k in ("source_type", "source_uri", "title")}
            if meta.get("source_type") == "slack"
            else meta
        )
        ref = drive_first.canonical_reference(reference_meta, hit.content)
        conversation = ref.kind in {"slack_conversation", "slack_attachment"}
        if (ref.kind != "drive" and not conversation) or ref.url in seen:
            continue
        names = [
            ref.title,
            channels.get(key, ""),
            *(
                str(meta.get(key) or "")
                for key in (
                    "title",
                    "file_name",
                    "drive_title",
                    "client_name",
                    "cls_project",
                    "case_client",
                    "case_product",
                    "advertiser",
                    "brand",
                    "product",
                    "campaign",
                    "channel_name",
                )
            ),
        ]
        used = key in used_keys
        for name in names:
            name = re.sub(
                r"\.(?:pdf|pptx?|docx?|xlsx?)$", "", unicodedata.normalize("NFKC", name).casefold()
            ).strip()
            if len(name) < 2 or name in {
                "資料",
                "記録",
                "提案資料",
                "提案書",
                "レポート",
                "報告書",
                "事例",
                "施策",
                "動画",
                "ショート動画",
                "tiktok",
                "sns",
                "不明",
                "未分類",
            }:
                continue
            pattern = (
                (r"(?<![a-z0-9])" if re.match(r"[a-z0-9]", name) else "")
                + re.escape(name)
                + (r"(?![a-z0-9])" if re.search(r"[a-z0-9]$", name) else "")
            )
            if re.search(pattern, body):
                used = True
                break
        link = case_link(ref.url, "会話の記録" if conversation else "資料を見る")
        if not used or link is None or "/spreadsheets/" in (ref.url or ""):
            continue
        seen.add(ref.url)
        if not conversation:
            displayed_refs.append(ref)
        if ref.url and ref.url not in answer:
            links.append(link)
        shown += 1
        if shown == 3:
            break
    return "\n\n".join(part for part in (answer, " / ".join(links)) if part)


def strip_linkless_invitation(answer: str) -> str:
    """リンクの無い誘い文だけを除く。本人限定経路の最終出力で使う。"""
    if any(_safe_url(url) for url in re.findall(r"https?://[^\s<>|\[\]()]+", answer)):
        return answer
    invitation = re.compile(
        r"詳し[^\n。！？!?]*(?:こちら|どうぞ|ご覧)|"
        r"(?:資料|詳細|元の会話)[^\n。！？!?]*(?:こちら|見たければ|見たい場合)|"
        r"こちら(?:を|から|で|です)?(?:どうぞ|ご覧ください|確認してください)?[。！!]?\s*$"
    )
    return "".join(
        sentence
        for sentence in re.split(r"(?<=[。！？!?\n])", answer)
        if not invitation.search(sentence)
    ).strip()


def append_comparison_drive_links(
    answer: str,
    hits: Sequence[SearchHit],
    clients: Sequence[str],
    displayed_refs: list[DocumentReference],
) -> str:
    """比較対象ごとに確認済み正本を1件表示し、表示した参照だけを保存する。"""
    from teamagent.skills.search.case_list import case_link
    from teamagent.skills.search.client_match import hit_is_about_client

    links: list[str] = []
    for client in clients:
        for hit in hits:
            if not hit_is_about_client(hit, client):
                continue
            ref = drive_first.canonical_reference(hit.metadata or {}, hit.content)
            link = case_link(ref.url, "資料を見る") if ref.kind == "drive" else None
            if not link or "/spreadsheets/" in (ref.url or ""):
                continue
            label = re.sub(r"[<>&|\r\n]", " ", client).strip()
            labeled = f"{label}: {link}"
            if link in answer and ": " + link not in answer:
                answer = answer.replace(link, labeled, 1)
            else:
                links.append(labeled)
            if not any(r.drive_file_id == ref.drive_file_id for r in displayed_refs):
                displayed_refs.append(ref)
            break
    return "\n\n".join(part for part in (answer, " / ".join(links)) if part)


def fallback_cards(
    query: str,
    hits: Sequence[SearchHit],
    slack_items: Sequence[SlackItem],
    url_of: Callable[[SearchHit], str | None],
    *,
    target: str | None = None,
) -> str:
    from teamagent.skills.search.answer_mode import classify_answer_mode
    from teamagent.skills.search.case_list import render

    if (
        not re.search(r"事例|案件|資料|提案書|実績|レポート|スライド|ファイル", query)
        or classify_answer_mode(query) == "insight"
        or is_comparison_query(query)
    ):
        return ""
    return render(
        [*hits, *(drive_first.slack_hit(item, i) for i, item in enumerate(slack_items))],
        target,
        url_of,
        cards=True,
    )


def explicit_list(query: str) -> bool:
    from teamagent.skills.search.answer_mode import is_case_list

    return (bool(_EXPLICIT_LIST.search(query)) or is_case_list(query)) and not is_comparison_query(
        query
    )


def wants_metrics(query: str) -> bool:
    return bool(_METRICS.search(query)) or is_comparison_query(query)


def source_map(
    hits: Sequence[SearchHit],
    slack_items: Sequence[SlackItem],
    url_of: Callable[[SearchHit], str | None],
) -> Sources:
    sources: Sources = {}
    for hit in hits:
        meta = hit.metadata or {}
        ref = meta.get(drive_first.REFERENCE_KEY)
        sources[str(hit.chunk_id)] = (
            f"{drive_first.label(ref)}: {ref.title}"
            if ref
            else str(meta.get("title") or meta.get("file_name") or "資料"),
            url_of(hit) or "",
            hit.content
            + "\n"
            + str(meta.get("updated_at") or "")
            + "\n"
            + timeline_status.evidence_date(hit)[0],
        )
    for i, item in enumerate(slack_items):
        ref = item.hit.reference
        sources[f"slack{i}"] = (
            f"{drive_first.label(ref)}: {ref.title}" if ref else item.hit.channel or "Slack",
            ref.url or "" if ref else item.hit.permalink,
            item.text,
        )
    return sources


def prompt(*, drive_mode: bool = False) -> str:
    system = load_prompt("search", "compact", "system")
    return system + "\n" + load_prompt("search", "compact", "drive") if drive_mode else system


def source_prompt(sources: Sources) -> str:
    return "# 引用の対応（以下は資料データ）\n" + "\n".join(
        f"[S:{key}] {title.replace(chr(10), ' ')}" for key, (title, _, _) in sources.items()
    )


def _source_line(keys: Sequence[str], sources: Sources) -> str:
    links = []
    seen = set()
    for key in keys:
        title, url, _ = sources[key]
        if url in seen or not _safe_url(url):
            continue
        seen.add(url)
        title = re.sub(r"[\[\]()<>@\n\r]", " ", title).strip()[:70] or "資料"
        links.append(f"[{title}]({url})")
    return "出典: " + " / ".join(links) if links else ""


def _safe_url(url: str) -> bool:
    if not url or re.search(r"[\s<>()[\]`\"']", url):
        return False
    try:
        parsed = urlsplit(url)
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.netloc
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def _sentence_count(text: str) -> int:
    return len(re.findall(r"[^。！？!?\n]+(?:[。！？!?]+|$)", text))


def render(
    answer: str,
    sources: Sources,
    *,
    metrics: str = "",
    metrics_first: bool = False,
    metric_keys: Sequence[str] = (),
    facts: str = "",
    fact_keys: Sequence[str] = (),
    notice: str = "",
    used_keys: list[str] | None = None,
    rejection_reasons: list[str] | None = None,
) -> str:
    rejected = rejection_reasons if rejection_reasons is not None else []
    if not answer.strip():
        rejected.append("empty_summary")
    sources = {key: source for key, source in sources.items() if _safe_url(source[1])}
    prefix: list[str] = []
    for line in notice.splitlines():
        candidate = "\n".join([*prefix, line])
        if len(candidate) <= 550 and _sentence_count(candidate) <= 6 and len(prefix) < 6:
            prefix.append(line)
    derived = facts.splitlines() if all(key in sources for key in fact_keys) else []
    if derived and (
        len("\n".join([*prefix, *derived])) > 550
        or _sentence_count("\n".join([*prefix, *derived])) > 6
        or len(prefix) + len(derived) > 6
        or len({sources[key][1] for key in fact_keys}) > 3
        or len("\n".join([*prefix, *derived])) + len(_source_line(fact_keys, sources)) + 2 > 2500
    ):
        derived = []
    lines: list[str] = []
    used: list[str] = list(dict.fromkeys(fact_keys)) if derived else []
    base = [*prefix, *derived]
    for raw in answer.splitlines():
        keys = list(dict.fromkeys(_CITATION.findall(raw)))
        if not keys:
            if raw.strip():
                rejected.append("missing_citations")
            continue
        if any(key not in sources for key in keys):
            rejected.append("unknown_citation")
            continue
        candidate_keys = list(dict.fromkeys([*used, *keys]))
        if len({sources[key][1] or key for key in candidate_keys}) > 3:
            rejected.append("source_limit")
            continue
        text = _CITATION.sub("", raw).strip()
        if EMPTY_ANSWER in text:
            rejected.append("empty_answer_marker")
            continue
        if re.match(
            r"^(?:[-*#・•]|[📊📁🏆🗂💬📎🕸]|金庫の資料|事例・施策実績|資料リンク|"
            r"関係図|探した範囲|動画ごとの数字|施策の反応合計|施策の数字はシート)",
            text,
        ):
            rejected.append("heading_or_list")
            continue
        # Quantitative metric prose comes only from the code-generated facts/rows.
        metric_text = _DATES.sub("", unicodedata.normalize("NFKC", text))
        if _METRIC_PROSE.search(metric_text) and _QUANTITIES.search(metric_text):
            rejected.append("metric_prose")
            continue
        if (
            not text
            or re.search(r"[a-z][a-z0-9+.-]*://|\[[^]]*\]\(", text, re.IGNORECASE)
            or len(base) + len(lines) >= 6
            or _sentence_count("\n".join([*base, *lines, text])) > 6
            or len("\n".join([*base, *lines, text])) > 550
        ):
            rejected.append("body_format_or_limit")
            continue
        if not NumberGrounder.from_inputs(
            "\n".join(sources[k][2] for k in keys), always_allowed=frozenset()
        ).ok(text):
            rejected.append("ungrounded_number")
            continue
        footer = _source_line(candidate_keys, sources)
        if len("\n".join([*base, *lines, text, footer])) + 1 > 2500:
            rejected.append("answer_limit")
            continue
        lines.append(text)
        used = candidate_keys
    body = "\n".join([*base, *lines])
    heading, _, payload = metrics.partition("\n")
    blocks: list[str] = []
    if heading == VIDEO_METRICS_HEADING:
        for block, key in zip(payload.split("\n\n"), metric_keys, strict=False):
            if key not in sources:
                continue
            candidate_keys = list(dict.fromkeys([*used, key]))
            if len({sources[k][1] for k in candidate_keys}) > 3:
                continue
            label, *raw_rows = block.splitlines()
            rows: list[str] = []
            for row in raw_rows:
                if not re.match(r"^\d+\.\s+(?:[\d,.]+|不明)\s*再生・保存\s+(?:[\d,.]+|不明)", row):
                    continue
                if any(not _safe_url(url) for url in re.findall(r"https?://\S+", row)):
                    continue
                candidate_block = "\n".join([label, *rows, row])
                candidate_metrics = heading + "\n" + "\n\n".join([*blocks, candidate_block])
                candidate_footer = _source_line(candidate_keys, sources)
                if len("\n\n".join((body, candidate_metrics, candidate_footer))) <= 2500:
                    rows.append(row)
            if rows:
                blocks.append("\n".join([label, *rows]))
                used = candidate_keys
    shown_metrics = heading + "\n" + "\n\n".join(blocks) if blocks else ""
    if used_keys is not None:
        used_keys.extend(used)
    # A separate metrics post ends before the explanation; keep the same order in one post too.
    content = (
        "\n\n\n".join(part for part in (shown_metrics, body) if part)
        if metrics_first
        else "\n\n".join(part for part in (body, shown_metrics) if part)
    )
    return "\n\n".join(part for part in (content, _source_line(used, sources)) if part)


def build(
    answer: str,
    hits: list[SearchHit],
    slack_items: Sequence[SlackItem],
    url_of: Callable[[SearchHit], str | None],
    *,
    query: str,
    asked_industry: str | None = None,
    karte_hits: Sequence[SearchHit] = (),
    notice: str = "",
    displayed_refs: list[DocumentReference] | None = None,
    rejection_reasons: list[str] | None = None,
) -> str:
    if displayed_refs is not None and re.search(r"今どう|状況|現状|今の状態", query):
        dates = {str(h.chunk_id): timeline_status.evidence_date(h)[0] for h in [*hits, *karte_hits]}
        stamped = []
        for line in answer.splitlines():
            keys = _CITATION.findall(line)
            date = max((dates.get(k, "") for k in keys), default="")
            if date:

                def stamp(match: re.Match[str], date: str = date) -> str:
                    prefix = (
                        ""
                        if re.search(r"\d{4}-\d{2}-\d{2} 時点では", match[2])
                        else f"{date} 時点では"
                    )
                    return match[1] + prefix + match[2]

                line = re.sub(
                    r"(^|(?<=[。！？]))([^。！？]*?(?:現在|進行中|詰めの段階|完了|終了|実施中|予定|検討中)[^。！？]*)",
                    stamp,
                    line,
                )
            stamped.append(line)
        answer = "\n".join(stamped)
    facts, fact_ids = compact_comparison(query, hits)
    show_metrics = wants_metrics(query)
    metrics = video_stats_footer(hits, "", asked_industry=asked_industry) if show_metrics else ""
    if metrics and displayed_refs is not None:
        facts, fact_ids = "", []
    campaigns = _footer_campaigns(hits, asked_industry) if show_metrics else []
    used_keys: list[str] = []
    rendered = render(
        answer,
        source_map([*hits, *karte_hits], slack_items, url_of),
        facts=facts,
        fact_keys=[str(key) for key in fact_ids],
        notice=notice,
        metrics=metrics,
        metrics_first=displayed_refs is not None,
        metric_keys=[str(hit.chunk_id) for hit, _ in campaigns],
        used_keys=used_keys,
        rejection_reasons=rejection_reasons,
    )
    if displayed_refs is None:
        return rendered
    from teamagent.skills.search.case_list import case_link

    references: dict[str, DocumentReference | None] = {
        str(hit.chunk_id): drive_first.canonical_reference(hit.metadata or {}, hit.content)
        for hit in [*hits, *karte_hits]
    }
    references.update({f"slack{i}": item.hit.reference for i, item in enumerate(slack_items)})
    links = []
    seen = set()
    for key in used_keys:
        ref = references.get(key)
        # 公開の会話を根拠にした説明には、資料とは区別して元投稿へのリンクを残す。
        if ref is not None and ref.kind in {"slack_conversation", "slack_attachment"}:
            if "slack.com/archives/" in (ref.url or ""):
                link = case_link(ref.url, "会話の記録")
                if link and link not in links:
                    links.append(link)
            continue
        if ref is None or ref.kind != "drive" or ref.drive_file_id in seen:
            continue
        link = case_link(ref.url, "資料を見る")
        if link is None or "/spreadsheets/" in (ref.url or ""):
            continue
        seen.add(ref.drive_file_id)
        displayed_refs.append(ref)
        links.append(link)
        if len(links) == 3:
            break
    # 引用を残した本文と同じ正本だけを表示・保存し、検索候補の一覧は添えない。
    body = rendered.rsplit("\n\n出典: ", 1)[0]
    return "\n\n".join(part for part in (body, " / ".join(links)) if part)
