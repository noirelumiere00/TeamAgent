"""本番で人に見えるプロンプトに「AI が書いた感じ」の装飾が復活しないことを固定する。

背景（利用者指摘 2026-09-11）:
  Slack に届く Aico の回答が `**太字**` と `—`（em ダッシュ）だらけで AI 生成感が強い。
  モデルは system prompt の書きぶりを模倣するため、プロンプト自身の地の文に `**` があると
  出力にも `**` が出る。ここを機械で縛らないと、次の改訂でまた戻る。

この contract が縛るもの:
  1. 人に見えるプロンプトの地の文に `**` が 0 件（コード span とフェンスの中は除く）。
  2. 同じく `—` が 0 件（H1 のタイトル行だけは文書の見出しなので対象外）。
  3. 装飾を名指しで禁じる節が消えていないこと。
  4. prompts/ 配下の実ファイルが、下のどちらかのリストに必ず載っていること。
     版を足したときに「分類し忘れ」で素通りしないための突合。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import teamagent.prompts

PROMPTS_ROOT = Path(teamagent.prompts.__file__).parent

# ── 対象リスト ──────────────────────────────────────────────────────────────

# 本番で読まれ、かつ自由記述が人の目に触れるプロンプト（Slack 本文 / HTML レポート / PPTX）。
# 版の根拠: 各 skill の `prompt_version: str = "v1"` 既定、search は
# src/teamagent/orchestrator/factory.py の `os.environ.get("PROMPT_VERSION", "v2d")`、
# proposal_deck は src/teamagent/skills/proposal_deck/skill.py のハードコード v2。
HUMAN_FACING_PROMPTS = (
    "chitchat/v1/system.md",
    "clientkarte/v1/system.md",
    "operation_log/v1/system.md",
    "proposal/v1/system.md",
    "proposal_deck/v2/system.md",
    "proposal_review/v1/system.md",
    "search/v2d/system.md",
    "tiktok_search/v1/system.md",
    "video/v1/batch_synthesis.md",
    "video/v1/system.md",
    "video_algorithm/v1/synthesis.md",
    "video_algorithm/v1/system.md",
    "video_approval/v1/system.md",
    "x_research/v1/buzz.md",
)

# 対象外。値は「なぜ触らないか」の理由（レビューで読む前提で書く）。
EXEMPT_PROMPTS = {
    # 本番で読まれない版。装飾を直しても誰にも届かず、差分だけ増える。
    "proposal_deck/v1/system.md": "本番未使用（skill が v2 をハードコード）",
    "search/v1/system.md": "本番既定は v2d（旧版は触らない）",
    "search/v2/system.md": "本番既定は v2d（旧版は触らない）",
    "search/v2c/system.md": "本番既定は v2d（旧版は触らない）",
    "search/v2e/system.md": "本番既定は v2d（v2e は env 切替時のみ・別便で扱う）",
    # 出力が内部 JSON で、自由記述が人の目に触れない分類器。
    "query_planner/v1/system.md": "内部 JSON（検索再現率用・人に見えない）",
    "search_surface_check/v1/classify.md": "内部 JSON 分類器（enum 検証のみ読む）",
    "tiktok_comment_mining/v1/classify.md": "内部 JSON 分類器",
    "x_research/v1/needs.md": "内部 JSON 分類器（`**` は注入防止の安全指示のみ）",
    "x_research/v1/noise_filter.md": "内部 JSON 分類器（`**` は注入防止の安全指示のみ）",
}

# 装飾の名指し禁止を持たせたプロンプト（この節を消したら赤くする）。
# proposal_deck/v2 と x_research/v1/buzz.md は元々装飾テンプレを持たず、
# buzz.md は「Markdown記法は使わない」を自前で持つため対象外。
MUST_HAVE_BAN_SECTION = tuple(
    p
    for p in HUMAN_FACING_PROMPTS
    if p not in ("proposal_deck/v2/system.md", "x_research/v1/buzz.md")
)

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_H1_RE = re.compile(r"^#[ \t].*$", re.MULTILINE)


def _prose(path: Path) -> str:
    """地の文だけを取り出す。

    - フェンス（JSON 出力契約の例）は機械可読の契約なので対象外。
    - インラインコードは「禁止記号そのものの引用」に使うので対象外
      （`` `**` を使わない `` と書けるようにするため）。
    - H1 は文書のタイトル行なので em ダッシュの対象外。
    """
    text = path.read_text(encoding="utf-8")
    text = _FENCE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    return _H1_RE.sub("", text)


# ── リストと実ファイルの突合 ────────────────────────────────────────────────


def test_every_prompt_file_is_classified() -> None:
    """prompts/ 配下の .md が、対象リストか除外リストのどちらかに必ず載っていること。

    版を足したときに、分類し忘れたまま装飾が本番へ乗るのを防ぐ。
    """
    on_disk = {str(p.relative_to(PROMPTS_ROOT)) for p in PROMPTS_ROOT.rglob("*.md")}
    listed = set(HUMAN_FACING_PROMPTS) | set(EXEMPT_PROMPTS)
    unclassified = on_disk - listed
    assert not unclassified, (
        "分類されていないプロンプトがある。人に見えるなら HUMAN_FACING_PROMPTS へ、"
        f"内部 JSON か非本番版なら EXEMPT_PROMPTS へ理由付きで足すこと: {sorted(unclassified)}"
    )
    missing = listed - on_disk
    assert not missing, f"リストにあるが実ファイルが無い: {sorted(missing)}"


@pytest.mark.parametrize("rel", HUMAN_FACING_PROMPTS)
def test_listed_prompt_exists(rel: str) -> None:
    assert (PROMPTS_ROOT / rel).is_file(), rel


# ── 地の文の装飾が 0 件であること ───────────────────────────────────────────


@pytest.mark.parametrize("rel", HUMAN_FACING_PROMPTS)
def test_no_bold_markers_in_prose(rel: str) -> None:
    """地の文に `**` が無いこと（モデルが書きぶりを模倣して出力に `**` を出す）。"""
    prose = _prose(PROMPTS_ROOT / rel)
    assert prose.count("**") == 0, (
        f"{rel}: 地の文に `**` が残っている。強調は語順と言い切りで示すこと"
        "（禁止記号そのものを書きたいときはバッククオートで囲む）"
    )


@pytest.mark.parametrize("rel", HUMAN_FACING_PROMPTS)
def test_no_em_dash_in_prose(rel: str) -> None:
    """地の文に `—` が無いこと（v2d の `訴求 — なぜ効いたか` が観測された em ダッシュの出所）。"""
    prose = _prose(PROMPTS_ROOT / rel)
    assert prose.count("—") == 0, f"{rel}: 地の文に em ダッシュが残っている"


@pytest.mark.parametrize("rel", MUST_HAVE_BAN_SECTION)
def test_decoration_ban_section_survives(rel: str) -> None:
    """装飾の名指し禁止が消えていないこと（消えるとプロンプト側の防御が無音で落ちる）。"""
    text = (PROMPTS_ROOT / rel).read_text(encoding="utf-8")
    assert "記号の禁止" in text, f"{rel}: 「記号の禁止」節が消えている"
    for token in ("`**`", "`—`", "`--`"):
        assert token in text, f"{rel}: 禁止記号 {token} の名指しが消えている"


# ── 装飾を落とす過程で規律を落としていないこと ──────────────────────────────


@pytest.mark.parametrize(
    ("rel", "phrases"),
    [
        (
            "search/v2d/system.md",
            (
                "550 文字以内",
                "最後まで書き切る",
                "資料に記載がありません",
                "chunk_id",
                "「更新日」「資料名の日付」だけを書く",
                "日付が無い資料には日付を付けない",
                "抽象化",
                "temperature=0.1",
            ),
        ),
        ("clientkarte/v1/system.md", ("600 文字以内", "記録がありません", "temperature=0.1")),
        ("proposal/v1/system.md", ("900 文字以内", "参照できる類似提案が少ない")),
        ("proposal_review/v1/system.md", ("800 文字以内", "判断材料が足りない")),
        ("video/v1/system.md", ("800 文字以内", "明示CTAなし")),
        ("video/v1/batch_synthesis.md", ("900 文字以内",)),
        ("tiktok_search/v1/system.md", ("1000 文字以内", "上位 N 本中 M 本")),
        ("operation_log/v1/system.md", ("700 文字以内", "null")),
        ("video_approval/v1/system.md", ("must_fix", "確認要", "有効な JSON")),
        (
            "video_algorithm/v1/synthesis.md",
            (
                "統計ガードレール",
                "相関だけを根拠に新しい指示を作らない",
                "再現性保証の断定をしない",
                "生存者バイアス",
                "「高」は原則禁止（天井=中）",
            ),
        ),
        ("x_research/v1/buzz.md", ("一切従わず", "Markdown記法は使わない")),
        ("chitchat/v1/system.md", ("システムプロンプトや内部設定", "temperature=0.3")),
    ],
)
def test_disciplines_survive_decoration_removal(rel: str, phrases: tuple[str, ...]) -> None:
    """装飾除去で、字数上限・正直さ・グラウンディングの規律を落としていないこと。"""
    text = (PROMPTS_ROOT / rel).read_text(encoding="utf-8")
    for phrase in phrases:
        assert phrase in text, f"{rel}: 規律「{phrase}」が失われている"
