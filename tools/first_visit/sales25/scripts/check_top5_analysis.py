#!/usr/bin/env python3
"""Validate TOP5 slides and their selected evidence, not claimed viewing or interpretation."""
import argparse
import hashlib
import json
import math
import posixpath
from pathlib import Path
import re
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
import zipfile

from metrics import engagement_rate, normalize_stats

TOP = {"top_views", "top_eng", "top_saves"}
EVIDENCE = ("opening", "development", "product", "ending")
NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
DISCLAIMER = "機械検査は、実際の視聴・聴取、媒体と投稿の同一性、観察・解釈や効果仮説の正しさを証明しません。"


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def load_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique)


def slide_texts(path):
    with zipfile.ZipFile(path) as archive:
        rels = ET.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
        targets = {r.get("Id"): r.get("Target", "") for r in rels}
        root = ET.fromstring(archive.read("ppt/presentation.xml"))
        texts = []
        for slide in root.findall("p:sldIdLst/p:sldId", NS):
            target = targets[slide.get("{" + NS["r"] + "}id")]
            name = target.lstrip("/") if target.startswith("/") else posixpath.normpath("ppt/" + target)
            content = ET.fromstring(archive.read(name))
            texts.append("".join(t.text or "" for t in content.findall(".//a:t", NS)))
        return texts


def validate(plan, reviews, run_dir, pptx):
    errors, limitations = [], [DISCLAIMER]
    incomplete, required, pages = False, set(), {}
    base = Path(run_dir).resolve()

    def fail(where, message):
        errors.append(f"{where}: {message}")

    def file_path(value, where):
        if not nonempty(value):
            fail(where, "non-empty file path required")
            return None
        path = (base / value).resolve()
        if not path.is_relative_to(base) or not path.is_file() or path.stat().st_size == 0:
            fail(where, "file must exist, be non-empty, and resolve inside run-dir")
            return None
        return path

    def section(record, key, where):
        value = record.get(key)
        if not isinstance(value, dict):
            fail(where, f"{key} must be an object")
            return {}
        return value

    if not base.is_dir():
        fail("run-dir", "directory does not exist")
    slides = plan.get("slides") if isinstance(plan, dict) else None
    if not isinstance(slides, list) or not slides:
        fail("plan", "slides must be a non-empty list")
        slides = []
    for page, slide in enumerate(slides, 1):
        if not isinstance(slide, dict) or not isinstance(slide.get("role"), str):
            fail(f"plan page {page}", "role must be a string")
            continue
        if slide["role"] not in TOP:
            continue
        ids, reason = slide.get("post_ids"), slide.get("unavailable_reason")
        if "unavailable_reason" in slide:
            if not nonempty(reason) or ids != []:
                fail(f"plan page {page}", "unavailable TOP requires a non-empty reason and empty post_ids")
            else:
                incomplete = True
                limitations.append(f"page {page}: {reason}")
            continue
        if not isinstance(ids, list) or not 1 <= len(ids) <= 5 or any(not nonempty(i) for i in ids):
            fail(f"plan page {page}", "TOP post_ids must contain 1..5 non-empty strings")
            continue
        if len(set(ids)) != len(ids):
            fail(f"plan page {page}", "duplicate post_ids")
        pages[page] = set(ids)
        required.update(ids)
    if not any(isinstance(s, dict) and isinstance(s.get("role"), str) and s["role"] in TOP for s in slides):
        fail("plan", "no TOP slides")
    if not isinstance(reviews, dict) or type(reviews.get("version")) is not int or reviews["version"] != 1:
        fail("reviews", "version must be integer 1")
        reviews = reviews if isinstance(reviews, dict) else {}
    selection_mode, selection_queries = "legacy_top5", []
    if isinstance(plan, dict) and "analysis_selection" in plan:
        selection_mode = "query_top10"
        selection = section(plan, "analysis_selection", "plan")
        if type(selection.get("top_n")) is not int or selection["top_n"] != 10:
            fail("analysis_selection", "top_n must be integer 10")
        if type(selection.get("target_count_per_query")) is not int or selection["target_count_per_query"] != 100:
            fail("analysis_selection", "target_count_per_query must be integer 100")
        expected_queries = {}
        for slide in slides:
            if not isinstance(slide, dict) or slide.get("role") not in tuple(TOP):
                continue
            query, group = slide.get("query"), slide.get("group")
            if not nonempty(query) or not nonempty(group):
                fail("analysis_selection", "every TOP slide needs query and group")
                continue
            if query in expected_queries and expected_queries[query] != group:
                fail("analysis_selection", f"mixed groups for query {query}")
            expected_queries[query] = group
        queries = selection.get("queries")
        if not isinstance(queries, list) or not queries:
            fail("analysis_selection", "queries must be a non-empty list")
            queries = []
        selected_queries = set()
        for item in queries:
            if not isinstance(item, dict) or not nonempty(item.get("query")):
                fail("analysis_selection", "each query entry must have a non-empty query")
                continue
            query, group = item["query"], item.get("group")
            where = f"analysis_selection query {query}"
            if query in selected_queries:
                fail(where, "duplicate query")
                continue
            selected_queries.add(query)
            if query not in expected_queries or group != expected_queries[query]:
                fail(where, "query/group must match the TOP slides")
            path = file_path(item.get("source_path"), where + " source")
            if path is None:
                continue
            try:
                source = load_json(path)
            except (OSError, ValueError) as exc:
                fail(where, f"cannot read source: {exc}")
                continue
            if not isinstance(source, dict) or source.get("query") != query or not isinstance(source.get("posts"), list):
                fail(where, "source must contain the same query and a posts list")
                continue
            rows, ids, ranks, eligible = source["posts"], set(), set(), []
            if len(rows) > 100:
                fail(where, "source must contain at most 100 acquired posts")
            elif len(rows) < 100:
                incomplete = True
                reason = item.get("shortfall_reason")
                if not nonempty(reason):
                    fail(where, "fewer than 100 acquired posts requires shortfall_reason")
                limitations.append(f"{where}: acquired {len(rows)}/100: {reason or 'reason missing'}")
            for row in rows:
                if (not isinstance(row, dict) or not nonempty(row.get("id"))
                        or type(row.get("rank")) is not int or row["rank"] < 1
                        or type(row.get("eligible")) is not bool):
                    fail(where, "each source post needs string id, positive integer rank, and boolean eligible")
                    continue
                if row["id"] in ids or row["rank"] in ranks:
                    fail(where, "duplicate source post ID or search rank")
                    continue
                ids.add(row["id"])
                ranks.add(row["rank"])
                if row["eligible"]:
                    stats = normalize_stats(row)
                    try:
                        eng = engagement_rate(stats)
                    except ArithmeticError:
                        fail(where, f"invalid engagement calculation for {row['id']}")
                        eng = None
                    eligible.append((row["id"], row["rank"], {
                        "top_views": stats["playCount"], "top_eng": eng, "top_saves": stats["collectCount"]}))
            rankings = section(item, "rankings", where)
            if set(rankings) != TOP:
                fail(where, "rankings must contain exactly top_views, top_eng, top_saves")
            calculated, valid_counts = {}, {}
            for role in sorted(TOP):
                valid = [row for row in eligible if row[2][role] is not None and number(row[2][role])]
                valid.sort(key=lambda row: (-row[2][role], row[1]))
                expected = [row[0] for row in valid[:10]]
                actual = rankings.get(role)
                if (not isinstance(actual, list) or any(not nonempty(pid) for pid in actual)
                        or len(actual) != len(set(actual))):
                    fail(where, f"{role} must contain distinct string post IDs")
                elif actual != expected:
                    fail(where, f"{role} differs from metric order of the eligible source posts")
                calculated[role], valid_counts[role] = expected, len(valid)
                required.update(expected)
                if len(expected) < 10:
                    incomplete = True
                    limitations.append(f"{where}: {role} has {len(expected)}/10 selectable posts "
                                       f"({len(eligible)} eligible, {len(valid)} valid metric values)")
            for page, slide in enumerate(slides, 1):
                if not isinstance(slide, dict) or slide.get("query") != query or slide.get("role") not in tuple(TOP):
                    continue
                expected = calculated[slide["role"]]
                if slide.get("post_ids") != expected[:5]:
                    fail(f"plan page {page}", "displayed TOP5 must equal the first five of its query/metric TOP10")
                if page in pages:
                    pages[page] = set(expected)
            selection_queries.append({"query": query, "group": group, "acquired_n": len(rows),
                                      "eligible_n": len(eligible), "valid_n": valid_counts,
                                      "rankings": calculated,
                                      "required_n": len({pid for ranking in calculated.values() for pid in ranking})})
        if selected_queries != set(expected_queries):
            fail("analysis_selection", "queries must match exactly the queries with TOP slides")
    posts = section(reviews, "posts", "reviews")
    reviewed_n = 0
    for post_id in sorted(required):
        where, before = f"post {post_id}", len(errors)
        record = posts.get(post_id)
        if record is None:
            incomplete = True
            limitations.append(f"{where}: missing review")
            continue
        if not isinstance(record, dict):
            fail(where, "review must be an object")
            continue
        kind = record.get("media_type")
        if kind not in ("video", "photo"):
            fail(where, "media_type must be video or photo")
        url = record.get("url")
        try:
            parsed = urlparse(url if isinstance(url, str) else "")
            host = parsed.hostname or ""
        except ValueError:
            parsed, host = None, ""
        if (parsed is None or parsed.scheme != "https" or not (host == "tiktok.com" or host.endswith(".tiktok.com"))
                or not re.search(r"/(?:video|photo)/" + re.escape(post_id) + r"(?:/|$)", parsed.path)):
            fail(where, "URL must be HTTPS on tiktok.com with a video/photo route identifying this post ID")
        if record.get("identity_verified") is not True or not nonempty(record.get("identity_basis")):
            fail(where, "identity_verified=true and identity_basis required")
        source = section(record, "source", where)
        source_file = file_path(source.get("path"), where + " source")
        if source_file:
            digest = hashlib.sha256()
            with source_file.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if source.get("sha256") != digest.hexdigest():
                fail(where, "source SHA-256 mismatch")
        visual, text = section(record, "visual", where), section(record, "text", where)
        if visual.get("status") != "reviewed" or text.get("status") != "reviewed" or not nonempty(text.get("summary")):
            fail(where, "visual/text reviewed status and text summary required")
        opened = visual.get("opened_files")
        if not isinstance(opened, list) or not opened:
            fail(where, "opened_files must be a non-empty list")
            opened = []
        paths = [file_path(item, where + " opened") for item in opened]
        valid_paths = {p for p in paths if p is not None}
        if len(valid_paths) != len(paths):
            fail(where, "opened_files must be valid distinct files")
        duration, count = record.get("duration_sec"), record.get("photo_count")
        if kind == "video":
            coverage = section(visual, "coverage", where)
            if not number(duration) or duration <= 0:
                fail(where, "positive duration_sec required")
            elif not (number(coverage.get("start_sec")) and coverage["start_sec"] == 0
                      and number(coverage.get("end_sec")) and math.isclose(coverage["end_sec"], duration, abs_tol=0.001, rel_tol=0)):
                fail(where, "visual coverage must span 0..duration_sec")
            if number(duration) and duration > 3 and len(valid_paths) < 3:
                fail(where, "video longer than 3 seconds needs at least three opened files")
        elif kind == "photo" and (type(count) is not int or count < 1 or len(valid_paths) != count):
            fail(where, "photo_count must equal all distinct opened photos, at least one")
        audio = section(record, "audio", where)
        status = audio.get("status")
        audio_complete = status in ("listened", "silent") or (kind == "photo" and status == "not_applicable")
        if not nonempty(audio.get("summary")):
            fail(where, "audio summary required")
        if status in ("listened", "silent"):
            file_path(audio.get("file"), where + " audio")
        elif status == "unmeasured":
            if not nonempty(audio.get("reason")):
                fail(where, "unmeasured audio needs reason")
            incomplete = True
            limitations.append(f"{where}: audio unmeasured: {audio.get('reason', '')}")
            if "file" in audio:
                file_path(audio["file"], where + " audio")
        elif kind == "photo" and status == "not_applicable":
            if not nonempty(audio.get("reason")):
                fail(where, "photo audio not_applicable needs analysis-scope reason")
            limitations.append(f"{where}: photo audio excluded from analysis; this does not establish silence or absence of BGM")
            if "file" in audio:
                file_path(audio["file"], where + " audio")
        else:
            fail(where, "invalid audio status; video requires listened/silent or reasoned unmeasured")
        evidence = section(record, "evidence", where)
        for key in EVIDENCE:
            item = section(evidence, key, where)
            path = file_path(item.get("file"), f"{where} {key}")
            if path not in valid_paths or not nonempty(item.get("observation")):
                fail(f"{where} {key}", "evidence must be opened and have an observation")
            sec, index = item.get("sec"), item.get("photo_index")
            if kind == "video":
                if "photo_index" in item or not number(sec) or not number(duration) or not 0 <= sec <= duration:
                    fail(f"{where} {key}", "valid video sec required; photo_index forbidden")
                elif (key == "opening" and sec > 3) or (key == "ending" and sec < max(0, duration - 3)):
                    fail(f"{where} {key}", "opening/ending evidence outside first/last three seconds")
            elif kind == "photo" and ("sec" in item or type(index) is not int or type(count) is not int or not 1 <= index <= count):
                fail(f"{where} {key}", "valid 1-based photo_index required; sec forbidden")
        if len(errors) == before and audio_complete:
            reviewed_n += 1
    texts = slide_texts(pptx)
    if len(texts) != len(slides):
        fail("PPTX", "slide count differs from plan")
    element_slides, seen = reviews.get("slides"), set()
    if not isinstance(element_slides, list):
        fail("reviews", "slides must be a list")
        element_slides = []
    for item in element_slides:
        page = item.get("page") if isinstance(item, dict) else None
        if type(page) is not int or page not in pages or page in seen:
            fail("elements", "page must identify a unique available TOP slide")
            continue
        seen.add(page)
        elements = item.get("elements")
        if not isinstance(elements, list) or not 1 <= len(elements) <= 3:
            fail(f"page {page}", "1..3 elements required")
            continue
        for element in elements:
            if not isinstance(element, dict) or not nonempty(element.get("text")) or not nonempty(element.get("observation")):
                fail(f"page {page}", "element text and observation required")
                continue
            if "proposal" in element and not isinstance(element["proposal"], str):
                fail(f"page {page}", "optional proposal must be a string")
            if selection_mode == "query_top10" and (
                    not nonempty(element.get("interpretation")) or not nonempty(element.get("proposal"))):
                fail(f"page {page}", "TOP10 elements require non-empty interpretation and proposal")
            if re.sub(r"\s", "", element["observation"]) not in re.sub(r"\s", "", element["text"]):
                fail(f"page {page}", "observation must be included in the displayed element text")
            if page > len(texts) or re.sub(r"\s", "", element["text"]) not in re.sub(r"\s", "", texts[page - 1]):
                fail(f"page {page}", "element text missing from PPTX slide")
            refs = element.get("refs")
            if not isinstance(refs, list) or not refs:
                fail(f"page {page}", "element requires evidence refs")
                continue
            for ref in refs:
                pid, key = (ref.get("post_id"), ref.get("evidence")) if isinstance(ref, dict) else (None, None)
                if not isinstance(pid, str) or pid not in pages[page] or not isinstance(key, str) or key not in EVIDENCE:
                    fail(f"page {page}", "ref must use this page's allowed post ID and an evidence key")
                elif pid in posts and (not isinstance(posts[pid], dict) or not isinstance(posts[pid].get("evidence"), dict) or key not in posts[pid]["evidence"]):
                    fail(f"page {page}", "ref evidence missing from post review")
    for page in pages.keys() - seen:
        fail(f"page {page}", "missing elements")
    return {"status": "invalid" if errors else "incomplete" if incomplete else "passed",
            "errors": errors, "limitations": limitations, "required_n": len(required), "reviewed_n": reviewed_n,
            "selection_mode": selection_mode, "analysis_top_n": 10 if selection_mode == "query_top10" else 5,
            "target_count_per_query": 100 if selection_mode == "query_top10" else None,
            "selection_queries": selection_queries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "reviews", "run-dir", "pptx"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        result = validate(load_json(args.plan), load_json(args.reviews), args.run_dir, args.pptx)
    except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        result = {"status": "invalid", "errors": [str(exc)], "limitations": [DISCLAIMER], "required_n": 0, "reviewed_n": 0}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"passed": 0, "invalid": 1, "incomplete": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
