"""LLM 出力の装飾正規化（strip_ai_decoration）テスト。

利用者指摘（2026-09-11）: Slack に届く回答が `**太字**` と `—` だらけで AI 生成感が強い。
このテストは「ダッシュ類は読点になる」だけでなく「壊してはいけないものが無傷である」ことを
固定する。後者が本体で、URL・コードブロック・Slack リンク記法を壊した時点でこの後処理は
失格になる。`**強調**` も壊してはいけない側に入る: 配信側の Markdown→mrkdwn 変換が
strong を Slack の太字へ描画するため、ここで畳む/落とすと送信面を毀損する。
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


# ── ダッシュ類が読点になること / 強調は残ること ─────────────────────────────


def test_reported_answer_loses_dashes_but_keeps_bold() -> None:
    """実例テキストから `—` `--` が 0 件になる。`**` は上流が太字へ変換するので残す。"""
    out = strip_ai_decoration(REPORTED_ANSWER)
    assert "—" not in out
    assert "--" not in out
    assert "**直接回答**" in out


def test_reported_answer_keeps_numbers_and_proper_nouns() -> None:
    """装飾を落としても、価値である数字と固有名詞は残る。"""
    out = strip_ai_decoration(REPORTED_ANSWER)
    assert "84万回再生（アクセンチュア）" in out
    assert "1日密着型" in out
    assert "高速様採用向け切り抜き" in out


def test_bold_markers_are_left_untouched() -> None:
    """`**強調**` は畳まない・落とさない（上流が strong を Slack の太字へ変換するため）。

    配信側の Markdown→mrkdwn 変換は bold=`*` / italic=`_` のマーカーで描画する
    （@openclaw/slack 2026.7.1 の buildSlackRenderOptions。repo 内では
    infra/openclaw/SOUL.md と tests/infra/test_soul_contract.py が同じ仕様を固定）。
    `**` を 1 個へ畳めば太字が斜体に化け、記号ごと落とせば太字が地の文に潰れる。
    """
    src = "これは**太字**です"
    assert strip_ai_decoration(src) == src


def test_existing_slack_bold_is_untouched() -> None:
    """既に Slack 正の `*bold*` になっているものを壊さない。"""
    src = "これは*太字*です"
    assert strip_ai_decoration(src) == src


@pytest.mark.parametrize(
    "src",
    [
        "a***x***b",  # bold+italic（上流では `*_x_*`）
        "a****x****b",
        "a**x*y**b",  # 入れ子まがい
        "**",  # 対になっていない
    ],
)
def test_asterisk_runs_pass_through_unchanged(src: str) -> None:
    """`***` `****` も素通しする（変換しないので対の有無で挙動が分岐しない）。

    上流が strong/em を解釈して描画するため、記号の数を repo 側で解釈し直さない。
    """
    assert strip_ai_decoration(src) == src


def test_asterisk_only_line_is_still_a_horizontal_rule() -> None:
    """記号しか無い行（`***`）は水平線なので落とす。強調ではないのでここだけは例外。"""
    assert strip_ai_decoration("前段\n\n***\n\n後段") == "前段\n\n後段"


def test_bold_markers_are_counted_not_converted() -> None:
    """`**` は変換しない代わりに残数をログへ出す（プロンプト指示の効き目を測る指標）。

    毀損ではないので警告ではなく観測。対になっているものも数に含める。
    """
    from structlog.testing import capture_logs

    src = "結論は**1日密着型**が効く"
    with capture_logs() as logs:
        out = strip_ai_decoration(src, request_id="req-bold")
    assert out == src
    events = [e for e in logs if e.get("event") == "llm_text_residual_decoration"]
    assert events and events[0]["bold_marker_count"] == 2


def test_power_operator_is_not_folded() -> None:
    """対になっていない `**`（べき乗など）を勝手に 1 個へ畳まない（意味が変わる）。"""
    src = "計算式は 2**3 です"
    assert strip_ai_decoration(src) == src


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
    """URL の中の `--` `—` `、、` は無加工で通す（リンクが壊れる）。"""
    src = "詳細は https://ex.test/a--b—c と https://ex.test/x?q=a、、b を参照"
    assert strip_ai_decoration(src) == src


def test_dashes_touching_a_url_are_still_normalized() -> None:
    r"""URL のある行でも、URL の外の `—` `--` は読点になる（過剰保護で後処理が無効化されない）。

    和文は URL の直後に空白を置かないため、裸 URL を `\S+` で貪欲に取ると行末までが
    保護領域になり、その行だけ後処理が素通しになる（実測）。保護は URL の範囲に留める。
    """
    assert (
        strip_ai_decoration("詳細は https://drive.test/file/d/AAA/view — 要点は3つ。")
        == "詳細は https://drive.test/file/d/AAA/view、要点は3つ。"
    )
    assert (
        strip_ai_decoration("参考: https://ex.test/a、**結論**は価格 -- 以上。")
        == "参考: https://ex.test/a、**結論**は価格、以上。"
    )


@pytest.mark.parametrize(
    "src",
    [
        "**詳細は https://drive.test/file/d/1Ab**",
        "資料は **https://example.test/x** を参照",
        "**A <https://x.test/y|y> B**",
    ],
)
def test_bold_spanning_a_url_never_becomes_asymmetric(src: str) -> None:
    """強調が URL を含む/URL で終わっても、開きだけ書き換えて `*…**` にしない。

    以前は貪欲な裸 URL 保護が閉じ `**` を URL の一部として飲み込み、開きだけが畳まれて
    入力より壊れたマークアップになっていた。いまは `**` を一切変換しないので、
    この非対称な壊れ方が構造的に起こり得ない（＝入力がそのまま残る）。
    """
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


@pytest.mark.parametrize(
    "delim",
    [
        "| --- | --- |",
        "|---|---|",
        "| :--- | ---: |",
        "| :---: | --- | ---: |",
        "｜ --- ｜ --- ｜",  # 全角の縦棒（和文 LLM が書く形）
        "|　---　|　---　|",  # 全角空白
    ],
)
def test_markdown_table_delimiter_row_survives(delim: str) -> None:
    """表の区切り行を読点にしない（`|、|、|` に化けて表が壊れる。実測）。

    新しい v2d は固定テンプレを廃し表を禁じていないので、比較質問で 2 列表が返るのは自然。
    answer は connect_web が textContent で、slack_bot が mrkdwn でそのまま出す。
    """
    src = f"| 項目 | 内容 |\n{delim}\n| 予算 | 300万円 |"
    assert strip_ai_decoration(src) == src


def test_fenced_code_block_keeps_japanese_punctuation() -> None:
    """フェンスの中の `、、`（CSV の空列など）を畳まない。

    句読点の整形を join 後の全文にかけると、行単位の保護を通らずフェンスの中まで
    書き換わり、CSV 見本が別物になっていた（実測）。
    """
    src = "サンプル:\n\n```\n名前、、金額\n山田、、1000\n```\n\n以上"
    assert strip_ai_decoration(src) == src


def test_inline_code_keeps_japanese_punctuation() -> None:
    src = "コードは `a、、b` です"
    assert strip_ai_decoration(src) == src


def test_url_query_keeps_japanese_punctuation() -> None:
    """URL のクエリの `、、` を畳まない（畳むとリンク先が変わる）。"""
    src = "資料 https://ex.test/x?q=a、、b を参照"
    assert strip_ai_decoration(src) == src


def test_untouched_line_keeps_author_punctuation() -> None:
    """置換が起きていない行の `、、` は書き手の意図なので触らない。"""
    src = "見本は 名前、、金額 の形です。"
    assert strip_ai_decoration(src) == src


@pytest.mark.parametrize(
    "src",
    [
        "再生数は 80—100 万でした。",
        "参考は 2024—2025 年の実績です。",
        "提案書—A社版.pptx を見てください。",
    ],
)
def test_em_dash_ranges_and_names_survive(src: str) -> None:
    """空白を伴わず英数字/固有名詞に接する `—` は範囲・名前なので読点にしない。

    `80—100` を `80、100` にするのは意味を変える置換で、モジュール冒頭の掟に反する。
    """
    assert strip_ai_decoration(src) == src


@pytest.mark.parametrize(
    "src",
    [
        "コマンドは uv run -- pytest です",  # 引数終端トークン
        "期間は 2024 -- 2025 です",  # 年レンジ
    ],
)
def test_spaced_double_hyphen_in_ascii_context_survives(src: str) -> None:
    """空白で囲まれた `--` でも、両側が和文でないものは触らない。"""
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


def test_control_characters_do_not_raise() -> None:
    """保護領域の退避に使う制御文字が入力に混じっていても例外にしない。

    ここで例外を出すと、装飾を直すための層が回答そのものを落とすことになる。
    """
    src = "資料 https://ex.test/a \x001\x01 と **太字** — 以上"
    out = strip_ai_decoration(src)
    assert "https://ex.test/a" in out
    assert "**太字**" in out
    assert "、以上" in out


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
