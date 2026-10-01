"""統合 LLM（v3）のサムネ（一覧の表紙）の指示 cover_directives（R17）の検査。

- 表紙の引用（refs の on="cover"）は、表紙の文字か主役の説明（AI の読み取り）だけで照合し、
  キャプションへは逃がさない。on を書き忘れても表紙として照合する。
- directives・avoid に表紙の引用や kind「表紙」を書いても、表紙の検査の抜け道にしない。
- コードだけの欄（origin・tier・ranks）は LLM の JSON から捨てる。掃除（R1/R3/R5/R11/R12）と
  数字の照合と欄の間の食い違い（R6）の検査を cover_directives にも掛ける。
- 表紙の文字の数字は、本文全体の照合（all）に入れない（表紙の節だけの照合で使う）。
壊し方（→ 赤）は各テストの docstring。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from teamagent.skills.video_algorithm.evidence import TIER_MAJORITY, Roster
from teamagent.skills.video_algorithm.schema import CrossSynthesis, SynthRef
from teamagent.skills.video_algorithm.synthesis import parse_synthesis_v3, synthesize
from teamagent.skills.video_algorithm.synthesis_checks import (
    CheckLog,
    conflict_fields,
    cover_directive_line,
    deny_hit,
    evidence_text,
    finalize,
)
from teamagent.skills.video_algorithm.synthesis_input import (
    COVER_SECTION_HEAD,
    SynthesisContext,
    build_grounders,
    render_prompt,
)
from tests.skills.video_algorithm.prod_shape import (
    CLIENT,
    COMPETITORS,
    QUERY,
    prod_board,
    prod_board_with_covers,
    prod_videos,
)
from tests.skills.video_algorithm.test_synthesis_v3 import _gemini

ROSTER = Roster.of(CLIENT, COMPETITORS)


def _ctx(
    *, covers: bool = True, avoid: list[str] | None = None, board: list[Any] | None = None
) -> SynthesisContext:
    rows = board if board is not None else prod_board_with_covers() if covers else prod_board()
    return SynthesisContext.build(
        prod_videos(), QUERY, board=rows, roster=ROSTER, avoid_terms=avoid
    )


def _cd(text: str, *refs: dict[str, Any], feature: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"text": text, "refs": list(refs)}
    if feature:
        out["feature"] = feature
    return out


def _final(payload: dict[str, Any], ctx: SynthesisContext | None = None) -> CrossSynthesis:
    return finalize(CrossSynthesis.model_validate(payload), ctx or _ctx())


def _llm(syn: CrossSynthesis) -> list[Any]:
    return [d for d in syn.cover_directives if d.origin == "llm"]


def test_cover_quotes_pass_only_on_cover_text() -> None:
    """表紙の文字の引用は合格し、キャプションにしか無い引用は不合格（キャプションへ逃がさない）。

    壊し方: 表紙の照合で見つからないときキャプション（evidence.verify_ref）へ逃がす → 赤。
    """
    syn = _final(
        {
            "cover_directives": [
                _cd(
                    "表紙に店名を大きく入れる",
                    {"rank": 4, "on": "cover", "quote": "無水スパイスカレー"},
                ),
                # #4 のキャプションには「こっそり食べる」があるが、表紙の文字には無い
                _cd("表紙に秘密感を出す", {"rank": 4, "on": "cover", "quote": "こっそり食べる"}),
                # on を書き忘れても表紙として照合する（D-31）
                _cd("湯気を見せる", {"rank": 1, "quote": "湯気の立つ皿と手元"}),
            ]
        }
    )
    llm = _llm(syn)
    assert [d.text for d in llm] == ["表紙に店名を大きく入れる", "湯気を見せる"]
    assert llm[0].refs[0].source == "cover_text" and llm[0].refs[0].on == "cover"
    assert llm[1].refs[0].source == "cover_note"
    assert all(d.kind == "表紙" for d in syn.cover_directives)


def test_directives_cannot_quote_the_cover_or_use_the_cover_kind() -> None:
    """表紙の引用は directives では不合格。kind「表紙」は directives では空にする。

    壊し方: verify_refs の on="cover" の拒否を外す → 表紙の文字がキャプションとして通り赤。
    """
    seen: list[tuple[str, str]] = []
    log = CheckLog(sink=lambda f, r: seen.append((f, r)))
    syn = finalize(
        CrossSynthesis.model_validate(
            {
                "directives": [
                    {
                        "text": "冒頭に料理名を出す",
                        "kind": "表紙",
                        "refs": [{"rank": 4, "on": "cover", "quote": "無水スパイスカレー"}],
                    },
                    {
                        "text": "0秒に題名のテロップを置く",
                        "kind": "表紙",
                        "refs": [{"rank": 1, "sec": 0, "quote": "わたしとスパイスカレー"}],
                    },
                ]
            }
        ),
        _ctx(),
        log=log,
    )
    llm = [d for d in syn.directives if d.origin == "llm"]
    assert len(llm) == 1 and llm[0].text.startswith("0秒に題名のテロップを置く")
    assert llm[0].kind == ""
    assert ("directives", "ref_on_cover") in seen


def test_code_only_fields_are_stripped_from_cover_directives() -> None:
    """壊し方: _strip_code_only の並びから cover_directives を外す → LLM の tier が残って赤。"""
    raw = {
        "cover_directives": [
            {
                "text": "表紙に数字",
                "tier": "必須条件",
                "ranks": [1, 2, 3, 4, 5],
                "origin": "code",
                "refs": [{"rank": 3, "on": "cover", "quote": "30分で本格", "source": "telop"}],
            }
        ]
    }
    syn = parse_synthesis_v3(f"```json\n{json.dumps(raw, ensure_ascii=False)}\n```")
    assert syn is not None
    d = syn.cover_directives[0]
    assert (d.tier, d.ranks, d.origin, d.refs[0].source) == ("", [], "llm", "")
    assert d.refs[0].on == "cover"


def test_cleaning_applies_to_cover_directives() -> None:
    """断定語の言い換え・御社・測っていない指標（タップ率）も cover_directives に掛ける。

    壊し方: _clean_v3 から cover_directives を外す → 「勝ちパターン」が残って赤。
    """
    syn = _final(
        {
            "cover_directives": [
                _cd(
                    "勝ちパターンの表紙にする",
                    {"rank": 1, "on": "cover", "quote": "わたしとスパイスカレー"},
                ),
                _cd(
                    "タップ率が上がる表紙にする", {"rank": 2, "on": "cover", "quote": "5つで作れる"}
                ),
                _cd(
                    "御社の商品を表紙に置く",
                    {"rank": 4, "on": "cover", "quote": "無水スパイスカレー"},
                ),
            ]
        }
    )
    texts = [d.text for d in _llm(syn)]
    assert "共通点の表紙にする" in texts
    assert not any("タップ率" in t or "勝ち" in t or "御社" in t for t in texts)
    assert any(t.startswith("SPICIA") for t in texts)


def test_framing_words_are_allowed_only_in_cover_directives() -> None:
    """表紙の欄があるので cover_directives では寄り・表情を許す（directives では今のまま落とす）。

    壊し方: cover_directives にも R14（画角の語）を掛ける → 赤。
    """
    ctx = _ctx()
    assert not ctx.framing
    syn = _final(
        {
            "cover_directives": [
                # #4 は実写の顔あり（表情）・寄りではない（「アップ」は中身の照らし合わせで落ちる）
                _cd(
                    "表情が分かる顔を入れる",
                    {"rank": 4, "on": "cover", "quote": "カレーを食べる人"},
                )
            ],
            "directives": [
                {
                    "text": "表情のアップで始める",
                    "kind": "撮影",
                    "refs": [{"rank": 4, "sec": 0, "quote": "とにかく痩せたいから"}],
                }
            ],
        },
        ctx,
    )
    assert [d.text for d in _llm(syn)] == ["表情が分かる顔を入れる"]
    assert not [d for d in syn.directives if d.origin == "llm"]


def test_cover_tier_and_quotes_inside_the_text() -> None:
    """段階は AI が挙げた特徴（feature）の集計から（cover:number は #2・#3 の 2/5＝事例）。"""
    syn = _final(
        {
            "cover_directives": [
                _cd(
                    "表紙に「5つで作れる」のような数を入れる",
                    {"rank": 2, "on": "cover", "quote": "5つで作れる"},
                    {"rank": 3, "on": "cover", "quote": "30分で本格"},
                    feature="cover:number",
                ),
                _cd(
                    "表紙に「3分で完成」と入れる",  # どの表紙にも無い引用 → 文ごと落とす
                    {"rank": 3, "on": "cover", "quote": "30分で本格"},
                    feature="cover:number",
                ),
            ]
        }
    )
    llm = _llm(syn)
    assert len(llm) == 1 and llm[0].ranks == [2, 3]
    assert llm[0].tier == "事例" and llm[0].feature == "cover:number"


def test_no_cover_reads_means_no_cover_directives() -> None:
    syn = _final(
        {
            "cover_directives": [
                _cd("表紙に数字", {"rank": 3, "on": "cover", "quote": "30分で本格"})
            ]
        },
        _ctx(covers=False),
    )
    assert syn.cover_directives == []
    assert "cover_directives" not in syn.model_dump()


def test_code_cover_directives_come_first_and_finalize_is_idempotent() -> None:
    once = _final(
        {
            "cover_directives": [
                _cd("湯気を見せる", {"rank": 1, "on": "cover", "quote": "湯気の立つ皿と手元"})
            ]
        }
    )
    codes = [d for d in once.cover_directives if d.origin == "code"]
    assert codes and once.cover_directives[: len(codes)] == codes
    assert codes[0].tier == TIER_MAJORITY
    twice = finalize(once, _ctx())
    assert twice.cover_directives == once.cover_directives


def test_conflict_fields_include_cover_directives() -> None:
    """壊し方: conflict_fields から cover_directives を外す → R6 の比べる欄に出ず赤。"""
    syn = CrossSynthesis.model_validate(
        {"cover_directives": [_cd("スパイスは4種類にする", {"rank": 1, "quote": "x"})]}
    )
    assert ("cover_directives[0].text", "スパイスは4種類にする") in conflict_fields(syn)


def test_evidence_text_labels_cover_quotes_as_ai_reading() -> None:
    ref = SynthRef(rank=4, quote="無水スパイスカレー", on="cover", source="cover_text")
    assert evidence_text(ref) == "#4 表紙の文字（AI読み取り）「無水スパイスカレー」"


def test_cover_numbers_do_not_widen_the_whole_prompt_grounder() -> None:
    """表紙の文字の数字（「5つ」）は本文全体の照合に入れない（表紙の節の照合にだけ使う）。

    壊し方: all の照合を本文全体（表紙の節を含む）から作る → 「11.5%」が all で通って赤。
    """
    ctx = _ctx()
    prompt = render_prompt(ctx)
    assert COVER_SECTION_HEAD in prompt
    grounders = build_grounders(ctx, prompt)
    assert grounders.cover is not None
    # 表紙の大きい文字 1 行の高さ（11.5%）は表紙の節にしか無い数字
    assert grounders.cover.reason("1行の高さは11.5%") is None
    assert grounders.all.reason("1行の高さは11.5%") is not None


@pytest.mark.parametrize("mode", ["enforce", "shadow"])
def test_cover_directive_numbers_are_grounded_by_the_cover_block(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """壊し方: _ground_v3 から cover_directives を外す → enforce でも入力に無い数字が残って赤。"""
    monkeypatch.setenv("GROUNDING_MODE_VIDEO_ALGORITHM", mode)
    payload = {
        "cover_directives": [
            _cd(
                "文字の高さは11.5%にする",
                {"rank": 1, "on": "cover", "quote": "わたしとスパイスカレー"},
            ),
            _cd("文字の高さは37.5%にする", {"rank": 2, "on": "cover", "quote": "5つで作れる"}),
        ]
    }
    syn, _ = synthesize(
        _gemini(payload),
        prod_videos(),
        QUERY,
        request_id="r-cover",
        board=prod_board_with_covers(),
        roster=ROSTER,
    )
    assert syn is not None
    texts = [d.text for d in _llm(syn)]
    assert "文字の高さは11.5%にする" in texts
    assert ("文字の高さは37.5%にする" in texts) is (mode == "shadow")


def test_prompt_marks_missing_cover_analysis() -> None:
    assert "サムネ（一覧の表紙）: 分析なし" in render_prompt(_ctx(covers=False))
    prompt = render_prompt(_ctx())
    assert "表紙の特徴の表" in prompt and "6〜30位の表紙はまだ読んでいない" in prompt
    assert "R1〜R17" in prompt


# ── AI の指示の段階は特徴の集計から・中身と根拠の照らし合わせ・効果と比べる文（R17）──────


def _lines(syn: CrossSynthesis, ctx: SynthesisContext) -> list[str]:
    return [cover_directive_line(d, ctx.cover) for d in _llm(syn)]


def test_llm_tier_comes_from_the_named_feature_not_from_the_quote_count() -> None:
    """段階・本数・順位は feature の集計から。引用はその特徴を持つ表紙に限る（#4 は湯気なし）。

    壊し方: 段階を引用した本数から付ける（以前の形）→ 事例（#1・#4）になり赤。
    """
    ctx = _ctx()
    syn = _final(
        {
            "cover_directives": [
                _cd(
                    "表紙で湯気を見せる",
                    {"rank": 1, "on": "cover", "quote": "湯気の立つ皿と手元"},
                    {"rank": 4, "on": "cover", "quote": "カレーを食べる人"},
                    feature="cover:sizzle",
                )
            ]
        },
        ctx,
    )
    (d,) = _llm(syn)
    assert (d.tier, d.ranks, d.feature) == (TIER_MAJORITY, [1, 2, 3, 5], "cover:sizzle")
    assert [r.rank for r in d.refs] == [1]
    (line,) = _lines(syn, ctx)
    assert "AI の提案・特徴「湯気・照り・断面・質感などの見せ場（AI判定）」" in line
    assert "多数派 4/5（#1・#2・#3・#5）" in line


def test_fabricated_face_directive_is_dropped() -> None:
    """顔の無い表紙（#2・#3・#5）を引用した「顔をカメラ目線で入れる」は、特徴があっても無くても落とす。

    09-29 反証: 以前は引用の本数だけで「多数派（#2・#3・#5）」の札が付いた。
    壊し方: 中身の照らし合わせ（unsupported_concept）を外す → 残って赤。
    """
    seen: list[tuple[str, str]] = []
    refs = [
        {"rank": 2, "on": "cover", "quote": "皿に盛ったカレー"},
        {"rank": 3, "on": "cover", "quote": "鍋のカレー"},
        {"rank": 5, "on": "cover", "quote": "店の皿のカレー"},
    ]
    syn = finalize(
        CrossSynthesis.model_validate(
            {
                "cover_directives": [
                    _cd("表紙は実写の人の顔をカメラ目線で大きく入れる", *refs),
                    _cd(
                        "表紙は実写の人の顔をカメラ目線で大きく入れる",
                        *refs,
                        feature="cover:el:result",
                    ),
                ]
            }
        ),
        _ctx(),
        log=CheckLog(sink=lambda f, r: seen.append((f, r))),
    )
    assert _llm(syn) == []
    assert ("cover_directives", "concept_unsupported:cover:gaze_camera") in seen


def test_feature_refs_must_have_the_feature() -> None:
    """feature の特徴を持たない表紙だけを引用した指示は落とす（#2 は実写の顔なし）。

    壊し方: 引用をその特徴の順位に絞らない → 残って赤。
    """
    syn = _final(
        {
            "cover_directives": [
                _cd(
                    "表紙に人を大きく入れる",
                    {"rank": 2, "on": "cover", "quote": "皿に盛ったカレー"},
                    feature="cover:face",
                )
            ]
        }
    )
    assert _llm(syn) == []


def test_directives_without_a_feature_are_marked_uncounted() -> None:
    """feature が無い（表に無い id も）AI の提案は段階を付けず「AI の提案（未集計）」と出す。

    壊し方: 特徴の無い指示に引用の本数から段階を付ける → 「事例」が付いて赤。
    """
    ctx = _ctx()
    syn = _final(
        {
            "cover_directives": [
                _cd("表紙の文字は短くまとめる", {"rank": 2, "on": "cover", "quote": "5つで作れる"}),
                _cd(
                    "表紙の文字を太くする",
                    {"rank": 3, "on": "cover", "quote": "30分で本格"},
                    feature="cover:bold",
                ),
            ]
        },
        ctx,
    )
    llm = _llm(syn)
    assert [(d.tier, d.feature) for d in llm] == [("", ""), ("", "")]
    for line in _lines(syn, ctx):
        assert "AI の提案（未集計）" in line
        assert not any(t in line for t in ("多数派", "事例", "必須条件"))
    code = [cover_directive_line(d, ctx.cover) for d in syn.cover_directives if d.origin == "code"]
    assert code and all("〔コードの集計｜" in x for x in code)


@pytest.mark.parametrize(
    "text",
    [
        "湯気を見せる表紙はタップされる",
        "湯気で押される表紙にする",
        "湯気の表紙がクリックを集める",
        "湯気の表紙が目を引く",
        "湯気の表紙は選ばれやすい",
    ],
)
def test_effect_claims_are_dropped_from_cover_directives(text: str) -> None:
    """効果（タップされる・押される・選ばれる・目を引く）を言い切る文は落とす（R17 をコードで）。

    壊し方: _cover_claims_ok の効果の語の検査を外す → 「押される」「目を引く」が残って赤。
    """
    ref = {"rank": 1, "on": "cover", "quote": "湯気の立つ皿と手元"}
    syn = _final({"cover_directives": [_cd(text, ref, feature="cover:sizzle")]})
    assert _llm(syn) == []
    kept = _final({"cover_directives": [_cd("表紙で湯気を見せる", ref, feature="cover:sizzle")]})
    assert [d.text for d in _llm(kept)] == ["表紙で湯気を見せる"]


def test_tap_and_click_effects_are_denied_in_every_field() -> None:
    """タップ・クリックの効果の言い方は v3 のどの欄でも落とす（動画の「惹きつける」は今のまま）。"""
    assert deny_hit("検索一覧でタップされる表紙") is not None
    assert deny_hit("クリックを集める冒頭") is not None
    assert deny_hit("冒頭の3秒で惹きつける") is None


def test_rest_comparisons_are_dropped_when_the_rest_was_not_read() -> None:
    """6〜30 位を読んでいない（top）ときは、ほかと比べる文を落とす。比べていない旨の文は残す。

    壊し方: 比べる文の検査を外す → 「6〜30位より湯気を見せている」が残って赤。
    """
    ref = {"rank": 3, "on": "cover", "quote": "鍋のカレー"}
    syn = _final(
        {
            "cover_directives": [
                _cd(
                    "上位の表紙は6〜30位より湯気を見せている。表紙で湯気を見せる",
                    ref,
                    feature="cover:sizzle",
                ),
                _cd(
                    "6〜30位とは比べていないが、上位は表紙で照りを見せる",
                    ref,
                    feature="cover:sizzle",
                ),
            ]
        }
    )
    assert [d.text for d in _llm(syn)] == [
        "表紙で湯気を見せる",
        "6〜30位とは比べていないが、上位は表紙で照りを見せる",
    ]
    board = _final(
        {
            "cover_directives": [
                _cd("上位の表紙は6〜30位より湯気を見せている", ref, feature="cover:sizzle")
            ]
        },
        _ctx(board=prod_board_with_covers(rest_kw={6, 7})),
    )
    assert [d.text for d in _llm(board)] == ["上位の表紙は6〜30位より湯気を見せている"]


def test_llm_directive_on_a_feature_more_common_in_the_rest_is_dropped() -> None:
    """ほかが多い向きの差の印の特徴（上位 1/5・ほか 25/25 の実写の顔）を勧める指示は落とす。"""
    from tests.skills.video_algorithm.test_cover_facts import _faces_in_rest

    seen: list[tuple[str, str]] = []
    syn = finalize(
        CrossSynthesis.model_validate(
            {
                "cover_directives": [
                    _cd(
                        "表紙に実写の人を入れる",
                        {"rank": 1, "on": "cover", "quote": "湯気の立つ皿と手元"},
                        feature="cover:face",
                    )
                ]
            }
        ),
        _ctx(board=_faces_in_rest()),
        log=CheckLog(sink=lambda f, r: seen.append((f, r))),
    )
    assert _llm(syn) == []
    assert ("cover_directives", "feature_more_in_rest") in seen
