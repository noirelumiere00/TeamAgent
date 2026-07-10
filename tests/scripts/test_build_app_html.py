"""scripts/build_app_html.py の運用ガードのテスト（実 Vault 0・書き込みは tmp_path のみ）。

契約（repo 版で追加した4点。フィルタ/HTML 生成ロジック自体は元スクリプト同一なので対象外）:
- 正常系: ダミー Vault + サイドカーから HTML を生成し、<out>.stats.json と
  フッタ焼き込み（更新: YYYY-MM-DD JST・取引先N・資料M）が入る
- fail-loud: Vault 不在 / サイドカー欠落 / clients==0 / docs==0 は exit 1
  （silent fallback・空 HTML の配信を作らない）
- サニティゲート: 取引先数/資料数/バイト数のいずれかが前回比 20% 超減なら exit 1 で
  既存 out を保持。--allow-shrink で明示的に通過し統計基準がリセットされる
- exclude_stems.json のフィルタ配線: 除外 stem の資料が payload に載らない
- PII 決定論除外: 請求書系 stem（正規化後に「請求」を含む）はサイドカー列挙なしで除外
  （個人名入り stem を repo に平文で持たない）
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "build_app_html", _ROOT / "scripts" / "build_app_html.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["build_app_html"] = _mod
_spec.loader.exec_module(_mod)


# ---------------- fixtures ----------------


@pytest.fixture()
def sidecars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """最小の有効サイドカー一式を tmp に用意し SIDECAR_DIR を差し替える。"""
    d = tmp_path / "filters"
    d.mkdir()
    (d / "exclude_stems.json").write_text(
        json.dumps(["除外対象資料"], ensure_ascii=False), encoding="utf-8"
    )
    (d / "dedup_drop_map.json").write_text(
        json.dumps({"drop": {}, "keep_canonical": []}, ensure_ascii=False), encoding="utf-8"
    )
    (d / "weird_rename_high.json").write_text("{}", encoding="utf-8")
    (d / "inter-var.b64").write_text("QUFBQQ==", encoding="utf-8")  # ダミー base64
    monkeypatch.setattr(_mod, "SIDECAR_DIR", d)
    return d


def _write_client(vault: Path, name: str) -> None:
    (vault / "clients" / f"{name}.md").write_text(
        f'---\nclient: "{name}"\nindustry: "エネルギー"\ndeal_phase: "提案"\n'
        f'bant_score: "B（前向き）"\nfb_count: "2"\ndoc_count: "3"\n---\n\n'
        f"# {name}\n\n### 商談メモ --1750000000\n前向き。\n\n## 関連資料\n- [[docs/提案書A]]\n",
        encoding="utf-8",
    )


def _write_doc(vault: Path, stem: str, client: str = "出光興産") -> None:
    (vault / "docs" / f"{stem}.md").write_text(
        f'---\ntitle: "{stem}"\nclient: "{client}"\nindustry: "エネルギー"\n'
        f'doc_type: "提案書"\nsolution: "動画広告"\nmodified_at: "2026-06-01"\n---\n\n'
        f"> {stem} の抜粋\n\n[[clients/{client}]]\n",
        encoding="utf-8",
    )


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """取引先1・資料3（+除外対象1）の最小ダミー Vault。"""
    v = tmp_path / "vault"
    (v / "clients").mkdir(parents=True)
    (v / "docs").mkdir()
    _write_client(v, "出光興産")
    for stem in ("提案書A", "提案書B", "議事録C"):
        _write_doc(v, stem)
    _write_doc(v, "除外対象資料")  # exclude_stems.json に載っている → 表示されない
    return v


def _run(vault: Path, out: Path, *extra: str) -> int:
    rc = _mod.main(["--vault", str(vault), "--out", str(out), *extra])
    assert isinstance(rc, int)
    return rc


# ---------------- 正常系 ----------------


def test_build_success_writes_html_stats_and_stamp(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0

    html = out.read_text(encoding="utf-8")
    assert "出光興産" in html
    # フッタ焼き込み（statusbar 内・プレースホルダは残っていない）
    assert "__BUILDSTAMP__" not in html
    assert "更新: " in html and "・取引先1・資料3" in html
    # フォント埋め込み（fallback 廃止で必ず入る）
    assert "QUFBQQ==" in html

    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["clients"] == 1
    assert stats["docs"] == 3
    assert stats["bytes"] == len(out.read_bytes())


def test_excluded_stem_not_in_payload(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    out = tmp_path / "app.html"
    _run(vault, out)
    assert "除外対象資料" not in out.read_text(encoding="utf-8")


def test_seikyusho_stem_excluded_without_sidecar_listing(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """請求書系 stem は exclude_stems.json に列挙せず決定論ルールで除外される（PII regression）。

    氏名は架空。サイドカー fixture（"除外対象資料" のみ）に載せていないことが本質:
    列挙ではなく「正規化後 stem に『請求』を含む」ルールが効いていることを検証する。
    """
    stem = "99_山田 花子様_25年1月分_請求書.pdf"
    _write_doc(vault, stem)
    out = tmp_path / "app.html"
    _run(vault, out)
    html = out.read_text(encoding="utf-8")
    assert "請求書" not in html
    assert "山田 花子" not in html
    # 除外されるので資料数は既定の 3 のまま（stats にも計上されない）
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["docs"] == 3


# ---------------- fail-loud ----------------


def test_vault_missing_exits_1(
    sidecars: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as ei:
        _mod.main(["--vault", str(tmp_path / "no_vault"), "--out", str(tmp_path / "o.html")])
    assert ei.value.code == 1
    assert "Vault が見つかりません" in capsys.readouterr().err


def test_sidecar_missing_exits_1(
    sidecars: Path, vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (sidecars / "exclude_stems.json").unlink()
    with pytest.raises(SystemExit) as ei:
        _run(vault, tmp_path / "o.html")
    assert ei.value.code == 1
    assert "サイドカー欠落: exclude_stems.json" in capsys.readouterr().err


def test_empty_clients_exits_1(
    sidecars: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    v = tmp_path / "vault"
    (v / "clients").mkdir(parents=True)
    (v / "docs").mkdir()
    _write_doc(v, "提案書A")
    with pytest.raises(SystemExit) as ei:
        _run(v, tmp_path / "o.html")
    assert ei.value.code == 1
    assert "clients==0" in capsys.readouterr().err


def test_empty_docs_exits_1(
    sidecars: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    v = tmp_path / "vault"
    (v / "clients").mkdir(parents=True)
    (v / "docs").mkdir()
    _write_client(v, "出光興産")
    with pytest.raises(SystemExit) as ei:
        _run(v, tmp_path / "o.html")
    assert ei.value.code == 1
    assert "docs==0" in capsys.readouterr().err


# ---------------- サニティゲート ----------------


def test_shrink_over_20pct_exits_1_and_keeps_previous_out(
    sidecars: Path, vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "app.html"
    _run(vault, out)
    first_bytes = out.read_bytes()

    # 資料 3 → 1（-66%）に痩せた Vault で再生成 → ゲートが止める
    (vault / "docs" / "提案書B.md").unlink()
    (vault / "docs" / "議事録C.md").unlink()
    with pytest.raises(SystemExit) as ei:
        _run(vault, out)
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "サニティゲート" in err and "--allow-shrink" in err
    # 既存 out・統計は上書きされていない
    assert out.read_bytes() == first_bytes
    assert json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))["docs"] == 3


def test_shrink_passes_with_allow_shrink_and_resets_baseline(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    out = tmp_path / "app.html"
    _run(vault, out)
    (vault / "docs" / "提案書B.md").unlink()
    (vault / "docs" / "議事録C.md").unlink()
    assert _run(vault, out, "--allow-shrink") == 0
    # 基準がリセットされ、次回は縮小扱いにならない
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["docs"] == 1
    assert _run(vault, out) == 0


def test_no_shrink_passes_without_flag(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    out = tmp_path / "app.html"
    _run(vault, out)
    assert _run(vault, out) == 0  # 同一 Vault の再生成はゲートを通る


def test_corrupt_stats_exits_1(
    sidecars: Path, vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "app.html"
    Path(str(out) + ".stats.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        _run(vault, out)
    assert ei.value.code == 1
    assert "前回統計" in capsys.readouterr().err
