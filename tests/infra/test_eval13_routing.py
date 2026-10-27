"""実機の依頼を別成果物へ読み替えず、未提供のリンクを創作しない規約。"""

from pathlib import Path

SOUL = Path(__file__).resolve().parents[2] / "infra/openclaw/SOUL.md"


def test_proposal_request_is_not_souvenir_or_outline():
    text = SOUL.read_text()
    section = text.split("## 資料・レポートをつくる依頼", 1)[1].split("\n## ", 1)[0]
    assert "「提案資料」「提案書」「統合提案書」は `proposal_builder_submit`" in section
    assert "お土産資料へ読み替えない" in section
    assert "競合・キーワードが「お任せ」なら自分で選び、聞き返さない" in section
    assert "公式URL・投稿開始日" in section
    assert "投稿開始日は省略可" in section
    assert "投稿開始日が無ければnullで渡し" in section
    assert "「未定」で作成する" in section
    assert "投稿開始日が未指定です" not in section
    assert "ツールが無い場合は「提案書PPTXの自動作成はこの環境では準備中です。」の一文" in section
    assert "骨子（`proposal_draft`）＋お土産資料を案内する" not in section


def test_slack_usage_only_uses_real_help_links():
    text = SOUL.read_text()
    assert "Slack の使い方は App Home" in text
    assert (
        "使い方を聞かれたら [Aico の使い方](https://vector-workspcae.slack.com/docs/T07MU5P2PBR/F0CCGL1SFPW) を付ける"
        in text
    )
    assert "使い方の固定URLは未確認" not in text
    assert "ヘルプやキャンバスのURLを作らない" in text
    assert "使い始めるには、このDMに「連携」" in text
