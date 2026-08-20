"""カルテへの「関連資料の同梱」テスト（ユーザー要求 2026-08-19 / 同日レビュー裁定を反映）。

死守する不変条件:
  - 顧客の資料が 0 件なら **セクションを出さない**（空セクション禁止）
  - **``KARTE_ATTACH_DOCS`` は関連資料機能まるごとの kill switch**（既定 ON）。false なら
    一覧も添付も出ず、本機能追加前の出力（カルテ本文＋出典 URL のみ）に戻る。
    カルテの answer は「聞かれたチャンネル」へ出るので、資料名と Drive リンクを
    出し続けたままバイトだけ止めるのは kill switch ではない（2026-08-19 H1 裁定）
  - Google ネイティブ文書（Slides/Docs/Sheets の docs.google.com リンク）は添付候補に
    しない（``download_file_bytes`` は get_media 専用で必ず失敗し、毎回誤った注記が付く）
  - 同じ資料を短時間に重ね送りしない（OpenClaw の再試行・多段ツール対策）
  - 案件名を名指しされたら添付は一致資料だけ（一致 1 件で無関係資料を巻き込まない）。
    案件名なしなら顧客資料の新しい順で上位 K 件
  - 配信先は依頼者本人の DM 固定。``channel_id`` があってもチャンネルへ出さない
  - **資料セクション（資料名・Drive リンク）もチャンネルには出さない**（2026-08-20 裁定 A）。
    チャンネル / スラッシュで呼ばれたらセクションは本人 DM へ送り、本文には資料名を
    含まない 1 行の通知だけを出す。DM で呼ばれたときだけその場に出す。
    DM へ送れなかったら本文には資料の情報を 1 文字も出さない（本文自体は必ず返す）
  - L2 オーケストレーター（run_agent）の中間ステップでは 1 件も送らない。**かつ**
    その戻り値もチャンネル面なら件数だけ（ダークフラグ 1 本で裁定 A が破れない）
  - チャンネルへ出す 1 行の数字は「DM に名前を書いた件数」と「実ファイルを送れた件数」
    だけ。**その顧客の資料在庫数を公開面に出さない**・送っていないものを送ったと言わない
  - 行の client_name が要求顧客と矛盾する資料は 1 行も出さない（他社案件の資料が
    「顧客名を 1 回言う」だけで実ファイルごと DM に落ちない）
  - 依頼文の断片（「の」「今週の空き時間」）が client_name に来たら資料経路へ入らない
    （client_name_guard・mail_* の G5 と同じ。カルテ本文は返す＝ fail-open）
  - 本人 DM の解決は 1 リクエスト 1 回（添付と一覧転送で往復を二重に撃たない）
  - 送った資料は必ず一覧に表示されていて、注記に**名前**が出る（部分失敗も申告する）
  - 添付上限 K はハードクランプされる / 添付が失敗してもカルテ本文は必ず返る
  - 提案文に実在しない資料名が混じらない / 外部文字列で装飾リンクを描画させない
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.adapters.slack_client import SlackPostResult
from teamagent.orchestrator.sdk_runner import build_skill_tools
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import SkillContext
from teamagent.skills.clientkarte.documents import (
    LIST_MAX,
    DocumentsSection,
    KarteDoc,
    attachment_only_notice,
    availability_notice,
    belongs_to_client,
    build_documents_section,
    channel_notice,
    is_dm_surface,
    to_docs,
)
from teamagent.skills.clientkarte.schema import ClientKarteInput, ClientKarteOutput
from teamagent.skills.clientkarte.skill import (
    ClientKarteSkill,
    _env_flag,
    _opt_meta,
    reset_attach_dedup_ledger,
)

KARTE_BODY = "### 1. 一行サマリ\n提案中"
ME = "u@vectorinc.co.jp"
# ⚠️ 面のフィクスチャは **実在する Slack ID の形**でなければ意味が無い。
# 本番の channel_id は mcp_gateway/caller_claim.py の ``^[CDG][A-Z0-9]{8,}$`` を
# 通った値で、公開チャンネル ID には D が普通に含まれる（下の ``C08D3KQ7ABC``）。
# ``"C_PUBLIC"`` のような疑似 ID を使っていると、``is_dm_surface`` を
# ``"D" in channel_id`` へ緩める変異が **1 本も赤にならない**（2026-08-20 変異 M4 実測）。
#
# 依頼者本人としか繋がっていない面（Slack の im は必ず ``D`` で始まる）。
DM_CH = "D01ABCDEFGH"
# 本人以外も読む面。ここに資料名を出したら裁定 A 違反。**D を含む実在形**にしてある。
PUBLIC_CH = "C08D3KQ7ABC"
# プライベートチャンネル / グループ DM。ここも本人だけの面ではない。
GROUP_CH = "G01D2EFGHIJ"

# ── フィクスチャ ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "KARTE_ATTACH_DOCS",
        "KARTE_ATTACH_DOCS_MAX",
        "KARTE_ATTACH_DOCS_MAX_BYTES",
        "KARTE_ATTACH_DOCS_DEDUP_TTL_S",
        "SLACK_WORKSPACE_DOMAIN",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _clean_dedup_ledger() -> Any:
    """重複防止台帳はプロセス内 module 変数。テスト間で漏らさない。"""
    reset_attach_dedup_ledger()
    yield
    reset_attach_dedup_ledger()


@pytest.fixture
def attach_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """添付経路の検証は env を明示 ON にして行う（既定値の変更に引きずられないため）。

    既定は ON だが、それを固定するのは ``test_attach_is_on_by_default`` 1 本の役目にする。
    """
    monkeypatch.setenv("KARTE_ATTACH_DOCS", "1")


def _doc(
    title: str,
    *,
    source_uri: str = "",
    source_type: str = "gdrive",
    modified_at: str = "2026-06-01",
    cls_project: str | None = None,
    client_name: str | None = None,
) -> dict[str, Any]:
    """list_documents_for_client が返す行 dict（tests/adapters の実例と同じ形）。"""
    return {
        "title": title,
        "source_uri": source_uri or f"gdrive://{re.sub(r'[^A-Za-z0-9]', '', title) or 'F'}",
        "source_type": source_type,
        "modified_at": modified_at,
        "cls_industry": None,
        "cls_project": cls_project,
        "cls_doc_type": "提案書",
        "cls_solution": None,
        "cls_budget": None,
        "cls_target": None,
        "client_name": client_name,
        "excerpt": "抜粋",
    }


def _bedrock() -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text=KARTE_BODY,
        usage=TokenUsage(
            input_tokens=10,
            output_tokens=10,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.001,
        ),
        model_id="jp.anthropic.claude-sonnet-4-6",
        latency_ms=1,
        stop_reason="end_turn",
    )
    return mock


def _pgvector(docs: list[dict[str, Any]], *, hits: list[SearchHit] | None = None) -> MagicMock:
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    mock = MagicMock()
    mock.connection.return_value = cm
    mock.list_client_timeline_recent.return_value = (
        hits
        if hits is not None
        else [
            SearchHit(
                chunk_id=1,
                content="温度感は高い",
                score=1.0,
                metadata={"occurred_at": "2026-05-10"},
            )
        ]
    )
    mock.list_documents_for_client.return_value = docs
    return mock


def _slack(*, upload: Any = True, post: Any = True) -> MagicMock:
    mock = MagicMock()
    mock.lookup_user_id_by_email = AsyncMock(return_value="U1")
    mock.open_dm = AsyncMock(return_value="D1")
    mock.upload_file = AsyncMock(
        **({"side_effect": upload} if isinstance(upload, list) else {"return_value": upload})
    )
    # 資料セクション本文の DM 転送（チャンネル呼び出し時）。実 SlackClient と同じく
    # 業務的な失敗は例外ではなく ``SlackPostResult.ok=False`` で返る。
    mock.post_message = AsyncMock(
        return_value=SlackPostResult(channel="D1", ts="1755400000.1", ok=bool(post))
    )
    return mock


def _dm_meta(email: str | None = ME) -> dict[str, Any]:
    """DM でカルテを呼ばれたときの metadata（資料セクションはその場に出る面）。"""
    meta: dict[str, Any] = {"channel_id": DM_CH}
    if email is not None:
        meta["user_email"] = email
    return meta


def _dm_text(slack: MagicMock) -> str:
    """本人 DM へ転送されたセクション本文（1 通目）。"""
    return str(slack.post_message.await_args.args[1])


def _gdrive(*, payload: Any = b"%PDF-1.4 fake") -> MagicMock:
    mock = MagicMock()
    if isinstance(payload, list):
        mock.download_file_bytes.side_effect = payload
    else:
        mock.download_file_bytes.return_value = payload
    return mock


def _skill(
    docs: list[dict[str, Any]],
    *,
    slack: MagicMock | None = None,
    gdrive: MagicMock | None = None,
    hits: list[SearchHit] | None = None,
) -> ClientKarteSkill:
    return ClientKarteSkill(
        bedrock=_bedrock(),
        pgvector=_pgvector(docs, hits=hits),
        slack=slack if slack is not None else _slack(),
        gdrive=gdrive if gdrive is not None else _gdrive(),
    )


def _run(
    docs: list[dict[str, Any]],
    *,
    project_name: str | None = None,
    client_name: str = "花王",
    slack: MagicMock | None = None,
    gdrive: MagicMock | None = None,
    metadata: dict[str, Any] | None = None,
    hits: list[SearchHit] | None = None,
) -> Any:
    """既定は **DM でカルテを呼ばれた**ケース（セクションがその場の answer に出る面）。

    チャンネル呼び出しの検証は ``metadata`` を明示的に渡して行う（§16）。
    """
    skill = _skill(docs, slack=slack, gdrive=gdrive, hits=hits)
    return skill.run(
        ClientKarteInput(client_name=client_name, project_name=project_name),
        SkillContext(request_id="r", metadata=metadata if metadata is not None else _dm_meta()),
    )


# ── 1. 顧客資料 0 件 → 何も出さない ───────────────────────────────────────


def test_no_documents_emits_no_section() -> None:
    slack = _slack()
    out = _run([], project_name="プリマヴィスタUV50", slack=slack)

    assert out.answer == KARTE_BODY  # カルテ本文だけ（空セクションを出さない）
    assert "📎" not in out.answer
    assert out.document_count == 0
    assert out.attached_count == 0
    slack.upload_file.assert_not_awaited()


def test_no_documents_without_project_also_emits_no_section() -> None:
    out = _run([])
    assert "📎" not in out.answer


# ── 2. 案件一致あり → リンク一覧 ＋ 一致資料だけ実ファイル添付 ──────────────


@pytest.mark.usefixtures("attach_on")
def test_project_match_lists_links_and_attaches_only_matched_files() -> None:
    docs = [
        _doc("プリマヴィスタUV50_提案書.pdf", source_uri="gdrive://F_UV50"),
        _doc("KANEBO様_500万視聴PKG.pdf", source_uri="gdrive://F_KANEBO"),
    ]
    slack = _slack()
    gdrive = _gdrive()
    out = _run(docs, project_name="プリマヴィスタUV50", slack=slack, gdrive=gdrive)

    assert "📎 関連資料（2件）" in out.answer  # 一覧は顧客資料の全件
    assert "見つかりませんでした" not in out.answer
    assert KARTE_BODY in out.answer  # 本文は壊れない
    lines = out.answer.splitlines()
    assert lines[lines.index("📎 関連資料（2件）") + 1].endswith("プリマヴィスタUV50_提案書.pdf")
    # 添付は案件一致資料 1 件だけ（無関係資料を巻き込まない）
    assert out.attached_count == 1
    assert [c.kwargs["file_id"] for c in gdrive.download_file_bytes.call_args_list] == ["F_UV50"]
    assert slack.upload_file.await_count == 1


@pytest.mark.usefixtures("attach_on")
def test_unmatched_documents_are_never_attached() -> None:
    """案件一致 1 件でも、残り枠を無関係資料で埋めない（0 件時と非対称にしない）。"""
    docs = [_doc("melt施策_提案.pdf", source_uri="gdrive://F_MELT")]
    docs += [_doc(f"無関係{i}.pdf", source_uri=f"gdrive://F_X{i}") for i in range(5)]
    slack = _slack()
    gdrive = _gdrive()
    out = _run(docs, project_name="melt", slack=slack, gdrive=gdrive)

    assert out.attached_count == 1
    assert [c.kwargs["file_id"] for c in gdrive.download_file_bytes.call_args_list] == ["F_MELT"]
    assert slack.upload_file.await_count == 1
    assert "melt施策_提案.pdf" in out.answer
    # 無関係資料は一覧には出るが、1 件も送られていない
    for name in (f"無関係{i}.pdf" for i in range(5)):
        assert name not in out.answer.split("（このうち")[-1]


@pytest.mark.usefixtures("attach_on")
def test_without_project_name_attaches_the_newest_documents() -> None:
    """案件名なし（Slack から普通にカルテを呼ぶ経路）でも資料は一緒に出る。

    要求は「カルテを見て資料をクリックする工数をなくす」。案件名が取れないときに
    1 件も送らないと、実運用の主経路で機能が 1 度も動かない。
    """
    docs = [
        _doc("A提案.pdf", source_uri="gdrive://F_A"),
        _doc("B報告.pdf", source_uri="gdrive://F_B"),
    ]
    slack = _slack()
    gdrive = _gdrive()
    out = _run(docs, slack=slack, gdrive=gdrive)

    assert "📎 関連資料（2件）" in out.answer  # 一覧とリンクは従来どおり出す
    assert out.attached_count == 2
    assert [c.kwargs["file_id"] for c in gdrive.download_file_bytes.call_args_list] == [
        "F_A",
        "F_B",
    ]
    assert slack.upload_file.await_count == 2


@pytest.mark.usefixtures("attach_on")
def test_without_project_name_attaches_newest_first_up_to_the_cap() -> None:
    """案件名なしの添付は「顧客資料の新しい順」の先頭 K 件（取得順＝ modified_at DESC）。"""
    docs = [_doc(f"資料{i}.pdf", source_uri=f"gdrive://F{i}") for i in range(6)]
    gdrive = _gdrive()
    out = _run(docs, gdrive=gdrive)

    assert out.attached_count == 3
    assert [c.kwargs["file_id"] for c in gdrive.download_file_bytes.call_args_list] == [
        "F0",
        "F1",
        "F2",
    ]


def test_resolvable_url_becomes_a_slack_link_and_unresolvable_stays_name_only() -> None:
    drive = "https://drive.google.com/file/d/F1/view"
    docs = [
        _doc("リンクあり.pdf", source_uri=drive),
        _doc("リンクなし.pdf", source_uri="gdrive://F2"),  # 内部識別子 → URL を推測しない
    ]
    out = _run(docs)

    assert f"• <{drive}|リンクあり.pdf>" in out.answer
    assert "• リンクなし.pdf" in out.answer
    assert "gdrive://" not in out.answer


# ── 3. 案件一致 0 件 ＋ 他資料あり → 提案（自動添付しない）───────────────────


@pytest.mark.usefixtures("attach_on")
def test_project_miss_offers_real_documents_without_attaching() -> None:
    docs = [
        _doc("KANEBO様_500万視聴PKG.pdf"),
        _doc("花王_縦型ソリューション.pdf"),
        _doc("花王_2026上期レポート.pdf"),
    ]
    slack = _slack()
    gdrive = _gdrive()
    out = _run(docs, project_name="プリマヴィスタUV50", slack=slack, gdrive=gdrive)

    assert (
        "「プリマヴィスタUV50」の資料は見つかりませんでした。"
        "花王さんでは「KANEBO様_500万視聴PKG.pdf」など3件あります。お送りしますか？"
    ) in out.answer
    # 実在名を最大3件挙げる
    for title in (d["title"] for d in docs):
        assert title in out.answer
    # 提案しただけ＝勝手に送らない
    assert out.attached_count == 0
    gdrive.download_file_bytes.assert_not_called()
    slack.upload_file.assert_not_awaited()


def test_offer_names_only_real_documents() -> None:
    """提案文に実在しない資料名が混じらない（名前は必ず実データ由来）。"""
    docs = [_doc("KANEBO様_500万視聴PKG.pdf"), _doc("花王_縦型ソリューション.pdf")]
    real = {d["title"] for d in docs}

    out = _run(docs, project_name="存在しない案件")

    quoted = set(re.findall(r"「([^」]+)」", out.answer))
    # 案件名の引用（先頭の「存在しない案件」）以外は全て実在する資料名
    assert quoted - {"存在しない案件"} <= real
    assert quoted - {"存在しない案件"}  # 少なくとも1件は挙げている


def test_document_count_is_the_fetched_total_not_the_attached_count() -> None:
    """schema の description どおり「取得できた総数」であることを固定する。

    提案分岐（1 件も同梱していない）でも総数が入る＝「同梱した件数」ではない。
    """
    docs = [_doc(f"資料{i}.pdf") for i in range(9)]
    out = _run(docs, project_name="存在しない案件")

    assert out.document_count == 9
    assert out.attached_count == 0
    field = ClientKarteOutput.model_fields["document_count"]
    assert field.description is not None
    assert "取得できた関連資料の件数" in field.description


def test_offer_lists_at_most_three_names() -> None:
    docs = [_doc(f"資料{i}.pdf") for i in range(7)]
    out = _run(docs, project_name="別案件")

    assert "など7件あります" in out.answer  # 総数は正直に述べる
    assert out.answer.count("• ") == 3  # 挙げる名前は最大3件


# ── 4. 案件名一致が優先される ──────────────────────────────────────────────


@pytest.mark.usefixtures("attach_on")
def test_project_matched_documents_are_ranked_first() -> None:
    """新しい順で後ろにいる案件一致資料が、一致しない新しい資料より先に一覧へ出る。"""
    docs = [
        _doc("最新_別案件.pdf", source_uri="gdrive://F_NEW", modified_at="2026-08-01"),
        _doc("melt施策_提案.pdf", source_uri="gdrive://F_MELT", modified_at="2026-01-01"),
    ]
    gdrive = _gdrive()
    out = _run(docs, project_name="melt", gdrive=gdrive)

    lines = out.answer.splitlines()
    header = "📎 関連資料（2件）"
    assert header in lines
    # 新しい「最新_別案件.pdf」より、案件一致の「melt施策_提案.pdf」が上に来る
    assert lines[lines.index(header) + 1].endswith("melt施策_提案.pdf")
    assert lines[lines.index(header) + 2].endswith("最新_別案件.pdf")
    assert [c.kwargs["file_id"] for c in gdrive.download_file_bytes.call_args_list] == ["F_MELT"]


def test_project_match_uses_cls_project_too() -> None:
    """案件名の照合先は title だけではない（cls_project も見る）。

    ⚠️ 行の ``client_name`` 列は **顧客同定**に使う（``belongs_to_client``）ので、
    案件名照合の材料として当てにしない。要求顧客と違う client_name の行は
    そもそもセクションへ来ない（下の ``test_documents_of_another_client_never_reach_the_user``）。
    """
    docs = [
        _doc("無題っぽい資料.pdf", cls_project="melt 2026春"),
        _doc("別の資料.pdf", client_name="花王株式会社", cls_project="melt"),
        _doc("無関係.pdf"),
    ]
    out = _run(docs, project_name="melt")

    lines = out.answer.splitlines()
    header = "📎 関連資料（3件）"
    assert header in lines
    assert lines[lines.index(header) + 1].endswith("無題っぽい資料.pdf")  # cls_project 一致
    assert lines[lines.index(header) + 2].endswith("別の資料.pdf")  # cls_project 一致
    assert lines[lines.index(header) + 3].endswith("無関係.pdf")


# ── 5. 添付上限 K ─────────────────────────────────────────────────────────


@pytest.mark.usefixtures("attach_on")
def test_attach_cap_defaults_to_three() -> None:
    docs = [_doc(f"資料{i}.pdf", source_uri=f"gdrive://F{i}") for i in range(6)]
    slack = _slack()
    out = _run(docs, project_name="資料", slack=slack)

    assert out.attached_count == 3
    assert slack.upload_file.await_count == 3
    assert "📎 関連資料（6件）" in out.answer  # 一覧は添付上限に縛られない


@pytest.mark.usefixtures("attach_on")
def test_attach_cap_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KARTE_ATTACH_DOCS_MAX", "1")
    docs = [_doc(f"資料{i}.pdf", source_uri=f"gdrive://F{i}") for i in range(4)]
    slack = _slack()
    out = _run(docs, project_name="資料", slack=slack)

    assert out.attached_count == 1
    assert slack.upload_file.await_count == 1


@pytest.mark.usefixtures("attach_on")
def test_attach_cap_is_hard_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """env をいくつに上げても 5 件を超えて一気にアップロードしない。"""
    monkeypatch.setenv("KARTE_ATTACH_DOCS_MAX", "999")
    docs = [_doc(f"資料{i}.pdf", source_uri=f"gdrive://F{i}") for i in range(12)]
    slack = _slack()
    out = _run(docs, project_name="資料", slack=slack)

    assert out.attached_count == 5
    assert slack.upload_file.await_count == 5


def test_section_never_offers_more_attachables_than_the_list_shows() -> None:
    """1 層目のクランプ: セクションが差し出す添付候補は一覧の行数を超えない。"""
    rows = [_doc(f"資料{i}.pdf", source_uri=f"gdrive://F{i}") for i in range(12)]

    no_project = build_documents_section(client_name="花王", project_name=None, docs=to_docs(rows))
    matched = build_documents_section(client_name="花王", project_name="資料", docs=to_docs(rows))

    assert len(no_project.attachable) == LIST_MAX
    assert len(matched.attachable) == LIST_MAX


@pytest.mark.usefixtures("attach_on")
def test_attach_is_hard_clamped_even_if_the_section_offers_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2 層目のクランプ: セクション側が 12 件差し出しても skill は 5 件で止める。

    ``build_documents_section`` 側のクランプに依存せず、``_ATTACH_MAX_CAP`` 単体が
    効いていることを固定する（片方を外しても緑のまま、を防ぐ）。
    """
    monkeypatch.setenv("KARTE_ATTACH_DOCS_MAX", "999")
    rows = [_doc(f"資料{i}.pdf", source_uri=f"gdrive://F{i}") for i in range(12)]
    oversized = DocumentsSection(
        kind="matched",
        text="📎 関連資料（12件）",
        attachable=tuple(to_docs(rows)),
    )
    slack = _slack()
    skill = _skill([], slack=slack)

    attached, note = skill._attach_documents(
        oversized,
        "花王",
        SkillContext(request_id="r", metadata={"user_email": ME}),
        MagicMock(),
    )

    assert attached == 5
    assert slack.upload_file.await_count == 5
    assert "の 5 件を実ファイルでお送りしました" in note


@pytest.mark.usefixtures("attach_on")
def test_attached_documents_are_always_shown_in_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「N 件お送りしました」が、一覧に 1 行も出ていない資料を指さない。"""
    monkeypatch.setenv("KARTE_ATTACH_DOCS_MAX", "999")
    docs = [_doc(f"資料{i}.pdf", source_uri=f"gdrive://F{i}") for i in range(12)]
    out = _run(docs, project_name="資料")

    listed = [line[2:] for line in out.answer.splitlines() if line.startswith("• ")]
    assert len(listed) == 5
    note = out.answer.split("（このうち ")[1].split(" の ")[0]
    for name in note.split("、"):
        assert name in listed  # 送った資料は必ず一覧に見えている


# ── 6. 実ファイル添付は既定 ON・env で止められる ───────────────────────────


def test_attach_is_on_by_default() -> None:
    """env 未設定でカルテと実ファイルが一緒に出る（要求の既定値）。"""
    docs = [_doc("melt_提案.pdf", source_uri="gdrive://F1")]
    slack = _slack()
    gdrive = _gdrive()
    out = _run(docs, project_name="melt", slack=slack, gdrive=gdrive)

    assert "📎 関連資料（1件）" in out.answer  # リンク一覧はフラグ無しで常に出る
    assert out.attached_count == 1
    gdrive.download_file_bytes.assert_called_once()
    slack.upload_file.assert_awaited_once()


def test_attach_flag_off_removes_the_whole_documents_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kill switch: false なら **資料名も Drive リンクも**出ない（H1 裁定）。

    カルテの answer は「聞かれたチャンネル」へ出る（slack_bot の in_channel 応答）。
    実ファイルだけ止めて資料名と URL を出し続けるのは「バイトだけ守って題名とリンクを
    守っていない」＝ kill switch とは呼べない。OFF なら本機能追加前の出力に戻す。
    """
    monkeypatch.setenv("KARTE_ATTACH_DOCS", "false")
    docs = [_doc("花王_見積_社外秘.pdf", source_uri="https://drive.google.com/file/d/FX/view")]
    slack = _slack()
    gdrive = _gdrive()
    out = _run(docs, project_name="melt", slack=slack, gdrive=gdrive)

    assert out.answer == KARTE_BODY  # 本機能追加前と同じ出力
    assert "📎" not in out.answer
    assert "花王_見積_社外秘.pdf" not in out.answer
    assert "drive.google.com" not in out.answer
    assert out.attached_count == 0
    assert out.document_count == 0
    assert "添付できませんでした" not in out.answer  # OFF は「失敗」ではない
    gdrive.download_file_bytes.assert_not_called()
    slack.upload_file.assert_not_awaited()


def test_attach_flag_off_does_not_even_query_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF なら資料を引きにも行かない（DB も叩かない）。"""
    monkeypatch.setenv("KARTE_ATTACH_DOCS", "0")
    pg = _pgvector([_doc("A提案.pdf")])
    skill = ClientKarteSkill(bedrock=_bedrock(), pgvector=pg, slack=_slack(), gdrive=_gdrive())

    out = skill.run(
        ClientKarteInput(client_name="花王"),
        SkillContext(request_id="r", metadata={"user_email": ME}),
    )

    assert out.answer == KARTE_BODY
    pg.list_documents_for_client.assert_not_called()


def test_attach_flag_off_also_stops_the_no_project_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """案件名なしの経路（既定で送る側）も同じ 1 本のスイッチで止まる。"""
    monkeypatch.setenv("KARTE_ATTACH_DOCS", "off")
    slack = _slack()
    gdrive = _gdrive()
    out = _run([_doc("A提案.pdf")], slack=slack, gdrive=gdrive)

    assert "📎" not in out.answer
    assert "A提案.pdf" not in out.answer
    assert out.attached_count == 0
    gdrive.download_file_bytes.assert_not_called()
    slack.upload_file.assert_not_awaited()


def test_attach_max_zero_stops_files_but_keeps_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """``KARTE_ATTACH_DOCS_MAX=0``（件数で止める操作）が黙って既定 3 件に戻らない（H3）。"""
    monkeypatch.setenv("KARTE_ATTACH_DOCS", "1")
    monkeypatch.setenv("KARTE_ATTACH_DOCS_MAX", "0")
    docs = [_doc(f"資料{i}.pdf", source_uri=f"gdrive://F{i}") for i in range(4)]
    slack = _slack()
    gdrive = _gdrive()
    out = _run(docs, project_name="資料", slack=slack, gdrive=gdrive)

    assert out.attached_count == 0
    gdrive.download_file_bytes.assert_not_called()
    slack.upload_file.assert_not_awaited()
    assert "📎 関連資料（4件）" in out.answer  # 一覧は残る（0 は「送らない」の指定）
    assert "添付できませんでした" not in out.answer  # 明示指定は「失敗」ではない


@pytest.mark.usefixtures("attach_on")
def test_negative_attach_max_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """負値・非数値は「不正」として既定へ戻す（0 だけが明示的な無効指定）。"""
    monkeypatch.setenv("KARTE_ATTACH_DOCS_MAX", "-1")
    docs = [_doc(f"資料{i}.pdf", source_uri=f"gdrive://F{i}") for i in range(4)]
    assert _run(docs, project_name="資料").attached_count == 3

    reset_attach_dedup_ledger()  # 同一テスト内の 2 回目が重複防止に食われないように
    monkeypatch.setenv("KARTE_ATTACH_DOCS_MAX", "three")
    assert _run(docs, project_name="資料").attached_count == 3


def test_unknown_env_flag_value_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KARTE_ATTACH_DOCS=enabled`` のようなタイポで機能が黙って反転しない。"""
    monkeypatch.setenv("KARTE_ATTACH_DOCS", "enabled")
    assert _env_flag("KARTE_ATTACH_DOCS", default=True) is True
    assert _env_flag("KARTE_ATTACH_DOCS", default=False) is False
    monkeypatch.setenv("KARTE_ATTACH_DOCS", "on")
    assert _env_flag("KARTE_ATTACH_DOCS", default=False) is True
    monkeypatch.setenv("KARTE_ATTACH_DOCS", "off")
    assert _env_flag("KARTE_ATTACH_DOCS", default=True) is False


# ── 7. 添付が失敗しても本文は返る（fail-open）──────────────────────────────


@pytest.mark.usefixtures("attach_on")
def test_upload_failure_still_returns_the_karte() -> None:
    slack = _slack(upload=False)  # upload_file が False（権限不足・未参加など）
    out = _run([_doc("melt_提案.pdf")], project_name="melt", slack=slack)

    assert KARTE_BODY in out.answer
    assert "📎 関連資料（1件）" in out.answer
    assert "添付できませんでした" in out.answer
    assert out.attached_count == 0


@pytest.mark.usefixtures("attach_on")
def test_download_failure_still_returns_the_karte() -> None:
    gdrive = MagicMock()
    gdrive.download_file_bytes.side_effect = RuntimeError("403")
    out = _run([_doc("melt_提案.pdf")], project_name="melt", gdrive=gdrive)

    assert KARTE_BODY in out.answer
    assert "添付できませんでした" in out.answer
    assert out.attached_count == 0


@pytest.mark.usefixtures("attach_on")
def test_attach_failure_note_points_at_a_route_that_exists() -> None:
    """添付候補は gdrive:// 行＝一覧にリンクが出ない。存在しない導線を案内しない。"""
    gdrive = MagicMock()
    gdrive.download_file_bytes.side_effect = RuntimeError("403")
    out = _run(
        [_doc("melt_提案.pdf", source_uri="gdrive://F1")], project_name="melt", gdrive=gdrive
    )

    assert "• melt_提案.pdf" in out.answer  # 一覧はリンクなしの名前だけ
    assert "http" not in out.answer  # 一覧に URL は 1 本も無い
    assert "上のリンク" not in out.answer
    assert "資料名を指定して「〇〇の資料を出して」とご依頼ください" in out.answer


@pytest.mark.usefixtures("attach_on")
def test_partial_download_failure_is_reported() -> None:
    """3 件中 1 件しか届かなかったことを黙らせない。"""
    docs = [_doc(f"melt_{i}.pdf", source_uri=f"gdrive://F{i}") for i in range(3)]
    gdrive = _gdrive(payload=[b"ok", RuntimeError("403"), RuntimeError("403")])
    out = _run(docs, project_name="melt", gdrive=gdrive)

    assert out.attached_count == 1
    assert "melt_0.pdf の 1 件を実ファイルでお送りしました" in out.answer
    assert "残り 2 件は実ファイルを取得できずお送りできませんでした" in out.answer


@pytest.mark.usefixtures("attach_on")
def test_slack_client_explosion_still_returns_the_karte() -> None:
    slack = MagicMock()
    slack.lookup_user_id_by_email = AsyncMock(side_effect=RuntimeError("slack down"))
    out = _run([_doc("melt_提案.pdf")], project_name="melt", slack=slack)

    assert KARTE_BODY in out.answer
    assert "添付できませんでした" in out.answer


def test_document_lookup_failure_never_breaks_the_karte() -> None:
    pg = _pgvector([])
    pg.list_documents_for_client.side_effect = RuntimeError("db down")
    skill = ClientKarteSkill(bedrock=_bedrock(), pgvector=pg, slack=_slack(), gdrive=_gdrive())

    out = skill.run(
        ClientKarteInput(client_name="花王"),
        SkillContext(request_id="r", metadata={"user_email": ME}),
    )

    assert out.answer == KARTE_BODY
    assert out.document_count == 0


@pytest.mark.usefixtures("attach_on")
def test_oversized_documents_are_not_attached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KARTE_ATTACH_DOCS_MAX_BYTES", "8")
    slack = _slack()
    out = _run(
        [_doc("melt_巨大.pdf")],
        project_name="melt",
        slack=slack,
        gdrive=_gdrive(payload=b"x" * 4096),
    )

    assert out.attached_count == 0
    assert "添付できませんでした" in out.answer
    slack.upload_file.assert_not_awaited()


# ── 8. 配信先は依頼者本人の DM 固定 ────────────────────────────────────────


@pytest.mark.usefixtures("attach_on")
def test_channel_context_never_drops_files_into_the_channel() -> None:
    """聞かれた場所（公開/社外共有チャンネル）へ社内資料のバイトを出さない。"""
    slack = _slack()
    out = _run(
        [_doc("melt_提案.pdf")],
        project_name="melt",
        slack=slack,
        metadata={"user_email": ME, "channel_id": PUBLIC_CH, "thread_ts": "111.222"},
    )

    assert out.attached_count == 1
    uploaded_channels = [call.args[0] for call in slack.upload_file.await_args_list]
    assert uploaded_channels == ["D1"]  # 本人 DM だけ
    assert PUBLIC_CH not in uploaded_channels
    assert slack.upload_file.await_args.kwargs["thread_ts"] is None
    slack.open_dm.assert_awaited()
    # 添付注記（資料名を含む）もチャンネルではなく DM 側へ回る（裁定 A）。
    assert "melt_提案.pdf の 1 件を実ファイルでお送りしました" in _dm_text(slack)
    assert "melt_提案.pdf" not in out.answer


@pytest.mark.usefixtures("attach_on")
def test_channel_without_requester_email_attaches_nothing() -> None:
    """DM を開けない（本人が特定できない）なら送らない。チャンネルに落とさない。"""
    slack = _slack()
    out = _run(
        [_doc("melt_提案.pdf")],
        project_name="melt",
        slack=slack,
        metadata={"channel_id": PUBLIC_CH, "thread_ts": "111.222"},
    )

    assert out.attached_count == 0
    slack.upload_file.assert_not_awaited()
    # 出せる面が 1 つも無いので、資料名もリンクも通知も本文に出さない。
    assert out.answer == KARTE_BODY
    assert "melt_提案.pdf" not in out.answer
    slack.post_message.assert_not_awaited()


def test_no_destination_keeps_the_karte_without_document_names() -> None:
    """配信先も面も分からない呼び出しでは、資料の情報を 1 文字も本文に出さない。

    ``metadata`` が空＝依頼者も channel_id も無い。channel_id 不明は「チャンネルかも
    しれない」側に倒す（``is_dm_surface`` の既定）ので、資料名を出せる面が存在しない。
    それでもカルテ本文は必ず返る（fail-open）。
    """
    slack = _slack()
    out = _run([_doc("melt_提案.pdf")], project_name="melt", slack=slack, metadata={})

    assert out.answer == KARTE_BODY
    assert "📎" not in out.answer
    assert "melt_提案.pdf" not in out.answer
    assert "添付できませんでした" not in out.answer  # 配信先不明は「失敗」ではない
    assert out.attached_count == 0
    assert out.document_count == 1  # 引けてはいる（出す面が無いだけ）
    slack.lookup_user_id_by_email.assert_not_awaited()
    slack.post_message.assert_not_awaited()


# ── 9. L2 オーケストレーター（run_agent）経由では 1 件も送らない ─────────────


def _orchestrated_call(
    skill: ClientKarteSkill, *, args: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    """run_agent と同じ経路（build_skill_tools のハンドラ）で clientkarte を呼ぶ。"""
    spec = ToolSpec(
        ClientKarteSkill.name,
        ClientKarteSkill.description,
        ClientKarteSkill,
        factory=lambda: skill,
    )
    _defn, handler = build_skill_tools([spec], request_id="r", user_id=ME, ctx_metadata=metadata)[0]
    result = asyncio.run(handler(args))
    payload: dict[str, Any] = json.loads(result["content"][0]["text"])
    return payload


@pytest.mark.usefixtures("attach_on")
def test_orchestrated_tool_call_never_attaches_files() -> None:
    """run_agent の「まず clientkarte で把握」で、最終回答前に資料を投下しない。"""
    slack = _slack()
    gdrive = _gdrive()
    skill = _skill([_doc("melt_提案.pdf")], slack=slack, gdrive=gdrive)

    payload = _orchestrated_call(
        skill,
        args={"client_name": "花王", "project_name": "melt"},
        metadata={"user_email": ME, "channel_id": DM_CH},
    )

    assert payload["attached_count"] == 0
    assert "📎 関連資料（1件）" in payload["answer"]  # DM 面なので材料は返る
    assert "添付できませんでした" not in payload["answer"]
    gdrive.download_file_bytes.assert_not_called()
    slack.upload_file.assert_not_awaited()
    slack.post_message.assert_not_awaited()


@pytest.mark.usefixtures("attach_on")
def test_orchestrated_tool_call_in_a_channel_returns_no_document_names() -> None:
    """裁定 A は run_agent 経路でも成立する（2026-08-20 レビュー 指摘4）。

    L2 の最終回答は **呼ばれたチャンネルへ出る**。旧実装は
    ``is_orchestrated_call(...) or is_dm_surface(...)`` の短絡順のせいで、
    orchestrated なら ``channel_id="C…"`` でも資料名＋Drive リンクを戻り値に入れていた。
    ``USE_AGENT_ORCHESTRATOR`` はいま既定 OFF だが、**ダークフラグ 1 本で裁定 A が
    破れる**状態をテストで塞ぐ（コメントではなく機械で気づける形にする）。
    """
    slack = _slack()
    gdrive = _gdrive()
    skill = _skill([_doc("melt_提案.pdf")], slack=slack, gdrive=gdrive)

    payload = _orchestrated_call(
        skill,
        args={"client_name": "花王", "project_name": "melt"},
        metadata={"user_email": ME, "channel_id": PUBLIC_CH, "thread_ts": "111.222"},
    )

    assert payload["attached_count"] == 0
    assert "melt_提案.pdf" not in payload["answer"]
    assert "drive.google.com" not in payload["answer"]
    assert "• " not in payload["answer"]
    # 件数だけの 1 行。まだ 1 通も送っていないので「お送りしました」とは言わない。
    assert "📎 関連資料1件が見つかりました（DM でお送りできます）" in payload["answer"]
    # 中間ステップなので副作用はゼロのまま（DM も撃たない）。
    gdrive.download_file_bytes.assert_not_called()
    slack.upload_file.assert_not_awaited()
    slack.post_message.assert_not_awaited()


@pytest.mark.usefixtures("attach_on")
def test_direct_tool_call_still_attaches() -> None:
    """オーケストレーター印が無ければ従来どおり本人 DM へ届く（印だけが差分）。"""
    slack = _slack()
    gdrive = _gdrive()
    out = _run(
        [_doc("melt_提案.pdf")],
        project_name="melt",
        slack=slack,
        gdrive=gdrive,
        metadata={"user_email": ME, "channel_id": PUBLIC_CH, "thread_ts": "111.222"},
    )

    assert out.attached_count == 1
    gdrive.download_file_bytes.assert_called_once()
    slack.upload_file.assert_awaited_once()


# ── 10. 添付候補の安全性 ───────────────────────────────────────────────────


@pytest.mark.usefixtures("attach_on")
def test_knowledge_sheet_row_is_never_attached_as_a_file() -> None:
    """gsheets 行の source_uri からシート本体 ID を誤抽出してシートごと添付しない。"""
    row = "https://docs.google.com/spreadsheets/d/SHEET1/edit?gid=1#gid=1&range=5:5"
    gdrive = _gdrive()
    out = _run(
        [_doc("社内共有情報_花王__melt", source_uri=row, source_type="gsheets")],
        project_name="melt",
        gdrive=gdrive,
    )

    assert "📎 関連資料（1件）" in out.answer
    assert f"<{row}|" in out.answer  # リンクとしては出す
    assert out.attached_count == 0
    gdrive.download_file_bytes.assert_not_called()


def test_existing_source_links_still_appended(monkeypatch: pytest.MonkeyPatch) -> None:
    """既存の出典 URL 追記（_with_source_links）と共存する。"""
    monkeypatch.setenv("SLACK_WORKSPACE_DOMAIN", "vectorinc")
    hits = [
        SearchHit(
            chunk_id=1,
            content="温度感は高い",
            score=1.0,
            metadata={"source_uri": "slack://C0ORIGIN/1755400000.100100"},
        )
    ]
    out = _run([_doc("資料.pdf")], hits=hits)

    permalink = "https://vectorinc.slack.com/archives/C0ORIGIN/p1755400000100100"
    assert f"🔗 出典: {permalink}" in out.answer
    assert out.answer.index("🔗 出典:") < out.answer.index("📎 関連資料")


# ── 11. 一覧の打ち切りと mrkdwn 安全性 ─────────────────────────────────────


def test_long_list_is_capped_and_states_the_remainder() -> None:
    docs = [_doc(f"資料{i}.pdf") for i in range(8)]
    out = _run(docs)

    assert "📎 関連資料（8件）" in out.answer  # 総数は正直
    assert out.answer.count("• ") == 5  # 並べるのは 5 行まで
    assert "…ほか3件" in out.answer


def test_titles_cannot_inject_slack_markup() -> None:
    """資料名は外部由来。任意 URL を装飾リンクとして描画させない。"""
    docs = [_doc("<https://evil.example|クリック>.pdf", source_uri="gdrive://F1")]
    out = _run(docs)

    assert "<https://evil.example" not in out.answer  # リンクとして開かない
    assert "&lt;https://evil.example／クリック&gt;.pdf" in out.answer  # 文字としては見える


def test_project_name_cannot_inject_slack_markup() -> None:
    """案件名は利用者の入力。そのまま埋め込んでリンクを作らせない。"""
    out = _run([_doc("実在資料.pdf")], project_name="<https://evil.example|x>")

    assert "<https://evil.example" not in out.answer
    assert "&lt;https://evil.example／x&gt;" in out.answer
    assert "見つかりませんでした" in out.answer


# 装飾リンクの注入ペイロード。``https://`` 版は client_name_guard が
# ``operator_colon`` で先に弾く（下の ``test_client_name_with_an_operator_colon_...`` で固定）
# ため、**エスケープそのもの**を検査するペイロードはコロン無しにする。
# ``<@U…|ラベル>`` は Slack の user mention 記法で、コロン無しでも装飾描画される。
LINK_INJECTION = "<@U0EVIL|購買ポータル>"
LINK_INJECTION_ESCAPED = "&lt;@U0EVIL／購買ポータル&gt;"


def test_client_name_cannot_inject_slack_markup() -> None:
    """顧客名も外部由来（同じ 1 文の案件名・資料名だけ潰しても穴が残る）。"""
    out = _run(
        [_doc("実在資料.pdf")],
        client_name=LINK_INJECTION,
        project_name="存在しない案件",
    )

    assert "<@U0EVIL" not in out.answer
    assert f"{LINK_INJECTION_ESCAPED}さんでは" in out.answer


@pytest.mark.usefixtures("attach_on")
def test_client_name_cannot_inject_slack_markup_into_the_dm_forward() -> None:
    """**チャンネル呼び出しの DM 転送文面**でも顧客名をエスケープする。

    §11 の他の injection テストは全て ``_run`` 既定＝ DM 面（in-place）を通るので、
    裁定 A で新設した「チャンネル → DM 転送」の文面（``dm_forward_text``）が
    1 度も検査対象になっていなかった。``slack_label()`` を外す変異が
    ``tests/skills`` 全体で 1 本も赤にならない状態だった（2026-08-20 変異 M2 実測）。
    """
    slack = _slack()
    out = _run(
        [_doc("実在資料.pdf", source_uri="gdrive://F1")],
        client_name=LINK_INJECTION,
        slack=slack,
        metadata={"user_email": ME, "channel_id": PUBLIC_CH},
    )

    dm = _dm_text(slack)
    assert "<@U0EVIL" not in dm  # 本人 DM に装飾リンクを描画させない
    assert f"🗂️ 「{LINK_INJECTION_ESCAPED}」のカルテの関連資料です。" in dm
    # チャンネル側は件数だけ（顧客名すら出さない）
    assert "U0EVIL" not in out.answer


def test_client_name_with_an_operator_colon_never_reaches_the_documents_path() -> None:
    """コロン入りの client_name は資料経路へ 1 歩も入らない（ガードが先に弾く）。"""
    slack = _slack()
    gdrive = _gdrive()
    out = _run(
        [_doc("実在資料.pdf", source_uri="gdrive://F1")],
        client_name="<https://evil.example|社内ポータル>",
        slack=slack,
        gdrive=gdrive,
    )

    assert "📎" not in out.answer
    assert "実在資料.pdf" not in out.answer
    assert out.document_count == 0
    gdrive.download_file_bytes.assert_not_called()
    slack.upload_file.assert_not_awaited()


@pytest.mark.usefixtures("attach_on")
def test_attach_note_cannot_inject_slack_markup() -> None:
    """添付注記に埋める資料名も外部由来（ここだけ素通しにしない）。"""
    docs = [_doc("<https://evil.example|melt>.pdf", source_uri="gdrive://F1")]
    out = _run(docs, project_name="melt")

    assert out.attached_count == 1
    assert "<https://evil.example" not in out.answer
    assert "&lt;https://evil.example／melt&gt;.pdf の 1 件" in out.answer


# ── 12. Google ネイティブ文書は添付候補にしない ────────────────────────────


@pytest.mark.usefixtures("attach_on")
@pytest.mark.parametrize(
    "native_uri",
    [
        "https://docs.google.com/presentation/d/NATIVE/edit?usp=drivesdk",
        "https://docs.google.com/document/d/NATIVE/edit?usp=drivesdk",
        "https://docs.google.com/spreadsheets/d/NATIVE/edit?usp=drivesdk",
    ],
)
def test_google_native_documents_are_never_attach_candidates(native_uri: str) -> None:
    """Slides/Docs/Sheets の web_view_link を掴んで毎回失敗する経路を塞ぐ。

    ingest は gdrive 行の source_uri に ``f.web_view_link or f"gdrive://{f.id}"`` を焼き、
    Google ネイティブ mime も取込対象に含む。一方 ``download_file_bytes`` は get_media 専用で
    ネイティブ文書には **必ず失敗する**。候補化すると提案書が Google Slides の顧客では
    毎回 Drive API を 3 回叩いて失敗し、誤った「添付できませんでした」が常時付く。
    """
    slack = _slack()
    gdrive = _gdrive()
    out = _run(
        [_doc("提案書_ネイティブ", source_uri=native_uri, source_type="gdrive")],
        project_name="提案書",
        slack=slack,
        gdrive=gdrive,
    )

    gdrive.download_file_bytes.assert_not_called()  # 1 回も叩かない
    slack.upload_file.assert_not_awaited()
    assert out.attached_count == 0
    assert "添付できませんでした" not in out.answer  # 誤った失敗注記を出さない
    assert f"<{native_uri}|" in out.answer  # 一覧にはリンクとして出す


@pytest.mark.usefixtures("attach_on")
def test_binary_drive_file_link_is_still_attached() -> None:
    """バイナリの web_view_link（drive.google.com/file/d/ID）は従来どおり添付できる。"""
    uri = "https://drive.google.com/file/d/FBIN/view?usp=drivesdk"
    gdrive = _gdrive()
    out = _run(
        [_doc("提案書.pdf", source_uri=uri)],
        project_name="提案書",
        gdrive=gdrive,
    )

    assert out.attached_count == 1
    assert [c.kwargs["file_id"] for c in gdrive.download_file_bytes.call_args_list] == ["FBIN"]


@pytest.mark.usefixtures("attach_on")
def test_failure_note_mentions_the_link_only_when_the_list_has_one() -> None:
    """失敗注記は「その実行で実在する導線」だけを案内する。"""
    gdrive = MagicMock()
    gdrive.download_file_bytes.side_effect = RuntimeError("403")
    out = _run(
        [_doc("提案書.pdf", source_uri="https://drive.google.com/file/d/FBIN/view")],
        project_name="提案書",
        gdrive=gdrive,
    )

    assert "上の一覧のリンクから開くか" in out.answer  # リンクが実在するので案内してよい


# ── 13. 同じ資料を重ね送りしない（OpenClaw の再試行・多段ツール対策）──────────


@pytest.mark.usefixtures("attach_on")
def test_repeated_calls_do_not_resend_the_same_files() -> None:
    """同じ人に同じ資料を 2 度投下しない（実測: 2 回呼ぶと uploads 6 件だった）。"""
    docs = [_doc(f"資料{i}.pdf", source_uri=f"gdrive://F{i}") for i in range(3)]
    slack = _slack()
    gdrive = _gdrive()
    skill = _skill(docs, slack=slack, gdrive=gdrive)
    args = ClientKarteInput(client_name="花王", project_name="資料")
    ctx = SkillContext(request_id="r1", metadata=_dm_meta())

    first = skill.run(args, ctx)
    second = skill.run(args, SkillContext(request_id="r2", metadata=_dm_meta()))

    assert first.attached_count == 3
    assert second.attached_count == 0
    assert slack.upload_file.await_count == 3  # 6 にならない
    assert [c.kwargs["file_id"] for c in gdrive.download_file_bytes.call_args_list] == [
        "F0",
        "F1",
        "F2",
    ]
    assert "重複してはお送りしていません" in second.answer
    assert "📎 関連資料（3件）" in second.answer  # 一覧は 2 回目も出る


@pytest.mark.usefixtures("attach_on")
def test_new_documents_are_still_sent_after_a_partial_duplicate() -> None:
    """既送分だけを弾き、新しい資料は送る（重複防止で機能を殺さない）。"""
    slack = _slack()
    gdrive = _gdrive()
    skill = _skill([_doc("旧.pdf", source_uri="gdrive://F_OLD")], slack=slack, gdrive=gdrive)
    ctx = SkillContext(request_id="r1", metadata=_dm_meta())
    skill.run(ClientKarteInput(client_name="花王", project_name="."), ctx)

    skill._pgvector.list_documents_for_client.return_value = [
        _doc("旧.pdf", source_uri="gdrive://F_OLD"),
        _doc("新.pdf", source_uri="gdrive://F_NEW"),
    ]
    out = skill.run(ClientKarteInput(client_name="花王"), ctx)

    assert out.attached_count == 1
    assert slack.upload_file.await_count == 2  # 1 回目 1 件 + 2 回目 1 件
    assert "新.pdf の 1 件を実ファイルでお送りしました" in out.answer
    assert "重複してはお送りしていません" in out.answer


@pytest.mark.usefixtures("attach_on")
def test_dedup_can_be_disabled_with_zero_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KARTE_ATTACH_DOCS_DEDUP_TTL_S", "0")
    docs = [_doc("資料.pdf", source_uri="gdrive://F0")]
    slack = _slack()
    skill = _skill(docs, slack=slack)
    ctx = SkillContext(request_id="r", metadata={"user_email": ME})

    skill.run(ClientKarteInput(client_name="花王"), ctx)
    second = skill.run(ClientKarteInput(client_name="花王"), ctx)

    assert second.attached_count == 1
    assert slack.upload_file.await_count == 2


@pytest.mark.usefixtures("attach_on")
def test_failed_delivery_is_not_remembered_as_delivered() -> None:
    """送れなかった資料を「送った」と覚えて次回まで殺さない（全滅ケース）。"""
    docs = [_doc("資料.pdf", source_uri="gdrive://F0")]
    slack = _slack(upload=[False, True])
    skill = _skill(docs, slack=slack)
    ctx = SkillContext(request_id="r", metadata={"user_email": ME})

    first = skill.run(ClientKarteInput(client_name="花王"), ctx)
    second = skill.run(ClientKarteInput(client_name="花王"), ctx)

    assert first.attached_count == 0
    assert second.attached_count == 1


@pytest.mark.usefixtures("attach_on")
def test_only_delivered_files_are_remembered_on_partial_failure() -> None:
    """部分失敗: 届いた分だけ覚え、届かなかった分は次回ちゃんと再送する。

    「候補を全部覚える」実装だと、1 度上げ損ねた資料が TTL の間ずっと届かなくなる。
    """
    docs = [
        _doc("A提案.pdf", source_uri="gdrive://F_A"),
        _doc("B報告.pdf", source_uri="gdrive://F_B"),
    ]
    slack = _slack(upload=[True, False, True])  # 1回目: A成功/B失敗 → 2回目: B成功
    skill = _skill(docs, slack=slack)
    ctx = SkillContext(request_id="r", metadata=_dm_meta())

    first = skill.run(ClientKarteInput(client_name="花王"), ctx)
    second = skill.run(ClientKarteInput(client_name="花王"), ctx)

    assert first.attached_count == 1
    assert "残り 1 件は実ファイルを取得できずお送りできませんでした" in first.answer
    assert second.attached_count == 1
    assert "B報告.pdf の 1 件を実ファイルでお送りしました" in second.answer
    assert "A提案.pdf" not in second.answer.split("（このうち")[-1]  # A は再送しない
    assert slack.upload_file.await_count == 3


@pytest.mark.usefixtures("attach_on")
def test_dedup_is_scoped_per_requester() -> None:
    """別の人には同じ資料が普通に届く（台帳キーは (宛先, file_id)）。"""
    docs = [_doc("資料.pdf", source_uri="gdrive://F0")]
    slack = _slack()
    skill = _skill(docs, slack=slack)

    skill.run(
        ClientKarteInput(client_name="花王"),
        SkillContext(request_id="r1", metadata={"user_email": ME}),
    )
    other = skill.run(
        ClientKarteInput(client_name="花王"),
        SkillContext(request_id="r2", metadata={"user_email": "other@vectorinc.co.jp"}),
    )

    assert other.attached_count == 1
    assert slack.upload_file.await_count == 2


# ── 14. サイズ上限は adapter へ素通しする（受信してから捨てない）───────────


@pytest.mark.usefixtures("attach_on")
def test_size_cap_is_passed_down_to_the_drive_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``max_bytes`` を渡さないと adapter 既定 256MB まで実際に受信してしまう（M5）。"""
    monkeypatch.setenv("KARTE_ATTACH_DOCS_MAX_BYTES", "12345")
    gdrive = _gdrive()
    _run([_doc("資料.pdf", source_uri="gdrive://F0")], project_name="資料", gdrive=gdrive)

    assert gdrive.download_file_bytes.call_args.kwargs["max_bytes"] == 12345


@pytest.mark.usefixtures("attach_on")
def test_default_size_cap_is_50mib() -> None:
    gdrive = _gdrive()
    _run([_doc("資料.pdf", source_uri="gdrive://F0")], project_name="資料", gdrive=gdrive)

    assert gdrive.download_file_bytes.call_args.kwargs["max_bytes"] == 50 * 1024 * 1024


# ── 15. 配信先 email の正規化 ─────────────────────────────────────────────


def test_requester_email_is_stripped_before_slack_lookup() -> None:
    """空白入りのまま Slack へ投げると照合が外れて添付だけ黙って落ちる。"""
    assert _opt_meta("  u@v.co.jp  ") == "u@v.co.jp"
    assert _opt_meta("   ") is None
    assert _opt_meta(None) is None
    assert _opt_meta(123) is None


@pytest.mark.usefixtures("attach_on")
def test_padded_email_still_reaches_the_right_dm() -> None:
    slack = _slack()
    out = _run(
        [_doc("資料.pdf", source_uri="gdrive://F0")],
        project_name="資料",
        slack=slack,
        metadata={"user_email": f"  {ME}  "},
    )

    assert out.attached_count == 1
    assert slack.lookup_user_id_by_email.await_args.args[0] == ME


# ── 16. 資料セクションの出力面（2026-08-20 ユーザー裁定 A）────────────────────
#
# 「資料セクションは全部 DM。チャンネルで呼ばれたら本文は 1 行通知だけ」。
# ここが緑でないと、案件名を指定した呼び出しで **別案件の資料名**まで
# チャンネルに並ぶ（build_documents_section の一覧は matched + rest 全件）。


@pytest.mark.usefixtures("attach_on")
def test_channel_call_keeps_document_names_out_of_the_channel() -> None:
    """チャンネルで呼ばれたら、本文に出るのは資料名を含まない 1 行だけ。"""
    docs = [
        _doc("melt施策_提案.pdf", source_uri="https://drive.google.com/file/d/F_MELT/view"),
        _doc("別案件_見積_社外秘.pdf", source_uri="https://drive.google.com/file/d/F_OTHER/view"),
    ]
    slack = _slack()
    out = _run(
        docs,
        project_name="melt",
        slack=slack,
        metadata={"user_email": ME, "channel_id": PUBLIC_CH, "thread_ts": "111.222"},
    )

    assert (
        out.answer == f"{KARTE_BODY}\n\n📎 関連資料2件（うち実ファイル1件）を DM でお送りしました"
    )
    # 資料名・Drive リンク・添付注記のどれもチャンネルには出ない
    assert "melt施策_提案.pdf" not in out.answer
    assert "別案件_見積_社外秘.pdf" not in out.answer  # 案件を名指ししても別案件名が漏れない
    assert "drive.google.com" not in out.answer
    assert "• " not in out.answer

    # 中身は本人 DM の 1 通に入っている
    slack.post_message.assert_awaited_once()
    assert slack.post_message.await_args.args[0] == "D1"
    dm = _dm_text(slack)
    assert "🗂️ 「花王」のカルテの関連資料です。" in dm
    assert "📎 関連資料（2件）" in dm
    assert "melt施策_提案.pdf" in dm
    assert "別案件_見積_社外秘.pdf" in dm
    assert "melt施策_提案.pdf の 1 件を実ファイルでお送りしました" in dm


@pytest.mark.usefixtures("attach_on")
def test_dm_call_shows_the_section_in_place_without_forwarding() -> None:
    """DM で呼ばれたらその場が本人だけの面。転送せずその場に出す（二重送信しない）。"""
    slack = _slack()
    out = _run(
        [_doc("melt施策_提案.pdf", source_uri="gdrive://F_MELT")],
        project_name="melt",
        slack=slack,
        metadata={"user_email": ME, "channel_id": DM_CH},
    )

    assert "📎 関連資料（1件）" in out.answer
    assert "melt施策_提案.pdf" in out.answer
    assert "melt施策_提案.pdf の 1 件を実ファイルでお送りしました" in out.answer
    assert "DM でお送りしました\n" not in out.answer  # 1 行通知は出さない
    assert out.answer.count("📎") == 1
    slack.post_message.assert_not_awaited()  # 転送 DM は撃たない


@pytest.mark.usefixtures("attach_on")
def test_channel_call_with_failed_dm_still_reports_the_files_that_landed() -> None:
    """一覧の DM 転送に失敗しても、**実ファイルが届いた件数**だけは正直に出す。

    ここを黙らせると「聞いてもいない資料が DM に湧いて、チャンネルには何の説明も
    出ない」＝機能が黙って半分死ぬ状態が利用者から不可視になる（要修正3(b)）。
    出すのは数字だけ。資料名・URL は 1 文字も出さない。
    """
    slack = _slack(post=False)  # chat.postMessage が ok=False（未参加・権限不足など）
    out = _run(
        [_doc("melt施策_提案.pdf", source_uri="gdrive://F_MELT")],
        project_name="melt",
        slack=slack,
        metadata={"user_email": ME, "channel_id": PUBLIC_CH},
    )

    assert out.answer == (
        f"{KARTE_BODY}\n\n📎 関連資料の実ファイル1件を DM でお送りしました"
        "（一覧はお送りできませんでした）"
    )
    assert "melt施策_提案.pdf" not in out.answer
    assert "drive.google.com" not in out.answer
    slack.post_message.assert_awaited_once()


@pytest.mark.usefixtures("attach_on")
def test_channel_call_with_failed_dm_and_no_files_says_nothing() -> None:
    """1 通も届いていないなら「DM でお送りしました」と言わない（安全側・M3 変異の的）。

    提案分岐（案件一致 0 件）は実ファイルを 1 件も送らないので、DM 転送が落ちたら
    資料の情報はチャンネルに 1 文字も出ない。
    """
    slack = _slack(post=False)
    out = _run(
        [_doc("KANEBO様_500万視聴PKG.pdf")],
        project_name="プリマヴィスタUV50",
        slack=slack,
        metadata={"user_email": ME, "channel_id": PUBLIC_CH},
    )

    assert out.answer == KARTE_BODY  # カルテ本文は必ず返る（fail-open）
    assert "📎" not in out.answer
    assert "KANEBO様_500万視聴PKG.pdf" not in out.answer
    assert out.attached_count == 0


@pytest.mark.usefixtures("attach_on")
def test_channel_call_with_exploding_dm_still_returns_the_karte() -> None:
    """chat.postMessage が例外を投げてもカルテ本文を巻き込まない。"""
    slack = _slack()
    slack.post_message = AsyncMock(side_effect=RuntimeError("slack down"))
    out = _run(
        # Google ネイティブ文書＝添付候補にならない → 実ファイルは 0 件
        [_doc("melt施策_提案.pdf", source_uri="https://docs.google.com/presentation/d/N/edit")],
        project_name="melt",
        slack=slack,
        metadata={"user_email": ME, "channel_id": GROUP_CH},
    )

    assert out.answer == KARTE_BODY
    assert "melt施策_提案.pdf" not in out.answer


@pytest.mark.usefixtures("attach_on")
def test_missing_channel_id_is_treated_as_a_channel() -> None:
    """channel_id が無い経路（EC2 systemd の slack_bot / run_karte）も安全側へ倒す。

    ``runtime/slack_bot.py`` の ``run_karte`` は channel_id を metadata に入れず、
    スラッシュコマンドの応答は ``response_type="in_channel"`` で返る。ここを
    「DM 扱い」にすると資料名がそのままチャンネルへ出る。
    """
    slack = _slack()
    out = _run(
        [_doc("melt施策_提案.pdf", source_uri="gdrive://F_MELT")],
        project_name="melt",
        slack=slack,
        metadata={"user_email": ME},
    )

    assert (
        out.answer == f"{KARTE_BODY}\n\n📎 関連資料1件（うち実ファイル1件）を DM でお送りしました"
    )
    assert "melt施策_提案.pdf" not in out.answer
    assert "melt施策_提案.pdf" in _dm_text(slack)


@pytest.mark.usefixtures("attach_on")
def test_offer_branch_is_also_dm_only_in_a_channel() -> None:
    """提案分岐（案件一致 0 件）も同じ扱い。実在する他案件の資料名を晒さない。"""
    docs = [_doc("KANEBO様_500万視聴PKG.pdf"), _doc("花王_縦型ソリューション.pdf")]
    slack = _slack()
    out = _run(
        docs,
        project_name="プリマヴィスタUV50",
        slack=slack,
        metadata={"user_email": ME, "channel_id": PUBLIC_CH},
    )

    assert out.answer == f"{KARTE_BODY}\n\n📎 関連資料2件のご案内を DM でお送りしました"
    assert "KANEBO様_500万視聴PKG.pdf" not in out.answer
    assert "見つかりませんでした" not in out.answer
    dm = _dm_text(slack)
    assert "「プリマヴィスタUV50」の資料は見つかりませんでした。" in dm
    assert "KANEBO様_500万視聴PKG.pdf" in dm
    assert "花王_縦型ソリューション.pdf" in dm


def test_kill_switch_off_sends_nothing_anywhere_in_a_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kill switch OFF は DM 転送も撃たない（面の分岐が新しい出口を作っていない）。"""
    monkeypatch.setenv("KARTE_ATTACH_DOCS", "false")
    slack = _slack()
    out = _run(
        [_doc("花王_見積_社外秘.pdf")],
        project_name="melt",
        slack=slack,
        metadata={"user_email": ME, "channel_id": PUBLIC_CH},
    )

    assert out.answer == KARTE_BODY
    slack.post_message.assert_not_awaited()
    slack.upload_file.assert_not_awaited()


def test_channel_notice_never_carries_names_or_urls() -> None:
    """チャンネルへ出し得る 1 行を **全種類**まとめて検査する。数字以外を入れない。"""
    rows = [
        _doc("melt施策_提案.pdf", source_uri="https://drive.google.com/file/d/F1/view"),
        _doc("別案件_見積.pdf"),
    ]
    matched = build_documents_section(client_name="花王", project_name="melt", docs=to_docs(rows))
    offer = build_documents_section(
        client_name="花王", project_name="存在しない案件", docs=to_docs(rows)
    )
    none = DocumentsSection(kind="none", text="", attachable=(), count=0, listed_count=0)

    assert channel_notice(matched) == "📎 関連資料2件のご案内を DM でお送りしました"
    assert channel_notice(matched, delivered=1) == (
        "📎 関連資料2件（うち実ファイル1件）を DM でお送りしました"
    )
    assert channel_notice(offer) == "📎 関連資料2件のご案内を DM でお送りしました"
    assert channel_notice(none) == ""
    assert attachment_only_notice(2) == (
        "📎 関連資料の実ファイル2件を DM でお送りしました（一覧はお送りできませんでした）"
    )
    assert attachment_only_notice(0) == ""
    assert availability_notice(matched) == "📎 関連資料2件が見つかりました（DM でお送りできます）"
    assert availability_notice(none) == ""

    every_notice = (
        channel_notice(matched),
        channel_notice(matched, delivered=1),
        channel_notice(offer),
        attachment_only_notice(2),
        availability_notice(matched),
        availability_notice(offer),
    )
    for notice in every_notice:
        assert notice  # 空文字を「名前が出ていない」で通さない
        assert "melt施策_提案.pdf" not in notice
        assert "別案件_見積.pdf" not in notice
        assert "花王" not in notice
        assert "http" not in notice


def test_channel_notice_counts_what_was_sent_not_the_whole_inventory() -> None:
    """通知の数字は「DM に **名前を書いた**件数」（在庫総数ではない）。

    在庫総数（``count``）を使っていた旧実装は、8 件在庫 / 一覧 5 行 / 実ファイル 3 件の
    実行で「📎 関連資料8件を DM でお送りしました」と公開チャンネルへ出していた。
    送っていない 3 件まで送ったと読めるうえ、**その顧客について社内に何件資料があるか**
    （最大 ``_DOCS_FETCH_LIMIT``=50）を公開面へ漏らしていた（2026-08-20 要修正1・実測）。
    """
    rows = [_doc(f"資料{i}.pdf") for i in range(8)]
    section = build_documents_section(client_name="花王", project_name=None, docs=to_docs(rows))

    assert "📎 関連資料（8件）" in section.text  # DM の見出しは在庫総数を正直に出す
    assert section.count == 8
    assert section.listed_count == LIST_MAX == 5
    # チャンネルへ出るのは「名前を書いた 5 件」。在庫の 8 は出ない。
    assert channel_notice(section) == "📎 関連資料5件のご案内を DM でお送りしました"
    assert channel_notice(section, delivered=3) == (
        "📎 関連資料5件（うち実ファイル3件）を DM でお送りしました"
    )
    assert "8" not in channel_notice(section, delivered=3)


@pytest.mark.usefixtures("attach_on")
def test_channel_notice_does_not_claim_files_that_were_never_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KARTE_ATTACH_DOCS_MAX=0``（実ファイルを 1 件も送らない指定）で断言しない。"""
    monkeypatch.setenv("KARTE_ATTACH_DOCS_MAX", "0")
    slack = _slack()
    out = _run(
        [_doc(f"資料{i}.pdf", source_uri=f"gdrive://F{i}") for i in range(8)],
        slack=slack,
        metadata={"user_email": ME, "channel_id": PUBLIC_CH},
    )

    assert out.attached_count == 0
    slack.upload_file.assert_not_awaited()
    assert out.answer == f"{KARTE_BODY}\n\n📎 関連資料5件のご案内を DM でお送りしました"
    assert "実ファイル" not in out.answer
    assert "8件" not in out.answer  # 在庫総数を公開面に出さない


@pytest.mark.usefixtures("attach_on")
def test_channel_call_does_not_post_the_same_list_twice() -> None:
    """OpenClaw の再試行で同じ一覧が 2 通 DM に積まれない（実ファイルと同じ H4 ガード）。"""
    docs = [_doc(f"資料{i}.pdf", source_uri=f"gdrive://F{i}") for i in range(2)]
    slack = _slack()
    skill = _skill(docs, slack=slack)
    args = ClientKarteInput(client_name="花王", project_name="資料")
    meta: dict[str, Any] = {"user_email": ME, "channel_id": PUBLIC_CH}

    first = skill.run(args, SkillContext(request_id="r1", metadata=meta))
    second = skill.run(args, SkillContext(request_id="r2", metadata=meta))

    assert slack.post_message.await_count == 1  # 2 にならない
    # 2 回目も「DM でお送りしました」と言ってよい（内容は既に本人の手元にある）。
    # ただし 2 回目は実ファイルも重複防止で 1 件も上げていないので、
    # 「うち実ファイルN件」は名乗らない（送っていないものを送ったと言わない）。
    assert (
        first.answer == f"{KARTE_BODY}\n\n📎 関連資料2件（うち実ファイル2件）を DM でお送りしました"
    )
    assert second.answer == f"{KARTE_BODY}\n\n📎 関連資料2件のご案内を DM でお送りしました"
    assert second.attached_count == 0
    for answer in (first.answer, second.answer):
        assert "資料0.pdf" not in answer  # どちらも資料名は 1 件も出さない


@pytest.mark.usefixtures("attach_on")
def test_dm_forward_is_resent_when_the_list_changed() -> None:
    """内容が変わったら再送する（重複防止で新しい資料の案内を殺さない）。"""
    slack = _slack()
    skill = _skill([_doc("旧.pdf", source_uri="gdrive://F_OLD")], slack=slack)
    meta: dict[str, Any] = {"user_email": ME, "channel_id": PUBLIC_CH}
    skill.run(ClientKarteInput(client_name="花王"), SkillContext(request_id="r1", metadata=meta))

    skill._pgvector.list_documents_for_client.return_value = [
        _doc("旧.pdf", source_uri="gdrive://F_OLD"),
        _doc("新.pdf", source_uri="gdrive://F_NEW"),
    ]
    out = skill.run(
        ClientKarteInput(client_name="花王"), SkillContext(request_id="r2", metadata=meta)
    )

    assert slack.post_message.await_count == 2
    assert "新.pdf" in _dm_text(slack)
    assert "📎 関連資料2件（うち実ファイル1件）を DM でお送りしました" in out.answer


@pytest.mark.usefixtures("attach_on")
def test_dm_forward_dedup_is_scoped_per_requester() -> None:
    """別の人には同じ一覧がちゃんと届く（台帳キーは宛先込み）。"""
    docs = [_doc("資料.pdf", source_uri="gdrive://F0")]
    slack = _slack()
    skill = _skill(docs, slack=slack)

    skill.run(
        ClientKarteInput(client_name="花王"),
        SkillContext(request_id="r1", metadata={"user_email": ME, "channel_id": PUBLIC_CH}),
    )
    skill.run(
        ClientKarteInput(client_name="花王"),
        SkillContext(
            request_id="r2",
            metadata={"user_email": "other@vectorinc.co.jp", "channel_id": PUBLIC_CH},
        ),
    )

    assert slack.post_message.await_count == 2


@pytest.mark.usefixtures("attach_on")
def test_slack_client_is_built_once_per_skill() -> None:
    """1 実行で Slack へ 2 回書くが、AsyncWebClient は 1 つで使い回す。"""
    built = _slack()
    skill = ClientKarteSkill(
        bedrock=_bedrock(),
        pgvector=_pgvector([_doc("資料.pdf", source_uri="gdrive://F0")]),
        slack=None,  # 未注入 → 遅延生成の経路
        gdrive=_gdrive(),
    )
    calls: list[int] = []

    def _build() -> Any:
        calls.append(1)
        return built

    skill._build_slack = _build  # type: ignore[method-assign]
    skill.run(
        ClientKarteInput(client_name="花王"),
        SkillContext(request_id="r", metadata={"user_email": ME, "channel_id": PUBLIC_CH}),
    )

    assert len(calls) == 1  # 添付と DM 転送で 2 回作らない
    assert built.upload_file.await_count == 1
    assert built.post_message.await_count == 1


@pytest.mark.parametrize(
    ("channel_id", "expected"),
    [
        (DM_CH, True),
        (f"  {DM_CH}  ", True),
        ("D0KARTEDM1", True),
        # 🔴 公開チャンネル ID には D が普通に含まれる。``"D" in channel_id`` へ
        # 緩める変異はここで赤くなる（2026-08-20 変異 M4 の的）。
        ("C08D3KQ7ABC", False),
        ("G01D2EFGHIJ", False),
        # 🔴 D で始まるが im の形ではない（``startswith("D")`` だけの判定を落とす）。
        ("DEV", False),
        ("D", False),
        ("Dm01abcdefg", False),  # 小文字＝署名 claim の形を通っていない
        ("", False),  # 面が分からない＝チャンネルかもしれない側へ倒す
        (None, False),
        (12345, False),
    ],
)
def test_dm_surface_detection_defaults_to_channel(channel_id: Any, expected: bool) -> None:
    assert is_dm_surface(channel_id) is expected


def test_dm_surface_uses_the_same_id_shape_as_the_signed_caller_claim() -> None:
    """面判定の形を ``caller_claim`` の ``^[CDG][A-Z0-9]{8,}$`` と揃える。

    署名 claim を通った実在の ID しか本番には来ない。その形の D 版だけを DM と認め、
    それ以外は全部チャンネル側（安全側）へ倒す。
    """
    from teamagent.mcp_gateway.caller_claim import _SLACK_CHANNEL_RE

    for cid in ("D01ABCDEFGH", "C08D3KQ7ABC", "G01D2EFGHIJ"):
        assert _SLACK_CHANNEL_RE.fullmatch(cid), "テストの面フィクスチャが実在 ID の形でない"
    assert is_dm_surface("D01ABCDEFGH") is True
    assert is_dm_surface("C08D3KQ7ABC") is False


# ── 18. 他社の資料は 1 行も出さない（顧客同定フィルタ）──────────────────────
#
# ``list_documents_for_client`` の WHERE は ``cls_project / client_name / title`` の
# **部分一致**。「花王」と言っただけで「競合比較_花王_vs_ライオン_社外秘.pdf」
# （行の client_name は「ライオン」）が返り、旧実装はそれを一覧に載せ、
# **実ファイルとして本人 DM にアップロードしていた**（2026-08-20 要修正2・実測）。
# 宛先は本人なので RLS 越えではないが、資料の拡散トリガが「明示依頼」から
# 「顧客名を 1 回言う」へ降りる。


@pytest.mark.usefixtures("attach_on")
def test_documents_of_another_client_never_reach_the_user() -> None:
    """他社案件の資料は一覧にも添付にも入らない（実測された事故そのもの）。"""
    docs = [
        _doc("花王_縦型ソリューション.pdf", source_uri="gdrive://F_KAO", client_name="花王"),
        _doc(
            "競合比較_花王_vs_ライオン_社外秘.pdf",
            source_uri="gdrive://F_LION1",
            client_name="ライオン",
        ),
        _doc(
            "【ライオン】値引き条件メモ_花王案件参考.pdf",
            source_uri="gdrive://F_LION2",
            client_name="ライオン",
        ),
    ]
    slack = _slack()
    gdrive = _gdrive()
    out = _run(docs, client_name="花王", slack=slack, gdrive=gdrive)

    assert "📎 関連資料（1件）" in out.answer  # 花王の 1 件だけ
    assert "競合比較_花王_vs_ライオン_社外秘.pdf" not in out.answer
    assert "【ライオン】値引き条件メモ_花王案件参考.pdf" not in out.answer
    assert out.attached_count == 1
    assert [c.kwargs["file_id"] for c in gdrive.download_file_bytes.call_args_list] == ["F_KAO"]
    assert slack.upload_file.await_count == 1


@pytest.mark.usefixtures("attach_on")
def test_project_name_cannot_pull_in_another_clients_documents() -> None:
    """案件名を名指ししても、他社の行は案件一致の材料にならない。"""
    docs = [
        _doc("値引き条件メモ_melt.pdf", source_uri="gdrive://F_LION", client_name="ライオン"),
        _doc("melt施策_提案.pdf", source_uri="gdrive://F_KAO", client_name="花王"),
    ]
    gdrive = _gdrive()
    out = _run(docs, client_name="花王", project_name="melt", gdrive=gdrive)

    assert "値引き条件メモ_melt.pdf" not in out.answer
    assert [c.kwargs["file_id"] for c in gdrive.download_file_bytes.call_args_list] == ["F_KAO"]


def test_offer_branch_never_names_another_clients_documents() -> None:
    """提案分岐（``◯◯さんでは…あります`` ）の帰属が事実と食い違わない（指摘5）。

    行の client_name メタは「資生堂」なのに「花王さんでは…あります」と名指しし、
    しかも **こちらから DM で提示していた**。
    """
    docs = [
        _doc("資生堂_花王競合比較_社外秘.pdf", client_name="資生堂"),
        _doc("花王_2026上期レポート.pdf", client_name="花王"),
    ]
    out = _run(docs, client_name="花王", project_name="プリマヴィスタ")

    assert "資生堂_花王競合比較_社外秘.pdf" not in out.answer
    assert "花王_2026上期レポート.pdf" in out.answer
    assert "など1件あります" in out.answer  # 在庫件数も花王の分だけ


def test_all_documents_belonging_to_others_collapse_to_no_section() -> None:
    """他社の行しか無ければセクション自体を出さない（空セクション禁止と同じ扱い）。"""
    slack = _slack()
    out = _run(
        [_doc("競合比較_花王_vs_ライオン.pdf", client_name="ライオン")],
        client_name="花王",
        slack=slack,
    )

    assert out.answer == KARTE_BODY
    assert "📎" not in out.answer
    # 見せていない他社の資料を document_count が数えない
    # （L2 / OpenClaw が「1 件あります」と語れてしまうため）。
    assert out.document_count == 0
    slack.upload_file.assert_not_awaited()


@pytest.mark.parametrize(
    ("owner", "requested", "expected"),
    [
        (None, "花王", True),  # メタ未設定は判定不能＝落とさない（fail-open）
        ("", "花王", True),
        ("花王", "花王", True),
        ("花王株式会社", "花王", True),  # 法人格付きを落とさない
        ("花王", "花王株式会社", True),
        ("KAO", "kao", True),  # 大小無視
        ("ライオン", "花王", False),
        ("資生堂", "花王", False),
    ],
)
def test_belongs_to_client_only_drops_rows_that_contradict(
    owner: str | None, requested: str, expected: bool
) -> None:
    doc = KarteDoc(
        title="資料.pdf",
        source_uri="gdrive://F",
        source_type="gdrive",
        cls_project=None,
        client_name=owner,
    )
    assert belongs_to_client(doc, requested) is expected


# ── 19. 無差別走査の禁止（client_name_guard・mail_* の G5 と同じ）──────────────


@pytest.mark.usefixtures("attach_on")
@pytest.mark.parametrize("fragment", ["の", "A", "今週の空き時間", "返信必要"])
def test_request_fragments_never_deliver_documents(fragment: str) -> None:
    """依頼文の断片が client_name に入っても、資料は 1 件も配らない。

    実測（2026-08-20 要修正3）: ``client_name="の"`` で 8 件を掴み、新しい順 3 件を
    **実ファイルで DM 配信**していた。mail_summary / mail_followup（読むだけ）は
    同じガードを通しているのに、副作用の重い clientkarte だけ素通しだった。
    """
    slack = _slack()
    gdrive = _gdrive()
    pg = _pgvector([_doc(f"資料{i}.pdf", source_uri=f"gdrive://F{i}") for i in range(8)])
    skill = ClientKarteSkill(bedrock=_bedrock(), pgvector=pg, slack=slack, gdrive=gdrive)

    out = skill.run(
        ClientKarteInput(client_name=fragment),
        SkillContext(request_id="r", metadata=_dm_meta()),
    )

    assert out.answer == KARTE_BODY  # カルテ本文は従来どおり返す（fail-open）
    assert "📎" not in out.answer
    assert out.document_count == 0
    assert out.attached_count == 0
    pg.list_documents_for_client.assert_not_called()  # DB も叩かない
    gdrive.download_file_bytes.assert_not_called()
    slack.upload_file.assert_not_awaited()
    slack.post_message.assert_not_awaited()


def test_real_client_names_still_pass_the_guard() -> None:
    """ガードが正当な顧客名を殺していない（fail-close で機能を止めない）。"""
    for name in ("花王", "アサヒ飲料", "日本ガイシ", "とらや", "(株)ABC"):
        out = _run([_doc("資料.pdf", source_uri="gdrive://F0")], client_name=name)
        assert "📎 関連資料（1件）" in out.answer, name


# ── 20. 本人 DM の解決は 1 リクエスト 1 回 ─────────────────────────────────


@pytest.mark.usefixtures("attach_on")
def test_dm_is_resolved_once_per_request() -> None:
    """添付と一覧転送で ``lookupByEmail`` / ``conversations.open`` を二重に撃たない。

    実測（2026-08-20 要修正3(a)）: ``deliver_files`` と ``_post_dm`` がそれぞれ独立に
    解決していたため 1 リクエストで各 2 回。2 回目が rate limit / 一時失敗に当たると
    「ファイルだけ届いて説明ゼロ」に直行する。
    """
    slack = _slack()
    out = _run(
        [_doc("資料.pdf", source_uri="gdrive://F0")],
        slack=slack,
        metadata={"user_email": ME, "channel_id": PUBLIC_CH},
    )

    assert out.attached_count == 1
    assert slack.post_message.await_count == 1
    assert slack.lookup_user_id_by_email.await_count == 1
    assert slack.open_dm.await_count == 1


@pytest.mark.usefixtures("attach_on")
def test_no_drive_download_when_the_dm_cannot_be_opened() -> None:
    """DM を開けないなら Drive から 1 バイトも落とさない（面が無い＝配れない）。"""
    slack = _slack()
    slack.open_dm = AsyncMock(return_value=None)
    gdrive = _gdrive()
    out = _run(
        [_doc("資料.pdf", source_uri="gdrive://F0")],
        slack=slack,
        gdrive=gdrive,
        metadata={"user_email": ME, "channel_id": PUBLIC_CH},
    )

    assert out.attached_count == 0
    gdrive.download_file_bytes.assert_not_called()
    slack.upload_file.assert_not_awaited()
    assert out.answer == KARTE_BODY  # 面が無いので資料の情報は 1 文字も出さない
    assert slack.lookup_user_id_by_email.await_count == 1  # 2 度目を撃たない


# ── 17. Google ネイティブ文書の除外は KarteDoc 単体でも成立する ───────────────


@pytest.mark.parametrize(
    ("source_uri", "expected"),
    [
        ("gdrive://F_INTERNAL", "F_INTERNAL"),
        ("https://drive.google.com/file/d/F_BIN/view?usp=drivesdk", "F_BIN"),
        ("https://docs.google.com/presentation/d/NATIVE/edit?usp=drivesdk", None),
        ("https://docs.google.com/document/d/NATIVE/edit?usp=drivesdk", None),
        ("https://docs.google.com/spreadsheets/d/NATIVE/edit?usp=drivesdk", None),
        ("", None),
    ],
)
def test_attach_file_id_accepts_only_real_binaries(source_uri: str, expected: str | None) -> None:
    """``source_type`` で分岐しない（gdrive 行にもネイティブ文書が入る）ことを含めて固定。"""
    doc = KarteDoc(
        title="提案書",
        source_uri=source_uri,
        source_type="gdrive",
        cls_project=None,
        client_name=None,
    )
    assert doc.attach_file_id == expected
