"""出力面ガード（deny-by-default）— 「本人しか読まない面」だけに本文を描く判定。

⚠️ ``skills/slack_summary/skill.py`` の ``_is_channel_surface`` を流用してはいけない。
あちらは ``bool(channel_id) and not channel_id.startswith("D")`` で、**空文字を
「出してよい」側へ倒す** fail-open（system event 由来の空 channel_id が本人 DM へ
フォールバックする前提に依存している）。事例ブリーフは社名・件数・取引先を載せるため、
「判定できなかった」を許可側に倒すと社外向けチャンネルへ取引先が漏れる。

ここは反転させて deny-by-default にする:

- ``identity_verified`` が真 **かつ** ``channel_id`` が ``D`` 始まり → True（描画してよい）
- 空文字 / ``C``（チャンネル）/ ``G``（グループ DM）/ 未知 prefix / ``None`` → False

False のときは件数も社名も出さず、定型文だけを返すのが呼び出し側の契約。
"""

from __future__ import annotations

# 本人 DM（Slack の IM channel）の prefix。ここだけが「本人しか読まない面」。
_DM_PREFIX = "D"


def is_private_surface(channel_id: str | None, identity_verified: bool) -> bool:
    """本人だけが読む面か（deny-by-default）。

    Args:
        channel_id: Slack の会話 ID。``None`` / 空文字は「判定できなかった」＝不許可。
        identity_verified: サーバ側で本人が確定しているか（OC 申告は不可）。

    Returns:
        本文（社名・件数・事例）を描いてよいときだけ True。
    """
    if not identity_verified:
        return False
    if not channel_id or not isinstance(channel_id, str):
        return False
    cid = channel_id.strip()
    if not cid:
        return False
    return cid.startswith(_DM_PREFIX)


__all__ = ["is_private_surface"]
