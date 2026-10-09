"""手元の PowerPoint を検査する CLI。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from teamagent.media.deck_qa import inspect_pptx


def main() -> int:
    parser = argparse.ArgumentParser(description="PowerPoint の機械検査")
    parser.add_argument("pptx", type=Path)
    parser.add_argument("--expected-slides", type=int)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="縦横比の差を error とする")
    args = parser.parse_args()
    try:
        report = inspect_pptx(args.pptx, expected_slides=args.expected_slides, strict=args.strict)
    except Exception as exc:
        result = {"qa_error": str(exc)}
        print(json.dumps(result, ensure_ascii=False) if args.json else f"検査失敗: {exc}")
        return 1
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"{report.slide_count} 枚 / error {report.error_count} / warn {report.warn_count}")
        for finding in report.findings:
            print(
                f"{finding.severity}: {finding.slide}枚目 {finding.shape}: {finding.kind} {finding.details}"
            )
        print("フォント: " + ", ".join(report.fonts))
    return int(report.error_count > 0)


if __name__ == "__main__":
    raise SystemExit(main())
