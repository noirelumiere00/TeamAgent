"""scripts/build_app_html.py の運用ガードのテスト（実 Vault 0・書き込みは tmp_path のみ）。

契約（repo 版で追加した4点。フィルタ/HTML 生成ロジック自体は元スクリプト同一なので対象外）:
- 正常系: ダミー Vault + サイドカーから HTML を生成し、<out>.stats.json と
  フッタ焼き込み（更新: YYYY-MM-DD JST・取引先N・資料M）が入る
- fail-loud: Vault 不在 / サイドカー欠落 / clients==0 / docs==0 は exit 1
  （silent fallback・空 HTML の配信を作らない）
- サニティゲート: 取引先数/資料数/バイト数のいずれかが前回比 20% 超減なら exit 1 で
  既存 out を保持。--allow-shrink で明示的に通過し統計基準がリセットされる
- exclude_stems.json のフィルタ配線: 除外 stem の資料が payload に載らない
- exclude_source_keys.json のフィルタ配線: タイトル変更後も source identity で除外し、
  source_type/external_id は payload/UI に載せない
- PII 決定論除外: 請求書系 stem（正規化後に「請求」を含む）はサイドカー列挙なしで除外
  （個人名入り stem を repo に平文で持たない）
- タグ第1弾: 媒体/動画形式/形式/横断（資料）・温度感/宿題（クライアント）の決定論判定と
  payload/JS 配線（グラフ用 _ctags/_dtags には載せない）＋テーブル「次アクション」列
- タグ第2弾: 担当/（フォーム由来FB引用の「送信者:」抽出・正規化・名寄せ）と
  FB日付の第3フォールバック（引用「タイムスタンプ: YYYY/MM/DD」・末尾切断は棄却）＋
  タグ/テーブル「担当」列/カルテprops の配線
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import unicodedata
from datetime import date
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
    (d / "exclude_source_keys.json").write_text(
        json.dumps(["gsheets:SHEET1:278789217:53"], ensure_ascii=False), encoding="utf-8"
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


def _write_doc(
    vault: Path,
    stem: str,
    client: str = "出光興産",
    *,
    title: str | None = None,
    source_type: str = "",
    external_id: str = "",
    source_url: str = "",
    generated_by: bool = False,
) -> None:
    display_title = title or stem
    generator_line = 'generated_by: "scripts/export_vault.py"\n' if generated_by else ""
    (vault / "docs" / f"{stem}.md").write_text(
        f"---\n{generator_line}"
        f'title: "{display_title}"\nclient: "{client}"\nindustry: "エネルギー"\n'
        f'doc_type: "提案書"\nsolution: "動画広告"\nmodified_at: "2026-06-01"\n'
        f'source_type: "{source_type}"\nexternal_id: "{external_id}"\n---\n\n'
        f"> {display_title} の抜粋\n\n"
        + (f"- 出典: [{source_type or 'source'}]({source_url})\n" if source_url else "")
        + f"\n[[clients/{client}]]\n",
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


def _write_test_export_manifest(
    vault: Path,
    *,
    active_paths: set[str] | None = None,
    complete_export: bool = True,
) -> None:
    """テストVaultをexporterのactive/hash manifest契約へ合わせる。"""
    managed = {
        f"{prefix}/{path.name}": hashlib.sha256(path.read_bytes()).hexdigest()
        for prefix in ("clients", "docs")
        for path in (vault / prefix).glob("*.md")
    }
    active = set(managed) if active_paths is None else set(active_paths)
    payload = {
        "version": 1,
        "generator": "scripts/export_vault.py",
        "complete_export": complete_export,
        "active_files": sorted(active),
        "files": managed,
    }
    (vault / ".export-vault-manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _rewrite_manifest_path_spelling(vault: Path, desired_rel: str) -> None:
    """portable aliasが同じ1 entryを、manifest上だけ指定の表記へ置き換える。"""
    manifest = vault / ".export-vault-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    alias_key = unicodedata.normalize("NFC", desired_rel).casefold()
    aliases = [
        rel for rel in payload["files"] if unicodedata.normalize("NFC", rel).casefold() == alias_key
    ]
    assert len(aliases) == 1
    digest = payload["files"].pop(aliases[0])
    payload["files"][desired_rel] = digest
    payload["active_files"] = [
        rel
        for rel in payload["active_files"]
        if unicodedata.normalize("NFC", rel).casefold() != alias_key
    ] + [desired_rel]
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _run(vault: Path, out: Path, *extra: str, refresh_manifest: bool = True) -> int:
    if refresh_manifest and (vault / "clients").is_dir() and (vault / "docs").is_dir():
        _write_test_export_manifest(vault)
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
    manifest_sha = hashlib.sha256((vault / ".export-vault-manifest.json").read_bytes()).hexdigest()
    payload = _payload(html)
    assert payload["manifest_sha256"] == manifest_sha
    assert payload["stats"]["manifest_sha256"] == manifest_sha
    assert stats["manifest_sha256"] == manifest_sha
    assert payload["build_inputs_sha256"] == _mod.BUILD_INPUTS_SHA256
    assert payload["stats"]["build_inputs_sha256"] == _mod.BUILD_INPUTS_SHA256
    assert stats["build_inputs_sha256"] == _mod.BUILD_INPUTS_SHA256
    data_line = next(line for line in html.splitlines() if line.startswith("const DATA="))
    raw_data = data_line.removeprefix("const DATA=").removesuffix(";")
    assert stats["data_sha256"] == hashlib.sha256(raw_data.encode("utf-8")).hexdigest()


def test_excluded_stem_not_in_payload(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    out = tmp_path / "app.html"
    _run(vault, out)
    assert "除外対象資料" not in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("title", ["最初の表示タイトル", "あとから変更した表示タイトル"])
def test_source_identity_excludes_doc_even_when_title_changes(
    sidecars: Path, vault: Path, tmp_path: Path, title: str
) -> None:
    """除外は可変な title/stem でなく source_type:external_id に追従する。"""
    stem = f"{title}-deadbeef"
    _write_doc(
        vault,
        stem,
        title=title,
        source_type="gsheets",
        external_id="SHEET1:278789217:53",
    )
    # client note 側の関連資料リンクも同じ source 判定で落ち、タイトルを UI へ残さない。
    client_note = vault / "clients" / "出光興産.md"
    client_note.write_text(
        client_note.read_text(encoding="utf-8") + f"- [[docs/{stem}]]\n",
        encoding="utf-8",
    )

    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert title not in html
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["docs"] == 3


def test_internal_source_identity_not_exposed_in_generated_html(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """非除外資料でも frontmatter の内部 source ID は DATA/UI へコピーしない。"""
    external_id = "DO-NOT-EXPOSE-SHEET:999:777"
    _write_doc(
        vault,
        "公開資料-feedface",
        title="公開資料",
        source_type="gsheets",
        external_id=external_id,
        source_url=(
            "https://docs.google.com/spreadsheets/d/DO-NOT-EXPOSE-SHEET/edit#gid=999&range=777:777"
        ),
    )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert "公開資料" in html  # 資料そのものは payload に載る
    assert external_id not in html
    assert f"gsheets:{external_id}" not in html
    assert '"external_id"' not in html
    assert '"source_type"' not in html
    # クリック用の出典 URL は既存の意図仕様。隠すのは frontmatter の複合安定IDそのもの。
    assert "docs.google.com/spreadsheets/d/DO-NOT-EXPOSE-SHEET/" in html


def test_export_vault_generated_same_name_docs_are_not_folded_as_chunks(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """exporterの `_2` は別Drive資料であり、旧chunk規則でsilent dropしない。"""
    _write_doc(vault, "同名提案", title="同名提案 A", generated_by=True)
    _write_doc(vault, "同名提案_2", title="同名提案 B", generated_by=True)

    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert "同名提案 A" in html
    assert "同名提案 B" in html
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["docs"] == 5


def test_unmarked_legacy_chunk_notes_still_fold_to_one(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """marker導入前の意図的な `_2` 分割断片は従来どおり代表1件へ束ねる。"""
    _write_doc(vault, "旧分割資料", title="旧分割資料")
    _write_doc(vault, "旧分割資料_2", title="旧分割資料 2ページ目")

    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert "旧分割資料" in html
    assert "旧分割資料 2ページ目" not in html
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["docs"] == 4


def test_unmanaged_and_inactive_notes_never_enter_html(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """手作業noteやACL解除後の保護noteはVault/ownershipに残っても公開しない。"""
    _write_doc(vault, "手作業の非共有資料", title="絶対に公開しない手作業資料")
    active_paths = {
        f"{prefix}/{path.name}"
        for prefix in ("clients", "docs")
        for path in (vault / prefix).glob("*.md")
        if path.stem != "手作業の非共有資料"
    }
    # files(hash ownership)には残すがactiveから外す = prune-skip保護noteを再現。
    _write_test_export_manifest(vault, active_paths=active_paths)

    out = tmp_path / "app.html"
    assert _run(vault, out, refresh_manifest=False) == 0
    html = out.read_text(encoding="utf-8")
    assert "絶対に公開しない手作業資料" not in html
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["docs"] == 3


def test_manifest_membership_matches_nfc_to_nfd_filesystem_name(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """manifestのNFC pathとreaddirで得るNFD名を同じ公開対象として照合する。"""
    nfc_stem = "Café提案"
    nfd_stem = unicodedata.normalize("NFD", nfc_stem)
    assert nfc_stem != nfd_stem
    _write_doc(vault, nfc_stem, title="Unicode正規化対象資料")
    source = vault / "docs" / f"{nfc_stem}.md"
    physical = vault / "docs" / f"{nfd_stem}.md"
    if not physical.exists():
        source.rename(physical)

    _write_test_export_manifest(vault)
    nfc_rel = f"docs/{nfc_stem}.md"
    _rewrite_manifest_path_spelling(vault, nfc_rel)

    out = tmp_path / "app.html"
    assert _run(vault, out, refresh_manifest=False) == 0
    html = out.read_text(encoding="utf-8")
    assert "Unicode正規化対象資料" in html
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["docs"] == 4


def test_nfd_filesystem_name_still_uses_source_identity_exclusion(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """NFD名でもactive判定を通し、source key除外を取りこぼさない。"""
    nfc_stem = "Café除外"
    nfd_stem = unicodedata.normalize("NFD", nfc_stem)
    _write_doc(
        vault,
        nfc_stem,
        title="非公開のUnicode資料",
        source_type="gsheets",
        external_id="SHEET1:278789217:53",
    )
    source = vault / "docs" / f"{nfc_stem}.md"
    physical = vault / "docs" / f"{nfd_stem}.md"
    if not physical.exists():
        source.rename(physical)
    _write_test_export_manifest(vault)
    _rewrite_manifest_path_spelling(vault, f"docs/{nfc_stem}.md")

    out = tmp_path / "app.html"
    assert _run(vault, out, refresh_manifest=False) == 0
    assert "非公開のUnicode資料" not in out.read_text(encoding="utf-8")
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["docs"] == 3


def test_nfd_filesystem_names_still_fold_legacy_chunks(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """NFD名でもactive判定を通し、旧 `_2` 分割断片を二重公開しない。"""
    nfc_base = "Café分割"
    stems_and_titles = (
        (nfc_base, "Unicode分割の先頭"),
        (f"{nfc_base}_2", "Unicode分割の後続"),
    )
    for nfc_stem, title in stems_and_titles:
        _write_doc(vault, nfc_stem, title=title)
        nfd_stem = unicodedata.normalize("NFD", nfc_stem)
        source = vault / "docs" / f"{nfc_stem}.md"
        physical = vault / "docs" / f"{nfd_stem}.md"
        if not physical.exists():
            source.rename(physical)
    _write_test_export_manifest(vault)
    for nfc_stem, _ in stems_and_titles:
        _rewrite_manifest_path_spelling(vault, f"docs/{nfc_stem}.md")

    out = tmp_path / "app.html"
    assert _run(vault, out, refresh_manifest=False) == 0
    html = out.read_text(encoding="utf-8")
    assert "Unicode分割の先頭" in html
    assert "Unicode分割の後続" not in html
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["docs"] == 4


@pytest.mark.parametrize(
    ("manifest_stem", "alias_stem"),
    [
        ("Café提案", unicodedata.normalize("NFD", "Café提案")),
        ("Report", "report"),
    ],
)
def test_manifest_portable_alias_collision_fails_closed(
    manifest_stem: str,
    alias_stem: str,
    sidecars: Path,
    vault: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """NFC/NFD・大小文字aliasを持つ曖昧なmanifestは公開前に拒否する。"""
    _write_doc(vault, manifest_stem)
    _write_test_export_manifest(vault)
    manifest = vault / ".export-vault-manifest.json"
    manifest_rel = f"docs/{manifest_stem}.md"
    alias_rel = f"docs/{alias_stem}.md"
    _rewrite_manifest_path_spelling(vault, manifest_rel)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    digest = payload["files"][manifest_rel]
    payload["files"][alias_rel] = digest
    payload["active_files"].append(alias_rel)
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SystemExit):
        _run(vault, tmp_path / "app.html", refresh_manifest=False)
    assert "衝突" in capsys.readouterr().err


def test_physical_unicode_alias_collision_fails_closed(
    sidecars: Path,
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """readdirがNFC/NFDの2名を返しても二重公開しない。"""
    nfc_stem = "Café提案"
    nfd_stem = unicodedata.normalize("NFD", nfc_stem)
    _write_doc(vault, nfc_stem)
    _write_test_export_manifest(vault)

    original_glob = Path.glob
    actual = next(
        path
        for path in original_glob(vault / "docs", "*.md")
        if unicodedata.normalize("NFC", path.name).casefold()
        == unicodedata.normalize("NFC", f"{nfc_stem}.md").casefold()
    )
    injected_name = f"{nfd_stem}.md" if actual.name == f"{nfc_stem}.md" else f"{nfc_stem}.md"

    def _glob_with_unicode_alias(path: Path, pattern: str):
        found = list(original_glob(path, pattern))
        if path == vault / "docs" and pattern == "*.md":
            found.append(path / injected_name)
        return iter(found)

    monkeypatch.setattr(Path, "glob", _glob_with_unicode_alias)

    with pytest.raises(SystemExit):
        _run(vault, tmp_path / "app.html", refresh_manifest=False)
    assert "Vault内のMarkdown" in capsys.readouterr().err


def test_active_note_modified_after_export_fails_loud(
    sidecars: Path, vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """manifest上activeでもhashが変わったnoteを静的HTMLへ流さない。"""
    _write_test_export_manifest(vault)
    target = vault / "docs" / "提案書A.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n手作業追記", encoding="utf-8")

    with pytest.raises(SystemExit):
        _run(vault, tmp_path / "app.html", refresh_manifest=False)
    assert "export後に変更" in capsys.readouterr().err


def test_partial_or_legacy_manifest_cannot_build_public_html(
    sidecars: Path, vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """完全exportの証明がないmanifestはcompany-shared公開に使わない。"""
    _write_test_export_manifest(vault, complete_export=False)
    with pytest.raises(SystemExit):
        _run(vault, tmp_path / "app.html", refresh_manifest=False)
    assert "完全exportではありません" in capsys.readouterr().err


@pytest.mark.parametrize(
    "manifest_case",
    ["empty", "duplicate", "traversal", "dot_md", "missing", "bad_hash"],
)
def test_malformed_active_manifest_fails_closed(
    manifest_case: str,
    sidecars: Path,
    vault: Path,
    tmp_path: Path,
) -> None:
    """公開境界の空/重複/逸脱path/欠損/hash不正を直接回帰固定する。"""
    _write_test_export_manifest(vault)
    manifest = vault / ".export-vault-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    first = payload["active_files"][0]
    if manifest_case == "empty":
        payload["active_files"] = []
    elif manifest_case == "duplicate":
        payload["active_files"] = [first, first]
    elif manifest_case == "traversal":
        payload["active_files"] = ["docs/../outside.md"]
        payload["files"]["docs/../outside.md"] = "0" * 64
    elif manifest_case == "dot_md":
        payload["active_files"] = ["docs/.md"]
        payload["files"]["docs/.md"] = "0" * 64
    elif manifest_case == "missing":
        payload["active_files"] = ["docs/missing.md"]
        payload["files"]["docs/missing.md"] = "0" * 64
    else:
        payload["files"][first] = "not-a-sha256"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SystemExit):
        _run(vault, tmp_path / "app.html", refresh_manifest=False)


def test_active_note_symlink_fails_closed(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """active pathをsymlinkへ差し替えてもリンク先を公開しない。"""
    _write_test_export_manifest(vault)
    active = vault / "docs" / "提案書A.md"
    outside = tmp_path / "outside.md"
    outside.write_text("秘密", encoding="utf-8")
    active.unlink()
    active.symlink_to(outside)

    with pytest.raises(SystemExit):
        _run(vault, tmp_path / "app.html", refresh_manifest=False)


def test_export_manifest_symlink_fails_closed(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """manifest自体をsymlinkへ差し替えても信頼しない。"""
    _write_test_export_manifest(vault)
    manifest = vault / ".export-vault-manifest.json"
    external_manifest = tmp_path / "external-manifest.json"
    external_manifest.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
    manifest.unlink()
    manifest.symlink_to(external_manifest)

    with pytest.raises(SystemExit):
        _run(vault, tmp_path / "app.html", refresh_manifest=False)


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


def test_entity_frontmatter_becomes_ents_and_kankeisaki_tag(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """frontmatter entities（名寄せ CSV）が DATA.docs.ents と「関係先/」タグに載る。

    親クライアント名（サンマルクカフェ）で子コラボ資料（祇園辻利プロモ）を /app の
    タグ・検索から辿れるようにする配線の回帰テスト。
    """
    (vault / "docs" / "0115_祇園辻利プロモーション.md").write_text(
        '---\ntitle: "0115_祇園辻利プロモーション"\nclient: "祇園辻利"\n'
        'industry: "飲食"\ndoc_type: "提案書"\nsolution: "動画広告"\n'
        'entities: "サンマルクカフェ,祇園辻利"\nmodified_at: "2026-06-18"\n---\n\n'
        "> サンマルクカフェ×祇園辻利のコラボ企画\n\n[[clients/祇園辻利]]\n",
        encoding="utf-8",
    )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    # DATA.docs に ents としてコラボ相手が載る（検索 hay/タグの元データ）
    assert '"ents"' in html and "サンマルクカフェ" in html
    # 関係先/ タグ系統が有効化されている（CATMETA/TAGORDER）
    assert "関係先" in html
    # self-filter は両辺 JS nrm() で比較（Py norm() 由来 cnorm との『・』差で自クライアント重複を出さない）
    assert "if(nrm(e)!==nrm(d.client))t.push" in html
    # エンティティ内の / はタグ階層を汚さないよう無害化してから「関係先/」に付ける
    assert 'e.split(/[\\/／]/).join("・")' in html


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


def test_source_key_sidecar_missing_exits_1(
    sidecars: Path, vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (sidecars / "exclude_source_keys.json").unlink()
    with pytest.raises(SystemExit) as ei:
        _run(vault, tmp_path / "o.html")
    assert ei.value.code == 1
    assert "サイドカー欠落: exclude_source_keys.json" in capsys.readouterr().err


def test_repository_source_exclusions_preserve_legacy_stems() -> None:
    """新 Vault はsource key、移行中の旧 Vault は従来stemの同じ6行を除外する。"""
    sheet = "1jRmoUPo0kAhOGA6secGcwGHILH5LHt7lYvEuxJ5uupo"
    rows = {53, 61, 75, 84, 213, 214}
    filters = _ROOT / "data" / "connect_web_filters"
    source_keys = set(
        json.loads((filters / "exclude_source_keys.json").read_text(encoding="utf-8"))
    )
    legacy_stems = set(json.loads((filters / "exclude_stems.json").read_text(encoding="utf-8")))

    assert source_keys == {f"gsheets:{sheet}:278789217:{row}" for row in rows}
    assert {
        f"ナレッジ共有 - フォーム回答 - フォーム回答 1 - row {row}" for row in rows
    } <= legacy_stems


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


# ---------------- parse_fb_events（施策タイムライン） ----------------


def test_parse_fb_date_from_quote_line_and_fields() -> None:
    """Slack 由来 FB: `> [YYYY-MM-DD HH:MM]` 行から日付、全フィールドを抽出。"""
    body = (
        "## 営業FB時系列（新しい順）\n\n"
        "### ---- #proj-ショート動画_営業フィードバック情報 1778489156.830189\n\n"
        "- フェーズ: ケイパ\n"
        "- BANT: C（検討）\n"
        "- チャネル: 代理店\n"
        "- ポジ反応: ・企業ブランディングの相談は多い。\n"
        "- ネガ反応: ・PIVOTの問い合わせは多い。\n"
        "- 次アクション: 全社に展開\n"
        "- 提案メニュー: UGC（TTO、切り抜きなど）\n\n"
        "> [2026-05-11 08:45] <B0AK>: 本文抜粋\n\n"
        "[出典](slack://C0A/1778489156.830189)\n\n"
        "## 関連資料\n\n- [[docs/x]]\n"
    )
    evs = _mod.parse_fb_events(body)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["d"] == "2026-05-11"  # > [..] 行が epoch より優先
    assert ev["src"] == "Slack"
    assert ev["ph"] == "ケイパ"
    assert ev["bant"] == "C（検討）"
    assert ev["menu"] == "UGC（TTO、切り抜きなど）"
    assert ev["pos"] == "・企業ブランディングの相談は多い。"
    assert ev["neg"] == "・PIVOTの問い合わせは多い。"
    assert ev["next"] == "全社に展開"


def test_parse_fb_epoch_converts_to_jst_date() -> None:
    """epoch → UTC+9 で日付化(UTC のままだと前日になる境界 epoch で検証)。"""
    # 1779120000 = 2026-05-19 01:00 JST（UTC では 2026-05-18 16:00）
    body = "### ---- #proj-ch 1779120000.123456\n\n- ポジ反応: よい\n"
    evs = _mod.parse_fb_events(body)
    assert evs[0]["d"] == "2026-05-19"
    # 仕様書の例: 1779101519.347119 → 2026-05-18
    evs2 = _mod.parse_fb_events("### ---- #proj-ch 1779101519.347119\n\n- ポジ反応: 別内容\n")
    assert evs2[0]["d"] == "2026-05-18"


def test_parse_fb_dedup_keeps_dated_entry() -> None:
    """Slack/フォーム二重登録: 正規化 (ポジ+ネガ) 一致 → 日付を持つ方だけ残る。"""
    body = (
        "### ---- ショート動画営業 FB - フォーム回答 - フォーム回答 1 - row 9\n\n"
        "- フェーズ: ケイパ\n"
        "- ポジ反応: ・相談は 多い。\n"
        "- ネガ反応: ・質問　あり\n\n"
        "### ---- #proj-ch 1778489156.830189\n\n"
        "- フェーズ: ケイパ\n"
        "- ポジ反応: ・相談は多い。\n"
        "- ネガ反応: ・質問あり\n"
    )
    evs = _mod.parse_fb_events(body)
    assert len(evs) == 1  # 空白/全角空白差は正規化で吸収され dedup される
    assert evs[0]["src"] == "Slack"
    assert evs[0]["d"] == "2026-05-11"


def test_parse_fb_form_src_and_dateless() -> None:
    body = (
        "### ---- ショート動画営業 FB - フォーム回答 - フォーム回答 1 - row 3\n\n"
        "- フェーズ: ヒアリング\n- ポジ反応: 保証型がよい\n"
    )
    evs = _mod.parse_fb_events(body)
    assert evs[0]["src"] == "フォーム"
    assert evs[0]["d"] == ""  # フォーム回答は日付なしを許容


def test_parse_fb_broken_heading_and_missing_fields_fail_open() -> None:
    """壊れた見出し・欠損フィールドは空文字で許容し、非FBのh3配下は拾わない。"""
    body = (
        "### ---- 壊れた見出し（epochもrowも無い）\n\n"
        "自由記述のみでフィールド行なし\n\n"
        "### 通常のh3見出し（FBではない）\n\n"
        "- フェーズ: 誤検知しない\n"
    )
    evs = _mod.parse_fb_events(body)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["d"] == "" and ev["ph"] == "" and ev["pos"] == "" and ev["next"] == ""
    assert ev["src"] == "Slack"  # 既定
    # 非入力・破損入力でも例外を漏らさない
    assert _mod.parse_fb_events("") == []
    assert _mod.parse_fb_events("FBセクションなしの本文") == []


def test_parse_fb_sorted_desc_dateless_last_and_capped_at_30() -> None:
    blocks = []
    for i in range(33):
        blocks.append(
            f"### ---- #proj-ch 通番{i}\n\n- ポジ反応: 個別内容{i}\n\n"
            f"> [2026-01-{(i % 28) + 1:02d} 10:00] x\n"
        )
    evs = _mod.parse_fb_events("\n".join(blocks))
    assert len(evs) == 30  # cap
    dates = [e["d"] for e in evs]
    assert dates == sorted(dates, reverse=True)  # 日付降順

    # 日付なしは末尾
    evs2 = _mod.parse_fb_events(
        "### ---- 日付なし row 1\n\n- ポジ反応: あ\n\n"
        "### ---- #ch 1779101519.347119\n\n- ポジ反応: い\n"
    )
    assert [e["d"] for e in evs2] == ["2026-05-18", ""]


def test_parse_fb_field_truncation() -> None:
    body = (
        "### ---- #ch 1779101519\n\n"
        "- ポジ反応: " + "あ" * 500 + "\n"
        "- ネガ反応: " + "え" * 500 + "\n"
        "- 提案メニュー: " + "い" * 100 + "\n"
        "- 次アクション: " + "う" * 200 + "\n"
    )
    ev = _mod.parse_fb_events(body)[0]
    # pos/neg=120 は実 Vault 計測（+400KB 超過）で 160 から落とした値（本体 FB_FIELD_MAP と対）
    assert len(ev["pos"]) == 120
    assert len(ev["neg"]) == 120
    assert len(ev["menu"]) == 60
    assert len(ev["next"]) == 120


# ---------------- タグ第2弾: 担当/（FB送信者）+ タイムスタンプ日付フォールバック ----------------


def _form_fb(quote: str, pos: str = "内容", row: int = 1) -> str:
    """フォーム由来FB1件分（見出し+フィールド+引用）。引用は実データ同様1行連結・氏名は架空。"""
    return (
        f"### ---- ショート動画営業 FB - フォーム回答 - フォーム回答 1 - row {row}\n\n"
        f"- ポジ反応: {pos}\n\n"
        f"> {quote}\n"
    )


def test_parse_fb_sender_extraction_and_normalization() -> None:
    """送信者抽出+正規化: そのまま／半角括弧／全角括弧+全角スペース／アンダースコア／半角スペース。"""
    cases = [
        (
            "送信者: 山本四郎 タイムスタンプ: 2026/05/11 17:38:30 連携ステータス: 連携済み",
            "山本四郎",
        ),
        ("送信者: 鈴木一郎(Suzuki", "鈴木一郎"),  # 半角括弧・300字切断で閉じ括弧なし
        ("送信者: 田中　次郎（tanaka ji", "田中次郎"),  # 全角括弧+全角スペース
        ("送信者: 高橋三郎_TakahashiSaburo タイムスタンプ: 2026/06/1", "高橋三郎"),
        (
            "送信者: 村山 五郎 タイムスタンプ: 2026/05/21 19:25:37 連携ステータス: 連携済み",
            "村山五郎",
        ),
    ]
    for i, (quote, expected) in enumerate(cases):
        evs = _mod.parse_fb_events(_form_fb(f"商流: 直販 {quote}", pos=f"内容{i}", row=i + 1))
        assert evs[0]["by"] == expected, quote


def test_parse_fb_sender_absent_or_slack_is_empty() -> None:
    """送信者行なし（Slack直投稿は <@UID> のみ）→ by は空文字。"""
    evs = _mod.parse_fb_events(
        "### ---- #proj-ch 1779101519.347119\n\n- ポジ反応: あ\n\n"
        "> [2026-05-18 10:00] <@U012AB>: 本文抜粋\n"
    )
    assert evs[0]["by"] == ""
    assert _mod.parse_fb_events(_form_fb("商流: 直販 顧客名: X社"))[0]["by"] == ""


def test_parse_fb_sender_same_person_variants_unify() -> None:
    """「鈴木一郎(Suzuki…」と「鈴木一郎」は正規化で同一人物へ統合される。"""
    body = _form_fb(
        "送信者: 鈴木一郎(Suzuki Ichiro) タイムスタンプ: 2026/05/01 10:00:00 連携ステータス: x",
        pos="A",
        row=1,
    ) + _form_fb(
        "送信者: 鈴木一郎 タイムスタンプ: 2026/05/02 10:00:00 連携ステータス: x", pos="B", row=2
    )
    evs = _mod.parse_fb_events(body)
    assert [e["by"] for e in evs] == ["鈴木一郎", "鈴木一郎"]
    assert _mod.tans_of(evs) == ["鈴木一郎"]


def test_parse_fb_dedup_backfills_by_from_folded_duplicate() -> None:
    """Slack転送（先出・日付あり・送信者なし）とフォーム行（実名あり）の二重登録:
    残るのは従来どおり先出のSlack側だが、by はフォーム側から補完される。"""
    body = (
        "### ---- #proj-ch 1778489156.830189\n\n"
        "- フェーズ: ケイパ\n- ポジ反応: ・相談は多い。\n- ネガ反応: ・質問あり\n\n"
        "> [2026-05-11 08:45] <@U01>: 本文\n\n"
        "### ---- ショート動画営業 FB - フォーム回答 - フォーム回答 1 - row 9\n\n"
        "- フェーズ: ケイパ\n- ポジ反応: ・相談は多い。\n- ネガ反応: ・質問あり\n\n"
        "> 商流: 代理店 送信者: 山本四郎 タイムスタンプ: 2026/05/11 17:38:30 連携ステータス: x\n"
    )
    evs = _mod.parse_fb_events(body)
    assert len(evs) == 1
    assert evs[0]["src"] == "Slack"  # 残す側の規則は不変（日付を持つ先出）
    assert evs[0]["d"] == "2026-05-11"
    assert evs[0]["by"] == "山本四郎"  # 消えたフォーム行から補完


def test_tans_of_distinct_sorted_capped() -> None:
    """tans_of: 非空 by の distinct・ソート済み・最大 TANS_MAX 名。空/None は空リスト。"""
    tl = [
        {"by": n} for n in ("佐々木", "青木", "", "佐々木", "山田", "田中", "中村", "渡辺", "伊藤")
    ]
    tans = _mod.tans_of(tl)
    assert len(tans) == _mod.TANS_MAX == 5
    assert tans == sorted(tans)
    assert "" not in tans and len(set(tans)) == 5
    assert _mod.tans_of([]) == []
    assert _mod.tans_of(None) == []


def test_parse_fb_timestamp_fallback_accepts_complete_date() -> None:
    """第3フォールバック: 日付の直後に文字が続く完全な日付のみ採用（YYYY-MM-DD へ変換）。"""
    evs = _mod.parse_fb_events(
        _form_fb("送信者: 山本四郎 タイムスタンプ: 2026/05/11 17:38:30 連携ステータス: 連携済み")
    )
    assert evs[0]["d"] == "2026-05-11"
    # 1桁月日はゼロ埋めで正規化（時刻の途中で切断されていても日付自体は完全）
    evs2 = _mod.parse_fb_events(_form_fb("送信者: 山本四郎 タイムスタンプ: 2026/6/2 1", row=2))
    assert evs2[0]["d"] == "2026-06-02"


def test_parse_fb_timestamp_truncated_at_line_end_rejected() -> None:
    """300字切断対策: 行末で終わる日付（2026/06/1 → /1X の途中の可能性）は棄却して日付なし。"""
    evs = _mod.parse_fb_events(_form_fb("送信者: 高橋三郎_T タイムスタンプ: 2026/06/1"))
    assert evs[0]["d"] == ""
    # 月までしか残っていない切断も不採用
    evs2 = _mod.parse_fb_events(_form_fb("送信者: 山本四郎 タイムスタンプ: 2026/04", row=2))
    assert evs2[0]["d"] == ""
    # 不正な日付（13月）は datetime 変換で棄却
    evs3 = _mod.parse_fb_events(
        _form_fb("タイムスタンプ: 2026/13/05 10:00:00 連携ステータス: x", row=3)
    )
    assert evs3[0]["d"] == ""


def test_parse_fb_existing_date_sources_win_over_timestamp() -> None:
    """既存2段（> [日付] 行 → 見出し epoch）が優先。タイムスタンプは両方欠けたときのみ。"""
    body = (
        "### ---- #proj-ch 1778489156.830189\n\n- ポジ反応: あ\n\n"
        "> [2026-05-11 08:45] <@U01>: 本文\n\n> タイムスタンプ: 2026/01/01 09:00:00 x\n"
    )
    assert _mod.parse_fb_events(body)[0]["d"] == "2026-05-11"
    body2 = (
        "### ---- #proj-ch 1779101519.347119\n\n- ポジ反応: い\n\n"
        "> タイムスタンプ: 2026/01/01 09:00:00 x\n"
    )
    assert _mod.parse_fb_events(body2)[0]["d"] == "2026-05-18"  # 見出し epoch(JST) が優先


def test_tans_payload_tags_table_props_wiring(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """担当: payload tans・タグ（clientTags/CATMETA/TAGORDER）・テーブル列・props・タイムライン配線。"""
    (vault / "clients" / "帝人.md").write_text(
        '---\nclient: "帝人"\nindustry: "メーカー"\ndeal_phase: "ヒアリング"\n'
        'bant_score: "B（前向き）"\nfb_count: 2\ndoc_count: 0\n---\n\n# 帝人\n\n'
        "## 営業FB時系列（新しい順）\n\n"
        "### ---- ショート動画営業 FB - フォーム回答 - フォーム回答 1 - row 1\n\n"
        "- フェーズ: ヒアリング\n- ポジ反応: ・保証型がよい\n\n"
        "> 商流: 直販 顧客名: 帝人 送信者: 鈴木一郎(Suzuki タイムスタンプ: 2026/05/11 17:38:30 連携ステータス: 連携済み\n\n"
        "### ---- ショート動画営業 FB - フォーム回答 - フォーム回答 1 - row 2\n\n"
        "- フェーズ: ヒアリング\n- ポジ反応: ・単価感が良い\n\n"
        "> 商流: 直販 顧客名: 帝人 送信者: 高橋三郎_TakahashiSaburo タイムスタンプ: 2026/06/02 11:27 連携ステー\n\n"
        "## 関連資料\n",
        encoding="utf-8",
    )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    # payload: 正規化済み送信者・distinct ソート済み tans・タイムスタンプ由来の日付
    assert '"by": "鈴木一郎"' in html
    assert '"tans": ["鈴木一郎", "高橋三郎"]' in html
    assert '"d": "2026-05-11"' in html and '"d": "2026-06-02"' in html
    # FBなしクライアント（出光興産）は tans 空
    assert re.search(r'"stem": "出光興産".*?"tans": \[\]', html)
    # JS 配線: clientTags → 担当/・CATMETA 系統色・TAGORDER 取引先群（先頭7）
    assert '(c.tans||[]).forEach(n=>t.push("担当/"+n))' in html
    assert '"担当":"#' in html
    assert "TAGORDER.slice(0,7)" in html and "TAGORDER.slice(7)" in html
    # テーブル: TCOLS「担当」列（ソート可）+ 行セル（空は—）
    assert '["tans","担当",c=>(c.tans||[]).join("・"),0]' in html
    assert 'c.tans.join("・")' in html
    # カルテ: propsPanel 撤去に伴い「担当」は summaryHeader へ移設（props 行アサートを差し替え）
    assert '["list","担当",(c.tans||[]).join("・")]' not in html
    assert "+summaryHeader(c)" in html
    assert 'khc("👤","担当",c.tans.join("・")' in html
    # 施策タイムライン: FBカードの送信者表示（非空時のみ）
    assert 'title="FB送信者"' in html and "f.by?" in html


# ---------------- 施策タイムライン: payload / DOM 配線 ----------------


def test_timeline_payload_and_dom_in_html(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """FB 持ちクライアントを追加して build → tl/lastfb/cnorm が payload に載り DOM/JS が入る。"""
    (vault / "clients" / "帝人.md").write_text(
        '---\nclient: "帝人"\nindustry: "メーカー"\ndeal_phase: "ヒアリング"\n'
        'bant_score: "B（前向き）"\nfb_count: 1\ndoc_count: 0\n---\n\n# 帝人\n\n'
        "## 営業FB時系列（新しい順）\n\n"
        "### ---- #proj-ショート動画_営業フィードバック情報 1779101519.347119\n\n"
        "- フェーズ: ヒアリング\n- BANT: B（前向き）\n- ポジ反応: ・保証型がよい\n"
        "- 次アクション: 与件獲得\n- 提案メニュー: UGC\n\n"
        "## 関連資料\n",
        encoding="utf-8",
    )
    out = tmp_path / "app.html"
    _run(vault, out)
    html = out.read_text(encoding="utf-8")
    # DOM/JS: タイムラインセクション・トグル・最終接点列
    assert "tlwrap" in html and "施策タイムライン" in html
    assert "tlmorebtn" in html
    assert "最終接点" in html
    # payload: FB イベント JSON（日付は epoch → JST）と lastfb / cnorm
    assert '"src": "Slack"' in html
    assert '"d": "2026-05-18"' in html
    assert '"lastfb": "2026-05-18"' in html
    assert '"pos": "・保証型がよい"' in html
    assert '"cnorm": "帝人"' in html


def test_timeline_merged_on_name_dedup(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """名寄せ統合時: 表記ゆれ 2 カルテの FB が結合される（正本1つに tl 2件・lastfb=最新日）。"""
    for fname, cname, epoch, pos in (
        ("帝人.md", "帝人", "1779101519.347119", "・保証型がよい"),
        ("帝人様.md", "帝人様", "1779120000.000000", "・単価感が抑えられる"),
    ):
        (vault / "clients" / fname).write_text(
            f'---\nclient: "{cname}"\nindustry: "メーカー"\ndeal_phase: ""\n'
            f'bant_score: ""\nfb_count: 1\ndoc_count: 0\n---\n\n# {cname}\n\n'
            "## 営業FB時系列（新しい順）\n\n"
            f"### ---- #proj-ch {epoch}\n\n- ポジ反応: {pos}\n\n"
            "## 関連資料\n",
            encoding="utf-8",
        )
    out = tmp_path / "app.html"
    _run(vault, out)
    html = out.read_text(encoding="utf-8")
    # 名寄せで 1 クライアントに統合され、tl は両カルテの FB を含む
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["clients"] == 2  # 出光興産 + 帝人（帝人様は統合）
    assert '"pos": "・保証型がよい"' in html
    assert '"pos": "・単価感が抑えられる"' in html
    # 最新日（2026-05-19 = 1779120000 JST）が lastfb
    assert '"lastfb": "2026-05-19"' in html


# ---------------- カルテUX第2弾: サマリーヘッダ/カード圧縮/日付分離/今日見るべき/経過日数バッジ ----------------


def test_summary_header_wiring_replaces_props(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """P1: propsPanel 撤去の受け皿 summaryHeader が openClient に配線され、共通経過日ヘルパが入る。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    # 関数定義と openClient への配線
    assert "function summaryHeader" in html
    assert "+summaryHeader(c)" in html
    # 共通ヘルパ（P1/P4/P5 で共用・閾値 31/92）
    for token in ("function daysAgo", "function ageSev", "function agoLabel"):
        assert token in html, token
    # ヘッダの各スロット（絵文字ラベル＋条件付き描画）
    assert 'khc("⏱","最終接点"' in html
    assert 'khc("🎯","次の一手",c.nx' in html
    assert 'khc("👤","担当",c.tans.join("・")' in html
    assert 'khc("💬","活動","FB"+c.fb+"・資料"+c.doc' in html
    # 資料だけ取引先(tl 空)の劣化形＝1行畳み（空ラベルを出さない）
    assert "khdr khmin" in html and "商談FBは未記録" in html
    # クライアント props の「担当」行は撤去済み（doc 用 propsPanel は残る）
    assert '["list","担当",(c.tans||[]).join("・")]' not in html


def test_timeline_fbcard_compression_wiring(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """P2: FBカードは既定1行プレビュー(=次アクション)＋クリックで全文（ポジ/ネガ/次）展開。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    # 圧縮カード構造とトグル・アフォーダンス
    for token in ('(isfb?" tlfbcard":"")', 'class="tlprev"', 'class="tlfull"', 'class="tltog"'):
        assert token in html, token
    # プレビュー行の中身は次アクション（無ければ menu→pos）
    assert 'f.next?("→ "+f.next):(f.menu||f.pos' in html
    # 現在未表示だったネガを展開で追加（payload にあり）
    assert "tlneg" in html and "ネガ" in html
    # カード委譲クリックの open トグル配線
    assert "classList.toggle(" in html and 'querySelectorAll(".tlfbcard")' in html


def test_timeline_fold_threshold_is_three_and_split(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """B+P3: 外側 fold は 3件目以降（dated 軸側のみ）・ev を dated/undated に分割。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    # 旧 10 件しきいは撤廃、新しきいは 3
    assert "ev.length>10" not in html
    assert "dated.length>3" in html
    assert "(dated.length-3)+" in html
    # 日付あり/なしの分割
    assert "ev.filter(x=>x.d)" in html and "ev.filter(x=>!x.d)" in html
    # tlmorebtn は保持（テスト固定クラス）
    assert "tlmorebtn" in html


def test_timeline_undated_group(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """P3: 日付なしFB は「日付不明の記録」ブロックへ分離（「—」列を根絶）。payload に日付なし ev が載る。"""
    (vault / "clients" / "帝人.md").write_text(
        '---\nclient: "帝人"\nindustry: "メーカー"\ndeal_phase: "ヒアリング"\n'
        'bant_score: "B（前向き）"\nfb_count: 2\ndoc_count: 0\n---\n\n# 帝人\n\n'
        "## 営業FB時系列（新しい順）\n\n"
        "### ---- #proj-ショート動画_営業フィードバック情報 1779101519.347119\n\n"
        "- フェーズ: ヒアリング\n- ポジ反応: ・保証型がよい\n- 次アクション: 与件獲得\n\n"
        "### ---- ショート動画営業 FB - フォーム回答 - フォーム回答 1 - row 9\n\n"
        "- フェーズ: ヒアリング\n- ポジ反応: ・日付が無い回答\n- 次アクション: 追客\n\n"
        "## 関連資料\n",
        encoding="utf-8",
    )
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    # 日付なし ev が payload に載る（form 回答は日付なしを許容）
    assert '"d": ""' in html
    # 分離ブロックの DOM/文言
    assert "tlundated" in html and "日付不明の記録" in html and "tluitem" in html


def test_home_triage_section_removed(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """ホーム「今日見るべき」節は撤去済み（接点不明が上位を占め実用にならず・ユーザーFB 2026-07-14）。
    宿題(hw)payload 自体は残る（テーブルの宿題フィルタ等で使うため）。"""
    (vault / "clients" / "帝人.md").write_text(
        '---\nclient: "帝人"\nindustry: "メーカー"\ndeal_phase: "ヒアリング"\n'
        'bant_score: "B（前向き）"\nfb_count: 1\ndoc_count: 0\n---\n\n# 帝人\n\n'
        "## 営業FB時系列（新しい順）\n\n"
        "### ---- ショート動画営業 FB - フォーム回答 - フォーム回答 1 - row 5\n\n"
        "- フェーズ: ヒアリング\n- 次アクション: 見積提示\n\n"
        "## 関連資料\n",
        encoding="utf-8",
    )
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    # 宿題 payload は残る（テーブルフィルタ等で使用）
    assert re.search(r'"stem": "帝人".*?"hw": 1', html)
    # 「今日見るべき」節の描画・配線は撤去済み
    assert "今日見るべき" not in html
    assert "trirows" not in html
    assert 'querySelectorAll(".tgrow")' not in html


def test_table_last_contact_age_badge(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """P5: テーブル最終接点セルが経過日数バッジ（相対＋重症度色）に。ソートは lastOf 不変。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert "function ageCell" in html
    assert "ageCell(lastOf(c))" in html
    assert 'class="age ' in html
    # ソートキーは既存 lastOf（表示のみ変更）
    assert '["last","最終接点",c=>lastOf(c),0]' in html


def test_name_emphasis_css(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """C: クライアント/資料名をカード・テーブル先頭列・検索結果で一段階強調（CSS のみ）。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert ".wcard .wt{font-size:var(--f-ui-med);color:var(--text);font-weight:700" in html
    assert ".tbv tbody td:first-child{color:var(--text);font-weight:700}" in html
    assert ".sr .srt{font-size:var(--f-ui-small);font-weight:600;color:var(--text)" in html


# ---------------- タグ拡充（最終接点/更新/情報源） ----------------


def test_doc_src_parsed_from_shutten_line(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """出典行のトークンが src に写像される（gdrive→Drive / gsheets→フォーム）。"""
    p = vault / "docs" / "提案書A.md"
    p.write_text(
        p.read_text() + "\n- 出典: [gdrive](https://drive.google.com/file/d/x/view)\n",
        encoding="utf-8",
    )
    (vault / "docs" / "フォーム回答X.md").write_text(
        '---\ntitle: "フォーム回答X"\nclient: "出光興産"\nindustry: ""\n'
        'doc_type: "議事録"\nsolution: ""\nmodified_at: "2026-07-01"\n---\n\n'
        "> 抜粋\n\n- 出典: [gsheets](https://docs.google.com/spreadsheets/d/x)\n",
        encoding="utf-8",
    )
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert '"src": "Drive"' in html
    assert '"src": "フォーム"' in html


def test_client_last_is_max_of_lastfb_and_doc_modified(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """最終接点 = max(最終FB日, 関連資料の最新 modified)。FB日付なしなら資料側が採用される。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert '"last": "2026-06-01"' in html


def test_freshness_tag_wiring_in_js(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """ageBucket と 最終接点/更新/情報源 タグの JS 配線が生成 HTML に存在する。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    for token in ("function ageBucket", "最終接点/", '"更新/"+a', '"情報源/"+d.src'):
        assert token in html, token


# ---------------- タグ第1弾: 媒体/（決定論 regex・誤爆対策） ----------------


def test_media_tags_positive_cases() -> None:
    """各媒体の正常系（TikTok ほか）と複数付与・MEDIA_RES 定義順。"""
    assert _mod.media_tags("TikTok縦型動画のご提案") == ["TikTok"]
    assert _mod.media_tags("YouTube・Instagram同時配信企画") == ["YouTube", "Instagram"]
    assert _mod.media_tags("インスタ運用レポート") == ["Instagram"]
    assert _mod.media_tags("LINE公式アカウント設計") == ["LINE"]
    assert _mod.media_tags("Facebook広告メニュー") == ["Facebook"]
    assert _mod.media_tags("テレビCM連動・TVer配信") == ["テレビ"]
    assert _mod.media_tags("駅サイネージ×OOH展開") == ["OOH"]
    assert _mod.media_tags("") == []


def test_media_tags_x_variants_and_guards() -> None:
    """X: 「X（旧Twitter）」「Twitter」表記は付き、DX・小文字コラボ表記 x は付かない。"""
    assert _mod.media_tags("X（旧Twitter）キャンペーン") == ["X"]
    assert _mod.media_tags("Twitterトレンド分析") == ["X"]
    assert _mod.media_tags("Ｘ（旧Ｔｗｉｔｔｅｒ）施策") == ["X"]  # 全角も NFKC で吸収
    assert _mod.media_tags("X運用のご提案") == ["X"]  # 英数字境界の単独 X
    assert _mod.media_tags("DX推進のご提案") == []
    assert _mod.media_tags("ブランドA x ブランドB コラボ") == []  # コラボ表記の小文字 x
    assert _mod.media_tags("https://x.com/example の投稿") == []  # URL の小文字 x


def test_media_tags_line_not_confused_by_english_words() -> None:
    """LINE: online/deadline/GUIDELINE/カタカナのライン系は誤爆しない。"""
    for text in (
        "online配信のご案内",
        "提出deadlineの共有",
        "ブランドGUIDELINE策定",
        "オンラインセミナー実施",
        "デッドライン注意",
        "商品ラインナップ一覧",
    ):
        assert "LINE" not in _mod.media_tags(text), text
    assert _mod.media_tags("LINEヤフー共同企画") == ["LINE"]  # 直後がカタカナなら付く


# ---------------- タグ第1弾: 動画形式/・形式/ ----------------


def test_video_format_tags() -> None:
    assert _mod.video_format_tags("縦型ショート動画企画") == ["ショート"]
    assert _mod.video_format_tags("切り抜き二次利用について") == ["切り抜き"]
    assert _mod.video_format_tags("ライブコマース実施要領") == ["ライブ配信"]
    assert _mod.video_format_tags("ライブ配信＋長尺アーカイブ") == ["ライブ配信", "長尺"]
    assert _mod.video_format_tags("ショートステイのご案内") == []  # 「ショート」単体では付けない


def test_file_format_tag_from_stem_suffix() -> None:
    """stem 末尾の拡張子表記のみ判定（大文字小文字無視・拡張子なし/途中出現は付けない）。"""
    assert _mod.file_format_tag("新提案書FMT.pptx") == "PPTX"
    assert _mod.file_format_tag("実績レポート.PDF") == "PDF"
    assert _mod.file_format_tag("進行管理表.xlsx") == "Excel"
    assert _mod.file_format_tag("覚書ドラフト.docx") == "Word"
    assert _mod.file_format_tag("議事録（拡張子なし）") == ""
    assert _mod.file_format_tag("pptx資料まとめ") == ""
    assert _mod.file_format_tag("") == ""


# ---------------- ナレッジ共有メタ4軸: クライアント種別/提案プロダクト/施策手法/代理店 ----------------


def test_client_tier_tags_splits_on_ascii_comma_not_toten() -> None:
    """クライアント種別: ASCII/全角カンマで分割・読点「、」では割らない（官公庁、自治体は1値）。"""
    # 読点で割ると 官公庁 と 自治体 に誤分割する回帰の防止 → alias で 官公庁・自治体 の1値
    assert _mod.client_tier_tags("官公庁、自治体") == ["官公庁・自治体"]
    assert _mod.client_tier_tags("TOP500 or ベス10,上場企業,メーカー") == [
        "TOP500 or ベス10",
        "上場企業",
        "メーカー",
    ]
    # 全角カンマ・前後スペースの trim
    assert _mod.client_tier_tags("メーカー, その他， 上場企業") == [
        "メーカー",
        "その他",
        "上場企業",
    ]
    # 読点内包値とカンマ多選択の共存
    assert _mod.client_tier_tags("TOP500 or ベス10, 官公庁、自治体") == [
        "TOP500 or ベス10",
        "官公庁・自治体",
    ]
    # プレースホルダ '-' / 空は付与しない・順序保持で重複除去
    assert _mod.client_tier_tags("-") == []
    assert _mod.client_tier_tags("") == []
    assert _mod.client_tier_tags("メーカー,メーカー") == ["メーカー"]


def test_product_tags_splits_and_fixes_typo() -> None:
    """提案プロダクト: ASCIIカンマ分割・InsigtFinder→InsightFinder のみ修正・読点は割らない。"""
    assert _mod.product_tags("ビデオリリース,タテガタ") == ["ビデオリリース", "タテガタ"]
    # 括弧内の読点は値の一部（割らない）
    assert _mod.product_tags("ショート動画提案（UGCや切り抜き、メディア）") == [
        "ショート動画提案（UGCや切り抜き、メディア）"
    ]
    # タイポ修正（他は素通り）・その他 も付与する
    assert _mod.product_tags("InsigtFinder") == ["InsightFinder"]
    assert _mod.product_tags("その他") == ["その他"]
    assert _mod.product_tags("") == []
    # 順序ゆれは集合として扱いたいが配列は入力順を保持（JS 側はタグ集計で吸収）
    assert _mod.product_tags("タテガタ, ビデオリリース") == ["タテガタ", "ビデオリリース"]


def test_method_tags_deterministic_keywords() -> None:
    """施策手法: 誤爆しにくいキーワードで決定論付与・NFKC+大文字化で全角/小文字を吸収・複数可。"""
    assert _mod.method_tags("切り抜き施策のご提案") == ["切り抜き・TTO"]
    assert _mod.method_tags("TTOで二次利用") == ["切り抜き・TTO"]
    assert _mod.method_tags("タテガタ縦型ショート") == ["縦型・タテガタ"]
    assert _mod.method_tags("インフルエンサーキャスティング") == ["インフルエンサー"]
    assert _mod.method_tags("KOL起用") == ["インフルエンサー"]
    assert _mod.method_tags("UGC活用") == ["UGC"]  # 全角/小文字も NFKC+upper で吸収
    assert _mod.method_tags("ｕｇｃ二次利用") == ["UGC"]
    assert _mod.method_tags("ビデオリリース配信") == ["ビデオリリース"]
    assert _mod.method_tags("VSEO・指名検索対策") == ["VSEO"]
    assert _mod.method_tags("ライブコマース実施") == ["ライブ配信"]
    # 複数付与（定義順）
    assert _mod.method_tags("切り抜きとUGCの提案") == ["切り抜き・TTO", "UGC"]
    # 非該当は空
    assert _mod.method_tags("通常の提案書です") == []
    assert _mod.method_tags("") == []


def test_agency_flag_boolean_only() -> None:
    """代理店/あり: 本文に「代理店」を含めば True（名前は取らない）。"""
    assert _mod.agency_flag("代理店：博報堂 経由の案件") is True
    assert _mod.agency_flag("エンド直販の提案") is False
    assert _mod.agency_flag("") is False


def _write_meta_doc(
    vault: Path, stem: str, category: str, tier: str, product: str, body: str
) -> None:
    """ナレッジ共有メタ frontmatter（category/client_tier/product）+ 本文を持つ資料 note。"""
    (vault / "docs" / f"{stem}.md").write_text(
        f'---\ntitle: "{stem}"\nclient: "出光興産"\nindustry: "エネルギー"\n'
        f'doc_type: "提案書"\nsolution: ""\nmodified_at: "2026-06-01"\n'
        f'category: "{category}"\nclient_tier: "{tier}"\nproduct: "{product}"\n---\n\n'
        f"> {body}\n\n[[clients/出光興産]]\n",
        encoding="utf-8",
    )


def test_knowledge_meta_tags_payload_and_js_wiring(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """カテゴリ/クライアント種別(多値・読点非分割)/提案プロダクト/施策手法/代理店 の payload と JS 配線。"""
    _write_meta_doc(
        vault,
        "エスタック_打ち返し資料",
        category="提案,クロージング",  # カテゴリも複数選択があり得る（Codex #215-カテゴリ複数値）
        tier="TOP500 or ベス10,官公庁、自治体",  # 読点内包値の共存
        product="ショート動画提案（UGCや切り抜き、メディア）,InsigtFinder",  # 読点内包+タイポ
        body="TikTok向けUGCと切り抜き施策。代理店経由での実施を想定。",
    )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")

    # payload: カテゴリも多値分割 / 多値は Python 側で分割・正規化済み配列 / 施策手法 / 代理店 bool
    assert '"category": ["提案", "クロージング"]' in html
    # 読点で割らず 官公庁・自治体 を1値化（誤分割回帰の防止）
    assert '"client_tier": ["TOP500 or ベス10", "官公庁・自治体"]' in html
    # タイポ InsigtFinder→InsightFinder / 括弧内読点は値の一部
    assert '"product": ["ショート動画提案（UGCや切り抜き、メディア）", "InsightFinder"]' in html
    # 施策手法は title+excerpt+product+category+本文 から決定論抽出（UGC・切り抜き）
    assert '"UGC"' in html and '"切り抜き・TTO"' in html
    assert '"agency": true' in html

    # JS docTags 配線（配列を素通しでタグ化・分割は Python 側）
    assert '(d.client_tier||[]).forEach(v=>t.push("クライアント種別/"+v))' in html
    assert '(d.product||[]).forEach(v=>t.push("提案プロダクト/"+v))' in html
    assert '(d.method||[]).forEach(v=>t.push("施策手法/"+v))' in html
    assert '(d.category||[]).forEach(v=>t.push("カテゴリ/"+v))' in html
    assert 'if(d.agency)t.push("代理店/あり")' in html
    # CATMETA 系統色 + TAGORDER（資料のタグ群・取引先群 slice(0,7) は不変）
    for cat in ("カテゴリ", "クライアント種別", "提案プロダクト", "施策手法", "代理店"):
        assert f'"{cat}":"#' in html, cat
    assert "TAGORDER.slice(0,7)" in html and "TAGORDER.slice(7)" in html


# ---------------- タグ第1弾: 温度感/・宿題/（クライアント） ----------------


def _tl1(pos: str, neg: str, next_: str = "", d: str = "2026-06-01") -> list[dict]:
    """温度感/宿題テスト用の最小 tl（parse_fb_events 出力と同形・先頭が最新）。"""
    return [
        {
            "d": d,
            "src": "Slack",
            "ph": "",
            "bant": "",
            "menu": "",
            "pos": pos,
            "neg": neg,
            "next": next_,
        }
    ]


def test_temperature_tag_four_branches_and_nashi() -> None:
    """4分岐: 高（neg空/特になし系）／ポジ優勢／ネガ優勢／拮抗。tl 空はタグなし。"""
    assert _mod.temperature_tag(_tl1("よい", "")) == "高"
    assert _mod.temperature_tag(_tl1("よい", "特になし")) == "高"
    assert _mod.temperature_tag(_tl1("よい", "・なし")) == "高"
    assert _mod.temperature_tag(_tl1("よい", "-")) == "高"
    assert _mod.temperature_tag(_tl1("あ" * 10, "い" * 5)) == "ポジ優勢"
    assert _mod.temperature_tag(_tl1("あ" * 4, "い" * 8)) == "ネガ優勢"
    assert _mod.temperature_tag(_tl1("あ" * 6, "い" * 5)) == "拮抗"
    assert _mod.temperature_tag(_tl1("", "予算が合わない")) == "ネガ優勢"  # pos 空は neg 側
    assert _mod.temperature_tag([]) == ""


def test_temperature_tag_uses_latest_event_only() -> None:
    """判定は先頭（最新）イベントのみ。過去の「特になし」に引きずられない。"""
    tl = _tl1("あ" * 3, "い" * 8, d="2026-06-01") + _tl1("よい", "特になし", d="2026-05-01")
    assert _mod.temperature_tag(tl) == "ネガ優勢"


def test_next_action_first_40_chars_of_latest_event() -> None:
    tl = _tl1("", "", next_="あ" * 100) + _tl1("", "", next_="古い方", d="2026-05-01")
    assert _mod.next_action(tl) == "あ" * 40
    assert _mod.next_action([]) == ""
    assert _mod.next_action(_tl1("", "", next_="")) == ""


def test_homework_flag_gating() -> None:
    """次アクションあり×最終接点が新しい→付かない。31日超過去 or 日付なし→付く。"""
    today = date(2026, 7, 11)
    assert _mod.homework_flag("与件獲得", "2026-07-01", today) is False  # 10日前 → 追えている
    assert _mod.homework_flag("与件獲得", "2026-06-10", today) is False  # ちょうど31日 → 付けない
    assert _mod.homework_flag("与件獲得", "2026-06-09", today) is True  # 32日前 → 放置
    assert _mod.homework_flag("与件獲得", "", today) is True  # 日付なし
    assert _mod.homework_flag("与件獲得", "invalid-date", today) is True  # 不正日付は日付なし扱い
    assert _mod.homework_flag("", "2026-01-01", today) is False  # 次アクションなしなら常に付けない


# ---------------- タグ第1弾: payload / JS 配線（統合） ----------------


def _write_client_links(vault: Path, name: str, doc_stems: list[str]) -> None:
    """FB なし・関連資料リンクだけ持つ最小クライアント（横断/ カウント用）。"""
    links = "\n".join(f"- [[docs/{s}]]" for s in doc_stems)
    (vault / "clients" / f"{name}.md").write_text(
        f'---\nclient: "{name}"\nindustry: "メーカー"\ndeal_phase: ""\nbant_score: ""\n'
        f"fb_count: 0\ndoc_count: {len(doc_stems)}\n---\n\n# {name}\n\n## 関連資料\n{links}\n",
        encoding="utf-8",
    )


def _doc_field(html: str, stem: str, key: str) -> str | None:
    """payload JSON から stem の doc オブジェクト内フィールド値を取り出す（テスト用）。"""
    m = re.search(f'"stem": "{re.escape(stem)}".*?"{key}": "([^"]*)"', html)
    return m.group(1) if m else None


def test_cross_reference_tag_counts_distinct_clients(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """横断/: 2社→2社タグ・3社→3社以上・1社のみは付かない（wikilink 逆引き）。"""
    # fixture の出光興産は 提案書A のみ参照。帝人・花王を追加して参照網を作る
    _write_client_links(vault, "帝人", ["提案書A", "議事録C"])
    _write_client_links(vault, "花王", ["提案書A", "議事録C"])
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert _doc_field(html, "提案書A", "xc") == "3社以上"  # 出光興産+帝人+花王
    assert _doc_field(html, "議事録C", "xc") == "2社"  # 帝人+花王
    assert _doc_field(html, "提案書B", "xc") == ""  # 誰からもリンクされない → 付かない


def test_doc_tag_fields_in_payload(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """媒体/動画形式/形式が doc payload に載る（title+excerpt 判定・stem 拡張子判定）。"""
    _write_doc(vault, "TikTok縦型企画案.pptx")
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert '"media": ["TikTok"]' in html
    assert '"vfmt": ["ショート"]' in html
    assert _doc_field(html, "TikTok縦型企画案.pptx", "fmt") == "PPTX"
    assert _doc_field(html, "提案書A", "fmt") == ""  # 拡張子なし stem はタグなし


def test_client_tag_fields_and_js_wiring(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """温度感/宿題/次アクションが client payload に載り、JS 配線・テーブル列が生成される。"""
    (vault / "clients" / "帝人.md").write_text(
        '---\nclient: "帝人"\nindustry: "メーカー"\ndeal_phase: "ヒアリング"\n'
        'bant_score: "B（前向き）"\nfb_count: 1\ndoc_count: 0\n---\n\n# 帝人\n\n'
        "## 営業FB時系列（新しい順）\n\n"
        "### ---- #proj-ショート動画_営業フィードバック情報 1779101519.347119\n\n"
        "- フェーズ: ヒアリング\n- ポジ反応: ・保証型がよい\n- ネガ反応: 特になし\n"
        "- 次アクション: 与件獲得に向けた提案書再提出\n\n"
        "## 関連資料\n",
        encoding="utf-8",
    )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    # 帝人: 最終接点 2026-05-18（31日超過去）× 次アクションあり → temp/nx/hw
    assert '"temp": "高"' in html
    assert '"nx": "与件獲得に向けた提案書再提出"' in html
    assert '"hw": 1' in html
    # 出光興産: FB時系列見出しなし（tl 空）→ 温度感なし・宿題なし
    assert re.search(r'"stem": "出光興産".*?"temp": ""', html)
    assert re.search(r'"stem": "出光興産".*?"hw": 0', html)
    # JS 配線: 新タグは docTags/clientTags のみ（グラフ用 _ctags/_dtags には載せない）
    for token in (
        '"温度感/"+c.temp',
        '"宿題/あり"',
        '"媒体/"+m',
        '"動画形式/"+v',
        '"形式/"+d.fmt',
        '"横断/"+d.xc',
    ):
        assert token in html, token
    # テーブル「次アクション」列（TCOLS + 行セル）
    assert '["nx","次アクション",c=>c.nx||"",0]' in html
    assert "c.nx?esc(c.nx)" in html


def test_homework_tag_not_set_when_last_contact_fresh(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """宿題ゲーティング: 次アクションありでも最終接点が新しければ hw=0。"""
    fresh = date.today().strftime("%Y-%m-%d")
    (vault / "clients" / "花王.md").write_text(
        '---\nclient: "花王"\nindustry: "日用品"\ndeal_phase: ""\nbant_score: ""\n'
        "fb_count: 1\ndoc_count: 1\n---\n\n# 花王\n\n"
        "## 営業FB時系列（新しい順）\n\n"
        "### ---- #proj-ch 1779101519.347119\n\n"
        "- ポジ反応: ・よい\n- 次アクション: 見積提出\n\n"
        "## 関連資料\n- [[docs/花王向け資料]]\n",
        encoding="utf-8",
    )
    # 今日更新の関連資料 → last が今日になり 31 日以内 → 宿題は付かない
    (vault / "docs" / "花王向け資料.md").write_text(
        f'---\ntitle: "花王向け資料"\nclient: "花王"\nindustry: "日用品"\n'
        f'doc_type: "提案書"\nsolution: ""\nmodified_at: "{fresh}"\n---\n\n> 抜粋\n',
        encoding="utf-8",
    )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    m = re.search(r'"stem": "花王".*?"nx": "([^"]*)".*?"hw": (\d)', html)
    assert m is not None
    assert m.group(1) == "見積提出"
    assert m.group(2) == "0"


# ---------------- 管理用レポートの既定非搭載 ----------------


def test_reports_excluded_by_default(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """_reports/ が実在しても既定では payload に載らない（16名向け画面から管理物を排除）。"""
    rdir = vault / "_reports"
    rdir.mkdir()
    (rdir / "followup_gaps.md").write_text("# レポート\n\n中身", encoding="utf-8")
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert '"reports": []' in html
    # payload に report エントリが無いこと（JS ソース内の openReport("...") リテラルは残ってよい）
    assert '"stem": "followup_gaps"' not in html


def test_include_reports_is_rejected_for_public_build(
    sidecars: Path,
    vault: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """manifest管理外の内部reportを明示flagでも公開HTMLへ混ぜない。"""
    rdir = vault / "_reports"
    rdir.mkdir()
    (rdir / "followup_gaps.md").write_text("# レポート\n\n中身", encoding="utf-8")
    out = tmp_path / "o.html"
    with pytest.raises(SystemExit):
        _run(vault, out, "--include-reports")
    assert "ACL manifest外" in capsys.readouterr().err
    assert not out.exists()


# ---------------- タグUX改善（審査済み6機能）: 生成 HTML への配線 ----------------


@pytest.fixture()
def ux_html(sidecars: Path, vault: Path, tmp_path: Path) -> str:
    """タグUX検証用に1回だけ build した HTML（機能別テストで共有）。"""
    out = tmp_path / "ux.html"
    assert _run(vault, out) == 0
    return out.read_text(encoding="utf-8")


def test_ux1_chip_foundation_and_runquery(ux_html: str) -> None:
    """機能1: CATMETA 14系統・chipHtml 部品・runQuery 集約・ダークテーマ hover 修正。"""
    html = ux_html
    # 系統色辞書: 14系統すべて定義（色は既存パレット hex）
    m = re.search(r"const CATMETA=\{([^}]*)\}", html)
    assert m is not None
    for cat in (
        "宿題",
        "温度感",
        "フェーズ",
        "BANT",
        "業種",
        "最終接点",
        "資料種別",
        "施策",
        "媒体",
        "動画形式",
        "形式",
        "横断",
        "更新",
        "情報源",
    ):
        assert f'"{cat}":"#' in m.group(1), cat
    # チップ部品: 文字色はCSS変数のみ（--muted/--text）・7pxドット・hoverで系統色枠
    assert "function chipHtml(tag,mode)" in html
    assert ".tagchip .tck{color:var(--muted)" in html
    assert ".tagchip .tcv{color:var(--text)" in html
    assert ".tcdot{display:inline-block;width:7px;height:7px" in html
    assert ".tagchip:hover{border-color:var(--cc2,var(--accent))}" in html
    # runQuery 集約: 4重複コードが単一路に（タグペイン/本文タグ/右パネル・グラフ=tagJump）
    assert "function runQuery(q)" in html
    assert "function tagJump(t){runQuery(tagQ(t));}" in html
    assert "onclick:()=>runQuery(tagQ(full))" in html
    assert (
        '.tg,.ntag,.note-tags .tagchip").forEach(el=>el.onclick=()=>runQuery(tagQ(el.dataset.tag)))'
        in html
    )
    # 空白入りタグ値は tag:"値" で発行（引用符対応）
    assert "function tagQ(t){return /\\s/.test(t)?'tag:\"'+t+'\"':\"tag:\"+t;}" in html
    # ダークテーマバグ修正: .wcard:hover のハードコード #f0f0f3 廃止
    assert "#f0f0f3" not in html
    assert ".wcard:hover{border-color:var(--border-focus);background:var(--hover)}" in html


def test_ux2_active_filter_chips_and_qhelp(ux_html: str) -> None:
    """機能2: ×付きフィルタチップ・すべて解除・0件回復・qhelp実例・debounce。"""
    html = ux_html
    # parseQuery と同一 regex の表示用トークナイザ + 完全一致除去（fail-safe）
    assert "function qTokens(q)" in html
    assert "function rmToken(val,raw)" in html
    assert 'data-rm="' in html
    assert "すべて解除" in html
    assert "×で条件を外してください" in html  # 0件時の回復導線
    # qhelp: 実データ由来のクリック実行例 + 従来リファレンス
    assert "function qhelpHtml()" in html
    assert "例（クリックで実行）" in html
    assert "tag:業種/食品" in html  # 構文リファレンスは維持
    # 120ms debounce（__lastQ は即時更新）
    assert "dt=setTimeout(()=>runSearchPane(inp.value,out),120)" in html
    # parseQuery/matchItem は不改造（検索コアの回帰担保・文字列レベル）
    assert (
        "function parseQuery(q){const P={plain:[],neg:[],tag:[],path:[],file:[],ntag:[],npath:[],nfile:[],props:[],regex:[]};"
        in html
    )
    assert "for(const t of P.tag)if(!tagMatch(it.tags,t))return false;" in html


def test_ux3_chips_on_results_note_rightpanel(ux_html: str) -> None:
    """機能3: 結果行チップ=AND追加（行openと分離）・カルテ note-tags・右パネルチップ化。"""
    html = ux_html
    # 結果行: 先頭3個+「+n」・stopPropagation で行クリックと分離・AND追記は結果行のみ
    assert 'it.tags.slice(0,3).map(t=>chipHtml(t,"and"))' in html
    assert (
        '".sr .tagchip").forEach(ch=>ch.onclick=e=>{e.stopPropagation();addTagToQuery(ch.dataset.tag);})'
        in html
    )
    assert "function addTagToQuery(tag)" in html
    assert "クリックで絞り込みに追加" in html
    # カルテ: 未使用だった .note-tags を実際に描画（クリック=置換は afterOpen でバインド）
    assert '\'<div class="note-tags">\'+ctg.map(t=>chipHtml(t)).join("")' in html
    assert '\'<div class="note-tags">\'+dtg.map(t=>chipHtml(t)).join("")' in html
    # 右パネル tags タブ: 素テキスト #タグ をチップに置換（tagJump のまま）
    assert '<div class="rtagwrap">' in html
    assert (
        'b.querySelectorAll(".tagchip").forEach(el=>el.onclick=()=>tagJump(el.dataset.tag))' in html
    )


def test_ux4_tag_pane_enhancements(ux_html: str) -> None:
    """機能4: 意味順+見出し・系統色・ペイン内絞り込み・件数/五十音トグル・12超畳み・active。"""
    html = ux_html
    assert (
        'const TAGORDER=["宿題","温度感","担当","フェーズ","BANT","業種","最終接点","資料種別","カテゴリ","クライアント種別","提案プロダクト","施策","施策手法","代理店","関係先","媒体","動画形式","形式","横断","更新","情報源"]'
        in html
    )
    # 先頭7=取引先のタグ（宿題〜最終接点）は不変・ナレッジ共有メタ4軸は資料のタグ群（index≥7）へ
    assert (
        '"最終接点","資料種別","カテゴリ"' in html
    )  # 資料群の先頭に挿入（取引先群 slice(0,7) を侵さない）
    assert "取引先のタグ" in html and "資料のタグ" in html
    assert "タグを絞り込み…" in html
    assert "tagSortAlpha" in html and "件数順⇄五十音順" in html
    assert '"他 "+(vk.length-12)+" 件を表示"' in html  # 葉12超の畳み
    assert "act.some(a=>tagMatch([t],a))" in html  # __lastQ と tagMatch 照合で .active
    assert "window.__tagFocus" in html  # ホームのテーザー経由初期展開


def test_ux5_welcome_quickfilter_and_teaser(ux_html: str) -> None:
    """機能5: クイックフィルタ（動的存在チェック・件数バッジ）+ タグ系統テーザー。"""
    html = ux_html
    assert "クイックフィルタ" in html and "タグで探す" in html
    # プリセットは1箇所定義 + tagCount による動的存在チェック（0件は非表示）
    assert 'const QF=[["宿題あり","宿題/あり"]' in html
    assert "QF.filter(([l,t])=>tagCount[t]>0).slice(0,8)" in html
    # クリック=runQuery 置換 / テーザー=タグペインを該当系統展開で開く
    assert '".qfc").forEach(el=>el.onclick=()=>runQuery(el.dataset.q))' in html
    assert 'window.__tagFocus=el.dataset.c;setPane("tags");' in html


def test_ux6_table_temp_and_homework_filters(ux_html: str) -> None:
    """機能6: 温度感セレクト+宿題チェック（tblFilter 2本追加のみ・TCOLS/ソート不介入）。"""
    html = ux_html
    assert 'tblFilter={q:"",ind:"",phase:"",temp:"",hw:false}' in html
    assert "if(tblFilter.temp)rows=rows.filter(c=>c.temp===tblFilter.temp);" in html
    assert "if(tblFilter.hw)rows=rows.filter(c=>c.hw);" in html
    # 温度感4値は実値集合から生成（ラベルはタグペインと同一文字列）+ 宿題チェック
    assert '["高","ポジ優勢","拮抗","ネガ優勢"].filter(t=>DATA.clients.some(c=>c.temp===t))' in html
    assert "温度感（すべて）" in html and "宿題ありのみ" in html
    # 0件時メッセージ + 適用中チップ（native select の補完）
    assert "条件に一致する取引先がありません — フィルタを1つ外してください" in html
    assert 'chipHtml("温度感/"+tblFilter.temp)' in html
    # 既存挙動の不介入: TCOLS・列定義は従来のまま（次アクション列まで9列）
    assert '["nx","次アクション",c=>c.nx||"",0]' in html
    # 行クリックはデータ行のみ（0件メッセージ行に openClient を配線しない）
    assert (
        'tb.querySelectorAll("tr[data-s]").forEach(tr=>tr.onclick=()=>openClient(tr.dataset.s))'
        in html
    )


def test_ux5_quickfilter_counts_in_stats(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """機能5: stats.json に QF プリセット8件の件数が焼き込まれる（回帰検知）。"""
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    qf = stats["qf"]
    assert set(qf.keys()) == set(_mod.QF_PRESET_TAGS)
    # ダミー Vault: 資料3件は全て doc_type=提案書 / 宿題・温度感・横断等の該当なし
    assert qf["資料種別/提案書"] == 3
    assert qf["宿題/あり"] == 0
    assert qf["横断"] == 0


def test_quickfilter_counts_unit() -> None:
    """_quickfilter_counts: tagMatch 同等の「子タグも一致」判定と ageBucket ミラー。"""
    today = date(2026, 7, 12)
    clients = [
        {
            "industry": "IT",
            "phase": "提案",
            "bantg": "B",
            "last": "2026-07-01",
            "temp": "ネガ優勢",
            "hw": 1,
        },
        {"industry": "", "phase": "", "bantg": "", "last": "2024-01-01", "temp": "", "hw": 0},
    ]
    docs = [
        {
            "doc_type": "提案書",
            "industry": "IT",
            "solution": "動画広告",
            "modified": "2026-07-05",
            "src": "Drive",
            "media": ["TikTok"],
            "vfmt": ["ショート"],
            "fmt": "PPTX",
            "xc": "2社",
        },
    ]
    qf = _mod._quickfilter_counts(clients, docs, today)
    assert qf["宿題/あり"] == 1
    assert qf["温度感/ネガ優勢"] == 1
    assert qf["温度感/高"] == 0
    assert qf["最終接点/1年以上前"] == 1  # 2024-01-01 → 366日超
    assert qf["更新/1ヶ月以内"] == 1
    assert qf["横断"] == 1  # 横断/2社 が親 preset「横断」に一致（tagMatch 同等）
    assert qf["動画形式/ショート"] == 1
    assert qf["資料種別/提案書"] == 1


def test_age_bucket_mirror_boundaries() -> None:
    """_age_bucket: JS ageBucket と同じ 31/92/183/366 閾値・不正/空は空文字。"""
    t = date(2026, 7, 12)
    ab = _mod._age_bucket
    assert ab("2026-07-12", t) == "1ヶ月以内"
    assert ab("2026-06-11", t) == "1ヶ月以内"  # 31日前
    assert ab("2026-06-10", t) == "3ヶ月以内"  # 32日前
    assert ab("2026-04-11", t) == "3ヶ月以内"  # 92日前
    assert ab("2026-01-10", t) == "半年以内"  # 183日前
    assert ab("2025-07-11", t) == "1年以内"  # 366日前
    assert ab("2025-07-10", t) == "1年以上前"  # 367日前
    assert ab("", t) == ""
    assert ab("invalid", t) == ""


# ---------------- 商談ジャーニー可視化（カルテのバー / ホームのパイプライン / 動線修理） ----------------


def test_journey_phasesteps_matches_phasecolor_keys(ux_html: str) -> None:
    """順序定義 PHASESTEPS は PHASECOLOR キーと完全一致（+失注のみ順序外）。片側だけの語彙変更を検知する回帰テスト。"""
    m = re.search(r"const PHASESTEPS=\[([^\]]*)\]", ux_html)
    assert m is not None
    steps = re.findall(r'"([^"]+)"', m.group(1))
    # ケイパが先頭（実データの時系列遷移はケイパ→ヒアリング方向・ユーザー確認済みの順序）
    assert steps == [
        "ケイパ",
        "ヒアリング",
        "1回目提案",
        "2回目以降提案",
        "最終交渉",
        "成約（口頭内示以上）",
    ]
    mc = re.search(r"const PHASECOLOR=\{([^}]*)\}", ux_html)
    assert mc is not None
    color_keys = set(re.findall(r'"([^"]+)":"#', mc.group(1)))
    assert set(steps) | {"失注"} == color_keys


def test_journey_bar_wiring_and_guard(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """a案: jbSection がカルテ（props と tl の間）に配線され、ph+dated FB 持ちクライアントの
    payload が載る。フェーズ未設定かつ dated ph 無しはバー非表示のガード付き。"""
    (vault / "clients" / "帝人.md").write_text(
        '---\nclient: "帝人"\nindustry: "メーカー"\ndeal_phase: "ヒアリング"\n'
        'bant_score: "B（前向き）"\nfb_count: 1\ndoc_count: 0\n---\n\n# 帝人\n\n'
        "## 営業FB時系列（新しい順）\n\n"
        "### ---- #proj-ショート動画_営業フィードバック情報 1779101519.347119\n\n"
        "- フェーズ: ヒアリング\n- BANT: B（前向き）\n\n"
        "## 関連資料\n",
        encoding="utf-8",
    )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    # バー描画関数と配線（props の直後・タイムラインの直前）
    assert "function jbSection(c)" in html and "function phaseDates(c)" in html
    assert "\n  +jbSection(c)\n  +tlSection(c)" in html
    # ph 持ちクライアントで発火する材料（frontmatter phase + dated ph イベント）が payload に載る
    assert '"ph": "ヒアリング"' in html and '"d": "2026-05-18"' in html
    # データ無しクライアントはバー非表示（データが無い演出をしない）・失注は終端 err 差し替え
    assert 'if(!lost&&idx<0&&!PHASESTEPS.some(p=>dates[p]))return "";' in html
    assert "jblost" in html and "後退あり" in html
    # 短縮ラベル（title に正式名）
    assert '"1回目提案":"提案①"' in html and '"成約（口頭内示以上）":"成約"' in html


def test_journey_pipeline_section_on_welcome(ux_html: str) -> None:
    """b案: ホームのクイックフィルタ直下にパイプライン節。クリックは tblFilter を全リセット+
    フェーズのみ設定してテーブル着地（select 復元と件数一致）。未設定 N は muted 非クリック。"""
    html = ux_html
    assert '<div class="wsec">パイプライン</div>' in html
    assert '<span class="plarr">→</span>' in html  # PHASESTEPS 順の→区切り
    assert 'tblFilter={q:"",ind:"",phase:el.dataset.p,temp:"",hw:false};tableView();' in html
    assert "未設定 '+unset+'" in html  # 空フェーズはテーブル filter で表現できないため非クリック
    assert (
        'class="tagchip pld"' in html
    )  # 0社フェーズは非クリック（select に無い値を選択状態にしない）


def test_journey_flow_repairs_homework_and_back(ux_html: str) -> None:
    """動線修理: 宿題ありチップだけテーブル着地（qft・hwのみON）。他QFチップの検索着地は不変。
    table 疑似ビューは履歴に積まれ「戻る」で tableView() 復元（tblFilter はモジュール変数で残存）。"""
    html = ux_html
    assert 'const tb=l==="宿題あり"' in html
    assert 'tblFilter={q:"",ind:"",phase:"",temp:"",hw:true};tableView();' in html
    assert '".qfc").forEach(el=>el.onclick=()=>runQuery(el.dataset.q))' in html  # 既存着地は不変
    assert 'pushHist("table","")' in html
    assert 'k==="table"?tableView():' in html
    # グラフ等他ビューの履歴挙動は変えない（openGraph は pushHist しない）
    assert 'pushHist("graph"' not in html


# ---------------- 表示名寄せ（tag_alias / client_alias・任意適用・可逆） ----------------


def _write_aliases(
    sidecar_dir: Path, *, tag: dict | None = None, client: dict | None = None
) -> None:
    """名寄せサイドカーを SIDECAR_DIR（monkeypatch 済 tmp）へ置く。既定 fixture には無い＝素通り。"""
    if tag is not None:
        (sidecar_dir / "tag_alias.json").write_text(
            json.dumps(tag, ensure_ascii=False), encoding="utf-8"
        )
    if client is not None:
        (sidecar_dir / "client_alias.json").write_text(
            json.dumps(client, ensure_ascii=False), encoding="utf-8"
        )


def test_tag_alias_industry_applied_to_doc_and_client(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """業種 variant（メディア・エンタメ）が doc/client の industry で canonical（メディア）へ正規化される。"""
    _write_aliases(sidecars, tag={"industry": {"メディア・エンタメ": "メディア"}, "solution": {}})
    (vault / "docs" / "番組資料.md").write_text(
        '---\ntitle: "番組資料"\nclient: "テレ朝"\nindustry: "メディア・エンタメ"\n'
        'doc_type: "提案書"\nsolution: ""\nmodified_at: "2026-06-01"\n---\n\n> 抜粋\n',
        encoding="utf-8",
    )
    (vault / "clients" / "テレ朝.md").write_text(
        '---\nclient: "テレ朝"\nindustry: "メディア・エンタメ"\ndeal_phase: ""\nbant_score: ""\n'
        "fb_count: 0\ndoc_count: 1\n---\n\n# テレ朝\n",
        encoding="utf-8",
    )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    # doc 側・client 側とも industry が canonical（=props/テーブル/タグ/検索hay へ一貫適用される元）
    assert _doc_field(html, "番組資料", "industry") == "メディア"
    assert re.search(r'"name": "テレ朝", "cnorm": "テレ朝", "industry": "メディア"', html)
    # variant 文字列は payload のどこにも残らない
    assert '"industry": "メディア・エンタメ"' not in html


def test_tag_alias_solution_applied_to_doc(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """施策 variant（動画制作）が doc の solution で canonical（動画広告）へ正規化される。"""
    _write_aliases(sidecars, tag={"industry": {}, "solution": {"動画制作": "動画広告"}})
    (vault / "docs" / "制作案.md").write_text(
        '---\ntitle: "制作案"\nclient: "出光興産"\nindustry: ""\n'
        'doc_type: "提案書"\nsolution: "動画制作"\nmodified_at: "2026-06-01"\n---\n\n> 抜粋\n',
        encoding="utf-8",
    )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert _doc_field(html, "制作案", "solution") == "動画広告"
    assert '"solution": "動画制作"' not in html


def test_solution_alias_field_canonical_but_vfmt_uses_raw(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """施策フィールドは正本化しつつ、動画形式(vfmt)判定は生の solution を使う（回帰防止の配線検証）。"""
    # 生 "縦型ショート企画" は VIDEO_FORMAT の「縦型」に一致→vfmt=ショート。canonical "動画広告" は非該当。
    _write_aliases(sidecars, tag={"industry": {}, "solution": {"縦型ショート企画": "動画広告"}})
    (
        vault / "docs" / "施策X案.md"
    ).write_text(  # title/excerpt は vfmt 非該当（solution 由来のみを見る）
        '---\ntitle: "施策X案"\nclient: "出光興産"\nindustry: ""\n'
        'doc_type: "提案書"\nsolution: "縦型ショート企画"\nmodified_at: "2026-06-01"\n---\n\n> 抜粋\n',
        encoding="utf-8",
    )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert _doc_field(html, "施策X案", "solution") == "動画広告"  # 施策フィールドは正本
    assert re.search(
        r'"stem": "施策X案".*?"vfmt": \["ショート"\]', html
    )  # vfmt は生値由来（正本では消える）


def test_client_alias_folds_variants_and_sums_fb(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """取引先 variant（SBI証券/SBI生命保険）が canonical 1枚へ畳まれ FB/資料が合算される。"""
    _write_aliases(
        sidecars,
        client={"client": {"SBI証券": "SBIホールディングス", "SBI生命保険": "SBIホールディングス"}},
    )
    for cname, fb, doc in (("SBI証券", 3, 2), ("SBI生命保険", 4, 1)):
        (vault / "clients" / f"{cname}.md").write_text(
            f'---\nclient: "{cname}"\nindustry: "金融"\ndeal_phase: ""\nbant_score: ""\n'
            f"fb_count: {fb}\ndoc_count: {doc}\n---\n\n# {cname}\n",
            encoding="utf-8",
        )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["clients"] == 2  # 出光興産 + SBI（2変種が1枚に統合）
    # 1枚のカードに fb=3+4=7 / doc=2+1=3 が合算・cnorm は canonical の norm
    assert re.search(
        r'"name": "SBIホールディングス", "cnorm": "sbiホールディングス"[^}]*"fb": 7, "doc": 3', html
    )
    assert '"name": "SBI証券"' not in html
    assert '"name": "SBI生命保険"' not in html


def test_client_alias_applied_to_doc_client_links_to_canonical(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """doc 側 client も正本化され cnorm が canonical と一致（doc→client リンク・関連資料の合流）。"""
    _write_aliases(sidecars, client={"client": {"SBI証券": "SBIホールディングス"}})
    (vault / "clients" / "SBIホールディングス.md").write_text(
        '---\nclient: "SBIホールディングス"\nindustry: "金融"\ndeal_phase: ""\nbant_score: ""\n'
        "fb_count: 0\ndoc_count: 1\n---\n\n# SBIホールディングス\n",
        encoding="utf-8",
    )
    (vault / "docs" / "証券資料.md").write_text(
        '---\ntitle: "証券資料"\nclient: "SBI証券"\nindustry: "金融"\n'
        'doc_type: "提案書"\nsolution: ""\nmodified_at: "2026-06-01"\n---\n\n> 抜粋\n',
        encoding="utf-8",
    )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert _doc_field(html, "証券資料", "client") == "SBIホールディングス"
    assert _doc_field(html, "証券資料", "cnorm") == "sbiホールディングス"
    assert '"client": "SBI証券"' not in html


def test_alias_sidecars_absent_pass_through(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """名寄せサイドカー欠落（既定 fixture）は素通り＝variant 値がそのまま残る（可逆・fail-loud にしない）。"""
    assert not (sidecars / "tag_alias.json").exists()
    assert not (sidecars / "client_alias.json").exists()
    (vault / "docs" / "番組資料.md").write_text(
        '---\ntitle: "番組資料"\nclient: "テレ朝"\nindustry: "メディア・エンタメ"\n'
        'doc_type: "提案書"\nsolution: "動画制作"\nmodified_at: "2026-06-01"\n---\n\n> 抜粋\n',
        encoding="utf-8",
    )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert _doc_field(html, "番組資料", "industry") == "メディア・エンタメ"
    assert _doc_field(html, "番組資料", "solution") == "動画制作"


def test_alias_canonical_and_unmapped_values_unchanged(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """canonical 自身・マップ未登録の値・未登録の取引先名はいずれも正規化されず素通り。"""
    _write_aliases(
        sidecars,
        tag={"industry": {"メディア・エンタメ": "メディア"}, "solution": {"動画制作": "動画広告"}},
        client={"client": {"SBI証券": "SBIホールディングス"}},
    )
    (vault / "docs" / "正規資料.md").write_text(
        '---\ntitle: "正規資料"\nclient: "自治体X"\nindustry: "メディア"\n'  # canonical 自身
        'doc_type: "提案書"\nsolution: "調査"\nmodified_at: "2026-06-01"\n---\n\n> 抜粋\n',  # unmapped
        encoding="utf-8",
    )
    (vault / "clients" / "自治体X.md").write_text(
        '---\nclient: "自治体X"\nindustry: "宇宙開発"\ndeal_phase: ""\nbant_score: ""\n'  # unmapped industry / unmapped client
        "fb_count: 1\ndoc_count: 1\n---\n\n# 自治体X\n",
        encoding="utf-8",
    )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert _doc_field(html, "正規資料", "industry") == "メディア"  # canonical 自身は不変
    assert _doc_field(html, "正規資料", "solution") == "調査"  # unmapped solution は素通り
    assert _doc_field(html, "正規資料", "client") == "自治体X"  # unmapped client は素通り
    assert re.search(
        r'"name": "自治体X", "cnorm": "自治体x", "industry": "宇宙開発"', html
    )  # unmapped industry 不変


# ---------------- まとめる軸（clustering axis）: 参照Map方式（ノード加算ゼロ） + グラフ JS 配線 ----------------


def _payload(html: str) -> dict:
    """生成 HTML の `const DATA=...;` 行から DATA ペイロード全体を取り出す。

    埋め込み時の < → \\u003c 等は JSON 合法な \\uXXXX エスケープなので json.loads で素直に読める。
    """
    line = next(ln for ln in html.splitlines() if ln.startswith("const DATA="))
    return json.loads(line[len("const DATA=") :].rstrip().rstrip(";"))


def _graph_nodes(html: str) -> list[dict]:
    return _payload(html)["graph"]["nodes"]


def test_graph_nodes_carry_no_cluster_fields(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """まとめ軸の値はノードへ再埋め込みしない＝どのグラフノードも last/doc_type/solution を持たない
    （参照Map方式でペイロード増ゼロ・グラフノードは baseline と同形）。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    nodes = _graph_nodes(out.read_text(encoding="utf-8"))
    assert nodes and all(
        "last" not in n and "doc_type" not in n and "solution" not in n for n in nodes
    )


def test_cluster_source_fields_live_in_payload_clients_docs(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """まとめ軸の元値は既存の DATA.clients/DATA.docs に載り、grpVal が stem で参照する。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    pl = _payload(out.read_text(encoding="utf-8"))
    c = next(c for c in pl["clients"] if c["name"] == "出光興産")
    assert c["stem"] and c["last"] == "2026-06-01"  # 最終接点=max(最終FB,資料 modified)
    d = next(d for d in pl["docs"] if d["stem"] == "提案書A")
    assert d["doc_type"] == "提案書" and d["solution"] == "動画広告"


def test_cluster_reference_map_wiring_in_js(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """grpVal は既存索引 cByStem/dByStem を stem（n.id.slice(2)）で引く＝ノード再埋め込みしない。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    # 既存の索引を流用（新規Mapは作らない）。素の {} ではなくプロトタイプ無しで作る
    # （stem="constructor" 等で Object.prototype のメンバを引かないため）。
    assert "const cByStem=Object.create(null),dByStem=Object.create(null)" in html
    assert "cByStem[n.id.slice(2)]" in html
    assert "dByStem[n.id.slice(2)]" in html


def test_graph_node_kinds_unchanged_by_cluster(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """ノード加算ゼロ＝ノード種は client/doc/tag のみ・件数は fixture 通り（新ノードを生まない）。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    nodes = _graph_nodes(out.read_text(encoding="utf-8"))
    assert {n["type"] for n in nodes} == {"client", "doc", "tag"}
    assert sum(1 for n in nodes if n["type"] == "client") == 1
    assert sum(1 for n in nodes if n["type"] == "doc") == 3


def test_cluster_opt_default_null_and_grp_helper(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """opt.cluster 既定 null（＝取引先ごと・現状維持）と grp() の opt.cluster ガード。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert "cluster:null" in html  # 既定=まとめOFF（開いた瞬間は現状不変）
    assert "function grp(n){return opt.cluster?grpVal(n,opt.cluster):null;}" in html


def test_grp_client_and_doc_basis_branches_in_js(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """grp 基準分岐: phase/last=client基準・doc_type/solution=doc基準、空は未設定/記録なしへ。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert 'return n.type==="client"?(n.phase||"未設定"):null;' in html  # phase はノード既存
    assert (
        'ageBucket((cByStem[n.id.slice(2)]||{}).last)||"記録なし"' in html
    )  # last は DATA.clients 参照
    assert '(dByStem[n.id.slice(2)]||{}).doc_type||"未設定"' in html  # doc_type は DATA.docs 参照
    assert '(dByStem[n.id.slice(2)]||{}).solution||"未設定"' in html  # solution は DATA.docs 参照


def test_build_centers_ring_placement_js(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """buildCenters: 実在値のみ列挙→リング配置（角度=i/総数*2π・半径=CLBASE*√N・未設定末尾）。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert "function buildCenters(ax)" in html
    assert "i/tot*Math.PI*2" in html  # 角度 = index/総数 * 2π
    assert "CLBASE*Math.sqrt(N.length)" in html  # 半径 = base*√(全ノード数)＝絞り込みで島が動かない
    assert "Object.keys(PHASECOLOR).filter" in html  # フェーズは PHASECOLOR キー順
    assert 'ax==="last"' in html and "AGEBK.filter" in html  # 最終接点は新→旧順
    assert '["未設定","記録なし"].forEach' in html  # 未設定/記録なし島は必ず末尾


def test_cluster_counts_use_visible_set_only(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """件数/ラベル/fit は可視ノードのみ。ただし**幾何は全ノードから1回**＝絞り込みで島が動かない。

    Codex P2 の要求は「件数・ラベル・fit」。半径や島順まで可視連動にすると 1 キーストロークで
    島が数百px移動する体験回帰になるため、幾何は固定し recount() で件数だけ更新する。
    """
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    # 幾何は全ノード走査（vis で間引かない）＝島の順序/角度/半径が絞り込みで動かない
    assert "for(let i=0;i<N.length;i++){const g=grpVal(N[i],ax);" in html
    # 件数だけ可視基準
    assert "function recount(){" in html
    assert "for(let i=0;i<N.length;i++){if(!vis(i))continue;const g=grpBin(N[i]);" in html
    assert "if(g!==null&&cCenters[g])cCenters[g].n++;" in html
    assert "recount();}" in html  # buildCenters の末尾で初回カウント
    # fit は可視のみの外接矩形。成功可否を返し、可視0件なら現画角維持
    assert "function fit(){let a=1e9,b=1e9,c=-1e9,d=-1e9,any=false;" in html
    assert "N.forEach((n,i)=>{if(!vis(i))return;any=true;" in html
    assert "if(!any)return false;" in html and "return true;}" in html
    assert "if(fitPending&&fit())fitPending=false;" in html  # 空振り fit で再フィットを消費しない
    # 可視変化は件数のみ更新（リヒートしない＝島が飛ばない）
    assert (
        'const visChanged=()=>{if(opt.cluster){recount();$("#gClusterCap").innerHTML=clusterCap();}dr();};'
        in html
    )
    # 可視を変える4コントロールが**実際に**visChanged を呼ぶ（"どこかに文字列がある"では
    # gTags だけ dr() に戻す回帰を素通ししてしまうため、ハンドラ全文で固定する）
    for ctl, ev, prop in (
        ("gFilter", "oninput", "opt.filter=e.target.value.toLowerCase()"),
        ("gTags", "onchange", "opt.showTags=e.target.checked"),
        ("gDocs", "onchange", "opt.showDocs=e.target.checked"),
        ("gOrph", "onchange", "opt.hideOrphan=e.target.checked"),
    ):
        assert f'$("#{ctl}").{ev}=e=>{{{prop};visChanged();}};' in html
    # 可視0件の島はラベルを出さない
    assert "for(const v in cCenters){const c=cCenters[v];if(!c.n)continue;" in html


def test_cluster_island_count_is_capped(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """自由記述軸(資料種類/施策)の島数に上限（表記ゆれで島が無限増殖しラベルが重なるのを防ぐ）。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert "const CLMAX=12;" in html
    assert 'const COTHER="その他";' in html
    # 溢れは「その他」島へ集約（無言で消さない）。挙動の固定は test_graph_cluster_js.py が JS 実行で行う
    assert "vals=rest.slice(0,CLMAX-1);" in html
    assert "clOther=new Set(over);" in html


def test_cluster_caption_admits_when_all_islands_empty(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """全島が空（対象typeを非表示にした等）のとき「まとめています」だけ出さず正直に言う。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert "let tot=0;for(const v in cCenters)tot+=cCenters[v].n;" in html
    assert "（表示中の対象がありません）" in html


def test_cluster_maps_are_prototype_free(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """島の辞書と stem 索引はプロトタイプ無し（"constructor" 等の値/stem で島が消えない・Codex）。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert "let cCenters=Object.create(null);" in html  # 初期化
    assert "const cnt=Object.create(null),seen=[];" in html  # 集計辞書
    assert "cCenters=Object.create(null);\n  const tot=vals.length||1" in html  # 軸切替時の作り直し
    # grpVal が引く stem 索引も素の {} だと Object.prototype のメンバを拾う
    assert "const cByStem=Object.create(null),dByStem=Object.create(null)" in html


def test_cluster_select_has_accessible_name(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """まとめ軸 select に達成可能なアクセシブル名（見出しと aria-labelledby で結ぶ・Codex）。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert '<div class="gh" id="gClusterLbl">まとめる（配置）</div>' in html
    assert 'id="gCluster" aria-labelledby="gClusterLbl" aria-describedby="gClusterCap"' in html


def test_cluster_force_replaces_origin_gravity_when_active(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """step(): cluster有効かつ所属ありは所属中心引力へ差し替え・未所属/既定は従来の原点重力を維持。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert "const gk=grpBin(n),gc=gk!==null?cCenters[gk]:null;" in html
    assert "if(gc){n.vx-=(n.x-gc.x)*opt.center*a;n.vy-=(n.y-gc.y)*opt.center*a;}" in html
    assert "else{n.vx-=n.x*opt.center*a;n.vy-=n.y*opt.center*a;}" in html  # 既定/未所属=不変


def test_gcluster_select_html_and_handler(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """まとめる（配置）UI: 5択セレクト・各option・onchange リヒート・1行ヘルパ・キャプション枠。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert "まとめる（配置）" in html
    # a11y: 見出し(div)と aria-labelledby で結んでアクセシブル名を与える
    assert 'id="gCluster" aria-labelledby="gClusterLbl" aria-describedby="gClusterCap"' in html
    for val, label in [
        ("", "取引先ごと（既定）"),
        ("phase", "フェーズ（取引先を島に）"),
        ("last", "最終接点（取引先を島に）"),
        ("doc_type", "資料の種類（資料を島に）"),
        ("solution", "施策（資料を島に）"),
    ]:
        assert f'<option value="{val}">{label}</option>' in html
    assert '$("#gCluster").onchange=' in html
    assert "buildCenters(opt.cluster)" in html
    assert (
        "alpha=Math.max(alpha,.5);fitPending=true;ensure();" in html
    )  # 既存リヒート＋収束後に再フィット
    assert "色分け＝何者か / まとめ＝どこに溜まるか" in html
    assert 'id="gClusterCap"' in html


def test_industry_not_offered_as_cluster_axis(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """色分けとの二重設定回避: まとめ軸セレクタに業種を入れない（業種＝色に一本化）。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    m = re.search(r'<select class="gin" id="gCluster"[^>]*>(.*?)</select>', html, re.S)
    assert m, "gCluster セレクトが見つからない"
    assert 'value="industry"' not in m.group(1)
    assert "業種" not in m.group(1)


def test_cluster_island_labels_and_caption_js(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """島ラベル（値＋件数・ハロー付き）と cluster有効時キャプション（未設定件数＋doc基準の体験変化）。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert "for(const v in cCenters)" in html  # 各島中心にラベル
    assert 'tx=v+" ("+c.n+")"' in html  # 値＋件数
    assert "ctx.strokeText(tx" in html and "ctx.fillText(tx" in html  # ハロー付きで可読
    assert "function clusterCap()" in html
    assert "でまとめています" in html
    assert "取引先は資料に引かれ周辺に配置" in html  # doc基準の体験変化を併記


def test_cluster_does_not_touch_colorby(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """色分け（ncol/groupBy/legend）は不変＝まとめ軸は配置のみに作用（二重設定を作らない）。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    # ncol は従来通り groupBy 依存で cluster を参照しない
    assert 'opt.groupBy==="phase"?(PHASECOLOR[n.phase]||"#9a9a9a"):colorOf(n.industry)' in html
    assert 'groupBy:"industry"' in html  # 既定色分けは業種のまま
