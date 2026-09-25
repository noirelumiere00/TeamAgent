"""検索上位チェックの結果を金庫（documents → Aico Vault）へ記録するテスト。

レポート HTML は 7 日で切れる署名 URL なので、結論・主要な集計・上位 10 本を x_research の
声集めと同じ ResearchPersister で残す。DB は叩かず、IngestRepository だけを差し替えて
skill → ResearchPersister.schedule → _persist → DocumentUpsert の本物の経路を通す。
"""

from __future__ import annotations

import re
import types
from typing import Any, ClassVar

import pytest

from teamagent.skills._shared.research_persist import ResearchPersister
from teamagent.skills.base import SkillContext
from teamagent.skills.search_surface_check.schema import SearchSurfaceCheckInput
from teamagent.skills.search_surface_check.skill import (
    SearchSurfaceCheckSkill,
    _surface_dedup_key,
)
from tests.skills.search_surface_check.fixtures import KEYWORD, NOW, FakeBedrock, s3_rows

_JOB_ID = "tk_0123456789ab"
_REPORT = "https://s3.example/surface?X-Amz-Signature=abc"
# 未エスケープの Markdown リンク/画像/wikilink/HTML/コードを成立させる素の記号
# （x_research の test_persist_body と同じ不変条件）。
_UNESCAPED_MD = re.compile(r"(?<!\\)[\[\]<>`]")


def _ctx(email: str = "a@vectorinc.co.jp") -> SkillContext:
    return SkillContext(request_id="req-test", user_id="U1", metadata={"user_email": email})


class _Source:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def posts(self, n: int | None = None) -> list[dict[str, Any]]:
        return self._rows[:n] if n else self._rows


class _EmptyApify:
    def ig_search(self, keyword: str, **kw: Any) -> tuple[list[Any], float]:
        return [], 0.0


class _RecordingPersister:
    """schedule() の kwargs を記録するだけの persister（x_research のテストと同じ代役）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def schedule(self, **kw: Any) -> None:
        self.calls.append(kw)


class _RaisingPersister:
    def schedule(self, **kw: Any) -> None:
        raise RuntimeError("persist down")


class _CaptureRepo:
    """IngestRepository の代役（upsert された doc/chunk を貯める）。"""

    docs: ClassVar[list[Any]] = []

    def __init__(self, pgvector: Any) -> None:
        pass

    def upsert_document_with_chunks(self, doc: Any, chunks: list[Any], request_id: str) -> str:
        _CaptureRepo.docs.append((doc, chunks))
        return "doc-id"


class _InlineExecutor:
    """submit をその場で実行する executor（本番の単一ワーカーの代わり）。"""

    def submit(self, fn: Any, **kwargs: Any) -> None:
        fn(**kwargs)


class _FakeEmbedder:
    def embed_passage(self, text: str) -> list[float]:
        return [0.01] * 1024


def _skill(
    persister: Any, *, rows: list[dict[str, Any]] | None = None, bedrock: Any = None
) -> SearchSurfaceCheckSkill:
    return SearchSurfaceCheckSkill(
        apify=_EmptyApify(),  # type: ignore[arg-type]
        bedrock=bedrock or FakeBedrock(),
        publisher=lambda path, *, request_id, query: _REPORT,
        tiktok_source_factory=lambda job_id, audit_hash: _Source(
            s3_rows() if rows is None else rows
        ),
        clock=lambda: NOW,
        persister=persister,
    )


def _input(**kw: Any) -> SearchSurfaceCheckInput:
    base: dict[str, Any] = {
        "keywords": [KEYWORD],
        "acquire_job_id": _JOB_ID,
        "client_name": "GABAN",
    }
    base.update(kw)
    return SearchSurfaceCheckInput(**base)


# ---------------------------------------------------------------------------
# 記録の呼び出し
# ---------------------------------------------------------------------------


def test_persist_is_scheduled_with_labels_owner_and_report() -> None:
    rec = _RecordingPersister()
    out = _skill(rec).run(_input(), _ctx())
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["tool"] == "search_surface"
    assert call["product_name"] == "GABAN"  # Vault の取引先 anchor（cls_project）
    assert call["owner_email"] == "a@vectorinc.co.jp"
    assert call["request_id"] == "req-test"
    # ingest/classify.py の固定表（_DOC_TYPES / _SOLUTIONS）から選んだ語彙
    assert call["cls_doc_type"] == "報告書"
    assert call["cls_solution"] == "SEO"
    assert call["source_uri"] == out.report_url == _REPORT
    assert call["dedup_key"] == _surface_dedup_key([KEYWORD], NOW)
    # 題名に媒体名を入れて build_app_html の 媒体/ タグを付ける
    assert call["title"] == "GABAN 検索上位チェック「スパイスカレー 作り方」（TikTok）"


def test_body_has_conclusion_facts_and_top10() -> None:
    rec = _RecordingPersister()
    _skill(rec).run(_input(), _ctx())
    md = rec.calls[0]["body_md"]
    assert md.startswith("# GABAN 検索上位チェック")
    assert "KW: 「スパイスカレー 作り方」／媒体: TikTok／実測 2026-09-25" in md
    assert "取得できなかった面: Instagram「スパイスカレー 作り方」" in md
    # 結論（見出し・勝ち筋・空白・打ち手・共通する切り口）
    assert "結論: 料理系クリエイターが上位15本中7本を持ち、再生の68%を取る面" in md
    assert "- 勝ち筋: クリエイター7本で再生の68%" in md and "（2・9位）" in md
    assert "- 空白: 公式は0本" in md
    assert "- 打ち手: フォロワー1万〜10万人の料理クリエイター" in md
    assert "- 上位に共通する切り口: 「スパイスを4つに絞る」1・2・14位" in md
    # 主要な集計（Slack 文面と同じ数字）
    assert "- 投稿者の構成: クリエイター 7本（再生の68%）／一般 4本（再生の5%）" in md
    assert "- 常連: スパイスこうき（@spice_koki） 2枠（2・9位）" in md
    assert "- フォロワー帯: " in md and "1万〜10万人" in md and "1万人未満は" in md
    assert "- 保存率の中央値 3.49%（最高は2位 @spice_koki 6.82%）" in md
    assert "- 投稿時期: " in md and "直近90日 6本" in md
    assert re.search(r"- 本文かタグに「スパイスカレー 作り方」の語をすべて含む: \d+/15本", md)
    assert "- PR表記あり: 6位" in md
    assert "- 「GABAN」に触れた投稿: 無し" in md  # 取引先のノートなので「無かった」も残す
    # 上位 10 本（順位・@ID・タイプ・再生・URL）。11 位以降は載せない
    post_lines = [ln for ln in md.splitlines() if re.match(r"^- \d+位 @", ln)]
    assert len(post_lines) == 10
    assert post_lines[0] == (
        "- 1位 @gonosara（クリエイター） 35.1万回 "
        "〈https://www.tiktok.com/@gonosara/video/7400000000000000001〉"
    )
    assert [ln.split("位", 1)[0] for ln in post_lines] == [f"- {r}" for r in range(1, 11)]
    assert f"〈{_REPORT}〉" in md


def test_rule_fallback_is_marked_in_the_body() -> None:
    rec = _RecordingPersister()
    _skill(rec, bedrock=FakeBedrock(analyze_error=RuntimeError("Throttling"))).run(_input(), _ctx())
    md = rec.calls[0]["body_md"]
    assert "（AI の読みを作れず、集計だけの見出し）" in md


def test_no_persist_without_client_name() -> None:
    """KW だけの実行は記録しない（cls_project＝取引先 anchor を KW で汚さない）。"""
    rec = _RecordingPersister()
    out = _skill(rec).run(_input(client_name=None), _ctx())
    assert out.surfaces and rec.calls == []


def test_no_persist_when_persister_is_not_injected() -> None:
    out = _skill(None).run(_input(), _ctx())
    assert out.surfaces and out.report_url == _REPORT


# ---------------------------------------------------------------------------
# 失敗しても応答は返る
# ---------------------------------------------------------------------------


def test_persist_failure_does_not_break_the_response() -> None:
    out = _skill(_RaisingPersister()).run(_input(), _ctx())
    assert out.report_url == _REPORT
    assert out.slack_summary.startswith("**検索上位チェック**")
    assert out.surfaces[0].conclusion is not None


def test_body_build_failure_does_not_break_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    import teamagent.skills.search_surface_check.skill as skill_mod

    def boom(*a: Any, **kw: Any) -> str:
        raise ValueError("broken body")

    monkeypatch.setattr(skill_mod, "build_surface_summary_md", boom)
    rec = _RecordingPersister()
    out = _skill(rec).run(_input(), _ctx())
    assert rec.calls == [] and out.report_url == _REPORT


# ---------------------------------------------------------------------------
# 第三者の文字列の安全化
# ---------------------------------------------------------------------------


def test_third_party_text_is_neutralized_in_the_body() -> None:
    rows = s3_rows()
    rows[1]["account_id"] = "ev`il]<script>"  # 2 位と 9 位は同じアカウント＝常連にも出る
    rows[8]["account_id"] = "ev`il]<script>"
    rows[1]["account_name"] = "[[clients/機密顧客]]"
    rows[8]["account_name"] = "[[clients/機密顧客]]"
    rows[0]["url"] = "javascript:alert()"
    rows[2]["url"] = "https://evil.example/phish"
    conclusion = {
        "headline": "釣り[ここ](javascript:steal())と<b>tag</b>",
        "winning": {"text": "![x](https://evil.example/a.png)", "ranks": [2]},
        "gap": {"text": "`code`と[[wikilink]]", "ranks": []},
        "actions": [],
        "angles": [],
    }
    rec = _RecordingPersister()
    _skill(rec, rows=rows, bedrock=FakeBedrock(conclusion=conclusion)).run(_input(), _ctx())
    md = rec.calls[0]["body_md"]
    assert not _UNESCAPED_MD.search(md), "未エスケープの [ ] < > ` が残っている（injection 面）"
    assert "[[clients/" not in md and "<script>" not in md and "<b>" not in md
    assert "javascript:alert" not in md  # 危険なスキームの URL は載せない
    assert "evil.example" not in md  # 既知 SNS ホスト以外の URL は載せない（LLM 文中は伏字）
    assert "釣り" in md  # 可読テキストは残す（黙って全消ししない）


# ---------------------------------------------------------------------------
# 重複排除キー（同じ KW 群・同じ日・同じ利用者は 1 doc）
# ---------------------------------------------------------------------------


def test_dedup_key_same_kw_set_same_day_is_one_record() -> None:
    a = _surface_dedup_key(["スパイスカレー 作り方", "カレー"], NOW)
    # 並び順・全角空白・連続空白・大文字小文字は同じ調査
    b = _surface_dedup_key(["カレー", "スパイスカレー　 作り方"], NOW + 3600)
    assert a == b
    assert _surface_dedup_key(["TikTok 料理"], NOW) == _surface_dedup_key(["tiktok 料理"], NOW)


def test_dedup_key_changes_with_day_and_kw_set() -> None:
    a = _surface_dedup_key(["カレー"], NOW)
    assert a != _surface_dedup_key(["カレー"], NOW + 86_400)  # 別の日の実測は別の記録
    assert a != _surface_dedup_key(["カレー", "スパイス"], NOW)  # KW 群が違えば別の記録
    # 区切りを入れて連結する（「ab」+「c」と「a」+「bc」を同じキーにしない）
    assert _surface_dedup_key(["ab", "c"], NOW) != _surface_dedup_key(["a", "bc"], NOW)


def test_real_persister_external_id_collapses_rerun_and_splits_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本物の ResearchPersister を通すと、同じ人の再実行は同じ external_id、別の人は別になる。"""
    import teamagent.ingest.repository as repo_mod

    monkeypatch.delenv("EMBEDDER_BACKEND", raising=False)
    monkeypatch.delenv("EMBEDDING_COLUMN", raising=False)
    monkeypatch.setattr(repo_mod, "IngestRepository", _CaptureRepo)
    _CaptureRepo.docs = []
    persister = ResearchPersister(
        pgvector=object(), embedder=_FakeEmbedder(), executor=_InlineExecutor()
    )
    skill = _skill(persister)
    skill.run(_input(), _ctx())
    skill.run(_input(), _ctx())  # 同じ日・同じ KW 群・同じ人の再実行
    skill.run(_input(), _ctx("b@vectorinc.co.jp"))  # 別の人
    ids = [doc.external_id for doc, _ in _CaptureRepo.docs]
    assert len(ids) == 3
    assert ids[0] == ids[1] and ids[0] != ids[2]
    assert ids[0].startswith("xresearch:search_surface:")
    doc, chunks = _CaptureRepo.docs[0]
    assert doc.metadata["cls_project"] == "GABAN"
    assert doc.metadata["cls_doc_type"] == "報告書"
    assert doc.metadata["cls_solution"] == "SEO"
    assert doc.metadata["x_research_tool"] == "search_surface"  # export_vault が全文を残す印
    assert doc.acl_emails == ["a@vectorinc.co.jp"]
    assert len(chunks) == 1 and "結論: " in chunks[0].content


# ---------------------------------------------------------------------------
# 有効化フラグ（USE_RESEARCH_PERSIST・既定 OFF）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("flag", "expect_persister"), [("1", True), ("0", False), (None, False)])
def test_factory_injects_persister_only_when_flag_is_on(
    monkeypatch: pytest.MonkeyPatch, flag: str | None, expect_persister: bool
) -> None:
    import teamagent.orchestrator.factory as factory

    monkeypatch.setattr(
        factory,
        "_build_search_skill",
        lambda: types.SimpleNamespace(pgvector=object(), embedder=object()),
    )
    monkeypatch.setenv("USE_SEARCH_SURFACE_TOOL", "1")
    if flag is None:
        monkeypatch.delenv("USE_RESEARCH_PERSIST", raising=False)
    else:
        monkeypatch.setenv("USE_RESEARCH_PERSIST", flag)
    spec = next(s for s in factory.build_production_tools() if s.name == "search_surface_check")
    assert spec.factory is not None
    skill = spec.factory()
    assert isinstance(skill._persister, ResearchPersister) is expect_persister
