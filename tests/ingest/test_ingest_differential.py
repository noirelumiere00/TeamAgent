"""差分取り込み（``INGEST_DIFFERENTIAL``・既定 OFF）のテスト。

検証する契約:

- 同一内容の 2 回目 run は **Bedrock 分類・embedding・upsert を一切呼ばない**
- 内容・ACL・実行時設定のどれかが変わったら必ず再処理する（黙った取りこぼし禁止）
- フラグ OFF は従来挙動とバイト等価（hash 保存なし・DB 照合なし）
- ハッシュ読み出し失敗は fail-open（スキップせず全件再処理＝コスト側へ倒す）
- スキップ数は collector → IngestStats.documents_unchanged で可視化される

フェイクは本番の失敗モードを再現する: upsert は metadata を全置換で保存し
（本番 SQL の ``EXCLUDED.metadata`` と同じ）、hash lookup は「過去の upsert が
保存した metadata に content_sha256 があるときだけ」返す。したがって
「1 回目が hash を保存しない実装」や「upsert が hash を落とす実装」に変異させると
2 回目スキップのテストが赤くなる。実 SQL round-trip は
tests/ingest/test_ingest_differential_postgres.py（TEAMAGENT_TEST_DB_DSN 必須）が担う。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.ingest.content_hash import (
    INGEST_CONTENT_HASH_KEY,
    compute_document_content_hash,
)
from teamagent.ingest.loader import (
    GSheetSpec,
    GSheetsTabSpec,
    IngestSources,
    SlackChannelSpec,
)
from teamagent.ingest.pipeline import _IngestUnchangedCollector


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """ハッシュ入力（pipeline_config）と分類経路に効く env を決定論化する。"""
    for name in (
        "INGEST_DIFFERENTIAL",
        "USE_DOC_CLASSIFY",
        "USE_CONTEXTUAL_INGEST",
        "USE_ENTITY_TAGS",
        "USE_DOC_KIND_RULES",
        "EMBEDDER_BACKEND",
        "TEAMAGENT_SHARED_COMPANY_DOMAINS",
    ):
        monkeypatch.delenv(name, raising=False)


class _CountingEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed_passage(self, text: str) -> list[float]:
        self.calls += 1
        return [0.1] * 1024


class _CountingBedrock:
    """converse 呼び出し回数を数える Bedrock フェイク（分類 JSON を返す）。"""

    def __init__(self) -> None:
        self.converse_calls = 0

    def converse(self, **kwargs: Any) -> Any:
        self.converse_calls += 1
        return SimpleNamespace(
            text='{"project": "テスト案件", "industry": "コスメ", "doc_type": "提案書"}'
        )


class _FakeDifferentialRepository:
    """本番の挙動・失敗モードを写像した fake repository。

    - ``upsert_document_with_chunks`` は metadata を**全置換**で保存する
      （本番 ``_upsert_document`` の ``metadata = EXCLUDED.metadata`` と同じ）
    - ``get_document_content_hashes`` は保存済み metadata に content_sha256 が
      あるものだけ返す（本番 SQL の ``metadata ? 'content_sha256'`` と同じ）
    """

    def __init__(self) -> None:
        self.upsert_calls: list[dict[str, Any]] = []
        self.documents: dict[tuple[str, str], dict[str, Any]] = {}
        self.hash_lookup_calls: list[list[str]] = []
        self.fail_hash_lookup = False

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
                "source_type": doc.source_type,
                "title": doc.title,
                "source_uri": doc.source_uri,
                "owner_email": doc.owner_email,
                "acl_emails": list(doc.acl_emails),
                "acl_groups": list(doc.acl_groups),
                "metadata": dict(doc.metadata),
                "modified_at": doc.modified_at,
                "chunk_texts": [c.content for c in chunks],
            }
        )
        self.documents[(doc.source_type, doc.external_id)] = dict(doc.metadata)
        return "fake-doc-id"

    def get_document_content_hashes(
        self, source_type: str, external_ids: list[str]
    ) -> dict[str, str]:
        self.hash_lookup_calls.append(list(external_ids))
        if self.fail_hash_lookup:
            raise RuntimeError("content hash lookup down")
        out: dict[str, str] = {}
        for external_id in external_ids:
            metadata = self.documents.get((source_type, external_id))
            if metadata and INGEST_CONTENT_HASH_KEY in metadata:
                out[external_id] = str(metadata[INGEST_CONTENT_HASH_KEY])
        return out


def _install_counting_classifier(monkeypatch: pytest.MonkeyPatch) -> _CountingBedrock:
    """USE_DOC_CLASSIFY=1 + BedrockClient.from_env を counting fake に差し替える。

    ``build_classifier_from_env`` の実構築経路（``DocClassifier(BedrockClient.from_env())``）
    をそのまま通す＝「分類が本当に Bedrock converse へ到達したか」を回数で観測できる。
    """
    bedrock = _CountingBedrock()
    monkeypatch.setenv("USE_DOC_CLASSIFY", "1")
    monkeypatch.setattr(
        "teamagent.adapters.bedrock_client.BedrockClient.from_env",
        classmethod(lambda cls, **kwargs: bedrock),
    )
    return bedrock


# -----------------------------------------------------------
# gsheets 経路
# -----------------------------------------------------------
_GSHEET_HEADERS = ("業界", "温度感")
_GSHEET_ROWS = (("飲食", "高"), ("コスメ", "中"))


def _install_gsheet_rows(
    monkeypatch: pytest.MonkeyPatch, rows: tuple[tuple[str, str], ...]
) -> None:
    from teamagent.adapters.gsheets_client import TabRows

    fake_client = MagicMock()
    fake_client.get_tab_rows.return_value = TabRows(
        sheet_id="1V",
        tab_name="フォーム回答 1",
        headers=_GSHEET_HEADERS,
        rows=rows,
        row_count=len(rows),
    )
    monkeypatch.setattr(
        "teamagent.adapters.gsheets_client.GSheetsClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )


def _gsheet_spec() -> GSheetSpec:
    return GSheetSpec(
        sheet_id="1V",
        sheet_name="FB",
        description="",
        tabs=(GSheetsTabSpec(gid=537831563, tab_name="フォーム回答 1"),),
    )


def _run_gsheet(
    repo: _FakeDifferentialRepository,
    *,
    embedder: _CountingEmbedder | None = None,
    collector: _IngestUnchangedCollector | None = None,
    dry_run: bool = False,
    request_id: str = "r",
) -> tuple[int, int]:
    from teamagent.ingest.pipeline import _ingest_gsheet

    return _ingest_gsheet(
        _gsheet_spec(),
        embedder=embedder or _CountingEmbedder(),  # type: ignore[arg-type]
        repository=repo,  # type: ignore[arg-type]
        owner_email="x@y.jp",
        dry_run=dry_run,
        request_id=request_id,
        unchanged_collector=collector,
    )


def test_gsheet_flag_off_is_legacy_byte_equal(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定（フラグ OFF）: hash を保存しない・DB 照合しない・毎回全件再処理する。"""
    bedrock = _install_counting_classifier(monkeypatch)
    _install_gsheet_rows(monkeypatch, _GSHEET_ROWS)
    repo = _FakeDifferentialRepository()

    assert _run_gsheet(repo, request_id="r1") == (2, 2)
    assert _run_gsheet(repo, request_id="r2") == (2, 2)

    assert bedrock.converse_calls == 4  # 2 行 × 2 run＝毎回再分類（従来挙動）
    assert len(repo.upsert_calls) == 4
    assert repo.hash_lookup_calls == []  # DB 照合もしない
    for call in repo.upsert_calls:
        assert INGEST_CONTENT_HASH_KEY not in call["metadata"]  # metadata はバイト等価


def test_gsheet_second_run_identical_skips_bedrock_embedding_and_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """フラグ ON: 同一内容の 2 回目 run は分類ゼロ・embedding ゼロ・upsert ゼロ。"""
    monkeypatch.setenv("INGEST_DIFFERENTIAL", "1")
    bedrock = _install_counting_classifier(monkeypatch)
    _install_gsheet_rows(monkeypatch, _GSHEET_ROWS)
    repo = _FakeDifferentialRepository()

    embedder1 = _CountingEmbedder()
    assert _run_gsheet(repo, embedder=embedder1, request_id="r1") == (2, 2)
    assert bedrock.converse_calls == 2
    assert embedder1.calls == 2
    assert len(repo.upsert_calls) == 2
    for call in repo.upsert_calls:
        # 1 回目は hash と分類の両方を保存している（スキップの前提を先に固定）
        assert call["metadata"][INGEST_CONTENT_HASH_KEY]
        assert call["metadata"]["cls_project"] == "テスト案件"

    embedder2 = _CountingEmbedder()
    collector = _IngestUnchangedCollector()
    assert _run_gsheet(repo, embedder=embedder2, collector=collector, request_id="r2") == (0, 0)

    assert bedrock.converse_calls == 2  # 2 回目の Bedrock 呼び出しはゼロ
    assert embedder2.calls == 0  # embedding 再計算もゼロ
    assert len(repo.upsert_calls) == 2  # upsert（chunks 再書込含む）もゼロ
    assert collector.count_for("gsheets") == 2  # スキップは黙らず可視化


def test_gsheet_changed_row_is_reclassified_others_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内容が変わった行だけ再分類・再 upsert し、他はスキップする。"""
    monkeypatch.setenv("INGEST_DIFFERENTIAL", "1")
    bedrock = _install_counting_classifier(monkeypatch)
    _install_gsheet_rows(monkeypatch, _GSHEET_ROWS)
    repo = _FakeDifferentialRepository()
    assert _run_gsheet(repo, request_id="r1") == (2, 2)
    old_hash = repo.documents[("gsheets", "1V:537831563:3")][INGEST_CONTENT_HASH_KEY]

    _install_gsheet_rows(monkeypatch, (("飲食", "高"), ("コスメ", "低に変更")))
    collector = _IngestUnchangedCollector()
    assert _run_gsheet(repo, collector=collector, request_id="r2") == (1, 1)

    assert bedrock.converse_calls == 3  # 変更行の 1 件だけ再分類
    assert collector.count_for("gsheets") == 1
    assert repo.upsert_calls[-1]["external_id"] == "1V:537831563:3"
    new_hash = repo.documents[("gsheets", "1V:537831563:3")][INGEST_CONTENT_HASH_KEY]
    assert new_hash != old_hash  # 保存 hash も新内容に更新される


def test_gsheet_hash_lookup_failure_fails_open_to_full_reprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hash 読み出し障害はスキップ側でなく全件再処理側へ倒れる（fail-open）。"""
    monkeypatch.setenv("INGEST_DIFFERENTIAL", "1")
    bedrock = _install_counting_classifier(monkeypatch)
    _install_gsheet_rows(monkeypatch, _GSHEET_ROWS)
    repo = _FakeDifferentialRepository()
    assert _run_gsheet(repo, request_id="r1") == (2, 2)

    repo.fail_hash_lookup = True
    collector = _IngestUnchangedCollector()
    assert _run_gsheet(repo, collector=collector, request_id="r2") == (2, 2)

    assert bedrock.converse_calls == 4  # 全件を従来どおり再分類
    assert len(repo.upsert_calls) == 4
    assert collector.count_for("gsheets") == 0


def test_gsheet_dry_run_keeps_differential_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """dry-run では DB 照合もスキップもせず、処理予定の全量を従来どおり数える。"""
    monkeypatch.setenv("INGEST_DIFFERENTIAL", "1")
    _install_counting_classifier(monkeypatch)
    _install_gsheet_rows(monkeypatch, _GSHEET_ROWS)
    repo = _FakeDifferentialRepository()

    collector = _IngestUnchangedCollector()
    assert _run_gsheet(repo, collector=collector, dry_run=True) == (2, 2)

    assert repo.hash_lookup_calls == []  # dry-run は DB を触らない
    assert len(repo.upsert_calls) == 0
    assert collector.count_for("gsheets") == 0


def test_gsheet_stored_hash_is_recomputable_from_upserted_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """anti-drift: 保存 hash は「実際に upsert された document の入力」から再計算できる。

    DocumentUpsert の title / source_uri / metadata 等の組み立てとハッシュ入力が
    乖離すると（例: タイトル書式だけ変更）、スキップ判定が実際の書き込み内容と
    ズレて永遠に不一致 or 偽一致になる。ここで一致を固定して乖離を検知する。
    """
    monkeypatch.setenv("INGEST_DIFFERENTIAL", "1")
    _install_counting_classifier(monkeypatch)
    _install_gsheet_rows(monkeypatch, _GSHEET_ROWS)
    repo = _FakeDifferentialRepository()
    assert _run_gsheet(repo) == (2, 2)

    for call in repo.upsert_calls:
        input_metadata = {
            k: v
            for k, v in call["metadata"].items()
            # cls_* と後方互換の industry は分類出力・content_sha256 はハッシュ自身
            if not k.startswith("cls_") and k not in (INGEST_CONTENT_HASH_KEY, "industry")
        }
        recomputed = compute_document_content_hash(
            source_type=call["source_type"],
            external_id=call["external_id"],
            text=call["chunk_texts"][0],
            title=call["title"],
            source_uri=call["source_uri"],
            owner_email=call["owner_email"],
            acl_emails=call["acl_emails"],
            acl_groups=call["acl_groups"],
            metadata=input_metadata,
            modified_at=call["modified_at"],
            pipeline_config={
                "classify": True,
                "contextualize": False,
                "embedder_backend": "local",
            },
        )
        assert recomputed == call["metadata"][INGEST_CONTENT_HASH_KEY]


# -----------------------------------------------------------
# slack 経路
# -----------------------------------------------------------
def _install_slack_channel(
    monkeypatch: pytest.MonkeyPatch,
    *,
    text: str = "親メッセージ",
    member_ids: tuple[str, ...] = ("U001", "U002"),
) -> None:
    from teamagent.adapters.slack_channel_ingest_client import (
        HistoryBatch,
        SlackChannelMember,
        SlackMessage,
    )

    parent = SlackMessage(
        ts="1700000001.000001",
        user="U001",
        text=text,
        thread_ts="1700000001.000001",
        reply_count=1,
    )
    fake_client = MagicMock()
    fake_client.list_channel_history.return_value = HistoryBatch(
        messages=(parent,), next_cursor=None, has_more=False
    )
    fake_client.list_thread_replies.return_value = HistoryBatch(
        messages=(parent,), next_cursor=None, has_more=False
    )
    fake_client.list_channel_members.return_value = (list(member_ids), None)
    fake_client.get_user_emails.return_value = [
        SlackChannelMember(user_id=uid, email=f"{uid.lower()}@x.jp", display_name=uid)
        for uid in member_ids
    ]
    monkeypatch.setattr(
        "teamagent.adapters.slack_channel_ingest_client.SlackChannelIngestClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )

    from teamagent.ingest import pipeline as pipeline_mod

    pipeline_mod._USER_EMAIL_CACHE.clear()


def _run_slack(
    repo: _FakeDifferentialRepository,
    *,
    embedder: _CountingEmbedder | None = None,
    collector: _IngestUnchangedCollector | None = None,
    request_id: str = "r",
) -> tuple[int, int]:
    from teamagent.ingest.pipeline import _ingest_slack_channel

    spec = SlackChannelSpec(channel_id="C0XYZ", channel_name="#test", description="")
    return _ingest_slack_channel(
        spec,
        embedder=embedder or _CountingEmbedder(),  # type: ignore[arg-type]
        repository=repo,  # type: ignore[arg-type]
        owner_email="bob@x.jp",
        dry_run=False,
        request_id=request_id,
        unchanged_collector=collector,
    )


def test_slack_second_run_identical_skips_bedrock_and_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INGEST_DIFFERENTIAL", "1")
    bedrock = _install_counting_classifier(monkeypatch)
    _install_slack_channel(monkeypatch)
    repo = _FakeDifferentialRepository()

    assert _run_slack(repo, request_id="r1") == (1, 1)
    assert bedrock.converse_calls == 1
    assert len(repo.upsert_calls) == 1
    assert repo.upsert_calls[0]["metadata"][INGEST_CONTENT_HASH_KEY]

    _install_slack_channel(monkeypatch)  # 同一内容で再取得（cache もクリア）
    embedder2 = _CountingEmbedder()
    collector = _IngestUnchangedCollector()
    assert _run_slack(repo, embedder=embedder2, collector=collector, request_id="r2") == (0, 0)

    assert bedrock.converse_calls == 1  # Bedrock 呼び出しゼロ
    assert embedder2.calls == 0
    assert len(repo.upsert_calls) == 1
    assert collector.count_for("slack") == 1


def test_slack_acl_membership_change_forces_reupsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本文が同一でも channel メンバー（ACL）が変わったら必ず再 upsert する。

    ACL をハッシュ対象から外す変異はここで赤くなる（権限変更の取りこぼしは
    コストでなくセキュリティの問題なので、専用テストで固定する）。
    メンバーは「追加」でなく**同人数の交代**（U002 → U003）にする: 追加だと
    metadata の channel_member_count 経由でもハッシュが変わり、acl_emails を
    ハッシュから外す変異を検知できない（変異テストで実証済みの隠蔽経路）。
    """
    monkeypatch.setenv("INGEST_DIFFERENTIAL", "1")
    bedrock = _install_counting_classifier(monkeypatch)
    _install_slack_channel(monkeypatch)
    repo = _FakeDifferentialRepository()
    assert _run_slack(repo, request_id="r1") == (1, 1)

    # 本文・メンバー数は同一のまま U002 が leave し U003 が join（ACL だけが差分）
    _install_slack_channel(monkeypatch, member_ids=("U001", "U003"))
    collector = _IngestUnchangedCollector()
    assert _run_slack(repo, collector=collector, request_id="r2") == (1, 1)

    assert bedrock.converse_calls == 2
    assert len(repo.upsert_calls) == 2
    assert collector.count_for("slack") == 0
    assert "u003@x.jp" in repo.upsert_calls[-1]["acl_emails"]  # 新 ACL が DB へ届く


def test_slack_content_change_forces_reupsert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INGEST_DIFFERENTIAL", "1")
    bedrock = _install_counting_classifier(monkeypatch)
    _install_slack_channel(monkeypatch)
    repo = _FakeDifferentialRepository()
    assert _run_slack(repo, request_id="r1") == (1, 1)

    _install_slack_channel(monkeypatch, text="親メッセージ（返信が増えた）")
    assert _run_slack(repo, request_id="r2") == (1, 1)
    assert bedrock.converse_calls == 2
    assert len(repo.upsert_calls) == 2


# -----------------------------------------------------------
# content hash の性質（単体）
# -----------------------------------------------------------
_HASH_BASE_KWARGS: dict[str, Any] = {
    "source_type": "gsheets",
    "external_id": "1V:1:2",
    "text": "本文",
    "title": "タイトル",
    "source_uri": "https://example.invalid/doc",
    "owner_email": "x@y.jp",
    "acl_emails": ["a@x.jp", "b@x.jp"],
    "acl_groups": ["x.jp"],
    "metadata": {"tab_name": "t", "row_idx": 2},
    "modified_at": "2026-08-14T18:00:00+09:00",
    "pipeline_config": {"classify": True},
}


def test_content_hash_ignores_ordering_but_not_values() -> None:
    base = compute_document_content_hash(**_HASH_BASE_KWARGS)

    # ACL の解決順・metadata の挿入順が違っても同一 hash（偽の「変更あり」を作らない）
    reordered = dict(_HASH_BASE_KWARGS)
    reordered["acl_emails"] = ["b@x.jp", "a@x.jp"]
    reordered["metadata"] = {"row_idx": 2, "tab_name": "t"}
    assert compute_document_content_hash(**reordered) == base

    # 値の差分は 1 文字でも別 hash
    changed = dict(_HASH_BASE_KWARGS)
    changed["text"] = "本文!"
    assert compute_document_content_hash(**changed) != base


def test_content_hash_excludes_classification_outputs() -> None:
    """cls_* とハッシュ自身が metadata に混入しても hash は入力のみで決まる。"""
    base = compute_document_content_hash(**_HASH_BASE_KWARGS)
    polluted = dict(_HASH_BASE_KWARGS)
    polluted["metadata"] = {
        "tab_name": "t",
        "row_idx": 2,
        "cls_project": "テスト案件",
        INGEST_CONTENT_HASH_KEY: "deadbeef",
    }
    assert compute_document_content_hash(**polluted) == base


def test_content_hash_changes_with_pipeline_config() -> None:
    """分類 ON/OFF 等の設定切替は全文書を再処理させる（旧出力の固着を防ぐ）。"""
    base = compute_document_content_hash(**_HASH_BASE_KWARGS)
    toggled = dict(_HASH_BASE_KWARGS)
    toggled["pipeline_config"] = {"classify": False}
    assert compute_document_content_hash(**toggled) != base


# -----------------------------------------------------------
# runner サマリの可視化
# -----------------------------------------------------------
def test_runner_summary_reports_documents_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IngestRunner.run() のサマリに documents_unchanged が出る（黙った省略の禁止）。"""
    from teamagent.ingest.pipeline import IngestRunner

    monkeypatch.setattr(
        "teamagent.ingest.pipeline.IngestRunner._maybe_check_freshness",
        lambda self, *, request_id: None,
    )
    monkeypatch.setenv("INGEST_DIFFERENTIAL", "1")
    _install_counting_classifier(monkeypatch)
    _install_gsheet_rows(monkeypatch, _GSHEET_ROWS)
    repo = _FakeDifferentialRepository()

    # 1 run 目で hash を保存（handler 直呼び）→ 2 run 目を runner 経由で実行
    assert _run_gsheet(repo, request_id="prime") == (2, 2)

    runner = IngestRunner(
        repository=repo,  # type: ignore[arg-type]
        embedder=_CountingEmbedder(),  # type: ignore[arg-type]
        owner_email="x@y.jp",
        dry_run=False,
    )
    sources = IngestSources(
        version=1,
        slack_channels=(),
        gdrive_folders=(),
        gsheets=(_gsheet_spec(),),
    )
    result = runner.run(sources, kinds=["gsheets"])

    assert result.by_kind["gsheets"].documents_upserted == 0
    assert result.by_kind["gsheets"].documents_unchanged == 2
    assert result.total_documents_unchanged() == 2
