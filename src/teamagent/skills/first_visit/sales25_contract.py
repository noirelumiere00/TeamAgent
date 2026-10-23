"""Offline sales25 contract. Deliberately independent of the legacy first-visit API."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

CONTRACT_VERSION = 1
BUNDLE = Path(__file__).resolve().parents[4] / "tools/first_visit/sales25"
LOCK_SHA256 = "03ea4a29ef59b16e325dcd2d07710a9511c55dd08cf4b17f96dfa3eeef2047ca"
STATS = ("playCount", "diggCount", "commentCount", "shareCount", "collectCount")
TOP = ("top_views", "top_eng", "top_saves")


def read_json(path: Path) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def local_path(root: Path, relative: str) -> Path:
    path = root / relative
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError(f"run-relative path required: {relative}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"path outside run: {relative}")
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError(f"symlink forbidden: {relative}")
    return path


def verify_bundle(bundle: Path = BUNDLE) -> dict[str, Any]:
    lock_path = bundle.parent / "sales25.lock.json"
    if hashlib.sha256(lock_path.read_bytes()).hexdigest() != LOCK_SHA256:
        raise ValueError("sales25 lock SHA-256 mismatch")
    lock: dict[str, Any] = read_json(lock_path)
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()}
    if actual != set(lock["files"]):
        raise ValueError("sales25 bundle file membership mismatch")
    for relative, expected in lock["files"].items():
        if hashlib.sha256(local_path(bundle, relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f"sales25 bundle SHA-256 mismatch: {relative}")
    return lock


def _case(case: Mapping[str, Any], owner_id: str) -> list[dict[str, Any]]:
    if not owner_id or case.get("owner_id") != owner_id:
        raise ValueError("owner mismatch")
    if case.get("edition") != "sales25" or not case.get("category"):
        raise ValueError("sales25 edition and category required")
    queries = case.get("queries")
    if not isinstance(queries, list) or len(queries) != 6:
        raise ValueError("six queries required")
    if [q["role"] for q in queries] != [
        "self",
        "competitor",
        "competitor",
        *(["market_keyword"] * 3),
    ]:
        raise ValueError("query order must be self, two competitors, three market keywords")
    for key in ("query_id", "query", "group"):
        values = [q[key] for q in queries]
        if any(not isinstance(v, str) or not v.strip() for v in values) or len(set(values)) != 6:
            raise ValueError(f"six distinct {key} values required")
    if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", q["query_id"]) for q in queries):
        raise ValueError("invalid query_id")
    return queries


def _source(
    query: Mapping[str, Any], raw: Mapping[str, Any], decisions: list[dict[str, Any]], owner_id: str
) -> dict[str, Any]:
    if raw.get("query") != query["query"] or raw.get("owner_id") != owner_id:
        raise ValueError("raw query/owner mismatch")
    datetime.fromisoformat(raw["fetched_at"])
    if (
        raw.get("sessions") != 1
        or type(raw.get("sessions")) is not int
        or raw.get("order_basis") != "search_display_order"
        or raw.get("unfiltered") is not True
        or raw.get("missingness_preserved") is not True
        or raw.get("stop_reason") not in {"exhausted", "no_new", "capped"}
        or raw.get("diagnostics", {}).get("captchaDetected")
    ):
        raise ValueError("single-session unfiltered acquisition evidence required")
    videos = raw["videos"]
    if not isinstance(videos, list):
        raise ValueError("raw videos must be a list")
    if raw.get("status") != ("ok" if videos else "observed_zero") or (
        not videos and raw.get("error_code") != "TIKTOK_TRULY_EMPTY"
    ):
        raise ValueError("acquisition failure/unknown is not measured zero")
    by_id = {d["id"]: d for d in decisions}
    if len(by_id) != len(decisions):
        raise ValueError("duplicate decision ID")
    posts: dict[str, Any] = {}
    for rank, video in enumerate(videos, 1):
        pid, url = video["id"], video["url"]
        parsed = urlsplit(url)
        if (
            not isinstance(pid, str)
            or not re.fullmatch(r"\d{6,25}", pid)
            or parsed.scheme != "https"
            or parsed.hostname not in {"www.tiktok.com", "tiktok.com"}
            or parsed.username
            or parsed.password
            or not re.fullmatch(r"/@[^/]+/(video|photo)/" + re.escape(pid), parsed.path)
        ):
            raise ValueError("post ID/URL mismatch")
        if type(video.get("rank_display")) is not int or video["rank_display"] != rank:
            raise ValueError("raw display rank mismatch")
        missing = video.get("missingFields")
        if not isinstance(missing, list) or any(not isinstance(k, str) for k in missing):
            raise ValueError("missingFields required; legacy zero cannot recover missingness")
        stats = {}
        for key in STATS:
            value = video["stats"].get(key)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("stats require nonnegative integers or null")
            if value is None and f"stats.{key}" not in missing:
                raise ValueError("null stat needs missingFields evidence")
            stats[key] = None if f"stats.{key}" in missing else value
        if pid in posts:
            if posts[pid]["url"] != url:
                raise ValueError("duplicate post URL mismatch")
            continue
        decision = by_id.get(pid, {})
        if type(decision.get("eligible")) is not bool or not decision.get("reason"):
            raise ValueError("every unique raw post needs eligible and reason")
        posts[pid] = {
            **copy.deepcopy(video),
            "stats": stats,
            "rank": rank,
            "eligible": decision["eligible"],
            "reason": decision["reason"],
        }
    if set(by_id) != set(posts) or len(posts) > 100:
        raise ValueError("decisions must match raw IDs; at most 100 unique posts")
    return {"query": query["query"], "posts": list(posts.values())}


def convert_inputs(
    case: Mapping[str, Any],
    raw: Mapping[str, Any],
    decisions: Mapping[str, list[dict[str, Any]]],
    *,
    owner_id: str,
) -> dict[str, Any]:
    """Keep raw intact; deduplicate source by first display rank, independently per query."""
    queries = _case(case, owner_id)
    ids = {q["query_id"] for q in queries}
    if set(raw) != ids or set(decisions) != ids:
        raise ValueError("raw/decisions query IDs differ from case")
    result: dict[str, Any] = {"case.json": copy.deepcopy(case)}
    for query in queries:
        qid = query["query_id"]
        result[f"raw/{qid}.json"] = copy.deepcopy(raw[qid])
        result[f"selection/decisions_{qid}.json"] = copy.deepcopy(decisions[qid])
        result[f"selection/source_{qid}.json"] = _source(query, raw[qid], decisions[qid], owner_id)
    return result


def write_inputs(root: Path, inputs: Mapping[str, Any]) -> None:
    encoded = {local_path(root, rel): json_bytes(value) for rel, value in inputs.items()}
    root.mkdir(parents=True, exist_ok=False)
    for path, data in encoded.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _rankings(posts: list[dict[str, Any]]) -> dict[str, list[str]]:
    values: dict[str, list[tuple[str, int, float]]] = {role: [] for role in TOP}
    for post in posts:
        if not post["eligible"]:
            continue
        s = post["stats"]
        eng = None
        if all(s[k] is not None for k in STATS) and s["playCount"]:
            eng = sum(s[k] for k in STATS[1:]) / s["playCount"] * 100
        for role, value in zip(TOP, (s["playCount"], eng, s["collectCount"]), strict=True):
            if value is not None:
                values[role].append((post["id"], post["rank"], value))
    return {
        role: [p[0] for p in sorted(rows, key=lambda p: (-p[2], p[1]))[:10]]
        for role, rows in values.items()
    }


def validate_inputs(root: Path, *, owner_id: str, bundle: Path = BUNDLE) -> None:
    verify_bundle(bundle)
    case = read_json(local_path(root, "case.json"))
    queries = _case(case, owner_id)
    sources: dict[str, Any] = {}
    urls: dict[str, str] = {}
    for query in queries:
        qid = query["query_id"]
        expected = _source(
            query,
            read_json(local_path(root, f"raw/{qid}.json")),
            read_json(local_path(root, f"selection/decisions_{qid}.json")),
            owner_id,
        )
        source = read_json(local_path(root, f"selection/source_{qid}.json"))
        if source != expected:
            raise ValueError(f"raw/source mismatch: {qid}")
        sources[qid] = source
        for post in source["posts"]:
            if post["id"] in urls and urls[post["id"]] != post["url"]:
                raise ValueError("cross-query post URL mismatch")
            urls[post["id"]] = post["url"]
    plan = read_json(local_path(root, "deck-plan.json"))
    if plan.get("edition") != "sales25" or plan.get("template_only"):
        raise ValueError("authored sales25 plan required")
    template = read_json(bundle / "assets/template-plan.json")
    slides = plan["slides"]
    if [s["role"] for s in slides] != [s["role"] for s in template["slides"]]:
        raise ValueError("25-page sales25 role order required")
    selection = plan.get("analysis_selection")
    if (
        not isinstance(selection, dict)
        or type(selection.get("top_n")) is not int
        or selection.get("top_n") != 10
        or type(selection.get("target_count_per_query")) is not int
        or selection.get("target_count_per_query") != 100
    ):
        raise ValueError("analysis_selection TOP10/100 required; legacy_top5 forbidden")
    selected = selection.get("queries", [])
    if [s["query"] for s in selected] != [q["query"] for q in queries[:3]]:
        raise ValueError("analysis_selection must cover exactly three brand queries")
    for index, query in enumerate(queries):
        qid = query["query_id"]
        block = (
            slides[2 + index * 4 : 6 + index * 4]
            if index < 3
            else slides[14 + (index - 3) * 2 : 16 + (index - 3) * 2]
        )
        if any(s.get("query") != query["query"] or s.get("group") != query["group"] for s in block):
            raise ValueError("plan query/group mismatch")
        if block[0]["post_ids"] != [p["id"] for p in sources[qid]["posts"][:10]]:
            raise ValueError("search-order post IDs mismatch")
        if index < 3:
            item, rankings = selected[index], _rankings(sources[qid]["posts"])
            if (
                item.get("group") != query["group"]
                or item.get("source_path") != f"selection/source_{qid}.json"
                or item.get("rankings") != rankings
            ):
                raise ValueError("TOP10 source/rankings mismatch")
            for slide in block[1:]:
                if slide["post_ids"] != rankings[slide["role"]][:5]:
                    raise ValueError("displayed TOP5 ranking mismatch")
    data = read_json(local_path(root, "deck-input.json"))
    reviews = read_json(local_path(root, "top5-analysis.json"))
    for pid, review in reviews["posts"].items():
        if review.get("url") != urls.get(pid):
            raise ValueError("review/raw post URL mismatch")
        source = local_path(root, review["source"]["path"])
        if hashlib.sha256(source.read_bytes()).hexdigest() != review["source"]["sha256"]:
            raise ValueError("review media SHA-256 mismatch")
        display_files = review["visual"].get("opened_display_files", [])
        if not isinstance(display_files, list) or any(
            not isinstance(p, str) for p in display_files
        ):
            raise ValueError("opened_display_files must be a list of paths")
        paths = [
            *review["visual"]["opened_files"],
            *display_files,
            *(e["file"] for e in review["evidence"].values()),
        ]
        if "file" in review["audio"]:
            paths.append(review["audio"]["file"])
        for relative in paths:
            if not local_path(root, relative).is_file():
                raise ValueError("missing review evidence")
    for image in data["images"].values():
        if not local_path(root, image["path"]).is_file():
            raise ValueError("missing image")
    for slide in slides:
        if slide["role"] in TOP:
            item = next(q for q in selected if q["query"] == slide["query"])
            if set(item["rankings"][slide["role"]]) - reviews["posts"].keys():
                raise ValueError("TOP10 review missing")
    for page, slide in enumerate(slides, 1):
        if slide["role"] not in TOP:
            continue
        for index, pid in enumerate(slide["post_ids"], 1):
            image = data["images"][f"p{page:02d}_image_{index:02d}"]
            if image.get("url") != urls[pid] or image["path"] not in (
                reviews["posts"][pid]["visual"]["opened_files"]
                + reviews["posts"][pid]["visual"].get("opened_display_files", [])
            ):
                raise ValueError("TOP5 image/link order differs from reviewed post IDs")
    _validate_pr(root, plan, data, reviews, sources, bundle)


def _validate_pr(
    root: Path, plan: Any, data: Any, reviews: Any, sources: Any, bundle: Path
) -> None:
    examples = plan.get("pr_examples", [])
    if [e["page"] for e in examples] != [21, 22, 23]:
        raise ValueError("three PR examples required")
    for example in examples:
        page, pid = example["page"], example["post_id"]
        post = next(p for p in sources[example["query_id"]]["posts"] if p["id"] == pid)
        if (
            plan["slides"][page - 1]["post_ids"] != [pid]
            or not example.get("pr_basis")
            or not (
                post.get("isAd") is True
                or any(
                    isinstance(tag, str) and tag.casefold() == "pr"
                    for tag in post.get("hashtags", [])
                )
            )
            or pid not in reviews["posts"]
        ):
            raise ValueError("PR post/evidence mismatch")
        visual = reviews["posts"][pid]["visual"]
        # Display derivatives must not inflate the checker's original photo count.
        opened = visual["opened_files"] + visual.get("opened_display_files", [])
        paths = []
        for index in range(1, 4):
            image = data["images"][f"p{page}_image_{index:02d}"]
            if image.get("url") != post["url"] or image["path"] not in opened:
                raise ValueError("PR three images must refer to the reviewed post")
            local_path(root, image["path"])
            paths.append(image["path"])
        if len(set(paths)) != 3:
            raise ValueError("three distinct PR images required")
        fields = read_json(bundle / "assets/fields.json")["fields"]
        right_keys = [
            f["key"]
            for f in fields
            if f["part"] == f"ppt/slides/slide{page}.xml"
            and f["kind"] == "text"
            and f["bbox"]["height"] >= 160
        ]
        for kind, expected_key in zip(("observation", "proposal"), right_keys, strict=True):
            key = example[f"{kind}_key"]
            if (
                key != expected_key
                or not example.get(kind)
                or data["texts"].get(key) != example[kind]
            ):
                raise ValueError("PR observation/proposal manuscript mismatch")


def _manifest(root: Path, owner_id: str) -> dict[str, Any]:
    files = {}
    for path in sorted(root.rglob("*")):
        local_path(root, path.relative_to(root).as_posix())
        if path.is_file() and path != root / "sales25-manifest.json":
            body = path.read_bytes()
            files[path.relative_to(root).as_posix()] = {
                "sha256": hashlib.sha256(body).hexdigest(),
                "size": len(body),
            }
    return {
        "schema_version": CONTRACT_VERSION,
        "owner_id": owner_id,
        "kit_lock_sha256": LOCK_SHA256,
        "files": files,
    }


def seal_inputs(root: Path, *, owner_id: str, bundle: Path = BUNDLE) -> dict[str, Any]:
    manifest = _manifest(root, owner_id)
    validate_inputs(root, owner_id=owner_id, bundle=bundle)
    if manifest != _manifest(root, owner_id):
        raise ValueError("inputs changed during validation")
    with local_path(root, "sales25-manifest.json").open("xb") as stream:
        stream.write(json_bytes(manifest))
    return manifest


def verify_manifest(root: Path, *, owner_id: str, bundle: Path = BUNDLE) -> None:
    if read_json(local_path(root, "sales25-manifest.json")) != _manifest(root, owner_id):
        raise ValueError("manifest owner/file membership/SHA-256 mismatch")
    validate_inputs(root, owner_id=owner_id, bundle=bundle)
    if read_json(root / "sales25-manifest.json") != _manifest(root, owner_id):
        raise ValueError("inputs changed during validation")
