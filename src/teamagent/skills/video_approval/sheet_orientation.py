"""案件スプレッドシート → OrientationBrief 抽出（動画一次FB審査の「正解条件」読取）。

ベクトル社の動画制作公募フローでは、案件 Slack チャンネルに 1 枚の公開スプレッドシート
があり、その中に「商材情報 / オリエン / 編集者の納品動画 Drive URL」が分散している。
このモジュールは、その実シート（伊藤園/NTV レイアウト）から 1 クリエイティブ分の
オリエンと納品動画 URL を取り出し、VideoApprovalSkill が使う OrientationBrief に整える。

実シートの“クセ”（2026-06-01 実機確認）:
- **ヘッダが 1 行目に無い**。上に案件名/KPI 等のバナー行が 1〜2 行ある。
  → 先頭数行を走査し、想定ヘッダ語を最も多く含む行をヘッダ行とみなす。
- **オリエンが 3 タブに分散**し、管理番号で結合する:
    1. 投稿管理シート: 1 行 = 1 クリエイティブ。管理番号 / 訴求軸 / フック型 /
       ハッシュタグ / 投稿文 / **FIX動画_格納URL（直リンク）= 納品動画**。
    2. 切り抜きマスター指示書: マスター番号で結合。**上バナー/下バナー = 必須テロップ**、
       想定フック / CTA / **インサート候補 = 必須シーン**。
    3. 派生指示書: 全体の派生/NG ルール（自由記述）。
  結合キー: 投稿管理.管理番号 == マスター指示書.マスター番号（例 "E01-01"）。

列の位置はクライアント毎に揺れるため**ヘッダ名（部分一致）で解決**する。
別レイアウトのシートは SheetLayout を足すだけで対応できる（コードは不変）。

3 層分離: これは Skill 層のオーケストレーション。シート I/O は adapters/gsheets_client、
スキーマは skills/video_approval/schema。googleapiclient は直接触らない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

from teamagent.adapters.drive_video import is_drive_url
from teamagent.adapters.gsheets_client import GSheetsClient
from teamagent.skills.video_approval.schema import OrientationBrief

logger = structlog.get_logger(__name__)


# -----------------------------------------------------------
# 正規化・ヘッダ解決ヘルパ
# -----------------------------------------------------------
def _norm(s: str) -> str:
    """突き合わせ用に正規化（空白/改行/全角空白を除去して小文字化）。"""
    return re.sub(r"[\s　]+", "", str(s)).lower()


def find_header_row(rows: list[list[str]], tokens: list[str], max_scan: int = 12) -> int:
    """先頭 max_scan 行から「ヘッダらしい行」を見つけて index を返す。

    各行について、tokens（期待ヘッダ語）が含まれるセル数を数え、最多の行を採用。
    バナー行（案件名/KPI 等）はヘッダ語をほとんど含まないので自然に弾かれる。
    見つからなければ 0（先頭行）を返す。max_scan を広めに取り、複数のバナー/別ブロックが
    上にあっても、ヘッダ語が最も多い「本物のヘッダ行」を選べるようにする。
    """
    norm_tokens = [_norm(t) for t in tokens]
    best_idx, best_hits = 0, 0
    for idx, row in enumerate(rows[:max_scan]):
        cells = [_norm(c) for c in row]
        hits = sum(1 for t in norm_tokens if any(t in c for c in cells))
        if hits > best_hits:
            best_idx, best_hits = idx, hits
    return best_idx


def build_header_index(header: list[str]) -> dict[str, int]:
    """ヘッダ行 → {正規化ヘッダ名: 最初に現れた列index}。

    同名ヘッダ（例: 管理番号が2箇所）は**最初の出現**を採用する。
    """
    index: dict[str, int] = {}
    for i, h in enumerate(header):
        key = _norm(h)
        if key and key not in index:
            index[key] = i
    return index


def band_header_index(
    all_rows: list[list[str]], header_idx: int, *, lookback: int = 2
) -> dict[str, int]:
    """ヘッダが複数行に跨る場合に対応した列見出し索引。

    実シートでは「グループ見出し(動画ステ/AIチェック/AI FB内容…)」が本ヘッダ行の
    1〜2 行上に置かれることがある。header_idx を含む直前 lookback 行を縦に走査し、
    各列について「非空セルの最後の値」を見出しとする(本ヘッダ行の値を優先しつつ、
    その列が本ヘッダ行で空なら上のグループ見出しを採用)。同名は最初の列を保持。
    """
    start = max(0, header_idx - lookback)
    width = max((len(all_rows[i]) for i in range(start, header_idx + 1)), default=0)
    index: dict[str, int] = {}
    for ci in range(width):
        label = ""
        for ri in range(start, header_idx + 1):
            row = all_rows[ri]
            val = str(row[ci]).strip() if ci < len(row) else ""
            if val:
                label = val  # 下の行(本ヘッダ)ほど優先。空なら上の見出しが残る
        key = _norm(label)
        if key and key not in index:
            index[key] = ci
    return index


def find_col(index: dict[str, int], *keywords: str) -> int | None:
    """正規化ヘッダ名に keyword（いずれか）を部分一致で含む最初の列を返す。"""
    for kw in keywords:
        nkw = _norm(kw)
        for key, i in index.items():
            if nkw in key:
                return i
    return None


def _cell(row: list[str], col: int | None) -> str:
    if col is None or col >= len(row):
        return ""
    return str(row[col]).strip()


def resolve_tab_title(
    client: GSheetsClient, sheet_id: str, keyword: str, request_id: str
) -> str | None:
    """タブ名キーワード（部分一致）から実タブ名を解決する。"""
    meta = client.get_sheet_metadata(sheet_id=sheet_id, request_id=request_id)
    nkw = _norm(keyword)
    for tab in meta.tabs:
        if nkw in _norm(tab.title):
            return tab.title
    return None


def col_letter(index: int) -> str:
    """0始まりの列 index を A1 列名へ（0→A, 25→Z, 26→AA）。"""
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def split_hashtags(raw: str) -> list[str]:
    """ハッシュタグセル（"#PR #伊藤園 #お茶" 等）を配列に分解。"""
    if not raw:
        return []
    parts = re.split(r"[\s　、,]+", raw.strip())
    tags: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        tags.append(p if p.startswith("#") else f"#{p}")
    return tags


# -----------------------------------------------------------
# レイアウト定義（クライアント毎の差異はここに閉じる）
# -----------------------------------------------------------
@dataclass(frozen=True)
class TabSpec:
    """1 タブの探し方とヘッダ検出語。"""

    name_keyword: str  # タブ名の部分一致（例 "投稿管理"）
    header_tokens: list[str]  # ヘッダ行検出に使う想定ヘッダ語


@dataclass(frozen=True)
class SheetLayout:
    """案件シートのレイアウト（伊藤園/NTV 既定）。

    列はヘッダ名キーワードで解決するため、列がずれても壊れにくい。
    """

    posting: TabSpec
    master: TabSpec
    derivation: TabSpec | None = None
    # 結合キー（投稿管理側 / マスター側のヘッダ語）
    join_posting_key: tuple[str, ...] = ("管理番号",)
    join_master_key: tuple[str, ...] = ("マスター番号", "管理番号")
    # 投稿管理側の列キーワード
    col_product: tuple[str, ...] = ("商材",)
    col_creative: tuple[str, ...] = ("クリエイティブ名", "クリエイティブ")
    col_appeal: tuple[str, ...] = ("訴求軸",)
    col_hook_type: tuple[str, ...] = ("フック", "表現の型")
    col_post_text: tuple[str, ...] = ("投稿文",)
    col_hashtags: tuple[str, ...] = ("ハッシュタグ",)
    col_kind: tuple[str, ...] = ("種別",)
    col_notes_posting: tuple[str, ...] = ("注意",)
    col_video_url: tuple[str, ...] = ("fix動画", "格納url", "動画url")
    # マスター指示書側の列キーワード
    col_context: tuple[str, ...] = ("文脈名",)
    col_upper_banner: tuple[str, ...] = ("上バナー",)
    col_lower_banner: tuple[str, ...] = ("下バナー",)
    col_expected_hook: tuple[str, ...] = ("想定フック",)
    col_cta: tuple[str, ...] = ("cta", "行動喚起")
    col_insert: tuple[str, ...] = ("インサート",)
    # AI 結果の書き戻し先（ユーザーがシートに用意した列の見出し）
    col_ai_verdict: tuple[str, ...] = ("AIチェック", "AI判定")
    col_ai_fb: tuple[str, ...] = ("AI FB内容", "AIFB内容", "AI_FB", "FB内容")


ITOEN_NTV_LAYOUT = SheetLayout(
    posting=TabSpec(
        name_keyword="投稿管理",
        header_tokens=["通し番号", "管理番号", "商材", "クリエイティブ名", "FIX動画"],
    ),
    master=TabSpec(
        name_keyword="マスター指示書",
        header_tokens=["マスター番号", "上バナー", "下バナー", "インサート"],
    ),
    derivation=TabSpec(name_keyword="派生指示書", header_tokens=["ルール"]),
)


# -----------------------------------------------------------
# 抽出結果
# -----------------------------------------------------------
@dataclass(frozen=True)
class CreativeRef:
    """投稿管理シート 1 行 = 1 クリエイティブの軽量参照（一覧/監視用）。"""

    management_no: str
    creative_name: str
    video_url: str
    has_drive_video: bool


@dataclass(frozen=True)
class OrientationExtract:
    """1 クリエイティブ分の抽出結果。"""

    management_no: str
    orientation: OrientationBrief
    video_url: str
    has_drive_video: bool


# -----------------------------------------------------------
# 抽出器本体
# -----------------------------------------------------------
@dataclass
class _LoadedTab:
    header_index: dict[str, int]
    rows: list[list[str]]  # ヘッダ行より下のデータ行


class OrientationExtractor:
    """案件シートから OrientationBrief + 納品動画 URL を取り出す。"""

    def __init__(
        self,
        client: GSheetsClient | None = None,
        *,
        layout: SheetLayout = ITOEN_NTV_LAYOUT,
        client_name: str | None = None,
    ) -> None:
        self._client = client
        self._layout = layout
        self._client_name = client_name

    def _gs(self) -> GSheetsClient:
        if self._client is None:
            self._client = GSheetsClient.from_env()
        return self._client

    # --- タブ読込 -------------------------------------------------
    def _resolve_tab_title(self, sheet_id: str, keyword: str, request_id: str) -> str | None:
        return resolve_tab_title(self._gs(), sheet_id, keyword, request_id)

    def _load_tab(self, sheet_id: str, spec: TabSpec, request_id: str) -> _LoadedTab | None:
        title = self._resolve_tab_title(sheet_id, spec.name_keyword, request_id)
        if title is None:
            logger.warning("orientation_tab_not_found", keyword=spec.name_keyword)
            return None
        tr = self._gs().get_tab_rows(sheet_id=sheet_id, tab_name=title, request_id=request_id)
        all_rows: list[list[str]] = [list(tr.headers)] + [list(r) for r in tr.rows]
        if not all_rows:
            return None
        h_idx = find_header_row(all_rows, spec.header_tokens)
        header_index = build_header_index(all_rows[h_idx])
        data_rows = all_rows[h_idx + 1 :]
        logger.info(
            "orientation_tab_loaded",
            tab=title,
            header_row=h_idx,
            data_rows=len(data_rows),
            cols=len(header_index),
        )
        return _LoadedTab(header_index=header_index, rows=data_rows)

    def _load_derivation_text(self, sheet_id: str, request_id: str) -> str:
        if self._layout.derivation is None:
            return ""
        title = self._resolve_tab_title(sheet_id, self._layout.derivation.name_keyword, request_id)
        if title is None:
            return ""
        tr = self._gs().get_tab_rows(sheet_id=sheet_id, tab_name=title, request_id=request_id)
        lines: list[str] = []
        for row in tr.rows:
            text = " ".join(str(c).strip() for c in row if str(c).strip())
            if text:
                lines.append(text)
        return "\n".join(lines)

    # --- 公開 API -------------------------------------------------
    def list_creatives(self, sheet_id: str, *, request_id: str = "orient") -> list[CreativeRef]:
        """投稿管理シートの全クリエイティブを軽量列挙（監視/一覧用）。"""
        posting = self._load_tab(sheet_id, self._layout.posting, request_id)
        if posting is None:
            return []
        idx = posting.header_index
        c_mgmt = find_col(idx, *self._layout.join_posting_key)
        c_name = find_col(idx, *self._layout.col_creative)
        c_url = find_col(idx, *self._layout.col_video_url)
        out: list[CreativeRef] = []
        for row in posting.rows:
            mgmt = _cell(row, c_mgmt)
            if not mgmt:
                continue
            url = _cell(row, c_url)
            out.append(
                CreativeRef(
                    management_no=mgmt,
                    creative_name=_cell(row, c_name),
                    video_url=url,
                    has_drive_video=is_drive_url(url),
                )
            )
        return out

    def extract(
        self, sheet_id: str, management_no: str, *, request_id: str = "orient"
    ) -> OrientationExtract | None:
        """管理番号 1 件分の OrientationBrief + 納品動画 URL を取り出す。"""
        posting = self._load_tab(sheet_id, self._layout.posting, request_id)
        if posting is None:
            logger.warning("orientation_posting_missing", management_no=management_no)
            return None
        p_idx = posting.header_index
        c_mgmt = find_col(p_idx, *self._layout.join_posting_key)
        target = _norm(management_no)
        prow: list[str] | None = None
        for row in posting.rows:
            if _norm(_cell(row, c_mgmt)) == target:
                prow = row
                break
        if prow is None:
            logger.warning("orientation_creative_not_found", management_no=management_no)
            return None

        # マスター指示書の対応行（無くても部分的に続行）
        master = self._load_tab(sheet_id, self._layout.master, request_id)
        mrow: list[str] = []
        m_idx: dict[str, int] = {}
        if master is not None:
            m_idx = master.header_index
            c_master_no = find_col(m_idx, *self._layout.join_master_key)
            for row in master.rows:
                if _norm(_cell(row, c_master_no)) == target:
                    mrow = row
                    break

        derivation_text = self._load_derivation_text(sheet_id, request_id)
        brief = self._build_brief(sheet_id, p_idx, prow, m_idx, mrow, derivation_text)
        video_url = _cell(prow, find_col(p_idx, *self._layout.col_video_url))
        return OrientationExtract(
            management_no=management_no,
            orientation=brief,
            video_url=video_url,
            has_drive_video=is_drive_url(video_url),
        )

    # --- OrientationBrief 組み立て ---------------------------------
    def _build_brief(
        self,
        sheet_id: str,
        p_idx: dict[str, int],
        prow: list[str],
        m_idx: dict[str, int],
        mrow: list[str],
        derivation_text: str,
    ) -> OrientationBrief:
        lay = self._layout

        creative = _cell(prow, find_col(p_idx, *lay.col_creative))
        product = _cell(prow, find_col(p_idx, *lay.col_product))
        appeal = _cell(prow, find_col(p_idx, *lay.col_appeal))
        hook_type = _cell(prow, find_col(p_idx, *lay.col_hook_type))
        post_text = _cell(prow, find_col(p_idx, *lay.col_post_text))
        kind = _cell(prow, find_col(p_idx, *lay.col_kind))
        posting_notes = _cell(prow, find_col(p_idx, *lay.col_notes_posting))
        hashtags = split_hashtags(_cell(prow, find_col(p_idx, *lay.col_hashtags)))

        upper = _cell(mrow, find_col(m_idx, *lay.col_upper_banner)) if mrow else ""
        lower = _cell(mrow, find_col(m_idx, *lay.col_lower_banner)) if mrow else ""
        expected_hook = _cell(mrow, find_col(m_idx, *lay.col_expected_hook)) if mrow else ""
        cta = _cell(mrow, find_col(m_idx, *lay.col_cta)) if mrow else ""
        insert = _cell(mrow, find_col(m_idx, *lay.col_insert)) if mrow else ""
        context = _cell(mrow, find_col(m_idx, *lay.col_context)) if mrow else ""

        required_telops = [t for t in (upper, lower) if t]
        required_scenes = [s for s in (insert,) if s]
        if cta:
            required_telops.append(cta)  # CTA も焼き込み想定なので必須テロップ扱い

        ng_items = _extract_ng(derivation_text)

        notes_parts: list[str] = []
        if context:
            notes_parts.append(f"文脈: {context}")
        if expected_hook:
            notes_parts.append(f"想定フック: {expected_hook}")
        if hook_type:
            notes_parts.append(f"フック・表現の型: {hook_type}")
        if kind:
            notes_parts.append(f"投稿種別: {kind}")
        if post_text:
            notes_parts.append(f"投稿文（参考）: {post_text}")
        if posting_notes:
            notes_parts.append(f"注意: {posting_notes}")
        if derivation_text:
            notes_parts.append(f"派生/レギュレーション:\n{derivation_text[:1500]}")

        product_name = self._client_name or product or None
        # クリエイティブ名は“この動画の主訴求/タイトル”なので main_message に寄せる
        main_message = " / ".join(p for p in (creative, appeal) if p) or None

        return OrientationBrief(
            product_name=product_name,
            main_message=main_message,
            required_scenes=required_scenes,
            required_telops=required_telops,
            ng_items=ng_items,
            format_spec=kind or None,
            hashtags=hashtags,
            notes="\n".join(notes_parts) or None,
        )


_NG_LINE_RE = re.compile(r"(NG|禁止|不可|してはいけない|避ける|使用しない)", re.IGNORECASE)


def _extract_ng(text: str) -> list[str]:
    """派生/レギュレーション自由記述から NG らしい行を拾う（取り過ぎない）。"""
    if not text:
        return []
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line and _NG_LINE_RE.search(line):
            out.append(line[:200])
        if len(out) >= 20:
            break
    return out
