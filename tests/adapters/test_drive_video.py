"""adapters/drive_video.py の単体テスト (Drive API をモック)。

Drive URL → ファイル ID 抽出と、bytes 取得の配線を検証する。実 Drive 不要。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.drive_video import (
    DriveVideoError,
    download_drive_video,
    extract_drive_file_id,
    is_drive_url,
)

_FID = "1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvW"  # 25文字以上の Drive ID 形状


@pytest.mark.parametrize(
    "url",
    [
        f"https://drive.google.com/file/d/{_FID}/view",
        f"https://drive.google.com/file/d/{_FID}/view?usp=sharing",
        f"https://drive.google.com/open?id={_FID}",
        f"https://drive.google.com/uc?id={_FID}&export=download",
        f"https://drive.google.com/file/d/{_FID}/preview",
    ],
)
def test_extract_file_id_variations(url: str) -> None:
    assert extract_drive_file_id(url) == _FID


def test_extract_file_id_non_drive_returns_none() -> None:
    assert extract_drive_file_id("https://example.com/video.mp4") is None
    assert extract_drive_file_id("ただのテキスト") is None


def test_is_drive_url() -> None:
    assert is_drive_url(f"https://drive.google.com/file/d/{_FID}/view")
    assert is_drive_url("https://docs.google.com/document/d/x/edit")
    assert not is_drive_url("https://youtube.com/watch?v=abc")


def test_download_extracts_id_and_returns_bytes() -> None:
    fake = MagicMock()
    fake.download_file_bytes.return_value = b"\x00\x01videobytes"
    data, mime = download_drive_video(f"https://drive.google.com/file/d/{_FID}/view", client=fake)
    assert data == b"\x00\x01videobytes"
    assert mime == "video/mp4"
    # download_file_bytes が抽出した ID で呼ばれた
    args = fake.download_file_bytes.call_args.args
    assert args[0] == _FID


def test_download_accepts_raw_file_id() -> None:
    """URL でなく素のファイル ID を渡しても動く。"""
    fake = MagicMock()
    fake.download_file_bytes.return_value = b"data"
    data, _ = download_drive_video(_FID, client=fake)
    assert data == b"data"
    assert fake.download_file_bytes.call_args.args[0] == _FID


def test_download_bad_url_raises() -> None:
    fake = MagicMock()
    with pytest.raises(DriveVideoError, match="DRIVE_BAD_URL"):
        download_drive_video("https://example.com/not-drive", client=fake)


def test_download_too_large_raises() -> None:
    fake = MagicMock()
    fake.download_file_bytes.return_value = b"x" * (21 * 1024 * 1024)  # 21MB
    with pytest.raises(DriveVideoError, match="DRIVE_FILE_TOO_LARGE"):
        download_drive_video(f"https://drive.google.com/file/d/{_FID}/view", client=fake, max_mb=20)


def test_download_access_error_raises_friendly() -> None:
    fake = MagicMock()
    fake.download_file_bytes.side_effect = RuntimeError("403 Forbidden")
    with pytest.raises(DriveVideoError, match="DRIVE_DOWNLOAD_FAILED"):
        download_drive_video(f"https://drive.google.com/file/d/{_FID}/view", client=fake)


def test_mime_from_extension() -> None:
    fake = MagicMock()
    fake.download_file_bytes.return_value = b"data"
    # .mov 拡張子付き URL → quicktime
    _, mime = download_drive_video(f"https://drive.google.com/file/d/{_FID}/view.mov", client=fake)
    assert mime == "video/quicktime"
