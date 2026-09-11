"""LLM 出力から「AI が書いた感じ」の装飾だけを落とす後処理（最終防御）。

背景（利用者指摘 2026-09-11）:
  Slack に届く Aico の回答が `**太字**` と `—`（em ダッシュ）だらけで「AI 生成感が強い」。
  一次対策はプロンプト側（装飾テンプレの廃止・記号の名指し禁止）だが、LLM は指示を
  無視することがあるため、Slack へ出る直前で決定論的に正規化する層を 1 枚置く。

設計の掟（ここを破るとバグになる）:
  - 意味を変える置換はしない。記号 → 記号 / 記号 → 読点 の写像だけを行う。
    ``→`` は「A なので B」「A から B」など文脈で訳が変わるため変換しない。
    出現数を構造化ログに出すだけにして、減らすのはプロンプト側の仕事とする。
    同じ理由で、範囲を表す ``80—100`` / ``2024—2025`` や、コマンドの引数終端
    ``uv run -- pytest``、markdown 表の区切り行 ``| --- | --- |`` は触らない。
  - URL・コードブロック・インラインコード・Slack リンク記法 ``<url|label>``・
    markdown リンクの遷移先 ``](url)`` の中は絶対に書き換えない。
    ここを書き換えるとリンクが壊れる＝機能の毀損であり、見た目の改善より重い。
    このため本モジュールは「保護領域を番兵へ退避 → 番兵入りの行を正規化 → 復元」の順で
    処理する。全文への一括置換は最後の空行畳みだけに限り、句読点の整形も
    行（番兵入り）の中だけで行う（フェンスや URL クエリの ``、、`` を壊さないため）。
  - 既に Slack 正の ``*bold*`` になっているものを壊さない。

変換の一覧:
  1. 対になった ``**強調**``（``***`` / ``****`` を含む）→ 記号を落として地の文にする。
     方向の根拠: 配信面が 2 つあり、``*`` も ``**`` も安全でない。
       - slack_bot.build_search_blocks / two_stage の後追い投稿は bot が mrkdwn を
         直接 chat.postMessage するので ``*語*`` が太字。
       - OpenClaw(@Aico) 経由は Markdown→mrkdwn 変換が入り、``*語*`` は斜体になる
         （infra/openclaw/SOUL.md:351 と tests/infra/test_soul_contract.py:469 が
         この変換仕様を repo 内の一次記述として固定している）。
     どちらの面でも誤解釈されない唯一の出力が「記号を持たない地の文」なので、
     太字は別記号へ移さず落とす。プロンプト側も「強調が要るなら語順と言い切りで示す」
     と書いており、方針が一致する。
     対になっていない ``**``（生成が途中で切れた時だけ出る）は、消すと対の片側だけが
     消えて入力より壊れるため触らず、出現数をログに出す。
  2. 行中の ``—``（em ダッシュ、連続含む）→ ``、``。行頭・行末では削除。
     ただし空白を伴わず英数字に挟まれた ``—`` は範囲表記なので触らない。
  3. 空白で囲まれた ``--``（2 個以上）で、かつ少なくとも片側が和文のもの → ``、``。
     ``--extra`` のようなフラグ、``uv run -- pytest`` の引数終端、``| --- | --- |``
     の表区切り、``2024 -- 2025`` の年レンジは、いずれも和文に接しないので触らない。
     和文に直接挟まれた ``--`` も読点にする。
  4. ``---`` / ``***`` / ``___`` だけの行（markdown の水平線。Slack では記号が
     そのまま見える）→ 行ごと削除し、空行が 3 連以上にならないよう畳む。

変換しない（ログするだけ）:
  - ``→``（意味が文脈依存のため）。
  - ``###`` などの markdown 見出し（行構造を壊さずに直す決定的な写像が無い）。
  - 対になっていない ``**``。
"""

from __future__ import annotations

import re

import structlog

logger = structlog.get_logger(__name__)

# ── 書き換え禁止領域 ────────────────────────────────────────────────────────
# 1 行の中で「触ってはいけない」部分。左から順に試すので、より広い記法を先に置く。
# 裸 URL は `*` で止める。`\S+` だと「**資料は https://…**」の閉じ `**` まで URL の一部と
# して飲み込み、開きだけが変換されて `*…**` という非対称な壊れ方を作る（実測で確認）。
# 逆に和文は URL の直後に空白を置かないため、貪欲なままだと行末までが保護領域になり
# `…/viewの**要点**` の装飾が素通しになる。`*` と `<>` で止めればどちらも解ける。
_PROTECTED_RE = re.compile(
    r"`[^`\n]*`"  # インラインコード
    r"|<[^<>\s|]+(?:\|[^<>\n]*)?>"  # Slack リンク <url|label> / <url>
    r"|\]\([^()\s]*\)"  # markdown リンクの遷移先 ](url)
    r"|https?://[^\s<>*]+"  # 裸 URL（`*` と山括弧の手前で止める）
)

# 保護領域を退避するときの番兵。ASCII 制御文字なので、和文判定（非 ASCII 判定）にも
# 句読点の整形にも引っかからない＝番兵の存在が周囲の変換結果を変えない。
_SENTINEL_OPEN = "\x00"
_SENTINEL_CLOSE = "\x01"
_SENTINEL_RE = re.compile(r"\x00(\d+)\x01")

# 水平線だけの行（markdown の <hr>。Slack では記号がそのまま出る）。
_HR_LINE_RE = re.compile(r"^[ \t　]*(?:-{3,}|\*{3,}|_{3,})[ \t　]*$")

# markdown 表の区切り行（`| --- | :---: |`）。セルの中身ではなく表の骨格なので、
# 読点に変えると表が `|、|、|` に化ける（実測）。行ごと素通しする。
# 全角の縦棒 `｜` と全角空白にも対応する。和文 LLM はこの形も書き、全角側は
# 「空白の隣が非 ASCII」に当たるので _SPACED_HYPHENS_RE だけでは守れない。
_TABLE_DELIM_RE = re.compile(
    r"^[ \t　]*[|｜][ \t　]*:?-{2,}:?[ \t　]*"
    r"(?:[|｜][ \t　]*:?-{2,}:?[ \t　]*)*[|｜]?[ \t　]*$"
)

# コードフェンスの開始/終了（``` / ~~~）。
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")

# 対になった強調。区切りは `*` 2 個以上、中身は単独の `*` を含んでよい（`**x*y**`）。
_BOLD_PAIR_RE = re.compile(r"\*{2,}((?:[^*\n]|\*(?!\*))+?)\*{2,}")

# 対になっていない `*` の連なり（変換せず数えるだけ）。
_ASTERISK_RUN_RE = re.compile(r"\*{2,}")

# em ダッシュ（連続可）と、その前後の空白。
_EM_DASH_RE = re.compile(r"(?P<lead>[ \t　]*)(?P<dash>—+)(?P<trail>[ \t　]*)")

# 空白で囲まれた 2 個以上のハイフンのうち、少なくとも片側が和文（非 ASCII）のもの。
_SPACED_HYPHENS_RE = re.compile(
    r"(?<=[^\x00-\x7F])[ \t　]+-{2,}[ \t　]+"  # 和文 -- なにか
    r"|[ \t　]+-{2,}[ \t　]+(?=[^\x00-\x7F])"  # なにか -- 和文
)

# 和文（非 ASCII）に挟まれた 2 個以上のハイフン。
_CJK_HYPHENS_RE = re.compile(r"(?<=[^\x00-\x7F])-{2,}(?=[^\x00-\x7F])")

# 変換後に生じる読点の重なりを畳む（自分が作った読点の後始末なので、置換が起きた行だけ）。
_DUP_PUNCT_RE = re.compile(r"、{2,}")
_PUNCT_THEN_COMMA_RE = re.compile(r"([。、！？，：・])、")
_COMMA_THEN_PUNCT_RE = re.compile(r"、(?=[。！？）」』])")

# 空行の 3 連以上（水平線を消した跡）。
_BLANK_RUN_RE = re.compile(r"\n{3,}")

# 変換しないが数えるもの。
_ARROW_RE = re.compile(r"[→⇒➡]")
_MD_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+\S", re.MULTILINE)


def _is_cjk(ch: str) -> bool:
    """和文（＝ASCII でない可視文字）かどうか。番兵は ASCII なので False になる。"""
    return bool(ch) and ord(ch) > 0x7F


def _mask_protected(line: str) -> tuple[str, list[str]]:
    """保護領域を番兵へ退避した行と、退避した実体の一覧を返す。"""
    kept: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        kept.append(m.group(0))
        return f"{_SENTINEL_OPEN}{len(kept) - 1}{_SENTINEL_CLOSE}"

    return _PROTECTED_RE.sub(_sub, line), kept


def _unmask_protected(line: str, kept: list[str]) -> str:
    if not kept:
        return line

    def _restore(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        # 入力自体が番兵と同じ制御文字を含んでいた場合に備える。ここで例外を出すと
        # 回答が丸ごと落ちるので、退避していない番号はその文字列のまま返す。
        return kept[idx] if idx < len(kept) else m.group(0)

    return _SENTINEL_RE.sub(_restore, line)


def _dash_to_comma(masked: str) -> str:
    """em ダッシュを読点にする。範囲表記（``80—100``）は意味が変わるので触らない。"""

    def _sub(m: re.Match[str]) -> str:
        # 行頭・行末の区切りは、繋ぐ相手がいないので読点にせず落とす。
        if m.start() == 0:
            return ""
        if m.end() == len(masked):
            return ""
        if m.group("lead") or m.group("trail"):
            return "、"  # 空白を伴う＝文の区切りとして使われている
        before = masked[m.start() - 1]
        after = masked[m.end()] if m.end() < len(masked) else ""
        if _is_cjk(before) and _is_cjk(after):
            return "、"  # 和文に直接挟まれている＝範囲ではなく挿入句
        return m.group(0)  # `80—100` `2024—2025` `提案書—A社版` は範囲/固有名詞

    return _EM_DASH_RE.sub(_sub, masked)


def _normalize_masked(masked: str) -> str:
    """番兵入りの 1 行に、記号の正規化をかける。"""
    out = _BOLD_PAIR_RE.sub(r"\1", masked)
    out = _dash_to_comma(out)
    out = _SPACED_HYPHENS_RE.sub("、", out)
    out = _CJK_HYPHENS_RE.sub("、", out)
    if out != masked:
        # 自分が作った読点の重なりだけを畳む。置換が起きていない行の `、、` は
        # 書き手の意図（CSV 見本など）なので触らない。
        out = _DUP_PUNCT_RE.sub("、", out)
        out = _PUNCT_THEN_COMMA_RE.sub(r"\1", out)
        out = _COMMA_THEN_PUNCT_RE.sub("", out)
    return out


def _normalize_line(line: str) -> str:
    """1 行を、保護領域を避けながら正規化する。"""
    masked, kept = _mask_protected(line)
    return _unmask_protected(_normalize_masked(masked), kept)


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
        if _TABLE_DELIM_RE.match(line):
            out_lines.append(line)  # 表の区切り行は骨格なので素通し
            continue
        out_lines.append(_normalize_line(line))

    out = _BLANK_RUN_RE.sub("\n\n", "\n".join(out_lines)).strip()

    # 変換しなかった「AI っぽさ」を観測だけしておく（減らすのはプロンプト側の仕事）。
    arrows = len(_ARROW_RE.findall(out))
    headings = len(_MD_HEADING_RE.findall(out))
    unpaired_bold = len(_ASTERISK_RUN_RE.findall(out))
    if arrows or headings or unpaired_bold:
        logger.info(
            "llm_text_residual_decoration",
            request_id=request_id,
            arrow_count=arrows,
            md_heading_count=headings,
            unpaired_bold_count=unpaired_bold,
        )
    return out


__all__ = ["strip_ai_decoration"]
