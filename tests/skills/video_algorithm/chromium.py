"""実描画のテストで使う chromium（playwright）の起動。無ければ skip する。

手元（macOS）は playwright が入れた headless shell を使う。CI には chromium が無いことがあるので、
起動できなければ pytest.skip にする（仕様 v3 §5 T22: CI に無ければ merge 前に手元で必須）。
"""

from __future__ import annotations

import os
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

MAC_HEADLESS_SHELL = os.path.expanduser(
    "~/Library/Caches/ms-playwright/chromium_headless_shell-1223/"
    "chrome-headless-shell-mac-arm64/chrome-headless-shell"
)


def chromium_path() -> str | None:
    """CHROMIUM_PATH か、手元の headless shell。どちらも無ければ None（playwright の既定を試す）。"""
    for cand in (os.environ.get("CHROMIUM_PATH"), MAC_HEADLESS_SHELL):
        if cand and os.path.exists(cand):
            return cand
    return None


@contextmanager
def launched_browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=chromium_path())
        except Exception as e:  # CI には chromium が無い
            pytest.skip(f"chromium を起動できない: {type(e).__name__}")
        try:
            yield browser
        finally:
            browser.close()


def tiny_png(w: int = 8, h: int = 8) -> bytes:
    """python-pptx が読める最小の有効 PNG（単色）。"""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xcc\xcc\xcc" * w for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# ── 撮影の順（T21）を確かめる playwright の代わり ─────────────────────────────


class FakePage:
    """playwright の page の代わり（呼ばれた順を記録する）。"""

    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.calls = calls

    def set_content(self, html: str, **_kw: object) -> None:
        self.calls.append(("set_content", len(html)))

    def add_style_tag(self, *, content: str) -> None:
        self.calls.append(("add_style_tag", content))

    def route(self, *_a: object) -> None:
        self.calls.append(("route", None))

    def evaluate(self, *_a: object) -> None:
        self.calls.append(("evaluate", None))

    def wait_for_function(self, *_a: object, **_kw: object) -> None:
        self.calls.append(("wait_for_function", None))

    def locator(self, _selector: str) -> FakePage:
        return self

    def count(self) -> int:
        return 2

    def nth(self, _i: int) -> FakePage:
        return self

    def screenshot(self, **_kw: object) -> bytes:
        self.calls.append(("screenshot", None))
        return tiny_png()


class FakePlaywright:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.calls = calls
        self.chromium = self

    def __call__(self) -> FakePlaywright:
        return self

    def __enter__(self) -> FakePlaywright:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def launch(self, **_kw: object) -> FakePlaywright:
        return self

    def new_page(self, **_kw: object) -> FakePage:
        return FakePage(self.calls)

    def close(self) -> None:
        self.calls.append(("close", None))


def install_fake_playwright(monkeypatch: pytest.MonkeyPatch, fake: FakePlaywright) -> None:
    """``playwright.sync_api.sync_playwright`` を偽物に差し替える（playwright が入っていない CI でも動く）。

    撮影の関数は呼ばれた時点で ``from playwright.sync_api import sync_playwright`` するので、
    ``sys.modules`` に偽のモジュールを置けば本物の有無に関係なくそれが使われる。
    """
    api = types.ModuleType("playwright.sync_api")
    api.sync_playwright = fake  # type: ignore[attr-defined]
    pkg = types.ModuleType("playwright")
    pkg.sync_api = api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)


def assert_hidden_before_shots(calls: list[tuple[str, object]]) -> None:
    from teamagent.skills.video_algorithm.slides import NOEXPORT_CSS

    names = [c for c, _v in calls]
    assert ("add_style_tag", NOEXPORT_CSS) in calls
    styled = names.index("add_style_tag")
    assert names.index("set_content") < styled < names.index("screenshot")


def edit_tip_brightness(png: bytes, scale: int = 2) -> float:
    """スライドの左上（編集ヒントが焼き込まれていた場所）の明るさの平均（0〜255）。"""
    import io as _io

    from PIL import Image, ImageStat

    img = Image.open(_io.BytesIO(png)).convert("L")
    region = img.crop((20 * scale, 2 * scale, 300 * scale, 14 * scale))
    return float(ImageStat.Stat(region).mean[0])


__all__ = [
    "MAC_HEADLESS_SHELL",
    "FakePage",
    "FakePlaywright",
    "assert_hidden_before_shots",
    "chromium_path",
    "edit_tip_brightness",
    "launched_browser",
    "tiny_png",
]
