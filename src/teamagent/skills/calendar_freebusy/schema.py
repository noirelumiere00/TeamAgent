"""calendar_freebusy Skill の I/O スキーマ（Pydantic v2）。

read-only（freebusy / events.list の読み取りのみ・書込 API 無し）。message は LLM が
そのまま返す決定的日本語文で、空き時間・予定の言い換えや再計算をエージェントにさせない
（曜日ねつ造の防止）。

mode で 2 面を持つ:
  - ``free``   （既定）: 空きウィンドウ + 開始候補（freebusy）
  - ``agenda`` : その日の予定一覧（events.list・同じ calendar.readonly スコープ）
新規ツールを足さないのは、OpenClaw の toolFilter/ツール定義を変えずに済ませるため
（＝OC イメージ再ビルドと再連携を発生させない）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CalendarFreeBusyInput(BaseModel):
    """「空いてる時間ある？」「明日の予定は？」等の自由文照会の入力（全項目省略可）。"""

    mode: Literal["free", "agenda"] = Field(
        default="free",
        description=(
            "free=空き時間（既定）/ agenda=予定の一覧。"
            "『予定』『何がある』『スケジュール』を聞かれたら agenda、"
            "『空いてる』『◯分入る？』は free。"
        ),
    )
    date: str = Field(
        default="",
        pattern=r"^(\d{4}-\d{2}-\d{2})?$",
        description=(
            "対象日 YYYY-MM-DD（JST）。**自分で今日の日付を計算して入れないこと**。"
            "依頼文に具体日が書かれている時だけ使い、"
            "『今日』『明日』は relative_day で指定する（省略時はサーバが明日を採用）。"
        ),
    )
    relative_day: Literal["", "today", "tomorrow"] = Field(
        default="",
        description=(
            "『今日』なら 'today'、『明日』なら 'tomorrow'（省略時も明日）。"
            "実日付の算出はサーバが JST で行う（LLM の日付計算を使わない）。"
            "date が指定されている場合は date が優先。"
        ),
    )
    days: int = Field(
        default=1, ge=1, le=7, description="対象日から何日ぶん照会するか（1〜7・既定1）"
    )
    duration_min: int = Field(
        default=60, ge=15, le=480, description="入れたい打合せの所要時間（分・15〜480・既定60）"
    )


class FreeWindow(BaseModel):
    """空きウィンドウ 1 件（ISO 日時と人間可読ラベル）。"""

    start: str = Field(description="空きの開始（ISO 8601・JST）")
    end: str = Field(description="空きの終了（ISO 8601・JST）")
    label: str = Field(description="「8/15(土) 09:00〜18:00」形式の表示用ラベル")


class AgendaItem(BaseModel):
    """対象日にかかっている予定 1 件（mode='agenda'・**マスク済みタイトル**）。"""

    start: str = Field(description="予定の開始（ISO 8601・終日は YYYY-MM-DD）")
    end: str = Field(default="", description="予定の終了（ISO 8601・終日は排他的な YYYY-MM-DD）")
    title: str = Field(
        default="",
        description=(
            "予定タイトル（PII マスク済み・60字で短縮）。"
            "[REDACTED_PII] はマスクの結果であり、原文の復元を試みないこと。"
        ),
    )
    all_day: bool = Field(default=False, description="終日予定か")
    label: str = Field(
        default="",
        description=(
            "「8/21(金) 10:00〜11:00 定例MTG」形式の表示用ラベル。"
            "表示にはこの文字列をそのまま使い、start/end から時刻・曜日を再計算しないこと"
        ),
    )


class CalendarFreeBusyOutput(BaseModel):
    """空き時間／予定一覧の結果（読み取りのみ・カレンダーへの書込は一切しない）。"""

    date_label: str = Field(default="", description="対象日の「8/15(土)」形式ラベル")
    non_business_note: str = Field(
        default="", description="対象日が土日のときの休日注記（祝日は未判定・平日は空）"
    )
    free_windows: list[FreeWindow] = Field(
        default_factory=list, description="営業時間内の空きウィンドウ（早い順）"
    )
    candidates: list[str] = Field(
        default_factory=list, description="duration_min が収まる開始候補ラベル（早い順）"
    )
    events: list[AgendaItem] = Field(
        default_factory=list,
        description=(
            "mode='agenda' のときの予定一覧（対象日ごとに終日→開始時刻の順）。"
            "mode='free' では常に空。"
        ),
    )
    busy_count: int = Field(default=0, ge=0, description="取得した busy 区間の数")
    error: str = Field(
        default="",
        description=(
            "失敗種別（not_connected/freebusy_failed/agenda_failed/bad_date 等・無ければ空）。"
            "**空なら取得は成功している**＝予定 0 件を『取得できなかった』と言い換えないこと。"
        ),
    )
    message: str = Field(
        default="",
        description="LLM がそのまま返す決定的日本語文（言い換え・並べ替え・再計算をしないこと）",
    )
