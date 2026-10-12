"""手元/CI 専用 JSON 子プロセス。全工程を期限・rlimit 付きで隔離する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

CPU_SECONDS = 300
MEMORY_BYTES = 3 << 30
FILE_BYTES = 1 << 30
CONVERT_SECONDS = 180
TOTAL_SECONDS = 480
PDF_FILTER = (
    'pdf:impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"true"},'
    '"ExportNotesPages":{"type":"boolean","value":"false"}}'
)


def limits() -> None:
    for kind, value in (
        (resource.RLIMIT_CPU, CPU_SECONDS),
        (resource.RLIMIT_AS, MEMORY_BYTES),
        (resource.RLIMIT_FSIZE, FILE_BYTES),
        (resource.RLIMIT_NOFILE, 1024),
        (resource.RLIMIT_NPROC, 256),
        (resource.RLIMIT_CORE, 0),
    ):
        resource.setrlimit(kind, (value, value))


def run(command: list[str], deadline: float, *, timeout: float = CONVERT_SECONDS) -> None:
    remaining = min(timeout, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("deadline")
    with subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        preexec_fn=limits,
    ) as proc:
        try:
            proc.wait(timeout=remaining)
            if proc.returncode:
                raise RuntimeError("child_failed")
        finally:
            # 成功時も子孫の常駐プロセスを残さない。
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()


def soffice_command(job: Path) -> list[str]:
    return [
        "soffice",
        "--headless",
        "--invisible",
        "--nologo",
        "--norestore",
        "--nolockcheck",
        "--nodefault",
        f"-env:UserInstallation={(job / 'lo').as_uri()}",
        "--infilter=Impress MS PowerPoint 2007 XML",
        "--convert-to",
        PDF_FILTER,
        "--outdir",
        str(job),
        str(job / "probe.pptx"),
        str(job / "original.pptx"),
    ]


def read_json(path: Path, limit: int = 1_000_000) -> dict[str, Any]:
    if path.stat().st_size > limit:
        raise ValueError("json_limit")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("json_object")
    return value


def _stage(mode: str, job: Path, source: Path, profile: str | None) -> None:
    from teamagent.media.render_qa import (
        ShapeBox,
        evaluate,
        extract_glyphs,
        load_profile,
        prepare_probe,
        sanitize_pptx,
    )

    if mode == "prepare":
        sanitized = sanitize_pptx(source, job / "original.pptx")
        shapes, width, height = prepare_probe(job / "original.pptx", job / "probe.pptx")
        (job / "geometry.json").write_text(
            json.dumps(
                {
                    "shapes": [asdict(s) for s in shapes],
                    "width": width,
                    "height": height,
                    "sanitized": sanitized,
                }
            )
        )
    elif mode == "extract":
        geometry = read_json(job / "geometry.json", 16_000_000)
        shapes = [
            ShapeBox(**{**s, "color": tuple(s["color"]) if s["color"] else None})
            for s in geometry["shapes"]
        ]
        glyphs, pages = extract_glyphs(job / "probe.pdf")
        # 欠落ページや誤ったページ寸法を合格にしない。
        if len(pages) != max((s.slide for s in shapes), default=0) or any(
            abs(w - geometry["width"]) > 1 or abs(h - geometry["height"]) > 1 for w, h in pages
        ):
            raise ValueError("pdf_page_mismatch")
        thresholds, baseline = load_profile(profile)
        substitute = (
            "palt" if Path("/usr/share/fonts/teamagent/palt.available").is_file() else "plain_noto"
        )
        result = evaluate(
            shapes,
            glyphs,
            geometry["width"],
            geometry["height"],
            thresholds=thresholds,
            baseline=baseline,
            font_substitute=substitute,
        )
        result["sanitized"] = geometry["sanitized"]
        encoded = json.dumps(result)
        if len(encoded.encode()) > 1_000_000:
            raise ValueError("result_limit")
        (job / "result.json").write_text(encoded)
    elif mode == "thumbnails":
        make_thumbnails(job)
    elif mode == "version":
        # stdout は結果 JSON 専用なので版もファイルへ。
        with (job / "lo-version.txt").open("w") as stream:
            subprocess.run(
                ["soffice", "--version"],
                check=True,
                stdout=stream,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
    else:
        raise ValueError("stage")


def make_thumbnails(job: Path) -> None:
    from PIL import Image, ImageDraw

    result = read_json(job / "result.json")
    pages = sorted(job.glob("thumb-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    if not pages:
        raise ValueError("no_thumbnails")
    flagged = {f["slide"] for f in result["findings"]}
    with Image.open(pages[0]) as first:
        tile_w, tile_h = first.size
    cols = 4
    sheet = Image.new(
        "RGB", (cols * (tile_w + 12), math_ceil(len(pages) / cols) * (tile_h + 32)), "white"
    )
    draw = ImageDraw.Draw(sheet)
    for index, path in enumerate(pages):
        x, y = (index % cols) * (tile_w + 12) + 6, (index // cols) * (tile_h + 32) + 6
        with Image.open(path) as image:
            sheet.paste(image, (x, y))
        draw.text(
            (x, y + tile_h + 3), str(index + 1), fill="red" if index + 1 in flagged else "black"
        )
        if index + 1 in flagged:
            draw.rectangle((x, y, x + tile_w, y + tile_h), outline="red", width=3)
    sheet.save(job / "contact_sheet.png")
    geometry = read_json(job / "geometry.json", 16_000_000)
    for slide in sorted(flagged)[:10]:
        path = job / f"detail-{slide}.png"
        with Image.open(path) as image:
            detail = image.convert("RGB")
            draw = ImageDraw.Draw(detail)
            sx, sy = image.width / geometry["width"], image.height / geometry["height"]
            for finding in result["findings"]:
                if finding["slide"] == slide and "rect" in finding["details"]:
                    x0, y0, x1, y1 = finding["details"]["rect"]
                    draw.rectangle(
                        (
                            min(image.width - 1, max(0, x0 * sx)),
                            min(image.height - 1, max(0, y0 * sy)),
                            min(image.width - 1, max(0, x1 * sx)),
                            min(image.height - 1, max(0, y1 * sy)),
                        ),
                        outline="red",
                        width=4,
                    )
            detail.save(job / f"slide-{slide:03d}.png")


def math_ceil(value: float) -> int:
    return int(-(-value // 1))


def execute(
    source: Path, output: Path, profile: str | None, timeout: float = TOTAL_SECONDS
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + timeout
    result: dict[str, Any] = {
        "status": "unavailable",
        "findings": [],
        "counts": {},
        "sanitized": {},
        "lo_version": "",
        "font_sha": "",
        "font_substitute": "plain_noto",
        "elapsed_ms": 0,
    }
    output.mkdir(parents=True, exist_ok=True)
    try:
        if not shutil.which("soffice") or not shutil.which("pdftoppm"):
            raise FileNotFoundError("renderer_unavailable")
        with tempfile.TemporaryDirectory(prefix="render-qa-") as temp:
            job = Path(temp)

            def stage(mode: str) -> None:
                args = [
                    sys.executable,
                    "-m",
                    "teamagent.media.render_qa_child",
                    "--stage",
                    mode,
                    "--job",
                    str(job),
                    str(source),
                ]
                if profile:
                    args += ["--profile", profile]
                run(args, deadline, timeout=CPU_SECONDS)

            stage("prepare")
            result["sanitized"] = read_json(job / "geometry.json", 16_000_000)["sanitized"]
            template = Path("/opt/render-qa/lo-profile")
            (job / "lo/user").mkdir(parents=True)
            if template.is_dir():
                shutil.copytree(template, job / "lo/user", dirs_exist_ok=True)
            run(soffice_command(job), deadline)
            if not (job / "original.pdf").is_file() or not (job / "probe.pdf").is_file():
                raise RuntimeError("pdf_missing")
            stage("extract")
            result = read_json(job / "result.json")
            stage("version")
            result["lo_version"] = (job / "lo-version.txt").read_text().strip()[:120]
            digest = hashlib.sha256()
            for font in sorted(Path("/usr/share/fonts").rglob("*")):
                if font.is_file():
                    digest.update(str(font.relative_to("/usr/share/fonts")).encode())
                    with font.open("rb") as stream:
                        while chunk := stream.read(1 << 20):
                            digest.update(chunk)
                    if time.monotonic() >= deadline:
                        raise TimeoutError("deadline")
            result["font_sha"] = digest.hexdigest()
            run(
                [
                    "pdftoppm",
                    "-png",
                    "-scale-to",
                    "320",
                    str(job / "original.pdf"),
                    str(job / "thumb"),
                ],
                deadline,
            )
            for slide in sorted({f["slide"] for f in result["findings"]})[:10]:
                prefix = job / f"detail-{slide}"
                run(
                    [
                        "pdftoppm",
                        "-png",
                        "-scale-to",
                        "1280",
                        "-f",
                        str(slide),
                        "-l",
                        str(slide),
                        "-singlefile",
                        str(job / "original.pdf"),
                        str(prefix),
                    ],
                    deadline,
                )
            stage("thumbnails")
            for path in [job / "contact_sheet.png", *sorted(job.glob("slide-*.png"))]:
                if time.monotonic() >= deadline:
                    raise TimeoutError("deadline")
                shutil.copyfile(path, output / path.name)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, TimeoutError) as exc:
        # 例外本文にはデッキの本文が紛れるため型名だけを返す。
        result["status"] = "unavailable"
        result["reason"] = type(exc).__name__
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded.encode()) > 1_000_000:
        result = {**result, "status": "unavailable", "findings": [], "reason": "result_limit"}
        encoded = json.dumps(result, ensure_ascii=False)
    (output / "render_qa.json").write_text(encoded)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pptx", type=Path)
    parser.add_argument("--out", type=Path, default=Path("/tmp/output"))
    parser.add_argument("--profile")
    parser.add_argument("--stage", choices=["prepare", "extract", "thumbnails", "version"])
    parser.add_argument("--job", type=Path)
    args = parser.parse_args()
    if args.stage:
        if args.job is None:
            parser.error("--job required")
        _stage(args.stage, args.job, args.pptx, args.profile)
    else:
        print(json.dumps(execute(args.pptx, args.out, args.profile), ensure_ascii=False))


if __name__ == "__main__":
    main()
