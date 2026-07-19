"""Isolated, network-denied renderer entrypoint for the media worker."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

from teamagent.media.operations import (
    _EXTERNAL_HTML_REF,
    _PLACEHOLDER,
    MediaOperationError,
    _iter_proposal_image_slots,
    _iter_text_frames,
    _replace_placeholders,
)


def _exact(value: Any, keys: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise MediaOperationError("MEDIA_RENDER_MANIFEST_INVALID", f"{name} is invalid")
    return value


def _path(root: Path, value: Any, *, output: bool = False) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise MediaOperationError("MEDIA_RENDER_PATH_INVALID", "renderer path is invalid")
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise MediaOperationError("MEDIA_RENDER_PATH_INVALID", "renderer path escaped workdir")
    if not output and (path.is_symlink() or not path.is_file()):
        raise MediaOperationError("MEDIA_RENDER_PATH_INVALID", "renderer input is invalid")
    if output and path.exists():
        raise MediaOperationError("MEDIA_RENDER_PATH_INVALID", "renderer output already exists")
    return path


def _slides(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    spec = _exact(
        manifest,
        {"kind", "html", "output", "selector", "width", "height", "scale"},
        "slides renderer manifest",
    )
    html_path = _path(root, spec["html"])
    destination = _path(root, spec["output"], output=True)
    html = html_path.read_text(encoding="utf-8")
    if _EXTERNAL_HTML_REF.search(html):
        raise MediaOperationError(
            "MEDIA_HTML_NETWORK_REFERENCE",
            "slides HTML references network",
        )
    from playwright.sync_api import sync_playwright
    from pptx import Presentation
    from pptx.util import Emu, Inches

    chromium = os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium-browser")
    images: list[bytes] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=chromium,
            headless=True,
            args=[
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-quic",
                "--disable-setuid-sandbox",
                "--host-resolver-rules=MAP * ~NOTFOUND",
                "--no-proxy-server",
            ],
            chromium_sandbox=True,
        )
        try:
            page = browser.new_page(
                viewport={"width": int(spec["width"]), "height": int(spec["height"])},
                device_scale_factor=int(spec["scale"]),
            )
            page.route("**/*", lambda route: route.abort())
            page.set_content(html, wait_until="domcontentloaded")
            slides = page.locator(str(spec["selector"]))
            count = slides.count()
            if count < 1 or count > 20:
                raise MediaOperationError(
                    "MEDIA_SLIDE_COUNT_INVALID",
                    "slide count is out of range",
                )
            for index in range(count):
                images.append(slides.nth(index).screenshot(type="png"))
        finally:
            browser.close()

    presentation = Presentation()
    presentation.slide_width = Inches(13.333)
    presentation.slide_height = Inches(7.5)
    blank = presentation.slide_layouts[6]
    for image in images:
        slide = presentation.slides.add_slide(blank)
        slide.shapes.add_picture(
            io.BytesIO(image),
            Emu(0),
            Emu(0),
            width=presentation.slide_width,
            height=presentation.slide_height,
        )
    presentation.save(str(destination))
    return {"slides": len(images), "network_requests_allowed": 0}


def _proposal(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    spec = _exact(
        manifest,
        {"kind", "template", "composer", "evidence", "output", "fail_if_missing"},
        "proposal renderer manifest",
    )
    template = _path(root, spec["template"])
    composer = _path(root, spec["composer"])
    destination = _path(root, spec["output"], output=True)
    evidence = spec["evidence"]
    if not isinstance(evidence, list) or len(evidence) > 20:
        raise MediaOperationError("MEDIA_RENDER_MANIFEST_INVALID", "evidence is invalid")
    try:
        raw = json.loads(composer.read_text(encoding="utf-8"))
        placeholders = {int(key): str(value) for key, value in raw["placeholders"].items()}
        skipped = {int(item["id"]) for item in raw.get("skipped_placeholders", [])}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaOperationError("MEDIA_COMPOSER_INVALID", "composer JSON is invalid") from exc
    valid_ids = set(range(1, 104)) - set(range(48, 56))
    if set(placeholders) - valid_ids or skipped - valid_ids:
        raise MediaOperationError("MEDIA_COMPOSER_IDS_INVALID", "composer IDs are invalid")
    for placeholder_id in valid_ids:
        placeholders.setdefault(placeholder_id, "要確認（データ未検出）")

    from pptx import Presentation
    from pptx.util import Emu

    presentation = Presentation(str(template))
    for text_frame in _iter_text_frames(presentation):
        _replace_placeholders(text_frame, placeholders)
    remaining: list[int] = []
    for text_frame in _iter_text_frames(presentation):
        combined = "".join(paragraph.text for paragraph in text_frame.paragraphs)
        remaining.extend(int(match.group(1)) for match in _PLACEHOLDER.finditer(combined))
    if remaining and spec["fail_if_missing"] is True:
        raise MediaOperationError("MEDIA_PPTX_UNFILLED", "unfilled placeholders remain")

    slots = _iter_proposal_image_slots(presentation)
    injected = 0
    ordered: list[tuple[int, int, Path]] = []
    for value in evidence:
        entry = _exact(value, {"placeholder_id", "rank", "path"}, "evidence entry")
        ordered.append(
            (
                int(entry["placeholder_id"]),
                int(entry["rank"]),
                _path(root, entry["path"]),
            )
        )
    for _placeholder_id, _rank, image in sorted(ordered):
        try:
            slide, shape = next(slots)
        except StopIteration:
            break
        try:
            picture = slide.shapes.add_picture(
                io.BytesIO(image.read_bytes()),
                Emu(int(shape.left)),
                Emu(int(shape.top)),
            )
        except Exception:
            continue
        if picture.width and picture.height:
            scale = int(shape.height) / picture.height
            picture.height = Emu(int(shape.height))
            picture.width = Emu(int(picture.width * scale))
        injected += 1
    presentation.save(str(destination))
    return {
        "filled": len(placeholders),
        "skipped": len(skipped),
        "evidence_images": injected,
        "remaining_placeholders": len(remaining),
    }


def _pdf(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    spec = _exact(manifest, {"kind", "html", "output"}, "PDF renderer manifest")
    html_path = _path(root, spec["html"])
    destination = _path(root, spec["output"], output=True)
    html = html_path.read_text(encoding="utf-8")
    if _EXTERNAL_HTML_REF.search(html):
        raise MediaOperationError(
            "MEDIA_HTML_NETWORK_REFERENCE",
            "PDF HTML references network",
        )
    from weasyprint import HTML

    def block_url_fetcher(_url: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise MediaOperationError(
            "MEDIA_HTML_NETWORK_REFERENCE",
            "PDF rendering network access is blocked",
        )

    HTML(string=html, url_fetcher=block_url_fetcher).write_pdf(str(destination))
    if not destination.is_file() or destination.stat().st_size < 5:
        raise MediaOperationError("MEDIA_PDF_EMPTY", "weasyprint produced an empty PDF")
    return {"network_requests_allowed": 0}


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise MediaOperationError(
                "MEDIA_RENDER_MANIFEST_INVALID",
                "renderer manifest argument is required",
            )
        root = Path.cwd().resolve()
        manifest_path = _path(root, sys.argv[1])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise MediaOperationError(
                "MEDIA_RENDER_MANIFEST_INVALID",
                "renderer manifest is invalid",
            )
        kind = manifest.get("kind")
        if kind == "slides":
            metadata = _slides(root, manifest)
        elif kind == "proposal_pptx":
            metadata = _proposal(root, manifest)
        elif kind == "pdf":
            metadata = _pdf(root, manifest)
        else:
            raise MediaOperationError(
                "MEDIA_RENDER_MANIFEST_INVALID",
                "renderer kind is invalid",
            )
        print(json.dumps({"ok": True, "metadata": metadata}, sort_keys=True))
        return 0
    except MediaOperationError as exc:
        print(json.dumps({"ok": False, "code": exc.code}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"ok": False, "code": "MEDIA_RENDER_FAILED"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
