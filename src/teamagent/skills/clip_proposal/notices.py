"""切り抜き提案の成果物に必ず添える注意文（計画 §2-2「完成」の 4 点）。

この 4 点は **成果物の配達コメントにも、資料の注記欄にも、必ず全部載る**。
1 行でも欠けると「今時点のイメージ」という前提が落ち、営業がそのまま得意先へ出す
事故になる。``REQUIRED_NOTICES`` を単一情報源にして、配達側とテスト側の両方が
同じ定数を参照する（写経すると片方だけ古くなる）。

要望文との差分（計画 §4 論点 4 の裁定 (A)）も、ここで明示して添付コメントへ載せる。
テンプレ本文は『ここは本編の MP4 をそのまま入れてください』『9:16で2秒間をトリミング』
と書いてあり、記入例は実 MP4 21 本を埋め込んでいるが、v1 は静止画で出す。
"""

from __future__ import annotations

from collections.abc import Sequence

#: 今時点のイメージであること。
NOTICE_DRAFT = "この資料は今時点のイメージです。デザインはこれから詰めます。"

#: 記載の秒数は本編での区間であり、編集で尺が短くなること。
NOTICE_DURATION = (
    "記載の秒数は本編での切り抜き区間です。"
    "編集で「えー」「あー」等を落とすため実際の尺は数秒短くなります。"
)

#: 界隈の規模・言語の確度。実在確認が取れない語は「（未検証）」を付けて出す。
NOTICE_COMMUNITY = (
    "界隈の規模は仮定値、界隈言語は実在確認が取れたものだけ素で載せ、"
    "取れないものは「（未検証）」と明記しています。"
)

#: v1 は静止画（テンプレの指示・記入例とは異なる）。便D で MP4 埋め込みへ。
NOTICE_STILL_IMAGE = (
    "今回はモック内が動画ではなく代表コマの静止画です"
    "（テンプレの指示とは異なります。動画埋め込みは次段）。"
)

#: 対外利用の可否。事例・素材の外部提示は担当の確認を経ること。
NOTICE_EXTERNAL_USE = (
    "対外利用の可否はこの資料では判定していません。"
    "得意先へ出す前に素材の権利と公開範囲を担当営業がご確認ください。"
)

REQUIRED_NOTICES: tuple[str, ...] = (
    NOTICE_DRAFT,
    NOTICE_DURATION,
    NOTICE_COMMUNITY,
    NOTICE_STILL_IMAGE,
    NOTICE_EXTERNAL_USE,
)


def build_notices(*, quality_note: str = "", extra: Sequence[str] = ()) -> list[str]:
    """配達コメント・資料注記に載せる注意文の全量。

    ``quality_note`` は解析品質が落ちた回（proxy を段階的に劣化させた回）の但し書き。
    必ず ``REQUIRED_NOTICES`` の後ろに足し、4 点を押し出さない。
    """

    notices = list(REQUIRED_NOTICES)
    if quality_note.strip():
        notices.append(quality_note.strip())
    for line in extra:
        text = str(line).strip()
        if text and text not in notices:
            notices.append(text)
    return notices


def build_delivery_comment(
    *,
    client_name: str,
    clip_count: int,
    notices: Sequence[str],
    dropped_clip_count: int = 0,
) -> str:
    """PPTX 添付時のコメント本文。注意文は 1 行も落とさずに載せる。"""

    who = client_name.strip() or "（クライアント名 未確定）"
    head = f"「{who}」の切り抜き提案です（切り抜き {clip_count} 本）。"
    if dropped_clip_count > 0:
        head += f" 内容の確認で {dropped_clip_count} 本を落としています。"
    body = "\n".join(f"・{line}" for line in notices)
    return f"{head}\n{body}"


def has_all_required_notices(text: str) -> bool:
    """配達コメント（または資料注記）に 4 点＋対外利用の注意が全部入っているか。"""

    return all(notice in text for notice in REQUIRED_NOTICES)


__all__ = [
    "NOTICE_COMMUNITY",
    "NOTICE_DRAFT",
    "NOTICE_DURATION",
    "NOTICE_EXTERNAL_USE",
    "NOTICE_STILL_IMAGE",
    "REQUIRED_NOTICES",
    "build_delivery_comment",
    "build_notices",
    "has_all_required_notices",
]
