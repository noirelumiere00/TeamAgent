"""朝ダイジェストの「本人ごとの設定」（DM で言われた指示を永続化したもの）。

書く側（``digest_settings`` ツール・mcp）と読む側（``scripts/run_morning_digest_fargate.py``・
毎朝の配信）の **唯一の定義**。保存先は migration 0030 の ``digest_preferences``（1 人 1 行・
JSON）。DB は「object であること」しか縛らないので、項目の妥当性はここで書く時も読む時も
検査する（壊れた行・古い形の行を読んでも、配信側を落とさない）。

設定できること（初版）:
  - 配信: 止める / 日付まで休止 / 送る曜日（平日のうち）
  - 欄: 要返信・未確認メール・Slack 返信漏れ・予定・事例ブリーフ の表示/非表示と件数
  - 返信の下書きを自動で作るか
  - 予定の直前リマインド: 止める / 何分前 / 予定名に特定の語を含むものは送らない

⚠️ 読めない・壊れているときは **既定（＝今までどおりの配信）** に倒す（fail-open）。
   止めたはずの人に 1 通届くのは「うるさい」で済むが、設定表の障害で誰にも届かないのは
   見逃しを生む（digest_ack と同じ判断）。
"""

from __future__ import annotations

import datetime as _dt
import unicodedata
from typing import Any, Final, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from teamagent.skills.morning_digest import calendar_window as _calwin

logger = structlog.get_logger(__name__)

#: 欄の鍵 → 表示名。並びは DM の並び順。
SECTION_LABELS: Final[dict[str, str]] = {
    "reply": "要返信メール",
    "unread": "未確認メール",
    "slack": "Slack 返信漏れ",
    "calendar": "今日の予定",
    "brief": "社外MTGの事例ブリーフ",
}
SECTION_KEYS: Final[tuple[str, ...]] = tuple(SECTION_LABELS)

#: 件数を変えられる欄 → (下限, 上限, 既定)。既定は現行の compact 描画の固定値と同じ。
#: 上限は Slack の blocks 50 個制限に収まる範囲（要返信と Slack は 1 件で複数ブロック使う）。
LIMIT_BOUNDS: Final[dict[str, tuple[int, int, int]]] = {
    "reply": (1, 8, 5),
    "unread": (1, 10, 5),
    "slack": (1, 8, 5),
    "calendar": (1, 15, 10),
}

#: 配信は平日だけ（EventBridge の rule が月〜金）。曜日は 0=月 … 4=金。
WEEKDAY_KEYS: Final[tuple[str, ...]] = ("mon", "tue", "wed", "thu", "fri")
_WEEKDAY_JA: Final[str] = "月火水木金"
ALL_WEEKDAYS: Final[tuple[int, ...]] = (0, 1, 2, 3, 4)

MAX_PAUSE_DAYS: Final[int] = 180
MAX_SKIP_KEYWORDS: Final[int] = 10
MAX_SKIP_KEYWORD_LEN: Final[int] = 20

#: 配信しない理由（配信側のログ・管理者 DM の集計に出す。個人の設定内容は出さない）。
SKIP_OFF: Final[str] = "pref_off"
SKIP_PAUSED: Final[str] = "pref_paused"
SKIP_WEEKDAY: Final[str] = "pref_weekday"


def normalize_keyword(raw: str) -> str:
    """予定名の照合用に正規化する（全角/半角・大文字小文字の差を吸収）。"""
    return unicodedata.normalize("NFKC", raw or "").strip().casefold()


class DigestPreferences(BaseModel):
    """1 人分の設定。既定値＝今までどおりの配信。"""

    # 知らない鍵は読み捨てる（新しい版が書いた行を古い配信側が読んでも落ちない）。
    model_config = ConfigDict(extra="ignore", frozen=True)

    delivery: bool = True
    paused_until: _dt.date | None = None
    weekdays: tuple[int, ...] = ALL_WEEKDAYS
    hidden_sections: tuple[str, ...] = ()
    limits: dict[str, int] = Field(default_factory=dict)
    auto_drafts: bool = True
    reminders: bool = True
    reminder_lead_minutes: int | None = Field(default=None, ge=1, le=60)
    reminder_skip_keywords: tuple[str, ...] = ()

    @field_validator("weekdays")
    @classmethod
    def _v_weekdays(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        days = tuple(sorted(set(v)))
        if not days or any(d not in ALL_WEEKDAYS for d in days):
            raise ValueError("weekdays must be a non-empty subset of Mon-Fri")
        return days

    @field_validator("hidden_sections")
    @classmethod
    def _v_hidden(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if any(k not in SECTION_LABELS for k in v):
            raise ValueError("unknown section")
        return tuple(k for k in SECTION_KEYS if k in set(v))

    @field_validator("limits")
    @classmethod
    def _v_limits(cls, v: dict[str, int]) -> dict[str, int]:
        out: dict[str, int] = {}
        for key, n in v.items():
            if key not in LIMIT_BOUNDS or type(n) is not int:
                raise ValueError("invalid limit")
            lo, hi, default = LIMIT_BOUNDS[key]
            if not lo <= n <= hi:
                raise ValueError("limit out of range")
            if n != default:  # 既定値は持たない（「既定に戻った」を 1 通りで表す）
                out[key] = n
        return out

    @field_validator("reminder_skip_keywords")
    @classmethod
    def _v_keywords(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        seen: dict[str, str] = {}
        for raw in v:
            word = unicodedata.normalize("NFKC", raw or "").strip()
            if not word or len(word) > MAX_SKIP_KEYWORD_LEN:
                raise ValueError("invalid keyword")
            if any(unicodedata.category(ch).startswith("C") for ch in word):
                raise ValueError("invalid keyword")
            seen.setdefault(word.casefold(), word)
        if len(seen) > MAX_SKIP_KEYWORDS:
            raise ValueError("too many keywords")
        return tuple(seen.values())

    # --- 読む側の問い合わせ ---

    def is_default(self) -> bool:
        return self == DigestPreferences()

    def shows(self, section: str) -> bool:
        return section not in self.hidden_sections

    def limit(self, section: str) -> int:
        return self.limits.get(section, LIMIT_BOUNDS[section][2])

    def skip_reason(self, day: _dt.date) -> str | None:
        """その日の朝ダイジェストを送らない理由（送るなら None）。"""
        if not self.delivery:
            return SKIP_OFF
        if self.paused_until is not None and day <= self.paused_until:
            return SKIP_PAUSED
        if day.weekday() in ALL_WEEKDAYS and day.weekday() not in self.weekdays:
            return SKIP_WEEKDAY
        return None

    def reminder_allowed(self, title: str) -> bool:
        """この予定に直前リマインドを送ってよいか。"""
        if not self.reminders:
            return False
        name = normalize_keyword(title)
        return not any(normalize_keyword(k) in name for k in self.reminder_skip_keywords)

    def to_storage(self) -> dict[str, Any]:
        """DB に書く形（既定値の項目は書かない＝行が小さく、既定の変更に追従する）。"""
        return self.model_dump(mode="json", exclude_defaults=True)


DEFAULT_PREFERENCES: Final[DigestPreferences] = DigestPreferences()


def from_storage(raw: Any) -> DigestPreferences:
    """DB の JSON から読む。壊れていれば ValueError（呼び出し側が既定へ倒す）。"""
    if not isinstance(raw, dict):
        raise ValueError("digest preferences must be an object")
    try:
        return DigestPreferences.model_validate(raw)
    except ValidationError as exc:
        raise ValueError("invalid digest preferences") from exc


class PreferencesReader(Protocol):
    def get(self, user_email: str, *, request_id: str) -> tuple[dict[str, Any] | None, int]: ...


def load_preferences(
    store: PreferencesReader | None, email: str, *, request_id: str
) -> DigestPreferences:
    """配信側の読み取り。障害・壊れた行は既定へ倒す（fail-open・理由は module docstring）。"""
    if store is None:
        return DEFAULT_PREFERENCES
    try:
        raw, _version = store.get(email, request_id=request_id)
    except Exception as exc:
        logger.warning("digest_prefs_read_failed", request_id=request_id, error=type(exc).__name__)
        return DEFAULT_PREFERENCES
    if raw is None:
        return DEFAULT_PREFERENCES
    try:
        return from_storage(raw)
    except ValueError:
        logger.warning("digest_prefs_invalid_row", request_id=request_id)
        return DEFAULT_PREFERENCES


# --- 本人への説明文（digest_settings の返答） ---


def _weekday_text(days: tuple[int, ...]) -> str:
    if days == ALL_WEEKDAYS:
        return "平日（月〜金）"
    return "・".join(_WEEKDAY_JA[d] for d in days) + "曜"


def describe(prefs: DigestPreferences, today: _dt.date, *, default_lead_minutes: int) -> str:
    """いまの設定を本人向けの箇条書きで返す（DM にそのまま出す文）。"""
    if not prefs.delivery:
        delivery = "止めています（「朝のサマリーを再開して」で戻せます）"
    elif prefs.paused_until is not None and prefs.paused_until >= today:
        resume = prefs.paused_until + _dt.timedelta(days=1)
        delivery = (
            f"{_calwin.fmt_jst_date(prefs.paused_until)} まで休み"
            f"（{_calwin.fmt_jst_date(resume)} 以降の{_weekday_text(prefs.weekdays)}に再開）"
        )
    else:
        delivery = f"{_weekday_text(prefs.weekdays)}の朝に届きます"

    shown: list[str] = []
    for key, label in SECTION_LABELS.items():
        if not prefs.shows(key):
            continue
        shown.append(f"{label}（最大{prefs.limit(key)}件）" if key in LIMIT_BOUNDS else label)
    hidden = [SECTION_LABELS[k] for k in prefs.hidden_sections]

    if prefs.reminders:
        lead = prefs.reminder_lead_minutes or default_lead_minutes
        reminder = f"開始 {lead} 分前に届きます"
        if prefs.reminder_skip_keywords:
            words = "・".join(f"「{w}」" for w in prefs.reminder_skip_keywords)
            reminder += f"（予定名に {words} を含む予定は送りません）"
    else:
        reminder = "止めています"

    drafts = "重要なメールには自動で作ります" if prefs.auto_drafts else "自動では作りません"
    lines = [
        "🛠 *朝のサマリーの設定*",
        f"• 配信: {delivery}",
        f"• 載せる欄: {'・'.join(shown) if shown else 'なし'}",
        f"• 載せない欄: {'・'.join(hidden) if hidden else 'なし'}",
        f"• 返信の下書き: {drafts}",
        f"• 予定の直前リマインド: {reminder}",
    ]
    return "\n".join(lines)


__all__ = [
    "ALL_WEEKDAYS",
    "DEFAULT_PREFERENCES",
    "LIMIT_BOUNDS",
    "MAX_PAUSE_DAYS",
    "MAX_SKIP_KEYWORDS",
    "SECTION_KEYS",
    "SECTION_LABELS",
    "SKIP_OFF",
    "SKIP_PAUSED",
    "SKIP_WEEKDAY",
    "WEEKDAY_KEYS",
    "DigestPreferences",
    "PreferencesReader",
    "describe",
    "from_storage",
    "load_preferences",
    "normalize_keyword",
]
