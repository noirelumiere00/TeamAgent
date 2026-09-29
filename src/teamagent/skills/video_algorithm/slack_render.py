"""動画分析（video_algorithm）の完了の直接投稿を Block Kit で描く。

mcp の切り離し（``mcp_gateway/detached_jobs.py``）が完了を依頼元の DM へ直接出す経路専用。
``skill._slack_summary``（Aico が中継する経路の文面）は書き換えず、出力の欄
（``VideoAlgorithmOutput``）から組み直す。Gemini の自由記述（``cross.summary``）は使わない
（同じ主張の二重表記と、文中の数字の出どころが追えない問題を避ける）。

並び: 見出し → 本数と注意 → 上位に多く見られた共通点（本数と段階つき・選び方の注記）→ 数字の欄 →
── → 対象の N 本（1 本 1 行・投稿への文字リンク）→ レポート・スライドの文字リンク → 概算

言い方の決まり:
- 共通点（``cross.win_factors``）は「分析できた動画の 6 割以上に出た点」で、メタで測れる点は検索結果
  全体と比べて目立つもの（リフト 1.5 以上）に限っている（``analysis.cross_analyze``）。いちばん多く
  出た点とは限らないので「最も多く見られた」とは書かず、選び方を添える。
- 分析できなかった上位の動画を下位で繰り上げたときは、その本数を書く（「上位 5 本」と
  言い切らない）。
- 分析できた動画が 0 本なら、共通点も数字も「分析できた動画がありません」と書く。
- 最上位の text（スクリーンリーダーが読む・通知・会話の履歴）には blocks と同じ中身をすべて入れる。
"""

from __future__ import annotations

from teamagent.skills._shared.slack_blocks import (
    Block,
    RichMessage,
    assemble,
    context,
    count_ja,
    divider,
    esc,
    header,
    link,
    link_url,
    message_text,
    post_url,
    section,
    stage,
)
from teamagent.skills.video_algorithm.schema import AnalyzedVideo, VideoAlgorithmOutput, WinFactor

_FACTORS = 3
_TOP = 10
REPORT_FAILED = "レポートの発行に失敗しました。同じ内容でもう一度依頼すると、課金なしで再発行します"
FACTOR_TITLE = "上位に多く見られた共通点"
FACTOR_RULE = (
    "選び方: 分析できた動画の6割以上に出た点（検索結果の全体でも同じくらい多い点は除く）"
    "・本数の多い順"
)
NO_FACTOR = "分析できた動画の6割以上に共通して出た点はありませんでした"
NO_ANALYZED = "分析できた動画がありませんでした"


def _seconds(sec: float) -> str:
    total = round(sec)
    if total <= 0:
        return ""
    minutes, seconds = divmod(total, 60)
    return f"{minutes}分{seconds:02d}秒" if minutes else f"{seconds}秒"


def _factor(w: WinFactor) -> str:
    """共通点 1 つ（本数と段階はコードが付ける）。"""
    count = ""
    if w.total > 0:
        word = stage(w.observed_in, w.total)
        count = f"（{w.observed_in}/{w.total}本" + (f"・{word}" if word else "") + "）"
    return esc(w.factor) + count


def _video_line(v: AnalyzedVideo) -> str:
    handle = f"@{v.meta.author}" if v.meta.author else "不明"
    head = f"*{v.meta.rank}位* {link(post_url(v.meta.url), handle)}"
    if v.analysis is None:
        return f"{head}　分析できませんでした"
    metrics = [f"{count_ja(v.meta.play_count)}回"] if v.meta.play_count > 0 else []
    if v.meta.play_count > 0 and v.meta.collect_count > 0:
        metrics.append(f"保存{v.meta.save_rate():.2f}%")
    duration = _seconds(v.analysis.duration_sec or v.meta.duration_sec)
    if duration:
        metrics.append(duration)
    return f"{head} " + "・".join(metrics) if metrics else head


def _about(videos: list[AnalyzedVideo], ok: int, backfilled: int, observed: int) -> str:
    """分析した本数（下位からの繰上げを含む）と、観測の注意。"""
    detail = f"分析できた {ok}本" + (f"・うち下位からの繰上げ {backfilled}本" if backfilled else "")
    about = [f"TikTok 検索上位から{len(videos)}本（{detail}）"]
    if observed:
        about.append(f"n={observed} の観測（相関であって因果ではありません）")
    return "・".join(about)


def completion_message(out: object) -> RichMessage | None:
    """完了の直接投稿（Block Kit）。分析した動画が無い・想定外の出力なら None（文字だけ）。"""
    if not isinstance(out, VideoAlgorithmOutput) or not out.videos:
        return None
    c = out.cross
    videos = sorted(out.videos, key=lambda v: v.meta.rank)
    ok = sum(1 for v in videos if v.analysis is not None)
    # 分析できなかった上位の代わりに下位を分析した本数
    # （skill._slack_summary の「下位繰上げ」と同じ数え方）。
    backfilled = sum(1 for v in videos if v.analysis is not None and v.meta.rank > len(videos))
    title = header(f"VSEO動画アルゴリズム分析「{out.query}」")
    head: list[Block | None] = [title, context(_about(videos, ok, backfilled, c.video_count))]
    factors = c.win_factors[:_FACTORS] if ok else []
    if factors:
        text = f":mag: *{FACTOR_TITLE}*\n" + "\n".join(f"• {_factor(w)}" for w in factors)
        text += f"\n{FACTOR_RULE}"
    else:
        text = f":mag: *{FACTOR_TITLE}*\n{NO_FACTOR if ok else NO_ANALYZED}"
    head.append(section(text))
    fields: list[str] = []
    if ok:
        fields.append(f"*平均エンゲージメント率*\n{c.avg_engagement_rate:g}%")
        fields.append(f"*平均保存率*\n{c.avg_save_rate:g}%")
        if c.median_duration_sec > 0:
            fields.append(f"*尺の中央値*\n{_seconds(c.median_duration_sec)}")
    fields.append(f"*分析できた本数*\n{ok}/{len(videos)}本")
    numbers = f"分析できた{ok}本の数字" if ok else "数字（分析できた動画がありません）"
    head.append(section(f":bar_chart: *{numbers}*", fields=fields))
    notes: list[str] = []
    if out.quota_note:
        notes.append(esc(out.quota_note))
    if out.search_volume:
        notes.append(f"月間検索量（手動実測）: {out.search_volume:,}")
    if notes:
        head.append(context(*notes))
    head += [
        divider(),
        section(
            f":clipboard: *対象の{len(videos[:_TOP])}本* （検索順位の順・再生・保存率・尺）\n"
            + "\n".join(_video_line(v) for v in videos[:_TOP])
        ),
    ]
    # リンクは 1 つずつ別の section に置く（署名つきの長い URL が 2 つ並んでも 3000 字に収める）。
    tail: list[Block] = []
    if not out.report_url:
        tail.append(section(f":page_facing_up: {REPORT_FAILED}"))
    for url, emoji, label, note in (
        (
            out.report_url,
            ":page_facing_up:",
            "詳細レポートを開く",
            "（タイムライン・テロップ位置・ブランド検出ほか）",
        ),
        (out.slides_url, ":pencil2:", "編集用スライドを開く", "（ブラウザで直接編集）"),
        (out.pptx_url, ":bar_chart:", "提案用パワポを開く", "（7日有効・そのまま提案資料へ）"),
    ):
        if not url:
            continue
        target = link_url(url)
        if target is None:
            # 自前の URL なのにリンクにできない＝想定外。文字だけの投稿に戻す。
            raise ValueError("artifact url is not linkable")
        tail.append(section(f"{emoji} {link(target, label)} {note}"))
    tail.append(context(f"概算 ${out.total_cost_usd:.4f}"))
    blocks = assemble([b for b in head if b], tail)
    body = [b for b in blocks[: len(blocks) - len(tail)] if b is not title]
    urls = [(f"{v.meta.rank}位", u) for v in videos[:_TOP] if (u := post_url(v.meta.url))]
    text = message_text(
        [_lead(out, len(videos), ok, backfilled)],
        body,
        blocks[len(blocks) - len(tail) :],
        urls=urls,
    )
    return RichMessage(text=text, blocks=blocks)


def _lead(out: VideoAlgorithmOutput, total: int, ok: int, backfilled: int) -> str:
    """最上位の text の 1 行目（通知のプレビュー）。"""
    bf = f"・下位繰上げ{backfilled}本" if backfilled else ""
    head = f"VSEO動画アルゴリズム分析「{esc(out.query)}」完了（{total}本／分析成功{ok}本{bf}）"
    factors = out.cross.win_factors
    if ok and factors:
        more = f"ほか{min(len(factors), _FACTORS) - 1}点" if len(factors) > 1 else ""
        head += f": {FACTOR_TITLE}は『{_factor(factors[0])}』{more}"
    return head


__all__ = [
    "FACTOR_RULE",
    "FACTOR_TITLE",
    "NO_ANALYZED",
    "NO_FACTOR",
    "REPORT_FAILED",
    "completion_message",
]
