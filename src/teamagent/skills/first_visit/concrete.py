"""Build a concrete proposal with the pinned kit, without production delivery."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, Field

from teamagent.skills.first_visit import contract
from teamagent.skills.first_visit.kit import ARCHIVE, archive_kit_problems

logger = structlog.get_logger(__name__)


class CliResult(BaseModel):
    stage: Literal["research", "pptx", "preflight"]
    returncode: int | None
    elapsed_seconds: float
    stdout_summary: str = ""
    stderr_summary: str = ""


class Finding(BaseModel):
    severity: Literal["致命的", "重大", "情報"]
    page: int
    kind: str
    message: str


class PreflightResult(BaseModel):
    fatal_count: int
    major_count: int
    information_count: int
    findings: list[Finding]


class ConcreteBuildResult(BaseModel):
    status: Literal["failed", "incomplete", "build_verified"] = "failed"
    failed_stage: (
        Literal["inputs", "kit", "dependencies", "research", "pptx", "preflight"] | None
    ) = None
    problems: list[str] = Field(default_factory=list)
    pptx_path: Path | None = None
    pptx_sha256: str | None = None
    slide_count: int | None = None
    preflight: PreflightResult | None = None
    cli: list[CliResult] = Field(default_factory=list)


# The pinned Python tools only need local I/O; keep writes inside the working case.
_PYTHON_CLI = r"""
import os, runpy, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
script = sys.argv[2]
sys.argv = sys.argv[2:]
sys.path.insert(0, str(Path(script).parent))
def audit(event, args):
    if event.startswith(('socket.', 'subprocess.', 'os.exec', 'os.spawn')) or event == 'os.system':
        raise PermissionError('network and child processes disabled')
    paths = []
    if event == 'open':
        path, mode, flags = args
        writing = (mode and any(c in mode for c in 'wax+'))
        if writing or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            paths = [path]
    elif event in {'os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.utime'}:
        paths = [args[0]]
    elif event in {'os.rename', 'os.link', 'os.symlink'}:
        paths = args[:2]
    for path in paths:
        if not isinstance(path, int) and not Path(path).resolve().is_relative_to(root):
            raise PermissionError('write outside working case disabled')
sys.addaudithook(audit)
runpy.run_path(script, run_name='__main__')
"""

_NODE_CLI = r"""
const deny = () => { throw new Error('network disabled'); };
require('net').Socket.prototype.connect = deny;
require('tls').connect = deny;
require('dgram').createSocket = deny;
require('dns').lookup = deny;
require(process.argv[1]);
"""


def _run(
    stage: Literal["research", "pptx", "preflight"],
    command: list[str],
    *,
    work: Path,
    request_id: str,
    timeout: float,
) -> CliResult:
    started = time.monotonic()
    code = None
    stdout, stderr = "", ""
    try:
        process = subprocess.run(
            command,
            cwd=work,
            env={
                "PATH": os.defpath,
                "HOME": str(work),
                "TMPDIR": str(work),
                "LANG": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        code, stdout, stderr = process.returncode, process.stdout, process.stderr
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace") + "\nCLI timed out"
    except OSError as exc:
        stderr = str(exc)
    elapsed = time.monotonic() - started
    logger.info(
        "concrete_cli", request_id=request_id, stage=stage, elapsed_seconds=elapsed, returncode=code
    )
    (work / f"{stage}.stdout").write_text(stdout, encoding="utf-8")
    (work / f"{stage}.stderr").write_text(stderr, encoding="utf-8")
    return CliResult(
        stage=stage,
        returncode=code,
        elapsed_seconds=elapsed,
        stdout_summary=stdout[-2000:],
        stderr_summary=stderr[-2000:],
    )


def build_concrete(
    root: Path,
    *,
    kit_path: Path,
    request_id: str,
    kit_archive: Path = ARCHIVE,
    kit_python: Path | None = None,
    timeout: float = 120,
) -> ConcreteBuildResult:
    """Consume a sealed case once; generated files stay in a new case-local directory.

    ``build_verified`` means machine preflight passed; visual QA/delivery is not done.
    Complete CLI output is kept beside the working inputs; only summaries are returned.
    """
    result = ConcreteBuildResult()
    result.failed_stage = "kit"
    if problems := archive_kit_problems(kit_path, kit_archive):
        result.problems = problems
        return result
    result.failed_stage = "inputs"
    if not math.isfinite(timeout) or timeout <= 0:
        result.problems = ["timeout は正の秒数が必要です"]
        return result
    if problems := contract.verify_manifest(
        root, kit_path=kit_path, kit_python=kit_python, part="concrete", kit_archive=kit_archive
    ):
        result.problems = problems
        return result
    kit_path, root = kit_path.resolve(), root.resolve()
    deck = kit_path / "tiktok-deck"
    modules = deck / "node_modules"
    result.failed_stage = "dependencies"
    try:
        package = json.loads((modules / "pptxgenjs/package.json").read_text(encoding="utf-8"))
        if package["version"] != "3.12.0":
            raise ValueError("pptxgenjs 3.12.0 が必要です")
    except (OSError, ValueError, KeyError) as exc:
        result.problems = [f"node_modules: npm ci 済みのキットが必要です: {exc}"]
        return result
    node = shutil.which("node")
    if node is None:
        result.problems = ["node 実行ファイルがありません"]
        return result
    result.failed_stage = "inputs"
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        # shortcut: outputs change manifest membership; retry on a fresh sealed case copy.
        work = Path(tempfile.mkdtemp(prefix="concrete-", dir=root))
        case = work / "case"
        for relative in manifest["files"]:
            source = contract._path(root, relative)
            target = contract._path(case, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        # Detect changes between verification and copying, before executing any CLI.
        if contract._manifest(case) != manifest:
            result.problems = ["manifest: 検査後に入力ファイルが変更されました"]
            return result
        cfg = json.loads((case / "case.json").read_text(encoding="utf-8"))
        python = str((kit_python or Path(sys.executable)).absolute())
        research = [
            python,
            "-I",
            "-B",
            "-c",
            _PYTHON_CLI,
            str(case),
            str(deck / "tools/build_research_json.py"),
            "--case",
            str(case),
            "--run",
            str(case),
            "--part",
            "concrete",
            "--no-videos",
            "--plain-name",
            "--out",
            "research_concrete.json",
        ]
        if cfg["research"]["review_collect"]:
            research += ["--reviews", str(case / "reviews")]
        pptx = case / "output/concrete.pptx"
        report = case / "preflight.json"
        commands: list[tuple[Literal["research", "pptx", "preflight"], list[str]]] = [
            ("research", research),
            (
                "pptx",
                [
                    node,
                    "--permission",
                    f"--allow-fs-read={kit_path}",
                    f"--allow-fs-read={modules.resolve()}",
                    f"--allow-fs-read={case}",
                    f"--allow-fs-write={case}",
                    "-e",
                    _NODE_CLI,
                    str(deck / "src/build_research.js"),
                    "--case",
                    str(case),
                    "--data",
                    "research_concrete.json",
                    "--out",
                    str(pptx),
                    "--client",
                ],
            ),
            (
                "preflight",
                [
                    python,
                    "-I",
                    "-B",
                    "-c",
                    _PYTHON_CLI,
                    str(case),
                    str(deck / "tools/preflight.py"),
                    str(pptx),
                    "--json",
                    str(report),
                ],
            ),
        ]
        for stage, command in commands:
            result.failed_stage = stage
            cli = _run(stage, command, work=case, request_id=request_id, timeout=timeout)
            result.cli.append(cli)
            if stage == "pptx" and cli.returncode == 0:
                result.pptx_path = pptx
                result.pptx_sha256 = hashlib.sha256(pptx.read_bytes()).hexdigest()
                with zipfile.ZipFile(pptx) as package_zip:
                    result.slide_count = sum(
                        name.startswith("ppt/slides/slide") and name.endswith(".xml")
                        for name in package_zip.namelist()
                    )
            if stage == "preflight" and report.is_file():
                data = json.loads(report.read_text(encoding="utf-8"))
                if data["pptx_sha256"] != result.pptx_sha256 or data["pages"] != result.slide_count:
                    result.problems = ["preflight: PPTX の SHA-256/枚数が一致しません"]
                    return result
                result.preflight = PreflightResult(
                    fatal_count=data["counts"].get("致命的", 0),
                    major_count=data["counts"].get("重大", 0),
                    information_count=data["counts"].get("情報", 0),
                    findings=[Finding.model_validate(row) for row in data["findings"]],
                )
                if result.preflight.fatal_count or result.preflight.major_count:
                    result.status = "incomplete"
                    result.problems = ["preflight: 致命的/重大な指摘が残っています"]
                    return result
            if cli.returncode != 0:
                result.problems = [f"{stage}: exit={cli.returncode}: {cli.stderr_summary}"]
                return result
        if result.preflight is None:
            result.problems = ["preflight: JSON レポートがありません"]
            return result
        result.status, result.failed_stage = "build_verified", None
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        result.problems = [f"{result.failed_stage}: {exc}"]
    return result
