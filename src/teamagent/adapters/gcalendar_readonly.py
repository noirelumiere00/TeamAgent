"""カレンダー読み取り専用 facade（書込ゼロを **型で** 担保する一次防御）。

なぜ denylist では足りないか（実コードで反証済み）:
``_GCAL_DESTRUCTIVE_METHODS``（``adapters/gcalendar_client.py``）は **``events.insert`` を
意図的に通している**（「構造化された insert_event 1 本に集約」する設計）。したがって
「破壊的メソッドは封鎖済みだから skill に client を渡してよい」は成り立たない。
``GCalendarClient`` を握っている限り ``insert_event`` は 1 行で呼べる。

そこで pre_meeting_brief / digest planner には ``GCalendarClient`` そのものを渡さず、
本 facade だけを渡す。転送するのは ``list_events`` **のみ**で、それ以外の属性アクセスは
``AttributeError`` になる（``__getattr__`` を実装しない普通のクラス＝既定で到達不能）。
AST テストは二次防御であって、一次防御はこの型である。

⚠️ 主張の正確な範囲（誇張しない）: これは **ガードレールであって sandbox ではない**。
保持するのは ``list_events`` の bound method 1 本だけで、``GCalendarClient`` を指す名前の
ついた属性は存在しない。それでも Python では bound method の ``__self__`` から生 client へ
到達できる（``facade._list_events.__self__.insert_event(...)``）。つまりこの型が保証する
のは「書込 API を **うっかり** 呼べる経路が無い」ことであって、「原理的に到達不能」では
ない。後者を主張すると、レビューで一段深く追った人がこの facade 全体を信用しなくなる。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 型のみ
    from teamagent.adapters.gcalendar_client import CalendarEvent


class ReadOnlyCalendar:
    """``list_events`` だけを転送する facade。他の属性は存在しない。

    ⚠️ 属性を足すときは「読み取りか」を必ず確認すること。``insert_event`` /
    ``freebusy`` / ``_ensure_service`` を転送した瞬間に、この facade の存在意義
    （書込経路が型として無い）が消える。テスト ``test_readonly_facade`` が
    公開属性の集合を厳密一致で固定している。

    保持するのは ``list_events`` の bound method だけ。生 client を指す属性
    （旧 ``_inner``）は持たない — ただしモジュール冒頭のとおり、``__self__`` 経由の
    到達までは塞いでいない。
    """

    __slots__ = ("_list_events",)

    def __init__(self, inner: Any) -> None:
        # 生 client を属性に持たない。束ねるのは読み取りメソッド 1 本だけ。
        self._list_events = inner.list_events

    def list_events(
        self,
        request_id: str,
        *,
        query: str | None = None,
        time_min: str | None = None,
        time_max: str | None = None,
        max_results: int = 20,
        calendar_id: str = "primary",
        want_description: bool = False,
    ) -> list[CalendarEvent]:
        """``GCalendarClient.list_events`` へそのまま転送する（読み取りのみ）。"""
        result: list[CalendarEvent] = self._list_events(
            request_id,
            query=query,
            time_min=time_min,
            time_max=time_max,
            max_results=max_results,
            calendar_id=calendar_id,
            want_description=want_description,
        )
        return result


__all__ = ["ReadOnlyCalendar"]
