"""直接投稿（Block Kit）のテスト用の本番の形のデータ（2026-09-28 本番 DM の A/B/C）。

出どころ: scratchpad/slackfmt/samples.md（本番で届いた Slack の生テキスト）。
- A（17:00・検索上位チェック 1 段目）: 集計（SurfaceFacts）・結論（LLM）・切り口・上位の行は実物の
  数字どおり。samples に行が無い投稿（3・4・6〜9・11〜30 位）は、実物の集計（区分の本数・常連の
  枠・PR表記の順位・保存率の上位）と矛盾しない仮の投稿で埋めた（@ID は ``sample_`` で始まる）。
  フォロワー帯は 17:00 の文面に無いので、15:30 版（D）の「10万〜100万人が本数の20%・再生の66%」を
  使い、残りの帯は仮の値。レポートの URL は samples で途中が省略されているので仮のトークン。
- B（15:42・2 段目の追記）: 集計（VideoDigest）・結論・1 本ずつは実物どおり。投稿の URL は
  samples に無いので仮の動画 ID（``/video/74000000000000000xx``）。
- C（13:51・動画分析の完了）: 平均 ENG・保存率・尺・最も共通する点は実物どおり。その点の本数
  （observed_in）は文面に無いので 4/5 本と仮に置いた。上位 5 本の行は A の実物（1・2・5 位）と仮の投稿。
"""

from __future__ import annotations

import datetime as dt

from teamagent.skills.search_surface_check.schema import (
    CategoryStat,
    ConclusionPoint,
    FollowupVideo,
    HolderStat,
    KwSurface,
    LabelCount,
    SaveLeader,
    SearchSurfaceCheckInput,
    SearchSurfaceCheckOutput,
    SurfaceConclusion,
    SurfaceFacts,
    SurfacePost,
    SurfaceVideoFollowupOutput,
    TagStat,
    TierStat,
    VideoDigest,
    VideoDigestConclusion,
)
from teamagent.skills.search_surface_check.summary import followup_notice_line
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    CrossAnalysis,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
    WinFactor,
)

JST = dt.timezone(dt.timedelta(hours=9))
KEYWORD = "スパイスカレー 作り方"
CLIENT = "GABAN"
A_MEASURED = int(dt.datetime(2026, 9, 28, 17, 0, tzinfo=JST).timestamp())
B_MEASURED = int(dt.datetime(2026, 9, 28, 15, 42, tzinfo=JST).timestamp())
_DAY = 86_400

A_REPORT = (
    "https://connect.newstv.co.jp/r/eyJrIjoidnNlby1yZXBvcnRzL3N1cmZhY2UifQ.nbKXJFYvTqmoMtPh-UiDKQ"
)
B_REPORT = (
    "https://connect.newstv.co.jp/r/eyJrIjoidnNlby1yZXBvcnRzL3ZpZGVvIn0.efdfZX6QI91_VZ-VBTkG5w"
)
C_REPORT = "https://connect.newstv.co.jp/r/eyJrIjoidnNlby1yZXBvcnRzL2FsZ28ifQ.Qm9vX2FsZ29fcmVwb3J0"
C_SLIDES = "https://connect.newstv.co.jp/r/eyJrIjoidnNlby1zbGlkZXMvYWxnbyJ9.U2xpZGVzX2FsZ29fMDE"

# 実物の上位の行（A）: (順位, @ID, 表示名, 区分, フォロワー, 再生, 保存率%, 投稿からの日数, PR, 本文, 動画 ID)
_KNOWN: dict[int, tuple[str, str, str, int, int, float, int, bool, str, str]] = {
    1: ("gonosara", "ごのさら", "creator", 52_000, 351_000, 0.8, 1_400, True,
        "【4つでいい。本格スパイスカレー】 スパイスカレー。 料理をより好きになった",
        "7165798692291644674"),
    2: ("spice_koki", "こうき｜日本一周カレーキャラバン", "ugc", 2_930, 224_000, 0.9, 485, False,
        "スパイスカレーを作るなら、 まずはこの4つだけ覚えておけばOK👌", "7504619681093799186"),
    5: ("katokenoshokutaku", "加藤家の食卓", "influencer", 154_000, 826_000, 0.4, 1_490, False,
        "@katokenosyokutaku 🍚🥢🙋‍♀️🙋", "7134797929339981058"),
    10: ("kantanrecipi", "かんたんレシピ", "ugc", 493, 69_000, 0.4, 1_065, False,
         "スパイスカレーって意外と簡単に作れちゃうんです🫣🍛基本の4種類で作ってみたよ",
         "7293836896323554562"),
}  # fmt: skip

# 実物の集計と矛盾しない仮の投稿（区分: メディア 4・インフルエンサー 5・クリエイター 9・一般 12）。
_HOLDER_ROWS: dict[int, tuple[str, str, str, int]] = {
    8: ("kurashiru.com", "クラシル【公式】Kurashiru", "media", 412_000),
    9: ("kurashiru.com", "クラシル【公式】Kurashiru", "media", 412_000),
    12: ("kurashiru.com", "クラシル【公式】Kurashiru", "media", 412_000),
    26: ("kurashiru.com", "クラシル【公式】Kurashiru", "media", 412_000),
    11: ("spice_koki", "こうき｜日本一周カレーキャラバン", "ugc", 2_930),
    22: ("spice_koki", "こうき｜日本一周カレーキャラバン", "ugc", 2_930),
    6: ("musuicurry", "無水カレーニキ", "influencer", 138_000),
    14: ("musuicurry", "無水カレーニキ", "influencer", 138_000),
    16: ("musuicurry", "無水カレーニキ", "influencer", 138_000),
    27: ("user8013099312681", "user8013099312681", "ugc", 1_200),
}
_SAVE_RATES = {27: 2.88, 11: 2.38, 21: 1.62}
PR_RANKS = [1, 6, 12, 14, 16]


def tiktok_url(author: str, video_id: str) -> str:
    return f"https://www.tiktok.com/@{author}/video/{video_id}"


def _post(rank: int) -> SurfacePost:
    if rank in _KNOWN:
        author, name, cat, followers, plays, save, days, pr, desc, vid = _KNOWN[rank]
    else:
        if rank in _HOLDER_ROWS:
            author, name, cat, followers = _HOLDER_ROWS[rank]
        elif rank in (3, 13, 17, 19, 20, 24, 28, 29):
            author, name, cat, followers = f"sample_creator{rank}", "", "creator", 30_000 + rank
        elif rank == 4:
            author, name, cat, followers = "sample_influencer4", "", "influencer", 210_000
        elif rank == 7:
            author, name, cat, followers = "sample_small7", "", "ugc", 5_100
        else:
            author, name, cat, followers = f"sample_user{rank}", "", "ugc", 12_000 + rank
        plays = max(9_000, 300_000 - rank * 9_000)
        save = _SAVE_RATES.get(rank, 0.6)
        days = 40 if rank in (3, 7, 13, 18, 25) else 330 + rank * 7
        pr = rank in PR_RANKS
        desc = f"スパイスカレーの作り方 その{rank}"
        vid = f"74000000000000000{rank:02d}"
    return SurfacePost(
        platform="tiktok",
        keyword=KEYWORD,
        rank=rank,
        url=tiktok_url(author, vid),
        author=author,
        author_name=name,
        author_followers=followers,
        desc=desc,
        hashtags=["スパイスカレー"],
        play_count=plays,
        save_count=round(plays * save / 100),
        posted_at=A_MEASURED - days * _DAY,
        duration_sec=60,
        category=cat,  # type: ignore[arg-type]
        is_pr=pr,
    )


def a_facts() -> SurfaceFacts:
    return SurfaceFacts(
        n=30,
        unique_authors=24,
        categories=[
            CategoryStat(category="ugc", count=12, count_share=0.4, play_share=0.14, median_plays=40_000),
            CategoryStat(category="creator", count=9, count_share=0.3, play_share=0.40, median_plays=90_000),
            CategoryStat(category="influencer", count=5, count_share=0.167, play_share=0.34, median_plays=150_000),
            CategoryStat(category="media", count=4, count_share=0.133, play_share=0.12, median_plays=60_000),
        ],
        holders=[
            HolderStat(author="kurashiru.com", author_name="クラシル【公式】Kurashiru", category="media", ranks=[8, 9, 12, 26]),
            HolderStat(author="spice_koki", author_name="こうき｜日本一周カレーキャラバン", category="ugc", ranks=[2, 11, 22]),
            HolderStat(author="musuicurry", author_name="無水カレーニキ", category="influencer", ranks=[6, 14, 16]),
        ],
        tiers=[
            TierStat(tier="10万〜100万人", count=6, play_share=0.66),
            TierStat(tier="1万〜10万人", count=13, play_share=0.24),
            TierStat(tier="1万人未満", count=11, play_share=0.10),
        ],
        small_in_top10=3,
        top10_n=10,
        median_plays=66_000,
        reach_ratio_median=2.78,
        most_played_rank=5,
        rank_play_rho=0.34,
        median_save_rate_pct=0.71,
        save_leaders=[
            SaveLeader(rank=27, author="user8013099312681", save_rate_pct=2.88, plays=48_000),
            SaveLeader(rank=11, author="spice_koki", save_rate_pct=2.38, plays=201_000),
            SaveLeader(rank=21, author="sample_user21", save_rate_pct=1.62, plays=111_000),
        ],
        median_age_days=335,
        recent_90d=5,
        median_duration_sec=60,
        kw_in_text=7,
        top_tags=[
            TagStat(tag="スパイスカレー", count=19),
            TagStat(tag="カレー", count=9),
            TagStat(tag="スパイス", count=6),
            TagStat(tag="簡単レシピ", count=6),
            TagStat(tag="レシピ", count=5),
        ],
        pr_ranks=PR_RANKS,
        client_ranks=[],
        mention_ranks=[],
    )  # fmt: skip


def a_conclusion() -> SurfaceConclusion:
    return SurfaceConclusion(
        headline="クリエイターとインフルエンサーが再生の74%を占める。小規模アカウントも3本入賞し、新規参入の余地あり。",
        winning=ConclusionPoint(
            text="クリエイター（本数30%、再生40%）とインフルエンサー（本数17%、再生34%）が上位の中心。@spice_koki、@musuicurry、@kurashiru.comが常連で、特に@spice_kokiは2,930フォロワーながら2位・11位で高再生を獲得。保存率が高い投稿（27位2.88%、11位2.38%）は手順の見返し需要に応えている。",
            ranks=[2, 6, 11, 27],
        ),
        gap=ConclusionPoint(
            text="直近90日以内の投稿は5本のみで、入れ替わりが遅い面。一度上位に入れば残りやすいが、新投稿の突破は難しい可能性。本文やタグに検索KWを明記した投稿は7本に留まり、KW最適化で差をつけられる余地がある。クライアント投稿は現在0本。",
        ),
        actions=[
            ConclusionPoint(
                text="保存率が高い投稿（1.6～2.9%）の共通点は調理工程の詳細化。手順を見返す需要に応える構成で、保存率向上を狙う。小規模アカウント（フォロワー1万未満）も上位10本に3本いるため、新規クリエイターとの協業も検討。",
                ranks=[27, 11, 21],
            )
        ],
        angles=[
            ConclusionPoint(text="基本スパイス4種類の選び方・黄金比", ranks=[2, 11]),
            ConclusionPoint(text="初心者向け・簡単・失敗しない", ranks=[10, 17, 19]),
            ConclusionPoint(text="無水カレー・時短調理", ranks=[6, 13, 14]),
            ConclusionPoint(text="玉ねぎの炒め方・下ごしらえのコツ", ranks=[8, 9, 27]),
        ],
        generated_by="llm",
    )


def a_input() -> SearchSurfaceCheckInput:
    return SearchSurfaceCheckInput(keywords=[KEYWORD], platforms=["tiktok"], client_name=CLIENT)


def a_output() -> SearchSurfaceCheckOutput:
    """A（17:00）の出力。slack_summary は今の文面の組み立て（summary.py）で作る。"""
    from teamagent.skills.search_surface_check.summary import (
        build_slack_summary,
        insert_before_report_line,
    )

    surface = KwSurface(
        keyword=KEYWORD,
        platform="tiktok",
        posts=[_post(r) for r in range(1, 31)],
        facts=a_facts(),
        conclusion=a_conclusion(),
    )
    out = SearchSurfaceCheckOutput(
        keywords=[KEYWORD],
        surfaces=[surface],
        report_url=A_REPORT,
        total_cost_usd=0.0186,
        measured_epoch=A_MEASURED,
    )
    out.slack_summary = build_slack_summary(
        out, a_input(), now_epoch=A_MEASURED, missing_platforms=[]
    )
    note = followup_notice_line(5)
    out.slack_summary = insert_before_report_line(out.slack_summary, note)
    out.followup_note = note
    return out


# ── B（2 段目の追記・15:42）──────────────────────────────────────────────

_B_ROWS = [
    FollowupVideo(rank=1, author="gonosara", url=tiktok_url("gonosara", "7400000000000000101"), state="watched", hook="ビジュアル", opening_telop=True, telop_kw=True, spoken_kw=False, duration_sec=59, pacing="ふつう", has_cta=True, narration=False),
    FollowupVideo(rank=2, author="spice_koki", url=tiktok_url("spice_koki", "7400000000000000102"), state="watched", hook="問題提起", opening_telop=True, telop_kw=True, spoken_kw=True, duration_sec=45, pacing="ふつう", has_cta=True, narration=True),
    FollowupVideo(rank=3, author="musuicurry", url=tiktok_url("musuicurry", "7400000000000000103"), state="watched", hook="POV", opening_telop=True, telop_kw=True, spoken_kw=True, duration_sec=75, cut_count=19, pacing="ふつう", has_cta=True, narration=True),
    FollowupVideo(rank=4, author="itamae_shinya", url=tiktok_url("itamae_shinya", "7400000000000000104"), state="watched", hook="問題提起", opening_telop=True, telop_kw=True, spoken_kw=True, duration_sec=60, pacing="ふつう", has_cta=True, narration=True),
    FollowupVideo(rank=5, author="kurashiru.com", url=tiktok_url("kurashiru.com", "7400000000000000105"), state="cover_only"),
]  # fmt: skip


def b_digest() -> VideoDigest:
    return VideoDigest(
        keyword=KEYWORD,
        requested=5,
        reserved=5,
        watched=4,
        watched_ranks=[1, 2, 3, 4],
        cover_only_ranks=[5],
        failed_ranks=[],
        hook_types=[
            LabelCount(label="問題提起", count=2),
            LabelCount(label="ビジュアル", count=1),
            LabelCount(label="POV", count=1),
        ],
        opening_telop=4,
        telop_kw=4,
        spoken_kw=3,
        median_duration_sec=60.0,
        median_cut_count=19.0,
        pacing=[LabelCount(label="ふつう", count=4)],
        cta=4,
        cta_types=[
            LabelCount(label="来店", count=3),
            LabelCount(label="保存", count=1),
            LabelCount(label="コメント", count=1),
            LabelCount(label="シェア", count=1),
        ],
        narration=3,
        trending_sound=0,
        median_coherence=95.0,
        save_top_ranks=[2, 3],
        save_top_common=[
            "冒頭にテロップ",
            "テロップに KW",
            "発話に KW",
            "テンポがふつう",
            "ナレーションあり",
        ],
    )


def b_output() -> SurfaceVideoFollowupOutput:
    return SurfaceVideoFollowupOutput(
        keyword=KEYWORD,
        status="ok",
        digest=b_digest(),
        conclusion=VideoDigestConclusion(
            headline="スパイス選びと分量を明確にした、初心者向けの実践的なレシピ動画が上位を占める",
            winning=ConclusionPoint(
                text="4/4本が冒頭テロップと検索KWを含み、3/4本がナレーションで発話にもKWを出す。問題提起で初心者の悩みに共感してから解決策を示す型が2本あり、スーパーで買える具体的なスパイスや分量をテロップとナレーションで重ねて伝える構成が保存につながっている。",
                ranks=[2, 4],
            ),
            save_reason=ConclusionPoint(
                text="保存率の高い2位と3位は、スーパーの売り場で実際に買い揃える買い物リストとして、また後で見返して作る際の分量・手順リストとして機能している。どちらも材料と調味料の分量を正確にテロップとナレーションで開示している点が共通している。",
                ranks=[2, 3],
            ),
            generated_by="llm",
            grounded=True,
        ),
        report_url=B_REPORT,
        slack_text="**上位5本の動画の中身**「スパイスカレー 作り方」TikTok\n（今の文字だけの文面）\n_概算 $0.3596_",
        total_cost_usd=0.3596,
        videos=[row.model_copy() for row in _B_ROWS],
        measured_epoch=B_MEASURED,
    )  # fmt: skip


# ── C（動画分析の完了・13:51）──────────────────────────────────────────────


def _c_video(rank: int, author: str, vid: str, plays: int, saves: int, sec: float) -> AnalyzedVideo:
    return AnalyzedVideo(
        meta=VideoMeta(
            rank=rank,
            url=tiktok_url(author, vid),
            author=author,
            play_count=plays,
            collect_count=saves,
            engagement_rate=2.75,
            duration_sec=sec,
        ),
        analysis=VideoVSEOAnalysis(duration_sec=sec, hook_type="problem"),
    )


def c_output() -> VideoAlgorithmOutput:
    return VideoAlgorithmOutput(
        query=KEYWORD,
        videos=[
            _c_video(1, "gonosara", "7165798692291644674", 351_000, 2_808, 59.0),
            _c_video(2, "spice_koki", "7504619681093799186", 224_000, 2_016, 45.0),
            _c_video(3, "sample_creator3", "7400000000000000203", 180_000, 1_500, 62.0),
            _c_video(4, "sample_influencer4", "7400000000000000204", 150_000, 900, 58.0),
            _c_video(5, "katokenoshokutaku", "7134797929339981058", 826_000, 3_304, 70.0),
        ],
        cross=CrossAnalysis(
            keyword=KEYWORD,
            video_count=5,
            avg_engagement_rate=2.75,
            avg_save_rate=0.806,
            median_duration_sec=59.0,
            win_factors=[
                WinFactor(
                    factor="テロップ(焼き込み)に検索KWが出る",
                    observed_in=4,
                    total=5,
                    confidence="中",
                    evidence="上位5本中4本で観測",
                )
            ],
            summary=(
                f"KW「{KEYWORD}」上位5本＝平均ENG 2.75% / 保存率 0.806% / 尺中央値 59.0秒。"
                "最も共通する勝ち筋は『テロップ(焼き込み)に検索KWが出る』。"
            ),
        ),
        report_url=C_REPORT,
        slides_url=C_SLIDES,
        slack_summary="🔎 **VSEO動画アルゴリズム分析** 完了（今の文字だけの文面）",
        total_cost_usd=0.4176,
    )


# ── 悪意のある第三者の文字列（無害化の確かめ用）──────────────────────────

HOSTILE = "<!here> <@U0EVIL0001> <https://evil.example/login|公式サイト> A&B *太字* `code` ~s~ |x"
EVIL_URL = "https://evil.example/@x/video/1"


def hostile_a() -> tuple[SearchSurfaceCheckOutput, SearchSurfaceCheckInput]:
    """KW・クライアント名・表示名・本文・LLM の文・切り口・URL に制御列を混ぜた A。"""
    out = a_output()
    surface = out.surfaces[0]
    surface.keyword = f"カレー {HOSTILE}"
    surface.posts[0].author_name = HOSTILE
    surface.posts[0].desc = HOSTILE
    surface.posts[1].url = "https://www.tiktok.com/@spice_koki/video/1|偽名>"
    surface.posts[2].url = EVIL_URL
    surface.posts[3].author = "evil<!channel>"
    assert surface.conclusion is not None and surface.facts is not None
    surface.conclusion.headline = f"結論 {HOSTILE}"
    surface.conclusion.actions[0].text = f"読み {HOSTILE}"
    surface.conclusion.angles[0].text = f"切り口 {HOSTILE}"
    surface.facts.holders[0].author = "evil<@U0EVIL0002>"
    out.warnings = [f"注意 {HOSTILE}"]
    out.followup_note = f"予告 {HOSTILE}"
    inp = SearchSurfaceCheckInput(
        keywords=[surface.keyword], platforms=["tiktok"], client_name=f"GABAN {HOSTILE}"
    )
    return out, inp


def hostile_b() -> SurfaceVideoFollowupOutput:
    out = b_output()
    out.keyword = f"カレー {HOSTILE}"
    assert out.conclusion is not None and out.conclusion.save_reason is not None
    out.conclusion.headline = f"結論 {HOSTILE}"
    out.conclusion.save_reason.text = f"保存 {HOSTILE}"
    out.videos[0].author = "evil<!channel>"
    out.videos[1].url = EVIL_URL
    out.videos[2].hook = HOSTILE
    return out


def hostile_c() -> VideoAlgorithmOutput:
    out = c_output()
    out.query = f"カレー {HOSTILE}"
    out.cross.win_factors[0].factor = HOSTILE
    out.quota_note = HOSTILE
    out.videos[0].meta.author = "evil<!channel>"
    out.videos[1].meta.url = EVIL_URL
    return out


__all__ = [
    "A_MEASURED",
    "A_REPORT",
    "B_MEASURED",
    "B_REPORT",
    "CLIENT",
    "C_REPORT",
    "C_SLIDES",
    "EVIL_URL",
    "HOSTILE",
    "KEYWORD",
    "a_input",
    "a_output",
    "b_output",
    "c_output",
    "hostile_a",
    "hostile_b",
    "hostile_c",
    "tiktok_url",
]
