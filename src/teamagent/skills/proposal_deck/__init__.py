"""proposal_deck Skill — 商材情報 + 研究素材 → FMT v2 (95 placeholder) → .pptx 生成。

teamagent_consulting で実証した Composer/契約/レンダラを本番 Skill 化したもの。
Agent SDK オーケストレータが search / proposal_draft / clientkarte / mail_constraints で
集めた素材を本 Skill に渡し、提案書 .pptx を生成する。
"""

from __future__ import annotations

from teamagent.skills.proposal_deck.skill import ProposalDeckSkill

__all__ = ["ProposalDeckSkill"]
