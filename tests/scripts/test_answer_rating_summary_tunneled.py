"""answer_rating_summary_tunneled.sh のシークレット露出・cleanup 契約テスト。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "answer_rating_summary_tunneled.sh"


def test_secret_is_piped_to_python_and_ssm_children_are_cleaned_up() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "printf '%s' \"$RAW\" | python3 -c" in source
    assert "raw, rds, local_port = sys.stdin.read(), sys.argv[1], sys.argv[2]" in source
    assert 'pkill -TERM -P "$SSM_PID"' in source
    assert 'kill "$SSM_PID"' in source
