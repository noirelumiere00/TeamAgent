"""提案/検索回答のグラウンディング忠実性を測る（⑤ 忠実性 / Phase 5）。

**決定的な citation-validity**（回答が引用した chunk_id が、実際に取得できた hits に
存在するか）を純関数で測る。LLM judge 不要 ＝ 課金0・CI で回せる。

検出できる忠実性の失敗:
- 捏造引用（fabricated）: hits に無い chunk_id を回答が引用している（hallucination の痕跡）。
- 無引用（no citations）: 根拠がある状況なのに回答が一切引用していない（追跡不能）。

これは「忠実性の下限」を決定的に押さえる層。文単位の主張が根拠に支持されているか（claim
support）の深い判定は LLM judge が要る（別関数・課金あり）。まずこの決定的層で「捏造引用が
ゼロか」を CI/eval で恒常監視できるようにする。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# 回答テキスト中の人間可読な引用: "[chunk_id: 123]" / "chunk_id：123" 等。
_ANSWER_CITE_RE = re.compile(r"chunk_id[\s:：=]+(\d+)")
# ツールが返す JSON 中の "chunk_id": 123（available 集合の抽出用）。
_JSON_CHUNK_RE = re.compile(r'"chunk_id"\s*:\s*(\d+)')


def extract_cited_chunk_ids(answer: str) -> list[int]:
    """回答テキストが引用している chunk_id を抽出する（順序つき・重複あり）。"""
    return [int(m) for m in _ANSWER_CITE_RE.findall(answer or "")]


def extract_chunk_ids_from_tool_json(json_text: str) -> list[int]:
    """ツール結果 JSON から chunk_id を抽出する（available 集合の構築用）。"""
    return [int(m) for m in _JSON_CHUNK_RE.findall(json_text or "")]


@dataclass(frozen=True)
class FaithfulnessScore:
    """回答の引用忠実性（決定的）。"""

    cited: tuple[int, ...]  # 回答が引用した chunk_id（重複除去）
    valid: tuple[int, ...]  # available に存在する引用
    fabricated: tuple[int, ...]  # available に無い引用（捏造の痕跡）
    available_count: int  # ツールが返した chunk_id の総数
    has_citations: bool  # 回答が1つでも引用しているか

    @property
    def citation_validity(self) -> float:
        """引用のうち available に裏付けられた割合（引用なしは 1.0=減点しない）。"""
        return len(self.valid) / len(self.cited) if self.cited else 1.0

    @property
    def is_clean(self) -> bool:
        """捏造引用が無いか（忠実性の最低ライン）。"""
        return not self.fabricated


def score_faithfulness(answer: str, available_chunk_ids: Iterable[int]) -> FaithfulnessScore:
    """回答の引用が available（取得済み hits）に裏付けられているかを決定的に採点する。"""
    avail = set(available_chunk_ids)
    cited_raw = extract_cited_chunk_ids(answer)
    cited = list(dict.fromkeys(cited_raw))  # 重複除去・順序保持
    valid = [c for c in cited if c in avail]
    fabricated = [c for c in cited if c not in avail]
    return FaithfulnessScore(
        cited=tuple(cited),
        valid=tuple(valid),
        fabricated=tuple(fabricated),
        available_count=len(avail),
        has_citations=bool(cited),
    )


__all__ = [
    "FaithfulnessScore",
    "extract_chunk_ids_from_tool_json",
    "extract_cited_chunk_ids",
    "score_faithfulness",
]
