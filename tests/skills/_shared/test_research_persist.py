"""ResearchPersister（カタログ成果物の pgvector 永続化・Part1）の単体テスト。

DocumentUpsert/ChunkUpsert の組成・external_id 冪等・no-op ガード（空商材/backend）を検証。
DB は叩かず IngestRepository を monkeypatch で差し替える。
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from teamagent.skills._shared.research_persist import ResearchPersister, _product_key


class _FakeEmbedder:
    def embed_passage(self, text: str) -> list[float]:
        return [0.01] * 1024


class _CapatureRepo:
    """upsert 引数を捕捉する fake IngestRepository。"""

    captured: ClassVar[dict[str, Any]] = {}

    def __init__(self, pgvector: Any) -> None:
        pass

    def upsert_document_with_chunks(self, doc: Any, chunks: list[Any], request_id: str) -> str:
        _CapatureRepo.captured = {"doc": doc, "chunks": chunks, "request_id": request_id}
        return "doc-id-1"


class _RecordingExecutor:
    """submit を記録するだけの fake executor（スレッドを起こさない）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def submit(self, fn: Any, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # 既定の local/e5⇄embedding を明示（_backend_ok を通す）。
    monkeypatch.delenv("EMBEDDER_BACKEND", raising=False)
    monkeypatch.delenv("EMBEDDING_COLUMN", raising=False)


def _persist_once(monkeypatch: pytest.MonkeyPatch, **over: Any) -> Any:
    import teamagent.ingest.repository as repo_mod

    monkeypatch.setattr(repo_mod, "IngestRepository", _CapatureRepo)
    p = ResearchPersister(pgvector=object(), embedder=_FakeEmbedder())
    kwargs = {
        "tool": "x_voice",
        "product_name": "辻利 抹茶",
        "title": "辻利 抹茶 Xの声集め（X（旧Twitter））",
        "body_md": "# 本文\n主要な声…",
        "owner_email": "s-komata@vectorinc.co.jp",
        "request_id": "rid-1",
        "cls_solution": "Xリサーチ",
        "cls_doc_type": "世の中の声",
        "dedup_key": None,
        "source_uri": None,
        "extra_metadata": {},
    }
    kwargs.update(over)
    p._persist(**kwargs)
    return _CapatureRepo.captured


def test_persist_builds_document_for_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _persist_once(monkeypatch)
    doc = cap["doc"]
    assert doc.source_type == "other"  # ENUM 新値不可
    assert doc.metadata["cls_project"] == "辻利 抹茶"  # Vault クライアント anchor
    assert doc.metadata["cls_solution"] == "Xリサーチ"
    assert doc.metadata["cls_doc_type"] == "世の中の声"
    assert doc.metadata["x_research_tool"] == "x_voice"
    # export_vault の除外条件を踏まない（付けない）。
    for k in ("is_sales_fb", "suppressed", "stale"):
        assert k not in doc.metadata
    assert doc.source_uri is None  # 失効する presigned を death-link にしない
    assert "X（旧Twitter）" in doc.title  # 媒体/X タグの自動付与用
    assert doc.acl_emails == ["s-komata@vectorinc.co.jp"]
    assert doc.external_id.startswith("xresearch:x_voice:")
    assert doc.external_id.endswith(cap["doc"].external_id.rsplit(":", 1)[-1])  # 末尾=JST日付
    assert len(cap["chunks"]) == 1
    assert len(cap["chunks"][0].embedding) == 1024


def test_external_id_is_idempotent_per_tool_product_day(monkeypatch: pytest.MonkeyPatch) -> None:
    a = _persist_once(monkeypatch)["doc"].external_id
    b = _persist_once(monkeypatch)["doc"].external_id
    assert a == b  # 同日同商材同ツールは同一キー＝1件に集約(UPDATE)
    c = _persist_once(monkeypatch, product_name="別商材")["doc"].external_id
    assert c != a


def test_external_id_no_collision_on_lossy_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """lossy slug で潰れる別名が別 external_id になる（別研究の silently 上書き防止・Codex）。"""
    a = _persist_once(monkeypatch, product_name="a:b/c")["doc"].external_id
    b = _persist_once(monkeypatch, product_name="abc")["doc"].external_id
    assert a != b


def test_external_id_is_per_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """同日同商材でも別ユーザーは別 external_id（本文/owner/ACL の相互上書きを防ぐ）。"""
    a = _persist_once(monkeypatch, owner_email="a@vectorinc.co.jp")["doc"].external_id
    b = _persist_once(monkeypatch, owner_email="b@vectorinc.co.jp")["doc"].external_id
    assert a != b
    a2 = _persist_once(monkeypatch, owner_email="A@Vectorinc.co.jp")["doc"].external_id
    assert a2 == a  # 同一 owner（大小/空白差）は同一キー＝冪等


def test_source_uri_is_set_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _persist_once(monkeypatch, source_uri="https://connect.newstv.co.jp/r/tok")["doc"]
    assert doc.source_uri == "https://connect.newstv.co.jp/r/tok"
    assert _persist_once(monkeypatch, source_uri=None)["doc"].source_uri is None


def test_dedup_key_overrides_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """dedup_key(buzz の job_id)を渡すと日付でなくそれで一意化＝再ポーリング日跨ぎ重複を防ぐ。"""
    eid = _persist_once(monkeypatch, dedup_key="job-abc123")["doc"].external_id
    assert eid.endswith(":job-abc123")


def test_schedule_noop_on_empty_product(monkeypatch: pytest.MonkeyPatch) -> None:
    ex = _RecordingExecutor()
    p = ResearchPersister(pgvector=object(), embedder=_FakeEmbedder(), executor=ex)
    p.schedule(
        tool="x_voice",
        product_name="  ",
        title="t",
        body_md="b",
        owner_email="u",
        request_id="r",
        cls_solution="s",
        cls_doc_type="d",
    )
    assert ex.calls == []  # 商材空 → 記録しない（Vault で拾えないため）


def test_schedule_noop_on_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    ex = _RecordingExecutor()
    p = ResearchPersister(pgvector=object(), embedder=_FakeEmbedder(), executor=ex)
    p.schedule(
        tool="x_voice",
        product_name="辻利",
        title="t",
        body_md="",
        owner_email="u",
        request_id="r",
        cls_solution="s",
        cls_doc_type="d",
    )
    assert ex.calls == []


def test_schedule_noop_on_non_local_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDER_BACKEND", "cohere")  # 別空間 → embedding 列を汚染しない
    ex = _RecordingExecutor()
    p = ResearchPersister(pgvector=object(), embedder=_FakeEmbedder(), executor=ex)
    p.schedule(
        tool="x_voice",
        product_name="辻利",
        title="t",
        body_md="b",
        owner_email="u",
        request_id="r",
        cls_solution="s",
        cls_doc_type="d",
    )
    assert ex.calls == []


def test_schedule_submits_when_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    ex = _RecordingExecutor()
    p = ResearchPersister(pgvector=object(), embedder=_FakeEmbedder(), executor=ex)
    p.schedule(
        tool="x_voice",
        product_name="辻利",
        title="t",
        body_md="b",
        owner_email="u",
        request_id="r",
        cls_solution="s",
        cls_doc_type="d",
    )
    assert len(ex.calls) == 1 and ex.calls[0]["product_name"] == "辻利"


def test_product_key_is_hash_without_plaintext_name() -> None:
    k = _product_key("辻利 抹茶ミルク!!")
    assert "辻利" not in k and "抹茶" not in k  # 商材名を平文で残さない（ログ露出防止）
    assert ":" not in k  # external_id 区切りの : を混入させない
    assert len(k) == 16 and all(c in "0123456789abcdef" for c in k)
    assert _product_key("a:b/c") != _product_key("abc")  # lossy 衝突を回避
    assert _product_key("辻利 抹茶") == _product_key("辻利 抹茶")  # 同一名は冪等
