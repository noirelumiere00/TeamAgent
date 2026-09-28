"""キャッシュ済みの結果から、横断（facts→synthesis）だけやり直す入口（仕様 v3 §3-4 Phase1）。

1 本ずつの動画分析（Gemini）はクライアント名に依存しない（区分はコードが名簿で決める）。
クライアント名・競合・避けたい訴求だけが違う依頼では、キャッシュ済みの videos と board を
読み、横断の集計と synthesis だけを作り直せばよい（動画 5 本の再分析をしない）。

- synthesize を渡すと LLM で作り直す（例: functools.partial(synthesis.synthesize, gemini,
  request_id=...)）。呼び出しは synthesize(videos, query, stats=, roster=, board=, avoid_terms=)。
- 渡さなければ LLM を呼ばず、キャッシュ済みの synthesis に v3 の検査を名簿つきで掛け直す
  （synthesis.recheck_synthesis）。受け入れ確認・テストで本番の出力を検査し直すのにも使う。

描画（report / slides / pptx）は次の段で行う。成果物の URL は古い中身を指すので空にして返す。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from teamagent.skills._shared.grounding import DropSink
from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.evidence import Roster
from teamagent.skills.video_algorithm.schema import CrossSynthesis, VideoAlgorithmOutput
from teamagent.skills.video_algorithm.synthesis import recheck_synthesis

Synthesizer = Callable[..., tuple[CrossSynthesis | None, float]]


def rebuild_cross(
    output: VideoAlgorithmOutput,
    *,
    client_name: str | None = None,
    competitors: Sequence[str] | None = None,
    avoid_terms: Sequence[str] | None = None,
    synthesize: Synthesizer | None = None,
    request_id: str = "rebuild",
    on_drop: DropSink | None = None,
) -> VideoAlgorithmOutput:
    """名簿・避けたい訴求を差し替えて、横断（cross）と synthesis を作り直した写しを返す。"""
    roster = Roster.of(client_name, competitors)
    avoid = list(dict.fromkeys(t.strip() for t in (avoid_terms or ()) if t and t.strip()))
    videos = list(output.videos)
    board = list(output.board)
    cross = cross_analyze(videos, output.query, board=board, roster=roster)
    cost = 0.0
    watched = [v for v in videos if v.analysis is not None]
    if len(watched) >= 2:
        if synthesize is not None:
            syn, cost = synthesize(
                videos,
                output.query,
                stats=cross.stats,
                roster=roster,
                board=board,
                avoid_terms=avoid,
            )
            cross.synthesis = syn
        elif output.cross.synthesis is not None:
            cross.synthesis = recheck_synthesis(
                output.cross.synthesis,
                videos,
                output.query,
                stats=cross.stats,
                roster=roster,
                board=board,
                avoid_terms=avoid,
                request_id=request_id,
                on_drop=on_drop,
            )
    update: dict[str, Any] = {
        "cross": cross,
        "client_name": roster.client_name,
        "competitors": list(roster.competitors),
        "avoid_terms": avoid,
        "total_cost_usd": round(output.total_cost_usd + cost, 6),
        "report_html_path": None,
        "report_url": None,
        "slides_url": None,
        "pptx_url": None,
        "slack_summary": "",
    }
    return output.model_copy(deep=True, update=update)


__all__ = ["Synthesizer", "rebuild_cross"]
