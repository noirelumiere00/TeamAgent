from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.verify_worker_bundle_provenance import (
    ProvenanceError,
    main,
    verify,
)

KEY_ARN = "arn:aws:kms:ap-northeast-1:718959508629:key/12345678-1234-4123-8123-123456789abc"


class _Kms:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def verify(self, **kwargs: Any) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "SignatureValid": True,
            "KeyId": KEY_ARN,
            "SigningAlgorithm": "RSASSA_PSS_SHA_256",
        }


def _files(tmp_path: Path) -> tuple[Path, Path, Path]:
    artifact = tmp_path / "worker.tar.gz"
    artifact.write_bytes(b"exact-clean-origin-worker-archive")
    receipt = {
        "schema": 1,
        "kind": "teamagent.worker-bundle-provenance",
        "source": {
            "origin": "git@github.com:noirelumiere00/TeamAgent.git",
            "branch": "dev",
            "commit": "a" * 40,
            "tree": "b" * 40,
            "clean": True,
        },
        "artifact": {
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "format": "tar.gz",
        },
        "signing": {
            "key_arn": KEY_ARN,
            "algorithm": "RSASSA_PSS_SHA_256",
        },
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    signature = tmp_path / "receipt.sig"
    signature.write_bytes(base64.b64encode(b"signed-receipt"))
    return artifact, receipt_path, signature


def test_worker_archive_requires_exact_clean_origin_receipt_and_signing_key(
    tmp_path: Path,
) -> None:
    artifact, receipt, signature = _files(tmp_path)
    kms = _Kms()

    verify(
        artifact=artifact,
        receipt_path=receipt,
        signature_path=signature,
        expected_key_arn=KEY_ARN,
        kms=kms,
    )

    assert len(kms.calls) == 1
    assert kms.calls[0]["KeyId"] == KEY_ARN
    assert kms.calls[0]["SigningAlgorithm"] == "RSASSA_PSS_SHA_256"
    with pytest.raises(ProvenanceError, match="invalid provenance"):
        verify(
            artifact=artifact,
            receipt_path=receipt,
            signature_path=signature,
            expected_key_arn=KEY_ARN.replace("12345678", "87654321", 1),
            kms=kms,
        )

    claim = json.loads(receipt.read_text(encoding="utf-8"))
    claim["source"]["clean"] = False
    receipt.write_text(json.dumps(claim), encoding="utf-8")
    with pytest.raises(ProvenanceError, match="invalid provenance"):
        verify(
            artifact=artifact,
            receipt_path=receipt,
            signature_path=signature,
            expected_key_arn=KEY_ARN,
            kms=kms,
        )


def test_worker_provenance_cli_redacts_client_exception(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact, receipt, signature = _files(tmp_path)

    class ExplodingKms:
        def verify(self, **_kwargs: Any) -> dict[str, object]:
            raise RuntimeError("sensitive-kms-client-detail")

    assert (
        main(
            [
                "--artifact",
                str(artifact),
                "--receipt",
                str(receipt),
                "--signature",
                str(signature),
                "--key-arn",
                KEY_ARN,
            ],
            kms=ExplodingKms(),
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == '{"code":"worker_provenance_invalid","ok":false}\n'
    assert captured.err == ""
    assert "sensitive-kms-client-detail" not in captured.out
