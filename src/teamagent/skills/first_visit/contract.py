"""Offline conversion and evidence gate for the pinned first-visit kit.

Acquisition metadata is evidence supplied at capture, never inferred from counts.
Legacy normalized zeros are ambiguous unless missingness was preserved upstream.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from teamagent.skills.first_visit.kit import archive_kit_problems
from teamagent.skills.omiyage_report.metrics import AxisData, PostRecord
from teamagent.skills.omiyage_report.schema import OmiyageReportSubmitInput

CONTRACT_VERSION = 1
KIT_VERSION = "2026.10.14.1"
KIT_COMMIT = "25eccd8219f01cad18dbb4f5a863b5f41d8f52fb"
KIT_TREE = "e12d92cc1fe03a3c3e0b7a75acdaa66b8d128ad6"
KIT_GATE_SHA256 = "4e84c1aae75f4a97f7f67d4626252db11183309285b3ee4d4bfa847d4c83409f"
_STATS = {
    "plays": "playCount",
    "likes": "diggCount",
    "comments": "commentCount",
    "shares": "shareCount",
    "saves": "collectCount",
}


class IncompleteInputsError(ValueError):
    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("初訪入力が未完了:\n" + "\n".join(self.problems))


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _handle(value: str) -> str:
    if value.startswith("https://"):
        parsed = urlparse(value)
        if parsed.hostname not in {"tiktok.com", "www.tiktok.com"}:
            raise ValueError("公式アカウントはTikTokのURLが必要です")
        value = parsed.path.strip("/")
    value = value.removeprefix("@")
    if not re.fullmatch(r"[\w.]+", value, flags=re.ASCII):
        raise ValueError("公式アカウントのハンドルが不正です")
    return value


def _legacy_row(post: PostRecord, query: str) -> dict[str, Any]:
    return {
        "provider_id": post.video_id,
        "url": post.url,
        "kw": query,
        "rank_display": post.rank,
        "title": post.caption,
        "hashtags": list(post.hashtags),
        "account_id": post.author,
        "account_name": post.nickname,
        "cover_url": post.cover_url,
        "duration": post.duration_sec,
        "followers": post.followers,
        **{field: getattr(post, field) for field in _STATS},
    }


def _identity(vid: Any, url: Any) -> None:
    if not isinstance(vid, str) or not re.fullmatch(r"\d{6,25}", vid):
        raise ValueError("provider_id: 取得元の文字列IDが必要です（pidは使用不可）")
    if not isinstance(url, str):
        raise ValueError(f"{vid}: URLが必要です")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"tiktok.com", "www.tiktok.com"}
        or not re.fullmatch(r"/@[^/]+/(?:video|photo)/" + vid, parsed.path.rstrip("/"))
    ):
        raise ValueError(f"{vid}: IDとURLが一致しません")


def _video(row: Mapping[str, Any], preserved: bool) -> dict[str, Any]:
    missing = row.get("missingFields") or []
    if not isinstance(missing, list) or any(not isinstance(field, str) for field in missing):
        raise ValueError("missingFieldsは文字列配列が必要です")
    unknown = row.get("unknownFields") or []
    if not isinstance(unknown, list) or any(not isinstance(field, str) for field in unknown):
        raise ValueError("unknownFieldsは文字列配列が必要です")
    targets = {
        **{k: f"stats.{v}" for k, v in _STATS.items()},
        "followers": "author.followerCount",
        "create_time": "createTime",
        "duration": "duration",
    }
    if set(unknown) - set(targets.values()) or set(unknown) & set(missing):
        raise ValueError("unknownFieldsは欠測と重複しない数値項目が必要です")
    unknown = [
        target
        for field, target in targets.items()
        if target not in missing
        and (target in unknown or (not preserved and row.get(field) in (None, 0)))
    ]

    def number(field: str) -> Any:
        value = row.get(field)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{field}: 非負整数またはnullが必要です")
        return None if targets[field] in missing or targets[field] in unknown else value

    vid, url = row.get("provider_id"), row.get("url")
    _identity(vid, url)
    ad = row.get("isAd")
    if ad is not None and not isinstance(ad, bool):
        raise ValueError(f"{vid}: isAdはbooleanまたはnullが必要です")
    video = {
        "id": vid,
        "url": url,
        "desc": row.get("title"),
        "hashtags": row.get("hashtags"),
        "author": {
            "uniqueId": row.get("account_id"),
            "nickname": row.get("account_name"),
            "followerCount": number("followers"),
        },
        "stats": {target: number(source) for source, target in _STATS.items()},
        "coverUrl": row.get("cover_url"),
        "createTime": number("create_time"),
        "duration": number("duration"),
        "rank_display": row.get("rank_display"),
        "isAd": ad,
        "mediaType": row.get("mediaType"),
        "imageCount": row.get("imageCount"),
        "textLanguage": row.get("textLanguage"),
    }
    video["missingFields"] = [
        target
        for field, target in targets.items()
        if number(field) is None and target not in unknown
    ]
    if unknown:
        video["unknownFields"] = unknown
    return video


def convert_inputs(
    request: OmiyageReportSubmitInput,
    axes: Sequence[AxisData],
    normalized: Mapping[str, Any] | None,
    *,
    acquisition: Mapping[str, Mapping[str, Any]],
    selection: Sequence[Mapping[str, Any]] = (),
    case_details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return relative JSON paths; retain unfiltered media rows and every axis occurrence.

    ``acquisition`` keys are ``<AxisData.role>:<AxisData.label>``. Its
    missingness_preserved/unfiltered declarations require upstream capture evidence.
    ``case_details.brand_details`` supplies confirmed display/official information.
    Saved captures can supply ``axis_posts``/``axis_shortfalls`` keyed by each
    acquisition's ``axis_id`` to keep separate snapshots with the same query.
    """
    details = dict(case_details or {})
    brands = details.pop("brand_details", {})
    allowed = {"vocab", "focus_products", "project", "client_short", "research", "first_visit"}
    if details.keys() - allowed:
        raise ValueError(f"未知の案件補完項目: {sorted(details.keys() - allowed)}")
    cfg: dict[str, Any] = {
        **details,
        "category": request.category or None,
        "client": {"client_name": request.brand, "short": details.pop("client_short", None)},
        "brands": [],
        "keywords": [],
        "acquisition": {},
    }
    cfg.pop("client_short", None)
    inputs: dict[str, Any] = {"case.json": cfg}
    posts = normalized.get("posts", []) if normalized is not None else []
    shortfalls = normalized.get("shortfalls", []) if normalized is not None else []
    if not isinstance(posts, list) or any(not isinstance(row, dict) for row in posts):
        raise ValueError("posts.normalized.json: postsはオブジェクト配列が必要です")
    if not isinstance(shortfalls, list) or any(not isinstance(row, dict) for row in shortfalls):
        raise ValueError("posts.normalized.json: shortfallsはオブジェクト配列が必要です")
    names = {
        "brand": [request.brand],
        "competitor": request.competitors,
        "general": request.keywords,
    }
    if any(
        sum(axis.role == role for axis in axes) != len(values) for role, values in names.items()
    ) or len(axes) != sum(map(len, names.values())):
        raise ValueError("依頼と検索軸が一致しない、または検索軸が重複しています")
    prefixes = {"general": "一般KW", "brand": "ブランド名", "competitor": "競合"}
    used: set[tuple[str, str]] = set()
    known: dict[str, str] = {}
    for index, axis in enumerate(axes, 1):
        matches = []
        for candidate in names[axis.role]:
            label = f"{prefixes[axis.role]}「{candidate}」検索"
            if axis.role != "general" and axis.query != candidate:
                label += f"（「{axis.query}」で検索）"
            if axis.label in {candidate, label}:
                matches.append(candidate)
        if len(matches) != 1 or (axis.role, matches[0]) in used:
            raise ValueError("依頼とAxisDataの検索軸名が一致しない、または重複しています")
        name = matches[0]
        used.add((axis.role, name))
        file = f"raw/axis_{index:02d}.json"
        meta = dict(acquisition.get(f"{axis.role}:{axis.label}", {}))
        rows = [row for row in posts if row.get("kw") == axis.query]
        failures = [s for s in shortfalls if s.get("kw") == axis.query]
        for field, values in (("axis_posts", rows), ("axis_shortfalls", failures)):
            if normalized is not None and field in normalized:
                keyed = normalized[field]
                if not isinstance(keyed, dict) or meta.get("axis_id") not in keyed:
                    raise ValueError(f"{field}: 軸IDごとの取得結果が必要です")
                captured = keyed[meta["axis_id"]]
                if not isinstance(captured, list) or any(not isinstance(r, dict) for r in captured):
                    raise ValueError(f"{field}: 投稿/不足はオブジェクト配列が必要です")
                if field == "axis_posts" and any(r.get("kw") != axis.query for r in captured):
                    raise ValueError("axis_posts: 投稿の実検索語が取得軸と一致しません")
                values[:] = captured
        media_present = normalized is not None
        if not media_present:
            rows = [_legacy_row(post, axis.query) for post in axis.posts]
        failed = (
            axis.failed
            or meta.get("status") == "failed"
            or any(
                s.get("reason") not in {"MEDIA_TIKTOK_RESULT_SHORTFALL", "MEDIA_TIKTOK_TRULY_EMPTY"}
                for s in failures
            )
        )
        videos = [
            _video(row, media_present and meta.get("missingness_preserved") is True) for row in rows
        ]
        for video in videos:
            if video["id"] in known and known[video["id"]] != video["url"]:
                raise ValueError(f"{video['id']}: 軸間でURLが矛盾しています")
            known[video["id"]] = video["url"]
        status = "failed" if failed else meta.get("status", "unknown")
        try:
            acquired_date = datetime.fromisoformat(meta.get("acquired_on") or "").date().isoformat()
        except (ValueError, TypeError):
            acquired_date = None
        raw = {
            "videos": videos,
            "query": axis.query,
            "ok": status == "ok",
            "fetched_on": acquired_date,
            "order_basis": meta.get("order_basis"),
            "stop_reason": meta.get("stop_reason", "unknown"),
            "diag": meta.get("diag", {}),
            "errorCode": (
                axis.failure_code
                or next(
                    (s.get("reason") for s in failures if s.get("reason")), "ACQUISITION_UNKNOWN"
                )
            )
            if failed
            else ("TIKTOK_TRULY_EMPTY" if status == "observed_zero" else None),
            "aico": {
                "role": axis.role,
                "label": axis.label,
                "requested": axis.requested,
                "status": status,
                "source": meta,
                "media_present": media_present,
                "shortfalls": failures,
            },
        }
        inputs[file] = raw
        cfg["acquisition"][name] = raw["stop_reason"]
        item: dict[str, Any] = {"name": name, "query": axis.query, "file": file}
        if axis.role == "general":
            item["primary"] = not cfg["keywords"]
            if item["primary"]:
                cfg["acquired_on"] = acquired_date
            cfg["keywords"].append(item)
        else:
            extra = dict(brands.get(name, {}))
            if extra.keys() - {"short", "official", "official_status"}:
                raise ValueError("ブランド補完はshort/official/official_statusだけ指定できます")
            official = extra.get("official")
            if official is None:
                official = (
                    [request.official_tiktok_account]
                    if axis.role == "brand" and request.official_tiktok_account
                    else []
                )
            if not isinstance(official, list) or any(not isinstance(v, str) for v in official):
                raise ValueError("officialはハンドルの配列が必要です")
            item.update(extra, own=axis.role == "brand", official=[_handle(v) for v in official])
            item.setdefault("official_status", "unknown")
            cfg["brands"].append(item)
    selected: list[dict[str, Any]] = []
    for row in selection:
        vid = row.get("id")
        if not isinstance(vid, str) or vid not in known or row.get("url") != known[vid]:
            raise ValueError("selectionは取得済みの文字列IDと同じURLが必要です")
        if any(s["id"] == vid for s in selected):
            raise ValueError("selectionの投稿IDが重複しています")
        selected.append(dict(row))
    inputs["selection.json"] = selected
    return inputs


def _path(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError(f"案件外のパス: {relative}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"案件外のパス: {relative}")
    return path


def _validate_video(video: Mapping[str, Any]) -> None:
    _identity(video.get("id"), video.get("url"))
    numeric = {
        **{f"stats.{field}": video.get("stats", {}).get(field) for field in _STATS.values()},
        "author.followerCount": video.get("author", {}).get("followerCount"),
        "createTime": video.get("createTime"),
        "duration": video.get("duration"),
    }
    missing = video.get("missingFields")
    if not isinstance(missing, list) or any(not isinstance(field, str) for field in missing):
        raise ValueError(f"{video['id']}: missingFieldsが不正です")
    unknown = video.get("unknownFields", [])
    if (
        not isinstance(unknown, list)
        or any(not isinstance(field, str) for field in unknown)
        or len(set(unknown)) != len(unknown)
        or set(unknown) - numeric.keys()
        or set(unknown) & set(missing)
    ):
        raise ValueError(f"{video['id']}: unknownFieldsが不正です")
    for field, value in numeric.items():
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{video['id']}: {field}は非負整数またはnullが必要です")
        if (value is None) != (field in missing or field in unknown):
            raise ValueError(f"{video['id']}: {field}の欠測記録が矛盾しています")
    if video.get("hashtags") is not None and (
        not isinstance(video["hashtags"], list)
        or any(not isinstance(tag, str) for tag in video["hashtags"])
    ):
        raise ValueError(f"{video['id']}: hashtagsは文字列配列が必要です")


def write_inputs(root: Path, inputs: Mapping[str, Any]) -> None:
    """Create a new case only; never overwrite a prepared case or old omiyage files."""
    encoded = {_path(root, rel): _json_bytes(value) for rel, value in inputs.items()}
    root.mkdir(parents=True, exist_ok=False)
    for path, data in encoded.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _kit_problems(kit_path: Path) -> list[str]:
    try:
        version = (kit_path / "VERSION").read_text(encoding="utf-8").strip()

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "--no-optional-locks", "-C", str(kit_path), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.strip()

        if (
            version != KIT_VERSION
            or git("rev-parse", "HEAD") != KIT_COMMIT
            or git("rev-parse", "HEAD^{tree}") != KIT_TREE
            or git("status", "--porcelain", "--untracked-files=all")
            or hashlib.sha256(
                (kit_path / "tiktok-deck/tools/research_input_gate.py").read_bytes()
            ).hexdigest()
            != KIT_GATE_SHA256
        ):
            return ["kit: 固定VERSION・git指紋と一致する未変更のキットが必要です"]
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"kit: 固定版を検証できません: {exc}"]
    return []


_GATE_SCRIPT = r"""
import json, sys
from pathlib import Path
root, kit = map(Path, sys.argv[1:3])
part = sys.argv[3]
sys.path.insert(0, str(kit / "tiktok-deck/tools"))
from research_input_gate import InputGateError, validate_research_inputs
from label_posts import axes_of
def read(file):
    return json.loads((root / file).read_text(encoding="utf-8"))
try:
    cfg = read("case.json")
    raw = {}
    for axis in axes_of(cfg):
        for video in read(axis["file"])["videos"]:
            raw.setdefault(video["id"], video)
    labels = {p["video_id"]: p for p in read("labels.json")["posts"]}
    rcfg = cfg.get("research") or {}
    reviews = root / "reviews" if rcfg.get("review_collect") is True else None
    spec = rcfg.get("review_spec")
    if spec and str(spec).startswith("@"):
        spec_path = kit / "tiktok-deck/references/research" / ("review_spec_" + spec[1:] + ".json")
    elif spec:
        spec_path = root / spec
    else:
        spec_path = kit / "tiktok-deck/references/research/review_spec_haircare.json"
    result = validate_research_inputs(
        case_dir=root, run_dir=root, cfg=cfg, raw=raw, labels=labels,
        synthesis=read("authored/synthesis.json"), selection_path=root / "selection.json",
        reviews_dir=reviews, explicit_reviews=reviews is not None, part=part,
        review_spec=json.loads(spec_path.read_text(encoding="utf-8")),
    )
    print(json.dumps({"problems": [], "quality_contract": result["quality_contract"]}))
except InputGateError as exc:
    print(json.dumps({"problems": exc.problems}))
except (OSError, ValueError, TypeError, KeyError, SystemExit) as exc:
    print(json.dumps({"problems": ["kit input gate: " + str(exc)]}))
"""


def missing_inputs(
    root: Path,
    *,
    kit_path: Path,
    kit_python: Path | None = None,
    part: Literal["first", "concrete"] = "first",
    kit_archive: Path | None = None,
) -> list[str]:
    """List missing decisions/evidence, then run the actual pinned kit gate read-only."""
    if part not in {"first", "concrete"}:
        return ["part: first または concrete が必要です"]
    problems = (
        _kit_problems(kit_path)
        if kit_archive is None
        else archive_kit_problems(kit_path, kit_archive)
    )
    try:
        if root.is_symlink() or any(p.is_symlink() for p in root.rglob("*")):
            return [*problems, "manifest: 案件内のシンボリックリンクは使用できません"]
        cfg = _read(root / "case.json")
        for field in ("category", "vocab", "focus_products"):
            if not cfg.get(field):
                problems.append(f"case.json: {field}を確定してください")
        if cfg.get("vocab") and cfg["vocab"] not in {"food", "beauty", "general"}:
            if not _path(root, cfg["vocab"]).is_file():
                problems.append("case.json: 案件内の語彙ファイルが必要です")
        for field in ("project_title", "recipient"):
            if not (cfg.get("project") or {}).get(field):
                problems.append(f"case.json: project.{field}を確定してください")
        brands, keywords = cfg.get("brands", []), cfg.get("keywords", [])
        if sum(b.get("own") is True for b in brands) != 1 or not keywords:
            problems.append("case.json: 自社1軸と一般検索軸が必要です")
        if not isinstance((cfg.get("research") or {}).get("review_collect"), bool):
            problems.append("case.json: research.review_collectが未回答です")
        files = [item["file"] for item in [*brands, *keywords]]
        if len(files) != len(set(files)):
            problems.append("case.json: 軸ごとに異なるrawファイルが必要です")
        for brand in brands:
            if not brand.get("short"):
                problems.append(f"case.json: {brand['name']}のshortを確定してください")
            status, official = brand.get("official_status"), brand.get("official")
            if (
                status not in {"confirmed", "none"}
                or not isinstance(official, list)
                or (status == "confirmed" and not official)
                or (status == "none" and official)
            ):
                problems.append(f"case.json: {brand['name']}の公式確認が未完了/矛盾しています")
            for handle in official or []:
                account_file = f"raw/account_{_handle(handle)}.json"
                if not _path(root, account_file).is_file():
                    problems.append(f"公式投稿: {account_file}の取得結果/失敗記録が必要です")
        ids: dict[str, str] = {}
        for item in [*brands, *keywords]:
            file = item["file"]
            raw = _read(_path(root, file))
            source = raw.get("aico", {})
            meta = source.get("source", {})
            try:
                acquired_date = datetime.fromisoformat(meta.get("acquired_on") or "").date()
                if raw.get("fetched_on") != acquired_date.isoformat():
                    problems.append(f"{file}: fetched_onと元取得日時が一致しません")
            except (ValueError, TypeError):
                problems.append(f"{file}: acquired_onが不明/不正です")
            if meta.get("stop_reason") not in {"exhausted", "no_new", "capped", "unknown"}:
                problems.append(f"{file}: stop_reasonが不明/不正です")
            if raw.get("query") != item.get("query"):
                problems.append(f"{file}: 実検索語がcase.jsonと一致しません")
            if (
                meta.get("order_basis") != "search_display_order"
                or type(meta.get("sessions")) is not int
                or meta.get("sessions") != 1
                or meta.get("diag", {}).get("captchaDetected")
            ):
                problems.append(f"{file}: セッション1の検索表示順を確認できません")
            for field in ("unfiltered", "missingness_preserved"):
                if meta.get(field) is not True or source.get("media_present") is not True:
                    problems.append(f"{file}: {field}の取得境界での保存が必要です")
            videos = raw.get("videos", [])
            status = source.get("status")
            if status not in {"ok", "observed_zero"} or (status == "ok" and not videos):
                problems.append(f"{file}: 取得失敗/未確定は実測0として使用できません")
            if status == "observed_zero" and (
                videos
                or raw.get("errorCode") != "TIKTOK_TRULY_EMPTY"
                or not (
                    meta.get("error_code") == "TIKTOK_TRULY_EMPTY"
                    or any(
                        s.get("reason") == "MEDIA_TIKTOK_TRULY_EMPTY"
                        for s in source.get("shortfalls", [])
                    )
                )
            ):
                problems.append(f"{file}: 実測0の取得記録が矛盾しています")
            if raw.get("ok") is not (status == "ok"):
                problems.append(f"{file}: 取得状態とokが矛盾しています")
            if source.get("role") == "general" and not videos:
                problems.append(f"{file}: 初訪には一般検索の投稿が必要です")
            ranks = [v.get("rank_display") for v in videos]
            if any(type(rank) is not int for rank in ranks) or (
                ranks != list(range(1, len(videos) + 1))
            ):
                problems.append(f"{file}: 元順位を保持した未除外の連続した表示順が必要です")
            axis_ids = [v["id"] for v in videos]
            if len(set(axis_ids)) != len(axis_ids):
                problems.append(f"{file}: 同一軸の投稿IDが重複しています")
            for video in videos:
                _validate_video(video)
                vid = video["id"]
                if vid in ids and ids[vid] != video["url"]:
                    problems.append(f"{vid}: 軸間でURLが一致しません")
                ids[vid] = video["url"]
                if not isinstance(video.get("isAd"), bool):
                    problems.append(f"{file}: {vid}の広告フラグが未取得です")
                if video.get("mediaType") not in {"video", "photo"}:
                    problems.append(f"{file}: {vid}の媒体種別が未取得です")
                if not (root / "assets/covers" / f"{vid}.jpg").is_file():
                    problems.append(f"assets/covers/{vid}.jpg: カバー実体が必要です")
        required_files = [
            "labels.json",
            "first_visit.json",
            "selection.json",
            "authored/synthesis.json",
        ]
        if (root / "selection.json").is_file():
            selected = _read(root / "selection.json")
            if not isinstance(selected, list) or not selected:
                problems.append("selection.json: 分析対象が未選定です")
            else:
                for row in selected:
                    vid = row.get("id") if isinstance(row, dict) else None
                    if not isinstance(vid, str) or not re.fullmatch(r"\d{6,25}", vid):
                        problems.append("selection.json: 投稿IDが不正です")
                        continue
                    required_files.extend([f"anatomy/{vid}.json", f"anatomy_page/{vid}.json"])
                    selected_video: dict[str, Any] = next(
                        (
                            v
                            for file in files
                            for v in _read(root / file)["videos"]
                            if v["id"] == vid
                        ),
                        {},
                    )
                    if selected_video.get("mediaType") != "photo":
                        required_files.extend(
                            [
                                f"media/{vid}.mp4",
                                f"frames/{vid}/frames.json",
                                f"transcripts/{vid}.json",
                            ]
                        )
        for file in required_files:
            if not (root / file).is_file():
                problems.append(f"{file}: 未作成です")
        if (root / "labels.json").is_file():
            for post in _read(root / "labels.json").get("posts", []):
                if post.get("cover"):
                    _path(root, post["cover"])
        for file in (root / "anatomy").glob("*.json"):
            for opened in _read(file).get("opened", []):
                _path(root, opened)
        spec = (cfg.get("research") or {}).get("review_spec")
        if spec:
            if str(spec).startswith("@"):
                if not re.fullmatch(r"@[\w-]+", spec):
                    problems.append("case.json: 口コミ分類表のプリセットが不正です")
            elif not _path(root, spec).is_file():
                problems.append("case.json: 案件内の口コミ分類表が必要です")
        if not problems:
            result = subprocess.run(
                [
                    str(kit_python or sys.executable),
                    "-I",
                    "-B",
                    "-c",
                    _GATE_SCRIPT,
                    str(root.resolve()),
                    str(kit_path.resolve()),
                    part,
                ],
                capture_output=True,
                text=True,
                check=True,
                cwd=root,
                timeout=60,
            )
            problems.extend(json.loads(result.stdout)["problems"])
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        subprocess.SubprocessError,
    ) as exc:
        problems.append(f"入力: {exc}")
    return problems


def _manifest(root: Path) -> dict[str, Any]:
    if root.is_symlink():
        raise ValueError("manifest: シンボリックリンクは使用できません")
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("manifest: シンボリックリンクは使用できません")
        if path.is_file() and path != root / "manifest.json":
            data = path.read_bytes()
            files[path.relative_to(root).as_posix()] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "contract_version": CONTRACT_VERSION,
            }
    return {
        "schema_version": CONTRACT_VERSION,
        "kit": {
            "version": KIT_VERSION,
            "git_commit": KIT_COMMIT,
            "git_tree": KIT_TREE,
            "research_input_gate_sha256": KIT_GATE_SHA256,
        },
        "files": files,
    }


def seal_inputs(
    root: Path,
    *,
    kit_path: Path,
    kit_python: Path | None = None,
    part: Literal["first", "concrete"] = "first",
    kit_archive: Path | None = None,
) -> dict[str, Any]:
    """Seal complete evidence only. No renderer is invoked by this module."""
    manifest = _manifest(root)
    problems = missing_inputs(
        root, kit_path=kit_path, kit_python=kit_python, part=part, kit_archive=kit_archive
    )
    if problems:
        raise IncompleteInputsError(problems)
    if manifest != _manifest(root):
        raise IncompleteInputsError(["manifest: 検査中に入力ファイルが変更されました"])
    with (root / "manifest.json").open("xb") as stream:
        stream.write(_json_bytes(manifest))
    return manifest


def verify_manifest(
    root: Path,
    *,
    kit_path: Path,
    kit_python: Path | None = None,
    part: Literal["first", "concrete"] = "first",
    kit_archive: Path | None = None,
) -> list[str]:
    """Verify exact file membership, bytes and pin before a future generation step."""
    try:
        if _read(root / "manifest.json") != _manifest(root):
            return ["manifest: ファイル一覧・SHA-256・サイズ・版が一致しません"]
    except (OSError, ValueError) as exc:
        return [f"manifest: {exc}"]
    problems = missing_inputs(
        root, kit_path=kit_path, kit_python=kit_python, part=part, kit_archive=kit_archive
    )
    try:
        if _read(root / "manifest.json") != _manifest(root):
            problems.append("manifest: 検査中に入力ファイルが変更されました")
    except (OSError, ValueError) as exc:
        problems.append(f"manifest: {exc}")
    return problems
