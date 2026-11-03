"""本番 image（no-editable install）でも名寄せ辞書を読めること。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import teamagent.karte_attributes as karte_attributes

ROOT = Path(__file__).resolve().parents[2]


def test_filters_found_from_workdir_when_installed_in_site_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # site-packages 配下に置かれた状態を再現: parents[2] の下に data/ は無い。
    fake_module = tmp_path / "venv/lib/python3.14/site-packages/teamagent/karte_attributes.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.touch()
    workdir = tmp_path / "app"
    shutil.copytree(ROOT / "data/connect_web_filters", workdir / "data/connect_web_filters")
    monkeypatch.setattr(karte_attributes, "__file__", str(fake_module))
    monkeypatch.chdir(workdir)
    karte_attributes._references.cache_clear()
    try:
        assert karte_attributes._filters_dir() == workdir / "data/connect_web_filters"
        aliases, industry = karte_attributes._references()
        assert aliases and industry
    finally:
        karte_attributes._references.cache_clear()
