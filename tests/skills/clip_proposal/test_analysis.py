"""解析パイプラインの安全装置（捏造・untrusted・秒区間・費用）。

フェイクは本番の失敗モードを再現する:
- Gemini が **自己整合的に捏造した** transcript をコール2 が返す（1 コール設計の失敗モード）
- テロップ由来の ``@channel`` / URL が clip のコピーへ混じる
- 秒区間が本編の尺を超える / 逆順で返る
- usage が cap を超える（リトライ分を含む累計）
"""

from __future__ import annotations

import json

import pytest

from teamagent.skills.clip_proposal.analysis import (
    CELL_COUNT,
    CLIP_SECONDS_MIN,
    LONG_EDGE_LADDER,
    PROXY_LIMIT_BYTES,
    ClipAnalysisError,
    ClipCostGateError,
    ClipProposalAnalyzer,
    CommunityTerm,
    GeminiCall,
    Transcript,
    TranscriptSegment,
    clamp_window,
    estimate_input_tokens,
    is_safe_output_text,
    parse_clips_payload,
    parse_transcript_payload,
    plan_long_edge_ladder,
    quality_note_for,
    select_own_video,
    spend_of,
)

_SEGMENTS = [
    {
        "start_sec": 0.0,
        "end_sec": 6.0,
        "text": "人命・財産・文化を火災から守る総合防災メーカーです",
    },
    {
        "start_sec": 6.0,
        "end_sec": 14.0,
        "text": "消火設備の提案から設計・施工・点検までを一貫して行います",
    },
    {
        "start_sec": 14.0,
        "end_sec": 30.0,
        "text": "1か月の導入研修や工場実習があり未経験の方も基礎から学べます",
    },
    {
        "start_sec": 30.0,
        "end_sec": 60.0,
        "text": "配管が吊られていく姿を見て達成感を一番そこに感じます",
    },
]


def _transcript_json(duration_sec: float = 60.0) -> str:
    return json.dumps({"duration_sec": duration_sec, "segments": _SEGMENTS}, ensure_ascii=False)


def _transcript() -> Transcript:
    return parse_transcript_payload(_transcript_json())


def _clip_item(index: int, **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "start_sec": 10.0,
        "end_sec": 28.0,
        "hook_start_sec": 10.0,
        "hook_seconds": 2.0,
        "hook_copy": f"フック{index}",
        "band_top": f"上帯{index}",
        "band_bottom": f"下帯{index}",
        "search_word": "テスト商材 採用",
        "takeaway": f"伝わること{index}",
        "quote_evidence": "未経験の方も基礎から学べます",
    }
    item.update(overrides)
    return item


def _plan_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "axes": [
            {"text": f"訴求軸{i}", "quote_evidence": "消火設備の提案から設計・施工・点検"}
            for i in range(1, 6)
        ],
        "communities": [
            {
                "name": f"界隈{i}",
                "is_primary": i == 1,
                "terms": [
                    {"term": f"観測語{i}", "observed_on_web": True},
                    {"term": f"未確認語{i}", "observed_on_web": False},
                ],
                "scale_note": f"約{i}万人",
                "description": f"説明{i}",
            }
            for i in range(1, 6)
        ],
        "insights": [
            {"target": f"属性{i}", "insight": f"本音{i}", "evidence_hint": f"材料{i}"}
            for i in range(1, 6)
        ],
        "clips": [_clip_item(i) for i in range(CELL_COUNT)],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# transcript
# ---------------------------------------------------------------------------


def test_transcript_parses_segments_and_duration() -> None:
    transcript = _transcript()
    assert len(transcript.segments) == 4
    assert transcript.duration_sec == 60.0
    assert transcript.contains("未経験の方も基礎から学べます")


def test_transcript_rejects_empty_payload() -> None:
    with pytest.raises(ClipAnalysisError) as excinfo:
        parse_transcript_payload(json.dumps({"segments": []}))
    assert excinfo.value.code == "CLIP_TRANSCRIPT_EMPTY"


def test_transcript_contains_normalizes_punctuation_but_not_content() -> None:
    transcript = _transcript()
    # 読点・全半角の揺れは吸収する（照合を緩めるのではなく、照合前に潰す）。
    assert transcript.contains("未経験の方も、基礎から学べます")
    # 本編に無い発言は通さない。
    assert not transcript.contains("年間休日は125日あります")
    # 短すぎる断片は「実在」の証明にならない。
    assert not transcript.contains("の方")


# ---------------------------------------------------------------------------
# 捏造ガード（安全装置1: quote_evidence はコール1 の確定 transcript とだけ照合）
# ---------------------------------------------------------------------------


def test_clip_with_fabricated_quote_is_not_adopted() -> None:
    fabricated = _clip_item(0, quote_evidence="年間休日は125日で有給消化率も高いです")
    raw = _plan_json(clips=[fabricated, *[_clip_item(i) for i in range(1, CELL_COUNT)]])
    _axes, clips, dropped = parse_clips_payload(raw, _transcript())
    assert len(clips) == CELL_COUNT - 1
    assert [d.reason for d in dropped] == ["quote_not_in_transcript"]


def test_call2_supplied_transcript_is_ignored() -> None:
    """コール2 が transcript を返しても捨てる（自己整合的な捏造を素通りさせない）。"""

    raw = _plan_json(
        transcript={"segments": [{"start_sec": 0, "end_sec": 5, "text": "年間休日は125日です"}]},
        clips=[_clip_item(0, quote_evidence="年間休日は125日です")],
    )
    _axes, clips, dropped = parse_clips_payload(raw, _transcript())
    assert clips == ()
    assert dropped[0].reason == "quote_not_in_transcript"


def test_axes_require_evidence_in_confirmed_transcript() -> None:
    raw = _plan_json(
        axes=[
            {"text": "実在する軸", "quote_evidence": "消火設備の提案から設計・施工・点検"},
            {"text": "捏造した軸", "quote_evidence": "上場企業で福利厚生も充実"},
        ]
    )
    axes, _clips, _dropped = parse_clips_payload(raw, _transcript())
    assert [axis.text for axis in axes] == ["実在する軸"]


# ---------------------------------------------------------------------------
# untrusted 扱い（安全装置2）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "<!channel> 今すぐ確認",
        "詳しくは https://example.com/promo",
        "www.example.com を見て",
        "連絡は @komata まで",
        "<@U12345> にDM",
        "行1\n\n\n行2",
        "制御\x07文字",
        "   ",
    ],
)
def test_unsafe_text_is_rejected(text: str) -> None:
    assert not is_safe_output_text(text)


def test_safe_text_passes() -> None:
    assert is_safe_output_text("未経験でも1か月で基礎が身につく")


def test_clip_with_telop_injection_is_dropped_with_marker() -> None:
    raw = _plan_json(
        clips=[
            _clip_item(0, band_top="<!channel> 応募はこちら"),
            *[_clip_item(i) for i in range(1, CELL_COUNT)],
        ]
    )
    _axes, clips, dropped = parse_clips_payload(raw, _transcript())
    assert len(clips) == CELL_COUNT - 1
    assert dropped[0].reason == "unsafe_text"
    # どの語で止まったかを必ず出す（黙って落とさない）。
    assert dropped[0].marker.startswith("<!channel>")


# ---------------------------------------------------------------------------
# 界隈言語の「未検証」表示（安全装置3）
# ---------------------------------------------------------------------------


def test_unverified_community_term_is_marked() -> None:
    assert CommunityTerm(term="配属ガチャ", verified=True).render() == "配属ガチャ"
    assert CommunityTerm(term="謎語", verified=False).render() == "謎語（未検証）"


def test_community_detail_lines_always_mark_scale_as_assumption() -> None:
    raw = _plan_json()
    _axes, clips, _dropped = parse_clips_payload(raw, _transcript())
    detail = clips[0].detail_lines
    assert detail[0] == "・界隈言語：観測語1／未確認語1（未検証）"
    assert detail[1].endswith("（仮定値）")


# ---------------------------------------------------------------------------
# 秒区間（安全装置4: LLM 申告を必ず clamp する）
# ---------------------------------------------------------------------------


def test_window_is_clamped_into_the_source_duration() -> None:
    window = clamp_window(50.0, 9999.0, duration_sec=60.0)
    assert window.end_sec == 60.0
    assert window.start_sec >= 0.0


def test_window_hook_stays_inside_the_clip() -> None:
    window = clamp_window(10.0, 30.0, duration_sec=60.0, hook_start_sec=0.0, hook_seconds=2.0)
    assert window.hook_start_sec >= window.start_sec
    assert window.hook_end_sec <= window.end_sec


def test_window_reversed_input_is_repaired() -> None:
    window = clamp_window(30.0, 10.0, duration_sec=60.0)
    assert window.start_sec < window.end_sec
    assert window.seconds >= CLIP_SECONDS_MIN


def test_window_requires_a_known_duration() -> None:
    with pytest.raises(ClipAnalysisError) as excinfo:
        clamp_window(0.0, 10.0, duration_sec=0.0)
    assert excinfo.value.code == "CLIP_WINDOW_INVALID"


def test_clip_render_shows_range_and_hook() -> None:
    _axes, clips, _dropped = parse_clips_payload(_plan_json(), _transcript())
    note = clips[0].render_note_lines()
    assert note[0] == "切り抜き箇所"
    assert "0:10〜0:28" in note[1]
    assert "フック映像" in note[2]


# ---------------------------------------------------------------------------
# 費用（安全装置5: 課金前ゲート / cap はリトライ分も含む累計で止める）
# ---------------------------------------------------------------------------


class _RecordingCaller:
    def __init__(self, responses: list[GeminiCall]) -> None:
        self.responses = responses
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> GeminiCall:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


def _analyzer(
    transcript_cost: float = 0.01,
    plan_cost: float = 0.01,
    *,
    cost_cap_usd: float = 1.0,
    input_token_gate: int = 1_100_000,
) -> tuple[ClipProposalAnalyzer, _RecordingCaller, _RecordingCaller]:
    transcript_caller = _RecordingCaller(
        [GeminiCall(text=_transcript_json(), cost_usd=transcript_cost, model_id="gemini-2.5-flash")]
    )
    text_caller = _RecordingCaller([GeminiCall(text=_plan_json(), cost_usd=plan_cost)])
    analyzer = ClipProposalAnalyzer(
        request_id="req-test",
        transcript_caller=lambda data, mime, prompt: transcript_caller(),
        text_caller=lambda prompt, request_id: text_caller(),
        cost_cap_usd=cost_cap_usd,
        input_token_gate=input_token_gate,
    )
    return analyzer, transcript_caller, text_caller


def test_analyzer_runs_two_calls_and_reports_measured_cost() -> None:
    analyzer, transcript_caller, text_caller = _analyzer()
    result = analyzer.run(
        video_bytes=b"x" * 1024,
        mime_type="video/mp4",
        duration_sec=60.0,
        client_name="テスト商事",
    )
    assert transcript_caller.calls == 1
    assert text_caller.calls == 1
    assert result.clip_count == CELL_COUNT
    assert len(result.axes) == 5
    assert result.gemini_calls == 2
    assert result.cost_usd == pytest.approx(0.02)


def test_pre_billing_gate_spends_nothing_when_input_is_too_large() -> None:
    analyzer, transcript_caller, text_caller = _analyzer(input_token_gate=1000)
    with pytest.raises(ClipCostGateError) as excinfo:
        analyzer.run(
            video_bytes=b"x" * 1024,
            mime_type="video/mp4",
            duration_sec=3600.0,
            client_name="テスト商事",
        )
    assert excinfo.value.code == "CLIP_INPUT_TOO_LARGE"
    # **1 コールも打っていない**ことが肝（打ってから測ると課金済み）。
    assert transcript_caller.calls == 0
    assert text_caller.calls == 0


def test_cost_cap_stops_before_the_second_call() -> None:
    analyzer, transcript_caller, text_caller = _analyzer(transcript_cost=2.0, cost_cap_usd=1.0)
    with pytest.raises(ClipCostGateError) as excinfo:
        analyzer.run(
            video_bytes=b"x" * 1024,
            mime_type="video/mp4",
            duration_sec=60.0,
            client_name="テスト商事",
        )
    assert excinfo.value.code == "CLIP_COST_CAP_EXCEEDED"
    assert transcript_caller.calls == 1
    assert text_caller.calls == 0  # cap 超過後に有料コールを足さない


def test_cost_cap_on_the_last_call_fails_the_job_instead_of_returning_a_deck() -> None:
    """cap を割ったら成果物を返さない（半端な資料を配って課金だけ増やさない）。"""

    analyzer, _transcript_caller, text_caller = _analyzer(
        transcript_cost=0.4, plan_cost=0.9, cost_cap_usd=1.0
    )
    with pytest.raises(ClipCostGateError) as excinfo:
        analyzer.run(
            video_bytes=b"x" * 1024,
            mime_type="video/mp4",
            duration_sec=60.0,
            client_name="テスト商事",
        )
    assert excinfo.value.code == "CLIP_COST_CAP_EXCEEDED"
    # 2 コール目までは打ててしまう（cap の分母は実測 usage なので事前には分からない）。
    assert text_caller.calls == 1


def test_truncated_output_is_not_handed_to_a_lenient_parser() -> None:
    analyzer = ClipProposalAnalyzer(
        request_id="req-test",
        transcript_caller=lambda data, mime, prompt: GeminiCall(
            text='{"segments": [{"start_sec": 0, "end_sec"', finish_reason="MAX_TOKENS"
        ),
        text_caller=lambda prompt, request_id: GeminiCall(text=_plan_json()),
    )
    with pytest.raises(ClipAnalysisError) as excinfo:
        analyzer.run(
            video_bytes=b"x",
            mime_type="video/mp4",
            duration_sec=60.0,
            client_name="テスト商事",
        )
    assert excinfo.value.code == "CLIP_OUTPUT_TRUNCATED"


def test_estimate_input_tokens_uses_duration_and_bytes() -> None:
    assert estimate_input_tokens(duration_sec=60.0, size_bytes=0) == 18_000
    # 1KiB ≒ 4 tokens（proxy 実測から導出・estimate_input_tokens の docstring 参照）。
    assert estimate_input_tokens(duration_sec=0.0, size_bytes=1024 * 1024) == 4096


def test_unknown_duration_is_fail_closed_before_any_paid_call() -> None:
    """本番の失敗モード: media client 未配線で尺が 0.0 のまま渡ってくる。

    ゲートは実質「尺のみ」で効いていて、バイト項は解析用 proxy 上限 18MB でも
    閾値の 1/15 にしかならない。素通しすると 100MB の素材でもゲートが 1 度も
    発火しないまま有料コールへ入る。
    """

    analyzer, _t, _p = _analyzer()
    with pytest.raises(ClipCostGateError) as excinfo:
        analyzer.preflight_cost_gate(duration_sec=0.0, size_bytes=100 * 1024 * 1024)
    assert excinfo.value.code == "CLIP_DURATION_UNKNOWN"

    with pytest.raises(ClipCostGateError):
        analyzer.preflight_cost_gate(duration_sec=-1.0, size_bytes=1024)

    # 尺が取れていれば従来どおり（既定ゲート 1,100,000 の内側）通す。
    analyzer.preflight_cost_gate(duration_sec=60.0, size_bytes=18 * 1024 * 1024)


def test_unknown_duration_never_reaches_the_transcript_call() -> None:
    """1 コールも打たない（ゲートの意味）。"""

    caller = _RecordingCaller([GeminiCall(text="{}", cost_usd=0.4)])
    analyzer = ClipProposalAnalyzer(
        request_id="req-1", transcript_caller=caller, text_caller=caller
    )
    with pytest.raises(ClipCostGateError):
        analyzer.run(
            video_bytes=b"x" * 1024,
            mime_type="video/mp4",
            duration_sec=0.0,
            client_name="テスト商事",
        )
    assert caller.calls == 0


# ---------------------------------------------------------------------------
# 段階的劣化（できないことを手作業へ突き返さない）
# ---------------------------------------------------------------------------


def test_degradation_ladder_reaches_the_lowest_rung() -> None:
    assert LONG_EDGE_LADDER == (1280, 720, 480, 360, 240)


def test_ladder_keeps_every_rung_when_the_source_already_fits() -> None:
    assert plan_long_edge_ladder(size_bytes=0) == LONG_EDGE_LADDER
    assert plan_long_edge_ladder(size_bytes=1024) == LONG_EDGE_LADDER
    assert plan_long_edge_ladder(size_bytes=PROXY_LIMIT_BYTES) == LONG_EDGE_LADDER


def test_ladder_skips_rungs_that_cannot_fit() -> None:
    """入らないと分かっている段を再エンコードしない（バイトは面積にほぼ比例）。

    100MB の素材を長辺 1280 起点で見ると 1280（100MB）・720（31.6MB）は上限 18MB を
    超え、480（14.1MB）で初めて収まる。
    """

    assert plan_long_edge_ladder(size_bytes=100 * 1024 * 1024) == (480, 360, 240)
    assert plan_long_edge_ladder(size_bytes=40 * 1024 * 1024) == (720, 480, 360, 240)


def test_ladder_always_offers_the_lowest_rung() -> None:
    """どの段でも入らない見積りでも **手作業へ突き返さない**（最下段は必ず返す）。"""

    assert plan_long_edge_ladder(size_bytes=100 * 1024 * 1024 * 1024) == (240,)


def test_quality_note_only_when_degraded() -> None:
    assert quality_note_for(1280) == ""
    note = quality_note_for(480)
    assert "480px" in note
    assert "音声中心" in note


# ---------------------------------------------------------------------------
# 素材の同意（安全装置6: 本人がアップロードした動画だけ）
# ---------------------------------------------------------------------------


def _file(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "id": "F1",
        "user": "U_ME",
        "mimetype": "video/mp4",
        "size": 1024,
        "url_private_download": "https://files.slack.com/a.mp4",
    }
    item.update(overrides)
    return item


def test_only_the_requesters_own_upload_is_a_candidate() -> None:
    picked, reason = select_own_video([_file(user="U_OTHER")], uploader_id="U_ME", max_bytes=10_000)
    assert picked is None
    assert reason == "not_owner"

    picked, reason = select_own_video([_file()], uploader_id="U_ME", max_bytes=10_000)
    assert picked is not None
    assert reason == ""


def test_external_and_tombstoned_and_oversized_files_are_not_candidates() -> None:
    assert select_own_video([_file(is_external=True)], uploader_id="U_ME", max_bytes=10_000) == (
        None,
        "not_found",
    )
    assert select_own_video([_file(is_tombstoned=True)], uploader_id="U_ME", max_bytes=10_000) == (
        None,
        "not_found",
    )
    assert select_own_video(
        [_file(mimetype="image/png")], uploader_id="U_ME", max_bytes=10_000
    ) == (
        None,
        "not_found",
    )
    picked, reason = select_own_video([_file(size=99_999)], uploader_id="U_ME", max_bytes=10_000)
    assert picked is None
    assert reason == "too_large"


def test_missing_verified_uploader_blocks_every_candidate() -> None:
    assert select_own_video([_file()], uploader_id="", max_bytes=10_000) == (None, "not_owner")


def test_transcript_prompt_text_is_passed_to_call2_not_the_video() -> None:
    transcript = Transcript(
        segments=(TranscriptSegment(start_sec=0.0, end_sec=3.0, text="はじめまして"),),
        duration_sec=3.0,
    )
    assert "[0.0-3.0] はじめまして" in transcript.as_prompt_text()


# ---------------------------------------------------------------------------
# untrusted 扱いの抜け（安全装置2 の残り面）
#
# clip 直下のコピーだけでなく、界隈 / インサイト側の文字列も
# ``render_detail_lines()`` 経由で資料本文の段落へ入る。ここが素通りすると、
# quote_evidence が確定 transcript に実在する「正規の」clip に対して、
# 第三者が仕込んだ URL とメンションが得意先向け PPTX に載る。
# ---------------------------------------------------------------------------


def _injected_community(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "name": "界隈1",
        "is_primary": True,
        "terms": [{"term": "観測語1", "observed_on_web": True}],
        "scale_note": "約1万人",
        "description": "説明1",
    }
    item.update(overrides)
    return item


@pytest.mark.parametrize(
    ("field", "payload"),
    [
        ("scale_note", "詳しくは https://evil.example/pwn を見てください"),
        ("description", "この界隈は <!channel> で盛り上がっている"),
        ("description", "連絡は @admin まで"),
        ("scale_note", "制御\x07文字入り"),
        ("name", "<@U12345> の界隈"),
    ],
)
def test_community_injection_drops_the_cell_not_just_the_clip_copy(
    field: str, payload: str
) -> None:
    raw = _plan_json(
        communities=[
            _injected_community(**{field: payload}),
            *[_injected_community(name=f"界隈{i}") for i in range(2, 6)],
        ]
    )
    _axes, clips, dropped = parse_clips_payload(raw, _transcript())

    # quote_evidence は実在するので clip 自体は「正しい」。落ちるのは界隈側の検査。
    assert [clip.cell_index for clip in clips] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert [d.cell_index for d in dropped] == [0]
    assert dropped[0].reason == "unsafe_source_text"
    assert dropped[0].marker  # どの語で止まったかを必ず出す
    assert payload.startswith(dropped[0].marker[:8])

    # 資料本文（detail_lines）に仕込みが 1 文字も残っていないこと。
    rendered = "\n".join("\n".join(clip.detail_lines) for clip in clips)
    assert "evil.example" not in rendered
    assert "<!channel>" not in rendered
    assert "@admin" not in rendered


def test_community_injection_does_not_shift_other_communities_into_the_wrong_cell() -> None:
    """落とした界隈を詰めると、界隈 2 の説明が界隈 1 のセルへ繰り上がる。"""

    raw = _plan_json(
        communities=[
            _injected_community(name="界隈1", description="https://evil.example/pwn"),
            *[_injected_community(name=f"界隈{i}", description=f"説明{i}") for i in range(2, 6)],
        ]
    )
    _axes, clips, _dropped = parse_clips_payload(raw, _transcript())
    community_cells = {clip.cell_index: clip for clip in clips if clip.kind == "community"}
    assert community_cells[1].label.startswith("界隈2")
    assert "・説明2" in community_cells[1].detail_lines


@pytest.mark.parametrize(
    "hint",
    [
        "http://evil.example/leak",
        "www.evil.example",
        "<!here> を見て",
        "連絡は @komata まで",
    ],
)
def test_insight_evidence_hint_injection_drops_the_cell(hint: str) -> None:
    raw = _plan_json(
        insights=[
            {"target": "属性1", "insight": "本音1", "evidence_hint": hint},
            *[
                {"target": f"属性{i}", "insight": f"本音{i}", "evidence_hint": f"材料{i}"}
                for i in range(2, 6)
            ],
        ]
    )
    _axes, clips, dropped = parse_clips_payload(raw, _transcript())

    # インサイトの 1 枠目 ＝ セル index 5（界隈 5 枠の次）。
    assert [d.cell_index for d in dropped] == [5]
    assert dropped[0].reason == "unsafe_source_text"
    rendered = "\n".join("\n".join(clip.detail_lines) for clip in clips)
    assert "evil.example" not in rendered
    assert "<!here>" not in rendered
    assert "@komata" not in rendered


def test_community_term_injection_is_also_refused() -> None:
    raw = _plan_json(
        communities=[
            _injected_community(terms=[{"term": "https://evil.example", "observed_on_web": True}]),
            *[_injected_community(name=f"界隈{i}") for i in range(2, 6)],
        ]
    )
    _axes, clips, dropped = parse_clips_payload(raw, _transcript())
    assert dropped[0].reason == "unsafe_source_text"
    assert "evil.example" not in "\n".join("\n".join(c.detail_lines) for c in clips)


def test_clean_sources_are_still_adopted() -> None:
    """検査を足したことで正規の界隈 / インサイトまで落としていないこと。"""

    _axes, clips, dropped = parse_clips_payload(_plan_json(), _transcript())
    assert len(clips) == CELL_COUNT
    assert dropped == ()
    assert clips[0].detail_lines[2] == "・説明1"
    assert clips[5].detail_lines[1] == "・判断材料：材料1"


# ---------------------------------------------------------------------------
# 失敗しても実課金分は計上する（安全装置5 の残り面）
# ---------------------------------------------------------------------------


def test_failed_parse_still_reports_what_was_actually_charged() -> None:
    """本番の失敗モード: 2 コール課金したあとコール2 の出力が JSON でない。

    費用カウンタが成功時にしか動かないと、プロンプト事故やモデル側の出力崩れが
    続く日は CLIP_DAILY_COST_CAP_USD が一度も発火しない。
    """

    caller = _RecordingCaller(
        [
            GeminiCall(text=_transcript_json(), cost_usd=0.40),
            GeminiCall(text="ごめん", cost_usd=0.40),
        ]
    )
    analyzer = ClipProposalAnalyzer(
        request_id="req-1",
        transcript_caller=caller,
        text_caller=caller,
        cost_cap_usd=5.0,
    )
    with pytest.raises(ClipAnalysisError) as excinfo:
        analyzer.run(
            video_bytes=b"x",
            mime_type="video/mp4",
            duration_sec=60.0,
            client_name="テスト商事",
        )
    assert excinfo.value.code == "CLIP_PLAN_INVALID"
    assert spend_of(excinfo.value) == pytest.approx(0.80)
    assert excinfo.value.calls == 2


def test_truncated_output_reports_the_call_it_already_paid_for() -> None:
    caller = _RecordingCaller(
        [GeminiCall(text=_transcript_json(), cost_usd=0.25, finish_reason="MAX_TOKENS")]
    )
    analyzer = ClipProposalAnalyzer(
        request_id="req-1", transcript_caller=caller, text_caller=caller, cost_cap_usd=5.0
    )
    with pytest.raises(ClipAnalysisError) as excinfo:
        analyzer.run(
            video_bytes=b"x",
            mime_type="video/mp4",
            duration_sec=60.0,
            client_name="テスト商事",
        )
    assert excinfo.value.code == "CLIP_OUTPUT_TRUNCATED"
    assert spend_of(excinfo.value) == pytest.approx(0.25)


def test_gate_failure_reports_zero_spend() -> None:
    """1 コールも打っていない失敗は 0.0（払っていない分を計上しない）。"""

    caller = _RecordingCaller([GeminiCall(text="{}", cost_usd=0.4)])
    analyzer = ClipProposalAnalyzer(
        request_id="req-1", transcript_caller=caller, text_caller=caller
    )
    with pytest.raises(ClipCostGateError) as excinfo:
        analyzer.run(
            video_bytes=b"x",
            mime_type="video/mp4",
            duration_sec=0.0,
            client_name="テスト商事",
        )
    assert spend_of(excinfo.value) == 0.0


def test_caller_exception_carries_the_spend_so_far() -> None:
    """注入された caller が任意の例外を上げても、直前までの実課金は伝わる。"""

    class _Boom:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, *args: object, **kwargs: object) -> GeminiCall:
            self.calls += 1
            if self.calls == 1:
                return GeminiCall(text=_transcript_json(), cost_usd=0.33)
            raise RuntimeError("vertex unavailable")

    caller = _Boom()
    analyzer = ClipProposalAnalyzer(
        request_id="req-1", transcript_caller=caller, text_caller=caller, cost_cap_usd=5.0
    )
    with pytest.raises(RuntimeError) as excinfo:
        analyzer.run(
            video_bytes=b"x",
            mime_type="video/mp4",
            duration_sec=60.0,
            client_name="テスト商事",
        )
    assert spend_of(excinfo.value) == pytest.approx(0.33)
