"""documents / 営業FB を Obsidian Vault へエクスポートする（read-only・ローカル書き出しのみ）。

Karpathy 式 Second Brain の「wiki 層」の組織版。pgvector（正）を読み取り、
Obsidian で開ける Vault ミラーをローカルに生成する:

- ``clients/<client>.md``  … クライアントカルテ note
  frontmatter: client / industry / 最新 deal_phase / bant_score / fb_count / doc_count
  本文: FB 時系列（新しい順）+ 関連資料リスト（[[docs/...]] wikilink + Drive URL）
- ``docs/<安全なファイル名>.md`` … 資料 note
  frontmatter: doc_type / client / industry / solution / entities / modified_at
  （entities=cls_entities の名寄せタグ CSV。/app の「関係先/」タグ・検索へ展開される）
  本文: excerpt + 出典（Drive/Slack）リンク + タグ（#提案書 #出光興産 #祇園辻利 等）
  + [[clients/<client>]] wikilink
- ``CLAUDE.md`` … Vault 使用ルール（読み取りミラー・正は pgvector・検索は connect-web）

設計（scripts/backfill_doc_kind.py と同流儀）:
- DB は **SELECT のみ**（UPDATE/INSERT/DDL 一切なし）。書くのはローカル Vault ファイルだけ。
- 既定 **dry-run**（生成予定ファイル一覧を表示するだけ）。``--commit`` で初めて書き出す。
- 再実行は同パスへの上書き＝冪等（DB 側の更新が Vault に反映される）。
- クライアント名/タイトル→ファイル名は ``safe_filename`` で必ずサニタイズ
  （パストラバーサル・OS 禁止文字・wikilink 破壊文字・長さ・予約名）。
- frontmatter 値は YAML double-quoted スカラーへエスケープ（``yaml_quote``）。
- DB 接続は SSM ポートフォワード前提の --dsn 引数（無ければ DATABASE_URL）。
  実行は人間ゲート（本スクリプトを自動実行しない）。

Usage:
    # SSM ポートフォワードを張ってから（例: localhost:15432 → RDS）
    python scripts/export_vault.py --dsn postgresql://user:pass@localhost:15432/db  # dry-run
    python scripts/export_vault.py --dsn ... --commit                    # ~/AiLaVault へ書き出し
    python scripts/export_vault.py --dsn ... --out ~/Vaults/aila --commit
    python scripts/export_vault.py --dsn ... --client 出光興産 --commit  # 1 クライアントだけ
    python scripts/export_vault.py --dsn ... --limit 5                   # 先頭 5 クライアントのみ
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

import structlog  # noqa: E402

from teamagent.skills.knowledge_deliver.skill import extract_drive_file_id  # noqa: E402

logger = structlog.get_logger(__name__)

_DEFAULT_OUT = "~/AiLaVault"
_SEARCH_URL = "https://connect.newstv.co.jp/search"

# OS 禁止文字（Windows 含む）+ 制御文字 + Obsidian の wikilink/タグを壊す文字（[]#^|）。
_FORBIDDEN_CHARS_RE = re.compile(r'[\\/:*?"<>|\[\]#^\x00-\x1f]+')
# Windows 予約デバイス名（拡張子なしファイル名として不可）。
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


# -----------------------------------------------------------
# 純関数（DB 非依存・テスト対象）
# -----------------------------------------------------------
def safe_filename(name: str | None, *, fallback: str = "untitled", max_len: int = 80) -> str:
    """クライアント名/タイトルを安全なファイル名（拡張子なし）へ正規化する。

    - パストラバーサル対策: ``/`` ``\\`` を ``_`` 化し、先頭/末尾のドットを除去
      （``../etc/passwd`` → ``_etc_passwd``、``..`` → fallback）
    - OS 禁止文字（Windows 含む）・制御文字・wikilink/タグ破壊文字（``[]#^|``）を ``_`` 化
    - 連続空白を 1 個に圧縮・前後空白除去・max_len 文字で切り詰め
    - Windows 予約名（CON/NUL/COM1 等）は末尾 ``_`` を付けて回避
    - 空になったら fallback
    """
    base = str(name or "").strip()
    base = _FORBIDDEN_CHARS_RE.sub("_", base)
    # '..' はセパレータ除去後も念のため残さない（`../../x` → `.._.._x` 対策の二重防御）。
    base = re.sub(r"\.{2,}", "_", base)
    base = re.sub(r"\s+", " ", base).strip()
    base = base.strip(". ")
    base = base[:max_len].strip(". ")
    if not base or not base.strip("_ "):
        return fallback
    if base.lower() in _WINDOWS_RESERVED:
        return base + "_"
    return base


def yaml_quote(value: Any) -> str:
    """frontmatter 値を安全な YAML double-quoted スカラーにする。

    ``\\`` と ``"`` をエスケープし、改行は ``\\n`` エスケープ表現へ（YAML 的に有効・
    frontmatter ブロックを壊さない）。None は空文字列扱い。
    """
    s = "" if value is None else str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return f'"{s}"'


def tag_token(value: Any) -> str:
    """Obsidian タグ用トークンへ正規化する（``#`` は呼び出し側で付ける）。

    空白・``#``・区切り記号をアンダースコアへ。空になったら '' を返す（タグを出さない）。
    """
    s = "" if value is None else str(value)
    t = re.sub(r"[\s#,;:!?\"'()\[\]{}|^\\/<>*]+", "_", s.strip()).strip("_")
    return t


def wikilink(path: str) -> str:
    """``[[...]]`` wikilink を組む（path は safe_filename 由来＝``]``/``|`` を含まない）。"""
    return f"[[{path}]]"


# タイトル等を本文へ逐語出力する際の Markdown インラインエスケープ（単行想定）。
# frontmatter は yaml_quote が守るが、H1/見出し/リスト行に出す title は別途退避が要る。
# リンク/画像/wikilink/HTML/コードを成立させる記号を CommonMark のバックスラッシュ退避で殺す
# （描画上は元の文字＝可読性は不変）。バックスラッシュを最初に退避する。
_MD_INLINE_DANGEROUS = ("\\", "`", "[", "]", "<", ">")


def md_inline_escape(text: Any) -> str:
    """title 等を Markdown 見出し/インライン行へ逐語出力する際の危険記号退避（改行は畳む）。"""
    s = str(text if text is not None else "").replace("\n", " ").replace("\r", " ")
    for ch in _MD_INLINE_DANGEROUS:
        s = s.replace(ch, "\\" + ch)
    return s


def source_link(source_uri: str | None, source_type: str | None) -> str | None:
    """出典リンク。gdrive は Drive view URL へ整形（app.py の _open_url イディオム）。

    file_id 抽出失敗時は元 source_uri に fail-open。slack:// 等はそのまま返す。
    """
    if source_type == "gdrive" or (source_uri or "").startswith("gdrive://"):
        fid = extract_drive_file_id(source_uri)
        if fid:
            return f"https://drive.google.com/file/d/{fid}/view"
    return source_uri


def render_claude_md() -> str:
    """Vault ルートの CLAUDE.md（使用ルール）を生成する。"""
    return (
        "# AiLa Vault 使用ルール\n"
        "\n"
        "- この Vault は **読み取りミラー** です。正（source of truth）は pgvector"
        "（RDS）で、ここでの編集は元データに反映されません。\n"
        f"- 最新の検索は {_SEARCH_URL} を使ってください（RLS 適用・常に最新）。\n"
        "- 再生成（上書き更新）: SSM トンネルを張ってから\n"
        "  `python scripts/export_vault.py --dsn postgresql://...@localhost:15432/... --commit`\n"
        "- `clients/` はクライアントカルテ（FB 時系列 + 関連資料）、`docs/` は資料 note。\n"
        "- 機密資料を含むため、この Vault を共有フォルダ/リポジトリへ置かないでください。\n"
    )


def render_client_note(
    client: str,
    timeline: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    doc_paths: list[str],
) -> str:
    """クライアントカルテ note を生成する。

    timeline は list_client_timeline 相当の行 dict（**古い順**）。表示は新しい順へ反転。
    最新フェーズ/BANT は末尾（最新）の行から取り、業界は資料側 cls_industry の最頻値。
    doc_paths は documents と同順の Vault 相対パス（拡張子なし・wikilink 用）。
    """
    latest = timeline[-1] if timeline else {}
    industries = [str(d["cls_industry"]) for d in documents if d.get("cls_industry")]
    industry = Counter(industries).most_common(1)[0][0] if industries else ""

    lines: list[str] = [
        "---",
        f"client: {yaml_quote(client)}",
        f"industry: {yaml_quote(industry)}",
        f"deal_phase: {yaml_quote(latest.get('deal_phase') or '')}",
        f"bant_score: {yaml_quote(latest.get('bant_score') or '')}",
        f"fb_count: {len(timeline)}",
        f"doc_count: {len(documents)}",
        "---",
        "",
        f"# {client}",
        "",
    ]
    tags = [t for t in (tag_token(client), tag_token(industry)) if t]
    if tags:
        lines.append(" ".join(f"#{t}" for t in tags))
        lines.append("")

    lines.append("## 営業FB時系列（新しい順）")
    lines.append("")
    if not timeline:
        lines.append("（FB の記録はまだありません）")
        lines.append("")
    for row in reversed(timeline):
        occurred = row.get("occurred_at") or "----"
        title = str(row.get("title") or "(無題)").replace("\n", " ")
        lines.append(f"### {occurred} {title}")
        lines.append("")
        pairs = [
            ("フェーズ", row.get("deal_phase")),
            ("BANT", row.get("bant_score")),
            ("チャネル", row.get("channel_type")),
            ("ポジ反応", row.get("positive_reaction")),
            ("ネガ反応", row.get("negative_reaction")),
            ("次アクション", row.get("next_action")),
            ("提案メニュー", row.get("proposed_menu")),
        ]
        for label, value in pairs:
            if value:
                lines.append(f"- {label}: {str(value).replace(chr(10), ' ')}")
        content = str(row.get("content") or "").strip()
        if content:
            lines.append("")
            lines.append("> " + content[:300].replace("\n", " "))
        link = source_link(row.get("source_uri"), "slack")
        if link:
            lines.append("")
            lines.append(f"[出典]({link})")
        lines.append("")

    lines.append("## 関連資料")
    lines.append("")
    if not documents:
        lines.append("（関連資料はまだありません）")
        lines.append("")
    for doc, path in zip(documents, doc_paths, strict=True):
        title = md_inline_escape(doc.get("title") or "(無題)")
        parts = [wikilink(path)]
        if doc.get("cls_doc_type"):
            parts.append(str(doc["cls_doc_type"]))
        if doc.get("modified_at"):
            parts.append(str(doc["modified_at"]))
        link = source_link(doc.get("source_uri"), doc.get("source_type"))
        entry = f"- {' / '.join(parts)} … {title}"
        if link:
            entry += f" ([open]({link}))"
        lines.append(entry)
    lines.append("")
    return "\n".join(lines)


def render_doc_note(doc: dict[str, Any], client: str, client_path: str) -> str:
    """資料 note を生成する（frontmatter + excerpt + 出典リンク + タグ + カルテへの wikilink）。"""
    lines: list[str] = [
        "---",
        f"title: {yaml_quote(doc.get('title') or '')}",
        f"doc_type: {yaml_quote(doc.get('cls_doc_type') or '')}",
        f"client: {yaml_quote(client)}",
        f"industry: {yaml_quote(doc.get('cls_industry') or '')}",
        f"solution: {yaml_quote(doc.get('cls_solution') or '')}",
        # 名寄せタグ（cls_entities）: この資料に登場する取引先/代理店/ブランド/コラボ相手を
        # CSV で保持（build_app_html が front() で拾い、/app の「関係先/」タグ・検索に展開する）。
        # 値は entity_extract 側で正規化済み＝カンマ非包含なので CSV が壊れない。
        f"entities: {yaml_quote(doc.get('cls_entities') or '')}",
        f"modified_at: {yaml_quote(doc.get('modified_at') or '')}",
    ]
    # ナレッジ共有メタ（カテゴリ/クライアント種別/提案プロダクト）は値があるときだけ出す。
    # build_app_html が front() で拾い、docTags の カテゴリ/クライアント種別/提案プロダクト
    # 軸へ展開する。空値の行は既存 note と同一出力を保つ（回帰なし）。
    for _key, _val in (
        ("category", doc.get("cls_category")),
        ("client_tier", doc.get("cls_client_tier")),
        ("product", doc.get("cls_product")),
    ):
        if _val:
            lines.append(f"{_key}: {yaml_quote(_val)}")
    lines += [
        "---",
        "",
        # H1 は本文なので Markdown 記法を退避（研究ノート title 等の逐語出力での注入を防ぐ）。
        f"# {md_inline_escape(doc.get('title') or '(無題)')}",
        "",
    ]
    # 名寄せタグ: 登場する取引先/ブランド名も Obsidian タグとして出す（グラフ/タグ検索用）。
    entity_tags = [tag_token(e) for e in str(doc.get("cls_entities") or "").split(",") if e.strip()]
    tags = [
        t
        for t in (
            tag_token(doc.get("cls_doc_type")),
            tag_token(client),
            tag_token(doc.get("cls_industry")),
            tag_token(doc.get("cls_solution")),
            *entity_tags,
        )
        if t
    ]
    if tags:
        # dict.fromkeys で順序を保って重複タグを除去
        lines.append(" ".join(f"#{t}" for t in dict.fromkeys(tags)))
        lines.append("")
    excerpt = str(doc.get("excerpt") or "").strip()
    if doc.get("x_research_tool"):
        # 施策研究ノートは要約 markdown を全文そのまま本文にする（構造保持・外部リンク非依存で
        # Vault に残る。SQL 側で研究 doc は全文 excerpt を返している）。
        if excerpt:
            lines.append(excerpt)
            lines.append("")
    elif excerpt:
        lines.append("> " + excerpt.replace("\n", " "))
        lines.append("")
    link = source_link(doc.get("source_uri"), doc.get("source_type"))
    if link:
        lines.append(f"- 出典: [{doc.get('source_type') or 'source'}]({link})")
    lines.append(f"- 取引先: {wikilink(client_path)}")
    lines.append("")
    return "\n".join(lines)


def plan_vault(clients: dict[str, dict[str, list[dict[str, Any]]]]) -> dict[str, str]:
    """クライアント別データから Vault の {相対パス: Markdown 本文} を組む（純関数・冪等）。

    clients: {client_name: {"timeline": FB 行 dict list（古い順）,
                            "documents": 資料行 dict list（新しい順）}}
    返り値のキーは Vault 相対パス（例 "clients/出光興産.md"）。同一資料（source_uri 一致）
    は最初のクライアントの note を再利用し、名前衝突は ``_2`` ``_3`` … を付番して回避する。
    付番は **files への実在チェック** で空きを探す（出現回数カウントだと「提案書_2」の
    ような天然の ``_2`` 名と衝突し、note が黙って上書き消失し wikilink が別資料を指す）。
    """
    files: dict[str, str] = {"CLAUDE.md": render_claude_md()}
    doc_path_by_uri: dict[str, str] = {}

    def _claim(prefix: str, base: str) -> str:
        """files に無い ``{prefix}/{base}`` 系の空きパス（拡張子なし）を返す。"""
        candidate = base
        i = 1
        while f"{prefix}/{candidate}.md" in files:
            i += 1
            candidate = f"{base}_{i}"
        return f"{prefix}/{candidate}"

    for client, data in sorted(clients.items()):
        timeline = list(data.get("timeline") or [])
        documents = list(data.get("documents") or [])

        # クライアント note は各イテレーション末尾で必ず files へ入り、docs/ 側が
        # clients/ を占有することは無いので、この実在チェックが予約を兼ねる。
        client_path = _claim("clients", safe_filename(client, fallback="client"))

        doc_paths: list[str] = []
        for doc in documents:
            uri = str(doc.get("source_uri") or "")
            if uri and uri in doc_path_by_uri:
                # 同一資料は既存 note を再利用（複数クライアントから wikilink される）
                doc_paths.append(doc_path_by_uri[uri])
                continue
            base = safe_filename(doc.get("title"), fallback="document")
            # タイトルが行ごとに一意でない経路は、衝突すると _claim が `_2` を振る →
            # build_app_html の _chunk_key が `_2` を分割断片とみなして /app から握り潰す。
            # document ごとに一意な external_id 由来の短ハッシュを付けて衝突自体を無くす
            # (`-` 区切りなので `_N` 束ねに当たらない)。stem は内部キーで、UI は title を出す
            # (build_app_html の IDX/dByStem) ため表示は汚れない。対象:
            #   - x_research_tool: 「商材名 Xの声集め」等で別owner/別研究が衝突
            #   - gsheets: ナレッジ共有の title は「正式社名 案件名」で行ごとに一意ではない
            #     (実測 142 行中 8 stem が衝突。案件名が空の行は社名だけに潰れて更に衝突する)
            if doc.get("x_research_tool") or doc.get("source_type") == "gsheets":
                seed = str(doc.get("external_id") or uri or doc.get("title") or "")
                disc = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
                base = f"{base}-{disc}"
            doc_path = _claim("docs", base)
            if uri:
                doc_path_by_uri[uri] = doc_path
            files[f"{doc_path}.md"] = render_doc_note(doc, client, client_path)
            doc_paths.append(doc_path)

        files[f"{client_path}.md"] = render_client_note(client, timeline, documents, doc_paths)
    return files


def write_vault(out_dir: Path, files: dict[str, str], *, commit: bool) -> dict[str, int]:
    """計画された Vault ファイルを書き出す（既定 dry-run＝一覧表示のみ・commit で上書き）。"""
    stats = {"planned": len(files), "written": 0}
    resolved_root = out_dir.resolve()
    for rel in sorted(files):
        target = (out_dir / rel).resolve()
        # 二重防御: safe_filename 済でも out_dir 外への書き出しは拒否する。
        if not target.is_relative_to(resolved_root):
            raise ValueError(f"unsafe path escapes vault root: {rel}")
        if commit:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(files[rel], encoding="utf-8")
            stats["written"] += 1
        else:
            print(f"  [dry-run] {rel}")
    return stats


# -----------------------------------------------------------
# DB 読み取り部（SELECT のみ・SSM トンネル越し admin DSN 直結）
# -----------------------------------------------------------
_CLIENTS_SQL = """
    SELECT DISTINCT name FROM (
        SELECT d.metadata->>'client_name' AS name FROM documents d
        WHERE d.metadata->>'client_name' IS NOT NULL
          AND d.metadata->>'client_name' <> ''
        UNION
        SELECT d.metadata->>'cls_project' AS name FROM documents d
        WHERE d.metadata->>'cls_project' IS NOT NULL
          AND d.metadata->>'cls_project' <> ''
          -- 施策研究ノート(x_research_tool 付き)の cls_project は「商材/テーマ名」であって
          -- 取引先ではない。取引先タクソノミー(名寄せ/facet/グラフ)を汚さないため client 一覧に
          -- 昇格させない（needs/buzz を未永続化にしたのと同じ姿勢＝名寄せ前は取引先化しない）。
          -- 商材名が既存取引先に substring 一致する場合は DOCUMENTS_SQL の cls_project ILIKE で
          -- その取引先へ自然に紐づく（＝実在取引先へは載る／新規の偽取引先は作らない）。
          AND d.metadata->>'x_research_tool' IS NULL
    ) AS names
    ORDER BY name
"""

_TIMELINE_SQL = """
    SELECT
        COALESCE(c.contextualized, c.content) AS content,
        to_char(d.modified_at, 'YYYY-MM-DD') AS occurred_at,
        d.source_uri,
        d.title,
        d.metadata->>'client_name' AS client_name,
        d.metadata->>'deal_phase' AS deal_phase,
        d.metadata->>'bant_score' AS bant_score,
        d.metadata->>'channel_type' AS channel_type,
        d.metadata->>'positive_reaction' AS positive_reaction,
        d.metadata->>'negative_reaction' AS negative_reaction,
        d.metadata->>'next_action' AS next_action,
        d.metadata->>'proposed_menu' AS proposed_menu
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE d.metadata->>'is_sales_fb' = 'true'
      AND d.metadata->>'client_name' LIKE %s
    ORDER BY d.modified_at DESC NULLS LAST, c.chunk_idx DESC
    LIMIT %s
"""
# ↑ DESC LIMIT で【最新 N 件】を取り、Python 側で reversed して古い順へ戻す。
#   ASC LIMIT だと FB が per_client_limit を超えるクライアントで「最古の N 件」になり、
#   カルテ frontmatter（最新 deal_phase/bant_score）と時系列が古い値で誤るため。

_DOCUMENTS_SQL_TEMPLATE = """
    SELECT
        d.title,
        d.source_uri,
        d.source_type::text AS source_type,
        to_char(d.modified_at, 'YYYY-MM-DD') AS modified_at,
        d.metadata->>'cls_industry' AS cls_industry,
        d.metadata->>'cls_project' AS cls_project,
        d.metadata->>'cls_doc_type' AS cls_doc_type,
        d.metadata->>'cls_solution' AS cls_solution,
        d.metadata->>'cls_entities' AS cls_entities,
        -- ナレッジ共有メタ（フォーム回答/ファイル記録シート由来・人間入力）を射影する。
        -- 出力列名は cls_* に倣うが読む metadata キーは人間入力側（knowledge_kind /
        -- client_type / proposed_menu）で、Haiku の cls_* 名前空間とは交差しない。
        d.metadata->>'knowledge_kind' AS cls_category,
        d.metadata->>'client_type' AS cls_client_tier,
        d.metadata->>'proposed_menu' AS cls_product,
        d.metadata->>'client_name' AS client_name,
        d.metadata->>'x_research_tool' AS x_research_tool,
        d.external_id AS external_id,
        ex.excerpt AS excerpt
    FROM documents d
    LEFT JOIN LATERAL (
        -- 施策研究ノート(x_research_tool 付き)は要約全文を Vault に残す（外部リンク非依存）。
        -- 通常資料は従来どおり先頭160字の抜粋（Vault 肥大を避ける）。
        SELECT CASE
                 WHEN d.metadata->>'x_research_tool' IS NOT NULL
                 THEN COALESCE(c.contextualized, c.content)
                 ELSE left(COALESCE(c.contextualized, c.content), 160)
               END AS excerpt
        FROM chunks c
        WHERE c.document_id = d.id
        ORDER BY (COALESCE((c.metadata->>'boilerplate')::bool, false)
                  OR COALESCE((c.metadata->>'title_only')::bool, false)) ASC,
                 c.chunk_idx ASC
        LIMIT 1
    ) ex ON true
    WHERE d.metadata->>'suppressed' IS DISTINCT FROM 'true'
      AND d.metadata->>'is_sales_fb' IS DISTINCT FROM 'true'
      {stale_clause}
      AND (d.metadata->>'cls_project' ILIKE %s
           OR d.metadata->>'client_name' ILIKE %s
           OR d.title ILIKE %s)
    ORDER BY d.modified_at DESC NULLS LAST
    LIMIT %s
"""

# 入れ込み v2 (2026-07-10): ingest の stale soft-delete（metadata.stale='true'）を
# Vault から既定で除外する（Drive 上から消えた/検索対象外に移された資料を配らない）。
_STALE_EXCLUDE_CLAUSE = "AND d.metadata->>'stale' IS DISTINCT FROM 'true'"


def documents_sql(*, include_stale: bool = False) -> str:
    """資料 SELECT SQL を組む（純関数・テスト対象）。

    既定は stale 除外。``--include-stale`` 指定時のみ従来挙動（stale も含める）。
    埋め込むのは固定リテラル節のみ（ユーザー入力は入らない）。
    """
    return _DOCUMENTS_SQL_TEMPLATE.format(
        stale_clause="" if include_stale else _STALE_EXCLUDE_CLAUSE
    )


def load_clients_data(
    dsn: str,
    *,
    client: str | None = None,
    limit: int | None = None,
    per_client_limit: int = 100,
    include_stale: bool = False,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """クライアント一覧（client_name ∪ cls_project）→ 各クライアントの FB/資料を読む。

    SELECT のみ（read-only）。--client 指定時はその部分一致 1 件系に絞り、--limit は
    クライアント数の先頭 N 件キャップ（検証用）。include_stale=False（既定）で
    metadata.stale='true' の資料を除外する（--include-stale で従来挙動）。
    """
    import psycopg
    from psycopg.rows import dict_row

    docs_sql = documents_sql(include_stale=include_stale)

    clients_data: dict[str, dict[str, list[dict[str, Any]]]] = {}
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(_CLIENTS_SQL)
            names = [str(r["name"]) for r in cur.fetchall() if r.get("name")]
        if client:
            needle = client.strip().lower()
            names = [n for n in names if needle in n.lower()]
        if limit:
            names = names[: int(limit)]
        for name in names:
            like = f"%{name}%"
            with conn.cursor() as cur:
                cur.execute(_TIMELINE_SQL, [like, per_client_limit])
                # DESC で取った最新 N 件を古い順へ戻す（render_client_note の
                # 「timeline は古い順・末尾＝最新」契約を保つ）
                timeline = [dict(r) for r in reversed(cur.fetchall())]
            with conn.cursor() as cur:
                cur.execute(docs_sql, [like, like, like, per_client_limit])
                documents = [dict(r) for r in cur.fetchall()]
            if timeline or documents:
                clients_data[name] = {"timeline": timeline, "documents": documents}
    logger.info("export_vault_loaded", clients=len(clients_data))
    return clients_data


def main() -> int:
    import os

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dsn",
        default=None,
        help="Postgres DSN（SSM ポートフォワード前提）。省略時は DATABASE_URL",
    )
    p.add_argument(
        "--out",
        default=_DEFAULT_OUT,
        help=f"Vault 出力先ディレクトリ（既定 {_DEFAULT_OUT}）",
    )
    p.add_argument("--commit", action="store_true", help="既定 dry-run。指定時のみ書き出し")
    p.add_argument("--limit", type=int, default=None, help="先頭 N クライアントのみ（検証用）")
    p.add_argument("--client", default=None, help="クライアント名の部分一致で絞り込み")
    p.add_argument(
        "--include-stale",
        action="store_true",
        help="metadata.stale='true' の資料も含める（既定は除外・従来挙動に戻すフラグ）",
    )
    args = p.parse_args()

    dsn = args.dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        print("[ERROR] --dsn か DATABASE_URL を指定してください", file=sys.stderr)
        return 2

    out_dir = Path(args.out).expanduser()
    try:
        clients_data = load_clients_data(
            dsn, client=args.client, limit=args.limit, include_stale=args.include_stale
        )
        files = plan_vault(clients_data)
        stats = write_vault(out_dir, files, commit=args.commit)
    except Exception as e:
        print(f"[ERROR] export failed: {e}", file=sys.stderr)
        return 2

    mode = "commit" if args.commit else "dry-run"
    print(
        f"clients={len(clients_data)} planned={stats['planned']} "
        f"written={stats['written']} out={out_dir} mode={mode}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
