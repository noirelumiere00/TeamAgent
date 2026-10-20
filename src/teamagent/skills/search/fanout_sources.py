"""統合検索（fan-out・第 1 版）の探し先: カルテ・事例・案件の取り出し（読み取りだけ）。

search 1 回で金庫・本人の Slack・取引先カルテ・事例・案件を同時に引くための部品のうち、
**pgvector から読むだけ**の 3 つをここに置く。レーンの並行・締め切り・答えの組み立ては
呼び出し側（fanout / answer_sections）の仕事で、ここは「引いて、分けて、状態で返す」だけ。

  - ``karte_lookup``: 名指しの取引先が確定したときだけ、営業 FB の最新数件を引く。
    clientkarte の ``Skill.run()`` は呼ばない。run() は Bedrock の要約を 1 回足し
    （clientkarte/skill.py:481-482）、資料の実ファイルを本人の DM へ送る（同:589-614）。
    検索の中で呼ぶと要約が二重になり、search のたびに DM が届く。SQL
    （``PgVectorClient.list_client_timeline_recent``）だけを使う。
  - ``classify_hits``: rerank の候補を、metadata だけで 施策実績／事例／案件／営業 FB／
    金庫の資料 に分ける（LLM は使わない）。
  - ``case_lookup_by_industry``: 名指しがあれば事例集の段 1→2、正準業種が取れて主ヒットに
    事例・施策実績が無ければ段 4 を引く（``PgVectorClient.list_case_studies``）。

守ること:
  R1 読み取りだけ。書き込み API・Slack・Bedrock を参照しない（テストで固定）。
  R2 失敗は例外を投げず、状態（ok / zero / failed(reason) / skipped(reason)）で返す。
     接続待ちの時間切れ（PoolTimeoutError）・statement timeout（QueryCanceled）を
     「0 件」と取り違えない（「無い」と「探せなかった」を分ける：composite.py の S3 と同じ）。
  R3 名指し・業種・明示された商材のいずれも無いときは SQL を打たない。
  R4 検索本体の接続を借りて読むときは、1 本ごとに SAVEPOINT で囲む。statement timeout で
     止まった SQL が本体のトランザクションを中断状態にし、後続の検索を全部失敗させない。
  R5 ログは件数・状態・理由コードだけ（取引先名・業種・本文を出さない）。

metadata のキーは取り込み側で確かめた値だけを使う:
  - 施策実績: ``campaign_aggregate='true'``（campaign_aggregate.py:478-481・
    pgvector_client.py:718-719 で検索の射影にも載る）。同じ文書に ``case_source='campaign_db'``・
    ``case_corpus='true'`` も付く（campaign_aggregate.py:442-461）ので、事例より先に判定する。
  - 事例: ``case_source`` が ``deck``（事例集 PPTX・pipeline.py:4851,5043）か
    ``sheet``（事例集マスター表・pipeline.py:4502）。ただし ``search_similar_new_schema`` は
    ``case_source``・``case_external_use`` を射影しない（pgvector_client.py:626-653）ので、
    主検索のヒットでは事例集 PPTX の印 ``cls_doc_type='事例'``（pipeline.py:5051・取り込みの
    LLM 分類の語彙には無い：classify.py:30）でも事例とみなす。社外利用の可否が読めない
    ヒットは **unknown（要確認）** にする（ok とは言わない）。
  - 案件: ``topic='案件決定'``（ingest_sources.yaml:57,185）。主検索の射影は topic も持たない
    ので、#proj-01 の投稿は ``channel_name`` に「案件決定」を含むことでも判定する
    （ingest_sources.yaml:49・pipeline.py:1810）。
  - 営業 FB: ``is_sales_fb``（射影では True、文書の metadata そのままでは 'true'）。
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

import psycopg
import structlog

from teamagent.adapters.pg_pool import PoolClosedError, PoolTimeoutError
from teamagent.adapters.pgvector_client import PgVectorClient, SearchHit
from teamagent.ingest.industry_taxonomy import OTHER_INDUSTRY, normalize_industry
from teamagent.skills._shared.client_name_guard import classify_client_name
from teamagent.skills.pre_meeting_brief.classify import is_usable_partial, normalize_company
from teamagent.skills.search.client_match import (
    aliases,
    normalize_client,
    normalize_filter_client,
)
from teamagent.skills.search.result_guard import find_client_mention, is_self_org_name

logger = structlog.get_logger(__name__)

PgConn = psycopg.Connection[dict[str, Any]]
#: 接続の借り方。呼ぶと RLS 済みの接続を返す context manager を作る（``rls_connection`` か
#: ``reuse_connection`` で作る）。接続を借りる所までここで持つのは、接続待ちの時間切れ
#: （PoolTimeoutError）も「探せなかった」として返すため。
ConnectionSource = Callable[[], AbstractContextManager[PgConn]]

# ── 件数の上限（設計 10-14: 別名ごとに limit 5・最大 3 本／段 4 は limit 3）──────────────
KARTE_PER_NAME_LIMIT = 5
KARTE_MAX_NAMES = 3
#: 返す営業 FB の件数（新しい順）。要約器への入力を膨らませない。
KARTE_MAX_ITEMS = 5
CASE_LIMIT = 3

# ── 状態と理由（固定語・答えの「探した範囲」とログに使う）───────────────────────────
SourceStatus = Literal["ok", "zero", "failed", "skipped"]
SourceName = Literal["karte", "case"]

REASON_OK = "ok"
#: 探して 0 件。
REASON_NO_RECORDS = "no_records"
#: 名前の一部が一致した行はあったが、別の取引先だったので除いた（0 件の内訳）。
REASON_OTHER_CLIENTS_ONLY = "other_clients_only"
#: 使わなかった理由。
REASON_NO_CLIENT = "no_client"
REASON_SELF_ORG = "self_org"
REASON_NOT_A_CLIENT_NAME = "not_a_client_name"
REASON_NO_INDUSTRY = "no_industry"
REASON_MAIN_HAS_CASES = "main_has_cases"
#: 探せなかった理由（「無い」という意味ではない）。
REASON_POOL_TIMEOUT = "pool_timeout"
REASON_STATEMENT_TIMEOUT = "statement_timeout"
REASON_DB_UNAVAILABLE = "db_unavailable"
REASON_DB_ERROR = "db_error"
REASON_INTERNAL_ERROR = "internal_error"

T = TypeVar("T")


@dataclass(frozen=True)
class SourceResult(Generic[T]):
    """1 つの探し先の結果。例外の代わりにこれを返す。

    - ``status``: ok＝見つかった／zero＝探して無かった／failed＝探せなかった／
      skipped＝条件に合わず使わなかった。
    - ``reason``: 上の ``REASON_*`` の固定語だけ。
    - ``searched``: 実際に SQL を打った語（取引先名・業種）。答えの「探した範囲」用で、
      ログには出さない。
    - ``excluded``: 名前の一部だけが一致した別の取引先として除いた件数。
    - ``incomplete``: 一部の SQL が失敗した（見つかった分だけ返している）。
    - ``stage``: 事例で当たった段（1・2・4・5。当たらなければ 0）。カルテは常に 0。
    """

    source: SourceName
    status: SourceStatus
    reason: str
    items: tuple[T, ...] = ()
    searched: tuple[str, ...] = ()
    excluded: int = 0
    incomplete: bool = False
    stage: int = 0
    elapsed_ms: int = 0


# ── 接続の借り方 ─────────────────────────────────────────────────────────────


def rls_connection(
    pgvector: PgVectorClient,
    metadata: Mapping[str, Any],
    *,
    app_role: str | None = "teamagent_app",
) -> ConnectionSource:
    """検索と同じ RLS で接続を借りる（SearchSkill._retrieve と同じ値の取り方：skill.py:1272-1286）。

    本人の email・groups・role を渡す。groups を落とすと Drive の domain 共有の資料が
    RLS で 0 件になる（pgvector_client.py:952-954）。
    """
    user_email = metadata.get("user_email")
    groups_raw = metadata.get("user_groups")
    user_groups = list(groups_raw) if isinstance(groups_raw, (list, tuple)) else None
    user_role = metadata.get("user_role")

    def _open() -> AbstractContextManager[PgConn]:
        return pgvector.connection(
            app_role=app_role,
            user_email=user_email,
            user_groups=user_groups,
            user_role=user_role,
        )

    return _open


def reuse_connection(conn: PgConn) -> ConnectionSource:
    """検索の本体がいま持っている接続（RLS 済み）を、そのまま使う。

    ⚠️ 同じスレッドで直列に呼ぶときだけ使う（psycopg の接続はスレッド間で共有しない：
    pg_pool.py:19-20）。ここで閉じたり commit したりはしない。SQL は 1 本ごとに
    SAVEPOINT で囲むので、失敗しても本体のトランザクションは使える状態で戻る（R4）。
    """
    return lambda: nullcontext(conn)


def failure_reason(exc: BaseException) -> str:
    """例外を「探せなかった」理由の固定語にする（0 件には決してしない）。

    QueryCanceled（SQLSTATE 57014）は OperationalError の子なので先に見る。
    """
    if isinstance(exc, PoolTimeoutError):
        return REASON_POOL_TIMEOUT
    if isinstance(exc, PoolClosedError):
        return REASON_DB_UNAVAILABLE
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return REASON_STATEMENT_TIMEOUT
    if isinstance(exc, psycopg.OperationalError):
        return REASON_DB_UNAVAILABLE
    if isinstance(exc, psycopg.Error):
        return REASON_DB_ERROR
    return REASON_INTERNAL_ERROR


def _escape_like(value: str) -> str:
    r"""LIKE のメタ文字を潰す（``\`` を先に）。

    ``list_client_timeline_recent`` は名前を ``%名前%`` に素通しで埋める
    （pgvector_client.py:1289）。PostgreSQL の LIKE は ESCAPE 指定が無くても ``\`` が
    エスケープ文字なので、ここで潰してから渡せば ``%`` ``_`` を含む名前でも文字どおりに当たる。
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _nfkc(value: str | None) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _names_this_client(value: str | None, names: Sequence[str]) -> bool:
    """行の取引先名が、引いた名前のどれかと **語境界つき** で一致するか（別会社を除く）。

    ``clients_match`` は名寄せ後の部分一致なので、LIKE '%イオン%' に当たった
    「ライオン株式会社」も同じ取引先とみなしてしまう（clients_match('イオン', 'ライオン') は
    True）。ここでは search が名指しの取引先を確定するのと同じ規則（result_guard の
    ``find_client_mention(strict=True)``：カタカナ・英数字の語の途中では切らない）を使う。
    「NGK（日本ガイシ）」「日本ガイシ株式会社」は残り、「ライオン」は落ちる。
    """
    row = _nfkc(value)
    if not row:
        return False
    return find_client_mention(row, [_nfkc(n) for n in names], strict=True) is not None


def _usable_name(name: str) -> bool:
    """語境界つきの照合に使える長さの名前か（2 文字未満・英数字 3 文字未満は使えない）。

    使えない名前で SQL を打っても、上の照合で全行が落ちる（＝無駄な往復）ので打たない。
    判定は照合と同じ関数で行う（名前が自分自身に一致しない＝照合の表層が無い）。
    """
    folded = _nfkc(name)
    return bool(folded) and find_client_mention(folded, [folded], strict=True) is not None


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _log(result: SourceResult[Any], request_id: str | None) -> None:
    # R5: 件数・状態・理由だけ（取引先名・業種・本文は出さない）。
    logger.info(
        "search_fanout_source",
        request_id=request_id,
        source=result.source,
        status=result.status,
        reason=result.reason,
        items=len(result.items),
        searched=len(result.searched),
        excluded=result.excluded,
        incomplete=result.incomplete,
        stage=result.stage,
        elapsed_ms=result.elapsed_ms,
    )


# ── 候補の分類（metadata だけ・LLM なし）──────────────────────────────────────

HitKind = Literal["campaign", "case", "deal", "sales_fb", "vault"]
ExternalUse = Literal["ok", "ng", "unknown"]

#: 節の名前（答えの組み立て側が使う）。
KIND_LABELS: dict[HitKind, str] = {
    "campaign": "施策実績",
    "case": "事例",
    "deal": "案件",
    "sales_fb": "営業FB",
    "vault": "金庫の資料",
}
#: 社外利用の可否の表示（コードの定数だけ。metadata の注記文字列は答えに写さない）。
EXTERNAL_USE_LABELS: dict[ExternalUse, str] = {
    "ng": "社外展開NG（社内のみ）",
    "unknown": "社外利用は要確認",
    "ok": "社外利用可",
}
#: 案件の節に必ず付ける注記（提案中・決定の案件を実績として出さない）。
DEAL_NOTE = "決定・進行中の案件。実績ではありません"

#: 事例の出典（documents.metadata.case_source）。campaign_db は施策実績の側で扱う。
_CASE_SOURCES = frozenset({"deck", "sheet"})
_CAMPAIGN_CASE_SOURCE = "campaign_db"
#: 事例集 PPTX の事例文書だけに付く資料種別（pipeline.py:5051）。
_CASE_DOC_TYPE = "事例"
_DEAL_TOPIC = "案件決定"
#: 業種の metadata（事例 → 施策実績に添えた広告主の業種 → 自動分類）の順に見る。
_INDUSTRY_KEYS = ("case_industry", "campaign_industry", "cls_industry")


def _is_true(value: Any) -> bool:
    """metadata の真偽（検索の射影は True、文書の metadata そのままは 'true'）。"""
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() == "true"


def _text(meta: Mapping[str, Any], key: str) -> str:
    return str(meta.get(key) or "").strip()


def hit_kind(metadata: Mapping[str, Any]) -> HitKind:
    """1 件の候補がどの探し先に属するか（判定の順番が仕様）。

    1. 施策実績（campaign_aggregate='true'・または case_source='campaign_db'）
    2. 事例（case_source が deck / sheet・または資料種別「事例」・または case_corpus='true'）
    3. 案件（topic='案件決定'・または #proj-01 案件決定のチャンネルの投稿）
    4. 営業 FB（is_sales_fb）
    5. それ以外は金庫の資料
    """
    meta = metadata or {}
    source = _text(meta, "case_source")
    if _is_true(meta.get("campaign_aggregate")) or source == _CAMPAIGN_CASE_SOURCE:
        return "campaign"
    if (
        source in _CASE_SOURCES
        or _text(meta, "cls_doc_type") == _CASE_DOC_TYPE
        or _is_true(meta.get("case_corpus"))
    ):
        return "case"
    if _text(meta, "topic") == _DEAL_TOPIC or _DEAL_TOPIC in _text(meta, "channel_name"):
        return "deal"
    if _is_true(meta.get("is_sales_fb")):
        return "sales_fb"
    return "vault"


def external_use_of(metadata: Mapping[str, Any]) -> ExternalUse:
    """社外利用の可否（ok / ng / unknown）。**明示の ok 以外は ok にしない。**

    値が無い（主検索の射影は case_external_use を持たない）・知らない値は unknown（要確認）。
    """
    value = _text(metadata or {}, "case_external_use").lower()
    if value == "ng":
        return "ng"
    if value == "ok":
        return "ok"
    return "unknown"


def metadata_industry(metadata: Mapping[str, Any]) -> str | None:
    """metadata に書かれた業種（正準値）。無ければ None（＝「業種不明」・推測しない）。"""
    meta = metadata or {}
    for key in _INDUSTRY_KEYS:
        value = normalize_industry(_text(meta, key) or None)
        if value:
            return value
    return None


@dataclass(frozen=True)
class ClassifiedHit:
    """分類した 1 件。``external_use`` は事例・施策実績だけ（それ以外は None）。"""

    hit: SearchHit
    kind: HitKind
    industry: str | None
    external_use: ExternalUse | None


@dataclass(frozen=True)
class ClassifiedHits:
    """探し先ごとの候補（各節の中は入力の順＝rerank の順のまま）。"""

    campaign: tuple[ClassifiedHit, ...] = ()
    case: tuple[ClassifiedHit, ...] = ()
    deal: tuple[ClassifiedHit, ...] = ()
    sales_fb: tuple[ClassifiedHit, ...] = ()
    vault: tuple[ClassifiedHit, ...] = ()

    @property
    def has_case_or_campaign(self) -> bool:
        return bool(self.case or self.campaign)

    def of(self, kind: HitKind) -> tuple[ClassifiedHit, ...]:
        return {
            "campaign": self.campaign,
            "case": self.case,
            "deal": self.deal,
            "sales_fb": self.sales_fb,
            "vault": self.vault,
        }[kind]

    def counts(self) -> dict[HitKind, int]:
        return {
            "campaign": len(self.campaign),
            "case": len(self.case),
            "deal": len(self.deal),
            "sales_fb": len(self.sales_fb),
            "vault": len(self.vault),
        }


def classify_hits(hits: Sequence[SearchHit]) -> ClassifiedHits:
    """rerank の候補を探し先ごとに分ける（metadata だけ・順番は保つ・入力は書き換えない）。"""
    buckets: dict[HitKind, list[ClassifiedHit]] = {
        "campaign": [],
        "case": [],
        "deal": [],
        "sales_fb": [],
        "vault": [],
    }
    for hit in hits:
        meta = getattr(hit, "metadata", None) or {}
        kind = hit_kind(meta)
        buckets[kind].append(
            ClassifiedHit(
                hit=hit,
                kind=kind,
                industry=metadata_industry(meta),
                external_use=external_use_of(meta) if kind in ("case", "campaign") else None,
            )
        )
    return ClassifiedHits(
        campaign=tuple(buckets["campaign"]),
        case=tuple(buckets["case"]),
        deal=tuple(buckets["deal"]),
        sales_fb=tuple(buckets["sales_fb"]),
        vault=tuple(buckets["vault"]),
    )


# ── カルテ（営業 FB の最新数件）─────────────────────────────────────────────


def karte_names(query_client: str | None) -> tuple[tuple[str, ...], str | None]:
    """カルテを引く名前（名指しの取引先＋明示の別名・最大 3 本）と、使わない理由。

    - 名指しが無い → ``no_client``。自社名 → ``self_org``（search の _asked_client と同じ）。
    - LLM が付けたままの括弧・敬称・法人格は剥ぐ（「（アース製薬）」「花王様」：
      client_match.normalize_filter_client）。営業 FB の client_name は「日本ガイシ」
      「NGK（日本ガイシ）」のように法人格が無いことが多い。
    - 依頼文の切れ端（「の」「今週の空き時間」）は clientkarte と同じガードで弾く
      （client_name_guard.classify_client_name：clientkarte/skill.py:446-459）。
    - 別名は client_match の静的な対だけ（推移展開しない）。
    """
    raw = (query_client or "").strip()
    if not raw:
        return (), REASON_NO_CLIENT
    primary = normalize_filter_client(raw) or raw
    if is_self_org_name(primary) or is_self_org_name(raw):
        return (), REASON_SELF_ORG
    if classify_client_name(primary).verdict != "ok":
        return (), REASON_NOT_A_CLIENT_NAME
    names: list[str] = []
    seen: set[str] = set()
    for name in [primary, *sorted(aliases(primary))]:
        key = normalize_client(name)
        if not key or key in seen or not _usable_name(name):
            continue
        seen.add(key)
        names.append(name)
        if len(names) >= KARTE_MAX_NAMES:
            break
    if not names:
        return (), REASON_NOT_A_CLIENT_NAME
    return tuple(names), None


def _merge_karte(
    hits: Sequence[SearchHit], names: Sequence[str]
) -> tuple[tuple[SearchHit, ...], int]:
    """別名ごとの結果を 1 つにする（1 FB 1 行・別会社を除く・新しい順・最大 5 件）。

    同じ FB の複数チャンクは同じ行（日付・フェーズ・次アクションは文書単位）になるので、
    文書（source_uri と題名の組）ごとに最初の 1 チャンクだけ残す。source_uri が無い行は
    チャンク単位のまま（畳まない）。
    """
    seen: set[tuple[str, str]] = set()
    kept: list[SearchHit] = []
    excluded = 0
    for hit in hits:
        meta = hit.metadata or {}
        uri = _text(meta, "source_uri")
        key = (uri, _text(meta, "title")) if uri else (f"chunk:{hit.chunk_id}", "")
        if key in seen:
            continue
        seen.add(key)
        if not _names_this_client(meta.get("client_name"), names):
            excluded += 1
            continue
        kept.append(hit)
    kept.sort(key=lambda h: _text(h.metadata or {}, "occurred_at"), reverse=True)
    return tuple(kept[:KARTE_MAX_ITEMS]), excluded


def karte_lookup(
    pgvector: PgVectorClient,
    connect: ConnectionSource,
    *,
    query_client: str | None,
    request_id: str | None = None,
) -> SourceResult[SearchHit]:
    """名指しの取引先の営業 FB（最新数件・新しい順）を引く。例外は投げない。

    ``query_client`` は search が確定させた名指しの取引先（probe['query_client'] 相当：
    skill.py:1292-1302）。無ければ SQL を 1 本も打たず、接続も借りない（R3）。
    返す ``items`` は ``list_client_timeline_recent`` の SearchHit そのまま
    （metadata に occurred_at・deal_phase・next_action・source_uri・client_name）。
    """
    started = time.perf_counter()
    names, skip = karte_names(query_client)
    if skip is not None:
        skipped: SourceResult[SearchHit] = SourceResult(
            source="karte", status="skipped", reason=skip, elapsed_ms=_elapsed_ms(started)
        )
        _log(skipped, request_id)
        return skipped

    collected: list[SearchHit] = []
    searched: list[str] = []
    failure: Exception | None = None
    try:
        with connect() as conn:
            for name in names:
                # R4: 1 本ごとに SAVEPOINT。失敗はここで巻き戻り、借りた接続は使える状態で戻る。
                with conn.transaction():
                    rows = pgvector.list_client_timeline_recent(
                        conn=conn,
                        client_name=_escape_like(name),
                        limit=KARTE_PER_NAME_LIMIT,
                        request_id=request_id,
                        log_client=False,
                    )
                searched.append(name)
                collected.extend(rows)
    except Exception as exc:  # R2: 探せなかったことを状態で返す（検索本体を落とさない）
        failure = exc
        logger.warning(
            "search_fanout_karte_failed", request_id=request_id, error=type(exc).__name__
        )

    items, excluded = _merge_karte(collected, names)
    result: SourceResult[SearchHit]
    if items:
        result = SourceResult(
            source="karte",
            status="ok",
            reason=REASON_OK,
            items=items,
            searched=tuple(searched),
            excluded=excluded,
            incomplete=failure is not None,
            elapsed_ms=_elapsed_ms(started),
        )
    elif failure is not None:
        result = SourceResult(
            source="karte",
            status="failed",
            reason=failure_reason(failure),
            searched=tuple(searched),
            excluded=excluded,
            incomplete=True,
            elapsed_ms=_elapsed_ms(started),
        )
    else:
        result = SourceResult(
            source="karte",
            status="zero",
            reason=REASON_OTHER_CLIENTS_ONLY if excluded else REASON_NO_RECORDS,
            searched=tuple(searched),
            excluded=excluded,
            elapsed_ms=_elapsed_ms(started),
        )
    _log(result, request_id)
    return result


# ── 事例（事例集・施策実績の case_corpus）──────────────────────────────────────


@dataclass(frozen=True)
class CaseRow:
    """事例集から引いた 1 件（list_case_studies の列だけ・本文は持たない）。

    ``in_main`` は主検索のヒットに同じ事例（source_uri と題名が同じ）が既にあるか。
    事例集 PPTX の事例は 1 ファイルを事例ごとに切った文書で、source_uri は全事例で同じ
    （pipeline.py:5010）なので、題名と組にして比べる。
    """

    title: str
    source_uri: str
    client: str
    brand: str
    product: str
    effect: str
    kind: Literal["case", "campaign"]
    source: str
    external_use: ExternalUse
    industry: str | None
    updated_at: str
    stage: int
    in_main: bool


def case_client_name(query_client: str | None) -> str | None:
    """事例集の照合キー（pre_meeting_brief と同じ normalize_company）。使えなければ None。

    事例集の client_name は取り込み時に normalize_company で正規化してある
    （case_deck.py:28-30・campaign_aggregate.py:453）。段 1 の完全一致は同じ関数でしか当たらない。
    normalize_company は括弧の **中身ごと** 捨てる（pre_meeting_brief/classify.py:149-151）ので、
    LLM が付けたままの「（アース製薬）」は先に normalize_filter_client で括弧だけ剥ぐ。
    """
    raw = (query_client or "").strip()
    if not raw or is_self_org_name(raw):
        return None
    unwrapped = normalize_filter_client(raw) or raw
    name = normalize_company(unwrapped) or unwrapped
    if is_self_org_name(name) or classify_client_name(name).verdict != "ok":
        return None
    return name


def _canonical_industry(industry: str | None) -> str | None:
    """段 4 に使える正準業種。「その他」は同業種の事例にならないので使わない。"""
    value = normalize_industry(industry)
    if not value or value == OTHER_INDUSTRY:
        return None
    return value


def _main_keys(hits: Sequence[SearchHit]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for hit in hits:
        meta = getattr(hit, "metadata", None) or {}
        keys.add((_text(meta, "source_uri"), _text(meta, "title")))
    return keys


def _to_case_row(row: Mapping[str, Any], *, stage: int, main: set[tuple[str, str]]) -> CaseRow:
    source = _text(row, "case_source")
    title = _text(row, "title")
    uri = _text(row, "source_uri")
    return CaseRow(
        title=title,
        source_uri=uri,
        client=_text(row, "case_client"),
        brand=_text(row, "case_brand"),
        product=_text(row, "case_product"),
        effect=_text(row, "case_effect"),
        kind="campaign" if source == _CAMPAIGN_CASE_SOURCE else "case",
        source=source,
        external_use=external_use_of(row),
        industry=normalize_industry(_text(row, "case_industry") or None),
        updated_at=_text(row, "updated_at"),
        stage=stage,
        in_main=(uri, title) in main,
    )


def case_lookup_by_industry(
    pgvector: PgVectorClient,
    connect: ConnectionSource,
    *,
    industry: str | None,
    main_hits: Sequence[SearchHit],
    query_client: str | None = None,
    request_id: str | None = None,
    product: str | None = None,
    limit: int = CASE_LIMIT,
) -> SourceResult[CaseRow]:
    """事例集から事例を引く（例外は投げない）。

    段の意味は list_case_studies のとおり:
    1＝client_name か case_brand の完全一致、2＝同じ 2 キーの部分一致、4＝同業種のみ。

    - 名指しの取引先があれば 段 1 → 段 2（段 2 は 3 文字以上だけ・pre_meeting_brief と同じ
      ``is_usable_partial``）。段 2 の部分一致は語境界つきで別会社を除く。
    - 名指しで当たらず、``industry`` が正準業種で、かつ主ヒットに事例・施策実績が無いときだけ
      段 4 を引く（limit 3）。業種は推測しない（正準値でなければ引かない）。
    - 業種不明でも明示された商材があれば段 5 で資料名・商材を照合する。
    - 以上の条件が無ければ SQL を打たず、接続も借りない（R3）。
    """
    started = time.perf_counter()
    client = case_client_name(query_client)
    canonical = _canonical_industry(industry)
    has_cases = classify_hits(main_hits).has_case_or_campaign
    want_industry = canonical is not None and not has_cases
    if client is None and not want_industry and not product:
        # 名指しが無い（使えない）うえに、業種が無い／主ヒットに事例・施策実績がもうある。
        reason = REASON_NO_INDUSTRY if canonical is None else REASON_MAIN_HAS_CASES
        skipped: SourceResult[CaseRow] = SourceResult(
            source="case", status="skipped", reason=reason, elapsed_ms=_elapsed_ms(started)
        )
        _log(skipped, request_id)
        return skipped

    main = _main_keys(main_hits)
    rows: list[dict[str, Any]] = []
    stage = 0
    excluded = 0
    searched: list[str] = []
    failure: Exception | None = None
    try:
        with connect() as conn:
            if client is not None:
                with conn.transaction():
                    found = pgvector.list_case_studies(
                        conn, client_name=client, limit=limit, stage=1, request_id=request_id
                    )
                searched.append(client)
                if found:
                    rows, stage = list(found), 1
                elif is_usable_partial(client):
                    with conn.transaction():
                        found = pgvector.list_case_studies(
                            conn,
                            client_name=client,
                            limit=limit,
                            stage=2,
                            request_id=request_id,
                        )
                    same = [
                        r
                        for r in found
                        if _names_this_client(r.get("case_client"), [client])
                        or _names_this_client(r.get("case_brand"), [client])
                    ]
                    excluded = len(found) - len(same)
                    if same:
                        rows, stage = same, 2
            if not rows and product:
                with conn.transaction():
                    found = pgvector.list_case_studies(
                        conn,
                        product=product,
                        industry=None,
                        limit=5,
                        stage=5,
                        request_id=request_id,
                    )
                searched.append(product)
                if found:
                    rows, stage = list(found), 5
            if not rows and want_industry and canonical is not None:
                with conn.transaction():
                    found = pgvector.list_case_studies(
                        conn, industry=canonical, limit=limit, stage=4, request_id=request_id
                    )
                searched.append(canonical)
                if found:
                    rows, stage = list(found), 4
    except Exception as exc:  # R2
        failure = exc
        logger.warning("search_fanout_case_failed", request_id=request_id, error=type(exc).__name__)

    items = tuple(_to_case_row(r, stage=stage, main=main) for r in rows)
    result: SourceResult[CaseRow]
    if items:
        result = SourceResult(
            source="case",
            status="ok",
            reason=REASON_OK,
            items=items,
            searched=tuple(searched),
            excluded=excluded,
            incomplete=failure is not None,
            stage=stage,
            elapsed_ms=_elapsed_ms(started),
        )
    elif failure is not None:
        result = SourceResult(
            source="case",
            status="failed",
            reason=failure_reason(failure),
            searched=tuple(searched),
            excluded=excluded,
            incomplete=True,
            elapsed_ms=_elapsed_ms(started),
        )
    else:
        result = SourceResult(
            source="case",
            status="zero",
            reason=REASON_OTHER_CLIENTS_ONLY if excluded else REASON_NO_RECORDS,
            searched=tuple(searched),
            excluded=excluded,
            elapsed_ms=_elapsed_ms(started),
        )
    _log(result, request_id)
    return result


__all__ = [
    "CASE_LIMIT",
    "DEAL_NOTE",
    "EXTERNAL_USE_LABELS",
    "KARTE_MAX_ITEMS",
    "KARTE_MAX_NAMES",
    "KARTE_PER_NAME_LIMIT",
    "KIND_LABELS",
    "REASON_DB_ERROR",
    "REASON_DB_UNAVAILABLE",
    "REASON_INTERNAL_ERROR",
    "REASON_MAIN_HAS_CASES",
    "REASON_NOT_A_CLIENT_NAME",
    "REASON_NO_CLIENT",
    "REASON_NO_INDUSTRY",
    "REASON_NO_RECORDS",
    "REASON_OK",
    "REASON_OTHER_CLIENTS_ONLY",
    "REASON_POOL_TIMEOUT",
    "REASON_SELF_ORG",
    "REASON_STATEMENT_TIMEOUT",
    "CaseRow",
    "ClassifiedHit",
    "ClassifiedHits",
    "ConnectionSource",
    "ExternalUse",
    "HitKind",
    "SourceResult",
    "SourceStatus",
    "case_client_name",
    "case_lookup_by_industry",
    "classify_hits",
    "external_use_of",
    "failure_reason",
    "hit_kind",
    "karte_lookup",
    "karte_names",
    "metadata_industry",
    "reuse_connection",
    "rls_connection",
]
