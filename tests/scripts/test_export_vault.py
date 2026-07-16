"""scripts/export_vault.py の純関数テスト（実 DB 0・書き込みは tmp_path のみ）。

契約:
- safe_filename: パストラバーサル（../）・OS 禁止文字・wikilink 破壊文字・長さ・
  Windows 予約名・空入力をすべて無害化する
- yaml_quote: frontmatter を壊す " / \\ / 改行 をエスケープした double-quoted スカラー
- plan_vault: CLAUDE.md + clients/*.md + docs/*.md を組み、同名資料は付番で衝突回避、
  同一 source_uri の資料は note を再利用する
- write_vault: 既定 dry-run は 1 ファイルも書かない・--commit は上書き（冪等）・
  Vault ルート外への書き出しは拒否する。完全 export + 明示 prune だけが manifest 管理済み
  （または旧 exporter の強い構造シグネチャを持つ）clients/docs Markdown を削除できる
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "export_vault", _ROOT / "scripts" / "export_vault.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["export_vault"] = _mod
_spec.loader.exec_module(_mod)

safe_filename = _mod.safe_filename
yaml_quote = _mod.yaml_quote
md_inline_escape = _mod.md_inline_escape
tag_token = _mod.tag_token
wikilink = _mod.wikilink
source_link = _mod.source_link
plan_vault = _mod.plan_vault
write_vault = _mod.write_vault
render_doc_note = _mod.render_doc_note
render_client_note = _mod.render_client_note
normalize_shared_group = _mod.normalize_shared_group


def _manifest_files(out: Path) -> dict[str, str]:
    payload = json.loads((out / _mod._MANIFEST_NAME).read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["generator"] == "scripts/export_vault.py"
    return payload["files"]


def _manifest_active(out: Path) -> tuple[set[str], bool]:
    payload = json.loads((out / _mod._MANIFEST_NAME).read_text(encoding="utf-8"))
    return set(payload["active_files"]), payload["complete_export"]


def _doc(title: str, uri: str = "gdrive://F1", **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "title": title,
        "source_uri": uri,
        "source_type": "gdrive",
        "modified_at": "2026-06-01",
        "cls_industry": "エネルギー",
        "cls_project": "出光興産",
        "cls_doc_type": "提案書",
        "cls_solution": "動画広告",
        "client_name": None,
        "excerpt": "抜粋テキスト",
    }
    row.update(over)
    return row


def _fb(occurred_at: str, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "content": "商談メモ",
        "occurred_at": occurred_at,
        "source_uri": "slack://C1/123",
        "title": "営業FB",
        "client_name": "出光興産",
        "deal_phase": "提案",
        "bant_score": "B（前向き）",
        "channel_type": None,
        "positive_reaction": None,
        "negative_reaction": None,
        "next_action": None,
        "proposed_menu": None,
    }
    row.update(over)
    return row


# ---------------- safe_filename ----------------


def test_safe_filename_blocks_path_traversal() -> None:
    assert "/" not in safe_filename("../etc/passwd")
    assert ".." not in safe_filename("../etc/passwd")
    assert not safe_filename("../etc/passwd").startswith(".")
    assert safe_filename("..") == "untitled"
    assert safe_filename("../../") == "untitled"


def test_safe_filename_replaces_os_forbidden_chars() -> None:
    out = safe_filename('a\\b/c:d*e?f"g<h>i|j')
    for ch in '\\/:*?"<>|':
        assert ch not in out
    assert "a" in out and "j" in out


def test_safe_filename_strips_wikilink_breaking_chars() -> None:
    # [ ] # ^ | は Obsidian の wikilink/タグを壊すため除去する
    out = safe_filename("株式会社[テスト]#1^x|y")
    for ch in "[]#^|":
        assert ch not in out


def test_safe_filename_limits_length() -> None:
    assert len(safe_filename("あ" * 500)) <= 80


def test_safe_filename_empty_and_none_fallback() -> None:
    assert safe_filename("") == "untitled"
    assert safe_filename(None) == "untitled"
    assert safe_filename("   ") == "untitled"
    assert safe_filename("///", fallback="client") == "client"


def test_safe_filename_windows_reserved_names() -> None:
    assert safe_filename("CON").lower() != "con"
    assert safe_filename("nul").lower() != "nul"


def test_safe_filename_keeps_japanese() -> None:
    assert safe_filename("出光興産") == "出光興産"
    assert safe_filename("NGK（日本ガイシ）") == "NGK（日本ガイシ）"


def test_safe_filename_normalizes_unicode_to_nfc() -> None:
    nfc = "【ベクトル】PR×ショート動画のご提案"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    assert safe_filename(nfd) == nfc
    assert unicodedata.is_normalized("NFC", safe_filename(nfd))


# ---------------- company-shared ACL input ----------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("vectorinc.co.jp", "vectorinc.co.jp"),
        ("  VectorInc.CO.JP  ", "vectorinc.co.jp"),
        ("company.example", "company.example"),
    ],
)
def test_normalize_shared_group_accepts_one_dns_domain(raw: str, expected: str) -> None:
    assert normalize_shared_group(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "vectorinc.co.jp,other.example",
        "vectorinc.co.jp,",
        "vectorinc co.jp",
        "vectorinc",
        "https://vectorinc.co.jp",
        "-bad.example",
        "会社.example",
    ],
)
def test_normalize_shared_group_rejects_empty_multiple_and_invalid(raw: str | None) -> None:
    with pytest.raises(ValueError, match="single"):
        normalize_shared_group(raw)


# ---------------- frontmatter / wikilink / tags ----------------


def test_yaml_quote_escapes_quotes_backslash_newline() -> None:
    assert yaml_quote('say "hi"') == '"say \\"hi\\""'
    assert yaml_quote("a\\b") == '"a\\\\b"'
    assert yaml_quote("line1\nline2") == '"line1\\nline2"'
    assert yaml_quote(None) == '""'


def test_tag_token_normalizes_spaces_and_symbols() -> None:
    assert tag_token("出光 興産") == "出光_興産"
    assert tag_token("#提案書") == "提案書"
    assert tag_token("  ") == ""
    assert tag_token(None) == ""


def test_wikilink_wraps_path() -> None:
    assert wikilink("clients/出光興産") == "[[clients/出光興産]]"


def test_source_link_shapes_gdrive_and_passes_slack() -> None:
    assert source_link("gdrive://FILE_X", "gdrive") == (
        "https://drive.google.com/file/d/FILE_X/view"
    )
    assert source_link("slack://C1/123", "slack") == "slack://C1/123"
    assert source_link(None, None) is None


# ---------------- note 生成 ----------------


def test_render_doc_note_has_frontmatter_tags_and_client_link() -> None:
    note = render_doc_note(_doc("出光興産様向け提案書"), "出光興産", "clients/出光興産")
    assert note.startswith("---\n")
    assert 'generated_by: "scripts/export_vault.py"' in note
    assert 'doc_type: "提案書"' in note
    assert 'client: "出光興産"' in note
    assert 'industry: "エネルギー"' in note
    assert 'solution: "動画広告"' in note
    assert 'modified_at: "2026-06-01"' in note
    assert "#提案書" in note and "#出光興産" in note
    assert "[[clients/出光興産]]" in note
    assert "https://drive.google.com/file/d/F1/view" in note
    assert "抜粋テキスト" in note
    # 名寄せタグ未設定でも entities フィールド自体は必ず出す（build_app_html の front() が拾える）
    assert 'entities: ""' in note
    # 通常資料は従来どおり excerpt を折り畳み blockquote 表示（研究doc全文化の対象外）
    assert "> 抜粋テキスト" in note


def test_render_doc_note_emits_internal_source_identity_frontmatter() -> None:
    """タイトル非依存の配信フィルタ用に source identity を Vault note へ残す。"""
    note = render_doc_note(
        _doc(
            "変更後の表示タイトル",
            source_type="gsheets",
            external_id="SHEET1:278789217:53",
        ),
        "出光興産",
        "clients/出光興産",
    )
    assert 'source_type: "gsheets"' in note
    assert 'external_id: "SHEET1:278789217:53"' in note


def test_render_doc_note_research_doc_renders_full_body() -> None:
    """施策研究ノート(x_research_tool)は要約全文を本文に出す（160字の折り畳み blockquote でない）。"""
    full = "# 辻利 Xの声集め\n\n## 主要な声\n- 「濃厚で最高」 — @a（❤️8,227）\n- 「甘さ控えめ」 — @b"
    note = render_doc_note(
        _doc(
            "辻利 Xの声集め（X（旧Twitter））",
            uri="https://connect.newstv.co.jp/r/tok",
            source_type="other",
            x_research_tool="x_voice",
            excerpt=full,
        ),
        "辻利",
        "clients/辻利",
    )
    assert "## 主要な声" in note  # 構造（見出し/箇条書き）が保持される
    assert "「濃厚で最高」" in note and "「甘さ控えめ」" in note
    assert "> # 辻利" not in note  # collapsed blockquote 化されていない


def test_render_doc_note_emits_entities_field_and_tags() -> None:
    # cls_entities（正規化済み CSV）は entities frontmatter とインラインタグ両方に出す。
    note = render_doc_note(
        _doc("0115_祇園辻利プロモーション", cls_entities="サンマルクカフェ,祇園辻利"),
        "祇園辻利",
        "clients/祇園辻利",
    )
    assert 'entities: "サンマルクカフェ,祇園辻利"' in note
    assert "#サンマルクカフェ" in note and "#祇園辻利" in note


def test_render_doc_note_ignores_blank_entity_segments() -> None:
    # 空セグメント（",,"や前後空白）はタグ化しない（#空タグを出さない）。
    note = render_doc_note(
        _doc("t", cls_entities=" , サンマルクカフェ ,"),
        "祇園辻利",
        "clients/祇園辻利",
    )
    assert "#サンマルクカフェ" in note
    assert "# \n" not in note and "#\n" not in note


def test_documents_sql_projects_knowledge_share_meta() -> None:
    """ナレッジ共有メタを cls_* 列名で射影する（読む metadata キーは人間入力側）。"""
    sql = _mod.documents_sql()
    assert "d.metadata->>'knowledge_kind' AS cls_category" in sql
    assert "d.metadata->>'client_type' AS cls_client_tier" in sql
    assert "d.metadata->>'proposed_menu' AS cls_product" in sql
    # stale 除外版でも同一（分岐は stale 節のみ）
    assert "d.metadata->>'knowledge_kind' AS cls_category" in _mod.documents_sql(include_stale=True)


def test_render_doc_note_emits_knowledge_meta_frontmatter_when_present() -> None:
    """category/client_tier/product は値があるときだけ frontmatter に出す。"""
    note = render_doc_note(
        _doc(
            "デルタ製薬様向け提案書",
            cls_category="提案",
            cls_client_tier="TOP500 or ベス10,メーカー",
            cls_product="ビデオリリース,タテガタ",
        ),
        "デルタ製薬",
        "clients/デルタ製薬",
    )
    # 多値の生文字列（カンマ/スペース内包）をそのまま double-quoted で載せる → build 側 front() が拾う
    assert 'category: "提案"' in note
    assert 'client_tier: "TOP500 or ベス10,メーカー"' in note
    assert 'product: "ビデオリリース,タテガタ"' in note


def test_render_doc_note_omits_knowledge_meta_when_absent() -> None:
    """値がない資料は既存 note と同一（category/client_tier/product 行を出さない・回帰なし）。"""
    note = render_doc_note(_doc("素の提案書"), "出光興産", "clients/出光興産")
    assert "category:" not in note
    assert "client_tier:" not in note
    assert "product:" not in note
    # 既存 frontmatter は不変
    assert 'doc_type: "提案書"' in note and 'entities: ""' in note


def test_render_client_note_header_and_desc_timeline() -> None:
    timeline = [
        _fb("2026-05-01", deal_phase="初回接触", bant_score="C（検討）"),
        _fb("2026-06-15", deal_phase="提案", bant_score="B（前向き）"),
    ]
    docs = [_doc("提案書A")]
    note = render_client_note("出光興産", timeline, docs, ["docs/提案書A"])
    assert 'generated_by: "scripts/export_vault.py"' in note
    # frontmatter は最新（末尾）FB のフェーズ/BANT + 資料側の業界
    assert 'deal_phase: "提案"' in note
    assert 'bant_score: "B（前向き）"' in note
    assert 'industry: "エネルギー"' in note
    assert "fb_count: 2" in note
    assert "doc_count: 1" in note
    # 時系列は新しい順（2026-06-15 が先）
    assert note.index("2026-06-15") < note.index("2026-05-01")
    assert "[[docs/提案書A]]" in note


def test_render_client_note_empty_sections() -> None:
    note = render_client_note("新規クライアント", [], [], [])
    assert "fb_count: 0" in note
    assert "FB の記録はまだありません" in note
    assert "関連資料はまだありません" in note


def test_render_client_note_escapes_markdown_injection() -> None:
    """FB 由来テキスト（client 名/見出し/pair 値/blockquote 本文）の Markdown 記法を退避する。

    Slack FB 本文や LLM 分類値に混ざった [x](javascript:…) / <img> / `code` / [[wikilink]] が
    AiLaVault(Obsidian) で装飾リンク・HTML・埋め込みとして描画されないよう、逐語出力箇所を
    md_inline_escape 済みで出す（frontmatter は yaml_quote が守るため対象外）。
    """
    fb = _fb(
        "2026-06-15",
        title="<img src=x onerror=alert(1)>",
        deal_phase="[phase](javascript:alert(1))",
        content="漏洩 [click](javascript:alert(1)) <script>x</script> [[secret]] `code`",
    )
    note = render_client_note("Evil <b>Co</b> [x]", [fb], [], [])

    # 逐語出力される FB 由来テキストはすべてバックスラッシュ退避された形で出る
    assert md_inline_escape("Evil <b>Co</b> [x]") in note  # H1 の client 名
    assert md_inline_escape("<img src=x onerror=alert(1)>") in note  # ### 見出しの title
    assert md_inline_escape("[phase](javascript:alert(1))") in note  # - フェーズ: の value
    assert (
        md_inline_escape("漏洩 [click](javascript:alert(1)) <script>x</script> [[secret]] `code`")
        in note
    )  # blockquote 本文
    # title は frontmatter に出ないので、生の HTML タグ記法は note のどこにも残らない
    assert "<img src=x onerror=alert(1)>" not in note

    # blockquote 本文行（"> " マーカーを除く）に未退避の [ ] < > ` が残っていないこと。
    # 直前が \ でない危険記号にマッチする正規表現でゼロを確認する。
    quote_body = next(ln for ln in note.splitlines() if ln.startswith("> "))[2:]
    assert not re.search(r"(?<!\\)[\[\]<>`]", quote_body)


def test_render_client_note_plain_fb_unescaped_noop() -> None:
    """通常の FB（危険記号なし）はエスケープが no-op で、従来どおり素の文字列で出る。"""
    fb = _fb("2026-06-15", title="営業FB", deal_phase="提案", content="商談メモ")
    note = render_client_note("出光興産", [fb], [], [])
    assert "# 出光興産" in note
    assert "### 2026-06-15 営業FB" in note
    assert "- フェーズ: 提案" in note
    assert "> 商談メモ" in note
    assert "\\" not in note.split("## 営業FB時系列")[1]  # FB 本文側にバックスラッシュ混入なし


# ---------------- plan_vault ----------------


def test_plan_vault_builds_claude_md_clients_and_docs() -> None:
    clients = {
        "出光興産": {
            "timeline": [_fb("2026-06-15")],
            "documents": [_doc("提案書A")],
        }
    }
    files = plan_vault(clients)
    assert set(files) == {"CLAUDE.md", "clients/出光興産.md", "docs/提案書A.md"}
    claude = files["CLAUDE.md"]
    assert "読み取りミラー" in claude
    assert "pgvector" in claude
    assert "https://connect.newstv.co.jp/search" in claude


def test_plan_vault_sanitizes_hostile_client_name() -> None:
    clients = {
        "../../etc/passwd": {
            "timeline": [],
            "documents": [_doc("../evil", uri="gdrive://E1")],
        }
    }
    files = plan_vault(clients)
    for path in files:
        assert ".." not in path
        assert not path.startswith("/")
        # clients/ docs/ の 1 階層下のみ（サニタイズ済名にスラッシュが残らない）
        assert path.count("/") <= 1


def test_plan_vault_dedupes_same_title_different_docs() -> None:
    clients = {
        "出光興産": {
            "timeline": [],
            "documents": [
                _doc("提案書", uri="gdrive://F1"),
                _doc("提案書", uri="gdrive://F2"),
            ],
        }
    }
    files = plan_vault(clients)
    assert "docs/提案書.md" in files
    assert "docs/提案書_2.md" in files


def test_plan_vault_dedupes_canonically_equivalent_unicode_paths() -> None:
    """macOS が同一視する NFC/NFD タイトルを別資料として安全に付番する。"""
    nfc = "【ベクトル】PR×ショート動画のご提案_ANA様"
    nfd = unicodedata.normalize("NFD", nfc)
    files = plan_vault(
        {
            "ANA": {
                "timeline": [],
                "documents": [
                    _doc(nfd, uri="gdrive://NFD"),
                    _doc(nfc, uri="gdrive://NFC"),
                ],
            }
        }
    )
    doc_notes = sorted(path for path in files if path.startswith("docs/"))
    assert doc_notes == [f"docs/{nfc}.md", f"docs/{nfc}_2.md"]
    assert all(unicodedata.is_normalized("NFC", path) for path in doc_notes)


def test_plan_vault_dedupes_case_insensitive_paths() -> None:
    files = plan_vault(
        {
            "A社": {"timeline": [], "documents": [_doc("Report", uri="gdrive://UPPER")]},
            "B社": {"timeline": [], "documents": [_doc("report", uri="gdrive://LOWER")]},
        }
    )
    doc_notes = sorted(path for path in files if path.startswith("docs/"))
    assert len(doc_notes) == 2
    assert len({unicodedata.normalize("NFC", path).casefold() for path in doc_notes}) == 2


def test_plan_vault_doc_numbering_survives_natural_suffix_names() -> None:
    """人力の「報告書_2」タイトルと付番の _2 が衝突しても note を潰さない。

    出現回数カウント方式だと 3 資料が 2 ファイルに潰れ、2 件目の note が 3 件目に
    黙って上書きされ wikilink が別資料を指していた（要修正 major の再発防止）。
    """
    clients = {
        "出光興産": {
            "timeline": [],
            "documents": [
                _doc("報告書", uri="gdrive://R1"),
                _doc("報告書", uri="gdrive://R2"),
                _doc("報告書_2", uri="gdrive://R3"),
            ],
        }
    }
    files = plan_vault(clients)
    doc_notes = sorted(p for p in files if p.startswith("docs/"))
    assert len(doc_notes) == 3  # 3 資料 = 3 note（サイレント上書き消失しない）
    # 3 資料の出典（R1/R2/R3）がすべて残っている
    contents = "\n".join(files[p] for p in doc_notes)
    for fid in ("R1", "R2", "R3"):
        assert f"https://drive.google.com/file/d/{fid}/view" in contents
    # カルテの wikilink は 3 本とも相異なり、実在ファイルと 1:1 対応する
    links = re.findall(r"\[\[(docs/[^\]]+)\]\]", files["clients/出光興産.md"])
    assert len(links) == 3
    assert set(links) == {p.removesuffix(".md") for p in doc_notes}


def test_plan_vault_client_numbering_survives_natural_suffix_names() -> None:
    """サニタイズ後同名（foo/bar・foo:bar）と天然の foo_bar_2 が共存してもカルテを潰さない。"""
    clients = {
        "foo/bar": {"timeline": [], "documents": []},
        "foo:bar": {"timeline": [], "documents": []},
        "foo_bar_2": {"timeline": [], "documents": []},
    }
    files = plan_vault(clients)
    karte_notes = [p for p in files if p.startswith("clients/")]
    assert len(karte_notes) == 3  # 3 クライアント = 3 カルテ（消失しない）
    bodies = "\n".join(files[p] for p in karte_notes)
    for heading in ("# foo/bar", "# foo:bar", "# foo_bar_2"):
        assert heading in bodies


def test_plan_vault_reuses_note_for_same_source_uri() -> None:
    doc = _doc("共通提案書", uri="gdrive://SHARED")
    clients = {
        "A社": {"timeline": [], "documents": [dict(doc)]},
        "B社": {"timeline": [], "documents": [dict(doc)]},
    }
    files = plan_vault(clients)
    doc_notes = [p for p in files if p.startswith("docs/")]
    assert doc_notes == ["docs/共通提案書.md"]  # note は 1 つだけ
    # 両クライアントのカルテから同じ note へ wikilink される
    assert "[[docs/共通提案書]]" in files["clients/A社.md"]
    assert "[[docs/共通提案書]]" in files["clients/B社.md"]


def test_plan_vault_research_docs_get_collision_proof_filenames() -> None:
    """施策研究ノートは同一タイトルでも external_id 由来ハッシュで一意名になり、
    別owner/別研究が /app から消えない（P1・#214-2/3）。

    build_app_html の _chunk_key は `_\\d{1,2}$` を分割断片として束ね代表1件しか残さないため、
    従来の `_2` 付番だと 2 件目の研究ノートが /app から握り潰されていた。`-<hash>` 分岐で回避する。
    """
    clients = {
        "アサヒ飲料": {
            "timeline": [],
            "documents": [
                # 同一商材・同一タイトル・report_url 無し（None）でも external_id が異なる別研究
                _doc(
                    "アサヒ白湯 Xの声集め",
                    uri="",
                    x_research_tool="x_voice",
                    external_id="xresearch:x_voice:aaa:owner1:k1",
                ),
                _doc(
                    "アサヒ白湯 Xの声集め",
                    uri="",
                    x_research_tool="x_voice",
                    external_id="xresearch:x_voice:aaa:owner2:k2",
                ),
            ],
        }
    }
    files = plan_vault(clients)
    doc_notes = sorted(p for p in files if p.startswith("docs/"))
    assert len(doc_notes) == 2  # 2 研究 = 2 note（サイレント消失しない）
    # `_N` サフィックスではなく `-<hash16>` で分岐（_chunk_key の束ねに当たらない）
    assert all(not re.search(r"_\d+\.md$", p) for p in doc_notes)
    assert all(re.search(r"-[0-9a-f]{16}\.md$", p) for p in doc_notes)


def test_plan_vault_gsheets_knowledge_rows_get_collision_proof_filenames() -> None:
    """ナレッジ共有行(gsheets)も external_id 由来ハッシュで一意名になり /app から消えない。

    #215 で title を「row N」から「正式社名 案件名」へ変えた結果、同一クライアントの同一案件で
    複数回共有された行 (この運用では常態) が同名になる。実シート 142 行の実測で 8 stem が衝突し、
    案件名が空の行は社名だけに潰れて更に衝突していた。`_2` が振られると _chunk_key の束ねで
    2 件目以降が /app から無言で消えるため、x_research と同じ `-<hash16>` 分岐で回避する。
    """
    clients = {
        "小林製薬": {
            "timeline": [],
            "documents": [
                # 同一「正式社名 案件名」= 同一 title。source_uri は行ごとに異なる(&range=N:N)。
                _doc(
                    "小林製薬 熱さまシート",
                    uri="https://docs.google.com/spreadsheets/d/S1/edit#gid=1&range=12:12",
                    source_type="gsheets",
                    external_id="S1:278789217:12",
                ),
                _doc(
                    "小林製薬 熱さまシート",
                    uri="https://docs.google.com/spreadsheets/d/S1/edit#gid=1&range=87:87",
                    source_type="gsheets",
                    external_id="S1:278789217:87",
                ),
            ],
        }
    }
    files = plan_vault(clients)
    doc_notes = sorted(p for p in files if p.startswith("docs/"))
    assert len(doc_notes) == 2  # 2 行 = 2 note（サイレント消失しない）
    assert all(not re.search(r"_\d+\.md$", p) for p in doc_notes)
    assert all(re.search(r"-[0-9a-f]{16}\.md$", p) for p in doc_notes)


def test_plan_vault_uses_more_than_32_bits_for_known_hash8_collision() -> None:
    """同じ8hexになる実入力でも別stemになり、`_2` へ退行しない。"""
    clients = {
        "A社": {
            "timeline": [],
            "documents": [
                _doc(
                    "同名資料",
                    uri="",
                    source_type="gsheets",
                    external_id="SHEET:278789217:91713",
                ),
                _doc(
                    "同名資料",
                    uri="",
                    source_type="gsheets",
                    external_id="SHEET:278789217:98197",
                ),
            ],
        }
    }
    files = plan_vault(clients)
    doc_notes = sorted(p for p in files if p.startswith("docs/"))
    assert len(doc_notes) == 2
    assert all(re.search(r"-[0-9a-f]{16}\.md$", p) for p in doc_notes)
    assert {p.rsplit("-", 1)[-1][:8] for p in doc_notes} == {"24899ad1"}
    assert len({p.rsplit("-", 1)[-1][:16] for p in doc_notes}) == 2


def test_plan_vault_hashed_multibyte_filename_stays_below_255_bytes() -> None:
    files = plan_vault(
        {
            "A社": {
                "timeline": [],
                "documents": [
                    _doc(
                        "😀" * 80,
                        uri="",
                        source_type="gsheets",
                        external_id="SHEET:1:2",
                    )
                ],
            }
        }
    )
    [doc_path] = [p for p in files if p.startswith("docs/")]
    assert len(Path(doc_path).name.encode("utf-8")) <= 255


@pytest.mark.parametrize(
    ("source_type", "extra"),
    [("gsheets", {}), ("gdrive", {"x_research_tool": "x_voice"})],
)
def test_plan_vault_discriminator_sources_require_external_id(
    source_type: str, extra: dict[str, str]
) -> None:
    """安定IDなしでタイトル/URLへ黙って退行せず、欠損データを明示的に止める。"""
    clients = {
        "A社": {
            "timeline": [],
            "documents": [
                _doc(
                    "同名資料",
                    uri="https://example.invalid/source",
                    source_type=source_type,
                    external_id="",
                    **extra,
                )
            ],
        }
    }
    with pytest.raises(ValueError, match="external_id is required"):
        plan_vault(clients)


def test_plan_vault_reuses_note_for_same_source_identity_without_uri() -> None:
    """URLなし/変化後でも同じ安定IDは二重noteにせず再利用する。"""
    docs = [
        _doc(
            "同名資料",
            uri="",
            source_type="gsheets",
            external_id="SHEET:1:2",
        ),
        _doc(
            "タイトル変更後",
            uri="https://example.invalid/changed",
            source_type="gsheets",
            external_id="SHEET:1:2",
        ),
    ]
    files = plan_vault({"A社": {"timeline": [], "documents": docs}})
    doc_notes = [p for p in files if p.startswith("docs/")]
    assert len(doc_notes) == 1
    # カルテ側の2行も同じnoteを参照する。
    stem = doc_notes[0].removeprefix("docs/").removesuffix(".md")
    assert files["clients/A社.md"].count(f"[[docs/{stem}]]") == 2


def test_plan_vault_true_discriminator_collision_is_not_chunk_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """異なる安定IDの64-bit衝突でも `_2` にせず、HTML の chunk 結合から2件を守る。"""
    monkeypatch.setattr(_mod, "source_discriminator", lambda external_id: "0" * 16)
    docs = [
        _doc("同名資料", uri="", source_type="gsheets", external_id=f"SHEET:1:{row}")
        for row in (2, 3)
    ]
    files = plan_vault({"A社": {"timeline": [], "documents": docs}})
    doc_notes = sorted(p for p in files if p.startswith("docs/"))
    assert len(doc_notes) == 2
    assert any("-dup-2.md" in p for p in doc_notes)
    assert all(not re.search(r"_\d+\.md$", p) for p in doc_notes)


def test_plan_vault_gdrive_docs_keep_plain_filenames() -> None:
    """gdrive 等 title が資料名で一意な経路にはハッシュを付けない（既存の見た目を壊さない）。"""
    clients = {
        "A社": {
            "timeline": [],
            "documents": [_doc("提案書A", uri="gdrive://F1", source_type="gdrive")],
        }
    }
    files = plan_vault(clients)
    assert "docs/提案書A.md" in files


def test_clients_sql_excludes_research_products_from_client_union() -> None:
    """施策研究の cls_project(商材名)は取引先一覧に昇格しない（取引先タクソノミー非汚染・#214-1）。"""
    sql = _mod._CLIENTS_SQL
    # コメント文字列ではなく実ガード句そのものを検証（"x_research_tool" はコメントにも出るため）。
    assert "x_research_tool' IS NULL" in sql
    # ガードは cls_project の UNION 枝側に入っている（client_name 枝ではない）
    assert sql.index("x_research_tool' IS NULL") > sql.index("cls_project")


def test_all_admin_dsn_select_paths_require_the_same_company_shared_acl() -> None:
    """client列挙/FB/資料のどの経路も company-shared ACL 謂語を外せない。"""
    predicate = _mod._SHARED_ACL_SQL

    # client 列挙は UNION 2 枝の両方、timeline/documents は各 1 箇所に必須。
    assert _mod._CLIENTS_SQL.count(predicate) == 2
    assert _mod._TIMELINE_SQL.count(predicate) == 1
    assert _mod._DOCUMENTS_SQL_TEMPLATE.count(predicate) == 1
    assert _mod._CLIENTS_SQL.count("%s") == 2
    assert _mod._TIMELINE_SQL.count("%s") == 3
    assert _mod._DOCUMENTS_SQL_TEMPLATE.count("%s") == 5

    # DB 側値とパラメータの両方で case/周辺空白を無視する。
    assert "lower(btrim(shared_acl.group_name))" in predicate
    assert "lower(btrim(%s::text))" in predicate


def test_shared_acl_sql_is_fail_closed_for_owner_only_or_empty_acl() -> None:
    """owner/email の迂回を持たず、空 array は unnest の EXISTS=false になる契約。"""
    predicate = _mod._SHARED_ACL_SQL
    assert "EXISTS (" in predicate
    assert "FROM unnest(d.acl_groups)" in predicate
    assert "owner_email" not in predicate
    assert "acl_emails" not in predicate
    assert " OR " not in predicate.upper()
    for sql in (_mod._CLIENTS_SQL, _mod._TIMELINE_SQL, _mod._DOCUMENTS_SQL_TEMPLATE):
        assert "owner_email" not in sql
        assert "acl_emails" not in sql


def test_acl_is_filter_only_and_not_exported_as_raw_data() -> None:
    """raw ACL は SELECT 列ではなく WHERE 謂語のみ（Vault/HTML へ流さない）。"""
    for sql in (_mod._TIMELINE_SQL, _mod._DOCUMENTS_SQL_TEMPLATE):
        select_list = sql.split("FROM", 1)[0]
        assert "acl_groups" not in select_list


def test_documents_sql_selects_external_id_for_research_filenames() -> None:
    """研究ノートの一意ファイル名生成に external_id が要るため SELECT に含める（#214-2）。"""
    assert "external_id" in _mod.documents_sql()


_UNESCAPED_MD_TITLE = re.compile(r"(?<!\\)[\[\]<>`]")


def test_render_doc_note_escapes_markdown_in_h1_title() -> None:
    """H1 見出しへ逐語出力する title の Markdown 記法を退避する（stored injection・#214-4）。"""
    note = render_doc_note(
        _doc("[phish](javascript:alert(1)) <img src=x> [[secret]]"),
        "出光興産",
        "clients/出光興産",
    )
    h1 = next(ln for ln in note.splitlines() if ln.startswith("# "))
    assert not _UNESCAPED_MD_TITLE.search(h1)  # 未エスケープの [ ] < > ` が無い＝記法が死ぬ
    assert "phish" in h1  # 可読テキスト自体は残す


# ---------------- write_vault ----------------


def test_write_vault_dry_run_writes_nothing(tmp_path: Path) -> None:
    files = {"CLAUDE.md": "x", "clients/A.md": "y"}
    stats = write_vault(tmp_path / "vault", files, commit=False)
    assert stats == {"planned": 2, "written": 0, "delete_planned": 0, "deleted": 0}
    assert not (tmp_path / "vault").exists()


def test_write_vault_commit_writes_and_is_idempotent(tmp_path: Path) -> None:
    out = tmp_path / "vault"
    files = {"CLAUDE.md": "v1", "clients/出光興産.md": "カルテ v1"}
    stats = write_vault(out, files, commit=True)
    assert stats == {"planned": 2, "written": 2, "delete_planned": 0, "deleted": 0}
    assert (out / "clients" / "出光興産.md").read_text(encoding="utf-8") == "カルテ v1"
    assert set(_manifest_files(out)) == {"clients/出光興産.md"}
    # 再実行は同パスへの上書き（冪等・更新が反映される）
    files2 = {"CLAUDE.md": "v1", "clients/出光興産.md": "カルテ v2"}
    write_vault(out, files2, commit=True)
    assert (out / "clients" / "出光興産.md").read_text(encoding="utf-8") == "カルテ v2"
    # 余計なファイルが増えない
    all_files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.md"))
    assert all_files == ["CLAUDE.md", "clients/出光興産.md"]


def test_write_vault_rejects_escape_from_root(tmp_path: Path) -> None:
    out = tmp_path / "vault"
    try:
        write_vault(out, {"../outside.md": "x"}, commit=True)
    except ValueError as e:
        assert "unsafe path" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for path escape")
    assert not (tmp_path / "outside.md").exists()


def test_write_vault_prune_dry_run_then_commit_reports_and_deletes_owned_notes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "vault"
    old = {"CLAUDE.md": "old", "clients/Old.md": "old client", "docs/Old.md": "old doc"}
    write_vault(out, old, commit=True)
    manifest_before = (out / _mod._MANIFEST_NAME).read_text(encoding="utf-8")
    capsys.readouterr()

    current = {"CLAUDE.md": "new", "clients/New.md": "new client", "docs/New.md": "new doc"}
    stats = write_vault(out, current, commit=False, prune=True, complete_export=True)
    assert stats == {"planned": 3, "written": 0, "delete_planned": 2, "deleted": 0}
    dry_run = capsys.readouterr().out
    assert "[dry-run] delete clients/Old.md" in dry_run
    assert "[dry-run] delete docs/Old.md" in dry_run
    assert (out / "clients" / "Old.md").exists()
    assert not (out / "clients" / "New.md").exists()
    assert (out / _mod._MANIFEST_NAME).read_text(encoding="utf-8") == manifest_before

    stats = write_vault(out, current, commit=True, prune=True, complete_export=True)
    assert stats == {"planned": 3, "written": 3, "delete_planned": 2, "deleted": 2}
    assert not (out / "clients" / "Old.md").exists()
    assert not (out / "docs" / "Old.md").exists()
    assert set(_manifest_files(out)) == {"clients/New.md", "docs/New.md"}
    assert _manifest_active(out) == ({"clients/New.md", "docs/New.md"}, True)


def test_prune_preserves_unmanaged_markdown_other_dirs_non_md_and_outside_root(
    tmp_path: Path,
) -> None:
    out = tmp_path / "vault"
    write_vault(
        out,
        {"clients/A.md": "owned client", "docs/A.md": "owned doc"},
        commit=True,
    )
    untouched = {
        out / "clients" / "manual.md": "personal client note",
        out / "docs" / "manual.md": "personal doc note",
        out / "docs" / "blob.txt": "not markdown",
        out / "root-note.md": "root markdown",
        out / "other" / "old.md": "different directory",
        tmp_path / "outside.md": "outside vault",
    }
    for path, content in untouched.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    stats = write_vault(
        out,
        {"clients/B.md": "new client", "docs/B.md": "new doc"},
        commit=True,
        prune=True,
        complete_export=True,
    )
    assert stats["delete_planned"] == 2
    assert stats["deleted"] == 2
    for path, content in untouched.items():
        assert path.read_text(encoding="utf-8") == content
    active, complete = _manifest_active(out)
    assert active == {"clients/B.md", "docs/B.md"}
    assert complete is True
    assert all(
        path.relative_to(out).as_posix() not in active
        for path in untouched
        if path.is_relative_to(out)
    )


def test_prune_never_unlinks_unicode_alias_of_current_path(tmp_path: Path) -> None:
    """旧NFD manifestをNFCへ移行しても、macOSで同一inodeの現行noteを消さない。"""
    out = tmp_path / "vault"
    nfc = "docs/【ベクトル】提案.md"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    write_vault(out, {nfd: "same content"}, commit=True)

    stats = write_vault(
        out,
        {nfc: "same content"},
        commit=True,
        prune=True,
        complete_export=True,
    )

    assert stats["delete_planned"] == 0
    assert stats["deleted"] == 0
    assert (out / nfc).read_text(encoding="utf-8") == "same content"
    assert set(_manifest_files(out)) == {nfc}


def test_first_prune_discovers_and_deletes_legacy_generated_orphans(tmp_path: Path) -> None:
    """manifest 導入前の non-company/stale note も exporter 固有構造なら初回に掃除できる。"""
    out = tmp_path / "vault"
    legacy_client = render_client_note("旧会社", [], [], [])
    legacy_doc = render_doc_note(_doc("旧資料"), "旧会社", "clients/旧会社")
    # generated_by marker 導入前の実ファイルを再現する。
    legacy_client = legacy_client.replace(_mod._GENERATED_BY_FIELD + "\n", "", 1)
    legacy_doc = legacy_doc.replace(_mod._GENERATED_BY_FIELD + "\n", "", 1)
    (out / "clients").mkdir(parents=True)
    (out / "docs").mkdir()
    (out / "clients" / "旧会社.md").write_text(legacy_client, encoding="utf-8")
    (out / "docs" / "旧資料.md").write_text(legacy_doc, encoding="utf-8")
    assert not (out / _mod._MANIFEST_NAME).exists()

    current = plan_vault({"新会社": {"timeline": [], "documents": [_doc("新資料")]}})
    stats = write_vault(out, current, commit=True, prune=True, complete_export=True)
    assert stats["delete_planned"] == 2
    assert stats["deleted"] == 2
    assert not (out / "clients" / "旧会社.md").exists()
    assert not (out / "docs" / "旧資料.md").exists()


def test_first_prune_discovers_legacy_gsheets_note_without_entities(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """実Vaultの旧 row note（entities 導入前）も強い本文構造で生成物と判定する。"""
    out = tmp_path / "vault"
    legacy_path = out / "docs" / "小林製薬 熱さまシート row 53.md"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        """---
title: "小林製薬 熱さまシート"
doc_type: "提案書"
client: "小林製薬"
industry: "製薬"
solution: "タテガタ"
modified_at: "2026-06-01"
---

# 小林製薬 熱さまシート

> 旧ファイル記録シート row 53 の抜粋

- 出典: [gsheets](https://docs.google.com/spreadsheets/d/S1/edit#gid=1&range=53:53)
- 取引先: [[clients/小林製薬]]
""",
        encoding="utf-8",
    )
    assert "entities:" not in legacy_path.read_text(encoding="utf-8")
    current = plan_vault({"新会社": {"timeline": [], "documents": [_doc("新資料")]}})

    stats = write_vault(out, current, commit=False, prune=True, complete_export=True)
    assert stats["delete_planned"] == 1
    assert stats["deleted"] == 0
    assert "[dry-run] delete docs/小林製薬 熱さまシート row 53.md" in capsys.readouterr().out
    assert legacy_path.exists()

    stats = write_vault(out, current, commit=True, prune=True, complete_export=True)
    assert stats["delete_planned"] == 1
    assert stats["deleted"] == 1
    assert not legacy_path.exists()


def test_prune_skips_manifest_owned_note_modified_after_export(tmp_path: Path) -> None:
    out = tmp_path / "vault"
    write_vault(
        out,
        {"clients/A.md": "owned client", "docs/A.md": "owned doc"},
        commit=True,
    )
    modified = out / "docs" / "A.md"
    modified.write_text("human edit", encoding="utf-8")

    stats = write_vault(
        out,
        {"clients/B.md": "new client", "docs/B.md": "new doc"},
        commit=True,
        prune=True,
        complete_export=True,
    )
    assert stats["delete_planned"] == 1
    assert stats["deleted"] == 1
    assert not (out / "clients" / "A.md").exists()
    assert modified.read_text(encoding="utf-8") == "human edit"
    assert "docs/A.md" in _manifest_files(out)  # 次回も変更済みとして保護を継続
    active, complete = _manifest_active(out)
    assert active == {"clients/B.md", "docs/B.md"}
    assert complete is True
    assert "docs/A.md" not in active  # ローカル保護しても静的/appの公開集合へ戻さない


def test_partial_export_never_prunes_or_shrinks_manifest(tmp_path: Path) -> None:
    out = tmp_path / "vault"
    initial = {
        "clients/A.md": "client A",
        "docs/A.md": "doc A",
        "clients/B.md": "client B",
        "docs/B.md": "doc B",
    }
    write_vault(out, initial, commit=True, complete_export=True)
    partial = {"clients/A.md": "client A2", "docs/A.md": "doc A2"}
    write_vault(out, partial, commit=True, complete_export=False)
    assert set(_manifest_files(out)) == set(initial)
    assert _manifest_active(out) == (set(initial), False)
    assert (out / "clients" / "B.md").read_text(encoding="utf-8") == "client B"

    with pytest.raises(ValueError, match="partial export"):
        write_vault(out, partial, commit=True, prune=True, complete_export=False)
    assert (out / "docs" / "B.md").read_text(encoding="utf-8") == "doc B"


def test_first_partial_export_cannot_claim_complete_publication_set(tmp_path: Path) -> None:
    """完全snapshot前のpartial commitはactive hashを持っても公開可能とは名乗らない。"""
    out = tmp_path / "vault"
    write_vault(
        out,
        {"clients/A.md": "client A", "docs/A.md": "doc A"},
        commit=True,
        complete_export=False,
    )
    assert _manifest_active(out) == ({"clients/A.md", "docs/A.md"}, False)


@pytest.mark.parametrize("managed_count", [0, 2])
def test_prune_refuses_empty_or_abnormally_small_plan_before_writing(
    tmp_path: Path, managed_count: int
) -> None:
    out = tmp_path / "vault"
    initial = {f"clients/C{i}.md": f"client {i}" for i in range(6)}
    initial["CLAUDE.md"] = "unchanged"
    write_vault(out, initial, commit=True, complete_export=True)
    current = {"CLAUDE.md": "must not be written"}
    current.update({f"clients/C{i}.md": f"new {i}" for i in range(managed_count)})

    with pytest.raises(ValueError, match="refusing prune"):
        write_vault(out, current, commit=True, prune=True, complete_export=True)
    assert (out / "CLAUDE.md").read_text(encoding="utf-8") == "unchanged"
    assert all((out / "clients" / f"C{i}.md").exists() for i in range(6))


def test_prune_refuses_empty_managed_plan_on_fresh_vault(tmp_path: Path) -> None:
    """前回 manifest が無くても空の完全 export を成功扱いにしない。"""
    out = tmp_path / "vault"
    with pytest.raises(ValueError, match="managed Markdown plan is empty"):
        write_vault(
            out,
            {"CLAUDE.md": "rules only"},
            commit=False,
            prune=True,
            complete_export=True,
        )
    assert not out.exists()


def test_explicit_prune_shrink_override_allows_reviewed_large_migration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ACL初回移行は明示override付きdry-runで削除一覧をレビューできる。"""
    out = tmp_path / "vault"
    initial = {f"clients/C{i}.md": f"client {i}" for i in range(6)}
    write_vault(out, initial, commit=True, complete_export=True)
    capsys.readouterr()

    stats = write_vault(
        out,
        {"clients/C0.md": "new 0"},
        commit=False,
        prune=True,
        complete_export=True,
        allow_prune_shrink=True,
    )
    assert stats["delete_planned"] == 5
    assert stats["deleted"] == 0
    assert capsys.readouterr().out.count("[dry-run] delete clients/") == 5
    assert all((out / "clients" / f"C{i}.md").exists() for i in range(6))


def test_prune_shrink_override_never_allows_empty_plan(tmp_path: Path) -> None:
    """override は『空を全削除』まで許可しない。"""
    with pytest.raises(ValueError, match="managed Markdown plan is empty"):
        write_vault(
            tmp_path / "vault",
            {"CLAUDE.md": "rules only"},
            commit=False,
            prune=True,
            complete_export=True,
            allow_prune_shrink=True,
        )


@pytest.mark.parametrize("unsafe_rel", ["../outside.md", "other/X.md", "docs/X.txt"])
def test_prune_rejects_unsafe_manifest_entries_without_deleting(
    tmp_path: Path, unsafe_rel: str
) -> None:
    out = tmp_path / "vault"
    (out / "clients").mkdir(parents=True)
    owned = out / "clients" / "A.md"
    owned.write_text("owned", encoding="utf-8")
    manifest = {
        "version": 1,
        "generator": "scripts/export_vault.py",
        "files": {unsafe_rel: "0" * 64},
    }
    (out / _mod._MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe managed path"):
        write_vault(
            out,
            {"clients/B.md": "new"},
            commit=True,
            prune=True,
            complete_export=True,
        )
    assert owned.read_text(encoding="utf-8") == "owned"
    assert not (out / "clients" / "B.md").exists()


@pytest.mark.parametrize("filter_arg", [["--client", "A"], ["--limit", "1"]])
def test_main_rejects_prune_with_partial_export_before_db_access(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    filter_arg: list[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["export_vault.py", "--dsn", "postgresql://unused", "--prune", *filter_arg],
    )
    assert _mod.main() == 2
    assert "併用できません" in capsys.readouterr().err


def test_main_rejects_prune_shrink_override_without_prune_before_db_access(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["export_vault.py", "--dsn", "postgresql://unused", "--allow-prune-shrink"],
    )
    assert _mod.main() == 2
    assert "--prune と併用" in capsys.readouterr().err


# ---------------- timeline の「最新 N 件」契約 ----------------


def test_timeline_sql_fetches_latest_n_desc() -> None:
    """_TIMELINE_SQL は DESC LIMIT（最新 N 件）。

    ASC LIMIT だと FB が per_client_limit を超えるクライアントで【最古の N 件】になり、
    frontmatter の deal_phase/bant_score と時系列が古い値で誤る（要修正 major の再発防止）。
    """
    assert "ORDER BY d.modified_at DESC NULLS LAST, c.chunk_idx DESC" in _mod._TIMELINE_SQL
    assert "LIMIT %s" in _mod._TIMELINE_SQL


def test_load_clients_data_returns_timeline_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 SQL の ACL パラメータ配線と timeline の古い順契約をフェイク DB で固定。"""
    import psycopg

    desc_rows = [
        {"occurred_at": "2026-06-15", "deal_phase": "提案"},
        {"occurred_at": "2026-05-01", "deal_phase": "初回接触"},
    ]
    executions: list[tuple[str, tuple[Any, ...]]] = []

    class _Cursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
            executions.append((sql, tuple(params or ())))
            if "DISTINCT name" in sql:
                self._rows = [{"name": "出光興産"}]
            elif "is_sales_fb' = 'true'" in sql:  # _TIMELINE_SQL（DB は新しい順で返す）
                self._rows = list(desc_rows)
            else:  # _DOCUMENTS_SQL
                self._rows = []

        def fetchall(self) -> list[dict[str, Any]]:
            return self._rows

    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def cursor(self) -> _Cursor:
            return _Cursor()

    monkeypatch.setattr(psycopg, "connect", lambda dsn, row_factory=None: _Conn())
    data = _mod.load_clients_data(
        "postgresql://stub",
        shared_group="  VectorInc.CO.JP  ",
    )
    timeline = data["出光興産"]["timeline"]
    assert [r["occurred_at"] for r in timeline] == ["2026-05-01", "2026-06-15"]  # 古い順
    assert timeline[-1]["deal_phase"] == "提案"  # 末尾＝最新（frontmatter が最新値になる）

    # shared_group は文字列補間せず、正規化した bind parameter で全経路へ渡す。
    assert [params for _, params in executions] == [
        ("vectorinc.co.jp", "vectorinc.co.jp"),  # _CLIENTS_SQL UNION 2 枝
        ("vectorinc.co.jp", "%出光興産%", 100),  # _TIMELINE_SQL
        ("vectorinc.co.jp", "%出光興産%", "%出光興産%", "%出光興産%", 100),
    ]
    assert all("vectorinc.co.jp" not in sql.lower() for sql, _ in executions)


@pytest.mark.parametrize("shared_group", ["", " ", "a.example,b.example", "not-a-domain"])
def test_load_clients_data_rejects_invalid_group_before_db_connect(
    monkeypatch: pytest.MonkeyPatch,
    shared_group: str,
) -> None:
    """load_clients_data 直呼びでも invalid group を DB に渡さない。"""
    import psycopg

    def _unexpected_connect(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("invalid shared_group must fail before DB connect")

    monkeypatch.setattr(psycopg, "connect", _unexpected_connect)
    with pytest.raises(ValueError, match="single"):
        _mod.load_clients_data("postgresql://stub", shared_group=shared_group)


def test_main_missing_shared_group_exits_2_before_db(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", raising=False)
    monkeypatch.setattr(sys, "argv", ["export_vault.py", "--dsn", "postgresql://stub"])
    monkeypatch.setattr(
        _mod,
        "load_clients_data",
        lambda *args, **kwargs: pytest.fail("missing shared group must not read DB"),
    )

    assert _mod.main() == 2
    assert "--shared-group" in capsys.readouterr().err


def test_main_rejects_comma_separated_env_group_before_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", "a.example,b.example")
    monkeypatch.setattr(sys, "argv", ["export_vault.py", "--dsn", "postgresql://stub"])
    monkeypatch.setattr(
        _mod,
        "load_clients_data",
        lambda *args, **kwargs: pytest.fail("multiple env groups must not read DB"),
    )

    assert _mod.main() == 2


def test_main_uses_single_env_group_as_safe_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def _load(dsn: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(dsn=dsn, **kwargs)
        return {}

    monkeypatch.setenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", " VectorInc.CO.JP ")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "export_vault.py",
            "--dsn",
            "postgresql://stub",
            "--out",
            str(tmp_path / "vault"),
        ],
    )
    monkeypatch.setattr(_mod, "load_clients_data", _load)

    assert _mod.main() == 0
    assert captured["shared_group"] == "vectorinc.co.jp"
