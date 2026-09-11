"""LLM 出力から「AI が書いた感じ」の装飾だけを落とす後処理（最終防御）。

背景（利用者指摘 2026-09-11）:
  Slack に届く Aico の回答が `**太字**` と `—`（em ダッシュ）だらけで「AI 生成感が強い」。
  一次対策はプロンプト側（装飾テンプレの廃止・記号の名指し禁止）だが、LLM は指示を
  無視することがあるため、Slack へ出る直前で決定論的に正規化する層を 1 枚置く。

設計の掟（ここを破るとバグになる）:
  - **意味を変える置換はしない。** 記号 → 記号 / 記号 → 読点 の写像だけを行う。
    ``→`` は「A なので B」「A から B」など文脈で訳が変わるため**変換しない**。
    出現数を構造化ログに出すだけにして、減らすのはプロンプト側の仕事とする。
  - URL・コードブロック・インラインコード・Slack リンク記法 ``<url|label>``・
    markdown リンクの遷移先 ``](url)`` の中は**絶対に書き換えない**。
    ここを書き換えるとリンクが壊れる＝機能の毀損であり、見た目の改善より重い。
  - 既に Slack 正の ``*bold*`` になっているものを壊さない。

変換の一覧:
  1. アスタリスク 2 個以上の連なり（``**`` / ``***`` / ``****``）→ ``*`` 1 個。
     Slack mrkdwn の太字は ``*語*`` なので、これで ``**語**`` が太字に落ちる。
     1 個の ``*`` は触らないため既存の ``*bold*`` は無傷。
  2. 行中の ``—``（em ダッシュ、連続含む）→ ``、``。行頭・行末では削除。
  3. 行中の空白で囲まれた ``--``（2 個以上）と、和文に挟まれた ``--`` → ``、``。
     ``--extra`` のようなコマンドフラグを壊さないため、空白で囲まれていない
     ASCII 文脈の ``--`` は**意図的に触らない**。
  4. ``---`` / ``***`` / ``___`` だけの行（markdown の水平線。Slack では記号が
     そのまま見える）→ 行ごと削除し、空行が 3 連以上にならないよう畳む。

変換しない（ログするだけ）:
  - ``→``（意味が文脈依存のため）。
  - ``###`` などの markdown 見出し（行構造を壊さずに直す決定的な写像が無い）。
"""

from __future__ import annotations

import re

import structlog

logger = structlog.get_logger(__name__)

# ── 書き換え禁止領域 ────────────────────────────────────────────────────────
# 1 行の中で「触ってはいけない」部分。左から順に試すので、より広い記法を先に置く。
_PROTECTED_RE = re.compile(
    r"`[^`\n]*`"  # インラインコード
    r"|<[^<>\s|]+(?:\|[^<>\n]*)?>"  # Slack リンク <url|label> / <url>
    r"|\]\([^()\s]*\)"  # markdown リンクの遷移先 ](url)
    r"|https?://\S+"  # 裸 URL
)

# 水平線だけの行（markdown の <hr>。Slack では記号がそのまま出る）。
_HR_LINE_RE = re.compile(r"^[ \t　]*(?:-{3,}|\*{3,}|_{3,})[ \t　]*$")

# コードフェンスの開始/終了（``` / ~~~）。
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")

# アスタリスクの連なり（2 個以上）。1 個は Slack 正の太字なので触らない。
_ASTERISK_RUN_RE = re.compile(r"\*{2,}")

# em ダッシュ（連続可）と、その前後の空白。
_EM_DASH_RE = re.compile(r"[ \t　]*—+[ \t　]*")

# 空白で囲まれた 2 個以上のハイフン。
_SPACED_HYPHENS_RE = re.compile(r"[ \t　]+-{2,}[ \t　]+")

# 和文（非 ASCII）に挟まれた 2 個以上のハイフン。
_CJK_HYPHENS_RE = re.compile(r"(?<=[^\x00-\x7F])-{2,}(?=[^\x00-\x7F])")

# 変換後に生じる読点の重なりを畳む。
_DUP_PUNCT_RE = re.compile(r"、{2,}")
_PUNCT_THEN_COMMA_RE = re.compile(r"([。、！？，：・])、")
_COMMA_THEN_PUNCT_RE = re.compile(r"、(?=[。！？）」』])")

# 空行の 3 連以上（水平線を消した跡）。
_BLANK_RUN_RE = re.compile(r"\n{3,}")

# 変換しないが数えるもの。
_ARROW_RE = re.compile(r"[→⇒➡]")
_MD_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+\S", re.MULTILINE)


def _normalize_free_text(part: str, *, at_line_start: bool, at_line_end: bool) -> str:
    """保護領域でない断片に、記号の正規化をかける。"""
    bolded = _ASTERISK_RUN_RE.sub("*", part)
    span_end = len(bolded)

    def _dash_to_comma(m: re.Match[str]) -> str:
        # 断片の先頭/末尾にある区切りは、繋ぐ相手がいないので読点にせず落とす。
        if at_line_start and m.start() == 0:
            return ""
        if at_line_end and m.end() == span_end:
            return ""
        return "、"

    out = _EM_DASH_RE.sub(_dash_to_comma, bolded)
    out = _SPACED_HYPHENS_RE.sub("、", out)
    out = _CJK_HYPHENS_RE.sub("、", out)
    return out


def _normalize_line(line: str) -> str:
    """1 行を、保護領域を避けながら正規化する。"""
    pieces: list[str] = []
    cursor = 0
    for m in _PROTECTED_RE.finditer(line):
        if m.start() > cursor:
            pieces.append(
                _normalize_free_text(
                    line[cursor : m.start()],
                    at_line_start=cursor == 0,
                    at_line_end=False,
                )
            )
        pieces.append(m.group(0))  # 保護領域は無加工で通す
        cursor = m.end()
    if cursor < len(line):
        pieces.append(
            _normalize_free_text(line[cursor:], at_line_start=cursor == 0, at_line_end=True)
        )
    return "".join(pieces)


def strip_ai_decoration(text: str, *, request_id: str | None = None) -> str:
    """LLM 本文から装飾記号を落とす。URL・コード・リンク記法は壊さない。

    Args:
        text: LLM が書いた本文。空文字・None 相当はそのまま返す。
        request_id: 構造化ログの相関キー（任意）。

    Returns:
        正規化後の本文。入力に手を入れる必要が無ければ入力と同一の文字列。
    """
    if not text:
        return text

    lines = text.split("\n")
    out_lines: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)  # コードブロックの中は一切触らない
            continue
        if _HR_LINE_RE.match(line):
            out_lines.append("")  # 水平線は落とす（後段で空行を畳む）
            continue
        out_lines.append(_normalize_line(line))

    out = "\n".join(out_lines)
    out = _DUP_PUNCT_RE.sub("、", out)
    out = _PUNCT_THEN_COMMA_RE.sub(r"\1", out)
    out = _COMMA_THEN_PUNCT_RE.sub("", out)
    out = _BLANK_RUN_RE.sub("\n\n", out)
    out = out.strip()

    # 変換しなかった「AI っぽさ」を観測だけしておく（減らすのはプロンプト側の仕事）。
    arrows = len(_ARROW_RE.findall(out))
    headings = len(_MD_HEADING_RE.findall(out))
    if arrows or headings:
        logger.info(
            "llm_text_residual_decoration",
            request_id=request_id,
            arrow_count=arrows,
            md_heading_count=headings,
        )
    return out


__all__ = ["strip_ai_decoration"]
