"""Verify the bundled sales25 assets and native slide geometry."""
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = ("assets/template.pptx", "assets/fields.json", "assets/template-plan.json")
NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}


def inspect_contract(root=ROOT):
    root = Path(root)
    result = {"version": None, "format_id": None, "status": "missing", "errors": []}
    try:
        manifest = root / "assets/build-info.json"
        if not manifest.is_file():
            return {**result, "errors": ["contract_missing"]}
        if manifest.is_symlink() or manifest.parent.is_symlink() or manifest.stat().st_size > 8192:
            raise ValueError()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or not isinstance(data.get("version"), str)
                or not re.fullmatch(r"\d+\.\d+\.\d+", data["version"])
                or data.get("format_id") != "sales25" or not isinstance(data.get("files"), dict)
                or set(data["files"]) != set(FILES)
                or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in data["files"].values())):
            raise ValueError()
        result.update(version=data["version"], format_id=data["format_id"], status="valid")
        for name in FILES:
            path = root / name
            if (path.is_symlink() or not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != data["files"][name]):
                result.update(status="mismatch", errors=["asset_mismatch"])
                break
    except (OSError, ValueError, TypeError):
        result.update(status="invalid", errors=["contract_invalid"])
    return result


def require_contract(root=ROOT):
    result = inspect_contract(root)
    if result["status"] != "valid":
        raise ValueError("Bundled FMT integrity failed; reinstall the same release before generating slides")
    return result


def geometry(element):
    if element is None:
        return None
    return (element.tag, tuple(sorted(element.attrib.items())), tuple(geometry(c) for c in element))


def slide_objects(content):
    root = ET.fromstring(content)
    objects = {}
    for kind in ("sp", "pic", "graphicFrame", "grpSp"):
        for shape in root.findall(".//p:" + kind, NS):
            identity = shape.find(".//p:cNvPr", NS)
            if identity is None:
                continue
            name = identity.get("name")
            if name in objects:
                raise ValueError("Duplicate native shape name")
            transform = shape.find("p:spPr/a:xfrm", NS)
            if transform is None:
                transform = shape.find("p:xfrm", NS)
            if transform is None:
                transform = shape.find("p:grpSpPr/a:xfrm", NS)
            objects[name] = geometry(transform)
    return objects


def check_layout(pptx, root=ROOT):
    require_contract(root)
    errors = []
    fields = json.loads((Path(root) / "assets/fields.json").read_text(encoding="utf-8"))["fields"]
    with zipfile.ZipFile(Path(root) / "assets/template.pptx") as source, zipfile.ZipFile(pptx) as output:
        slides = sorted(n for n in source.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        actual = {n for n in output.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)}
        if actual != set(slides):
            return ["Output slide parts differ from the bundled FMT"]
        for part in slides:
            expected = slide_objects(source.read(part))
            observed = slide_objects(output.read(part))
            if any(name not in observed or observed[name] != bounds for name, bounds in expected.items()):
                errors.append(f"{part}: native shape position/size differs from the bundled FMT")
            allowed_badges = {f['key'] + '_PR': f['pr_badge_bbox'] for f in fields
                              if f.get('part') == part and f.get('kind') == 'image' and 'pr_badge_bbox' in f}
            extra = set(observed) - set(expected)
            for name in extra:
                box = allowed_badges.get(name)
                if box is None:
                    errors.append(f"{part}: unexpected native shape")
                    continue
                xf = ET.Element('{' + NS['a'] + '}xfrm')
                ET.SubElement(xf, '{' + NS['a'] + '}off', {'x':str(round(box['left']*9525)), 'y':str(round(box['top']*9525))})
                ET.SubElement(xf, '{' + NS['a'] + '}ext', {'cx':str(round(box['width']*9525)), 'cy':str(round(box['height']*9525))})
                shape = next((s for s in ET.fromstring(output.read(part)).findall('.//p:sp', NS)
                              if (i := s.find('.//p:cNvPr', NS)) is not None and i.get('name') == name), None)
                text = ''.join(t.text or '' for t in shape.findall('.//a:t', NS)) if shape is not None else ''
                if observed[name] != geometry(xf) or text != 'PR':
                    errors.append(f"{part}: unexpected PR badge geometry or text")
    return errors
