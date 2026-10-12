"""手元の隔離 Docker イメージで PPTX の描画 QA を実行する。build は別途行う。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def docker_command(pptx: Path, output: Path, profile: str | None, image: str) -> list[str]:
    args = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "4g",
        "--pids-limit",
        "256",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=4g,mode=1777",
        "--mount",
        f"type=bind,source={pptx},target=/input.pptx,readonly",
        "--mount",
        f"type=bind,source={output},target=/tmp/output",
        image,
        "/input.pptx",
        "--out",
        "/tmp/output",
    ]
    if profile:
        args += ["--profile", profile]
    return args


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pptx", type=Path)
    parser.add_argument("--profile", choices=["proposal_builder_v1"])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", type=Path, default=Path("render-qa-output"))
    parser.add_argument("--image", default="teamagent-render-qa:local")
    args = parser.parse_args()
    pptx = args.pptx.resolve(strict=True)
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        # 既存の出力ディレクトリの権限は変えず、専用の一時 mount だけを書込み可にする。
        with tempfile.TemporaryDirectory(prefix=".render-qa-", dir=output) as temp:
            staging = Path(temp)
            staging.chmod(0o777)
            proc = subprocess.run(
                docker_command(pptx, staging, args.profile, args.image),
                capture_output=True,
                text=True,
                timeout=510,
                check=True,
            )
            result = json.loads(proc.stdout)
            for artifact in staging.iterdir():
                if (
                    not artifact.is_symlink()
                    and artifact.is_file()
                    and (
                        artifact.name == "contact_sheet.png"
                        or re.fullmatch(r"slide-\d{3,5}\.png", artifact.name)
                    )
                ):
                    shutil.copyfile(artifact, output / artifact.name)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result = {
            "status": "unavailable",
            "findings": [],
            "counts": {},
            "sanitized": {},
            "lo_version": "",
            "font_sha": "",
            "font_substitute": "plain_noto",
            "elapsed_ms": 0,
            "reason": type(exc).__name__,
        }
    (output / "render_qa.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        json.dumps(result, ensure_ascii=False)
        if args.json
        else f"{result['status']}: error={result['counts'].get('error', 0)}, warn={result['counts'].get('warn', 0)} → {output}"
    )
    return 1 if result["counts"].get("error", 0) else 2 if result["status"] == "unavailable" else 0


if __name__ == "__main__":
    raise SystemExit(main())
