#!/usr/bin/env python3
"""Check the declared TikTok deck structure; this is not visual or factual QA."""
import argparse
import json
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from kit_contract import require_contract, check_layout

NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
CORE = ("search_order", "section_summary", "top_views", "top_eng", "top_saves")
TOP = set(CORE[2:])
QUERY = TOP | {"search_order"}
ROLES = set(CORE) | {"cover", "overall_summary", "neutral_comparison",
                     "pr_analysis", "approach", "methods"}
SALES25_ROLES = (["cover", "overall_summary"]
                 + ["search_order", "top_views", "top_eng", "top_saves"] * 3
                 + ["search_order", "neutral_comparison"] * 3
                 + ["pr_analysis"] * 3 + ["approach", "methods"])
SALES26_ROLES = SALES25_ROLES[:2] + ["brand_summary_merged"] + SALES25_ROLES[2:]
MARKERS = {"overall_summary": r"サマリ|要約|まとめ|結論",
           "section_summary": r"サマリ|要約|まとめ|結論",
           "brand_summary_merged": r"サマリ|要約|まとめ|結論",
           "search_order": r"表示順|検索順位",
           "top_views": r"再生数", "top_eng": r"エンゲージメント率|ENG\s*率|反応率",
           "top_saves": r"保存数"}
PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}|［[^］\n]*］|\[[^\]\n]*\]|FMT|未入力", re.I)
UNAVAILABLE = re.compile(r"算出不能|算出不可|算出できない|取得不可|取得失敗|取得できない|該当なし|未取得|確認できない|計測不能|計測不可")
UNCHECKED = ["文字あふれ・可読性・全ページの見た目", "動画の実視聴・画像と投稿IDの対応",
             "数値の真偽・ランキング計算・検索表示順の取得根拠", "PR判定・分析内容の妥当性"]
SALES26_FORBIDDEN = re.compile(r"中央値|詳細版|別添|表示未確認|判定不能")
TOP_FORMULA = re.compile(r"(?:反応率|ENG\s*率|エンゲージメント率)\s*[＝=]|÷", re.I)
PR_DETAIL = re.compile(r"^\s*(?:PR(?:判定|区分|確認)?\s*[:：]|無印(?:は|：)|(?:#PR|isAd)\s*[=:：])", re.I)
METADATA_LINE = re.compile(
    r"^\s*(?:取得(?:日時|日)?\s*[:：]|投稿日\s*[:：]?|(?:媒体(?:形式|種別)?|投稿形式)\s*[:：]"
    r"|(?:19|20)\d{2}(?:[/.-]\d{1,2}[/.-]\d{1,2}|年\d{1,2}月\d{1,2}日))")


def validate_plan(plan):
    errors = []
    slides = plan.get("slides") if isinstance(plan, dict) else None
    if not isinstance(slides, list) or not slides:
        return ["plan.slides must be a non-empty list"]
    edition = plan.get("edition")
    sales26 = edition == "sales26"
    sales = edition in ("sales25", "sales26")
    allowed_roles = ROLES | ({"brand_summary_merged"} if sales26 else set())
    for n, slide in enumerate(slides, 1):
        if not isinstance(slide, dict) or not isinstance(slide.get("role"), str) or slide["role"] not in allowed_roles:
            errors.append(f"plan slide {n}: unknown/missing role")
            continue
        role = slide["role"]
        reason = slide.get("unavailable_reason")
        unavailable = role in QUERY and isinstance(reason, str) and bool(reason.strip())
        if "unavailable_reason" in slide and not unavailable:
            errors.append(f"plan slide {n}: unavailable_reason must be a non-empty string on a search/TOP slide")
        comparison = sales and role == "neutral_comparison"
        if (role in CORE or comparison) and (not isinstance(slide.get("group"), str) or not slide["group"].strip()):
            errors.append(f"plan slide {n}: group is required")
        if role in QUERY or comparison or "query" in slide:
            if not isinstance(slide.get("query"), str) or not slide["query"].strip():
                errors.append(f"plan slide {n}: query must be a non-empty string")
        if role in QUERY:
            ids = slide.get("post_ids")
            if not isinstance(ids, list) or (not ids and not unavailable) or any(not isinstance(i, str) or not i.strip() for i in ids):
                errors.append(f"plan slide {n}: valid post_ids are required; empty is allowed only with unavailable_reason")
            elif unavailable and ids:
                errors.append(f"plan slide {n}: unavailable search/TOP must have empty post_ids")
            elif len(ids) != len(set(ids)):
                errors.append(f"plan slide {n}: duplicate post_ids")
        if role in TOP:
            missing = slide.get("media_missing", {})
            if (not isinstance(missing, dict) or not isinstance(slide.get("post_ids"), list)
                    or any(pid not in slide["post_ids"] or not isinstance(reason, str) or not reason.strip()
                           for pid, reason in missing.items())):
                errors.append(f"plan slide {n}: media_missing must name selected posts with explicit reasons")
            count = slide.get("expected_image_count")
            if type(count) is not int or not (count == 0 if unavailable else 1 <= count <= 5):
                errors.append(f"plan slide {n}: expected_image_count must be 0 for unavailable TOP, otherwise 1..5")
            elif isinstance(slide.get("post_ids"), list) and count != len(slide["post_ids"]):
                errors.append(f"plan slide {n}: image count and post_ids count differ")
    if errors:
        return errors
    roles = [s["role"] for s in slides]
    if sales:
        expected = SALES26_ROLES if sales26 else SALES25_ROLES
        if roles != expected:
            return [f"{edition} must contain exactly {len(expected)} slides in the declared {edition} role order"]
        start = 3 if sales26 else 2
        blocks = ([slides[i:i + 4] for i in range(start, start + 12, 4)]
                  + [slides[i:i + 2] for i in range(start + 12, start + 18, 2)])
        for block in blocks:
            if len({s["group"].strip() for s in block}) != 1:
                errors.append(f"{edition} block: mixed groups")
            if len({s["query"].strip() for s in block}) != 1:
                errors.append(f"{edition} block: mixed queries")
        if len({b[0]["group"].strip() for b in blocks}) != 6:
            errors.append(f"{edition} needs six distinct groups")
        if len({b[0]["query"].strip() for b in blocks}) != 6:
            errors.append(f"{edition} needs six distinct queries")
        return errors
    if roles[:2] != ["cover", "overall_summary"] or any(r in {"cover", "overall_summary"} for r in roles[2:]):
        errors.append("plan must begin with exactly one cover then overall_summary")
    groups, closed, active, tail = {}, set(), None, False
    for slide in slides[2:]:
        if slide["role"] not in CORE:
            tail = True
            continue
        group = slide["group"]
        if tail:
            errors.append(f"plan group {group}: core slides must precede optional ending slides")
        if active != group:
            if group in closed:
                errors.append(f"plan group {group}: blocks must be contiguous")
            closed.add(group)
            active = group
        groups.setdefault(group, []).append(slide)
    if not groups:
        errors.append("plan needs at least one complete search group")
    for group, block in groups.items():
        sequence = [s["role"] for s in block]
        while sequence[:2] == ["search_order", "search_order"]:
            sequence.pop(0)
        if sequence != list(CORE):
            errors.append(f"plan group {group}: expected search_order(s), section_summary, top_views, top_eng, top_saves")
        if len({s["query"].strip() for s in block if "query" in s}) != 1:
            errors.append(f"plan group {group}: mixed queries")
        ids = [i for s in block if s["role"] == "search_order" for i in s["post_ids"]]
        if len(ids) != len(set(ids)):
            errors.append(f"plan group {group}: repeated post_ids across search pages")
    return errors


def relationships(archive, part):
    path = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if path not in archive.namelist():
        return {}
    result = {}
    for rel in ET.fromstring(archive.read(path)):
        if rel.get("TargetMode") != "External":
            target = rel.get("Target", "")
            result[rel.get("Id")] = (target.lstrip("/") if target.startswith("/") else
                                     posixpath.normpath(posixpath.join(posixpath.dirname(part), target)))
    return result


def check(pptx, plan, template=False):
    errors, unchecked = validate_plan(plan), list(UNCHECKED)
    if errors:
        return {"status": "invalid", "errors": errors, "unchecked": unchecked}
    edition = plan.get("edition")
    sales = edition in ("sales25", "sales26")
    with zipfile.ZipFile(pptx) as archive:
        presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
        size = presentation.find("p:sldSz", NS)
        width, height = int(size.get("cx")), int(size.get("cy"))
        if (width, height) != (18288000, 10287000):
            errors.append("slide size must be 20 × 11.25 inches")
        rels = relationships(archive, "ppt/presentation.xml")
        paths = [rels[s.get("{" + NS["r"] + "}id")] for s in presentation.findall("p:sldIdLst/p:sldId", NS)]
        if len(paths) != len(plan["slides"]):
            errors.append(f"slide count: actual {len(paths)}, plan {len(plan['slides'])}")
        for n, (path, declared) in enumerate(zip(paths, plan["slides"]), 1):
            root = ET.fromstring(archive.read(path))
            paragraphs = ["".join(t.text or "" for t in paragraph.findall(".//a:t", NS))
                          for paragraph in root.findall(".//a:p", NS)]
            text = "\n".join(paragraphs)
            headers = []
            for shape in root.findall(".//p:sp", NS):
                off = shape.find("p:spPr/a:xfrm/a:off", NS)
                if off is not None and int(off.get("y")) <= height * .2:
                    headers.extend(t.text or "" for t in shape.findall(".//a:t", NS))
            header, role = " ".join(headers), declared["role"]
            if sales:
                forbidden = SALES26_FORBIDDEN.search(text)
                if forbidden:
                    errors.append(f"slide {n}: {edition} visible text contains forbidden term {forbidden.group()}")
                if role in TOP and TOP_FORMULA.search(text):
                    errors.append(f"slide {n}: {edition} TOP formula must be kept on methods only")
                if role in TOP and any(PR_DETAIL.search(line) for line in paragraphs):
                    errors.append(f"slide {n}: {edition} TOP PR explanations must be kept on methods only")
                if role != "cover" and any(METADATA_LINE.search(line) for line in paragraphs):
                    errors.append(f"slide {n}: {edition} publication/media metadata or acquisition footer remains")
            if role in MARKERS and not re.search(MARKERS[role], header, re.I):
                errors.append(f"slide {n}: title does not match role {role}")
            if role in TOP and not re.search(r"TOP\s*5", header, re.I):
                errors.append(f"slide {n}: TOP5 title is missing")
            if (role in QUERY or (sales and role == "neutral_comparison")) and declared["query"].strip() not in text:
                errors.append(f"slide {n}: declared query is absent from slide")
            if "unavailable_reason" in declared:
                if declared["unavailable_reason"].strip() not in text:
                    errors.append(f"slide {n}: unavailable_reason is absent from slide")
                if not UNAVAILABLE.search(text):
                    errors.append(f"slide {n}: unavailable metric status must be stated explicitly")
            if PLACEHOLDER.search(text):
                message = f"slide {n}: placeholders remain"
                (unchecked if template else errors).append(message)
            if role not in TOP:
                continue
            rels, pictures = relationships(archive, path), []
            for pic in root.findall(".//p:pic", NS):
                blip = pic.find("p:blipFill/a:blip", NS)
                target = rels.get(blip.get("{" + NS["r"] + "}embed")) if blip is not None else None
                if not target or target not in archive.namelist() or not archive.read(target):
                    errors.append(f"slide {n}: picture has no embedded image")
                    continue
                transform = pic.find("p:spPr/a:xfrm", NS)
                if transform is None:
                    errors.append(f"slide {n}: picture position cannot be checked")
                    continue
                off, ext = transform.find("a:off", NS), transform.find("a:ext", NS)
                pictures.append(tuple(int(e.get(k)) for e, k in [(off, "x"), (off, "y"), (ext, "cx"), (ext, "cy")]))
            missing = declared.get("media_missing", {})
            for reason in missing.values():
                if reason not in text:
                    errors.append(f"slide {n}: missing media reason is absent from slide")
            expected = declared["expected_image_count"] - len(missing)
            if len(pictures) != expected:
                message = f"slide {n}: embedded image count {len(pictures)}, expected {expected}"
                (unchecked if template and len(pictures) < expected else errors).append(message)
            if not pictures:
                continue
            if any(w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > width or y + h > height for x, y, w, h in pictures):
                errors.append(f"slide {n}: image bounds are invalid")
            if any(h > 0 and abs(w / h - 9 / 16) > (9 / 16) * .15 for x, y, w, h in pictures):
                errors.append(f"slide {n}: images must use a 9:16 portrait frame (±15%)")
            if any(a[0] + a[2] > b[0] for a, b in zip(pictures, pictures[1:])):
                errors.append(f"slide {n}: image columns overlap or are not in left-to-right order")
            if max(p[1] for p in pictures) - min(p[1] for p in pictures) > height * .1:
                errors.append(f"slide {n}: images are not vertically aligned")
    if template:
        unchecked.append("templateモード：未入力ひな形の構造確認のみ。本番資料の合格ではありません")
    return {"status": "invalid" if errors else "template_only" if template else "structure_passed",
            "errors": errors, "unchecked": unchecked}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pptx", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--template", action="store_true")
    args = parser.parse_args()
    try:
        contract = require_contract()
        with open(args.plan, encoding="utf-8") as stream:
            plan = json.load(stream)
        if not isinstance(plan, dict) or plan.get("edition") != contract["format_id"]:
            raise ValueError("This kit requires sales25; do not mix legacy or unrelated FMT plans")
        layout_errors = check_layout(args.pptx)
        result = check(args.pptx, plan, args.template)
        result["errors"].extend(layout_errors)
        if result["errors"]:
            result["status"] = "invalid"
        result.update(kit_version=contract["version"], format_id=contract["format_id"])
    except (OSError, ValueError, KeyError, AttributeError, ET.ParseError, zipfile.BadZipFile) as exc:
        result = {"status": "invalid", "errors": [f"Cannot inspect input: {exc}"], "unchecked": UNCHECKED}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
