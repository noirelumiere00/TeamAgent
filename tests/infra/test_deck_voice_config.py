import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_voice_is_default_off_and_has_separate_empty_allowlist() -> None:
    variables = (ROOT / "infra/terraform/variables_fargate.tf").read_text()
    task = (ROOT / "infra/terraform/fargate.tf").read_text()
    assert 'variable "enable_deck_voice_research"' in variables
    assert 'variable "deck_voice_allowed_emails"' in variables
    assert (
        '{ name = "USE_DECK_VOICE_RESEARCH", value = var.enable_deck_voice_research ? "1" : "0" }'
        in task
    )
    assert '{ name = "DECK_VOICE_ALLOWED_EMAILS", value = var.deck_voice_allowed_emails }' in task
    assert 'USE_TIKTOK_COMMENT_TOOLS", value = "false"' in task
    assert re.search(r'variable "enable_deck_voice_research" \{[^}]*default\s*=\s*false', variables)
    assert re.search(r'variable "deck_voice_allowed_emails" \{[^}]*default\s*=\s*""', variables)
