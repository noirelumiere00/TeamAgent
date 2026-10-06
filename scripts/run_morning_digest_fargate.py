"""Fargate Scheduled Task 用 morning_digest エントリポイント。

EventBridge cron (平日 0:30 UTC = 9:30 JST) が ECS RunTask で本スクリプトを起動する。

役割:
  1. 対象ユーザー解決（env `MORNING_DIGEST_USERS` 明示優先・無ければ RDS `oauth_tokens` 動的抽出）
  2. 各ユーザーごとに `MorningDigestSkill.run()` を実行
  3. 結果を Slack DM（Block Kit）で本人に配信
  4. CloudWatch Logs に JSON 構造化ログで結果サマリ出力
  5. 祝日スキップ（MORNING_DIGEST_HOLIDAY_SKIP・既定 OFF）が ON なら、祝日・会社休日は
     DM を送らず予定リマインドだけ登録し、祝日明けはメールの走査範囲を広げる

⚠️ 安全規則:
  - 生メール本文・生件名・生 From を一切ログに出さない（masked のみ）
  - 連携未済ユーザーは Skill 内で fail-closed・本スクリプトは skip して次へ
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import functools
import json
import os
import re
import sys
import uuid
from typing import Any

import structlog

from teamagent.adapters.digest_delivery_store import CLAIM_CLAIMED as _CLAIM_CLAIMED
from teamagent.adapters.digest_delivery_store import CLAIM_RESERVED as _CLAIM_RESERVED
from teamagent.adapters.digest_delivery_store import CLAIM_TAKEN as _CLAIM_TAKEN
from teamagent.hmac_durable_state import require_runtime_startup
from teamagent.hmac_keyring import MAIL_ACTION_MAX_TOKEN_TTL_S
from teamagent.skills._shared import slack_handoff as _handoff
from teamagent.skills._shared.grapheme_cut import truncate_graphemes
from teamagent.skills._shared.mail_connection import (
    FETCH_NEEDS_RECONNECT,
    FETCH_OK,
    FETCH_SCOPE_MISSING,
    FETCH_TEMPORARY,
    FETCH_TOKEN_EXPIRED,
    FETCH_UNKNOWN,
)
from teamagent.skills.morning_digest import calendar_window as _calwin
from teamagent.skills.morning_digest import preferences as _prefs

logger = structlog.get_logger(__name__)


def _resolve_target_users() -> list[str]:
    """env or DB から対象 user_email リストを取得し、除外リストを差し引く。

    優先順位:
      1. env `MORNING_DIGEST_USERS`（カンマ区切り・明示指定）
      2. RDS `oauth_tokens` の連携済全員（動的抽出）
    どちらの経路でも最後に env `MORNING_DIGEST_EXCLUDE` のユーザーを除外する。
    """
    explicit = os.environ.get("MORNING_DIGEST_USERS", "").strip()
    if explicit:
        users = [e.strip().lower() for e in explicit.split(",") if e.strip()]
    else:
        users = _fetch_connected_users_from_rds()
    return _apply_exclude(users)


def _apply_exclude(users: list[str]) -> list[str]:
    """env `MORNING_DIGEST_EXCLUDE`（カンマ区切り）のユーザーを対象から外す。

    Google 連携を切らずに、テストユーザーや一時停止したい人だけを digest 対象から
    除外する仕組み。明示リスト・RDS 動的抽出のどちらの経路でも最後に適用する。
    """
    raw = os.environ.get("MORNING_DIGEST_EXCLUDE", "").strip()
    if not raw:
        return users
    excluded = {e.strip().lower() for e in raw.split(",") if e.strip()}
    if not excluded:
        return users
    kept = [u for u in users if u.lower() not in excluded]
    removed = len(users) - len(kept)
    if removed:
        print(
            f"[run_morning_digest_fargate] excluded {removed} user(s) via MORNING_DIGEST_EXCLUDE",
            flush=True,
        )
    return kept


#: F0: 直近の「対象者の取得」が失敗した理由（型名だけ）。None は失敗なし。
#: ``_fetch_connected_users_from_rds`` は失敗を ``[]`` に潰す（既存の契約）ので、
#: 「対象 0 人」と「取得できず誰にも送っていない」を main() が区別するための印。
_TARGET_FETCH_ERROR: str | None = None

#: RDS の連携済み一覧が例外なしで 0 行だった印。連携済みの利用者がいる本番では起こらない
#: はずの形で、GUC・RLS・ロールの権限が崩れたときの症状（下の SET app.user_role の注記と
#: 同じ事故）。例外が無いので型名では拾えず、ここで失敗として扱う。
TARGET_ZERO_ROWS = "rds_zero_rows"


def _fetch_connected_users_from_rds() -> list[str]:
    """RDS oauth_tokens から連携済 user_email を取得。"""
    global _TARGET_FETCH_ERROR
    import psycopg

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("[run_morning_digest_fargate] WARN: DATABASE_URL 未設定", file=sys.stderr)
        _TARGET_FETCH_ERROR = "DATABASE_URL_missing"
        return []
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                # oauth_tokens は FORCE RLS（本人 GUC or admin）。この一覧取得は「配信対象の
                # 列挙」という管理系読み取りなので、policy に用意された admin 経路を明示する
                # （GUC 無しだと接続ロールによっては 0 行になり「誰にも配信されない」事故に
                # なる・2026-07-13 自動モード切替の事前監査で検出）。token 本体は読まない。
                cur.execute("SET app.user_role = 'admin'")
                cur.execute("SELECT user_email FROM oauth_tokens")
                rows = cur.fetchall()
        users = [str(r[0]).strip().lower() for r in rows if r and r[0]]
        if not users:
            # 例外なしの 0 行も「誰にも届かない」朝。main() が ERROR と管理者 DM で知らせる。
            print("[run_morning_digest_fargate] WARN: RDS 連携済抽出 0 行", file=sys.stderr)
            _TARGET_FETCH_ERROR = TARGET_ZERO_ROWS
        return users
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: RDS 連携済抽出失敗 {type(exc).__name__}",
            file=sys.stderr,
        )
        _TARGET_FETCH_ERROR = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:40] or "Exception"
        return []


def _build_token_store() -> Any:
    """factory.py の _build_token_store と同等（RDS + KMS or InMemory）。"""
    from teamagent.orchestrator.factory import _build_token_store

    return _build_token_store()


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1] if local else ''}***@{domain}"


# Gmail/Calendar への deep link（受信トレイ全体＝DLP 安全。項目別 from: はマスク済みのため不採用）。
_GMAIL_DRAFTS_URL = "https://mail.google.com/mail/u/0/#drafts"
_GMAIL_INBOX_URL = "https://mail.google.com/mail/u/0/#inbox"
_CALENDAR_URL = "https://calendar.google.com/"

# ボタン押下（block_actions）を固定 OpenClaw Slack adapter が署名検証し、caller identity
# plugin の同名 interactive namespace へ渡す action_id。value は HMAC 署名トークン
# （生 thread_id は載せない＝G3）。
_ACTION_MAIL_DRAFT = "mail_draft"
# 📅 カレンダー登録ボタン（v0.3 Task3）。value は event_token（HMAC署名・日時/タイトル入り）。
_ACTION_CALENDAR_EVENT = "calendar_event"
# 🗓 日程候補を提案ボタン（v0.3 Task4）。value は draft_token（同一形式・thread_id 由来）。
_ACTION_SCHEDULE_PROPOSE = "schedule_propose"
# ☑️ 確認済みボタン。value は ack_token（HMAC 署名・生 thread_id / channel_id は載らない）。
# 個別ボタンも「全部確認した」も同じ action_id で、種別は署名済み payload の typ が持つ。
_ACTION_DIGEST_ACK = "digest_ack"


def _schedule_button_enabled() -> bool:
    """MORNING_DIGEST_SCHEDULE_BUTTON=1 のときのみ🗓ボタンを描画（既定OFF・§10 E1-2）。"""
    return os.environ.get("MORNING_DIGEST_SCHEDULE_BUTTON", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _calendar_button_enabled() -> bool:
    """MORNING_DIGEST_CALENDAR_BUTTON=1 のときのみ📅ボタンを描画（既定OFF・§10 E1-2）。

    ボタンは押下先の calendar_event tool（USE_CALENDAR_EVENT_TOOL + toolFilter.include）が
    本番で有効になってから ON にする（先に出すと無反応ボタンになる）。"""
    return os.environ.get("MORNING_DIGEST_CALENDAR_BUTTON", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _ack_button_enabled() -> bool:
    """MORNING_DIGEST_ACK_BUTTON=1 のときのみ ☑️ボタンを描画（既定OFF）。

    ボタンは押下先の digest_ack tool（USE_DIGEST_ACK_TOOL + toolFilter.include）が
    本番で有効になってから ON にする（先に出すと無反応ボタンになる）。なお skill 側の
    MORNING_DIGEST_ACK_FILTER が OFF なら ack_token 自体が空なので、この flag だけ
    ON にしてもボタンは 1 つも出ない（二重の安全弁）。"""
    return os.environ.get("MORNING_DIGEST_ACK_BUTTON", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _compact_enabled() -> bool:
    """MORNING_DIGEST_COMPACT=1 のときのみ密度優先描画（既定OFF・旧描画を完全温存）。

    2026-07-13 パイロットFB「Slackとメールの部分が見づらい」対応。ON/OFF は env のみで
    切替可能（taskdef 差し替えだけ・再ビルド不要）。"""
    return os.environ.get("MORNING_DIGEST_COMPACT", "").strip().lower() in {"1", "true", "yes"}


# --- 密度優先描画（MORNING_DIGEST_COMPACT）の表示上限と切り詰め ---
_COMPACT_SUBJ_LEN = 60  # 件名/要約の切詰
_COMPACT_SECTION_CHARS = 2800  # Slack section text 上限3000字の保険
_COMPACT_MAX_BLOCKS = 48  # Slack blocks 上限50個の保険

# --- 💬 Slack 返信漏れ（判定層 _shared/slack_handoff の出力を並べるだけ）---
_HANDOFF_MAX_ITEMS = 5  # DM に並べるカード数の上限（母数は見出しに出す）
_HANDOFF_CHANNEL_NAME_LEN = 24  # chip のチャンネル名の切詰（1 件 1 行を守る）
_HANDOFF_SENDER_NAME_LEN = 12  # chip の差出人名の切詰（同上）
_HANDOFF_BUCKET_EMOJI: dict[str, str] = {
    _handoff.BUCKET_YOURS: "🔴",
    _handoff.BUCKET_WATCH: "⏸",
    _handoff.BUCKET_FYI: "👁",
}
_HANDOFF_EMPTY_LINE = "💬 *Slack 返信漏れ*: なし"
#: 走査できていない（未連携・scope 不足・取得失敗）ときの 1 行。**「なし」と言わない**。
#: 見逃し防止が目的の機能で「見ていない」を「無い」と言うのは、最もやってはいけない嘘。
_HANDOFF_UNSCANNED_LINE = (
    "💬 *Slack 返信漏れ*: 確認できませんでした（Slack 未連携か、取得に失敗しています）"
)
#: 判定層で想定外の例外が出たときの 1 行（💬 だけを落とし、DM 全体は配信する）。
_HANDOFF_FAILED_LINE = "💬 *Slack 返信漏れ*: 表示できませんでした"
#: ⚠️ 見出しは逐語ではない（話題の切り出し＋固定語尾・型不明のときだけ依頼文そのまま）。
#: ここで「原文のみ」と言い切ると、その真横で作った述語が嘘になる。
#: 逆に `「」` の読み分け（囲みは相手の言葉／囲み無しは Aico のラベル）は **利用者に
#: 伝わって初めて意味がある**ので、脚注で名乗る。伝えずに囲むだけでは誤読は消えない。
_HANDOFF_FOOTNOTE = (
    "※ 「」内は相手の原文そのままです。囲みの無い見出しは Aico が付けた定型の言い換えです"
    "（要約文は作りません）。"
)

#: 本人の設定（digest_settings）で表示を変えている人の DM 末尾に添える 1 行。
#: 「欄が消えた＝不具合」と誤解させず、戻し方をその場で分かるようにする。
_PREFS_FOOTER = (
    "_あなたの設定で表示を変えています。"
    "Aico の DM で「朝の設定を見せて」と言えば確認・変更できます。_"
)

#: 既に敬称が付いている表示名（「田中さん」）へ「さん」を重ねないための検査。
_HONORIFIC_TAIL_RE = re.compile(r"(?:さん|サン|様|さま|氏|君|くん|ちゃん|先生|部長|課長|社長)$")

#: 実名が引けなかったときの表記。**架空の名前を作らない**＝空欄だと明示する。
_NAME_UNRESOLVED = "（表示名なし）"
_MENTION_UNRESOLVED = f"@{_NAME_UNRESOLVED}"
_CHANNEL_UNRESOLVED = f"#{_NAME_UNRESOLVED}"

#: 描画済みリンク `<url|ラベル>` / `<url>`。生 ID 検査から URL を退避するのに使う。
_LINK_MARKUP_RE = re.compile(r"<(https?://[^|>\s]+)(?:\|([^>]*))?>")

#: channel_id の先頭1文字 → 会話種別（API 追加呼び出し 0 回で判る）。
_CHANNEL_ID_PREFIX_KIND: dict[str, str] = {"D": "dm", "G": "group_dm", "C": "channel"}

_MENTION_RE = re.compile(r"<@[A-Za-z0-9_.\-]+\|([^>]+)>")
_MENTION_BARE_RE = re.compile(r"<@([A-Za-z0-9_.\-]+)>")
_CHANNEL_TOKEN_RE = re.compile(r"<#[A-Z0-9]+\|([^>]*)>")
#: ラベル無しの `<#C08…>`。現行の Slack API はこの形も普通に返すので、生 ID を
#: そのまま見せないよう「#（表示名なし）」へ畳む（名前は取りに行かない＝API 追加 0 回）。
_CHANNEL_BARE_RE = re.compile(r"<#[A-Z0-9]+>")
#: ユーザーグループ `<!subteam^S08…|@design>` / ラベル無し、および `<!here>` 等。
_USERGROUP_RE = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|([^>]*))?>")
_SPECIAL_MENTION_RE = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")
_LINK_LABEL_RE = re.compile(r"<https?://[^|>]+\|([^>]+)>")
_LINK_BARE_RE = re.compile(r"<https?://[^>]+>")


def _truncate(s: str, limit: int) -> str:
    """limit 超過時は末尾を「…」に置き換える（1件=1行原則のための単純字数切詰）。

    切り口は絵文字（🇯🇵・ZWJ 連結など）を割らない（片割れを「…」の前に残さない）。
    """
    s = s or ""
    return s if len(s) <= limit else truncate_graphemes(s, max(0, limit - 1)) + "…"


def _resolve_mention(user_id: str, names: dict[str, str] | None) -> str:
    """`<@U…>` を実名へ。**引けなければ架空の名前を作らず「表示名なし」と明示する。**

    旧描画は一律 "@メンバー" に潰していたが、これは DLP マスクではなく単なる表示整形
    だった（実名が引ければ置換で直る）。data 層が users.info で解決した表示名を
    ``names``（user_id → 表示名）で渡し、引けなかったものだけ空欄表記へ落とす。
    """
    name = (names or {}).get(user_id, "")
    return f"@{name}" if name else _MENTION_UNRESOLVED


def _flatten_slack_text(raw: str, names: dict[str, str] | None = None) -> str:
    """Slack 生本文の抜粋整形: メンション/リンク表記を可読化し空白を1つに畳む。

    処理順は「正規化→切詰→escape」（escape は呼び出し側）。`<https://evil|クリック>` の
    ような偽装リンクはラベル文字列だけが残り、リンクとしては絶対に描画されない。
    """
    s = raw or ""
    s = _MENTION_RE.sub(r"@\1", s)
    s = _MENTION_BARE_RE.sub(lambda m: _resolve_mention(m.group(1), names), s)
    s = _SPECIAL_MENTION_RE.sub(r"@\1", s)
    s = _USERGROUP_RE.sub(lambda m: m.group(1) or _MENTION_UNRESOLVED, s)
    s = _CHANNEL_TOKEN_RE.sub(r"#\1", s)
    s = _CHANNEL_BARE_RE.sub(_CHANNEL_UNRESOLVED, s)
    s = _LINK_LABEL_RE.sub(r"\1", s)
    s = _LINK_BARE_RE.sub("(リンク)", s)
    return re.sub(r"\s+", " ", s.replace("\x00", "")).strip()


# JST は skill 側と同一定義を使う（窓・表示・リマインドで解釈がズレないよう単一の真実源）。
_JST = _calwin.JST


def _fmt_time(iso: str | None) -> str:
    """ISO 開始時刻 → JST の HH:MM（本人は日本在勤）。パース失敗時は原文 or '?'。"""
    if not iso:
        return "?"
    try:
        dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(_JST)
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return iso[:16]


def _slack_escape(s: str) -> str:
    """Slack mrkdwn の特殊文字をエスケープ。

    実件名/実名(未マスクの display)を mrkdwn に入れるため、メール件名に
    `<https://evil|クリック>` 等を仕込まれてもリンク偽装/書式崩れにならないようにする。
    Slack 仕様では & < > のみエスケープが必要。
    """
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# 💬 Slack 返信漏れセクション（判定は _shared/slack_handoff・ここは並べるだけ）
#
# 設計の芯（ユーザー承認済みモック）:
#   - 1件=1行。補足行（└）は判定層が「原文を見る価値がある」と印を付けた件だけ。
#   - 要約文は作らない。ただし **見出しは逐語ではない**（判定層が原文から切り出した話題
#     ＋固定語尾。型が判らない依頼だけ依頼文そのまま）。脚注もそう名乗ること。
#   - 読み取れなかった項目は **描かない**（推測で埋めない）。0 件も「走査できたときだけ」
#     なしと書く（未走査を「なし」と言うのは、この機能が潰そうとしている見逃しそのもの）。
#   - この digest が持っている user_id / channel_id は 1 文字も出さない
#     （_guard_no_raw_ids が最終検査。本文中の `<#C…>` `<!subteam^…>` は種別語へ畳む）。
#     ⚠️ 形が ID に似ているだけの語（@BUZZFEEDJAPAN・#CAMPAIGN2026）は **潰さない**。
#     根拠のない置換は原文改変＝捏造側であり、見逃しより有害。
# ---------------------------------------------------------------------------


def _handoff_now() -> _dt.datetime:
    """判定層へ注入する現在時刻（JST）。テストで固定できるよう関数に切ってある。"""
    now: _dt.datetime = _calwin.now_jst()
    return now


def _handoff_names(items: list[Any]) -> dict[str, str]:
    """user_id → 表示名（data 層が users.info で解決できたぶんだけ）。"""
    names: dict[str, str] = {}
    for it in items:
        uid = str(getattr(it, "from_user_id", "") or "").strip()
        name = str(getattr(it, "from_display_name", "") or "").strip()
        if uid and name:
            names[uid] = name
    return names


def _handoff_known_ids(items: list[Any]) -> frozenset[str]:
    """この digest が実際に持っている Slack ID＝描画に漏れうる ID の全集合。

    「形が ID っぽい語」ではなく「実在する ID」だけを掃除対象にするための材料。
    """
    ids: set[str] = set()
    for it in items:
        for field in ("channel_id", "from_user_id", "thread_last_user_id"):
            value = str(getattr(it, field, "") or "").strip()
            if len(value) >= 4:
                ids.add(value)
        for field in ("thread_participant_ids", "mentioned_user_ids"):
            for raw in getattr(it, field, ()) or ():
                value = str(raw).strip()
                if len(value) >= 4:
                    ids.add(value)
    return frozenset(ids)


@functools.lru_cache(maxsize=16)
def _id_scrub_pattern(known_ids: frozenset[str]) -> re.Pattern[str] | None:
    """``known_ids`` を **1 本の交替パターンへ事前コンパイル**して使い回す。

    ⚠️ ID ごとに `re.sub(パターン文字列, …)` を呼ぶと、ID 数が `re._MAXCACHE`(512) を
    超えた瞬間に全パターンが毎回再コンパイルされ、描画が数十倍に跳ねる（実測 22ms→1.2s）。
    件数に比例して増えるものを毎行ループで回さないこと。
    """
    ids = sorted((i for i in known_ids if i), key=len, reverse=True)
    if not ids:
        return None
    body = "|".join(re.escape(i) for i in ids)
    return re.compile(rf"(?<![0-9A-Za-z])(?:{body})(?![0-9A-Za-z])")


def _scrub_slack_ids(s: str, known_ids: frozenset[str] = frozenset()) -> str:
    """生 ID を「（表示名なし）」へ落とす（本人にとって無意味な文字列を見せない）。

    落とすのは **``known_ids`` の完全一致だけ**＝この digest に実在する channel_id /
    user_id。形だけの総当たり置換をしないのは、原文の普通の語を壊さないため
    （`@BUZZFEEDJAPAN` `#CAMPAIGN2026` `CONFIDENTIAL` は生 ID と同じ形をしている）。
    実名が引けなかった `<@U…>` は :func:`_flatten_slack_text` が既に
    「@（表示名なし）」へ落としているので、素の `@英大文字` を形で潰す必要は無い。
    """
    pat = _id_scrub_pattern(known_ids)
    return pat.sub(_NAME_UNRESOLVED, s or "") if pat is not None else (s or "")


def _handoff_display(
    raw: str, names: dict[str, str], known_ids: frozenset[str] = frozenset()
) -> str:
    """表示テキストの共通経路: **NFKC 正規化 → 実名解決 → 生 ID 除去 → mrkdwn エスケープ**。

    順序が要（escape を先にやると `<@U…>` が `&lt;@U…&gt;` になって実名解決が効かない）。

    ⚠️ **`_handoff.normalize_text`（NFKC）は必ず先頭**。NFKC は全角記号を半角へ倒すので、
    `_slack_escape` の後ろへ移すと本文の `＜@U08…＞` が escape をすり抜けてから `<@U08…>`
    へ戻り、実名解決も生 ID 検査も飛ばして **Slack の生メンションとして描画される**
    （＝無害化の貫通）。この 1 行の並びが不変条件で、順序テストで固定してある。
    """
    return _slack_escape(
        _scrub_slack_ids(_flatten_slack_text(_handoff.normalize_text(raw), names), known_ids)
    )


def _handoff_link(url: str) -> str:
    """permalink をリンクとして描画してよいか。https 以外・区切り文字混入は捨てる。

    ⚠️ `javascript:` 等を弾くだけでなく `http://` も捨てる（Slack permalink は必ず
    https。平文にダウングレードした URL を DM から踏ませる導線を作らない）。
    """
    u = (url or "").strip()
    if not u.startswith("https://"):
        return ""
    return "" if any(c in u for c in "<>|\x00 \t\n") else u


def _guard_no_raw_ids(line: str, known_ids: frozenset[str] = frozenset()) -> str:
    """**描画直前の最終検査**。リンク URL 以外に生 ID が残っていたら安全な表記へ落とす。

    permalink の URL には会話 ID が必ず入るが、それは本人に見えない（ラベルは「開く」）。
    そこでリンク記法だけを退避してから検査し、URL は原形のまま戻す。

    ⚠️ 退避の目印に NUL を使うので、**本文由来の NUL は先に落とす**（`\\x000\\x00` を
    本文に仕込まれると、戻すときに permalink 記法を任意の位置へ複製できてしまう）。
    """
    line = (line or "").replace("\x00", "")
    holes: list[str] = []

    def _hide(m: re.Match[str]) -> str:
        url, label = m.group(1), m.group(2)
        safe = f"<{url}|{_scrub_slack_ids(label, known_ids)}>" if label is not None else f"<{url}>"
        holes.append(safe)
        return f"\x00{len(holes) - 1}\x00"

    masked = _scrub_slack_ids(_LINK_MARKUP_RE.sub(_hide, line or ""), known_ids)
    for i, hole in enumerate(holes):
        masked = masked.replace(f"\x00{i}\x00", hole)
    return masked


def _handoff_channel_kind(item: Any) -> str:
    """会話種別。data 層の channel_kind が正。無い（旧 output）ときだけ ID の先頭で補う。

    ⚠️ 明示的な "unknown" は「判定できなかった」＝空欄と同義なので上書きしない。
    """
    kind = str(getattr(item, "channel_kind", "") or "").strip()
    if kind:
        return kind
    cid = str(getattr(item, "channel_id", "") or "").strip().upper()
    return _CHANNEL_ID_PREFIX_KIND.get(cid[:1], "unknown") if cid else "unknown"


def _with_honorific(name: str) -> str:
    """差出人名に「さん」を付ける（既に敬称が付いている名前へ重ねない）。"""
    return name if _HONORIFIC_TAIL_RE.search(name) else f"{name}さん"


def _handoff_channel_chip(item: Any, names: dict[str, str], known_ids: frozenset[str]) -> str:
    """**誰から・どこで**の chip（**戻り値は display 済み**＝呼び出し側で二重に通さない）。

    **相手を先頭に出す**（`江畑 未来さん（DM）`）。DM が 3 件並んだとき、利用者が最初に
    知りたいのは「誰が待っているか」であって会話の種別ではない（2026-09-11 実物の指摘）。

    **`#` はチャンネル（C）のときだけ**付ける。DM / グループDM の `channel.name` は
    user_id そのものなので、`#` を無条件に前置すると本人に意味の無い生 ID が出る
    （旧描画の実害）。実名が引けなければ種別ラベルまでしか言わない（架空の名前を作らない）。
    """
    kind = _handoff_channel_kind(item)
    # "DM" / "グループDM" / "チャンネル" / ""（unknown＝判定できなかった＝空欄）
    base: str = _handoff.channel_label(kind)
    if kind == "channel":
        name = _handoff_display(
            str(getattr(item, "channel_name_display", "") or ""), names, known_ids
        )
        name = _truncate(name.strip().lstrip("#").strip(), _HANDOFF_CHANNEL_NAME_LEN)
        # 名前が引けない＝「チャンネル」までしか言わない（生 ID を `#` で飾らない）。
        return f"#{name}" if name and _NAME_UNRESOLVED not in name else base
    # 差出人名は `_handoff_names`（user_id → 表示名）を唯一の解決経路にする
    # ＝実名解決の配線がここで実際に効く（配線が切れたら chip から名前が消えて赤くなる）。
    who = _handoff_display(
        names.get(str(getattr(item, "from_user_id", "") or "").strip(), ""), names, known_ids
    )
    who = _truncate(who.strip(), _HANDOFF_SENDER_NAME_LEN)
    if not who or _NAME_UNRESOLVED in who:
        return base  # 実名が引けなかった＝架空の名前を作らず種別だけ
    named = _with_honorific(who)
    return f"{named}（{base}）" if base else named


def _handoff_effort_chip(effort_label: str) -> str:
    """所要時間の chip。**単位だけを置いた「1分」は意味が伝わらない**ので何の時間か言う。

    数字（`EFFORT_BY_KIND` の固定表）は判定層の値をそのまま使い、ここで新しい推定はしない。
    """
    return f"対応に約{effort_label}" if effort_label else ""


def _handoff_card_line(
    card: Any, item: Any, names: dict[str, str], known_ids: frozenset[str]
) -> str:
    """カード 1 件 = 1 行。chip は判定層が確定済みのものを非空だけ並べる。

    行の形は `N. *見出し*（文脈）　— 相手（場所）・時間情報　〔開く〕`。
    **区切りは 2 種類だけ**（`—` が見出しと状況を割り、`・` が状況の中を割る）。旧描画は
    見出しも相手も時間も所要も全部 `・` で同列に並べており、重要度が読めなかった
    （2026-09-11 実物の指摘）。全角スペースで 3 つの塊（見出し／状況／導線）を離す。

    時間の chip は **期限として書かれていれば期限・無ければ経過日数**（1 行に時間軸を
    2 つ出さない）。期限ではない日付語は `date_mention_label` として別に添える
    （経過日数を押し出さない＝「期限」を騙る日付で本当の滞留時間を消さない）。
    """
    line = f"{card.index}. *{_handoff_display(card.headline, names, known_ids)}*"
    context = _handoff_display(card.context, names, known_ids)
    if context:
        line += f"（{context}）"
    # ⚠️ 会話 chip は **既に display 済み**。ここで再度通すと `&` が二重エスケープされる
    #    （実測: "r&d-team" → "#r&amp;amp;d-team"）。display は 1 回だけ。
    chips = [_handoff_channel_chip(item, names, known_ids)]
    chips += [
        _handoff_display(raw, names, known_ids)
        for raw in (
            card.due_label or card.elapsed_label,
            card.date_mention_label,
            _handoff_effort_chip(card.effort_label),
            f"他{card.mentioned_others}名も名指し" if card.mentioned_others >= 1 else "",
            card.fold_reason,
        )
        if raw
    ]
    body = "・".join(chip for chip in chips if chip)
    if body:
        line += f"　— {body}"
    url = _handoff_link(card.permalink)
    if url:
        line += f"　〔<{url}|開く>〕"  # permalink は実 URL なのでエスケープしない
    return line


def _handoff_hidden_line(cards: list[Any], hidden: int) -> str:
    """表示から漏れた件への導線を 1 行（`（表示していない2件は 〔…〕）`）。

    見出しの「7件中5件を表示」だけでは、残りに触れる手段が無く見落とす。
    ⚠️ **URL は既存のものしか使わない**。この digest が持っている URL は各件の permalink
    だけなので、隠れた先頭 1 件の permalink をそのまま導線にする（検索ビューの URL を
    組み立てたりはしない）。使える permalink が無ければ **この行は出さない**（捏造しない）。
    ラベルも実際に開くもの（次の 1 件）を名乗る。
    """
    if hidden <= 0:
        return ""
    for card in cards:
        url = _handoff_link(getattr(card, "permalink", ""))
        if url:
            return f"（表示していない{hidden}件は 〔<{url}|次の1件を開く>〕）"
    return ""


def _handoff_header_line(shown: int, total: int, truncated: bool, summary: str) -> str:
    """見出し。母数は走査打ち切り時に下限値なので「N件以上」と明示する（確定値と混ぜない）。"""
    head = f"💬 *Slack 返信漏れ {total}件以上*" if truncated else f"💬 *Slack 返信漏れ {total}件*"
    if total > shown:
        head += f"（うち{shown}件を表示）"
    return f"{head} ｜ {summary}" if summary else head


def _slack_handoff_count(digest: Any) -> int:
    """💬 の件数（母数）。表示は上限で切るが、ヘッダは走査で見つかった総数を出す。"""
    items = list(getattr(digest, "slack_unread", []) or [])
    return max(int(getattr(digest, "slack_unread_total", 0) or 0), len(items))


def _slack_was_scanned(digest: Any) -> bool:
    """Slack を **実際に走査できたか**（0 件が「無い」なのか「見ていない」なのかの根拠）。

    data 層は fail-open で、未連携・scope 不足・store 障害・API 失敗がすべて
    「空リスト」に潰れる。走査の有無は :attr:`MorningDigestOutput.slack_unread_scanned`
    が唯一の証拠。加えて skill が `slack:` の失敗を errors に積んでいたら未走査扱い。
    """
    if any(str(e).startswith("slack:") for e in (getattr(digest, "errors", []) or [])):
        return False
    return bool(getattr(digest, "slack_unread_scanned", False))


def _slack_handoff_lines(digest: Any, max_items: int = _HANDOFF_MAX_ITEMS) -> list[str]:
    """💬 セクションの行リスト（旧描画・compact 描画で共通）。0 件でも 1 行返す。"""
    items = list(getattr(digest, "slack_unread", []) or [])
    if not items:
        # 「なし」と言い切れるのは **走査できたときだけ**。未走査を「なし」と書くのは
        # 見逃し防止が目的の機能で最も出してはいけない出力（毎朝の嘘になる）。
        return [_HANDOFF_EMPTY_LINE if _slack_was_scanned(digest) else _HANDOFF_UNSCANNED_LINE]
    names = _handoff_names(items)
    known_ids = _handoff_known_ids(items)
    # me_user_id は描画時点で解決できない（email→user_id は API 呼び出し）。判定層は
    # 未指定なら「名指しリストから自分 1 人を引く」フォールバックで他人数を数える。
    triaged = _handoff.triage_slack_handoff(items, now=_handoff_now(), me_user_id=None)
    shown = triaged.cards[:max_items]
    total = _slack_handoff_count(digest)
    truncated = bool(getattr(digest, "slack_unread_truncated", False))

    # 内訳は **取得できた全件**で数える（表示 5 件の内訳を母数の内訳と誤読させない。
    # 隠れた 4 件が全部「あなたの番」でも見出しが変わらないのでは見落とし防止にならない）。
    counts = {b: triaged.count(b) for b in _handoff.BUCKET_ORDER}
    summary = "・".join(
        f"{_handoff.BUCKET_LABELS[b]} {counts[b]}" for b in _handoff.BUCKET_ORDER if counts[b]
    )
    lines = [_handoff_header_line(len(shown), total, truncated, summary)]
    for bucket in _handoff.BUCKET_ORDER:
        cards = [c for c in shown if c.bucket == bucket]
        if not cards:
            continue
        emoji = _HANDOFF_BUCKET_EMOJI[bucket]
        label = _handoff.BUCKET_LABELS[bucket]
        # バケット見出しは「このバケットの取得件数」と「うち何件を並べたか」を分けて出す。
        head = (
            f"{label}（{len(cards)}件）"
            if counts[bucket] == len(cards)
            else f"{label}（{counts[bucket]}件中{len(cards)}件を表示）"
        )
        lines.append("")
        lines.append(f"{emoji} *{head}*")
        # shown はカード列の先頭ぶん、カード列はバケット順に並んでいる＝表示された cards は
        # 必ず cards_in(bucket) の先頭。残りはその続き（この前提が崩れたら導線も崩れる）。
        rest = list(triaged.cards_in(bucket))[len(cards) :]
        hidden_line = _handoff_hidden_line(rest, counts[bucket] - len(cards))
        if hidden_line:
            lines.append(hidden_line)
        for card in cards:
            lines.append(_handoff_card_line(card, items[card.source_index], names, known_ids))
            if card.note:  # 補足行は「原文を見る価値が本当にある件」だけ（判定層が印を付ける）
                lines.append(f"　└ {_handoff_display(card.note, names, known_ids)}")
    lines.append("")
    lines.append(_HANDOFF_FOOTNOTE)
    return [_guard_no_raw_ids(ln, known_ids) for ln in lines]


def _slack_handoff_card_blocks(
    digest: Any, max_items: int = _HANDOFF_MAX_ITEMS
) -> list[dict[str, Any]]:
    """💬 セクションを「1 カード = 1 section + ☑️ accessory」で描く（ack ボタン ON 時のみ）。

    ボタン OFF のときは呼ばれない。OFF 時の描画（`_slack_handoff_lines` → 1 つの section）は
    1 バイトも変えない＝この機能を入れる前と完全に同じ DM が届く。

    ボタンを `actions` ブロックではなく section の `accessory` に載せるのは blocks 予算のため
    （`actions` を足すと 1 カードにつき 2 ブロック消費する）。バケット見出しは、そのバケット
    最初のカードの本文へ前置して畳み込む（見出し専用ブロックを立てない）。

    ⚠️ 文面（見出し・件数の言い回し・フッター）は行版と同一に保つこと。ここだけ言葉が
    変わると、flag の ON/OFF で「昨日と違うことを言う朝ダイジェスト」になる。
    """
    items = list(getattr(digest, "slack_unread", []) or [])
    if not items:
        line = _HANDOFF_EMPTY_LINE if _slack_was_scanned(digest) else _HANDOFF_UNSCANNED_LINE
        return [{"type": "section", "text": {"type": "mrkdwn", "text": line}}]
    names = _handoff_names(items)
    known_ids = _handoff_known_ids(items)
    triaged = _handoff.triage_slack_handoff(items, now=_handoff_now(), me_user_id=None)
    shown = triaged.cards[:max_items]
    total = _slack_handoff_count(digest)
    truncated = bool(getattr(digest, "slack_unread_truncated", False))
    counts = {b: triaged.count(b) for b in _handoff.BUCKET_ORDER}
    summary = "・".join(
        f"{_handoff.BUCKET_LABELS[b]} {counts[b]}" for b in _handoff.BUCKET_ORDER if counts[b]
    )

    def _section(text: str, accessory: dict[str, Any] | None = None) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _guard_no_raw_ids(text, known_ids)},
        }
        if accessory is not None:
            block["accessory"] = accessory
        return block

    blocks: list[dict[str, Any]] = [
        _section(_handoff_header_line(len(shown), total, truncated, summary))
    ]
    for bucket in _handoff.BUCKET_ORDER:
        cards = [c for c in shown if c.bucket == bucket]
        if not cards:
            continue
        emoji = _HANDOFF_BUCKET_EMOJI[bucket]
        label = _handoff.BUCKET_LABELS[bucket]
        head = (
            f"{label}（{len(cards)}件）"
            if counts[bucket] == len(cards)
            else f"{label}（{counts[bucket]}件中{len(cards)}件を表示）"
        )
        # shown はカード列の先頭ぶん、カード列はバケット順に並んでいる＝表示された cards は
        # 必ず cards_in(bucket) の先頭。残りはその続き（この前提が崩れたら導線も崩れる）。
        rest = list(triaged.cards_in(bucket))[len(cards) :]
        hidden_line = _handoff_hidden_line(rest, counts[bucket] - len(cards))
        pending_head: str | None = f"{emoji} *{head}*"
        if hidden_line:
            pending_head += f"\n{hidden_line}"
        for card in cards:
            body = _handoff_card_line(card, items[card.source_index], names, known_ids)
            if card.note:
                body += f"\n　└ {_handoff_display(card.note, names, known_ids)}"
            if pending_head is not None:
                body = f"{pending_head}\n{body}"
                pending_head = None
            token = getattr(items[card.source_index], "ack_token", "")
            accessory = (
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "☑️ 確認済み", "emoji": True},
                    "action_id": _ACTION_DIGEST_ACK,
                    "value": token,
                }
                if token
                else None
            )
            blocks.append(_section(body, accessory))
    blocks.append(_section(_HANDOFF_FOOTNOTE))
    return blocks


def _slack_handoff_block_section(
    digest: Any, max_items: int = _HANDOFF_MAX_ITEMS
) -> list[dict[str, Any]]:
    """`_slack_handoff_card_blocks` の **fail-safe 境界**（行版 `_slack_handoff_section` と同役）。

    判定層は 800 行超の決定論ロジックを任意のユーザー本文に対して走らせる。そこで想定外の
    例外が出たときに、メールも予定も含む DM ごと落とす（`_process_user` の except が
    `return "error"` ＝ 1 通も届かない）のは割に合わない。ここで受け止めて 💬 の 1 行へ
    縮退させ、他セクションを巻き添えにしない。
    """
    try:
        return _slack_handoff_card_blocks(digest, max_items)
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: 💬 ブロック描画失敗 {type(exc).__name__}",
            flush=True,
        )
        return [{"type": "section", "text": {"type": "mrkdwn", "text": _HANDOFF_FAILED_LINE}}]


def _push_slack_handoff(
    blocks: list[dict[str, Any]], digest: Any, max_items: int = _HANDOFF_MAX_ITEMS
) -> None:
    """💬 セクションを積む。ack ボタン OFF なら従来どおりの行描画に完全に一致させる。"""
    if _ack_button_enabled():
        blocks.extend(_slack_handoff_block_section(digest, max_items))
    else:
        _push_section_lines(blocks, _slack_handoff_section(digest, max_items))


def _ack_all_blocks(digest: Any) -> list[dict[str, Any]]:
    """末尾の「☑️ 全部確認した」。token が空なら何も積まない（サイズ超過/機能OFF）。"""
    token = getattr(digest, "ack_all_token", "")
    if not token or not _ack_button_enabled():
        return []
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "☑️ 全部確認した", "emoji": True},
                    "action_id": _ACTION_DIGEST_ACK,
                    "value": token,
                }
            ],
        }
    ]


def _slack_handoff_section(digest: Any, max_items: int = _HANDOFF_MAX_ITEMS) -> list[str]:
    """💬 セクションの描画（**このセクションだけの fail-safe**）。

    判定層は 800 行を超える決定論ロジックを任意のユーザー本文に対して走らせる。そこで
    想定外の例外が出たときに、メールも予定もリマインドも含む DM ごと落とす
    （`_process_user` の except が `return "error"` ＝ 1 通も届かない）のは割に合わない。
    ここで受け止めて 💬 の 1 行へ縮退させ、他セクションを巻き添えにしない。
    """
    try:
        return _slack_handoff_lines(digest, max_items)
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: 💬 描画失敗 {type(exc).__name__}",
            file=sys.stderr,
        )
        return [_HANDOFF_FAILED_LINE]


def _push_section_lines(blocks: list[dict[str, Any]], lines: list[str]) -> None:
    """行リストを 2800 字以内の section に分割して積む（3000 字上限の保険）。"""
    buf: list[str] = []
    size = 0
    for ln in lines:
        if buf and size + len(ln) + 1 > _COMPACT_SECTION_CHARS:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(buf)}})
            buf, size = [], 0
        buf.append(ln)
        size += len(ln) + 1
    if buf:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(buf)}})


def _fmt_meeting_button_time(start_iso: str | None) -> str:
    """meeting_start(ISO) → 「7/15 14:00」（📅ボタン文言用・JST）。不正は "" で汎用文言に落とす。"""
    # ⚠️ offset 無し（naive）はコンテナのローカル TZ（本番 UTC）解釈で 9 時間ずれるため、
    #    JST を明示的に付ける（parse_jst_datetime が naive を JST とみなす）。
    dt = _calwin.parse_jst_datetime(start_iso)
    if dt is None:
        return ""
    return f"{dt.month}/{dt.day} {dt.strftime('%H:%M')}"


def _digest_date(digest: Any) -> _dt.date:
    """予定セクションの対象日（JST）。skill が載せた calendar_date を最優先で使う。

    旧バージョンの output（calendar_date 無し）でも描画できるよう、空なら JST の今日。
    """
    raw = str(getattr(digest, "calendar_date", "") or "").strip()
    return _calwin.parse_jst_date(raw) or _calwin.now_jst().date()


def _fmt_event_time(
    start_at: str | None,
    end_at: str | None,
    *,
    all_day: bool | None = None,
    target_date: _dt.date | None = None,
) -> str:
    """ISO 文字列を '10:00–11:00' / '終日' に整形する（JST 明示）。

    `target_date` を渡すと、その日と違う予定には日付を前置する（例 "8/21(金) 終日"）。
    複数日の終日は "終日(8/19–8/21)"（Google の排他的 end.date を -1 日した最終日）。
    """
    return _calwin.event_when_label(start_at, end_at, all_day=all_day, target_date=target_date)


def _mail_line(m: Any) -> tuple[str, str]:
    """1 メール（スレッド）の (件名section本文, 相手) を作る。display は本人 DM のみ・ログ厳禁。"""
    subj = _slack_escape(getattr(m, "subject_display", "") or m.subject_scrubbed or "(件名なし)")
    who = _slack_escape(getattr(m, "counterpart_display", "") or m.counterpart_masked)
    return subj, who


def _reply_buttons(m: Any) -> list[dict[str, Any]]:
    """要返信メール 1 件のボタン行：未作成のみ [✏️ 下書きを作成]、常に [✅ 下書きを確認]。

    作成済みの下書きはスレッドを開けばそこに表示されるので、行内は「確認」1つで足りる。
    旧「📨 下書きを開く」（＝下書きフォルダ直行）は重複のため廃止し、一覧は DM 末尾に集約する。
    """
    btns: list[dict[str, Any]] = []
    thread_url = getattr(m, "thread_gmail_url", "") or _GMAIL_INBOX_URL
    has_draft = bool(getattr(m, "has_draft", False))
    draft_token = getattr(m, "draft_token", "")
    if not has_draft and draft_token:
        # 下書き未作成時のみ。押下 identity/value は plugin が heartbeat run へ one-use 束縛する。
        btns.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✏️ 下書きを作成", "emoji": True},
                "action_id": _ACTION_MAIL_DRAFT,
                "value": draft_token,
                "style": "primary",
            }
        )
    btns.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✅ 下書きを確認", "emoji": True},
            "url": thread_url,  # そのスレッドへワンタップ直行（url ボタン＝非発火）
        }
    )
    # 📅 確定MTGのカレンダー登録（v0.3 Task3・既定OFF）。日時確定×To本人のみ token が発行される。
    # ボタン文言に登録される日時を明示する（何が登録されるか見えない「盲目の同意」を防ぐ。
    # メール本文＝攻撃者制御値を LLM が抽出した日時なので、押す前に本人が検証できることが HITL の実質）。
    event_token = getattr(m, "event_token", "")
    if event_token and _calendar_button_enabled():
        when = _fmt_meeting_button_time(getattr(m, "meeting_start", None))
        label = f"📅 {when} に登録" if when else "📅 カレンダーに登録"
        btns.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": label[:75], "emoji": True},
                "action_id": _ACTION_CALENDAR_EVENT,
                "value": event_token,
            }
        )
    # 🗓 日程打診への候補提案（v0.3 Task4・既定OFF）。相手が日程を求めている×To本人のみ。
    # value は draft_token（thread_id 由来・schedule_propose がスレッドへの返信下書きに使う）。
    if getattr(m, "scheduling_request", False) and draft_token and _schedule_button_enabled():
        btns.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🗓 日程候補を提案", "emoji": True},
                "action_id": _ACTION_SCHEDULE_PROPOSE,
                "value": draft_token,
            }
        )
    # ☑️ 確認済み（既定OFF）。押すと翌朝以降このスレッドを隠す（新着が来れば再表示）。
    # ⚠️ 同じ行の「✅ 下書きを確認」は Gmail を開く url ボタン。絵文字と語尾（〜にする）で
    # 「開く」と「状態を変える」を見分けられるようにしている。✅ を再利用しないこと。
    ack_token = getattr(m, "ack_token", "")
    if ack_token and _ack_button_enabled():
        btns.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "☑️ 確認済みにする", "emoji": True},
                "action_id": _ACTION_DIGEST_ACK,
                "value": ack_token,
            }
        )
    return btns


# ---------------------------------------------------------------------------
# F0: 連携切れの見える化（MORNING_DIGEST_FETCH_STATUS_EMAILS・既定 OFF）
#
# 取れなかった節を「新着なし」「予定なし」と書かない。原因が本人の再連携で直るもの
# （失効・権限不足）なら冒頭に案内を 1 つだけ出し、一時的な失敗なら再連携へ誘導しない
# （設定不備の朝に全員へ「再連携して」と出す事故を作らない）。
# ⚠️ 案内文に認可 URL は貼らない（30 分で失効し、長い URL は再タイプ事故の実績がある）。
#    既存の前例どおり「この DM で『連携』」＝ Aico が正規のリンクを出す経路へ寄せる。
# ⚠️ 文言はすべて固定。メール本文・件名・例外の文面は 1 文字も入れない。
# ---------------------------------------------------------------------------

#: 取得状態 → 「確認できませんでした」の括弧内の理由（利用者向け）。
_FETCH_REASON_TEXT: dict[str, str] = {
    FETCH_TOKEN_EXPIRED: "連携切れ",
    FETCH_SCOPE_MISSING: "権限不足",
    FETCH_TEMPORARY: "取得・整理の途中で失敗しました",
    FETCH_UNKNOWN: "取得できたか確かめられませんでした",
}
_FETCH_SECTION_NAME: dict[str, str] = {"mail": "メール", "calendar": "予定"}
_F0_NOTE_TRUNCATED = "_表示しきれない項目があります。Gmail / カレンダーで確認してください。_"

#: 50 ブロック上限の最終ガードで「削ってよい塊」の順位（小さいほど先に削る）。
#: F0 の案内・取得状態の行・今日の予定・末尾は **削らない**（守る側に登録する）。
_DROP_MAIL_UNREAD = 0
_DROP_MAIL_EXTRA = 1  # 〈他N件〉・📁 下書き一覧
_DROP_MAIL_ITEM = 2  # 要返信 1 件（section + ボタン行）。後ろの件から削る
_DROP_SLACK = 3
_DROP_BRIEF = 4


def _fetch_status_enabled(user_email: str) -> bool:
    """この人に F0 の描画（確認できませんでした・案内・節の優先順位）を出すか。

    ``MORNING_DIGEST_FETCH_STATUS_EMAILS``: 空＝全員 OFF（従来の描画と 1 バイトも変わらない）／
    カンマ区切りの email ／ ``*`` で全員。
    """
    raw = os.environ.get("MORNING_DIGEST_FETCH_STATUS_EMAILS", "").strip()
    if not raw:
        return False
    allowed = {e.strip().lower() for e in raw.split(",") if e.strip()}
    return "*" in allowed or (user_email or "").strip().lower() in allowed


def _fetch_state(digest: Any, section: str) -> str:
    """``mail_fetch`` / ``calendar_fetch``。知らない値・欠落は unknown（＝確認できなかった側）。"""
    raw = str(getattr(digest, f"{section}_fetch", FETCH_UNKNOWN) or FETCH_UNKNOWN)
    return raw if raw == FETCH_OK or raw in _FETCH_REASON_TEXT else FETCH_UNKNOWN


def _fetch_what(sections: list[str]) -> str:
    return "と".join(_FETCH_SECTION_NAME[s] for s in sections)


def _f0_problem(digest: Any) -> str:
    """通知プレビュー用の一言（問題が無ければ空）。件数以外の中身は入れない。"""
    failed = [s for s in ("mail", "calendar") if _fetch_state(digest, s) != FETCH_OK]
    if not failed:
        return ""
    what = _fetch_what(failed)
    states = {_fetch_state(digest, s) for s in failed}
    if FETCH_TOKEN_EXPIRED in states:
        return f"Google の連携が切れています（{what}を確認できませんでした）"
    if FETCH_SCOPE_MISSING in states:
        return f"{what}を確認する権限が足りません"
    return f"{what}を確認できませんでした"


def _f0_notice_text(digest: Any) -> str:
    """冒頭の案内（本人の再連携で直るときだけ・メールと予定が両方だめでも 1 つにまとめる）。"""
    expired = [s for s in ("mail", "calendar") if _fetch_state(digest, s) == FETCH_TOKEN_EXPIRED]
    if expired:
        return (
            f"⚠️ *Google の連携が切れているため、{_fetch_what(expired)}を確認できませんでした*"
            "（パスワードの変更などで無効になることがあります）。\n"
            "この DM で「連携」と送っていただければ、Aico が再連携のリンクをお出しします"
            "（1 分・「許可」を押すだけ）。"
        )
    scope = [s for s in ("mail", "calendar") if _fetch_state(digest, s) == FETCH_SCOPE_MISSING]
    if scope:
        return (
            f"⚠️ *{_fetch_what(scope)}を確認する権限が足りないため、確認できませんでした*。\n"
            "この DM で「連携」と送り、表示される画面ですべての項目にチェックを入れて"
            "許可してください。"
        )
    return ""


def _mail_unavailable_text(digest: Any) -> str:
    state = _fetch_state(digest, "mail")
    text = (
        f"⚠️ *メール*: 確認できませんでした"
        f"（{_FETCH_REASON_TEXT[state]}。新着が無いという意味ではありません）"
    )
    if state not in FETCH_NEEDS_RECONNECT:
        text += f"  <{_GMAIL_INBOX_URL}|受信トレイを開く>"
    return text


def _mail_threads_failed_text(n: int, *, has_items: bool) -> str:
    if has_items:
        return f"⚠️ ほか{n}件のメールは読み込めませんでした（<{_GMAIL_INBOX_URL}|受信トレイで見る>）"
    return (
        f"⚠️ *メール*: {n}件のメールを読み込めませんでした（新着が無いという意味ではありません）"
        f"  <{_GMAIL_INBOX_URL}|受信トレイを開く>"
    )


def _calendar_unavailable_text(digest: Any, day_label: str) -> str:
    state = _fetch_state(digest, "calendar")
    tail = "予定が無いという意味ではありません"
    if _reminders_enabled():
        tail += "。本日の予定リマインドもお送りできません"
    text = f"⚠️ *{day_label} の予定*: 確認できませんでした（{_FETCH_REASON_TEXT[state]}。{tail}）"
    if state not in FETCH_NEEDS_RECONNECT:
        text += f"  <{_CALENDAR_URL}|カレンダーを開く>"
    return text


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


class _BlockUnits:
    """50 ブロックの最終ガード用に「どの塊を、どの順で削ってよいか」を記録する。

    描画の本体は従来どおり blocks へ積むだけで、ここは添え字の範囲を控えるだけ
    （＝F0 OFF のときは何も使わず、描画は 1 バイトも変わらない）。
    """

    __slots__ = ("droppable", "protected")

    def __init__(self) -> None:
        self.droppable: list[tuple[int, int, int]] = []  # (順位, start, end)
        self.protected: list[tuple[int, int]] = []

    def drop(self, rank: int, start: int, end: int) -> None:
        if end > start:
            self.droppable.append((rank, start, end))

    def protect(self, start: int, end: int) -> None:
        if end > start:
            self.protected.append((start, end))


def _fit_blocks(
    body: list[dict[str, Any]],
    units: _BlockUnits,
    tail: list[dict[str, Any]],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """F0: 50 ブロック上限の最終ガード（節の優先順位つき）。

    従来の compact は末尾から切っていた＝「今日の予定」が真っ先に消える。ここでは
    メールの一覧（未確認 → 要返信の後ろの件）→ Slack → 事例ブリーフ の順に削り、
    F0 の案内・取得状態の行・今日の予定・末尾（☑️一括・脚注）は残す。
    """
    budget = (_COMPACT_MAX_BLOCKS if limit is None else limit) - len(tail)
    if len(body) <= budget:
        return body + tail
    over = len(body) - (budget - 1)  # 1 枠は「表示しきれない」の注記に使う
    dropped: set[int] = set()
    for _rank, start, end in sorted(units.droppable, key=lambda u: (u[0], -u[1])):
        if over <= 0:
            break
        span = set(range(start, end)) - dropped
        dropped |= span
        over -= len(span)
    if over > 0:
        # 想定外（削ってよい塊を全部削っても収まらない）。守る塊以外を後ろから落とす。
        keep = {i for start, end in units.protected for i in range(start, end)}
        for i in range(len(body) - 1, -1, -1):
            if over <= 0:
                break
            if i not in dropped and i not in keep and i >= 2:  # 見出し 2 ブロックは残す
                dropped.add(i)
                over -= 1
    kept = [b for i, b in enumerate(body) if i not in dropped]
    kept.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _F0_NOTE_TRUNCATED}]})
    return kept + tail


def _push_f0_notice(blocks: list[dict[str, Any]], units: _BlockUnits, digest: Any) -> None:
    """冒頭の案内（見出しの直後）。削らない塊として登録する。"""
    notice = _f0_notice_text(digest)
    if not notice:
        return
    start = len(blocks)
    blocks.append(_section(notice))
    blocks.append({"type": "divider"})
    units.protect(start, len(blocks))


def _push_mail_status(
    blocks: list[dict[str, Any]],
    units: _BlockUnits,
    digest: Any,
    *,
    f0: bool,
    has_items: bool,
) -> None:
    """メール節の締め（従来の「📭 新着なし」の位置）。F0 OFF は従来と完全に同じ。"""
    if not f0:
        if not has_items:
            blocks.append(_section("📭 *メール*: 新着なし"))
            blocks.append({"type": "divider"})
        return
    start = len(blocks)
    threads_failed = int(getattr(digest, "mail_threads_failed", 0) or 0)
    if _fetch_state(digest, "mail") != FETCH_OK:
        blocks.append(_section(_mail_unavailable_text(digest)))
        blocks.append({"type": "divider"})
    elif threads_failed > 0:
        blocks.append(_section(_mail_threads_failed_text(threads_failed, has_items=has_items)))
        blocks.append({"type": "divider"})
    elif not has_items:
        blocks.append(_section("📭 *メール*: 新着なし"))
        blocks.append({"type": "divider"})
    units.protect(start, len(blocks))


def _push_slack_handoff_units(
    blocks: list[dict[str, Any]],
    units: _BlockUnits,
    digest: Any,
    max_items: int = _HANDOFF_MAX_ITEMS,
) -> None:
    """💬 節を積み、削ってよい塊を控える（☑️ボタン時はカード 1 枚ずつ）。"""
    start = len(blocks)
    _push_slack_handoff(blocks, digest, max_items)
    end = len(blocks)
    if _ack_button_enabled() and end - start > 2:
        # [見出し, カード…, 脚注]。見出しと脚注は残し、カードを後ろから削る。
        for i in range(start + 1, end - 1):
            units.drop(_DROP_SLACK, i, i + 1)
    else:
        units.drop(_DROP_SLACK, start, end)


def _footer_text(digest: Any) -> str:
    """末尾の説明文。下書きの一文は **実際の作り方（draft_mode）** に合わせる。

    本番は朝に自動で作り置き（DRAFT_ON_DEMAND_ONLY=false）なのに、長く「ボタンを押した時に
    生成」と書いていた（09-29 裁定で実態に合わせる）。ボタン押下時のみの設定では従来の文言。
    """
    head = "_Aico｜本人だけに届く DM です（件名・相手は実名表示／監査ログ側はマスク）。"
    mode = str(getattr(digest, "draft_mode", "auto") or "auto")
    if mode == "on_demand":
        return head + "下書きはボタンを押した時に生成し、送信はされません（手動送信）。_"
    if mode == "off":
        return head + "_"
    limit = int(getattr(digest, "draft_limit", 0) or 0)
    cap = f"最大 {limit} 件・" if limit > 0 else ""
    # 社内だけのやり取りを外したときは除外にも書く（10-01 BU1 ヒアリング・#504）。
    internal = "社内だけのやり取りと" if getattr(digest, "draft_skip_internal", False) else ""
    return head + (
        f"重要で本人宛てのメールには、Aico が返信の下書きを Gmail に作っておきます"
        f"（{cap}{internal}日程の打診は除く）。送信はしません（送るかはご自身で）。_"
    )


def _footer_blocks(digest: Any) -> list[dict[str, Any]]:
    return [
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _footer_text(digest)}]},
    ]


def _format_block_kit(digest: Any, user_email: str) -> tuple[str, list[dict[str, Any]]]:
    """MorningDigestOutput → Slack Block Kit（要返信→未開封→当日の予定。下書きはボタン生成）。"""
    f0 = _fetch_status_enabled(user_email)
    # fallback text は通知プレビュー用。slack_bot の chat_update が同一文字列を再送するため
    # ここは固定のまま（日付明示は本文側＝blocks で行う）。F0 の対象者で取得に失敗した日だけ
    # 先頭に ⚠️ を付ける（プレビューだけで「今日は取れていない」と分かるように）。
    text = "メールと本日の予定をお送りします。"
    problem = _f0_problem(digest) if f0 else ""
    if problem:
        text = f"⚠️ {problem}。{text}"
    day = _digest_date(digest)
    day_label = _calwin.fmt_jst_date(day)  # 例 "8/20(木)"
    units = _BlockUnits()

    mail_items = list(getattr(digest, "mail_digest", []) or [])

    # 要返信メール ＝ high かつ「本人が To に直接いる」（＝自分が返信すべきもの）。
    # To に自分がいない（CC のみ/メーリス宛）メールは high でも要返信に出さず未開封へ回す。
    def _is_reply(m: Any) -> bool:
        return m.importance == "high" and bool(getattr(m, "to_self", False))

    high = [m for m in mail_items if _is_reply(m)]
    # 未開封 ＝ 未読(UNREAD) かつ 要返信に出ていないもの（To に自分がいない高重要もここ・閲覧のみ）。
    unread = [m for m in mail_items if getattr(m, "is_unread", False) and not _is_reply(m)]
    cal_items = list(getattr(digest, "calendar_events", []) or [])

    # 冒頭の枕詞（飾らない一文）。
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"📬 *メールと {day_label} の予定をお送りします。*",
            },
        },
        {"type": "divider"},
    ]
    units.protect(0, len(blocks))
    if f0:
        _push_f0_notice(blocks, units, digest)

    # --- 🔴 要返信メール（最大10件・各件にボタン）---
    if high:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"🔴 *要返信メール（{len(high)}件）*"},
            }
        )
        for m in high[:10]:
            item_start = len(blocks)
            subj, who = _mail_line(m)
            tag = f"`{m.sender_label}` " if getattr(m, "sender_label", "") else ""
            thr = f" 〔{m.thread_count}通〕" if getattr(m, "thread_count", 1) > 1 else ""
            body = f"{tag}*{subj}*{thr} — {who}"
            if m.summary:
                body += f"\n_{_slack_escape(m.summary)}_"
            if getattr(m, "deadline", None):
                body += f"\n⏰ 期限: {_slack_escape(str(m.deadline))}"
            if getattr(m, "ask", ""):
                body += f"\n📌 依頼: {_slack_escape(m.ask)}"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
            blocks.append({"type": "actions", "elements": _reply_buttons(m)})
            units.drop(_DROP_MAIL_ITEM, item_start, len(blocks))
        # 作り置き済みの下書きが1件でもあれば、末尾に「一覧をまとめて開く」を1つだけ集約する
        # （行内の重複を排し、下書きフォルダへの導線はここに一本化）。
        if any(getattr(m, "has_draft", False) for m in high):
            units.drop(_DROP_MAIL_EXTRA, len(blocks), len(blocks) + 1)
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "📁 下書き一覧を開く",
                                "emoji": True,
                            },
                            "url": _GMAIL_DRAFTS_URL,
                        }
                    ],
                }
            )
        blocks.append({"type": "divider"})

    # --- 📬 未確認（未読・最大5件＋「他N件」・件名/相手＋AI要約）---
    if unread:
        unread_start = len(blocks)
        lines = [f"📬 *未確認（{len(unread)}件）*"]
        for m in unread[:5]:
            subj, who = _mail_line(m)
            line = f"• *{subj}* — {who}"
            if m.summary:
                line += f"\n　_{_slack_escape(m.summary)}_"
            lines.append(line)
        rem = max(0, len(unread) - 5)
        if rem:
            lines.append(f"• 〈他{rem}件〉")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        blocks.append({"type": "divider"})
        units.drop(_DROP_MAIL_UNREAD, unread_start, len(blocks))

    # 「📭 新着なし」の位置。F0 の対象者は、取れなかった日を「新着なし」と書かない。
    _push_mail_status(blocks, units, digest, f0=f0, has_items=bool(high or unread))

    # --- 💬 Slack 返信漏れ（判定は _shared/slack_handoff・ここは並べるだけ。
    #     display は本人 DM のみ・ログ厳禁 G3/G7）---
    _push_slack_handoff_units(blocks, units, digest)
    blocks.append({"type": "divider"})

    # --- 📌 本日の社外MTG 事例ブリーフ（既定OFF・節ごと消える設計）---
    brief_start = len(blocks)
    _push_brief_section(blocks, digest)
    units.drop(_DROP_BRIEF, brief_start, len(blocks))

    # --- 📅 当日の予定（予定・会議室・会議リンク。display は本人 DM のみ・ログ厳禁 G3/G7）---
    # 見出しは「今日」ではなく実日付を出す（2026-08-20 の日付ずれで「今日」表記が誤りを
    # 隠したため。行側も対象日と違う予定には日付を前置する）。
    cal_start = len(blocks)
    if cal_items:
        lines = [f"📅 *{day_label} の予定（{len(cal_items)}件）*"]
        for ev in cal_items[:10]:
            when = _fmt_event_time(
                getattr(ev, "start_at", None),
                getattr(ev, "end_at", None),
                all_day=getattr(ev, "all_day", None),
                target_date=day,
            )
            title = _slack_escape(
                getattr(ev, "summary_display", "")
                or getattr(ev, "summary_scrubbed", "")
                or "(無題)"
            )
            loc = getattr(ev, "location_display", "") or getattr(ev, "location_scrubbed", "")
            line = f"• `{when}`  {title}"
            if loc:
                line += f"  〔{_slack_escape(loc)}〕"
            url = getattr(ev, "meeting_url", "")
            if url:
                line += f"  <{url}|🔗参加>"  # 会議リンクは実 URL なのでエスケープしない
            lines.append(line)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    elif f0 and _fetch_state(digest, "calendar") != FETCH_OK:
        # 取れなかった日を「予定なし」と書かない（リマインドも登録されない日）。
        blocks.append(_section(_calendar_unavailable_text(digest, day_label)))
    else:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"📅 *{day_label} の予定*: なし"},
            }
        )
    units.protect(cal_start, len(blocks))

    # --- ☑️ 全部確認した（既定OFF・脚注の前）＋ 脚注（DLP 注記・下書きの作り方）---
    tail = _ack_all_blocks(digest) + _footer_blocks(digest)
    if f0:
        # 50 ブロック上限の最終ガード（節の優先順位つき・案内と今日の予定は削らない）。
        return text, _fit_blocks(blocks, units, tail)
    blocks.extend(tail)
    return text, blocks


def _format_block_kit_compact(
    digest: Any,
    user_email: str,
    prefs: _prefs.DigestPreferences = _prefs.DEFAULT_PREFERENCES,
) -> tuple[str, list[dict[str, Any]]]:
    """密度優先の Block Kit（MORNING_DIGEST_COMPACT=1・2026-07-13 パイロットFB対応）。

    設計原則: DM は「索引」・詳細は元アプリ（Gmail/Slack/Calendar）。1件=1行、
    要約・本文プレビューは出さない（要返信のみ ⏰期限/📌依頼 の構造化1行を許可・
    どちらも無ければ要約60字で代替）。全セクションで「見出し=全数・表示=上限・
    超過=〈他N件〉+リンク」を統一。ボタン群（_reply_buttons）・脚注・PII 規約
    （display は本人 DM のみ・ログ厳禁 G3/G7）は旧描画と共通。
    """
    mail_items = list(getattr(digest, "mail_digest", []) or [])

    def _is_reply(m: Any) -> bool:
        return m.importance == "high" and bool(getattr(m, "to_self", False))

    high = [m for m in mail_items if _is_reply(m)]
    unread = [m for m in mail_items if getattr(m, "is_unread", False) and not _is_reply(m)]
    cal_items = list(getattr(digest, "calendar_events", []) or [])
    slack_total = _slack_handoff_count(digest)
    # 本人の設定（digest_settings）。既定なら従来と 1 バイトも変わらない。
    show_reply, show_unread = prefs.shows("reply"), prefs.shows("unread")
    show_slack, show_cal = prefs.shows("slack"), prefs.shows("calendar")
    lim_reply, lim_unread = prefs.limit("reply"), prefs.limit("unread")
    lim_cal = prefs.limit("calendar")
    f0 = _fetch_status_enabled(user_email)
    units = _BlockUnits()

    # ヘッダも予定セクションも同じ「対象日」を使う（描画のたびに now を読むと、
    # 日付をまたぐ再描画でヘッダと予定の日付がズレる）。
    day = _digest_date(digest)
    day_label = _calwin.fmt_jst_date(day)  # 例 "8/20(木)"
    # 件数の表示。F0 の対象者で取れなかった節は「0」ではなく「–」（0 件と区別する）。
    mail_ok = not f0 or _fetch_state(digest, "mail") == FETCH_OK
    cal_ok = not f0 or _fetch_state(digest, "calendar") == FETCH_OK
    n_high = str(len(high)) if mail_ok else "–"
    n_unread = str(len(unread)) if mail_ok else "–"
    n_cal = str(len(cal_items)) if cal_ok else "–"
    # fallback text は通知プレビューに出るため件数のみ（PII ゼロ）。
    # 見出しの件数は **載せる欄だけ**（本人が消した欄の件数を毎朝見せない）。
    text_parts = [
        part
        for shown, part in (
            (show_reply, f"要返信{n_high}"),
            (show_unread, f"未確認{n_unread}"),
            (show_slack, f"Slack{slack_total}"),
            (show_cal, f"予定{n_cal}"),
        )
        if shown
    ]
    head_parts = [
        part
        for shown, part in (
            (show_reply, f"🔴{n_high}"),
            (show_unread, f"📬{n_unread}"),
            (show_slack, f"💬{slack_total}"),
            (show_cal, f"📅{n_cal}"),
        )
        if shown
    ]
    text = "朝ダイジェスト｜" + ("・".join(text_parts) or "設定により表示する欄なし")
    problem = _f0_problem(digest) if f0 else ""
    if problem:
        text = f"朝ダイジェスト｜⚠️ {problem}｜{text.split('｜', 1)[1]}"
    header = f"📬 *{day_label} の朝ダイジェスト*" + (
        "｜" + "・".join(head_parts) if head_parts else ""
    )
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "divider"},
    ]
    units.protect(0, len(blocks))
    if f0:
        _push_f0_notice(blocks, units, digest)

    def _push_lines(lines: list[str]) -> None:
        _push_section_lines(blocks, lines)

    def _subj_who(m: Any) -> tuple[str, str]:
        subj = _slack_escape(
            _truncate(
                getattr(m, "subject_display", "") or m.subject_scrubbed or "(件名なし)",
                _COMPACT_SUBJ_LEN,
            )
        )
        who = _slack_escape(getattr(m, "counterpart_display", "") or m.counterpart_masked)
        return subj, who

    # --- 🔴 要返信（既定 最大5件・各件にボタン）---
    if high and show_reply:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"🔴 *要返信（{len(high)}件）*"}}
        )
        for m in high[:lim_reply]:
            item_start = len(blocks)
            subj, who = _subj_who(m)
            tag = f"`{m.sender_label}` " if getattr(m, "sender_label", "") else ""
            thr = f"〔{m.thread_count}通〕" if getattr(m, "thread_count", 1) > 1 else ""
            body = f"{tag}{who}: *{subj}*{thr}"
            meta: list[str] = []
            if getattr(m, "deadline", None):
                meta.append(f"⏰ {_slack_escape(_truncate(str(m.deadline), 40))}")
            if getattr(m, "ask", ""):
                meta.append(f"📌 {_slack_escape(_truncate(m.ask, _COMPACT_SUBJ_LEN))}")
            if meta:
                body += "\n" + " ｜ ".join(meta)
            elif m.summary:
                body += f"\n_{_slack_escape(_truncate(m.summary, _COMPACT_SUBJ_LEN))}_"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
            blocks.append({"type": "actions", "elements": _reply_buttons(m)})
            units.drop(_DROP_MAIL_ITEM, item_start, len(blocks))
        rem = len(high) - lim_reply
        if rem > 0:
            units.drop(_DROP_MAIL_EXTRA, len(blocks), len(blocks) + 1)
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"〈他{rem}件〉 <{_GMAIL_INBOX_URL}|受信トレイで見る>",
                    },
                }
            )
        if any(getattr(m, "has_draft", False) for m in high):
            units.drop(_DROP_MAIL_EXTRA, len(blocks), len(blocks) + 1)
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "📁 下書き一覧を開く",
                                "emoji": True,
                            },
                            "url": _GMAIL_DRAFTS_URL,
                        }
                    ],
                }
            )
        blocks.append({"type": "divider"})

    # --- 📬 未確認（既定 最大5件・1件=1行・要約なし）---
    if unread and show_unread:
        unread_start = len(blocks)
        lines = [f"📬 *未確認（{len(unread)}件）*"]
        for m in unread[:lim_unread]:
            subj, who = _subj_who(m)
            lines.append(f"• {who}: *{subj}*")
        rem = len(unread) - lim_unread
        if rem > 0:
            lines.append(f"• 〈他{rem}件〉 <{_GMAIL_INBOX_URL}|受信トレイで見る>")
        _push_lines(lines)
        blocks.append({"type": "divider"})
        units.drop(_DROP_MAIL_UNREAD, unread_start, len(blocks))

    # 「📭 新着なし」の位置。F0 の対象者は、取れなかった日を「新着なし」と書かない。
    # メールの欄を両方消した人には出さない。片方だけ消した人は、消した欄に件数があれば
    # 「新着なし」と言わない（has_items は消した欄も含めて数える＝嘘の「なし」を書かない）。
    if show_reply or show_unread:
        _push_mail_status(blocks, units, digest, f0=f0, has_items=bool(high or unread))

    # --- 💬 Slack 返信漏れ（判定は _shared/slack_handoff・ここは並べるだけ。
    #     display は本人 DM のみ・ログ厳禁 G3/G7）---
    if show_slack:
        _push_slack_handoff_units(blocks, units, digest, prefs.limit("slack"))
        blocks.append({"type": "divider"})

    # --- 📌 本日の社外MTG 事例ブリーフ（既定OFF・節ごと消える設計）---
    if prefs.shows("brief"):
        brief_start = len(blocks)
        _push_brief_section(blocks, digest)
        units.drop(_DROP_BRIEF, brief_start, len(blocks))

    # --- 📅 当日の予定（最大10件・1行形式は旧描画と共通・見出しは実日付）---
    cal_start = len(blocks)
    if not show_cal:
        pass
    elif cal_items:
        lines = [f"📅 *{day_label} の予定（{len(cal_items)}件）*"]
        for ev in cal_items[:lim_cal]:
            when = _fmt_event_time(
                getattr(ev, "start_at", None),
                getattr(ev, "end_at", None),
                all_day=getattr(ev, "all_day", None),
                target_date=day,
            )
            title = _slack_escape(
                getattr(ev, "summary_display", "")
                or getattr(ev, "summary_scrubbed", "")
                or "(無題)"
            )
            loc = getattr(ev, "location_display", "") or getattr(ev, "location_scrubbed", "")
            line = f"• `{when}`  {title}"
            if loc:
                line += f"  〔{_slack_escape(loc)}〕"
            url = getattr(ev, "meeting_url", "")
            if url:
                line += f"  <{url}|🔗参加>"  # 会議リンクは実 URL なのでエスケープしない
            lines.append(line)
        rem = len(cal_items) - lim_cal
        if rem > 0:
            lines.append(f"• 〈他{rem}件〉 <{_CALENDAR_URL}|カレンダーを開く>")
        _push_lines(lines)
    elif f0 and _fetch_state(digest, "calendar") != FETCH_OK:
        # 取れなかった日を「予定なし」と書かない（リマインドも登録されない日）。
        blocks.append(_section(_calendar_unavailable_text(digest, day_label)))
    else:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"📅 *{day_label} の予定*: なし"},
            }
        )
    units.protect(cal_start, len(blocks))

    # --- 末尾（☑️ 全部確認した + 脚注（DLP 注記・下書きの作り方・旧描画と同一））---
    # 打ち切りに巻き込ませないため、本文とは別に組んで最後に足す。
    # ☑️「全部確認した」は、本人が消した欄に項目があるときは出さない（見ていない項目まで
    # 確認済みにしてしまうため。個別の ☑️ は表示した項目にしか付かないので残る）。
    hidden_has_items = (
        (not show_reply and bool(high))
        or (not show_unread and bool(unread))
        or (not show_slack and slack_total > 0)
    )
    ack_all = [] if hidden_has_items else _ack_all_blocks(digest)
    tail: list[dict[str, Any]] = ack_all + _footer_blocks(digest)
    if prefs.hidden_sections or prefs.limits:
        # 欄・件数を変えている人だけ（リマインドだけ変えた人の DM は見た目が変わらない）。
        tail.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _PREFS_FOOTER}]})

    if f0:
        # F0: 節の優先順位つきの最終ガード（案内と今日の予定は削らず、メールの一覧から削る）。
        return text, _fit_blocks(blocks, units, tail)

    # blocks 50 個上限の保険（静的上限の積算では起きない想定の最終ガード）。
    # 切るのは本文側だけにする: ☑️一括ボタンが黙って消えると「押したつもりが押せて
    # いない」という見えない失敗になるため、末尾は常に残す。
    budget = _COMPACT_MAX_BLOCKS - len(tail)
    if len(blocks) > budget:
        blocks = blocks[: budget - 1]
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "_表示しきれない項目があります。Gmail / カレンダーで確認してください。_",
                    }
                ],
            }
        )
    blocks.extend(tail)
    return text, blocks


def _push_brief_section(blocks: list[dict[str, Any]], digest: Any) -> None:
    """📌 事例ブリーフ節を積む（**このセクションだけの fail-safe**）。

    - 事例集が未取込 / 走査できていない → ``render_brief_lines`` が空を返す＝節ごと出ない
      （「事例集が未取込のためスキップしました」を毎朝 29 名へ送るのは「できません」の
      定期配信になるため、運用ログにだけ落とす）
    - 社外 0 件 → ``📌 本日は社外MTGなし`` の 1 行（予定が無い日と区別できる）
    - 例外 → 何も積まない（📅/📧 は通常配信）
    """
    from teamagent.skills.pre_meeting_brief.render import render_brief_lines

    brief = getattr(digest, "pre_meeting_brief", None)
    if brief is None:
        return
    try:
        lines = render_brief_lines(
            brief,
            _digest_date(digest),
            early_notice=_early_notice(digest),
        )
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: brief 描画失敗 {type(exc).__name__}",
            file=sys.stderr,
        )
        return
    if not lines:
        return
    _push_section_lines(blocks, [_guard_no_raw_ids(ln) for ln in lines])
    blocks.append({"type": "divider"})


def _early_notice(digest: Any) -> bool:
    """送信時刻が下限 06:00 に張り付いた回か（＝冒頭に 1 行添える回か）。

    ⚠️ 予約ペイロードにフラグを載せない。発火した側が **planner と同じ純関数**
    （``compute_send_time``）で計算し直す＝2 箇所が別々の式を持たない。
    予約発火でない回（既定時刻の一括実行）は常に False（通常どおりの時刻で届いている）。
    """
    if _mode() != "single":
        return False
    from teamagent.skills.morning_digest.send_window import compute_send_time, first_timed_start

    day = _digest_date(digest)
    starts = [
        str(getattr(ev, "start_at", "") or "")
        # 終日とタスク枠は「最初の予定」の計算から除外（planner と同じ扱い）。
        for ev in (getattr(digest, "calendar_events", []) or [])
        if not bool(getattr(ev, "all_day", False))
        and "T" in str(getattr(ev, "start_at", "") or "")
        and not bool(getattr(ev, "personal_block", False))
    ]
    plan = compute_send_time(day, first_timed_start(starts, day), default_hhmm=_default_send_hhmm())
    return plan.clamped_to_floor


def _preferences_enabled() -> bool:
    """MORNING_DIGEST_PREFERENCES=1 のときだけ本人の設定（migration 0030）を読む（既定OFF）。

    OFF の間は表を 1 度も読まない＝今までどおりの配信（表が未作成の環境でも警告を出さない）。
    """
    return os.environ.get("MORNING_DIGEST_PREFERENCES", "").strip().lower() in {"1", "true", "yes"}


def _preferences_store() -> Any | None:
    """本人の設定ストア（OFF・DB 未設定なら None＝全員既定）。"""
    if not _preferences_enabled() or not os.environ.get("DATABASE_URL", "").strip():
        return None
    from teamagent.adapters.digest_preferences_store import DigestPreferencesStore

    return DigestPreferencesStore()


def _load_prefs(store: Any | None, email: str, request_id: str) -> _prefs.DigestPreferences:
    """1 人分の設定。読めなければ既定（fail-open・理由は preferences の docstring）。"""
    return _prefs.load_preferences(store, email, request_id=request_id)


def _reminders_enabled() -> bool:
    """MORNING_DIGEST_REMINDERS=1 のときのみ予定リマインドを登録（既定OFF・§10 E1-2）。"""
    return os.environ.get("MORNING_DIGEST_REMINDERS", "").strip().lower() in {"1", "true", "yes"}


def _schedule_event_reminders(
    digest: Any,
    im_channel: str,
    prefs: _prefs.DigestPreferences = _prefs.DEFAULT_PREFERENCES,
) -> int:
    """当日予定の「開始 N 分前」リマインドを EventBridge Scheduler に登録する（v0.3 Task5）。

    - 対象: start_at が「今から lead+1 分より先」の予定のみ（過ぎた/直近すぎる予定は skip）
    - 終日予定（date のみ）は対象外
    - payload に short title（≤60字）を載せる（2026-07-14・本人の予定を本人 DM に出す用途に
      限定。「何の予定か分からない」の解消・ユーザー要望）。Lambda はタイトルをログに出さない
    - schedule 名は channel×開始時刻から決定的＝再実行でも二重登録しない（Conflict→成功扱い）
    - 本人の設定（digest_settings）: リマインドを止めた人は 1 件も登録しない／何分前かの上書き／
      予定名に除外語を含む予定（例「タスク」）は登録しない
    """
    if not prefs.reminders:
        return 0
    from teamagent.adapters.scheduler_client import SchedulerClient

    try:
        scheduler = SchedulerClient.from_env()
    except ValueError as exc:
        print(f"[run_morning_digest_fargate] WARN: reminder 設定不備 {exc}", file=sys.stderr)
        return 0
    try:
        lead_min = int(os.environ.get("REMINDER_LEAD_MINUTES", "5"))
    except ValueError:
        lead_min = 5
    lead_min = min(60, max(1, lead_min))
    if prefs.reminder_lead_minutes is not None:
        lead_min = prefs.reminder_lead_minutes

    # 壁時計は calendar_window.now_jst（skill の取得窓と同じ時刻源・テストで固定できる）。
    now = _calwin.now_jst()
    count = 0
    for ev in list(getattr(digest, "calendar_events", []) or []):
        start_iso = str(getattr(ev, "start_at", "") or "")
        # 終日は API 由来フラグを優先（"…T00:00:00Z" 形の終日を時刻付きと誤認しない）。
        if bool(getattr(ev, "all_day", False)) or "T" not in start_iso:
            continue  # 終日 or 不明
        start = _calwin.parse_jst_datetime(start_iso)  # naive は JST とみなす（UTC 誤解釈防止）
        if start is None:
            continue
        # 除外語は表示名（生タイトル）で照合する（本人が見ている名前で「タスク」と言っている）。
        raw_title = str(
            getattr(ev, "summary_display", "") or getattr(ev, "summary_scrubbed", "") or ""
        )
        if not prefs.reminder_allowed(
            raw_title, personal_block=bool(getattr(ev, "personal_block", False))
        ):
            continue
        fire_at = start - _dt.timedelta(minutes=lead_min)
        if fire_at <= now + _dt.timedelta(minutes=1):
            continue  # もう間に合わない/過去の予定
        url = str(getattr(ev, "meeting_url", "") or "") or _CALENDAR_URL
        # 本人の予定タイトル（本人 DM 表示用の display）。空なら通知は従来どおり無題で成立。
        title = str(getattr(ev, "summary_display", "") or getattr(ev, "summary_scrubbed", "") or "")
        ok = scheduler.schedule_reminder(
            channel=im_channel,
            start_iso=start_iso,
            fire_at=fire_at,
            url=url,
            request_id=f"reminder-{uuid.uuid4().hex[:8]}",
            title=title,
            end_iso=str(getattr(ev, "end_at", "") or ""),
            location=str(
                getattr(ev, "location_display", "") or getattr(ev, "location_scrubbed", "") or ""
            ),
        )
        if ok:
            count += 1
    return count


async def _deliver_to_slack(
    user_email: str, text: str, blocks: list[dict[str, Any]]
) -> tuple[bool, str | None]:
    """Slack DM 配信（chat.postMessage with user IM channel）。

    返り値 (delivered, im_channel)。im_channel はリマインド登録（v0.3 Task5）が
    通知先として使う（配信失敗時は None）。
    """
    from teamagent.adapters.slack_client import SlackClient

    try:
        slack = SlackClient.from_env()
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: SlackClient.from_env 失敗 {exc}", file=sys.stderr
        )
        return (False, None)

    # email → Slack user_id → IM channel を開く
    try:
        user_id = await _email_to_slack_user_id(slack, user_email)
        if not user_id:
            return (False, None)
        im_channel = await _open_im_channel(slack, user_id)
        if not im_channel:
            return (False, None)
        result = await slack.post_message(
            channel=im_channel,
            text=text,
            request_id=f"morning-digest-{uuid.uuid4().hex[:8]}",
            blocks=blocks,
        )
        return (bool(getattr(result, "ok", False)), im_channel)
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: Slack 配信失敗 {type(exc).__name__}",
            file=sys.stderr,
        )
        return (False, None)


async def _email_to_slack_user_id(slack: Any, email: str) -> str | None:
    """users.lookupByEmail で Slack user_id を解決（bot scope: users:read.email）。"""
    try:
        # SlackClient は AsyncWebClient を self._client に保持。async メソッドは直接 await する
        # （to_thread に渡すと coroutine が未await のまま返り解決できない）。
        client = getattr(slack, "_client", None)
        if client is None:
            print("[run_morning_digest_fargate] WARN: slack._client 取得失敗", file=sys.stderr)
            return None
        resp = await client.users_lookupByEmail(email=email)
        user_id = str(resp.get("user", {}).get("id", "")) or None
        if user_id is None:
            # 解決はできたが該当ユーザー無し（Slack 未登録等）。配信失敗と区別して記録。
            print(
                f"[run_morning_digest_fargate] WARN: Slack user 未解決 {_mask_email(email)}",
                file=sys.stderr,
            )
        return user_id
    except Exception as exc:
        # ⚠️ {exc} は email を含み得る（PII）ため型名のみ。email はマスク（G3/G7）。
        print(
            f"[run_morning_digest_fargate] WARN: lookupByEmail 失敗 "
            f"{_mask_email(email)} {type(exc).__name__}",
            file=sys.stderr,
        )
        return None


async def _open_im_channel(slack: Any, user_id: str) -> str | None:
    """conversations.open で本人 IM channel を取得（bot scope: im:write）。"""
    try:
        client = getattr(slack, "_client", None)
        if client is None:
            return None
        resp = await client.conversations_open(users=user_id)
        return str(resp.get("channel", {}).get("id", "")) or None
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: conversations.open 失敗 {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None


async def _open_dm_channel(user_email: str) -> str | None:
    """本人 DM の channel を **開くだけ**（何も投稿しない）。祝日の予定リマインド登録用。

    ⚠️ 1 対 1 の DM（D 始まり）以外は使わない。チャンネル（C/G）へリマインドを向けない。
    """
    from teamagent.adapters.slack_client import SlackClient

    try:
        slack = SlackClient.from_env()
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: SlackClient.from_env 失敗 {type(exc).__name__}",
            file=sys.stderr,
        )
        return None
    user_id = await _email_to_slack_user_id(slack, user_email)
    if not user_id:
        return None
    channel = await _open_im_channel(slack, user_id)
    if not channel or not channel.startswith("D"):
        return None
    return channel


# ===========================================================================
# 個人別配信時刻（DELTA §1）— planner / 単独利用者モード / 二重配信の防止
# ===========================================================================
# 全体像:
#   04:00 JST  planner モード（本スクリプトを --mode=planner で起動）
#              → 連携済み利用者ごとに当日カレンダーを読み、送信時刻を決めて
#                EventBridge Scheduler（既存のリマインドと同じ group）へ 1 回きりの
#                予約 digest-<user_ref>-<YYYYMMDD> を作る
#   各人の時刻  予約が SQS へ → 既存 reminder_notify Lambda の kind=digest 分岐が
#              本スクリプトを ECS RunTask で **その 1 人分だけ** 起動
#   既定時刻    従来どおりの一括実行。ただし「その日すでに送った人」は必ず除外する
#
# 既定 OFF: MORNING_DIGEST_PERSONALIZED が未設定なら planner は 1 件も予約せず、
# 一括実行は claim を 1 度も呼ばない＝現行動作と 1 バイトも変わらない。


def _personalized_enabled() -> bool:
    """個人別配信（予約＋二重配信防止）を使うか。既定 OFF。"""
    return os.environ.get("MORNING_DIGEST_PERSONALIZED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _default_send_hhmm() -> tuple[int, int]:
    """上限＝現行の既定時刻。``MORNING_DIGEST_DEFAULT_TIME``（HH:MM）・既定 09:30。"""
    from teamagent.skills.morning_digest.send_window import parse_hhmm

    return parse_hhmm(os.environ.get("MORNING_DIGEST_DEFAULT_TIME"), (9, 30))


def _read_only_calendar(token_store: Any, email: str) -> Any | None:
    """本人トークンから **読み取り専用 facade** を作る（GCalendarClient を外へ出さない）。"""
    from teamagent.adapters.gcalendar_client import GCalendarClient
    from teamagent.adapters.gcalendar_readonly import ReadOnlyCalendar

    try:
        token = token_store.get(email) if token_store is not None else None
    except Exception:
        token = None
    if token is None:
        return None
    try:
        return ReadOnlyCalendar(GCalendarClient.from_user_token(token))
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: {_mask_email(email)} calendar 構築失敗 "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return None


def _plan_send_time(calendar: Any, day: _dt.date, request_id: str) -> Any:
    """当日の最初の「時刻つき」会議から送信時刻を決める（終日とタスク枠は除外）。

    タスク枠（ゲストも会議リンクも無い予定）は数えない。カレンダーに作業を入れている人は
    朝 7:00 の「メール処理」で 6:30 に起こされることになるため（10-05 小俣さん指摘）。
    """
    from teamagent.skills.morning_digest.send_window import compute_send_time, first_timed_start

    window_start = _dt.datetime.combine(day, _dt.time.min, tzinfo=_JST)
    window_end = window_start + _dt.timedelta(days=1)
    events = calendar.list_events(
        request_id,
        time_min=window_start.isoformat(),
        time_max=window_end.isoformat(),
        max_results=100,
    )
    starts = [
        str(getattr(ev, "start", "") or "")
        # ⚠️ 終日予定は「最初の予定」の計算から除外（終日だけの日は予定なし扱い）。
        for ev in events
        if not bool(getattr(ev, "all_day", False))
        and "T" in str(getattr(ev, "start", "") or "")
        and not _calwin.is_personal_block(
            getattr(ev, "attendees", None), getattr(ev, "meeting_url", "")
        )
    ]
    return compute_send_time(day, first_timed_start(starts, day), default_hhmm=_default_send_hhmm())


#: カレンダー未連携の人へ **週 1 回（月曜のみ）** 出す 1 行（PLAN §2-1）。
#: ⚠️ 認可 URL を文面に貼らない。長い URL は取り違え・再タイプ事故の実績がある
#:    （2026-09-03）。代わりに「この DM で『連携』」＝ Aico 側が正規のリンクを出す
#:    既存経路へ寄せる（「できません」で終わらせず、Aico が続きを引き受ける）。
CALENDAR_UNLINKED_LINE = (
    "📅 カレンダーが未連携のため、本日のアポ前ブリーフはお出しできません。"
    "この DM で「連携」と送っていただければ、Aico が連携リンクをお出しします。"
)


def _is_weekly_notice_day(day: _dt.date) -> bool:
    """未連携のお知らせを出す日か（**月曜のみ**）。毎日送らない。"""
    return day.weekday() == 0


def _notice_store() -> Any | None:
    """お知らせの重複を止める claim ストア（``None`` なら印を取らない＝テスト用）。

    ⚠️ 本番経路では必ず実体を返す。``None`` を返す分岐を足すと F5（2 通配信）へ戻る。
    """
    from teamagent.adapters.digest_notice_store import DigestNoticeStore

    return DigestNoticeStore()


def _notify_calendar_unlinked(email: str, day: _dt.date) -> bool:
    """カレンダー未連携の 1 行 DM（月曜のみ・事例ブリーフ ON・**その日 1 通だけ**）。

    fail-open: 送れなくても planner の戻り値は変えない（お知らせの失敗で配信予約
    そのものを落とさない）。ログにメールアドレスは出さない。

    ⚠️ 冪等の担保は DB の一意制約（migration 0027 ``digest_notice``）。planner の
    Scheduler ターゲットは ``retry_policy { maximum_retry_attempts = 1 }``
    （infra/terraform/morning_digest_schedule.tf）なので、途中で落ちて再実行されると
    未連携者 **全員** に同じ DM が 2 通届く。配信予約の方は schedule 名が決定的で
    ConflictException を成功扱いにするため再実行に耐えるが、ここには何も無かった。
    claim は **配信の前** に取る（送ってから印を付けると、印の書込に失敗した再実行で
    もう 1 通出る）。取れなければ送らない＝fail-closed。
    """
    # ⚠️ ゲートは skill 側と **同じ関数** を使う（2 箇所が別々の env 解釈を持たない）。
    from teamagent.adapters.digest_notice_store import NOTICE_CALENDAR_UNLINKED
    from teamagent.skills.morning_digest.skill import _brief_enabled

    if not _brief_enabled() or not _is_weekly_notice_day(day):
        return False
    store = _notice_store()
    if store is not None and not store.claim(
        email,
        day,
        kind=NOTICE_CALENDAR_UNLINKED,
        request_id=f"digest-notice-{uuid.uuid4().hex[:8]}",
    ):
        return False
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": CALENDAR_UNLINKED_LINE}}]
    try:
        delivered, _ = asyncio.run(_deliver_to_slack(email, CALENDAR_UNLINKED_LINE, blocks))
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: {_mask_email(email)} 未連携通知失敗 "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return False
    return bool(delivered)


def run_planner(users: list[str]) -> int:
    """04:00 JST の planner。利用者ごとに 1 回きりの配信予約を作る。

    - 予約が作れなかった / カレンダー未連携の人は **何もしない**＝既定時刻の一括実行に残る
      （現行動作の維持）。
    - 送信時刻が既定時刻のまま（時刻つき予定なし／最初の予定が遅い）の人も
      **予約を作らない**。一括実行が拾うので 1 通は必ず出るし、予約を作ると
      一括実行と同時刻に 1 人 1 タスクの Fargate が余分に立つ。
    - 予約名は決定的なので planner を再実行しても当日分を作り直さない（冪等）。
    - Scheduler への書込は **既存のリマインド生成が持つ権限経路だけ** を使う
      （skill 側には書込権限を渡さない）。
    """
    from teamagent.adapters.scheduler_client import SchedulerClient
    from teamagent.digest_user_ref import digest_schedule_name, user_ref

    if not _personalized_enabled():
        print("[run_morning_digest_fargate] planner: disabled (default OFF)", flush=True)
        return 0
    try:
        scheduler = SchedulerClient.from_env()
    except ValueError as exc:
        print(f"[run_morning_digest_fargate] planner: 設定不備 {exc}", file=sys.stderr)
        return 0
    token_store = _build_token_store()
    day = _digest_day()  # 配信側（run_digest）と同じ対象日: MORNING_DIGEST_DATE が効く
    # 祝日スキップ ON のとき、祝日は予約を作らない（予約すると祝日に DM が届く）。
    # 月曜の未連携のお知らせも DM なので送らない。予定リマインドは 9:30 の実行が登録する。
    holiday_cal = _holiday_calendar()
    holiday_reason = holiday_cal.holiday_reason(day) if holiday_cal is not None else None
    if holiday_reason is not None:
        print(
            f"[run_morning_digest_fargate] planner: holiday skip {holiday_reason}",
            flush=True,
        )
        return 0
    day_compact = day.strftime("%Y%m%d")
    planned = 0
    skipped = 0
    notified = 0
    prefs_store = _preferences_store()
    # 予約印（migration 0031）。9:30 の一括が「後で個別に送る人」を見送るための唯一の根拠。
    delivery_store = _delivery_store()
    default_at = _dt.datetime.combine(day, _dt.time(*_default_send_hhmm()), tzinfo=_JST)
    for email in users:
        request_id = f"digest-plan-{uuid.uuid4().hex[:8]}"
        # 本人が止めた/休み/曜日外の日は予約を作らない（予約が発火すると DM が届く）。
        if _load_prefs(prefs_store, email, request_id).skip_reason(day) is not None:
            skipped += 1
            continue
        calendar = _read_only_calendar(token_store, email)
        if calendar is None:
            # PLAN §2-1: 未連携の人が「自分はブリーフの対象外」だと永久に気づけない
            # 状態を作らない。週 1 回（月曜）だけ 1 行で知らせる。
            if _notify_calendar_unlinked(email, day):
                notified += 1
            skipped += 1
            continue
        try:
            plan = _plan_send_time(calendar, day, request_id)
        except Exception as exc:
            print(
                f"[run_morning_digest_fargate] WARN: {_mask_email(email)} planner 失敗 "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
            skipped += 1
            continue
        if plan.no_timed_event or plan.clamped_to_default or plan.fire_at == default_at:
            # ⚠️ 既定時刻のままの人は **予約を作らない**（DELTA §1「予定が 1 件も無い日＝
            #   既定時刻」「予約が作れなかった利用者は既定時刻の一括実行に残す」）。
            #   ここで予約を作ると (a) 一括配信が走らない土曜にも DM が出る
            #   (b) 平日は bulk と同時刻に 1 人 1 タスクの Fargate が余分に立ち、claim
            #   競合でどちらかが無駄走りする、の 2 つが同時に起きる。
            skipped += 1
            continue
        ref = user_ref(email)
        if not ref:
            skipped += 1
            continue
        # 先に予約印を付ける（付けられなければ予約しない＝9:30 の一括に残す）。
        # ⚠️ 印だけ残って予約が無い状態はその日 1 通も届かないので、予約に失敗したら印を消す。
        if delivery_store is None or not delivery_store.reserve(email, day, request_id=request_id):
            skipped += 1
            continue
        ok = scheduler.schedule_digest(
            name=digest_schedule_name(ref, day_compact),
            user_ref=ref,
            date_iso=day.isoformat(),
            fire_at=plan.fire_at,
            request_id=request_id,
        )
        if ok:
            planned += 1
        else:
            delivery_store.release(email, day, request_id=request_id)
            skipped += 1
    # ⚠️ 件数のみ。メールアドレス・予定タイトル・時刻の個人分布は出さない。
    summary = {
        "users": len(users),
        "planned": planned,
        "skipped": skipped,
        "notified": notified,
    }
    print(f"[run_morning_digest_fargate] planner done {json.dumps(summary)}", flush=True)
    return 0


def _resolve_single_user(users: list[str]) -> str | None:
    """``MORNING_DIGEST_USER_REF`` を連携済み利用者へ解決する（fail-closed）。

    ⚠️ Scheduler / SQS 由来の値を宛先として信用しない。ペイロードに channel は入って
    おらず、ここで解決した email から配信側が本人 DM を **解決し直す**。
    """
    from teamagent.digest_user_ref import resolve_user_ref

    ref = os.environ.get("MORNING_DIGEST_USER_REF", "").strip()
    if not ref:
        return None
    email = resolve_user_ref(ref, users)
    if email is None:
        print("[run_morning_digest_fargate] WARN: user_ref 未解決（配信しない）", file=sys.stderr)
    return email


def _digest_day() -> _dt.date:
    """対象日（``MORNING_DIGEST_DATE`` があればそれ・無ければ JST の今日）。"""
    raw = os.environ.get("MORNING_DIGEST_DATE", "").strip()
    if raw:
        parsed = _calwin.parse_jst_date(raw)
        if parsed is not None:
            return parsed
    return _calwin.now_jst().date()  # テストはこの関数を差し替えて「今日」を固定する


# ===========================================================================
# 祝日スキップ（F0・PR-0c）— 既定 OFF（MORNING_DIGEST_HOLIDAY_SKIP）
# ===========================================================================
# ON のとき:
#   - 祝日（内閣府の表）と会社休日（MORNING_DIGEST_EXTRA_SKIP_DATES）は skill.run を呼ばず
#     DM も送らない。予定の開始前リマインド（MORNING_DIGEST_REMINDERS）だけは登録する
#     （2026-09-29 裁定: 祝日は休み・予定 5 分前の知らせは続ける）。
#   - 祝日明けはメールの走査範囲を「前の配信日から今日まで」に広げる（最低 3 日）。
#   - 表の期限の 60 日前から / 範囲外の日は jp_holiday_table_stale を出す（配信は止めない）。
# OFF のとき: 表を 1 度も見ない・MorningDigestInput も今と同じ＝現行動作と 1 バイトも変わらない。


def _holiday_calendar() -> Any | None:
    """祝日スキップが ON なら配信日カレンダー、OFF なら None（＝今と同じ）。"""
    from teamagent.skills.morning_digest.delivery_calendar import (
        DeliveryCalendar,
        holiday_skip_enabled,
    )

    if not holiday_skip_enabled():
        return None
    return DeliveryCalendar.from_env()


def _warn_if_holiday_table_stale(day: _dt.date) -> bool:
    """祝日の表が期限の 60 日前を切った・切れた・範囲外なら警告イベントを出す。

    ⚠️ 配信は止めない（止める側に倒すと全員に届かない日が出る）。CloudWatch の
    metric filter（morning_digest_schedule.tf の morning_digest_holiday_table_stale）が
    このイベント名で拾う。管理者 DM（PR-0a）は ``jp_holidays.coverage_notice`` の 1 行を足す。
    """
    from teamagent import jp_holidays

    status = jp_holidays.coverage_status(day)
    if not status.stale:
        return False
    logger.warning(
        "jp_holiday_table_stale",
        state=status.state,
        days_left=status.days_left,
        coverage_end=status.coverage_end.isoformat(),
        day=day.isoformat(),
    )
    return True


def _register_holiday_reminders(
    skill: Any,
    skill_input: Any,
    email: str,
    prefs: _prefs.DigestPreferences = _prefs.DEFAULT_PREFERENCES,
) -> tuple[str, int]:
    """祝日の 1 人分: 予定を取ってリマインドだけ登録する（DM は送らない）。

    返り値 (状態, 登録数)。状態は "reminded" / "skipped"（未連携・予定なし）/ "error"。
    例外は内側で封じ込める（1 人の失敗で全体を落とさない・ログはマスク済みのみ）。
    """
    from teamagent.skills.base import SkillContext
    from teamagent.skills.morning_digest.schema import MorningDigestOutput

    if not prefs.reminders:
        return ("skipped", 0)  # 本人がリマインドを止めている（予定も取りに行かない）
    ctx = SkillContext(
        request_id=f"morning-holiday-{uuid.uuid4().hex[:8]}", metadata={"user_email": email}
    )
    try:
        events = skill.collect_calendar_events(skill_input, ctx)
    except PermissionError:
        return ("skipped", 0)  # 未連携
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: {_mask_email(email)} 祝日の予定取得失敗 "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return ("error", 0)
    if not events:
        return ("skipped", 0)
    try:
        im_channel = asyncio.run(_open_dm_channel(email))
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: {_mask_email(email)} 祝日の DM 解決失敗 "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        im_channel = None
    if not im_channel:
        return ("error", 0)
    holder = MorningDigestOutput(user_email_masked=_mask_email(email), calendar_events=events)
    try:
        n = _schedule_event_reminders(holder, im_channel, prefs)
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: reminder 登録失敗 {type(exc).__name__}",
            file=sys.stderr,
        )
        return ("error", 0)
    return ("reminded", n)


def _run_holiday(users: list[str], day: _dt.date, reason: str) -> int:
    """祝日・会社休日の実行: ダイジェストは誰にも送らず、予定リマインドだけ登録する。"""
    from teamagent import jp_holidays

    reminders_on = _reminders_enabled()
    counts = {"reminded": 0, "reminders": 0, "skipped": 0, "errors": 0}
    if reminders_on:
        from teamagent.skills.morning_digest.schema import MorningDigestInput
        from teamagent.skills.morning_digest.skill import MorningDigestSkill

        skill = MorningDigestSkill(token_store=_build_token_store())
        skill_input = MorningDigestInput()
        prefs_store = _preferences_store()
        for email in users:
            prefs = _load_prefs(prefs_store, email, f"morning-holiday-{uuid.uuid4().hex[:8]}")
            state, n = _register_holiday_reminders(skill, skill_input, email, prefs)
            counts[{"reminded": "reminded", "skipped": "skipped"}.get(state, "errors")] += 1
            counts["reminders"] += n
    # ⚠️ 件数のみ。メールアドレス・予定のタイトルは出さない。
    summary = {
        "day": day.isoformat(),
        "reason": reason,
        "holiday": jp_holidays.holiday_name(day) or "",
        "users": len(users),
        "reminders_enabled": reminders_on,
        **counts,
    }
    logger.info("morning_digest_holiday_skip", **summary)
    print(
        f"[run_morning_digest_fargate] holiday skip {json.dumps(summary, ensure_ascii=False)}",
        flush=True,
    )
    return 0


def _delivery_store() -> Any | None:
    """二重配信を止める claim ストア（個人別配信が OFF なら None＝現行動作のまま）。"""
    if not _personalized_enabled():
        return None
    from teamagent.adapters.digest_delivery_store import DigestDeliveryStore

    return DigestDeliveryStore()


def _claim_delivery(store: Any, email: str, day: _dt.date, *, origin: str, request_id: str) -> str:
    """配信権を取り、結果を claimed / taken / failed の 3 通りで返す（送ってよいのは claimed だけ）。

    本物の ``DigestDeliveryStore`` は ``claim_result`` で 3 通りを返す。``claim``（真偽）しか
    持たないストアは、False を「既に取られている」とみなす（従来どおり）。
    """
    claim_result = getattr(store, "claim_result", None)
    if callable(claim_result):
        return str(claim_result(email, day, origin=origin, request_id=request_id))
    if store.claim(email, day, origin=origin, request_id=request_id):
        return _CLAIM_CLAIMED
    return _CLAIM_TAKEN


# ===========================================================================
# F0: 管理者 DM（MORNING_DIGEST_ADMIN_REPORT_EMAILS・既定 OFF）と実行結果の集計
# ===========================================================================
# 毎朝 1 行（配信数と失敗数）を管理者の本人 DM へ送る。この 1 行が「動いた」印を兼ねる
# ＝届かない朝は起動していないと分かる。問題があった日だけ内訳を足す。
# ⚠️ 中身の規律:
#   - メールの件名・本文・相手・予定のタイトル・例外の文面は **入れない**。
#     入れるのは件数・分類コード・内訳コード（型名と Google の識別子）・利用者の @ より前だけ。
#   - 利用者の名前（@ より前）は管理者 DM の本文にだけ出し、ログにも DB にも書かない。
# ⚠️ 宛先の規律（チャンネルへは構造上送れない）:
#   - env の値は社内ドメインの email だけ受け付ける（最大 3 件・それ以外は捨てる）。
#   - users.lookupByEmail の結果で、削除済み・bot・ゲスト（制限付き）・社外（Slack Connect）
#     でないこと、返ってきた email が要求と一致することを確かめる。
#   - conversations.open の結果が D（本人 DM）で始まるときだけ投稿する。C/G には送らない。

_ADMIN_REPORT_MAX_RECIPIENTS = 3


class UserOutcome:
    """1 人分の実行結果（管理者 DM の材料・メモリ上だけ。email はログに出さない）。"""

    __slots__ = (
        "calendar_detail",
        "calendar_fetch",
        "email",
        "error",
        "guided",
        "mail_detail",
        "mail_fetch",
        "mail_threads_failed",
        "reason",
        "status",
    )

    def __init__(
        self,
        email: str,
        status: str,
        reason: str = "",
        *,
        mail_fetch: str = FETCH_UNKNOWN,
        calendar_fetch: str = FETCH_UNKNOWN,
        mail_detail: str = "",
        calendar_detail: str = "",
        mail_threads_failed: int = 0,
        guided: bool = False,
        error: str = "",
    ) -> None:
        self.email = email
        self.status = status
        self.reason = reason
        self.mail_fetch = mail_fetch
        self.calendar_fetch = calendar_fetch
        self.mail_detail = mail_detail
        self.calendar_detail = calendar_detail
        self.mail_threads_failed = mail_threads_failed
        self.guided = guided
        self.error = error


def _safe_code(raw: Any, limit: int = 60) -> str:
    """内訳コードを英数字・``_``・``:`` だけに絞る（管理者 DM に中身を混ぜない最後の砦）。"""
    return re.sub(r"[^A-Za-z0-9_:]", "", str(raw or ""))[:limit]


def _user_outcome(email: str, status: str, reason: str, digest: Any, error: str) -> UserOutcome:
    if digest is None:
        return UserOutcome(email, status, reason, error=_safe_code(error, 40))
    return UserOutcome(
        email,
        status,
        reason,
        mail_fetch=_fetch_state(digest, "mail"),
        calendar_fetch=_fetch_state(digest, "calendar"),
        mail_detail=_safe_code(getattr(digest, "mail_fetch_detail", "")),
        calendar_detail=_safe_code(getattr(digest, "calendar_fetch_detail", "")),
        mail_threads_failed=int(getattr(digest, "mail_threads_failed", 0) or 0),
        guided=_fetch_status_enabled(email),
        error=_safe_code(error, 40),
    )


def _internal_domain() -> str:
    """社内ドメイン（skill の差出人区分と同じ env）。空なら管理者 DM は誰にも送らない。"""
    return os.environ.get("DIGEST_INTERNAL_DOMAIN", "vectorinc.co.jp").strip().lower().lstrip("@")


def _admin_report_recipients() -> list[str]:
    """``MORNING_DIGEST_ADMIN_REPORT_EMAILS`` のうち、社内ドメインの email だけ（最大 3 件）。"""
    raw = os.environ.get("MORNING_DIGEST_ADMIN_REPORT_EMAILS", "").strip()
    domain = _internal_domain()
    if not raw or not domain:
        return []
    pattern = re.compile(r"[a-z0-9._%+\-]{1,64}@" + re.escape(domain))
    out: list[str] = []
    rejected = 0
    for part in raw.split(","):
        email = part.strip().lower()
        if not email:
            continue
        if not pattern.fullmatch(email):
            rejected += 1
            continue
        if email not in out:
            out.append(email)
    if len(out) > _ADMIN_REPORT_MAX_RECIPIENTS:
        rejected += len(out) - _ADMIN_REPORT_MAX_RECIPIENTS
        out = out[:_ADMIN_REPORT_MAX_RECIPIENTS]
    if rejected:
        logger.warning("morning_digest_admin_report_recipient_rejected", rejected=rejected)
    return out


def _is_internal_member(user: Any, email: str) -> bool:
    """lookupByEmail の結果が「社内の正規メンバー本人」か（ゲスト・社外・bot・削除済みは不可）。"""
    if not isinstance(user, dict):
        return False
    for flag in (
        "deleted",
        "is_bot",
        "is_app_user",
        "is_restricted",
        "is_ultra_restricted",
        "is_stranger",
        "is_invited_user",
    ):
        if user.get(flag):
            return False
    uid = str(user.get("id", "") or "")
    if not uid or uid[0] not in "UW":
        return False
    profile = user.get("profile") or {}
    got = str(profile.get("email", "") or "").strip().lower() if isinstance(profile, dict) else ""
    return bool(got) and got == email and got.endswith("@" + _internal_domain())


async def _deliver_admin_report(recipients: list[str], text: str) -> tuple[int, int]:
    """管理者 DM を送る。返り値 (送れた数, 宛先の検査で止めた数)。fail-open（例外は外へ出さない）。"""
    from teamagent.adapters.slack_client import SlackClient

    try:
        slack = SlackClient.from_env()
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: 管理者 DM の Slack 初期化失敗 {type(exc).__name__}",
            file=sys.stderr,
        )
        return (0, 0)
    client = getattr(slack, "_client", None)
    if client is None:
        return (0, 0)
    sent = 0
    refused = 0
    for email in recipients:
        try:
            resp = await client.users_lookupByEmail(email=email)
            user = resp.get("user") if hasattr(resp, "get") else None
            if not _is_internal_member(user, email):
                refused += 1
                continue
            channel = await _open_im_channel(slack, str(user["id"]))  # type: ignore[index]
            # 本人 DM（D…）以外には送らない＝チャンネル・グループへは構造上届かない。
            if not channel or not channel.startswith("D"):
                refused += 1
                continue
            result = await slack.post_message(
                channel=channel,
                text=text,
                request_id=f"morning-digest-admin-{uuid.uuid4().hex[:8]}",
                blocks=[_section(text)],
            )
            if bool(getattr(result, "ok", False)):
                sent += 1
        except Exception as exc:
            print(
                f"[run_morning_digest_fargate] WARN: 管理者 DM 失敗 {type(exc).__name__}",
                file=sys.stderr,
            )
    return (sent, refused)


def _local_part(email: str) -> str:
    """管理者 DM に出す名前（@ より前だけ・英数記号に限る）。"""
    return re.sub(r"[^a-z0-9._\-]", "", (email or "").split("@", 1)[0].lower())[:40]


def _format_admin_report(
    outcomes: list[UserOutcome],
    *,
    day: _dt.date,
    users: int,
    target_error: str | None = None,
) -> tuple[str, bool]:
    """管理者 DM の本文と「問題があったか」。件数・分類・内訳コード・@ より前だけで組む。"""
    delivered = sum(1 for o in outcomes if o.status == "delivered")
    errors = [o for o in outcomes if o.status == "error"]
    not_connected = sum(1 for o in outcomes if o.reason == "not_connected")
    already = sum(1 for o in outcomes if o.reason == "already_delivered")
    # 最初の予定が遅い日で、あとで個別に送る予約の人（9:30 の一括では送らない・正常）。
    later = sum(1 for o in outcomes if o.reason == "reserved_later")
    # 配信権（digest_delivery）を DB で確かめられず、送らずに止めた人。「送信済み」とは別に数える
    # （DB 障害の朝は全員がここに入り、誰にも届かない）。
    claim_failed = sum(1 for o in outcomes if o.reason == "claim_failed")
    head = (
        f"🔧 朝ダイジェスト {_calwin.fmt_jst_date(day)} の実行結果（管理者向け）"
        f"｜対象 {users}・配信 {delivered}・配信失敗 {len(errors)}・未連携 {not_connected}"
    )
    if already:
        head += f"・送信済み {already}"
    if later:
        head += f"・予定に合わせて後で送る {later}"
    if claim_failed:
        head += f"・送信の確認失敗 {claim_failed}"
    # 本人が DM で止めた/休み/曜日外にした人（件数だけ・誰がどう設定したかは出さない）。
    by_pref = sum(1 for o in outcomes if o.reason.startswith("pref_"))
    if by_pref:
        head += f"・本人設定で停止 {by_pref}"
    lines = [head]
    if target_error == TARGET_ZERO_ROWS:
        lines.append(
            "⚠️ 連携済みの対象者が 0 人と返り、誰にも配信していません"
            "（連携済みの人がいるはずなら、DB の権限と RLS の設定を確認）"
        )
    elif target_error:
        lines.append(
            f"⚠️ 対象者を取得できず、誰にも配信していません（原因: {_safe_code(target_error, 40)}）"
        )

    fetched = [
        o for o in outcomes if o.status in ("delivered", "error") and o.reason != "skill_failed"
    ]
    mail_bad = [o for o in fetched if o.mail_fetch != FETCH_OK]
    cal_bad = [o for o in fetched if o.calendar_fetch != FETCH_OK]
    by_state: dict[str, list[UserOutcome]] = {}
    for o in fetched:
        states = {o.mail_fetch, o.calendar_fetch} - {FETCH_OK}
        for state in (FETCH_TOKEN_EXPIRED, FETCH_SCOPE_MISSING, FETCH_TEMPORARY, FETCH_UNKNOWN):
            if state in states:
                by_state.setdefault(state, []).append(o)
                break  # 1 人 1 区分（失効 > 権限不足 > 一時的 > 不明 の順で代表させる）
    partial = [o for o in fetched if o.mail_fetch == FETCH_OK and o.mail_threads_failed > 0]
    problem = bool(target_error or errors or mail_bad or cal_bad or partial or claim_failed)
    if not problem:
        return "\n".join(lines), False

    if claim_failed:
        lines.append(
            f"・送信済みかを DB で確かめられず、送らなかった: {claim_failed} 人"
            "（二重配信を避けて止めています。DB の接続と digest_delivery の権限を確認）"
        )

    if mail_bad or cal_bad:
        lines.append(f"取得できなかった: メール {len(mail_bad)} 人・予定 {len(cal_bad)} 人")
    labels = (
        (FETCH_TOKEN_EXPIRED, "再連携が必要（連携切れ）"),
        (FETCH_SCOPE_MISSING, "再連携が必要（権限不足）"),
        (FETCH_TEMPORARY, "一時的な失敗（再連携は案内していません）"),
        (FETCH_UNKNOWN, "取得できたか不明"),
    )
    for state, label in labels:
        people = by_state.get(state, [])
        if not people:
            continue
        line = f"・{label}: {len(people)} 人"
        if state in FETCH_NEEDS_RECONNECT:
            line += f"（{', '.join(_local_part(o.email) for o in people)}）"
            unguided = sum(1 for o in people if not o.guided)
            if unguided:
                line += f" ※ うち {unguided} 人には案内を表示していません（FETCH_STATUS の対象外）"
        lines.append(line)
    if partial:
        lines.append(
            f"・一部のメールを読み込めなかった: {len(partial)} 人"
            f"（計 {sum(o.mail_threads_failed for o in partial)} 件）"
        )
    if errors:
        lines.append(
            f"・配信できなかった: {len(errors)} 人（{', '.join(_local_part(o.email) for o in errors)}）"
        )
    counts: dict[str, int] = {}
    for o in fetched:
        for detail in (o.mail_detail, o.calendar_detail):
            if detail:
                counts[detail] = counts.get(detail, 0) + 1
    for o in errors:
        if o.error:
            counts[f"{o.reason}:{o.error}"] = counts.get(f"{o.reason}:{o.error}", 0) + 1
    if counts:
        lines.append("原因の内訳: " + ", ".join(f"{k}×{v}" for k, v in sorted(counts.items())))
    lines.append("※ メールの件名・本文・相手は含みません")
    return "\n".join(lines), True


def _send_admin_report(
    outcomes: list[UserOutcome],
    *,
    day: _dt.date,
    users: int,
    target_error: str | None = None,
) -> None:
    """管理者 DM（フラグ OFF なら何もしない）。失敗しても配信の結果は変えない。"""
    recipients = _admin_report_recipients()
    if not recipients:
        return
    try:
        text, problem = _format_admin_report(
            outcomes, day=day, users=users, target_error=target_error
        )
        sent, refused = asyncio.run(_deliver_admin_report(recipients, text))
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: 管理者 DM 組み立て失敗 {type(exc).__name__}",
            file=sys.stderr,
        )
        return
    logger.info(
        "morning_digest_admin_report",
        recipients=len(recipients),
        sent=sent,
        refused=refused,
        problem=problem,
    )


def _log_run_done(outcomes: list[UserOutcome], summary: dict[str, int]) -> None:
    """実行の締めを 1 行の JSON イベントで出す（件数だけ・email は出さない）。"""
    fetched = [
        o for o in outcomes if o.status in ("delivered", "error") and o.reason != "skill_failed"
    ]
    states = [s for o in fetched for s in (o.mail_fetch, o.calendar_fetch)]
    logger.info(
        "morning_digest_run_done",
        **summary,
        mail_fetch_failed=sum(1 for o in fetched if o.mail_fetch != FETCH_OK),
        calendar_fetch_failed=sum(1 for o in fetched if o.calendar_fetch != FETCH_OK),
        token_expired=states.count(FETCH_TOKEN_EXPIRED),
        scope_missing=states.count(FETCH_SCOPE_MISSING),
        temporary=states.count(FETCH_TEMPORARY),
        threads_failed_users=sum(1 for o in fetched if o.mail_threads_failed > 0),
        claim_failed=sum(1 for o in outcomes if o.reason == "claim_failed"),
    )


def _process_user(
    skill: Any,
    skill_input: Any,
    email: str,
    *,
    store: Any | None = None,
    day: _dt.date | None = None,
    origin: str = "bulk",
    sink: list[UserOutcome] | None = None,
    prefs_store: Any | None = None,
) -> str:
    """1 ユーザー分を処理し "delivered"/"skipped"/"error" を返す（例外は内側で封じ込め）。

    スレッドから呼ぶため副作用は print（stderr・マスク済）と Slack 配信のみ・共有状態を書かない。
    ``sink`` を渡すと結果（F0 の取得状態つき）を 1 件積む（管理者 DM の集計用・メモリ上だけ。
    list.append はスレッド間で安全）。戻り値の型は変えない。

    ``store`` を渡すと **その日の配信権を DB の一意制約で 1 回だけ取る**（二重配信の防止）。
    - 取れなければ "skipped"（既に別経路が送っている／障害で確認できない＝fail-closed）
    - 配信に失敗したら印を戻す（次の経路に再挑戦させる）
    ⚠️ 例外を「たぶん送ってよい」側に倒さないこと。倒すと 29 名に 2 通届く。
    """
    from teamagent.skills.base import SkillContext

    def _done(status: str, reason: str = "", digest: Any = None, error: str = "") -> str:
        if sink is not None:
            sink.append(_user_outcome(email, status, reason, digest, error))
        return status

    request_id = f"morning-{uuid.uuid4().hex[:10]}"
    target_day = day or _dt.datetime.now(tz=_JST).date()
    # 本人の設定（digest_settings）。止めた/休み/曜日外なら、配信権も取らず skill も呼ばない
    # （Gmail 走査・下書き生成・Bedrock を 1 度も走らせない）。
    prefs = _load_prefs(prefs_store, email, request_id)
    skip_reason = prefs.skip_reason(target_day)
    if skip_reason is not None:
        return _done("skipped", skip_reason)
    if not prefs.auto_drafts:
        # 返信の下書きを自動で作らない人。共有の skill_input は書き換えず、この人の分だけ複製。
        skill_input = skill_input.model_copy(update={"max_drafts": 0})
    if store is not None:
        verdict = _claim_delivery(store, email, target_day, origin=origin, request_id=request_id)
        if verdict != _CLAIM_CLAIMED:
            # 「別の経路が送った（正常）」と「DB で確かめられず止めた（全員に届かない障害）」を
            # 数え分ける。どちらも送らない（fail-closed）のは同じ。
            reason = {
                _CLAIM_TAKEN: "already_delivered",
                # planner が「後で個別に送る」と予約した人（最初の予定が遅い日）。正常な見送り。
                _CLAIM_RESERVED: "reserved_later",
            }.get(verdict, "claim_failed")
            return _done("skipped", reason)
    ctx = SkillContext(request_id=request_id, metadata={"user_email": email})
    try:
        digest = skill.run(skill_input, ctx)
    except PermissionError:
        if store is not None:
            store.release(email, target_day, request_id=request_id)
        return _done("skipped", "not_connected")  # 未連携
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: {_mask_email(email)} skill 失敗 "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        if store is not None:
            store.release(email, target_day, request_id=request_id)
        return _done("error", "skill_failed", error=type(exc).__name__)
    # 配信(整形+Slack)も封じ込め（1 人の失敗で全体を落とさない）。
    try:
        # 欄・件数を変えている人は密度優先描画で組む（設定は compact 描画だけが解釈する）。
        if _compact_enabled() or prefs.hidden_sections or prefs.limits:
            text, blocks = _format_block_kit_compact(digest, email, prefs)
        else:
            text, blocks = _format_block_kit(digest, email)
        delivered, im_channel = asyncio.run(_deliver_to_slack(email, text, blocks))
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: {_mask_email(email)} 配信失敗 "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        if store is not None:
            store.release(email, target_day, request_id=request_id)
        return _done("error", "deliver_failed", digest, error=type(exc).__name__)
    if delivered:
        digest.delivered = True
        # v0.3 Task5: 当日予定の開始前リマインドをワンタイム登録（flag 既定OFF・fail-open＝
        # 登録失敗してもダイジェスト配信の成功は変えない）。
        if im_channel and _reminders_enabled():
            try:
                n = _schedule_event_reminders(digest, im_channel, prefs)
                if n:
                    print(f"[run_morning_digest_fargate] reminders scheduled: {n}", flush=True)
            except Exception as exc:
                print(
                    f"[run_morning_digest_fargate] WARN: reminder 登録失敗 {type(exc).__name__}",
                    file=sys.stderr,
                )
        return _done("delivered", "", digest)
    if store is not None:
        # Slack が受け付けなかった（未解決・DM 不可等）＝送れていないので印を戻す。
        store.release(email, target_day, request_id=request_id)
    return _done("error", "deliver_failed", digest, error="not_delivered")


def _mode() -> str:
    """実行モード: ``bulk``（既定時刻の一括・従来）/ ``planner`` / ``single``。

    ``--mode=planner`` か ``MORNING_DIGEST_MODE=planner`` で planner。
    ``MORNING_DIGEST_USER_REF`` があれば単独利用者モード（予約発火時）。
    """
    for arg in sys.argv[1:]:
        if arg.startswith("--mode="):
            value = arg.split("=", 1)[1].strip().lower()
            if value in ("planner", "single", "bulk"):
                return value
    env_mode = os.environ.get("MORNING_DIGEST_MODE", "").strip().lower()
    if env_mode in ("planner", "single", "bulk"):
        return env_mode
    if os.environ.get("MORNING_DIGEST_USER_REF", "").strip():
        return "single"
    return "bulk"


PERSONAL_MEMORY_SWEEP_ENV = "PERSONAL_MEMORY_RETIRE_SWEEP"


def _maybe_sweep_personal_memory() -> None:
    """本人メモの退職（Slack で削除済み）を消し、ゲスト化を凍結する（M6・設計 §10b.5）。

    ``PERSONAL_MEMORY_RETIRE_SWEEP`` が真のときだけ。Slack の確認に失敗した人は何もしない。
    この処理の失敗で朝の予約を止めない（例外は型名だけ残して握る）。
    """
    if os.environ.get(PERSONAL_MEMORY_SWEEP_ENV, "").strip().lower() not in {"1", "true", "yes"}:
        return
    try:
        from slack_sdk import WebClient

        from teamagent.adapters.personal_memory_admin import PersonalMemoryAdmin, sweep_retired

        token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        if not token:
            logger.warning("personal_memory_sweep_skipped", reason="no_slack_bot_token")
            return
        sweep_retired(PersonalMemoryAdmin(), WebClient(token=token, timeout=10))
    except Exception as exc:
        logger.warning("personal_memory_sweep_failed", error=type(exc).__name__)


def main() -> int:
    global _TARGET_FETCH_ERROR
    # F0: ログを JSON にする（タスク定義は STRUCTLOG_FORMAT=json を渡しているのに、この
    # スクリプトは一度も configure しておらず、CloudWatch の metric filter（$.level 等）が
    # 1 度も一致しなかった）。モジュール経由で呼ぶのはテストが差し替えられるようにするため。
    from teamagent.observability import logging_config as _logging_config

    _logging_config.configure_logging()
    require_runtime_startup((("mail_action", MAIL_ACTION_MAX_TOKEN_TTL_S),))
    _TARGET_FETCH_ERROR = None
    users = _resolve_target_users()
    target_error = _TARGET_FETCH_ERROR
    mode = _mode()
    if target_error:
        # 「対象 0 人」と「取得できず誰にも送っていない」は別事象（後者は全員に届かない）。
        logger.error("morning_digest_target_fetch_failed", err=target_error, mode=mode)
    if not users:
        print("[run_morning_digest_fargate] no target users (env+RDS empty)", flush=True)
        if mode == "bulk":
            _send_admin_report([], day=_digest_day(), users=0, target_error=target_error)
        return 0

    if mode == "planner":
        # 04:00 JST: 予約を作るだけ。digest は 1 通も配信しない。
        # 本人メモの退職・ゲスト化の掃除も 1 日 1 回ここで行う（既定 OFF・失敗しても予約は作る）。
        _maybe_sweep_personal_memory()
        return run_planner(users)
    if mode == "single":
        # 予約発火: user_ref を連携済み利用者へ解決し、その 1 人分だけ実行する。
        resolved = _resolve_single_user(users)
        if resolved is None:
            return 0
        users = [resolved]
        print("[run_morning_digest_fargate] single-user run", flush=True)

    print(f"[run_morning_digest_fargate] start users={len(users)}", flush=True)

    # 祝日スキップ（既定 OFF＝None。OFF の間は表を見ず、走査範囲も今と同じ既定 3 日）。
    day = _digest_day()
    lookback_days: int | None = None
    holiday_cal = _holiday_calendar()
    if holiday_cal is not None:
        _warn_if_holiday_table_stale(day)
        holiday_reason = holiday_cal.holiday_reason(day)
        if holiday_reason is not None:
            # 祝日・会社休日: skill.run を呼ばず DM も送らない。予定リマインドだけ登録する。
            return _run_holiday(users, day, holiday_reason)
        lookback_days = holiday_cal.mail_lookback_days(day)
        logger.info(
            "morning_digest_lookback",
            day=day.isoformat(),
            prev_delivery_day=holiday_cal.prev_delivery_day(day).isoformat(),
            lookback_days=lookback_days,
        )

    from teamagent.skills.morning_digest.schema import MorningDigestInput
    from teamagent.skills.morning_digest.skill import MorningDigestSkill

    try:
        concurrency = max(1, int(os.environ.get("MORNING_DIGEST_CONCURRENCY", "1")))
    except ValueError:
        concurrency = 1

    token_store = _build_token_store()
    # 本人Slack文脈（USE_SLACK_CONTEXT 有効時のみ非 None）。朝ダイジェストの自動下書きにも反映。
    from teamagent.orchestrator.factory import _build_slack_context_provider

    slack_ctx = _build_slack_context_provider()
    # Slack 返信漏れ検知（v0.3 Task1・MORNING_DIGEST_SLACK_UNREAD=1 のときのみ非 None・既定OFF）。
    # Provider は fail-open（未連携ユーザーは空）なので、flag ON でも既存挙動を壊さない。
    slack_unreplied = None
    if os.environ.get("MORNING_DIGEST_SLACK_UNREAD", "").strip().lower() in {"1", "true", "yes"}:
        from teamagent.orchestrator.factory import _build_slack_store
        from teamagent.skills._shared.slack_unreplied import SlackUnrepliedProvider

        slack_unreplied = SlackUnrepliedProvider(slack_store=_build_slack_store())
    if concurrency > 1:
        # 並列時は Bedrock クライアントを事前生成して共有（lazy-init の競合を避ける）。
        from teamagent.adapters.bedrock_client import BedrockClient

        skill = MorningDigestSkill(
            token_store=token_store,
            bedrock=BedrockClient.from_env(),
            deal_provider=slack_ctx,
            slack=slack_unreplied,
        )
    else:
        skill = MorningDigestSkill(
            token_store=token_store, deal_provider=slack_ctx, slack=slack_unreplied
        )
    # concurrency と同じく env 不正値でも落とさない。schema は 0..10、0=自動下書き無効。
    try:
        max_drafts = int(os.environ.get("MORNING_DIGEST_MAX_DRAFTS", "3"))
    except ValueError:
        max_drafts = 3
    max_drafts = min(10, max(0, max_drafts))
    if lookback_days is None:
        skill_input = MorningDigestInput(max_drafts=max_drafts)
    else:
        # 祝日明けは前の配信日までさかのぼる（祝日スキップ ON のときだけ・最低 3 日）。
        skill_input = MorningDigestInput(max_drafts=max_drafts, lookback_days=lookback_days)

    # 二重配信の防止。既定 OFF（store=None）のときは claim を 1 度も呼ばない＝現行動作。
    # ⚠️ 既定時刻の一括実行は「その日すでに送った人」を必ず除外する。ここが壊れると
    #    予約で受け取った人へ 9:30 にもう 1 通届く。
    store = _delivery_store()
    prefs_store = _preferences_store()
    origin = "scheduled" if mode == "single" else "bulk"
    # F0: 1 人ずつの結果（管理者 DM と run_done の集計用・メモリ上だけ）。
    outcomes: list[UserOutcome] = []

    def _run_one(email: str) -> str:
        return _process_user(
            skill,
            skill_input,
            email,
            store=store,
            day=day,
            origin=origin,
            sink=outcomes,
            prefs_store=prefs_store,
        )

    # concurrency=1（既定）は従来どおり逐次。>1 で人数に応じた所要時間短縮。
    if concurrency > 1:
        from concurrent.futures import ThreadPoolExecutor

        print(f"[run_morning_digest_fargate] concurrency={concurrency}", flush=True)
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            results = list(ex.map(_run_one, users))
    else:
        results = [_run_one(e) for e in users]

    summary = {"users": len(users), "delivered": 0, "skipped": 0, "errors": 0}
    for r in results:
        summary["delivered" if r == "delivered" else "skipped" if r == "skipped" else "errors"] += 1

    print(
        f"[run_morning_digest_fargate] done {json.dumps(summary, ensure_ascii=False)}",
        flush=True,
    )
    _log_run_done(outcomes, summary)
    # 管理者 DM は既定時刻の一括実行だけ（予約で 1 人ずつ走る回に毎回送らない）。
    if mode == "bulk":
        _send_admin_report(outcomes, day=day, users=len(users))
    return 0


if __name__ == "__main__":
    sys.exit(main())
