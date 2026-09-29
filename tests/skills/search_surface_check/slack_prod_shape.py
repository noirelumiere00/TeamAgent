"""直接投稿（Block Kit）のテスト用の本番の形のデータ（2026-09-28 本番 DM の A/B/C）。

出どころ: scratchpad/slackfmt/samples.md（本番で届いた Slack の生テキスト）。
- A（17:00・検索上位チェック 1 段目）: 集計（SurfaceFacts）は**投稿から ``insights.compute_facts`` で
  計算する**（手で書いた集計と投稿の食い違いを作らない）。投稿は、実物の集計（区分の本数と再生の
  割合・常連の枠・フォロワー1万人未満の本数・再生の中央値・再生÷フォロワーの中央値・保存率の
  中央値と上位・投稿時期・尺・KW を含む本数・よく付くタグ・PR表記の順位）が同じ値になるように置いた。
  実物の行は 1・2・5・10 位だけ（samples の「上位10本」の行。キャプションは samples に出ている頭まで）。
  ほかの投稿は仮（@ID が ``sample_`` で始まるもの・常連の枠の数字・タグの付き方）。
  - 21 位の保存率 1.62% は samples に無い。LLM の「1.6～2.9%」と根拠の順位（27・11・21位）から置いた。
  - フォロワー帯と順位と再生の一致度は投稿から計算した値（17:00 の文面に帯は無く、一致度は実物の
    0.34 と一致しない。一致度は Slack に出さない）。
  - レポートの URL は samples で途中が省略されているので仮のトークン。
- B（2 段目の追記）: 集計（VideoDigest）・結論・1 本ずつは実物どおり。追記が届いたのは 15:42 だが、
  出力の ``measured_epoch`` は 1 段目の集計の時刻（15:30＝D の版）。投稿の URL は samples に無いので
  仮の動画 ID（``/video/74000000000000001xx``）。
- C（13:51・動画分析の完了）: 平均 ENG・平均保存率・尺の中央値・共通点は実物どおり。その点の本数
  （observed_in）は文面に無いので 4/5 本と仮に置いた。上位 5 本の行は A の実物（1・2・5 位）と仮の
  投稿で、各行の保存率の平均が実物の平均保存率 0.806% になるように保存数を置いた。
"""

from __future__ import annotations

import datetime as dt

from teamagent.skills.search_surface_check.insights import compute_facts
from teamagent.skills.search_surface_check.schema import (
    ConclusionPoint,
    FollowupVideo,
    KwSurface,
    LabelCount,
    SearchSurfaceCheckInput,
    SearchSurfaceCheckOutput,
    SurfaceConclusion,
    SurfaceFacts,
    SurfacePost,
    SurfaceVideoFollowupOutput,
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
# 2 段目の measured_epoch は 1 段目の集計の時刻（追記を出した 15:42 ではない）。
B_MEASURED = int(dt.datetime(2026, 9, 28, 15, 30, tzinfo=JST).timestamp())
_DAY = 86_400

A_REPORT = (
    "https://connect.newstv.co.jp/r/eyJrIjoidnNlby1yZXBvcnRzL3N1cmZhY2UifQ.nbKXJFYvTqmoMtPh-UiDKQ"
)
B_REPORT = (
    "https://connect.newstv.co.jp/r/eyJrIjoidnNlby1yZXBvcnRzL3ZpZGVvIn0.efdfZX6QI91_VZ-VBTkG5w"
)
C_REPORT = "https://connect.newstv.co.jp/r/eyJrIjoidnNlby1yZXBvcnRzL2FsZ28ifQ.Qm9vX2FsZ29fcmVwb3J0"
C_SLIDES = "https://connect.newstv.co.jp/r/eyJrIjoidnNlby1zbGlkZXMvYWxnbyJ9.U2xpZGVzX2FsZ29fMDE"

# 投稿: 順位 → (@ID, 表示名, 区分, フォロワー, 再生, 保存率%, 投稿からの日数, タグ, 本文, 動画 ID)
# 実物は 1・2・5・10 位（本文は samples に出ている頭まで）。ほかは仮（上の docstring）。
_P = tuple[str, str, str, int, int, float, int, list[str], str, str]
_KNOWN: dict[int, _P] = {
    1: ("gonosara", "ごのさら", "creator", 52_000, 351_000, 0.80, 1_400,
        ["スパイスカレー", "カレー", "PR"], "【4つでいい。本格スパイスカレー】 スパイスカレー。",
        "7165798692291644674"),
    2: ("spice_koki", "こうき｜日本一周カレーキャラバン", "ugc", 2_930, 224_000, 0.90, 485,
        ["スパイスカレー", "スパイス"], "スパイスカレーを作るなら、 まずはこの4つだけ覚えて",
        "7504619681093799186"),
    5: ("katokenoshokutaku", "加藤家の食卓", "influencer", 154_000, 826_000, 0.40, 1_490,
        ["カレー"], "@katokenosyokutaku 🍚🥢🙋‍♀️🙋", "7134797929339981058"),
    10: ("kantanrecipi", "かんたんレシピ", "ugc", 493, 69_000, 0.40, 1_065,
         ["スパイスカレー", "簡単レシピ"], "スパイスカレーって意外と簡単に作れちゃうんです🫣🍛基",
         "7293836896323554562"),
}  # fmt: skip
_KURA = ("kurashiru.com", "クラシル【公式】Kurashiru", "media", 412_000)
_KOKI = ("spice_koki", "こうき｜日本一周カレーキャラバン", "ugc", 2_930)
_NIKI = ("musuicurry", "無水カレーニキ", "influencer", 138_000)
# 仮の投稿: 順位 → (@ID, 表示名, 区分, フォロワー), 再生, 保存率%, 日数, タグ, 本文に KW の語をすべて含むか
_SAMPLE: dict[int, tuple[tuple[str, str, str, int], int, float, int, list[str], bool]] = {
    3: (("sample_creator3", "", "creator", 48_000), 150_000, 0.70, 40, ["スパイスカレー", "簡単レシピ"], True),
    4: (("sample_influencer4", "", "influencer", 210_000), 55_000, 0.50, 400, ["料理"], False),
    6: (_NIKI, 45_000, 0.75, 300, ["スパイスカレー", "無水カレー", "PR"], True),
    7: (("sample_small7", "", "ugc", 5_100), 15_000, 0.60, 40, ["スパイスカレー"], False),
    8: (_KURA, 110_000, 0.72, 331, ["スパイスカレー", "レシピ"], True),
    9: (_KURA, 95_000, 0.66, 334, ["スパイスカレー", "レシピ"], False),
    11: (_KOKI, 20_000, 2.38, 320, ["スパイスカレー", "スパイス"], True),
    12: (_KURA, 80_000, 0.64, 338, ["カレー", "レシピ", "PR"], False),
    13: (("sample_creator13", "", "creator", 36_000), 130_000, 0.72, 40, ["スパイスカレー", "カレー"], False),
    14: (_NIKI, 35_000, 0.62, 310, ["スパイスカレー", "PR"], False),
    15: (("sample_user15", "", "ugc", 12_500), 10_000, 0.58, 330, ["カレー"], False),
    16: (_NIKI, 25_000, 0.56, 290, ["スパイスカレー", "PR"], True),
    17: (("sample_creator17", "", "creator", 27_000), 110_000, 0.74, 340, ["スパイスカレー", "簡単レシピ"], False),
    18: (("sample_user18", "", "ugc", 14_000), 9_000, 0.54, 40, ["スパイス"], False),
    19: (("sample_creator19", "", "creator", 38_168), 100_000, 0.76, 333, ["スパイスカレー", "簡単レシピ"], False),
    20: (("sample_creator20", "", "creator", 19_000), 90_000, 0.78, 700, ["スパイス"], False),
    21: (("sample_user21", "", "ugc", 11_000), 8_000, 1.62, 339, ["カレー", "スパイス"], True),
    22: (_KOKI, 12_000, 0.52, 600, ["スパイスカレー"], False),
    23: (("sample_user23", "", "ugc", 16_000), 8_000, 0.50, 800, ["カレー"], False),
    24: (("sample_creator24", "", "creator", 28_912), 85_000, 0.82, 900, ["スパイスカレー", "レシピ"], False),
    25: (("sample_user25", "", "ugc", 13_000), 7_000, 0.48, 40, ["簡単レシピ"], False),
    26: (_KURA, 63_000, 0.84, 1_200, ["スパイスカレー", "レシピ"], False),
    27: (("user8013099312681", "user8013099312681", "ugc", 1_200), 18_000, 2.88, 500, ["スパイスカレー", "スパイス"], True),
    28: (("sample_creator28", "", "creator", 23_000), 74_000, 0.86, 1_000, ["スパイスカレー", "カレー"], False),
    29: (("sample_creator29", "", "creator", 23_000), 70_000, 0.88, 1_100, ["自炊"], False),
    30: (("sample_user30", "", "ugc", 15_000), 6_000, 0.46, 1_300, ["カレー", "簡単レシピ"], False),
}  # fmt: skip


def tiktok_url(author: str, video_id: str) -> str:
    return f"https://www.tiktok.com/@{author}/video/{video_id}"


def _post(rank: int) -> SurfacePost:
    if rank in _KNOWN:
        author, name, cat, followers, plays, save, days, tags, desc, vid = _KNOWN[rank]
    else:
        (author, name, cat, followers), plays, save, days, tags, has_kw = _SAMPLE[rank]
        desc = f"スパイスカレーの作り方 その{rank}" if has_kw else f"スパイスカレー その{rank}"
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
        hashtags=tags,
        play_count=plays,
        save_count=round(plays * save / 100),
        posted_at=A_MEASURED - days * _DAY,
        duration_sec=60,
        category=cat,  # type: ignore[arg-type]
        is_pr="PR" in tags,
    )


def a_posts() -> list[SurfacePost]:
    return [_post(r) for r in range(1, 31)]


def a_facts(posts: list[SurfacePost] | None = None) -> SurfaceFacts:
    """投稿から計算した集計（skill と同じ compute_facts）。"""
    return compute_facts(
        posts if posts is not None else a_posts(),
        keyword=KEYWORD,
        client_name=CLIENT,
        now_epoch=A_MEASURED,
    )


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

    posts = a_posts()
    surface = KwSurface(
        keyword=KEYWORD,
        platform="tiktok",
        posts=posts,
        facts=a_facts(posts),
        conclusion=a_conclusion(),
    )
    out = SearchSurfaceCheckOutput(
        keywords=[KEYWORD],
        surfaces=[surface],
        report_url=A_REPORT,
        total_cost_usd=0.0186,
        measured_epoch=A_MEASURED,
        tiktok_source="direct",  # 1〜2 語は直接取得（samples の A は 1 語）
    )
    out.slack_summary = build_slack_summary(
        out, a_input(), now_epoch=A_MEASURED, missing_platforms=[]
    )
    note = followup_notice_line(5)
    out.slack_summary = insert_before_report_line(out.slack_summary, note)
    out.followup_note = note
    return out


# ── B（2 段目の追記・15:42 に届いた。1 段目は 15:30）──────────────────────────

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
            # 保存率 0.80・0.90・1.03・0.90・0.40% → 平均 0.806%（実物の平均保存率）
            _c_video(1, "gonosara", "7165798692291644674", 351_000, 2_808, 59.0),
            _c_video(2, "spice_koki", "7504619681093799186", 224_000, 2_016, 45.0),
            _c_video(3, "sample_creator3", "7400000000000000203", 180_000, 1_854, 62.0),
            _c_video(4, "sample_influencer4", "7400000000000000204", 150_000, 1_350, 58.0),
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


# ── 失敗・例外の経路（注記の文言と宛先を固定する用）───────────────────────────

CLIENT_ACCOUNT = "gaban_official"


def a_problem_case() -> tuple[SearchSurfaceCheckOutput, SearchSurfaceCheckInput]:
    """A に、クライアントのアカウント（3 位を仮にクライアントの投稿にする）・注意（warnings）・
    取得できなかった媒体（Instagram を頼んだが取れなかった）を足した版。"""
    out = a_output()
    surface = out.surfaces[0]
    posts = a_posts()
    posts[2] = posts[2].model_copy(
        update={
            "author": CLIENT_ACCOUNT,
            "author_name": "GABAN【公式】",
            "url": tiktok_url(CLIENT_ACCOUNT, "7400000000000000003"),
            "is_client": True,
        }
    )
    surface.posts = posts
    surface.client_ranks = [3]
    surface.facts = a_facts(posts)
    out.warnings = ["一部の面は AI の読みを作れず、集計だけの見出しにしています"]
    inp = SearchSurfaceCheckInput(
        keywords=[KEYWORD],
        platforms=["tiktok", "instagram"],
        client_name=CLIENT,
        client_accounts=[f"@{CLIENT_ACCOUNT}"],
    )
    return out, inp


def b_partial_output() -> SurfaceVideoFollowupOutput:
    """5 本頼んだが今月の残りで 4 本だけ分析し、4 位は分析できなかった版（見た 3 本で集計）。"""
    out = b_output()
    rows = [r.model_copy() for r in _B_ROWS[:4]]
    rows[3] = FollowupVideo(rank=4, author=rows[3].author, url=rows[3].url, state="failed")
    digest = b_digest().model_copy(
        update={
            "requested": 5,
            "reserved": 4,
            "watched": 3,
            "watched_ranks": [1, 2, 3],
            "cover_only_ranks": [],
            "failed_ranks": [4],
            "hook_types": [
                LabelCount(label="ビジュアル", count=1),
                LabelCount(label="問題提起", count=1),
                LabelCount(label="POV", count=1),
            ],
            "opening_telop": 3,
            "telop_kw": 3,
            "spoken_kw": 2,
            "cta": 3,
            "narration": 2,
            "pacing": [LabelCount(label="ふつう", count=3)],
        }
    )
    return out.model_copy(update={"videos": rows, "digest": digest})


def c_backfilled_output() -> VideoAlgorithmOutput:
    """4 位の動画が分析できず、6 位を繰り上げて分析した版（1・2・3・5・6 位）。"""
    out = c_output()
    videos = [v for v in out.videos if v.meta.rank != 4]
    videos.append(_c_video(6, "sample_user6", "7400000000000000206", 140_000, 1_260, 61.0))
    return out.model_copy(update={"videos": videos})


def c_one_failed_output() -> VideoAlgorithmOutput:
    """4 位の動画が分析できず、繰り上げる候補も無かった版（5 本中 4 本）。"""
    out = c_output()
    videos = [v.model_copy(deep=True) for v in out.videos]
    videos[3].analysis = None
    videos[3].error = "取得失敗"
    cross = out.cross.model_copy(update={"video_count": 4})
    return out.model_copy(update={"videos": videos, "cross": cross})


def c_none_analyzed_output() -> VideoAlgorithmOutput:
    """1 本も分析できなかった版。"""
    out = c_output()
    videos = [v.model_copy(update={"analysis": None, "error": "取得失敗"}) for v in out.videos]
    cross = CrossAnalysis(keyword=KEYWORD, video_count=0)
    return out.model_copy(update={"videos": videos, "cross": cross})


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
    "CLIENT_ACCOUNT",
    "C_REPORT",
    "C_SLIDES",
    "EVIL_URL",
    "HOSTILE",
    "KEYWORD",
    "a_facts",
    "a_input",
    "a_output",
    "a_posts",
    "a_problem_case",
    "b_output",
    "b_partial_output",
    "c_backfilled_output",
    "c_none_analyzed_output",
    "c_one_failed_output",
    "c_output",
    "hostile_a",
    "hostile_b",
    "hostile_c",
    "tiktok_url",
]
