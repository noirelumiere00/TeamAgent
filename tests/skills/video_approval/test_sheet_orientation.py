"""sheet_orientation.py のユニットテスト（実シートの形を模した合成フィクスチャ）。

実シート（伊藤園/NTV）の“クセ”を再現:
- ヘッダが先頭行に無い（上にバナー行が 1〜2 行）
- オリエンが 投稿管理 / マスター指示書 / 派生指示書 の 3 タブに分散
- 結合キーは 投稿管理.管理番号 == マスター指示書.マスター番号（"E01-01"）
"""

from __future__ import annotations

from teamagent.adapters.gsheets_client import SheetMetadata, SheetTab, TabRows
from teamagent.skills.video_approval.sheet_orientation import (
    OrientationExtractor,
    band_header_index,
    build_header_index,
    find_col,
    find_header_row,
    split_hashtags,
)

DRIVE_URL = "https://drive.google.com/file/d/ABC123ABC123ABC123ABC123x/view"

# --- 投稿管理シート（バナー2行 → 本ヘッダ → データ） ---------------------
_POSTING = [
    # sheet row1: バナー
    ["CL名", "", "", "伊藤園", "KPI", "250万回視聴/130本", "", "", "", "", "", "", "#PR"],
    # sheet row2: サブバナー
    ["案件名", "", "", "伊藤園記者発表会", "管理番号説明", "", "", "", "", "", "", "", ""],
    # sheet row3: 本ヘッダ（A..Z）
    [
        "通し番号",  # 0 A
        "管理番号",  # 1 B  ← 結合キー
        "商材",  # 2 C
        "クリエイティブ名",  # 3 D
        "投稿アカウント名",  # 4 E
        "種別",  # 5 F
        "注意",  # 6 G
        "投稿日時",  # 7 H
        "訴求軸①",  # 8 I
        "訴求軸",  # 9 J
        "フック・表現の型",  # 10 K
        "タイトル（YouTube用）",  # 11 L
        "投稿文",  # 12 M
        "ハッシュタグ",  # 13 N
        "文字数",  # 14 O
        "",
        "",
        "",
        "",
        "",
        "管理番号",  # 20 U（2つ目・無視される）
        "FIX動画_格納URL（直リンク）",  # 21 V ← 納品動画
    ],
    # data: E01-01（納品あり）
    [
        "1",
        "E01-01",
        "O",
        "日本茶の未来、ついに動き出した。",
        "NewsTV",
        "TikTok",
        "冒頭3秒は字幕必須",
        "2026/1/14",
        "純国産茶葉100%",
        "日本茶の価値再定義",
        "問いかけフック",
        "",
        "伊藤園が新戦略を発表。",
        "#PR #伊藤園 #お茶",
        "100",
        "",
        "",
        "",
        "",
        "",
        "E01-01",
        DRIVE_URL,
    ],
    # data: E01-02（未入稿＝ファイル名のみ）
    [
        "2",
        "E01-02",
        "O",
        "純・新・進——3つの答え。",
        "NewsTV",
        "YouTube",
        "",
        "",
        "純国産",
        "",
        "",
        "",
        "",
        "#PR",
        "",
        "",
        "",
        "",
        "",
        "",
        "E01-02",
        " 【E01-02】_02.mp4",
    ],
]

# --- マスター指示書（バナー1行 → 本ヘッダ → データ） ---------------------
_MASTER = [
    # sheet row1: バナー（ほぼ空、末尾に案件名）
    ["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "26_伊藤園"],
    # sheet row2: 本ヘッダ
    [
        "通し番号",  # 0
        "大分類",  # 1
        "文脈番号",  # 2
        "文脈名",  # 3
        "マスター番号",  # 4 ← 結合キー
        "役割",  # 5
        "担当D",  # 6
        "想定フック",  # 7
        "想定本編・切り抜きポイント",  # 8
        "上バナー",  # 9
        "下バナー",  # 10
        "CTA（行動喚起）",  # 11
        "投稿用タイトル",  # 12
        "投稿文",  # 13
        "ハッシュタグ（5つまで）",  # 14
        "動画URL",  # 15
        "戻し",  # 16
        "ステータス",  # 17
        "IN点",  # 18
        "OUT点",  # 19
        "インサート候補",  # 20
    ],
    # data: E01-01
    [
        "1",
        "経済パート",
        "1",
        "伊藤園が掲げる“日本茶の未来戦略”3本柱",
        "E01-01",
        "全体要約",
        "酒井",
        "日本茶の未来、ついに動き出した。",
        "3つのキーワード",
        "日本茶の未来戦略3本柱",
        "純・新・進で業界変革へ",
        "詳細は発表内容をチェック",
        "【発表】伊藤園",
        "伊藤園が新たな事業戦略",
        "#PR #伊藤園 #お茶 #日本茶",
        "https://youtu.be/src",
        "",
        "投稿済",
        "20:40",
        "21:09",
        "03:00地点で登壇資料P.12「3本柱概念図」を2秒挿入",
    ],
]

# --- 派生指示書（1列・自由記述） ------------------------------------------
_DERIVATION = [
    ["派生ルール表（指示書）"],
    ["ルール"],
    ["派生動画は、以下①【レイヤー】の組み合わせで派生化する。"],
    ["NG: 効果・効能を断定する表現は使用しない。"],
    ["禁止: 競合商品名を出してはいけない。"],
    ["通常の補足説明テキスト（参考情報）。"],
]


class _FakeGS:
    """GSheetsClient の get_sheet_metadata / get_tab_rows だけ模したスタブ。"""

    def __init__(self) -> None:
        self._tabs = {
            "施策全体スケジュール": [["A"], ["x"]],
            "投稿管理シート_NTV管理用（92）": _POSTING,
            "切り抜きマスター指示書（初稿）": _MASTER,
            "派生指示書": _DERIVATION,
        }

    def get_sheet_metadata(self, *, sheet_id: str, request_id: str) -> SheetMetadata:
        tabs = tuple(
            SheetTab(sheet_id=sheet_id, gid=i, title=t, row_count=len(rows), col_count=10)
            for i, (t, rows) in enumerate(self._tabs.items())
        )
        return SheetMetadata(sheet_id=sheet_id, title="伊藤園コピー", tabs=tabs)

    def get_tab_rows(
        self, *, sheet_id: str, tab_name: str, request_id: str, range_a1: str | None = None
    ) -> TabRows:
        rows = self._tabs[tab_name]
        headers = tuple(rows[0])
        body = tuple(tuple(r) for r in rows[1:])
        return TabRows(
            sheet_id=sheet_id,
            tab_name=tab_name,
            headers=headers,
            rows=body,
            row_count=len(body),
        )


def _extractor() -> OrientationExtractor:
    return OrientationExtractor(client=_FakeGS())  # type: ignore[arg-type]


# -----------------------------------------------------------
# ヘルパ単体
# -----------------------------------------------------------
def test_find_header_row_skips_banner_rows() -> None:
    all_rows = [list(r) for r in _POSTING]
    idx = find_header_row(all_rows, ["通し番号", "管理番号", "商材", "FIX動画"])
    assert idx == 2  # バナー2行を飛ばして本ヘッダを選ぶ


def test_find_header_row_falls_back_to_zero() -> None:
    rows = [["a", "b"], ["c", "d"]]
    assert find_header_row(rows, ["まったく無い語"]) == 0


def test_find_header_row_picks_richest_row_past_many_banners() -> None:
    """上に別ブロック/バナーが複数あっても、ヘッダ語が最多の本ヘッダ行を選ぶ。"""
    rows = [["x"]] * 9
    rows.append(["通し番号", "管理番号", "商材"])  # idx9: 3 hits（途中の弱いヘッダ）
    rows.append(["通し番号", "管理番号", "商材", "クリエイティブ名"])  # idx10: 4 hits（本命）
    tokens = ["通し番号", "管理番号", "商材", "クリエイティブ名"]
    assert find_header_row(rows, tokens) == 10


def test_band_header_index_merges_group_and_main_header() -> None:
    """グループ見出し(1行上)と本ヘッダ行を縦に合成して列を解決する。"""
    all_rows = [
        ["CL名", "", "", "伊藤園"],  # 0 バナー
        ["案件名", "", "", "", "", "動画ステ", "AIチェック", "AI　FB内容"],  # 1 グループ見出し
        ["通し番号", "管理番号", "商材", "クリエイティブ名"],  # 2 本ヘッダ
        ["1", "E01-01", "O", "日本茶の未来"],  # 3 データ
    ]
    idx = band_header_index(all_rows, 2, lookback=2)
    assert find_col(idx, "管理番号") == 1  # 本ヘッダ行から
    assert find_col(idx, "AIチェック") == 6  # グループ見出し行から
    assert find_col(idx, "AI FB内容", "AIFB内容") == 7  # 全角空白を無視して一致


def test_build_header_index_takes_first_occurrence() -> None:
    idx = build_header_index(["管理番号", "x", "管理番号"])
    assert idx["管理番号"] == 0  # 2つ目(idx2)ではなく最初


def test_find_col_partial_match_and_normalization() -> None:
    idx = build_header_index(["FIX動画_格納URL（直リンク）", "商　材"])
    assert find_col(idx, "fix動画") == 0
    assert find_col(idx, "商材") == 1  # 全角空白を無視して一致


def test_split_hashtags() -> None:
    assert split_hashtags("#PR #伊藤園 #お茶") == ["#PR", "#伊藤園", "#お茶"]
    assert split_hashtags("PR、伊藤園") == ["#PR", "#伊藤園"]
    assert split_hashtags("") == []


# -----------------------------------------------------------
# 抽出（結合あり）
# -----------------------------------------------------------
def test_extract_joins_master_by_management_no() -> None:
    res = _extractor().extract("sheetid", "E01-01")
    assert res is not None
    assert res.video_url == DRIVE_URL
    assert res.has_drive_video is True

    o = res.orientation
    # マスター指示書の上/下バナー = 必須テロップ、CTA も含む
    assert "日本茶の未来戦略3本柱" in o.required_telops
    assert "純・新・進で業界変革へ" in o.required_telops
    assert "詳細は発表内容をチェック" in o.required_telops
    # インサート候補 = 必須シーン
    assert any("3本柱概念図" in s for s in o.required_scenes)
    # ハッシュタグ
    assert "#伊藤園" in o.hashtags
    # クリエイティブ名が main_message に反映
    assert o.main_message is not None and "日本茶の未来" in o.main_message
    # 種別 → format_spec
    assert o.format_spec == "TikTok"
    # 注意書きが notes に乗る
    assert o.notes is not None and "字幕必須" in o.notes


def test_extract_ng_items_from_derivation() -> None:
    o = _extractor().extract("sheetid", "E01-01").orientation  # type: ignore[union-attr]
    joined = " / ".join(o.ng_items)
    assert "効果・効能" in joined
    assert "競合商品名" in joined
    # NGではない補足行は拾わない
    assert "通常の補足説明" not in joined


def test_extract_creative_without_drive_video() -> None:
    res = _extractor().extract("sheetid", "E01-02")
    assert res is not None
    assert res.has_drive_video is False  # ファイル名のみ＝未入稿
    # マスターに E01-02 行は無い → テロップ等は空でも落ちない
    assert res.orientation.required_telops == []


def test_extract_unknown_management_no_returns_none() -> None:
    assert _extractor().extract("sheetid", "ZZZ-99") is None


def test_extract_normalizes_management_no_whitespace() -> None:
    # 全角・前後空白が入っても結合できる
    res = _extractor().extract("sheetid", " E01-01 ")
    assert res is not None
    assert res.has_drive_video is True


# -----------------------------------------------------------
# 一覧（監視/トリガー用）
# -----------------------------------------------------------
def test_list_creatives_flags_drive_videos() -> None:
    refs = _extractor().list_creatives("sheetid")
    by_no = {r.management_no: r for r in refs}
    assert set(by_no) == {"E01-01", "E01-02"}
    assert by_no["E01-01"].has_drive_video is True
    assert by_no["E01-02"].has_drive_video is False
    assert by_no["E01-01"].creative_name.startswith("日本茶")


def test_client_name_override() -> None:
    ext = OrientationExtractor(client=_FakeGS(), client_name="伊藤園")  # type: ignore[arg-type]
    o = ext.extract("sheetid", "E01-01").orientation  # type: ignore[union-attr]
    assert o.product_name == "伊藤園"
