"""GAS ファイル記録の確定対応。検索索引・名前による推測には使わない。"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class SavedFileRecord:
    record_number: str
    branch_number: str
    slack_source_uri: str | None
    drive_file_id: str
    drive_url: str
    title: str


def _literal_link(value: str) -> tuple[str, str]:
    formula = re.fullmatch(
        r'=HYPERLINK\(\s*"((?:[^"]|"")*)"\s*[,;]\s*"((?:[^"]|"")*)"\s*\)',
        value.strip(),
        re.IGNORECASE,
    )
    if formula:
        return formula[1].replace('""', '"'), formula[2].replace('""', '"')
    return value.strip(), ""


def slack_source_uri(value: object) -> str | None:
    """Slack 投稿自身の channel＋ts に正規化する。thread_ts で別投稿へ読み替えない。"""
    text = _literal_link(str(value or ""))[0]
    direct = re.fullmatch(r"slack://([CGD][A-Z0-9]+)/([0-9]+\.[0-9]{6})", text)
    if direct:
        return f"slack://{direct[1]}/{direct[2]}"
    try:
        parsed = urlsplit(text)
        if (
            parsed.scheme != "https"
            or not (parsed.hostname or "").endswith(".slack.com")
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            return None
    except ValueError:
        return None
    match = re.fullmatch(r"/archives/([CGD][A-Z0-9]+)/p([0-9]+)([0-9]{6})/?", parsed.path)
    return f"slack://{match[1]}/{match[2]}.{match[3]}" if match else None


def _drive_file_id(value: str) -> str | None:
    try:
        parsed = urlsplit(_literal_link(value)[0])
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"drive.google.com", "docs.google.com"}
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            return None
    except ValueError:
        return None
    match = re.fullmatch(
        r"/(?:file|document|presentation|spreadsheets|drawings)/d/([A-Za-z0-9_-]+)(?:/[^/]*)?/?",
        parsed.path,
    )
    ids = set(parse_qs(parsed.query).get("id", []))
    if match:
        ids.add(match[1])
    if len(ids) != 1:
        return None
    file_id = next(iter(ids))
    return file_id if re.fullmatch(r"[A-Za-z0-9_-]+", file_id) else None


def _label(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def _source(row: dict[str, str]) -> str | None:
    sources = {
        source
        for label in ("Slack投稿URL", "投稿URL", "元投稿URL")
        if (source := slack_source_uri(row.get(label)))
    }
    channel, ts = row.get("チャンネルID", ""), row.get("ts", "")
    if source := slack_source_uri(f"slack://{channel}/{ts}"):
        sources.add(source)
    return next(iter(sources)) if len(sources) == 1 else None


def select_saved_file_records(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    slack_sources: Sequence[str] = (),
    record_keys: Sequence[tuple[str, str]] = (),
) -> list[SavedFileRecord]:
    labels = [_label(header) for header in headers]
    if len(set(labels)) != len(labels) or not {"連番", "枝番"} <= set(labels):
        return []
    by_key: dict[tuple[str, str], list[int]] = defaultdict(list)
    by_source: dict[str, list[int]] = defaultdict(list)
    records: list[dict[str, str]] = []
    for index, values in enumerate(rows):
        row = {
            label: str(values[i]).strip() if i < len(values) else ""
            for i, label in enumerate(labels)
        }
        records.append(row)
        by_key[(row["連番"], row["枝番"])].append(index)
        if source := _source(row):
            by_source[source].append(index)
    wanted: set[int] = set()
    for raw in slack_sources:
        matches = by_source.get(slack_source_uri(raw) or "", [])
        if len(matches) == 1:
            wanted.add(matches[0])
    for key in record_keys:
        matches = by_key.get((str(key[0]).strip(), str(key[1]).strip()), [])
        if len(matches) == 1:
            wanted.add(matches[0])
    result: list[SavedFileRecord] = []
    for index in sorted(wanted):
        row = records[index]
        key = (row["連番"], row["枝番"])
        if not all(key) or len(by_key[key]) != 1:
            continue
        direct = row.get("保存ファイルリンク", "")
        linked, title = _literal_link(row.get("保存ファイル(リンク付き)", ""))
        links = [direct] if direct else []
        if linked.startswith(("https://", "=")):
            links.append(linked)
        file_ids = {_drive_file_id(link) for link in links}
        if len(file_ids) != 1 or None in file_ids:
            continue
        file_id = next(iter(file_ids))
        assert file_id is not None
        result.append(
            SavedFileRecord(
                *key,
                slack_source_uri=_source(row),
                drive_file_id=file_id,
                drive_url=f"https://drive.google.com/file/d/{file_id}/view",
                title=title or (linked if not linked.startswith(("https://", "=")) else ""),
            )
        )
    return result
