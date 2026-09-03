"""計測結果 + 動画解析 → 契約準拠の計測JSON（DeckPlan）と監査JSONの決定論組成。

U1裁定（2026-08-24）のデッキ構成:
  A 表紙 → B 章扉(PART1) → 露出シェア導入（Q番号なしの「現状」枠・D類型）
  → Q1 フォロワー階層別（D） → Q2 PR比較（C） → Q3 界隈クラスタ（C・動画解析由来）
  → Q4 登場率+頻出ハッシュタグ（D） → Q5 TOP5（E） → H 総括（CTA固定文をdark結論バンド内）

規律:
- 文言は全てここで実測数値から決定論で組む（レンダラ無作文原則）。
- 取得失敗軸・解析失敗動画は該当スライドを黙って埋めず、省略+開示（監査JSONと
  H/リード文）で扱う。クラスタ表の分母は「解析できた本数」を必ず明記する。
- 数値は丸め前実数（指標定義の丸めは計測値そのもの）。K/M省略はレンダラ責務。
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from teamagent.skills.omiyage_report.contract import (
    CTA_TEXT,
    EG_RATE_FOOTNOTE,
    VOICE_UNMEASURED_NOTE,
    Card,
    CardImage,
    ComparisonGroup,
    DeckBrand,
    DeckMeta,
    DeckPlan,
    QListItem,
    RankingCard,
    RankingMetrics,
    Slide,
    SlideDataA,
    SlideDataB,
    SlideDataC,
    SlideDataD,
    SlideDataE,
    SlideDataH,
    SlideTag,
    SummaryRow,
)
from teamagent.skills.omiyage_report.metrics import (
    AxisMeasurement,
    OmiyageMeasurement,
    PostRecord,
    TelopMetrics,
    aggregate_clusters,
    avg_eg_rate_pct,
    has_pr_tag,
    keyword_variants,
    measure_follower_tiers,
    measure_telop_route,
    top_hashtags,
    top_posts,
)
from teamagent.skills.omiyage_report.video_analysis import VideoAnalysisReport

_QUESTIONS: dict[str, str] = {
    "Q1": "どのフォロワー帯が再生とEG率を取っているのか？",
    "Q2": "伸びているのは、PR表記あり投稿か・なし投稿か？",
    "Q3": "どんな界隈で語られているのか？",
    "Q4": "キーワードはどの経路で語られているのか？",
    "Q5": "いちばん見られている動画はどれか？",
}


class DeckPlanBuildError(RuntimeError):
    """契約準拠の計測JSONを組めない（必須軸が全滅など）。"""


# ---------------------------------------------------------------------------
# 先行調査メモ（research_notes・設計C 2026-09-03）
# 調査ツール（x_voice_search / search_surface_check / web_research）の材料を、出典URL付きの
# 行だけ「生活者の声／検索面の勢力図」の章として併記する。出典の無い主張は採用しない。
# ---------------------------------------------------------------------------

_RESEARCH_URL_RE = re.compile(r"https?://[^\s<>\"'()（）「」]+")
_RESEARCH_BULLET = "-・*•●▪◦ \t"
_RESEARCH_MAX_ROWS = 8
_RESEARCH_MAX_TEXT = 120
# fmt レンダラの pr_labels ゲートと同じ語彙・注記（「オーガニック」使用時に必須）。
_ORGANIC_WORD = "オーガニック"
_ORGANIC_NOTE = (
    "本資料で「オーガニック」は#PR等の表記が確認できない投稿を指し、広告出稿の有無は断定しない。"
)


@dataclass(frozen=True)
class ResearchNote:
    text: str
    source_url: str


def parse_research_notes(notes: str) -> tuple[list[ResearchNote], int]:
    """行ごとに (要点, 出典URL) を取り出す。出典URL（https）の無い行は落として件数だけ返す。"""
    adopted: list[ResearchNote] = []
    dropped = 0
    for raw in notes.splitlines():
        line = raw.strip().lstrip(_RESEARCH_BULLET).strip()
        if not line:
            continue
        urls = [
            url.rstrip(".,;:、。")
            for url in _RESEARCH_URL_RE.findall(line)
            if urlsplit(url).scheme == "https" and urlsplit(url).hostname
        ]
        if not urls:
            dropped += 1
            continue
        text = _RESEARCH_URL_RE.sub(" ", line)
        text = re.sub(r"[（(]\s*[)）]", " ", text)
        text = text.replace("出典：", " ").replace("出典:", " ")
        text = " ".join(text.split()).strip(" :：|｜-—–")
        if not text:
            dropped += 1
            continue
        adopted.append(ResearchNote(text=text[:_RESEARCH_MAX_TEXT], source_url=urls[0]))
    return adopted, dropped


def _research_slide(notes: str) -> Slide | None:
    adopted, dropped = parse_research_notes(notes)
    shown = adopted[:_RESEARCH_MAX_ROWS]
    if not shown:
        return None
    tag_text = f"先行調査の要点{len(shown)}件（すべて出典URL付き）を併記。"
    if dropped:
        tag_text += f"出典URLの無い{dropped}件は採用していない。"
    if len(adopted) > len(shown):
        tag_text += f"紙面の都合で{len(adopted) - len(shown)}件は監査記録のみ。"
    tag_text += "数値の実測は本資料の各ページで行い、本ページは調査ツールの一次情報への導線。"
    footnote = _ORGANIC_NOTE if any(_ORGANIC_WORD in note.text for note in shown) else ""
    return Slide(
        type="D",
        part=1,
        q_number="",
        heading="先行調査で見えた、生活者の声と検索面の勢力図",
        lead=(
            "調査ツール（X検索・検索面チェック・Web調査）で裏取りした要点と出典。"
            "出典URLの無い主張は載せていない"
        ),
        footnote=footnote,
        tag=SlideTag(variant="所見", text=tag_text),
        data=SlideDataD(
            columns=["要点（先行調査）", "出典"],
            rows=[[note.text, note.source_url] for note in shown],
        ),
    )


def _fmt_pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value}%"


def _fmt_num(value: float | None) -> str:
    return "N/A" if value is None else f"{value}"


def _fmt_rank(value: int | None) -> str:
    return "-" if value is None else f"{value}位"


def _pr_split(posts: Sequence[PostRecord]) -> tuple[list[PostRecord], list[PostRecord]]:
    pr = [post for post in posts if has_pr_tag(post.caption, post.hashtags)]
    no_pr = [post for post in posts if not has_pr_tag(post.caption, post.hashtags)]
    return pr, no_pr


def _brand_pair(
    measurement: OmiyageMeasurement,
) -> tuple[AxisMeasurement | None, AxisMeasurement | None]:
    return measurement.brand_axis, next(iter(measurement.competitor_axes), None)


def _exposure_slide(measurement: OmiyageMeasurement) -> Slide | None:
    rows: list[list[str]] = []
    for axis_measurement in measurement.general_axes:
        axis = axis_measurement.axis
        for exposure in axis_measurement.brand_exposure:
            rows.append(
                [
                    axis.label,
                    exposure.brand,
                    f"{exposure.videos}本",
                    f"{len(axis.posts)}本中 {_fmt_pct(exposure.share_pct)}",
                    _fmt_rank(exposure.best_rank),
                ]
            )
    if not rows:
        return None
    first = measurement.general_axes[0]
    own = next(
        (e for e in first.brand_exposure if e.brand == measurement.brand),
        None,
    )
    others = [e for e in first.brand_exposure if e.brand != measurement.brand]
    top_rival = max(others, key=lambda e: e.videos, default=None)
    tag_text = (
        f"{first.axis.label}の上位{len(first.axis.posts)}本のうち、"
        f"{measurement.brand}関連は{own.videos if own else 0}本"
        f"（{_fmt_pct(own.share_pct) if own else 'N/A'}）。"
    )
    if top_rival is not None:
        tag_text += (
            f"競合最多は{top_rival.brand}の{top_rival.videos}本"
            f"（{_fmt_pct(top_rival.share_pct)}）。"
        )
    tag_text += "検索結果の取り合いが、指名検索の前で起きている。"
    return Slide(
        type="D",
        part=1,
        q_number="",
        heading="一般キーワード検索は、いま誰が取り合っているのか",
        lead="一般キーワード検索の上位に露出しているブランドの構成（取得順のスナップショット）",
        tag=SlideTag(variant="結論", text=tag_text),
        data=SlideDataD(
            columns=["検索軸", "ブランド", "露出本数", "露出シェア", "最上位順位"],
            rows=rows,
        ),
    )


def _tier_slide(measurement: OmiyageMeasurement) -> Slide | None:
    axis_a, axis_b = _brand_pair(measurement)
    if axis_a is None or axis_b is None:
        # 片軸でも取得失敗なら、失敗側を「0本/N/A」の実測値として描かず
        # Q2 と同じく省略+開示（監査JSONの failed/omitted）で扱う。
        return None
    name_a = measurement.brand
    name_b = measurement.competitors[0] if measurement.competitors else "競合"
    tiers_a = measure_follower_tiers(axis_a.axis.posts)
    tiers_b = measure_follower_tiers(axis_b.axis.posts)
    rows = [
        [
            tier_a.label,
            f"{tier_a.videos}本",
            _fmt_num(tier_a.avg_plays),
            _fmt_pct(tier_a.avg_eg_rate_pct),
            f"{tier_b.videos}本",
            _fmt_num(tier_b.avg_plays),
            _fmt_pct(tier_b.avg_eg_rate_pct),
        ]
        for tier_a, tier_b in zip(tiers_a, tiers_b, strict=True)
    ]
    busiest = max(tiers_a, key=lambda tier: tier.videos)
    tag_text = (
        f"{name_a}のブランド名検索では{busiest.label}が{busiest.videos}本で最多。"
        "投稿の主役がどのフォロワー帯かで、施策の打ち手（起用先）が変わる。"
    )
    return Slide(
        type="D",
        part=1,
        q_number="Q1",
        heading="どのフォロワー帯が、再生とEG率を取っているのか",
        lead=(
            f"ブランド名検索上位のフォロワー帯別の本数・平均再生・平均EG率"
            f"（左: {name_a}／右: {name_b}）"
        ),
        tag=SlideTag(variant="発見", text=tag_text),
        data=SlideDataD(
            columns=[
                "フォロワー階層",
                f"{name_a} 本数",
                f"{name_a} 平均再生",
                f"{name_a} 平均EG率",
                f"{name_b} 本数",
                f"{name_b} 平均再生",
                f"{name_b} 平均EG率",
            ],
            rows=rows,
        ),
    )


def _pr_slide(measurement: OmiyageMeasurement) -> Slide | None:
    axis_a, axis_b = _brand_pair(measurement)
    if axis_a is None or axis_b is None:
        return None
    pr_a, no_pr_a = _pr_split(axis_a.axis.posts)
    pr_b, no_pr_b = _pr_split(axis_b.axis.posts)
    groups = [
        ComparisonGroup(
            label="#PR表記あり 本数",
            value_a=float(len(pr_a)),
            value_b=float(len(pr_b)),
            unit="本",
        ),
        ComparisonGroup(
            label="#PR表記なし 本数",
            value_a=float(len(no_pr_a)),
            value_b=float(len(no_pr_b)),
            unit="本",
        ),
        ComparisonGroup(
            label="#PR表記あり 平均EG率",
            value_a=avg_eg_rate_pct(pr_a) or 0.0,
            value_b=avg_eg_rate_pct(pr_b) or 0.0,
            unit="%",
        ),
        ComparisonGroup(
            label="#PR表記なし 平均EG率",
            value_a=avg_eg_rate_pct(no_pr_a) or 0.0,
            value_b=avg_eg_rate_pct(no_pr_b) or 0.0,
            unit="%",
        ),
    ]
    name_a = measurement.brand
    tag_text = (
        f"{name_a}のブランド名検索上位{len(axis_a.axis.posts)}本のうち"
        f"#PR表記ありは{len(pr_a)}本、なしは{len(no_pr_a)}本。"
        "#PRは完全一致タグのみで判定。本資料で「オーガニック」は"
        "#PR等の表記が確認できない投稿を指し、広告出稿の有無は断定しない。"
    )
    return Slide(
        type="C",
        part=1,
        q_number="Q2",
        heading="伸びているのは、PR表記あり投稿か・なし投稿か",
        lead="ブランド名検索上位の #PR 完全一致タグ有無別の本数と平均EG率（両群同じ指標で併記）",
        tag=SlideTag(variant="発見", text=tag_text),
        data=SlideDataC(groups=groups),
    )


def _cluster_slide(
    measurement: OmiyageMeasurement,
    analysis: VideoAnalysisReport | None,
) -> Slide | None:
    if analysis is None or analysis.analyzed == 0:
        return None
    axis_a, axis_b = _brand_pair(measurement)
    if axis_a is None and axis_b is None:
        return None
    posts_a = axis_a.axis.posts if axis_a else ()
    posts_b = axis_b.axis.posts if axis_b else ()
    assignments = analysis.assignments
    clusters_a = aggregate_clusters(posts_a, assignments, analysis.vocabulary)
    clusters_b = aggregate_clusters(posts_b, assignments, analysis.vocabulary)
    analyzed_a = sum(cluster.videos for cluster in clusters_a)
    analyzed_b = sum(cluster.videos for cluster in clusters_b)
    if analyzed_a == 0 and analyzed_b == 0:
        return None
    name_a = measurement.brand
    name_b = measurement.competitors[0] if measurement.competitors else "競合"
    groups = [
        ComparisonGroup(
            label=(
                f"{cluster_a.label}"
                f"（{name_a} {cluster_a.videos}本 / {name_b} {cluster_b.videos}本）"
            ),
            value_a=cluster_a.avg_eg_rate_pct or 0.0,
            value_b=cluster_b.avg_eg_rate_pct or 0.0,
            unit="%",
        )
        for cluster_a, cluster_b in zip(clusters_a, clusters_b, strict=True)
    ]
    failure_count = len(analysis.failures) + len(analysis.skipped_video_ids)
    tag_text = (
        f"クラスタは動画フレームの視覚AIによる推定分類。"
        f"分母は解析できた{name_a} {analyzed_a}本・{name_b} {analyzed_b}本のみで、"
        f"取得・解析できなかった{failure_count}本は分母に混ぜず監査記録に残している。"
    )
    return Slide(
        type="C",
        part=1,
        q_number="Q3",
        heading="どんな界隈で語られているのか（推定）",
        lead=(
            f"動画解析できた{name_a} {analyzed_a}本・{name_b} {analyzed_b}本の"
            "界隈クラスタ別の本数と平均EG率（分母=解析できた本数）"
        ),
        tag=SlideTag(variant="発見", text=tag_text),
        data=SlideDataC(groups=groups),
    )


def _keyword_axes(measurement: OmiyageMeasurement) -> list[AxisMeasurement]:
    ordered: list[AxisMeasurement] = []
    if measurement.brand_axis is not None:
        ordered.append(measurement.brand_axis)
    ordered.extend(measurement.competitor_axes)
    ordered.extend(measurement.general_axes)
    return ordered


def _telop_cell(telop: TelopMetrics) -> str:
    if telop.analyzed == 0:
        return "未計測"
    return f"{_fmt_pct(telop.rate_pct)} / {_fmt_num(telop.avg)}回（分母{telop.analyzed}本）"


def _keyword_slide(
    measurement: OmiyageMeasurement,
    analysis: VideoAnalysisReport | None,
) -> Slide | None:
    axes = _keyword_axes(measurement)
    if not axes:
        return None
    variants = keyword_variants(measurement.keywords)
    telops = analysis.telops if analysis is not None else {}
    rows: list[list[str]] = []
    best_route: tuple[str, float] | None = None
    for axis_measurement in axes:
        axis = axis_measurement.axis
        keyword = axis_measurement.keyword
        telop = measure_telop_route(axis.posts, telops, variants)
        tags = top_hashtags(axis.posts, limit=3)
        rows.append(
            [
                axis.label,
                f"{keyword.denominator}本",
                f"{_fmt_pct(keyword.rate_pct(keyword.caption.videos_with))}"
                f" / {_fmt_num(keyword.avg(keyword.caption.total_mentions))}回",
                f"{_fmt_pct(keyword.rate_pct(keyword.hashtag.videos_with))}"
                f" / {_fmt_num(keyword.avg(keyword.hashtag.total_mentions))}回",
                _telop_cell(telop),
                "、".join(f"{tag.display}({tag.videos})" for tag in tags) or "-",
            ]
        )
        rate = keyword.combined_rate_pct
        if rate is not None and (best_route is None or rate > best_route[1]):
            best_route = (axis.label, rate)
    kw_label = "・".join(f"「{kw}」" for kw in measurement.keywords)
    tag_text = (
        f"一般キーワード{kw_label}の登場率（caption+hashtag計）が最も高いのは"
        f"{best_route[0]}の{best_route[1]}%。"
        if best_route
        else "登場率を比較できる軸がなかった。"
    )
    tag_text += (
        "登場率の分母は0回の動画も含む。テロップは視覚AI読取で、解析できた動画のみを分母とする。"
    )
    return Slide(
        type="D",
        part=1,
        q_number="Q4",
        heading="キーワードは、どの経路で語られているのか",
        lead=(
            f"一般キーワード{kw_label}のキャプション/ハッシュタグ/テロップ3経路の"
            "登場率と1本平均回数（音声は未計測）"
        ),
        tag=SlideTag(variant="発見", text=tag_text),
        data=SlideDataD(
            columns=[
                "検索軸",
                "分母",
                "キャプション 登場率/平均",
                "ハッシュタグ 登場率/平均",
                "テロップ 登場率/平均",
                "頻出ハッシュタグ上位",
            ],
            rows=rows,
        ),
    )


def _top5_axis(measurement: OmiyageMeasurement) -> AxisMeasurement | None:
    if measurement.general_axes:
        return measurement.general_axes[0]
    return measurement.brand_axis


def top5_candidates(measurement: OmiyageMeasurement) -> tuple[PostRecord, ...]:
    """Q5 TOP5 の対象投稿（skill 側の解析対象選定と組成側で共用する唯一の定義）。"""
    axis_measurement = _top5_axis(measurement)
    if axis_measurement is None:
        return ()
    candidates = [post for post in axis_measurement.axis.posts if post.url]
    return top_posts(candidates, limit=5)


def _thumb_data_uri(data: bytes) -> str | None:
    """実フレーム bytes → data URI（JPEG/PNG 以外は不採用 = 捏造しない）。"""
    if data.startswith(b"\xff\xd8"):
        mime = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    else:
        return None
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def _embed_budget_bytes() -> tuple[int, int]:
    """spec の embed_budget（per-image / deck total・decoded bytes）。"""
    from teamagent.skills.omiyage_report.fmt.spec import load_fmt_spec

    budget = load_fmt_spec().image_rules.embed_budget
    return budget.per_image_max_kb * 1024, budget.deck_total_max_kb * 1024


def _select_top5_thumbs(
    measurement: OmiyageMeasurement,
    analysis: VideoAnalysisReport | None,
) -> tuple[list[tuple[PostRecord, str, int]], list[str]]:
    """TOP5候補のうち実フレームを埋め込める投稿を選ぶ（budget 順守・脱落は開示）。

    戻り値: ([(post, data_uri, bytes_len)], dropped_video_ids)。
    デッキ合計バジェットは「表紙が先頭2枚を再掲する」ぶんまで含めて計算する。
    """
    candidates = top5_candidates(measurement)
    if not candidates:
        return [], []
    thumbs = analysis.thumbs if analysis is not None else {}
    per_image_max, deck_total_max = _embed_budget_bytes()
    selected: list[tuple[PostRecord, str, int]] = []
    dropped: list[str] = []
    for post in candidates:
        data = thumbs.get(post.video_id, b"")
        uri = _thumb_data_uri(data) if data else None
        if uri is None or len(data) > per_image_max:
            dropped.append(post.video_id)
            continue
        selected.append((post, uri, len(data)))

    def deck_total(items: Sequence[tuple[PostRecord, str, int]]) -> int:
        sizes = [size for _post, _uri, size in items]
        cover_dup = sizes[0] + sizes[1] if len(sizes) >= 2 else 0
        return sum(sizes) + cover_dup

    while selected and deck_total(selected) > deck_total_max:
        dropped.append(selected.pop()[0].video_id)
    return selected, dropped


def _top5_slide(
    measurement: OmiyageMeasurement,
    analysis: VideoAnalysisReport | None,
) -> Slide | None:
    axis_measurement = _top5_axis(measurement)
    if axis_measurement is None:
        return None
    selected, _dropped = _select_top5_thumbs(measurement, analysis)
    if not selected:
        # 実フレームが1枚も無ければ Q5 は省略し、監査JSON+開示文言に回す（黙って埋めない）
        return None
    cards = [
        RankingCard(
            source_url=post.url,
            image=CardImage(data_uri=uri, image_kind="real_frame"),
            metrics=RankingMetrics(
                plays=post.plays,
                eg_rate_pct=post.eg_rate_pct,
                followers=post.followers,
            ),
            account_name=post.nickname or post.author,
            content_summary=post.caption[:60],
        )
        for post, uri, _size in selected
    ]
    top1 = selected[0][0]
    tag_text = (
        f"最多再生は{top1.nickname or top1.author}の{top1.plays}再生"
        f"（EG率{top1.eg_rate_pct}%）。"
        "画像は取得動画の解析フレーム（1コマ目・実フレーム）で、実画面の解剖は詳細版で行う。"
    )
    return Slide(
        type="E",
        part=1,
        q_number="Q5",
        heading="いちばん見られている動画は、どれか",
        lead=f"{axis_measurement.axis.label}上位の再生数TOP{len(cards)}（再生数/EG率/フォロワー）",
        tag=SlideTag(variant="発見", text=tag_text),
        data=SlideDataE(cards=cards),
    )


def _summary_slide(
    measurement: OmiyageMeasurement,
    q_slides: Sequence[Slide],
) -> Slide:
    rows: list[SummaryRow] = []
    for index, slide in enumerate(q_slides, start=1):
        label = slide.q_number or "現状"
        assert slide.tag is not None  # 便1の各Qスライドはタグ必須（組成側の不変量）
        rows.append(
            SummaryRow(
                number=index,
                pattern=f"{label}: {slide.heading}",
                description=slide.tag.text,
            )
        )
    own_line = "検索面の現状データを、次回の具体提案（上位動画の型・実画面比較）へつなげたい。"
    return Slide(
        type="H",
        part=None,  # H は SUMMARY 扱い（PART ラベルを付けない・レンダラ規範）
        q_number="",
        heading="総括：検索データから見えた現状",
        lead="各ページの発見の再掲（数値は全て本資料の実測値）",
        tag=None,
        data=SlideDataH(
            summary_rows=rows,
            cta=True,
            conclusion=own_line,
        ),
    )


def build_deck_plan(
    measurement: OmiyageMeasurement,
    analysis: VideoAnalysisReport | None,
    *,
    generated_on: str,
    search_depth: int,
    issuer: str | None = None,
    research_notes: str = "",
) -> DeckPlan:
    """U1構成の計測JSONを組む。組める材料が無ければ DeckPlanBuildError。

    ``research_notes``（任意）は出典URL付きの行だけを「先行調査」D スライドとして露出シェア
    導入の直後に併記する（Q番号なし・H の再掲行には含めない＝U1 の 6 行上限を保つ）。
    """
    if not measurement.competitors:
        raise DeckPlanBuildError("competitors are required for the deck brand pair")

    q_slides = [
        slide
        for slide in (
            _exposure_slide(measurement),
            _tier_slide(measurement),
            _pr_slide(measurement),
            _cluster_slide(measurement, analysis),
            _keyword_slide(measurement, analysis),
            _top5_slide(measurement, analysis),
        )
        if slide is not None
    ]
    if not q_slides:
        raise DeckPlanBuildError("no slide could be built from measurements")
    # U2脚注（EG率定義）は「EG率が初出するスライド」に1回だけ付ける。
    # Q1 固定にしない（Q1 が軸取得失敗で省略されても脚注を失わない）。
    for index, slide in enumerate(q_slides):
        if "EG率" in slide.lead:
            q_slides[index] = slide.model_copy(update={"footnote": EG_RATE_FOOTNOTE})
            break
    # 先行調査（research_notes）は露出シェア導入（Q番号なし）の直後、無ければ先頭に置く。
    content_slides = list(q_slides)
    research_slide = _research_slide(research_notes) if research_notes else None
    if research_slide is not None:
        insert_at = 1 if content_slides[0].q_number == "" else 0
        content_slides.insert(insert_at, research_slide)

    q_list = [
        QListItem(q_number=slide.q_number, question=_QUESTIONS[slide.q_number])
        for slide in q_slides
        if slide.q_number
    ]
    part_title = "データで見る現状"
    cover_kw = measurement.keywords[0] if measurement.keywords else measurement.brand
    axes_summary = "、".join(
        f"{m.axis.label}{len(m.axis.posts)}本" for m in measurement.axes if not m.axis.failed
    )
    deck_meta = DeckMeta(
        addressee=f"{measurement.brand}様",
        cover_title=f"「{cover_kw}」検索面の現状解剖",
        abstract=(
            f"TikTok検索結果の実測データ（{axes_summary}）から、"
            f"{measurement.brand}と競合の露出・語られ方の現状を確認する。"
        ),
        category_en="TIKTOK SEARCH SNAPSHOT REPORT",
        running_head="TIKTOK SEARCH REPORT",
        issuer=issuer or os.environ.get("OMIYAGE_ISSUER", "NewsTV"),
        brand_a=DeckBrand(name=measurement.brand),
        brand_b=DeckBrand(name=measurement.competitors[0]),
        part_titles=[part_title],
        method_target_constraints=[
            (
                f"手法: TikTok検索結果の実測取得（各軸 上位最大{search_depth}本・"
                "取得順位は検索時スナップショット）+ 上位動画の視覚AI解析"
            ),
            (
                f"対象: {axes_summary or '取得できた検索軸'}。"
                "登場率の分母は各軸の取得本数（キーワード0回の動画も含む）"
            ),
            VOICE_UNMEASURED_NOTE,
        ],
    )
    # 表紙の段差サムネ2枚 = Q5 TOP の実フレーム上位2枚を再掲（新規取得はしない）
    top5_slide = next((slide for slide in q_slides if slide.type == "E"), None)
    cover_data: SlideDataA | None = None
    if top5_slide is not None and isinstance(top5_slide.data, SlideDataE):
        top_cards = top5_slide.data.cards
        if len(top_cards) >= 2:
            cover_data = SlideDataA(
                thumbnail_pair=[
                    Card(source_url=card.source_url, image=card.image) for card in top_cards[:2]
                ]
            )
    slides: list[Slide] = [
        Slide(
            type="A",
            part=None,
            q_number="",
            heading=deck_meta.cover_title,
            lead="",
            tag=None,
            data=cover_data,
        ),
        Slide(
            type="B",
            part=1,
            q_number="",
            heading=part_title,
            lead="",
            tag=None,
            data=SlideDataB(
                part=1,
                title=part_title,
                abstract=deck_meta.abstract,
                q_list=q_list or [QListItem(q_number="Q1", question=_QUESTIONS["Q1"])],
            ),
        ),
        *content_slides,
        _summary_slide(measurement, q_slides),
    ]
    return DeckPlan(
        generated_on=generated_on,
        deck_meta=deck_meta,
        slide_plan=slides,
    )


def build_audit(
    measurement: OmiyageMeasurement,
    analysis: VideoAnalysisReport | None,
    plan: DeckPlan | None,
    *,
    generated_on: str,
    search_depth: int,
    research_notes: str = "",
) -> dict[str, Any]:
    """案件記録用の監査JSON（取得失敗一覧・解析失敗一覧・コスト・分母定義）。"""
    included = [slide.q_number for slide in (plan.slide_plan if plan else []) if slide.q_number]
    omitted = [q for q in _QUESTIONS if q not in included]
    selected, thumb_dropped = _select_top5_thumbs(measurement, analysis)
    research_adopted, research_dropped = (
        parse_research_notes(research_notes) if research_notes else ([], 0)
    )
    return {
        "research_notes": {
            # 先行調査メモの採否（出典URLの無い行は採用しない・紙面超過分は記録のみ）
            "provided": bool(research_notes),
            "adopted": len(research_adopted),
            "shown": min(len(research_adopted), _RESEARCH_MAX_ROWS),
            "dropped_without_source": research_dropped,
            "sources": [note.source_url for note in research_adopted],
        },
        "thumbnails": {
            # Q5/表紙の実フレーム埋め込み状況（budget・未解析で落とした投稿は開示）
            "embedded": len(selected),
            "dropped_video_ids": thumb_dropped,
        },
        "generated_on": generated_on,
        "search_depth": search_depth,
        "cta_text": CTA_TEXT,
        "axes": [
            {
                "role": m.axis.role,
                "label": m.axis.label,
                "query": m.axis.query,
                "requested": m.axis.requested,
                "fetched": len(m.axis.posts),
                "failed": m.axis.failed,
                "failure_code": m.axis.failure_code,
            }
            for m in measurement.axes
        ],
        "denominator_rule": (
            "登場率の分母は取得できた全投稿（0回も含む）。テロップ経路の分母は解析できた本数。"
        ),
        "video_analysis": analysis.to_audit() if analysis is not None else None,
        "included_q_numbers": included,
        "omitted_q_numbers": omitted,
    }


__all__ = [
    "DeckPlanBuildError",
    "ResearchNote",
    "build_audit",
    "build_deck_plan",
    "parse_research_notes",
    "top5_candidates",
]
