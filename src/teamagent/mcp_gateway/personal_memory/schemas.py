"""本人メモの 3 ツールの入力（pydantic v2・extra='forbid'）。

引数名は utterance / has_attachment / action / item_no だけにする。
``query`` は usage_events に本文として残り（server._record_usage）、
``query``・``goal``・``text``・``message``・``prompt`` は「連携」の自動振り替え
（skills/_shared/connect_intent.FREE_TEXT_FIELDS）の走査対象なので使わない。契約テストで固定する。

自由文を受け取るのは observe の utterance だけ。コマンドは plugin が全文一致で判定し、
サーバには列挙値（action）だけが届く。
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

MAX_UTTERANCE_CHARS: Final = 800
MAX_ITEM_NO: Final = 80

CommandAction = Literal[
    "list", "forget", "freeze", "resume", "erase_request", "erase_confirm", "notice_ack"
]
COMMAND_ACTIONS: Final[tuple[str, ...]] = (
    "list",
    "forget",
    "freeze",
    "resume",
    "erase_request",
    "erase_confirm",
    "notice_ack",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ObserveInput(_Strict):
    """DM の 1 発話（本人の発言だけ・bot の発言は送らない）。"""

    utterance: str = Field(min_length=1, max_length=MAX_UTTERANCE_CHARS)
    # 必須（渡し忘れで添付つきの発話を本文だけとして学習しない）
    has_attachment: StrictBool


class ContextInput(_Strict):
    """返信前に本人メモを読む（引数なし）。"""


class CommandInput(_Strict):
    """本人のコマンド（plugin が全文一致で判定した結果の列挙値）。"""

    action: CommandAction
    item_no: StrictInt | None = Field(default=None, ge=1, le=MAX_ITEM_NO)

    @model_validator(mode="after")
    def _item_no_only_for_forget(self) -> CommandInput:
        if (self.action == "forget") != (self.item_no is not None):
            raise ValueError("item_no is required for forget and only for forget")
        return self


INPUT_MODELS: Final[dict[str, type[_Strict]]] = {
    "personal_memory_observe": ObserveInput,
    "personal_memory_context": ContextInput,
    "personal_memory_command": CommandInput,
}

__all__ = [
    "COMMAND_ACTIONS",
    "INPUT_MODELS",
    "MAX_ITEM_NO",
    "MAX_UTTERANCE_CHARS",
    "CommandAction",
    "CommandInput",
    "ContextInput",
    "ObserveInput",
]
