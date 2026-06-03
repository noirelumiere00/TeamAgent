"""PoC 用 fixture スキル＋シナリオ decider（オフライン・架空データ）.

実 adapter（Gmail/Drive/Bedrock/pgvector）には繋がない。実 Skill の I/O 契約を
模した軽量 BaseSkill で、適応ループ機構と分岐を検証する。実 Skill への差し替えは
ToolSpec.factory に本物を渡すだけ（本実装フェーズ）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from teamagent.orchestrator.decider import (
    Decision,
    FinalAnswer,
    Observation,
    ToolCall,
)
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import BaseSkill, SkillContext

# 架空の NG キーワード（Mail に「これは NG」と書かれている想定）.
_BANNED_APPROACH = "インフルエンサー一斉投下"


# --- ① get_client_history（operation_log + search 相当）---
class ClientHistoryInput(BaseModel):
    client: str = Field(..., description="クライアント識別子")


class ClientHistoryOutput(BaseModel):
    summary: str
    flopped_axis: str | None = Field(None, description="過去に不調だった訴求軸")
    total_cost_usd: float = 0.0


class GetClientHistorySkill(BaseSkill[ClientHistoryInput, ClientHistoryOutput]):
    name = "get_client_history"
    description = "クライアントの過去施策履歴と結果（どの訴求軸で滑ったか）を返す"
    input_schema = ClientHistoryInput
    output_schema = ClientHistoryOutput

    def run(
        self, input: ClientHistoryInput, ctx: SkillContext
    ) -> ClientHistoryOutput:
        return ClientHistoryOutput(
            summary=f"{input.client}は過去に『認知』施策を実施したがKPI未達（滑った）",
            flopped_axis="認知",
            total_cost_usd=0.001,
        )


# --- ③⑤ draft_measure（proposal 相当）---
class DraftInput(BaseModel):
    brief: str
    axis: str | None = None


class DraftOutput(BaseModel):
    draft: str
    approach: str = Field(..., description="施策の具体手法（Mail制約チェックの対象）")
    total_cost_usd: float = 0.0


class DraftMeasureSkill(BaseSkill[DraftInput, DraftOutput]):
    name = "draft_measure"
    description = "briefと訴求軸から施策案を生成する"
    input_schema = DraftInput
    output_schema = DraftOutput

    def run(self, input: DraftInput, ctx: SkillContext) -> DraftOutput:
        # 「別案/NG回避/再/避け…」等の言い回しが brief にあれば安全な手法に切替（最初の案は NG 手法）.
        # 実 LLM の自然な再ドラフト指示でもスタックせず差し替わるよう、トリガを広めに取る.
        _avoid = any(k in input.brief for k in ("NG回避", "別案", "別の", "避け", "代替", "再"))
        approach = "比較動画でCV導線設計" if _avoid else _BANNED_APPROACH
        return DraftOutput(
            draft=f"[{input.axis}型] {input.brief} / 手法: {approach}",
            approach=approach,
            total_cost_usd=0.01,
        )


# --- ④ check_mail_constraints（gmail_client 相当）---
class MailCheckInput(BaseModel):
    approach: str


class MailCheckOutput(BaseModel):
    ng: bool
    ng_reason: str | None = None
    total_cost_usd: float = 0.0


class CheckMailConstraintsSkill(BaseSkill[MailCheckInput, MailCheckOutput]):
    name = "check_mail_constraints"
    description = "Mailに記録された制約（この手法はNG等）に手法が抵触するか判定する"
    input_schema = MailCheckInput
    output_schema = MailCheckOutput

    def run(self, input: MailCheckInput, ctx: SkillContext) -> MailCheckOutput:
        if _BANNED_APPROACH in input.approach:
            return MailCheckOutput(
                ng=True,
                ng_reason="過去にインフルエンサー一斉投下でクレーム（Mail記録）",
                total_cost_usd=0.001,
            )
        return MailCheckOutput(ng=False, total_cost_usd=0.001)


# --- ⑤ search_past_cases（search 相当）---
class CasesInput(BaseModel):
    query: str
    top_k: int = 3


class CasesOutput(BaseModel):
    summary: str
    n_cases: int
    total_cost_usd: float = 0.0


class SearchPastCasesSkill(BaseSkill[CasesInput, CasesOutput]):
    name = "search_past_cases"
    description = "Driveの過去事例から、クエリに合う成功事例を検索して要約する"
    input_schema = CasesInput
    output_schema = CasesOutput

    def run(self, input: CasesInput, ctx: SkillContext) -> CasesOutput:
        return CasesOutput(
            summary=f"『{input.query}』に合致するCV型成功事例3件をDriveから取得",
            n_cases=3,
            total_cost_usd=0.005,
        )


# --- 高コストスキル（cost_cap ガードレール検証用）---
class HighCostSkill(BaseSkill[CasesInput, CasesOutput]):
    name = "expensive_tool"
    description = "コスト上限テスト用のダミー高コストツール"
    input_schema = CasesInput
    output_schema = CasesOutput

    def run(self, input: CasesInput, ctx: SkillContext) -> CasesOutput:
        return CasesOutput(summary="expensive", n_cases=0, total_cost_usd=1.0)


def scenario_tools() -> list[ToolSpec]:
    return [
        ToolSpec("get_client_history", GetClientHistorySkill.description, GetClientHistorySkill),
        ToolSpec("draft_measure", DraftMeasureSkill.description, DraftMeasureSkill),
        ToolSpec(
            "check_mail_constraints",
            CheckMailConstraintsSkill.description,
            CheckMailConstraintsSkill,
        ),
        ToolSpec("search_past_cases", SearchPastCasesSkill.description, SearchPastCasesSkill),
    ]


class ScenarioDecider:
    """ユーザー要望シナリオを再現する決定的 decider（観測を読んで適応する）.

    LLM の代わり。history（観測）を見て次の一手を変えるため、ループの
    「結果次第で分岐する」性質を実コードで検証できる。
    """

    def decide(
        self, goal: str, tools: list[ToolSpec], history: list[Observation]
    ) -> Decision:
        hist = [o for o in history if o.tool == "get_client_history"]
        drafts = [o for o in history if o.tool == "draft_measure"]
        mail = [o for o in history if o.tool == "check_mail_constraints"]
        cases = [o for o in history if o.tool == "search_past_cases"]

        # ① まず履歴を確認
        if not hist:
            return ToolCall("get_client_history", {"client": "X"})

        flopped: Any = hist[-1].output.get("flopped_axis")
        # ② 認知が滑った → 今回は CV を提案（適応判断）
        target_axis = "CV" if flopped == "認知" else "認知"

        # ③ まず施策案を作る
        if not drafts:
            return ToolCall(
                "draft_measure", {"brief": f"{target_axis}重視の施策案", "axis": target_axis}
            )

        # ④ 直近案の手法を Mail 制約に照合
        if not mail:
            approach: Any = drafts[-1].output.get("approach", "")
            return ToolCall("check_mail_constraints", {"approach": approach})

        # ④' NG なら別案に差し替え（1回だけ）
        if mail[-1].output.get("ng") and len(drafts) < 2:
            reason: Any = mail[-1].output.get("ng_reason") or ""
            return ToolCall(
                "draft_measure",
                {"brief": f"{target_axis}重視・NG回避（{reason}）の別案", "axis": target_axis},
            )

        # ⑤ 裏付け事例を Drive から取得
        if not cases:
            return ToolCall("search_past_cases", {"query": f"{target_axis} 成功事例"})

        # ⑥ 統合して最終回答
        final_draft: Any = drafts[-1].output.get("draft", "")
        case_summary: Any = cases[-1].output.get("summary", "")
        ng_reason2: Any = mail[-1].output.get("ng_reason")
        return FinalAnswer(
            f"【施策提案】過去の『{flopped}』施策が不調だったため、今回は{target_axis}型を提案します。"
            f" Mail制約（{ng_reason2}）を避けた案: {final_draft}。"
            f" 裏付け: {case_summary}。"
        )


class RunawayDecider:
    """常にツールを呼び続ける（max_steps ガードレール検証用）。"""

    def decide(
        self, goal: str, tools: list[ToolSpec], history: list[Observation]
    ) -> Decision:
        return ToolCall("search_past_cases", {"query": "loop"})


class ExpensiveDecider:
    """高コストツールを呼び続ける（cost_cap ガードレール検証用）。"""

    def decide(
        self, goal: str, tools: list[ToolSpec], history: list[Observation]
    ) -> Decision:
        return ToolCall("expensive_tool", {"query": "x"})


__all__ = [
    "CheckMailConstraintsSkill",
    "DraftMeasureSkill",
    "ExpensiveDecider",
    "GetClientHistorySkill",
    "HighCostSkill",
    "RunawayDecider",
    "ScenarioDecider",
    "SearchPastCasesSkill",
    "scenario_tools",
]
