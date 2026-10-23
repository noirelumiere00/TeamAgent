#!/usr/bin/env python3
"""Fill this kit's anonymous field map; final analysis and visual review remain required."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import sys
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr
import zipfile
from kit_contract import require_contract


ASSETS = Path(__file__).resolve().parents[1] / "assets"
NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
EMU = 9525
TOKEN = re.compile(r"\{\{[^{}]*\}\}")
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_ZIP_BYTES = 256 * 1024 * 1024
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_DIMENSION = 16_000


def tag(prefix, name):
    return "{" + NS[prefix] + "}" + name


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path):
    data = Path(path).read_bytes()
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("Input JSON exceeds 2 MiB")
    return json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique_object)


def safe_part(part):
    if (not isinstance(part, str) or not part or "\\" in part or ":" in part
            or part.startswith("/") or ".." in PurePosixPath(part).parts
            or posixpath.normpath(part) != part.rstrip("/")):
        raise ValueError(f"Unsafe package part: {part!r}")
    return part


def parse_xml(data):
    if b"<!DOCTYPE" in data.upper():
        raise ValueError("DOCTYPE is not supported in template XML")
    namespaces = {prefix: uri for _, (prefix, uri) in ET.iterparse(BytesIO(data), events=["start-ns"])}
    return ET.fromstring(data), namespaces


def xml_bytes(root, namespaces=None):
    namespaces = namespaces or {}
    for prefix, uri in {**namespaces, **NS}.items():
        if not re.fullmatch(r"ns\d+", prefix):
            ET.register_namespace(prefix, uri)
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    # Preserve unused declarations referenced by OOXML attributes such as mc:Ignorable.
    start = data.index(b"<", data.index(b"?>") + 2)
    end = data.index(b">", start)
    opening = data[start:end].decode("utf-8")
    extra = ""
    for prefix, uri in namespaces.items():
        name = "xmlns" + (":" + prefix if prefix else "")
        if not re.search(r"\s" + re.escape(name) + r"=", opening):
            extra += " " + name + "=" + quoteattr(uri)
    return data[:end] + extra.encode("utf-8") + data[end:]


def https_url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Links must be HTTPS URLs without whitespace")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Links must be HTTPS URLs without credentials")
    _ = parsed.port
    return value


def image_file(run, value):
    if not isinstance(value, str) or not value:
        raise ValueError("Image path must be a nonempty string")
    path = Path(value)
    path = Path(os.path.abspath(path if path.is_absolute() else run / path))
    if not path.is_relative_to(run):
        raise ValueError("Image must be inside run-dir")
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise ValueError("Image symlinks are not allowed")
        if parent == run:
            break
    if not path.is_file() or not path.stat().st_size or path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("Image must be a nonempty file no larger than 25 MiB")
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError("Pillow is required to verify PNG/JPEG inputs") from exc
    try:
        with Image.open(path) as image:
            width, height = image.size
            if image.format not in {"PNG", "JPEG"}:
                raise ValueError("Only PNG and JPEG images are supported")
            if max(width, height) > MAX_DIMENSION or width * height > MAX_PIXELS:
                raise ValueError("Image dimensions exceed the supported limit")
            extension = "png" if image.format == "PNG" else "jpeg"
            image.verify()
    except (Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise ValueError(f"Image verification failed: {path.name}: {exc}") from exc
    return path.read_bytes(), width, height, extension


def bbox(value, size):
    if not isinstance(value, dict):
        raise ValueError("Field bbox is required")
    result = [value.get(key) for key in ("left", "top", "width", "height")]
    if any(isinstance(n, bool) or not isinstance(n, (float, int)) or not math.isfinite(n) for n in result):
        raise ValueError("Field bbox must contain finite numbers")
    x, y, width, height = result
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > size[0] + .01 or y + height > size[1] + .01:
        raise ValueError("Field bbox is outside the slide")
    return result


def shape_for(root, name):
    paths = {tag("p", "sp"): "p:nvSpPr/p:cNvPr", tag("p", "pic"): "p:nvPicPr/p:cNvPr",
             tag("p", "graphicFrame"): "p:nvGraphicFramePr/p:cNvPr"}
    found = [shape for shape in root.iter() if shape.tag in paths
             and (node := shape.find(paths[shape.tag], NS)) is not None and node.get("name") == name]
    if len(found) != 1:
        raise ValueError(f"Field shape must exist exactly once: {name}")
    return found[0]


def text_container(shape, field):
    if "cell" not in field:
        return shape
    cell = field["cell"]
    if (not isinstance(cell, dict) or any(type(cell.get(key)) is not int or cell[key] < 0 for key in ("row", "column"))):
        raise ValueError("Cell row/column must be nonnegative integers")
    try:
        return shape.findall(".//a:tbl/a:tr", NS)[cell["row"]].findall("a:tc", NS)[cell["column"]]
    except IndexError as exc:
        raise ValueError("Field cell is outside the table") from exc


def replace_token(container, token, value):
    replaced = 0
    for paragraph in container.findall(".//a:p", NS):
        while True:
            nodes = paragraph.findall(".//a:t", NS)
            joined = "".join(node.text or "" for node in nodes)
            start = joined.find(token)
            if start < 0:
                break
            if token in value:
                raise ValueError("Replacement text cannot contain its own template token")
            end, offset, inserted = start + len(token), 0, False
            for node in nodes:
                text = node.text or ""
                left, right = max(0, start - offset), min(len(text), end - offset)
                if left < right:
                    node.text = text[:left] + (value if not inserted else "") + text[right:]
                    inserted = True
                offset += len(text)
            replaced += 1
        parents = {child: parent for parent in paragraph.iter() for child in parent}
        for node in list(paragraph.findall(".//a:t", NS)):
            if "\n" not in (node.text or ""):
                continue
            run = parents[node]
            if run.tag != tag("a", "r") or parents.get(run) is not paragraph:
                raise ValueError("Unsupported text run for multiline field")
            index = list(paragraph).index(run)
            paragraph.remove(run)
            for number, line in enumerate(node.text.replace("\r\n", "\n").split("\n")):
                if number:
                    paragraph.insert(index, ET.Element(tag("a", "br")))
                    index += 1
                replacement = deepcopy(run)
                replacement.find("a:t", NS).text = line
                replacement.find("a:t", NS).set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                paragraph.insert(index, replacement)
                index += 1
    if not replaced:
        raise ValueError(f"Template token absent from declared field: {token}")


def fill(input_path, run_dir, output, draft=False):
    contract = require_contract(ASSETS.parent)
    run = Path(os.path.abspath(Path(run_dir).expanduser()))
    if run.is_symlink() or not run.is_dir():
        raise ValueError("run-dir must be an existing, nonsymlink directory")
    run = run.resolve()
    output = Path(os.path.abspath(Path(output).expanduser()))
    status_path = output.with_name("build-status.json")
    if output.exists() or output.is_symlink() or status_path.exists() or status_path.is_symlink():
        raise ValueError("Output PPTX/build-status.json already exists; use a new output directory")
    if output.suffix.lower() != ".pptx":
        raise ValueError("Output must end in .pptx")
    source, data = load_json(ASSETS / "fields.json"), load_json(input_path)
    if (not isinstance(source, dict) or source.get("schema_version") != 1
            or not isinstance(source.get("fields"), list) or len(source["fields"]) > 10_000):
        raise ValueError("Unsupported field map")
    size = source.get("slide_size")
    if size != {"width": 1920, "height": 1080}:
        raise ValueError("Field map slide size must be 1920 × 1080")
    if not isinstance(data, dict) or set(data) - {"texts", "images", "links", "notes"}:
        raise ValueError("Input must contain only texts/images/links/notes")
    inputs = {kind: data.get(kind, {}) for kind in ("texts", "images", "links", "notes")}
    if any(not isinstance(value, dict) for value in inputs.values()):
        raise ValueError("texts/images/links/notes must be objects")
    fields, kinds = source["fields"], {}
    for field in fields:
        if (not isinstance(field, dict) or not isinstance(field.get("key"), str) or not field["key"]
                or field.get("kind") not in {"text", "image"} or not isinstance(field.get("token"), str)
                or not TOKEN.fullmatch(field["token"]) or not isinstance(field.get("shape_name"), str)):
            raise ValueError("Invalid field declaration")
        if field["key"] in kinds and kinds[field["key"]] != field["kind"]:
            raise ValueError("A field key cannot mix text and image kinds")
        kinds[field["key"]] = field["kind"]
        safe_part(field.get("part"))
        bbox(field.get("bbox"), (1920, 1080))
    limitations = []
    for kind, expected in (("texts", "text"), ("images", "image"), ("links", None)):
        unknown = set(inputs[kind]) - {key for key, value in kinds.items() if expected is None or value == expected}
        if unknown:
            limitations.append(f"Unknown {kind} keys: {', '.join(sorted(unknown))}")
    for key, kind in kinds.items():
        if key not in inputs["texts" if kind == "text" else "images"]:
            limitations.append(f"Missing {kind} input: {key}")
    if limitations and not draft:
        raise ValueError("; ".join(limitations))
    for key, value in inputs["texts"].items():
        if not isinstance(value, str) or len(value) > 10_000:
            raise ValueError(f"Text must be a string of at most 10000 characters: {key}")
    for value in inputs["links"].values():
        https_url(value)
    with zipfile.ZipFile(ASSETS / "template.pptx") as archive:
        infos = archive.infolist()
        if len(infos) > 10_000 or sum(info.file_size for info in infos) > MAX_ZIP_BYTES:
            raise ValueError("Template ZIP exceeds supported limits")
        if len({info.filename for info in infos}) != len(infos):
            raise ValueError("Duplicate template ZIP parts")
        for info in infos:
            safe_part(info.filename)
        package = {info.filename: archive.read(info) for info in infos if not info.is_dir()}
    trees = {}

    def tree(part):
        if part not in trees:
            if part not in package:
                raise ValueError(f"Template part is absent: {part}")
            trees[part] = parse_xml(package[part])
        return trees[part][0]

    def rels(part):
        name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
        if name not in package and name not in trees:
            trees[name] = (ET.Element("{" + REL + "}Relationships"), {})
        return tree(name) if name in package else trees[name][0]

    def relationship(part, target, kind, external=False):
        root = rels(part)
        for item in root:
            if (item.get("Type") == NS["r"] + "/" + kind and item.get("Target") == target
                    and (item.get("TargetMode") == "External") == external):
                return item.get("Id")
        number = 1
        while any(item.get("Id") == f"rId{number}" for item in root):
            number += 1
        attributes = {"Id": f"rId{number}", "Type": NS["r"] + "/" + kind, "Target": target}
        if external:
            attributes["TargetMode"] = "External"
        ET.SubElement(root, "{" + REL + "}Relationship", attributes)
        return attributes["Id"]

    def content_type(part, value):
        root = tree("[Content_Types].xml")
        if not any(item.get("PartName") == "/" + part for item in root):
            ET.SubElement(root, "{" + CT + "}Override", {"PartName": "/" + part, "ContentType": value})

    def internal_target(part, target):
        if not isinstance(target, str):
            raise ValueError("Internal relationship target is missing")
        return safe_part(posixpath.normpath(target.lstrip("/") if target.startswith("/") else
                                           posixpath.join(posixpath.dirname(part), target)))

    def link(part, node, url):
        for existing in list(node.findall("a:hlinkClick", NS)):
            node.remove(existing)
        ET.SubElement(node, tag("a", "hlinkClick"), {tag("r", "id"): relationship(part, url, "hyperlink", True)})

    def next_id(root):
        return str(1 + max([int(node.get("id", "0")) for node in root.findall(".//p:cNvPr", NS)] or [0]))

    embedded, records = {}, []
    for field in fields:
        key, part, kind = field["key"], field["part"], field["kind"]
        root = tree(part)
        shape = shape_for(root, field["shape_name"])
        container = text_container(shape, field)
        record = {"key": key, "kind": kind, "part": part, "shape_name": field["shape_name"], "status": "missing"}
        records.append(record)
        if kind == "text":
            if key not in inputs["texts"]:
                continue
            replace_token(container, field["token"], inputs["texts"][key])
            if key in inputs["links"]:
                for run_node in container.findall(".//a:r", NS):
                    properties = run_node.find("a:rPr", NS)
                    if properties is None:
                        properties = ET.Element(tag("a", "rPr"))
                        run_node.insert(0, properties)
                    link(part, properties, inputs["links"][key])
            record["status"] = "filled"
            continue
        if shape.tag != tag("p", "sp"):
            raise ValueError("Image slots must be native p:sp placeholders")
        if field["token"] not in "".join(shape.itertext()):
            raise ValueError("Image placeholder does not contain its declared token")
        if key not in inputs["images"]:
            continue
        value = inputs["images"].get(key)
        if not isinstance(value, dict) or set(value) - {"path", "url", "pr_confirmed", "unavailable_reason"}:
            raise ValueError(f"Invalid image input: {key}")
        confirmed = value.get("pr_confirmed", False)
        if type(confirmed) is not bool:
            raise ValueError("pr_confirmed must be true or false")
        if confirmed and "pr_badge_bbox" not in field:
            raise ValueError(f"Confirmed PR has no declared badge position: {key}")
        image_url = value.get("url")
        if image_url is not None:
            https_url(image_url)
        reason = value.get("unavailable_reason")
        if reason is not None:
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 500 or value.get("path"):
                raise ValueError("unavailable_reason must be a nonempty reason without an image path")
            replace_token(shape, field["token"], "画像未取得：" + reason)
            record.update(status="unavailable", unavailable_reason=reason)
            limitations.append(f"Image unavailable: {key}: {reason}")
            continue
        if image_url and key in inputs["links"] and image_url != inputs["links"][key]:
            raise ValueError(f"Conflicting image links: {key}")
        _, _, frame_width, frame_height = bbox(field["bbox"], (1920, 1080))
        image_key = (key, frame_width, frame_height)
        if image_key not in embedded:
            image, width, height, extension = image_file(run, value.get("path"))
            if not math.isclose(width * frame_height, height * frame_width, rel_tol=1e-9):
                from PIL import Image, ImageOps
                # LibreOffice ignores fillRect insets, so pad only the embedded copy.
                canvas_size = (max(1, round(frame_width * 4)), max(1, round(frame_height * 4)))
                if max(canvas_size) > MAX_DIMENSION or math.prod(canvas_size) > MAX_PIXELS:
                    raise ValueError("Image canvas dimensions exceed the supported limit")
                with Image.open(BytesIO(image)) as source_image:
                    contained = ImageOps.contain(source_image.convert("RGBA"), canvas_size, Image.Resampling.LANCZOS)
                    canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
                    canvas.paste(contained, ((canvas.width - contained.width) // 2, (canvas.height - contained.height) // 2))
                    payload = BytesIO()
                    canvas.save(payload, format="PNG")
                image, extension = payload.getvalue(), "png"
            number = len(embedded) + 1
            media = f"ppt/media/filled_image_{number:04d}.{extension}"
            while media in package:
                number += 1
                media = f"ppt/media/filled_image_{number:04d}.{extension}"
            package[media] = image
            content_type(media, "image/" + extension)
            embedded[image_key] = media
        media = embedded[image_key]
        transform = shape.find("p:spPr/a:xfrm", NS)
        properties = shape.find("p:nvSpPr/p:cNvPr", NS)
        if transform is None or properties is None:
            raise ValueError("Image slot is missing its native geometry or identity")
        picture = ET.Element(tag("p", "pic"))
        nonvisual = ET.SubElement(picture, tag("p", "nvPicPr"))
        identity = deepcopy(properties)
        nonvisual.append(identity)
        locks = ET.SubElement(nonvisual, tag("p", "cNvPicPr"))
        ET.SubElement(locks, tag("a", "picLocks"), {"noChangeAspect": "1"})
        ET.SubElement(nonvisual, tag("p", "nvPr"))
        fill_node = ET.SubElement(picture, tag("p", "blipFill"))
        ET.SubElement(fill_node, tag("a", "blip"), {tag("r", "embed"): relationship(part, posixpath.relpath(media, posixpath.dirname(part)), "image")})
        stretch = ET.SubElement(fill_node, tag("a", "stretch"))
        ET.SubElement(stretch, tag("a", "fillRect"), {"l": "0", "r": "0", "t": "0", "b": "0"})
        geometry = ET.SubElement(picture, tag("p", "spPr"))
        geometry.append(deepcopy(transform))
        ET.SubElement(ET.SubElement(geometry, tag("a", "prstGeom"), {"prst": "rect"}), tag("a", "avLst"))
        if image_url or key in inputs["links"]:
            link(part, identity, image_url or inputs["links"][key])
        parent = next(node for node in root.iter() if shape in list(node))
        index = list(parent).index(shape)
        parent.remove(shape)
        parent.insert(index, picture)
        record.update(status="filled", media_part=media)
        if confirmed:
            x, y, badge_width, badge_height = bbox(field["pr_badge_bbox"], (1920, 1080))
            if (badge_width, badge_height) != (64, 36):
                raise ValueError("PR badge must use the declared 64 × 36 pixel frame")
            badge = ET.SubElement(parent, tag("p", "sp"))
            nv = ET.SubElement(badge, tag("p", "nvSpPr"))
            ET.SubElement(nv, tag("p", "cNvPr"), {"id": next_id(root), "name": key + "_PR"})
            ET.SubElement(nv, tag("p", "cNvSpPr")); ET.SubElement(nv, tag("p", "nvPr"))
            props = ET.SubElement(badge, tag("p", "spPr")); xf = ET.SubElement(props, tag("a", "xfrm"))
            ET.SubElement(xf, tag("a", "off"), {"x": str(round(x * EMU)), "y": str(round(y * EMU))})
            ET.SubElement(xf, tag("a", "ext"), {"cx": str(64 * EMU), "cy": str(36 * EMU)})
            ET.SubElement(ET.SubElement(props, tag("a", "prstGeom"), {"prst": "roundRect"}), tag("a", "avLst"))
            ET.SubElement(ET.SubElement(props, tag("a", "solidFill")), tag("a", "srgbClr"), {"val": "000000"})
            body = ET.SubElement(badge, tag("p", "txBody"))
            ET.SubElement(body, tag("a", "bodyPr"), {"anchor": "ctr", "lIns": "0", "rIns": "0", "tIns": "0", "bIns": "0"})
            ET.SubElement(body, tag("a", "lstStyle")); paragraph = ET.SubElement(body, tag("a", "p"))
            ET.SubElement(paragraph, tag("a", "pPr"), {"algn": "ctr"})
            run_node = ET.SubElement(paragraph, tag("a", "r")); font = ET.SubElement(run_node, tag("a", "rPr"), {"sz": "1500", "b": "1"})
            ET.SubElement(ET.SubElement(font, tag("a", "solidFill")), tag("a", "srgbClr"), {"val": "FFFFFF"})
            ET.SubElement(font, tag("a", "latin"), {"typeface": "YuGothic"}); ET.SubElement(font, tag("a", "ea"), {"typeface": "YuGothic"})
            ET.SubElement(run_node, tag("a", "t")).text = "PR"

    presentation = tree("ppt/presentation.xml")
    slide_rels = {item.get("Id"): item.get("Target") for item in rels("ppt/presentation.xml")}
    slide_parts = [internal_target("ppt/presentation.xml", slide_rels[item.get(tag("r", "id"))])
                   for item in presentation.findall("p:sldIdLst/p:sldId", NS)]
    for page, text in inputs["notes"].items():
        if (not page.isascii() or not page.isdigit() or str(int(page)) != page
                or not 1 <= int(page) <= len(slide_parts) or not isinstance(text, str) or len(text) > 50_000):
            raise ValueError("Notes need a valid one-based slide number and at most 50000 characters")
        part = slide_parts[int(page) - 1]
        existing = next((item for item in rels(part) if item.get("Type", "").endswith("/notesSlide")
                         and item.get("TargetMode") != "External"), None)
        if existing is not None:
            notes_part = internal_target(part, existing.get("Target"))
        else:
            number = int(page)
            notes_part = f"ppt/notesSlides/notesSlide{number}.xml"
            while notes_part in package or notes_part in trees:
                number += 1; notes_part = f"ppt/notesSlides/notesSlide{number}.xml"
            relationship(part, posixpath.relpath(notes_part, posixpath.dirname(part)), "notesSlide")
        notes = ET.Element(tag("p", "notes")); common = ET.SubElement(notes, tag("p", "cSld"))
        shapes = ET.SubElement(common, tag("p", "spTree")); nv = ET.SubElement(shapes, tag("p", "nvGrpSpPr"))
        ET.SubElement(nv, tag("p", "cNvPr"), {"id": "1", "name": ""}); ET.SubElement(nv, tag("p", "cNvGrpSpPr")); ET.SubElement(nv, tag("p", "nvPr")); ET.SubElement(shapes, tag("p", "grpSpPr"))
        shape = ET.SubElement(shapes, tag("p", "sp")); nv = ET.SubElement(shape, tag("p", "nvSpPr"))
        ET.SubElement(nv, tag("p", "cNvPr"), {"id": "2", "name": "Evidence notes"}); ET.SubElement(nv, tag("p", "cNvSpPr"))
        ET.SubElement(ET.SubElement(nv, tag("p", "nvPr")), tag("p", "ph"), {"type": "body", "idx": "1"}); ET.SubElement(shape, tag("p", "spPr"))
        body = ET.SubElement(shape, tag("p", "txBody")); ET.SubElement(body, tag("a", "bodyPr")); ET.SubElement(body, tag("a", "lstStyle"))
        for line in text.split("\n"):
            ET.SubElement(ET.SubElement(ET.SubElement(body, tag("a", "p")), tag("a", "r")), tag("a", "t")).text = line
        trees[notes_part] = (notes, {})
        if not any(item.get("Type", "").endswith("/slide") for item in rels(notes_part)):
            relationship(notes_part, posixpath.relpath(part, posixpath.dirname(notes_part)), "slide")
        master = next((name for name in package if name.startswith("ppt/notesMasters/") and name.endswith(".xml")), None)
        if master and not any(item.get("Type", "").endswith("/notesMaster") for item in rels(notes_part)):
            relationship(notes_part, posixpath.relpath(master, posixpath.dirname(notes_part)), "notesMaster")
        content_type(notes_part, "application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml")
    for part, (root, namespaces) in trees.items():
        package[part] = xml_bytes(root, namespaces)
    unresolved = []
    for part, content in package.items():
        if part.endswith(".xml"):
            root, _ = parse_xml(content)
            tokens = TOKEN.findall("".join(root.itertext()))
            if tokens:
                unresolved.append({"part": part, "tokens": sorted(set(tokens))})
    if unresolved and not draft:
        raise ValueError("Unresolved template tokens remain")
    if unresolved:
        limitations.append("Unresolved template tokens remain")
    result = {"schema_version": 1, "status": "partial" if limitations else "filled", "delivery_status": "incomplete",
              "kit_version": contract["version"], "format_id": contract["format_id"],
              "analysis_review": "not_verified", "visual_review": "not_verified", "draft": draft,
              "input_sha256": hashlib.sha256(Path(input_path).read_bytes()).hexdigest(),
              "template_sha256": hashlib.sha256((ASSETS / "template.pptx").read_bytes()).hexdigest(),
              "fields_sha256": hashlib.sha256((ASSETS / "fields.json").read_bytes()).hexdigest(),
              "output": str(output), "fields": records, "limitations": limitations, "unresolved_tokens": unresolved}
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with output.open("xb") as destination:
            created = True
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                for part, content in package.items():
                    archive.writestr(part, content)
        with status_path.open("x", encoding="utf-8") as destination:
            json.dump(result, destination, ensure_ascii=False, indent=2)
            destination.write("\n")
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--draft", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = fill(args.input, args.run_dir, args.output, args.draft)
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({key: value for key, value in result.items() if key != "fields"}, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "partial" else 0


if __name__ == "__main__":
    raise SystemExit(main())
