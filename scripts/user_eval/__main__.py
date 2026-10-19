"""記録した会話のオフライン採点と偽物デモ。送受信や認証は行わない。"""
from pathlib import Path

from scripts.user_eval.core import Attachment, Message, Runner, run_suite
from scripts.user_eval.fakes import FakeClock, FakeJudge, FakeLogReader, FakeTransport


def main() -> None:
    import argparse
    import json
    import re

    parser = argparse.ArgumentParser(description=__doc__)
    import sys

    if sys.argv[1:2] == ["score"]:
        from scripts.user_eval.score import overall, score_recorded

        parser.add_argument("command", choices=["score"])
        parser.add_argument("--recorded", type=Path, required=True)
        parser.add_argument("--scenarios", type=Path, required=True)
        parser.add_argument("--out", type=Path, required=True)
        parser.add_argument("--previous", type=Path)
        args = parser.parse_args()
        try:
            results = score_recorded(args.recorded, args.scenarios, args.out, args.previous)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            parser.error(str(exc))
        print(f"全体点: {overall(results):.1f} / 100: {args.out / 'report.md'}")
        return
    parser.add_argument("--fake", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    path = Path(__file__).with_name("scenarios.json")
    # デモは実名の割り当てなしで、差し込み名のまま会話を作る。
    raw = path.read_text()
    keys = set(re.findall(r"\{([a-z][a-z0-9_]*)\}", raw))
    for key in keys:
        raw = raw.replace("{" + key + "}", "DEMO_" + key)
    rows = json.loads(raw)
    replies = []
    for row in rows:
        turns = []
        for _ in row["utterances"]:
            turns.append([[Message("metrics", "📊 動画A 再生1,234 保存56", 1),
                           Message("answer", "実施済み。https://drive.google.com/file/d/demo\n導入の見せ方が効いた。", 2,
                                   attachments=[Attachment("demo.pdf")], request_id="fake-request")]])
        replies.extend(turns)
    runner = Runner("DDEMO", FakeTransport(replies), FakeJudge(), FakeLogReader(), FakeClock())
    run_suite(runner, rows, args.output)
    print(f"偽物の結果（品質評価ではない）: {args.output}")


if __name__ == "__main__":
    main()
