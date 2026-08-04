"""統合FMT(143MB)がCLI既定手順で必ず落ちた ChecksumSHA256 検査の回帰。

マルチパートアップロードのオブジェクトは合成チェックサム
``<base64>-<パート数>`` を返し、全体SHA-256とは原理的に一致しない。
本文の完全性は _stream_and_publish の全バイトsha256照合が担保する。
"""

import base64
import hashlib

import pytest

from teamagent.adapters import proposal_assets


class _Spec:
    def __init__(self, body: bytes) -> None:
        self.sha256 = hashlib.sha256(body).hexdigest()


def _whole_object_checksum(body: bytes) -> str:
    return base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")


def test_multipart_composite_checksum_is_accepted() -> None:
    body = b"x" * 32
    assert proposal_assets._checksum_header_matches("abcDEF123+/=-12", _Spec(body)) is True


def test_single_part_checksum_must_match_exactly() -> None:
    body = b"x" * 32
    spec = _Spec(body)
    assert proposal_assets._checksum_header_matches(_whole_object_checksum(body), spec) is True


def test_single_part_checksum_mismatch_is_rejected() -> None:
    spec = _Spec(b"x" * 32)
    assert (
        proposal_assets._checksum_header_matches(_whole_object_checksum(b"different"), spec)
        is False
    )


@pytest.mark.parametrize("returned", ["", "not-base64!!", "abcDEF-", "-3"])
def test_malformed_checksum_headers_are_rejected(returned: str) -> None:
    assert proposal_assets._checksum_header_matches(returned, _Spec(b"x")) is False
