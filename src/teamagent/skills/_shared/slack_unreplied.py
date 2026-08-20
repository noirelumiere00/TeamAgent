"""Slack 返信漏れ（未返信メンション）検知 Provider（v0.3 Task 1）。

朝ダイジェストの「Slack 返信漏れ」セクション用に、本人 xoxp で
「horizon 日以内に自分がメンションされ、その後そのスレッドで自分が発言していない」
メッセージを集める。判定はビジネスロジックなので skills 層（本モジュール）に置き、
Slack I/O は adapters 層の :class:`SlackUserReader` に委譲する（3層分離）。

設計原則:
  - **fail-open**: トークン無し・scope 不足・API 失敗はすべて空リスト（朝ダイジェスト
    全体を絶対に止めない。コスト/付加機能は fail-open＋可観測性、の統一原則）。
  - **API 呼び出し上限**: search 1 回＋conversations.replies 最大 ``max_thread_checks``
    回に固定（非 Marketplace アプリのレート制限が未実測のため保守的に。制限に当たった
    呼び出しは SlackUserReader 側の fail-open で空になり、本 Provider は判定不能として
    その候補を **skip する**＝証拠なしに「未返信」を主張しない）。
  - **G8**: ログは件数・latency のみ。本文・permalink・channel 名は出さない。

既知の限界（v1・意図的）:
  - スレッド外（チャンネル直下）のメンションに「スレッドを使わず後続メッセージで
    返信した」ケースは検知できず未返信扱いになる（permalink 付きで出すので本人が
    1 クリックで確認できる。過検知は許容、見逃しよりまし）。
  - リアクションだけで済ませたケースも未返信扱い（SlackMessage に reactions が
    無いため。必要になったら adapter 拡張で対応）。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import structlog

logger = structlog.get_logger(__name__)

# JST（zoneinfo を引かずに固定オフセットで足りる用途）。
_JST = timezone(timedelta(hours=9))

# search.messages で一度に見るメンション候補の上限（API 1 回に収める）。
_DEFAULT_SEARCH_COUNT = 20
# conversations.replies を呼ぶスレッド数の上限（レート制限への保守設計）。
_DEFAULT_MAX_THREAD_CHECKS = 10
# 後方互換 API :meth:`SlackUnrepliedProvider.collect` が返す件数の上限。
# ⚠️ :meth:`collect_detailed` はこれで間引かない（判定層へ全件渡す）。表示件数の決定は
# 描画層（_HANDOFF_MAX_ITEMS）の仕事で、ここで先に削ると 6 件目以降が永久に見えない。
_DEFAULT_MAX_ITEMS = 5
# users.info を叩く差出人の上限（表示する件数ぶんで足りる。レート制限への保守設計）。
_DEFAULT_MAX_NAME_LOOKUPS = 8

# 本文中の名指し（`<@U123>` / `<@U123|alice>`）から user_id だけを取り出す。
_MENTION_ID_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")

# conversation id の先頭 1 文字 → 会話種別（Slack の ID 採番規則・API 追加呼び出し 0 回）。
_CHANNEL_KIND_BY_PREFIX = {"D": "dm", "G": "group_dm", "C": "channel"}
# 判定できなかったときの値。空欄と同じ意味で、**推測で埋めない**ことを明示する。
CHANNEL_KIND_UNKNOWN = "unknown"


@dataclass(frozen=True)
class UnrepliedMention:
    """未返信と判定されたメンション 1 件（マスク前の生値・ログ厳禁）。

    ``user`` 以降は「相手があなたの返事で止まっているか」を判定するための材料。
    すべて **既に取得済みのレスポンスから導出**（``user_display`` を除き API 追加
    呼び出しは 0 回）。読み取れなかったものは ``None`` / 空 / ``unknown`` のまま
    にする＝勝手に埋めない。
    """

    channel_id: str
    channel_name: str
    ts: str
    text: str
    permalink: str
    occurred_at: str  # ISO8601（JST）
    # --- 差出人 ---
    user: str | None = None
    """メンションした人の Slack user_id（search.messages 由来・取れなければ None）。"""
    user_display: str | None = None
    """差出人の表示名。users.info で解決できなかったら **None のまま**（架空の名前を作らない）。"""
    # --- 会話の種別（channel_id の先頭 1 文字から・API 追加呼び出し 0 回）---
    channel_kind: str = CHANNEL_KIND_UNKNOWN
    """"dm" / "group_dm" / "channel" / "unknown"。"""
    # --- スレッド由来の文脈（conversations.replies の結果を捨てずに使う）---
    thread_message_count: int = 0
    """スレッド内のメッセージ総数（親を含む）。0 = 取得できなかった。"""
    thread_participant_ids: tuple[str, ...] = ()
    """スレッド参加者の user_id（登場順・重複排除）。"""
    thread_last_user_id: str | None = None
    """スレッド最終発言者の user_id。"""
    thread_last_at: str = ""
    """スレッド最終発言の日時（ISO8601 JST）。空 = 不明。"""
    answered_by_other: bool = False
    """メンション後に「自分でも差出人でもない」誰かが発言したか（＝他人が代わりに答えた可能性）。

    差出人自身の追撃は :attr:`sender_followed_up` の担当。ここに混ぜると
    「催促が来ている」を「もう解決した」と読み違える。
    """
    sender_followed_up: bool = False
    """差出人自身がメンション後にもう一度発言したか（催促 or 自己解決）。"""
    mentioned_user_ids: tuple[str, ...] = ()
    """本文で名指しされた user_id（自分を含む・登場順）。「他N名も名指し」の判定用。"""


@dataclass(frozen=True)
class UnrepliedCollection:
    """1 回の走査結果。表示ぶん（``items``）と母数（``total_unreplied``）を分けて持つ。

    ``total_unreplied`` は **実際に判定できた範囲での** 未返信件数。走査上限に当たって
    打ち切った場合は ``scan_truncated=True`` になり、``total_unreplied`` は下限値
    （「少なくともこれだけある」）を意味する。呼び出し側はこれを混同してはいけない。
    """

    items: tuple[UnrepliedMention, ...] = ()
    total_unreplied: int = 0
    scanned_matches: int = 0
    thread_checks: int = 0
    undetermined: int = 0
    """conversations.replies が取れず判定不能だった候補数（未返信と主張しない件）。"""
    scan_truncated: bool = False
    """走査上限で打ち切ったか（True なら total_unreplied は下限値）。"""
    scanned: bool = False
    """**実際に走査できたか**（search を 1 回でも投げられたか）。

    この Provider は fail-open で、未連携・scope 不足・store 障害・reader 生成失敗を
    すべて「空の Collection」に潰す。空リストだけでは「0 件だった」と「見ていない」を
    区別できず、下流が「返信漏れなし」と嘘をつく。区別できる唯一の事実がこれ。
    """


class _SlackStore(Protocol):
    def get(self, user_email: str) -> Any: ...


def _thread_root(permalink: str, ts: str) -> str:
    """permalink の ``thread_ts`` クエリからスレッド親 ts を得る（無ければ自身が親扱い）。"""
    try:
        qs = parse_qs(urlparse(permalink).query)
        root = (qs.get("thread_ts") or [""])[0]
        return root or ts
    except Exception:
        return ts


def _ts_float(ts: str) -> float:
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def _channel_kind(channel_id: str, channel_name: str = "") -> str:
    """会話種別を判定する（**API 追加呼び出し 0 回**）。

    一次情報は conversation id の先頭 1 文字（D=DM / G=グループDM / C=チャンネル）。
    ただし ``G`` は旧世代のプライベートチャンネルにも使われ、逆に新しいワークスペースの
    複数人 DM が ``C`` 始まりのこともある。Slack は複数人 DM の ``name`` を必ず
    ``mpdm-…`` にするので、名前が ``mpdm-`` ならそちらを優先する。
    どちらでも決まらなければ ``unknown``（空欄と同義・推測で埋めない）。
    """
    if (channel_name or "").startswith("mpdm-"):
        return "group_dm"
    head = (channel_id or "")[:1]
    return _CHANNEL_KIND_BY_PREFIX.get(head, CHANNEL_KIND_UNKNOWN)


def _mentioned_ids(text: str) -> tuple[str, ...]:
    """本文から名指しされた user_id を登場順・重複排除で取り出す。"""
    return tuple(dict.fromkeys(_MENTION_ID_RE.findall(text or "")))


def _iso_jst(ts: str) -> str:
    f = _ts_float(ts)
    if f <= 0:
        return ""
    return datetime.fromtimestamp(f, tz=_JST).isoformat(timespec="seconds")


class SlackUnrepliedProvider:
    """本人 xoxp で「メンションされたのに未返信」のスレッドを集める（読み取り専用）。"""

    def __init__(
        self,
        slack_store: _SlackStore,
        *,
        reader_factory: Callable[[str], Any] | None = None,
        search_count: int = _DEFAULT_SEARCH_COUNT,
        max_thread_checks: int = _DEFAULT_MAX_THREAD_CHECKS,
        max_items: int = _DEFAULT_MAX_ITEMS,
        max_name_lookups: int = _DEFAULT_MAX_NAME_LOOKUPS,
    ) -> None:
        if reader_factory is None:
            from teamagent.adapters.slack_user_reader import SlackUserReader

            reader_factory = SlackUserReader.from_user_token
        self._store = slack_store
        self._reader_factory = reader_factory
        self._search_count = search_count
        self._max_thread_checks = max_thread_checks
        self._max_items = max_items
        self._max_name_lookups = max_name_lookups

    def collect(
        self, user_email: str, horizon_days: int, request_id: str
    ) -> list[UnrepliedMention]:
        """未返信メンションを新しい順で最大 ``max_items`` 件返す（後方互換 API）。

        母数（走査した範囲の総件数）が要る呼び出し側は :meth:`collect_detailed` を使う。
        """
        items = self.collect_detailed(user_email, horizon_days, request_id).items
        return list(items[: self._max_items])

    def collect_detailed(
        self, user_email: str, horizon_days: int, request_id: str
    ) -> UnrepliedCollection:
        """未返信メンションを走査し、**判定できた全件**＋母数＋走査の完全性を返す。

        走査を止めるのは ``max_thread_checks``（＝conversations.replies の呼び出し
        上限）だけ。``items`` は表示用に間引かない（間引くと「新しい順の先頭 N 件」
        しか判定層に届かず、その先にある「あなたの番」が DM に一切出なくなる）。
        表示件数を決めるのは描画層の仕事。
        失敗はすべて空の :class:`UnrepliedCollection`（fail-open・``scanned=False``）。
        """
        start = time.perf_counter()
        try:
            token = self._store.get(user_email)
        except Exception as e:  # store 障害でもダイジェストは止めない
            logger.warning(
                "slack_unreplied_store_failed", request_id=request_id, error=type(e).__name__
            )
            return UnrepliedCollection()
        if token is None or not getattr(token, "access_token", ""):
            # 未連携（正常系）。エラーにしない＝指示書の「トークン無しは空リスト」。
            return UnrepliedCollection()
        uid = getattr(token, "slack_user_id", "") or ""
        scopes = tuple(getattr(token, "scopes", ()) or ())
        if not uid or "search:read" not in scopes:
            # 旧スコープで連携済みのユーザー。再連携（Reinstall 後の再認可）が必要。
            logger.info(
                "slack_unreplied_scope_missing",
                request_id=request_id,
                has_uid=bool(uid),
            )
            return UnrepliedCollection()

        try:
            reader = self._reader_factory(token.access_token)
        except Exception as e:  # xoxp 空等（防御的・通常は上で弾ける）
            logger.warning(
                "slack_unreplied_reader_failed", request_id=request_id, error=type(e).__name__
            )
            return UnrepliedCollection()

        # Slack 検索の after: は「その日付より後」（日付単位・排他的）。horizon_days ちょうど
        # 前の日を含めるため +1 日遡る（見逃しより過検知を許容する本機能の哲学と整合）。
        after = (datetime.now(tz=_JST) - timedelta(days=horizon_days + 1)).date().isoformat()
        # 自分へのメンションを新しい順に検索。自分の発言は除外（自己メンション対策）。
        matches = reader.search(f"<@{uid}> after:{after}", request_id, count=self._search_count)

        found: list[UnrepliedMention] = []
        seen_roots: set[tuple[str, str]] = set()
        checks = 0
        undetermined = 0
        truncated = False
        for m in matches:
            if checks >= self._max_thread_checks:
                # まだ候補が残っているのに上限に当たった。
                # ＝この先に未返信があっても数えられていない＝母数は下限値。
                truncated = True
                break
            if not m.channel_id or not m.ts:
                continue
            if m.user and m.user == uid:
                continue  # 自分の発言内の自己メンション
            root = _thread_root(m.permalink, m.ts)
            key = (m.channel_id, root)
            if key in seen_roots:
                continue
            seen_roots.add(key)
            thread = reader.read_thread(m.channel_id, root, request_id)
            checks += 1
            if not thread:
                # API 失敗（fail-open で空）＝判定不能。証拠なしに「未返信」と言わない。
                undetermined += 1
                continue
            mention_ts = _ts_float(m.ts)
            after_mention = [t for t in thread if _ts_float(t.ts) > mention_ts]
            if any(t.user == uid for t in after_mention):
                continue  # 自分が返している＝未返信ではない
            last = max(thread, key=lambda t: _ts_float(t.ts))
            sender = m.user or None
            others_after = [t for t in after_mention if t.user and t.user not in (uid, sender)]
            found.append(
                UnrepliedMention(
                    channel_id=m.channel_id,
                    channel_name=m.channel_name,
                    ts=m.ts,
                    text=m.text,
                    permalink=m.permalink,
                    occurred_at=_iso_jst(m.ts),
                    user=m.user or None,
                    channel_kind=_channel_kind(m.channel_id, m.channel_name),
                    thread_message_count=len(thread),
                    thread_participant_ids=tuple(dict.fromkeys(t.user for t in thread if t.user)),
                    thread_last_user_id=last.user or None,
                    thread_last_at=_iso_jst(last.ts),
                    answered_by_other=bool(others_after),
                    sender_followed_up=bool(
                        sender and any(t.user == sender for t in after_mention)
                    ),
                    mentioned_user_ids=_mentioned_ids(m.text),
                )
            )

        # search 自体が上限で頭打ちなら、その先にメンションが残っている可能性がある。
        # ここも「母数は下限値」として正直に申告する（読み取れなかったものは埋めない）。
        if len(matches) >= self._search_count:
            truncated = True

        # 判定できたぶんは **全部** 返す。ここで表示件数まで削ると「新しい順の先頭 5 件」
        # だけが判定層に届き、6 番目以降にある「あなたの番」が 🔴 に一切現れない
        # （母数だけが増える）。件数の上限は max_thread_checks が構造的に押さえている。
        # 実名解決は max_name_lookups で別に上限を掛ける（users.info を浪費しない）。
        names = self._resolve_display_names(reader, found, request_id)
        items = tuple(replace(x, user_display=names.get(x.user or "")) for x in found)

        logger.info(
            "slack_unreplied_collected",
            request_id=request_id,
            matches=len(matches),
            thread_checks=checks,
            unreplied=len(found),
            shown=len(items),
            named=sum(1 for x in items if x.user_display),
            undetermined=undetermined,
            scan_truncated=truncated,
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return UnrepliedCollection(
            items=items,
            total_unreplied=len(found),
            scanned_matches=len(matches),
            thread_checks=checks,
            undetermined=undetermined,
            scan_truncated=truncated,
            scanned=True,  # ここまで来た＝search を投げられた＝0 件は本当に 0 件
        )

    def _resolve_display_names(
        self, reader: Any, mentions: Iterable[UnrepliedMention], request_id: str
    ) -> dict[str, str]:
        """差出人 user_id → 表示名。解決できなかった ID は **辞書に入れない**（None のまま）。

        呼び出しは ``max_name_lookups`` 件で打ち切る（判定できた全件を渡すので、
        users.info の回数はこの上限だけが押さえている）。

        reader が ``get_display_name`` を持たない場合（旧 adapter / テスト fake）は
        何もしない＝この機能が無かったときと同じ挙動に落ちる。
        """
        resolve = getattr(reader, "get_display_name", None)
        if not callable(resolve):
            return {}
        out: dict[str, str] = {}
        looked = 0
        for uid in dict.fromkeys(x.user for x in mentions if x.user):
            if looked >= self._max_name_lookups:
                break
            looked += 1
            try:
                name = resolve(uid, request_id)
            except Exception:  # 名前が引けないだけでダイジェストは止めない
                name = None
            if isinstance(name, str) and name.strip():
                out[str(uid)] = name.strip()
        return out


__all__ = [
    "CHANNEL_KIND_UNKNOWN",
    "SlackUnrepliedProvider",
    "UnrepliedCollection",
    "UnrepliedMention",
]
