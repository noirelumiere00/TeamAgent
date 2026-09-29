"""本番（2026-09-28「スパイスカレー 作り方」）と同じ**形**の合成データ。

本物の出力（第三者の公開キャプション・テロップを含む）は repo に入れない。文言・アカウント・
ブランド名は作り話にし、次の「本番で起きた誤りの形」と「本番の値」（仕様 v3 §2-2/2-3 の
このデータでの値）だけを再現する。

本番で起きた誤りの形（テストで直ったことを確かめる）:
- 1 位のキャプションは 720 字を超え、末尾に #PR（先頭 220 字で切ると PR を見落とす）
- KW を含まないテロップに kw_match=True（4 位 21.5 秒のブランド名・5 位 86 秒の料理名）
- テロップに実在しない言い換え「工程」への一致（1 位・19/34/37 秒）
- クライアント名を渡していないのに brand_relation=client（4 位のタイアップ先）
- 文言も秒も無い comment の CTA（4 位）
- 横長のコマ 320×180（5 位）
- meta と Gemini で 1 秒ずれた尺（3 位 60/59・4 位 75/74・5 位 89/90）
- 2 位は分析 AI が「5 つと 4 つで乖離」と自分で書きながら一致度 95
- 3 位のキャプションは全角の「T＆K」（名簿の「T&K」と NFKC で当たる）

再現する本番の値（仕様 v3 の受け入れ確認の値）:
- 尺（meta）中央値 60 秒（46〜89）・1 秒あたりのテロップ 中央値 0.70（0.33〜0.73）・語り #2 #3 #4
- 「スパイスカレー」テロップ 5/5・キャプション 4/5（上位 30 本 26/30）・ハッシュタグ 3/5（23/30）
  ・発話 3/5／「作り方」テロップ 完全一致 0/5・言い換え 1/5（#2）・キャプション 1/5（9/30）
- 分量テロップ #3 #4 #5（4 位は 7 枚・25 秒「大さじ8杯」）・キャプションに分量 #1 #3
  （#1 はキャプションにだけ「大さじ」がある＝「テロップに出す」の仮説に数えない形）
- タイアップ 上位 #1 #4・ボード #1 #4 #12 #16 #20／最良の 1 本 #4（再生・保存率・シェア）
- 外れ値 #3（再生 2.05 万・中央値 27.4 万の 1 割未満）・#5（横長・89 秒・2021 年投稿）
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from typing import Any

from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    CoverRead,
    CrossSynthesis,
    FrameShot,
    Scene,
    VideoMeta,
    VideoVSEOAnalysis,
)

QUERY = "スパイスカレー 作り方"
KW1, KW2 = "スパイスカレー", "作り方"
# 名簿（仕様 T9 の GABAN / S&B|エスビー食品 / ナチュラル専科 と同じ形の作り話）。
CLIENT = "SPICIA"
COMPETITORS = ["T&K|ティーケー食品", "ハーブ専科"]
_JST = timezone(timedelta(hours=9))


def jpeg(width: int, height: int) -> bytes:
    """SOF0 に幅と高さを持つ最小の JPEG（SOI・APP0・SOF0・EOI）。"""
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def jpeg_uri(width: int, height: int) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg(width, height)).decode()


def _epoch(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, 12, tzinfo=_JST).timestamp())


def _id_url(author: str, y: int, m: int, d: int) -> str:
    """動画 ID の上位 32bit が投稿日時（create_time が 0 のとき facts が換算する）。"""
    vid = (_epoch(y, m, d) << 32) | 0x1234
    return f"https://www.tiktok.com/@{author}/video/{vid}"


def _telops(rows: list[tuple[float, str]], position: str, kw: set[float]) -> list[dict[str, Any]]:
    return [
        {"sec": sec, "text": text, "position": position, "kw_match": sec in kw}
        for sec, text in rows
    ]


def _scenes(bounds: list[float]) -> list[dict[str, Any]]:
    return [
        {"start_sec": s, "end_sec": e, "desc": f"場面{i + 1}"}
        for i, (s, e) in enumerate(pairwise(bounds))
    ]


def _km(
    keyword: str,
    layer: str,
    match_type: str,
    secs: list[float],
    surface: str | None,
    *,
    matched: bool = True,
) -> dict[str, Any]:
    return {
        "keyword": keyword,
        "matched": matched,
        "match_type": match_type,
        "layer": layer,
        "appear_sec": secs,
        "surface_text": surface,
    }


def _brand(
    name: str,
    secs: list[float],
    total: float,
    prominence: str,
    *,
    intent: str = "organic_mention",
    relation: str = "neutral_third_party",
    caption: str | None = None,
    source: str = "product_package",
) -> dict[str, Any]:
    return {
        "brand_name": name,
        "detection_source": source,
        "appear_sec": secs,
        "total_screen_time_sec": total,
        "prominence": prominence,
        "is_intentional": intent,
        "co_occurring_caption": caption,
        "brand_relation": relation,
    }


# ── 1 位: 43 枚・中央・語りなし・分量はキャプションだけ・#PR は 720 字より後 ──────────
_T1 = [
    (0.0, "わたしとスパイスカレー"),
    (3.0, "わたしにとって"),
    (4.0, "とくべつな料理"),
    (5.0, "料理の楽しさを"),
    (6.0, "おしえてくれた"),
    (7.0, "もはや先生"),
    (8.0, "会社をやめてまで"),
    (10.0, "料理をしごとにしたいと"),
    (11.0, "思わせた"),
    (12.0, "罪なひと皿"),
    (13.0, "玉ねぎのうまみを引き出す"),
    (14.0, "しっかり煮つめる"),
    (15.0, "甘み・酸味・苦みを足す"),
    (16.0, "スパイスカレーは"),
    (17.0, "料理の基本がつまってる"),
    (18.0, "と、思う"),
    (20.0, "玉ねぎは軽く塩をして"),
    (21.0, "あめ色に"),
    (22.0, "焦げそうなら水を少し"),
    (23.0, "しょうがとにんにく"),
    (24.0, "ターメリック"),
    (26.0, "レッドペッパー"),
    (28.0, "クミン"),
    (30.0, "コリアンダー"),
    (32.0, "ダマにならないように"),
    (34.0, "トマト缶は"),
    (35.0, "水分をしっかり飛ばす"),
    (37.0, "鶏肉とお湯を入れて"),
    (39.0, "弱火で30分"),
    (42.0, "ここで味見"),
    (44.0, "うまいらしい"),
    (47.0, "スパイスカレーは"),
    (48.0, "だしはいらない"),
    (49.0, "玉ねぎとトマトと鶏のうまみ"),
    (50.0, "スパイスの香り"),
    (51.0, "それだけ"),
    (52.0, "市販の調味料を使わず"),
    (53.0, "ぜんぶ自分で作ったという"),
    (54.0, "満足感"),
    (55.0, "これがたまらない"),
    (56.0, "ああ料理たのしい"),
    (58.0, "しまった"),
    (58.0, "まだ飲んでない"),
]
_D1_HEAD = (
    "【スパイス4種で作る本格スパイスカレー】 スパイスカレー。 料理が好きになったきっかけの一皿。"
    "玉ねぎのうまみを引き出し、スパイスで香りを足し、水分を飛ばす。この流れを市販品なしで"
    "作れるのがスパイスカレーの面白さ。 【材料】玉ねぎ 2個・トマト缶 1缶・鶏もも肉 300g・"
    "ターメリック 小さじ1/2・クミン 小さじ1・コリアンダー 大さじ2・バター10g "
)
_D1_FILL = "ポイント: 玉ねぎは焦がさずじっくり、トマトは水分をしっかり飛ばすこと。"
_D1 = (_D1_HEAD + _D1_FILL * 30)[:722] + " #料理記録 #PR "
assert len(_D1) > 720 and _D1.index("#PR") >= 720


# ── 2 位: 32 枚・下段・語りあり・分量なし・CTA はプロフィール誘導（秒は 2 秒早い）──────
_T2 = [
    (0.0, "スパイスカレーを作ってみたい！"),
    (1.0, "でも種類が多くて大変そう…"),
    (3.0, "スパイス好きのぼくが教える"),
    (5.0, "スーパーでそろう5つのスパイス"),
    (7.0, "これだけ押さえれば"),
    (8.0, "本格的でおいしい"),
    (10.0, "スパイスカレーはこれで作れます"),
    (12.0, "クミン 主役の香り"),
    (14.0, "クミン 使う量も多め"),
    (15.0, "クミン カレーらしさの元"),
    (16.0, "クミン 迷ったらこれ"),
    (17.0, "コリアンダー"),
    (18.0, "コリアンダー さわやかな香り"),
    (20.0, "コリアンダー エスニック料理にも"),
    (22.0, "コリアンダー うまみの名脇役"),
    (23.0, "ターメリック"),
    (24.0, "ターメリック 黄色のもと"),
    (26.0, "ターメリック クセがあるので"),
    (27.0, "ターメリック 入れすぎると"),
    (28.0, "ターメリック バランスが崩れる"),
    (29.0, "ターメリック 体にもうれしい"),
    (31.0, "チリペッパー"),
    (32.0, "チリペッパー 辛さの調整役"),
    (33.0, "チリペッパー 量で辛さが変わる"),
    (34.0, "チリペッパー ほかの料理にも"),
    (35.0, "ガラムマサラ"),
    (36.0, "ガラムマサラ 仕上げにひと振り"),
    (37.0, "ガラムマサラ いくつも混ざった"),
    (39.0, "ガラムマサラ 香りの仕上げ役"),
    (40.0, "ガラムマサラ 本格感が一気に出る"),
    (42.0, "このスパイスで作るレシピは"),
    (44.0, "詳しくはプロフィールから見てね"),
]
_D2 = (
    "スパイスカレーを作るなら、まずはこの4つを覚えればOK ✔ クミン ✔ コリアンダー "
    "✔ ターメリック ✔ ガラムマサラ むずかしく考えなくて大丈夫。使い方やレシピはこれから"
    "投稿していくので、まずは「保存」しておいてね！ わからないことはコメントで気軽にどうぞ。"
    " #スパイス好き #スパイスカレー #簡単レシピ #おうちごはん"
)


# ── 3 位: 42 枚・語りあり・分量テロップ 10 枚・再生が極端に少ない ─────────────────────
_T3 = [
    (0.0, "カレールーはもう卒業！"),
    (2.0, "調理時間は30分で"),
    (3.0, "本格スパイスカレー"),
    (5.0, "まず玉ねぎは"),
    (6.0, "くし切りにします"),
    (8.0, "電子レンジで"),
    (9.0, "6分温めます"),
    (11.0, "にんにくは"),
    (12.0, "みじん切りに"),
    (13.0, "油 大さじ3"),
    (15.0, "温めた玉ねぎ1.5個分"),
    (16.0, "きつね色まで炒めます"),
    (18.0, "約6分できつね色"),
    (20.0, "トマト缶 200g"),
    (21.0, "水気がなくなるまで炒めます"),
    (24.0, "ここがいちばん大事"),
    (26.0, "トマトを炒めると酸味が飛び"),
    (28.0, "うまみだけが残り"),
    (29.0, "カレーのコクが増します"),
    (31.0, "このくらいでOK"),
    (33.0, "そして"),
    (33.0, "しょうが"),
    (34.0, "にんにく 小さじ2"),
    (35.0, "カレー粉 小さじ2"),
    (36.0, "1分ほど炒めて"),
    (37.0, "香りを出します"),
    (39.0, "鶏もも肉 200g"),
    (40.0, "水 400cc"),
    (43.0, "塩 小さじ1"),
    (43.0, "こしょう"),
    (44.0, "砂糖 小さじ1"),
    (45.0, "バター 10g"),
    (46.0, "全体になじむように"),
    (47.0, "混ぜたらOK"),
    (48.0, "味見をして"),
    (49.0, "塩が足りなければ"),
    (50.0, "調整してください"),
    (52.0, "それでは"),
    (53.0, "いただきます"),
    (55.0, "意外とかんたん"),
    (56.0, "本格スパイスカレーを"),
    (58.0, "ぜひ一度お試しを"),
]
_D3_HEAD = (
    "【ルーなしで挑戦】30分で仕上がる本格スパイスカレー ---- ＜ポイント＞ "
    "①T＆Kの赤缶で手軽に本格スパイスカレーになる ②玉ねぎはレンジで時短 "
    "作り方は下にまとめました。 【材料】玉ねぎ 1.5個・トマト缶 200g・鶏もも肉 200g・"
    "油 大さじ3・カレー粉 小さじ2・塩 小さじ1 "
)
_D3_FILL = "01.玉ねぎを切ってレンジで温める。02.油で炒める。03.トマトを加えて水気を飛ばす。"
_D3 = (_D3_HEAD + _D3_FILL * 30)[:860] + " #簡単 #本格スパイスカレー #赤缶 #おうちごはん"
assert len(_D3) > 880


# ── 4 位: 28 枚・下段・語りあり・分量テロップ 7 枚・タイアップ・最良の 1 本 ──────────────
_T4 = [
    (0.0, "とにかく痩せたいから"),
    (3.0, "こっそり食べていた"),
    (5.0, "無水スパイスカレー"),
    (7.0, "しっかり絞れてうまい"),
    (9.0, "何回食べても飽きない"),
    (11.0, "トマト大6つ"),
    (13.0, "ナス大3本"),
    (15.0, "えのき1袋"),
    (17.5, "鶏むねひき肉800g"),
    (19.5, "脂質をとことんカット"),
    (21.5, "ハーブ専科カレースパイス"),
    (23.0, "家族みんなで愛用中"),
    (25.0, "大さじ8杯"),
    (26.5, "とても低脂質"),
    (28.5, "醤油大さじ5杯"),
    (33.0, "ソース大さじ5杯"),
    (38.0, "塩小さじ2"),
    (41.0, "きれいな山"),
    (43.0, "ふたをする"),
    (46.0, "中火で15分から20分"),
    (49.0, "ふたが閉まったので開けます"),
    (53.5, "トマトを崩しながら混ぜる"),
    (57.0, "この時点でおいしそう"),
    (60.0, "わくわくする時間"),
    (63.0, "減量中でもごはんが進む"),
    (65.5, "たまらない"),
    (67.5, "ほんとに好き"),
    (71.0, "ぜひ試してみて"),
]
_D4 = (
    "とにかく痩せたい日にこっそり食べる無水スパイスカレー @ハーブ専科  "
    "#PR #スパイスカレー　#ハーブ専科　#無水カレー"
)


# ── 5 位: 29 枚・横長・語りなし・分量テロップ 13 枚・2021 年投稿 ──────────────────────
_T5 = [
    (0.0, "料理人がたどり着いたスパイスカレー"),
    (7.0, "料理人のレシピ『スパイスカレー』"),
    (8.0, "まずベースのトマトスープを作ります"),
    (10.0, "トマト(1個)を角切りにします"),
    (13.0, "塩 小さじ1/2、砂糖 小さじ1/2、10分ほどおきます"),
    (18.0, "ひと口大に切った鶏もも肉(300g)"),
    (21.0, "塩(3g)とこしょう(少々)で下味"),
    (25.0, "オリーブオイル(大さじ1)を入れます"),
    (28.0, "半分に切ったにんにく(1片)を中火で"),
    (31.0, "香りが出たらトマトを入れます"),
    (34.0, "やわらかくなったらフォークでつぶします"),
    (36.0, "水(400ml)を加えて5分ほど煮ます"),
    (39.0, "ミキサーでなめらかにします"),
    (42.0, "ミキサーが無ければこの手順は省いて大丈夫"),
    (44.0, "オリーブオイル(大さじ2)で温めます"),
    (46.0, "クミンシード(小さじ1)で香りを出します"),
    (51.0, "みじん切りの玉ねぎ(1個)をきつね色まで"),
    (55.0, "おろしにんにくとしょうがを入れます"),
    (58.0, "香りを出します(中火)"),
    (61.0, "下味の鶏肉を入れて表面を焼きます"),
    (65.0, "ターメリック・コリアンダー・チリを入れます"),
    (69.0, "粉のスパイスは焦げやすいので水(大さじ2)"),
    (72.0, "スパイスをしっかり炒めます"),
    (75.0, "トマトスープを加えます"),
    (78.0, "弱火で15分煮つめます"),
    (81.0, "15分後"),
    (82.0, "このくらいのとろみで完成"),
    (86.0, "スパイスチキンカレー"),
    (88.0, "お店の味です！"),
]
_D5 = "料理人が作る絶品スパイスチキンカレー#料理人の技 #料理 #簡単レシピ"


def _meta(
    rank: int,
    author: str,
    followers: int,
    plays: int,
    saves: int,
    shares: int,
    duration: float,
    desc: str,
    *,
    posted: tuple[int, int, int],
    create_time: bool = True,
    hashtags: list[str] | None = None,
) -> VideoMeta:
    likes = plays // 40
    comments = plays // 2000
    return VideoMeta(
        rank=rank,
        url=_id_url(author, *posted),
        author=author,
        follower_count=followers,
        desc=desc,
        play_count=plays,
        digg_count=likes,
        comment_count=comments,
        share_count=shares,
        collect_count=saves,
        engagement_rate=round((likes + comments + shares + saves) / plays * 100, 2),
        cover_url=f"https://p16.example/cover{rank}.jpg",
        duration_sec=duration,
        create_time=_epoch(*posted) if create_time else 0,
        hashtags=hashtags or [],
        music_title=f"オリジナル楽曲 - {author}",
    )


def _analysis(**kw: Any) -> VideoVSEOAnalysis:
    return VideoVSEOAnalysis.model_validate(kw)


def top_metas() -> list[VideoMeta]:
    return [
        # 1 位は create_time が 0（動画 ID から換算する）
        _meta(
            1,
            "cook_a",
            52200,
            351200,
            2886,
            475,
            59.0,
            _D1,
            posted=(2022, 11, 14),
            create_time=False,
        ),
        _meta(
            2,
            "spice_b",
            2930,
            223900,
            2119,
            188,
            46.0,
            _D2,
            posted=(2025, 5, 15),
            hashtags=["スパイス好き", "スパイスカレー", "簡単レシピ", "おうちごはん"],
        ),
        _meta(3, "chef_c", 31500, 20500, 130, 19, 60.0, _D3, posted=(2026, 6, 8)),
        _meta(4, "muscle_d", 137700, 859800, 9215, 1347, 75.0, _D4, posted=(2026, 7, 25)),
        _meta(5, "chef_e", 903200, 274500, 1528, 702, 89.0, _D5, posted=(2021, 10, 26)),
    ]


def top_analyses() -> list[VideoVSEOAnalysis]:
    a1 = _analysis(
        duration_sec=59.0,
        hook_type="visual",
        hook_summary="薄暗い台所で具材を炒める画に題名の文字が重なる",
        hook_has_caption=True,
        telop_density="heavy",
        telops=_telops(_T1, "center", {0.0, 16.0, 47.0}),
        brand_detections=[
            _brand("SPICIA", [24.0, 26.0, 28.0, 30.0], 8.0, "prominent"),
            _brand("麦酒X", [7.0, 8.0], 2.0, "background", intent="incidental"),
            _brand("OLIVA", [34.0], 1.0, "prominent"),
        ],
        scenes=_scenes([0, 12, 18, 23, 31, 41, 44, 52, 59]),
        pacing="moderate",
        cta_type=[],
        has_narration=False,
        spoken_keywords=[
            _km(KW1, "narration", "none", [], None, matched=False),
            _km(KW2, "narration", "none", [], None, matched=False),
        ],
        keyword_matches=[
            _km(KW1, "caption", "exact", [0.0], KW1),
            _km(KW1, "telop", "exact", [0.0, 16.0, 47.0], KW1),
            _km(KW2, "caption", "synonym", [0.0], "作れるのが"),
            # 本番の誤り: テロップに「工程」は無い
            _km(KW2, "telop", "synonym", [19.0, 34.0, 37.0], "工程"),
        ],
        message_coherence=95,
        save_share_motivation="材料と手順がまとまっていて後で見返したくなる",
    )
    a2 = _analysis(
        duration_sec=46.0,
        hook_type="problem",
        hook_summary="種類が多くて大変という悩みをテロップで出す",
        hook_has_caption=True,
        telop_density="heavy",
        telops=_telops(_T2, "bottom", {0.0, 10.0}),
        brand_detections=[
            _brand("ティーケー食品", [12.0, 17.0, 23.0, 31.0, 35.0], 30.0, "hero"),
            _brand(
                "スーパーZ",
                [6.0, 12.0, 17.0, 23.0, 31.0, 35.0],
                32.0,
                "background",
                intent="incidental",
                source="storefront",
            ),
        ],
        scenes=_scenes([0, 3, 5, 12, 42, 46]),
        pacing="moderate",
        cta_type=["visit"],
        cta_text="詳しくはプロフィールから見てね",
        cta_sec=42.0,
        has_narration=True,
        spoken_keywords=[
            _km(KW1, "narration", "exact", [0.0, 10.0], KW1),
            _km(KW2, "narration", "synonym", [10.0, 42.0], "作れます、レシピ"),
        ],
        keyword_matches=[
            _km(KW1, "telop", "exact", [0.0, 10.0], KW1),
            _km(KW1, "caption", "exact", [0.0], KW1),
            _km(KW1, "hashtag", "exact", [0.0], f"#{KW1}"),
            # 片ごとに前後 2 秒のテロップに実在する（10 秒「作れます」・42 秒「レシピ」）
            _km(KW2, "telop", "synonym", [10.0, 42.0], "作れます、レシピ"),
            _km(KW2, "caption", "synonym", [0.0], "使い方やレシピ"),
        ],
        message_coherence=95,
        divergence_note="動画では5つ、キャプションでは4つを紹介していて記載数に差がある",
        save_share_motivation="買い物のときに見返せる",
    )
    a3 = _analysis(
        duration_sec=59.0,
        hook_type="shock",
        hook_summary="ルーをやめる宣言のテロップ",
        hook_has_caption=True,
        telop_density="heavy",
        telops=[
            *_telops(_T3[:3], "center", {3.0}),
            *_telops(_T3[3:], "bottom", {56.0}),
        ],
        brand_detections=[
            _brand(
                "T&K",
                [33.0, 34.0, 35.0],
                3.0,
                "incidental",
                source="other",
                caption="※T＆Kの赤缶を使用",
            ),
            _brand("ノンアルY", [55.0, 56.0, 57.0, 58.0], 4.0, "prominent"),
        ],
        scenes=_scenes([0, 4, 12, 19, 32, 38, 41, 47, 51, 59]),
        pacing="moderate",
        cta_type=["visit"],
        cta_text="ぜひ一度お試しを",
        cta_sec=58.0,
        has_narration=True,
        spoken_keywords=[
            _km(KW1, "narration", "partial", [4.0, 57.0], f"本格{KW1}"),
            _km(KW2, "narration", "none", [], None, matched=False),
        ],
        keyword_matches=[
            _km(KW1, "telop", "partial", [3.0, 56.0], f"本格{KW1}"),
            _km(KW1, "caption", "partial", [0.0], f"本格{KW1}"),
            _km(KW2, "caption", "exact", [0.0], KW2),
            _km(KW1, "hashtag", "partial", [0.0], f"本格{KW1}"),
        ],
        message_coherence=95,
        save_share_motivation="分量と手順がキャプションにまとまっている",
    )
    a4 = _analysis(
        duration_sec=74.0,
        hook_type="problem",
        hook_summary="痩せたいという動機をテロップで出す",
        hook_has_caption=True,
        telop_density="medium",
        # 本番の誤り: 21.5 秒のブランド名のテロップに kw_match=True
        telops=_telops(_T4, "bottom", {5.0, 21.5}),
        brand_detections=[
            _brand(
                "ハーブ専科",
                [21.5, 22.0, 23.0],
                3.0,
                "hero",
                intent="likely_sponsored",
                relation="client",  # 本番の誤り: クライアント名を渡していないのに client
                caption="@ハーブ専科",
            )
        ],
        scenes=_scenes([0, 10, 21, 41, 48, 62, 74]),
        pacing="moderate",
        cta_type=["comment"],  # 本番の誤り: 文言も秒も無い
        cta_text=None,
        cta_sec=None,
        has_narration=True,
        spoken_keywords=[
            _km(KW1, "narration", "exact", [5.0, 22.0], KW1),
            _km(KW2, "narration", "none", [], None, matched=False),
        ],
        keyword_matches=[
            _km(KW1, "caption", "exact", [0.0], KW1),
            _km(KW1, "telop", "exact", [5.0, 21.5], KW1),
            _km(KW2, "caption", "none", [], None, matched=False),
        ],
        message_coherence=95,
        save_share_motivation="あとで作るために保存したくなる",
    )
    a5 = _analysis(
        duration_sec=90.0,
        hook_type="visual",
        hook_summary="完成したカレーをスプーンですくう画",
        hook_has_caption=True,
        telop_density="heavy",
        telops=[
            *_telops(_T5[:2], "center", {0.0, 7.0}),
            *_telops(_T5[2:-2], "bottom", set()),
            # 本番の誤り: 86 秒の料理名のテロップに kw_match=True（KW を含まない）
            *_telops(_T5[-2:-1], "center", {86.0}),
            *_telops(_T5[-1:], "bottom", set()),
        ],
        brand_detections=[
            _brand("OLIVE-M", [26.0, 45.0], 4.0, "prominent"),
            _brand("MIXER-B", [41.0], 3.0, "prominent"),
        ],
        scenes=_scenes([0, 6, 8, 43, 75, 85, 90]),
        pacing="moderate",
        cta_type=[],
        has_narration=False,
        spoken_keywords=[],
        keyword_matches=[
            _km(KW1, "telop", "exact", [0.0, 7.0], KW1),
            _km(KW2, "telop", "none", [], None, matched=False),
        ],
        message_coherence=98,
        save_share_motivation="分量と手順が細かく書かれている",
    )
    return [a1, a2, a3, a4, a5]


def _frames(analysis: VideoVSEOAnalysis, width: int, height: int) -> list[FrameShot]:
    uri = jpeg_uri(width, height)
    secs = [0.8] + [round((sc.start_sec + sc.end_sec) / 2, 1) for sc in analysis.scenes[1:6]]
    return [FrameShot(sec=s, caption="", data_uri=uri) for s in secs]


# 本番のコマの秒（pick_timecodes の 6 枚＝前半に偏る。#2 は 46 秒の動画で 6 枚とも 12 秒以内）。
PROD_FRAME_SECS: dict[int, list[float]] = {
    1: [0.8, 6.0, 16.0, 24.0, 34.0, 58.5],
    2: [0.8, 4.0, 6.0, 8.5, 10.0, 12.0],
    3: [0.8, 3.0, 8.0, 15.5, 33.0, 55.0],
    4: [0.8, 5.0, 15.5, 21.5, 31.0, 44.5],
    5: [0.8, 3.0, 7.0, 26.0, 41.0, 59.0],
}
# 本番の #1 の場面（場面と場面のあいだに 1 秒の隙間がある）。
PROD_GAPPED_SCENES_1: list[tuple[float, float]] = [
    (0, 12),
    (13, 18),
    (19, 23),
    (24, 31),
    (32, 41),
    (42, 44),
    (45, 52),
    (53, 59),
]


def prod_videos(frames: str = "scene") -> list[AnalyzedVideo]:
    """上位 5 本（v2 の出力＝場面に役割が無い）。コマは 1〜4 位が縦長・5 位が横長。

    frames="scene" はコマが場面の中央に並ぶ形（場面ごとのコマ）。frames="prod" は本番と同じ
    偏り（PROD_FRAME_SECS）と、#1 の隙間のある場面（PROD_GAPPED_SCENES_1）。
    """
    sizes = [(320, 584), (320, 568), (320, 568), (320, 568), (320, 180)]
    analyses = top_analyses()
    if frames == "prod":
        analyses[0].scenes = [
            Scene(start_sec=s, end_sec=e, desc=f"場面{i + 1}")
            for i, (s, e) in enumerate(PROD_GAPPED_SCENES_1)
        ]
    out = []
    for meta, a, size in zip(top_metas(), analyses, sizes, strict=True):
        if frames == "prod":
            uri = jpeg_uri(*size)
            shots = [FrameShot(sec=s, caption="", data_uri=uri) for s in PROD_FRAME_SECS[meta.rank]]
        else:
            shots = _frames(a, *size)
        out.append(
            AnalyzedVideo(
                meta=meta,
                analysis=a,
                frames=shots,
                cost_usd=0.08,
                model_id="gemini-3.5-flash",
            )
        )
    return out


# ── 上位ボード 30 本（6〜30 位はメタだけ）──────────────────────────────────────
# 6〜30 位: キャプションに「スパイスカレー」が無い＝10・14・19 位、ハッシュタグに無い＝それに
# 8・29 位を足したもの。キャプションに「作り方」＝7・10・12・13・15・17・18・19 位。
# タイアップ＝12・16・20 位。作り手の重なり＝spice_b（2・11・24・28）・recipe_site（7・10・12・29）
# ・muscle_d（4・16・20）・bro_curry（21・26）。「無水」＝14・16・20 位（と 4 位）。
_NO_KW_CAPTION = {10, 14, 19}
_NO_KW_TAG = {8, 10, 14, 19, 29}
_HOW_TO = {7, 10, 12, 13, 15, 17, 18, 19}
_PR = {12, 16, 20}
_MUSUI = {14, 16, 20}
_AUTHORS = {
    11: "spice_b",
    24: "spice_b",
    28: "spice_b",
    7: "recipe_site",
    10: "recipe_site",
    12: "recipe_site",
    29: "recipe_site",
    16: "muscle_d",
    20: "muscle_d",
    21: "bro_curry",
    26: "bro_curry",
}
# (再生, 保存, 投稿年) 6〜30 位。保存率の上位は 11 位 2.38%・19 位 1.79%・10 位 1.68%。
_BOARD_STATS: dict[int, tuple[int, int, int]] = {
    6: (69400, 259, 2023),
    7: (27900, 168, 2024),
    8: (2051, 7, 2026),
    9: (825800, 2931, 2022),
    10: (46200, 776, 2025),
    11: (43800, 1042, 2025),
    12: (417600, 4810, 2023),
    13: (45100, 106, 2024),
    14: (282800, 801, 2025),
    15: (207500, 2561, 2026),
    16: (95100, 340, 2025),
    17: (71400, 561, 2025),
    18: (13200, 101, 2026),
    19: (33100, 592, 2026),
    20: (31400, 121, 2026),
    21: (63000, 606, 2025),
    22: (26800, 169, 2023),
    23: (25600, 145, 2025),
    24: (9666, 63, 2026),
    25: (54100, 199, 2023),
    26: (43600, 637, 2025),
    27: (90400, 455, 2026),
    28: (86400, 478, 2025),
    29: (80100, 235, 2025),
    30: (4224, 10, 2026),
}


def _board_desc(rank: int) -> str:
    parts = [f"おうちで試した{rank}番目のカレー記録"]
    if rank in _MUSUI:
        parts.append("無水で煮込む")
    if rank in _HOW_TO:
        parts.append("チキンカレーの作り方")
    if rank not in _NO_KW_CAPTION and rank in _NO_KW_TAG:
        parts.append("はじめてのスパイスカレー")
    tags = ["#カレー"]
    if rank in _PR:
        tags.append("#PR")
    if rank not in _NO_KW_TAG:
        tags.append(f"#{KW1}")
    return " ".join(parts) + " " + " ".join(tags)


def prod_board() -> list[VideoMeta]:
    board = list(top_metas())
    for rank in range(6, 31):
        plays, saves, year = _BOARD_STATS[rank]
        author = _AUTHORS.get(rank, f"user{rank:02d}")
        board.append(
            VideoMeta(
                rank=rank,
                url=_id_url(author, year, 3, 1),
                author=author,
                follower_count=1000 + rank,
                desc=_board_desc(rank),
                play_count=plays,
                collect_count=saves,
                share_count=plays // 200,
                duration_sec=60.0,
                create_time=_epoch(year, 3, 1),
            )
        )
    return board


def prod_synthesis() -> CrossSynthesis:
    """本番の横断シンセシスと同じ誤りの形（ρ タグ・〔上位 2/5〕・御社・来店/保存・4〜5 種の食い違い）。

    文言は作り話。後段（synthesis v3・描画）の検査がこの形を落とせるかを見るために使う。
    """
    return CrossSynthesis.model_validate(
        {
            "headline": "スパイスカレー作り方面はスパイス4選と30分調理の手軽さで勝つ",
            "strategy": "スパイスを4種類に絞り、手軽さを伝えます。玉ねぎを炒める場面をアップで"
            "見せて離脱を防ぎます。〔上位 2/5〕",
            "creative_brief": [
                "冒頭でスプーンで引き上げる完成映像を見せる〔上位 2/5〕",
                "悩む表情のアップから始める",
                "尺は46-59秒に収める〔尺46-59秒, n=5〕",
            ],
            "posting_design": "キャプションの最後で保存を促す。〔キャプション 4/5〕",
            "client_pitch": "御社の調味料を使い、4つのスパイスだけで作る時短カレーを投稿します。",
            "common_concepts": [
                {
                    "concept": "スパイス4種選定",
                    "gist": "必須スパイスを4種類に限定する。〔上位 2/5、ρ=−0.30, n=5〕",
                    "videos": [1, 2],
                    "prevalence": "2/5",
                },
                {
                    "concept": "30分調理",
                    "gist": "30分で作れる手軽さを出す。",
                    "videos": [2, 3],
                    "prevalence": "2/5",
                },
            ],
            "angle_clusters": [
                {
                    "angle": "problem_solving",
                    "label_jp": "悩み解決",
                    "videos": [1, 2, 3, 4],
                    "why_works": "苦手意識を少ないスパイスで解決するため。",
                }
            ],
            "shared_funnel": {
                "pattern": "冒頭で完成品や悩みを見せ、分量を解説したあと保存を促す。",
                "cta_consensus": ["visit", "save"],
                "save_logic": "分量を見返すために保存される。〔保存率0.8%以上, n=5〕",
            },
            "differentiators": [{"rank": 1, "edge": "薄暗い台所の世界観。"}],
            "win_hypotheses": [
                {
                    "hypothesis": "紹介するスパイスを4種類から5種類に厳選すると保存率が上がる。"
                    "〔ρ=−0.30, n=5〕",
                    "supported_by": [2, 3],
                    "confidence": "低",
                    "counter_example": "#4（健康訴求）",
                    "so_what": "構成案では主役4種に絞る指示を出します。",
                },
                {
                    "hypothesis": "カレールー卒業をフックにすると離脱を防げる。〔ρ=−0.80, n=5〕",
                    "supported_by": [2, 3],
                    "confidence": "低",
                    "so_what": "既存ルーとの比較をフックにする。",
                },
            ],
            "caveat": "n=5は統計的に極小です。",
        }
    )


# ── サムネ（一覧の表紙）の読み取り（作り話。第三者の文字は入れない）──────────────
# 本番の形: 上位 4 本は表紙に文字（うち 4 本に「スパイスカレー」）・#5 は文字なし。
# #3 の商品名「赤缶」はハッシュタグで照合できる・#4 の「ハーブ専科」はキャプションで照合できる
# （名簿では競合）。#1 と #4 は実写の顔。枠は 0〜1000 の [上, 左, 下, 右]。
_COVER_READS: dict[int, dict[str, Any]] = {
    1: {
        "elements": ["result", "person"],
        "subject_note": "湯気の立つ皿と手元",
        "texts": [
            {"text": "わたしとスパイスカレー", "box_2d": [80, 60, 220, 940], "style": ["outline"]}
        ],
        "face": {
            "kind": "real",
            "expression": "smile",
            "gaze": "camera",
            "box_2d": [300, 300, 500, 600],
        },
        "closeup": True,
        "sizzle": ["steam"],
        "product": "none",
        "clutter": "simple",
        "legibility": "good",
        "appeals": ["reaction"],
    },
    2: {
        "elements": ["result", "text_main"],
        "subject_note": "皿に盛ったカレー",
        "texts": [
            {"text": "スパイスカレー\n5つで作れる", "box_2d": [100, 80, 330, 920], "style": ["box"]}
        ],
        "face": {"kind": "none"},
        "closeup": True,
        "sizzle": ["gloss"],
        "product": "none",
        "clutter": "simple",
        "legibility": "good",
        "appeals": ["how_to", "benefit"],
    },
    3: {
        "elements": ["result"],
        "subject_note": "鍋のカレー",
        "texts": [
            {
                "text": "30分で本格\nスパイスカレー",
                "box_2d": [650, 50, 900, 950],
                "style": ["outline"],
            }
        ],
        "face": {"kind": "none"},
        "closeup": True,
        "sizzle": ["steam", "gloss"],
        "product": "visible",
        "brand_text": ["赤缶"],
        "clutter": "moderate",
        "legibility": "good",
        "appeals": ["time_saving", "benefit"],
    },
    4: {
        "elements": ["person", "result", "product"],
        "subject_note": "カレーを食べる人",
        "texts": [
            {
                "text": "とにかく痩せたい\n無水スパイスカレー",
                "box_2d": [60, 40, 300, 960],
                "style": ["outline"],
            }
        ],
        "face": {
            "kind": "real",
            "expression": "surprise",
            "gaze": "camera",
            "box_2d": [350, 250, 600, 700],
        },
        "action": "eating",
        "closeup": False,
        "sizzle": [],
        "product": "hero",
        "brand_text": ["ハーブ専科"],
        "clutter": "moderate",
        "legibility": "ok",
        "appeals": ["target", "benefit"],
    },
    5: {
        "elements": ["result", "scene"],
        "subject_note": "店の皿のカレー",
        "texts": [],
        "face": {"kind": "none"},
        "closeup": False,
        "sizzle": ["steam"],
        "product": "none",
        "clutter": "simple",
        "legibility": "none",
        "appeals": [],
    },
}


def prod_cover_read(rank: int, **update: Any) -> CoverRead:
    """上位 1〜5 位の表紙の読み取り（status ok・幅 540・media の取得）。"""
    read = CoverRead.model_validate(_COVER_READS[rank])
    return read.model_copy(
        update={
            "rank": rank,
            "group": "top",
            "status": "ok",
            "version": "v1-test",
            "via": "media",
            "img_w": 540,
            "img_h": 960,
            **update,
        }
    )


def rest_cover_read(rank: int, *, kw: bool, **update: Any) -> CoverRead:
    """6〜30 位の表紙の読み取り（作り話・kw=True で「スパイスカレー」の文字）。"""
    texts = [{"text": "スパイスカレー" if kw else "今日のごはん", "box_2d": [700, 100, 800, 900]}]
    read = CoverRead.model_validate(
        {
            "elements": ["result"],
            "subject_note": "カレーの皿",
            "texts": texts,
            "face": {"kind": "none"},
            "closeup": False,
            "sizzle": [],
            "product": "none",
            "clutter": "moderate",
            "legibility": "ok",
            "appeals": [],
        }
    )
    return read.model_copy(
        update={
            "rank": rank,
            "group": "rest",
            "status": "ok",
            "version": "v1-test",
            "via": "media",
            "img_w": 540,
            "img_h": 960,
            **update,
        }
    )


def prod_board_with_covers(*, rest_kw: set[int] | None = None) -> list[VideoMeta]:
    """上位ボードに表紙の読み取りを付けたもの。rest_kw を渡すと 6〜30 位も読んだ形（board）。"""
    board = prod_board()
    for m in board:
        if m.rank <= 5:
            m.cover_read = prod_cover_read(m.rank)
        elif rest_kw is not None:
            m.cover_read = rest_cover_read(m.rank, kw=m.rank in rest_kw)
    return board


def prod_videos_with_covers() -> list[AnalyzedVideo]:
    """上位 5 本（表紙の画像 540×960 を埋め込んだもの・出どころは表紙）。"""
    videos = prod_videos()
    for v in videos:
        v.cover_data_uri = jpeg_uri(540, 960)
        v.cover_source = "cover"
    return videos


__all__ = [
    "CLIENT",
    "COMPETITORS",
    "KW1",
    "KW2",
    "PROD_FRAME_SECS",
    "PROD_GAPPED_SCENES_1",
    "QUERY",
    "jpeg",
    "jpeg_uri",
    "prod_board",
    "prod_board_with_covers",
    "prod_cover_read",
    "prod_synthesis",
    "prod_videos",
    "prod_videos_with_covers",
    "rest_cover_read",
    "top_analyses",
    "top_metas",
]
