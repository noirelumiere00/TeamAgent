"""TikTokSearchOutput → 共通 Report への詰め替え（純粋関数・I/O なし）。

汎用テンプレへ丸投げすると全部ただの表になるため、この skill 固有の情報設計をここに置く:

- **再生数は横棒**で並べる（上位10本の中で「どれが突き抜けているか」は数字の羅列では読めない）。
- **保存率（保存数 ÷ 再生数）を計算して列に足す**。レシピ・ノウハウ系の検索面では「あとで作る」
  が本命の指標で、再生数だけ見ると保存率の高い中位動画の強さを取りこぼす。元データに無い唯一の
  導出値で、それ以外は API 実測値をそのまま出す。
- **分析文を見出し単位のカードに割る**。この skill の system prompt は出力フォーマットが順序固定
  （①サマリ ②勝ちパターン ③フックの型 ④頻出ハッシュタグ ⑤推奨アクション）なので、構造を拾える。
  ひと続きで流すと「勝ちパターン」も「推奨アクション」も同じ重さで並び、読む側が拾えない。
  先頭のサマリだけは本文として大きく置き、残りをカードにする。見出しが取れなければ全文を本文へ
  流す（プロンプト改訂で崩れても壊れない）。
"""

from __future__ import annotations

import re

from teamagent.skills._html.report import Cell, Chip, Column, Report, Section, Table
from teamagent.skills._html.sections import split_sections
from teamagent.skills.tiktok_search.schema import TikTokSearchOutput, TikTokVideoOut

# 保存率の色分け閾値（実測分布から: 2%超で明確に強い / 1%未満は伸びていない）。
_SAVE_RATE_OK = 0.02
_SAVE_RATE_MID = 0.01

_DESC_MAX = 90

# 見出し末尾の生成条件（「（最大 4、頻度/再生順）」など）。数字か「最大」を含むものだけ落とす。
_TITLE_HINT_RE = re.compile(r"[（(][^（()）]*(?:最大|\d)[^（()）]*[)）]\s*$")


def _compact(n: int) -> str:
    """再生数を日本語の桁で読める形にする（1,800,000 → 180万）。"""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}億"
    if n >= 10_000:
        value = n / 10_000
        return f"{value:.0f}万" if value >= 10 else f"{value:.1f}万"
    return f"{n:,}"


def _save_rate(video: TikTokVideoOut) -> float | None:
    """保存率。再生数 0（取得漏れ）は計算しない＝0% と偽らない。"""
    if video.play_count <= 0:
        return None
    return video.collect_count / video.play_count


def _tone(rate: float | None) -> str:
    if rate is None:
        return "muted"
    if rate >= _SAVE_RATE_OK:
        return "ok"
    if rate >= _SAVE_RATE_MID:
        return "warn"
    return "muted"


def _short_desc(desc: str) -> str:
    """説明文の 1 行目だけを切り出す（レシピ全文・定型の宣伝ブロックを表に持ち込まない）。"""
    head = (desc or "").strip().split("\n", 1)[0].strip()
    return head[:_DESC_MAX] + "…" if len(head) > _DESC_MAX else head


def _clean_title(title: str) -> str:
    """見出し末尾の指示書き（「（最大 4、頻度/再生順）」等）を落とす。

    プロンプトの出力フォーマットをそのまま見出しにすると、読者には無関係な生成条件が
    タイトルに残る。落とすのは **末尾の丸括弧で、かつ数字か「最大」を含むもの**だけ
    （内容の一部である括弧を消さないため）。
    """
    cleaned = _TITLE_HINT_RE.sub("", title).strip()
    return cleaned or title.strip()


def _split_analysis(analysis: str) -> tuple[str, list[Section]]:
    """分析文を「冒頭サマリ（本文）」と「残りのブロック」に割る。

    見出しが 1 つも無い（プロンプト改訂・生成失敗）なら、全文を本文として返す＝退化しない。
    """
    parts = split_sections(analysis)
    if not parts:
        return analysis, []
    first_title, first_body = parts[0]
    rest = [Section(title=_clean_title(title), body_md=body) for title, body in parts[1:] if body]
    if not rest:
        return analysis, []
    return first_body or f"{first_title}", rest


def build_report(out: TikTokSearchOutput, thumbs: dict[str, str] | None = None) -> Report:
    """検索結果 ＋ Gemini 分析を 1 枚の HTML レポートへ詰め替える。

    Args:
        thumbs: ``{cover_url: 再ホスト済みURL}``。I/O は skill 層で済ませて渡す
            （このモジュールは純粋関数のまま保つ）。空なら画像列を出さない。
    """
    videos = list(out.videos)
    max_play = max((v.play_count for v in videos), default=0)
    total_play = sum(v.play_count for v in videos)

    thumb_map = thumbs or {}
    has_thumbs = any(v.cover_url in thumb_map for v in videos)

    rows: list[list[Cell]] = []
    for v in videos:
        rate = _save_rate(v)
        cells = [Cell(str(v.rank))]
        if has_thumbs:
            cells.append(Cell("", image=thumb_map.get(v.cover_url)))
        rows.append(
            [
                *cells,
                Cell(
                    f"@{v.author}",
                    href=v.url,
                    sub=f"フォロワー {v.author_followers:,}",
                ),
                Cell(
                    _compact(v.play_count),
                    bar=(v.play_count / max_play) if max_play else None,
                ),
                Cell(f"{v.collect_count:,}"),
                Cell("—" if rate is None else f"{rate:.2%}", tone=_tone(rate)),
                Cell(f"{v.engagement_rate:.2%}"),
                Cell(f"{v.duration}" if v.duration else "—"),
                Cell(_short_desc(v.desc)),
            ]
        )

    columns = [
        Column("#"),
        *([Column("")] if has_thumbs else []),
        Column("アカウント"),
        Column("再生数", align="right"),
        Column("保存", align="right"),
        Column("保存率", align="right"),
        Column("EG率", align="right"),
        Column("秒", align="right"),
        Column("説明（1行目）"),
    ]

    chips = [
        Chip("本数", f"{out.count}"),
        Chip("合計再生", _compact(total_play)),
        Chip("検索種別", out.search_type),
    ]
    if out.model_id:
        chips.append(Chip("分析", out.model_id))
    if out.total_cost_usd:
        chips.append(Chip("コスト", f"${out.total_cost_usd:.4f}"))

    body, sections = _split_analysis(out.analysis or "")

    return Report(
        title=f"{out.query} — TikTok 上位 {out.count} 本",
        subtitle="検索面の上位動画メタと横断分析。数値は取得時点のスナップショット。",
        chips=chips,
        body_md=body,
        sections=sections,
        tables=[
            Table(
                columns=columns,
                rows=rows,
                caption="上位動画",
                note="再生数バーは本セット内の最大値が基準。保存率＝保存数÷再生数（当ツールでの導出値）。",
            )
        ],
        source_note="出典: TikTok 検索結果（実測メタデータ）。",
    )


__all__ = ["build_report"]
