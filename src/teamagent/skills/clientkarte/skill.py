"""ClientKarte Skill 本体。

あるクライアントの営業 FB を時系列で束ね、提案履歴・温度感推移・推奨ネクスト
アクションを 1 枚のカルテに合成する。営業の「あの会社、今どうなってる？」に即答する。

3 層分離: 本ファイルは Skill 層。psycopg / boto3 は触らず adapters/ 経由。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import threading
import time
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.adapters.pgvector_client import PgVectorClient, SearchHit
from teamagent.prompts.loader import load_prompt
from teamagent.skills._shared.client_name_guard import classify_client_name
from teamagent.skills._shared.drive_slack_delivery import (
    deliver_files,
    prepare_drive_files,
    safe_filename,
)
from teamagent.skills._shared.source_url import source_link
from teamagent.skills.base import BaseSkill, SkillContext, is_orchestrated_call, register
from teamagent.skills.clientkarte.documents import (
    EMPTY_SECTION,
    DocumentsSection,
    KarteDoc,
    attachment_only_notice,
    availability_notice,
    belongs_to_client,
    build_documents_section,
    channel_notice,
    dm_forward_text,
    is_dm_surface,
    slack_label,
    to_docs,
)
from teamagent.skills.clientkarte.schema import (
    ClientKarteInput,
    ClientKarteOutput,
    KarteEvent,
)

logger = structlog.get_logger(__name__)

_DEFAULT_EVENT_SUMMARY_MAX_CHARS = 160
_DEFAULT_SYNTHESIS_BODY_MAX_CHARS = 200
_TRUNCATION_MARKER = "…"
_SENTENCE_BOUNDARY_RE = re.compile(r"(?:[。！？!?]+[」』）】”’\"']*|[.]+(?=\s|$)|\n+)")
# A boundary cut this far below the cap loses more than it saves, so fall back to
# the character cut. Sales feedback arriving as "列名: 値" lines makes every
# newline a boundary, and the last one inside the cap can sit right before the
# free-text body (measured: a 160-char excerpt collapsing to 58).
_MIN_BOUNDARY_YIELD_RATIO = 0.6


def _env_positive_int(name: str, default: int) -> int:
    """正の整数 env を読む。不正値は安全な既定値へ戻す。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_attach_max(name: str, default: int) -> int:
    """添付件数上限 env を読む。**0 は「添付しない」の明示指定**として尊重する。

    ``_env_positive_int`` を使うと ``KARTE_ATTACH_DOCS_MAX=0``（件数で止めるつもりの操作）が
    黙って既定 3 件へ戻り、止めたはずが 3 件届く（2026-08-19 レビュー H3・実測）。
    負値・非数値だけを「不正」として既定へ戻し、そのとき警告を残す
    （``_env_flag`` の未知値と同じ流儀）。
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("clientkarte_env_int_invalid", flag=name, fell_back_to=default)
        return default
    if value < 0:
        logger.warning("clientkarte_env_int_invalid", flag=name, fell_back_to=default)
        return default
    return value


_SOURCE_LINKS_MAX = 3

# 関連資料の同梱（ユーザー要求 2026-08-19「カルテを出した時点で資料も一緒に出す」）。
#
# このフラグは **関連資料機能まるごと**の kill switch（2026-08-19 レビュー H1 裁定）。
# 実ファイルだけを止めて資料名と Drive リンクの一覧を出し続けると、カルテの answer は
# 「聞かれたチャンネル」へ出る（runtime/slack_bot.py の in_channel 応答）ため、
# **バイトだけ守って題名とリンクを守らない**状態になる。OFF にしたら本機能追加前と
# 同じ出力（カルテ本文＋出典 URL だけ）に戻る、を不変条件にする。
#
# 既定 ON（要求そのもの: 「カルテを見て資料をクリックする」工数をなくすのが目的なので、
# 既定 OFF では機能が 1 度も動かない）。安全側の担保は「止められること」で取る:
#   - 実ファイルの配信先は依頼者本人の DM 固定
#     （聞かれたチャンネルには絶対に出さない・_attach_documents）
#   - 資料名と Drive リンクも同じく本人 DM 固定。チャンネル / スラッシュで呼ばれた場合、
#     本文へ出るのは資料名を含まない 1 行の通知だけ（2026-08-20 裁定 A・
#     _deliver_documents）。run_agent 経路も同じ（ダークフラグで裁定 A が破れない）
#   - 顧客名が依頼文の断片なら資料経路へ入らない（client_name_guard）。行の client_name が
#     要求顧客と矛盾する資料は一覧にも添付にも出さない（belongs_to_client）
#   - kill switch は infra/terraform/fargate.tf に結線済み（var.karte_attach_docs）
#   - 同じ資料を短時間に重ね送りしない（_ATTACH_DEDUP_TTL_FLAG）
#
# ⚠️ 運用の実際（terraform_runtime_guard.sh との整合・2026-08-19 実測）:
#   mcp task definition の env は validate_plan（mode=sync＝通常 apply）が live snapshot と
#   **完全一致**を要求し、env の追加・変更・削除を die する。よってこの変数を足す apply も
#   true→false へ倒す apply も **素の apply では通らない**。env を動かすには
#   mode=migration / kind=runtime の manifest allowlist 経路が要る。
#   「terraform を触れば署名リリース無しで即止まる」は誤り。止める手順は migration 経路。
_ATTACH_FLAG = "KARTE_ATTACH_DOCS"
_ATTACH_DEFAULT_ON = True
_ATTACH_MAX_FLAG = "KARTE_ATTACH_DOCS_MAX"
_DEFAULT_ATTACH_MAX = 3
# 添付上限のハードキャップ。env でいくら大きくしても超えない（knowledge_deliver の
# 入力スキーマ le=5 と揃える）。LIST_MAX と同値なので「添付したのに一覧に無い」も起きない。
_ATTACH_MAX_CAP = 5
# 1 ファイルあたりの取得サイズ上限（既定 50MiB）。gdrive_client は最大 256MB を
# メモリに載せるため、常時通る経路に無制限の DL を許さない。
_ATTACH_MAX_BYTES_FLAG = "KARTE_ATTACH_DOCS_MAX_BYTES"
_DEFAULT_ATTACH_MAX_BYTES = 50 * 1024 * 1024
# list_documents_for_client に投げる取得上限（既定と同じ 50 件）。
_DOCS_FETCH_LIMIT = 50
# 添付に失敗しても本文は必ず返す（fail-open）。本文に足すのはこの 1 行だけ。
#
# 案内する導線は **その実行で実在する導線だけ**にする。添付候補は
#   - ``gdrive://ID`` 行  → 一覧に URL が出ない（source_link が None）
#   - ``drive.google.com/file/d/ID`` 行 → 一覧に装飾リンクが出る
# の 2 形があり、前者しか無いのに「上のリンクから」と書くと存在しない導線を案内してしまう
# （旧コメントは「候補は実質 gdrive://ID 行だけ」と書いていたが、これは事実と違った）。
_ATTACH_FAILED_LINE = (
    "（関連資料の実ファイルは添付できませんでした。"
    "資料名を指定して「〇〇の資料を出して」とご依頼ください）"
)
_ATTACH_FAILED_LINE_WITH_LINK = (
    "（関連資料の実ファイルは添付できませんでした。"
    "上の一覧のリンクから開くか、資料名を指定して「〇〇の資料を出して」とご依頼ください）"
)
# 同じ資料の重ね送り防止（2026-08-19 レビュー H4）。
# 本番の呼び出し元は OpenClaw → mcp_gateway の dispatch で、そこには「中間ステップ」の
# 印が立たない（印を立てるのは run_agent＝USE_AGENT_ORCHESTRATOR で dark な経路だけ）。
# OpenClaw は 1 ターンで複数ツールを回すし、mcp 側に skill タイムアウトが無いので
# OpenClaw 側タイムアウト → 再試行で同じ資料が 2 度 DM に届く（実測: uploads 6 件）。
# プロセス内の TTL 台帳で「直近に同じ人へ送った file_id」を弾く。0 で無効。
_ATTACH_DEDUP_TTL_FLAG = "KARTE_ATTACH_DOCS_DEDUP_TTL_S"
_DEFAULT_ATTACH_DEDUP_TTL_S = 600
# 台帳の上限件数（LRU 相当で古い順に捨てる）。無制限に積んでメモリを食わない。
_DEDUP_LEDGER_MAX = 4096
_ATTACH_DEDUP_LINE = (
    "（関連資料は先ほどお送りした分と同じでしたので、重複してはお送りしていません）"
)

_dedup_lock = threading.Lock()
# (配信先 email, Drive file_id) -> 失効する monotonic 時刻
_dedup_ledger: dict[tuple[str, str], float] = {}


def _dedup_prune(now: float) -> None:
    """失効分を捨てる。溢れたら古い順に捨てる（呼び出し側で lock 済みの前提）。"""
    for key in [k for k, deadline in _dedup_ledger.items() if deadline <= now]:
        del _dedup_ledger[key]
    while len(_dedup_ledger) > _DEDUP_LEDGER_MAX:
        _dedup_ledger.pop(next(iter(_dedup_ledger)))


def _dedup_recent(email: str, file_ids: list[str], ttl_s: int) -> set[str]:
    """直近 ``ttl_s`` 秒に同じ相手へ送り終えている file_id を返す。"""
    if ttl_s <= 0:
        return set()
    now = time.monotonic()
    with _dedup_lock:
        _dedup_prune(now)
        return {fid for fid in file_ids if _dedup_ledger.get((email, fid), 0.0) > now}


def _dedup_remember(email: str, file_ids: set[str], ttl_s: int) -> None:
    """**送れた** file_id だけを台帳に載せる（失敗を覚えて再送を止めない）。"""
    if ttl_s <= 0 or not file_ids:
        return
    now = time.monotonic()
    with _dedup_lock:
        _dedup_prune(now)
        for fid in file_ids:
            _dedup_ledger[(email, fid)] = now + ttl_s


def reset_attach_dedup_ledger() -> None:
    """テスト用。プロセス内台帳を空にする（本番コードからは呼ばない）。"""
    with _dedup_lock:
        _dedup_ledger.clear()


# 資料セクション本文の重ね送り防止に使う台帳キーの接頭辞。Drive の file_id は
# ``[A-Za-z0-9_-]`` しか含まないので、":" 入りのこの形と衝突しない。
_SECTION_DEDUP_PREFIX = "karte-docs-section:"


def _section_dedup_key(*, client_name: str, section_text: str) -> str:
    """(顧客名 + セクション本文) から台帳キーを作る。長い本文を鍵にしないためのハッシュ。"""
    digest = hashlib.sha256(f"{client_name}\n{section_text}".encode()).hexdigest()
    return f"{_SECTION_DEDUP_PREFIX}{digest[:32]}"


def _opt_meta(value: Any) -> str | None:
    """SkillContext.metadata の配信先ヒントを str|None に正規化する。

    **前後の空白は落とす**（knowledge_deliver が ``requester.strip()`` しているのと揃える）。
    本番は build_rls_metadata → normalize_email を通るので現状は空白入りが来ないが、
    DI / テスト経路で ``"  u@v.co.jp  "`` のまま lookup_user_id_by_email に渡すと
    Slack 側でヒットせず添付だけが黙って落ちる。
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _env_flag(name: str, *, default: bool) -> bool:
    """真偽 env を読む。未設定・空・**未知値**は default へ戻す。

    ``_env_positive_int`` と同じ流儀に揃える（不正値は黙って落とさず既定へ戻して警告）。
    ``KARTE_ATTACH_DOCS=enabled`` のようなタイポで機能が黙って反転しないようにする。
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    logger.warning("clientkarte_env_flag_invalid", flag=name, fell_back_to=default)
    return default


def _with_source_links(answer: str, events: list[KarteEvent]) -> str:
    """イベントで解決済みの出典 URL を回答末尾へ決定論で追記する（重複除去・最大3件）。"""
    links: list[str] = []
    for e in events:
        if e.url and e.url not in links:
            links.append(e.url)
    if not links:
        return answer
    lines = "\n".join(f"🔗 出典: {u}" for u in links[:_SOURCE_LINKS_MAX])
    return f"{answer}\n\n{lines}"


def _truncate_at_sentence_boundary(text: str, max_chars: int) -> str:
    """上限内の最後の文境界で切り、後続があることを ``…`` で示す。

    単一文が上限を超える場合だけは、上限を守るため文字境界へフォールバックする。
    """
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    if max_chars <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER[:max_chars]

    # 上限ちょうどの文末も完全な一文として残す。文末が marker 分の余白を使い切る場合は、
    # 文中切断して marker を付けるより、marker なしで文境界を優先する。
    capped = cleaned[:max_chars]
    matches = list(_SENTENCE_BOUNDARY_RE.finditer(capped))
    if matches:
        last = matches[-1]
        clipped = capped[: last.end()].rstrip()
        # A real sentence end is always a legitimate stop, however early it is.
        # A line break is not: sales feedback arriving as "列名: 値" lines makes
        # every newline a boundary, and the last one inside the cap can sit right
        # before the free-text body and drop it (measured: 160 chars to 58).
        line_break_only = "\n" in last.group()
        yields_enough = len(clipped) >= int(max_chars * _MIN_BOUNDARY_YIELD_RATIO)
        if not line_break_only or yields_enough:
            if len(clipped) + len(_TRUNCATION_MARKER) <= max_chars:
                return f"{clipped}{_TRUNCATION_MARKER}"
            return clipped

    budget = max_chars - len(_TRUNCATION_MARKER)
    return f"{cleaned[:budget].rstrip()}{_TRUNCATION_MARKER}"


def _attach_note(*, delivered: set[str], requested: list[str], title_by_fid: dict[str, str]) -> str:
    """添付結果の注記。**送った資料名を明示**し、届かなかった分も正直に述べる。

    件数だけの「うち N 件」は、一覧に出ていない資料を指して読めてしまう
    （一覧は 5 行で打ち切るため）。名前を書けばどれが届いたか曖昧さが残らない。
    部分失敗（3 件中 1 件だけ届いた等）を黙らせないため、失敗件数も併記する。
    """
    names = [slack_label(title_by_fid[fid]) for fid in requested if fid in delivered]
    # 「あなたの DM に」とは書かない。この注記は **本人 DM の中**にも出る
    # （DM でカルテを呼んだ場合・チャンネル呼び出しの転送文面の場合の両方）ので、
    # DM の中で「あなたの DM にお送りしました」と言うことになる（2026-08-20 指摘6）。
    note = f"（このうち {'、'.join(names)} の {len(names)} 件を実ファイルでお送りしました）"
    failed = len(requested) - len(names)
    if failed > 0:
        note += f"\n（残り {failed} 件は実ファイルを取得できずお送りできませんでした）"
    return note


async def _open_dm_channel(slack: Any, *, email: str, request_id: str) -> str | None:
    """依頼者本人の DM チャンネル ID を解決する（users.lookupByEmail → conversations.open）。"""
    user_id = await slack.lookup_user_id_by_email(email, request_id)
    if not user_id:
        return None
    dm: str | None = await slack.open_dm(user_id, request_id)
    return dm or None


async def _post_dm(slack: Any, *, channel: str, text: str, request_id: str) -> bool:
    """解決済みの DM チャンネルへテキストを 1 通投稿する。届いたときだけ True。

    ``SlackClient.post_message`` は API エラーで例外を投げ、業務的な失敗は
    ``SlackPostResult.ok`` で返す。**両方**を見ないと「送れていないのに送った」と
    答えてしまう（チャンネルへ「DM でお送りしました」と嘘をつく）。
    例外の握り潰しは呼び出し側（``_forward_section_to_dm``）の責務。
    """
    result = await slack.post_message(channel, text, request_id)
    return bool(getattr(result, "ok", False))


class _DmTarget:
    """1 リクエストにつき本人 DM を **1 度だけ**解決して使い回す。

    実ファイル添付（``deliver_files``）と一覧の DM 転送（``_post_dm``）は同じ DM へ
    書くのに、以前はそれぞれ独立に ``users.lookupByEmail`` + ``conversations.open`` を
    叩いていた（1 リクエストで各 2 回・実測）。2 回目が rate limit / 一時失敗に当たると
    「ファイルだけ黙って届いて、説明が 1 文字も出ない」に直行する
    （2026-08-20 レビュー 要修正3(a)）。``_slack_client()`` でインスタンスは 1 つに
    束ねてあったが、API 往復は減っていなかった。

    解決は遅延（``channel()`` の初回呼び出し時）。``KARTE_ATTACH_DOCS_MAX=0`` かつ
    DM 面のように「DM を 1 度も使わない」実行で Slack を叩かないため。
    """

    def __init__(self, slack_factory: Any, *, email: str | None, request_id: str, log: Any) -> None:
        self._slack_factory = slack_factory
        self.email = email
        self._request_id = request_id
        self._log = log
        self._resolved = False
        self._channel: str | None = None

    def channel(self) -> str | None:
        """本人 DM の channel_id。取れなければ None（例外は外へ出さない）。"""
        if self._resolved:
            return self._channel
        self._resolved = True
        if not self.email:
            return None
        try:
            self._channel = asyncio.run(
                _open_dm_channel(
                    self._slack_factory(), email=self.email, request_id=self._request_id
                )
            )
        except Exception as e:
            # 型名だけ残す（本文は Slack のエラーメッセージを含み得る）。
            self._log.warning("clientkarte_dm_open_failed", error=type(e).__name__)
            self._channel = None
        return self._channel


@register
class ClientKarteSkill(BaseSkill[ClientKarteInput, ClientKarteOutput]):
    """クライアント単位の時系列カルテを合成する Skill。"""

    name: ClassVar[str] = "clientkarte"
    description: ClassVar[str] = "指定クライアントの提案履歴・温度感・次アクションを時系列で束ねる"
    input_schema: ClassVar[type[BaseModel]] = ClientKarteInput
    output_schema: ClassVar[type[BaseModel]] = ClientKarteOutput

    def __init__(
        self,
        bedrock: BedrockClient | None = None,
        pgvector: PgVectorClient | None = None,
        *,
        prompt_version: str = "v1",
        summary_max_tokens: int = 900,
        event_summary_max_chars: int | None = None,
        synthesis_body_max_chars: int | None = None,
        app_role: str | None = "teamagent_app",
        slack: Any = None,
        gdrive: Any = None,
    ) -> None:
        self._bedrock = bedrock or BedrockClient.from_env()
        self._pgvector = pgvector or PgVectorClient.from_env()
        # 関連資料の実ファイル添付用。未注入なら添付が必要になった時だけ遅延生成する
        # （カルテ本体は Slack/Drive に依存しないので、起動時には作らない）。
        self._slack = slack
        self._gdrive = gdrive
        self._prompt_version = prompt_version
        self._summary_max_tokens = summary_max_tokens
        # 数値の既定値は従来の 160/200 字を維持し、運用で品質比較できるよう env 化する。
        self._event_summary_max_chars = max(
            1,
            event_summary_max_chars
            if event_summary_max_chars is not None
            else _env_positive_int(
                "CLIENTKARTE_EVENT_SUMMARY_MAX_CHARS",
                _DEFAULT_EVENT_SUMMARY_MAX_CHARS,
            ),
        )
        self._synthesis_body_max_chars = max(
            1,
            synthesis_body_max_chars
            if synthesis_body_max_chars is not None
            else _env_positive_int(
                "CLIENTKARTE_SYNTHESIS_BODY_MAX_CHARS",
                _DEFAULT_SYNTHESIS_BODY_MAX_CHARS,
            ),
        )
        self._app_role = app_role

    def run(self, input: ClientKarteInput, ctx: SkillContext) -> ClientKarteOutput:
        log = ctx.bind_logger(self.name)
        log.info("clientkarte_start", client_name=input.client_name, limit=input.limit)

        user_email = ctx.metadata.get("user_email")
        user_groups_raw = ctx.metadata.get("user_groups")
        user_groups = list(user_groups_raw) if isinstance(user_groups_raw, (list, tuple)) else None
        user_role = ctx.metadata.get("user_role")

        # kill switch は「関連資料機能まるごと」に効く（H1 裁定）。OFF なら資料を
        # **引かない**（リンク一覧も出ない・DB も叩かない）＝本機能追加前の出力に戻る。
        docs_enabled = _env_flag(_ATTACH_FLAG, default=_ATTACH_DEFAULT_ON)
        if docs_enabled:
            # 無差別走査の禁止（mail_summary / mail_followup の G5 と同じガード）。
            # ``client_name`` は OpenClaw が自然文から詰める引数で、依頼文の断片
            # （「の」「今週の空き時間」等）がそのまま入る事故が実測されている。
            # clientkarte は mail 系（読むだけ）より副作用が重い（Drive DL +
            # Slack file upload）のに、ここだけガードを通していなかった:
            # ``client_name="の"`` で 8 件を掴み、新しい順 3 件を **実ファイルで DM 配信**
            # していた（2026-08-20 レビュー 要修正3・実測）。
            # 止めるのは資料セクションだけ。カルテ本文は従来どおり返す（fail-open）。
            verdict = classify_client_name(input.client_name)
            if verdict.verdict != "ok":
                log.info("clientkarte_documents_skipped", reason=verdict.reason)
                docs_enabled = False

        with self._pgvector.connection(
            app_role=self._app_role,
            user_email=user_email,
            user_groups=user_groups,
            user_role=user_role,
        ) as conn:
            hits = self._pgvector.list_client_timeline_recent(
                conn=conn,
                client_name=input.client_name,
                limit=input.limit,
                request_id=ctx.request_id,
            )
            # 関連資料は「あれば足す」付随物。ここで落ちてもカルテ本体は返す（fail-open）。
            docs = (
                self._list_documents(conn, input.client_name, ctx.request_id, log)
                if docs_enabled
                else []
            )

        events = [self._to_event(h) for h in hits]
        answer, cost_usd = self._synthesize(input.client_name, hits, ctx.request_id)
        # 出典 URL は LLM に書かせず、イベントに解決済みの URL をサーバ側で末尾へ付ける
        # （slack_summary A10 と同じ流儀）。重複を除き最大 3 件まで。
        answer = _with_source_links(answer, events)

        # 関連資料セクション（決定論・LLM に書かせない）を出典リンクの後ろへ足す。
        section = (
            build_documents_section(
                client_name=input.client_name,
                project_name=input.project_name,
                docs=docs,
            )
            if docs_enabled
            else EMPTY_SECTION
        )
        attached_count = 0
        if section.kind != "none":
            answer, attached_count = self._deliver_documents(
                answer=answer,
                section=section,
                client_name=input.client_name,
                ctx=ctx,
                log=log,
            )

        log.info(
            "clientkarte_done",
            event_count=len(events),
            doc_count=len(docs),
            doc_section=section.kind,
            attached=attached_count,
            cost_usd=cost_usd,
        )
        return ClientKarteOutput(
            client_name=input.client_name,
            answer=answer,
            events=events,
            event_count=len(events),
            document_count=len(docs),
            attached_count=attached_count,
            total_cost_usd=cost_usd,
        )

    def _list_documents(
        self, conn: Any, client_name: str, request_id: str, log: Any
    ) -> list[KarteDoc]:
        """その顧客の資料を既存 API で引く。失敗しても [] を返しカルテは止めない。

        ``list_documents_for_client`` の WHERE は部分一致なので他社案件の行が混ざる。
        ここで落としておく（``build_documents_section`` 側でも同じフィルタを掛けるが、
        こちらを通しておかないと ``document_count`` だけが「見せていない他社の資料」を
        数え、L2 / OpenClaw が「1 件あります」と語れてしまう）。
        """
        try:
            rows = self._pgvector.list_documents_for_client(
                conn,
                client_name,
                limit=_DOCS_FETCH_LIMIT,
                request_id=request_id,
            )
        except Exception:
            log.warning("clientkarte_documents_lookup_failed")
            return []
        docs = to_docs(rows)
        kept = [d for d in docs if belongs_to_client(d, client_name)]
        if len(kept) != len(docs):
            log.info("clientkarte_documents_filtered", dropped=len(docs) - len(kept))
        return kept

    def _deliver_documents(
        self,
        *,
        answer: str,
        section: DocumentsSection,
        client_name: str,
        ctx: SkillContext,
        log: Any,
    ) -> tuple[str, int]:
        """**面を先に決めてから**資料を配る（2026-08-20 裁定 A ＋ 同日レビュー 指摘5）。

        旧実装は実ファイルの upload を面判定より前に撃っていたため、DM 転送に失敗した
        実行で「ファイルだけ黙って DM に湧き、チャンネルには何も出ない」状態になった。
        面の分岐をこの 1 か所に集め、副作用を出す前に「どこへ出すか」を確定させる。

        分岐は 3 つ:

        - **L2 オーケストレーター（run_agent）の中間ステップ** → 副作用ゼロ。
          DM 面なら材料としてセクション本文を返し、そうでなければ件数だけの 1 行に
          落とす。L2 の最終回答はチャンネルへ出るので、ここを素通しにすると
          ``USE_AGENT_ORCHESTRATOR`` を ON にした瞬間に裁定 A が破れる（指摘4）。
        - **DM で呼ばれた** → その場（answer）にセクションを足す。転送はしない。
        - **チャンネル / スラッシュ** → セクションは本人 DM へ送り、answer には資料名を
          1 件も含まない 1 行の通知だけを足す。DM 転送に失敗しても実ファイルが届いて
          いれば件数だけの 1 行は出す（機能が黙って半分死ぬのを可視化する・要修正3(b)）。
          どちらも成立しなければ資料の情報を 1 文字も足さない（安全側）。

        どの分岐でもカルテ本文はそのまま返る（fail-open。裁定 A の「本文は必ず返す」）。
        """
        dm_surface = is_dm_surface(ctx.metadata.get("channel_id"))
        if is_orchestrated_call(ctx.metadata):
            # run_agent の「まず clientkarte で把握」中間ステップ。最終回答より前に
            # 資料を投下しない（DM も撃たない＝副作用ゼロ）。
            log.info("clientkarte_docs_orchestrated", dm_surface=dm_surface)
            if dm_surface:
                return f"{answer}\n\n{section.text}", 0
            notice = availability_notice(section)
            return (f"{answer}\n\n{notice}" if notice else answer), 0

        dm = _DmTarget(
            self._slack_client,
            email=_opt_meta(ctx.metadata.get("user_email")),
            request_id=ctx.request_id,
            log=log,
        )
        attached, attach_note = self._attach_documents(section, client_name, ctx, log, dm=dm)

        if dm_surface:
            body = f"{answer}\n\n{section.text}"
            return (f"{body}\n{attach_note}" if attach_note else body), attached

        forwarded = self._forward_section_to_dm(
            section=section,
            attach_note=attach_note,
            client_name=client_name,
            ctx=ctx,
            log=log,
            dm=dm,
        )
        if forwarded:
            return f"{answer}\n\n{channel_notice(section, delivered=attached)}", attached
        # 一覧は届かなかった。実ファイルが届いているなら、その件数だけは正直に出す
        # （資料名・URL は 1 文字も出さない）。何も届いていなければ黙る。
        notice = attachment_only_notice(attached)
        return (f"{answer}\n\n{notice}" if notice else answer), attached

    def _forward_section_to_dm(
        self,
        *,
        section: DocumentsSection,
        attach_note: str,
        client_name: str,
        ctx: SkillContext,
        log: Any,
        dm: _DmTarget,
    ) -> bool:
        """資料セクションの本文を依頼者本人の DM へ投稿する。成功したときだけ True。

        例外は外へ出さない（カルテ本文の返却を DM の失敗で巻き込まない）。

        実ファイル添付と同じ再試行ガードを通す（2026-08-19 レビュー H4 と同じ理由）。
        これはチャンネル経路では **毎回撃つ Slack 書き込み**で、本番の呼び出し元
        （OpenClaw → mcp_gateway dispatch）には中間ステップの印が立たない。OpenClaw 側の
        タイムアウト → 再試行で同じ一覧が 2 通 DM に積まれる。台帳キーは
        ``(宛先, 顧客名 + セクション本文)`` で、**添付注記は鍵に含めない**:
        再試行では注記だけが「重複してはお送りしていません」に変わるので、
        注記込みで鍵を作ると内容が同じでも別物と判定されて 2 通目が出てしまう。
        """
        email = dm.email
        if not email:
            # 本人が特定できない＝安全に出せる面が無い。「失敗」ではないので警告にしない。
            log.info("clientkarte_docs_dm_skipped", reason="no_requester_email")
            return False
        dedup_ttl_s = _env_attach_max(_ATTACH_DEDUP_TTL_FLAG, _DEFAULT_ATTACH_DEDUP_TTL_S)
        dedup_key = _section_dedup_key(client_name=client_name, section_text=section.text)
        if _dedup_recent(email, [dedup_key], dedup_ttl_s):
            # 直前に同じ一覧を本人 DM へ届けている。もう本人の手元にあるので、
            # 2 通目は出さずに「送った」と答えてよい（チャンネルの通知は嘘にならない）。
            log.info("clientkarte_docs_dm_deduped")
            return True
        text = dm_forward_text(
            client_name=client_name, section_text=section.text, attach_note=attach_note
        )
        channel = dm.channel()
        if not channel:
            # 実ファイル添付と同じ 1 回の解決を共有する（2 度目の lookup を撃たない）。
            log.warning("clientkarte_docs_dm_failed", reason="no_dm_channel")
            return False
        try:
            ok = asyncio.run(
                _post_dm(
                    self._slack_client(), channel=channel, text=text, request_id=ctx.request_id
                )
            )
        except Exception as e:
            log.warning("clientkarte_docs_dm_failed", reason="exception", error=type(e).__name__)
            return False
        if not ok:
            log.warning("clientkarte_docs_dm_failed", reason="post")
            return False
        _dedup_remember(email, {dedup_key}, dedup_ttl_s)
        return True

    def _attach_documents(
        self,
        section: DocumentsSection,
        client_name: str,
        ctx: SkillContext,
        log: Any,
        *,
        dm: _DmTarget | None = None,
    ) -> tuple[int, str]:
        """添付候補（``section.attachable``）の上位 K 件を **依頼者本人の DM** へ添付する。

        返り値 ``(添付件数, 本文に足す注記)``。どこで失敗しても例外は外へ出さない（fail-open）。

        配信先を DM 固定にしている理由（2026-08-19 レビュー裁定）:
          カルテは「〇〇どうなってる？」の常時経路で、ユーザーは「ファイルを出して」と
          言っていない。``channel_id`` を採ると社外共有チャンネルを含む「聞かれた場所」へ
          社内資料が出てしまう。明示依頼のツール（knowledge_deliver）と違い、ここは
          本人の DM だけに閉じる。
        """
        if not section.attachable:
            return 0, ""
        if is_orchestrated_call(ctx.metadata):
            # run_agent の「まず clientkarte で把握」中間ステップ。最終回答より前に
            # 資料を投下しない。
            # ⚠️ この印が立つのは orchestrator/sdk_runner 経由だけで、その run_agent は
            # ``USE_AGENT_ORCHESTRATOR`` 既定 false（terraform にも 1 行も無い）＝本番 OFF。
            # 本番で実際に clientkarte を呼ぶ OpenClaw → mcp_gateway dispatch には
            # 印が立たないので、「最終回答より前に投下しない／多重投下しない」の実効的な
            # 担保はこの分岐ではなく下の重複防止（_dedup_*）が持つ。
            log.info("clientkarte_attach_skipped", reason="orchestrated_call")
            return 0, ""
        if not _env_flag(_ATTACH_FLAG, default=_ATTACH_DEFAULT_ON):
            return 0, ""

        max_files = min(
            _env_attach_max(_ATTACH_MAX_FLAG, _DEFAULT_ATTACH_MAX),
            _ATTACH_MAX_CAP,
        )
        if max_files <= 0:
            # ``KARTE_ATTACH_DOCS_MAX=0``＝件数で明示的に止める操作。「失敗」ではないので
            # 注記も出さない（リンク一覧だけが残る）。
            log.info("clientkarte_attach_skipped", reason="max_files_zero")
            return 0, ""
        max_bytes = _env_positive_int(_ATTACH_MAX_BYTES_FLAG, _DEFAULT_ATTACH_MAX_BYTES)
        dedup_ttl_s = _env_attach_max(_ATTACH_DEDUP_TTL_FLAG, _DEFAULT_ATTACH_DEDUP_TTL_S)
        candidates: list[tuple[str, str]] = []
        title_by_fid: dict[str, str] = {}
        linkable_fids: set[str] = set()
        for doc in section.attachable:
            file_id = doc.attach_file_id
            if not file_id or file_id in title_by_fid:
                continue
            title_by_fid[file_id] = doc.title
            if doc.url:
                linkable_fids.add(file_id)
            candidates.append((file_id, safe_filename(doc.title)))
            if len(candidates) >= max_files:
                break
        if not candidates:
            return 0, ""

        # 配信先ヒント（identity ではない＝RLS/認可には一切使わない）。DM 固定なので
        # channel_id / thread_ts は**読まない**（チャンネル投下の経路を残さない）。
        # ``dm`` は 1 リクエスト内で共有される DM 解決結果（一覧転送と往復を共有する）。
        if dm is None:
            dm = _DmTarget(
                self._slack_client,
                email=_opt_meta(ctx.metadata.get("user_email")),
                request_id=ctx.request_id,
                log=log,
            )
        email = dm.email
        if not email:
            # 配信先が分からないので「失敗」ではない。リンク一覧だけで返す。
            return 0, ""

        # 直近に同じ相手へ送り終えた資料は送り直さない（OpenClaw の再試行・多段ツール対策）。
        already = _dedup_recent(email, [fid for fid, _ in candidates], dedup_ttl_s)
        if already:
            log.info("clientkarte_attach_deduped", skipped=len(already))
            candidates = [(fid, name) for fid, name in candidates if fid not in already]
        if not candidates:
            return 0, _ATTACH_DEDUP_LINE

        failed_line = (
            _ATTACH_FAILED_LINE_WITH_LINK
            if any(fid in linkable_fids for fid, _ in candidates)
            else _ATTACH_FAILED_LINE
        )

        # DM を先に解決する。開けないなら Drive から 1 バイトも落とさない
        # （旧実装は DL してから deliver_files の中で lookup が落ちていた）。
        dm_channel = dm.channel()
        if not dm_channel:
            log.warning("clientkarte_attach_failed", reason="no_dm_channel")
            return 0, failed_line

        tmpdir: str | None = None
        try:
            gdrive = self._gdrive or self._build_gdrive()
            tmpdir, prepared = prepare_drive_files(
                gdrive,
                candidates,
                request_id=ctx.request_id,
                log=log,
                log_prefix="clientkarte_docs",
                tmp_prefix="aila_karte_docs_",
                max_bytes=max_bytes,
            )
            if not prepared:
                log.warning("clientkarte_attach_failed", reason="download")
                return 0, failed_line
            delivered, _where = asyncio.run(
                deliver_files(
                    self._slack_client(),
                    prepared=prepared,
                    # 一覧の DM 転送（``dm_forward_text`` の 🗂️ 行）と同じ DM に並ぶので、
                    # 「実ファイル」と明記して同じ文の重複に見えないようにする（指摘6）。
                    comment=(
                        f"📎 「{slack_label(client_name.strip())}」のカルテの関連資料"
                        "（実ファイル）です。"
                    ),
                    request_id=ctx.request_id,
                    channel_id=None,
                    thread_ts=None,
                    dm_channel=dm_channel,
                )
            )
        except Exception as e:
            # 型名だけ残す（403 / タイムアウト / event loop 事故を切り分けられるように）。
            # メッセージ本文は Drive のファイル名等を含み得るので載せない。
            log.warning("clientkarte_attach_failed", reason="exception", error=type(e).__name__)
            return 0, failed_line
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

        if not delivered:
            log.warning("clientkarte_attach_failed", reason="upload")
            return 0, failed_line
        _dedup_remember(email, delivered, dedup_ttl_s)
        note = _attach_note(
            delivered=delivered,
            requested=[fid for fid, _ in candidates],
            title_by_fid=title_by_fid,
        )
        if already:
            note += f"\n{_ATTACH_DEDUP_LINE}"
        return len(delivered), note

    def _slack_client(self) -> Any:
        """Slack クライアントを遅延生成して **1 インスタンスだけ**使い回す。

        チャンネル経路のカルテは同じ実行の中で Slack へ 2 回書く（実ファイル添付 →
        資料セクションの DM 転送）。毎回 ``from_env()`` すると 1 リクエストで
        AsyncWebClient が 2 つ立つので、ここで掴んで共有する。
        """
        if self._slack is None:
            self._slack = self._build_slack()
        return self._slack

    def _build_slack(self) -> Any:
        from teamagent.adapters.slack_client import SlackClient

        return SlackClient.from_env()

    def _build_gdrive(self) -> Any:
        from teamagent.adapters.gdrive_client import GDriveClient

        return GDriveClient.from_env(readonly=True)

    def _to_event(self, hit: SearchHit) -> KarteEvent:
        meta = hit.metadata or {}
        # 出典 URL 方針: カルテの 1 行 1 行が引用元（Slack スレッド permalink /
        # FB シート行直リンク / Drive web_view_link）へ辿れるようにする。
        # 変換できない内部識別子は URL を出さない（推測しない）。
        return KarteEvent(
            chunk_id=hit.chunk_id,
            url=source_link(str(meta.get("source_uri") or "")),
            occurred_at=meta.get("occurred_at"),
            deal_phase=meta.get("deal_phase"),
            bant_score=meta.get("bant_score"),
            channel_type=meta.get("channel_type"),
            next_action=meta.get("next_action"),
            summary=_truncate_at_sentence_boundary(
                hit.content,
                self._event_summary_max_chars,
            ),
        )

    def _synthesize(
        self, client_name: str, hits: list[SearchHit], request_id: str
    ) -> tuple[str, float]:
        """時系列 FB を Bedrock でカルテに合成する。FB が無ければ Bedrock を呼ばない。"""
        if not hits:
            return (f"「{client_name}」の営業 FB 記録が見つかりませんでした。", 0.0)

        system = load_prompt("clientkarte", self._prompt_version, "system")

        lines: list[str] = []
        for h in hits:
            m = h.metadata or {}
            head = (
                f"[chunk_id: {h.chunk_id}] {m.get('occurred_at', '日付不明')} "
                f"/ フェーズ={m.get('deal_phase', '-')} / BANT={m.get('bant_score', '-')} "
                f"/ チャネル={m.get('channel_type', '-')}"
            )
            extras: list[str] = []
            for label, key in (
                ("顧客反応", "client_reaction"),
                ("ポジ", "positive_reaction"),
                ("ネガ", "negative_reaction"),
                ("次アクション", "next_action"),
                ("提案メニュー", "proposed_menu"),
                ("共有メモ", "shared_memo"),
            ):
                if m.get(key):
                    extras.append(f"{label}: {m[key]}")
            # 構造化メタを本文より前に独立表示し、位置依存の本文抜粋より優先して合成させる。
            extra_block = ("\n  構造化メタ: " + " / ".join(extras)) if extras else ""
            body = _truncate_at_sentence_boundary(
                h.content,
                self._synthesis_body_max_chars,
            )
            lines.append(f"{head}{extra_block}\n  補足本文（構造化メタにない情報の補完）: {body}")

        timeline_block = "\n\n".join(lines)
        user_message = (
            f"# 対象クライアント\n{client_name}\n\n"
            f"# 時系列の営業 FB（古い順、{len(hits)} 件）\n{timeline_block}\n\n"
            "上記を、フォーマットに従ってクライアント・カルテに束ねてください。"
        )

        resp = self._bedrock.converse(
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            request_id=request_id,
            system=system,
            cache_system=True,
            max_tokens=self._summary_max_tokens,
        )
        return resp.text, resp.usage.cost_usd
