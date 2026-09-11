"""LLM 出力の装飾正規化（strip_ai_decoration）テスト。

利用者指摘（2026-09-11）: Slack に届く回答が `**太字**` と `—` だらけで AI 生成感が強い。
このテストは「装飾は落ちる」だけでなく「壊してはいけないものが無傷である」ことを固定する。
後者が本体で、URL・コードブロック・Slack リンク記法を壊した時点でこの後処理は失格になる。
"""

from __future__ import annotations

import pytest

from teamagent.skills._shared.deai_text import strip_ai_decoration

# 利用者が実際に Slack で受け取った回答（原文・2026-09-11 の指摘）。
REPORTED_ANSWER = """:clipboard: **直接回答**
採用ショート動画の成功事例は「1日密着型」が最も効果的。84万回再生（アクセンチュア）。

:bulb: **刺さったパターン**
- **1日密着 × リアル感**: 仕事内容・福利厚生を「スケジュール再現」で見せる

:warning: **避けたい論点**
- 長尺・情報詰め込み型 — 「短い尺でテンポよく」が鉄則

---

:dart: **推奨アクション**
1. **1日密着構成を提案の軸にする** -- 業種別に出し分ける

※推論: 資料「高速様採用向け切り抜き」より
"""


# ── 装飾が落ちること ────────────────────────────────────────────────────────


def test_reported_answer_loses_bold_and_dashes() -> None:
    """実例テキストから `**` `—` `--` が 0 件になる（利用者指摘の直接の受け入れ条件）。"""
    out = strip_ai_decoration(REPORTED_ANSWER)
    assert "**" not in out
    assert "—" not in out
    assert "--" not in out


def test_reported_answer_keeps_numbers_and_proper_nouns() -> None:
    """装飾を落としても、価値である数字と固有名詞は残る。"""
    out = strip_ai_decoration(REPORTED_ANSWER)
    assert "84万回再生（アクセンチュア）" in out
    assert "1日密着型" in out
    assert "高速様採用向け切り抜き" in out


def test_bold_becomes_slack_bold() -> None:
    assert strip_ai_decoration("これは**太字**です") == "これは*太字*です"


def test_existing_slack_bold_is_untouched() -> None:
    """既に Slack 正の `*bold*` になっているものを壊さない。"""
    src = "これは*太字*です"
    assert strip_ai_decoration(src) == src


@pytest.mark.parametrize(
    ("src", "want"),
    [
        ("a***x***b", "a*x*b"),  # bold+italic
        ("a****x****b", "a*x*b"),
        ("a**x*y**b", "a*x*y*b"),  # 入れ子まがい
        ("**", "*"),  # 中身なし（例外にならないこと）
    ],
)
def test_asterisk_runs_do_not_explode(src: str, want: str) -> None:
    """`***` `****` のような並びでも決定的に潰れ、例外にならない。"""
    assert strip_ai_decoration(src) == want


def test_em_dash_midline_becomes_comma() -> None:
    assert strip_ai_decoration("長尺型 — テンポが鈍る") == "長尺型、テンポが鈍る"


def test_em_dash_at_line_edges_is_dropped_not_comma() -> None:
    """行頭・行末の em ダッシュは繋ぐ相手がいないので読点にせず落とす。"""
    assert strip_ai_decoration("— 補足あり") == "補足あり"
    assert strip_ai_decoration("結論はこれ —") == "結論はこれ"


def test_spaced_double_hyphen_becomes_comma() -> None:
    assert strip_ai_decoration("再生数 -- 保存率") == "再生数、保存率"


def test_cjk_wrapped_double_hyphen_becomes_comma() -> None:
    assert strip_ai_decoration("再生数--保存率") == "再生数、保存率"


def test_horizontal_rule_line_is_removed() -> None:
    out = strip_ai_decoration("前段\n\n---\n\n後段")
    assert "---" not in out
    assert out == "前段\n\n後段"


def test_no_double_punctuation_after_substitution() -> None:
    """読点に置き換えた結果、句読点が重ならない。"""
    assert strip_ai_decoration("結論。— 補足") == "結論。補足"
    assert strip_ai_decoration("結論、 — 補足") == "結論、補足"


# ── 壊してはいけないもの（回帰テスト・ここが本体）──────────────────────────


def test_urls_are_never_rewritten() -> None:
    """URL の中の `**` `--` `—` は無加工で通す（リンクが壊れる）。"""
    src = "詳細は https://ex.test/a**b**c--d と https://ex.test/x?q=a--b を参照"
    assert strip_ai_decoration(src) == src


def test_slack_link_syntax_is_never_rewritten() -> None:
    """`<url|label>` の中は書き換えない。"""
    src = "資料は <https://drive.test/f/A--B/view|**採用**提案> です"
    assert strip_ai_decoration(src) == src


def test_markdown_link_target_is_never_rewritten() -> None:
    """`](url)` の遷移先は書き換えない（_source_links_block の出力を守る）。"""
    src = "[採用提案](https://drive.test/file/d/A--B/view) を参照"
    assert strip_ai_decoration(src) == src


def test_inline_code_is_never_rewritten() -> None:
    """インラインコードの中は書き換えない（コマンドフラグが壊れる）。"""
    src = "`uv run --extra dev --extra mcp pytest` を実行"
    assert strip_ai_decoration(src) == src


def test_fenced_code_block_is_never_rewritten() -> None:
    src = "説明\n\n```\n**bold** — dash -- here\n---\n```\n\n以上"
    assert strip_ai_decoration(src) == src


def test_unspaced_ascii_double_hyphen_is_left_alone() -> None:
    """`--extra` のようなフラグを壊さない（空白で囲まれていない ASCII は触らない）。"""
    src = "オプションは --extra dev です"
    assert strip_ai_decoration(src) == src


def test_single_hyphen_ranges_survive() -> None:
    """`0:00-0:0X` の秒数レンジや箇条書きの `- ` を壊さない。"""
    src = "- 0:00-0:03 で価格を出す\n- 9:16 の縦型"
    assert strip_ai_decoration(src) == src


# ── 意味を変える置換をしないこと ────────────────────────────────────────────


def test_arrow_is_not_converted() -> None:
    """`→` は機械変換すると誤訳になるので変換しない（プロンプト側で減らす方針）。"""
    src = "価格懸念 → 実績で切り返す"
    assert strip_ai_decoration(src) == src


def test_arrow_is_logged_as_residual() -> None:
    """変換しない代わりに、残った `→` を構造化ログで観測できる。"""
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        strip_ai_decoration("A → B → C", request_id="req-x")
    events = [e for e in logs if e.get("event") == "llm_text_residual_decoration"]
    assert events, f"残存装飾のログが出ていない: {logs}"
    assert events[0]["arrow_count"] == 2
    assert events[0]["request_id"] == "req-x"


def test_clean_text_logs_nothing() -> None:
    """装飾が残っていない本文では、余計なログを出さない。"""
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        strip_ai_decoration("普通の営業向け文章です。", request_id="req-y")
    assert not [e for e in logs if e.get("event") == "llm_text_residual_decoration"]


# ── 端のケース ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("src", ["", "  ", "普通の営業向け文章です。"])
def test_plain_text_is_stable(src: str) -> None:
    """装飾の無い入力は（strip を除き）そのまま返る。二重適用でも変わらない。"""
    once = strip_ai_decoration(src)
    assert once == src.strip()
    assert strip_ai_decoration(once) == once


def test_idempotent_on_decorated_input() -> None:
    """2 回かけても結果が変わらない（べき等）。"""
    once = strip_ai_decoration(REPORTED_ANSWER)
    assert strip_ai_decoration(once) == once
