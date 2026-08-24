"""FMT レンダリングの入口: 計測JSON → HTML + 画像モードPPTX + 編集用PPTX。

- 正 = 画像モードPPTX（HTML → media worker slides オペ(chromium) → 1920x1080 PNG →
  python-pptx 全面貼付）。``build_image_pptx`` が経路A（spec_README）で生成する。
- 併走 = 編集用ネイティブPPTX（editable.py）。ファイル名と表紙に
  「編集用（見た目は画像版が正）」を明記する。
- 配信文は ``build_delivery_comment``（要点3行 + 修正はこのスレッドで再依頼 + 次の一手）。
"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from teamagent.skills.omiyage_report.fmt.contract import DeckContent, validate_deck_content
from teamagent.skills.omiyage_report.fmt.editable import EDIT_MARKER, render_editable_pptx
from teamagent.skills.omiyage_report.fmt.html import render_deck_html
from teamagent.skills.omiyage_report.fmt.spec import FmtDeckSpec, load_fmt_spec

# media worker の slides HTML 上限（operations.py の MEDIA_HTML_SIZE_EXCEEDED と同値）
_MAX_HTML_BYTES = 2 * 1024 * 1024

# 便1配信の固定文言（場面設計: 修正ループはスレッド内で完結・数分で再生成）
REVISION_NOTE = "修正はこのスレッドで再依頼ください（数分で再生成してお届けします）。"


class FmtRenderError(RuntimeError):
    """レンダリング成果物が media 契約（2MB等）を満たさない。"""


@dataclass(frozen=True)
class FmtDeckArtifacts:
    content: DeckContent
    html: str
    editable_pptx: bytes
    image_filename: str
    editable_filename: str
    slide_labels: tuple[str, ...]


def render_fmt_deck(
    raw_content: object,
    *,
    spec: FmtDeckSpec | None = None,
    generated_on: str,
    font_dir: Path | None = None,
) -> FmtDeckArtifacts:
    """計測JSON（input_contract）→ 検証 → HTML + 編集用PPTX（決定論）。"""
    resolved_spec = spec or load_fmt_spec()
    content = validate_deck_content(raw_content, resolved_spec)
    html = render_deck_html(content, resolved_spec, font_dir=font_dir)
    html_bytes = len(html.encode("utf-8"))
    if html_bytes > _MAX_HTML_BYTES:
        raise FmtRenderError(
            f"slides HTML exceeds media worker limit ({html_bytes} > {_MAX_HTML_BYTES} bytes); "
            "画像バジェット（embed_budget）とフォントサブセットを見直すこと"
        )
    editable = render_editable_pptx(content, resolved_spec)
    labels = tuple(
        f"{page_no:02d} {slide.type}"
        + (f" {slide.q_number}" if slide.q_number else "")
        + (f" {slide.heading}" if slide.heading else "")
        for page_no, slide in enumerate(content.slides, start=1)
    )
    return FmtDeckArtifacts(
        content=content,
        html=html,
        editable_pptx=editable,
        image_filename=f"omiyage_fmt_{generated_on}.pptx",
        editable_filename=f"omiyage_fmt_{generated_on}_{EDIT_MARKER}.pptx",
        slide_labels=labels,
    )


def build_image_pptx(html: str, *, request_fingerprint: str) -> bytes:
    """経路A: media worker slides オペで 1920x1080・scale=1 の画像モードPPTXを得る。

    本番は media worker（chromium + python-pptx）。ローカル opt-in 時のみ
    playwright + python-pptx（video_algorithm の実装を流用）。
    """
    from teamagent.adapters.media_job import MediaJobClient

    if MediaJobClient.is_configured():
        return MediaJobClient().slides_to_pptx(
            html,
            request_fingerprint=request_fingerprint,
            width=1920,
            height=1080,
            device_scale_factor=1,
        )
    if MediaJobClient.local_runtime_enabled():
        from teamagent.skills.video_algorithm.pptx_export import build_pptx, shoot_sections

        with tempfile.TemporaryDirectory(prefix="omiyage-fmt-") as workdir:
            out_path = str(Path(workdir) / "slides.pptx")
            build_pptx(shoot_sections(html), out_path)
            return Path(out_path).read_bytes()
    MediaJobClient.require_configured()
    raise AssertionError("unreachable")  # pragma: no cover


def build_delivery_comment(summary_lines: Sequence[str], next_step: str) -> str:
    """配信文: 要点3行 → 修正ループ案内（固定文） → 次の一手1行。"""
    lines = [*summary_lines, REVISION_NOTE, next_step]
    return "\n".join(line for line in lines if line)
