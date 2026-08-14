"""calendar_freebusy Skill の I/O スキーマ（Pydantic v2）。

read-only（freebusy 読み取りのみ・書込 API 無し）。message は LLM がそのまま返す
決定的日本語文で、空き時間の言い換え・再計算をエージェントにさせない（曜日ねつ造の防止）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CalendarFreeBusyInput(BaseModel):
    """「空いてる時間ある？」等の自由文照会の入力（全項目省略可）。"""

    date: str = Field(
        default="",
        pattern=r"^(\d{4}-\d{2}-\d{2})?$",
        description="対象日 YYYY-MM-DD（JST・省略時はサーバが明日を採用）",
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


class CalendarFreeBusyOutput(BaseModel):
    """空き時間照会の結果（読み取りのみ・カレンダーへの書込は一切しない）。"""

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
    busy_count: int = Field(default=0, ge=0, description="取得した busy 区間の数")
    error: str = Field(
        default="", description="失敗種別（not_connected/freebusy_failed 等・無ければ空）"
    )
    message: str = Field(
        default="",
        description="LLM がそのまま返す決定的日本語文（言い換え・並べ替え・再計算をしないこと）",
    )
