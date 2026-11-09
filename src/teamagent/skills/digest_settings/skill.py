"""digest_settings Skill 本体 — 朝ダイジェストの本人ごとの設定を見る/変える/既定に戻す。

利用者が Aico の DM で「朝のサマリー、Slack の欄はいらない」「来週まで止めて」
と言ったときに呼ばれ、migration 0030 の ``digest_preferences``
に本人の設定として保存する。毎朝の配信（scripts/run_morning_digest_fargate.py）がそれを読む。
本人が変えるか戻すまで、ずっと効く。

⚠️ 死守ライン:
  本人限定: 触るのは ``user_email``（署名済み Slack user からサーバ側で解決したもの）の行だけ。
    DB 側も RLS で本人行に束縛（アプリのバグだけが唯一の防壁にならないように）。
  DM 限定: 署名済み claim の channel_id が D 始まり（本人と Aico の 1:1 DM）のときだけ動く。
    チャンネルで「止めて」と言われても変えない（誰の発言で誰の設定が変わるかを曖昧にしない）。
  黙って成功と言わない: 書けなかったら「変えられなかった」と返す。書けた内容は毎回
    全項目の一覧で返す（本人が「何がどうなっているか」をいつでも確かめられるように）。
  外部送信ゼロ: Slack・Google の API は呼ばない。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from teamagent.skills._shared.user_context import USER_CONTEXT_RULE
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.digest_settings.schema import DigestSettingsInput, DigestSettingsOutput
from teamagent.skills.morning_digest import calendar_window as _calwin
from teamagent.skills.morning_digest.preferences import (
    DEFAULT_PREFERENCES,
    LIMIT_BOUNDS,
    MAX_PAUSE_DAYS,
    SECTION_LABELS,
    WEEKDAY_KEYS,
    DigestPreferences,
    describe,
    from_storage,
)

_MSG_DM_ONLY = (
    "朝のサマリーの設定は、Aico との DM で見たり変えたりできます。DM でもう一度言ってください。"
)
_MSG_STORE_FAILED = "設定を読み書きできませんでした。時間をおいて、もう一度お試しください。"
_MSG_CONFLICT = (
    "ちょうど別の操作で設定が変わったため、変えられませんでした。もう一度お試しください。"
)
_FOOTER = (
    "変えたいときは、この DM でそのまま言ってください"
    "（例:「Slack の欄はいらない」「来週まで止めて」）。"
)
_APPLY_NOTE = "次の朝のサマリーから反映されます。"
_MSG_REMINDERS_UNAVAILABLE = "予定のリマインドは今は提供していません"
_REMINDER_INPUT_FIELDS = (
    "reminders",
    "reminder_lead_minutes",
    "reminder_personal_blocks",
    "reminder_skip_add",
    "reminder_skip_remove",
)

#: 変更点の表示名（ログにも鍵だけ出す＝設定内容は出さない）。
_FIELD_LABELS: dict[str, str] = {
    "delivery": "配信",
    "paused_until": "休止",
    "weekdays": "配信する曜日",
    "hidden_sections": "載せる欄",
    "limits": "件数",
    "auto_drafts": "返信の下書き",
}

_LIMIT_FIELDS: dict[str, str] = {
    "reply": "limit_reply",
    "unread": "limit_unread",
    "slack": "limit_slack",
    "calendar": "limit_calendar",
}


class _InvalidRequestError(ValueError):
    """本人に理由を返して止める入力エラー（message は本人向けの日本語）。"""


def _apply(
    current: DigestPreferences, req: DigestSettingsInput, today: _dt.date
) -> tuple[DigestPreferences, list[str]]:
    """変更を当てた新しい設定と、本人に添える注記（丸めた等）を返す。"""
    if req.action == "reset":
        # 廃止したリマインドの保存値は、サマリーを既定に戻しても残す。
        reminder_values = {
            k: v for k, v in current.model_dump().items() if k.startswith("reminder")
        }
        return DEFAULT_PREFERENCES.model_copy(
            update={
                **reminder_values,
                "daily_schedule_template": current.daily_schedule_template,
            }
        ), []
    data: dict[str, Any] = current.model_dump()
    notes: list[str] = []

    if req.delivery is False:
        data["delivery"] = False
    elif req.delivery is True:
        data["delivery"] = True
        data["paused_until"] = None

    if req.pause_until is not None or req.pause_days is not None:
        until = req.pause_until or today + _dt.timedelta(days=(req.pause_days or 1) - 1)
        if until < today:
            raise _InvalidRequestError(
                "休止の終わりが過去の日付です。いつまで止めるかをもう一度教えてください。"
            )
        if until > today + _dt.timedelta(days=MAX_PAUSE_DAYS):
            raise _InvalidRequestError(
                f"休止は {MAX_PAUSE_DAYS} 日先までです。"
                "ずっと止めたいときは「朝のサマリーを止めて」と言ってください。"
            )
        data["paused_until"] = until
        data["delivery"] = True

    if req.weekdays is not None:
        if not req.weekdays:
            raise _InvalidRequestError(
                "送る曜日が空です。全部止めたいときは「朝のサマリーを止めて」と言ってください。"
            )
        data["weekdays"] = tuple(WEEKDAY_KEYS.index(d) for d in req.weekdays)

    hide = set(req.hide_sections or ())
    show = set(req.show_sections or ())
    if hide & show:
        raise _InvalidRequestError(
            "同じ欄を「載せる」と「載せない」の両方に指定しています。どちらかを教えてください。"
        )
    if hide or show:
        data["hidden_sections"] = tuple((set(current.hidden_sections) | hide) - show)

    limits = dict(current.limits)
    for section, field in _LIMIT_FIELDS.items():
        raw = getattr(req, field)
        if raw is None:
            continue
        lo, hi, _default = LIMIT_BOUNDS[section]
        n = min(hi, max(lo, int(raw)))
        if n != raw:
            notes.append(
                f"{SECTION_LABELS[section]}の件数は {lo}〜{hi} 件なので {n} 件にしました。"
            )
        limits[section] = n
    data["limits"] = limits

    if req.auto_drafts is not None:
        data["auto_drafts"] = req.auto_drafts
    try:
        return DigestPreferences.model_validate(data), notes
    except ValidationError as exc:
        raise _InvalidRequestError(
            "その設定は使えない値を含んでいます。言い方を変えてもう一度お願いします。"
        ) from exc


def _changed_fields(old: DigestPreferences, new: DigestPreferences) -> list[str]:
    return [k for k in _FIELD_LABELS if getattr(old, k) != getattr(new, k)]


@register
class DigestSettingsSkill(BaseSkill[DigestSettingsInput, DigestSettingsOutput]):
    """朝ダイジェストの本人ごとの設定を見る/変える/既定に戻す Skill。"""

    name: ClassVar[str] = "digest_settings"
    description: ClassVar[str] = (
        "朝のサマリー/朝ダイジェストの設定確認・変更・停止・再開。指定された項目だけ渡す。DM only。"
    ) + USER_CONTEXT_RULE
    input_schema: ClassVar[type[BaseModel]] = DigestSettingsInput
    output_schema: ClassVar[type[BaseModel]] = DigestSettingsOutput

    def __init__(self, store: Any | None = None) -> None:
        # store は差し込み可（テスト用）。既定は遅延生成＝import 時に DB を触らない。
        self._store = store

    def _get_store(self) -> Any:
        if self._store is None:
            from teamagent.adapters.digest_preferences_store import DigestPreferencesStore

            self._store = DigestPreferencesStore()
        return self._store

    def _read(self, email: str, request_id: str) -> tuple[DigestPreferences, int]:
        raw, version = self._get_store().get(email, request_id=request_id)
        if raw is None:
            return DEFAULT_PREFERENCES, version
        try:
            return from_storage(raw), version
        except ValueError:
            # 壊れた行は既定として扱い、次の書き込みで上書きさせる（版は保つ＝楽観ロックは効く）。
            return DEFAULT_PREFERENCES, version

    def run(self, input: DigestSettingsInput, ctx: SkillContext) -> DigestSettingsOutput:
        log = ctx.bind_logger(self.name)

        requester = ctx.metadata.get("user_email")
        if not requester or not isinstance(requester, str) or not requester.strip():
            raise PermissionError("digest_settings は本人 user_email が必須です（fail-closed）")
        requester = requester.strip()

        # DM 限定。channel_id は署名済み caller claim 由来（server._resolve_metadata）。
        # 署名検証を通っていない呼び出し（identity_verified が True でない）も拒否する。
        channel = str(ctx.metadata.get("channel_id", "") or "").strip()
        if not channel.startswith("D") or ctx.metadata.get("identity_verified") is not True:
            log.info("digest_settings_not_dm")
            return DigestSettingsOutput(message=_MSG_DM_ONLY, error="dm_only")

        if any(getattr(input, field) is not None for field in _REMINDER_INPUT_FIELDS):
            return DigestSettingsOutput(message=_MSG_REMINDERS_UNAVAILABLE, error="unavailable")

        today = _calwin.now_jst().date()

        def _render(prefs: DigestPreferences, head: str = "") -> str:
            body = describe(prefs, today)
            return "\n\n".join(p for p in (head, body, _FOOTER) if p)

        try:
            current, version = self._read(requester, ctx.request_id)
        except Exception as exc:
            log.warning("digest_settings_read_failed", error=type(exc).__name__)
            return DigestSettingsOutput(message=_MSG_STORE_FAILED, error="store_failed")

        if input.action == "show":
            log.info("digest_settings_shown", custom=not current.is_default())
            return DigestSettingsOutput(message=_render(current))

        # update/reset: 読んだ版に当てて書く。途中で他が書いていたら 1 回だけ読み直して当て直す。
        for attempt in range(2):
            try:
                new, notes = _apply(current, input, today)
            except _InvalidRequestError as exc:
                log.info("digest_settings_invalid")
                return DigestSettingsOutput(message=_render(current, f"⚠️ {exc}"), error="invalid")
            changed = _changed_fields(current, new)
            if not changed and input.action != "reset":
                log.info("digest_settings_no_change")
                head = "\n".join([*notes, "設定は今のままです（変わった項目はありません）。"])
                return DigestSettingsOutput(message=_render(current, head), error="no_change")
            try:
                saved = self._get_store().save(
                    requester, new.to_storage(), expected_version=version, request_id=ctx.request_id
                )
            except Exception as exc:
                log.warning("digest_settings_save_failed", error=type(exc).__name__)
                return DigestSettingsOutput(message=_MSG_STORE_FAILED, error="store_failed")
            if saved is not None:
                labels = "・".join(_FIELD_LABELS[k] for k in changed)
                title = (
                    "↩︎ 既定の設定に戻しました。"
                    if input.action == "reset"
                    else f"✅ 変えました（{labels}）。"
                )
                head = "\n".join([title, *notes, _APPLY_NOTE])
                log.info("digest_settings_saved", changed=changed, attempt=attempt)
                return DigestSettingsOutput(message=_render(new, head), changed=changed)
            try:
                current, version = self._read(requester, ctx.request_id)
            except Exception as exc:
                log.warning("digest_settings_read_failed", error=type(exc).__name__)
                return DigestSettingsOutput(message=_MSG_STORE_FAILED, error="store_failed")

        log.warning("digest_settings_conflict")
        return DigestSettingsOutput(message=_MSG_CONFLICT, error="conflict")
