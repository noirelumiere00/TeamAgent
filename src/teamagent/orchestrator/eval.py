"""オーケストレーション評価（Phase 4）: ゴールドセット ＋ 純粋な採点ロジック。

「期待ツール列を踏むか／禁止手法を避けるか／反復上限内か」を **決定的に採点**する。
採点は純関数（`score_case`）なので課金ゼロ・CI で回せる。実 Bedrock で実際の `tool_calls`
を取る部分（`scripts/eval_orchestration.py`）だけが課金対象（手動/nightly）。

DoD（PROGRESS_AND_NEXT_PLAN.md Phase 4）:
- 期待ツール列の充足率・禁止回避率・反復上限の数値化。
- CI は決定的 mock のみ（課金ゼロ）。実 Bedrock eval は手動。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GoldCase:
    """1 つの評価ケース。goal に対して期待/禁止するツール挙動を宣言的に定義する。"""

    id: str
    goal: str
    expect_all: tuple[str, ...] = ()  # これら全ツールが呼ばれるべき
    expect_any: tuple[str, ...] = ()  # これらのうち最低 1 つは呼ばれるべき
    forbid: tuple[str, ...] = ()  # これらは呼ばれてはいけない
    max_turns: int = 8
    needs_flags: tuple[str, ...] = ()  # 実行に必要な env フラグ（例: USE_MAIL_TOOLS）
    note: str = ""


@dataclass(frozen=True)
class CaseScore:
    """1 ケースの採点結果。"""

    case_id: str
    passed: bool
    missing_required: tuple[str, ...]  # expect_all のうち呼ばれなかったもの
    missing_any: bool  # expect_any を 1 つも満たさなかった
    forbidden_called: tuple[str, ...]  # forbid なのに呼ばれたもの
    over_turns: bool  # max_turns 超過
    tools_called: tuple[str, ...]  # 実際に呼ばれたツール（重複除去・順序保持）
    num_turns: int

    @property
    def reasons(self) -> tuple[str, ...]:
        """不合格理由を人間可読で返す（合格なら空）。"""
        out: list[str] = []
        if self.missing_required:
            out.append(f"未実行(必須): {', '.join(self.missing_required)}")
        if self.missing_any:
            out.append("expect_any を1つも満たさず")
        if self.forbidden_called:
            out.append(f"禁止ツール使用: {', '.join(self.forbidden_called)}")
        if self.over_turns:
            out.append(f"反復上限超過(num_turns={self.num_turns})")
        return tuple(out)


def score_case(case: GoldCase, tool_calls: list[str], num_turns: int) -> CaseScore:
    """ケースを採点する（純関数・決定的）。

    tool_calls はエージェントが呼んだツール名の列（順序つき・重複あり）。
    """
    called: tuple[str, ...] = tuple(dict.fromkeys(tool_calls))  # 重複除去・順序保持
    called_set = set(called)

    missing_required = tuple(t for t in case.expect_all if t not in called_set)
    missing_any = bool(case.expect_any) and not (set(case.expect_any) & called_set)
    forbidden_called = tuple(t for t in case.forbid if t in called_set)
    over_turns = num_turns > case.max_turns

    passed = not missing_required and not missing_any and not forbidden_called and not over_turns
    return CaseScore(
        case_id=case.id,
        passed=passed,
        missing_required=missing_required,
        missing_any=missing_any,
        forbidden_called=forbidden_called,
        over_turns=over_turns,
        tools_called=called,
        num_turns=num_turns,
    )


def summarize(scores: list[CaseScore]) -> dict[str, object]:
    """ケース採点の集計（合格率・不合格 id）。"""
    n = len(scores)
    passed = sum(1 for s in scores if s.passed)
    return {
        "total": n,
        "passed": passed,
        "pass_rate": round(passed / n, 3) if n else 0.0,
        "failed_ids": [s.case_id for s in scores if not s.passed],
    }


# ── ゴールドセット（最低 10 本・実スキルに対応）─────────────────────────────
# expect_* は「ツール選択（ルーティング）が妥当か」を測る部分集合。LLM の自由度を
# 残すため過度に縛らない（expect_all は最小限、forbid は明確に不要なものだけ）。
GOLD_CASES: tuple[GoldCase, ...] = (
    GoldCase(
        id="search_winpatterns",
        goal="ショート動画施策で成果が出た過去事例の勝ちパターンを、根拠資料つきで3つ教えて",
        expect_all=("search",),
        max_turns=6,
        note="純粋な検索・グラウンディング。",
    ),
    GoldCase(
        id="search_pricing",
        goal="ショート動画施策の料金・パッケージ設計の実例を、根拠資料つきで知りたい",
        expect_all=("search",),
        max_turns=6,
        note="料金の実データ引き当て。",
    ),
    GoldCase(
        id="client_history",
        goal="森ビルの提案履歴・温度感・次アクションを時系列で教えて",
        expect_all=("clientkarte",),
        max_turns=6,
        note="クライアント・カルテ。",
    ),
    GoldCase(
        id="research_only_no_draft",
        goal="競合のショート動画事例を調べて要約だけして。提案ドラフトは作らないで",
        expect_all=("search",),
        forbid=("proposal_draft", "proposal_review"),
        max_turns=6,
        note="調査のみ。明示的に提案不要。",
    ),
    GoldCase(
        id="propose_with_review",
        goal="森ビル向けに次の施策を提案して、過去の勝ち筋と照らしてレビューもして",
        expect_all=("proposal_draft", "proposal_review"),
        expect_any=("search", "clientkarte"),
        max_turns=8,
        note="ドラフト→レビューの多段。",
    ),
    GoldCase(
        id="full_adaptive_propose",
        goal="森ビルへ次施策を提案して。過去の失敗も踏まえて根拠つきで",
        expect_all=("proposal_draft",),
        expect_any=("clientkarte", "search"),
        max_turns=8,
        note="履歴/検索→提案の適応。",
    ),
    GoldCase(
        id="review_existing_idea",
        goal="『インフルエンサータイアップで認知拡大』という案を、過去の勝ち筋/失注理由と照合して診断して",
        expect_all=("proposal_review",),
        # 注: proposal_review は内部で過去事例を pgvector+rerank で検索（self-grounding）する。
        # よって別途 search を必須にしない（2026-06-03 実eval で agent は proposal_review 単独で
        # 根拠つき診断を生成。当初の expect_any=("search",) は誤りだったため除去）。
        max_turns=8,
        note="既存案のレビュー。proposal_review が自前で過去事例を引くため search は任意。",
    ),
    GoldCase(
        id="client_then_propose",
        goal="INPEX向けの提案を、過去の提案履歴を踏まえて作って",
        expect_all=("proposal_draft",),
        expect_any=("clientkarte", "search"),
        max_turns=8,
        note="履歴→提案。",
    ),
    GoldCase(
        id="ng_swap_with_mail",
        goal="A社へCV施策を提案して。MailにNGがあればそれを避けて別案にして、根拠も添えて",
        expect_all=("mail_constraints", "proposal_draft"),
        expect_any=("search", "clientkarte"),
        max_turns=10,
        needs_flags=("USE_MAIL_TOOLS",),
        note="Mail制約→差替の本命シナリオ（要 USE_MAIL_TOOLS=1）。",
    ),
    GoldCase(
        id="mail_constraint_only",
        goal="B社についてMailで合意済み/NGになっている制約を整理して。提案はまだ要らない",
        expect_all=("mail_constraints",),
        forbid=("proposal_draft", "proposal_review"),
        max_turns=6,
        needs_flags=("USE_MAIL_TOOLS",),
        note="制約整理のみ（要 USE_MAIL_TOOLS=1）。",
    ),
)


__all__ = [
    "GOLD_CASES",
    "CaseScore",
    "GoldCase",
    "score_case",
    "summarize",
]
