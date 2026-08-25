"""documents / 営業FB を Obsidian Vault へエクスポートする（read-only・ローカル書き出しのみ）。

Karpathy 式 Second Brain の「wiki 層」の組織版。pgvector（正）を読み取り、
Obsidian で開ける Vault ミラーをローカルに生成する:

- ``clients/<client>.md``  … クライアントカルテ note
  frontmatter: client / industry / 最新 deal_phase / bant_score / fb_count / doc_count
  本文: FB 時系列（新しい順）+ 関連資料リスト（[[docs/...]] wikilink + Drive URL）
- ``docs/<安全なファイル名>.md`` … 資料 note
  frontmatter: doc_type / client / industry / solution / entities / modified_at
  + source_type / external_id（配信フィルタ専用の内部メタ。UI には出さない）
  （entities=cls_entities の名寄せタグ CSV。/app の「関係先/」タグ・検索へ展開される）
  本文: excerpt + 出典（Drive/Slack）リンク + タグ（#提案書 #出光興産 #祇園辻利 等）
  + [[clients/<client>]] wikilink
- ``CLAUDE.md`` … Vault 使用ルール（読み取りミラー・正は pgvector・検索は connect-web）

設計（scripts/backfill_doc_kind.py と同流儀）:
- DB は **SELECT のみ**（UPDATE/INSERT/DDL 一切なし）。書くのはローカル Vault ファイルだけ。
- 既定 **dry-run**（生成予定ファイル一覧を表示するだけ）。``--commit`` で初めて書き出す。
- 再実行は同パスへの上書き＝冪等（DB 側の更新が Vault に反映される）。
- ``--prune`` は完全 export のときだけ有効。manifest で本スクリプトの生成物と確認できた
  ``clients/*.md`` / ``docs/*.md`` の古い note だけを削除する（dry-run では予定表示のみ）。
  ``--client`` / ``--limit`` との併用は拒否し、前回 manifest の半分未満に縮む計画も拒否する。
- クライアント名/タイトル→ファイル名は ``safe_filename`` で必ずサニタイズ
  （パストラバーサル・OS 禁止文字・wikilink 破壊文字・長さ・予約名）。
- frontmatter 値は YAML double-quoted スカラーへエスケープ（``yaml_quote``）。
- DB 接続は SSM ポートフォワード前提の --dsn 引数（無ければ DATABASE_URL）。
  実行は人間ゲート（本スクリプトを自動実行しない）。
- admin DSN は RLS を bypass するため、``--shared-group`` で明示した単一の会社
  共有ドメインを ``documents.acl_groups`` に持つ行だけを SQL で選ぶ。
  owner-only / acl_emails-only / acl_groups 空の資料は Vault や静的 ``/app`` に出さない。
- 静的 ``/app`` は per-user 表示ではなく、指定会社の共有集合ミラー。
  raw ACL は Vault/frontmatter/HTML へ書き出さない。

Usage:
    # SSM ポートフォワードを張ってから（例: localhost:15432 → RDS）
    python scripts/export_vault.py --dsn postgresql://user:pass@localhost:15432/db \
        --shared-group vectorinc.co.jp --prune                 # 完全 export + 削除予定の dry-run
    python scripts/export_vault.py --dsn ... --shared-group vectorinc.co.jp \
        --commit --prune                                       # 書き出し + 古い生成物削除
    # ACL 初回移行で 50% 超縮小が確認済みの場合だけ、dry-run/commit の両方へ追加:
    #   --allow-prune-shrink
    python scripts/export_vault.py --dsn ... --shared-group vectorinc.co.jp \
        --out ~/Vaults/aila --commit --prune
    python scripts/export_vault.py --dsn ... --shared-group vectorinc.co.jp \
        --client 出光興産 --commit                             # 1 クライアントだけ（prune 不可）
    python scripts/export_vault.py --dsn ... --shared-group vectorinc.co.jp --limit 5

``--shared-group`` 省略時は ``TEAMAGENT_SHARED_COMPANY_DOMAINS`` の単一値を使える。
未設定・空・カンマ区切り・不正ドメインは DB を読まず exit 2。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

import structlog  # noqa: E402

from teamagent.client_identity import (  # noqa: E402
    CLIENT_LEGAL_FORMS,
    client_identity_key,
)
from teamagent.client_properties import (  # noqa: E402
    identity_value_map,
    resolve_client_industry_with_source,
)
from teamagent.skills.knowledge_deliver.skill import extract_drive_file_id  # noqa: E402

logger = structlog.get_logger(__name__)

_DEFAULT_OUT = "~/AiLaVault"
_SEARCH_URL = "https://connect.newstv.co.jp/search"
_MANIFEST_NAME = ".export-vault-manifest.json"
_MANIFEST_VERSION = 1
_MANIFEST_GENERATOR = "scripts/export_vault.py"
_GENERATED_BY_FIELD = f"generated_by: {json.dumps(_MANIFEST_GENERATOR)}"
_MANAGED_DIRS = frozenset({"clients", "docs"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CLIENT_INDUSTRY_PATH = _ROOT / "data" / "connect_web_filters" / "client_industry.json"


def _load_client_industry_master() -> dict[str, str]:
    """Load the reviewed company-industry map and fail before any export if invalid."""

    try:
        payload = json.loads(_CLIENT_INDUSTRY_PATH.read_text(encoding="utf-8"))
        values = payload["industry"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("invalid client industry master") from exc
    if not isinstance(values, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in values.items()
    ):
        raise RuntimeError("invalid client industry master")
    return identity_value_map(values)


CLIENT_INDUSTRY_BY_IDENTITY = _load_client_industry_master()

# export は会社共有の「単一 DNS ドメイン」だけを受け取る。複数値の解釈を
# 呼び出し側ごとに変えないため、カンマ区切りは明示的に拒否する。
_DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_SHARED_GROUP_RE = re.compile(rf"{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})+")

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
    - Unicode を NFC へ正規化し、macOS で NFC/NFD の別名が同一 inode に潰れるのを防ぐ
    - OS 禁止文字（Windows 含む）・制御文字・wikilink/タグ破壊文字（``[]#^|``）を ``_`` 化
    - 連続空白を 1 個に圧縮・前後空白除去・max_len 文字で切り詰め
    - Windows 予約名（CON/NUL/COM1 等）は末尾 ``_`` を付けて回避
    - 空になったら fallback
    """
    base = unicodedata.normalize("NFC", str(name or "")).strip()
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


def source_discriminator(external_id: str) -> str:
    """安定 source ID を filename 用 64-bit hex discriminator へ変換する。"""
    return hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:16]


def _portable_path_key(path: str) -> str:
    """macOS/Windows が同一視する Unicode・大文字小文字差を衝突キーへ畳む。"""
    return unicodedata.normalize("NFC", path).casefold()


def normalize_shared_group(raw: str | None) -> str:
    """会社共有 group を SQL パラメータ用の単一 DNS ドメインへ正規化する。

    空、カンマ区切り、内部空白、非 ASCII、DNS ドメインでない値は
    ``ValueError`` にし、admin DSN 上で意図せず広い集合を出力しない。
    """
    if not isinstance(raw, str):
        raise ValueError("shared group must be a single DNS domain")
    value = raw.strip().lower()
    if (
        not value
        or "," in value
        or not value.isascii()
        or any(ch.isspace() for ch in value)
        or len(value) > 253
        or _SHARED_GROUP_RE.fullmatch(value) is None
    ):
        raise ValueError(
            "shared group must be a single valid DNS domain (comma-separated values forbidden)"
        )
    return value


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
        "# Aico Vault 使用ルール\n"
        "\n"
        "- この Vault は **読み取りミラー** です。正（source of truth）は pgvector"
        "（RDS）で、ここでの編集は元データに反映されません。\n"
        f"- 最新の検索は {_SEARCH_URL} を使ってください（RLS 適用・常に最新）。\n"
        "- この静的ミラーは per-user 表示ではなく、指定会社の共有 group 付き"
        "資料だけを含みます。owner-only 資料は含みません。\n"
        "- 再生成（上書き更新）: SSM トンネルを張ってから\n"
        "  `python scripts/export_vault.py --dsn postgresql://...@localhost:15432/... "
        "--shared-group <company.example> --commit --prune`\n"
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
    最新フェーズ/BANT は末尾（最新）の行から取る。業界は監査済みマスターを優先し、
    マスターが無い場合は DB の主担当 client_name / cls_project がこの取引先と完全一致する
    全資料の業界が1種類に合意したときだけ採用する。不一致は推測せず空欄へ倒し、title
    だけが一致した汎用事例は取引先プロパティへ混ぜない。
    doc_paths は documents と同順の Vault 相対パス（拡張子なし・wikilink 用）。
    """
    latest = timeline[-1] if timeline else {}
    client_key = client_identity_key(client)
    primary_industries = [
        str(d["cls_industry"])
        for d in documents
        if d.get("cls_industry")
        and client_key
        and client_identity_key(d.get("client_name")) == client_key
    ]
    project_industries = [
        str(d["cls_industry"])
        for d in documents
        if d.get("cls_industry")
        and client_key
        and client_identity_key(d.get("cls_project")) == client_key
    ]
    industry, industry_source = resolve_client_industry_with_source(
        client,
        primary_industries,
        project_industries,
        CLIENT_INDUSTRY_BY_IDENTITY,
    )

    lines: list[str] = [
        "---",
        _GENERATED_BY_FIELD,
        f"client: {yaml_quote(client)}",
        f"industry: {yaml_quote(industry)}",
        f"industry_source: {yaml_quote(industry_source)}",
        f"deal_phase: {yaml_quote(latest.get('deal_phase') or '')}",
        f"bant_score: {yaml_quote(latest.get('bant_score') or '')}",
        f"fb_count: {len(timeline)}",
        f"doc_count: {len(documents)}",
        "---",
        "",
        # H1 は本文なので Markdown 記法を退避（client 名の逐語出力での注入を防ぐ）。
        f"# {md_inline_escape(client)}",
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
        # 見出し/リスト/blockquote へ逐語出力する FB 由来テキスト（Slack 本文・LLM 分類）は
        # md_inline_escape で退避する。frontmatter は yaml_quote が守るためここは本文のみ。
        occurred = md_inline_escape(row.get("occurred_at") or "----")
        title = md_inline_escape(row.get("title") or "(無題)")
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
                # label は固定文字列。value（deal_phase/BANT 等＝LLM 分類由来）のみ退避。
                lines.append(f"- {label}: {md_inline_escape(value)}")
        content = str(row.get("content") or "").strip()
        if content:
            lines.append("")
            # blockquote 本文＝Slack メッセージ原文（最も攻撃者到達しやすい）。300字に切ってから退避。
            lines.append("> " + md_inline_escape(content[:300]))
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


def render_doc_note(doc: dict[str, Any], client_path: str = "", project_path: str = "") -> str:
    """資料noteをDBの主担当/案件プロパティと対応するbacklink付きで生成する。"""
    primary_client = unicodedata.normalize("NFC", str(doc.get("client_name") or "")).strip()
    project = unicodedata.normalize("NFC", str(doc.get("cls_project") or "")).strip()
    lines: list[str] = [
        "---",
        _GENERATED_BY_FIELD,
        f"title: {yaml_quote(doc.get('title') or '')}",
        f"doc_type: {yaml_quote(doc.get('cls_doc_type') or '')}",
        # ループ中のtitle-match clientではなくDB source truthをそのまま保持する。
        f"client: {yaml_quote(primary_client)}",
        f"project: {yaml_quote(project)}",
        f"industry: {yaml_quote(doc.get('cls_industry') or '')}",
        f"solution: {yaml_quote(doc.get('cls_solution') or '')}",
        # 名寄せタグ（cls_entities）: この資料に登場する取引先/代理店/ブランド/コラボ相手を
        # CSV で保持（build_app_html が front() で拾い、/app の「関係先/」タグ・検索に展開する）。
        # 値は entity_extract 側で正規化済み＝カンマ非包含なので CSV が壊れない。
        f"entities: {yaml_quote(doc.get('cls_entities') or '')}",
        f"modified_at: {yaml_quote(doc.get('modified_at') or '')}",
        # connect-web の除外はタイトル変更で外れない source identity を使う。
        # build_app_html は除外判定にだけ利用し、HTML payload へは載せない内部メタ。
        f"source_type: {yaml_quote(doc.get('source_type') or '')}",
        f"external_id: {yaml_quote(doc.get('external_id') or '')}",
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
            tag_token(primary_client),
            tag_token(project),
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
    if client_path:
        lines.append(f"- 取引先: {wikilink(client_path)}")
    if project_path and project_path != client_path:
        lines.append(f"- 案件: {wikilink(project_path)}")
    lines.append("")
    return "\n".join(lines)


def plan_vault(clients: dict[str, dict[str, list[dict[str, Any]]]]) -> dict[str, str]:
    """クライアント別データから Vault の {相対パス: Markdown 本文} を組む（純関数・冪等）。

    clients: {client_name: {"timeline": FB 行 dict list（古い順）,
                            "documents": 資料行 dict list（新しい順）}}
    返り値のキーは Vault 相対パス（例 "clients/出光興産.md"）。同一資料（source_uri 一致）
    は1つのnoteを再利用するが、client/projectはDB値で固定し、検索順へ依存させない。
    名前衝突は ``_2`` ``_3`` … を付番して回避する。
    付番は **files への実在チェック** で空きを探す（出現回数カウントだと「提案書_2」の
    ような天然の ``_2`` 名と衝突し、note が黙って上書き消失し wikilink が別資料を指す）。
    """
    files: dict[str, str] = {"CLAUDE.md": render_claude_md()}
    claimed_path_keys = {_portable_path_key(path) for path in files}
    doc_path_by_uri: dict[str, str] = {}
    doc_path_by_source_key: dict[tuple[str, str], str] = {}
    doc_ownership_by_path: dict[str, tuple[str, str]] = {}

    def _claim(prefix: str, base: str, *, duplicate_separator: str = "_") -> str:
        """portable filesystem 上で未使用の ``{prefix}/{base}`` 系パスを予約して返す。"""
        candidate = base
        i = 1
        path = f"{prefix}/{candidate}"
        while _portable_path_key(f"{path}.md") in claimed_path_keys:
            i += 1
            candidate = f"{base}{duplicate_separator}{i}"
            path = f"{prefix}/{candidate}"
        claimed_path_keys.add(_portable_path_key(f"{path}.md"))
        return path

    # 先に全client pathを予約し、共有資料のnoteが「最初にtitle一致したclient」ではなく
    # DB client_name / cls_project の正しいcardへbacklinkできるようにする。
    client_items = sorted(clients.items())
    client_path_by_name: dict[str, str] = {}
    client_path_by_identity: dict[str, str] = {}
    for client, _data in client_items:
        client_path = _claim("clients", safe_filename(client, fallback="client"))
        client_path_by_name[client] = client_path
        identity = client_identity_key(client)
        if identity:
            client_path_by_identity.setdefault(identity, client_path)

    for client, data in client_items:
        timeline = list(data.get("timeline") or [])
        documents = list(data.get("documents") or [])
        client_path = client_path_by_name[client]

        doc_paths: list[str] = []
        for doc in documents:
            primary_client = unicodedata.normalize("NFC", str(doc.get("client_name") or "")).strip()
            project = unicodedata.normalize("NFC", str(doc.get("cls_project") or "")).strip()
            ownership = (primary_client, project)
            uri = str(doc.get("source_uri") or "")
            source_type = str(doc.get("source_type") or "")
            external_id = str(doc.get("external_id") or "").strip()
            source_key = (source_type, external_id) if source_type and external_id else None
            if source_key and source_key in doc_path_by_source_key:
                # URL が空/変更済みでも同じ stable identity は同一論理資料として再利用する。
                reused_path = doc_path_by_source_key[source_key]
                if doc_ownership_by_path[reused_path] != ownership:
                    raise ValueError(
                        "same source identity has conflicting client ownership: "
                        f"{source_key[0]}:{source_key[1]}"
                    )
                if uri:
                    doc_path_by_uri[uri] = reused_path
                doc_paths.append(reused_path)
                continue
            if uri and uri in doc_path_by_uri:
                # 同一資料は既存 note を再利用（複数クライアントから wikilink される）
                reused_path = doc_path_by_uri[uri]
                if doc_ownership_by_path[reused_path] != ownership:
                    raise ValueError(f"same source URI has conflicting client ownership: {uri}")
                if source_key:
                    doc_path_by_source_key[source_key] = reused_path
                doc_paths.append(reused_path)
                continue
            needs_discriminator = bool(doc.get("x_research_tool") or source_type == "gsheets")
            # discriminator 付きは UTF-8 4 byte/文字でも一般的な 255-byte の
            # filename 上限内に収める（55*4 + '-' + 16hex + '.md' = 240 bytes）。
            base = safe_filename(
                doc.get("title"),
                fallback="document",
                max_len=55 if needs_discriminator else 80,
            )
            # タイトルが行ごとに一意でない経路は、衝突すると _claim が `_2` を振る →
            # build_app_html の _chunk_key が `_2` を分割断片とみなして /app から握り潰す。
            # document ごとに一意な external_id 由来の 64-bit discriminator を付けて
            # 衝突自体を無くす（8 hex/32-bit は実際に衝突する入力があるため使わない）。
            # (`-` 区切りなので `_N` 束ねに当たらない)。stem は内部キーで、UI は title を出す
            # (build_app_html の IDX/dByStem) ため表示は汚れない。対象:
            #   - x_research_tool: 「商材名 Xの声集め」等で別owner/別研究が衝突
            #   - gsheets: ナレッジ共有の title は「正式社名 案件名」で行ごとに一意ではない
            #     (実測 142 行中 8 stem が衝突。案件名が空の行は社名だけに潰れて更に衝突する)
            if needs_discriminator:
                if not external_id:
                    raise ValueError(
                        "external_id is required for gsheets/x_research document filenames"
                    )
                disc = source_discriminator(external_id)
                base = f"{base}-{disc}"
            # 異なる external_id で 64-bit hash の二次衝突が起きても、``_2`` にして
            # build_app_html の chunk 結合へ誤認させない。両方を見える形で保持する。
            doc_path = _claim(
                "docs",
                base,
                duplicate_separator="-dup-" if needs_discriminator else "_",
            )
            if source_key:
                doc_path_by_source_key[source_key] = doc_path
            if uri:
                doc_path_by_uri[uri] = doc_path
            doc_ownership_by_path[doc_path] = ownership
            primary_path = (
                client_path_by_name.get(primary_client)
                or client_path_by_identity.get(client_identity_key(primary_client))
                or ""
            )
            project_path = (
                client_path_by_name.get(project)
                or client_path_by_identity.get(client_identity_key(project))
                or ""
            )
            files[f"{doc_path}.md"] = render_doc_note(doc, primary_path, project_path)
            doc_paths.append(doc_path)

        files[f"{client_path}.md"] = render_client_note(client, timeline, documents, doc_paths)
    return files


def _is_managed_markdown_path(rel: str) -> bool:
    """manifest で所有管理できる、直下 1 階層の生成 Markdown パスか。"""
    if not rel or "\\" in rel:
        return False
    path = PurePosixPath(rel)
    return (
        not path.is_absolute()
        and path.as_posix() == rel
        and len(path.parts) == 2
        and path.parts[0] in _MANAGED_DIRS
        and path.name != ".md"
        and path.suffix == ".md"
    )


def _safe_target(out_dir: Path, rel: str) -> Path:
    """書込対象が Vault root 内に留まり、既存 symlink でもないことを検証する。"""
    resolved_root = out_dir.resolve()
    raw_target = out_dir / rel
    target = raw_target.resolve()
    if not target.is_relative_to(resolved_root):
        raise ValueError(f"unsafe path escapes vault root: {rel}")
    # write_text は symlink のリンク先を書き換えるため、root 内を指す symlink も拒否する。
    if raw_target.is_symlink():
        raise ValueError(f"unsafe symlink target in vault plan: {rel}")
    return raw_target


def _managed_target(out_dir: Path, rel: str) -> Path:
    """削除可能な所有対象を厳格に解決する（clients/docs 直下・md・symlink 不可）。"""
    if not _is_managed_markdown_path(rel):
        raise ValueError(f"unsafe managed path in export manifest: {rel!r}")
    prefix, name = PurePosixPath(rel).parts
    resolved_root = out_dir.resolve()
    raw_dir = out_dir / prefix
    expected_dir = resolved_root / prefix
    # clients/ や docs/ 自体が symlink の場合、別領域のファイルを消す恐れがあるので拒否する。
    if raw_dir.is_symlink() or raw_dir.resolve() != expected_dir:
        raise ValueError(f"unsafe managed directory in vault: {prefix}")
    if raw_dir.exists() and not raw_dir.is_dir():
        raise ValueError(f"managed directory is not a directory: {prefix}")
    raw_target = raw_dir / name
    if raw_target.is_symlink() or raw_target.resolve().parent != expected_dir:
        raise ValueError(f"unsafe managed target in vault: {rel}")
    return raw_target


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_export_manifest_state(
    out_dir: Path,
) -> tuple[dict[str, str], set[str], bool]:
    """生成物hash、直近の公開対象、完全export済みかをmanifestから読む。"""
    manifest = out_dir / _MANIFEST_NAME
    resolved_root = out_dir.resolve()
    if manifest.is_symlink() or manifest.resolve().parent != resolved_root:
        raise ValueError("unsafe export manifest path")
    if not manifest.exists():
        return {}, set(), False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid export manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid export manifest: root must be an object")
    if payload.get("version") != _MANIFEST_VERSION:
        raise ValueError("invalid export manifest: unsupported version")
    if payload.get("generator") != _MANIFEST_GENERATOR:
        raise ValueError("invalid export manifest: unexpected generator")
    raw_files = payload.get("files")
    if not isinstance(raw_files, dict):
        raise ValueError("invalid export manifest: files must be an object")

    files: dict[str, str] = {}
    for rel, digest in raw_files.items():
        if not isinstance(rel, str) or not _is_managed_markdown_path(rel):
            raise ValueError(f"unsafe managed path in export manifest: {rel!r}")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ValueError(f"invalid content hash in export manifest: {rel}")
        files[rel] = digest

    # active_files/complete_export は ACL-aware manifest 導入前には無い。旧manifestは
    # ownership/prune用として読めるが、build公開集合としては fail-closed（complete=False）。
    raw_active = payload.get("active_files", [])
    complete_export = payload.get("complete_export", False)
    if not isinstance(raw_active, list) or not all(isinstance(rel, str) for rel in raw_active):
        raise ValueError("invalid export manifest: active_files must be a string array")
    if len(raw_active) != len(set(raw_active)):
        raise ValueError("invalid export manifest: duplicate active_files entry")
    if not isinstance(complete_export, bool):
        raise ValueError("invalid export manifest: complete_export must be boolean")
    active_files = set(raw_active)
    for rel in active_files:
        if not _is_managed_markdown_path(rel) or rel not in files:
            raise ValueError(f"invalid active path in export manifest: {rel!r}")
    return files, active_files, complete_export


def _load_export_manifest(out_dir: Path) -> dict[str, str]:
    """前回までに本スクリプトが生成した note と内容 hash を読む。"""
    files, _, _ = _load_export_manifest_state(out_dir)
    return files


def _write_export_manifest(
    out_dir: Path,
    files: dict[str, str],
    *,
    active_files: set[str],
    complete_export: bool,
) -> None:
    """manifest を同一ディレクトリの一時ファイルから atomic replace する。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = out_dir / _MANIFEST_NAME
    tmp = out_dir / f"{_MANIFEST_NAME}.tmp"
    resolved_root = out_dir.resolve()
    for path in (manifest, tmp):
        if path.is_symlink() or path.resolve().parent != resolved_root:
            raise ValueError("unsafe export manifest path")
    if not active_files <= files.keys():
        raise ValueError("active export paths must be present in manifest files")
    payload = {
        "version": _MANIFEST_VERSION,
        "generator": _MANIFEST_GENERATOR,
        "complete_export": complete_export,
        "active_files": sorted(active_files),
        "files": dict(sorted(files.items())),
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(manifest)


def _frontmatter_keys(text: str) -> set[str]:
    """先頭 YAML frontmatter のキー名だけを依存ライブラリなしで読む。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return set()
    keys: set[str] = set()
    for line in lines[1:]:
        if line.strip() == "---":
            return keys
        match = re.match(r"^([a-z][a-z0-9_]*):", line)
        if match:
            keys.add(match.group(1))
    return set()


def _looks_like_generated_note(path: Path, prefix: str) -> bool:
    """manifest 導入前の export_vault 生成 note を強い構造シグネチャで判定する。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    keys = _frontmatter_keys(text)
    if prefix == "clients":
        required = {"client", "industry", "deal_phase", "bant_score", "fb_count", "doc_count"}
        return required <= keys and "## 営業FB時系列（新しい順）" in text and "## 関連資料" in text
    # entities は名寄せ導入前の旧 gsheets note（row 53 等）には無いため必須にしない。
    # その代わり exporter 固有の本文構造も全て要求し、一般 Markdown の誤採用を避ける。
    required = {"title", "doc_type", "client", "industry", "solution", "modified_at"}
    return (
        required <= keys
        and re.search(r"(?m)^# \S", text) is not None
        and "- 出典:" in text
        and "- 取引先: [[clients/" in text
    )


def _discover_generated_markdown(out_dir: Path) -> dict[str, str]:
    """初回 prune 用に clients/docs 直下の旧 exporter 生成 Markdown だけを発見する。"""
    resolved_root = out_dir.resolve()
    found: dict[str, str] = {}
    for prefix in sorted(_MANAGED_DIRS):
        directory = out_dir / prefix
        expected_dir = resolved_root / prefix
        if not directory.exists():
            continue
        if directory.is_symlink() or directory.resolve() != expected_dir or not directory.is_dir():
            raise ValueError(f"unsafe managed directory in vault: {prefix}")
        for path in directory.iterdir():
            # 非再帰・小文字 .md・regular file のみ。symlink はリンク先に関係なく対象外。
            if path.is_symlink() or not path.is_file() or path.suffix != ".md":
                continue
            rel = f"{prefix}/{path.name}"
            if _is_managed_markdown_path(rel) and _looks_like_generated_note(path, prefix):
                found[rel] = _sha256_file(path)
    return found


def _plan_prune(
    out_dir: Path,
    previous: dict[str, str],
    current: dict[str, str],
    *,
    allow_shrink: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """削除可能な stale note と、変更済みのため保持する stale note を分ける。"""
    # DB 障害や filter 誤りで空/激減した plan を「全削除」と解釈しない安全弁。
    # fresh Vault（previous も空）でも prune 付きの空 export は成功扱いにしない。
    if not current:
        raise ValueError("refusing prune: managed Markdown plan is empty")
    if len(current) * 2 < len(previous) and not allow_shrink:
        raise ValueError(
            "refusing prune: managed Markdown plan is less than 50% of the previous manifest "
            f"({len(current)} < {len(previous)} / 2)"
        )

    current_path_keys = {_portable_path_key(rel) for rel in current}
    # APFS/HFS+ の既定設定では NFC/NFD や大文字小文字だけが異なるパスは同一 inode を
    # 指し得る。旧manifestの別名を stale として unlink すると、直前に書いた current 側まで
    # 消してしまうため、portable key が現行集合にある alias は manifest から忘れるだけにする。
    stale = sorted(
        rel
        for rel in set(previous) - set(current)
        if _portable_path_key(rel) not in current_path_keys
    )
    if not stale:
        return [], {}

    deletions: list[str] = []
    protected: dict[str, str] = {}
    for rel in stale:
        target = _managed_target(out_dir, rel)
        if not target.exists():
            # 既に無いものは manifest から自然に落とす。unlink は呼ばない。
            continue
        if not target.is_file():
            protected[rel] = "not a regular file"
            continue
        if _sha256_file(target) != previous[rel]:
            # 生成後に人が編集した note は所有物と断定せず、削除も manifest 忘却もしない。
            protected[rel] = "content changed since export"
            continue
        deletions.append(rel)
    return deletions, protected


def write_vault(
    out_dir: Path,
    files: dict[str, str],
    *,
    commit: bool,
    prune: bool = False,
    complete_export: bool = False,
    allow_prune_shrink: bool = False,
) -> dict[str, int]:
    """計画を Vault へ反映し、明示された完全 export だけ古い生成 note を削除する。

    ``commit=False`` は書込・削除・manifest 更新を一切しない。manifest は生成 Markdown の
    相対パス/content hashに加え、直近の完全exportでACLを通ったactive集合を持つ。
    未管理 note や人が編集した note はpruneせずローカル保護するが、active公開集合には入れない。
    ``complete_export=False``（部分 export）で ``prune=True`` は常に拒否する。
    """
    if prune and not complete_export:
        raise ValueError("refusing prune for partial export (--client/--limit)")
    if allow_prune_shrink and not prune:
        raise ValueError("--allow-prune-shrink requires --prune")

    # 1 件でも危険な書込先があれば、何も変更する前に全 plan を拒否する。
    targets = {rel: _safe_target(out_dir, rel) for rel in files}
    current_manifest = {
        rel: _sha256_text(content)
        for rel, content in files.items()
        if _is_managed_markdown_path(rel)
    }
    for rel in current_manifest:
        _managed_target(out_dir, rel)

    if commit or prune:
        previous_manifest, previous_active, _ = _load_export_manifest_state(out_dir)
    else:
        previous_manifest, previous_active = {}, set()
    if prune:
        # manifest 導入前に残った stale/non-company note も、旧 exporter 固有の構造を
        # 満たすものだけ初回所有候補へ加える。既存 manifest の hash を常に優先する。
        for rel, digest in _discover_generated_markdown(out_dir).items():
            previous_manifest.setdefault(rel, digest)
    deletions: list[str] = []
    protected: dict[str, str] = {}
    if prune:
        deletions, protected = _plan_prune(
            out_dir,
            previous_manifest,
            current_manifest,
            allow_shrink=allow_prune_shrink,
        )

    stats = {
        "planned": len(files),
        "written": 0,
        "delete_planned": len(deletions),
        "deleted": 0,
    }
    if not commit:
        for rel in sorted(files):
            print(f"  [dry-run] {rel}")
        for rel in deletions:
            print(f"  [dry-run] delete {rel}")
        for rel, reason in sorted(protected.items()):
            print(f"  [prune-skip] {rel}: {reason}")
        return stats

    for rel in sorted(files):
        target = targets[rel]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(files[rel], encoding="utf-8")
        stats["written"] += 1

    # manifest の hash と今の内容が一致することを unlink 直前にも確認する（競合時 fail-safe）。
    retained = dict(protected)
    for rel in deletions:
        target = _managed_target(out_dir, rel)
        try:
            if not target.is_file() or _sha256_file(target) != previous_manifest[rel]:
                retained[rel] = "content changed before delete"
                continue
            target.unlink()
        except FileNotFoundError:
            continue
        stats["deleted"] += 1
        print(f"  [prune] deleted {rel}")

    if prune:
        next_manifest = dict(current_manifest)
        for rel in retained:
            next_manifest[rel] = previous_manifest[rel]
    else:
        # 部分 export や prune 無しの更新で、他 note の所有記録を縮めない。
        next_manifest = dict(previous_manifest)
        next_manifest.update(current_manifest)
    if complete_export:
        # 公開対象は「今回のACL付き完全SELECTから生成した集合」だけ。prune-skipで
        # ローカル保護した旧/手編集noteはownership filesに残してもactiveへ戻さない。
        next_active = set(current_manifest)
        next_complete = True
    else:
        # partial は以前の完全snapshotに別時点/別shared-groupの一部を混ぜ得る。
        # active hashはownership継続用に保持するが、必ずcomplete=Falseへ落として
        # build_app_htmlに次の完全exportを要求する。
        next_active = (set(previous_active) | set(current_manifest)) & set(next_manifest)
        next_complete = False
    _write_export_manifest(
        out_dir,
        next_manifest,
        active_files=next_active,
        complete_export=next_complete,
    )
    return stats


# -----------------------------------------------------------
# DB 読み取り部（SELECT のみ・SSM トンネル越し admin DSN 直結）
# -----------------------------------------------------------
_SHARED_ACL_SQL = """EXISTS (
        SELECT 1
        FROM unnest(d.acl_groups) AS shared_acl(group_name)
        WHERE lower(btrim(shared_acl.group_name)) = lower(btrim(%s::text))
    )"""
# admin DSN は RLS を bypass する。そのため全 SELECT にこの必須謂語を埋め込む。
# unnest('{}') に行はないので、acl_groups 空や owner/acl_emails だけの document は
# EXISTS=false の fail-closed になる。ACL 値そのものは SELECT 列に含めない。

# 短い取引先名をタイトル/metadata の単語途中へ誤爆させないための「左境界」。
# POSIX alnum は DB locale によって非 ASCII の扱いが変わるため、日本語・全角英数・
# 結合文字を明示して、C locale でも「レポート」「株主パスポート」内の「ポート」を
# 境界開始とみなさない。一方、右境界は課さず「ポート株式会社」のような正式社名を、
# 法人格 alternative で「株式会社ポート」のような前置正式社名も引き続き拾う。
_CLIENT_WORD_CHARS = (
    "[:alnum:]_Ａ-Ｚａ-ｚ０-９一-鿿々〆〇ぁ-ゖゝ-ゟァ-ヺーヽ-ヿｦ-ﾟ\u0300-\u036f\u3099-\u309a"
)
_CLIENT_LEGAL_PREFIX_PATTERN = "|".join(re.escape(form) for form in CLIENT_LEGAL_FORMS)
_CLIENT_LEFT_BOUNDARY = rf"(^|[^{_CLIENT_WORD_CHARS}]|{_CLIENT_LEGAL_PREFIX_PATTERN})"
_CLIENT_LEFT_BOUNDARY_JAPANESE = rf"(^|[^{_CLIENT_WORD_CHARS}]|_|{_CLIENT_LEGAL_PREFIX_PATTERN})"
_JAPANESE_CLIENT_RE = re.compile(r"[一-鿿々〆〇ぁ-ゖゝ-ゟァ-ヺーヽ-ヿｦ-ﾟ]")
_CLIENT_ASCII_DATE_PREFIX = r"^[0-9]{8}_?"
_CLIENT_ASCII_RIGHT_BOUNDARY = r"($|[^A-Za-z0-9])"
_PG_REGEX_META_RE = re.compile(r"([\\.^$|?*+()\[\]{}])")


def client_match_pattern(name: str) -> str:
    """取引先名の左境界付き PostgreSQL regex を安全に組む。

    値は呼び出し側で必ず bind parameter のまま渡す。ここでは PostgreSQL ARE の
    metacharacter を literal escape し、LIKE の ``%`` / ``_`` も通常文字として扱う。
    NFC 化は SQL 側の ``normalize(..., NFC)`` と対になり、表記の合成差を吸収する。
    """
    normalized = unicodedata.normalize("NFC", str(name)).strip()
    if not normalized:
        raise ValueError("client name must not be blank")
    literal = _PG_REGEX_META_RE.sub(r"\\\1", normalized)
    if _JAPANESE_CLIENT_RE.search(normalized):
        # Drive/Sheets の実タイトルは ``20250919_ポート株式会社`` のように ``_`` を
        # 区切りへ使う。日本語名に限って左 ``_`` を許可する。
        return f"{_CLIENT_LEFT_BOUNDARY_JAPANESE}{literal}"
    return f"{_CLIENT_LEFT_BOUNDARY}{literal}"


def client_title_match_pattern(name: str) -> str:
    """資料 title 専用の境界付き PostgreSQL regex を組む。

    metadata/timeline は :func:`client_match_pattern` の strict 境界だけを使う。英字名の
    title に限り、実ファイル名の先頭8桁日付を安全な区切りとして追加で許可する。
    """
    strict = client_match_pattern(name)
    normalized = unicodedata.normalize("NFC", str(name)).strip()
    if _JAPANESE_CLIENT_RE.search(normalized):
        return strict

    literal = _PG_REGEX_META_RE.sub(r"\\\1", normalized)
    # ``20251113_PIVOT媒体資料`` / ``20260514NewsTV`` のtitle-only正資料を復元する。
    # literal直後のASCII英数字を拒否し、portfolio等のprefix誤爆を防ぐ
    # （日本語説明・``_``・``/``・末尾は正しいファイル名の続きとして許可）。
    dated_filename = f"{_CLIENT_ASCII_DATE_PREFIX}{literal}{_CLIENT_ASCII_RIGHT_BOUNDARY}"
    return f"({strict}|{dated_filename})"


_CLIENTS_SQL = f"""
    SELECT DISTINCT name FROM (
        SELECT d.metadata->>'client_name' AS name FROM documents d
        WHERE d.metadata->>'client_name' IS NOT NULL
          AND d.metadata->>'client_name' <> ''
          AND {_SHARED_ACL_SQL}
        UNION
        SELECT d.metadata->>'cls_project' AS name FROM documents d
        WHERE d.metadata->>'cls_project' IS NOT NULL
          AND d.metadata->>'cls_project' <> ''
          AND {_SHARED_ACL_SQL}
          -- 施策研究ノート(x_research_tool 付き)の cls_project は「商材/テーマ名」であって
          -- 取引先ではない。取引先タクソノミー(名寄せ/facet/グラフ)を汚さないため client 一覧に
          -- 昇格させない（needs/buzz を未永続化にしたのと同じ姿勢＝名寄せ前は取引先化しない）。
          -- 商材名が既存取引先に左境界付き一致する場合は DOCUMENTS_SQL の cls_project regex で
          -- その取引先へ自然に紐づく（＝実在取引先へは載る／新規の偽取引先は作らない）。
          AND d.metadata->>'x_research_tool' IS NULL
    ) AS names
    ORDER BY name
"""

_TIMELINE_SQL = f"""
    SELECT
        COALESCE(c.contextualized, c.content) AS content,
        to_char(d.modified_at AT TIME ZONE 'Asia/Tokyo', 'YYYY-MM-DD') AS occurred_at,
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
    WHERE {_SHARED_ACL_SQL}
      AND d.metadata->>'is_sales_fb' = 'true'
      AND normalize(COALESCE(d.metadata->>'client_name', ''), NFC) ~* %s
    ORDER BY d.modified_at DESC NULLS LAST, c.chunk_idx DESC
    LIMIT %s
"""
# ↑ DESC LIMIT で【最新 N 件】を取り、Python 側で reversed して古い順へ戻す。
#   ASC LIMIT だと FB が per_client_limit を超えるクライアントで「最古の N 件」になり、
#   カルテ frontmatter（最新 deal_phase/bant_score）と時系列が古い値で誤るため。

_DOCUMENTS_SQL_TEMPLATE = f"""
    SELECT
        d.title,
        d.source_uri,
        d.source_type::text AS source_type,
        to_char(d.modified_at AT TIME ZONE 'Asia/Tokyo', 'YYYY-MM-DD') AS modified_at,
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
    WHERE {_SHARED_ACL_SQL}
      AND d.metadata->>'suppressed' IS DISTINCT FROM 'true'
      AND d.metadata->>'is_sales_fb' IS DISTINCT FROM 'true'
      {{stale_clause}}
      AND (normalize(COALESCE(d.metadata->>'cls_project', ''), NFC) ~* %s
           OR normalize(COALESCE(d.metadata->>'client_name', ''), NFC) ~* %s
           OR normalize(COALESCE(d.title, ''), NFC) ~* %s)
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
    shared_group: str,
    client: str | None = None,
    limit: int | None = None,
    per_client_limit: int = 100,
    include_stale: bool = False,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """クライアント一覧（client_name ∪ cls_project）→ 各クライアントの FB/資料を読む。

    SELECT のみ（read-only）。admin DSN が RLS を bypass しても、必須の
    ``shared_group`` を acl_groups に持つ company-shared document だけを全 3 経路で
    読む。空/複数/不正 group は DB 接続前に fail-closed。

    --client 指定時はその部分一致 1 件系に絞り、--limit はクライアント数の先頭
    N 件キャップ（検証用）。include_stale=False（既定）で metadata.stale='true'
    の資料を除外する（--include-stale で従来挙動）。
    """
    normalized_group = normalize_shared_group(shared_group)

    import psycopg
    from psycopg.rows import dict_row

    docs_sql = documents_sql(include_stale=include_stale)

    clients_data: dict[str, dict[str, list[dict[str, Any]]]] = {}
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # UNION の 2 枝に同じ shared group パラメータを個別に配線。
            cur.execute(_CLIENTS_SQL, (normalized_group, normalized_group))
            # SQL DISTINCT では NFC/NFD が別値になり得る。照合・出力名と同じ NFC/strip へ
            # 揃え、空白だけの値を除きつつ先勝ちで重複を落とす。
            names = list(
                dict.fromkeys(
                    normalized
                    for row in cur.fetchall()
                    if row.get("name")
                    and (normalized := unicodedata.normalize("NFC", str(row["name"])).strip())
                )
            )
        if client:
            needle = client.strip().lower()
            names = [n for n in names if needle in n.lower()]
        if limit:
            names = names[: int(limit)]
        for name in names:
            match_pattern = client_match_pattern(name)
            title_match_pattern = client_title_match_pattern(name)
            with conn.cursor() as cur:
                cur.execute(
                    _TIMELINE_SQL,
                    (normalized_group, match_pattern, per_client_limit),
                )
                # DESC で取った最新 N 件を古い順へ戻す（render_client_note の
                # 「timeline は古い順・末尾＝最新」契約を保つ）
                timeline = [dict(r) for r in reversed(cur.fetchall())]
            with conn.cursor() as cur:
                cur.execute(
                    docs_sql,
                    (
                        normalized_group,
                        match_pattern,
                        match_pattern,
                        title_match_pattern,
                        per_client_limit,
                    ),
                )
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
    p.add_argument(
        "--prune",
        action="store_true",
        help=(
            "完全 export で manifest 管理済みの古い clients/docs Markdown を削除。"
            "dry-run は削除予定のみ表示（--client/--limit と併用不可）"
        ),
    )
    p.add_argument(
        "--allow-prune-shrink",
        action="store_true",
        help=(
            "明示確認済みの大規模移行だけ、prune の前回比50%%ブレーキを解除。"
            "空 plan は解除不可（--prune 必須）"
        ),
    )
    p.add_argument("--limit", type=int, default=None, help="先頭 N クライアントのみ（検証用）")
    p.add_argument("--client", default=None, help="クライアント名の部分一致で絞り込み")
    p.add_argument(
        "--shared-group",
        default=None,
        help=(
            "必須: export 対象の単一会社共有ドメイン。省略時は "
            "TEAMAGENT_SHARED_COMPANY_DOMAINS の単一値"
        ),
    )
    p.add_argument(
        "--include-stale",
        action="store_true",
        help="metadata.stale='true' の資料も含める（既定は除外・従来挙動に戻すフラグ）",
    )
    args = p.parse_args()

    partial_export = args.client is not None or args.limit is not None
    if args.prune and partial_export:
        print("[ERROR] --prune は --client/--limit と併用できません", file=sys.stderr)
        return 2
    if args.allow_prune_shrink and not args.prune:
        print("[ERROR] --allow-prune-shrink は --prune と併用してください", file=sys.stderr)
        return 2

    shared_group_raw = (
        args.shared_group
        if args.shared_group is not None
        else os.environ.get("TEAMAGENT_SHARED_COMPANY_DOMAINS")
    )
    try:
        shared_group = normalize_shared_group(shared_group_raw)
    except ValueError as exc:
        print(
            "[ERROR] --shared-group に単一の有効な会社ドメインを指定するか、"
            f"TEAMAGENT_SHARED_COMPANY_DOMAINS を単一値にしてください: {exc}",
            file=sys.stderr,
        )
        return 2

    dsn = args.dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        print("[ERROR] --dsn か DATABASE_URL を指定してください", file=sys.stderr)
        return 2

    out_dir = Path(args.out).expanduser()
    try:
        clients_data = load_clients_data(
            dsn,
            shared_group=shared_group,
            client=args.client,
            limit=args.limit,
            include_stale=args.include_stale,
        )
        files = plan_vault(clients_data)
        stats = write_vault(
            out_dir,
            files,
            commit=args.commit,
            prune=args.prune,
            complete_export=not partial_export,
            allow_prune_shrink=args.allow_prune_shrink,
        )
    except Exception as e:
        print(f"[ERROR] export failed: {e}", file=sys.stderr)
        return 2

    mode = "commit" if args.commit else "dry-run"
    print(
        f"clients={len(clients_data)} planned={stats['planned']} "
        f"written={stats['written']} delete_planned={stats['delete_planned']} "
        f"deleted={stats['deleted']} out={out_dir} mode={mode}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
