"""ingest CLI の clean/warning/error 終了コード契約。"""

from __future__ import annotations

import pytest

from scripts.ingest_sources import _result_exit_code
from teamagent.ingest.pipeline import IngestResult, IngestStats


@pytest.mark.parametrize(
    ("errors", "warnings", "expected"),
    [
        ([], {}, 0),
        ([], {"office_download_failed": 1}, 2),
        (["source failed"], {}, 1),
        (["source failed"], {"corrupt_zip": 1}, 1),
    ],
)
def test_result_exit_code_distinguishes_non_clean_outcomes(
    errors: list[str],
    warnings: dict[str, int],
    expected: int,
) -> None:
    result = IngestResult(
        by_kind={
            "gdrive": IngestStats(
                source_kind="gdrive",
                errors=errors,
                warning_reasons=warnings,
            )
        }
    )

    assert _result_exit_code(result) == expected
