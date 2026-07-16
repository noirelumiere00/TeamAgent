"""scripts/export_vault.py の純関数テスト（実 DB 0・書き込みは tmp_path のみ）。

契約:
- safe_filename: パストラバーサル（../）・OS 禁止文字・wikilink 破壊文字・長さ・
  Windows 予約名・空入力をすべて無害化する
- yaml_quote: frontmatter を壊す " / \\ / 改行 をエスケープした double-quoted スカラー
- plan_vault: CLAUDE.md + clients/*.md + docs/*.md を組み、同名資料は付番で衝突回避、
  同一 source_uri の資料は note を再利用する
- write_vault: 既定 dry-run は 1 ファイルも書かない・--commit は上書き（冪等）・
  Vault ルート外への書き出しは拒否する
"""

from __future__ import annotations

import importlib.util
import re
import sys
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
tag_token = _mod.tag_token
wikilink = _mod.wikilink
source_link = _mod.source_link
plan_vault = _mod.plan_vault
write_vault = _mod.write_vault
render_doc_note = _mod.render_doc_note
render_client_note = _mod.render_client_note


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
    # `_N` サフィックスではなく `-<hash8>` で分岐（_chunk_key の束ねに当たらない）
    assert all(not re.search(r"_\d+\.md$", p) for p in doc_notes)
    assert all(re.search(r"-[0-9a-f]{8}\.md$", p) for p in doc_notes)


def test_clients_sql_excludes_research_products_from_client_union() -> None:
    """施策研究の cls_project(商材名)は取引先一覧に昇格しない（取引先タクソノミー非汚染・#214-1）。"""
    sql = _mod._CLIENTS_SQL
    # コメント文字列ではなく実ガード句そのものを検証（"x_research_tool" はコメントにも出るため）。
    assert "x_research_tool' IS NULL" in sql
    # ガードは cls_project の UNION 枝側に入っている（client_name 枝ではない）
    assert sql.index("x_research_tool' IS NULL") > sql.index("cls_project")


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
    assert stats == {"planned": 2, "written": 0}
    assert not (tmp_path / "vault").exists()


def test_write_vault_commit_writes_and_is_idempotent(tmp_path: Path) -> None:
    out = tmp_path / "vault"
    files = {"CLAUDE.md": "v1", "clients/出光興産.md": "カルテ v1"}
    stats = write_vault(out, files, commit=True)
    assert stats == {"planned": 2, "written": 2}
    assert (out / "clients" / "出光興産.md").read_text(encoding="utf-8") == "カルテ v1"
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
    """DESC で届いた最新 N 件を Python 反転で古い順へ戻す（末尾＝最新の契約を保つ）。"""
    import psycopg

    desc_rows = [
        {"occurred_at": "2026-06-15", "deal_phase": "提案"},
        {"occurred_at": "2026-05-01", "deal_phase": "初回接触"},
    ]

    class _Cursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
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
    data = _mod.load_clients_data("postgresql://stub")
    timeline = data["出光興産"]["timeline"]
    assert [r["occurred_at"] for r in timeline] == ["2026-05-01", "2026-06-15"]  # 古い順
    assert timeline[-1]["deal_phase"] == "提案"  # 末尾＝最新（frontmatter が最新値になる）
