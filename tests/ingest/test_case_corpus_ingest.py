"""事例集 corpus 取込（B-10）のテスト。

対象:
- ``form_mappings.map_case_fields`` / ``normalize_case_external_use`` /
  ``find_case_ng_name`` / ``resolve_case_external_use``
- ``loader`` の ID 後入れ（``sheet_id_env`` / ``gid_env``・未設定なら skip）
- ``pipeline._ingest_gsheet`` の事例集経路（metadata 付与・sticky・fail-closed）
- **既存 2 シートへの副作用ゼロ**（cls_doc_type / cls_project が動かないこと）

フェイクは本番の失敗モードを再現する:
- 「対外利用可否」列が **存在しない** シート（列追加はユーザー検討中＝v1 の正常系）
- 空セル（未記入）・全角/半角の揺れた列名
- 再取込で列が消えた行（前回 ng）→ metadata 全置換で ⚠ が消える事故
- sticky 読み出しが例外（RDS 一時障害）→ ⚠ を降格させずタブごと見送る
- 0 行のタブ
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.ingest.form_mappings import (
    derive_knowledge_client_name,
    find_case_ng_name,
    map_case_fields,
    map_knowledge_fields,
    normalize_case_external_use,
    resolve_case_external_use,
)
from teamagent.ingest.loader import GSheetSpec, GSheetsTabSpec, load_ingest_sources
from teamagent.ingest.slack_fb_parser import map_fb_fields

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
REAL_YAML = PROJECT_ROOT / "data" / "ingest_sources.yaml"

CASE_SHEET_ID = "1CaseCorpusMasterSheetIdXXXXXXXXXXXXXXXXXXX"
CASE_GID = 1234567

# マスター表の想定ヘッダ（要望原文が名指しした 5 列。「対外利用可否」列は**まだ無い**）。
_CASE_HEADERS_NO_USE_COLUMN = ("カテゴリ", "企業名", "商材", "効果", "営業担当")
# 「対外利用可否」列が足された後のヘッダ。
_CASE_HEADERS_WITH_USE_COLUMN = (*_CASE_HEADERS_NO_USE_COLUMN, "対外利用可否")

# 既存 2 シートの実ヘッダ（誤爆しないことの証明に使う）。
_KNOWLEDGE_HEADERS = (
    "ファイルをアップ",
    "正式社名",
    "案件名",
    "クライアント種別",
    "提案プロダクト",
    "資料の概要",
    "このナレッジのポイントはここ！",
    "なぜそのナレッジ（資料）を共有したのか？",
    "フリーコメント",
    "送信者",
    "タイムスタンプ",
)
_FB_HEADERS = (
    "タイムスタンプ",
    "商流",
    "顧客名",
    "顧客名・案件名",
    "商談フェーズ",
    "商談感触（BANT）",
    "顧客反応(ポジ・ネガ)",
    "提案メニュー",
)


def _fields(headers: tuple[str, ...], values: tuple[str, ...]) -> dict[str, str]:
    return dict(zip(headers, values, strict=False))


# ===========================================================
# map_case_fields — 列写像・表記ゆれ・誤爆ゼロ
# ===========================================================
def test_map_case_fields_maps_master_table_columns() -> None:
    """マスター表の 5 列が case_* キーへ写像される（「対外利用可否」列は無い＝正常系）。"""
    out = map_case_fields(
        _fields(
            _CASE_HEADERS_NO_USE_COLUMN,
            (
                "観光・テーマパーク",
                "株式会社ジャングリア沖縄",
                "ジャングリア沖縄",
                "サテライトアカウント＋TTO80本でSNSのネガ情報比率を改善",
                "清水",
            ),
        )
    )
    assert out == {
        "case_category": "観光・テーマパーク",
        "case_company": "株式会社ジャングリア沖縄",
        "case_product": "ジャングリア沖縄",
        "case_effect": "サテライトアカウント＋TTO80本でSNSのネガ情報比率を改善",
        "case_owner": "清水",
    }
    # 生セルの受け皿（case_external_use_source）は列が無いので出てこない
    assert "case_external_use_source" not in out


def test_map_case_fields_absorbs_header_variants() -> None:
    """列名の表記ゆれ（全角/半角・別語・括弧注記・空白）を吸収する。

    実マスター表のヘッダは未確定なので、運用でありうる書き方を通す必要がある。
    """
    out = map_case_fields(
        {
            "業　種": "金融",  # 全角スペース入り・別語（業種→カテゴリ）
            "会社名": "ＪＣＢ",  # 別語 + 全角英字（NFKC で JCB へ）
            "商品名": "JCB×USJ",  # 別語
            "成果（数値）": "指名検索が前月比5倍",  # 別語 + 末尾括弧注記
            "営業 担当": "望月",  # 内部空白
        }
    )
    assert out == {
        "case_category": "金融",
        "case_company": "ＪＣＢ",  # 値は生のまま（正規化はヘッダだけ）
        "case_product": "JCB×USJ",
        "case_effect": "指名検索が前月比5倍",
        "case_owner": "望月",
    }


def test_map_case_fields_skips_empty_cells() -> None:
    """空セル（未記入）はキーごと落とす（空文字を metadata に残さない）。"""
    out = map_case_fields(
        _fields(_CASE_HEADERS_WITH_USE_COLUMN, ("食品", "伊藤ハム", "", "  ", "小池", ""))
    )
    assert out == {"case_category": "食品", "case_company": "伊藤ハム", "case_owner": "小池"}


def test_map_case_fields_returns_empty_for_knowledge_sheet() -> None:
    """ナレッジ共有シートの行は事例集として写像されない（副作用ゼロ）。

    「正式社名」は 企業名 の別名として受けるためコア一致は 1 つ出るが、閾値 3 に届かない。
    変異: _CASE_MIN_CORE_HITS を 1 に下げるとこのテストが赤になる。
    """
    row = _fields(
        _KNOWLEDGE_HEADERS,
        (
            "https://files.slack.com/x",
            "株式会社デルタ製薬",
            "新製品プロモーション",
            "メーカー",
            "ビデオリリース",
            "提案",
            "",
            "",
            "",
            "@山田",
            "2025/06/17 13:14:45",
        ),
    )
    assert map_case_fields(row) == {}
    # 逆向き: ナレッジ共有としては従来どおり写像される（既存経路は不変）
    assert map_knowledge_fields(row)["client_company"] == "株式会社デルタ製薬"


def test_map_case_fields_returns_empty_for_fb_sheet() -> None:
    """営業 FB シートの行も事例集として写像されない（コアが 1 つも交差しない）。"""
    row = _fields(
        _FB_HEADERS,
        ("2026/03/11 0:58:09", "直販", "SCSK", "SCSK・採用", "提案", "B", "ポジ", "タテガタ"),
    )
    assert map_case_fields(row) == {}
    assert map_fb_fields(row)  # FB 経路は従来どおり立つ


def test_map_case_fields_returns_empty_for_arbitrary_sheet() -> None:
    """無関係なシート・空 dict は空 dict（既存の任意シートに副作用ゼロ）。"""
    assert map_case_fields({}) == {}
    assert map_case_fields({"業界": "飲食", "温度感": "高"}) == {}  # コア 1 つだけ


# ===========================================================
# 対外利用可否の 3 値化
# ===========================================================
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("可", "ok"),
        ("OK", "ok"),
        ("○", "ok"),
        ("〇", "ok"),
        ("展開可", "ok"),
        ("ＯＫ", "ok"),  # 全角（NFKC）
        ("NG", "ng"),
        ("ng", "ng"),
        ("不可", "ng"),  # 「可」を含むが ng が勝つ
        ("×", "ng"),
        ("口頭紹介のみ", "ng"),
        ("クライアント展開NG", "ng"),
        ("confidential", "ng"),
        ("非公開", "ng"),  # 「公開」を含むが ng が勝つ
        ("", "unknown"),
        ("   ", "unknown"),
        (None, "unknown"),
        ("要確認", "unknown"),
        ("営業に確認", "unknown"),
    ],
)
def test_normalize_case_external_use(raw: str | None, expected: str) -> None:
    assert normalize_case_external_use(raw) == expected


def test_normalize_case_external_use_checks_ng_before_ok() -> None:
    """ng を先に評価する（順序が安全装置そのもの）。

    変異: ok マーカーを先に見るようにすると「不可」「非公開」が ok になり赤。
    """
    assert normalize_case_external_use("不可") == "ng"
    assert normalize_case_external_use("非公開") == "ng"
    assert normalize_case_external_use("口頭のみ可") == "ng"


# ===========================================================
# 名前（フォルダ名 / ファイル名 / シート名）由来の NG
# ===========================================================
def test_find_case_ng_name_detects_markers() -> None:
    assert (
        find_case_ng_name(["20260708_各社成功事例集★クライアント展開NG"])
        == "20260708_各社成功事例集★クライアント展開NG"
    )
    assert find_case_ng_name(["03｜事例（開示NG）"]) == "03｜事例（開示NG）"
    assert find_case_ng_name(["口頭紹介のみ_事例"]) == "口頭紹介のみ_事例"
    # 全角 NG / 小文字 ng も拾う
    assert find_case_ng_name(["展開ｎｇ資料"]) == "展開ｎｇ資料"


def test_find_case_ng_name_returns_none_for_safe_names() -> None:
    assert find_case_ng_name([]) is None
    assert find_case_ng_name([None, "", "📍ショート動画施策事例集"]) is None


# ===========================================================
# resolve_case_external_use — 単調性（sticky）
# ===========================================================
def test_resolve_uses_column_value_when_no_other_signal() -> None:
    assert resolve_case_external_use(column_value="可").value == "ok"
    assert resolve_case_external_use(column_value="NG").value == "ng"
    assert resolve_case_external_use(column_value="").value == "unknown"


def test_resolve_missing_column_is_unknown_not_ng() -> None:
    """「対外利用可否」列が無い状態を unknown に倒す（全件 ⚠ で狼少年にしない）。"""
    result = resolve_case_external_use(column_value=None, names=["📍ショート動画施策事例集"])
    assert result.value == "unknown"
    assert result.note is None


def test_resolve_name_marker_beats_ok_column() -> None:
    """列が「可」でもフォルダ名が展開NG なら ng（名前シグナルが勝つ）。

    変異: names の評価を消すと ok になって赤。
    """
    result = resolve_case_external_use(
        column_value="可",
        names=["20260708_各社成功事例集★クライアント展開NG"],
    )
    assert result.value == "ng"
    assert result.note == "20260708_各社成功事例集★クライアント展開NG"


def test_resolve_previous_ng_is_sticky_when_column_disappears() -> None:
    """前回 ng なら、列が空になっても・名前が安全になっても ng のまま（sticky）。

    documents.metadata は upsert で全置換されるので、ここで持ち上げないと
    再取込のたびに「⚠なしの NG 事例」が翌朝の DM に載る。
    変異: previous の分岐を消すと unknown になって赤。
    """
    result = resolve_case_external_use(
        column_value="",
        names=["📍ショート動画施策事例集"],
        previous="ng",
        previous_note="事例集フォルダが展開NG",
    )
    assert result.value == "ng"
    assert result.note == "事例集フォルダが展開NG"


def test_resolve_previous_ng_is_sticky_even_against_ok_column() -> None:
    """前回 ng は、列が「可」に書き換わっても降格しない。"""
    assert resolve_case_external_use(column_value="可", previous="ng").value == "ng"


def test_resolve_previous_ok_is_not_sticky() -> None:
    """ok は sticky にしない（unknown への劣化は安全側なので許す）。

    変異: previous を無条件に引き継ぐと ok が返って赤。
    """
    assert resolve_case_external_use(column_value="", previous="ok").value == "unknown"
    assert resolve_case_external_use(column_value="", previous="unknown").value == "unknown"


# ===========================================================
# loader — ID 後入れ（sheet_id_env / gid_env）
# ===========================================================
def _real_case_entry(sources: Any) -> GSheetSpec | None:
    for spec in sources.gsheets:
        if spec.sheet_id_env == "CASE_CORPUS_SHEET_ID":
            return spec
    return None


def test_real_yaml_case_entry_is_skipped_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env 未設定なら事例集ソースは **読み込まれない**（既存 ingest に影響ゼロ）。

    変異: sheet_id_env 未設定時に placeholder をそのまま採用すると gsheets が 3 になり赤。
    """
    monkeypatch.delenv("CASE_CORPUS_SHEET_ID", raising=False)
    monkeypatch.delenv("CASE_CORPUS_SHEET_GID", raising=False)
    sources = load_ingest_sources(REAL_YAML, skip_placeholder=True)
    assert _real_case_entry(sources) is None
    # 既存 2 シートだけ・並びも内容も従来どおり
    assert [s.sheet_id for s in sources.gsheets] == [
        "1jRmoUPo0kAhOGA6secGcwGHILH5LHt7lYvEuxJ5uupo",
        "1VukC1Qv0MRqxSvgxuSqDwzpPsM_K1FJNTpTXs10KQhY",
    ]
    assert all(s.extra_metadata.get("case_corpus") is None for s in sources.gsheets)


def test_real_yaml_case_entry_does_not_break_strict_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """strict mode（skip_placeholder=False）でも例外にしない。

    scripts/ingest_sources.py は起動時にこの loader を通す単一プロセスなので、
    ここで raise すると sheet_id 未確定の 1 エントリだけで slack/gdrive/既存 gsheets
    ごと ingest が全断する。
    変異: sheet_id_env 宣言済でも strict で raise させると赤。
    """
    monkeypatch.delenv("CASE_CORPUS_SHEET_ID", raising=False)
    sources = load_ingest_sources(REAL_YAML, skip_placeholder=False)
    assert len(sources.gsheets) == 2


def test_real_yaml_case_entry_resolves_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """env を入れると実 ID として採用され、gid も env で上書きできる。"""
    monkeypatch.setenv("CASE_CORPUS_SHEET_ID", CASE_SHEET_ID)
    monkeypatch.setenv("CASE_CORPUS_SHEET_GID", str(CASE_GID))
    sources = load_ingest_sources(REAL_YAML, skip_placeholder=True)
    spec = _real_case_entry(sources)
    assert spec is not None
    assert spec.sheet_id == CASE_SHEET_ID
    assert spec.extra_metadata["case_corpus"] == "true"
    assert [t.gid for t in spec.tabs] == [CASE_GID]
    # 既存 2 シートは env の有無で一切変わらない
    assert [s.sheet_id for s in sources.gsheets][:2] == [
        "1jRmoUPo0kAhOGA6secGcwGHILH5LHt7lYvEuxJ5uupo",
        "1VukC1Qv0MRqxSvgxuSqDwzpPsM_K1FJNTpTXs10KQhY",
    ]


def test_case_entry_gid_falls_back_to_yaml_when_env_absent_or_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gid_env は任意。未設定・非数値なら yaml の gid を使う（起動失敗させない）。"""
    monkeypatch.setenv("CASE_CORPUS_SHEET_ID", CASE_SHEET_ID)
    monkeypatch.delenv("CASE_CORPUS_SHEET_GID", raising=False)
    spec = _real_case_entry(load_ingest_sources(REAL_YAML, skip_placeholder=True))
    assert spec is not None and [t.gid for t in spec.tabs] == [0]

    monkeypatch.setenv("CASE_CORPUS_SHEET_GID", "not-a-number")
    spec = _real_case_entry(load_ingest_sources(REAL_YAML, skip_placeholder=True))
    assert spec is not None and [t.gid for t in spec.tabs] == [0]


def test_case_entry_env_holding_a_placeholder_is_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env に雛形文字列がそのまま入っていた場合も「未設定」扱いで skip する。"""
    monkeypatch.setenv("CASE_CORPUS_SHEET_ID", "REPLACE_WITH_CASE_CORPUS_SHEET_ID")
    assert _real_case_entry(load_ingest_sources(REAL_YAML, skip_placeholder=True)) is None
    monkeypatch.setenv("CASE_CORPUS_SHEET_ID", "   ")
    assert _real_case_entry(load_ingest_sources(REAL_YAML, skip_placeholder=True)) is None


def test_existing_gsheet_specs_never_read_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """実 ID を持つ既存エントリは env で差し替えられない（黙った上書きの口を作らない）。"""
    monkeypatch.setenv("CASE_CORPUS_SHEET_ID", CASE_SHEET_ID)
    sources = load_ingest_sources(REAL_YAML, skip_placeholder=True)
    knowledge = sources.gsheets[0]
    assert knowledge.sheet_id == "1jRmoUPo0kAhOGA6secGcwGHILH5LHt7lYvEuxJ5uupo"
    assert knowledge.sheet_id_env is None


# ===========================================================
# pipeline — 事例集経路
# ===========================================================
class _FakeEmbedder:
    def embed_passage(self, text: str) -> list[float]:
        return [0.0, 0.0, 0.0]


class _FakeCaseRepository:
    """事例集 sticky 読み出しに対応した fake（本番 IngestRepository の必要面だけ）。"""

    def __init__(self, stored: dict[str, dict[str, str]] | None = None) -> None:
        self.upsert_calls: list[dict[str, Any]] = []
        self.stored = stored or {}
        self.metadata_lookup_calls: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []

    def get_document_metadata_values(
        self, source_type: str, external_ids: Any, keys: Any
    ) -> dict[str, dict[str, str]]:
        self.metadata_lookup_calls.append((source_type, tuple(external_ids), tuple(keys)))
        return {k: dict(v) for k, v in self.stored.items() if k in set(external_ids)}

    def upsert_document_with_chunks(
        self,
        doc: Any,
        chunks: list[Any],
        request_id: str,
        *,
        replace_existing_chunks: bool = True,
    ) -> str:
        self.upsert_calls.append(
            {
                "external_id": doc.external_id,
                "title": doc.title,
                "metadata": dict(doc.metadata),
                "source_uri": doc.source_uri,
            }
        )
        return "fake-doc-id"


class _RaisingCaseRepository(_FakeCaseRepository):
    """sticky 読み出しが落ちる fake（RDS 一時障害 / statement_timeout の再現）。"""

    def get_document_metadata_values(
        self, source_type: str, external_ids: Any, keys: Any
    ) -> dict[str, dict[str, str]]:
        raise RuntimeError("connection reset by peer")


def _install_fake_sheets(
    monkeypatch: pytest.MonkeyPatch,
    *,
    headers: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
    tab_name: str = "マスター表",
) -> MagicMock:
    from teamagent.adapters.gsheets_client import TabRows

    fake_client = MagicMock()
    fake_client.get_sheet_metadata.side_effect = RuntimeError("no metadata in test")
    fake_client.get_tab_rows.return_value = TabRows(
        sheet_id=CASE_SHEET_ID,
        tab_name=tab_name,
        headers=headers,
        rows=rows,
        row_count=len(rows),
    )
    monkeypatch.setattr(
        "teamagent.adapters.gsheets_client.GSheetsClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )
    return fake_client


def _case_spec(
    *, tab_name: str = "マスター表", sheet_name: str = "📍ショート動画施策事例集"
) -> GSheetSpec:
    return GSheetSpec(
        sheet_id=CASE_SHEET_ID,
        sheet_name=sheet_name,
        description="",
        tabs=(GSheetsTabSpec(gid=CASE_GID, tab_name=tab_name),),
        extra_metadata={"case_corpus": "true", "topic": "ショート動画施策事例集"},
        sheet_id_env="CASE_CORPUS_SHEET_ID",
    )


def _run_ingest(spec: GSheetSpec, repo: Any, request_id: str = "r-case") -> tuple[int, int]:
    from teamagent.ingest.pipeline import _ingest_gsheet

    return _ingest_gsheet(
        spec,
        embedder=_FakeEmbedder(),
        repository=repo,
        owner_email="s-komata@vectorinc.co.jp",
        dry_run=False,
        request_id=request_id,
    )


def test_case_rows_become_documents_with_case_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1 行 = 1 document・metadata に case_corpus / case_effect / case_product /
    case_owner / case_external_use が載る。"""
    _install_fake_sheets(
        monkeypatch,
        headers=_CASE_HEADERS_NO_USE_COLUMN,
        rows=(
            (
                "観光・テーマパーク",
                "株式会社ジャングリア沖縄",
                "ジャングリア沖縄",
                "TTO80本でネガ情報比率を改善",
                "清水",
            ),
        ),
    )
    repo = _FakeCaseRepository()
    docs_n, chunks_n = _run_ingest(_case_spec(), repo)

    assert (docs_n, chunks_n) == (1, 1)
    md = repo.upsert_calls[0]["metadata"]
    assert md["case_corpus"] == "true"
    assert md["case_effect"] == "TTO80本でネガ情報比率を改善"
    assert md["case_product"] == "ジャングリア沖縄"
    assert md["case_owner"] == "清水"
    assert md["case_category"] == "観光・テーマパーク"
    # 「対外利用可否」列が無い日でも 3 値は必ず載る（未判定を unknown で明示）
    assert md["case_external_use"] == "unknown"
    # client_name は既存の導出器をそのまま使う（法人格を落とす）
    assert md["client_name"] == "ジャングリア沖縄"
    # external_id は行単位・title は「企業名 商材」（"row N" にしない）
    assert repo.upsert_calls[0]["external_id"] == f"{CASE_SHEET_ID}:{CASE_GID}:2"
    assert repo.upsert_calls[0]["title"] == "ジャングリア沖縄 ジャングリア沖縄"
    assert f"range={2}:{2}" in repo.upsert_calls[0]["source_uri"]


def test_case_external_use_column_is_normalized_to_three_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「対外利用可否」列がある場合は 3 値へ正規化し、理由（生セル）を note に残す。"""
    _install_fake_sheets(
        monkeypatch,
        headers=_CASE_HEADERS_WITH_USE_COLUMN,
        rows=(
            ("金融", "JCB", "JCB×USJ", "指名検索が前月比5倍", "望月", "NG（confidential）"),
            ("食品", "伊藤ハム", "クイックディナー", "目標再生数120%超", "小池", "可"),
            ("外食", "すかいらーく", "ガスト", "", "高林", ""),
        ),
    )
    repo = _FakeCaseRepository()
    _run_ingest(_case_spec(), repo)

    by_use = [c["metadata"]["case_external_use"] for c in repo.upsert_calls]
    assert by_use == ["ng", "ok", "unknown"]
    assert repo.upsert_calls[0]["metadata"]["case_external_use_note"] == "NG（confidential）"
    assert "case_external_use_note" not in repo.upsert_calls[2]["metadata"]


def test_ng_sheet_name_marks_every_row_ng(monkeypatch: pytest.MonkeyPatch) -> None:
    """シート名・タブ名に「展開NG」があれば列の値に関わらず全行 ng。

    変異: pipeline の names 引数から sheet_name/tab_title を外すと ok になって赤。
    """
    _install_fake_sheets(
        monkeypatch,
        headers=_CASE_HEADERS_WITH_USE_COLUMN,
        rows=(("金融", "JCB", "JCB×USJ", "指名検索5倍", "望月", "可"),),
        tab_name="事例（クライアント展開NG）",
    )
    repo = _FakeCaseRepository()
    _run_ingest(_case_spec(tab_name="事例（クライアント展開NG）"), repo)

    md = repo.upsert_calls[0]["metadata"]
    assert md["case_external_use"] == "ng"
    assert md["case_external_use_note"] == "事例（クライアント展開NG）"


def test_reingest_does_not_downgrade_ng_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """再取込で ng が unknown に戻らない（sticky）。

    本番の失敗モード: 「対外利用可否」列を運用で消した / 値を空にした行を再取込すると、
    documents.metadata は EXCLUDED.metadata の全置換なので ⚠ が黙って消える。
    変異: pipeline の previous/previous_note 引き渡しを外すと unknown になって赤。
    """
    external_id = f"{CASE_SHEET_ID}:{CASE_GID}:2"
    _install_fake_sheets(
        monkeypatch,
        headers=_CASE_HEADERS_NO_USE_COLUMN,  # 列ごと消えた
        rows=(("金融", "JCB", "JCB×USJ", "指名検索5倍", "望月"),),
    )
    repo = _FakeCaseRepository(
        stored={
            external_id: {
                "case_external_use": "ng",
                "case_external_use_note": "事例集フォルダが展開NG",
            }
        }
    )
    _run_ingest(_case_spec(), repo)

    md = repo.upsert_calls[0]["metadata"]
    assert md["case_external_use"] == "ng"
    assert md["case_external_use_note"] == "事例集フォルダが展開NG"
    # sticky 読み出しは行単位でなく「タブごと 1 クエリ」（件数に比例して接続を開かない）
    assert len(repo.metadata_lookup_calls) == 1
    source_type, ids, keys = repo.metadata_lookup_calls[0]
    assert source_type == "gsheets"
    assert ids == (external_id,)
    # 読むのは可否 2 キーだけ（社名・効果本文は読まない）
    assert set(keys) == {"case_external_use", "case_external_use_note"}


def test_sticky_lookup_failure_skips_the_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    """既存値が読めない run は事例集タブを **取り込まない**（fail-closed）。

    読めないまま upsert すると ng が unknown へ降格して ⚠ が消える。事例集が 1 run
    古いほうが安全。
    変異: 例外を握りつぶして {} で続行すると docs_n=1 になって赤。
    """
    _install_fake_sheets(
        monkeypatch,
        headers=_CASE_HEADERS_NO_USE_COLUMN,
        rows=(("金融", "JCB", "JCB×USJ", "指名検索5倍", "望月"),),
    )
    repo = _RaisingCaseRepository()
    docs_n, chunks_n = _run_ingest(_case_spec(), repo)

    assert (docs_n, chunks_n) == (0, 0)
    assert repo.upsert_calls == []


def test_repository_without_metadata_lookup_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sticky 読み出しメソッドを持たない repository では事例集を取り込まない。

    「メソッドが無ければ {} で続行」にすると、fake や古い repository 実装で ng が
    静かに降格する。
    """
    _install_fake_sheets(
        monkeypatch,
        headers=_CASE_HEADERS_NO_USE_COLUMN,
        rows=(("金融", "JCB", "JCB×USJ", "指名検索5倍", "望月"),),
    )

    class _NoLookupRepo:
        def __init__(self) -> None:
            self.upsert_calls: list[dict[str, Any]] = []

        def upsert_document_with_chunks(
            self, doc: Any, chunks: list[Any], request_id: str, **kwargs: Any
        ) -> str:
            self.upsert_calls.append({"external_id": doc.external_id})
            return "x"

    repo = _NoLookupRepo()
    docs_n, _ = _run_ingest(_case_spec(), repo)
    assert docs_n == 0
    assert repo.upsert_calls == []


def test_empty_case_tab_is_harmless(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 行のタブでも例外にならず 0 件で終わる。"""
    _install_fake_sheets(monkeypatch, headers=_CASE_HEADERS_NO_USE_COLUMN, rows=())
    repo = _FakeCaseRepository()
    assert _run_ingest(_case_spec(), repo) == (0, 0)
    assert repo.upsert_calls == []


def test_case_corpus_headers_unmatched_still_marks_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """フラグ付きシートの列名が丸ごと変わっても取り込みは止まらず、ng は sticky のまま。

    「案件が消える」より「効果が空で出る」ほうが安全。運用検知は WARNING ログ。
    """
    external_id = f"{CASE_SHEET_ID}:{CASE_GID}:2"
    _install_fake_sheets(
        monkeypatch,
        headers=("col1", "col2", "col3"),
        rows=(("a", "b", "c"),),
    )
    repo = _FakeCaseRepository(stored={external_id: {"case_external_use": "ng"}})
    docs_n, _ = _run_ingest(_case_spec(), repo)

    assert docs_n == 1
    md = repo.upsert_calls[0]["metadata"]
    assert md["case_corpus"] == "true"
    assert md["case_external_use"] == "ng"  # sticky は列写像に依存しない
    assert "case_effect" not in md


# ===========================================================
# 既存 document への副作用ゼロ
# ===========================================================
def test_knowledge_sheet_metadata_is_byte_identical_without_case_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既存のナレッジ共有シートを取り込んでも case_* / case_corpus は 1 つも付かない。

    母集団は spec の case_corpus フラグに限定しているので、この経路には入らない。
    変異: _is_case_corpus_spec を「常に True」にすると case_corpus が載って赤。
    """
    from teamagent.adapters.gsheets_client import TabRows

    fake_client = MagicMock()
    fake_client.get_sheet_metadata.side_effect = RuntimeError("no metadata in test")
    fake_client.get_tab_rows.return_value = TabRows(
        sheet_id="1jRmoUPo0kAhOGA6secGcwGHILH5LHt7lYvEuxJ5uupo",
        tab_name="フォーム回答 1",
        headers=_KNOWLEDGE_HEADERS,
        rows=(
            (
                "https://files.slack.com/x",
                "株式会社デルタ製薬",
                "新製品プロモーション",
                "メーカー",
                "ビデオリリース",
                "提案",
                "",
                "",
                "",
                "@山田",
                "2025/06/17 13:14:45",
            ),
        ),
        row_count=1,
    )
    monkeypatch.setattr(
        "teamagent.adapters.gsheets_client.GSheetsClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )

    spec = GSheetSpec(
        sheet_id="1jRmoUPo0kAhOGA6secGcwGHILH5LHt7lYvEuxJ5uupo",
        sheet_name="ナレッジ共有 - フォーム回答",
        description="",
        tabs=(GSheetsTabSpec(gid=278789217, tab_name="フォーム回答 1"),),
        extra_metadata={"topic": "提案ナレッジ", "structure": "form-response"},
    )
    repo = _FakeCaseRepository()
    docs_n, _ = _run_ingest(spec, repo, request_id="r-knowledge")

    assert docs_n == 1
    md = repo.upsert_calls[0]["metadata"]
    assert not [k for k in md if k.startswith("case_")]
    assert "case_corpus" not in md
    assert md["is_knowledge_share"] is True
    assert md["client_name"] == "デルタ製薬"
    # 既存シートでは sticky 読み出しのクエリを 1 本も撃たない（DB 負荷も不変）
    assert repo.metadata_lookup_calls == []


def test_case_sheet_external_ids_cannot_collide_with_existing_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事例集の external_id は別 sheet_id 名前空間なので既存 document を UPSERT しない。

    ＝ 取込前後で既存 document の cls_doc_type / cls_project は 1 件も動かない
    （同じ (source_type, external_id) に当たらない限り ON CONFLICT が発火しないため）。
    """
    _install_fake_sheets(
        monkeypatch,
        headers=_CASE_HEADERS_NO_USE_COLUMN,
        rows=(
            ("金融", "JCB", "JCB×USJ", "指名検索5倍", "望月"),
            ("食品", "伊藤ハム", "クイックディナー", "再生数120%", "小池"),
        ),
    )
    repo = _FakeCaseRepository()
    _run_ingest(_case_spec(), repo)

    touched = {c["external_id"] for c in repo.upsert_calls}
    assert touched == {f"{CASE_SHEET_ID}:{CASE_GID}:2", f"{CASE_SHEET_ID}:{CASE_GID}:3"}
    for existing_sheet in (
        "1jRmoUPo0kAhOGA6secGcwGHILH5LHt7lYvEuxJ5uupo",
        "1VukC1Qv0MRqxSvgxuSqDwzpPsM_K1FJNTpTXs10KQhY",
    ):
        assert not any(eid.startswith(existing_sheet) for eid in touched)


def test_derive_knowledge_client_name_is_unchanged() -> None:
    """既存 ingest の名寄せ関数は変更していない（回帰の門）。

    事例集の client_name もこの関数を **そのまま** 使う。代理店注記の退避が要るなら
    呼び出し側でやる。
    """
    assert derive_knowledge_client_name("ロート製薬（代理店：博報堂）") == "ロート製薬"
    assert derive_knowledge_client_name("株式会社GA technologies") == "GA technologies"
    assert derive_knowledge_client_name("カゴメ様") == "カゴメ"
    assert derive_knowledge_client_name("集英社／キリンビバレッジ／ドン・キホーテ") == "集英社"
    assert derive_knowledge_client_name("その他") is None
