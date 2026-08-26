"""Fargate Scheduled Task 用 morning_digest エントリポイント。

EventBridge cron (平日 0:30 UTC = 9:30 JST) が ECS RunTask で本スクリプトを起動する。

役割:
  1. 対象ユーザー解決（env `MORNING_DIGEST_USERS` 明示優先・無ければ RDS `oauth_tokens` 動的抽出）
  2. 各ユーザーごとに `MorningDigestSkill.run()` を実行
  3. 結果を Slack DM（Block Kit）で本人に配信
  4. CloudWatch Logs に JSON 構造化ログで結果サマリ出力

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

from teamagent.hmac_durable_state import require_runtime_startup
from teamagent.hmac_keyring import MAIL_ACTION_MAX_TOKEN_TTL_S
from teamagent.skills._shared import slack_handoff as _handoff
from teamagent.skills.morning_digest import calendar_window as _calwin

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


def _fetch_connected_users_from_rds() -> list[str]:
    """RDS oauth_tokens から連携済 user_email を取得。"""
    import psycopg

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("[run_morning_digest_fargate] WARN: DATABASE_URL 未設定", file=sys.stderr)
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
        return [str(r[0]).strip().lower() for r in rows if r and r[0]]
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: RDS 連携済抽出失敗 {type(exc).__name__}",
            file=sys.stderr,
        )
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
_HANDOFF_FOOTNOTE = "※ 見出しは原文からの切り出し＋定型の語尾です（要約文は作りません）。"

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
    """limit 超過時は末尾を「…」に置き換える（1件=1行原則のための単純字数切詰）。"""
    s = s or ""
    return s if len(s) <= limit else s[: max(0, limit - 1)] + "…"


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
    """表示テキストの共通経路: 実名解決 → 生 ID 除去 → mrkdwn エスケープ。

    順序が要（escape を先にやると `<@U…>` が `&lt;@U…&gt;` になって実名解決が効かない）。
    """
    return _slack_escape(_scrub_slack_ids(_flatten_slack_text(raw, names), known_ids))


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


def _handoff_channel_chip(item: Any, names: dict[str, str], known_ids: frozenset[str]) -> str:
    """会話の chip（**戻り値は display 済み**＝呼び出し側で二重に通さないこと）。

    **`#` はチャンネル（C）のときだけ**付ける。DM / グループDM の `channel.name` は
    user_id そのものなので、`#` を無条件に前置すると本人に意味の無い生 ID が出る
    （旧描画の実害）。DM 系は種別ラベル＋**差出人の実名**を出す
    （DM が 3 件並ぶと「・DM」だけでは誰が待っているのか分からないため）。
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
    return f"{base}（{who}）" if base else who


def _handoff_card_line(
    card: Any, item: Any, names: dict[str, str], known_ids: frozenset[str]
) -> str:
    """カード 1 件 = 1 行。chip は判定層が確定済みのものを非空だけ並べる。

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
            card.effort_label,
            f"他{card.mentioned_others}名も名指し" if card.mentioned_others >= 1 else "",
            card.fold_reason,
        )
        if raw
    ]
    for chip in chips:
        if chip:
            line += f" ・{chip}"
    url = _handoff_link(card.permalink)
    if url:
        line += f" 〔<{url}|開く>〕"  # permalink は実 URL なのでエスケープしない
    return line


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


def _slack_handoff_lines(digest: Any) -> list[str]:
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
    shown = triaged.cards[:_HANDOFF_MAX_ITEMS]
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
        for card in cards:
            lines.append(_handoff_card_line(card, items[card.source_index], names, known_ids))
            if card.note:  # 補足行は「原文を見る価値が本当にある件」だけ（判定層が印を付ける）
                lines.append(f"　└ {_handoff_display(card.note, names, known_ids)}")
    lines.append("")
    lines.append(_HANDOFF_FOOTNOTE)
    return [_guard_no_raw_ids(ln, known_ids) for ln in lines]


def _slack_handoff_card_blocks(digest: Any) -> list[dict[str, Any]]:
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
    shown = triaged.cards[:_HANDOFF_MAX_ITEMS]
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
        pending_head: str | None = f"{emoji} *{head}*"
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


def _slack_handoff_block_section(digest: Any) -> list[dict[str, Any]]:
    """`_slack_handoff_card_blocks` の **fail-safe 境界**（行版 `_slack_handoff_section` と同役）。

    判定層は 800 行超の決定論ロジックを任意のユーザー本文に対して走らせる。そこで想定外の
    例外が出たときに、メールも予定も含む DM ごと落とす（`_process_user` の except が
    `return "error"` ＝ 1 通も届かない）のは割に合わない。ここで受け止めて 💬 の 1 行へ
    縮退させ、他セクションを巻き添えにしない。
    """
    try:
        return _slack_handoff_card_blocks(digest)
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: 💬 ブロック描画失敗 {type(exc).__name__}",
            flush=True,
        )
        return [{"type": "section", "text": {"type": "mrkdwn", "text": _HANDOFF_FAILED_LINE}}]


def _push_slack_handoff(blocks: list[dict[str, Any]], digest: Any) -> None:
    """💬 セクションを積む。ack ボタン OFF なら従来どおりの行描画に完全に一致させる。"""
    if _ack_button_enabled():
        blocks.extend(_slack_handoff_block_section(digest))
    else:
        _push_section_lines(blocks, _slack_handoff_section(digest))


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


def _slack_handoff_section(digest: Any) -> list[str]:
    """💬 セクションの描画（**このセクションだけの fail-safe**）。

    判定層は 800 行を超える決定論ロジックを任意のユーザー本文に対して走らせる。そこで
    想定外の例外が出たときに、メールも予定もリマインドも含む DM ごと落とす
    （`_process_user` の except が `return "error"` ＝ 1 通も届かない）のは割に合わない。
    ここで受け止めて 💬 の 1 行へ縮退させ、他セクションを巻き添えにしない。
    """
    try:
        return _slack_handoff_lines(digest)
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


def _format_block_kit(digest: Any, user_email: str) -> tuple[str, list[dict[str, Any]]]:
    """MorningDigestOutput → Slack Block Kit（要返信→未開封→当日の予定。下書きはボタン生成）。"""
    # fallback text は通知プレビュー用。slack_bot の chat_update が同一文字列を再送するため
    # ここは固定のまま（日付明示は本文側＝blocks で行う）。
    text = "メールと本日の予定をお送りします。"
    day = _digest_date(digest)
    day_label = _calwin.fmt_jst_date(day)  # 例 "8/20(木)"

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

    # --- 🔴 要返信メール（最大10件・各件にボタン）---
    if high:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"🔴 *要返信メール（{len(high)}件）*"},
            }
        )
        for m in high[:10]:
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
        # 作り置き済みの下書きが1件でもあれば、末尾に「一覧をまとめて開く」を1つだけ集約する
        # （行内の重複を排し、下書きフォルダへの導線はここに一本化）。
        if any(getattr(m, "has_draft", False) for m in high):
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

    if not high and not unread:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "📭 *メール*: 新着なし"}}
        )
        blocks.append({"type": "divider"})

    # --- 💬 Slack 返信漏れ（判定は _shared/slack_handoff・ここは並べるだけ。
    #     display は本人 DM のみ・ログ厳禁 G3/G7）---
    _push_slack_handoff(blocks, digest)
    blocks.append({"type": "divider"})

    # --- 📅 当日の予定（予定・会議室・会議リンク。display は本人 DM のみ・ログ厳禁 G3/G7）---
    # 見出しは「今日」ではなく実日付を出す（2026-08-20 の日付ずれで「今日」表記が誤りを
    # 隠したため。行側も対象日と違う予定には日付を前置する）。
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
    else:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"📅 *{day_label} の予定*: なし"},
            }
        )

    # --- ☑️ 全部確認した（既定OFF・脚注の前）---
    blocks.extend(_ack_all_blocks(digest))

    # --- 脚注（DLP 注記）---
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "_AiLa｜本人だけに届く DM です（件名・相手は実名表示／監査ログ側はマスク）。"
                        "下書きはボタンを押した時に生成し、送信はされません（手動送信）。_"
                    ),
                }
            ],
        }
    )
    return text, blocks


def _format_block_kit_compact(digest: Any, user_email: str) -> tuple[str, list[dict[str, Any]]]:
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

    # ヘッダも予定セクションも同じ「対象日」を使う（描画のたびに now を読むと、
    # 日付をまたぐ再描画でヘッダと予定の日付がズレる）。
    day = _digest_date(digest)
    day_label = _calwin.fmt_jst_date(day)  # 例 "8/20(木)"
    # fallback text は通知プレビューに出るため件数のみ（PII ゼロ）。
    text = (
        f"朝ダイジェスト｜要返信{len(high)}・未確認{len(unread)}"
        f"・Slack{slack_total}・予定{len(cal_items)}"
    )
    header = (
        f"📬 *{day_label} の朝ダイジェスト*"
        f"｜🔴{len(high)}・📬{len(unread)}・💬{slack_total}・📅{len(cal_items)}"
    )
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "divider"},
    ]

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

    # --- 🔴 要返信（最大5件・各件にボタン）---
    if high:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"🔴 *要返信（{len(high)}件）*"}}
        )
        for m in high[:5]:
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
        rem = len(high) - 5
        if rem > 0:
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

    # --- 📬 未確認（最大5件・1件=1行・要約なし）---
    if unread:
        lines = [f"📬 *未確認（{len(unread)}件）*"]
        for m in unread[:5]:
            subj, who = _subj_who(m)
            lines.append(f"• {who}: *{subj}*")
        rem = len(unread) - 5
        if rem > 0:
            lines.append(f"• 〈他{rem}件〉 <{_GMAIL_INBOX_URL}|受信トレイで見る>")
        _push_lines(lines)
        blocks.append({"type": "divider"})

    if not high and not unread:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "📭 *メール*: 新着なし"}}
        )
        blocks.append({"type": "divider"})

    # --- 💬 Slack 返信漏れ（判定は _shared/slack_handoff・ここは並べるだけ。
    #     display は本人 DM のみ・ログ厳禁 G3/G7）---
    _push_slack_handoff(blocks, digest)
    blocks.append({"type": "divider"})

    # --- 📅 当日の予定（最大10件・1行形式は旧描画と共通・見出しは実日付）---
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
        rem = len(cal_items) - 10
        if rem > 0:
            lines.append(f"• 〈他{rem}件〉 <{_CALENDAR_URL}|カレンダーを開く>")
        _push_lines(lines)
    else:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"📅 *{day_label} の予定*: なし"},
            }
        )

    # --- 末尾（☑️ 全部確認した + 脚注（DLP 注記・旧描画と同一））---
    # 打ち切りに巻き込ませないため、本文とは別に組んで最後に足す。
    tail: list[dict[str, Any]] = _ack_all_blocks(digest)
    tail.append({"type": "divider"})
    tail.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "_AiLa｜本人だけに届く DM です（件名・相手は実名表示／監査ログ側はマスク）。"
                        "下書きはボタンを押した時に生成し、送信はされません（手動送信）。_"
                    ),
                }
            ],
        }
    )

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


def _reminders_enabled() -> bool:
    """MORNING_DIGEST_REMINDERS=1 のときのみ予定リマインドを登録（既定OFF・§10 E1-2）。"""
    return os.environ.get("MORNING_DIGEST_REMINDERS", "").strip().lower() in {"1", "true", "yes"}


def _schedule_event_reminders(digest: Any, im_channel: str) -> int:
    """当日予定の「開始 N 分前」リマインドを EventBridge Scheduler に登録する（v0.3 Task5）。

    - 対象: start_at が「今から lead+1 分より先」の予定のみ（過ぎた/直近すぎる予定は skip）
    - 終日予定（date のみ）は対象外
    - payload に short title（≤60字）を載せる（2026-07-14・本人の予定を本人 DM に出す用途に
      限定。「何の予定か分からない」の解消・ユーザー要望）。Lambda はタイトルをログに出さない
    - schedule 名は channel×開始時刻から決定的＝再実行でも二重登録しない（Conflict→成功扱い）
    """
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

    now = _dt.datetime.now(tz=_JST)
    count = 0
    for ev in list(getattr(digest, "calendar_events", []) or []):
        start_iso = str(getattr(ev, "start_at", "") or "")
        # 終日は API 由来フラグを優先（"…T00:00:00Z" 形の終日を時刻付きと誤認しない）。
        if bool(getattr(ev, "all_day", False)) or "T" not in start_iso:
            continue  # 終日 or 不明
        start = _calwin.parse_jst_datetime(start_iso)  # naive は JST とみなす（UTC 誤解釈防止）
        if start is None:
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


def _process_user(skill: Any, skill_input: Any, email: str) -> str:
    """1 ユーザー分を処理し "delivered"/"skipped"/"error" を返す（例外は内側で封じ込め）。

    スレッドから呼ぶため副作用は print（stderr・マスク済）と Slack 配信のみ・共有状態を書かない。
    """
    from teamagent.skills.base import SkillContext

    request_id = f"morning-{uuid.uuid4().hex[:10]}"
    ctx = SkillContext(request_id=request_id, metadata={"user_email": email})
    try:
        digest = skill.run(skill_input, ctx)
    except PermissionError:
        return "skipped"  # 未連携
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: {_mask_email(email)} skill 失敗 "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return "error"
    # 配信(整形+Slack)も封じ込め（1 人の失敗で全体を落とさない）。
    try:
        if _compact_enabled():
            text, blocks = _format_block_kit_compact(digest, email)
        else:
            text, blocks = _format_block_kit(digest, email)
        delivered, im_channel = asyncio.run(_deliver_to_slack(email, text, blocks))
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: {_mask_email(email)} 配信失敗 "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return "error"
    if delivered:
        digest.delivered = True
        # v0.3 Task5: 当日予定の開始前リマインドをワンタイム登録（flag 既定OFF・fail-open＝
        # 登録失敗してもダイジェスト配信の成功は変えない）。
        if im_channel and _reminders_enabled():
            try:
                n = _schedule_event_reminders(digest, im_channel)
                if n:
                    print(f"[run_morning_digest_fargate] reminders scheduled: {n}", flush=True)
            except Exception as exc:
                print(
                    f"[run_morning_digest_fargate] WARN: reminder 登録失敗 {type(exc).__name__}",
                    file=sys.stderr,
                )
        return "delivered"
    return "error"


def main() -> int:
    require_runtime_startup((("mail_action", MAIL_ACTION_MAX_TOKEN_TTL_S),))
    users = _resolve_target_users()
    if not users:
        print("[run_morning_digest_fargate] no target users (env+RDS empty)", flush=True)
        return 0
    print(f"[run_morning_digest_fargate] start users={len(users)}", flush=True)

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
    skill_input = MorningDigestInput(max_drafts=max_drafts)

    # concurrency=1（既定）は従来どおり逐次。>1 で人数に応じた所要時間短縮。
    if concurrency > 1:
        from concurrent.futures import ThreadPoolExecutor

        print(f"[run_morning_digest_fargate] concurrency={concurrency}", flush=True)
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            results = list(ex.map(lambda e: _process_user(skill, skill_input, e), users))
    else:
        results = [_process_user(skill, skill_input, e) for e in users]

    summary = {"users": len(users), "delivered": 0, "skipped": 0, "errors": 0}
    for r in results:
        summary["delivered" if r == "delivered" else "skipped" if r == "skipped" else "errors"] += 1

    print(
        f"[run_morning_digest_fargate] done {json.dumps(summary, ensure_ascii=False)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
